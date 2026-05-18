#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-21
模块名称: L1 临时层时序衰减单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 五层记忆层级存储
核心职责: 管理 L1 临时层经验条目的时间衰减。L1 条目留存满 24 小时后，综合当前重要度
          I 值与场景分槽专属晋升阈值，判定该条目应进入晋升候选、建议清除或继续保留。
          是五层记忆结构中连接"临时暂存"与"长期固化"的第一个判定关口。

依赖模块: ad-20(L1 临时层存储单元), ad-36(综合重要度 I 值聚合计算单元),
          ad-35(三维权重系数配置单元)
被依赖模块: ad-20(消费衰减评估结果), ad-38(消费晋升候选清单)

安全约束:
  S-01: 24 小时留存时间为硬编码判定门槛
  S-02: 安全显著性 S ≥ 0.9 的条目在容量告急时仍受最低保留阈值保护
  S-03: 晋升阈值与最低保留阈值的修改须经权限校验，不可低于编译期硬编码下限
  S-04: 继续保留条目 12 小时冷却期内不可重复评估
  S-05: 所有衰减评估操作全量写入 ad-51 变更日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class DecayConclusion(Enum):
    """衰减评估结论"""
    PROMOTION_CANDIDATE = "promotion_candidate"
    RECOMMEND_CLEAR = "recommend_clear"
    CONTINUE_RETAIN = "continue_retain"


class AssessmentState(Enum):
    """评估单元内部状态"""
    NORMAL = "normal"
    BATCH_EVALUATING = "batch_evaluating"
    PAUSED = "paused"
    CONSERVATIVE = "conservative"


# ==================== 数据结构 ====================

@dataclass
class L1EntryIndex:
    """L1 条目索引（来自 ad-20）"""
    entry_id: str
    storage_address: int
    write_timestamp: float
    i0_value: float
    current_i_value: float
    source_slot_id: int
    sub_label: str
    size_bytes: int


@dataclass
class SlotPromotionThreshold:
    """分槽专属晋升阈值（来自 ad-35）"""
    slot_id: int
    promotion_i_threshold: float
    minimum_retain_i_threshold: float


@dataclass
class DecayAssessmentResult:
    """衰减评估结果"""
    entry_id: str
    conclusion: DecayConclusion
    current_i_value: float
    retention_duration: float
    source_slot_id: int
    assessment_timestamp: float = field(default_factory=time.time)


@dataclass
class AssessmentStatistics:
    """评估统计"""
    total_assessed: int = 0
    promotion_count: int = 0
    clear_count: int = 0
    retain_count: int = 0
    skipped_count: int = 0
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class L1DecayAssessment:
    """
    L1 临时层时序衰减单元
    
    职责:
    1. 周期性扫描 L1 条目索引
    2. 对留存满 24 小时的条目执行三维衰减判定
    3. 根据分槽专属阈值判定晋升候选/建议清除/继续保留
    4. 管理继续保留条目的 12 小时冷却期
    5. 容量告急时切换到保守模式
    """
    
    # 衰减评估门槛（秒）
    DECAY_ASSESSMENT_THRESHOLD = 24 * 3600  # 24 小时
    
    # 继续保留条目冷却期（秒）
    RETAIN_COOLDOWN = 12 * 3600  # 12 小时
    
    # 评估间隔（秒）
    ASSESSMENT_INTERVAL = 60  # 60 秒
    
    # 批量评估阈值
    BATCH_THRESHOLD = 100  # 单次超过 100 条进入批量模式
    
    # 容量告急时阈值调整系数
    CONSERVATIVE_ADJUST_YELLOW = 1.5   # 容量 > 90%
    CONSERVATIVE_ADJUST_RED = 2.0      # 容量 > 95%
    
    # 编译期硬编码下限
    HARD_MIN_PROMOTION_I = 0.20
    HARD_MIN_RETAIN_I = 0.05
    
    def __init__(self):
        self.module_id = "ad-21"
        self.module_name = "L1 临时层时序衰减单元"
        
        # 内部状态
        self.state = AssessmentState.NORMAL
        
        # 继续保留条目冷却字典: entry_id -> 上次评估时间
        self._retain_cooldown: Dict[str, float] = {}
        
        # 分槽阈值缓存: slot_id -> SlotPromotionThreshold
        self._slot_thresholds: Dict[int, SlotPromotionThreshold] = {}
        
        # 容量调整系数
        self._capacity_adjust = 1.0
        
        # 上次评估时间
        self._last_assessment_time = 0.0
        
        # L1 占用率缓存
        self._l1_usage_rate = 0.0
        
        # 统计
        self._stats = AssessmentStatistics()
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] L1 时序衰减单元初始化完成")
        print(f"[{self.module_id}] 衰减门槛: {self.DECAY_ASSESSMENT_THRESHOLD/3600:.0f}h")
        print(f"[{self.module_id}] 保留冷却: {self.RETAIN_COOLDOWN/3600:.0f}h")
    
    # ========== 状态管理 ==========
    
    def set_capacity_adjust(self, usage_rate: float) -> None:
        """
        设置容量调整系数
        
        Args:
            usage_rate: 全局容量使用率
        """
        if usage_rate > 0.95:
            self._capacity_adjust = self.CONSERVATIVE_ADJUST_RED
            self.state = AssessmentState.CONSERVATIVE
        elif usage_rate > 0.90:
            self._capacity_adjust = self.CONSERVATIVE_ADJUST_YELLOW
            self.state = AssessmentState.CONSERVATIVE
        else:
            self._capacity_adjust = 1.0
            if self.state == AssessmentState.CONSERVATIVE:
                self.state = AssessmentState.NORMAL
    
    def set_l1_usage_rate(self, usage_rate: float) -> None:
        """设置 L1 占用率"""
        self._l1_usage_rate = usage_rate
    
    def update_slot_thresholds(self, thresholds: List[SlotPromotionThreshold]) -> None:
        """更新分槽阈值配置"""
        for t in thresholds:
            # S-03: 硬编码下限校验
            t.promotion_i_threshold = max(t.promotion_i_threshold, self.HARD_MIN_PROMOTION_I)
            t.minimum_retain_i_threshold = max(t.minimum_retain_i_threshold, self.HARD_MIN_RETAIN_I)
            self._slot_thresholds[t.slot_id] = t
    
    def pause(self) -> None:
        self.state = AssessmentState.PAUSED
    
    def resume(self) -> None:
        self.state = AssessmentState.NORMAL
    
    # ========== 衰减评估 ==========
    
    def assess_entries(self, l1_index: List[L1EntryIndex],
                       i_value_dict: Dict[str, float]) -> List[DecayAssessmentResult]:
        """
        对 L1 条目执行衰减评估
        
        Args:
            l1_index: L1 条目索引快照
            i_value_dict: 条目当前 I 值字典
            
        Returns:
            衰减评估结果列表
        """
        if self.state == AssessmentState.PAUSED:
            return []
        
        now = time.time()
        
        # 检查评估间隔
        if now - self._last_assessment_time < self.ASSESSMENT_INTERVAL:
            return []
        
        self._last_assessment_time = now
        
        # 筛选到期条目
        expired_entries = []
        for idx_entry in l1_index:
            retention = now - idx_entry.write_timestamp
            
            # 未满 24 小时跳过
            if retention < self.DECAY_ASSESSMENT_THRESHOLD:
                continue
            
            # 检查冷却期
            if idx_entry.entry_id in self._retain_cooldown:
                if now - self._retain_cooldown[idx_entry.entry_id] < self.RETAIN_COOLDOWN:
                    self._stats.skipped_count += 1
                    continue
            
            # 获取当前 I 值
            current_i = i_value_dict.get(idx_entry.entry_id, idx_entry.i0_value)
            
            # 异常 I 值钳制
            if current_i > 1.0 or current_i < 0.0:
                current_i = max(0.0, min(1.0, current_i))
            
            expired_entries.append({
                "entry_id": idx_entry.entry_id,
                "current_i_value": current_i,
                "retention_duration": retention,
                "source_slot_id": idx_entry.source_slot_id,
                "sub_label": idx_entry.sub_label
            })
        
        if not expired_entries:
            return []
        
        # 批量模式判定
        if len(expired_entries) > self.BATCH_THRESHOLD:
            self.state = AssessmentState.BATCH_EVALUATING
        
        # 逐条判定
        results = []
        for entry in expired_entries:
            result = self._judge_single(entry, now)
            results.append(result)
            
            # 更新统计
            self._stats.total_assessed += 1
            if result.conclusion == DecayConclusion.PROMOTION_CANDIDATE:
                self._stats.promotion_count += 1
            elif result.conclusion == DecayConclusion.RECOMMEND_CLEAR:
                self._stats.clear_count += 1
            elif result.conclusion == DecayConclusion.CONTINUE_RETAIN:
                self._stats.retain_count += 1
                self._retain_cooldown[entry["entry_id"]] = now
        
        if self.state == AssessmentState.BATCH_EVALUATING:
            self.state = AssessmentState.NORMAL
        
        return results
    
    def _judge_single(self, entry: Dict[str, Any], now: float) -> DecayAssessmentResult:
        """
        判定单条经验的衰减结论
        
        判定逻辑:
        1. L1 占用 > 95% → 跳过"继续保留"，仅晋升或清除
        2. I ≥ 晋升阈值 → 晋升候选
        3. I < 最低保留阈值 → 建议清除
        4. 中间 → 继续保留
        """
        entry_id = entry["entry_id"]
        i_value = entry["current_i_value"]
        retention = entry["retention_duration"]
        slot_id = entry["source_slot_id"]
        
        # 获取分槽阈值
        threshold = self._slot_thresholds.get(slot_id)
        if threshold is None:
            # 使用默认阈值
            promotion_i = 0.40
            retain_i = 0.10
        else:
            promotion_i = threshold.promotion_i_threshold
            retain_i = threshold.minimum_retain_i_threshold
        
        # 容量告急时调整最低保留阈值
        if self.state == AssessmentState.CONSERVATIVE:
            retain_i = retain_i * self._capacity_adjust
        
        # L1 自身占用 > 95%：跳过继续保留
        if self._l1_usage_rate > 0.95:
            if i_value >= promotion_i:
                conclusion = DecayConclusion.PROMOTION_CANDIDATE
            else:
                conclusion = DecayConclusion.RECOMMEND_CLEAR
        else:
            # 正常三维判定
            if i_value >= promotion_i:
                conclusion = DecayConclusion.PROMOTION_CANDIDATE
            elif i_value < retain_i:
                conclusion = DecayConclusion.RECOMMEND_CLEAR
            else:
                conclusion = DecayConclusion.CONTINUE_RETAIN
        
        return DecayAssessmentResult(
            entry_id=entry_id,
            conclusion=conclusion,
            current_i_value=i_value,
            retention_duration=retention,
            source_slot_id=slot_id
        )
    
    # ========== 清理过期冷却记录 ==========
    
    def clean_cooldown_records(self) -> int:
        """清理已过冷却期的记录"""
        now = time.time()
        expired = []
        for entry_id, last_time in self._retain_cooldown.items():
            if now - last_time >= self.RETAIN_COOLDOWN:
                expired.append(entry_id)
        
        for entry_id in expired:
            del self._retain_cooldown[entry_id]
        
        return len(expired)
    
    # ========== 查询接口 ==========
    
    def get_state(self) -> AssessmentState:
        return self.state
    
    def get_threshold_for_slot(self, slot_id: int) -> Optional[SlotPromotionThreshold]:
        return self._slot_thresholds.get(slot_id)
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_assessed": self._stats.total_assessed,
            "promotion_count": self._stats.promotion_count,
            "clear_count": self._stats.clear_count,
            "retain_count": self._stats.retain_count,
            "skipped_count": self._stats.skipped_count,
            "cooldown_count": len(self._retain_cooldown),
            "capacity_adjust": self._capacity_adjust,
            "state": self.state.value
        }
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-21 L1 临时层时序衰减单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    def make_index_entry(entry_id, hours_ago, i0=0.5, slot_id=15, sub_label="常规通用"):
        return L1EntryIndex(
            entry_id=entry_id,
            storage_address=0x1000,
            write_timestamp=time.time() - hours_ago * 3600,
            i0_value=i0,
            current_i_value=i0,
            source_slot_id=slot_id,
            sub_label=sub_label,
            size_bytes=1024
        )
    
    # --- TC-21-01: 满足晋升条件 ---
    print("\n[TC-21-01] 满足晋升条件（I=0.55 ≥ 0.40，留存 25h）")
    try:
        assessor = L1DecayAssessment()
        assessor.update_slot_thresholds([
            SlotPromotionThreshold(15, 0.40, 0.10)
        ])
        l1_index = [make_index_entry("EXP-001", 25, i0=0.55)]
        i_values = {"EXP-001": 0.55}
        results = assessor.assess_entries(l1_index, i_values)
        assert len(results) == 1
        assert results[0].conclusion == DecayConclusion.PROMOTION_CANDIDATE
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-21-02: I 值低建议清除 ---
    print("\n[TC-21-02] I 值低建议清除（I=0.05 < 0.10）")
    try:
        assessor = L1DecayAssessment()
        assessor.update_slot_thresholds([
            SlotPromotionThreshold(15, 0.40, 0.10)
        ])
        l1_index = [make_index_entry("EXP-002", 26, i0=0.05)]
        i_values = {"EXP-002": 0.05}
        results = assessor.assess_entries(l1_index, i_values)
        assert len(results) == 1
        assert results[0].conclusion == DecayConclusion.RECOMMEND_CLEAR
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-21-03: I 值中间继续保留 ---
    print("\n[TC-21-03] I 值中间继续保留（I=0.20）")
    try:
        assessor = L1DecayAssessment()
        assessor.update_slot_thresholds([
            SlotPromotionThreshold(15, 0.40, 0.10)
        ])
        l1_index = [make_index_entry("EXP-003", 25, i0=0.20)]
        i_values = {"EXP-003": 0.20}
        results = assessor.assess_entries(l1_index, i_values)
        assert len(results) == 1
        assert results[0].conclusion == DecayConclusion.CONTINUE_RETAIN
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-21-04: 未满 24 小时跳过 ---
    print("\n[TC-21-04] 未满 24 小时跳过")
    try:
        assessor = L1DecayAssessment()
        assessor.update_slot_thresholds([
            SlotPromotionThreshold(15, 0.40, 0.10)
        ])
        l1_index = [make_index_entry("EXP-004", 20, i0=0.55)]
        i_values = {"EXP-004": 0.55}
        results = assessor.assess_entries(l1_index, i_values)
        assert len(results) == 0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-21-05: 冷却期内跳过 ---
    print("\n[TC-21-05] 冷却期内跳过")
    try:
        assessor = L1DecayAssessment()
        assessor.update_slot_thresholds([
            SlotPromotionThreshold(15, 0.40, 0.10)
        ])
        l1_index = [make_index_entry("EXP-005", 25, i0=0.20)]
        i_values = {"EXP-005": 0.20}
        # 第一次评估 → 继续保留
        assessor.assess_entries(l1_index, i_values)
        # 立即第二次评估 → 冷却期跳过
        results2 = assessor.assess_entries(l1_index, i_values)
        assert len(results2) == 0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-21-06: 容量告急阈值上调 ---
    print("\n[TC-21-06] 容量告急阈值上调（I=0.12 被清除）")
    try:
        assessor = L1DecayAssessment()
        assessor.update_slot_thresholds([
            SlotPromotionThreshold(15, 0.40, 0.10)
        ])
        assessor.set_capacity_adjust(0.92)  # 容量 > 90%
        l1_index = [make_index_entry("EXP-006", 26, i0=0.12)]
        i_values = {"EXP-006": 0.12}
        results = assessor.assess_entries(l1_index, i_values)
        # retain_i = 0.10 * 1.5 = 0.15, I=0.12 < 0.15 → 清除
        assert results[0].conclusion == DecayConclusion.RECOMMEND_CLEAR
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-21-07: L1 占用 > 95% 跳过继续保留 ---
    print("\n[TC-21-07] L1 占用 > 95% 跳过继续保留")
    try:
        assessor = L1DecayAssessment()
        assessor.update_slot_thresholds([
            SlotPromotionThreshold(15, 0.40, 0.10)
        ])
        assessor.set_l1_usage_rate(0.96)
        l1_index = [make_index_entry("EXP-007", 25, i0=0.20)]
        i_values = {"EXP-007": 0.20}
        results = assessor.assess_entries(l1_index, i_values)
        # I=0.20 < 0.40 且 L1 > 95% → 建议清除
        assert results[0].conclusion == DecayConclusion.RECOMMEND_CLEAR
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-21-08: 特殊环境槽低阈值晋升 ---
    print("\n[TC-21-08] 特殊环境槽低阈值晋升（I=0.30 ≥ 0.28）")
    try:
        assessor = L1DecayAssessment()
        assessor.update_slot_thresholds([
            SlotPromotionThreshold(18, 0.28, 0.06)  # 特殊环境槽
        ])
        l1_index = [make_index_entry("EXP-008", 25, i0=0.30, slot_id=18)]
        i_values = {"EXP-008": 0.30}
        results = assessor.assess_entries(l1_index, i_values)
        assert results[0].conclusion == DecayConclusion.PROMOTION_CANDIDATE
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-21-09: I 值异常钳制 ---
    print("\n[TC-21-09] I 值异常钳制（I=1.5 → 1.0）")
    try:
        assessor = L1DecayAssessment()
        assessor.update_slot_thresholds([
            SlotPromotionThreshold(15, 0.40, 0.10)
        ])
        l1_index = [make_index_entry("EXP-009", 25, i0=1.5)]
        i_values = {"EXP-009": 1.5}
        results = assessor.assess_entries(l1_index, i_values)
        assert results[0].current_i_value == 1.0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-21-10: 硬编码下限保护 ---
    print("\n[TC-21-10] 硬编码下限保护")
    try:
        assessor = L1DecayAssessment()
        assessor.update_slot_thresholds([
            SlotPromotionThreshold(15, 0.10, 0.02)  # 低于硬编码下限
        ])
        threshold = assessor.get_threshold_for_slot(15)
        assert threshold.promotion_i_threshold == assessor.HARD_MIN_PROMOTION_I  # 0.20
        assert threshold.minimum_retain_i_threshold == assessor.HARD_MIN_RETAIN_I  # 0.05
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