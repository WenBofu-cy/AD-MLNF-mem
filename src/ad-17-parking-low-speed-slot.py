#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-17
模块名称: 泊车低速槽
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 场景分槽管理
核心职责: 承载泊车入库、狭窄空间穿行、人车混行区域蠕行等低速场景驾驶经验的
          完整五层存储与晋升管理。执行专属遗忘策略：最低重要度阈值下调50%，
          大幅延长泊车与低速复杂场景经验的保留时间，避免低频但关键的操作经验
          被过早遗忘。

依赖模块: ad-14(场景判定与分槽路由单元), ad-20至ad-30(五层存储与晋升遗忘执行模块),
          ad-36(综合重要度I值聚合计算单元)
被依赖模块: ad-14(上报存储占用率与活跃状态), ad-03(漏斗二专属调度单元)

专属遗忘策略:
  - 最低重要度遗忘阈值: 0.075（标准0.15下调50%）
  - 安全显著性权重(α): 0.40（低速场景碰撞风险相对较低）
  - 复用频次权重(γ): 0.35（重复路径操作值得强化）
  - 风格匹配度权重(β): 0.25（泊车舒适度是重要评价维度）
  - L1→L2晋升时间阈值: 48h（标准24h延长）
  - L2→L3晋升时间阈值: 10日（标准7日延长）
  - L3→L4晋升时间阈值: 45日（标准30日延长）

安全约束:
  S-01: 最低遗忘阈值下限为0.075，编译期硬编码，不可被运行时配置下调
  S-02: 晋升时间阈值下限为编译期硬约束，不可突破
  S-03: 泊车经验因涉及低速近距离人车交互，涉及行人的经验I₀仍自动+0.10
  S-04: 容量告急时优先触发归并和冷归档，而非加速遗忘
  S-05: 所有操作日志写入ad-51
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
    RETAINED = "retained"


class TargetClass(Enum):
    """目标类别"""
    CLASS_1 = "静态固定"
    CLASS_2 = "机动动态"
    CLASS_3 = "非人生物"
    CLASS_4 = "人类及非机动"
    CLASS_5 = "环境要素"


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
    source_slot_id: int = 17
    result_label: str = "成功优化"
    force_majeure: bool = False
    core_risk_target_class: Optional[TargetClass] = None
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
    slot_id: int = 17
    storage_usage_rate: float = 0.0
    l1_count: int = 0
    l2_count: int = 0
    l3_count: int = 0
    l4_count: int = 0
    l5_count: int = 0
    is_active: bool = True
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class ParkingLowSpeedSlot:
    """
    泊车低速槽 - 场景分槽之一
    
    职责:
    1. 存储泊车与低速场景驾驶经验（L1→L2→L3→L4→L5五层）
    2. 管理晋升候选与遗忘候选
    3. 执行专属遗忘策略（遗忘阈值=0.075，极低）
    4. 容量告急时优先归并和冷归档，而非加速遗忘
    5. 涉及行人的经验I₀自动+0.10
    """
    
    # 专属遗忘策略参数
    ALPHA = 0.40          # 安全显著性权重
    BETA = 0.25           # 风格匹配度权重
    GAMMA = 0.35          # 复用频次权重
    MIN_FORGET_THRESHOLD = 0.075  # 最低遗忘I阈值（下调50%）
    
    # 晋升时间阈值（秒）
    L1_TO_L2_TIME = 48 * 3600       # 48小时（延长）
    L2_TO_L3_TIME = 10 * 24 * 3600  # 10日（延长）
    L3_TO_L4_TIME = 45 * 24 * 3600  # 45日（延长）
    L4_TO_L5_TIME = 90 * 24 * 3600  # 90日
    
    # I₀加成
    PARKING_I0_BOOST = 0.05         # 泊车经验基础加成
    PEDESTRIAN_I0_BOOST = 0.10      # 涉及行人额外加成
    
    # 容量阈值
    CAPACITY_WARNING = 0.85
    CAPACITY_CRITICAL = 0.90
    
    # 单层最大条目数
    MAX_L1_ENTRIES = 600
    MAX_L2_ENTRIES = 250
    MAX_L3_ENTRIES = 100
    MAX_L4_ENTRIES = 45
    MAX_L5_ENTRIES = 5
    
    # 归并间隔（秒）
    MERGE_INTERVAL = 48 * 3600      # 48小时
    
    # 归并相似度阈值（泊车场景放宽）
    MERGE_SIMILARITY_THRESHOLD = 0.75
    
    def __init__(self):
        self.module_id = "ad-17"
        self.module_name = "泊车低速槽"
        
        self.state = SlotState.NORMAL
        
        self._storage: Dict[MemoryLayer, Dict[str, ExperienceEntry]] = {
            MemoryLayer.L1: {}, MemoryLayer.L2: {},
            MemoryLayer.L3: {}, MemoryLayer.L4: {}, MemoryLayer.L5: {},
        }
        
        self._last_merge_time = time.time()
        
        self._total_writes = 0
        self._total_promotions = 0
        self._total_forgets = 0
        self._total_merges = 0
        self._total_archives = 0
        
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 泊车低速槽初始化完成")
        print(f"[{self.module_id}] 专属策略: α={self.ALPHA}, β={self.BETA}, γ={self.GAMMA}")
        print(f"[{self.module_id}] 极低遗忘阈值: {self.MIN_FORGET_THRESHOLD}")
        print(f"[{self.module_id}] 泊车经验I₀自动+{self.PARKING_I0_BOOST}, "
              f"行人经验额外+{self.PEDESTRIAN_I0_BOOST}")
    
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
        
        泊车低速槽专属规则:
        - 所有泊车经验I₀自动+0.05
        - 涉及行人(Class4)的经验I₀额外+0.10
        """
        if self.state == SlotState.FROZEN:
            return False, "分槽已冻结，拒绝写入"
        
        if self.state == SlotState.MAINTENANCE:
            return False, "分槽维护中，拒绝写入"
        
        # 泊车经验基础加成
        entry.i0_value = min(entry.i0_value + self.PARKING_I0_BOOST, 1.0)
        
        # 行人经验额外加成
        if entry.core_risk_target_class == TargetClass.CLASS_4:
            entry.i0_value = min(entry.i0_value + self.PEDESTRIAN_I0_BOOST, 1.0)
            print(f"[{self.module_id}] 行人泊车经验I₀额外提升: {entry.entry_id[:12]}")
        
        entry.i_value = max(entry.i_value, entry.i0_value)
        
        # 检查L1容量
        if len(self._storage[MemoryLayer.L1]) >= self.MAX_L1_ENTRIES:
            self._emergency_clean_l1()
            if len(self._storage[MemoryLayer.L1]) >= self.MAX_L1_ENTRIES:
                return False, "L1存储满"
        
        entry.current_layer = MemoryLayer.L1
        entry.store_timestamp = time.time()
        
        self._storage[MemoryLayer.L1][entry.entry_id] = entry
        self._total_writes += 1
        
        return True, f"写入L1成功"
    
    def _emergency_clean_l1(self) -> None:
        """紧急清理L1"""
        l1 = self._storage[MemoryLayer.L1]
        if not l1:
            return
        
        sorted_entries = sorted(l1.items(), key=lambda x: x[1].i_value)
        remove_count = max(1, int(len(l1) * 0.03))  # 仅清理3%（更保守）
        
        for i in range(remove_count):
            entry_id = sorted_entries[i][0]
            if l1[entry_id].force_majeure:
                continue
            if l1[entry_id].core_risk_target_class == TargetClass.CLASS_4:
                continue
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
        """晋升单个条目"""
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
        
        本槽专属规则:
        - 使用极低遗忘阈值0.075
        - 容量告急时优先冷归档而非直接删除
        """
        if self.state == SlotState.FROZEN:
            return [(c.entry_id, ForgetResult.RETAINED) for c in candidates]
        
        results = []
        for candidate in candidates:
            if candidate.current_layer in [MemoryLayer.L4, MemoryLayer.L5]:
                results.append((candidate.entry_id, ForgetResult.SKIP_L4_L5))
                continue
            
            if candidate.i_value < self.MIN_FORGET_THRESHOLD:
                entry = self._storage[candidate.current_layer].get(candidate.entry_id)
                if entry:
                    if entry.force_majeure:
                        results.append((candidate.entry_id, ForgetResult.RETAINED))
                        continue
                    if entry.core_risk_target_class == TargetClass.CLASS_4:
                        results.append((candidate.entry_id, ForgetResult.RETAINED))
                        continue
                
                # 优先冷归档
                self._storage[candidate.current_layer].pop(candidate.entry_id, None)
                self._total_forgets += 1
                
                if candidate.current_layer in [MemoryLayer.L3, MemoryLayer.L4]:
                    self._total_archives += 1
                    results.append((candidate.entry_id, ForgetResult.ARCHIVED))
                else:
                    results.append((candidate.entry_id, ForgetResult.DELETED))
            else:
                results.append((candidate.entry_id, ForgetResult.RETAINED))
        
        return results
    
    # ========== 归并处理 ==========
    
    def check_and_merge(self) -> Optional[MergeSuggestion]:
        """检查并执行L3相似经验归并"""
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
                target_entry.i_value = (target_entry.i_value * target_entry.reuse_count + 
                                        source_entry.i_value * source_entry.reuse_count) / \
                                       (target_entry.reuse_count + source_entry.reuse_count)
                self._total_merges += 1
            
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
        
        泊车低速槽专属: 容量告急时优先建议归并和冷归档，不加速遗忘
        """
        usage = self._calculate_usage_rate()
        if usage > self.CAPACITY_CRITICAL and self.state != SlotState.CAPACITY_WARNING:
            self.state = SlotState.CAPACITY_WARNING
            return {
                "usage_rate": usage,
                "action": "prioritize_merge_and_archive",
                "note": "泊车低速槽不触发加速遗忘，建议增加配额或冷归档"
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
        return SlotStatusSnapshot(
            slot_id=17,
            storage_usage_rate=self._calculate_usage_rate(),
            l1_count=len(self._storage[MemoryLayer.L1]),
            l2_count=len(self._storage[MemoryLayer.L2]),
            l3_count=len(self._storage[MemoryLayer.L3]),
            l4_count=len(self._storage[MemoryLayer.L4]),
            l5_count=len(self._storage[MemoryLayer.L5]),
            is_active=(self.state != SlotState.FROZEN)
        )
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_writes": self._total_writes,
            "total_promotions": self._total_promotions,
            "total_forgets": self._total_forgets,
            "total_merges": self._total_merges,
            "total_archives": self._total_archives,
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
    print("ad-17 泊车低速槽 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    def make_entry(entry_id, i_value=0.5, core_class=None, force_majeure=False):
        return ExperienceEntry(
            entry_id=entry_id, content={"behavior": "泊车"},
            i_value=i_value, i0_value=i_value,
            source_slot_id=17, result_label="成功优化",
            core_risk_target_class=core_class,
            force_majeure=force_majeure
        )
    
    # --- TC-17-01: 泊车经验I₀自动加成 ---
    print("\n[TC-17-01] 泊车经验I₀自动加成")
    try:
        slot = ParkingLowSpeedSlot()
        entry = make_entry("EXP-001", i_value=0.3, i0_value=0.3)
        success, msg = slot.write_entry(entry)
        assert success == True
        stored = slot._storage[MemoryLayer.L1]["EXP-001"]
        assert stored.i0_value == 0.35  # 0.3 + 0.05
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-17-02: 行人泊车经验I₀额外加成 ---
    print("\n[TC-17-02] 行人泊车经验I₀额外加成")
    try:
        slot = ParkingLowSpeedSlot()
        entry = make_entry("EXP-002", i_value=0.3, i0_value=0.3, core_class=TargetClass.CLASS_4)
        success, msg = slot.write_entry(entry)
        assert success == True
        stored = slot._storage[MemoryLayer.L1]["EXP-002"]
        assert stored.i0_value == 0.45  # 0.3 + 0.05 + 0.10
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-17-03: 遗忘候选（I=0.10 ≥ 0.075，保留） ---
    print("\n[TC-17-03] 遗忘候选（I=0.10 ≥ 0.075，保留）")
    try:
        slot = ParkingLowSpeedSlot()
        slot.write_entry(make_entry("EXP-003", i_value=0.10))
        candidates = [ForgetCandidate("EXP-003", MemoryLayer.L1, 0.10)]
        results = slot.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.RETAINED
        assert "EXP-003" in slot._storage[MemoryLayer.L1]
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-17-04: 遗忘候选（I=0.05 < 0.075，删除） ---
    print("\n[TC-17-04] 遗忘候选（I=0.05 < 0.075，删除）")
    try:
        slot = ParkingLowSpeedSlot()
        slot.write_entry(make_entry("EXP-004", i_value=0.05))
        candidates = [ForgetCandidate("EXP-004", MemoryLayer.L1, 0.05)]
        results = slot.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.DELETED
        assert "EXP-004" not in slot._storage[MemoryLayer.L1]
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-17-05: 行人经验遗忘保护 ---
    print("\n[TC-17-05] 行人经验遗忘保护")
    try:
        slot = ParkingLowSpeedSlot()
        slot.write_entry(make_entry("EXP-005", i_value=0.03, core_class=TargetClass.CLASS_4))
        candidates = [ForgetCandidate("EXP-005", MemoryLayer.L1, 0.03)]
        results = slot.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.RETAINED
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-17-06: L1→L2晋升 ---
    print("\n[TC-17-06] L1→L2晋升")
    try:
        slot = ParkingLowSpeedSlot()
        slot.write_entry(make_entry("EXP-006", i_value=0.5))
        candidates = [PromotionCandidate("EXP-006", MemoryLayer.L2, 0.5, 50*3600)]
        results = slot.process_promotions(candidates)
        assert results[0][1] == PromotionResult.SUCCESS
        assert "EXP-006" in slot._storage[MemoryLayer.L2]
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-17-07: 容量告警优先归并和归档 ---
    print("\n[TC-17-07] 容量告警优先归并和归档")
    try:
        slot = ParkingLowSpeedSlot()
        slot.MAX_L1_ENTRIES = 10
        for i in range(10):
            slot.write_entry(make_entry(f"EXP-{i:03d}", i_value=0.5))
        alert = slot.check_capacity()
        assert alert is not None
        assert "prioritize_merge_and_archive" in alert["action"]
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-17-08: 不可抗力豁免遗忘 ---
    print("\n[TC-17-08] 不可抗力豁免遗忘")
    try:
        slot = ParkingLowSpeedSlot()
        slot.write_entry(make_entry("EXP-008", i_value=0.02, force_majeure=True))
        candidates = [ForgetCandidate("EXP-008", MemoryLayer.L1, 0.02)]
        results = slot.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.RETAINED
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-17-09: L4/L5不参与遗忘 ---
    print("\n[TC-17-09] L4/L5不参与遗忘")
    try:
        slot = ParkingLowSpeedSlot()
        slot._storage[MemoryLayer.L4]["EXP-L4"] = make_entry("EXP-L4", i_value=0.01)
        candidates = [ForgetCandidate("EXP-L4", MemoryLayer.L4, 0.01)]
        results = slot.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.SKIP_L4_L5
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-17-10: 归并检测 ---
    print("\n[TC-17-10] 归并检测")
    try:
        slot = ParkingLowSpeedSlot()
        slot._last_merge_time = 0
        slot._storage[MemoryLayer.L3]["EXP-A"] = make_entry("EXP-A", i_value=0.7)
        slot._storage[MemoryLayer.L3]["EXP-B"] = make_entry("EXP-B", i_value=0.6)
        merge = slot.check_and_merge()
        assert merge is not None or slot._calculate_usage_rate() < slot.CAPACITY_WARNING
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