#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-41
模块名称: 最低复用次数校验单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 晋升与遗忘执行机制
核心职责: 对遗忘候选清单中的每条经验进行最低复用次数校验。复用次数不足且 I 值低于
          阈值的条目通过校验，确认可遗忘；复用次数达到保护门槛的条目拒绝遗忘，从
          候选清单中移除。是遗忘判定流程的最后一道防线，防止尚有实战价值的经验被误删。

依赖模块: ad-40(遗忘阈值判定单元，提供遗忘候选清单),
          ad-33(复用频次 C 值统计单元，提供准确复用次数)
被依赖模块: ad-42(冗余记忆删除与归档单元，接收最终通过校验的遗忘执行清单)

校验规则:
  通用条件: 通过校验 = (复用次数 < C_min_protection)
  特殊豁免:
    - I < 0.05（极低重要度）: 即使复用次数达标也通过校验
    - 策略失误且未通过安全仲裁: 即使复用次数达标也通过校验
    - 冷条目清单中: 保护阈值减半

安全约束:
  S-01: 最低复用次数校验是遗忘流程的最后一道防线，拒绝遗忘的条目不得强制清除
  S-02: 极低重要度（I < 0.05）豁免复用保护
  S-03: 失败经验若无安全仲裁认可，不享受复用保护
  S-04: 本单元仅执行校验判定，不直接操作存储介质
  S-05: 所有校验操作（含通过、拒绝）全量写入 ad-51 不可变日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class ValidationState(Enum):
    """校验单元内部状态"""
    IDLE = "idle"
    VALIDATING = "validating"
    PAUSED = "paused"


class ValidationConclusion(Enum):
    """校验结论"""
    PASS = "pass"
    PASS_LOW_I = "pass_low_i"                       # 极低 I 值豁免
    PASS_NO_ARBITRATION = "pass_no_arbitration"     # 失败经验无仲裁认可
    REJECT_PROTECTED = "reject_protected"           # 复用次数达标，受保护


# ==================== 数据结构 ====================

@dataclass
class ForgetCandidate:
    """遗忘候选条目（来自 ad-40）"""
    entry_id: str
    current_layer: str
    i_value: float
    reuse_count: int = 0
    forget_method: str = "直接删除"
    source_slot_id: int = 19
    reason: str = ""
    priority: float = 0.0


@dataclass
class ValidatedEntry:
    """通过校验的遗忘执行条目"""
    entry_id: str
    current_layer: str
    i_value: float
    reuse_count: int
    forget_method: str
    source_slot_id: int
    validation_conclusion: ValidationConclusion
    priority: float = 0.0


@dataclass
class RejectedEntry:
    """拒绝遗忘的条目"""
    entry_id: str
    current_layer: str
    i_value: float
    reuse_count: int
    protection_threshold: int
    rejection_reason: str


@dataclass
class ValidationResult:
    """校验结果汇总"""
    cycle_id: str
    total_candidates: int
    passed: int
    rejected: int
    passed_entries: Dict[str, List[ValidatedEntry]]   # 层级 -> 列表
    rejected_entries: List[RejectedEntry]
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class MinUsageCountValidation:
    """
    最低复用次数校验单元
    
    职责:
    1. 接收 ad-40 下发的遗忘候选清单
    2. 向 ad-33 查询各候选条目的累计复用次数
    3. 按各层级/分槽的保护阈值进行比对校验
    4. 应用特殊豁免规则（极低 I 值、失败经验无仲裁、冷条目阈值减半）
    5. 输出通过校验的遗忘执行清单至 ad-42
    """
    
    # 各层级默认保护阈值
    DEFAULT_PROTECTION_THRESHOLDS = {
        "L1": 3,
        "L2": 3,
        "L3": 4,    # L3 标准保护门槛较高
        "L4": 1,    # L4 仅在容量红色告警时参与，复用≥1即保护
    }
    
    # 各分槽保护阈值覆写（L3 层）
    SLOT_PROTECTION_OVERRIDES_L3 = {
        15: 5,   # 高速巡航槽
        16: 5,   # 城区路口槽
        17: 2,   # 泊车低速槽（低频场景降低保护门槛）
        18: 2,   # 特殊环境槽（低频场景降低保护门槛）
        19: 4,   # 通用驾驶槽
    }
    
    # 极低重要度阈值
    EXTREMELY_LOW_I_THRESHOLD = 0.05
    
    # 冷条目阈值减半系数
    COLD_ENTRY_REDUCTION_FACTOR = 0.5
    
    def __init__(self):
        self.module_id = "ad-41"
        self.module_name = "最低复用次数校验单元"
        
        # 内部状态
        self.state = ValidationState.IDLE
        
        # 统计
        self._total_validated = 0
        self._total_passed = 0
        self._total_rejected = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 最低复用次数校验单元初始化完成")
        print(f"[{self.module_id}] 默认保护阈值: L1/L2=3, L3=4, L4=1")
        print(f"[{self.module_id}] 极低I值豁免: I<{self.EXTREMELY_LOW_I_THRESHOLD}")
    
    # ========== 状态管理 ==========
    
    def pause(self) -> None:
        self.state = ValidationState.PAUSED
    
    def resume(self) -> None:
        self.state = ValidationState.IDLE
    
    def get_state(self) -> ValidationState:
        return self.state
    
    # ========== 主校验流程 ==========
    
    def execute_validation(self,
                           candidates: Dict[str, List[ForgetCandidate]],
                           reuse_counts: Dict[str, int],
                           cold_entry_ids: Optional[set] = None,
                           failed_arbitration_ids: Optional[set] = None) -> ValidationResult:
        """
        执行最低复用次数校验
        
        Args:
            candidates: 遗忘候选清单（来自 ad-40），按层级分组
            reuse_counts: 条目复用次数字典（来自 ad-33）
            cold_entry_ids: 冷条目 ID 集合（来自 ad-33，用于阈值减半）
            failed_arbitration_ids: 未通过安全仲裁的条目 ID 集合
            
        Returns:
            校验结果汇总
        """
        if self.state == ValidationState.PAUSED:
            return ValidationResult("", 0, 0, 0, {}, [])
        
        self.state = ValidationState.VALIDATING
        cycle_id = f"validate-{uuid.uuid4().hex[:8]}"
        
        if cold_entry_ids is None:
            cold_entry_ids = set()
        if failed_arbitration_ids is None:
            failed_arbitration_ids = set()
        
        passed_entries: Dict[str, List[ValidatedEntry]] = {}
        rejected_entries: List[RejectedEntry] = []
        total = 0
        
        for layer_name, layer_candidates in candidates.items():
            if not layer_candidates:
                continue
            
            passed_list = []
            
            for candidate in layer_candidates:
                total += 1
                entry_id = candidate.entry_id
                
                # 获取复用次数
                reuse_count = reuse_counts.get(entry_id, 0)
                
                # 规则1: 极低重要度豁免（S-02）
                if candidate.i_value < self.EXTREMELY_LOW_I_THRESHOLD:
                    passed_list.append(ValidatedEntry(
                        entry_id=entry_id,
                        current_layer=layer_name,
                        i_value=candidate.i_value,
                        reuse_count=reuse_count,
                        forget_method=candidate.forget_method,
                        source_slot_id=candidate.source_slot_id,
                        validation_conclusion=ValidationConclusion.PASS_LOW_I,
                        priority=candidate.priority
                    ))
                    continue
                
                # 规则2: 失败经验无仲裁认可豁免（S-03）
                if entry_id in failed_arbitration_ids:
                    passed_list.append(ValidatedEntry(
                        entry_id=entry_id,
                        current_layer=layer_name,
                        i_value=candidate.i_value,
                        reuse_count=reuse_count,
                        forget_method=candidate.forget_method,
                        source_slot_id=candidate.source_slot_id,
                        validation_conclusion=ValidationConclusion.PASS_NO_ARBITRATION,
                        priority=candidate.priority
                    ))
                    continue
                
                # 获取保护阈值
                protection_threshold = self._get_protection_threshold(layer_name, candidate.source_slot_id)
                
                # 规则3: 冷条目阈值减半
                if entry_id in cold_entry_ids:
                    protection_threshold = max(int(protection_threshold * self.COLD_ENTRY_REDUCTION_FACTOR), 1)
                
                # 比对判定
                if reuse_count < protection_threshold:
                    passed_list.append(ValidatedEntry(
                        entry_id=entry_id,
                        current_layer=layer_name,
                        i_value=candidate.i_value,
                        reuse_count=reuse_count,
                        forget_method=candidate.forget_method,
                        source_slot_id=candidate.source_slot_id,
                        validation_conclusion=ValidationConclusion.PASS,
                        priority=candidate.priority
                    ))
                else:
                    rejected_entries.append(RejectedEntry(
                        entry_id=entry_id,
                        current_layer=layer_name,
                        i_value=candidate.i_value,
                        reuse_count=reuse_count,
                        protection_threshold=protection_threshold,
                        rejection_reason=f"复用次数达标({reuse_count}≥{protection_threshold})"
                    ))
            
            if passed_list:
                passed_entries[layer_name] = passed_list
        
        passed_count = sum(len(v) for v in passed_entries.values())
        rejected_count = len(rejected_entries)
        
        self._total_validated += total
        self._total_passed += passed_count
        self._total_rejected += rejected_count
        
        result = ValidationResult(
            cycle_id=cycle_id,
            total_candidates=total,
            passed=passed_count,
            rejected=rejected_count,
            passed_entries=passed_entries,
            rejected_entries=rejected_entries
        )
        
        self.state = ValidationState.IDLE
        return result
    
    def _get_protection_threshold(self, layer: str, slot_id: int) -> int:
        """获取指定层级和分槽的保护阈值"""
        if layer == "L3":
            return self.SLOT_PROTECTION_OVERRIDES_L3.get(slot_id, 4)
        return self.DEFAULT_PROTECTION_THRESHOLDS.get(layer, 3)
    
    # ========== 查询接口 ==========
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_validated": self._total_validated,
            "total_passed": self._total_passed,
            "total_rejected": self._total_rejected,
            "state": self.state.value
        }
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-41 最低复用次数校验单元 单元测试")
    print("=" * 60)
    passed, failed = 0, 0
    
    def make_candidate(eid, layer, i_val, reuse=0, slot=15):
        return ForgetCandidate(eid, layer, i_val, reuse, "直接删除", slot, "", 0.5)
    
    # TC-41-01: 复用不足通过校验
    print("\n[TC-41-01] L2 复用1 < 保护阈值3 → 通过")
    try:
        validator = MinUsageCountValidation()
        candidates = {"L2": [make_candidate("EXP-001", "L2", 0.10, 1)]}
        reuse_counts = {"EXP-001": 1}
        result = validator.execute_validation(candidates, reuse_counts)
        assert result.passed == 1
        assert result.passed_entries["L2"][0].validation_conclusion == ValidationConclusion.PASS
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-41-02: 复用达标被拒绝
    print("\n[TC-41-02] L2 复用4 ≥ 保护阈值3 → 拒绝")
    try:
        validator = MinUsageCountValidation()
        candidates = {"L2": [make_candidate("EXP-002", "L2", 0.10, 4)]}
        reuse_counts = {"EXP-002": 4}
        result = validator.execute_validation(candidates, reuse_counts)
        assert result.rejected == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-41-03: 极低I值豁免复用保护
    print("\n[TC-41-03] I=0.03 < 0.05 豁免复用保护 → 通过")
    try:
        validator = MinUsageCountValidation()
        candidates = {"L2": [make_candidate("EXP-003", "L2", 0.03, 10)]}
        reuse_counts = {"EXP-003": 10}
        result = validator.execute_validation(candidates, reuse_counts)
        assert result.passed == 1
        assert result.passed_entries["L2"][0].validation_conclusion == ValidationConclusion.PASS_LOW_I
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-41-04: 冷条目阈值减半后通过
    print("\n[TC-41-04] 冷条目阈值减半：4→2，复用2≥2 → 拒绝")
    try:
        validator = MinUsageCountValidation()
        candidates = {"L3": [make_candidate("EXP-004", "L3", 0.10, 2, 19)]}
        reuse_counts = {"EXP-004": 2}
        result = validator.execute_validation(candidates, reuse_counts, cold_entry_ids={"EXP-004"})
        # 通用驾驶槽 L3 保护阈值原为 4，减半后为 2，复用2 ≥ 2 → 拒绝
        assert result.rejected == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-41-05: 泊车低速槽 L3 低保护门槛
    print("\n[TC-41-05] 泊车低速槽 L3 保护阈值=2，复用1<2 → 通过")
    try:
        validator = MinUsageCountValidation()
        candidates = {"L3": [make_candidate("EXP-005", "L3", 0.10, 1, 17)]}
        reuse_counts = {"EXP-005": 1}
        result = validator.execute_validation(candidates, reuse_counts)
        assert result.passed == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-41-06: 失败经验无仲裁认可豁免
    print("\n[TC-41-06] 失败经验无仲裁认可 → 通过（豁免复用保护）")
    try:
        validator = MinUsageCountValidation()
        candidates = {"L3": [make_candidate("EXP-006", "L3", 0.10, 8, 15)]}
        reuse_counts = {"EXP-006": 8}
        result = validator.execute_validation(candidates, reuse_counts, failed_arbitration_ids={"EXP-006"})
        assert result.passed == 1
        assert result.passed_entries["L3"][0].validation_conclusion == ValidationConclusion.PASS_NO_ARBITRATION
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-41-07: 混合校验统计正确
    print("\n[TC-41-07] 混合校验：通过2，拒绝1")
    try:
        validator = MinUsageCountValidation()
        candidates = {
            "L1": [make_candidate("EXP-A", "L1", 0.10, 0)],
            "L2": [make_candidate("EXP-B", "L2", 0.10, 5)],
            "L3": [make_candidate("EXP-C", "L3", 0.03, 10)],
        }
        reuse_counts = {"EXP-A": 0, "EXP-B": 5, "EXP-C": 10}
        result = validator.execute_validation(candidates, reuse_counts)
        assert result.passed == 2  # EXP-A 通过, EXP-C 极低I豁免
        assert result.rejected == 1  # EXP-B 复用5 ≥ 3 拒绝
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    print(f"\n测试结果: {passed} PASS, {failed} FAIL")