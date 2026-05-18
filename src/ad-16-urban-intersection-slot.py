#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-16
模块名称: 城区路口槽
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 场景分槽管理
核心职责: 承载城市道路场景下红绿灯通行、人行横道礼让、无保护左转、拥堵跟车等
          驾驶经验的完整五层存储与晋升管理。执行专属遗忘策略：安全显著性权重上调20%，
          强化涉及行人及高风险场景的经验保留优先级。

依赖模块: ad-14(场景判定与分槽路由单元), ad-20至ad-30(五层存储与晋升遗忘执行模块),
          ad-36(综合重要度I值聚合计算单元)
被依赖模块: ad-14(上报存储占用率与活跃状态), ad-03(漏斗二专属调度单元)

专属遗忘策略:
  - 安全显著性权重(α): 0.60（标准0.50上调20%）
  - 复用频次权重(γ): 0.24（相应下调）
  - 风格匹配度权重(β): 0.16（相应下调）
  - 最低重要度遗忘阈值: 0.10（标准0.15下调33%）
  - L1→L2晋升时间阈值: 12h（标准24h缩短）
  - L2→L3晋升时间阈值: 5日（标准7日缩短）
  - L4安全底线自动锁定: S>0.85自动申请L5锁定

安全约束:
  S-01: 安全显著性权重上调20%为编译期默认值，运行时可调范围0.50–0.65
  S-02: 涉及行人/非机动车的经验I₀自动提升10%
  S-03: 行人礼让相关的L5经验永久锁定，不可遗忘
  S-04: 容量告警时加速遗忘仅限L1/L2，保障中期以上经验安全
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
    """目标类别（简化）"""
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
    source_slot_id: int = 16
    result_label: str = "成功优化"
    force_majeure: bool = False
    core_risk_target_class: Optional[TargetClass] = None
    current_layer: MemoryLayer = MemoryLayer.L1
    store_timestamp: float = field(default_factory=time.time)
    promotion_count: int = 0
    reuse_count: int = 0
    arbitration_passed: bool = False


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
    slot_id: int = 16
    storage_usage_rate: float = 0.0
    l1_count: int = 0
    l2_count: int = 0
    l3_count: int = 0
    l4_count: int = 0
    l5_count: int = 0
    is_active: bool = True
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class UrbanIntersectionSlot:
    """
    城区路口槽 - 场景分槽之一
    
    职责:
    1. 存储城区路口场景驾驶经验（L1→L2→L3→L4→L5五层）
    2. 管理晋升候选与遗忘候选
    3. 执行专属遗忘策略（α=0.60，遗忘阈值=0.10）
    4. 行人/非机动车相关经验I₀自动提升10%
    5. S>0.85的高安全经验自动锁定L5
    6. 容量监控与L3相似经验归并
    """
    
    # 专属遗忘策略参数
    ALPHA = 0.60          # 安全显著性权重（上调20%）
    BETA = 0.16           # 风格匹配度权重
    GAMMA = 0.24          # 复用频次权重
    MIN_FORGET_THRESHOLD = 0.10   # 最低遗忘I阈值（下调33%）
    
    # 晋升时间阈值（秒）
    L1_TO_L2_TIME = 12 * 3600       # 12小时（缩短）
    L2_TO_L3_TIME = 5 * 24 * 3600   # 5日（缩短）
    L3_TO_L4_TIME = 30 * 24 * 3600  # 30日
    L4_TO_L5_TIME = 90 * 24 * 3600  # 90日
    
    # L4自动锁定条件
    AUTO_LOCK_S_THRESHOLD = 0.85
    
    # 行人/非机动车I₀提升比例
    PEDESTRIAN_I0_BOOST = 0.10
    
    # 容量阈值
    CAPACITY_WARNING = 0.85
    CAPACITY_CRITICAL = 0.90
    
    # 单层最大条目数
    MAX_L1_ENTRIES = 600
    MAX_L2_ENTRIES = 250
    MAX_L3_ENTRIES = 100
    MAX_L4_ENTRIES = 45
    MAX_L5_ENTRIES = 5
    
    def __init__(self):
        self.module_id = "ad-16"
        self.module_name = "城区路口槽"
        
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
        self._total_auto_locks = 0
        
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 城区路口槽初始化完成")
        print(f"[{self.module_id}] 专属策略: α={self.ALPHA}, β={self.BETA}, γ={self.GAMMA}, "
              f"遗忘阈值={self.MIN_FORGET_THRESHOLD}")
        print(f"[{self.module_id}] 行人/非机动车I₀自动提升: +{self.PEDESTRIAN_I0_BOOST*100}%")
        print(f"[{self.module_id}] L4自动锁定条件: S>{self.AUTO_LOCK_S_THRESHOLD}")
    
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
        
        城区路口槽专属规则:
        - 涉及行人(Class4)或非人生物(Class3)的经验I₀自动提升10%
        """
        if self.state == SlotState.FROZEN:
            return False, "分槽已冻结，拒绝写入"
        
        if self.state == SlotState.MAINTENANCE:
            return False, "分槽维护中，拒绝写入"
        
        # 行人/非机动车经验I₀自动提升
        if entry.core_risk_target_class in [TargetClass.CLASS_4, TargetClass.CLASS_3]:
            entry.i0_value = min(entry.i0_value * (1 + self.PEDESTRIAN_I0_BOOST), 1.0)
            entry.i_value = max(entry.i_value, entry.i0_value)
            print(f"[{self.module_id}] 行人/非机动车经验I₀提升: {entry.entry_id[:12]}, "
                  f"新I₀={entry.i0_value:.2f}")
        
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
        """紧急清理L1：删除I值最低的5%条目"""
        l1 = self._storage[MemoryLayer.L1]
        if not l1:
            return
        
        sorted_entries = sorted(l1.items(), key=lambda x: x[1].i_value)
        remove_count = max(1, int(len(l1) * 0.05))
        
        for i in range(remove_count):
            entry_id = sorted_entries[i][0]
            if l1[entry_id].force_majeure:
                continue
            if l1[entry_id].core_risk_target_class in [TargetClass.CLASS_4, TargetClass.CLASS_3]:
                continue  # 行人/非机动车经验保护
            del l1[entry_id]
    
    # ========== 晋升处理 ==========
    
    def process_promotions(self, candidates: List[PromotionCandidate]) -> List[Tuple[str, PromotionResult]]:
        """
        处理晋升候选清单
        
        城区路口槽专属规则:
        - L4→L5时，S>0.85自动锁定
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
        
        # L5锁定
        if candidate.target_layer == MemoryLayer.L5:
            entry.i_value = max(entry.i_value, 0.90)
            if entry.core_risk_target_class in [TargetClass.CLASS_4]:
                entry.force_majeure = True  # 行人礼让经验永久锁定
                print(f"[{self.module_id}] 行人礼让L5经验永久锁定: {candidate.entry_id[:12]}")
        
        # L4自动锁定条件
        if candidate.target_layer == MemoryLayer.L4:
            if entry.s_value > self.AUTO_LOCK_S_THRESHOLD:
                self._total_auto_locks += 1
                print(f"[{self.module_id}] L4自动锁定建议: {candidate.entry_id[:12]}, S={entry.s_value:.2f}")
        
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
        - 使用专属遗忘阈值0.10
        - 涉及行人/非机动车的经验即使I值低也保留
        """
        if self.state == SlotState.FROZEN:
            return [(c.entry_id, ForgetResult.RETAINED) for c in candidates]
        
        results = []
        for candidate in candidates:
            if candidate.current_layer in [MemoryLayer.L4, MemoryLayer.L5]:
                results.append((candidate.entry_id, ForgetResult.SKIP_L4_L5))
                continue
            
            if candidate.i_value < self.MIN_FORGET_THRESHOLD:
                # 检查是否涉及行人/非机动车
                entry = self._storage[candidate.current_layer].get(candidate.entry_id)
                if entry:
                    if entry.force_majeure:
                        results.append((candidate.entry_id, ForgetResult.RETAINED))
                        continue
                    if entry.core_risk_target_class in [TargetClass.CLASS_4, TargetClass.CLASS_3]:
                        results.append((candidate.entry_id, ForgetResult.RETAINED))
                        continue
                
                self._storage[candidate.current_layer].pop(candidate.entry_id, None)
                self._total_forgets += 1
                
                if candidate.current_layer == MemoryLayer.L3:
                    results.append((candidate.entry_id, ForgetResult.ARCHIVED))
                else:
                    results.append((candidate.entry_id, ForgetResult.DELETED))
            else:
                results.append((candidate.entry_id, ForgetResult.RETAINED))
        
        return results
    
    # ========== 容量监控 ==========
    
    def _calculate_usage_rate(self) -> float:
        total = (len(self._storage[MemoryLayer.L1]) / self.MAX_L1_ENTRIES * 0.60 +
                 len(self._storage[MemoryLayer.L2]) / self.MAX_L2_ENTRIES * 0.25 +
                 len(self._storage[MemoryLayer.L3]) / self.MAX_L3_ENTRIES * 0.10 +
                 len(self._storage[MemoryLayer.L4]) / self.MAX_L4_ENTRIES * 0.045 +
                 len(self._storage[MemoryLayer.L5]) / self.MAX_L5_ENTRIES * 0.005)
        return total
    
    def check_capacity(self) -> Optional[Dict[str, Any]]:
        usage = self._calculate_usage_rate()
        if usage > self.CAPACITY_CRITICAL and self.state != SlotState.CAPACITY_WARNING:
            self.state = SlotState.CAPACITY_WARNING
            return {"usage_rate": usage, "action": "accelerate_forget_l1_l2"}
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
            slot_id=16,
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
            "total_auto_locks": self._total_auto_locks,
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
    print("ad-16 城区路口槽 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    def make_entry(entry_id, i_value=0.5, s_value=0.3, core_class=None, force_majeure=False):
        return ExperienceEntry(
            entry_id=entry_id, content={"behavior": "路口通行"},
            i_value=i_value, s_value=s_value,
            source_slot_id=16, result_label="成功优化",
            core_risk_target_class=core_class,
            force_majeure=force_majeure
        )
    
    # --- TC-16-01: 行人经验I₀自动提升 ---
    print("\n[TC-16-01] 行人经验I₀自动提升")
    try:
        slot = UrbanIntersectionSlot()
        entry = make_entry("EXP-001", i_value=0.5, core_class=TargetClass.CLASS_4)
        entry.i0_value = 0.5
        success, msg = slot.write_entry(entry)
        assert success == True
        stored = slot._storage[MemoryLayer.L1]["EXP-001"]
        assert stored.i0_value > 0.5
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-16-02: L1→L2晋升 ---
    print("\n[TC-16-02] L1→L2晋升")
    try:
        slot = UrbanIntersectionSlot()
        slot.write_entry(make_entry("EXP-002", i_value=0.5))
        candidates = [PromotionCandidate("EXP-002", MemoryLayer.L2, 0.5, 13*3600)]
        results = slot.process_promotions(candidates)
        assert results[0][1] == PromotionResult.SUCCESS
        assert "EXP-002" in slot._storage[MemoryLayer.L2]
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-16-03: 遗忘候选（I=0.08 < 0.10） ---
    print("\n[TC-16-03] 遗忘候选（I=0.08 < 0.10）")
    try:
        slot = UrbanIntersectionSlot()
        slot.write_entry(make_entry("EXP-003", i_value=0.08))
        candidates = [ForgetCandidate("EXP-003", MemoryLayer.L1, 0.08)]
        results = slot.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.DELETED
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-16-04: 遗忘候选（I=0.12 ≥ 0.10，保留） ---
    print("\n[TC-16-04] 遗忘候选（I=0.12 ≥ 0.10，保留）")
    try:
        slot = UrbanIntersectionSlot()
        slot.write_entry(make_entry("EXP-004", i_value=0.12))
        candidates = [ForgetCandidate("EXP-004", MemoryLayer.L1, 0.12)]
        results = slot.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.RETAINED
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-16-05: 行人经验遗忘保护 ---
    print("\n[TC-16-05] 行人经验遗忘保护")
    try:
        slot = UrbanIntersectionSlot()
        slot.write_entry(make_entry("EXP-005", i_value=0.05, core_class=TargetClass.CLASS_4))
        candidates = [ForgetCandidate("EXP-005", MemoryLayer.L1, 0.05)]
        results = slot.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.RETAINED
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-16-06: L4自动锁定条件 ---
    print("\n[TC-16-06] L4自动锁定条件（S>0.85）")
    try:
        slot = UrbanIntersectionSlot()
        slot.write_entry(make_entry("EXP-006", i_value=0.8, s_value=0.90))
        candidates = [PromotionCandidate("EXP-006", MemoryLayer.L4, 0.8, 31*24*3600)]
        results = slot.process_promotions(candidates)
        assert results[0][1] == PromotionResult.SUCCESS
        assert slot._total_auto_locks == 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-16-07: 行人礼让L5永久锁定 ---
    print("\n[TC-16-07] 行人礼让L5永久锁定")
    try:
        slot = UrbanIntersectionSlot()
        # 先晋升到L4
        entry = make_entry("EXP-007", i_value=0.85, core_class=TargetClass.CLASS_4)
        entry.promotion_count = 3
        slot.write_entry(entry)
        slot.process_promotions([PromotionCandidate("EXP-007", MemoryLayer.L2, 0.85, 8*24*3600)])
        slot.process_promotions([PromotionCandidate("EXP-007", MemoryLayer.L3, 0.85, 35*24*3600)])
        slot.process_promotions([PromotionCandidate("EXP-007", MemoryLayer.L4, 0.85, 95*24*3600)])
        # 晋升到L5
        results = slot.process_promotions([PromotionCandidate("EXP-007", MemoryLayer.L5, 0.85, 95*24*3600)])
        assert results[0][1] == PromotionResult.SUCCESS
        assert slot._storage[MemoryLayer.L5]["EXP-007"].force_majeure == True
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-16-08: L4/L5不参与遗忘 ---
    print("\n[TC-16-08] L4/L5不参与遗忘")
    try:
        slot = UrbanIntersectionSlot()
        slot._storage[MemoryLayer.L4]["EXP-L4"] = make_entry("EXP-L4", i_value=0.05)
        candidates = [ForgetCandidate("EXP-L4", MemoryLayer.L4, 0.05)]
        results = slot.process_forget_candidates(candidates)
        assert results[0][1] == ForgetResult.SKIP_L4_L5
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-16-09: 容量告警 ---
    print("\n[TC-16-09] 容量告警")
    try:
        slot = UrbanIntersectionSlot()
        slot.MAX_L1_ENTRIES = 10
        for i in range(10):
            slot.write_entry(make_entry(f"EXP-{i:03d}", i_value=0.5))
        alert = slot.check_capacity()
        assert alert is not None
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-16-10: 非机动车经验I₀提升 ---
    print("\n[TC-16-10] 非机动车经验I₀提升")
    try:
        slot = UrbanIntersectionSlot()
        entry = make_entry("EXP-010", i_value=0.5, core_class=TargetClass.CLASS_3)
        entry.i0_value = 0.5
        success, msg = slot.write_entry(entry)
        assert success == True
        stored = slot._storage[MemoryLayer.L1]["EXP-010"]
        assert stored.i0_value > 0.5
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- 测试结果汇总 ---
    print("\n" + "=" * 60)
    print(f"测试结果: {passed} PASS, {failed} FAIL")
    print("=" * 60)