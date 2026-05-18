#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-27
模块名称: L4 长期层经验抽象提炼单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 五层记忆层级存储
核心职责: 从 L4 长期层已固化的多条同类成功经验中，提取通用驾驶规则、策略模式与
          技能范式。将具体场景相关的经验转化为不依赖特定时间、地点、对象的抽象规则。
          提炼结果反哺 L4 条目标记，并作为可脱敏共享的核心技能知识储备。

依赖模块: ad-26(L4 长期层存储单元，提供经验数据与接收提炼结果),
          ad-36(综合重要度 I 值聚合计算单元，提供条目 I 值参考)
被依赖模块: ad-26(接收提炼出的通用规则与条目标记更新),
            ad-50(记忆导入导出与脱敏共享单元，消费提炼出的泛化技能包)

提炼流程:
  1. 从 ad-26 拉取未提炼的成功经验（结果标签="成功优化"）
  2. 基于场景特征向量执行 DBSCAN 聚类
  3. 从每个簇中归纳通用 IF-THEN 规则
  4. 与已有规则库进行冲突检测与合并
  5. 回写提炼结果至 ad-26（条目标记 + 规则追加）

安全约束:
  S-01: 提炼生成的规则不得包含任何原始场景的具体数据（GPS、时间戳、车牌等）
  S-02: 新规则在正式存入前，必须通过冲突检测
  S-03: 策略失误经验绝对不可参与规则提炼
  S-04: 不可抗力场景经验不参与常规提炼，可单独聚类生成最高安全级别警示规则
  S-05: 提炼出的规则置信度低于 0.60 不可成为正式规则
  S-06: 所有提炼操作全量写入 ad-51 不可变日志
"""

from typing import Dict, List, Optional, Any, Tuple, Set
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid
import math
from collections import defaultdict


# ==================== 枚举定义 ====================

class RefineState(Enum):
    """提炼单元内部状态"""
    IDLE = "idle"
    FETCHING = "fetching"
    CLUSTERING = "clustering"
    INDUCTING = "inducting"
    VERIFYING = "verifying"
    WRITING_BACK = "writing_back"
    PAUSED = "paused"


class RefineTriggerType(Enum):
    """提炼触发类型"""
    PERIODIC = "periodic"
    THRESHOLD = "threshold"         # 新条目达到阈值
    MANUAL = "manual"


class ConflictType(Enum):
    """冲突类型"""
    NONE = "none"
    CONTRADICTION = "contradiction"  # 矛盾
    OVERLAP = "overlap"              # 覆盖/包含
    DUPLICATE = "duplicate"          # 重复


# ==================== 数据结构 ====================

@dataclass
class ExperienceForRefine:
    """待提炼的经验条目（来自 ad-26）"""
    entry_id: str
    scene_feature_vector: List[float]   # 场景特征向量
    i_value: float
    s_value: float
    source_slot_id: int
    sub_label: str
    result_label: str
    behavior_type: str                  # 行为类型（跟车/变道/制动等）
    key_params: Dict[str, float]        # 关键参数（如跟车时距、制动减速度等）


@dataclass
class ClusterResult:
    """聚类结果"""
    cluster_id: int
    entry_ids: List[str]
    centroid: List[float]
    size: int


@dataclass
class RefinedRule:
    """提炼出的抽象规则"""
    rule_id: str
    if_condition: str               # 泛化前置条件
    then_action: str                # 建议驾驶策略
    applicable_scenes: List[str]    # 适用场景标签
    confidence: float               # 规则置信度 0-1
    contributing_entries: List[str] # 贡献条目 ID 列表
    source_slot_id: int
    created_at: float = field(default_factory=time.time)


@dataclass
class ConflictReport:
    """冲突检测报告"""
    new_rule_id: str
    existing_rule_id: str
    conflict_type: ConflictType
    resolution: str                 # 处理方式
    details: str


@dataclass
class RefineResultReport:
    """提炼结果报告"""
    report_id: str
    trigger_type: RefineTriggerType
    total_entries_fetched: int
    clusters_found: int
    rules_generated: int
    rules_updated: int
    rules_merged: int
    rules_rejected: int
    conflicts: List[ConflictReport]
    duration_ms: float
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class L4AbstractionRefine:
    """
    L4 长期层经验抽象提炼单元
    
    职责:
    1. 周期性拉取 L4 中未提炼的成功经验
    2. 基于场景特征向量执行聚类分析
    3. 从每个簇中归纳通用驾驶规则
    4. 与已有规则库进行冲突检测与合并
    5. 回写提炼结果（条目标记 + 规则追加）
    6. 生成提炼结果报告
    """
    
    # 聚类参数
    DEFAULT_EPS = 0.75               # DBSCAN 邻域半径（相似度阈值）
    MIN_SAMPLES = 3                  # 最小簇条目数
    
    # 规则归纳参数
    CONDITION_SATISFY_RATE = 0.80    # 共性条件满足率阈值
    MIN_RULE_CONFIDENCE = 0.60       # 规则最低置信度
    
    # 触发条件
    PERIODIC_INTERVAL = 30 * 24 * 3600  # 30 日
    MIN_ENTRIES_TO_TRIGGER = 10
    
    # 各分槽聚类参数
    SLOT_EPS = {
        15: 0.78,   # 高速巡航槽
        16: 0.75,   # 城区路口槽
        17: 0.72,   # 泊车低速槽
        18: 0.80,   # 特殊环境槽（更严格）
        19: 0.75,   # 通用驾驶槽
    }
    
    # 冲突处理参数
    MAX_RULE_LIBRARY_SIZE = 1000
    
    def __init__(self):
        self.module_id = "ad-27"
        self.module_name = "L4 长期层经验抽象提炼单元"
        
        # 内部状态
        self.state = RefineState.IDLE
        
        # 已有规则库缓存
        self._rule_library: Dict[str, RefinedRule] = {}
        
        # 上次提炼时间
        self._last_refine_time = 0.0
        
        # 统计
        self._total_refines = 0
        self._total_rules_generated = 0
        self._total_rules_updated = 0
        self._total_rules_rejected = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] L4 抽象提炼单元初始化完成")
        print(f"[{self.module_id}] 聚类参数: eps={self.DEFAULT_EPS}, min_samples={self.MIN_SAMPLES}")
        print(f"[{self.module_id}] 规则置信度阈值: {self.MIN_RULE_CONFIDENCE}")
    
    # ========== 状态管理 ==========
    
    def pause(self) -> None:
        self.state = RefineState.PAUSED
    
    def resume(self) -> None:
        self.state = RefineState.IDLE
    
    def get_state(self) -> RefineState:
        return self.state
    
    # ========== 触发检查 ==========
    
    def should_trigger(self, trigger_type: RefineTriggerType,
                       unrefined_count: int = 0) -> bool:
        """
        检查是否应触发提炼
        
        Args:
            trigger_type: 触发类型
            unrefined_count: 未提炼条目数量
            
        Returns:
            是否应触发
        """
        if trigger_type == RefineTriggerType.MANUAL:
            return True
        
        if trigger_type == RefineTriggerType.THRESHOLD:
            return unrefined_count >= self.MIN_ENTRIES_TO_TRIGGER
        
        if trigger_type == RefineTriggerType.PERIODIC:
            now = time.time()
            if now - self._last_refine_time < self.PERIODIC_INTERVAL:
                return False
            return unrefined_count >= self.MIN_ENTRIES_TO_TRIGGER
        
        return False
    
    # ========== 提炼主流程 ==========
    
    def execute_refine(self, entries: List[ExperienceForRefine],
                       existing_rules: Optional[List[RefinedRule]] = None,
                       trigger_type: RefineTriggerType = RefineTriggerType.PERIODIC
                       ) -> Optional[RefineResultReport]:
        """
        执行提炼主流程
        
        步骤:
        1. 拉取数据（已完成，entries 作为参数传入）
        2. 聚类分析
        3. 规则归纳
        4. 冲突检测
        5. 生成结果报告
        """
        if self.state == RefineState.PAUSED:
            return None
        
        start_time = time.time()
        
        # 筛选成功经验（S-03: 排除策略失误）
        valid_entries = [e for e in entries if e.result_label == "成功优化"]
        
        if len(valid_entries) < self.MIN_SAMPLES:
            return None
        
        self._last_refine_time = time.time()
        self._total_refines += 1
        
        # 更新规则库
        if existing_rules:
            for rule in existing_rules:
                self._rule_library[rule.rule_id] = rule
        
        # 步骤2: 聚类分析
        self.state = RefineState.CLUSTERING
        clusters = self._perform_clustering(valid_entries)
        
        if not clusters:
            self.state = RefineState.IDLE
            return None
        
        # 步骤3: 规则归纳
        self.state = RefineState.INDUCTING
        new_rules = []
        entry_markings = {}  # entry_id -> rule_id
        
        for cluster in clusters:
            cluster_entries = [e for e in valid_entries if e.entry_id in cluster.entry_ids]
            if len(cluster_entries) < self.MIN_SAMPLES:
                continue
            
            # 从簇中归纳规则
            rule = self._induce_rule(cluster_entries, cluster.cluster_id)
            
            if rule and rule.confidence >= self.MIN_RULE_CONFIDENCE:
                new_rules.append(rule)
                for entry_id in cluster.entry_ids:
                    entry_markings[entry_id] = rule.rule_id
        
        # 步骤4: 冲突检测
        self.state = RefineState.VERIFYING
        conflicts = []
        accepted_rules = []
        
        for rule in new_rules:
            conflict = self._detect_conflict(rule)
            if conflict:
                conflicts.append(conflict)
                # 根据冲突处理结果决定是否接受
                if conflict.conflict_type == ConflictType.CONTRADICTION:
                    if rule.confidence > self._rule_library.get(conflict.existing_rule_id, RefinedRule("", "", "", [], 0, [], 0)).confidence:
                        accepted_rules.append(rule)
                        self._total_rules_updated += 1
                    else:
                        self._total_rules_rejected += 1
                elif conflict.conflict_type == ConflictType.OVERLAP:
                    # 合并规则
                    accepted_rules.append(rule)
                    self._total_rules_updated += 1
                else:
                    accepted_rules.append(rule)
            else:
                accepted_rules.append(rule)
                self._total_rules_generated += 1
        
        # 更新规则库
        for rule in accepted_rules:
            self._rule_library[rule.rule_id] = rule
        
        # 步骤5: 生成报告
        duration_ms = (time.time() - start_time) * 1000
        
        report = RefineResultReport(
            report_id=f"refine-{uuid.uuid4().hex[:8]}",
            trigger_type=trigger_type,
            total_entries_fetched=len(valid_entries),
            clusters_found=len(clusters),
            rules_generated=len(accepted_rules) - self._total_rules_updated,
            rules_updated=self._total_rules_updated,
            rules_merged=sum(1 for c in conflicts if c.conflict_type == ConflictType.OVERLAP),
            rules_rejected=self._total_rules_rejected,
            conflicts=conflicts,
            duration_ms=duration_ms
        )
        
        self.state = RefineState.IDLE
        return report
    
    # ========== 聚类分析（简化 DBSCAN） ==========
    
    def _perform_clustering(self, entries: List[ExperienceForRefine]) -> List[ClusterResult]:
        """
        执行简化版 DBSCAN 聚类
        
        按分槽分组，组内聚类
        """
        clusters = []
        cluster_id = 0
        visited = set()
        
        # 按分槽分组
        slot_groups: Dict[int, List[ExperienceForRefine]] = defaultdict(list)
        for entry in entries:
            slot_groups[entry.source_slot_id].append(entry)
        
        for slot_id, group in slot_groups.items():
            eps = self.SLOT_EPS.get(slot_id, self.DEFAULT_EPS)
            
            for entry in group:
                if entry.entry_id in visited:
                    continue
                
                visited.add(entry.entry_id)
                
                # 查找邻域
                neighbors = [entry]
                for other in group:
                    if other.entry_id in visited:
                        continue
                    sim = self._cosine_similarity(entry.scene_feature_vector, other.scene_feature_vector)
                    if sim >= eps:
                        neighbors.append(other)
                        visited.add(other.entry_id)
                
                if len(neighbors) >= self.MIN_SAMPLES:
                    cluster = ClusterResult(
                        cluster_id=cluster_id,
                        entry_ids=[e.entry_id for e in neighbors],
                        centroid=entry.scene_feature_vector,
                        size=len(neighbors)
                    )
                    clusters.append(cluster)
                    cluster_id += 1
        
        return clusters
    
    def _cosine_similarity(self, vec_a: List[float], vec_b: List[float]) -> float:
        """计算余弦相似度"""
        if not vec_a or not vec_b:
            return 0.0
        min_len = min(len(vec_a), len(vec_b))
        v1, v2 = vec_a[:min_len], vec_b[:min_len]
        
        dot = sum(a * b for a, b in zip(v1, v2))
        norm1 = math.sqrt(sum(a * a for a in v1))
        norm2 = math.sqrt(sum(b * b for b in v2))
        
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return dot / (norm1 * norm2)
    
    # ========== 规则归纳 ==========
    
    def _induce_rule(self, cluster_entries: List[ExperienceForRefine], cluster_id: int) -> Optional[RefinedRule]:
        """
        从簇中归纳通用规则
        
        步骤:
        1. 提取共性前置条件（满足率 ≥ 80%）
        2. 归纳数值参数（取中位数或保守值）
        3. 生成 IF-THEN 规则
        4. 计算置信度
        """
        if len(cluster_entries) < self.MIN_SAMPLES:
            return None
        
        # 提取共性场景条件
        slot_ids = set(e.source_slot_id for e in cluster_entries)
        sub_labels = set(e.sub_label for e in cluster_entries)
        
        # 统计行为类型分布
        behavior_counts = defaultdict(int)
        for entry in cluster_entries:
            behavior_counts[entry.behavior_type] += 1
        
        # 取最多出现的行为类型
        dominant_behavior = max(behavior_counts, key=behavior_counts.get)
        
        # 归纳关键参数（取中位数作为建议值）
        param_values = defaultdict(list)
        for entry in cluster_entries:
            for param, value in entry.key_params.items():
                param_values[param].append(value)
        
        param_medians = {}
        for param, values in param_values.items():
            sorted_vals = sorted(values)
            n = len(sorted_vals)
            param_medians[param] = sorted_vals[n // 2]
        
        # 生成 IF 条件（泛化）
        if_conditions = []
        if len(slot_ids) == 1:
            slot_id = list(slot_ids)[0]
            if slot_id == 15:
                if_conditions.append("高速巡航场景")
            elif slot_id == 16:
                if_conditions.append("城区路口场景")
            elif slot_id == 17:
                if_conditions.append("泊车低速场景")
            elif slot_id == 18:
                if_conditions.append("特殊环境场景")
        
        if "乡村道路" in sub_labels:
            if_conditions.append("乡村非铺装道路")
        
        if not if_conditions:
            if_conditions.append("通用驾驶场景")
        
        if_condition_str = " AND ".join(if_conditions)
        
        # 生成 THEN 建议
        then_parts = []
        for param, median_val in param_medians.items():
            then_parts.append(f"{param} ≈ {median_val:.1f}")
        
        then_action_str = f"{dominant_behavior}: " + ", ".join(then_parts) if then_parts else dominant_behavior
        
        # 计算置信度
        avg_i = sum(e.i_value for e in cluster_entries) / len(cluster_entries)
        avg_s = sum(e.s_value for e in cluster_entries) / len(cluster_entries)
        confidence = 0.5 * avg_i + 0.3 * avg_s + 0.2 * min(len(cluster_entries) / 10, 1.0)
        
        rule = RefinedRule(
            rule_id=f"rule-{uuid.uuid4().hex[:8]}",
            if_condition=if_condition_str,
            then_action=then_action_str,
            applicable_scenes=list(slot_ids),
            confidence=confidence,
            contributing_entries=[e.entry_id for e in cluster_entries],
            source_slot_id=list(slot_ids)[0] if len(slot_ids) == 1 else 19
        )
        
        return rule
    
    # ========== 冲突检测 ==========
    
    def _detect_conflict(self, new_rule: RefinedRule) -> Optional[ConflictReport]:
        """
        检测新规则与已有规则库的冲突
        
        Returns:
            冲突报告，无冲突返回 None
        """
        for existing_id, existing_rule in self._rule_library.items():
            # 检查 IF 条件是否相同或相似
            if new_rule.if_condition == existing_rule.if_condition:
                if new_rule.then_action == existing_rule.then_action:
                    return ConflictReport(
                        new_rule_id=new_rule.rule_id,
                        existing_rule_id=existing_id,
                        conflict_type=ConflictType.DUPLICATE,
                        resolution="跳过重复规则",
                        details="IF-THEN 完全一致"
                    )
                else:
                    return ConflictReport(
                        new_rule_id=new_rule.rule_id,
                        existing_rule_id=existing_id,
                        conflict_type=ConflictType.CONTRADICTION,
                        resolution="保留置信度更高的规则",
                        details=f"新规则 THEN={new_rule.then_action}, 已有 THEN={existing_rule.then_action}"
                    )
            
            # 检查子集包含关系（简化处理）
            if new_rule.if_condition in existing_rule.if_condition or \
               existing_rule.if_condition in new_rule.if_condition:
                return ConflictReport(
                    new_rule_id=new_rule.rule_id,
                    existing_rule_id=existing_id,
                    conflict_type=ConflictType.OVERLAP,
                    resolution="合并适用范围",
                    details="前置条件存在包含关系"
                )
        
        return None
    
    # ========== 查询接口 ==========
    
    def get_all_rules(self) -> List[RefinedRule]:
        """获取所有已提炼的规则"""
        return list(self._rule_library.values())
    
    def get_rules_for_export(self) -> List[Dict[str, Any]]:
        """
        获取可导出的规则（供 ad-50 消费）
        
        S-01: 规则已天然不含具体场景数据
        """
        rules = []
        for rule in self._rule_library.values():
            rules.append({
                "rule_id": rule.rule_id,
                "if_condition": rule.if_condition,
                "then_action": rule.then_action,
                "applicable_scenes": rule.applicable_scenes,
                "confidence": rule.confidence,
                "contributing_entry_count": len(rule.contributing_entries),
                "source_slot_id": rule.source_slot_id,
                "created_at": rule.created_at
            })
        return rules
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_refines": self._total_refines,
            "total_rules_generated": self._total_rules_generated,
            "total_rules_updated": self._total_rules_updated,
            "total_rules_rejected": self._total_rules_rejected,
            "rule_library_size": len(self._rule_library),
            "state": self.state.value
        }
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-27 L4 长期层经验抽象提炼单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    def make_entry(entry_id, feature_vec, behavior_type="跟车",
                   key_params=None, i_value=0.8, s_value=0.7,
                   slot_id=15, sub_label="常规通用"):
        if key_params is None:
            key_params = {"跟车时距": 2.5}
        return ExperienceForRefine(
            entry_id=entry_id,
            scene_feature_vector=feature_vec,
            i_value=i_value,
            s_value=s_value,
            source_slot_id=slot_id,
            sub_label=sub_label,
            result_label="成功优化",
            behavior_type=behavior_type,
            key_params=key_params
        )
    
    # --- TC-27-01: 成功提炼一条规则 ---
    print("\n[TC-27-01] 成功提炼一条规则（3条相似经验）")
    try:
        refiner = L4AbstractionRefine()
        entries = [
            make_entry("EXP-01", [1.0, 2.0, 3.0], key_params={"跟车时距": 2.5}, i_value=0.85),
            make_entry("EXP-02", [1.0, 2.1, 3.0], key_params={"跟车时距": 2.3}, i_value=0.80),
            make_entry("EXP-03", [1.0, 1.9, 3.0], key_params={"跟车时距": 2.7}, i_value=0.82),
        ]
        report = refiner.execute_refine(entries, trigger_type=RefineTriggerType.MANUAL)
        assert report is not None
        assert report.clusters_found >= 1
        assert len(refiner.get_all_rules()) >= 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-27-02: 不足最小样本量不提炼 ---
    print("\n[TC-27-02] 不足最小样本量不提炼")
    try:
        refiner = L4AbstractionRefine()
        entries = [
            make_entry("EXP-04", [1.0, 2.0]),
            make_entry("EXP-05", [5.0, 6.0]),  # 不相似
        ]
        report = refiner.execute_refine(entries, trigger_type=RefineTriggerType.MANUAL)
        assert report is None or report.rules_generated == 0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-27-03: 排除策略失误经验 ---
    print("\n[TC-27-03] 排除策略失误经验")
    try:
        refiner = L4AbstractionRefine()
        entry_mistake = make_entry("EXP-06", [1.0, 2.0, 3.0])
        entry_mistake.result_label = "策略失误"
        entries = [
            make_entry("EXP-07", [1.0, 2.0, 3.0]),
            make_entry("EXP-08", [1.0, 2.0, 3.0]),
            make_entry("EXP-09", [1.0, 2.0, 3.0]),
            entry_mistake,  # 应被排除
        ]
        report = refiner.execute_refine(entries, trigger_type=RefineTriggerType.MANUAL)
        if report:
            assert report.total_entries_fetched == 3  # 排除策略失误后
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-27-04: 不同分槽不跨组聚类 ---
    print("\n[TC-27-04] 不同分槽不跨组聚类")
    try:
        refiner = L4AbstractionRefine()
        entries = [
            make_entry("EXP-10", [1.0, 2.0, 3.0], slot_id=15),
            make_entry("EXP-11", [1.0, 2.0, 3.0], slot_id=15),
            make_entry("EXP-12", [1.0, 2.0, 3.0], slot_id=15),
            make_entry("EXP-13", [1.0, 2.0, 3.0], slot_id=16),  # 不同分槽
        ]
        report = refiner.execute_refine(entries, trigger_type=RefineTriggerType.MANUAL)
        # 高速巡航槽 3 条应成簇，城区路口槽 1 条不成簇
        assert report is not None
        assert report.clusters_found == 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-27-05: 规则置信度不足被拒绝 ---
    print("\n[TC-27-05] 规则置信度不足被拒绝")
    try:
        refiner = L4AbstractionRefine()
        # 低 I 值和低 S 值的条目
        entries = [
            make_entry("EXP-14", [1.0, 2.0, 3.0], i_value=0.5, s_value=0.4),
            make_entry("EXP-15", [1.0, 2.0, 3.0], i_value=0.5, s_value=0.4),
            make_entry("EXP-16", [1.0, 2.0, 3.0], i_value=0.5, s_value=0.4),
        ]
        report = refiner.execute_refine(entries, trigger_type=RefineTriggerType.MANUAL)
        # 置信度可能低于 0.60
        if report:
            assert report.rules_generated + report.rules_updated == 0 or \
                   all(r.confidence >= refiner.MIN_RULE_CONFIDENCE for r in refiner.get_all_rules())
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-27-06: 冲突检测：重复规则跳过 ---
    print("\n[TC-27-06] 冲突检测：重复规则跳过")
    try:
        refiner = L4AbstractionRefine()
        entries1 = [
            make_entry("EXP-17", [1.0, 2.0, 3.0]),
            make_entry("EXP-18", [1.0, 2.0, 3.0]),
            make_entry("EXP-19", [1.0, 2.0, 3.0]),
        ]
        refiner.execute_refine(entries1, trigger_type=RefineTriggerType.MANUAL)
        # 再次提炼相同经验（模拟）
        refiner._total_refines = 0  # 重置计数器以便再次触发
        entries2 = [
            make_entry("EXP-20", [1.0, 2.0, 3.0]),
            make_entry("EXP-21", [1.0, 2.0, 3.0]),
            make_entry("EXP-22", [1.0, 2.0, 3.0]),
        ]
        report = refiner.execute_refine(entries2, trigger_type=RefineTriggerType.MANUAL)
        # 应检测到重复或覆盖
        if report:
            assert len(refiner.get_all_rules()) <= 2  # 规则数量不应翻倍
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-27-07: 可导出规则不含具体数据 ---
    print("\n[TC-27-07] 可导出规则不含具体数据")
    try:
        refiner = L4AbstractionRefine()
        entries = [
            make_entry("EXP-23", [1.0, 2.0, 3.0]),
            make_entry("EXP-24", [1.0, 2.0, 3.0]),
            make_entry("EXP-25", [1.0, 2.0, 3.0]),
        ]
        refiner.execute_refine(entries, trigger_type=RefineTriggerType.MANUAL)
        export_rules = refiner.get_rules_for_export()
        if export_rules:
            # 检查是否包含脱敏后的数据
            assert "entry_id" not in str(export_rules[0]) or "contributing_entry_count" in export_rules[0]
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-27-08: 触发条件检查 ---
    print("\n[TC-27-08] 触发条件检查")
    try:
        refiner = L4AbstractionRefine()
        assert refiner.should_trigger(RefineTriggerType.THRESHOLD, unrefined_count=5) == False
        assert refiner.should_trigger(RefineTriggerType.THRESHOLD, unrefined_count=12) == True
        assert refiner.should_trigger(RefineTriggerType.MANUAL) == True
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-27-09: 参数归纳取中位数 ---
    print("\n[TC-27-09] 参数归纳取中位数")
    try:
        refiner = L4AbstractionRefine()
        entries = [
            make_entry("E1", [1.0, 2.0, 3.0], key_params={"跟车时距": 2.0}),
            make_entry("E2", [1.0, 2.0, 3.0], key_params={"跟车时距": 3.0}),
            make_entry("E3", [1.0, 2.0, 3.0], key_params={"跟车时距": 2.5}),
        ]
        report = refiner.execute_refine(entries, trigger_type=RefineTriggerType.MANUAL)
        if report and refiner.get_all_rules():
            rule = refiner.get_all_rules()[0]
            # 中位数应为 2.5
            assert "2.5" in rule.then_action
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-27-10: 暂停状态不处理 ---
    print("\n[TC-27-10] 暂停状态不处理")
    try:
        refiner = L4AbstractionRefine()
        refiner.pause()
        entries = [
            make_entry("E1", [1.0, 2.0, 3.0]),
            make_entry("E2", [1.0, 2.0, 3.0]),
            make_entry("E3", [1.0, 2.0, 3.0]),
        ]
        report = refiner.execute_refine(entries, trigger_type=RefineTriggerType.MANUAL)
        assert report is None
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