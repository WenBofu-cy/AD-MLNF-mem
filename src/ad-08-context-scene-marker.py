#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-08
模块名称: 上下文场景标记单元
所属分区: 二、漏斗一：驾驶员画像漏斗
核心职责: 同步获取世界模型环境特征，为每条驾驶行为观测条目打上场景标签（常规路况/
          紧急突发/特殊场景）。应急避险操作自动标记为"应急特殊操作"，不纳入陋习统计。
          场景标签随行为条目写入目标子画像槽，用于后续行为判定。

依赖模块: ad-44(独立世界模型库), ad-07(驾驶行为观测记录单元)
被依赖模块: ad-09(行为判定标签单元)

安全约束:
  S-01: 漏斗一数据编译期禁止接入自动驾驶决策链路
  S-02: 应急标记为 True 的行为条目，在 ad-09 中自动标记为"应急特殊操作"
  S-03: 世界模型查询超时或异常时，降级使用"未知道路"标签，不可猜测
  S-04: 场景标签判定以世界模型实时输出为准，不受漏斗记忆经验影响
  S-05: 标签置信度低于 0.5 时，ad-09 行为判定应采用保守规则
  S-06: 紧急熔断时立即停止处理并丢弃未完成条目
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class SceneLabel(Enum):
    """场景标签"""
    # 常规
    NORMAL = "常规路况"
    # 特殊
    CONGESTION = "拥堵蠕行"
    BAD_WEATHER = "恶劣天气"
    NIGHT_DARK = "夜间无灯"
    UNPAVED = "非铺装道路"
    # 紧急
    HIGH_COLLISION_RISK = "碰撞高风险"
    EMERGENCY_VEHICLE = "特种车辆临近"
    ROAD_SURFACE_ANOMALY = "路面突发异常"
    SUDDEN_INTRUSION = "行人/动物突然侵入"
    # 未知
    UNKNOWN = "未知道路"


class MarkerState(Enum):
    """标记单元内部状态"""
    NORMAL = "normal"
    QUERYING = "querying"
    MATCHING = "matching"
    FALLBACK = "fallback"
    PAUSED = "paused"
    EMERGENCY_RO = "emergency_ro"


# ==================== 数据结构 ====================

@dataclass
class BehaviorObservation:
    """行为观测条目（来自 ad-07）"""
    obs_id: str
    timestamp: float
    steering_angle: float
    steering_rate: float
    throttle: float
    brake_pressure: float
    brake_active: bool
    turn_signal: str
    speed: float
    gear: str
    behavior_type: str
    data_quality: str
    target_slot_id: int


@dataclass
class WorldModelSceneResult:
    """世界模型场景特征查询结果"""
    road_level: str               # 道路等级
    road_type: str                # 路面类型
    weather: str                  # 天气
    lighting: str                 # 光照
    time_period: str              # 时段
    traffic_density: float        # 交通流密度 0-1
    avg_speed: float              # 平均车速 km/h
    ttc_min: float                # 最小碰撞时间
    collision_risk: str           # 碰撞风险等级
    emergency_vehicle_detected: bool  # 检测到特种车辆
    road_anomaly_detected: bool       # 检测到路面异常
    class4_intrusion_ttc: Optional[float]  # 第四类目标侵入TTC
    class3_intrusion_ttc: Optional[float]  # 第三类目标侵入TTC
    confidence: float             # 场景分类置信度


@dataclass
class TaggedObservation:
    """带场景标签的行为观测条目"""
    obs_id: str
    original_observation: BehaviorObservation
    scene_label: SceneLabel
    label_confidence: float       # 标签置信度 0-1
    is_emergency: bool            # 是否应急标记
    tag_timestamp: float = field(default_factory=time.time)
    wm_query_duration_ms: float = 0.0  # 世界模型查询耗时


# ==================== 主类定义 ====================

class ContextSceneMarker:
    """
    上下文场景标记单元
    
    职责:
    1. 接收 ad-07 行为观测条目
    2. 向 ad-44 世界模型查询当前场景特征
    3. 按优先级规则匹配场景标签
    4. 判定应急标记（True/False）
    5. 输出带场景标签的行为条目至 ad-09
    """
    
    # 世界模型查询超时（秒）
    WM_QUERY_TIMEOUT = 0.05  # 50ms
    
    # 世界模型连续失败上限
    WM_MAX_FAILURES = 3
    
    # 降级重试间隔（秒）
    WM_RETRY_INTERVAL = 30.0
    
    def __init__(self):
        self.module_id = "ad-08"
        self.module_name = "上下文场景标记单元"
        
        # 内部状态
        self.state = MarkerState.NORMAL
        
        # 世界模型查询统计
        self._wm_fail_count = 0
        self._wm_last_fail_time = 0.0
        self._wm_disabled = False  # 连续失败后暂停查询
        
        # 标签统计
        self._total_tagged = 0
        self._emergency_tagged = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 上下文场景标记单元初始化完成")
    
    # ========== 状态管理 ==========
    
    def pause(self) -> None:
        """暂停标记处理"""
        self.state = MarkerState.PAUSED
        print(f"[{self.module_id}] 标记处理已暂停")
    
    def resume(self) -> None:
        """恢复标记处理"""
        self.state = MarkerState.NORMAL
        print(f"[{self.module_id}] 标记处理已恢复")
    
    def emergency_stop(self) -> None:
        """紧急熔断"""
        self.state = MarkerState.EMERGENCY_RO
        print(f"[{self.module_id}] 紧急熔断，停止标记处理")
    
    # ========== 场景标签匹配 ==========
    
    def tag_observation(self, observation: BehaviorObservation,
                        wm_result: Optional[WorldModelSceneResult] = None,
                        wm_query_duration_ms: float = 0.0) -> Optional[TaggedObservation]:
        """
        为行为观测条目打上场景标签
        
        逻辑:
        1. 查询世界模型获取场景特征
        2. 按优先级从高到低匹配标签（紧急 > 特殊 > 常规）
        3. 判定应急标记
        4. 返回带标签条目
        
        Args:
            observation: 行为观测条目
            wm_result: 世界模型查询结果（None 表示查询失败）
            wm_query_duration_ms: 世界模型查询耗时
            
        Returns:
            带场景标签的观测条目，或 None（紧急熔断时）
        """
        if self.state == MarkerState.EMERGENCY_RO:
            return None
        
        if self.state == MarkerState.PAUSED:
            return None
        
        self.state = MarkerState.QUERYING
        
        # 世界模型查询失败处理
        if wm_result is None:
            self._wm_fail_count += 1
            self._wm_last_fail_time = time.time()
            
            if self._wm_fail_count >= self.WM_MAX_FAILURES:
                self._wm_disabled = True
                print(f"[{self.module_id}] 世界模型连续{self.WM_MAX_FAILURES}次查询失败，暂停查询")
            
            # 降级：使用"未知道路"标签
            self.state = MarkerState.FALLBACK
            return self._create_fallback_tagged(observation)
        
        # 重置失败计数
        self._wm_fail_count = 0
        self._wm_disabled = False
        
        # 如果世界模型已被禁用，且距上次失败超过重试间隔，尝试恢复
        if self._wm_disabled:
            if time.time() - self._wm_last_fail_time > self.WM_RETRY_INTERVAL:
                self._wm_disabled = False
            else:
                return self._create_fallback_tagged(observation)
        
        # 置信度低降级
        if wm_result.confidence < 0.5:
            self.state = MarkerState.FALLBACK
            tagged = self._create_fallback_tagged(observation)
            tagged.label_confidence = wm_result.confidence
            return tagged
        
        self.state = MarkerState.MATCHING
        
        # 按优先级匹配场景标签
        scene_label = self._match_scene_label(wm_result)
        is_emergency = self._is_emergency_scene(scene_label, observation.behavior_type)
        
        self._total_tagged += 1
        if is_emergency:
            self._emergency_tagged += 1
        
        tagged = TaggedObservation(
            obs_id=observation.obs_id,
            original_observation=observation,
            scene_label=scene_label,
            label_confidence=wm_result.confidence,
            is_emergency=is_emergency,
            wm_query_duration_ms=wm_query_duration_ms
        )
        
        self.state = MarkerState.NORMAL
        return tagged
    
    def _match_scene_label(self, wm_result: WorldModelSceneResult) -> SceneLabel:
        """
        按优先级匹配场景标签
        
        优先级: 紧急 > 特殊 > 常规
        """
        # 紧急场景（最高优先级）
        if wm_result.collision_risk in ["高", "极高"] or wm_result.ttc_min < 3.0:
            return SceneLabel.HIGH_COLLISION_RISK
        
        if wm_result.emergency_vehicle_detected:
            return SceneLabel.EMERGENCY_VEHICLE
        
        if wm_result.road_anomaly_detected:
            return SceneLabel.ROAD_SURFACE_ANOMALY
        
        if wm_result.class4_intrusion_ttc is not None and wm_result.class4_intrusion_ttc < 2.0:
            return SceneLabel.SUDDEN_INTRUSION
        
        if wm_result.class3_intrusion_ttc is not None and wm_result.class3_intrusion_ttc < 2.0:
            return SceneLabel.SUDDEN_INTRUSION
        
        # 特殊场景
        if wm_result.weather in ["暴雨", "暴雪", "大雾", "沙尘暴"]:
            return SceneLabel.BAD_WEATHER
        
        if wm_result.time_period == "夜间" and wm_result.lighting == "无灯":
            return SceneLabel.NIGHT_DARK
        
        if wm_result.traffic_density > 0.8 and wm_result.avg_speed < 10.0:
            return SceneLabel.CONGESTION
        
        if wm_result.road_type in ["泥土", "碎石", "沙土"]:
            return SceneLabel.UNPAVED
        
        # 常规场景
        return SceneLabel.NORMAL
    
    def _is_emergency_scene(self, scene_label: SceneLabel, behavior_type: str) -> bool:
        """
        判定是否为应急场景
        
        应急标签类型 + 特定行为组合 → True
        """
        emergency_labels = {
            SceneLabel.HIGH_COLLISION_RISK,
            SceneLabel.EMERGENCY_VEHICLE,
            SceneLabel.ROAD_SURFACE_ANOMALY,
            SceneLabel.SUDDEN_INTRUSION,
        }
        
        if scene_label in emergency_labels:
            return True
        
        # 恶劣天气/夜间无灯 + 紧急制动/紧急避让 → 应急
        if scene_label in [SceneLabel.BAD_WEATHER, SceneLabel.NIGHT_DARK]:
            if behavior_type in ["制动", "变道"]:
                return True
        
        return False
    
    def _create_fallback_tagged(self, observation: BehaviorObservation) -> TaggedObservation:
        """创建降级标记条目"""
        self._total_tagged += 1
        return TaggedObservation(
            obs_id=observation.obs_id,
            original_observation=observation,
            scene_label=SceneLabel.UNKNOWN,
            label_confidence=0.3,
            is_emergency=False
        )
    
    # ========== 查询接口 ==========
    
    def get_state(self) -> MarkerState:
        return self.state
    
    def is_wm_disabled(self) -> bool:
        return self._wm_disabled
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_tagged": self._total_tagged,
            "emergency_tagged": self._emergency_tagged,
            "wm_fail_count": self._wm_fail_count,
            "wm_disabled": self._wm_disabled,
            "state": self.state.value
        }
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-08 上下文场景标记单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    def make_observation(behavior_type="匀速巡航"):
        return BehaviorObservation(
            obs_id=f"obs-{uuid.uuid4().hex[:6]}",
            timestamp=time.time(),
            steering_angle=5.0, steering_rate=10.0,
            throttle=30.0, brake_pressure=0.0, brake_active=False,
            turn_signal="关闭", speed=50.0, gear="D",
            behavior_type=behavior_type,
            data_quality="完整", target_slot_id=1
        )
    
    # --- TC-08-01: 常规路况标记 ---
    print("\n[TC-08-01] 常规路况标记")
    try:
        marker = ContextSceneMarker()
        wm = WorldModelSceneResult(
            road_level="高速", road_type="沥青", weather="晴",
            lighting="日间", time_period="白天",
            traffic_density=0.3, avg_speed=80.0,
            ttc_min=10.0, collision_risk="低",
            emergency_vehicle_detected=False,
            road_anomaly_detected=False,
            class4_intrusion_ttc=None, class3_intrusion_ttc=None,
            confidence=0.95
        )
        obs = make_observation("匀速巡航")
        tagged = marker.tag_observation(obs, wm)
        assert tagged is not None
        assert tagged.scene_label == SceneLabel.NORMAL
        assert tagged.is_emergency == False
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-08-02: 碰撞高风险应急标记 ---
    print("\n[TC-08-02] 碰撞高风险应急标记")
    try:
        marker = ContextSceneMarker()
        wm = WorldModelSceneResult(
            "高速", "沥青", "晴", "日间", "白天",
            0.3, 80.0, 1.5, "高",
            False, False, None, None,
            confidence=0.95
        )
        obs = make_observation("制动")
        tagged = marker.tag_observation(obs, wm)
        assert tagged is not None
        assert tagged.scene_label == SceneLabel.HIGH_COLLISION_RISK
        assert tagged.is_emergency == True
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-08-03: 特种车辆临近应急标记 ---
    print("\n[TC-08-03] 特种车辆临近应急标记")
    try:
        marker = ContextSceneMarker()
        wm = WorldModelSceneResult(
            "城市主干道", "沥青", "晴", "日间", "白天",
            0.5, 40.0, 5.0, "中",
            True, False, None, None,
            confidence=0.90
        )
        obs = make_observation()
        tagged = marker.tag_observation(obs, wm)
        assert tagged.scene_label == SceneLabel.EMERGENCY_VEHICLE
        assert tagged.is_emergency == True
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-08-04: 恶劣天气特殊标记 ---
    print("\n[TC-08-04] 恶劣天气特殊标记")
    try:
        marker = ContextSceneMarker()
        wm = WorldModelSceneResult(
            "高速", "沥青", "暴雨", "日间", "白天",
            0.2, 60.0, 8.0, "低",
            False, False, None, None,
            confidence=0.85
        )
        obs = make_observation()
        tagged = marker.tag_observation(obs, wm)
        assert tagged.scene_label == SceneLabel.BAD_WEATHER
        assert tagged.is_emergency == False
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-08-05: 世界模型查询失败降级 ---
    print("\n[TC-08-05] 世界模型查询失败降级")
    try:
        marker = ContextSceneMarker()
        obs = make_observation()
        tagged = marker.tag_observation(obs, None)  # 查询失败
        assert tagged is not None
        assert tagged.scene_label == SceneLabel.UNKNOWN
        assert tagged.label_confidence == 0.3
        assert tagged.is_emergency == False
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-08-06: 世界模型连续失败暂停查询 ---
    print("\n[TC-08-06] 世界模型连续失败暂停查询")
    try:
        marker = ContextSceneMarker()
        for i in range(3):
            marker.tag_observation(make_observation(), None)
        assert marker.is_wm_disabled() == True
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-08-07: 低置信度降级 ---
    print("\n[TC-08-07] 低置信度降级")
    try:
        marker = ContextSceneMarker()
        wm = WorldModelSceneResult(
            "未知", "未知", "未知", "未知", "未知",
            0.0, 0.0, 99, "未知",
            False, False, None, None,
            confidence=0.3  # 低置信度
        )
        obs = make_observation()
        tagged = marker.tag_observation(obs, wm)
        assert tagged.scene_label == SceneLabel.UNKNOWN
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-08-08: 紧急熔断丢弃 ---
    print("\n[TC-08-08] 紧急熔断丢弃")
    try:
        marker = ContextSceneMarker()
        marker.emergency_stop()
        obs = make_observation()
        tagged = marker.tag_observation(obs, None)
        assert tagged is None
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