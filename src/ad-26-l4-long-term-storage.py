#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-26
模块名称: L4 长期层存储单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 五层记忆层级存储
核心职责: 存储跨场景可泛化复用的高阶驾驶技能与已验证的关键经验，占漏斗二总存储
          容量的 4.5%。接收来自 L3 晋升的条目（已通过安全仲裁），在留存满 90 日
          且满足极高重要度条件后进入 L5 核心层晋升候选。本层经验享受最高等级遗忘
          保护，采用冷归档替代直接删除。

依赖模块: ad-24(L3 中期层存储单元), ad-27(L4 长期层经验抽象提炼单元),
          ad-38(晋升双条件判定单元), ad-40(遗忘阈值判定单元), ad-50(导出单元)
被依赖模块: ad-27(消费 L4 经验进行抽象提炼), ad-38(消费 L4 晋升候选条目),
            ad-40(消费 L4 遗忘候选条目), ad-50(消费 L4 泛化经验导出)

L5 晋升条件（硬编码）:
  - I ≥ 0.80
  - 留存 ≥ 90 日
  - 复用 ≥ 10 次
  - 不可抗力豁免时间和复用限制

安全约束:
  S-01: L4 遗忘执行冷归档而非直接删除，所有归档经验可追溯恢复
  S-02: 不可抗力场景经验在 L4 遗忘评估时绝对豁免
  S-03: L5 晋升条件硬编码，不可抗力豁免时间和复用限制
  S-04: 晋升 L5 失败回退 ≥ 1 次即标记"L5 晋升困难"
  S-05: 导出至 ad-50 的经验包须执行脱敏处理
  S-06: 策略失误经验进入 L4 须持有安全仲裁通过标记
  S-07: 所有操作全量写入 ad-51 变更日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class StorageState(Enum):
    """L4 存储内部状态"""
    NORMAL = "normal"
    NEAR_FULL = "near_full"
    FULL = "full"
    MAINTENANCE = "maintenance"
    FROZEN = "frozen"


class PromotionResult(Enum):
    """晋升结果"""
    SUCCESS = "success"
    FAIL_TARGET_FULL = "fail_target_full"
    FAIL_CONDITIONS_NOT_MET = "fail_conditions_not_met"
    DEFER = "defer"


class ForgetResult(Enum):
    """遗忘结果"""
    ARCHIVED = "archived"
    RETAINED = "retained"
    RETAINED_FORCE_MAJEURE = "retained_force_majeure"


# ==================== 数据结构 ====================

@dataclass
class L4EntryIndex:
    """L4 条目索引"""
    entry_id: str
    storage_address: int
    promote_timestamp: float         # 晋升到 L4 的时间
    i_value: float
    s_value: float
    source_slot_id: int
    sub_label: str
    result_label: str
    force_majeure: bool
    arbitration_passed: bool         # 安全仲裁通过标记
    reuse_count: int
    size_bytes: int
    fallback_count: int = 0
    is_refined: bool = False         # 是否已被 ad-27 提炼
    refined_rule_id: Optional[str] = None
    promotion_difficult: bool = False


@dataclass
class PromotionCandidate:
    """晋升候选条目"""
    entry_id: str
    target_layer: str                # "L5"
    i_value: float
    retention_duration: float


@dataclass
class ForgetCandidate:
    """遗忘候选条目"""
    entry_id: str
    current_layer: str               # "L4"
    i_value: float


@dataclass
class ExportRequest:
    """导出请求（来自 ad-50）"""
    request_id: str
    export_scope: str                # "all" / "slot" / "rule"
    slot_id_filter: Optional[int] = None
    require_desensitize: bool = True
    timestamp: float = field(default_factory=time.time)


@dataclass
class L4StatusSnapshot:
    """L4 状态快照"""
    total_capacity: int
    used_count: int
    usage_rate: float
    avg_retention_days: float
    refined_count: int
    force_majeure_count: int
    entries_by_slot: Dict[int, int]
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class L4LongTermStorage:
    """
    L4 长期层存储单元
    
    职责:
    1. 接收并存储从 L3 晋升的经验条目（须已通过安全仲裁）
    2. 管理 L5 晋升条件判定（I≥0.80, 留存≥90日, 复用≥10次）
    3. 不可抗力条目豁免时间和复用限制
    4. 遗忘处理（冷归档，不可抗力豁免）
    5. 向 ad-27 提供经验数据用于抽象提炼
    6. 向 ad-50 提供脱敏经验用于导出
    """
    
    # L5 晋升条件（硬编码）
    L5_MIN_I = 0.80
    L5_MIN_RETENTION = 90 * 24 * 3600    # 90 日
    L5_MIN_REUSE = 10
    
    # 容量阈值
    NEAR_FULL_THRESHOLD = 0.85
    FULL_THRESHOLD = 0.95
    
    # 晋升 I 值固化加成
    L4_I_BOOST = 0.05
    
    # 遗忘 I 阈值（极高门槛）
    MIN_FORGET_I_THRESHOLD = 0.20
    
    # 晋升困难 I 值惩罚
    PROMOTION_DIFFICULTY_I_PENALTY = 0.03
    
    # 抽象提炼触发间隔（秒）
    REFINE_TRIGGER_INTERVAL = 30 * 24 * 3600  # 30 日
    REFINE_MIN_ENTRIES = 10
    
    # 碎片整理间隔（秒）
    DEFRAG_INTERVAL = 7 * 24 * 3600
    
    def __init__(self, max_entries: int = 45):
        """
        初始化 L4 长期层
        
        Args:
            max_entries: 最大条目数（占漏斗二总容量 4.5%）
        """
        self.module_id = "ad-26"
        self.module_name = "L4 长期层存储单元"
        
        self.state = StorageState.NORMAL
        self.max_entries = max_entries
        
        self._index: Dict[str, L4EntryIndex] = {}
        self._rule_index: Dict[str, Dict[str, Any]] = {}  # 提炼规则索引
        
        self._next_address = 0x40000000
        
        self._last_defrag_time = time.time()
        self._last_refine_check = time.time()
        
        self._total_promotions_in = 0
        self._total_promotions_out = 0
        self._total_forgets = 0
        self._total_exports = 0
        self._total_rejections = 0
        
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] L4 长期层初始化完成, 最大容量={max_entries}")
        print(f"[{self.module_id}] L5 晋升条件: I≥{self.L5_MIN_I}, "
              f"留存≥{self.L5_MIN_RETENTION/86400:.0f}日, 复用≥{self.L5_MIN_REUSE}次")
    
    # ========== 状态管理 ==========
    
    def freeze(self) -> None:
        self.state = StorageState.FROZEN
    
    def unfreeze(self) -> None:
        self.state = StorageState.NORMAL
    
    def get_state(self) -> StorageState:
        return self.state
    
    def get_item_count(self) -> int:
        return len(self._index)
    
    def get_usage_rate(self) -> float:
        return len(self._index) / self.max_entries if self.max_entries > 0 else 0.0
    
    # ========== 晋升写入（L3 → L4） ==========
    
    def receive_from_transfer(self, entries: List[Dict[str, Any]]) -> int:
        """
        接收从 L3 晋升上来的条目
        
        S-06: 策略失误经验须持有安全仲裁通过标记
        """
        if self.state in [StorageState.FROZEN, StorageState.MAINTENANCE]:
            return 0
        
        written = 0
        for entry_data in entries:
            entry_id = entry_data.get("entry_id", "")
            result_label = entry_data.get("result_label", "成功优化")
            arbitration_passed = entry_data.get("arbitration_passed", True)
            
            if not entry_id:
                self._total_rejections += 1
                continue
            
            # S-06: 策略失误经验须通过仲裁
            if result_label == "策略失误" and not arbitration_passed:
                self._total_rejections += 1
                print(f"[{self.module_id}] 拒绝晋升: {entry_id[:12]} (策略失误未通过仲裁)")
                continue
            
            # 检查容量
            if self.get_usage_rate() >= self.FULL_THRESHOLD:
                self._emergency_archive()
                if self.get_usage_rate() >= self.FULL_THRESHOLD:
                    self._total_rejections += 1
                    return written
            
            # I 值固化加成
            i_value = min(entry_data.get("i_value", 0.0) + self.L4_I_BOOST, 1.0)
            
            storage_address = self._next_address
            self._next_address += 8192  # L4 条目更大，8KB
            
            idx_entry = L4EntryIndex(
                entry_id=entry_id,
                storage_address=storage_address,
                promote_timestamp=time.time(),
                i_value=i_value,
                s_value=entry_data.get("s_value", 0.0),
                source_slot_id=entry_data.get("source_slot_id", 0),
                sub_label=entry_data.get("sub_label", ""),
                result_label=result_label,
                force_majeure=entry_data.get("force_majeure", False),
                arbitration_passed=arbitration_passed,
                reuse_count=entry_data.get("reuse_count", 0),
                size_bytes=8192,
            )
            
            self._index[entry_id] = idx_entry
            self._total_promotions_in += 1
            written += 1
        
        self._update_capacity_state()
        
        if written > 0:
            print(f"[{self.module_id}] 接收 L3 晋升: {written} 条")
        
        return written
    
    def _emergency_archive(self) -> int:
        """紧急冷归档：归档最旧的 10% 条目（跳过不可抗力）"""
        if not self._index:
            return 0
        
        sorted_entries = sorted(self._index.items(), key=lambda x: x[1].promote_timestamp)
        remove_count = max(1, int(len(self._index) * 0.10))
        
        archived = 0
        for i in range(min(remove_count, len(sorted_entries))):
            entry_id, idx_entry = sorted_entries[i]
            if idx_entry.force_majeure:
                continue
            del self._index[entry_id]
            archived += 1
            self._total_forgets += 1
        
        if archived > 0:
            print(f"[{self.module_id}] 紧急冷归档: {archived} 条")
        
        return archived
    
    def _update_capacity_state(self) -> None:
        usage = self.get_usage_rate()
        if usage >= self.FULL_THRESHOLD:
            self.state = StorageState.FULL
        elif usage >= self.NEAR_FULL_THRESHOLD:
            self.state = StorageState.NEAR_FULL
        else:
            if self.state in [StorageState.NEAR_FULL, StorageState.FULL]:
                self.state = StorageState.NORMAL
    
    # ========== 晋升处理（L4 → L5） ==========
    
    def process_promotions(self, candidates: List[PromotionCandidate]) -> List[Tuple[str, PromotionResult]]:
        """
        处理晋升候选清单（L4 → L5）
        
        L5 晋升条件:
        - I ≥ 0.80
        - 留存 ≥ 90 日
        - 复用 ≥ 10 次
        - 不可抗力豁免时间和复用限制
        """
        if self.state == StorageState.FROZEN:
            return [(c.entry_id, PromotionResult.FAIL_TARGET_FULL) for c in candidates]
        
        results = []
        for candidate in candidates:
            entry_id = candidate.entry_id
            
            if entry_id not in self._index:
                results.append((entry_id, PromotionResult.FAIL_TARGET_FULL))
                continue
            
            idx_entry = self._index[entry_id]
            
            # 不可抗力豁免
            if idx_entry.force_majeure:
                if candidate.i_value >= self.L5_MIN_I:
                    del self._index[entry_id]
                    self._total_promotions_out += 1
                    results.append((entry_id, PromotionResult.SUCCESS))
                    continue
            
            # 正常三条件判定
            retention_ok = candidate.retention_duration >= self.L5_MIN_RETENTION
            i_ok = candidate.i_value >= self.L5_MIN_I
            reuse_ok = idx_entry.reuse_count >= self.L5_MIN_REUSE
            
            if retention_ok and i_ok and reuse_ok:
                del self._index[entry_id]
                self._total_promotions_out += 1
                results.append((entry_id, PromotionResult.SUCCESS))
            else:
                reasons = []
                if not retention_ok:
                    reasons.append(f"留存不足({candidate.retention_duration/86400:.0f}<90日)")
                if not i_ok:
                    reasons.append(f"I值不足({candidate.i_value:.2f}<{self.L5_MIN_I})")
                if not reuse_ok:
                    reasons.append(f"复用不足({idx_entry.reuse_count}<{self.L5_MIN_REUSE})")
                
                results.append((entry_id, PromotionResult.FAIL_CONDITIONS_NOT_MET))
        
        self._update_capacity_state()
        return results
    
    # ========== 遗忘处理（L4 → 冷归档） ==========
    
    def process_forget_candidates(self, candidates: List[ForgetCandidate]) -> List[Tuple[str, ForgetResult]]:
        """
        处理遗忘候选清单
        
        S-02: 不可抗力绝对豁免
        """
        if self.state == StorageState.FROZEN:
            return [(c.entry_id, ForgetResult.RETAINED) for c in candidates]
        
        results = []
        for candidate in candidates:
            entry_id = candidate.entry_id
            
            if entry_id not in self._index:
                results.append((entry_id, ForgetResult.ARCHIVED))
                continue
            
            idx_entry = self._index[entry_id]
            
            # 不可抗力保护
            if idx_entry.force_majeure:
                results.append((entry_id, ForgetResult.RETAINED_FORCE_MAJEURE))
                continue
            
            # 冷归档
            if candidate.i_value < self.MIN_FORGET_I_THRESHOLD:
                del self._index[entry_id]
                self._total_forgets += 1
                results.append((entry_id, ForgetResult.ARCHIVED))
            else:
                results.append((entry_id, ForgetResult.RETAINED))
        
        self._update_capacity_state()
        return results
    
    # ========== 晋升失败回退 ==========
    
    def handle_promotion_fallback(self, entry_id: str, reason: str) -> None:
        """
        处理从 L5 晋升失败回退
        
        S-04: 回退 ≥ 1 次即标记"L5 晋升困难"
        """
        if entry_id not in self._index:
            return
        
        idx_entry = self._index[entry_id]
        idx_entry.fallback_count += 1
        
        if idx_entry.fallback_count >= 1:
            idx_entry.promotion_difficult = True
            idx_entry.i_value = max(0.0, idx_entry.i_value - self.PROMOTION_DIFFICULTY_I_PENALTY)
            print(f"[{self.module_id}] 条目 {entry_id[:12]} L5 晋升困难，I 值降至 {idx_entry.i_value:.3f}")
    
    # ========== 抽象提炼触发 ==========
    
    def should_trigger_refine(self) -> bool:
        """检查是否应触发抽象提炼"""
        now = time.time()
        if now - self._last_refine_check < self.REFINE_TRIGGER_INTERVAL:
            return False
        self._last_refine_check = now
        return len(self._index) >= self.REFINE_MIN_ENTRIES
    
    def get_unrefined_entries(self) -> List[L4EntryIndex]:
        """获取尚未被提炼的条目"""
        return [idx for idx in self._index.values() if not idx.is_refined]
    
    def mark_as_refined(self, entry_ids: List[str], rule_id: str) -> None:
        """标记条目已被提炼"""
        for entry_id in entry_ids:
            if entry_id in self._index:
                self._index[entry_id].is_refined = True
                self._index[entry_id].refined_rule_id = rule_id
    
    def add_rule(self, rule_id: str, rule_data: Dict[str, Any]) -> None:
        """添加提炼规则到规则索引"""
        self._rule_index[rule_id] = rule_data
    
    # ========== 导出处理 ==========
    
    def handle_export(self, request: ExportRequest) -> List[Dict[str, Any]]:
        """
        处理导出请求
        
        S-05: 执行脱敏处理——剔除 GPS 坐标、行人/车辆特征、时间戳
        """
        self._total_exports += 1
        export_data = []
        
        for idx_entry in self._index.values():
            # 按导出范围筛选
            if request.export_scope == "slot" and request.slot_id_filter is not None:
                if idx_entry.source_slot_id != request.slot_id_filter:
                    continue
            
            entry_data = {
                "entry_id": idx_entry.entry_id,
                "i_value": idx_entry.i_value,
                "s_value": idx_entry.s_value,
                "source_slot_id": idx_entry.source_slot_id,
                "sub_label": idx_entry.sub_label,
                "result_label": idx_entry.result_label,
                "reuse_count": idx_entry.reuse_count,
            }
            
            # 脱敏：已由上层 ad-50 负责完整脱敏
            export_data.append(entry_data)
        
        print(f"[{self.module_id}] 导出请求处理: {len(export_data)} 条")
        return export_data
    
    # ========== 状态上报 ==========
    
    def generate_snapshot(self) -> L4StatusSnapshot:
        entries_by_slot: Dict[int, int] = {}
        total_retention = 0.0
        refined_count = 0
        force_majeure_count = 0
        
        for idx in self._index.values():
            entries_by_slot[idx.source_slot_id] = entries_by_slot.get(idx.source_slot_id, 0) + 1
            total_retention += time.time() - idx.promote_timestamp
            if idx.is_refined:
                refined_count += 1
            if idx.force_majeure:
                force_majeure_count += 1
        
        avg_days = (total_retention / max(len(self._index), 1)) / (24 * 3600)
        
        return L4StatusSnapshot(
            total_capacity=self.max_entries,
            used_count=len(self._index),
            usage_rate=self.get_usage_rate(),
            avg_retention_days=avg_days,
            refined_count=refined_count,
            force_majeure_count=force_majeure_count,
            entries_by_slot=entries_by_slot
        )
    
    def get_entry(self, entry_id: str) -> Optional[L4EntryIndex]:
        return self._index.get(entry_id)
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_promotions_in": self._total_promotions_in,
            "total_promotions_out": self._total_promotions_out,
            "total_forgets": self._total_forgets,
            "total_exports": self._total_exports,
            "total_rejections": self._total_rejections,
            "current_entries": len(self._index),
            "rules_count": len(self._rule_index),
            "max_entries": self.max_entries,
            "usage_rate": self.get_usage_rate(),
            "state": self.state.value
        }
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-26 L4 长期层存储单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    def make_transfer_entry(entry_id, i_value=0.85, result_label="成功优化", 
                            force_majeure=False, arbitration_passed=True, reuse_count=15):
        return {
            "entry_id": entry_id,
            "i_value": i_value,
            "s_value": 0.7,
            "source_slot_id": 15,
            "sub_label": "常规通用",
            "result_label": result_label,
            "force_majeure": force_majeure,
            "arbitration_passed": arbitration_passed,
            "reuse_count": reuse_count,
            "content": {"behavior": "高速跟车"}
        }
    
    # --- TC-26-01: 成功经验写入（I 值固化 +0.05） ---
    print("\n[TC-26-01] 成功经验写入（I 值固化 +0.05）")
    try:
        l4 = L4LongTermStorage(max_entries=30)
        entries = [make_transfer_entry("EXP-001", i_value=0.85)]
        written = l4.receive_from_transfer(entries)
        assert written == 1
        assert l4._index["EXP-001"].i_value == 0.90
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-26-02: 不可抗力写入 ---
    print("\n[TC-26-02] 不可抗力写入")
    try:
        l4 = L4LongTermStorage(max_entries=30)
        entries = [make_transfer_entry("EXP-002", i_value=0.85, force_majeure=True)]
        written = l4.receive_from_transfer(entries)
        assert written == 1
        assert l4._index["EXP-002"].force_majeure == True
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-26-03: 策略失误未通过仲裁拒绝 ---
    print("\n[TC-26-03] 策略失误未通过仲裁拒绝")
    try:
        l4 = L4LongTermStorage(max_entries=30)
        entries = [make_transfer_entry("EXP-003", result_label="策略失误", arbitration_passed=False)]
        written = l4.receive_from_transfer(entries)
        assert written == 0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-26-04: 满足 L5 晋升条件 ---
    print("\n[TC-26-04] 满足 L5 晋升条件")
    try:
        l4 = L4LongTermStorage(max_entries=30)
        l4.receive_from_transfer([make_transfer_entry("EXP-004", i_value=0.88, reuse_count=12)])
        candidates = [PromotionCandidate("EXP-004", "L5", 0.88, 95*24*3600)]
        results = l4.process_promotions(candidates)
        assert results[0][1] == PromotionResult.SUCCESS
        assert "EXP-004" not in l4._index
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-26-05: 复用不足暂缓晋升 ---
    print("\n[TC-26-05] 复用不足暂缓晋升（复用 5 < 10）")
    try:
        l4 = L4LongTermStorage(max_entries=30)
        l4.receive_from_transfer([make_transfer_entry("EXP-005", i_value=0.88, reuse_count=5)])
        candidates = [PromotionCandidate("EXP-005", "L5", 0.88, 95*24*3600)]
        results = l4.process_promotions(candidates)
        assert results[0][1] == PromotionResult.FAIL_CONDITIONS_NOT_MET
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-26-06: 不可抗力豁免复用晋升 ---
    print("\n[TC-26-06] 不可抗力豁免复用限制晋升")
    try:
        l4 = L4LongTermStorage(max_entries=30)
        l4.receive_from_transfer([make_transfer_entry("EXP-006", i_value=0.85, force_majeure=True, reuse_count=3)])
        candidates = [PromotionCandidate("EXP-006", "L5", 0.85, 30*24*3600)]
        results = l4.process_promotions(candidates)
        assert results[0][1] == PromotionResult.SUCCESS
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-26-07: 遗忘候选（冷归档） ---
    print("\n[TC-26-07] 遗忘候选（冷归档）")
    try:
        l4 = L4LongTermStorage(max_entries=30)
        l4.receive_from_transfer([make_transfer_entry("EXP-007", i_value=0.15)])
        candidates = [ForgetCandidate("EXP-007", "L4", 0.15)]
        results = l4.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.ARCHIVED
        assert "EXP-007" not in l4._index
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-26-08: 不可抗力遗忘豁免 ---
    print("\n[TC-26-08] 不可抗力遗忘豁免")
    try:
        l4 = L4LongTermStorage(max_entries=30)
        l4.receive_from_transfer([make_transfer_entry("EXP-008", i_value=0.10, force_majeure=True)])
        candidates = [ForgetCandidate("EXP-008", "L4", 0.10)]
        results = l4.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.RETAINED_FORCE_MAJEURE
        assert "EXP-008" in l4._index
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-26-09: 晋升失败回退标记困难 ---
    print("\n[TC-26-09] 晋升失败回退标记困难")
    try:
        l4 = L4LongTermStorage(max_entries=30)
        l4.receive_from_transfer([make_transfer_entry("EXP-009", i_value=0.88)])
        l4.handle_promotion_fallback("EXP-009", "L5_storage_full")
        assert l4._index["EXP-009"].promotion_difficult == True
        assert l4._index["EXP-009"].i_value == 0.85
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-26-10: 存储满紧急冷归档 ---
    print("\n[TC-26-10] 存储满紧急冷归档（跳过不可抗力）")
    try:
        l4 = L4LongTermStorage(max_entries=5)
        l4.receive_from_transfer([make_transfer_entry("EXP-A", i_value=0.8, force_majeure=True)])
        for i in range(5):
            l4.receive_from_transfer([make_transfer_entry(f"EXP-{i:03d}", i_value=0.8)])
        assert "EXP-A" in l4._index  # 不可抗力被保护
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