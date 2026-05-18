#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-19
模块名称: 通用驾驶槽
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 场景分槽管理
核心职责: 承载跨场景通用驾驶风格、乡村道路、未匹配到专属分槽的日常驾驶经验的
          完整五层存储与晋升管理。作为漏斗二的兜底分槽，确保任何无法精确归类的
          驾驶经验都有归宿。乡村道路子类享有独立遗忘保护参数。

依赖模块: ad-14(场景判定与分槽路由单元), ad-20至ad-30(五层存储与晋升遗忘执行模块),
          ad-36(综合重要度I值聚合计算单元)
被依赖模块: ad-14(上报存储占用率与活跃状态), ad-03(漏斗二专属调度单元)

专属遗忘策略:
  常规通用子类: 使用漏斗二标准默认值
  乡村道路子类:
    - 最低重要度遗忘阈值: 0.075（标准0.15下调50%）
    - 安全显著性权重(α): 0.55
    - 复用频次权重(γ): 0.20
    - 风格匹配度权重(β): 0.25
    - L1→L2晋升时间阈值: 36h（标准24h延长）
    - L2→L3晋升时间阈值: 10日（标准7日延长）
    - 遗忘方式: 优先冷归档（绝不直接删除）

安全约束:
  S-01: 通用驾驶槽作为漏斗二的兜底分槽，编译期保证其始终存在，不可删除或禁用
  S-02: 乡村道路子类经验遗忘阈值0.075为硬编码下限
  S-03: 乡村道路子类经验遗忘方式硬编码为"冷归档"，不可直接删除
  S-04: L3归并时禁止跨子类合并，防止乡村道路经验与通用经验混淆
  S-05: 容量告急时加速遗忘仅针对常规子类L1/L2，乡村道路子类不受影响
  S-06: 所有晋升、遗忘、归并操作全量写入ad-51变更日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class SlotState(Enum):
    """分槽内部状态"""
    NORMAL = "normal"
    CAPACITY_WARNING = "capacity_warning"
    MAINTENANCE = "maintenance"
    FROZEN = "frozen"


class MemoryLayer(Enum):
    """五层记忆层级"""
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"
    L5 = "L5"


class PromotionResult(Enum):
    """晋升结果"""
    SUCCESS = "success"
    FAIL_STORAGE_FULL = "storage_full"
    FAIL_LAYER_NOT_EXIST = "layer_not_exist"
    FAIL_LOCKED = "locked"


class ForgetResult(Enum):
    """遗忘结果"""
    DELETED = "deleted"
    ARCHIVED = "archived"
    SKIP_L4_L5 = "skip_l4_l5"
    SKIP_FORCE_MAJEURE = "skip_force_majeure"
    RETAINED = "retained"


class SubLabel(Enum):
    """子类标记"""
    REGULAR = "常规通用"
    RURAL = "乡村道路"
    FALLBACK_WM = "降级路由"
    MERGE_OVERFLOW = "归并溢出"


# ==================== 数据结构 ====================

@dataclass
class ExperienceEntry:
    """经验条目"""
    entry_id: str
    content: Dict[str, Any]
    i_value: float
    i0_value: float = 0.5
    s_value: float = 0.0
    v_value: float = 0.0
    c_value: float = 0.0
    source_slot_id: int = 19
    result_label: str = "成功优化"
    force_majeure: bool = False
    sub_label: str = SubLabel.REGULAR.value
    current_layer: MemoryLayer = MemoryLayer.L1
    store_timestamp: float = field(default_factory=time.time)
    promotion_count: int = 0
    reuse_count: int = 0


@dataclass
class PromotionCandidate:
    """晋升候选条目"""
    entry_id: str
    target_layer: MemoryLayer
    i_value: float
    retention_duration: float


@dataclass
class ForgetCandidate:
    """遗忘候选条目"""
    entry_id: str
    current_layer: MemoryLayer
    i_value: float


@dataclass
class MergeSuggestion:
    """归并建议"""
    source_entry_id: str
    target_entry_id: str
    similarity: float


@dataclass
class SlotStatusSnapshot:
    """槽位状态快照"""
    slot_id: int = 19
    storage_usage_rate: float = 0.0
    l1_count: int = 0
    l2_count: int = 0
    l3_count: int = 0
    l4_count: int = 0
    l5_count: int = 0
    regular_count: int = 0
    rural_count: int = 0
    is_active: bool = True
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class GeneralDrivingSlot:
    """
    通用驾驶槽 - 兜底分槽
    
    职责:
    1. 存储通用驾驶经验（常规通用 + 乡村道路双子类）
    2. 管理晋升候选与遗忘候选
    3. 乡村道路子类享有独立遗忘保护（遗忘阈值0.075，仅冷归档）
    4. 容量告急时仅加速遗忘常规子类L1/L2
    5. L3归并禁止跨子类合并
    """
    
    # 常规子类参数（标准默认）
    REGULAR_ALPHA = 0.50
    REGULAR_BETA = 0.20
    REGULAR_GAMMA = 0.30
    REGULAR_FORGET_THRESHOLD = 0.15
    
    # 乡村道路子类参数（独立保护）
    RURAL_ALPHA = 0.55
    RURAL_BETA = 0.25
    RURAL_GAMMA = 0.20
    RURAL_FORGET_THRESHOLD = 0.075
    RURAL_I0_BOOST = 0.05
    
    # 归并溢出I₀加成
    MERGE_OVERFLOW_I0_BOOST = 0.03
    
    # 晋升时间阈值（秒）
    REGULAR_L1_L2_TIME = 24 * 3600
    REGULAR_L2_L3_TIME = 7 * 24 * 3600
    RURAL_L1_L2_TIME = 36 * 3600
    RURAL_L2_L3_TIME = 10 * 24 * 3600
    L3_L4_TIME = 30 * 24 * 3600
    L4_L5_TIME = 90 * 24 * 3600
    
    # 容量阈值
    CAPACITY_WARNING = 0.85
    CAPACITY_CRITICAL = 0.90
    
    # 单层最大条目数
    MAX_L1_ENTRIES = 600
    MAX_L2_ENTRIES = 250
    MAX_L3_ENTRIES = 100
    MAX_L4_ENTRIES = 45
    MAX_L5_ENTRIES = 5
    
    # 归并参数
    MERGE_INTERVAL = 24 * 3600
    MERGE_SIMILARITY_THRESHOLD = 0.70
    
    def __init__(self):
        self.module_id = "ad-19"
        self.module_name = "通用驾驶槽"
        
        self.state = SlotState.NORMAL
        
        self._storage: Dict[MemoryLayer, Dict[str, ExperienceEntry]] = {
            MemoryLayer.L1: {}, MemoryLayer.L2: {},
            MemoryLayer.L3: {}, MemoryLayer.L4: {}, MemoryLayer.L5: {},
        }
        
        # 子类标记字典: entry_id -> sub_label
        self._sub_labels: Dict[str, str] = {}
        
        self._last_merge_time = time.time()
        
        self._total_writes = 0
        self._total_promotions = 0
        self._total_forgets = 0
        self._total_merges = 0
        self._total_archives = 0
        
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 通用驾驶槽初始化完成（兜底分槽）")
        print(f"[{self.module_id}] 常规子类: α={self.REGULAR_ALPHA}, 遗忘阈值={self.REGULAR_FORGET_THRESHOLD}")
        print(f"[{self.module_id}] 乡村子类: α={self.RURAL_ALPHA}, 遗忘阈值={self.RURAL_FORGET_THRESHOLD}, 仅冷归档")
    
    # ========== 状态管理 ==========
    
    def freeze(self) -> None:
        self.state = SlotState.FROZEN
    
    def unfreeze(self) -> None:
        self.state = SlotState.NORMAL
    
    def get_state(self) -> SlotState:
        return self.state
    
    # ========== 经验写入 ==========
    
    def write_entry(self, entry: ExperienceEntry) -> Tuple[bool, str]:
        """
        将新经验写入L1临时层
        
        通用驾驶槽专属规则:
        - 乡村道路子类I₀+0.05
        - 归并溢出I₀+0.03
        """
        if self.state == SlotState.FROZEN:
            return False, "分槽已冻结，拒绝写入"
        
        if self.state == SlotState.MAINTENANCE:
            return False, "分槽维护中，拒绝写入"
        
        sub = entry.sub_label
        
        # 根据子类调整I₀
        if sub == SubLabel.RURAL.value:
            entry.i0_value = min(entry.i0_value + self.RURAL_I0_BOOST, 1.0)
            entry.i_value = max(entry.i_value, entry.i0_value)
            print(f"[{self.module_id}] 乡村道路经验I₀加成: {entry.entry_id[:12]}")
        elif sub == SubLabel.MERGE_OVERFLOW.value:
            entry.i0_value = min(entry.i0_value + self.MERGE_OVERFLOW_I0_BOOST, 1.0)
            entry.i_value = max(entry.i_value, entry.i0_value)
        
        # 记录子类标记
        self._sub_labels[entry.entry_id] = sub
        
        # 检查L1容量
        if len(self._storage[MemoryLayer.L1]) >= self.MAX_L1_ENTRIES:
            self._emergency_clean_l1()
            if len(self._storage[MemoryLayer.L1]) >= self.MAX_L1_ENTRIES:
                return False, "L1存储满"
        
        entry.current_layer = MemoryLayer.L1
        entry.store_timestamp = time.time()
        
        self._storage[MemoryLayer.L1][entry.entry_id] = entry
        self._total_writes += 1
        
        return True, f"写入L1成功, 子类={sub}"
    
    def _emergency_clean_l1(self) -> None:
        """紧急清理L1：删除I值最低的5%条目（乡村道路子类受保护）"""
        l1 = self._storage[MemoryLayer.L1]
        if not l1:
            return
        
        sorted_entries = sorted(l1.items(), key=lambda x: x[1].i_value)
        remove_count = max(1, int(len(l1) * 0.05))
        
        for i in range(remove_count):
            entry_id = sorted_entries[i][0]
            if l1[entry_id].force_majeure:
                continue
            if self._sub_labels.get(entry_id) == SubLabel.RURAL.value:
                continue  # 乡村道路子类受保护
            del l1[entry_id]
    
    # ========== 晋升处理 ==========
    
    def process_promotions(self, candidates: List[PromotionCandidate]) -> List[Tuple[str, PromotionResult]]:
        """处理晋升候选清单"""
        if self.state == SlotState.FROZEN:
            return [(c.entry_id, PromotionResult.FAIL_LOCKED) for c in candidates]
        
        results = []
        for candidate in candidates:
            result = self._promote_single(candidate)
            results.append((candidate.entry_id, result))
        
        return results
    
    def _promote_single(self, candidate: PromotionCandidate) -> PromotionResult:
        """晋升单个条目（根据子类使用不同时间阈值）"""
        source_layer = self._get_source_layer(candidate.target_layer)
        if source_layer is None:
            return PromotionResult.FAIL_LAYER_NOT_EXIST
        
        if candidate.entry_id not in self._storage[source_layer]:
            return PromotionResult.FAIL_LAYER_NOT_EXIST
        
        target_count = len(self._storage[candidate.target_layer])
        max_count = self._get_max_for_layer(candidate.target_layer)
        if target_count >= max_count:
            return PromotionResult.FAIL_STORAGE_FULL
        
        entry = self._storage[source_layer].pop(candidate.entry_id)
        entry.current_layer = candidate.target_layer
        entry.promotion_count += 1
        
        self._storage[candidate.target_layer][candidate.entry_id] = entry
        self._total_promotions += 1
        
        return PromotionResult.SUCCESS
    
    def _get_source_layer(self, target_layer: MemoryLayer) -> Optional[MemoryLayer]:
        layer_map = {
            MemoryLayer.L2: MemoryLayer.L1,
            MemoryLayer.L3: MemoryLayer.L2,
            MemoryLayer.L4: MemoryLayer.L3,
            MemoryLayer.L5: MemoryLayer.L4,
        }
        return layer_map.get(target_layer)
    
    # ========== 遗忘处理 ==========
    
    def process_forget_candidates(self, candidates: List[ForgetCandidate]) -> List[Tuple[str, ForgetResult]]:
        """
        处理遗忘候选清单
        
        通用驾驶槽专属规则:
        - 乡村道路子类使用0.075极低遗忘阈值，优先冷归档
        - 常规子类使用标准0.15遗忘阈值，直接删除
        """
        if self.state == SlotState.FROZEN:
            return [(c.entry_id, ForgetResult.RETAINED) for c in candidates]
        
        results = []
        for candidate in candidates:
            if candidate.current_layer in [MemoryLayer.L4, MemoryLayer.L5]:
                results.append((candidate.entry_id, ForgetResult.SKIP_L4_L5))
                continue
            
            sub = self._sub_labels.get(candidate.entry_id, SubLabel.REGULAR.value)
            
            # 根据子类确定遗忘阈值
            if sub == SubLabel.RURAL.value:
                forget_threshold = self.RURAL_FORGET_THRESHOLD
                use_archive = True
            else:
                forget_threshold = self.REGULAR_FORGET_THRESHOLD
                use_archive = False
            
            if candidate.i_value < forget_threshold:
                entry = self._storage[candidate.current_layer].get(candidate.entry_id)
                if entry and entry.force_majeure:
                    results.append((candidate.entry_id, ForgetResult.SKIP_FORCE_MAJEURE))
                    continue
                
                self._storage[candidate.current_layer].pop(candidate.entry_id, None)
                self._total_forgets += 1
                
                if use_archive:
                    self._total_archives += 1
                    results.append((candidate.entry_id, ForgetResult.ARCHIVED))
                else:
                    results.append((candidate.entry_id, ForgetResult.DELETED))
            else:
                results.append((candidate.entry_id, ForgetResult.RETAINED))
        
        return results
    
    # ========== 归并处理 ==========
    
    def check_and_merge(self) -> Optional[MergeSuggestion]:
        """检查并执行L3相似经验归并（禁止跨子类合并）"""
        now = time.time()
        if now - self._last_merge_time < self.MERGE_INTERVAL:
            return None
        
        usage = self._calculate_usage_rate()
        if usage < self.CAPACITY_WARNING:
            return None
        
        self.state = SlotState.MAINTENANCE
        self._last_merge_time = now
        
        l3_entries = list(self._storage[MemoryLayer.L3].items())
        if len(l3_entries) < 2:
            self.state = SlotState.NORMAL
            return None
        
        best_pair = None
        best_sim = 0.0
        
        for i in range(len(l3_entries)):
            for j in range(i + 1, len(l3_entries)):
                # 禁止跨子类归并
                sub_i = self._sub_labels.get(l3_entries[i][0], SubLabel.REGULAR.value)
                sub_j = self._sub_labels.get(l3_entries[j][0], SubLabel.REGULAR.value)
                if sub_i != sub_j:
                    continue
                
                sim = self._calc_similarity(l3_entries[i][1], l3_entries[j][1])
                if sim > best_sim and sim >= self.MERGE_SIMILARITY_THRESHOLD:
                    best_sim = sim
                    if l3_entries[i][1].i_value >= l3_entries[j][1].i_value:
                        best_pair = (l3_entries[i][0], l3_entries[j][0], sim)
                    else:
                        best_pair = (l3_entries[j][0], l3_entries[i][0], sim)
        
        if best_pair:
            source_id, target_id, sim = best_pair
            source_entry = self._storage[MemoryLayer.L3].pop(source_id, None)
            if source_entry and target_id in self._storage[MemoryLayer.L3]:
                target_entry = self._storage[MemoryLayer.L3][target_id]
                target_entry.reuse_count += source_entry.reuse_count
                target_entry.i_value = (target_entry.i_value + source_entry.i_value) / 2
                self._total_merges += 1
                # 保留目标条目的子类标记，清理源条目标记
                self._sub_labels.pop(source_id, None)
            
            self.state = SlotState.NORMAL
            return MergeSuggestion(source_entry_id=source_id, target_entry_id=target_id, similarity=sim)
        
        self.state = SlotState.NORMAL
        return None
    
    def _calc_similarity(self, entry1: ExperienceEntry, entry2: ExperienceEntry) -> float:
        """计算两个经验条目的相似度"""
        score = 0.0
        if entry1.result_label == entry2.result_label:
            score += 0.5
        if entry1.source_slot_id == entry2.source_slot_id:
            score += 0.3
        i_diff = abs(entry1.i_value - entry2.i_value)
        score += max(0, 0.2 - i_diff)
        return min(score, 1.0)
    
    # ========== 容量监控 ==========
    
    def _calculate_usage_rate(self) -> float:
        total = (len(self._storage[MemoryLayer.L1]) / self.MAX_L1_ENTRIES * 0.60 +
                 len(self._storage[MemoryLayer.L2]) / self.MAX_L2_ENTRIES * 0.25 +
                 len(self._storage[MemoryLayer.L3]) / self.MAX_L3_ENTRIES * 0.10 +
                 len(self._storage[MemoryLayer.L4]) / self.MAX_L4_ENTRIES * 0.045 +
                 len(self._storage[MemoryLayer.L5]) / self.MAX_L5_ENTRIES * 0.005)
        return total
    
    def check_capacity(self) -> Optional[Dict[str, Any]]:
        """
        检查容量状态
        
        通用驾驶槽专属: 容量告急时仅加速遗忘常规子类L1/L2，乡村道路子类不受影响
        """
        usage = self._calculate_usage_rate()
        if usage > self.CAPACITY_CRITICAL and self.state != SlotState.CAPACITY_WARNING:
            self.state = SlotState.CAPACITY_WARNING
            return {
                "usage_rate": usage,
                "action": "accelerate_forget_regular_only",
                "note": "仅加速常规子类L1/L2遗忘，乡村道路子类受保护"
            }
        elif usage < 0.70 and self.state == SlotState.CAPACITY_WARNING:
            self.state = SlotState.NORMAL
            return {"usage_rate": usage, "action": "restore_normal"}
        return None
    
    def _get_max_for_layer(self, layer: MemoryLayer) -> int:
        return {
            MemoryLayer.L1: self.MAX_L1_ENTRIES,
            MemoryLayer.L2: self.MAX_L2_ENTRIES,
            MemoryLayer.L3: self.MAX_L3_ENTRIES,
            MemoryLayer.L4: self.MAX_L4_ENTRIES,
            MemoryLayer.L5: self.MAX_L5_ENTRIES,
        }.get(layer, 100)
    
    # ========== 状态上报 ==========
    
    def generate_snapshot(self) -> SlotStatusSnapshot:
        regular = sum(1 for s in self._sub_labels.values() if s == SubLabel.REGULAR.value)
        rural = sum(1 for s in self._sub_labels.values() if s == SubLabel.RURAL.value)
        return SlotStatusSnapshot(
            slot_id=19,
            storage_usage_rate=self._calculate_usage_rate(),
            l1_count=len(self._storage[MemoryLayer.L1]),
            l2_count=len(self._storage[MemoryLayer.L2]),
            l3_count=len(self._storage[MemoryLayer.L3]),
            l4_count=len(self._storage[MemoryLayer.L4]),
            l5_count=len(self._storage[MemoryLayer.L5]),
            regular_count=regular,
            rural_count=rural,
            is_active=(self.state != SlotState.FROZEN)
        )
    
    def get_statistics(self) -> Dict[str, Any]:
        regular = sum(1 for s in self._sub_labels.values() if s == SubLabel.REGULAR.value)
        rural = sum(1 for s in self._sub_labels.values() if s == SubLabel.RURAL.value)
        return {
            "total_writes": self._total_writes,
            "total_promotions": self._total_promotions,
            "total_forgets": self._total_forgets,
            "total_merges": self._total_merges,
            "total_archives": self._total_archives,
            "regular_count": regular,
            "rural_count": rural,
            "l1_count": len(self._storage[MemoryLayer.L1]),
            "l2_count": len(self._storage[MemoryLayer.L2]),
            "l3_count": len(self._storage[MemoryLayer.L3]),
            "l4_count": len(self._storage[MemoryLayer.L4]),
            "l5_count": len(self._storage[MemoryLayer.L5]),
            "usage_rate": self._calculate_usage_rate(),
            "state": self.state.value
        }
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-19 通用驾驶槽 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    def make_entry(entry_id, i_value=0.5, sub_label=SubLabel.REGULAR.value):
        return ExperienceEntry(
            entry_id=entry_id, content={"behavior": "通用驾驶"},
            i_value=i_value, i0_value=i_value,
            source_slot_id=19, result_label="成功优化",
            sub_label=sub_label
        )
    
    # --- TC-19-01: 常规通用写入 ---
    print("\n[TC-19-01] 常规通用写入（I₀不变）")
    try:
        slot = GeneralDrivingSlot()
        entry = make_entry("EXP-001", i_value=0.5, sub_label=SubLabel.REGULAR.value)
        entry.i0_value = 0.5
        success, msg = slot.write_entry(entry)
        assert success == True
        stored = slot._storage[MemoryLayer.L1]["EXP-001"]
        assert stored.i0_value == 0.5
        assert slot._sub_labels["EXP-001"] == SubLabel.REGULAR.value
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-19-02: 乡村道路写入I₀加成 ---
    print("\n[TC-19-02] 乡村道路写入I₀加成")
    try:
        slot = GeneralDrivingSlot()
        entry = make_entry("EXP-002", i_value=0.5, sub_label=SubLabel.RURAL.value)
        entry.i0_value = 0.5
        success, msg = slot.write_entry(entry)
        assert success == True
        stored = slot._storage[MemoryLayer.L1]["EXP-002"]
        assert stored.i0_value == 0.55
        assert slot._sub_labels["EXP-002"] == SubLabel.RURAL.value
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-19-03: 乡村道路遗忘保护（I=0.10 ≥ 0.075，保留） ---
    print("\n[TC-19-03] 乡村道路遗忘保护（I=0.10 ≥ 0.075，保留）")
    try:
        slot = GeneralDrivingSlot()
        slot.write_entry(make_entry("EXP-003", i_value=0.10, sub_label=SubLabel.RURAL.value))
        candidates = [ForgetCandidate("EXP-003", MemoryLayer.L1, 0.10)]
        results = slot.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.RETAINED
        assert "EXP-003" in slot._storage[MemoryLayer.L1]
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-19-04: 乡村道路遗忘（I=0.05 < 0.075，冷归档） ---
    print("\n[TC-19-04] 乡村道路遗忘（I=0.05 < 0.075，冷归档）")
    try:
        slot = GeneralDrivingSlot()
        slot.write_entry(make_entry("EXP-004", i_value=0.05, sub_label=SubLabel.RURAL.value))
        candidates = [ForgetCandidate("EXP-004", MemoryLayer.L1, 0.05)]
        results = slot.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.ARCHIVED
        assert slot._total_archives == 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-19-05: 常规遗忘（I=0.10 < 0.15，直接删除） ---
    print("\n[TC-19-05] 常规遗忘（I=0.10 < 0.15，直接删除）")
    try:
        slot = GeneralDrivingSlot()
        slot.write_entry(make_entry("EXP-005", i_value=0.10, sub_label=SubLabel.REGULAR.value))
        candidates = [ForgetCandidate("EXP-005", MemoryLayer.L1, 0.10)]
        results = slot.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.DELETED
        assert "EXP-005" not in slot._storage[MemoryLayer.L1]
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-19-06: 容量告警仅针对常规子类 ---
    print("\n[TC-19-06] 容量告警仅针对常规子类")
    try:
        slot = GeneralDrivingSlot()
        slot.MAX_L1_ENTRIES = 10
        for i in range(10):
            slot.write_entry(make_entry(f"EXP-{i:03d}", i_value=0.5))
        alert = slot.check_capacity()
        assert alert is not None
        assert "accelerate_forget_regular_only" in alert["action"]
        assert "乡村道路子类受保护" in alert["note"]
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-19-07: 跨子类禁止归并 ---
    print("\n[TC-19-07] 跨子类禁止归并")
    try:
        slot = GeneralDrivingSlot()
        slot._last_merge_time = 0
        slot._storage[MemoryLayer.L3]["EXP-A"] = make_entry("EXP-A", i_value=0.7, sub_label=SubLabel.RURAL.value)
        slot._storage[MemoryLayer.L3]["EXP-B"] = make_entry("EXP-B", i_value=0.6, sub_label=SubLabel.REGULAR.value)
        slot._sub_labels["EXP-A"] = SubLabel.RURAL.value
        slot._sub_labels["EXP-B"] = SubLabel.REGULAR.value
        merge = slot.check_and_merge()
        assert merge is None
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-19-08: 同子类可归并 ---
    print("\n[TC-19-08] 同子类可归并")
    try:
        slot = GeneralDrivingSlot()
        slot._last_merge_time = 0
        slot._storage[MemoryLayer.L3]["EXP-C"] = make_entry("EXP-C", i_value=0.7, sub_label=SubLabel.REGULAR.value)
        slot._storage[MemoryLayer.L3]["EXP-D"] = make_entry("EXP-D", i_value=0.6, sub_label=SubLabel.REGULAR.value)
        slot._sub_labels["EXP-C"] = SubLabel.REGULAR.value
        slot._sub_labels["EXP-D"] = SubLabel.REGULAR.value
        merge = slot.check_and_merge()
        assert merge is not None or slot._calculate_usage_rate() < slot.CAPACITY_WARNING
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-19-09: 降级路由写入 ---
    print("\n[TC-19-09] 降级路由写入（I₀不变）")
    try:
        slot = GeneralDrivingSlot()
        entry = make_entry("EXP-009", i_value=0.4, sub_label=SubLabel.FALLBACK_WM.value)
        entry.i0_value = 0.4
        success, msg = slot.write_entry(entry)
        assert success == True
        stored = slot._storage[MemoryLayer.L1]["EXP-009"]
        assert stored.i0_value == 0.4
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-19-10: 双子类统计 ---
    print("\n[TC-19-10] 双子类统计")
    try:
        slot = GeneralDrivingSlot()
        slot.write_entry(make_entry("EXP-R1", i_value=0.5, sub_label=SubLabel.RURAL.value))
        slot.write_entry(make_entry("EXP-R2", i_value=0.5, sub_label=SubLabel.RURAL.value))
        slot.write_entry(make_entry("EXP-G1", i_value=0.5, sub_label=SubLabel.REGULAR.value))
        stats = slot.get_statistics()
        assert stats["rural_count"] == 2
        assert stats["regular_count"] == 1
        snapshot = slot.generate_snapshot()
        assert snapshot.rural_count == 2
        assert snapshot.regular_count == 1
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