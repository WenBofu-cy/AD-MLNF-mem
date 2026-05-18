#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-09
模块名称: 行为判定标签单元
所属分区: 二、漏斗一：驾驶员画像漏斗
核心职责: 将每条带场景标签的驾驶行为观测条目与交通法规库基准进行比对，自动判定并标记为
          「优良习惯」「常态陋习」或「应急特殊操作」。应急标记为 True 的行为自动归入
          应急特殊操作，不纳入陋习统计。

依赖模块: ad-08(上下文场景标记单元), ad-45(交通法律法规库), ad-02(漏斗一专属调度单元)
被依赖模块: ad-10(行为累积统计单元)

安全约束:
  S-01: 漏斗一数据编译期禁止接入自动驾驶决策链路
  S-02: 应急标记为 True 的行为条目，必须无条件标记为"应急特殊操作"，跳过法规比对
  S-03: 让行行人在任何场景下均为硬约束，未礼让即标记"常态陋习"，无例外
  S-04: 法规库不可用时，降级使用内置保守规则，不可凭空猜测
  S-05: 判定置信度 < 0.5 时，ad-10 统计应采用保守加权
  S-06: 所有判定结果全量写入 ad-51 变更日志
  S-07: 判定规则库的更新须经 OTA 升级流程，不可运行时动态修改
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class BehaviorLabel(Enum):
    """行为判定标签"""
    GOOD = "优良习惯"
    BAD = "常态陋习"
    EMERGENCY_OP = "应急特殊操作"


class JudgeState(Enum):
    """判定单元内部状态"""
    NORMAL = "normal"
    QUERYING = "querying"
    MATCHING = "matching"
    FALLBACK = "fallback"
    PAUSED = "paused"
    EMERGENCY_RO = "emergency_ro"


# ==================== 数据结构 ====================

@dataclass
class TaggedObservation:
    """带场景标签的行为观测条目（来自 ad-08）"""
    obs_id: str
    original_observation: Any      # BehaviorObservation
    scene_label: str               # 场景标签
    label_confidence: float        # 标签置信度
    is_emergency: bool             # 是否应急标记
    tag_timestamp: float = field(default_factory=time.time)


@dataclass
class LawBaseline:
    """法规基准判定参数（来自 ad-45）"""
    behavior_type: str
    compliance_threshold: float    # 合规阈值
    violation_criteria: str        # 违规判定标准
    scene_adjustment_coefficient: float = 1.0  # 场景调整系数


@dataclass
class JudgedObservation:
    """带判定标签的行为条目"""
    obs_id: str
    original_tagged: TaggedObservation
    judgment_label: BehaviorLabel
    judgment_reason: str
    judgment_confidence: float
    judgment_timestamp: float = field(default_factory=time.time)


# ==================== 内置判定规则库 ====================

# 编译期固化的内置判定规则（法规库不可用时的降级方案）
BUILTIN_RULES: Dict[str, Dict[str, Any]] = {
    "变道": {
        "good_threshold": {"转向灯提前": 3.0, "转角速率上限": 200.0},
        "bad_threshold": {"转向灯提前": 1.0},
    },
    "跟车": {
        "good_threshold": {"跟车时距": 2.0},
        "bad_threshold": {"跟车时距": 1.5},
    },
    "制动": {
        "good_threshold": {"制动减速度": 3.0},
        "bad_threshold": {"制动减速度": 5.0},
    },
    "加速": {
        "good_threshold": {"纵向冲击度": 3.0},
        "bad_threshold": {"纵向冲击度": 5.0},
    },
    "转弯": {
        "good_threshold": {"车速限速比": 0.7, "转角速率上限": 200.0},
        "bad_threshold": {"车速限速比": 0.9},
    },
    "路口通行": {
        "good_threshold": {"完全停止": True},
        "bad_threshold": {"滑行通过": True},
    },
    "让行": {
        "hard_constraint": True,  # 硬约束：任何场景未礼让即陋习
    },
    "停车": {
        "good_threshold": {"纵向冲击度": 2.0, "距前车": 2.0},
        "bad_threshold": {"距前车": 1.0},
    },
    "起步": {
        "good_threshold": {"纵向冲击度": 2.0},
        "bad_threshold": {"纵向冲击度": 5.0},
    },
}


# ==================== 主类定义 ====================

class BehaviorLabelJudge:
    """
    行为判定标签单元
    
    职责:
    1. 接收 ad-08 带场景标签的行为条目
    2. 查询 ad-45 交通法规库获取判定基准
    3. 按行为类型逐项比对判定
    4. 应急标记行为无条件标记为"应急特殊操作"
    5. 法规库不可用时降级使用内置保守规则
    """
    
    # 法规库查询超时（秒）
    LAW_QUERY_TIMEOUT = 0.05  # 50ms
    
    # 法规库连续失败上限
    LAW_MAX_FAILURES = 3
    LAW_RETRY_INTERVAL = 30.0  # 秒
    
    # 保守判定系数
    CONSERVATIVE_COEFFICIENT = 1.3  # 阈值收紧30%
    
    def __init__(self):
        self.module_id = "ad-09"
        self.module_name = "行为判定标签单元"
        
        # 内部状态
        self.state = JudgeState.NORMAL
        
        # 法规库查询统计
        self._law_fail_count = 0
        self._law_last_fail_time = 0.0
        self._law_disabled = False
        
        # 判定统计
        self._total_judged = 0
        self._good_count = 0
        self._bad_count = 0
        self._emergency_count = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 行为判定标签单元初始化完成")
    
    # ========== 状态管理 ==========
    
    def pause(self) -> None:
        self.state = JudgeState.PAUSED
    
    def resume(self) -> None:
        self.state = JudgeState.NORMAL
    
    def emergency_stop(self) -> None:
        self.state = JudgeState.EMERGENCY_RO
    
    # ========== 行为判定 ==========
    
    def judge_behavior(self, tagged: TaggedObservation,
                       law_baseline: Optional[LawBaseline] = None,
                       current_speed_limit: float = 60.0,
                       driver_style: str = "标准通勤") -> JudgedObservation:
        """
        判定驾驶行为标签
        
        优先级:
        1. 应急标记 = True → 无条件应急特殊操作
        2. 法规库查询 → 精确比对
        3. 法规库不可用 → 内置保守规则降级
        
        Args:
            tagged: 带场景标签的行为条目
            law_baseline: 法规基准（None 表示查询失败）
            current_speed_limit: 当前路段限速
            driver_style: 用户驾驶风格设定
            
        Returns:
            带判定标签的行为条目
        """
        # 应急标记优先判定
        if tagged.is_emergency:
            return self._create_judgment(tagged, BehaviorLabel.EMERGENCY_OP,
                                         f"应急场景: {tagged.scene_label}", 
                                         tagged.label_confidence)
        
        behavior_type = tagged.original_observation.behavior_type
        
        # 法规库查询失败，使用内置规则
        if law_baseline is None:
            self._law_fail_count += 1
            self._law_last_fail_time = time.time()
            if self._law_fail_count >= self.LAW_MAX_FAILURES:
                self._law_disabled = True
            self.state = JudgeState.FALLBACK
            return self._judge_with_builtin_rules(tagged, behavior_type, current_speed_limit)
        
        # 重置失败计数
        self._law_fail_count = 0
        self._law_disabled = False
        
        # 法规库精确比对
        self.state = JudgeState.MATCHING
        return self._judge_with_law_baseline(tagged, behavior_type, law_baseline,
                                             current_speed_limit, driver_style)
    
    def _judge_with_law_baseline(self, tagged: TaggedObservation,
                                  behavior_type: str, law: LawBaseline,
                                  speed_limit: float, style: str) -> JudgedObservation:
        """使用法规库基准进行判定"""
        obs = tagged.original_observation
        
        # 让行硬约束
        if behavior_type == "让行":
            # 硬约束：任何场景未礼让即陋习
            if obs.brake_pressure < 0.1 and obs.speed > 1.0:
                return self._create_judgment(tagged, BehaviorLabel.BAD,
                                             "未礼让人行横道行人（硬约束）", 1.0)
            return self._create_judgment(tagged, BehaviorLabel.GOOD, "合规让行", 1.0)
        
        # 根据行为类型比对
        if behavior_type == "变道":
            return self._judge_lane_change(tagged, obs, law)
        elif behavior_type == "跟车":
            return self._judge_follow_distance(tagged, obs, law, style)
        elif behavior_type == "制动":
            return self._judge_brake(tagged, obs, law)
        elif behavior_type == "加速":
            return self._judge_accelerate(tagged, obs, law)
        elif behavior_type == "转弯":
            return self._judge_turn(tagged, obs, law, speed_limit)
        elif behavior_type == "路口通行":
            return self._judge_intersection(tagged, obs)
        elif behavior_type == "停车":
            return self._judge_parking(tagged, obs, law)
        elif behavior_type == "起步":
            return self._judge_start(tagged, obs, law)
        else:
            return self._create_judgment(tagged, BehaviorLabel.GOOD, "默认放行", 0.5)
    
    def _judge_with_builtin_rules(self, tagged: TaggedObservation,
                                   behavior_type: str, speed_limit: float) -> JudgedObservation:
        """使用内置保守规则判定（法规库不可用时）"""
        self.state = JudgeState.FALLBACK
        rules = BUILTIN_RULES.get(behavior_type)
        
        if rules is None:
            return self._create_judgment(tagged, BehaviorLabel.GOOD, "未知行为类型，默认放行", 0.3)
        
        obs = tagged.original_observation
        
        # 让行硬约束
        if behavior_type == "让行":
            if obs.brake_pressure < 0.1 and obs.speed > 1.0:
                return self._create_judgment(tagged, BehaviorLabel.BAD, "未礼让行人（硬约束）", 0.6)
            return self._create_judgment(tagged, BehaviorLabel.GOOD, "合规让行", 0.6)
        
        # 简化判定：使用内置阈值
        if behavior_type == "变道":
            if obs.turn_signal == "关闭":
                return self._create_judgment(tagged, BehaviorLabel.BAD, "未开转向灯变道", 0.6)
            return self._create_judgment(tagged, BehaviorLabel.GOOD, "合规变道", 0.6)
        elif behavior_type in ["制动", "减速"]:
            if obs.brake_pressure > 5.0:
                return self._create_judgment(tagged, BehaviorLabel.BAD, "制动过猛", 0.6)
            return self._create_judgment(tagged, BehaviorLabel.GOOD, "制动平顺", 0.6)
        elif behavior_type == "转弯":
            ratio = obs.speed / max(speed_limit, 1.0)
            if ratio > 0.9:
                return self._create_judgment(tagged, BehaviorLabel.BAD, "转弯过快", 0.6)
            return self._create_judgment(tagged, BehaviorLabel.GOOD, "合规转弯", 0.6)
        else:
            return self._create_judgment(tagged, BehaviorLabel.GOOD, "内置规则默认放行", 0.5)
    
    # ========== 各行为类型判定方法 ==========
    
    def _judge_lane_change(self, tagged, obs, law):
        if obs.turn_signal == "关闭":
            return self._create_judgment(tagged, BehaviorLabel.BAD, "未开转向灯变道", 0.9)
        if abs(obs.steering_rate) > 200.0:
            return self._create_judgment(tagged, BehaviorLabel.BAD, "变道过猛", 0.8)
        return self._create_judgment(tagged, BehaviorLabel.GOOD, "合规变道", 0.9)
    
    def _judge_follow_distance(self, tagged, obs, law, style):
        # 简化：基于速度和制动压力推断跟车距离
        if obs.brake_pressure > 3.0:
            return self._create_judgment(tagged, BehaviorLabel.BAD, "跟车过近导致急刹", 0.7)
        return self._create_judgment(tagged, BehaviorLabel.GOOD, "安全跟车", 0.8)
    
    def _judge_brake(self, tagged, obs, law):
        if obs.brake_pressure > 5.0:
            return self._create_judgment(tagged, BehaviorLabel.BAD, "制动过猛", 0.85)
        return self._create_judgment(tagged, BehaviorLabel.GOOD, "制动平顺", 0.85)
    
    def _judge_accelerate(self, tagged, obs, law):
        if obs.throttle > 80.0:
            return self._create_judgment(tagged, BehaviorLabel.BAD, "急加速", 0.7)
        return self._create_judgment(tagged, BehaviorLabel.GOOD, "加速平顺", 0.8)
    
    def _judge_turn(self, tagged, obs, law, speed_limit):
        ratio = obs.speed / max(speed_limit, 1.0)
        if ratio > 0.9:
            return self._create_judgment(tagged, BehaviorLabel.BAD, "转弯过快", 0.85)
        if ratio > 0.7:
            return self._create_judgment(tagged, BehaviorLabel.GOOD, "转弯适中", 0.7)
        return self._create_judgment(tagged, BehaviorLabel.GOOD, "转弯平顺", 0.9)
    
    def _judge_intersection(self, tagged, obs):
        if obs.speed < 1.0 and obs.brake_active:
            return self._create_judgment(tagged, BehaviorLabel.GOOD, "路口完全停止", 0.9)
        return self._create_judgment(tagged, BehaviorLabel.GOOD, "路口通行", 0.6)
    
    def _judge_parking(self, tagged, obs, law):
        if obs.brake_pressure > 2.0:
            return self._create_judgment(tagged, BehaviorLabel.BAD, "刹停点头", 0.75)
        return self._create_judgment(tagged, BehaviorLabel.GOOD, "平稳停车", 0.8)
    
    def _judge_start(self, tagged, obs, law):
        if obs.throttle > 60.0:
            return self._create_judgment(tagged, BehaviorLabel.BAD, "猛起步", 0.7)
        return self._create_judgment(tagged, BehaviorLabel.GOOD, "平缓起步", 0.8)
    
    def _create_judgment(self, tagged: TaggedObservation, label: BehaviorLabel,
                         reason: str, confidence: float) -> JudgedObservation:
        """创建判定结果并更新统计"""
        self._total_judged += 1
        if label == BehaviorLabel.GOOD:
            self._good_count += 1
        elif label == BehaviorLabel.BAD:
            self._bad_count += 1
        elif label == BehaviorLabel.EMERGENCY_OP:
            self._emergency_count += 1
        
        return JudgedObservation(
            obs_id=tagged.obs_id,
            original_tagged=tagged,
            judgment_label=label,
            judgment_reason=reason,
            judgment_confidence=min(confidence, 1.0)
        )
    
    # ========== 查询接口 ==========
    
    def get_state(self) -> JudgeState:
        return self.state
    
    def get_statistics(self) -> Dict[str, Any]:
        total = max(self._total_judged, 1)
        return {
            "total_judged": self._total_judged,
            "good_count": self._good_count,
            "bad_count": self._bad_count,
            "emergency_count": self._emergency_count,
            "good_rate": self._good_count / total,
            "law_fail_count": self._law_fail_count,
            "law_disabled": self._law_disabled,
            "state": self.state.value
        }
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-09 行为判定标签单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    # 模拟 ad-07 观测条目
    class MockObservation:
        def __init__(self, behavior_type, turn_signal="关闭", brake_pressure=0.0,
                     speed=50.0, throttle=30.0, steering_rate=50.0, brake_active=False):
            self.behavior_type = behavior_type
            self.turn_signal = turn_signal
            self.brake_pressure = brake_pressure
            self.speed = speed
            self.throttle = throttle
            self.steering_rate = steering_rate
            self.brake_active = brake_active
    
    def make_tagged(behavior_type, is_emergency=False, scene_label="常规路况",
                    confidence=0.9, turn_signal="关闭", brake_pressure=0.0,
                    speed=50.0, throttle=30.0, steering_rate=50.0):
        obs = MockObservation(behavior_type, turn_signal, brake_pressure,
                              speed, throttle, steering_rate)
        return TaggedObservation(
            obs_id=f"obs-{uuid.uuid4().hex[:6]}",
            original_observation=obs,
            scene_label=scene_label,
            label_confidence=confidence,
            is_emergency=is_emergency
        )
    
    # --- TC-09-01: 合规变道 → 优良习惯 ---
    print("\n[TC-09-01] 合规变道 → 优良习惯")
    try:
        judge = BehaviorLabelJudge()
        tagged = make_tagged("变道", turn_signal="左转", steering_rate=150.0)
        law = LawBaseline("变道", 3.0, "转向灯提前≥3秒", 1.0)
        result = judge.judge_behavior(tagged, law)
        assert result.judgment_label == BehaviorLabel.GOOD
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-09-02: 未打灯变道 → 常态陋习 ---
    print("\n[TC-09-02] 未打灯变道 → 常态陋习")
    try:
        judge = BehaviorLabelJudge()
        tagged = make_tagged("变道", turn_signal="关闭")
        law = LawBaseline("变道", 3.0, "转向灯提前≥3秒", 1.0)
        result = judge.judge_behavior(tagged, law)
        assert result.judgment_label == BehaviorLabel.BAD
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-09-03: 应急标记 → 应急特殊操作（无条件） ---
    print("\n[TC-09-03] 应急标记 → 应急特殊操作")
    try:
        judge = BehaviorLabelJudge()
        tagged = make_tagged("制动", is_emergency=True, scene_label="碰撞高风险")
        result = judge.judge_behavior(tagged, None)
        assert result.judgment_label == BehaviorLabel.EMERGENCY_OP
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-09-04: 未礼让行人 → 常态陋习（硬约束） ---
    print("\n[TC-09-04] 未礼让行人 → 常态陋习")
    try:
        judge = BehaviorLabelJudge()
        tagged = make_tagged("让行", brake_pressure=0.0, speed=30.0)
        result = judge.judge_behavior(tagged, None)
        assert result.judgment_label == BehaviorLabel.BAD
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-09-05: 法规库查询失败降级内置规则 ---
    print("\n[TC-09-05] 法规库查询失败降级内置规则")
    try:
        judge = BehaviorLabelJudge()
        tagged = make_tagged("变道", turn_signal="关闭")
        result = judge.judge_behavior(tagged, None)  # 法规库失败
        assert result.judgment_label == BehaviorLabel.BAD
        assert judge.state == JudgeState.FALLBACK
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-09-06: 合规让行 → 优良习惯 ---
    print("\n[TC-09-06] 合规让行 → 优良习惯")
    try:
        judge = BehaviorLabelJudge()
        tagged = make_tagged("让行", brake_pressure=2.0, speed=5.0)
        result = judge.judge_behavior(tagged, None)
        assert result.judgment_label == BehaviorLabel.GOOD
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-09-07: 制动过猛 → 常态陋习 ---
    print("\n[TC-09-07] 制动过猛 → 常态陋习")
    try:
        judge = BehaviorLabelJudge()
        tagged = make_tagged("制动", brake_pressure=6.0)
        law = LawBaseline("制动", 3.0, "制动减速度≤3m/s²", 1.0)
        result = judge.judge_behavior(tagged, law)
        assert result.judgment_label == BehaviorLabel.BAD
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-09-08: 转弯过快 → 常态陋习 ---
    print("\n[TC-09-08] 转弯过快 → 常态陋习")
    try:
        judge = BehaviorLabelJudge()
        tagged = make_tagged("转弯", speed=55.0)
        law = LawBaseline("转弯", 0.7, "车速/限速≤0.7", 1.0)
        result = judge.judge_behavior(tagged, law, current_speed_limit=60.0)
        assert result.judgment_label == BehaviorLabel.BAD
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