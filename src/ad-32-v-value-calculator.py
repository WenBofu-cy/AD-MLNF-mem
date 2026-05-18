#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-32
模块名称: 风格匹配度 V 值计算单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 三维重要度计算引擎
核心职责: 将系统实际执行的驾驶动作与用户设定的驾驶风格偏好（平顺舒适/标准通勤/高效通行）
          进行契合度量化比对，计算风格匹配度分值 V（0–1）。V 值反映当前驾驶行为是否符合
          用户期望的驾乘体验，参与综合重要度 I 值计算。

依赖模块: ad-35(三维权重系数配置单元，获取 β 权重及各风格基准参数),
          ad-36(综合重要度 I 值聚合计算单元，接收 V 值)
被依赖模块: ad-36(消费 V 值参与 I 值计算)

安全约束:
  S-01: 紧急制动事件（减速度 > 7m/s²）不参与 V 值计算，避免因安全操作而误判为“风格不匹配”
  S-02: 特殊环境槽（ad-18）经验 V 值强制置 1.0，安全优先于风格评判
  S-03: 任何风格参数的物理/法规下限硬编码，基准值不可低于此下限
  S-04: V 值计算仅用于经验评估与晋升参考，不可作为实时驾驶干预的依据
  S-05: 用户风格偏好数据属于个人隐私，不参与云端同步或导出
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class DrivingStyle(Enum):
    """驾驶风格"""
    COMFORT = "平顺舒适"
    STANDARD = "标准通勤"
    EFFICIENT = "高效通行"


class BehaviorDimension(Enum):
    """行为维度"""
    FOLLOW_DISTANCE = "跟车时距"
    LONGITUDINAL_JERK = "纵向冲击度"
    LATERAL_JERK = "横向冲击度"
    BRAKE_DECEL = "制动减速度"
    START_ACCEL = "起步加速度"
    TURN_SPEED_RATIO = "转弯车速比"
    LANE_CHANGE_GAP = "变道间隙"
    STOP_PITCH = "刹停点头"


class CalcState(Enum):
    """计算单元内部状态"""
    NORMAL = "normal"
    UPDATING = "updating"
    PAUSED = "paused"


# ==================== 数据结构 ====================

@dataclass
class DrivingBehaviorData:
    """驾驶行为数据包"""
    entry_id: str
    behavior_type: str             # 行为类型（跟车/变道/制动等）
    source_slot_id: int
    # 实际执行参数
    follow_distance: Optional[float] = None    # 跟车时距 (s)
    longitudinal_jerk: Optional[float] = None  # 纵向冲击度 (m/s³)
    lateral_jerk: Optional[float] = None       # 横向冲击度 (m/s³)
    brake_decel: Optional[float] = None        # 制动减速度 (m/s²)
    start_accel: Optional[float] = None        # 起步加速度 (m/s²)
    turn_speed_ratio: Optional[float] = None   # 转弯车速/限速比
    lane_change_gap: Optional[float] = None    # 变道间隙 (s)
    stop_jerk: Optional[float] = None          # 刹停冲击度 (m/s³)
    is_emergency_brake: bool = False           # 是否紧急制动 (>7m/s²)


@dataclass
class StyleBaseline:
    """风格基准参数"""
    style: DrivingStyle
    follow_distance: float         # 目标跟车时距 (s)
    longitudinal_jerk: float       # 纵向冲击度上限 (m/s³)
    lateral_jerk: float            # 横向冲击度上限 (m/s³)
    brake_decel: float             # 制动减速度上限 (m/s²)
    start_accel: float             # 起步加速度上限 (m/s²)
    turn_speed_ratio: float        # 转弯车速/限速比上限
    lane_change_gap: float         # 变道间隙下限 (s)
    stop_jerk: float               # 刹停冲击度上限 (m/s³)


@dataclass
class PhysicalLimits:
    """物理/法规硬约束"""
    min_follow_distance: float = 1.8       # 法规下限
    max_longitudinal_jerk: float = 5.0     # 物理上限
    max_lateral_jerk: float = 3.0          # 物理上限
    max_brake_decel_non_emergency: float = 7.0  # 非紧急上限
    max_start_accel: float = 5.0
    max_turn_speed_ratio: float = 0.9
    min_lane_change_gap: float = 1.5       # 法规下限
    max_stop_jerk: float = 2.0


@dataclass
class VValueResult:
    """V 值计算结果"""
    entry_id: str
    v_value: float
    dimension_scores: Dict[str, Dict[str, float]]  # 维度 -> {实际值, 基准值, 偏差}
    exempted_dimensions: List[str]         # 被豁免的维度
    calculation_timestamp: float = field(default_factory=time.time)


# ==================== 默认风格基准库 ====================

STYLE_BASELINES: Dict[DrivingStyle, StyleBaseline] = {
    DrivingStyle.COMFORT: StyleBaseline(
        style=DrivingStyle.COMFORT,
        follow_distance=2.5,
        longitudinal_jerk=2.0,
        lateral_jerk=1.5,
        brake_decel=2.5,
        start_accel=1.5,
        turn_speed_ratio=0.5,
        lane_change_gap=3.0,
        stop_jerk=1.0
    ),
    DrivingStyle.STANDARD: StyleBaseline(
        style=DrivingStyle.STANDARD,
        follow_distance=2.0,
        longitudinal_jerk=3.0,
        lateral_jerk=2.0,
        brake_decel=3.5,
        start_accel=2.0,
        turn_speed_ratio=0.7,
        lane_change_gap=2.5,
        stop_jerk=1.5
    ),
    DrivingStyle.EFFICIENT: StyleBaseline(
        style=DrivingStyle.EFFICIENT,
        follow_distance=1.8,
        longitudinal_jerk=3.5,
        lateral_jerk=2.5,
        brake_decel=4.0,
        start_accel=2.5,
        turn_speed_ratio=0.8,
        lane_change_gap=2.0,
        stop_jerk=2.0
    ),
}

# ==================== 主类定义 ====================

class VValueCalculator:
    """
    风格匹配度 V 值计算单元
    
    职责:
    1. 根据用户设定的驾驶风格加载对应的基准参数
    2. 逐维度计算实际驾驶行为与基准的偏差
    3. 加权聚合各维度偏差，计算 V 值
    4. 特殊场景豁免（紧急制动、特殊环境槽等）
    """
    
    # 各行为维度权重
    DIMENSION_WEIGHTS = {
        BehaviorDimension.FOLLOW_DISTANCE: 0.20,
        BehaviorDimension.LONGITUDINAL_JERK: 0.15,
        BehaviorDimension.LATERAL_JERK: 0.15,
        BehaviorDimension.BRAKE_DECEL: 0.15,
        BehaviorDimension.START_ACCEL: 0.10,
        BehaviorDimension.TURN_SPEED_RATIO: 0.10,
        BehaviorDimension.LANE_CHANGE_GAP: 0.10,
        BehaviorDimension.STOP_PITCH: 0.05,
    }
    
    # 各分槽 V 值特殊处理
    SLOT_SPECIAL_HANDLING = {
        15: {"follow_distance_weight": 0.30, "stop_pitch_weight": 0.0},   # 高速巡航槽
        16: {"brake_decel_weight": 0.25, "start_accel_weight": 0.15},     # 城区路口槽
        17: {"all_threshold_multiplier": 0.6},                            # 泊车低速槽
        18: {"force_v_1": True},                                          # 特殊环境槽
        19: {"turn_speed_ratio_weight": 0.20, "longitudinal_jerk_weight": 0.10},  # 通用-乡村
    }
    
    # 连续低 V 值告警阈值
    LOW_V_THRESHOLD = 0.3
    LOW_V_CONSECUTIVE = 5
    
    def __init__(self):
        self.module_id = "ad-32"
        self.module_name = "风格匹配度 V 值计算单元"
        
        # 内部状态
        self.state = CalcState.NORMAL
        
        # 当前用户风格设定
        self._current_style = DrivingStyle.STANDARD
        
        # 物理/法规硬约束
        self._physical_limits = PhysicalLimits()
        
        # 近期 V 值历史（用于连续低 V 告警）
        self._recent_v_history: List[float] = []
        
        # 统计
        self._total_calculations = 0
        self._total_exemptions = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] V 值计算单元初始化完成")
        print(f"[{self.module_id}] 当前风格: {self._current_style.value}")
    
    # ========== 状态管理 ==========
    
    def set_style(self, style: DrivingStyle) -> None:
        """设置用户驾驶风格"""
        self._current_style = style
        self.state = CalcState.UPDATING
        print(f"[{self.module_id}] 风格切换: {style.value}")
        self.state = CalcState.NORMAL
    
    def get_style(self) -> DrivingStyle:
        return self._current_style
    
    def pause(self) -> None:
        self.state = CalcState.PAUSED
    
    def resume(self) -> None:
        self.state = CalcState.NORMAL
    
    def get_state(self) -> CalcState:
        return self.state
    
    # ========== V 值计算 ==========
    
    def calculate(self, behavior_data: DrivingBehaviorData) -> VValueResult:
        """
        计算风格匹配度 V 值
        
        V = CLAMP( 1.0 - Σ( w_i × deviation_i ), 0.0, 1.0 )
        """
        if self.state == CalcState.PAUSED:
            return VValueResult(
                entry_id=behavior_data.entry_id,
                v_value=1.0,
                dimension_scores={},
                exempted_dimensions=["系统暂停"]
            )
        
        self._total_calculations += 1
        slot_id = behavior_data.source_slot_id
        
        # S-02: 特殊环境槽 V 值强制置 1.0
        if self.SLOT_SPECIAL_HANDLING.get(slot_id, {}).get("force_v_1", False):
            self._total_exemptions += 1
            return VValueResult(
                entry_id=behavior_data.entry_id,
                v_value=1.0,
                dimension_scores={},
                exempted_dimensions=["特殊环境槽强制 V=1.0"]
            )
        
        # 获取风格基准
        baseline = STYLE_BASELINES.get(self._current_style, STYLE_BASELINES[DrivingStyle.STANDARD])
        
        # 获取分槽特殊权重
        slot_weights = self._get_slot_weights(slot_id)
        
        # 获取泊车槽的阈值乘数
        threshold_multiplier = self.SLOT_SPECIAL_HANDLING.get(slot_id, {}).get("all_threshold_multiplier", 1.0)
        
        total_weighted_deviation = 0.0
        active_weight_sum = 0.0
        dimension_scores = {}
        exempted_dimensions = []
        
        # 跟车时距
        if behavior_data.follow_distance is not None:
            base = baseline.follow_distance * threshold_multiplier
            deviation = self._calc_positive_deviation(
                behavior_data.follow_distance, base,
                self._physical_limits.min_follow_distance
            )
            w = slot_weights.get("follow_distance_weight", self.DIMENSION_WEIGHTS[BehaviorDimension.FOLLOW_DISTANCE])
            total_weighted_deviation += w * deviation
            active_weight_sum += w
            dimension_scores["跟车时距"] = {"实际值": behavior_data.follow_distance, "基准": base, "偏差": deviation}
        
        # 纵向冲击度
        if behavior_data.longitudinal_jerk is not None:
            base = baseline.longitudinal_jerk * threshold_multiplier
            deviation = self._calc_negative_deviation(
                behavior_data.longitudinal_jerk, base,
                self._physical_limits.max_longitudinal_jerk
            )
            w = slot_weights.get("longitudinal_jerk_weight", self.DIMENSION_WEIGHTS[BehaviorDimension.LONGITUDINAL_JERK])
            total_weighted_deviation += w * deviation
            active_weight_sum += w
            dimension_scores["纵向冲击度"] = {"实际值": behavior_data.longitudinal_jerk, "基准": base, "偏差": deviation}
        
        # 横向冲击度
        if behavior_data.lateral_jerk is not None:
            base = baseline.lateral_jerk * threshold_multiplier
            deviation = self._calc_negative_deviation(
                behavior_data.lateral_jerk, base,
                self._physical_limits.max_lateral_jerk
            )
            w = slot_weights.get("lateral_jerk_weight", self.DIMENSION_WEIGHTS[BehaviorDimension.LATERAL_JERK])
            total_weighted_deviation += w * deviation
            active_weight_sum += w
            dimension_scores["横向冲击度"] = {"实际值": behavior_data.lateral_jerk, "基准": base, "偏差": deviation}
        
        # 制动减速度
        if behavior_data.brake_decel is not None:
            # S-01: 紧急制动不参与 V 值计算
            if behavior_data.is_emergency_brake or behavior_data.brake_decel > self._physical_limits.max_brake_decel_non_emergency:
                exempted_dimensions.append("制动减速度（紧急制动豁免）")
            else:
                base = baseline.brake_decel * threshold_multiplier
                deviation = self._calc_negative_deviation(
                    behavior_data.brake_decel, base,
                    self._physical_limits.max_brake_decel_non_emergency
                )
                w = slot_weights.get("brake_decel_weight", self.DIMENSION_WEIGHTS[BehaviorDimension.BRAKE_DECEL])
                total_weighted_deviation += w * deviation
                active_weight_sum += w
                dimension_scores["制动减速度"] = {"实际值": behavior_data.brake_decel, "基准": base, "偏差": deviation}
        
        # 起步加速度
        if behavior_data.start_accel is not None:
            base = baseline.start_accel * threshold_multiplier
            deviation = self._calc_negative_deviation(
                behavior_data.start_accel, base,
                self._physical_limits.max_start_accel
            )
            w = slot_weights.get("start_accel_weight", self.DIMENSION_WEIGHTS[BehaviorDimension.START_ACCEL])
            total_weighted_deviation += w * deviation
            active_weight_sum += w
            dimension_scores["起步加速度"] = {"实际值": behavior_data.start_accel, "基准": base, "偏差": deviation}
        
        # 转弯车速比
        if behavior_data.turn_speed_ratio is not None:
            base = baseline.turn_speed_ratio * threshold_multiplier
            deviation = self._calc_negative_deviation(
                behavior_data.turn_speed_ratio, base,
                self._physical_limits.max_turn_speed_ratio
            )
            w = slot_weights.get("turn_speed_ratio_weight", self.DIMENSION_WEIGHTS[BehaviorDimension.TURN_SPEED_RATIO])
            total_weighted_deviation += w * deviation
            active_weight_sum += w
            dimension_scores["转弯车速比"] = {"实际值": behavior_data.turn_speed_ratio, "基准": base, "偏差": deviation}
        
        # 变道间隙
        if behavior_data.lane_change_gap is not None:
            base = baseline.lane_change_gap * threshold_multiplier
            deviation = self._calc_positive_deviation(
                behavior_data.lane_change_gap, base,
                self._physical_limits.min_lane_change_gap
            )
            w = slot_weights.get("lane_change_gap_weight", self.DIMENSION_WEIGHTS[BehaviorDimension.LANE_CHANGE_GAP])
            total_weighted_deviation += w * deviation
            active_weight_sum += w
            dimension_scores["变道间隙"] = {"实际值": behavior_data.lane_change_gap, "基准": base, "偏差": deviation}
        
        # 刹停点头
        if behavior_data.stop_jerk is not None:
            base = baseline.stop_jerk * threshold_multiplier
            deviation = self._calc_negative_deviation(
                behavior_data.stop_jerk, base,
                self._physical_limits.max_stop_jerk
            )
            w = slot_weights.get("stop_pitch_weight", self.DIMENSION_WEIGHTS[BehaviorDimension.STOP_PITCH])
            total_weighted_deviation += w * deviation
            active_weight_sum += w
            dimension_scores["刹停点头"] = {"实际值": behavior_data.stop_jerk, "基准": base, "偏差": deviation}
        
        # 归一化：使用实际活跃权重和
        if active_weight_sum > 0:
            total_weighted_deviation = total_weighted_deviation / active_weight_sum
        
        v_value = max(0.0, min(1.0, 1.0 - total_weighted_deviation))
        
        # 记录低 V 值历史
        self._update_low_v_history(v_value)
        
        result = VValueResult(
            entry_id=behavior_data.entry_id,
            v_value=v_value,
            dimension_scores=dimension_scores,
            exempted_dimensions=exempted_dimensions
        )
        
        return result
    
    def _get_slot_weights(self, slot_id: int) -> Dict[str, float]:
        """获取分槽特殊权重"""
        handling = self.SLOT_SPECIAL_HANDLING.get(slot_id, {})
        weights = {}
        for key, value in handling.items():
            if key.endswith("_weight"):
                dim_name = key.replace("_weight", "")
                weights[key] = value
        return weights
    
    def _calc_positive_deviation(self, actual: float, baseline: float, lower_limit: float) -> float:
        """
        计算正向偏差（值越大越好，如跟车时距、变道间隙）
        实际值 ≥ 基准值 → 偏差 = 0
        """
        if actual >= baseline:
            return 0.0
        if baseline <= lower_limit:
            return 1.0
        deviation = (baseline - actual) / (baseline - lower_limit)
        return max(0.0, min(1.0, deviation))
    
    def _calc_negative_deviation(self, actual: float, baseline: float, upper_limit: float) -> float:
        """
        计算负向偏差（值越小越好，如冲击度、减速度）
        实际值 ≤ 基准值 → 偏差 = 0
        """
        if actual <= baseline:
            return 0.0
        if upper_limit <= baseline:
            return 1.0
        deviation = (actual - baseline) / (upper_limit - baseline)
        return max(0.0, min(1.0, deviation))
    
    def _update_low_v_history(self, v_value: float) -> None:
        """更新低 V 值历史"""
        self._recent_v_history.append(v_value)
        if len(self._recent_v_history) > self.LOW_V_CONSECUTIVE * 2:
            self._recent_v_history = self._recent_v_history[-self.LOW_V_CONSECUTIVE:]
    
    def is_low_v_alert(self) -> bool:
        """
        检测是否应触发连续低 V 值告警
        """
        if len(self._recent_v_history) < self.LOW_V_CONSECUTIVE:
            return False
        recent = self._recent_v_history[-self.LOW_V_CONSECUTIVE:]
        avg_v = sum(recent) / len(recent)
        return avg_v < self.LOW_V_THRESHOLD
    
    # ========== 查询接口 ==========
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_calculations": self._total_calculations,
            "total_exemptions": self._total_exemptions,
            "current_style": self._current_style.value,
            "recent_avg_v": sum(self._recent_v_history[-10:]) / max(len(self._recent_v_history[-10:]), 1),
            "state": self.state.value
        }
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-32 风格匹配度 V 值计算单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    # --- TC-32-01: 平顺风格完美匹配 V=1.0 ---
    print("\n[TC-32-01] 平顺风格完美匹配 V=1.0")
    try:
        calc = VValueCalculator()
        calc.set_style(DrivingStyle.COMFORT)
        data = DrivingBehaviorData(
            entry_id="EXP-001", behavior_type="跟车",
            source_slot_id=15, follow_distance=2.8, longitudinal_jerk=1.5,
            lateral_jerk=1.0, brake_decel=2.0
        )
        result = calc.calculate(data)
        assert result.v_value >= 0.95
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-32-02: 高效风格跟车偏近 ---
    print("\n[TC-32-02] 高效风格跟车偏近（1.5s < 1.8s 法规下限）")
    try:
        calc = VValueCalculator()
        calc.set_style(DrivingStyle.EFFICIENT)
        data = DrivingBehaviorData(
            entry_id="EXP-002", behavior_type="跟车",
            source_slot_id=15, follow_distance=1.5
        )
        result = calc.calculate(data)
        # 1.5 < 1.8, 偏差 = (1.8-1.5)/(1.8-1.8) → 保护
        # 但分母为0时，取最大偏差1.0
        assert result.dimension_scores["跟车时距"]["偏差"] > 0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-32-03: 紧急制动豁免 ---
    print("\n[TC-32-03] 紧急制动豁免（减速度 8.0 > 7.0）")
    try:
        calc = VValueCalculator()
        data = DrivingBehaviorData(
            entry_id="EXP-003", behavior_type="制动",
            source_slot_id=16, brake_decel=8.0, is_emergency_brake=True
        )
        result = calc.calculate(data)
        assert "制动减速度（紧急制动豁免）" in result.exempted_dimensions
        # 只有制动被豁免，其他维度无数据，V 应为 1.0
        assert result.v_value == 1.0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-32-04: 特殊环境槽强制 V=1.0 ---
    print("\n[TC-32-04] 特殊环境槽强制 V=1.0")
    try:
        calc = VValueCalculator()
        data = DrivingBehaviorData(
            entry_id="EXP-004", behavior_type="跟车",
            source_slot_id=18, follow_distance=1.2  # 很差
        )
        result = calc.calculate(data)
        assert result.v_value == 1.0
        assert "特殊环境槽强制 V=1.0" in result.exempted_dimensions
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-32-05: 泊车低速槽阈值收紧 ---
    print("\n[TC-32-05] 泊车低速槽阈值收紧（×0.6）")
    try:
        calc = VValueCalculator()
        calc.set_style(DrivingStyle.COMFORT)
        data = DrivingBehaviorData(
            entry_id="EXP-005", behavior_type="泊车",
            source_slot_id=17, longitudinal_jerk=1.5
        )
        result = calc.calculate(data)
        # 舒适风格基准 2.0 → 收紧后 1.2，实际 1.5 > 1.2，应有偏差
        score = result.dimension_scores.get("纵向冲击度", {}).get("偏差", 0)
        assert score > 0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-32-06: 风格切换 ---
    print("\n[TC-32-06] 风格切换后使用新基准")
    try:
        calc = VValueCalculator()
        calc.set_style(DrivingStyle.EFFICIENT)
        data = DrivingBehaviorData(
            entry_id="EXP-006", behavior_type="起步",
            source_slot_id=15, start_accel=2.0
        )
        result1 = calc.calculate(data)
        # 高效风格基准 start_accel=2.5，实际 2.0 ≤ 2.5 → 偏差 0
        assert result1.dimension_scores["起步加速度"]["偏差"] == 0.0
        
        calc.set_style(DrivingStyle.COMFORT)
        result2 = calc.calculate(data)
        # 舒适风格基准 start_accel=1.5，实际 2.0 > 1.5 → 偏差 > 0
        assert result2.dimension_scores["起步加速度"]["偏差"] > 0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-32-07: 连续低 V 值告警 ---
    print("\n[TC-32-07] 连续低 V 值告警")
    try:
        calc = VValueCalculator()
        calc.set_style(DrivingStyle.COMFORT)
        for _ in range(5):
            data = DrivingBehaviorData(
                entry_id=f"EXP-{uuid.uuid4().hex[:4]}", behavior_type="跟车",
                source_slot_id=15, follow_distance=1.5, longitudinal_jerk=4.0
            )
            calc.calculate(data)
        assert calc.is_low_v_alert() == True
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-32-08: 多维度加权综合 ---
    print("\n[TC-32-08] 多维度加权综合")
    try:
        calc = VValueCalculator()
        calc.set_style(DrivingStyle.STANDARD)
        data = DrivingBehaviorData(
            entry_id="EXP-008", behavior_type="变道",
            source_slot_id=15,
            follow_distance=2.2,
            longitudinal_jerk=2.5,
            lateral_jerk=2.5,
            lane_change_gap=2.0
        )
        result = calc.calculate(data)
        # V 值应在 0.5 到 0.9 之间
        assert 0.4 < result.v_value < 0.95
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-32-09: 默认风格（标准通勤） ---
    print("\n[TC-32-09] 默认风格（标准通勤）")
    try:
        calc = VValueCalculator()
        assert calc.get_style() == DrivingStyle.STANDARD
        data = DrivingBehaviorData(
            entry_id="EXP-009", behavior_type="巡航",
            source_slot_id=15, follow_distance=2.0, longitudinal_jerk=3.0
        )
        result = calc.calculate(data)
        # 完全匹配标准风格，V 值应接近 1.0
        assert result.v_value >= 0.95
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-32-10: 数据缺失维度不参与计算 ---
    print("\n[TC-32-10] 数据缺失维度不参与计算")
    try:
        calc = VValueCalculator()
        data = DrivingBehaviorData(
            entry_id="EXP-010", behavior_type="转弯",
            source_slot_id=15, turn_speed_ratio=0.6
            # 其他维度全部缺失
        )
        result = calc.calculate(data)
        assert len(result.dimension_scores) == 1
        assert result.v_value >= 0.9
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- 测试结果汇总 ---
    print("\n" + "=" * 60)
    print(f"测试结果: {passed} PASS, {failed} FAIL")
    print("=" * 60)