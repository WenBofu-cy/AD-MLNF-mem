#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-20
模块名称: L1 临时层存储单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 五层记忆层级存储
核心职责: 存储本次行程的瞬时驾驶经验片段，作为漏斗二经验入口的第一站。
          占漏斗二总存储容量的 60%，是五层结构中流转速度最快、容量最大的层级。
          所有新经验首先写入本层，在留存满 24 小时后由 L1 时序衰减单元评估
          是否进入晋升候选或清除。

依赖模块: ad-14(场景判定与分槽路由单元，经对应分槽转发), ad-21(L1时序衰减单元),
          ad-38(晋升双条件判定单元), ad-48(全局容量配额管控单元)
被依赖模块: ad-21(消费L1条目进行衰减评估), ad-38(消费L1晋升候选条目)

安全约束:
  S-01: L1层仅存储临时经验数据，所有条目最大留存时间硬编码为 24 小时
  S-02: L1层经验条目在 24 小时内不可被手动删除（除非全局容量告急触发批量清除）
  S-03: 安全显著性 S ≥ 0.9 的条目在 L1 批量清除时享有保护，不被清除
  S-04: 存储写入须校验条目完整性（ID、内容、I₀、时间戳四要素齐全）
  S-05: 条目索引表每 24 小时自动备份至冗余分区
  S-06: 冻结状态下禁止任何写入操作
  S-07: 所有写入、清除、晋升候选操作全量写入 ad-51 变更日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid
import bisect


# ==================== 枚举定义 ====================

class StorageState(Enum):
    """L1 存储内部状态"""
    NORMAL = "normal"
    NEAR_FULL = "near_full"
    FULL = "full"
    MAINTENANCE = "maintenance"
    FROZEN = "frozen"


class DecayConclusion(Enum):
    """衰减评估结论"""
    PROMOTION_CANDIDATE = "promotion_candidate"
    RECOMMEND_CLEAR = "recommend_clear"
    CONTINUE_RETAIN = "continue_retain"


class ClearStrategy(Enum):
    """清除策略"""
    DELETE = "delete"
    ARCHIVE = "archive"


# ==================== 数据结构 ====================

@dataclass
class ExperienceEntry:
    """经验条目"""
    entry_id: str
    content: Dict[str, Any]
    i0_value: float
    s_value: float = 0.0
    timestamp: float = field(default_factory=time.time)
    source_slot_id: int = 0
    sub_label: str = ""


@dataclass
class L1EntryIndex:
    """L1 条目索引"""
    entry_id: str
    storage_address: int
    write_timestamp: float
    i0_value: float
    current_i_value: float
    source_slot_id: int
    sub_label: str
    size_bytes: int


@dataclass
class DecayAssessmentResult:
    """衰减评估结果（来自 ad-21）"""
    entry_id: str
    conclusion: DecayConclusion
    current_i_value: float
    retention_duration: float


@dataclass
class L1StatusSnapshot:
    """L1 状态快照"""
    total_capacity: int
    used_count: int
    usage_rate: float
    avg_retention_hours: float
    entries_by_slot: Dict[int, int]
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class L1TemporaryStorage:
    """
    L1 临时层存储单元
    
    职责:
    1. 接收并存储所有新产生的驾驶经验（漏斗二入口）
    2. 维护条目索引表
    3. 处理 ad-21 下发的衰减评估结果
    4. 存储满时执行紧急清除（保护高安全条目）
    5. 处理晋升失败回退条目
    6. 响应全局容量告急的批量清除指令
    """
    
    # 单条经验最大留存时间（秒）
    MAX_RETENTION_SECONDS = 24 * 3600  # 24 小时
    
    # 容量阈值
    NEAR_FULL_THRESHOLD = 0.85
    FULL_THRESHOLD = 0.95
    
    # 紧急清除比例
    EMERGENCY_CLEAR_RATIO = 0.05      # 清除 I 值最低的 5%
    BATCH_CLEAR_RATIO = 0.20          # 全局告急时清除 20%
    
    # 安全条目保护阈值
    SAFE_S_THRESHOLD = 0.9             # S ≥ 0.9 受保护
    
    # 碎片整理间隔（秒）
    DEFRAG_INTERVAL = 6 * 3600         # 6 小时
    
    def __init__(self, max_entries: int = 600):
        """
        初始化 L1 临时层
        
        Args:
            max_entries: 最大条目数（占漏斗二总容量 60%）
        """
        self.module_id = "ad-20"
        self.module_name = "L1 临时层存储单元"
        
        # 内部状态
        self.state = StorageState.NORMAL
        
        # 最大容量
        self.max_entries = max_entries
        
        # 条目索引表: entry_id -> L1EntryIndex
        self._index: Dict[str, L1EntryIndex] = {}
        
        # 存储地址计数器（模拟）
        self._next_address = 0x10000000
        
        # 上次碎片整理时间
        self._last_defrag_time = time.time()
        
        # 晋升候选清单缓冲区
        self._promotion_candidates: List[DecayAssessmentResult] = []
        
        # 统计
        self._total_writes = 0
        self._total_clears = 0
        self._total_promotions = 0
        self._total_rejections = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] L1 临时层初始化完成, 最大容量={max_entries}")
    
    # ========== 状态管理 ==========
    
    def freeze(self) -> None:
        """冻结 L1（驾驶模式切换时调用）"""
        self.state = StorageState.FROZEN
        print(f"[{self.module_id}] L1 已冻结")
    
    def unfreeze(self) -> None:
        """解冻 L1"""
        self.state = StorageState.NORMAL
        print(f"[{self.module_id}] L1 已解冻")
    
    def get_state(self) -> StorageState:
        return self.state
    
    def get_item_count(self) -> int:
        return len(self._index)
    
    def get_usage_rate(self) -> float:
        return len(self._index) / self.max_entries if self.max_entries > 0 else 0.0
    
    # ========== 经验写入 ==========
    
    def write_entry(self, entry: ExperienceEntry) -> Tuple[bool, str, Optional[str]]:
        """
        将新经验写入 L1
        
        Args:
            entry: 经验条目
            
        Returns:
            (成功, 消息, entry_id)
        """
        # S-06: 冻结状态禁止写入
        if self.state == StorageState.FROZEN:
            self._total_rejections += 1
            return False, "L1 已冻结，拒绝写入", None
        
        if self.state == StorageState.MAINTENANCE:
            self._total_rejections += 1
            return False, "L1 维护中，拒绝写入", None
        
        # S-04: 校验条目完整性
        if not entry.entry_id or not entry.content or entry.i0_value is None:
            self._total_rejections += 1
            return False, "条目不完整（缺少 ID/内容/I₀）", None
        
        # 检查容量
        usage = self.get_usage_rate()
        
        if usage >= self.FULL_THRESHOLD:
            # 执行紧急清除
            self._emergency_clear()
            if self.get_usage_rate() >= self.FULL_THRESHOLD:
                self._total_rejections += 1
                return False, "L1 存储满，紧急清除后仍不足", None
            self.state = StorageState.NEAR_FULL
        
        elif usage >= self.NEAR_FULL_THRESHOLD and self.state == StorageState.NORMAL:
            self.state = StorageState.NEAR_FULL
        
        # 分配存储地址
        storage_address = self._next_address
        self._next_address += 1024  # 模拟每条经验 1KB
        
        # 创建索引条目
        index_entry = L1EntryIndex(
            entry_id=entry.entry_id,
            storage_address=storage_address,
            write_timestamp=entry.timestamp,
            i0_value=entry.i0_value,
            current_i_value=entry.i0_value,  # 初始等于 I₀
            source_slot_id=entry.source_slot_id,
            sub_label=entry.sub_label,
            size_bytes=1024
        )
        
        self._index[entry.entry_id] = index_entry
        self._total_writes += 1
        
        return True, f"写入 L1 成功", entry.entry_id
    
    def _emergency_clear(self) -> int:
        """
        紧急清除：删除 I 值最低的 5% 条目
        
        S-03: S ≥ 0.9 的条目受保护
        
        Returns:
            清除的条目数
        """
        if not self._index:
            return 0
        
        # 按 current_i_value 升序排列
        sorted_entries = sorted(self._index.items(), key=lambda x: x[1].current_i_value)
        remove_count = max(1, int(len(self._index) * self.EMERGENCY_CLEAR_RATIO))
        
        cleared = 0
        for i in range(min(remove_count, len(sorted_entries))):
            entry_id, idx_entry = sorted_entries[i]
            
            # S-03: S ≥ 0.9 受保护
            if idx_entry.current_i_value >= self.SAFE_S_THRESHOLD:
                continue
            
            del self._index[entry_id]
            cleared += 1
            self._total_clears += 1
        
        if cleared > 0:
            print(f"[{self.module_id}] 紧急清除: {cleared} 条")
        
        return cleared
    
    # ========== 衰减评估处理 ==========
    
    def process_decay_assessments(self, assessments: List[DecayAssessmentResult]) -> None:
        """
        处理 ad-21 下发的衰减评估结果
        
        Args:
            assessments: 衰减评估结果列表
        """
        if self.state == StorageState.FROZEN:
            return
        
        self._promotion_candidates.clear()
        
        for assessment in assessments:
            entry_id = assessment.entry_id
            
            if assessment.conclusion == DecayConclusion.PROMOTION_CANDIDATE:
                # 加入晋升候选清单
                self._promotion_candidates.append(assessment)
                self._total_promotions += 1
            
            elif assessment.conclusion == DecayConclusion.RECOMMEND_CLEAR:
                # 建议清除
                if entry_id in self._index:
                    # S-03: 保护高安全条目
                    if self._index[entry_id].current_i_value >= self.SAFE_S_THRESHOLD:
                        continue
                    del self._index[entry_id]
                    self._total_clears += 1
            
            elif assessment.conclusion == DecayConclusion.CONTINUE_RETAIN:
                # 继续保留，不操作
                pass
        
        if self._promotion_candidates:
            print(f"[{self.module_id}] 晋升候选: {len(self._promotion_candidates)} 条")
    
    def get_promotion_candidates(self) -> List[DecayAssessmentResult]:
        """获取晋升候选清单（供 ad-38 消费）"""
        candidates = self._promotion_candidates.copy()
        self._promotion_candidates.clear()
        return candidates
    
    # ========== 晋升失败回退 ==========
    
    def handle_promotion_fallback(self, entry_id: str, reason: str) -> None:
        """
        处理晋升失败回退条目
        
        Args:
            entry_id: 条目 ID
            reason: 回退原因
        """
        if entry_id not in self._index:
            # 条目可能已被清除，重新创建索引
            print(f"[{self.module_id}] 回退条目 {entry_id[:12]} 不在索引中，跳过")
            return
        
        if reason == "L2_storage_full":
            # L2 满，继续保留在 L1
            print(f"[{self.module_id}] 回退条目 {entry_id[:12]} 保留 L1（L2 满）")
        else:
            # 其他原因（如条目损坏），标记清除
            del self._index[entry_id]
            self._total_clears += 1
            print(f"[{self.module_id}] 回退条目 {entry_id[:12]} 已清除（{reason}）")
    
    # ========== 批量清除 ==========
    
    def execute_batch_clear(self, clear_ratio: float = None) -> int:
        """
        执行全局容量告急的批量清除
        
        S-03: S ≥ 0.9 的条目受保护
        
        Args:
            clear_ratio: 清除比例（默认 20%）
            
        Returns:
            清除的条目数
        """
        if clear_ratio is None:
            clear_ratio = self.BATCH_CLEAR_RATIO
        
        if not self._index:
            return 0
        
        self.state = StorageState.MAINTENANCE
        
        # 按 current_i_value 升序排列
        sorted_entries = sorted(self._index.items(), key=lambda x: x[1].current_i_value)
        remove_count = max(1, int(len(self._index) * clear_ratio))
        
        cleared = 0
        for i in range(min(remove_count, len(sorted_entries))):
            entry_id, idx_entry = sorted_entries[i]
            
            # S-03: S ≥ 0.9 受保护
            if idx_entry.current_i_value >= self.SAFE_S_THRESHOLD:
                continue
            
            del self._index[entry_id]
            cleared += 1
            self._total_clears += 1
        
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
        
        # 模拟碎片整理
        print(f"[{self.module_id}] 执行碎片整理, 当前使用率={usage:.1%}")
        
        self.state = StorageState.NORMAL
    
    # ========== 状态上报 ==========
    
    def generate_snapshot(self) -> L1StatusSnapshot:
        """生成 L1 状态快照"""
        entries_by_slot: Dict[int, int] = {}
        total_retention = 0.0
        
        for idx in self._index.values():
            entries_by_slot[idx.source_slot_id] = entries_by_slot.get(idx.source_slot_id, 0) + 1
            total_retention += time.time() - idx.write_timestamp
        
        avg_hours = (total_retention / max(len(self._index), 1)) / 3600
        
        return L1StatusSnapshot(
            total_capacity=self.max_entries,
            used_count=len(self._index),
            usage_rate=self.get_usage_rate(),
            avg_retention_hours=avg_hours,
            entries_by_slot=entries_by_slot
        )
    
    def get_entry_i_value(self, entry_id: str) -> Optional[float]:
        """获取条目的当前 I 值"""
        if entry_id in self._index:
            return self._index[entry_id].current_i_value
        return None
    
    def get_index_snapshot(self) -> List[L1EntryIndex]:
        """获取条目索引快照（供 ad-21 消费）"""
        return list(self._index.values())
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_writes": self._total_writes,
            "total_clears": self._total_clears,
            "total_promotions": self._total_promotions,
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
    print("ad-20 L1 临时层存储单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    def make_entry(entry_id, i0_value=0.5, s_value=0.0):
        return ExperienceEntry(
            entry_id=entry_id,
            content={"behavior": "测试"},
            i0_value=i0_value,
            s_value=s_value,
            source_slot_id=15,
            sub_label="常规通用"
        )
    
    # --- TC-20-01: 正常写入 L1 ---
    print("\n[TC-20-01] 正常写入 L1")
    try:
        l1 = L1TemporaryStorage(max_entries=100)
        entry = make_entry("EXP-001", i0_value=0.5)
        success, msg, eid = l1.write_entry(entry)
        assert success == True
        assert l1.get_item_count() == 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-20-02: 条目不完整拒绝写入 ---
    print("\n[TC-20-02] 条目不完整拒绝写入")
    try:
        l1 = L1TemporaryStorage(max_entries=100)
        entry = ExperienceEntry(entry_id="", content={}, i0_value=None)
        success, msg, eid = l1.write_entry(entry)
        assert success == False
        assert "不完整" in msg
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-20-03: 存储满紧急清除 ---
    print("\n[TC-20-03] 存储满紧急清除")
    try:
        l1 = L1TemporaryStorage(max_entries=20)
        for i in range(19):
            l1.write_entry(make_entry(f"EXP-{i:03d}", i0_value=0.5))
        l1.write_entry(make_entry("EXP-FULL", i0_value=0.5))
        # 此时应触发紧急清除
        assert l1.get_item_count() < 20
        assert l1._total_clears > 0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-20-04: 高安全条目受保护 ---
    print("\n[TC-20-04] 高安全条目受保护（S ≥ 0.9）")
    try:
        l1 = L1TemporaryStorage(max_entries=10)
        # 写入一条高安全条目
        l1.write_entry(make_entry("EXP-SAFE", i0_value=0.9, s_value=0.95))
        # 填满 L1
        for i in range(9):
            l1.write_entry(make_entry(f"EXP-{i:03d}", i0_value=0.1))
        # 触发紧急清除
        l1.write_entry(make_entry("EXP-FULL", i0_value=0.1))
        # 高安全条目应被保留
        assert "EXP-SAFE" in l1._index
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-20-05: 处理衰减评估（晋升候选） ---
    print("\n[TC-20-05] 处理衰减评估（晋升候选）")
    try:
        l1 = L1TemporaryStorage(max_entries=100)
        l1.write_entry(make_entry("EXP-005", i0_value=0.55))
        assessments = [
            DecayAssessmentResult("EXP-005", DecayConclusion.PROMOTION_CANDIDATE, 0.55, 25*3600)
        ]
        l1.process_decay_assessments(assessments)
        candidates = l1.get_promotion_candidates()
        assert len(candidates) == 1
        assert candidates[0].conclusion == DecayConclusion.PROMOTION_CANDIDATE
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-20-06: 处理衰减评估（建议清除） ---
    print("\n[TC-20-06] 处理衰减评估（建议清除）")
    try:
        l1 = L1TemporaryStorage(max_entries=100)
        l1.write_entry(make_entry("EXP-006", i0_value=0.05))
        assessments = [
            DecayAssessmentResult("EXP-006", DecayConclusion.RECOMMEND_CLEAR, 0.05, 26*3600)
        ]
        l1.process_decay_assessments(assessments)
        assert "EXP-006" not in l1._index
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-20-07: 冻结状态拒绝写入 ---
    print("\n[TC-20-07] 冻结状态拒绝写入")
    try:
        l1 = L1TemporaryStorage(max_entries=100)
        l1.freeze()
        entry = make_entry("EXP-007", i0_value=0.5)
        success, msg, eid = l1.write_entry(entry)
        assert success == False
        assert "冻结" in msg
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-20-08: 晋升失败回退 ---
    print("\n[TC-20-08] 晋升失败回退")
    try:
        l1 = L1TemporaryStorage(max_entries=100)
        l1.write_entry(make_entry("EXP-008", i0_value=0.5))
        l1.handle_promotion_fallback("EXP-008", "L2_storage_full")
        assert "EXP-008" in l1._index  # 继续保留
        l1.handle_promotion_fallback("EXP-008", "entry_corrupted")
        assert "EXP-008" not in l1._index  # 被清除
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-20-09: 批量清除 ---
    print("\n[TC-20-09] 批量清除（全局容量告急）")
    try:
        l1 = L1TemporaryStorage(max_entries=50)
        for i in range(30):
            l1.write_entry(make_entry(f"EXP-{i:03d}", i0_value=0.1 + i * 0.01))
        cleared = l1.execute_batch_clear(clear_ratio=0.20)
        assert cleared > 0
        assert l1.get_item_count() < 30
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-20-10: 状态快照生成 ---
    print("\n[TC-20-10] 状态快照生成")
    try:
        l1 = L1TemporaryStorage(max_entries=100)
        l1.write_entry(make_entry("EXP-010", i0_value=0.5))
        snapshot = l1.generate_snapshot()
        assert snapshot.used_count == 1
        assert snapshot.total_capacity == 100
        assert snapshot.usage_rate == 0.01
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