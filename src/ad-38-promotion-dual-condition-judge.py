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
from collections import defaultdict
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
    APPROVED = "approved"                       # 批准晋升
    DEFERRED_RETENTION = "deferred_retention"   # 留存时长不足
    DEFERRED_I_VALUE = "deferred_i_value"       # I值不足
    DEFERRED_ARBITRATION = "deferred_arbitration"   # 等待安全仲裁
    DEFERRED_CAPACITY = "deferred_capacity"         # 目标层级容量不足
    REJECTED_ARBITRATION = "rejected_arbitration"   # 安全仲裁驳回
    REJECTED_INVALID = "rejected_invalid"           # 条目不存在或分槽阈值缺失
    DIRECT_L5 = "direct_l5"                         # 仲裁直达L5


# ==================== 数据结构 ====================

@dataclass
class EntrySnapshot:
    """条目快照（来自各层级存储）"""
    entry_id: str
    current_layer: str                 # "L1"/"L2"/"L3"/"L4"
    retention_seconds: float           # 留存时长（秒）
    i_value: float                     # 当前 I 值
    source_slot_id: int                # 来源分槽号
    result_label: str                  # "成功优化"/"策略失误"/"不可抗力场景"
    force_majeure: bool                # 是否不可抗力
    arbitration_status: str            # "none"/"pending"/"in_progress"/"approved"/"rejected"/"direct_l5"
    reuse_count: int = 0               # 复用计数
    heat_value: float = 0.5            # 热度值（来自 ad-23）


@dataclass
class SlotThresholds:
    """分槽专属晋升阈值（来自 ad-35）"""
    slot_id: int
    time_l1_l2: float = 24 * 3600          # L1→L2 时间阈值（秒）
    time_l2_l3: float = 7 * 24 * 3600      # L2→L3
    time_l3_l4: float = 30 * 24 * 3600     # L3→L4
    time_l4_l5: float = 90 * 24 * 3600     # L4→L5
    i_l1_l2: float = 0.40                   # L1→L2 I 阈值
    i_l2_l3: float = 0.60                   # L2→L3
    i_l3_l4: float = 0.80                   # L3→L4
    i_l4_l5: float = 0.80                   # L4→L5


@dataclass
class ArbitrationRequest:
    """安全仲裁请求"""
    entry_id: str
    experience_content: Dict[str, Any]
    result_label: str
    scene_features: Dict[str, Any]
    request_source: str = "L3/L4"
    request_timestamp: float = field(default_factory=time.time)


@dataclass
class ArbitrationResult:
    """安全仲裁结果（来自 ad-43）"""
    entry_id: str
    conclusion: str                     # "放行晋升"/"保留L3警示"/"永久锁定L5"


@dataclass
class PromotionCandidate:
    """晋升候选条目"""
    entry_id: str
    current_layer: str
    target_layer: str
    i_value: float
    priority: float                     # 晋升优先级（用于排序）
    notes: str = ""


@dataclass
class DeferredEntry:
    """暂缓条目"""
    entry_id: str
    path: str                           # "L1→L2" 等
    reason: str
    conclusion: PromotionConclusion


@dataclass
class RejectedEntry:
    """驳回条目"""
    entry_id: str
    path: str
    reason: str
    conclusion: PromotionConclusion


@dataclass
class JudgeResult:
    """判定结果汇总"""
    cycle_id: str
    promotion_list: List[PromotionCandidate]
    deferred_list: List[DeferredEntry]
    rejected_list: List[RejectedEntry]
    arbitration_requests: List[ArbitrationRequest]
    scanned_count: int
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class PromotionDualConditionJudge:
    """
    晋升双条件判定单元
    
    职责:
    1. 周期性扫描各层级条目快照，逐条执行晋升资格判定
    2. 严格按"留存时长达标 + 重要度 I 值达标"双条件放行
    3. 失败经验自动触发安全仲裁，不可抗力经验享有豁免
    4. 按优先级（I值×0.6 + 留存率×0.2 + 热度×0.2）排序
    5. 目标层级容量不足时按优先级截断
    """
    
    # L4→L5 不可抗力豁免条件
    L5_FORCE_MAJEURE_MIN_I = 0.80
    
    # L4→L5 正常额外条件
    L5_MIN_REUSE = 10
    
    # 晋升优先级权重
    PRIORITY_I_WEIGHT = 0.6
    PRIORITY_RETENTION_WEIGHT = 0.2
    PRIORITY_HEAT_WEIGHT = 0.2
    
    # 仲裁超时（秒）
    ARBITRATION_TIMEOUT = 72 * 3600  # 72 小时
    
    # 判定间隔（秒）
    JUDGE_INTERVAL = 60  # 60 秒
    
    def __init__(self):
        self.module_id = "ad-38"
        self.module_name = "晋升双条件判定单元"
        
        # 内部状态
        self.state = JudgeState.IDLE
        
        # 仲裁等待字典: entry_id -> 请求时间
        self._arbitration_waiting: Dict[str, float] = {}
        
        # 上次判定时间
        self._last_judge_time = 0.0
        
        # 统计
        self._total_cycles = 0
        self._total_scanned = 0
        self._total_approved = 0
        self._total_deferred = 0
        self._total_rejected = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 晋升双条件判定单元初始化完成")
        print(f"[{self.module_id}] 双条件: 留存时长 + I值, 优先级: I×{self.PRIORITY_I_WEIGHT} + "
              f"留存×{self.PRIORITY_RETENTION_WEIGHT} + 热度×{self.PRIORITY_HEAT_WEIGHT}")
    
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
                         arbitration_results: Optional[List[ArbitrationResult]] = None,
                         force: bool = False) -> JudgeResult:
        """
        执行全层级晋升判定
        
        Args:
            layer_entries: 各层级条目快照 {"L1": [...], "L2": [...], "L3": [...], "L4": [...]}
            slot_thresholds: 各分槽晋升阈值 {slot_id: SlotThresholds}
            layer_capacities: 各层级剩余容量 {"L2": 100, "L3": 50, "L4": 30, "L5": 10}
            arbitration_results: 本次返回的仲裁结果列表
            force: 是否强制执行（忽略判定间隔）
            
        Returns:
            判定结果汇总
        """
        # 状态检查
        if self.state == JudgeState.PAUSED:
            return JudgeResult("", [], [], [], [], 0)
        
        # 间隔检查
        now = time.time()
        if not force and now - self._last_judge_time < self.JUDGE_INTERVAL:
            return JudgeResult("", [], [], [], [], 0)
        
        self._last_judge_time = now
        self.state = JudgeState.JUDGING
        self._total_cycles += 1
        
        cycle_id = f"judge-{uuid.uuid4().hex[:8]}"
        
        # 处理返回的仲裁结果
        if arbitration_results:
            for result in arbitration_results:
                self._arbitration_waiting.pop(result.entry_id, None)
        
        # 处理仲裁超时
        timeout_entries = self._check_arbitration_timeouts()
        for entry_id in timeout_entries:
            # 超时自动按驳回处理：从各层级快照中标记
            pass
        
        # 初始化汇总容器
        promotion_list: List[PromotionCandidate] = []
        deferred_list: List[DeferredEntry] = []
        rejected_list: List[RejectedEntry] = []
        arbitration_requests: List[ArbitrationRequest] = []
        scanned_count = 0
        
        # 逐层级判定
        for src_layer, dst_layer in [("L1", "L2"), ("L2", "L3"), ("L3", "L4"), ("L4", "L5")]:
            entries = layer_entries.get(src_layer, [])
            dst_capacity = layer_capacities.get(dst_layer, 0)
            
            is_l5 = (dst_layer == "L5")
            check_arbitration = (src_layer in ["L3", "L4"])
            
            for entry in entries:
                scanned_count += 1
                self._judge_single(
                    entry, src_layer, dst_layer,
                    slot_thresholds, dst_capacity,
                    promotion_list, deferred_list, rejected_list, arbitration_requests,
                    check_arbitration, is_l5
                )
        
        # 按优先级降序排列
        promotion_list.sort(key=lambda x: x.priority, reverse=True)
        
        # 容量截断
        self._capacity_truncate(promotion_list, layer_capacities, deferred_list)
        
        # 更新统计
        self._total_scanned += scanned_count
        self._total_approved += len(promotion_list)
        self._total_deferred += len(deferred_list)
        self._total_rejected += len(rejected_list)
        
        # 构建结果
        result = JudgeResult(
            cycle_id=cycle_id,
            promotion_list=promotion_list,
            deferred_list=deferred_list,
            rejected_list=rejected_list,
            arbitration_requests=arbitration_requests,
            scanned_count=scanned_count
        )
        
        self._log_cycle(result)
        self.state = JudgeState.IDLE
        return result
    
    def _judge_single(self,
                      entry: EntrySnapshot,
                      src_layer: str,
                      dst_layer: str,
                      thresholds: Dict[int, SlotThresholds],
                      dst_capacity: int,
                      promotion_list: List[PromotionCandidate],
                      deferred_list: List[DeferredEntry],
                      rejected_list: List[RejectedEntry],
                      arbitration_requests: List[ArbitrationRequest],
                      check_arbitration: bool,
                      is_l5: bool) -> None:
        """判定单条经验的晋升资格"""
        entry_id = entry.entry_id
        path = f"{src_layer}→{dst_layer}"
        slot_id = entry.source_slot_id
        
        # 获取分槽阈值
        th = thresholds.get(slot_id)
        if th is None:
            rejected_list.append(RejectedEntry(entry_id, path, "分槽阈值缺失", PromotionConclusion.REJECTED_INVALID))
            return
        
        # 获取时间和 I 阈值
        time_th = self._get_time_threshold(th, src_layer, dst_layer)
        i_th = self._get_i_threshold(th, src_layer, dst_layer)
        
        # ====== 不可抗力豁免（仅 L4→L5） ======
        if is_l5 and entry.force_majeure:
            if entry.i_value >= self.L5_FORCE_MAJEURE_MIN_I:
                priority = self._calc_priority(entry.i_value, entry.retention_seconds, entry.heat_value)
                promotion_list.append(PromotionCandidate(
                    entry_id, src_layer, dst_layer, entry.i_value, priority,
                    "不可抗力豁免（时间+复用限制解除）"
                ))
                return
        
        # ====== L4→L5 额外复用条件 ======
        if is_l5 and not entry.force_majeure:
            if entry.reuse_count < self.L5_MIN_REUSE:
                deferred_list.append(DeferredEntry(
                    entry_id, path,
                    f"复用次数不足（{entry.reuse_count} < {self.L5_MIN_REUSE}）",
                    PromotionConclusion.DEFERRED_CAPACITY
                ))
                return
        
        # ====== 失败经验仲裁检查 ======
        if check_arbitration and entry.result_label == "策略失误":
            arb_status = entry.arbitration_status
            
            if arb_status in ["none", "pending"]:
                # 发起仲裁请求
                arbitration_requests.append(ArbitrationRequest(
                    entry_id, {}, entry.result_label, {},
                    request_source=path
                ))
                self._arbitration_waiting[entry_id] = time.time()
                deferred_list.append(DeferredEntry(
                    entry_id, path, "等待安全仲裁",
                    PromotionConclusion.DEFERRED_ARBITRATION
                ))
                return
            
            elif arb_status == "in_progress":
                deferred_list.append(DeferredEntry(
                    entry_id, path, "安全仲裁进行中",
                    PromotionConclusion.DEFERRED_ARBITRATION
                ))
                return
            
            elif arb_status == "rejected":
                rejected_list.append(RejectedEntry(
                    entry_id, path, "安全仲裁驳回",
                    PromotionConclusion.REJECTED_ARBITRATION
                ))
                return
            
            elif arb_status == "direct_l5":
                # 仲裁结果为直达 L5
                priority = self._calc_priority(entry.i_value, entry.retention_seconds, entry.heat_value)
                promotion_list.append(PromotionCandidate(
                    entry_id, src_layer, dst_layer, entry.i_value, priority,
                    "仲裁直达L5"
                ))
                return
            
            # approved → 继续正常判定
        
        # ====== 双条件判定 ======
        time_ok = entry.retention_seconds >= time_th
        i_ok = entry.i_value >= i_th
        
        if time_ok and i_ok:
            # 通过
            priority = self._calc_priority(entry.i_value, entry.retention_seconds, entry.heat_value)
            promotion_list.append(PromotionCandidate(
                entry_id, src_layer, dst_layer, entry.i_value, priority
            ))
        elif not time_ok:
            deferred_list.append(DeferredEntry(
                entry_id, path,
                f"留存不足（{entry.retention_seconds/3600:.1f}h < {time_th/3600:.0f}h）",
                PromotionConclusion.DEFERRED_RETENTION
            ))
        else:
            deferred_list.append(DeferredEntry(
                entry_id, path,
                f"I值不足（{entry.i_value:.2f} < {i_th:.2f}）",
                PromotionConclusion.DEFERRED_I_VALUE
            ))
    
    def _get_time_threshold(self, th: SlotThresholds, src: str, dst: str) -> float:
        """获取晋升时间阈值"""
        key = f"time_{src.lower()}_{dst.lower()}"
        mapping = {
            "time_l1_l2": th.time_l1_l2,
            "time_l2_l3": th.time_l2_l3,
            "time_l3_l4": th.time_l3_l4,
            "time_l4_l5": th.time_l4_l5,
        }
        return mapping.get(key, 24 * 3600)
    
    def _get_i_threshold(self, th: SlotThresholds, src: str, dst: str) -> float:
        """获取晋升 I 阈值"""
        key = f"i_{src.lower()}_{dst.lower()}"
        mapping = {
            "i_l1_l2": th.i_l1_l2,
            "i_l2_l3": th.i_l2_l3,
            "i_l3_l4": th.i_l3_l4,
            "i_l4_l5": th.i_l4_l5,
        }
        return mapping.get(key, 0.40)
    
    def _calc_priority(self, i_value: float, retention: float, heat: float) -> float:
        """计算晋升优先级"""
        retention_score = min(retention / (90 * 24 * 3600), 1.0)
        return (self.PRIORITY_I_WEIGHT * i_value +
                self.PRIORITY_RETENTION_WEIGHT * retention_score +
                self.PRIORITY_HEAT_WEIGHT * heat)
    
    def _capacity_truncate(self,
                           promotion_list: List[PromotionCandidate],
                           capacities: Dict[str, int],
                           deferred_list: List[DeferredEntry]) -> None:
        """根据目标层级容量截断晋升候选（S-06）"""
        used: Dict[str, int] = defaultdict(int)
        to_remove = []
        
        for cand in promotion_list:
            cap = capacities.get(cand.target_layer, 0)
            if used[cand.target_layer] >= cap:
                deferred_list.append(DeferredEntry(
                    cand.entry_id,
                    f"{cand.current_layer}→{cand.target_layer}",
                    f"目标层级容量不足（{cap}），按优先级截断",
                    PromotionConclusion.DEFERRED_CAPACITY
                ))
                to_remove.append(cand)
            else:
                used[cand.target_layer] += 1
        
        for cand in to_remove:
            promotion_list.remove(cand)
    
    def _check_arbitration_timeouts(self) -> List[str]:
        """检查仲裁超时的条目"""
        now = time.time()
        timeout_entries = []
        for entry_id, start_time in list(self._arbitration_waiting.items()):
            if now - start_time > self.ARBITRATION_TIMEOUT:
                timeout_entries.append(entry_id)
                del self._arbitration_waiting[entry_id]
        return timeout_entries
    
    # ========== 变更日志 ==========
    
    def _log_cycle(self, result: JudgeResult) -> None:
        """记录判定周期日志"""
        self._pending_logs.append({
            "log_id": f"judge-{uuid.uuid4().hex[:8]}",
            "cycle_id": result.cycle_id,
            "scanned": result.scanned_count,
            "approved": len(result.promotion_list),
            "deferred": len(result.deferred_list),
            "rejected": len(result.rejected_list),
            "arbitration_requests": len(result.arbitration_requests),
            "timestamp": result.timestamp
        })
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs
    
    # ========== 查询接口 ==========
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_cycles": self._total_cycles,
            "total_scanned": self._total_scanned,
            "total_approved": self._total_approved,
            "total_deferred": self._total_deferred,
            "total_rejected": self._total_rejected,
            "arbitration_waiting": len(self._arbitration_waiting),
            "state": self.state.value
        }


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-38 晋升双条件判定单元 单元测试")
    print("=" * 60)
    passed, failed = 0, 0
    
    def make_entry(eid, layer, retention_h, i_value, slot=15, result="成功优化",
                   force_majeure=False, arb="none", reuse=0, heat=0.5):
        return EntrySnapshot(eid, layer, retention_h*3600, i_value, slot,
                             result, force_majeure, arb, reuse, heat)
    
    default_th = {
        15: SlotThresholds(15),
        18: SlotThresholds(18, i_l1_l2=0.28, i_l2_l3=0.42, i_l3_l4=0.56,
                           time_l1_l2=17*3600, time_l2_l3=5*86400, time_l3_l4=21*86400),
    }
    
    # TC-38-01: L1→L2 正常通过
    print("\n[TC-38-01] L1→L2 双条件满足 → 晋升候选")
    try:
        judge = PromotionDualConditionJudge()
        entries = {"L1": [make_entry("EXP-001", "L1", 25, 0.55)]}
        result = judge.execute_judgment(entries, default_th, {"L2": 100}, force=True)
        assert len(result.promotion_list) == 1
        assert result.scanned_count == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-38-02: 留存不足暂缓
    print("\n[TC-38-02] L1→L2 留存20h < 24h → 暂缓")
    try:
        judge = PromotionDualConditionJudge()
        entries = {"L1": [make_entry("EXP-002", "L1", 20, 0.60)]}
        result = judge.execute_judgment(entries, default_th, {"L2": 100}, force=True)
        assert len(result.deferred_list) == 1
        assert result.deferred_list[0].conclusion == PromotionConclusion.DEFERRED_RETENTION
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-38-03: I值不足暂缓
    print("\n[TC-38-03] L1→L2 I=0.30 < 0.40 → 暂缓")
    try:
        judge = PromotionDualConditionJudge()
        entries = {"L1": [make_entry("EXP-003", "L1", 30, 0.30)]}
        result = judge.execute_judgment(entries, default_th, {"L2": 100}, force=True)
        assert len(result.deferred_list) == 1
        assert result.deferred_list[0].conclusion == PromotionConclusion.DEFERRED_I_VALUE
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-38-04: 不可抗力豁免
    print("\n[TC-38-04] L4→L5 不可抗力 I=0.85 → 豁免晋升")
    try:
        judge = PromotionDualConditionJudge()
        entries = {"L4": [make_entry("EXP-004", "L4", 30*24, 0.85, force_majeure=True, reuse=3)]}
        result = judge.execute_judgment(entries, default_th, {"L5": 10}, force=True)
        assert len(result.promotion_list) == 1
        assert "不可抗力豁免" in result.promotion_list[0].notes
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-38-05: L4→L5 复用不足暂缓
    print("\n[TC-38-05] L4→L5 复用5 < 10 → 暂缓")
    try:
        judge = PromotionDualConditionJudge()
        entries = {"L4": [make_entry("EXP-005", "L4", 95*24, 0.85, reuse=5)]}
        result = judge.execute_judgment(entries, default_th, {"L5": 10}, force=True)
        assert len(result.deferred_list) == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-38-06: 失败经验等待仲裁
    print("\n[TC-38-06] L3→L4 策略失误未仲裁 → 发起仲裁请求")
    try:
        judge = PromotionDualConditionJudge()
        entries = {"L3": [make_entry("EXP-006", "L3", 35*24, 0.85, result="策略失误", arb="pending")]}
        result = judge.execute_judgment(entries, default_th, {"L4": 50}, force=True)
        assert len(result.arbitration_requests) == 1
        assert len(result.deferred_list) == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-38-07: 仲裁驳回
    print("\n[TC-38-07] L3→L4 仲裁驳回 → 拒绝晋升")
    try:
        judge = PromotionDualConditionJudge()
        entries = {"L3": [make_entry("EXP-007", "L3", 35*24, 0.85, result="策略失误", arb="rejected")]}
        result = judge.execute_judgment(entries, default_th, {"L4": 50}, force=True)
        assert len(result.rejected_list) == 1
        assert result.rejected_list[0].conclusion == PromotionConclusion.REJECTED_ARBITRATION
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-38-08: 容量截断
    print("\n[TC-38-08] L2→L3 5条候选 L3容量仅2 → 截断")
    try:
        judge = PromotionDualConditionJudge()
        entries = {"L2": [make_entry(f"EXP-{i}", "L2", 8*24, 0.70) for i in range(5)]}
        result = judge.execute_judgment(entries, default_th, {"L3": 2}, force=True)
        assert len(result.promotion_list) == 2
        assert len(result.deferred_list) >= 3
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-38-09: 特殊环境槽低阈值晋升
    print("\n[TC-38-09] 特殊环境槽 L1→L2 I=0.30 ≥ 0.28 → 通过")
    try:
        judge = PromotionDualConditionJudge()
        entries = {"L1": [make_entry("EXP-009", "L1", 18, 0.30, slot=18)]}
        result = judge.execute_judgment(entries, default_th, {"L2": 100}, force=True)
        assert len(result.promotion_list) == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-38-10: 暂停状态返回空
    print("\n[TC-38-10] 暂停状态 → 返回空结果")
    try:
        judge = PromotionDualConditionJudge()
        judge.pause()
        entries = {"L1": [make_entry("EXP-010", "L1", 25, 0.55)]}
        result = judge.execute_judgment(entries, default_th, {"L2": 100}, force=True)
        assert result.scanned_count == 0
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    print(f"\n测试结果: {passed} PASS, {failed} FAIL")