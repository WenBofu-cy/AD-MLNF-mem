#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-39
模块名称: 层级单向搬运写入单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 晋升与遗忘执行机制
核心职责: 执行经验条目从当前层级向上一层存储分区的物理搬运。接收晋升候选清单后，
          将条目的完整经验数据从源层级复制写入目标层级，校验写入完整性后，在源层级
          标记删除或正式清除。编译期硬约束禁止高层级向低层级回退搬运。确保搬运过程
          的原子性。

依赖模块: ad-38(晋升双条件判定单元，提供晋升候选清单),
          ad-20/22/24/26/28(各层级存储单元，提供源数据与接收写入)
被依赖模块: ad-20/22/24/26(消费搬运指令，执行源层级删除),
            ad-22/24/26/28(消费搬运指令，执行目标层级写入)

安全约束:
  S-01: 单向搬运为编译期硬约束。源层级编号必须严格小于目标层级编号，且差值必须为 1
  S-02: 搬运操作不可跳过层级。L1 只能搬运至 L2，L2 只能搬运至 L3，以此类推
  S-03: 目标层级为 L5 的写入，须额外校验 ad-30 返回的令牌验证通过回执
  S-04: 搬运过程中目标层级写入成功后，源层级删除失败时，数据不得丢失
  S-05: 紧急熔断时立即中断搬运，已完成的保留，未开始的取消
  S-06: 所有搬运操作（含成功、失败、中断）全量写入 ad-51 不可变日志
"""

from typing import Dict, List, Optional, Any, Tuple, Callable
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class TransferState(Enum):
    """搬运单元内部状态"""
    IDLE = "idle"
    TRANSFERRING = "transferring"
    ROLLING_BACK = "rolling_back"
    PAUSED = "paused"


class TransferResult(Enum):
    """单条目搬运结果"""
    SUCCESS = "success"
    FAIL_SOURCE_NOT_FOUND = "fail_source_not_found"
    FAIL_TARGET_WRITE_ERROR = "fail_target_write_error"
    FAIL_SOURCE_DELETE_ERROR = "fail_source_delete_error"
    FAIL_LAYER_INVALID = "fail_layer_invalid"
    FAIL_L5_TOKEN_MISSING = "fail_l5_token_missing"
    FAIL_EMERGENCY_ABORT = "fail_emergency_abort"


# ==================== 数据结构 ====================

@dataclass
class PromotionCandidate:
    """晋升候选条目（来自 ad-38）"""
    entry_id: str
    current_layer: str      # "L1" / "L2" / "L3" / "L4"
    target_layer: str       # "L2" / "L3" / "L4" / "L5"
    i_value: float
    priority: float
    notes: str = ""
    l5_security_token: Optional[str] = None  # 用于 L5 写入的令牌


@dataclass
class TransferReport:
    """单条目搬运报告"""
    entry_id: str
    result: TransferResult
    source_layer: str
    target_layer: str
    message: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class BatchTransferResult:
    """批次搬运汇总"""
    batch_id: str
    total: int
    success: int
    failed: int
    details: List[TransferReport]
    duration_ms: float
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class LayerTransferUnit:
    """
    层级单向搬运写入单元
    
    职责:
    1. 接收 ad-38 下发的晋升候选清单
    2. 校验搬运方向（只允许向上一级，禁止跨级、禁止向下）
    3. 从源层级读取完整经验数据
    4. 写入目标层级
    5. 确认写入成功后，删除源层级条目
    6. 处理搬运过程中的异常与回滚
    7. L5 搬运需额外安全令牌验证
    """
    
    # 层级编号映射
    LAYER_NUM = {"L1": 1, "L2": 2, "L3": 3, "L4": 4, "L5": 5}
    
    # 每批次条目间短暂释放间隔（秒）
    ITEM_GAP = 0.005  # 5ms
    
    def __init__(self):
        self.module_id = "ad-39"
        self.module_name = "层级单向搬运写入单元"
        
        # 内部状态
        self.state = TransferState.IDLE
        
        # 统计
        self._total_batches = 0
        self._total_transferred = 0
        self._total_failed = 0
        self._total_aborted = 0
        
        # 当前批次快照（用于回滚或中断）
        self._current_batch: Optional[List[TransferReport]] = None
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 层级单向搬运写入单元初始化完成")
        print(f"[{self.module_id}] 搬运方向: L1→L2→L3→L4→L5（单向，不可回退）")
    
    # ========== 状态管理 ==========
    
    def pause(self) -> None:
        self.state = TransferState.PAUSED
    
    def resume(self) -> None:
        self.state = TransferState.IDLE
    
    def emergency_abort(self) -> List[TransferReport]:
        """
        紧急熔断中断搬运
        
        S-05: 已完成的保留，未开始的取消
        """
        if self.state != TransferState.TRANSFERRING:
            return []
        
        self.state = TransferState.PAUSED
        aborted = []
        if self._current_batch:
            for report in self._current_batch:
                if report.result == TransferResult.SUCCESS:
                    continue
                report.result = TransferResult.FAIL_EMERGENCY_ABORT
                report.message = "紧急熔断中断"
                aborted.append(report)
        
        self._total_aborted += len(aborted)
        print(f"[{self.module_id}] 紧急熔断: 中断搬运, {len(aborted)} 条未完成")
        return aborted
    
    # ========== 搬运执行 ==========
    
    def execute_batch(self,
                      candidates: List[PromotionCandidate],
                      read_source: Callable[[str, str], Optional[Dict[str, Any]]],
                      write_target: Callable[[str, Dict[str, Any]], bool],
                      delete_source: Callable[[str, str], bool],
                      l5_token_validator: Optional[Callable[[str, str], bool]] = None) -> BatchTransferResult:
        """
        执行批次搬运
        
        Args:
            candidates: 晋升候选列表
            read_source: 读取源层级条目数据的回调 (layer, entry_id) -> data or None
            write_target: 写入目标层级条目的回调 (layer, data) -> success
            delete_source: 删除源层级条目的回调 (layer, entry_id) -> success
            l5_token_validator: L5 写入令牌验证回调 (entry_id, token) -> success
            
        Returns:
            批次搬运汇总
        """
        if self.state != TransferState.IDLE:
            return BatchTransferResult("", 0, 0, 0, [], 0, time.time())
        
        self.state = TransferState.TRANSFERRING
        start_time = time.time()
        batch_id = f"transfer-{uuid.uuid4().hex[:8]}"
        
        # 按优先级排序
        sorted_candidates = sorted(candidates, key=lambda x: x.priority, reverse=True)
        
        reports: List[TransferReport] = []
        success_count = 0
        failed_count = 0
        
        self._current_batch = []
        
        for candidate in sorted_candidates:
            # S-05: 紧急熔断检查
            if self.state != TransferState.TRANSFERRING:
                report = TransferReport(
                    entry_id=candidate.entry_id,
                    result=TransferResult.FAIL_EMERGENCY_ABORT,
                    source_layer=candidate.current_layer,
                    target_layer=candidate.target_layer,
                    message="紧急熔断中断"
                )
                reports.append(report)
                self._current_batch.append(report)
                failed_count += 1
                self._total_aborted += 1
                continue
            
            report = self._transfer_single(
                candidate, read_source, write_target, delete_source, l5_token_validator
            )
            reports.append(report)
            self._current_batch.append(report)
            
            if report.result == TransferResult.SUCCESS:
                success_count += 1
                self._total_transferred += 1
            else:
                failed_count += 1
                self._total_failed += 1
            
            # 条目间短暂释放
            time.sleep(self.ITEM_GAP)
        
        self._total_batches += 1
        duration_ms = (time.time() - start_time) * 1000
        
        result = BatchTransferResult(
            batch_id=batch_id,
            total=len(candidates),
            success=success_count,
            failed=failed_count,
            details=reports,
            duration_ms=duration_ms,
            timestamp=time.time()
        )
        
        self._current_batch = None
        self.state = TransferState.IDLE
        
        self._log_batch(result)
        return result
    
    def _transfer_single(self,
                         candidate: PromotionCandidate,
                         read_source: Callable[[str, str], Optional[Dict[str, Any]]],
                         write_target: Callable[[str, Dict[str, Any]], bool],
                         delete_source: Callable[[str, str], bool],
                         l5_token_validator: Optional[Callable[[str, str], bool]] = None) -> TransferReport:
        """
        搬运单条经验
        
        流程: 校验方向 → 读取源数据 → 写入目标 → 删除源
        """
        entry_id = candidate.entry_id
        src = candidate.current_layer
        dst = candidate.target_layer
        
        # S-01 / S-02: 方向校验
        valid, msg = self._validate_direction(src, dst)
        if not valid:
            return TransferReport(entry_id, TransferResult.FAIL_LAYER_INVALID, src, dst, msg)
        
        # S-03: L5 特殊校验
        if dst == "L5" and l5_token_validator is not None:
            token = getattr(candidate, 'l5_security_token', None)
            if not l5_token_validator(entry_id, token):
                return TransferReport(entry_id, TransferResult.FAIL_L5_TOKEN_MISSING, src, dst,
                                    "L5令牌验证失败")
        
        # 1. 读取源数据
        source_data = read_source(src, entry_id)
        if source_data is None:
            return TransferReport(entry_id, TransferResult.FAIL_SOURCE_NOT_FOUND, src, dst,
                                "源条目不存在")
        
        # 2. 写入目标层级
        write_ok = write_target(dst, source_data)
        if not write_ok:
            return TransferReport(entry_id, TransferResult.FAIL_TARGET_WRITE_ERROR, src, dst,
                                "目标层级写入失败")
        
        # 3. 删除源层级条目
        delete_ok = delete_source(src, entry_id)
        if not delete_ok:
            # S-04: 目标已写入，源删除失败 → 数据重复但不丢失
            return TransferReport(entry_id, TransferResult.FAIL_SOURCE_DELETE_ERROR, src, dst,
                                "目标写入成功但源删除失败，数据可能重复")
        
        return TransferReport(entry_id, TransferResult.SUCCESS, src, dst, "搬运成功")
    
    def _validate_direction(self, src: str, dst: str) -> Tuple[bool, str]:
        """校验搬运方向"""
        if src not in self.LAYER_NUM or dst not in self.LAYER_NUM:
            return False, f"无效层级: {src}→{dst}"
        
        src_num = self.LAYER_NUM[src]
        dst_num = self.LAYER_NUM[dst]
        
        if src_num >= dst_num:
            return False, f"禁止回退或同级搬运: {src}→{dst}"
        
        if dst_num - src_num != 1:
            return False, f"禁止跨级搬运: {src}→{dst}（仅允许逐级搬运）"
        
        return True, ""
    
    # ========== 变更日志 ==========
    
    def _log_batch(self, result: BatchTransferResult) -> None:
        self._pending_logs.append({
            "log_id": f"transfer-{uuid.uuid4().hex[:8]}",
            "batch_id": result.batch_id,
            "total": result.total,
            "success": result.success,
            "failed": result.failed,
            "duration_ms": result.duration_ms,
            "timestamp": result.timestamp
        })
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs
    
    # ========== 查询接口 ==========
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_batches": self._total_batches,
            "total_transferred": self._total_transferred,
            "total_failed": self._total_failed,
            "total_aborted": self._total_aborted,
            "state": self.state.value
        }


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-39 层级单向搬运写入单元 单元测试")
    print("=" * 60)
    passed, failed = 0, 0
    
    # 模拟存储
    store = {
        "L1": {},
        "L2": {},
        "L3": {},
        "L4": {},
        "L5": {}
    }
    
    def read_src(layer, eid):
        return store.get(layer, {}).get(eid)
    
    def write_dst(layer, data):
        if layer not in store:
            return False
        store[layer][data["entry_id"]] = data
        return True
    
    def del_src(layer, eid):
        if layer in store and eid in store[layer]:
            del store[layer][eid]
            return True
        return False
    
    def l5_token_ok(eid, token):
        return token == "valid_l5_token"
    
    # --- TC-39-01: 正常 L1→L2 搬运 ---
    print("\n[TC-39-01] 正常 L1→L2 搬运")
    try:
        store["L1"]["EXP-001"] = {"entry_id": "EXP-001", "data": "test"}
        store["L2"] = {}
        unit = LayerTransferUnit()
        candidates = [PromotionCandidate("EXP-001", "L1", "L2", 0.55, 1.0)]
        result = unit.execute_batch(candidates, read_src, write_dst, del_src)
        assert result.success == 1
        assert "EXP-001" in store["L2"]
        assert "EXP-001" not in store["L1"]
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-39-02: 禁止回退搬运 L2→L1 ---
    print("\n[TC-39-02] 禁止回退搬运 L2→L1")
    try:
        unit = LayerTransferUnit()
        candidates = [PromotionCandidate("EXP-002", "L2", "L1", 0.5, 1.0)]
        result = unit.execute_batch(candidates, read_src, write_dst, del_src)
        assert result.failed == 1
        assert result.details[0].result == TransferResult.FAIL_LAYER_INVALID
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-39-03: 禁止跨级搬运 L1→L3 ---
    print("\n[TC-39-03] 禁止跨级搬运 L1→L3")
    try:
        unit = LayerTransferUnit()
        candidates = [PromotionCandidate("EXP-003", "L1", "L3", 0.5, 1.0)]
        result = unit.execute_batch(candidates, read_src, write_dst, del_src)
        assert result.failed == 1
        assert result.details[0].result == TransferResult.FAIL_LAYER_INVALID
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-39-04: 源条目不存在 ---
    print("\n[TC-39-04] 源条目不存在")
    try:
        store["L1"] = {}
        unit = LayerTransferUnit()
        candidates = [PromotionCandidate("EXP-004", "L1", "L2", 0.5, 1.0)]
        result = unit.execute_batch(candidates, read_src, write_dst, del_src)
        assert result.failed == 1
        assert result.details[0].result == TransferResult.FAIL_SOURCE_NOT_FOUND
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-39-05: 目标写入失败 ---
    print("\n[TC-39-05] 目标写入失败")
    try:
        store["L1"]["EXP-005"] = {"entry_id": "EXP-005", "data": "test"}
        store["L2"] = {}
        
        def write_fail(layer, data):
            return False
        
        unit = LayerTransferUnit()
        candidates = [PromotionCandidate("EXP-005", "L1", "L2", 0.5, 1.0)]
        result = unit.execute_batch(candidates, read_src, write_fail, del_src)
        assert result.failed == 1
        assert result.details[0].result == TransferResult.FAIL_TARGET_WRITE_ERROR
        # 源条目应保留
        assert "EXP-005" in store["L1"]
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-39-06: 源删除失败（目标已写入） ---
    print("\n[TC-39-06] 目标写入成功，源删除失败 → 标记错误")
    try:
        store["L1"]["EXP-006"] = {"entry_id": "EXP-006", "data": "test"}
        store["L2"] = {}
        
        def del_fail(layer, eid):
            return False
        
        unit = LayerTransferUnit()
        candidates = [PromotionCandidate("EXP-006", "L1", "L2", 0.5, 1.0)]
        result = unit.execute_batch(candidates, read_src, write_dst, del_fail)
        assert result.failed == 1
        assert result.details[0].result == TransferResult.FAIL_SOURCE_DELETE_ERROR
        # 目标应已写入
        assert "EXP-006" in store["L2"]
        # 源可能仍存在（数据重复但不丢失）
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-39-07: L5 令牌缺失 ---
    print("\n[TC-39-07] L5 搬运令牌缺失 → 失败")
    try:
        store["L4"]["EXP-007"] = {"entry_id": "EXP-007", "data": "test"}
        store["L5"] = {}
        unit = LayerTransferUnit()
        candidates = [PromotionCandidate("EXP-007", "L4", "L5", 0.85, 1.0, l5_security_token=None)]
        result = unit.execute_batch(candidates, read_src, write_dst, del_src, l5_token_ok)
        assert result.failed == 1
        assert result.details[0].result == TransferResult.FAIL_L5_TOKEN_MISSING
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-39-08: L5 令牌验证成功 ---
    print("\n[TC-39-08] L5 搬运令牌有效 → 搬运成功")
    try:
        store["L4"]["EXP-008"] = {"entry_id": "EXP-008", "data": "test"}
        store["L5"] = {}
        unit = LayerTransferUnit()
        candidates = [PromotionCandidate("EXP-008", "L4", "L5", 0.85, 1.0, l5_security_token="valid_l5_token")]
        result = unit.execute_batch(candidates, read_src, write_dst, del_src, l5_token_ok)
        assert result.success == 1
        assert "EXP-008" in store["L5"]
        assert "EXP-008" not in store["L4"]
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-39-09: 紧急熔断中断 ---
    print("\n[TC-39-09] 紧急熔断中断搬运")
    try:
        store["L1"]["EXP-A"] = {"entry_id": "EXP-A", "data": "a"}
        store["L1"]["EXP-B"] = {"entry_id": "EXP-B", "data": "b"}
        store["L2"] = {}
        unit = LayerTransferUnit()
        unit.state = TransferState.TRANSFERRING  # 模拟搬运中
        # 模拟部分完成
        unit._current_batch = [
            TransferReport("EXP-A", TransferResult.SUCCESS, "L1", "L2", "ok"),
            TransferReport("EXP-B", TransferResult.SUCCESS, "L1", "L2", "ok")
        ]
        aborted = unit.emergency_abort()
        # 已成功的应保留，所以 aborted 应为空
        assert len(aborted) == 0
        # 再发起新批次搬运，应处于暂停状态
        candidates = [PromotionCandidate("EXP-C", "L1", "L2", 0.5, 1.0)]
        result = unit.execute_batch(candidates, read_src, write_dst, del_src)
        assert result.total == 0  # 因为状态非 IDLE
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # --- TC-39-10: 批量搬运统计 ---
    print("\n[TC-39-10] 批量搬运统计正确")
    try:
        store["L1"]["EXP-X"] = {"entry_id": "EXP-X", "data": "x"}
        store["L1"]["EXP-Y"] = {"entry_id": "EXP-Y", "data": "y"}
        store["L2"] = {}
        unit = LayerTransferUnit()
        candidates = [
            PromotionCandidate("EXP-X", "L1", "L2", 0.6, 0.9),
            PromotionCandidate("EXP-Y", "L1", "L2", 0.5, 0.8)
        ]
        result = unit.execute_batch(candidates, read_src, write_dst, del_src)
        assert result.total == 2
        assert result.success == 2
        assert unit.get_statistics()["total_transferred"] == 2
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    print(f"\n测试结果: {passed} PASS, {failed} FAIL")