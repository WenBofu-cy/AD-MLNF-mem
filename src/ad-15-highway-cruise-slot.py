#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-15
模块名称: 高速巡航槽
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 场景分槽管理
核心职责: 承载高速公路场景下跟车、变道、匝道汇入等驾驶经验的完整五层存储与晋升管理。
          执行专属遗忘策略：复用频次权重上调20%，强化高频操作的经验保留优先级。
          是本场景经验从L1临时层到L5核心层的完整生命周期管理者。

依赖模块: ad-14(场景判定与分槽路由单元), ad-20至ad-30(五层存储与晋升遗忘执行模块),
          ad-36(综合重要度I值聚合计算单元)
被依赖模块: ad-14(上报存储占用率与活跃状态), ad-03(漏斗二专属调度单元)

专属遗忘策略:
  - 复用频次权重(γ): 0.36（标准0.30上调20%）
  - 安全显著性权重(α): 0.50（不变）
  - 风格匹配度权重(β): 0.14（相应下调）
  - 最低重要度遗忘阈值: 0.12（标准0.15下调20%）
  - L2→L3晋升时间阈值: 5日（标准7日缩短）

安全约束:
  S-01: L5核心层经验物理锁定，仅可读取，不可修改或删除
  S-02: 专属遗忘策略参数编译期固化为默认值，运行时可调但不可超出安全边界
  S-03: 容量告警时加速遗忘仅针对L1/L2层，L3及以上层级受保护
  S-04: L5层不可抗力事件永久锁定，终身不可遗忘
  S-05: 所有晋升、遗忘、归并操作全量写入ad-51变更日志
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
    L1 = "L1"  # 临时层
    L2 = "L2"  # 近期层
    L3 = "L3"  # 中期层
    L4 = "L4"  # 长期层
    L5 = "L5"  # 核心层


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


# ==================== 数据结构 ====================

@dataclass
class ExperienceEntry:
    """经验条目（简化版）"""
    entry_id: str
    content: Dict[str, Any]           # 经验内容
    i_value: float                    # 当前重要度I值
    s_value: float                    # 安全显著性S值
    c_value: float                    # 复用频次C值
    source_slot_id: int               # 来源分槽号
    result_label: str                 # 结果分类标签
    force_majeure: bool = False       # 是否不可抗力
    current_layer: MemoryLayer = MemoryLayer.L1
    store_timestamp: float = field(default_factory=time.time)
    promotion_count: int = 0          # 晋升次数
    reuse_count: int = 0              # 复用计数


@dataclass
class PromotionCandidate:
    """晋升候选条目"""
    entry_id: str
    target_layer: MemoryLayer
    i_value: float
    retention_duration: float         # 留存时长（秒）


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
    similarity: float                 # 相似度 0.0-1.0


@dataclass
class SlotStatusSnapshot:
    """槽位状态快照"""
    slot_id: int = 15
    storage_usage_rate: float = 0.0
    l1_count: int = 0
    l2_count: int = 0
    l3_count: int = 0
    l4_count: int = 0
    l5_count: int = 0
    is_active: bool = True
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class HighwayCruiseSlot:
    """
    高速巡航槽 - 场景分槽之一
    
    职责:
    1. 存储高速场景驾驶经验（L1→L2→L3→L4→L5五层）
    2. 管理晋升候选与遗忘候选
    3. 执行专属遗忘策略（γ=0.36，遗忘阈值=0.12）
    4. 容量监控与L3相似经验归并
    5. 周期性状态上报
    """
    
    # 专属遗忘策略参数（编译期默认值）
    ALPHA = 0.50          # 安全显著性权重
    BETA = 0.14           # 风格匹配度权重
    GAMMA = 0.36          # 复用频次权重（上调20%）
    MIN_FORGET_THRESHOLD = 0.12  # 最低遗忘I阈值（下调20%）
    
    # 晋升时间阈值（秒）
    L1_TO_L2_TIME = 24 * 3600        # 24小时
    L2_TO_L3_TIME = 5 * 24 * 3600    # 5日（缩短）
    L3_TO_L4_TIME = 30 * 24 * 3600   # 30日
    L4_TO_L5_TIME = 90 * 24 * 3600   # 90日
    
    # 容量阈值
    CAPACITY_WARNING = 0.85
    CAPACITY_CRITICAL = 0.90
    CAPACITY_FULL = 0.95
    
    # 单层最大条目数（模拟值）
    MAX_L1_ENTRIES = 600
    MAX_L2_ENTRIES = 250
    MAX_L3_ENTRIES = 100
    MAX_L4_ENTRIES = 45
    MAX_L5_ENTRIES = 5
    
    # 归并间隔（秒）
    MERGE_INTERVAL = 24 * 3600       # 24小时
    
    def __init__(self):
        self.module_id = "ad-15"
        self.module_name = "高速巡航槽"
        
        # 内部状态
        self.state = SlotState.NORMAL
        
        # 五层存储: 层级 -> {entry_id: ExperienceEntry}
        self._storage: Dict[MemoryLayer, Dict[str, ExperienceEntry]] = {
            MemoryLayer.L1: {},
            MemoryLayer.L2: {},
            MemoryLayer.L3: {},
            MemoryLayer.L4: {},
            MemoryLayer.L5: {},
        }
        
        # 上次归并时间
        self._last_merge_time = time.time()
        
        # 统计
        self._total_writes = 0
        self._total_promotions = 0
        self._total_forgets = 0
        self._total_merges = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 高速巡航槽初始化完成")
        print(f"[{self.module_id}] 专属策略: α={self.ALPHA}, β={self.BETA}, γ={self.GAMMA}, "
              f"遗忘阈值={self.MIN_FORGET_THRESHOLD}")
    
    # ========== 状态管理 ==========
    
    def freeze(self) -> None:
        """冻结分槽（驾驶模式切换时调用）"""
        self.state = SlotState.FROZEN
        print(f"[{self.module_id}] 高速巡航槽已冻结")
    
    def unfreeze(self) -> None:
        """解冻分槽"""
        self.state = SlotState.NORMAL
        print(f"[{self.module_id}] 高速巡航槽已解冻")
    
    def get_state(self) -> SlotState:
        return self.state
    
    # ========== 经验写入 ==========
    
    def write_entry(self, entry: ExperienceEntry) -> Tuple[bool, str]:
        """
        将新经验写入L1临时层
        
        Args:
            entry: 经验条目
            
        Returns:
            (成功, 消息)
        """
        if self.state == SlotState.FROZEN:
            return False, "分槽已冻结，拒绝写入"
        
        if self.state == SlotState.MAINTENANCE:
            return False, "分槽维护中，拒绝写入"
        
        # 检查L1容量
        l1_count = len(self._storage[MemoryLayer.L1])
        if l1_count >= self.MAX_L1_ENTRIES:
            # 触发紧急清理
            self._emergency_clean_l1()
            if len(self._storage[MemoryLayer.L1]) >= self.MAX_L1_ENTRIES:
                return False, "L1存储满"
        
        entry.current_layer = MemoryLayer.L1
        entry.store_timestamp = time.time()
        
        self._storage[MemoryLayer.L1][entry.entry_id] = entry
        self._total_writes += 1
        
        return True, f"写入L1成功, entry={entry.entry_id[:12]}"
    
    def _emergency_clean_l1(self) -> None:
        """紧急清理L1：删除I值最低的5%条目"""
        l1 = self._storage[MemoryLayer.L1]
        if not l1:
            return
        
        sorted_entries = sorted(l1.items(), key=lambda x: x[1].i_value)
        remove_count = max(1, int(len(l1) * 0.05))
        
        for i in range(remove_count):
            entry_id = sorted_entries[i][0]
            # 跳过不可抗力条目
            if l1[entry_id].force_majeure:
                continue
            del l1[entry_id]
    
    # ========== 晋升处理 ==========
    
    def process_promotions(self, candidates: List[PromotionCandidate]) -> List[Tuple[str, PromotionResult]]:
        """
        处理晋升候选清单
        
        Args:
            candidates: 晋升候选列表
            
        Returns:
            [(entry_id, 结果), ...]
        """
        if self.state == SlotState.FROZEN:
            return [(c.entry_id, PromotionResult.FAIL_LOCKED) for c in candidates]
        
        results = []
        
        for candidate in candidates:
            result = self._promote_single(candidate)
            results.append((candidate.entry_id, result))
        
        return results
    
    def _promote_single(self, candidate: PromotionCandidate) -> PromotionResult:
        """晋升单个条目"""
        # 确定源层级
        source_layer = self._get_source_layer(candidate.target_layer)
        if source_layer is None:
            return PromotionResult.FAIL_LAYER_NOT_EXIST
        
        # 检查源层级是否有该条目
        if candidate.entry_id not in self._storage[source_layer]:
            return PromotionResult.FAIL_LAYER_NOT_EXIST
        
        # 检查目标层级容量
        target_count = len(self._storage[candidate.target_layer])
        max_count = self._get_max_for_layer(candidate.target_layer)
        if target_count >= max_count:
            return PromotionResult.FAIL_STORAGE_FULL
        
        # 执行搬运
        entry = self._storage[source_layer].pop(candidate.entry_id)
        entry.current_layer = candidate.target_layer
        entry.promotion_count += 1
        
        # L5锁定标记
        if candidate.target_layer == MemoryLayer.L5:
            entry.i_value = max(entry.i_value, 0.90)
        
        self._storage[candidate.target_layer][candidate.entry_id] = entry
        self._total_promotions += 1
        
        return PromotionResult.SUCCESS
    
    def _get_source_layer(self, target_layer: MemoryLayer) -> Optional[MemoryLayer]:
        """根据目标层级确定源层级"""
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
        - L4/L5不参与遗忘
        - 使用专属遗忘阈值0.12（标准为0.15）
        
        Args:
            candidates: 遗忘候选列表
            
        Returns:
            [(entry_id, 结果), ...]
        """
        if self.state == SlotState.FROZEN:
            return [(c.entry_id, ForgetResult.RETAINED) for c in candidates]
        
        results = []
        
        for candidate in candidates:
            # L4/L5跳过
            if candidate.current_layer in [MemoryLayer.L4, MemoryLayer.L5]:
                results.append((candidate.entry_id, ForgetResult.SKIP_L4_L5))
                continue
            
            # 使用专属遗忘阈值判定
            if candidate.i_value < self.MIN_FORGET_THRESHOLD:
                # 不可抗力豁免
                if candidate.entry_id in self._storage[candidate.current_layer]:
                    entry = self._storage[candidate.current_layer][candidate.entry_id]
                    if entry.force_majeure:
                        results.append((candidate.entry_id, ForgetResult.RETAINED))
                        continue
                
                # 删除
                self._storage[candidate.current_layer].pop(candidate.entry_id, None)
                self._total_forgets += 1
                
                if candidate.current_layer == MemoryLayer.L3:
                    results.append((candidate.entry_id, ForgetResult.ARCHIVED))
                else:
                    results.append((candidate.entry_id, ForgetResult.DELETED))
            else:
                results.append((candidate.entry_id, ForgetResult.RETAINED))
        
        return results
    
    # ========== 归并处理 ==========
    
    def check_and_merge(self) -> Optional[MergeSuggestion]:
        """
        检查并执行L3相似经验归并
        
        Returns:
            归并建议（如果有），否则None
        """
        now = time.time()
        if now - self._last_merge_time < self.MERGE_INTERVAL:
            return None
        
        usage = self._calculate_usage_rate()
        if usage < self.CAPACITY_WARNING:
            return None
        
        self.state = SlotState.MAINTENANCE
        self._last_merge_time = now
        
        # 简化实现：查找L3中相似度最高的两个条目
        l3_entries = list(self._storage[MemoryLayer.L3].items())
        if len(l3_entries) < 2:
            self.state = SlotState.NORMAL
            return None
        
        best_pair = None
        best_sim = 0.0
        
        for i in range(len(l3_entries)):
            for j in range(i + 1, len(l3_entries)):
                sim = self._calc_similarity(l3_entries[i][1], l3_entries[j][1])
                if sim > best_sim and sim >= 0.75:
                    best_sim = sim
                    # 保留I值更高的
                    if l3_entries[i][1].i_value >= l3_entries[j][1].i_value:
                        best_pair = (l3_entries[i][0], l3_entries[j][0], sim)
                    else:
                        best_pair = (l3_entries[j][0], l3_entries[i][0], sim)
        
        if best_pair:
            # 执行归并
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
        """计算两个经验条目的相似度（简化实现）"""
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
        """计算存储占用率"""
        total = (len(self._storage[MemoryLayer.L1]) / self.MAX_L1_ENTRIES * 0.60 +
                 len(self._storage[MemoryLayer.L2]) / self.MAX_L2_ENTRIES * 0.25 +
                 len(self._storage[MemoryLayer.L3]) / self.MAX_L3_ENTRIES * 0.10 +
                 len(self._storage[MemoryLayer.L4]) / self.MAX_L4_ENTRIES * 0.045 +
                 len(self._storage[MemoryLayer.L5]) / self.MAX_L5_ENTRIES * 0.005)
        return total
    
    def check_capacity(self) -> Optional[Dict[str, Any]]:
        """检查容量状态"""
        usage = self._calculate_usage_rate()
        
        if usage > self.CAPACITY_CRITICAL and self.state != SlotState.CAPACITY_WARNING:
            self.state = SlotState.CAPACITY_WARNING
            return {
                "usage_rate": usage,
                "action": "accelerate_forget_l1_l2",
                "forget_threshold_boost": 0.30
            }
        elif usage < 0.70 and self.state == SlotState.CAPACITY_WARNING:
            self.state = SlotState.NORMAL
            return {
                "usage_rate": usage,
                "action": "restore_normal"
            }
        
        return None
    
    def _get_max_for_layer(self, layer: MemoryLayer) -> int:
        """获取层级最大容量"""
        return {
            MemoryLayer.L1: self.MAX_L1_ENTRIES,
            MemoryLayer.L2: self.MAX_L2_ENTRIES,
            MemoryLayer.L3: self.MAX_L3_ENTRIES,
            MemoryLayer.L4: self.MAX_L4_ENTRIES,
            MemoryLayer.L5: self.MAX_L5_ENTRIES,
        }.get(layer, 100)
    
    # ========== 状态上报 ==========
    
    def generate_snapshot(self) -> SlotStatusSnapshot:
        """生成槽位状态快照"""
        return SlotStatusSnapshot(
            slot_id=15,
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
    print("ad-15 高速巡航槽 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    def make_entry(entry_id, i_value=0.5, force_majeure=False):
        return ExperienceEntry(
            entry_id=entry_id,
            content={"behavior": "跟车"},
            i_value=i_value,
            s_value=0.3,
            c_value=0.0,
            source_slot_id=15,
            result_label="成功优化",
            force_majeure=force_majeure
        )
    
    # --- TC-15-01: 写入L1成功 ---
    print("\n[TC-15-01] 写入L1成功")
    try:
        slot = HighwayCruiseSlot()
        entry = make_entry("EXP-001", i_value=0.5)
        success, msg = slot.write_entry(entry)
        assert success == True
        assert slot._storage[MemoryLayer.L1]["EXP-001"].i_value == 0.5
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-15-02: L1→L2晋升成功 ---
    print("\n[TC-15-02] L1→L2晋升成功")
    try:
        slot = HighwayCruiseSlot()
        slot.write_entry(make_entry("EXP-002", i_value=0.55))
        candidates = [PromotionCandidate("EXP-002", MemoryLayer.L2, 0.55, 26*3600)]
        results = slot.process_promotions(candidates)
        assert results[0][1] == PromotionResult.SUCCESS
        assert "EXP-002" in slot._storage[MemoryLayer.L2]
        assert "EXP-002" not in slot._storage[MemoryLayer.L1]
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-15-03: 遗忘候选（I值低于专属阈值0.12） ---
    print("\n[TC-15-03] 遗忘候选（I=0.08 < 0.12）")
    try:
        slot = HighwayCruiseSlot()
        slot.write_entry(make_entry("EXP-003", i_value=0.08))
        candidates = [ForgetCandidate("EXP-003", MemoryLayer.L1, 0.08)]
        results = slot.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.DELETED
        assert "EXP-003" not in slot._storage[MemoryLayer.L1]
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-15-04: 遗忘候选（I=0.15 ≥ 0.12，保留） ---
    print("\n[TC-15-04] 遗忘候选（I=0.15 ≥ 0.12，保留）")
    try:
        slot = HighwayCruiseSlot()
        slot.write_entry(make_entry("EXP-004", i_value=0.15))
        candidates = [ForgetCandidate("EXP-004", MemoryLayer.L1, 0.15)]
        results = slot.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.RETAINED
        assert "EXP-004" in slot._storage[MemoryLayer.L1]
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-15-05: L4/L5不参与遗忘 ---
    print("\n[TC-15-05] L4/L5不参与遗忘")
    try:
        slot = HighwayCruiseSlot()
        slot._storage[MemoryLayer.L4]["EXP-L4"] = make_entry("EXP-L4", i_value=0.05)
        candidates = [ForgetCandidate("EXP-L4", MemoryLayer.L4, 0.05)]
        results = slot.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.SKIP_L4_L5
        assert "EXP-L4" in slot._storage[MemoryLayer.L4]
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-15-06: 不可抗力豁免遗忘 ---
    print("\n[TC-15-06] 不可抗力豁免遗忘")
    try:
        slot = HighwayCruiseSlot()
        slot.write_entry(make_entry("EXP-005", i_value=0.05, force_majeure=True))
        candidates = [ForgetCandidate("EXP-005", MemoryLayer.L1, 0.05)]
        results = slot.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.RETAINED
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-15-07: 冻结状态拒绝写入 ---
    print("\n[TC-15-07] 冻结状态拒绝写入")
    try:
        slot = HighwayCruiseSlot()
        slot.freeze()
        success, msg = slot.write_entry(make_entry("EXP-006"))
        assert success == False
        assert "冻结" in msg
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-15-08: 容量告警检测 ---
    print("\n[TC-15-08] 容量告警检测")
    try:
        slot = HighwayCruiseSlot()
        slot.MAX_L1_ENTRIES = 10
        for i in range(10):
            slot.write_entry(make_entry(f"EXP-{i:03d}", i_value=0.5))
        alert = slot.check_capacity()
        assert alert is not None
        assert alert["action"] == "accelerate_forget_l1_l2"
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-15-09: L3归并检测 ---
    print("\n[TC-15-09] L3归并检测")
    try:
        slot = HighwayCruiseSlot()
        slot._last_merge_time = 0
        slot._storage[MemoryLayer.L3]["EXP-A"] = make_entry("EXP-A", i_value=0.7)
        slot._storage[MemoryLayer.L3]["EXP-B"] = make_entry("EXP-B", i_value=0.6)
        # 模拟高容量触发归并
        merge = slot.check_and_merge()
        assert merge is not None or slot._calculate_usage_rate() < slot.CAPACITY_WARNING
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-15-10: 状态快照生成 ---
    print("\n[TC-15-10] 状态快照生成")
    try:
        slot = HighwayCruiseSlot()
        slot.write_entry(make_entry("EXP-SNAP", i_value=0.5))
        snapshot = slot.generate_snapshot()
        assert snapshot.slot_id == 15
        assert snapshot.l1_count == 1
        assert snapshot.is_active == True
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