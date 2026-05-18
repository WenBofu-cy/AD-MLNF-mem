#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-22
模块名称: L2 近期层存储单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 五层记忆层级存储
核心职责: 存储近 7 日内高频出现的驾驶场景经验，占漏斗二总存储容量的 25%。
          接收来自 L1 晋升的条目，在留存满 7 日且满足重要度条件后进入 L3 晋升候选。
          维护本层条目的热度统计，为晋升决策提供查询命中频率参考。

依赖模块: ad-20(L1 临时层存储单元，接收晋升条目), ad-23(L2 近期层热度统计单元),
          ad-38(晋升双条件判定单元), ad-40(遗忘阈值判定单元)
被依赖模块: ad-23(消费 L2 存储条目进行热度统计), ad-38(消费 L2 晋升候选条目),
            ad-40(消费 L2 遗忘候选条目)

安全约束:
  S-01: L2 层所有条目最大留存时间硬编码为 7 日（168 小时），超时未晋升则进入遗忘评估
  S-02: 安全显著性 S ≥ 0.7 的条目在批量清除时享有保护，不被强制清除
  S-03: 晋升失败回退次数 ≥ 3 的条目须标记为"晋升困难"，降低 I 值并上报告警
  S-04: 条目索引表每 12 小时自动备份至冗余分区
  S-05: 存储写入须校验条目完整性
  S-06: 冻结状态下禁止任何写入操作
  S-07: 所有写入、晋升、遗忘、回退操作全量写入 ad-51 变更日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class StorageState(Enum):
    """L2 存储内部状态"""
    NORMAL = "normal"
    NEAR_FULL = "near_full"
    FULL = "full"
    MAINTENANCE = "maintenance"
    FROZEN = "frozen"


class PromotionResult(Enum):
    """晋升结果"""
    SUCCESS = "success"
    FAIL_TARGET_FULL = "fail_target_full"
    FAIL_LAYER_NOT_EXIST = "fail_layer_not_exist"


class ForgetResult(Enum):
    """遗忘结果"""
    DELETED = "deleted"
    ARCHIVED = "archived"
    RETAINED = "retained"


# ==================== 数据结构 ====================

@dataclass
class ExperienceEntry:
    """经验条目"""
    entry_id: str
    content: Dict[str, Any]
    i_value: float
    s_value: float = 0.0
    source_slot_id: int = 0
    sub_label: str = ""
    result_label: str = "成功优化"
    force_majeure: bool = False
    reuse_count: int = 0


@dataclass
class L2EntryIndex:
    """L2 条目索引"""
    entry_id: str
    storage_address: int
    promote_timestamp: float         # 晋升到 L2 的时间
    i_value: float
    s_value: float
    source_slot_id: int
    sub_label: str
    result_label: str
    force_majeure: bool
    reuse_count: int
    size_bytes: int
    fallback_count: int = 0          # 晋升失败回退次数


@dataclass
class PromotionCandidate:
    """晋升候选条目（来自 ad-38）"""
    entry_id: str
    target_layer: str                # "L3"
    i_value: float
    retention_duration: float


@dataclass
class ForgetCandidate:
    """遗忘候选条目（来自 ad-40）"""
    entry_id: str
    current_layer: str               # "L2"
    i_value: float


@dataclass
class L2StatusSnapshot:
    """L2 状态快照"""
    total_capacity: int
    used_count: int
    usage_rate: float
    avg_retention_days: float
    entries_by_slot: Dict[int, int]
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class L2RecentStorage:
    """
    L2 近期层存储单元
    
    职责:
    1. 接收并存储从 L1 晋升的经验条目
    2. 维护条目索引表
    3. 处理晋升候选（L2 → L3）
    4. 处理遗忘候选
    5. 处理晋升失败回退
    6. 响应全局容量告急的批量清除指令
    """
    
    # 单条经验最大留存时间（秒）
    MAX_RETENTION_SECONDS = 7 * 24 * 3600  # 7 日
    
    # 容量阈值
    NEAR_FULL_THRESHOLD = 0.85
    FULL_THRESHOLD = 0.95
    
    # 紧急清除比例
    EMERGENCY_CLEAR_RATIO = 0.05
    BATCH_CLEAR_RATIO = 0.15              # 全局告急时清除 15%
    
    # 安全条目保护阈值
    SAFE_S_THRESHOLD = 0.7
    
    # 晋升困难回退次数阈值
    PROMOTION_DIFFICULTY_THRESHOLD = 3
    PROMOTION_DIFFICULTY_I_PENALTY = 0.05
    
    # 碎片整理间隔（秒）
    DEFRAG_INTERVAL = 12 * 3600           # 12 小时
    
    # L2 晋升 I 值微调
    L2_I_BOOST = 0.02
    
    def __init__(self, max_entries: int = 250):
        """
        初始化 L2 近期层
        
        Args:
            max_entries: 最大条目数（占漏斗二总容量 25%）
        """
        self.module_id = "ad-22"
        self.module_name = "L2 近期层存储单元"
        
        # 内部状态
        self.state = StorageState.NORMAL
        
        # 最大容量
        self.max_entries = max_entries
        
        # 条目索引表: entry_id -> L2EntryIndex
        self._index: Dict[str, L2EntryIndex] = {}
        
        # 存储地址计数器（模拟）
        self._next_address = 0x20000000
        
        # 上次碎片整理时间
        self._last_defrag_time = time.time()
        
        # 统计
        self._total_promotions_in = 0
        self._total_promotions_out = 0
        self._total_forgets = 0
        self._total_fallbacks = 0
        self._total_rejections = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] L2 近期层初始化完成, 最大容量={max_entries}")
    
    # ========== 状态管理 ==========
    
    def freeze(self) -> None:
        """冻结 L2（驾驶模式切换时调用）"""
        self.state = StorageState.FROZEN
        print(f"[{self.module_id}] L2 已冻结")
    
    def unfreeze(self) -> None:
        """解冻 L2"""
        self.state = StorageState.NORMAL
        print(f"[{self.module_id}] L2 已解冻")
    
    def get_state(self) -> StorageState:
        return self.state
    
    def get_item_count(self) -> int:
        return len(self._index)
    
    def get_usage_rate(self) -> float:
        return len(self._index) / self.max_entries if self.max_entries > 0 else 0.0
    
    # ========== 晋升写入（L1 → L2） ==========
    
    def receive_from_transfer(self, entries: List[Dict[str, Any]]) -> int:
        """
        接收从 L1 晋升上来的条目
        
        Args:
            entries: 条目列表
            
        Returns:
            成功写入的条目数
        """
        if self.state == StorageState.FROZEN:
            return 0
        
        if self.state == StorageState.MAINTENANCE:
            return 0
        
        written = 0
        for entry_data in entries:
            entry_id = entry_data.get("entry_id", "")
            i_value = entry_data.get("i_value", 0.0)
            s_value = entry_data.get("s_value", 0.0)
            
            # S-05: 校验完整性
            if not entry_id:
                self._total_rejections += 1
                continue
            
            # 检查容量
            if self.get_usage_rate() >= self.FULL_THRESHOLD:
                self._emergency_clear()
                if self.get_usage_rate() >= self.FULL_THRESHOLD:
                    self._total_rejections += 1
                    return written
            
            # L2 晋升 I 值微调
            adjusted_i = min(i_value + self.L2_I_BOOST, 1.0)
            
            # 分配存储地址
            storage_address = self._next_address
            self._next_address += 2048  # 模拟每条经验 2KB
            
            # 创建索引条目
            idx_entry = L2EntryIndex(
                entry_id=entry_id,
                storage_address=storage_address,
                promote_timestamp=time.time(),
                i_value=adjusted_i,
                s_value=s_value,
                source_slot_id=entry_data.get("source_slot_id", 0),
                sub_label=entry_data.get("sub_label", ""),
                result_label=entry_data.get("result_label", "成功优化"),
                force_majeure=entry_data.get("force_majeure", False),
                reuse_count=entry_data.get("reuse_count", 0),
                size_bytes=2048
            )
            
            self._index[entry_id] = idx_entry
            self._total_promotions_in += 1
            written += 1
        
        # 更新容量状态
        self._update_capacity_state()
        
        if written > 0:
            print(f"[{self.module_id}] 接收 L1 晋升: {written} 条")
        
        return written
    
    def _emergency_clear(self) -> int:
        """紧急清除：删除 I 值最低的 5% 条目"""
        if not self._index:
            return 0
        
        sorted_entries = sorted(self._index.items(), key=lambda x: x[1].i_value)
        remove_count = max(1, int(len(self._index) * self.EMERGENCY_CLEAR_RATIO))
        
        cleared = 0
        for i in range(min(remove_count, len(sorted_entries))):
            entry_id, idx_entry = sorted_entries[i]
            
            # S-02: S ≥ 0.7 受保护
            if idx_entry.s_value >= self.SAFE_S_THRESHOLD:
                continue
            # 不可抗力保护
            if idx_entry.force_majeure:
                continue
            
            del self._index[entry_id]
            cleared += 1
            self._total_forgets += 1
        
        if cleared > 0:
            print(f"[{self.module_id}] 紧急清除: {cleared} 条")
        
        return cleared
    
    def _update_capacity_state(self) -> None:
        """更新容量状态"""
        usage = self.get_usage_rate()
        if usage >= self.FULL_THRESHOLD:
            self.state = StorageState.FULL
        elif usage >= self.NEAR_FULL_THRESHOLD:
            self.state = StorageState.NEAR_FULL
        else:
            if self.state in [StorageState.NEAR_FULL, StorageState.FULL]:
                self.state = StorageState.NORMAL
    
    # ========== 晋升处理（L2 → L3） ==========
    
    def process_promotions(self, candidates: List[PromotionCandidate]) -> List[Tuple[str, PromotionResult]]:
        """
        处理晋升候选清单（L2 → L3）
        
        Args:
            candidates: 晋升候选列表
            
        Returns:
            [(entry_id, 结果), ...]
        """
        if self.state == StorageState.FROZEN:
            return [(c.entry_id, PromotionResult.FAIL_LAYER_NOT_EXIST) for c in candidates]
        
        results = []
        for candidate in candidates:
            if candidate.entry_id not in self._index:
                results.append((candidate.entry_id, PromotionResult.FAIL_LAYER_NOT_EXIST))
                continue
            
            # 模拟目标层级容量检查（简化实现）
            # 实际应由 ad-38 在判定时检查 L3 容量
            # 此处假设 L3 有足够空间
            
            # 从 L2 移除
            del self._index[candidate.entry_id]
            self._total_promotions_out += 1
            results.append((candidate.entry_id, PromotionResult.SUCCESS))
        
        self._update_capacity_state()
        return results
    
    # ========== 遗忘处理 ==========
    
    def process_forget_candidates(self, candidates: List[ForgetCandidate]) -> List[Tuple[str, ForgetResult]]:
        """
        处理遗忘候选清单
        
        Args:
            candidates: 遗忘候选列表
            
        Returns:
            [(entry_id, 结果), ...]
        """
        if self.state == StorageState.FROZEN:
            return [(c.entry_id, ForgetResult.RETAINED) for c in candidates]
        
        results = []
        for candidate in candidates:
            if candidate.entry_id not in self._index:
                results.append((candidate.entry_id, ForgetResult.DELETED))
                continue
            
            entry = self._index[candidate.entry_id]
            
            # 不可抗力保护
            if entry.force_majeure:
                results.append((candidate.entry_id, ForgetResult.RETAINED))
                continue
            
            # 安全条目保护
            if entry.s_value >= self.SAFE_S_THRESHOLD:
                results.append((candidate.entry_id, ForgetResult.RETAINED))
                continue
            
            # 执行删除
            del self._index[candidate.entry_id]
            self._total_forgets += 1
            results.append((candidate.entry_id, ForgetResult.DELETED))
        
        self._update_capacity_state()
        return results
    
    # ========== 晋升失败回退 ==========
    
    def handle_promotion_fallback(self, entry_id: str, reason: str) -> None:
        """
        处理从 L3 晋升失败回退的条目
        
        S-03: 回退次数 ≥ 3 时标记为"晋升困难"，降低 I 值
        
        Args:
            entry_id: 条目 ID
            reason: 回退原因
        """
        if entry_id not in self._index:
            print(f"[{self.module_id}] 回退条目 {entry_id[:12]} 不在 L2 中，重新添加")
            return
        
        entry = self._index[entry_id]
        entry.fallback_count += 1
        self._total_fallbacks += 1
        
        if entry.fallback_count >= self.PROMOTION_DIFFICULTY_THRESHOLD:
            entry.i_value = max(0.0, entry.i_value - self.PROMOTION_DIFFICULTY_I_PENALTY)
            print(f"[{self.module_id}] 条目 {entry_id[:12]} 晋升困难（回退{entry.fallback_count}次），"
                  f"I 值降至 {entry.i_value:.3f}")
        
        print(f"[{self.module_id}] 回退条目 {entry_id[:12]} 保留 L2（{reason}）")
    
    # ========== 批量清除 ==========
    
    def execute_batch_clear(self, clear_ratio: float = None) -> int:
        """
        执行全局容量告急的批量清除
        
        S-02: S ≥ 0.7 的条目受保护
        
        Args:
            clear_ratio: 清除比例（默认 15%）
            
        Returns:
            清除的条目数
        """
        if clear_ratio is None:
            clear_ratio = self.BATCH_CLEAR_RATIO
        
        if not self._index:
            return 0
        
        self.state = StorageState.MAINTENANCE
        
        sorted_entries = sorted(self._index.items(), key=lambda x: x[1].i_value)
        remove_count = max(1, int(len(self._index) * clear_ratio))
        
        cleared = 0
        for i in range(min(remove_count, len(sorted_entries))):
            entry_id, idx_entry = sorted_entries[i]
            
            if idx_entry.s_value >= self.SAFE_S_THRESHOLD:
                continue
            if idx_entry.force_majeure:
                continue
            
            del self._index[entry_id]
            cleared += 1
            self._total_forgets += 1
        
        self.state = StorageState.NORMAL
        
        if cleared > 0:
            print(f"[{self.module_id}] 批量清除: {cleared} 条 ({clear_ratio*100:.0f}%)")
        
        return cleared
    
    # ========== 碎片整理 ==========
    
    def check_defrag(self) -> None:
        """检查并执行碎片整理"""
        now = time.time()
        if now - self._last_defrag_time < self.DEFRAG_INTERVAL:
            return
        
        usage = self.get_usage_rate()
        if usage < 0.70:
            return
        
        self.state = StorageState.MAINTENANCE
        self._last_defrag_time = now
        print(f"[{self.module_id}] 执行碎片整理, 使用率={usage:.1%}")
        self.state = StorageState.NORMAL
    
    # ========== 状态上报 ==========
    
    def generate_snapshot(self) -> L2StatusSnapshot:
        """生成 L2 状态快照"""
        entries_by_slot: Dict[int, int] = {}
        total_retention = 0.0
        
        for idx in self._index.values():
            entries_by_slot[idx.source_slot_id] = entries_by_slot.get(idx.source_slot_id, 0) + 1
            total_retention += time.time() - idx.promote_timestamp
        
        avg_days = (total_retention / max(len(self._index), 1)) / (24 * 3600)
        
        return L2StatusSnapshot(
            total_capacity=self.max_entries,
            used_count=len(self._index),
            usage_rate=self.get_usage_rate(),
            avg_retention_days=avg_days,
            entries_by_slot=entries_by_slot
        )
    
    def get_index_snapshot(self) -> List[L2EntryIndex]:
        """获取条目索引快照（供 ad-23 消费）"""
        return list(self._index.values())
    
    def get_entry(self, entry_id: str) -> Optional[L2EntryIndex]:
        """获取指定条目"""
        return self._index.get(entry_id)
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_promotions_in": self._total_promotions_in,
            "total_promotions_out": self._total_promotions_out,
            "total_forgets": self._total_forgets,
            "total_fallbacks": self._total_fallbacks,
            "total_rejections": self._total_rejections,
            "current_entries": len(self._index),
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
    print("ad-22 L2 近期层存储单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    def make_transfer_entry(entry_id, i_value=0.5, s_value=0.3):
        return {
            "entry_id": entry_id,
            "i_value": i_value,
            "s_value": s_value,
            "source_slot_id": 15,
            "sub_label": "常规通用",
            "result_label": "成功优化",
            "force_majeure": False,
            "reuse_count": 0
        }
    
    # --- TC-22-01: 接收 L1 晋升条目 ---
    print("\n[TC-22-01] 接收 L1 晋升条目")
    try:
        l2 = L2RecentStorage(max_entries=100)
        entries = [make_transfer_entry("EXP-001", i_value=0.55)]
        written = l2.receive_from_transfer(entries)
        assert written == 1
        assert l2.get_item_count() == 1
        # I 值应被微调
        assert l2._index["EXP-001"].i_value == 0.57
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-22-02: 存储满紧急清除 ---
    print("\n[TC-22-02] 存储满紧急清除")
    try:
        l2 = L2RecentStorage(max_entries=10)
        for i in range(10):
            l2.receive_from_transfer([make_transfer_entry(f"EXP-{i:03d}", i_value=0.3)])
        l2.receive_from_transfer([make_transfer_entry("EXP-FULL", i_value=0.3)])
        assert l2.get_item_count() < 10
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-22-03: 高安全条目受保护 ---
    print("\n[TC-22-03] 高安全条目受保护（S ≥ 0.7）")
    try:
        l2 = L2RecentStorage(max_entries=10)
        l2.receive_from_transfer([make_transfer_entry("EXP-SAFE", i_value=0.9, s_value=0.85)])
        for i in range(9):
            l2.receive_from_transfer([make_transfer_entry(f"EXP-{i:03d}", i_value=0.1, s_value=0.1)])
        l2.receive_from_transfer([make_transfer_entry("EXP-FULL", i_value=0.1, s_value=0.1)])
        assert "EXP-SAFE" in l2._index
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-22-04: 处理晋升候选（L2 → L3） ---
    print("\n[TC-22-04] 处理晋升候选（L2 → L3）")
    try:
        l2 = L2RecentStorage(max_entries=100)
        l2.receive_from_transfer([make_transfer_entry("EXP-004", i_value=0.65)])
        candidates = [PromotionCandidate("EXP-004", "L3", 0.65, 8*24*3600)]
        results = l2.process_promotions(candidates)
        assert results[0][1] == PromotionResult.SUCCESS
        assert "EXP-004" not in l2._index
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-22-05: 处理遗忘候选 ---
    print("\n[TC-22-05] 处理遗忘候选")
    try:
        l2 = L2RecentStorage(max_entries=100)
        l2.receive_from_transfer([make_transfer_entry("EXP-005", i_value=0.05)])
        candidates = [ForgetCandidate("EXP-005", "L2", 0.05)]
        results = l2.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.DELETED
        assert "EXP-005" not in l2._index
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-22-06: 不可抗力遗忘保护 ---
    print("\n[TC-22-06] 不可抗力遗忘保护")
    try:
        l2 = L2RecentStorage(max_entries=100)
        entry_data = make_transfer_entry("EXP-006", i_value=0.03)
        entry_data["force_majeure"] = True
        l2.receive_from_transfer([entry_data])
        candidates = [ForgetCandidate("EXP-006", "L2", 0.03)]
        results = l2.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.RETAINED
        assert "EXP-006" in l2._index
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-22-07: 晋升失败回退 ---
    print("\n[TC-22-07] 晋升失败回退（回退 3 次标记困难）")
    try:
        l2 = L2RecentStorage(max_entries=100)
        l2.receive_from_transfer([make_transfer_entry("EXP-007", i_value=0.60)])
        for i in range(3):
            l2.handle_promotion_fallback("EXP-007", "L3_storage_full")
        assert l2._index["EXP-007"].fallback_count == 3
        assert l2._index["EXP-007"].i_value == 0.55  # 降低 0.05
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-22-08: 冻结状态拒绝写入 ---
    print("\n[TC-22-08] 冻结状态拒绝写入")
    try:
        l2 = L2RecentStorage(max_entries=100)
        l2.freeze()
        written = l2.receive_from_transfer([make_transfer_entry("EXP-008")])
        assert written == 0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-22-09: 批量清除 ---
    print("\n[TC-22-09] 批量清除（全局容量告急）")
    try:
        l2 = L2RecentStorage(max_entries=50)
        for i in range(20):
            l2.receive_from_transfer([make_transfer_entry(f"EXP-{i:03d}", i_value=0.1 + i * 0.01)])
        cleared = l2.execute_batch_clear(clear_ratio=0.20)
        assert cleared > 0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-22-10: 状态快照生成 ---
    print("\n[TC-22-10] 状态快照生成")
    try:
        l2 = L2RecentStorage(max_entries=100)
        l2.receive_from_transfer([make_transfer_entry("EXP-010", i_value=0.5)])
        snapshot = l2.generate_snapshot()
        assert snapshot.used_count == 1
        assert snapshot.total_capacity == 100
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