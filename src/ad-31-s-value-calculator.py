#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-31
模块名称: 安全显著性 S 值计算单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 三维重要度计算引擎
核心职责: 从每个驾驶经验条目对应的场景数据中，提取并量化安全相关物理信号，计算安全
          显著性分值 S（0–1）。S 值代表该场景中涉及人身安全与碰撞风险的紧迫与严重
          程度，直接驱动重要度 I 值计算与 L5 直达写入判定。

依赖模块: ad-44(独立世界模型库，提供风险特征), ad-36(综合重要度 I 值聚合计算单元),
          ad-35(三维权重系数配置单元，获取 α 权重)
被依赖模块: ad-36(消费 S 值参与 I 值计算), ad-28(S≥0.9 时触发安全事件直达写入 L5)

S 值计算模型:
  S = CLAMP( S_base + S_event + S_EnvMod , 0.0 , 1.0 )
  - S_base:   场景基础风险得分（0.0–0.5），由 TTC/THW/目标类别/车速决定
  - S_event:  离散危险事件触发增量（0.0–0.8），由 AEB/ESC/碰撞风险等驱动
  - S_EnvMod: 环境风险调节因子（-0.1–+0.2），由天气/光照/路面状态修正

安全约束:
  S-01: S ≥ 0.9 强制触发 L5 直达写入，绕过常规晋升与仲裁流程
  S-02: 安全关键信号丢失时采用保守估算（取高值），宁可高估不可低估
  S-03: S 值计算基于实时世界模型数据，不受历史经验或驾驶员偏好影响
  S-04: 碰撞不可避免（TTC < 0.5s 且无有效避让路径）S 值直接置为 0.8 起评
  S-05: 所有 S ≥ 0.7 的计算结果全量写入 ad-51 变更日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class RiskLevel(Enum):
    """碰撞风险等级"""
    LOW = "低"
    MEDIUM = "中"
    HIGH = "高"
    CRITICAL = "极高"


class TargetClass(Enum):
    """目标类别"""
    CLASS_1 = "静态固定"
    CLASS_2 = "机动动态"
    CLASS_3 = "非人生物"
    CLASS_4 = "人类及非机动"
    CLASS_5 = "环境要素"


class EventType(Enum):
    """危险事件类型"""
    AEB_TRIGGERED = "AEB触发"
    COLLISION_INEVITABLE = "碰撞不可避免"
    ESC_ABS_ACTIVE = "ESC/ABS介入"
    SUDDEN_INTRUSION = "行人/动物突然侵入"
    SEVERE_SKID = "严重侧滑风险"
    EMERGENCY_VEHICLE = "特种车辆临近"
    TRAFFIC_VIOLATION = "交通违规风险"
    BLIND_SPOT_DANGER = "盲区危险"


class CalcState(Enum):
    """计算单元内部状态"""
    NORMAL = "normal"
    BATCH = "batch"
    DEGRADED = "degraded"
    PAUSED = "paused"


# ==================== 数据结构 ====================

@dataclass
class SceneRiskData:
    """场景风险数据"""
    entry_id: str
    ttc: float                     # 碰撞时间（秒），99 表示无风险
    thw: float                     # 跟车时距（秒），99 表示无前车
    target_class: TargetClass      # 核心风险目标类别
    speed: float                   # 本车速度（km/h）
    speed_limit: float             # 路段限速（km/h）
    # 危险事件标记
    aeb_triggered: bool = False
    esc_abs_active: bool = False
    collision_inevitable: bool = False
    sudden_intrusion: bool = False
    intrusion_ttc: float = 99.0
    severe_skid: bool = False
    lateral_g: float = 0.0
    emergency_vehicle: bool = False
    traffic_violation: bool = False
    blind_spot_danger: bool = False
    # 环境条件
    weather: str = "晴"
    lighting: str = "日间"
    road_surface: str = "干燥"
    road_type: str = "沥青"


@dataclass
class SValueResult:
    """S 值计算结果"""
    entry_id: str
    s_value: float
    s_base: float
    s_event: float
    s_env_mod: float
    trigger_events: List[EventType]
    data_quality: str              # "完整" / "降级"
    calculation_timestamp: float = field(default_factory=time.time)


@dataclass
class SValueCache:
    """S 值缓存条目"""
    entry_id: str
    s_value: float
    s_base: float
    s_event: float
    s_env_mod: float
    last_updated: float


# ==================== 主类定义 ====================

class SValueCalculator:
    """
    安全显著性 S 值计算单元
    
    职责:
    1. 从场景数据中提取安全相关物理信号
    2. 计算 S_base（基础风险得分）
    3. 检测离散危险事件，计算 S_event
    4. 根据环境条件计算 S_EnvMod 调节因子
    5. 综合计算 S 值
    6. S ≥ 0.9 时触发 L5 直达写入信号
    7. 信号丢失时采用保守估算
    """
    
    # S_base 各因子得分上限
    TTC_MAX_SCORE = 0.30
    THW_MAX_SCORE = 0.10
    TARGET_MAX_SCORE = 0.15
    SPEED_MAX_SCORE = 0.15
    S_BASE_MAX = 0.50
    
    # TTC 得分映射
    TTC_SCORE_MAP = [
        (4.0, 0.0),    # TTC > 4s: 无风险
        (2.0, 0.10),   # 2s < TTC ≤ 4s
        (1.0, 0.20),   # 1s < TTC ≤ 2s
        (0.0, 0.30),   # TTC ≤ 1s
    ]
    
    # THW 得分映射
    THW_SCORE_MAP = [
        (2.5, 0.0),    # THW > 2.5s
        (1.5, 0.05),   # 1.5s < THW ≤ 2.5s
        (0.0, 0.10),   # THW ≤ 1.5s
    ]
    
    # 目标类别风险得分
    TARGET_SCORE = {
        TargetClass.CLASS_4: 0.15,   # 行人最高
        TargetClass.CLASS_3: 0.10,   # 动物次之
        TargetClass.CLASS_2: 0.05,   # 机动车
        TargetClass.CLASS_1: 0.00,   # 静态
        TargetClass.CLASS_5: 0.05,   # 环境要素
    }
    
    # S_event 危险事件增量
    EVENT_SCORES = {
        EventType.AEB_TRIGGERED: 0.60,
        EventType.COLLISION_INEVITABLE: 0.80,
        EventType.ESC_ABS_ACTIVE: 0.40,
        EventType.SUDDEN_INTRUSION: 0.50,
        EventType.SEVERE_SKID: 0.50,
        EventType.EMERGENCY_VEHICLE: 0.30,
        EventType.TRAFFIC_VIOLATION: 0.20,
        EventType.BLIND_SPOT_DANGER: 0.30,
    }
    
    # S_event 上限
    S_EVENT_MAX = 0.80
    
    # S_EnvMod 环境调节量
    ENV_MODS = {
        "路面": {"湿滑": 0.10, "积雪": 0.15, "结冰": 0.15},
        "天气": {"暴雨": 0.10, "暴雪": 0.10, "大雾": 0.10, "沙尘暴": 0.10},
        "光照": {"夜间无灯": 0.05},
        "道路": {"泥土": 0.10, "碎石": 0.10, "沙土": 0.10},
    }
    ENV_MOD_MIN = -0.10
    ENV_MOD_MAX = 0.20
    
    # 理想条件调节
    IDEAL_REDUCTION = -0.05
    
    # L5 直达触发阈值
    L5_DIRECT_S_THRESHOLD = 0.90
    
    # 高安全显著性标记阈值
    HIGH_S_THRESHOLD = 0.70
    
    # 碰撞不可避免起评 S 值
    COLLISION_INEVITABLE_BASE_S = 0.80
    
    def __init__(self):
        self.module_id = "ad-31"
        self.module_name = "安全显著性 S 值计算单元"
        
        # 内部状态
        self.state = CalcState.NORMAL
        
        # S 值缓存
        self._s_cache: Dict[str, SValueCache] = {}
        
        # 历史 S_base 最大值（用于信号丢失时保守估算）
        self._max_s_base_historical = 0.30
        
        # 权重配置
        self._alpha = 0.50
        
        # 统计
        self._total_calculations = 0
        self._total_l5_triggers = 0
        self._total_high_s = 0
        
        # L5 直达触发信号缓冲区
        self._l5_direct_signals: List[Dict[str, Any]] = []
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] S 值计算单元初始化完成")
        print(f"[{self.module_id}] L5 直达阈值: S ≥ {self.L5_DIRECT_S_THRESHOLD}")
    
    # ========== 状态管理 ==========
    
    def set_alpha(self, alpha: float) -> None:
        self._alpha = max(0.30, min(0.70, alpha))
    
    def pause(self) -> None:
        self.state = CalcState.PAUSED
    
    def resume(self) -> None:
        self.state = CalcState.NORMAL
    
    def get_state(self) -> CalcState:
        return self.state
    
    # ========== S 值计算 ==========
    
    def calculate(self, scene_data: SceneRiskData) -> SValueResult:
        """
        计算安全显著性 S 值
        
        公式: S = CLAMP( S_base + S_event + S_EnvMod , 0.0 , 1.0 )
        """
        if self.state == CalcState.PAUSED:
            return SValueResult(
                entry_id=scene_data.entry_id,
                s_value=0.0, s_base=0.0, s_event=0.0, s_env_mod=0.0,
                trigger_events=[], data_quality="暂停"
            )
        
        self._total_calculations += 1
        
        # 检查信号完整性
        signals_ok = self._check_signal_integrity(scene_data)
        data_quality = "完整" if signals_ok else "降级"
        
        if not signals_ok:
            self.state = CalcState.DEGRADED
        elif self.state == CalcState.DEGRADED:
            self.state = CalcState.NORMAL
        
        # 计算 S_base
        s_base = self._calc_s_base(scene_data, signals_ok)
        
        # 计算 S_event
        s_event, trigger_events = self._calc_s_event(scene_data)
        
        # 计算 S_EnvMod
        s_env_mod = self._calc_s_env_mod(scene_data)
        
        # 综合 S 值
        s_value = s_base + s_event + s_env_mod
        s_value = max(0.0, min(1.0, s_value))
        
        # 碰撞不可避免特殊处理（S-04）
        if scene_data.collision_inevitable:
            s_value = max(s_value, self.COLLISION_INEVITABLE_BASE_S)
            s_event = max(s_event, 0.80)
        
        # 更新缓存
        self._s_cache[scene_data.entry_id] = SValueCache(
            entry_id=scene_data.entry_id,
            s_value=s_value, s_base=s_base,
            s_event=s_event, s_env_mod=s_env_mod,
            last_updated=time.time()
        )
        
        # 更新历史 S_base 最大值
        if s_base > self._max_s_base_historical:
            self._max_s_base_historical = s_base
        
        # 高 S 值统计
        if s_value >= self.HIGH_S_THRESHOLD:
            self._total_high_s += 1
        
        # L5 直达触发
        if s_value >= self.L5_DIRECT_S_THRESHOLD:
            self._total_l5_triggers += 1
            self._l5_direct_signals.append({
                "entry_id": scene_data.entry_id,
                "s_value": s_value,
                "trigger_events": [e.value for e in trigger_events],
                "reason": "碰撞不可避免" if scene_data.collision_inevitable else f"S≥{self.L5_DIRECT_S_THRESHOLD}"
            })
        
        result = SValueResult(
            entry_id=scene_data.entry_id,
            s_value=s_value,
            s_base=s_base,
            s_event=s_event,
            s_env_mod=s_env_mod,
            trigger_events=trigger_events,
            data_quality=data_quality
        )
        
        return result
    
    def _check_signal_integrity(self, data: SceneRiskData) -> bool:
        """检查安全关键信号完整性"""
        # TTC 和 THW 为关键信号
        if data.ttc is None or data.thw is None:
            return False
        if data.target_class is None:
            return False
        return True
    
    # ========== S_base 基础风险计算 ==========
    
    def _calc_s_base(self, data: SceneRiskData, signals_ok: bool) -> float:
        """计算 S_base（0.0–0.5）"""
        if not signals_ok:
            # S-02: 信号丢失时保守估算
            return max(self._max_s_base_historical, 0.30)
        
        s_base = 0.0
        
        # TTC 得分
        s_base += self._map_score(data.ttc, self.TTC_SCORE_MAP)
        
        # THW 得分
        s_base += self._map_score(data.thw, self.THW_SCORE_MAP)
        
        # 目标类别得分
        s_base += self.TARGET_SCORE.get(data.target_class, 0.05)
        
        # 车速风险
        if data.speed_limit > 0:
            speed_ratio = data.speed / data.speed_limit
            if speed_ratio > 1.2:
                s_base += 0.15
            elif speed_ratio > 1.0:
                s_base += 0.10
            elif speed_ratio < 0.5:
                s_base -= 0.05
        
        return min(s_base, self.S_BASE_MAX)
    
    def _map_score(self, value: float, score_map: List[Tuple[float, float]]) -> float:
        """根据阈值映射表计算得分（值越小越危险，得分越高）"""
        # 无风险标记值
        if value >= 99.0:
            return 0.0
        
        for threshold, score in score_map:
            if value > threshold:
                return score
        
        # 返回最低阈值对应的得分（最危险情况）
        return score_map[-1][1] if score_map else 0.0
    
    # ========== S_event 危险事件增量 ==========
    
    def _calc_s_event(self, data: SceneRiskData) -> Tuple[float, List[EventType]]:
        """计算 S_event（0.0–0.8），多事件并发时叠加"""
        events = self._detect_events(data)
        
        if not events:
            return 0.0, []
        
        # 按优先级排序（得分从高到低）
        events.sort(key=lambda e: self.EVENT_SCORES.get(e, 0.0), reverse=True)
        
        # 主事件全量 + 次事件 × 0.3 叠加
        s_event = self.EVENT_SCORES.get(events[0], 0.0)
        if len(events) > 1:
            s_event += self.EVENT_SCORES.get(events[1], 0.0) * 0.3
        
        return min(s_event, self.S_EVENT_MAX), events
    
    def _detect_events(self, data: SceneRiskData) -> List[EventType]:
        """检测离散危险事件"""
        events = []
        
        # 碰撞不可避免（最高优先级）
        if data.collision_inevitable:
            events.append(EventType.COLLISION_INEVITABLE)
        
        # AEB 触发
        if data.aeb_triggered:
            events.append(EventType.AEB_TRIGGERED)
        
        # 行人/动物突然侵入
        if data.sudden_intrusion and data.intrusion_ttc < 2.0:
            events.append(EventType.SUDDEN_INTRUSION)
        
        # 严重侧滑
        if data.severe_skid or data.lateral_g > 0.7:
            events.append(EventType.SEVERE_SKID)
        
        # ESC/ABS 介入
        if data.esc_abs_active:
            events.append(EventType.ESC_ABS_ACTIVE)
        
        # 盲区危险
        if data.blind_spot_danger:
            events.append(EventType.BLIND_SPOT_DANGER)
        
        # 特种车辆
        if data.emergency_vehicle:
            events.append(EventType.EMERGENCY_VEHICLE)
        
        # 交通违规
        if data.traffic_violation:
            events.append(EventType.TRAFFIC_VIOLATION)
        
        return events
    
    # ========== S_EnvMod 环境调节 ==========
    
    def _calc_s_env_mod(self, data: SceneRiskData) -> float:
        """计算 S_EnvMod（-0.1–+0.2）"""
        s_env_mod = 0.0
        
        # 路面状态调节
        s_env_mod += self.ENV_MODS["路面"].get(data.road_surface, 0.0)
        
        # 天气调节
        s_env_mod += self.ENV_MODS["天气"].get(data.weather, 0.0)
        
        # 光照调节
        s_env_mod += self.ENV_MODS["光照"].get(data.lighting, 0.0)
        
        # 道路类型调节
        s_env_mod += self.ENV_MODS["道路"].get(data.road_type, 0.0)
        
        # 理想条件适度降低
        if (data.weather == "晴" and data.lighting == "日间" and
                data.road_surface == "干燥" and data.road_type == "沥青"):
            s_env_mod += self.IDEAL_REDUCTION
        
        return max(self.ENV_MOD_MIN, min(self.ENV_MOD_MAX, s_env_mod))
    
    # ========== 查询接口 ==========
    
    def get_s_value(self, entry_id: str) -> Optional[float]:
        """获取缓存的 S 值"""
        cache = self._s_cache.get(entry_id)
        return cache.s_value if cache else None
    
    def get_l5_direct_signals(self) -> List[Dict[str, Any]]:
        """获取 L5 直达触发信号列表"""
        signals = self._l5_direct_signals.copy()
        self._l5_direct_signals.clear()
        return signals
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_calculations": self._total_calculations,
            "total_l5_triggers": self._total_l5_triggers,
            "total_high_s": self._total_high_s,
            "cached_entries": len(self._s_cache),
            "max_s_base_historical": self._max_s_base_historical,
            "state": self.state.value
        }
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-31 安全显著性 S 值计算单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    def make_scene(entry_id, ttc=99.0, thw=99.0, target=TargetClass.CLASS_2,
                   speed=80, speed_limit=120, aeb=False, esc=False,
                   collision=False, intrusion=False, intrusion_ttc=99.0,
                   skid=False, lateral_g=0.0, ev=False, violation=False,
                   blind=False, weather="晴", lighting="日间",
                   road_surface="干燥", road_type="沥青"):
        return SceneRiskData(
            entry_id=entry_id, ttc=ttc, thw=thw, target_class=target,
            speed=speed, speed_limit=speed_limit,
            aeb_triggered=aeb, esc_abs_active=esc,
            collision_inevitable=collision,
            sudden_intrusion=intrusion, intrusion_ttc=intrusion_ttc,
            severe_skid=skid, lateral_g=lateral_g,
            emergency_vehicle=ev, traffic_violation=violation,
            blind_spot_danger=blind,
            weather=weather, lighting=lighting,
            road_surface=road_surface, road_type=road_type
        )
    
    # --- TC-31-01: 理想巡航 S≈0 ---
    print("\n[TC-31-01] 理想巡航 S≈0")
    try:
        calc = SValueCalculator()
        scene = make_scene("EXP-001", ttc=99.0, thw=99.0)
        result = calc.calculate(scene)
        assert result.s_value <= 0.05  # 理想条件有 -0.05 调节
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-31-02: AEB 触发 S≥0.7 ---
    print("\n[TC-31-02] AEB 触发 S≥0.7")
    try:
        calc = SValueCalculator()
        scene = make_scene("EXP-002", ttc=1.2, thw=1.5, aeb=True,
                           target=TargetClass.CLASS_2, speed=80, speed_limit=120)
        result = calc.calculate(scene)
        assert result.s_value >= 0.7
        assert EventType.AEB_TRIGGERED in result.trigger_events
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-31-03: 碰撞不可避免 L5 直达 ---
    print("\n[TC-31-03] 碰撞不可避免 L5 直达")
    try:
        calc = SValueCalculator()
        scene = make_scene("EXP-003", ttc=0.3, thw=1.0, collision=True,
                           target=TargetClass.CLASS_4)
        result = calc.calculate(scene)
        assert result.s_value >= 0.80
        signals = calc.get_l5_direct_signals()
        assert len(signals) >= 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-31-04: 行人目标高风险 ---
    print("\n[TC-31-04] 行人目标高风险（S_base 含 0.15）")
    try:
        calc = SValueCalculator()
        scene = make_scene("EXP-004", ttc=2.5, thw=2.0, target=TargetClass.CLASS_4)
        result = calc.calculate(scene)
        assert result.s_base >= 0.15  # 仅目标类别就 0.15
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-31-05: 冰雪路面环境调节 ---
    print("\n[TC-31-05] 冰雪路面环境调节（S_EnvMod=+0.15）")
    try:
        calc = SValueCalculator()
        scene = make_scene("EXP-005", ttc=3.0, thw=2.0, road_surface="结冰")
        result = calc.calculate(scene)
        assert result.s_env_mod >= 0.10
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-31-06: 信号丢失保守估算 ---
    print("\n[TC-31-06] 信号丢失保守估算")
    try:
        calc = SValueCalculator()
        scene = SceneRiskData("EXP-006", ttc=None, thw=None,
                              target_class=None, speed=80, speed_limit=120)
        result = calc.calculate(scene)
        assert result.data_quality == "降级"
        assert result.s_base >= 0.30  # 保守取 0.30
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-31-07: 多事件并发叠加 ---
    print("\n[TC-31-07] 多事件并发（AEB + ESC）")
    try:
        calc = SValueCalculator()
        scene = make_scene("EXP-007", ttc=1.0, thw=1.0, aeb=True, esc=True,
                           target=TargetClass.CLASS_2)
        result = calc.calculate(scene)
        # AEB(0.60) + ESC(0.40×0.3=0.12) = 0.72
        assert result.s_event >= 0.70
        assert len(result.trigger_events) >= 2
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-31-08: S≥0.9 触发 L5 直达信号 ---
    print("\n[TC-31-08] S≥0.9 触发 L5 直达信号")
    try:
        calc = SValueCalculator()
        scene = make_scene("EXP-008", ttc=0.5, thw=0.8, collision=True,
                           target=TargetClass.CLASS_4, road_surface="结冰")
        result = calc.calculate(scene)
        assert result.s_value >= 0.9
        signals = calc.get_l5_direct_signals()
        assert len(signals) >= 1
        assert signals[0]["entry_id"] == "EXP-008"
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-31-09: 夜间无灯环境调节 ---
    print("\n[TC-31-09] 夜间无灯环境调节（S_EnvMod=+0.05）")
    try:
        calc = SValueCalculator()
        scene = make_scene("EXP-009", ttc=2.5, thw=2.0, lighting="夜间无灯")
        result = calc.calculate(scene)
        assert result.s_env_mod >= 0.05
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-31-10: 特种车辆临近 ---
    print("\n[TC-31-10] 特种车辆临近（S_event=+0.30）")
    try:
        calc = SValueCalculator()
        scene = make_scene("EXP-010", ttc=4.0, thw=3.0, ev=True)
        result = calc.calculate(scene)
        assert EventType.EMERGENCY_VEHICLE in result.trigger_events
        assert result.s_event >= 0.30
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