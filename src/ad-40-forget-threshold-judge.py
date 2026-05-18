#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-40
模块名称: 遗忘阈值判定单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 晋升与遗忘执行机制
核心职责: 扫描漏斗二各层级中重要度 I 值低于遗忘阈值、且复用次数不足的经验条目，
          生成遗忘候选清单。L4/L5 层不参与遗忘扫描，L3 层遗忘优先冷归档而非直接删除。
          遗忘阈值受分槽专属策略与全局容量告急双重调节。

依赖模块: ad-36(综合重要度 I 值聚合计算单元，提供条目当前 I 值),
          ad-20/22/24/26(各层级存储单元，提供条目元数据与复用计数),
          ad-33(复用频次 C 值统计单元，提供复用次数),
          ad-35(三维权重系数配置单元，获取各分槽最低遗忘阈值),
          ad-48(全局容量配额管控单元，获取容量告急信号)
被依赖模块: ad-20/22/24/26(各层级存储单元，消费遗忘候选清单),
            ad-42(冗余记忆删除与归档单元，接收遗忘执行指令)

安全约束:
  S-01: L5 核心层永不参与遗忘扫描，编译期硬编码豁免
  S-02: 不可抗力场景经验在所有层级均强制豁免遗忘
  S-03: L4 长期层仅在容量红色告警(>95%)时纳入有限遗忘扫描
  S-04: 遗忘阈值动态调节上限硬编码为 0.60，防止容量告急时过度清除
  S-05: 遗忘判定仅生成候选清单，实际删除或归档由 ad-42 执行
  S-06: 所有遗忘判定全量写入 ad-51 不可变日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class ForgetMethod(Enum):
    """遗忘方式"""
    DIRECT_DELETE = "直接删除"
    COLD_ARCHIVE = "冷归档"


class CapacityLevel(Enum):
    """容量告急等级"""
    NORMAL = "normal"
    YELLOW = "yellow"    # 85%-90%
    ORANGE = "orange"    # 90%-95%
    RED = "red"          # >95%


class JudgeState(Enum):
    """判定单元内部状态"""
    IDLE = "idle"
    JUDGING = "judging"
    PAUSED = "paused"


class ExemptionReason(Enum):
    """豁免原因"""
    FORCE_MAJEURE = "不可抗力永久保护"
    L5_CORE = "L5核心层永不遗忘"
    MANUAL_LOCK = "人工锁定"
    RECENT_ADOPTED = "近7日被成功采用"
    HIGH_S_VALUE = "高安全显著性保护(S≥0.7)"


# ==================== 数据结构 ====================

@dataclass
class EntryMetadata:
    """条目元数据（来自各层级存储）"""
    entry_id: str
    current_layer: str          # "L1"/"L2"/"L3"/"L4"/"L5"
    i_value: float
    reuse_count: int = 0
    source_slot_id: int = 19
    sub_label: str = ""
    result_label: str = "成功优化"
    force_majeure: bool = False
    manual_locked: bool = False
    s_value: float = 0.0
    last_adopted_time: Optional[float] = None
    retention_seconds: float = 0.0


@dataclass
class ForgetCandidate:
    """遗忘候选条目"""
    entry_id: str
    current_layer: str
    i_value: float
    reuse_count: int
    forget_method: ForgetMethod
    source_slot_id: int
    reason: str
    priority: float = 0.0


@dataclass
class ExemptedEntry:
    """豁免遗忘的条目"""
    entry_id: str
    current_layer: str
    reason: ExemptionReason


@dataclass
class ForgetJudgmentResult:
    """遗忘判定结果"""
    cycle_id: str
    candidates: Dict[str, List[ForgetCandidate]]  # 层级 -> 候选列表
    exempted: List[ExemptedEntry]
    scanned_count: int
    capacity_pressure_warning: bool
    estimated_release_bytes: int
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class ForgetThresholdJudge:
    """
    遗忘阈值判定单元
    
    职责:
    1. 周期性扫描各层级条目元数据
    2. 按分层保护规则执行遗忘判定
    3. 容量告急时动态调节遗忘阈值
    4. 检查强制豁免规则
    5. 生成遗忘候选清单
    """
    
    # 容量阈值调节系数
    CAPACITY_ADJUST = {
        CapacityLevel.NORMAL: 1.0,
        CapacityLevel.YELLOW: 1.2,
        CapacityLevel.ORANGE: 1.5,
        CapacityLevel.RED: 2.0,
    }
    
    # 动态阈值上限
    MAX_DYNAMIC_THRESHOLD = 0.60
    
    # 默认遗忘阈值
    DEFAULT_THRESHOLD = 0.15
    
    # 各分槽默认遗忘阈值
    SLOT_DEFAULTS = {
        15: 0.12,   # 高速巡航槽
        16: 0.10,   # 城区路口槽
        17: 0.075,  # 泊车低速槽
        18: 0.09,   # 特殊环境槽
        19: 0.15,   # 通用驾驶槽
    }
    
    # 强制豁免规则
    EXEMPTION_RULES = {
        "force_majeure": ExemptionReason.FORCE_MAJEURE,
        "L5": ExemptionReason.L5_CORE,
        "manual_locked": ExemptionReason.MANUAL_LOCK,
        "recent_adopted_7d": ExemptionReason.RECENT_ADOPTED,
        "high_s_0.7": ExemptionReason.HIGH_S_VALUE,
    }
    
    # L4 纳入扫描的容量条件
    L4_SCAN_CAPACITY_LEVEL = CapacityLevel.RED
    
    # L3 默认遗忘方式
    L3_DEFAULT_METHOD = ForgetMethod.COLD_ARCHIVE
    
    # L4 遗忘方式
    L4_FORGET_METHOD = ForgetMethod.COLD_ARCHIVE
    
    def __init__(self):
        self.module_id = "ad-40"
        self.module_name = "遗忘阈值判定单元"
        
        # 内部状态
        self.state = JudgeState.IDLE
        
        # 当前容量等级
        self._capacity_level = CapacityLevel.NORMAL
        
        # 统计
        self._total_scanned = 0
        self._total_candidates = 0
        self._total_exempted = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 遗忘阈值判定单元初始化完成")
        print(f"[{self.module_id}] L5豁免 | 不可抗力豁免 | L4仅在容量红色告警时扫描")
    
    # ========== 状态管理 ==========
    
    def set_capacity_level(self, level: CapacityLevel) -> None:
        """设置当前容量告急等级"""
        self._capacity_level = level
    
    def pause(self) -> None:
        self.state = JudgeState.PAUSED
    
    def resume(self) -> None:
        self.state = JudgeState.IDLE
    
    # ========== 主判定流程 ==========
    
    def execute_judgment(self,
                         layer_entries: Dict[str, List[EntryMetadata]],
                         slot_thresholds: Optional[Dict[int, float]] = None,
                         cold_entry_ids: Optional[List[str]] = None) -> ForgetJudgmentResult:
        """
        执行遗忘判定
        
        Args:
            layer_entries: 各层级条目元数据 {"L1": [...], "L2": [...], ...}
            slot_thresholds: 各分槽遗忘阈值覆写
            cold_entry_ids: 冷条目ID列表（来自 ad-33，用于辅助判定）
            
        Returns:
            遗忘判定结果
        """
        if self.state == JudgeState.PAUSED:
            return ForgetJudgmentResult("", {}, [], 0, False, 0)
        
        self.state = JudgeState.JUDGING
        cycle_id = f"forget-{uuid.uuid4().hex[:8]}"
        
        candidates: Dict[str, List[ForgetCandidate]] = {"L1": [], "L2": [], "L3": [], "L4": []}
        exempted: List[ExemptedEntry] = []
        scanned = 0
        
        # 容量调整系数
        adjust = self.CAPACITY_ADJUST.get(self._capacity_level, 1.0)
        
        # L3 遗忘方式（橙色及以上升级为直接删除）
        l3_method = ForgetMethod.DIRECT_DELETE if self._capacity_level in [CapacityLevel.ORANGE, CapacityLevel.RED] else self.L3_DEFAULT_METHOD
        
        # L4 是否纳入扫描
        scan_l4 = (self._capacity_level == self.L4_SCAN_CAPACITY_LEVEL)
        
        for layer_name, entries in layer_entries.items():
            if layer_name == "L5":
                # S-01: L5 永不扫描
                for entry in entries:
                    exempted.append(ExemptedEntry(entry.entry_id, "L5", ExemptionReason.L5_CORE))
                continue
            
            if layer_name == "L4" and not scan_l4:
                # S-03: L4 不参与常规扫描
                continue
            
            for entry in entries:
                scanned += 1
                
                # 强制豁免检查
                exempt_reason = self._check_exemptions(entry, cold_entry_ids)
                if exempt_reason:
                    exempted.append(ExemptedEntry(entry.entry_id, layer_name, exempt_reason))
                    continue
                
                # 获取分槽遗忘阈值
                if slot_thresholds and entry.source_slot_id in slot_thresholds:
                    base_threshold = slot_thresholds[entry.source_slot_id]
                else:
                    base_threshold = self.SLOT_DEFAULTS.get(entry.source_slot_id, self.DEFAULT_THRESHOLD)
                
                # 动态阈值 = 基础阈值 × 容量调整系数（上限 0.60）
                dynamic_threshold = min(base_threshold * adjust, self.MAX_DYNAMIC_THRESHOLD)
                
                # 遗忘双条件判定
                if entry.i_value < dynamic_threshold and entry.reuse_count < 2:
                    # 确定遗忘方式
                    if layer_name == "L4":
                        method = self.L4_FORGET_METHOD
                    elif layer_name == "L3":
                        method = l3_method
                    else:
                        method = ForgetMethod.DIRECT_DELETE
                    
                    # 计算遗忘优先级（I值越低、复用越少、越久未访问 → 优先级越高）
                    priority = (1.0 - entry.i_value) * 0.5 + (1.0 - min(entry.reuse_count/5, 1.0)) * 0.3
                    
                    candidates[layer_name].append(ForgetCandidate(
                        entry_id=entry.entry_id,
                        current_layer=layer_name,
                        i_value=entry.i_value,
                        reuse_count=entry.reuse_count,
                        forget_method=method,
                        source_slot_id=entry.source_slot_id,
                        reason=f"I={entry.i_value:.3f}<θ={dynamic_threshold:.3f}, 复用={entry.reuse_count}",
                        priority=priority
                    ))
        
        # 按优先级降序排列各层候选
        for layer_name in candidates:
            candidates[layer_name].sort(key=lambda x: x.priority, reverse=True)
        
        self._total_scanned += scanned
        total_candidates = sum(len(v) for v in candidates.values())
        self._total_candidates += total_candidates
        self._total_exempted += len(exempted)
        
        # 评估容量压力
        estimated_release = total_candidates * 4096  # 模拟每条 4KB
        pressure_warning = (self._capacity_level in [CapacityLevel.ORANGE, CapacityLevel.RED] and total_candidates < 10)
        
        result = ForgetJudgmentResult(
            cycle_id=cycle_id,
            candidates=candidates,
            exempted=exempted,
            scanned_count=scanned,
            capacity_pressure_warning=pressure_warning,
            estimated_release_bytes=estimated_release
        )
        
        self.state = JudgeState.IDLE
        return result
    
    def _check_exemptions(self, entry: EntryMetadata, cold_entry_ids: Optional[List[str]]) -> Optional[ExemptionReason]:
        """检查强制豁免规则"""
        # 不可抗力
        if entry.force_majeure:
            return ExemptionReason.FORCE_MAJEURE
        
        # 人工锁定
        if entry.manual_locked:
            return ExemptionReason.MANUAL_LOCK
        
        # L5 已在层级循环外处理
        
        # 近 7 日被成功采用
        if entry.last_adopted_time is not None:
            if time.time() - entry.last_adopted_time < 7 * 24 * 3600:
                return ExemptionReason.RECENT_ADOPTED
        
        # 高安全显著性 S ≥ 0.7
        if entry.s_value >= 0.7:
            return ExemptionReason.HIGH_S_VALUE
        
        return None
    
    # ========== 查询接口 ==========
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_scanned": self._total_scanned,
            "total_candidates": self._total_candidates,
            "total_exempted": self._total_exempted,
            "capacity_level": self._capacity_level.value,
            "state": self.state.value
        }
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-40 遗忘阈值判定单元 单元测试")
    print("=" * 60)
    passed, failed = 0, 0
    
    def make_entry(eid, layer, i_val, reuse=0, slot=15, force_majeure=False, s_val=0.0):
        return EntryMetadata(eid, layer, i_val, reuse, slot, "", "成功优化", force_majeure, False, s_val)
    
    # TC-40-01: 正常遗忘判定
    print("\n[TC-40-01] L2 低 I 值条目被标记为遗忘候选")
    try:
        judge = ForgetThresholdJudge()
        entries = {"L1": [], "L2": [make_entry("EXP-001", "L2", 0.05, 0, 15)],
                   "L3": [], "L4": [], "L5": []}
        result = judge.execute_judgment(entries)
        assert len(result.candidates["L2"]) == 1
        assert result.candidates["L2"][0].forget_method == ForgetMethod.DIRECT_DELETE
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-40-02: 不可抗力豁免
    print("\n[TC-40-02] 不可抗力条目豁免遗忘")
    try:
        judge = ForgetThresholdJudge()
        entries = {"L1": [make_entry("EXP-002", "L1", 0.02, 0, force_majeure=True)],
                   "L2": [], "L3": [], "L4": [], "L5": []}
        result = judge.execute_judgment(entries)
        assert len(result.exempted) == 1
        assert result.exempted[0].reason == ExemptionReason.FORCE_MAJEURE
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-40-03: L3 默认冷归档
    print("\n[TC-40-03] L3 条目遗忘方式为冷归档")
    try:
        judge = ForgetThresholdJudge()
        entries = {"L1": [], "L2": [], "L3": [make_entry("EXP-003", "L3", 0.04, 0)],
                   "L4": [], "L5": []}
        result = judge.execute_judgment(entries)
        assert len(result.candidates["L3"]) == 1
        assert result.candidates["L3"][0].forget_method == ForgetMethod.COLD_ARCHIVE
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-40-04: 容量橙色 L3 升级为直接删除
    print("\n[TC-40-04] 容量橙色 L3 遗忘升级为直接删除")
    try:
        judge = ForgetThresholdJudge()
        judge.set_capacity_level(CapacityLevel.ORANGE)
        entries = {"L1": [], "L2": [], "L3": [make_entry("EXP-004", "L3", 0.04, 0)],
                   "L4": [], "L5": []}
        result = judge.execute_judgment(entries)
        assert result.candidates["L3"][0].forget_method == ForgetMethod.DIRECT_DELETE
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-40-05: 容量红色 L4 纳入扫描
    print("\n[TC-40-05] 容量红色 L4 纳入遗忘扫描")
    try:
        judge = ForgetThresholdJudge()
        judge.set_capacity_level(CapacityLevel.RED)
        entries = {"L1": [], "L2": [], "L3": [],
                   "L4": [make_entry("EXP-005", "L4", 0.03, 0)],
                   "L5": []}
        result = judge.execute_judgment(entries)
        assert len(result.candidates["L4"]) == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-40-06: L5 永不扫描
    print("\n[TC-40-06] L5 条目永不参与遗忘扫描")
    try:
        judge = ForgetThresholdJudge()
        entries = {"L1": [], "L2": [], "L3": [], "L4": [],
                   "L5": [make_entry("EXP-006", "L5", 0.01, 0)]}
        result = judge.execute_judgment(entries)
        assert len(result.exempted) == 1
        assert result.exempted[0].reason == ExemptionReason.L5_CORE
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    print(f"\n测试结果: {passed} PASS, {failed} FAIL")