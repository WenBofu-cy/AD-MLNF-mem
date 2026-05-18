#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-03
模块名称: 漏斗二专属调度单元 - 自成长经验漏斗管家
所属分区: 一、顶层总控中枢
核心职责: 漏斗二内部场景分槽的创建、激活与经验路由分发。
          依据世界模型输出的道路属性判定场景类别，将 ECC 大脑下发的经验写入请求
          路由至对应场景分槽。管理5个预设场景分槽的遗忘策略独立配置。

依赖模块: ad-14(场景判定与分槽路由单元), ad-15至ad-19(五个场景分槽),
          ad-44(独立世界模型库), ad-01(总控漏斗F₀)
被依赖模块: ad-01(总控漏斗F₀), ad-20至ad-43(五层存储与晋升遗忘执行模块),
            ad-37(重要度增量定时刷新单元)

安全约束:
  S-01: 漏斗二仅在自动驾驶模式下运行，人工驾驶模式时全部冻结只读
  S-02: 场景判定以世界模型实时输出为准，不信任经验请求中的场景标签
  S-03: 新场景分槽创建须校验分槽总数上限，编译期硬编码 Nmax_slot
  S-04: 紧急接管或安全急停时，漏斗二全部锁定只读
  S-05: 乡村道路经验归入通用驾驶槽子类，享独立遗忘保护参数
  S-06: 各分槽遗忘策略独立配置，不得跨槽混用
  S-07: 所有分槽创建、冻结、遗忘策略调整操作全量写入 ad-51 变更日志
  S-08: 漏斗二与漏斗一物理存储分区隔离，编译期强制，本模块负责路由隔离
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class SceneCategory(Enum):
    """场景类别"""
    HIGHWAY = "highway_cruise"      # 高速巡航
    URBAN = "urban_intersection"    # 城区路口
    PARKING = "parking_low_speed"   # 泊车低速
    SPECIAL = "special_environment" # 特殊环境
    GENERAL = "general_driving"     # 通用驾驶
    RURAL = "rural_road"            # 乡村道路（通用驾驶槽子类）


class SlotStatus(Enum):
    """分槽状态"""
    ACTIVE = "active"
    FROZEN = "frozen"
    LOW_ACTIVITY = "low_activity"


class DispatcherState(Enum):
    """调度单元内部状态"""
    WAITING_SCENE = "waiting_scene"
    MATCHING = "matching"
    SLOT_READY = "slot_ready"
    CREATING_SLOT = "creating_slot"
    FALLBACK_GENERAL = "fallback_general"
    FROZEN = "frozen"
    MAINTENANCE = "maintenance"


# ==================== 数据结构 ====================

@dataclass
class SceneSlotMeta:
    """场景分槽元数据"""
    slot_id: int
    scene_category: SceneCategory
    sub_label: str = ""            # 子类标记（如"乡村道路"）
    status: SlotStatus = SlotStatus.ACTIVE
    storage_usage_rate: float = 0.0
    entry_count: int = 0
    create_time: float = field(default_factory=time.time)
    last_active_time: float = field(default_factory=time.time)
    # 专属遗忘策略参数
    forget_threshold: float = 0.15
    promotion_threshold_l1_l2: float = 0.40
    promotion_threshold_l2_l3: float = 0.60
    promotion_threshold_l3_l4: float = 0.80


@dataclass
class ExperienceWriteRequest:
    """经验写入请求"""
    request_id: str
    scene_label: str              # ECC 提供的场景标签（仅供参考）
    experience_entry: Dict[str, Any]
    priority: int = 0
    timestamp: float = field(default_factory=time.time)


@dataclass
class WorldModelSceneResult:
    """世界模型场景判定结果"""
    road_level: str               # 道路等级
    road_type: str                # 路面类型
    lane_count: int               # 车道数
    traffic_sign_density: str     # 交通标识密度
    weather: str                  # 天气
    lighting: str                 # 光照
    time_period: str              # 时段
    special_flags: List[str]      # 特殊标记（施工区、积水等）
    scene_category: SceneCategory # 判定后的场景类别
    confidence: float             # 置信度 0.0-1.0


@dataclass
class RouteResult:
    """路由结果"""
    target_slot_id: int
    scene_category: SceneCategory
    sub_label: str
    confidence: float
    route_tag: str                # "精确匹配" / "降级路由" / "归并溢出"
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class FunnelTwoDispatcher:
    """
    漏斗二专属调度单元 - 自成长经验漏斗管家
    
    职责:
    1. 场景分槽的创建、激活与经验路由分发
    2. 依据世界模型道路属性判定场景类别
    3. 管理5个预设场景分槽 + 乡村道路子类
    4. 各分槽遗忘策略独立配置
    5. 漏斗二仅在自动驾驶模式下运行
    """
    
    # 预设分槽数量上限（编译期硬编码）
    NMAX_SLOT = 8
    
    # 预设的5个核心分槽
    PRESET_SLOTS = {
        15: SceneCategory.HIGHWAY,
        16: SceneCategory.URBAN,
        17: SceneCategory.PARKING,
        18: SceneCategory.SPECIAL,
        19: SceneCategory.GENERAL,
    }
    
    # 场景判定置信度阈值
    SCENE_CONFIDENCE_THRESHOLD = 0.5
    
    # 存储告警阈值
    STORAGE_WARNING_THRESHOLD = 0.90
    
    def __init__(self):
        self.module_id = "ad-03"
        self.module_name = "漏斗二专属调度单元"
        
        # 内部状态
        self.state = DispatcherState.WAITING_SCENE
        
        # 分槽注册表: slot_id -> SceneSlotMeta
        self._slots: Dict[int, SceneSlotMeta] = {}
        
        # 当前活跃分槽
        self._active_slot_id: Optional[int] = None
        
        # 初始化5个预设分槽
        self._initialize_preset_slots()
        
        # 统计
        self._total_routes = 0
        self._total_creates = 0
        self._fallback_count = 0
        
        # 待写入 ad-51 的变更日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 漏斗二调度单元初始化完成, {len(self._slots)}个预设分槽就绪")
    
    def _initialize_preset_slots(self) -> None:
        """初始化5个预设场景分槽及其专属遗忘策略"""
        
        # 高速巡航槽
        self._slots[15] = SceneSlotMeta(
            slot_id=15, scene_category=SceneCategory.HIGHWAY,
            forget_threshold=0.12,
            promotion_threshold_l1_l2=0.40
        )
        
        # 城区路口槽
        self._slots[16] = SceneSlotMeta(
            slot_id=16, scene_category=SceneCategory.URBAN,
            forget_threshold=0.10,
            promotion_threshold_l1_l2=0.40
        )
        
        # 泊车低速槽
        self._slots[17] = SceneSlotMeta(
            slot_id=17, scene_category=SceneCategory.PARKING,
            forget_threshold=0.075,
            promotion_threshold_l1_l2=0.40
        )
        
        # 特殊环境槽
        self._slots[18] = SceneSlotMeta(
            slot_id=18, scene_category=SceneCategory.SPECIAL,
            forget_threshold=0.09,
            promotion_threshold_l1_l2=0.28
        )
        
        # 通用驾驶槽
        self._slots[19] = SceneSlotMeta(
            slot_id=19, scene_category=SceneCategory.GENERAL,
            forget_threshold=0.15,
            promotion_threshold_l1_l2=0.40
        )
    
    # ========== 经验路由 ==========
    
    def route_experience(self, request: ExperienceWriteRequest,
                         world_model_result: WorldModelSceneResult) -> RouteResult:
        """
        路由经验写入请求至对应场景分槽
        
        逻辑:
        1. 以世界模型实时输出为准，忽略请求中的场景标签
        2. 置信度 < 0.5 → 降级路由至通用驾驶槽(19)
        3. 匹配已有分槽 → 路由至对应分槽
        4. 未匹配且分槽未达上限 → 创建新分槽
        5. 分槽已达上限 → 归并至相似度最高的分槽
        """
        self.state = DispatcherState.MATCHING
        self._total_routes += 1
        
        # S-02: 以世界模型实时输出为准
        scene_category = world_model_result.scene_category
        confidence = world_model_result.confidence
        
        # 降级路由判定
        if confidence < self.SCENE_CONFIDENCE_THRESHOLD:
            self.state = DispatcherState.FALLBACK_GENERAL
            self._fallback_count += 1
            print(f"[{self.module_id}] 低置信度({confidence:.2f})，降级路由至通用驾驶槽")
            return RouteResult(
                target_slot_id=19,
                scene_category=SceneCategory.GENERAL,
                sub_label="降级路由",
                confidence=confidence,
                route_tag="降级路由"
            )
        
        # 乡村道路子类检测
        sub_label = ""
        if world_model_result.road_type in ["泥土", "碎石", "沙土"] or \
           (world_model_result.lane_count == 1 and world_model_result.traffic_sign_density == "无"):
            sub_label = "乡村道路"
            target_slot_id = 19  # 归入通用驾驶槽
        else:
            # 查找匹配的预设分槽
            target_slot_id = self._find_matching_slot(scene_category)
        
        if target_slot_id is None:
            # 无匹配分槽，检查是否可以创建新槽
            if len(self._slots) < self.NMAX_SLOT:
                self.state = DispatcherState.CREATING_SLOT
                target_slot_id = self._create_new_slot(scene_category)
            else:
                # 分槽已达上限，归并至相似度最高的已有分槽
                target_slot_id = self._find_closest_slot(scene_category)
                route_tag = "归并溢出"
                result = RouteResult(
                    target_slot_id=target_slot_id,
                    scene_category=scene_category,
                    sub_label=sub_label,
                    confidence=confidence,
                    route_tag=route_tag
                )
                self.state = DispatcherState.SLOT_READY
                return result
        
        self.state = DispatcherState.SLOT_READY
        
        # 更新分槽活跃时间
        if target_slot_id in self._slots:
            self._slots[target_slot_id].last_active_time = time.time()
        
        route_tag = "精确匹配" if sub_label == "" else "乡村道路子类"
        
        print(f"[{self.module_id}] 路由: 场景={scene_category.value}, 目标槽={target_slot_id}, 标签={route_tag}")
        return RouteResult(
            target_slot_id=target_slot_id,
            scene_category=scene_category,
            sub_label=sub_label,
            confidence=confidence,
            route_tag=route_tag
        )
    
    def _find_matching_slot(self, scene_category: SceneCategory) -> Optional[int]:
        """查找匹配的已有分槽"""
        for slot_id, slot in self._slots.items():
            if slot.scene_category == scene_category and slot.status == SlotStatus.ACTIVE:
                return slot_id
        return None
    
    def _find_closest_slot(self, scene_category: SceneCategory) -> int:
        """找到与指定场景类别最相似的分槽（归并兜底）"""
        # 简化实现：归入通用驾驶槽
        return 19
    
    def _create_new_slot(self, scene_category: SceneCategory) -> int:
        """创建新场景分槽"""
        self._total_creates += 1
        # 分配新槽号
        new_slot_id = max(self._slots.keys()) + 1 if self._slots else 20
        
        slot = SceneSlotMeta(
            slot_id=new_slot_id,
            scene_category=scene_category
        )
        self._slots[new_slot_id] = slot
        
        self._log_event("SLOT_CREATE", {
            "slot_id": new_slot_id,
            "scene_category": scene_category.value
        })
        
        print(f"[{self.module_id}] 创建新分槽: slot_{new_slot_id}, category={scene_category.value}")
        return new_slot_id
    
    # ========== 分槽管理 ==========
    
    def freeze_all_slots(self) -> None:
        """冻结全部场景分槽（驾驶模式切换时调用）"""
        self.state = DispatcherState.FROZEN
        for slot in self._slots.values():
            slot.status = SlotStatus.FROZEN
        print(f"[{self.module_id}] 全部场景分槽已冻结")
    
    def unfreeze_slots(self) -> None:
        """解冻全部场景分槽"""
        for slot in self._slots.values():
            if slot.status == SlotStatus.FROZEN:
                slot.status = SlotStatus.ACTIVE
        self.state = DispatcherState.WAITING_SCENE
        print(f"[{self.module_id}] 全部场景分槽已解冻")
    
    def check_slot_health(self) -> List[int]:
        """
        检查分槽健康状态
        
        Returns:
            存储占用率超过告警阈值的分槽ID列表
        """
        warning_slots = []
        for slot_id, slot in self._slots.items():
            if slot.storage_usage_rate > self.STORAGE_WARNING_THRESHOLD:
                warning_slots.append(slot_id)
            if slot.status == SlotStatus.ACTIVE and \
               time.time() - slot.last_active_time > 7 * 24 * 3600:
                slot.status = SlotStatus.LOW_ACTIVITY
        return warning_slots
    
    def get_slot_forget_params(self, slot_id: int) -> Optional[Dict[str, float]]:
        """获取指定分槽的遗忘策略参数"""
        if slot_id not in self._slots:
            return None
        slot = self._slots[slot_id]
        return {
            "forget_threshold": slot.forget_threshold,
            "promotion_l1_l2": slot.promotion_threshold_l1_l2,
            "promotion_l2_l3": slot.promotion_threshold_l2_l3,
            "promotion_l3_l4": slot.promotion_threshold_l3_l4,
        }
    
    def update_slot_forget_params(self, slot_id: int, params: Dict[str, float]) -> bool:
        """更新指定分槽的遗忘策略参数"""
        if slot_id not in self._slots:
            return False
        slot = self._slots[slot_id]
        if "forget_threshold" in params:
            slot.forget_threshold = params["forget_threshold"]
        if "promotion_l1_l2" in params:
            slot.promotion_threshold_l1_l2 = params["promotion_l1_l2"]
        self._log_event("FORGET_PARAM_UPDATE", {"slot_id": slot_id, "params": params})
        return True
    
    # ========== 状态上报 ==========
    
    def get_active_slot_count(self) -> int:
        """获取活跃分槽数量"""
        return sum(1 for s in self._slots.values() if s.status == SlotStatus.ACTIVE)
    
    def get_all_slot_ids(self) -> List[int]:
        """获取所有分槽ID"""
        return list(self._slots.keys())
    
    def get_slot_status_summary(self) -> Dict[int, Dict[str, Any]]:
        """获取全部分槽状态摘要"""
        summary = {}
        for slot_id, slot in self._slots.items():
            summary[slot_id] = {
                "category": slot.scene_category.value,
                "status": slot.status.value,
                "storage_usage": slot.storage_usage_rate,
                "entry_count": slot.entry_count
            }
        return summary
    
    # ========== 变更日志 ==========
    
    def _log_event(self, event_type: str, details: Dict[str, Any]) -> None:
        """记录变更日志"""
        self._pending_logs.append({
            "log_id": f"log-{uuid.uuid4().hex[:8]}",
            "event_type": event_type,
            "source_module": self.module_id,
            "details": details,
            "timestamp": time.time()
        })
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        """收集待写入 ad-51 的变更日志"""
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_slots": len(self._slots),
            "active_slots": self.get_active_slot_count(),
            "total_routes": self._total_routes,
            "total_creates": self._total_creates,
            "fallback_count": self._fallback_count,
            "current_state": self.state.value
        }


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-03 漏斗二专属调度单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    # --- TC-03-01: 精确匹配高速巡航槽 ---
    print("\n[TC-03-01] 精确匹配高速巡航槽")
    try:
        dispatcher = FunnelTwoDispatcher()
        wm_result = WorldModelSceneResult(
            road_level="高速公路", road_type="沥青", lane_count=3,
            traffic_sign_density="高", weather="晴", lighting="日间",
            time_period="白天", special_flags=[],
            scene_category=SceneCategory.HIGHWAY, confidence=0.95
        )
        request = ExperienceWriteRequest(
            request_id="req-001", scene_label="高速",
            experience_entry={"behavior": "跟车"}
        )
        result = dispatcher.route_experience(request, wm_result)
        assert result.target_slot_id == 15
        assert result.route_tag == "精确匹配"
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-03-02: 低置信度降级路由至通用驾驶槽 ---
    print("\n[TC-03-02] 低置信度降级路由")
    try:
        dispatcher = FunnelTwoDispatcher()
        wm_result = WorldModelSceneResult(
            road_level="未知道路", road_type="未知", lane_count=1,
            traffic_sign_density="无", weather="雾", lighting="微光",
            time_period="夜间", special_flags=[],
            scene_category=SceneCategory.GENERAL, confidence=0.3
        )
        request = ExperienceWriteRequest("req-002", "未知", {})
        result = dispatcher.route_experience(request, wm_result)
        assert result.target_slot_id == 19
        assert result.route_tag == "降级路由"
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-03-03: 乡村道路子类路由至通用驾驶槽 ---
    print("\n[TC-03-03] 乡村道路子类路由")
    try:
        dispatcher = FunnelTwoDispatcher()
        wm_result = WorldModelSceneResult(
            road_level="未分级", road_type="泥土", lane_count=1,
            traffic_sign_density="无", weather="晴", lighting="日间",
            time_period="白天", special_flags=[],
            scene_category=SceneCategory.GENERAL, confidence=0.85
        )
        request = ExperienceWriteRequest("req-003", "乡村", {})
        result = dispatcher.route_experience(request, wm_result)
        assert result.target_slot_id == 19
        assert result.sub_label == "乡村道路"
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-03-04: 冻结全部场景分槽 ---
    print("\n[TC-03-04] 冻结全部场景分槽")
    try:
        dispatcher = FunnelTwoDispatcher()
        dispatcher.freeze_all_slots()
        assert dispatcher.state == DispatcherState.FROZEN
        for slot in dispatcher._slots.values():
            assert slot.status == SlotStatus.FROZEN
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-03-05: 解冻场景分槽 ---
    print("\n[TC-03-05] 解冻场景分槽")
    try:
        dispatcher = FunnelTwoDispatcher()
        dispatcher.freeze_all_slots()
        dispatcher.unfreeze_slots()
        assert dispatcher.state == DispatcherState.WAITING_SCENE
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-03-06: 获取分槽遗忘策略参数 ---
    print("\n[TC-03-06] 获取分槽遗忘策略参数")
    try:
        dispatcher = FunnelTwoDispatcher()
        params = dispatcher.get_slot_forget_params(15)
        assert params is not None
        assert params["forget_threshold"] == 0.12
        params18 = dispatcher.get_slot_forget_params(18)
        assert params18["promotion_l1_l2"] == 0.28
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-03-07: 更新分槽遗忘策略 ---
    print("\n[TC-03-07] 更新分槽遗忘策略")
    try:
        dispatcher = FunnelTwoDispatcher()
        result = dispatcher.update_slot_forget_params(15, {"forget_threshold": 0.10})
        assert result == True
        params = dispatcher.get_slot_forget_params(15)
        assert params["forget_threshold"] == 0.10
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-03-08: 统计信息 ---
    print("\n[TC-03-08] 统计信息")
    try:
        dispatcher = FunnelTwoDispatcher()
        stats = dispatcher.get_statistics()
        assert stats["total_slots"] == 5
        assert stats["total_routes"] == 0
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