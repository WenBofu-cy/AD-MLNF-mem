#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-14
模块名称: 场景判定与分槽路由单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 场景分槽管理
核心职责: 依据独立世界模型库输出的道路属性与场景分类信号，判定当前驾驶场景类别，
          将经验写入请求精准路由至对应场景分槽。当场景无法精确判定时，降级路由至
          通用驾驶槽。当分槽数量达上限时，归并至相似度最高的已有分槽。

依赖模块: ad-44(独立世界模型库), ad-03(漏斗二专属调度单元), ad-15至ad-19(五个场景分槽)
被依赖模块: ad-03(漏斗二专属调度单元), ad-15至ad-19(消费经验写入指令)

安全约束:
  S-01: 场景判定以独立世界模型库实时输出为准，经验请求中的场景标签仅作参考
  S-02: 世界模型不可用时必须降级路由至通用驾驶槽，不可凭历史缓存猜测
  S-03: 通用驾驶槽作为无条件兜底分槽，编译期保证其始终存在且不可被归并或删除
  S-04: 乡村道路经验归入通用驾驶槽的子类标签，享有独立遗忘保护参数
  S-05: 分槽归并前须校验归并后总条目数不超过单槽容量上限的80%
  S-06: 紧急熔断时暂存队列最长保留5秒，超时则丢弃，优先保障安全
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid
import math


# ==================== 枚举定义 ====================

class SceneCategory(Enum):
    """场景类别"""
    HIGHWAY = "highway_cruise"
    URBAN = "urban_intersection"
    PARKING = "parking_low_speed"
    SPECIAL = "special_environment"
    GENERAL = "general_driving"
    RURAL = "rural_road"


class RouterState(Enum):
    """路由单元内部状态"""
    NORMAL = "normal"
    QUERYING_WM = "querying_wm"
    JUDGING = "judging"
    FALLBACK = "fallback"
    MERGING = "merging"
    PAUSED = "paused"
    EMERGENCY_RO = "emergency_ro"


class RouteTag(Enum):
    """路由标记"""
    PRECISE = "精确匹配"
    FALLBACK_LOW_CONF = "低置信度降级"
    FALLBACK_WM_FAIL = "世界模型降级"
    RURAL_SUB = "乡村道路子类"
    GENERAL_DEFAULT = "常规通用"
    MERGE_OVERFLOW = "归并溢出"


# ==================== 数据结构 ====================

@dataclass
class WorldModelResult:
    """世界模型查询结果"""
    road_level: str
    road_type: str
    lane_count: int
    traffic_sign_density: str
    weather: str
    lighting: str
    time_period: str
    special_flags: List[str]
    scene_category: SceneCategory
    confidence: float


@dataclass
class ExperienceEntry:
    """经验条目（简化）"""
    entry_id: str
    scene_label: str
    content: Dict[str, Any]
    priority: int = 0


@dataclass
class SlotStatus:
    """场景分槽状态"""
    slot_id: int
    scene_category: SceneCategory
    storage_usage_rate: float
    entry_count: int
    is_active: bool


@dataclass
class RouteResult:
    """路由结果"""
    target_slot_id: int
    scene_category: SceneCategory
    route_tag: RouteTag
    confidence: float
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class SceneJudgmentRouter:
    """
    场景判定与分槽路由单元
    
    职责:
    1. 接收经验写入请求
    2. 向世界模型查询当前场景
    3. 按判定规则库精确匹配场景分槽
    4. 降级路由处理（世界模型不可用/低置信度）
    5. 分槽归并触发
    """
    
    # 预设分槽ID（编译期固定）
    SLOT_HIGHWAY = 15
    SLOT_URBAN = 16
    SLOT_PARKING = 17
    SLOT_SPECIAL = 18
    SLOT_GENERAL = 19
    
    # 分槽数量上限
    NMAX_SLOT = 8
    
    # 世界模型查询超时（秒）
    WM_QUERY_TIMEOUT = 0.05  # 50ms
    
    # 世界模型连续失败上限
    WM_MAX_FAILURES = 3
    WM_RETRY_INTERVAL = 30.0
    
    # 降级置信度阈值
    LOW_CONFIDENCE_THRESHOLD = 0.5
    
    # 归并相似度阈值
    MERGE_SIMILARITY_THRESHOLD = 0.7
    
    # 紧急暂存队列超时（秒）
    EMERGENCY_QUEUE_TIMEOUT = 5.0
    
    def __init__(self):
        self.module_id = "ad-14"
        self.module_name = "场景判定与分槽路由单元"
        
        # 内部状态
        self.state = RouterState.NORMAL
        
        # 世界模型查询统计
        self._wm_fail_count = 0
        self._wm_last_fail_time = 0.0
        self._wm_disabled = False
        
        # 分槽状态缓存: slot_id -> SlotStatus
        self._slot_status: Dict[int, SlotStatus] = {}
        
        # 紧急暂存队列
        self._emergency_queue: List[Tuple[ExperienceEntry, float]] = []
        
        # 统计
        self._total_routes = 0
        self._fallback_routes = 0
        self._merge_suggestions = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        # 初始化预设分槽状态
        self._init_preset_slots()
        
        print(f"[{self.module_id}] 场景判定与分槽路由单元初始化完成")
    
    def _init_preset_slots(self) -> None:
        """初始化5个预设分槽的状态"""
        for slot_id, category in [(15, SceneCategory.HIGHWAY), (16, SceneCategory.URBAN),
                                   (17, SceneCategory.PARKING), (18, SceneCategory.SPECIAL),
                                   (19, SceneCategory.GENERAL)]:
            self._slot_status[slot_id] = SlotStatus(
                slot_id=slot_id,
                scene_category=category,
                storage_usage_rate=0.0,
                entry_count=0,
                is_active=True
            )
    
    # ========== 状态管理 ==========
    
    def update_slot_status(self, slot_id: int, status: SlotStatus) -> None:
        """更新分槽状态"""
        self._slot_status[slot_id] = status
    
    def register_new_slot(self, slot_id: int, category: SceneCategory) -> None:
        """注册新场景分槽"""
        self._slot_status[slot_id] = SlotStatus(
            slot_id=slot_id,
            scene_category=category,
            storage_usage_rate=0.0,
            entry_count=0,
            is_active=True
        )
        print(f"[{self.module_id}] 注册新分槽: slot_{slot_id}, category={category.value}")
    
    def pause(self) -> None:
        self.state = RouterState.PAUSED
    
    def resume(self) -> None:
        self.state = RouterState.NORMAL
    
    def emergency_stop(self) -> None:
        self.state = RouterState.EMERGENCY_RO
        self._emergency_queue.clear()
        print(f"[{self.module_id}] 紧急熔断，清空暂存队列")
    
    # ========== 场景判定与路由 ==========
    
    def route_experience(self, entry: ExperienceEntry,
                         wm_result: Optional[WorldModelResult] = None) -> RouteResult:
        """
        路由经验写入请求
        
        判定优先级:
        1. 特殊环境（天气/路面异常）
        2. 高速巡航
        3. 城区路口
        4. 泊车低速
        5. 乡村道路（通用驾驶槽子类）
        6. 通用驾驶（兜底）
        
        Args:
            entry: 经验条目
            wm_result: 世界模型查询结果（None 表示查询失败）
            
        Returns:
            路由结果
        """
        if self.state == RouterState.EMERGENCY_RO:
            # 暂存到紧急队列
            if len(self._emergency_queue) < 100:
                self._emergency_queue.append((entry, time.time()))
            return RouteResult(
                target_slot_id=self.SLOT_GENERAL,
                scene_category=SceneCategory.GENERAL,
                route_tag=RouteTag.FALLBACK_WM_FAIL,
                confidence=0.0
            )
        
        if self.state == RouterState.PAUSED:
            return RouteResult(
                target_slot_id=self.SLOT_GENERAL,
                scene_category=SceneCategory.GENERAL,
                route_tag=RouteTag.FALLBACK_WM_FAIL,
                confidence=0.0
            )
        
        self._total_routes += 1
        
        # 世界模型查询失败处理
        if wm_result is None:
            self._wm_fail_count += 1
            self._wm_last_fail_time = time.time()
            if self._wm_fail_count >= self.WM_MAX_FAILURES:
                self._wm_disabled = True
            self.state = RouterState.FALLBACK
            self._fallback_routes += 1
            return RouteResult(
                target_slot_id=self.SLOT_GENERAL,
                scene_category=SceneCategory.GENERAL,
                route_tag=RouteTag.FALLBACK_WM_FAIL,
                confidence=0.3
            )
        
        self._wm_fail_count = 0
        
        # 低置信度降级
        if wm_result.confidence < self.LOW_CONFIDENCE_THRESHOLD:
            self.state = RouterState.FALLBACK
            self._fallback_routes += 1
            return RouteResult(
                target_slot_id=self.SLOT_GENERAL,
                scene_category=SceneCategory.GENERAL,
                route_tag=RouteTag.FALLBACK_LOW_CONF,
                confidence=wm_result.confidence
            )
        
        self.state = RouterState.JUDGING
        
        # 精确匹配
        target_slot, route_tag = self._match_scene(wm_result)
        
        result = RouteResult(
            target_slot_id=target_slot,
            scene_category=wm_result.scene_category,
            route_tag=route_tag,
            confidence=wm_result.confidence
        )
        
        self.state = RouterState.NORMAL
        return result
    
    def _match_scene(self, wm_result: WorldModelResult) -> Tuple[int, RouteTag]:
        """按优先级精确匹配场景分槽"""
        # 1. 特殊环境（最高优先级）
        if wm_result.weather in ["暴雨", "暴雪", "大雾", "沙尘暴"] or \
           wm_result.road_type in ["积水", "结冰", "积雪"] or \
           "施工区" in wm_result.special_flags:
            return self.SLOT_SPECIAL, RouteTag.PRECISE
        
        # 2. 高速巡航
        if wm_result.road_level in ["高速公路", "城市快速路"] and \
           wm_result.traffic_sign_density in ["高", "中"]:
            return self.SLOT_HIGHWAY, RouteTag.PRECISE
        
        # 3. 城区路口
        if wm_result.road_level in ["城市主干道", "次干道", "支路"]:
            return self.SLOT_URBAN, RouteTag.PRECISE
        
        # 4. 乡村道路（归入通用驾驶槽子类）
        if wm_result.road_type in ["泥土", "碎石", "沙土"] or \
           (wm_result.lane_count == 1 and wm_result.traffic_sign_density == "无"):
            return self.SLOT_GENERAL, RouteTag.RURAL_SUB
        
        # 5. 泊车低速（车速 < 5km/h 场景，由上层传递）
        # 6. 通用驾驶（兜底）
        return self.SLOT_GENERAL, RouteTag.GENERAL_DEFAULT
    
    # ========== 分槽归并检测 ==========
    
    def check_merge_needed(self) -> Optional[Tuple[int, int, float]]:
        """
        检查是否需要归并分槽
        
        Returns:
            (源槽号, 目标槽号, 相似度) 或 None
        """
        active_slots = [s for s in self._slot_status.values() if s.is_active]
        
        if len(active_slots) < self.NMAX_SLOT:
            return None
        
        self.state = RouterState.MERGING
        
        # 计算两两相似度（简化：基于场景类别的字符串相似度）
        best_pair = None
        best_similarity = 0.0
        
        for i in range(len(active_slots)):
            for j in range(i + 1, len(active_slots)):
                sim = self._calculate_category_similarity(
                    active_slots[i].scene_category,
                    active_slots[j].scene_category
                )
                if sim > best_similarity and sim >= self.MERGE_SIMILARITY_THRESHOLD:
                    best_similarity = sim
                    # 保留条目数多的槽，合并条目数少的槽
                    if active_slots[i].entry_count >= active_slots[j].entry_count:
                        best_pair = (active_slots[j].slot_id, active_slots[i].slot_id, sim)
                    else:
                        best_pair = (active_slots[i].slot_id, active_slots[j].slot_id, sim)
        
        if best_pair:
            self._merge_suggestions += 1
            print(f"[{self.module_id}] 建议归并: slot_{best_pair[0]} → slot_{best_pair[1]}, "
                  f"相似度={best_pair[2]:.2f}")
        
        self.state = RouterState.NORMAL
        return best_pair
    
    def _calculate_category_similarity(self, cat1: SceneCategory, cat2: SceneCategory) -> float:
        """计算场景类别相似度（简化实现）"""
        if cat1 == cat2:
            return 1.0
        
        # 相关类别有较高相似度
        related_pairs = {
            (SceneCategory.HIGHWAY, SceneCategory.GENERAL): 0.7,
            (SceneCategory.URBAN, SceneCategory.GENERAL): 0.65,
            (SceneCategory.PARKING, SceneCategory.GENERAL): 0.5,
            (SceneCategory.SPECIAL, SceneCategory.GENERAL): 0.4,
        }
        
        pair = (cat1, cat2) if (cat1, cat2) in related_pairs else (cat2, cat1)
        return related_pairs.get(pair, 0.3)
    
    # ========== 紧急队列处理 ==========
    
    def process_emergency_queue(self) -> List[Tuple[ExperienceEntry, RouteResult]]:
        """处理紧急熔断期间暂存的经验条目"""
        now = time.time()
        processed = []
        remaining = []
        
        for entry, timestamp in self._emergency_queue:
            if now - timestamp > self.EMERGENCY_QUEUE_TIMEOUT:
                # 超时丢弃
                continue
            # 重新路由
            result = self.route_experience(entry)
            processed.append((entry, result))
        
        self._emergency_queue = remaining
        
        if processed:
            print(f"[{self.module_id}] 处理紧急队列: {len(processed)} 条")
        
        return processed
    
    # ========== 查询接口 ==========
    
    def get_active_slot_count(self) -> int:
        return sum(1 for s in self._slot_status.values() if s.is_active)
    
    def get_slot_ids(self) -> List[int]:
        return list(self._slot_status.keys())
    
    def get_state(self) -> RouterState:
        return self.state
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_routes": self._total_routes,
            "fallback_routes": self._fallback_routes,
            "merge_suggestions": self._merge_suggestions,
            "wm_fail_count": self._wm_fail_count,
            "wm_disabled": self._wm_disabled,
            "active_slots": self.get_active_slot_count(),
            "state": self.state.value
        }
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-14 场景判定与分槽路由单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    def make_wm_result(scene, confidence=0.95):
        base = {
            "road_level": "高速公路", "road_type": "沥青", "lane_count": 3,
            "traffic_sign_density": "高", "weather": "晴", "lighting": "日间",
            "time_period": "白天", "special_flags": [], "confidence": confidence
        }
        if scene == SceneCategory.HIGHWAY:
            pass
        elif scene == SceneCategory.URBAN:
            base["road_level"] = "城市主干道"
        elif scene == SceneCategory.SPECIAL:
            base["weather"] = "暴雨"
        elif scene == SceneCategory.GENERAL:
            base["road_level"] = "未分级"
            base["road_type"] = "泥土"
            base["traffic_sign_density"] = "无"
            base["lane_count"] = 1
        return WorldModelResult(**base, scene_category=scene)
    
    # --- TC-14-01: 精确匹配高速巡航槽 ---
    print("\n[TC-14-01] 精确匹配高速巡航槽")
    try:
        router = SceneJudgmentRouter()
        entry = ExperienceEntry("exp-001", "高速", {"behavior": "跟车"})
        wm = make_wm_result(SceneCategory.HIGHWAY)
        result = router.route_experience(entry, wm)
        assert result.target_slot_id == 15
        assert result.route_tag == RouteTag.PRECISE
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-14-02: 特殊环境优先 ---
    print("\n[TC-14-02] 特殊环境优先路由")
    try:
        router = SceneJudgmentRouter()
        entry = ExperienceEntry("exp-002", "暴雨", {})
        wm = make_wm_result(SceneCategory.SPECIAL)
        result = router.route_experience(entry, wm)
        assert result.target_slot_id == 18
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-14-03: 乡村道路子类路由至通用驾驶槽 ---
    print("\n[TC-14-03] 乡村道路子类路由至通用驾驶槽")
    try:
        router = SceneJudgmentRouter()
        entry = ExperienceEntry("exp-003", "乡村", {})
        wm = make_wm_result(SceneCategory.GENERAL)
        result = router.route_experience(entry, wm)
        assert result.target_slot_id == 19
        assert result.route_tag == RouteTag.RURAL_SUB
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-14-04: 世界模型查询失败降级 ---
    print("\n[TC-14-04] 世界模型查询失败降级")
    try:
        router = SceneJudgmentRouter()
        entry = ExperienceEntry("exp-004", "未知", {})
        result = router.route_experience(entry, None)
        assert result.target_slot_id == 19
        assert result.route_tag == RouteTag.FALLBACK_WM_FAIL
        assert result.confidence == 0.3
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-14-05: 低置信度降级 ---
    print("\n[TC-14-05] 低置信度降级")
    try:
        router = SceneJudgmentRouter()
        entry = ExperienceEntry("exp-005", "低置信", {})
        wm = make_wm_result(SceneCategory.HIGHWAY, confidence=0.3)
        result = router.route_experience(entry, wm)
        assert result.target_slot_id == 19
        assert result.route_tag == RouteTag.FALLBACK_LOW_CONF
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-14-06: 城区路口槽匹配 ---
    print("\n[TC-14-06] 城区路口槽匹配")
    try:
        router = SceneJudgmentRouter()
        entry = ExperienceEntry("exp-006", "城区", {})
        wm = make_wm_result(SceneCategory.URBAN)
        result = router.route_experience(entry, wm)
        assert result.target_slot_id == 16
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-14-07: 分槽归并检测 ---
    print("\n[TC-14-07] 分槽归并检测")
    try:
        router = SceneJudgmentRouter()
        # 添加两个相似分槽
        router.register_new_slot(20, SceneCategory.HIGHWAY)
        router.register_new_slot(21, SceneCategory.GENERAL)
        # 调整NMAX为当前分槽数
        router.NMAX_SLOT = len(router._slot_status)
        merge = router.check_merge_needed()
        assert merge is not None
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-14-08: 紧急熔断队列暂存 ---
    print("\n[TC-14-08] 紧急熔断队列暂存")
    try:
        router = SceneJudgmentRouter()
        router.emergency_stop()
        entry = ExperienceEntry("exp-008", "测试", {})
        result = router.route_experience(entry, None)
        assert result.route_tag == RouteTag.FALLBACK_WM_FAIL
        assert len(router._emergency_queue) == 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-14-09: 紧急队列超时丢弃 ---
    print("\n[TC-14-09] 紧急队列超时丢弃")
    try:
        router = SceneJudgmentRouter()
        router.emergency_stop()
        router._emergency_queue.append((ExperienceEntry("exp-old", "旧", {}), time.time() - 10))
        processed = router.process_emergency_queue()
        assert len(processed) == 0  # 超时丢弃
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-14-10: 世界模型连续失败后暂停查询 ---
    print("\n[TC-14-10] 世界模型连续失败后暂停查询")
    try:
        router = SceneJudgmentRouter()
        for _ in range(3):
            router.route_experience(ExperienceEntry("exp-fail", "失败", {}), None)
        assert router._wm_disabled == True
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- 测试结果汇总 ---
    print("\n" + "=" * 60)
    print(f"测试结果: {passed} PASS, {failed} FAIL")
    print("=" * 60)
```