#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-34
模块名称: 基础重要度 I₀ 赋值单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 三维重要度计算引擎
核心职责: 基于新写入漏斗二的经验条目的结构化数据，为每条经验赋予基础重要度初始值
          I₀（0–1）。I₀ 是三维重要度驱动公式 I = I₀ + α·S + β·V + γ·C 的初始基线，
          代表该经验在未经时间与复用检验前的固有价值。

依赖模块: ad-14(场景判定与分槽路由单元，提供经验写入请求),
          各场景分槽（ad-15至ad-19，提供子类标签与路由标记）
被依赖模块: ad-36(综合重要度 I 值聚合计算单元，消费 I₀ 值),
            ad-21(L1时序衰减单元，参考 I₀ 作为衰减基准)

I₀ 赋值模型:
  核心公式: I₀ = CLAMP( I_base × (1 + Σ 场景加成) + 事件加权 , 0.05 , 0.90 )
  - I_base: 由经验生成来源决定（人类示教0.70 / 仿真0.60 / 常规0.50 / 降级0.35 / 应急0.80）
  - 场景加成: 高风险目标+0.15 / 极端天气+0.10 / 夜间无灯+0.05 等，上限累加0.30
  - 事件加权: 碰撞避免+0.20 / 不可抗力应对+0.30 / 法规合规+0.10 / 效率优化+0.05
  - 特殊规则: 不可抗力I₀=0.90 / S≥0.9直达I₀=0.90 / 特殊环境槽额外+0.05

安全约束:
  S-01: I₀ 上限硬编码为 0.90，为 I 值动态增量（α·S / β·V / γ·C）留出空间
  S-02: 不可抗力场景 I₀ 强制设为 0.90，确保顶级安全经验获得最高初始权重
  S-03: I₀ 赋值规则库须经过安全审计与签名校验，OTA 更新时校验完整性
  S-04: I₀ 赋值不因经验的"成败"标签而产生极端偏差，策略失误经验保留客观固有价值
"""

from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class ExperienceSource(Enum):
    """经验生成来源"""
    HUMAN_DEMO = "人类示教"                # 驾驶员演示，I_base=0.70
    SIMULATION = "仿真回灌验证"            # 离线仿真验证通过，I_base=0.60
    AUTONOMOUS_EXPLORE = "系统自主探索"     # 自主学习成功经验，I_base=0.55
    REGULAR_AUTONOMOUS = "常规自动驾驶决策"  # 日常决策，I_base=0.50
    LOW_CONFIDENCE = "低置信度降级路由"      # 不确定场景，I_base=0.35
    EMERGENCY_OP = "应急避险操作"            # 安全操作，I_base=0.80


class CalcState(Enum):
    """赋值单元内部状态"""
    NORMAL = "normal"
    UPDATING = "updating"      # 规则库更新中
    PAUSED = "paused"


# ==================== 数据结构 ====================

@dataclass
class SceneFeatures:
    """场景特征"""
    has_pedestrian: bool = False         # 是否有行人（第四类目标）
    extreme_weather: bool = False        # 是否极端天气（暴雨/暴雪/大雾/沙尘）
    night_dark: bool = False             # 是否夜间无灯
    unpaved: bool = False                # 是否非铺装路面
    high_speed: bool = False             # 是否高速场景（>80km/h）
    novel_scene: bool = False            # 是否首次遇到的新场景类型


@dataclass
class EventAnnotations:
    """事件语义标注"""
    collision_avoided: bool = False      # 成功避免碰撞
    force_majeure_handled: bool = False  # 不可抗力成功应对
    regulation_compliant: bool = False   # 法规合规示范
    efficiency_optimized: bool = False   # 通行效率优化


@dataclass
class ExperienceWriteRequest:
    """经验写入请求（简化版，从 ad-14 传入）"""
    entry_id: str
    source: ExperienceSource = ExperienceSource.REGULAR_AUTONOMOUS
    result_label: str = "成功优化"          # "成功优化" / "策略失误" / "不可抗力场景"
    force_majeure: bool = False
    s_value: float = 0.0                   # 已计算的 S 值（若有，用于 S≥0.9 直通判定）
    source_slot_id: int = 19               # 来源分槽号（18=特殊环境槽，享有额外加成）
    scene_features: SceneFeatures = field(default_factory=SceneFeatures)
    event_annotations: EventAnnotations = field(default_factory=EventAnnotations)


@dataclass
class I0Result:
    """I₀ 赋值结果"""
    entry_id: str
    i0_value: float
    i_base: float                          # 来源基础分值
    scene_bonus_total: float               # 场景加成总和
    event_weight_total: float              # 事件加权总和
    assignment_basis: str                  # 赋值依据说明
    timestamp: float = field(default_factory=time.time)


@dataclass
class AssignmentStats:
    """赋值统计"""
    total_assigned: int = 0
    force_majeure_count: int = 0
    high_s_direct_count: int = 0
    normal_count: int = 0
    avg_i0: float = 0.0


# ==================== 主类定义 ====================

class I0Assignment:
    """
    基础重要度 I₀ 赋值单元
    
    职责:
    1. 根据经验生成来源查表获取 I_base
    2. 根据场景特征计算加成系数（上限 0.30）
    3. 根据事件语义标注计算额外加权
    4. 不可抗力或 S≥0.9 场景强制 I₀=0.90
    5. 特殊环境槽额外加成 +0.05
    6. 综合计算 I₀ = CLAMP( I_base × (1 + 加成) + 事件加权 , 0.05 , 0.90 )
    """
    
    # ========== 编译期常量 ==========
    I0_MIN = 0.05
    I0_MAX = 0.90
    HIGH_S_THRESHOLD = 0.90               # S≥0.90 直通 I₀_MAX
    
    # ========== 来源基础分值表 ==========
    SOURCE_BASE: Dict[ExperienceSource, float] = {
        ExperienceSource.HUMAN_DEMO: 0.70,
        ExperienceSource.SIMULATION: 0.60,
        ExperienceSource.AUTONOMOUS_EXPLORE: 0.55,
        ExperienceSource.REGULAR_AUTONOMOUS: 0.50,
        ExperienceSource.LOW_CONFIDENCE: 0.35,
        ExperienceSource.EMERGENCY_OP: 0.80,
    }
    
    # ========== 场景加成系数表 ==========
    SCENE_BONUS: Dict[str, float] = {
        "has_pedestrian": 0.15,       # 高风险目标（行人/非机动车）
        "extreme_weather": 0.10,       # 极端天气（暴雨/暴雪/大雾/沙尘暴）
        "night_dark": 0.05,            # 夜间无路灯照明
        "unpaved": 0.05,               # 非铺装路面（泥土/碎石/沙土）
        "high_speed": 0.05,            # 高速行驶（>80km/h）
        "novel_scene": 0.10,           # 首次遇到的新场景类型
        "strategy_mistake": 0.05,       # 失败教训（策略失误经验）
    }
    MAX_SCENE_BONUS = 0.30             # 场景加成总和上限
    
    # ========== 事件加权表 ==========
    EVENT_WEIGHT: Dict[str, float] = {
        "collision_avoided": 0.20,        # 成功避免碰撞
        "force_majeure_handled": 0.30,    # 不可抗力成功应对
        "regulation_compliant": 0.10,     # 法规合规示范
        "efficiency_optimized": 0.05,     # 通行效率优化
    }
    
    # ========== 特殊规则 ==========
    SPECIAL_SLOT_BONUS = 0.05           # 特殊环境槽(ad-18)额外加成
    DEFAULT_CONSERVATIVE_I0 = 0.40      # 字段缺失时的保守默认值
    
    def __init__(self):
        self.module_id = "ad-34"
        self.module_name = "基础重要度 I₀ 赋值单元"
        
        # 内部状态
        self.state = CalcState.NORMAL
        
        # 统计
        self._stats = AssignmentStats()
        
        # 规则库版本号
        self._rule_version = 1
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] I₀ 赋值单元初始化完成")
        print(f"[{self.module_id}] I₀ 范围: [{self.I0_MIN}, {self.I0_MAX}]")
        print(f"[{self.module_id}] S≥{self.HIGH_S_THRESHOLD} 直达 I₀_MAX")
    
    # ========== 状态管理 ==========
    
    def pause(self) -> None:
        """暂停赋值服务"""
        self.state = CalcState.PAUSED
        print(f"[{self.module_id}] 赋值服务已暂停")
    
    def resume(self) -> None:
        """恢复赋值服务"""
        self.state = CalcState.NORMAL
        print(f"[{self.module_id}] 赋值服务已恢复")
    
    def get_state(self) -> CalcState:
        return self.state
    
    # ========== 主赋值方法 ==========
    
    def assign(self, request: ExperienceWriteRequest) -> I0Result:
        """
        为经验写入请求赋予基础重要度 I₀
        
        处理优先级:
        1. 不可抗力场景 → 直接 I₀ = 0.90
        2. S ≥ 0.90 的安全直达 → 直接 I₀ = 0.90
        3. 正常计算: I₀ = I_base × (1 + 加成) + 事件加权
        4. 特殊环境槽额外加成 +0.05
        
        Args:
            request: 经验写入请求
            
        Returns:
            I₀ 赋值结果
        """
        if self.state == CalcState.PAUSED:
            return I0Result(
                entry_id=request.entry_id,
                i0_value=self.DEFAULT_CONSERVATIVE_I0,
                i_base=0.0, scene_bonus_total=0.0, event_weight_total=0.0,
                assignment_basis="系统暂停，使用保守默认值"
            )
        
        self._stats.total_assigned += 1
        
        # ====== 规则1: 不可抗力场景 ======
        if request.force_majeure or request.result_label == "不可抗力场景":
            self._stats.force_majeure_count += 1
            self._update_avg_i0(self.I0_MAX)
            return I0Result(
                entry_id=request.entry_id,
                i0_value=self.I0_MAX,
                i_base=0.0, scene_bonus_total=0.0, event_weight_total=0.0,
                assignment_basis="不可抗力场景，I₀强制=0.90"
            )
        
        # ====== 规则2: S≥0.90 安全直达 ======
        if request.s_value >= self.HIGH_S_THRESHOLD:
            self._stats.high_s_direct_count += 1
            self._update_avg_i0(self.I0_MAX)
            return I0Result(
                entry_id=request.entry_id,
                i0_value=self.I0_MAX,
                i_base=0.0, scene_bonus_total=0.0, event_weight_total=0.0,
                assignment_basis=f"安全显著性 S={request.s_value:.2f}≥{self.HIGH_S_THRESHOLD}，I₀直达0.90"
            )
        
        # ====== 规则3: 正常计算 ======
        self._stats.normal_count += 1
        
        # 3a. 获取来源基础分值
        i_base = self.SOURCE_BASE.get(request.source, 0.50)
        
        # 3b. 计算场景加成
        scene_bonus_total = self._calculate_scene_bonus(request)
        
        # 3c. 计算事件加权
        event_weight_total = self._calculate_event_weight(request)
        
        # 3d. 综合计算 I₀
        i0 = i_base * (1.0 + scene_bonus_total) + event_weight_total
        i0 = max(self.I0_MIN, min(self.I0_MAX, i0))
        
        # 3e. 特殊环境槽额外加成
        if request.source_slot_id == 18:
            i0 = min(i0 + self.SPECIAL_SLOT_BONUS, self.I0_MAX)
            bonus_note = "，特殊环境槽+0.05"
        else:
            bonus_note = ""
        
        self._update_avg_i0(i0)
        
        # 生成赋值依据
        basis = (f"来源={request.source.value}(I_base={i_base:.2f}), "
                 f"加成={scene_bonus_total:.2f}, "
                 f"事件={event_weight_total:.2f}"
                 f"{bonus_note}")
        
        return I0Result(
            entry_id=request.entry_id,
            i0_value=i0,
            i_base=i_base,
            scene_bonus_total=scene_bonus_total,
            event_weight_total=event_weight_total,
            assignment_basis=basis
        )
    
    def _calculate_scene_bonus(self, request: ExperienceWriteRequest) -> float:
        """
        计算场景加成总和
        
        加成项:
        - has_pedestrian: +0.15（行人/非机动车等高风险目标）
        - extreme_weather: +0.10（暴雨/暴雪/大雾/沙尘暴）
        - night_dark: +0.05（夜间无路灯）
        - unpaved: +0.05（泥土/碎石/沙土路面）
        - high_speed: +0.05（车速>80km/h）
        - novel_scene: +0.10（首次遇到的新场景）
        - strategy_mistake: +0.05（失败教训，仅当 result_label="策略失误"时）
        
        上限: 0.30
        """
        sf = request.scene_features
        total = 0.0
        
        if sf.has_pedestrian:
            total += self.SCENE_BONUS["has_pedestrian"]
        if sf.extreme_weather:
            total += self.SCENE_BONUS["extreme_weather"]
        if sf.night_dark:
            total += self.SCENE_BONUS["night_dark"]
        if sf.unpaved:
            total += self.SCENE_BONUS["unpaved"]
        if sf.high_speed:
            total += self.SCENE_BONUS["high_speed"]
        if sf.novel_scene:
            total += self.SCENE_BONUS["novel_scene"]
        if request.result_label == "策略失误":
            total += self.SCENE_BONUS["strategy_mistake"]
        
        return min(total, self.MAX_SCENE_BONUS)
    
    def _calculate_event_weight(self, request: ExperienceWriteRequest) -> float:
        """
        计算事件加权总和
        
        加权项:
        - collision_avoided: +0.20（成功避免碰撞）
        - force_majeure_handled: +0.30（不可抗力成功应对）
        - regulation_compliant: +0.10（法规合规示范）
        - efficiency_optimized: +0.05（通行效率优化）
        """
        ea = request.event_annotations
        total = 0.0
        
        if ea.collision_avoided:
            total += self.EVENT_WEIGHT["collision_avoided"]
        if ea.force_majeure_handled:
            total += self.EVENT_WEIGHT["force_majeure_handled"]
        if ea.regulation_compliant:
            total += self.EVENT_WEIGHT["regulation_compliant"]
        if ea.efficiency_optimized:
            total += self.EVENT_WEIGHT["efficiency_optimized"]
        
        return total
    
    def _update_avg_i0(self, i0: float) -> None:
        """更新平均 I₀ 统计"""
        n = self._stats.total_assigned
        self._stats.avg_i0 = (self._stats.avg_i0 * (n - 1) + i0) / n if n > 0 else i0
    
    # ========== 规则库管理 ==========
    
    def get_rule_version(self) -> int:
        """获取规则库版本号"""
        return self._rule_version
    
    def update_rules(self, new_rules: Dict[str, Any], signature: str) -> Tuple[bool, str]:
        """
        OTA 更新赋值规则库
        
        S-03: 须经过安全审计与签名校验
        
        Args:
            new_rules: 新规则配置
            signature: 数字签名
            
        Returns:
            (是否成功, 消息)
        """
        # 简化的签名校验
        if signature != f"VALID_SIG_V{self._rule_version + 1}":
            return False, "数字签名校验失败，回退至编译期版本"
        
        self.state = CalcState.UPDATING
        
        # 更新规则（仅允许调整阈值，不允许修改结构）
        if "scene_bonus" in new_rules:
            for key, value in new_rules["scene_bonus"].items():
                if key in self.SCENE_BONUS:
                    self.SCENE_BONUS[key] = max(0.0, min(0.30, value))
        
        if "event_weight" in new_rules:
            for key, value in new_rules["event_weight"].items():
                if key in self.EVENT_WEIGHT:
                    self.EVENT_WEIGHT[key] = max(0.0, min(0.40, value))
        
        self._rule_version += 1
        self.state = CalcState.NORMAL
        
        self._log_event("RULE_UPDATE", {"version": self._rule_version})
        print(f"[{self.module_id}] 规则库已更新至版本 {self._rule_version}")
        
        return True, f"规则库已更新至版本 {self._rule_version}"
    
    # ========== 查询接口 ==========
    
    def get_statistics(self) -> Dict[str, Any]:
        """获取赋值统计"""
        return {
            "total_assigned": self._stats.total_assigned,
            "force_majeure_count": self._stats.force_majeure_count,
            "high_s_direct_count": self._stats.high_s_direct_count,
            "normal_count": self._stats.normal_count,
            "avg_i0": round(self._stats.avg_i0, 4),
            "rule_version": self._rule_version,
            "state": self.state.value
        }
    
    def reset_statistics(self) -> None:
        """重置统计信息"""
        self._stats = AssignmentStats()
    
    # ========== 变更日志 ==========
    
    def _log_event(self, event_type: str, details: Dict[str, Any]) -> None:
        """记录事件日志"""
        self._pending_logs.append({
            "log_id": f"i0-{uuid.uuid4().hex[:8]}",
            "event_type": event_type,
            "details": details,
            "timestamp": time.time()
        })
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-34 基础重要度 I₀ 赋值单元 单元测试")
    print("=" * 60)
    passed, failed = 0, 0
    
    # --- TC-34-01: 常规自动驾驶决策，I₀=0.50 ---
    print("\n[TC-34-01] 常规自动驾驶决策，无加成 → I₀=0.50")
    try:
        assigner = I0Assignment()
        req = ExperienceWriteRequest(
            entry_id="EXP-001",
            source=ExperienceSource.REGULAR_AUTONOMOUS,
            result_label="成功优化"
        )
        result = assigner.assign(req)
        assert abs(result.i0_value - 0.50) < 0.01, f"期望0.50，实际{result.i0_value:.3f}"
        assert assigner._stats.normal_count == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-34-02: 不可抗力场景，I₀=0.90 ---
    print("\n[TC-34-02] 不可抗力场景 → I₀=0.90")
    try:
        assigner = I0Assignment()
        req = ExperienceWriteRequest(
            entry_id="EXP-002",
            result_label="不可抗力场景",
            force_majeure=True
        )
        result = assigner.assign(req)
        assert result.i0_value == 0.90
        assert assigner._stats.force_majeure_count == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-34-03: S≥0.90 直达 I₀=0.90 ---
    print("\n[TC-34-03] S=0.95 ≥ 0.90 → I₀直达0.90")
    try:
        assigner = I0Assignment()
        req = ExperienceWriteRequest(
            entry_id="EXP-003",
            source=ExperienceSource.REGULAR_AUTONOMOUS,
            s_value=0.95
        )
        result = assigner.assign(req)
        assert result.i0_value == 0.90
        assert assigner._stats.high_s_direct_count == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-34-04: 人类示教 + 行人风险 + 极端天气 = 0.875 ---
    print("\n[TC-34-04] 人类示教(0.70) + 行人(0.15) + 极端天气(0.10) → I₀=0.875")
    try:
        assigner = I0Assignment()
        req = ExperienceWriteRequest(
            entry_id="EXP-004",
            source=ExperienceSource.HUMAN_DEMO,
            scene_features=SceneFeatures(has_pedestrian=True, extreme_weather=True)
        )
        result = assigner.assign(req)
        # I_base=0.70, 加成=0.15+0.10=0.25, I₀=0.70×1.25=0.875
        assert abs(result.i0_value - 0.875) < 0.01, f"期望0.875，实际{result.i0_value:.3f}"
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-34-05: 场景加成超上限截断 ---
    print("\n[TC-34-05] 场景加成总和超0.30截断")
    try:
        assigner = I0Assignment()
        # 所有场景加成全开：0.15+0.10+0.05+0.05+0.05+0.10=0.50 > 0.30
        req = ExperienceWriteRequest(
            entry_id="EXP-005",
            source=ExperienceSource.REGULAR_AUTONOMOUS,
            scene_features=SceneFeatures(
                has_pedestrian=True, extreme_weather=True, night_dark=True,
                unpaved=True, high_speed=True, novel_scene=True
            )
        )
        result = assigner.assign(req)
        # I_base=0.50, 加成截断为0.30, I₀=0.50×1.30=0.65
        assert abs(result.i0_value - 0.65) < 0.01, f"期望0.65，实际{result.i0_value:.3f}"
        assert result.scene_bonus_total == 0.30
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-34-06: 低置信度降级路由 I_base=0.35 ---
    print("\n[TC-34-06] 低置信度降级路由 → I_base=0.35")
    try:
        assigner = I0Assignment()
        req = ExperienceWriteRequest(
            entry_id="EXP-006",
            source=ExperienceSource.LOW_CONFIDENCE
        )
        result = assigner.assign(req)
        assert abs(result.i0_value - 0.35) < 0.01, f"期望0.35，实际{result.i0_value:.3f}"
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-34-07: 应急避险操作 I_base=0.80 ---
    print("\n[TC-34-07] 应急避险操作 → I_base=0.80")
    try:
        assigner = I0Assignment()
        req = ExperienceWriteRequest(
            entry_id="EXP-007",
            source=ExperienceSource.EMERGENCY_OP
        )
        result = assigner.assign(req)
        assert abs(result.i0_value - 0.80) < 0.01, f"期望0.80，实际{result.i0_value:.3f}"
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-34-08: 事件加权（碰撞避免+法规合规）---
    print("\n[TC-34-08] 碰撞避免+0.20 + 法规合规+0.10 → I₀=0.80")
    try:
        assigner = I0Assignment()
        req = ExperienceWriteRequest(
            entry_id="EXP-008",
            source=ExperienceSource.REGULAR_AUTONOMOUS,
            event_annotations=EventAnnotations(
                collision_avoided=True, regulation_compliant=True
            )
        )
        result = assigner.assign(req)
        # I_base=0.50, 加成=0, 事件=0.20+0.10=0.30, I₀=0.50+0.30=0.80
        assert abs(result.i0_value - 0.80) < 0.01, f"期望0.80，实际{result.i0_value:.3f}"
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-34-09: 特殊环境槽额外加成 +0.05 ---
    print("\n[TC-34-09] 特殊环境槽(ad-18)额外加成 +0.05")
    try:
        assigner = I0Assignment()
        req = ExperienceWriteRequest(
            entry_id="EXP-009",
            source=ExperienceSource.REGULAR_AUTONOMOUS,
            source_slot_id=18,  # 特殊环境槽
            scene_features=SceneFeatures(extreme_weather=True)
        )
        result = assigner.assign(req)
        # I_base=0.50, 加成=0.10, I₀=0.50×1.10=0.55, +特殊环境槽0.05=0.60
        assert abs(result.i0_value - 0.60) < 0.01, f"期望0.60，实际{result.i0_value:.3f}"
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-34-10: 策略失误经验不受成败标签影响 ---
    print("\n[TC-34-10] 策略失误经验I₀正常计算（不降级）")
    try:
        assigner = I0Assignment()
        req = ExperienceWriteRequest(
            entry_id="EXP-010",
            source=ExperienceSource.REGULAR_AUTONOMOUS,
            result_label="策略失误"
        )
        result = assigner.assign(req)
        # I_base=0.50, 策略失误加成0.05, I₀=0.50×1.05=0.525
        assert abs(result.i0_value - 0.525) < 0.01, f"期望0.525，实际{result.i0_value:.3f}"
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-34-11: 暂停状态使用保守默认值 ---
    print("\n[TC-34-11] 暂停状态 → 使用保守默认值0.40")
    try:
        assigner = I0Assignment()
        assigner.pause()
        req = ExperienceWriteRequest(entry_id="EXP-011")
        result = assigner.assign(req)
        assert result.i0_value == 0.40
        assert "暂停" in result.assignment_basis
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-34-12: I₀ 值上限截断保护 ---
    print("\n[TC-34-12] 各种加成全开后 I₀ 不超过 0.90")
    try:
        assigner = I0Assignment()
        req = ExperienceWriteRequest(
            entry_id="EXP-012",
            source=ExperienceSource.HUMAN_DEMO,  # I_base=0.70
            scene_features=SceneFeatures(
                has_pedestrian=True, extreme_weather=True, night_dark=True,
                unpaved=True, high_speed=True, novel_scene=True
            ),
            event_annotations=EventAnnotations(
                collision_avoided=True, force_majeure_handled=True,
                regulation_compliant=True, efficiency_optimized=True
            )
        )
        result = assigner.assign(req)
        # I_base=0.70, 加成截断=0.30, I₀=0.70×1.30+0.65=1.56 → 截断至0.90
        assert result.i0_value == 0.90, f"期望0.90，实际{result.i0_value:.3f}"
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- 测试结果汇总 ---
    print("\n" + "=" * 60)
    print(f"测试结果: {passed} PASS, {failed} FAIL")
    print("=" * 60)
```