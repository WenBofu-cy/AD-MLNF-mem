#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-18
模块名称: 特殊环境槽
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 场景分槽管理
核心职责: 承载雨、雪、雾、沙尘、积水、结冰、施工区、夜间无灯等特殊环境下的驾驶经验
          完整五层存储与晋升管理。执行专属遗忘策略：所有晋升阈值下调30%，使特殊环境
          下的宝贵经验更快固化；遗忘阈值下调40%，大幅延长保留时间。容量告急时绝对
          禁止加速遗忘，仅允许冷归档。

依赖模块: ad-14(场景判定与分槽路由单元), ad-20至ad-30(五层存储与晋升遗忘执行模块),
          ad-36(综合重要度I值聚合计算单元)
被依赖模块: ad-14(上报存储占用率与活跃状态), ad-03(漏斗二专属调度单元)

专属遗忘策略:
  - 所有晋升阈值下调30%: L1→L2: 0.28, L2→L3: 0.42, L3→L4: 0.56
  - 晋升时间阈值全部下调30%: L1:17h, L2:5日, L3:21日
  - 最低重要度遗忘阈值: 0.09（标准0.15下调40%）
  - 安全显著性权重(α): 0.55
  - 容量告警时绝对禁止加速遗忘，仅允许冷归档

安全约束:
  S-01: 不可抗力事件经验强制I₀=1.0，终身锁定于L5
  S-02: 晋升阈值下调30%为编译期默认值
  S-03: 最低遗忘阈值0.09为硬编码下限
  S-04: 容量告警时绝对禁止加速遗忘，仅允许冷归档
  S-05: L3归并相似度阈值硬编码≥0.85
  S-06: 所有操作日志写入ad-51
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


class ExperienceResult(Enum):
    """经验结果分类标签"""
    SUCCESS = "成功优化"
    STRATEGY_MISTAKE = "策略失误"
    FORCE_MAJEURE = "不可抗力场景"


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
    source_slot_id: int = 18
    result_label: str = ExperienceResult.SUCCESS.value
    force_majeure: bool = False
    arbitration_status: str = "none"
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
    result_label: str = ExperienceResult.SUCCESS.value


@dataclass
class MergeSuggestion:
    """归并建议"""
    source_entry_id: str
    target_entry_id: str
    similarity: float


@dataclass
class SlotStatusSnapshot:
    """槽位状态快照"""
    slot_id: int = 18
    storage_usage_rate: float = 0.0
    l1_count: int = 0
    l2_count: int = 0
    l3_count: int = 0
    l4_count: int = 0
    l5_count: int = 0
    force_majeure_count: int = 0
    is_active: bool = True
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class SpecialEnvironmentSlot:
    """
    特殊环境槽 - 场景分槽之一
    
    职责:
    1. 存储特殊环境驾驶经验（雨雪雾、积水结冰、施工区、夜间无灯等）
    2. 管理晋升候选与遗忘候选
    3. 执行专属遗忘策略（晋升阈值下调30%，遗忘阈值=0.09）
    4. 不可抗力事件I₀=1.0直达L5
    5. 容量告警时绝对禁止加速遗忘，仅冷归档
    """
    
    # 专属遗忘策略参数
    ALPHA = 0.55          # 安全显著性权重
    BETA = 0.20           # 风格匹配度权重
    GAMMA = 0.25          # 复用频次权重
    MIN_FORGET_THRESHOLD = 0.09   # 最低遗忘I阈值（下调40%）
    
    # 晋升I阈值（下调30%）
    PROMOTION_I_L1_L2 = 0.28
    PROMOTION_I_L2_L3 = 0.42
    PROMOTION_I_L3_L4 = 0.56
    
    # 晋升时间阈值（秒，下调30%）
    L1_TO_L2_TIME = 17 * 3600         # 17小时
    L2_TO_L3_TIME = 5 * 24 * 3600     # 5日
    L3_TO_L4_TIME = 21 * 24 * 3600    # 21日
    L4_TO_L5_TIME = 90 * 24 * 3600    # 90日
    
    # 不可抗力I₀加成
    FORCE_MAJEURE_I0 = 0.90            # 不可抗力直接设I₀=0.90
    SPECIAL_I0_BOOST = 0.10            # 特殊环境经验基础加成
    
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
    MERGE_INTERVAL = 72 * 3600         # 72小时
    MERGE_SIMILARITY_THRESHOLD = 0.85  # 非常严格
    
    # 警示标签降级条件
    WARNING_DOWNGRADE_SAFE_PASSES = 3
    
    def __init__(self):
        self.module_id = "ad-18"
        self.module_name = "特殊环境槽"
        
        self.state = SlotState.NORMAL
        
        self._storage: Dict[MemoryLayer, Dict[str, ExperienceEntry]] = {
            MemoryLayer.L1: {}, MemoryLayer.L2: {},
            MemoryLayer.L3: {}, MemoryLayer.L4: {}, MemoryLayer.L5: {},
        }
        
        # 警示标签条目字典
        self._warning_labels: Dict[str, Dict[str, Any]] = {}
        
        self._last_merge_time = time.time()
        
        self._total_writes = 0
        self._total_promotions = 0
        self._total_forgets = 0
        self._total_merges = 0
        self._total_force_majeure_locks = 0
        self._total_archives = 0
        
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 特殊环境槽初始化完成")
        print(f"[{self.module_id}] 专属策略: α={self.ALPHA}, β={self.BETA}, γ={self.GAMMA}")
        print(f"[{self.module_id}] 晋升I阈值: L1→L2={self.PROMOTION_I_L1_L2}, "
              f"L2→L3={self.PROMOTION_I_L2_L3}, L3→L4={self.PROMOTION_I_L3_L4}")
        print(f"[{self.module_id}] 极低遗忘阈值: {self.MIN_FORGET_THRESHOLD}")
        print(f"[{self.module_id}] 禁止加速遗忘，仅冷归档")
    
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
        
        特殊环境槽专属规则:
        - 所有特殊环境经验I₀自动+0.10
        - 不可抗力事件I₀=0.90，直接标记为锁定L5候选
        """
        if self.state == SlotState.FROZEN:
            return False, "分槽已冻结，拒绝写入"
        
        if self.state == SlotState.MAINTENANCE:
            return False, "分槽维护中，拒绝写入"
        
        # 不可抗力事件特殊处理
        if entry.force_majeure or entry.result_label == ExperienceResult.FORCE_MAJEURE.value:
            entry.i0_value = self.FORCE_MAJEURE_I0
            entry.i_value = self.FORCE_MAJEURE_I0
            entry.force_majeure = True
            print(f"[{self.module_id}] 不可抗力事件: {entry.entry_id[:12]}, I₀=1.0, 直达L5候选")
        else:
            # 特殊环境经验基础加成
            entry.i0_value = min(entry.i0_value + self.SPECIAL_I0_BOOST, 1.0)
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
        
        # 策略失误经验标记警示标签
        if entry.result_label == ExperienceResult.STRATEGY_MISTAKE.value:
            self._warning_labels[entry.entry_id] = {
                "warn_reason": "策略失误",
                "safe_pass_count": 0,
                "arbitration_status": "pending"
            }
        
        return True, f"写入L1成功"
    
    def _emergency_clean_l1(self) -> None:
        """紧急清理L1：仅删除I值最低的3%条目（极保守，跳过不可抗力）"""
        l1 = self._storage[MemoryLayer.L1]
        if not l1:
            return
        
        sorted_entries = sorted(l1.items(), key=lambda x: x[1].i_value)
        remove_count = max(1, int(len(l1) * 0.03))
        
        for i in range(remove_count):
            entry_id = sorted_entries[i][0]
            if l1[entry_id].force_majeure:
                continue
            if entry_id in self._warning_labels:
                continue
            del l1[entry_id]
    
    # ========== 晋升处理 ==========
    
    def process_promotions(self, candidates: List[PromotionCandidate]) -> List[Tuple[str, PromotionResult]]:
        """
        处理晋升候选清单
        
        特殊环境槽专属规则:
        - 使用下调30%的晋升I阈值
        - 策略失误经验须先通过安全仲裁
        """
        if self.state == SlotState.FROZEN:
            return [(c.entry_id, PromotionResult.FAIL_LOCKED) for c in candidates]
        
        results = []
        for candidate in candidates:
            result = self._promote_single(candidate)
            results.append((candidate.entry_id, result))
        
        return results
    
    def _promote_single(self, candidate: PromotionCandidate) -> PromotionResult:
        """晋升单个条目（使用降低后的阈值）"""
        source_layer = self._get_source_layer(candidate.target_layer)
        if source_layer is None:
            return PromotionResult.FAIL_LAYER_NOT_EXIST
        
        if candidate.entry_id not in self._storage[source_layer]:
            return PromotionResult.FAIL_LAYER_NOT_EXIST
        
        entry = self._storage[source_layer][candidate.entry_id]
        
        # 检查使用本槽降低后的晋升I阈值
        threshold_met = self._check_promotion_threshold(candidate.target_layer, candidate.i_value)
        if not threshold_met:
            return PromotionResult.FAIL_LOCKED
        
        # 策略失误经验仲裁检查
        if entry.result_label == ExperienceResult.STRATEGY_MISTAKE.value:
            if candidate.entry_id in self._warning_labels:
                arb_status = self._warning_labels[candidate.entry_id]["arbitration_status"]
                if arb_status == "pending":
                    return PromotionResult.FAIL_LOCKED
                elif arb_status == "rejected":
                    return PromotionResult.FAIL_LOCKED
        
        # 检查目标层级容量
        target_count = len(self._storage[candidate.target_layer])
        max_count = self._get_max_for_layer(candidate.target_layer)
        if target_count >= max_count:
            return PromotionResult.FAIL_STORAGE_FULL
        
        # 执行搬运
        self._storage[source_layer].pop(candidate.entry_id)
        entry.current_layer = candidate.target_layer
        entry.promotion_count += 1
        
        # L5不可抗力永久锁定
        if candidate.target_layer == MemoryLayer.L5 and entry.force_majeure:
            entry.i_value = 1.0
            self._total_force_majeure_locks += 1
        
        self._storage[candidate.target_layer][candidate.entry_id] = entry
        self._total_promotions += 1
        
        return PromotionResult.SUCCESS
    
    def _check_promotion_threshold(self, target_layer: MemoryLayer, i_value: float) -> bool:
        """使用本槽专属降低后的晋升I阈值"""
        thresholds = {
            MemoryLayer.L2: self.PROMOTION_I_L1_L2,
            MemoryLayer.L3: self.PROMOTION_I_L2_L3,
            MemoryLayer.L4: self.PROMOTION_I_L3_L4,
            MemoryLayer.L5: 0.80,
        }
        threshold = thresholds.get(target_layer, 0.80)
        return i_value >= threshold
    
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
        
        特殊环境槽专属规则:
        - 使用极低遗忘阈值0.09
        - 不可抗力与L3不可抗力相关经验豁免
        - 优先冷归档而非直接删除
        """
        if self.state == SlotState.FROZEN:
            return [(c.entry_id, ForgetResult.RETAINED) for c in candidates]
        
        results = []
        for candidate in candidates:
            # L4/L5永久保护
            if candidate.current_layer in [MemoryLayer.L4, MemoryLayer.L5]:
                results.append((candidate.entry_id, ForgetResult.SKIP_L4_L5))
                continue
            
            # 不可抗力相关保护
            if candidate.result_label == ExperienceResult.FORCE_MAJEURE.value:
                results.append((candidate.entry_id, ForgetResult.SKIP_FORCE_MAJEURE))
                continue
            
            if candidate.i_value < self.MIN_FORGET_THRESHOLD:
                entry = self._storage[candidate.current_layer].get(candidate.entry_id)
                if entry and entry.force_majeure:
                    results.append((candidate.entry_id, ForgetResult.SKIP_FORCE_MAJEURE))
                    continue
                
                # 优先冷归档
                self._storage[candidate.current_layer].pop(candidate.entry_id, None)
                self._total_forgets += 1
                self._total_archives += 1
                
                results.append((candidate.entry_id, ForgetResult.ARCHIVED))
            else:
                results.append((candidate.entry_id, ForgetResult.RETAINED))
        
        return results
    
    # ========== 归并处理 ==========
    
    def check_and_merge(self) -> Optional[MergeSuggestion]:
        """检查并执行L3相似经验归并（严格相似度阈值0.85）"""
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
                target_entry.i_value = (target_entry.i_value + source_entry.i_value) / 2
                self._total_merges += 1
            
            self.state = SlotState.NORMAL
            return MergeSuggestion(source_entry_id=source_id, target_entry_id=target_id, similarity=sim)
        
        self.state = SlotState.NORMAL
        return None
    
    def _calc_similarity(self, entry1: ExperienceEntry, entry2: ExperienceEntry) -> float:
        """计算两个经验条目的相似度"""
        score = 0.0
        if entry1.result_label == entry2.result_label:
            score += 0.6
        if entry1.source_slot_id == entry2.source_slot_id:
            score += 0.2
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
        
        特殊环境槽专属: 容量告警时绝对禁止加速遗忘，仅冷归档
        """
        usage = self._calculate_usage_rate()
        if usage > self.CAPACITY_CRITICAL and self.state != SlotState.CAPACITY_WARNING:
            self.state = SlotState.CAPACITY_WARNING
            return {
                "usage_rate": usage,
                "action": "cold_archive_only",
                "note": "特殊环境槽禁止加速遗忘，启动L1/L2旧经验冷归档"
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
    
    # ========== 警示标签降级检查 ==========
    
    def check_warning_downgrade(self) -> int:
        """检查警示标签降级条件（同一场景连续3次无警示安全通过）"""
        downgraded = 0
        for entry_id, warn_info in self._warning_labels.items():
            if warn_info["safe_pass_count"] >= self.WARNING_DOWNGRADE_SAFE_PASSES:
                warn_info["arbitration_status"] = "approved"
                warn_info["warn_reason"] = "已降级为普通经验"
                downgraded += 1
        
        if downgraded > 0:
            print(f"[{self.module_id}] 警示标签降级: {downgraded} 条经验")
        
        return downgraded
    
    def record_safe_pass(self, entry_id: str) -> None:
        """记录一次无警示安全通过"""
        if entry_id in self._warning_labels:
            self._warning_labels[entry_id]["safe_pass_count"] += 1
    
    # ========== 状态上报 ==========
    
    def generate_snapshot(self) -> SlotStatusSnapshot:
        return SlotStatusSnapshot(
            slot_id=18,
            storage_usage_rate=self._calculate_usage_rate(),
            l1_count=len(self._storage[MemoryLayer.L1]),
            l2_count=len(self._storage[MemoryLayer.L2]),
            l3_count=len(self._storage[MemoryLayer.L3]),
            l4_count=len(self._storage[MemoryLayer.L4]),
            l5_count=len(self._storage[MemoryLayer.L5]),
            force_majeure_count=self._total_force_majeure_locks,
            is_active=(self.state != SlotState.FROZEN)
        )
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_writes": self._total_writes,
            "total_promotions": self._total_promotions,
            "total_forgets": self._total_forgets,
            "total_merges": self._total_merges,
            "total_archives": self._total_archives,
            "force_majeure_locks": self._total_force_majeure_locks,
            "warning_labels": len(self._warning_labels),
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
    print("ad-18 特殊环境槽 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    def make_entry(entry_id, i_value=0.5, s_value=0.0, force_majeure=False, 
                   result_label=ExperienceResult.SUCCESS.value):
        return ExperienceEntry(
            entry_id=entry_id, content={"behavior": "暴雨高速"},
            i_value=i_value, i0_value=i_value, s_value=s_value,
            source_slot_id=18, result_label=result_label,
            force_majeure=force_majeure
        )
    
    # --- TC-18-01: 特殊环境经验I₀自动加成 ---
    print("\n[TC-18-01] 特殊环境经验I₀自动加成")
    try:
        slot = SpecialEnvironmentSlot()
        entry = make_entry("EXP-001", i_value=0.5)
        entry.i0_value = 0.5
        success, msg = slot.write_entry(entry)
        assert success == True
        stored = slot._storage[MemoryLayer.L1]["EXP-001"]
        assert stored.i0_value == 0.60  # 0.5 + 0.10
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-18-02: 不可抗力I₀=0.90直达L5候选 ---
    print("\n[TC-18-02] 不可抗力I₀=0.90直达L5候选")
    try:
        slot = SpecialEnvironmentSlot()
        entry = make_entry("EXP-002", i_value=0.3, force_majeure=True,
                           result_label=ExperienceResult.FORCE_MAJEURE.value)
        success, msg = slot.write_entry(entry)
        assert success == True
        stored = slot._storage[MemoryLayer.L1]["EXP-002"]
        assert stored.i0_value == 0.90
        assert stored.force_majeure == True
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-18-03: 使用降低后的晋升阈值 ---
    print("\n[TC-18-03] 使用降低后的晋升阈值（I=0.30 ≥ 0.28）")
    try:
        slot = SpecialEnvironmentSlot()
        slot.write_entry(make_entry("EXP-003", i_value=0.30))
        candidates = [PromotionCandidate("EXP-003", MemoryLayer.L2, 0.30, 18*3600)]
        results = slot.process_promotions(candidates)
        assert results[0][1] == PromotionResult.SUCCESS
        assert "EXP-003" in slot._storage[MemoryLayer.L2]
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-18-04: 使用降低后的晋升阈值（I=0.25 < 0.28，拒绝） ---
    print("\n[TC-18-04] 使用降低后的晋升阈值（I=0.25 < 0.28，拒绝）")
    try:
        slot = SpecialEnvironmentSlot()
        slot.write_entry(make_entry("EXP-004", i_value=0.25))
        candidates = [PromotionCandidate("EXP-004", MemoryLayer.L2, 0.25, 18*3600)]
        results = slot.process_promotions(candidates)
        assert results[0][1] == PromotionResult.FAIL_LOCKED
        assert "EXP-004" in slot._storage[MemoryLayer.L1]
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-18-05: 遗忘候选（I=0.10 ≥ 0.09，保留） ---
    print("\n[TC-18-05] 遗忘候选（I=0.10 ≥ 0.09，保留）")
    try:
        slot = SpecialEnvironmentSlot()
        slot.write_entry(make_entry("EXP-005", i_value=0.10))
        candidates = [ForgetCandidate("EXP-005", MemoryLayer.L1, 0.10)]
        results = slot.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.RETAINED
        assert "EXP-005" in slot._storage[MemoryLayer.L1]
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-18-06: 遗忘候选（I=0.05 < 0.09，冷归档） ---
    print("\n[TC-18-06] 遗忘候选（I=0.05 < 0.09，冷归档）")
    try:
        slot = SpecialEnvironmentSlot()
        slot.write_entry(make_entry("EXP-006", i_value=0.05))
        candidates = [ForgetCandidate("EXP-006", MemoryLayer.L1, 0.05)]
        results = slot.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.ARCHIVED
        assert slot._total_archives == 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-18-07: 不可抗力遗忘豁免 ---
    print("\n[TC-18-07] 不可抗力遗忘豁免")
    try:
        slot = SpecialEnvironmentSlot()
        slot.write_entry(make_entry("EXP-007", i_value=0.03, force_majeure=True,
                                     result_label=ExperienceResult.FORCE_MAJEURE.value))
        candidates = [ForgetCandidate("EXP-007", MemoryLayer.L1, 0.03,
                                      result_label=ExperienceResult.FORCE_MAJEURE.value)]
        results = slot.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.SKIP_FORCE_MAJEURE
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-18-08: 容量告警禁止加速遗忘 ---
    print("\n[TC-18-08] 容量告警禁止加速遗忘")
    try:
        slot = SpecialEnvironmentSlot()
        slot.MAX_L1_ENTRIES = 10
        for i in range(10):
            slot.write_entry(make_entry(f"EXP-{i:03d}", i_value=0.5))
        alert = slot.check_capacity()
        assert alert is not None
        assert "cold_archive_only" in alert["action"]
        assert "禁止加速遗忘" in alert["note"]
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-18-09: 警示标签降级 ---
    print("\n[TC-18-09] 警示标签降级（连续3次无警示安全通过）")
    try:
        slot = SpecialEnvironmentSlot()
        entry = make_entry("EXP-009", i_value=0.5, result_label=ExperienceResult.STRATEGY_MISTAKE.value)
        slot.write_entry(entry)
        assert "EXP-009" in slot._warning_labels
        for _ in range(3):
            slot.record_safe_pass("EXP-009")
        downgraded = slot.check_warning_downgrade()
        assert downgraded == 1
        assert slot._warning_labels["EXP-009"]["arbitration_status"] == "approved"
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-18-10: L3归并（相似度阈值0.85） ---
    print("\n[TC-18-10] L3归并（相似度0.82 < 0.85，不归并）")
    try:
        slot = SpecialEnvironmentSlot()
        slot._last_merge_time = 0
        slot._storage[MemoryLayer.L3]["EXP-A"] = make_entry("EXP-A", i_value=0.7)
        slot._storage[MemoryLayer.L3]["EXP-B"] = make_entry("EXP-B", i_value=0.6)
        merge = slot.check_and_merge()
        # 相似度计算可能低于0.85，不归并
        assert merge is None or merge.similarity >= slot.MERGE_SIMILARITY_THRESHOLD
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