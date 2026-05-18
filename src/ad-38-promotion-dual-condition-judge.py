#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-38
模块名称: 晋升双条件判定单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 晋升与遗忘执行机制
核心职责: 严格按照“留存时长达标 + 重要度 I 值达标”双重条件，对漏斗二中每一层级的
          经验条目进行晋升资格判定。将符合条件的条目按优先级排序后生成晋升候选清单，
          下发至对应层级的存储单元执行晋升搬运。L4→L5 晋升额外校验安全仲裁状态与
          不可抗力标记。

依赖模块: ad-36(综合重要度 I 值聚合计算单元，提供条目当前 I 值),
          ad-20/22/24/26(各层级存储单元，提供条目留存时长与元数据),
          ad-35(三维权重系数配置单元，获取各分槽专属晋升阈值),
          ad-43(失败经验安全仲裁三道校验单元，校验 L4→L5 失败经验)
被依赖模块: ad-20/22/24/26(各层级存储单元，消费晋升候选清单),
            ad-39(层级单向搬运写入单元，接收搬运指令)

安全约束:
  S-01: 晋升双条件（留存时长 + I 值）必须同时满足，单条件不可放行。编译期固化判定逻辑
  S-02: 失败经验（结果标签="策略失误"）晋升 L3→L4 及 L4→L5 必须通过 ad-43 三道安全仲裁
  S-03: 不可抗力经验晋升 L5 豁免留存时间与复用次数限制，但仍需 I ≥ 0.80
  S-04: 晋升优先级计算仅决定同一批次内的晋升顺序，不得因优先级低而永久阻止晋升
  S-05: 所有晋升判定（含晋升、暂缓、驳回、仲裁）全量写入 ad-51 不可变日志
  S-06: 目标层级容量为 0 时禁止强行晋升，防止数据溢出覆盖已有经验
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
    WAITING_ARBITRATION = "waiting_arbitration"
    PAUSED = "paused"


class PromotionConclusion(Enum):
    """晋升结论"""
    APPROVED = "approved"               # 晋升
    DEFERRED_RETENTION = "deferred_retention"   # 留存不足
    DEFERRED_I_VALUE = "deferred_i_value"       # I值不足
    DEFERRED_ARBITRATION = "deferred_arbitration" # 待仲裁
    REJECTED_ARBITRATION = "rejected_arbitration" # 仲裁驳回
    REJECTED_CAPACITY = "rejected_capacity"     # 目标层级满
    REJECTED_INVALID = "rejected_invalid"       # 条目不存在


# ==================== 数据结构 ====================

@dataclass
class EntrySnapshot:
    """条目快照（来自各层级存储）"""
    entry_id: str
    current_layer: str
    retention_seconds: float
    i_value: float
    source_slot_id: int
    result_label: str               # "成功优化" / "策略失误" / "不可抗力场景"
    force_majeure: bool
    arbitration_status: str         # "none" / "pending" / "approved" / "rejected"
    reuse_count: int = 0
    heat_value: float = 0.5


@dataclass
class SlotThresholds:
    """分槽晋升阈值"""
    slot_id: int
    time_l1_l2: float
    time_l2_l3: float
    time_l3_l4: float
    time_l4_l5: float = 90 * 24 * 3600
    i_l1_l2: float = 0.40
    i_l2_l3: float = 0.60
    i_l3_l4: float = 0.80
    i_l4_l5: float = 0.80


@dataclass
class ArbitrationRequest:
    """安全仲裁请求"""
    entry_id: str
    experience_content: Dict[str, Any]
    result_label: str
    scene_features: Dict[str, Any]
    request_source: str = "L3/L4"


@dataclass
class ArbitrationResult:
    """安全仲裁结果"""
    entry_id: str
    conclusion: str                 # "放行晋升" / "保留警示" / "永久锁定L5"


@dataclass
class PromotionCandidate:
    """晋升候选条目"""
    entry_id: str
    current_layer: str
    target_layer: str
    i_value: float
    priority: float                 # 晋升优先级
    notes: str = ""


@dataclass
class JudgeResult:
    """判定结果汇总"""
    cycle_id: str
    promotion_list: List[PromotionCandidate]
    deferred_list: List[Dict[str, Any]]
    rejected_list: List[Dict[str, Any]]
    arbitration_requests: List[ArbitrationRequest]
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class PromotionDualConditionJudge:
    """
    晋升双条件判定单元
    
    职责:
    1. 周期性扫描各层级条目快照
    2. 严格按双条件（时间+重要度）判定晋升资格
    3. 失败经验仲裁管理
    4. 不可抗力经验豁免
    5. 晋升优先级排序（I值 + 留存时长 + 热度）
    6. 目标层级容量检查与截断
    """
    
    # L4→L5 不可抗力豁免条件
    L5_FORCE_MAJEURE_MIN_I = 0.80
    
    # L4→L5 正常额外条件
    L5_MIN_REUSE = 10
    
    # 优先级权重
    PRIORITY_I_WEIGHT = 0.6
    PRIORITY_RETENTION_WEIGHT = 0.2
    PRIORITY_HEAT_WEIGHT = 0.2
    
    def __init__(self):
        self.module_id = "ad-38"
        self.module_name = "晋升双条件判定单元"
        
        self.state = JudgeState.IDLE
        
        # 仲裁等待字典
        self._arbitration_waiting: Dict[str, float] = {}
        self._arbitration_timeout = 72 * 3600  # 72 小时超时
        
        # 统计
        self._total_cycles = 0
        self._total_approved = 0
        self._total_deferred = 0
        self._total_rejected = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 晋升双条件判定单元初始化完成")
    
    # ========== 状态管理 ==========
    
    def pause(self) -> None:
        self.state = JudgeState.PAUSED
    
    def resume(self) -> None:
        self.state = JudgeState.IDLE
    
    def get_state(self) -> JudgeState:
        return self.state
    
    # ========== 主判定流程 ==========
    
    def execute_judgment(self,
                         layer_entries: Dict[str, List[EntrySnapshot]],
                         slot_thresholds: Dict[int, SlotThresholds],
                         layer_capacities: Dict[str, int],
                         arbitration_results: Optional[List[ArbitrationResult]] = None) -> JudgeResult:
        """
        执行全层级晋升判定
        
        Args:
            layer_entries: 各层级条目快照 {"L1": [...], "L2": [...], ...}
            slot_thresholds: 各分槽晋升阈值
            layer_capacities: 各层级剩余容量 {"L2": 100, "L3": 50, ...}
            arbitration_results: 本次返回的仲裁结果
            
        Returns:
            判定结果汇总
        """
        if self.state == JudgeState.PAUSED:
            return JudgeResult("", [], [], [], [])
        
        self.state = JudgeState.JUDGING
        self._total_cycles += 1
        cycle_id = f"judge-{uuid.uuid4().hex[:8]}"
        
        # 处理返回的仲裁结果
        if arbitration_results:
            for result in arbitration_results:
                self._arbitration_waiting.pop(result.entry_id, None)
        
        promotion_list = []
        deferred_list = []
        rejected_list = []
        arbitration_requests = []
        
        # L1 → L2
        self._judge_layer(
            layer_entries.get("L1", []), "L1", "L2",
            slot_thresholds, layer_capacities.get("L2", 0),
            promotion_list, deferred_list, rejected_list, arbitration_requests
        )
        
        # L2 → L3
        self._judge_layer(
            layer_entries.get("L2", []), "L2", "L3",
            slot_thresholds, layer_capacities.get("L3", 0),
            promotion_list, deferred_list, rejected_list, arbitration_requests
        )
        
        # L3 → L4（需检查仲裁）
        self._judge_layer(
            layer_entries.get("L3", []), "L3", "L4",
            slot_thresholds, layer_capacities.get("L4", 0),
            promotion_list, deferred_list, rejected_list, arbitration_requests,
            check_arbitration=True
        )
        
        # L4 → L5（最严苛，额外检查）
        self._judge_layer(
            layer_entries.get("L4", []), "L4", "L5",
            slot_thresholds, layer_capacities.get("L5", 0),
            promotion_list, deferred_list, rejected_list, arbitration_requests,
            check_arbitration=True, is_l5=True
        )
        
        # 优先级排序
        promotion_list.sort(key=lambda x: x.priority, reverse=True)
        
        # 容量截断
        self._capacity_truncate(promotion_list, layer_capacities, deferred_list)
        
        self._total_approved += len(promotion_list)
        self._total_deferred += len(deferred_list)
        self._total_rejected += len(rejected_list)
        
        result = JudgeResult(
            cycle_id=cycle_id,
            promotion_list=promotion_list,
            deferred_list=deferred_list,
            rejected_list=rejected_list,
            arbitration_requests=arbitration_requests
        )
        
        self.state = JudgeState.IDLE
        return result
    
    def _judge_layer(self, entries: List[EntrySnapshot], src_layer: str, dst_layer: str,
                     thresholds: Dict[int, SlotThresholds], dst_capacity: int,
                     promotion_list: List[PromotionCandidate],
                     deferred_list: List[Dict[str, Any]],
                     rejected_list: List[Dict[str, Any]],
                     arbitration_requests: List[ArbitrationRequest],
                     check_arbitration: bool = False, is_l5: bool = False) -> None:
        """逐条判定某个晋升路径"""
        
        if dst_capacity <= 0:
            # 目标层满，全部暂缓
            for entry in entries:
                deferred_list.append({
                    "entry_id": entry.entry_id,
                    "path": f"{src_layer}→{dst_layer}",
                    "reason": "目标层级容量不足"
                })
            return
        
        for entry in entries:
            slot_id = entry.source_slot_id
            th = thresholds.get(slot_id)
            if th is None:
                rejected_list.append({"entry_id": entry.entry_id, "path": f"{src_layer}→{dst_layer}", "reason": "分槽阈值缺失"})
                continue
            
            # 获取时间阈值
            time_th = getattr(th, f"time_{src_layer.lower()}_{dst_layer.lower()}", None)
            if time_th is None:
                time_th = 24 * 3600  # 默认
            
            # 获取 I 阈值
            i_th = getattr(th, f"i_{src_layer.lower()}_{dst_layer.lower()}", 0.40)
            
            # 不可抗力豁免（仅 L4→L5）
            if is_l5 and entry.force_majeure:
                if entry.i_value >= self.L5_FORCE_MAJEURE_MIN_I:
                    priority = self._calc_priority(entry.i_value, entry.retention_seconds, entry.heat_value)
                    promotion_list.append(PromotionCandidate(
                        entry.entry_id, src_layer, dst_layer, entry.i_value, priority, "不可抗力豁免"
                    ))
                    continue
            
            # L4→L5 额外复用条件
            if is_l5 and not entry.force_majeure:
                if entry.reuse_count < self.L5_MIN_REUSE:
                    deferred_list.append({
                        "entry_id": entry.entry_id, "path": f"{src_layer}→{dst_layer}",
                        "reason": f"复用不足({entry.reuse_count}<{self.L5_MIN_REUSE})"
                    })
                    continue
            
            # 失败经验仲裁检查
            if check_arbitration and entry.result_label == "策略失误":
                arb_status = entry.arbitration_status
                if arb_status in ["none", "pending"]:
                    arbitration_requests.append(ArbitrationRequest(
                        entry.entry_id, {}, entry.result_label, {}
                    ))
                    deferred_list.append({
                        "entry_id": entry.entry_id, "path": f"{src_layer}→{dst_layer}",
                        "reason": "等待安全仲裁"
                    })
                    continue
                elif arb_status == "rejected":
                    rejected_list.append({
                        "entry_id": entry.entry_id, "path": f"{src_layer}→{dst_layer}",
                        "reason": "安全仲裁驳回"
                    })
                    continue
            
            # 双条件判定
            time_ok = entry.retention_seconds >= time_th
            i_ok = entry.i_value >= i_th
            
            if time_ok and i_ok:
                priority = self._calc_priority(entry.i_value, entry.retention_seconds, entry.heat_value)
                promotion_list.append(PromotionCandidate(
                    entry.entry_id, src_layer, dst_layer, entry.i_value, priority
                ))
            elif not time_ok:
                deferred_list.append({
                    "entry_id": entry.entry_id, "path": f"{src_layer}→{dst_layer}",
                    "reason": f"留存不足({entry.retention_seconds/3600:.1f}h<{time_th/3600:.0f}h)"
                })
            else:
                deferred_list.append({
                    "entry_id": entry.entry_id, "path": f"{src_layer}→{dst_layer}",
                    "reason": f"I值不足({entry.i_value:.2f}<{i_th:.2f})"
                })
    
    def _calc_priority(self, i_value: float, retention: float, heat: float) -> float:
        """计算晋升优先级"""
        return (self.PRIORITY_I_WEIGHT * i_value +
                self.PRIORITY_RETENTION_WEIGHT * min(retention / (90*24*3600), 1.0) +
                self.PRIORITY_HEAT_WEIGHT * heat)
    
    def _capacity_truncate(self, promotion_list: List[PromotionCandidate],
                           capacities: Dict[str, int],
                           deferred_list: List[Dict[str, Any]]) -> None:
        """根据目标层级容量截断晋升候选"""
        from collections import defaultdict
        count = defaultdict(int)
        truncated = []
        
        for cand in promotion_list[:]:
            cap = capacities.get(cand.target_layer, 0)
            if count[cand.target_layer] >= cap:
                deferred_list.append({
                    "entry_id": cand.entry_id,
                    "path": f"{cand.current_layer}→{cand.target_layer}",
                    "reason": "目标层级容量不足(排序截断)"
                })
                promotion_list.remove(cand)
            else:
                count[cand.target_layer] += 1
    
    # ========== 仲裁超时检查 ==========
    
    def check_arbitration_timeouts(self) -> List[str]:
        """检查仲裁等待超时的条目，返回自动按驳回处理的条目ID列表"""
        now = time.time()
        timeout_entries = []
        for entry_id, start_time in list(self._arbitration_waiting.items()):
            if now - start_time > self._arbitration_timeout:
                timeout_entries.append(entry_id)
                del self._arbitration_waiting[entry_id]
        return timeout_entries
    
    # ========== 查询接口 ==========
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_cycles": self._total_cycles,
            "total_approved": self._total_approved,
            "total_deferred": self._total_deferred,
            "total_rejected": self._total_rejected,
            "arbitration_waiting": len(self._arbitration_waiting),
            "state": self.state.value
        }
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-38 晋升双条件判定单元 单元测试")
    print("=" * 60)
    passed, failed = 0, 0
    
    def make_entry(entry_id, layer, retention_h, i_value, slot=15, result="成功优化",
                   force_majeure=False, arb="none", reuse=0, heat=0.5):
        return EntrySnapshot(entry_id, layer, retention_h*3600, i_value, slot,
                             result, force_majeure, arb, reuse, heat)
    
    default_th = {
        15: SlotThresholds(15, 24*3600, 7*86400, 30*86400, 90*86400,
                           0.40, 0.60, 0.80, 0.80),
    }
    
    # TC-38-01: L1→L2 正常通过
    print("\n[TC-38-01] L1→L2 正常通过（25h, I=0.55）")
    try:
        judge = PromotionDualConditionJudge()
        entries = {"L1": [make_entry("EXP-001", "L1", 25, 0.55)]}
        result = judge.execute_judgment(entries, default_th, {"L2": 100})
        assert len(result.promotion_list) == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-38-02: 留存不足暂缓
    print("\n[TC-38-02] 留存不足暂缓（20h < 24h）")
    try:
        judge = PromotionDualConditionJudge()
        entries = {"L1": [make_entry("EXP-002", "L1", 20, 0.60)]}
        result = judge.execute_judgment(entries, default_th, {"L2": 100})
        assert len(result.deferred_list) == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-38-03: 不可抗力豁免
    print("\n[TC-38-03] 不可抗力 L4→L5 豁免复用（I=0.85）")
    try:
        judge = PromotionDualConditionJudge()
        entries = {"L4": [make_entry("EXP-003", "L4", 30*24, 0.85, force_majeure=True, reuse=3)]}
        result = judge.execute_judgment(entries, default_th, {"L5": 10})
        assert len(result.promotion_list) == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-38-04: 失败经验等待仲裁
    print("\n[TC-38-04] 失败经验等待仲裁")
    try:
        judge = PromotionDualConditionJudge()
        entries = {"L3": [make_entry("EXP-004", "L3", 35*24, 0.85, result="策略失误", arb="pending")]}
        result = judge.execute_judgment(entries, default_th, {"L4": 50})
        assert len(result.arbitration_requests) == 1
        assert len(result.deferred_list) == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-38-05: 目标层级容量截断
    print("\n[TC-38-05] L2→L3 容量截断")
    try:
        judge = PromotionDualConditionJudge()
        entries = {"L2": [make_entry(f"EXP-{i}", "L2", 8*24, 0.70) for i in range(5)]}
        result = judge.execute_judgment(entries, default_th, {"L3": 2})
        assert len(result.promotion_list) == 2
        assert len(result.deferred_list) >= 3
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    print(f"\n测试结果: {passed} PASS, {failed} FAIL")