**文件路径：** `ad-mlnf-mem/src/ad-34-i0-assignment.py`

**提交信息：** `添加 ad-34-基础重要度I0赋值单元 代码骨架`

```python
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

安全约束:
  S-01: I₀ 上限硬编码为 0.90，为 I 值动态增量留出空间
  S-02: 不可抗力场景 I₀ 强制设为 0.90，确保顶级安全经验获得最高初始权重
  S-03: I₀ 赋值规则库须经过安全审计与签名校验
  S-04: I₀ 赋值不因经验的“成败”标签而产生极端偏差
"""

from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class ExperienceSource(Enum):
    """经验生成来源"""
    HUMAN_DEMO = "人类示教"
    SIMULATION = "仿真回灌验证"
    AUTONOMOUS_EXPLORE = "系统自主探索"
    REGULAR_AUTONOMOUS = "常规自动驾驶决策"
    LOW_CONFIDENCE = "低置信度降级路由"
    EMERGENCY_OP = "应急避险操作"


class CalcState(Enum):
    """赋值单元内部状态"""
    NORMAL = "normal"
    UPDATING = "updating"
    PAUSED = "paused"


# ==================== 数据结构 ====================

@dataclass
class ExperienceWriteRequest:
    """经验写入请求（简化版）"""
    entry_id: str
    source: ExperienceSource = ExperienceSource.REGULAR_AUTONOMOUS
    result_label: str = "成功优化"          # "成功优化" / "策略失误" / "不可抗力场景"
    force_majeure: bool = False
    s_value: float = 0.0                   # 已计算的 S 值（若有）
    scene_features: Dict[str, Any] = field(default_factory=dict)
    source_slot_id: int = 19


@dataclass
class I0Result:
    """I₀ 赋值结果"""
    entry_id: str
    i0_value: float
    assignment_basis: str
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class I0Assignment:
    """
    基础重要度 I₀ 赋值单元
    
    职责:
    1. 根据经验生成来源查表获取 I_base
    2. 根据场景特征计算加成系数
    3. 根据事件语义标注计算额外加权
    4. 综合计算 I₀ = CLAMP( I_base × (1 + 加成) + 事件加权 , 0.05 , 0.90 )
    """
    
    I0_MIN = 0.05
    I0_MAX = 0.90
    
    # 生成来源基础分值
    SOURCE_BASE = {
        ExperienceSource.HUMAN_DEMO: 0.70,
        ExperienceSource.SIMULATION: 0.60,
        ExperienceSource.AUTONOMOUS_EXPLORE: 0.55,
        ExperienceSource.REGULAR_AUTONOMOUS: 0.50,
        ExperienceSource.LOW_CONFIDENCE: 0.35,
        ExperienceSource.EMERGENCY_OP: 0.80,
    }
    
    # 场景加成系数
    SCENE_BONUS = {
        "high_risk_target": 0.15,      # 高风险目标
        "extreme_weather": 0.10,        # 极端天气
        "night_dark": 0.05,             # 夜间无灯
        "unpaved_road": 0.05,           # 非铺装路面
        "high_speed": 0.05,             # 高速场景
        "novel_scene": 0.10,            # 首次遇到的新场景
        "strategy_mistake": 0.05,       # 失败教训
    }
    MAX_SCENE_BONUS = 0.30
    
    # 事件加权
    EVENT_WEIGHT = {
        "collision_avoided": 0.20,
        "force_majeure_handled": 0.30,
        "regulation_compliant": 0.10,
        "efficiency_optimized": 0.05,
    }
    
    # 特殊环境槽额外加成
    SPECIAL_SLOT_BONUS = 0.05
    
    def __init__(self):
        self.module_id = "ad-34"
        self.module_name = "基础重要度 I₀ 赋值单元"
        self.state = CalcState.NORMAL
        self._total_assigned = 0
        self._pending_logs: List[Dict[str, Any]] = []
        print(f"[{self.module_id}] I₀ 赋值单元初始化完成")
    
    def assign(self, request: ExperienceWriteRequest) -> I0Result:
        """为经验写入请求赋予基础重要度 I₀"""
        if self.state == CalcState.PAUSED:
            return I0Result(request.entry_id, 0.40, "系统暂停，使用保守默认值")
        
        self._total_assigned += 1
        
        # S-02: 不可抗力直接设为上限
        if request.force_majeure or request.result_label == "不可抗力场景":
            return I0Result(request.entry_id, self.I0_MAX, "不可抗力场景")
        
        # S≥0.9 的安全直达
        if request.s_value >= 0.90:
            return I0Result(request.entry_id, self.I0_MAX, f"安全显著性 S={request.s_value:.2f}")
        
        # 获取基础分值
        i_base = self.SOURCE_BASE.get(request.source, 0.50)
        
        # 计算场景加成
        total_bonus = 0.0
        features = request.scene_features
        if features.get("has_pedestrian"):
            total_bonus += self.SCENE_BONUS["high_risk_target"]
        if features.get("extreme_weather"):
            total_bonus += self.SCENE_BONUS["extreme_weather"]
        if features.get("night_dark"):
            total_bonus += self.SCENE_BONUS["night_dark"]
        if features.get("unpaved"):
            total_bonus += self.SCENE_BONUS["unpaved_road"]
        if features.get("high_speed"):
            total_bonus += self.SCENE_BONUS["high_speed"]
        if features.get("novel_scene"):
            total_bonus += self.SCENE_BONUS["novel_scene"]
        if request.result_label == "策略失误":
            total_bonus += self.SCENE_BONUS["strategy_mistake"]
        total_bonus = min(total_bonus, self.MAX_SCENE_BONUS)
        
        # 计算事件加权
        event_weight = 0.0
        if features.get("collision_avoided"):
            event_weight += self.EVENT_WEIGHT["collision_avoided"]
        if features.get("force_majeure_handled"):
            event_weight += self.EVENT_WEIGHT["force_majeure_handled"]
        if features.get("regulation_compliant"):
            event_weight += self.EVENT_WEIGHT["regulation_compliant"]
        if features.get("efficiency_optimized"):
            event_weight += self.EVENT_WEIGHT["efficiency_optimized"]
        
        i0 = i_base * (1 + total_bonus) + event_weight
        i0 = max(self.I0_MIN, min(self.I0_MAX, i0))
        
        # 特殊环境槽额外加成
        if request.source_slot_id == 18:
            i0 = min(i0 + self.SPECIAL_SLOT_BONUS, self.I0_MAX)
        
        basis = f"来源={request.source.value}, 加成={total_bonus:.2f}, 事件={event_weight:.2f}"
        return I0Result(request.entry_id, i0, basis)
    
    def get_statistics(self) -> Dict[str, Any]:
        return {"total_assigned": self._total_assigned, "state": self.state.value}
    
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
    
    # TC-34-01: 常规决策 I₀=0.50
    print("\n[TC-34-01] 常规决策 I₀=0.50")
    try:
        assigner = I0Assignment()
        req = ExperienceWriteRequest("EXP-001", source=ExperienceSource.REGULAR_AUTONOMOUS)
        result = assigner.assign(req)
        assert abs(result.i0_value - 0.50) < 0.01
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-34-02: 不可抗力 I₀=0.90
    print("\n[TC-34-02] 不可抗力 I₀=0.90")
    try:
        assigner = I0Assignment()
        req = ExperienceWriteRequest("EXP-002", force_majeure=True)
        result = assigner.assign(req)
        assert result.i0_value == 0.90
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-34-03: 人类示教 + 行人风险 + 极端天气
    print("\n[TC-34-03] 人类示教 + 行人 + 极端天气")
    try:
        assigner = I0Assignment()
        req = ExperienceWriteRequest("EXP-003", source=ExperienceSource.HUMAN_DEMO,
                                     scene_features={"has_pedestrian": True, "extreme_weather": True})
        result = assigner.assign(req)
        expected = 0.70 * (1 + 0.15 + 0.10)  # = 0.875
        assert abs(result.i0_value - expected) < 0.02
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-34-04: 加成上限截断
    print("\n[TC-34-04] 加成上限截断")
    try:
        assigner = I0Assignment()
        req = ExperienceWriteRequest("EXP-004", source=ExperienceSource.REGULAR_AUTONOMOUS,
                                     scene_features={k: True for k in assigner.SCENE_BONUS})
        result = assigner.assign(req)
        assert result.i0_value <= 0.90
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-34-05: 低置信度降级路由 I_base=0.35
    print("\n[TC-34-05] 低置信度降级 I_base=0.35")
    try:
        assigner = I0Assignment()
        req = ExperienceWriteRequest("EXP-005", source=ExperienceSource.LOW_CONFIDENCE)
        result = assigner.assign(req)
        assert abs(result.i0_value - 0.35) < 0.01
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    print(f"\n测试结果: {passed} PASS, {failed} FAIL")
```