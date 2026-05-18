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
  S-06: 所有遗忘判定（含遗忘候选、豁免、保留）全量写入 ad-51 不可变日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class JudgeState(Enum):
    """判定单元内部状态"""
    IDLE = "idle"
    JUDGING = "judging"
    PAUSED = "paused"


class ForgetMethod(Enum):
    """遗忘方式"""
    DIRECT_DELETE = "直接删除"
    COLD_ARCHIVE = "冷归档"


class CapacityLevel(Enum):
    """容量告急等级"""
    NORMAL = "normal"        # < 85%
    YELLOW = "yellow"        # 85%–90%
    ORANGE = "orange"        # 90%–95%
    RED = "red"              # >95%


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
    current_layer: str              # "L1"/"L2"/"L3"/"L4"/"L5"
    i_value: float                  # 当前 I 值
    reuse_count: int = 0            # 复用次数
    source_slot_id: int = 19        # 来源分槽号
    sub_label: str = ""             # 子类标记
    result_label: str = "成功优化"  # 结果分类标签
    force_majeure: bool = False     # 是否不可抗力
    manual_locked: bool = False     # 是否人工锁定
    s_value: float = 0.0            # 安全显著性
    last_adopted_time: Optional[float] = None  # 最近成功采用时间
    retention_seconds: float = 0.0  # 留存时长


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
    priority: float = 0.0           # 遗忘优先级（越高越优先遗忘）


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
    candidates: Dict[str, List[ForgetCandidate]]    # 层级 -> 候选列表
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
    2. 按分层保护规则执行遗忘判定（L5永不参与，L4有限参与）
    3. 检查强制豁免规则
    4. 容量告急时动态调节遗忘阈值
    5. 生成遗忘候选清单（按优先级排序）
    """
    
    # ========== 容量阈值调节系数 ==========
    CAPACITY_ADJUST: Dict[CapacityLevel, float] = {
        CapacityLevel.NORMAL: 1.0,
        CapacityLevel.YELLOW: 1.2,
        CapacityLevel.ORANGE: 1.5,
        CapacityLevel.RED: 2.0,
    }
    
    # ========== 动态阈值上限 ==========
    MAX_DYNAMIC_THRESHOLD = 0.60
    
    # ========== 默认遗忘阈值 ==========
    DEFAULT_THRESHOLD = 0.15
    
    # ========== 各分槽默认遗忘阈值 ==========
    SLOT_DEFAULTS: Dict[int, float] = {
        15: 0.12,   # 高速巡航槽
        16: 0.10,   # 城区路口槽
        17: 0.075,  # 泊车低速槽
        18: 0.09,   # 特殊环境槽
        19: 0.15,   # 通用驾驶槽（常规子类）
    }
    
    # ========== L4 纳入扫描的容量条件 ==========
    L4_SCAN_CAPACITY_LEVEL = CapacityLevel.RED
    
    # ========== 遗忘双条件：最小复用次数 ==========
    MIN_REUSE_FOR_RETENTION = 2
    
    # ========== 豁免条件：近期采用天数 ==========
    RECENT_ADOPTED_DAYS = 7
    
    # ========== 豁免条件：高安全显著性阈值 ==========
    HIGH_S_THRESHOLD = 0.7
    
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
        print(f"[{self.module_id}] 动态阈值上限: {self.MAX_DYNAMIC_THRESHOLD}")
    
    # ========== 状态管理 ==========
    
    def set_capacity_level(self, level: CapacityLevel) -> None:
        """设置当前容量告急等级"""
        self._capacity_level = level
        if level != CapacityLevel.NORMAL:
            print(f"[{self.module_id}] 容量告急等级更新: {level.value}")
    
    def pause(self) -> None:
        self.state = JudgeState.PAUSED
    
    def resume(self) -> None:
        self.state = JudgeState.IDLE
    
    def get_state(self) -> JudgeState:
        return self.state
    
    # ========== 主判定流程 ==========
    
    def execute_judgment(self,
                         layer_entries: Dict[str, List[EntryMetadata]],
                         slot_thresholds: Optional[Dict[int, float]] = None,
                         cold_entry_ids: Optional[set] = None) -> ForgetJudgmentResult:
        """
        执行遗忘判定
        
        Args:
            layer_entries: 各层级条目元数据 {"L1": [...], "L2": [...], ...}
            slot_thresholds: 各分槽遗忘阈值覆写（可选，来自 ad-35）
            cold_entry_ids: 冷条目ID集合（来自 ad-33，用于辅助判定）
            
        Returns:
            遗忘判定结果
        """
        if self.state == JudgeState.PAUSED:
            return ForgetJudgmentResult("", {}, [], 0, False, 0)
        
        self.state = JudgeState.JUDGING
        cycle_id = f"forget-{uuid.uuid4().hex[:8]}"
        
        candidates: Dict[str, List[ForgetCandidate]] = {
            "L1": [], "L2": [], "L3": [], "L4": []
        }
        exempted: List[ExemptedEntry] = []
        scanned = 0
        
        # 容量调整系数
        adjust = self.CAPACITY_ADJUST.get(self._capacity_level, 1.0)
        
        # L3 遗忘方式：橙色及以上升级为直接删除
        l3_method = (
            ForgetMethod.DIRECT_DELETE
            if self._capacity_level in [CapacityLevel.ORANGE, CapacityLevel.RED]
            else ForgetMethod.COLD_ARCHIVE
        )
        
        # L4 是否纳入扫描
        scan_l4 = (self._capacity_level == self.L4_SCAN_CAPACITY_LEVEL)
        
        for layer_name, entries in layer_entries.items():
            # S-01: L5 永不扫描
            if layer_name == "L5":
                for entry in entries:
                    exempted.append(ExemptedEntry(
                        entry.entry_id, "L5", ExemptionReason.L5_CORE
                    ))
                continue
            
            # S-03: L4 不参与常规扫描
            if layer_name == "L4" and not scan_l4:
                continue
            
            for entry in entries:
                scanned += 1
                
                # ====== 强制豁免检查 ======
                exempt_reason = self._check_exemptions(entry, cold_entry_ids)
                if exempt_reason:
                    exempted.append(ExemptedEntry(
                        entry.entry_id, layer_name, exempt_reason
                    ))
                    continue
                
                # ====== 获取分槽遗忘阈值 ======
                if slot_thresholds and entry.source_slot_id in slot_thresholds:
                    base_threshold = slot_thresholds[entry.source_slot_id]
                else:
                    base_threshold = self.SLOT_DEFAULTS.get(
                        entry.source_slot_id, self.DEFAULT_THRESHOLD
                    )
                
                # S-04: 动态阈值 = 基础阈值 × 容量调整系数（上限 0.60）
                dynamic_threshold = min(
                    base_threshold * adjust, self.MAX_DYNAMIC_THRESHOLD
                )
                
                # ====== 遗忘双条件判定 ======
                if entry.i_value < dynamic_threshold and entry.reuse_count < self.MIN_REUSE_FOR_RETENTION:
                    # 确定遗忘方式
                    if layer_name == "L4":
                        method = ForgetMethod.COLD_ARCHIVE
                    elif layer_name == "L3":
                        method = l3_method
                    else:
                        method = ForgetMethod.DIRECT_DELETE
                    
                    # 计算遗忘优先级
                    priority = self._calc_forget_priority(entry.i_value, entry.reuse_count)
                    
                    candidates[layer_name].append(ForgetCandidate(
                        entry_id=entry.entry_id,
                        current_layer=layer_name,
                        i_value=entry.i_value,
                        reuse_count=entry.reuse_count,
                        forget_method=method,
                        source_slot_id=entry.source_slot_id,
                        reason=(
                            f"I={entry.i_value:.3f} < θ={dynamic_threshold:.3f}, "
                            f"复用={entry.reuse_count} < {self.MIN_REUSE_FOR_RETENTION}"
                        ),
                        priority=priority
                    ))
        
        # 按遗忘优先级降序排列各层候选（优先级越高越先遗忘）
        for layer_name in candidates:
            candidates[layer_name].sort(key=lambda x: x.priority, reverse=True)
        
        self._total_scanned += scanned
        total_candidates = sum(len(v) for v in candidates.values())
        self._total_candidates += total_candidates
        self._total_exempted += len(exempted)
        
        # 评估容量压力
        estimated_release = total_candidates * 4096  # 模拟每条 4KB
        pressure_warning = (
            self._capacity_level in [CapacityLevel.ORANGE, CapacityLevel.RED]
            and total_candidates < 10
        )
        
        result = ForgetJudgmentResult(
            cycle_id=cycle_id,
            candidates=candidates,
            exempted=exempted,
            scanned_count=scanned,
            capacity_pressure_warning=pressure_warning,
            estimated_release_bytes=estimated_release
        )
        
        self._log_cycle(result)
        self.state = JudgeState.IDLE
        return result
    
    def _check_exemptions(self, entry: EntryMetadata,
                          cold_entry_ids: Optional[set]) -> Optional[ExemptionReason]:
        """
        检查强制豁免规则
        
        豁免优先级（任一满足即豁免）:
        1. 不可抗力标记
        2. 人工锁定标记
        3. 近 7 日被成功采用
        4. 高安全显著性 S ≥ 0.7
        """
        # 不可抗力
        if entry.force_majeure:
            return ExemptionReason.FORCE_MAJEURE
        
        # 人工锁定
        if entry.manual_locked:
            return ExemptionReason.MANUAL_LOCK
        
        # 近 7 日被成功采用
        if entry.last_adopted_time is not None:
            if time.time() - entry.last_adopted_time < self.RECENT_ADOPTED_DAYS * 24 * 3600:
                return ExemptionReason.RECENT_ADOPTED
        
        # 高安全显著性
        if entry.s_value >= self.HIGH_S_THRESHOLD:
            return ExemptionReason.HIGH_S_VALUE
        
        return None
    
    def _calc_forget_priority(self, i_value: float, reuse_count: int) -> float:
        """
        计算遗忘优先级（越高越优先遗忘）
        
        公式: (1 - I值) × 0.5 + (1 - min(复用/5, 1)) × 0.3 + 0.2
        """
        reuse_score = 1.0 - min(reuse_count / 5.0, 1.0)
        return (1.0 - i_value) * 0.5 + reuse_score * 0.3 + 0.2
    
    # ========== 变更日志 ==========
    
    def _log_cycle(self, result: ForgetJudgmentResult) -> None:
        total_candidates = sum(len(v) for v in result.candidates.values())
        self._pending_logs.append({
            "log_id": f"forget-{uuid.uuid4().hex[:8]}",
            "cycle_id": result.cycle_id,
            "scanned": result.scanned_count,
            "candidates": total_candidates,
            "exempted": len(result.exempted),
            "capacity_pressure": result.capacity_pressure_warning,
            "estimated_release_bytes": result.estimated_release_bytes,
            "timestamp": result.timestamp
        })
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs
    
    # ========== 查询接口 ==========
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_scanned": self._total_scanned,
            "total_candidates": self._total_candidates,
            "total_exempted": self._total_exempted,
            "capacity_level": self._capacity_level.value,
            "state": self.state.value
        }


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-40 遗忘阈值判定单元 单元测试")
    print("=" * 60)
    passed, failed = 0, 0
    
    def make_entry(eid, layer, i_val, reuse=0, slot=15, force_majeure=False,
                   manual_locked=False, s_val=0.0, last_adopted=None):
        return EntryMetadata(
            entry_id=eid, current_layer=layer, i_value=i_val,
            reuse_count=reuse, source_slot_id=slot,
            force_majeure=force_majeure, manual_locked=manual_locked,
            s_value=s_val, last_adopted_time=last_adopted
        )
    
    # --- TC-40-01: 正常遗忘判定 ---
    print("\n[TC-40-01] L2 低 I 值条目被标记为遗忘候选")
    try:
        judge = ForgetThresholdJudge()
        entries = {
            "L1": [], "L2": [make_entry("EXP-001", "L2", 0.05, 0, 15)],
            "L3": [], "L4": [], "L5": []
        }
        result = judge.execute_judgment(entries)
        assert len(result.candidates["L2"]) == 1
        assert result.candidates["L2"][0].forget_method == ForgetMethod.DIRECT_DELETE
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-40-02: 不可抗力豁免 ---
    print("\n[TC-40-02] 不可抗力条目豁免遗忘")
    try:
        judge = ForgetThresholdJudge()
        entries = {
            "L1": [make_entry("EXP-002", "L1", 0.02, 0, force_majeure=True)],
            "L2": [], "L3": [], "L4": [], "L5": []
        }
        result = judge.execute_judgment(entries)
        assert len(result.exempted) == 1
        assert result.exempted[0].reason == ExemptionReason.FORCE_MAJEURE
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-40-03: L3 默认冷归档 ---
    print("\n[TC-40-03] L3 条目遗忘方式为冷归档")
    try:
        judge = ForgetThresholdJudge()
        entries = {
            "L1": [], "L2": [], "L3": [make_entry("EXP-003", "L3", 0.04, 0)],
            "L4": [], "L5": []
        }
        result = judge.execute_judgment(entries)
        assert len(result.candidates["L3"]) == 1
        assert result.candidates["L3"][0].forget_method == ForgetMethod.COLD_ARCHIVE
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-40-04: 容量橙色 L3 升级为直接删除 ---
    print("\n[TC-40-04] 容量橙色 L3 遗忘升级为直接删除")
    try:
        judge = ForgetThresholdJudge()
        judge.set_capacity_level(CapacityLevel.ORANGE)
        entries = {
            "L1": [], "L2": [], "L3": [make_entry("EXP-004", "L3", 0.04, 0)],
            "L4": [], "L5": []
        }
        result = judge.execute_judgment(entries)
        assert result.candidates["L3"][0].forget_method == ForgetMethod.DIRECT_DELETE
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-40-05: 容量红色 L4 纳入扫描 ---
    print("\n[TC-40-05] 容量红色 L4 纳入遗忘扫描")
    try:
        judge = ForgetThresholdJudge()
        judge.set_capacity_level(CapacityLevel.RED)
        entries = {
            "L1": [], "L2": [], "L3": [],
            "L4": [make_entry("EXP-005", "L4", 0.03, 0)],
            "L5": []
        }
        result = judge.execute_judgment(entries)
        assert len(result.candidates["L4"]) == 1
        assert result.candidates["L4"][0].forget_method == ForgetMethod.COLD_ARCHIVE
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-40-06: L5 永不扫描 ---
    print("\n[TC-40-06] L5 条目永不参与遗忘扫描")
    try:
        judge = ForgetThresholdJudge()
        entries = {
            "L1": [], "L2": [], "L3": [], "L4": [],
            "L5": [make_entry("EXP-006", "L5", 0.01, 0)]
        }
        result = judge.execute_judgment(entries)
        assert len(result.exempted) == 1
        assert result.exempted[0].reason == ExemptionReason.L5_CORE
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-40-07: 近 7 日采用豁免 ---
    print("\n[TC-40-07] 近 7 日被成功采用 → 豁免遗忘")
    try:
        judge = ForgetThresholdJudge()
        recent_time = time.time() - 3 * 24 * 3600  # 3 天前
        entries = {
            "L1": [make_entry("EXP-007", "L1", 0.05, 0, last_adopted=recent_time)],
            "L2": [], "L3": [], "L4": [], "L5": []
        }
        result = judge.execute_judgment(entries)
        assert len(result.exempted) == 1
        assert result.exempted[0].reason == ExemptionReason.RECENT_ADOPTED
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-40-08: 高 S 值豁免 ---
    print("\n[TC-40-08] 高安全显著性 S≥0.7 → 豁免遗忘")
    try:
        judge = ForgetThresholdJudge()
        entries = {
            "L1": [make_entry("EXP-008", "L1", 0.05, 0, s_val=0.75)],
            "L2": [], "L3": [], "L4": [], "L5": []
        }
        result = judge.execute_judgment(entries)
        assert len(result.exempted) == 1
        assert result.exempted[0].reason == ExemptionReason.HIGH_S_VALUE
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-40-09: 人工锁定豁免 ---
    print("\n[TC-40-09] 人工锁定条目 → 豁免遗忘")
    try:
        judge = ForgetThresholdJudge()
        entries = {
            "L2": [make_entry("EXP-009", "L2", 0.03, 0, manual_locked=True)],
            "L1": [], "L3": [], "L4": [], "L5": []
        }
        result = judge.execute_judgment(entries)
        assert len(result.exempted) == 1
        assert result.exempted[0].reason == ExemptionReason.MANUAL_LOCK
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-40-10: 容量动态调节遗忘阈值 ---
    print("\n[TC-40-10] 容量黄色 I=0.14 ≥ 0.12×1.2 → 保留")
    try:
        judge = ForgetThresholdJudge()
        judge.set_capacity_level(CapacityLevel.YELLOW)
        # 高速巡航槽基础阈值 0.12，黄色调节后 0.144
        entries = {
            "L1": [make_entry("EXP-010", "L1", 0.14, 0, slot=15)],
            "L2": [], "L3": [], "L4": [], "L5": []
        }
        result = judge.execute_judgment(entries)
        assert len(result.candidates["L1"]) == 0  # 应被保留
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    print(f"\n测试结果: {passed} PASS, {failed} FAIL")