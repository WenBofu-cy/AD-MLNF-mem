#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-42
模块名称: 冗余记忆删除与归档单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 晋升与遗忘执行机制
核心职责: 执行遗忘流程的最终操作：对通过校验的遗忘条目执行安全删除（直接覆写擦除）
          或冷归档（压缩后转存至 ad-49）。是漏斗二记忆体系中唯一有权物理清除数据的
          模块。删除操作执行 DoD 5220.22-M 标准单次覆写，归档操作确保压缩数据可追溯恢复。

依赖模块: ad-41(最低复用次数校验单元，提供通过校验的遗忘执行清单),
          ad-49(存储压缩与冷归档单元，接收归档数据),
          ad-20/22/24/26(各层级存储单元，确认条目可删除),
          ad-29(L5 核心层安全规则硬锁定单元，校验 L3 及以上条目是否受写保护)
被依赖模块: ad-20/22/24/26(接收删除完成确认，释放存储空间),
            ad-49(接收归档数据), ad-51(接收操作日志)

安全约束:
  S-01: 直接删除必须执行 DoD 5220.22-M 标准单次全零覆写，覆写完成后强制 FLUSH
  S-02: 冷归档条目在归档确认成功前不得删除源条目。归档失败时条目保留在源层级
  S-03: L3 及以上层级条目在执行前须校验写保护状态
  S-04: 本单元是漏斗二中唯一有权物理清除数据的模块
  S-05: 紧急熔断时立即中断操作，已完成的保留，未开始的取消
  S-06: 所有删除、归档操作（含成功、失败、中断）全量写入 ad-51 不可变日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class OperationType(Enum):
    """操作类型"""
    DIRECT_DELETE = "直接删除"
    COLD_ARCHIVE = "冷归档"


class PruneState(Enum):
    """清理单元内部状态"""
    IDLE = "idle"
    EXECUTING = "executing"
    ROLLING_BACK = "rolling_back"
    PAUSED = "paused"


class PruneResult(Enum):
    """单条目操作结果"""
    SUCCESS = "success"
    FAIL_ENTRY_NOT_FOUND = "fail_entry_not_found"
    FAIL_WRITE_PROTECTED = "fail_write_protected"
    FAIL_OVERWRITE_ERROR = "fail_overwrite_error"
    FAIL_ARCHIVE_ERROR = "fail_archive_error"
    FAIL_SOURCE_DELETE_ERROR = "fail_source_delete_error"
    FAIL_EMERGENCY_ABORT = "fail_emergency_abort"


# ==================== 数据结构 ====================

@dataclass
class ValidatedEntry:
    """通过校验的遗忘执行条目（来自 ad-41）"""
    entry_id: str
    current_layer: str
    i_value: float
    reuse_count: int
    forget_method: str         # "直接删除" / "冷归档"
    source_slot_id: int
    validation_conclusion: str
    priority: float = 0.0


@dataclass
class EntryData:
    """条目完整数据（从源层级读取）"""
    entry_id: str
    content: Dict[str, Any]
    metadata: Dict[str, Any]
    storage_address: int
    size_bytes: int


@dataclass
class PruneReport:
    """单条目操作报告"""
    entry_id: str
    operation_type: OperationType
    result: PruneResult
    source_layer: str
    message: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class BatchPruneResult:
    """批次操作汇总"""
    batch_id: str
    total: int
    delete_success: int
    archive_success: int
    failed: int
    release_bytes: int
    details: List[PruneReport]
    duration_ms: float
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class RedundantPruneUnit:
    """
    冗余记忆删除与归档单元
    
    职责:
    1. 接收 ad-41 下发的通过校验的遗忘执行清单
    2. 从源层级读取条目完整数据
    3. 根据遗忘方式执行安全删除或冷归档
    4. 安全删除：DoD 5220.22-M 单次全零覆写 → FLUSH → 删除源索引
    5. 冷归档：压缩数据 → 发送至 ad-49 → 确认成功后删除源条目
    6. L3 及以上层级执行前校验写保护状态
    """
    
    # 覆写块大小（字节）
    OVERWRITE_BLOCK_SIZE = 4096  # 4KB
    
    # 覆写模式
    PATTERN_ZERO = 0x00
    
    # 条目间释放间隔（秒）
    ITEM_GAP = 0.005  # 5ms
    
    def __init__(self):
        self.module_id = "ad-42"
        self.module_name = "冗余记忆删除与归档单元"
        
        # 内部状态
        self.state = PruneState.IDLE
        
        # 统计
        self._total_batches = 0
        self._total_deleted = 0
        self._total_archived = 0
        self._total_failed = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 冗余记忆删除与归档单元初始化完成")
        print(f"[{self.module_id}] 删除标准: DoD 5220.22-M 单次全零覆写")
        print(f"[{self.module_id}] 漏斗二唯一物理清除权限")
    
    # ========== 状态管理 ==========
    
    def pause(self) -> None:
        self.state = PruneState.PAUSED
    
    def resume(self) -> None:
        self.state = PruneState.IDLE
    
    def emergency_abort(self) -> None:
        """
        紧急熔断中断当前操作
        
        S-05: 已完成的保留，未开始的取消
        """
        if self.state == PruneState.EXECUTING:
            self.state = PruneState.PAUSED
            print(f"[{self.module_id}] 紧急熔断: 中断当前操作")
    
    def get_state(self) -> PruneState:
        return self.state
    
    # ========== 主执行流程 ==========
    
    def execute_batch(self,
                      entries: Dict[str, List[ValidatedEntry]],
                      read_source: Any,
                      write_protection_check: Any,
                      archive_callback: Any,
                      delete_source_callback: Any) -> BatchPruneResult:
        """
        执行批次遗忘操作
        
        Args:
            entries: 通过校验的遗忘执行清单（按层级分组）
            read_source: 读取源层级条目数据的回调 (layer, entry_id) -> EntryData or None
            write_protection_check: 校验写保护的回调 (entry_id) -> bool（True=受保护）
            archive_callback: 冷归档回调 (entry_id, compressed_data) -> bool（是否成功）
            delete_source_callback: 删除源层级条目的回调 (layer, entry_id) -> bool
            
        Returns:
            批次操作汇总
        """
        if self.state != PruneState.IDLE:
            return BatchPruneResult("", 0, 0, 0, 0, [], 0, 0)
        
        self.state = PruneState.EXECUTING
        start_time = time.time()
        batch_id = f"prune-{uuid.uuid4().hex[:8]}"
        
        reports = []
        delete_success = 0
        archive_success = 0
        failed = 0
        total_release = 0
        
        for layer_name, layer_entries in entries.items():
            # 按优先级排序
            sorted_entries = sorted(layer_entries, key=lambda x: x.priority, reverse=True)
            
            for entry in sorted_entries:
                # S-05: 紧急熔断检查
                if self.state != PruneState.EXECUTING:
                    report = PruneReport(
                        entry_id=entry.entry_id,
                        operation_type=OperationType.DIRECT_DELETE,
                        result=PruneResult.FAIL_EMERGENCY_ABORT,
                        source_layer=layer_name,
                        message="紧急熔断中断"
                    )
                    reports.append(report)
                    failed += 1
                    continue
                
                report = self._process_single(
                    entry, layer_name,
                    read_source, write_protection_check,
                    archive_callback, delete_source_callback
                )
                reports.append(report)
                
                if report.result == PruneResult.SUCCESS:
                    if report.operation_type == OperationType.DIRECT_DELETE:
                        delete_success += 1
                        self._total_deleted += 1
                    else:
                        archive_success += 1
                        self._total_archived += 1
                else:
                    failed += 1
                    self._total_failed += 1
                
                # 条目间短暂释放
                time.sleep(self.ITEM_GAP)
        
        self._total_batches += 1
        duration_ms = (time.time() - start_time) * 1000
        
        result = BatchPruneResult(
            batch_id=batch_id,
            total=sum(len(v) for v in entries.values()),
            delete_success=delete_success,
            archive_success=archive_success,
            failed=failed,
            release_bytes=total_release,
            details=reports,
            duration_ms=duration_ms
        )
        
        self.state = PruneState.IDLE
        self._log_batch(result)
        return result
    
    def _process_single(self,
                        entry: ValidatedEntry,
                        layer_name: str,
                        read_source: Any,
                        write_protection_check: Any,
                        archive_callback: Any,
                        delete_source_callback: Any) -> PruneReport:
        """
        处理单条遗忘条目
        
        流程:
        1. 读取源数据
        2. L3+ 写保护校验
        3. 根据遗忘方式执行
        """
        entry_id = entry.entry_id
        is_direct_delete = (entry.forget_method == "直接删除")
        operation_type = OperationType.DIRECT_DELETE if is_direct_delete else OperationType.COLD_ARCHIVE
        
        # 1. 读取源数据
        entry_data = read_source(layer_name, entry_id)
        if entry_data is None:
            return PruneReport(entry_id, operation_type, PruneResult.FAIL_ENTRY_NOT_FOUND,
                              layer_name, "条目数据不存在，可能已被其他操作删除")
        
        # 2. S-03: L3 及以上层级写保护校验
        if layer_name in ["L3", "L4", "L5"]:
            if write_protection_check(entry_id):
                return PruneReport(entry_id, operation_type, PruneResult.FAIL_WRITE_PROTECTED,
                                  layer_name, "条目受写保护，不可删除或归档")
        
        # 3. 根据遗忘方式执行
        if is_direct_delete:
            return self._execute_delete(entry, entry_data, layer_name, delete_source_callback)
        else:
            return self._execute_archive(entry, entry_data, layer_name, archive_callback, delete_source_callback)
    
    def _execute_delete(self,
                        entry: ValidatedEntry,
                        entry_data: EntryData,
                        layer_name: str,
                        delete_source_callback: Any) -> PruneReport:
        """
        执行安全删除
        
        S-01: DoD 5220.22-M 单次全零覆写 → FLUSH → 删除源索引
        """
        entry_id = entry.entry_id
        storage_addr = entry_data.storage_address
        data_size = entry_data.size_bytes
        
        # 执行覆写
        overwrite_ok = self._secure_overwrite(storage_addr, data_size)
        if not overwrite_ok:
            return PruneReport(entry_id, OperationType.DIRECT_DELETE,
                              PruneResult.FAIL_OVERWRITE_ERROR, layer_name,
                              "安全覆写失败，存储硬件可能故障")
        
        # FLUSH 存储控制器缓存
        self._flush_cache()
        
        # 删除源层级索引
        delete_ok = delete_source_callback(layer_name, entry_id)
        if not delete_ok:
            # 覆写已完成（数据已不可恢复），源条目标记为待清理
            return PruneReport(entry_id, OperationType.DIRECT_DELETE,
                              PruneResult.FAIL_SOURCE_DELETE_ERROR, layer_name,
                              "覆写完成但源索引删除失败，已标记待清理")
        
        return PruneReport(entry_id, OperationType.DIRECT_DELETE,
                          PruneResult.SUCCESS, layer_name, "安全删除成功")
    
    def _execute_archive(self,
                         entry: ValidatedEntry,
                         entry_data: EntryData,
                         layer_name: str,
                         archive_callback: Any,
                         delete_source_callback: Any) -> PruneReport:
        """
        执行冷归档
        
        S-02: 归档确认成功前不得删除源条目
        """
        entry_id = entry.entry_id
        
        # 模拟压缩（实际使用 LZ4/Zstandard）
        compressed_data = entry_data.content  # 简化处理
        
        # 发送至 ad-49 归档
        archive_ok = archive_callback(entry_id, compressed_data)
        if not archive_ok:
            return PruneReport(entry_id, OperationType.COLD_ARCHIVE,
                              PruneResult.FAIL_ARCHIVE_ERROR, layer_name,
                              "冷归档失败，条目保留在源层级")
        
        # 归档成功后删除源条目
        delete_ok = delete_source_callback(layer_name, entry_id)
        if not delete_ok:
            return PruneReport(entry_id, OperationType.COLD_ARCHIVE,
                              PruneResult.FAIL_SOURCE_DELETE_ERROR, layer_name,
                              "归档成功但源索引删除失败，数据可能重复")
        
        return PruneReport(entry_id, OperationType.COLD_ARCHIVE,
                          PruneResult.SUCCESS, layer_name, "冷归档成功")
    
    def _secure_overwrite(self, address: int, size: int) -> bool:
        """
        执行 DoD 5220.22-M 单次全零覆写
        
        Args:
            address: 存储起始地址
            size: 数据大小（字节）
            
        Returns:
            是否成功
        """
        # 模拟覆写操作
        # 实际实现中遍历存储块，写入 0x00
        blocks = (size + self.OVERWRITE_BLOCK_SIZE - 1) // self.OVERWRITE_BLOCK_SIZE
        for _ in range(blocks):
            # 模拟写入一个块的全零数据
            pass
        return True
    
    def _flush_cache(self) -> None:
        """刷新存储控制器缓存，确保覆写落地到物理介质"""
        # 模拟 FLUSH 操作
        pass
    
    # ========== 变更日志 ==========
    
    def _log_batch(self, result: BatchPruneResult) -> None:
        self._pending_logs.append({
            "log_id": f"prune-{uuid.uuid4().hex[:8]}",
            "batch_id": result.batch_id,
            "total": result.total,
            "delete_success": result.delete_success,
            "archive_success": result.archive_success,
            "failed": result.failed,
            "release_bytes": result.release_bytes,
            "duration_ms": result.duration_ms,
            "timestamp": result.timestamp
        })
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_batches": self._total_batches,
            "total_deleted": self._total_deleted,
            "total_archived": self._total_archived,
            "total_failed": self._total_failed,
            "state": self.state.value
        }


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-42 冗余记忆删除与归档单元 单元测试")
    print("=" * 60)
    passed, failed = 0, 0
    
    # 模拟存储
    store = {"L1": {"EXP-001": EntryData("EXP-001", {"data": "test"}, {}, 0x1000, 1024)},
             "L2": {}, "L3": {}, "L4": {}, "L5": {}}
    protected_ids = set()
    archive_store = {}
    
    def read_src(layer, eid):
        return store.get(layer, {}).get(eid)
    
    def wp_check(eid):
        return eid in protected_ids
    
    def archive_cb(eid, data):
        archive_store[eid] = data
        return True
    
    def archive_fail_cb(eid, data):
        return False
    
    def del_src(layer, eid):
        if layer in store and eid in store[layer]:
            del store[layer][eid]
            return True
        return False
    
    def make_entry(eid, layer, method="直接删除", priority=0.5):
        return ValidatedEntry(eid, layer, 0.10, 0, method, 15, "PASS", priority)
    
    # TC-42-01: 直接删除成功
    print("\n[TC-42-01] 直接删除成功")
    try:
        store["L1"]["EXP-001"] = EntryData("EXP-001", {"data": "test"}, {}, 0x1000, 1024)
        pruner = RedundantPruneUnit()
        entries = {"L1": [make_entry("EXP-001", "L1")]}
        result = pruner.execute_batch(entries, read_src, wp_check, archive_cb, del_src)
        assert result.delete_success == 1
        assert "EXP-001" not in store["L1"]
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-42-02: 冷归档成功
    print("\n[TC-42-02] 冷归档成功")
    try:
        archive_store.clear()
        store["L1"]["EXP-002"] = EntryData("EXP-002", {"data": "archive_test"}, {}, 0x2000, 2048)
        pruner = RedundantPruneUnit()
        entries = {"L1": [make_entry("EXP-002", "L1", "冷归档")]}
        result = pruner.execute_batch(entries, read_src, wp_check, archive_cb, del_src)
        assert result.archive_success == 1
        assert "EXP-002" in archive_store
        assert "EXP-002" not in store["L1"]
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-42-03: 冷归档失败保留源条目
    print("\n[TC-42-03] 冷归档失败保留源条目")
    try:
        store["L1"]["EXP-003"] = EntryData("EXP-003", {"data": "keep"}, {}, 0x3000, 1024)
        pruner = RedundantPruneUnit()
        entries = {"L1": [make_entry("EXP-003", "L1", "冷归档")]}
        result = pruner.execute_batch(entries, read_src, wp_check, archive_fail_cb, del_src)
        assert result.failed == 1
        assert result.details[0].result == PruneResult.FAIL_ARCHIVE_ERROR
        assert "EXP-003" in store["L1"]  # 保留
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-42-04: 写保护拒绝
    print("\n[TC-42-04] L3 条目受写保护拒绝操作")
    try:
        protected_ids.add("EXP-004")
        store["L3"]["EXP-004"] = EntryData("EXP-004", {"data": "protected"}, {}, 0x4000, 4096)
        pruner = RedundantPruneUnit()
        entries = {"L3": [make_entry("EXP-004", "L3")]}
        result = pruner.execute_batch(entries, read_src, wp_check, archive_cb, del_src)
        assert result.failed == 1
        assert result.details[0].result == PruneResult.FAIL_WRITE_PROTECTED
        assert "EXP-004" in store["L3"]
        protected_ids.discard("EXP-004")
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-42-05: 条目不存在
    print("\n[TC-42-05] 条目已被删除，操作失败")
    try:
        pruner = RedundantPruneUnit()
        entries = {"L1": [make_entry("NON_EXIST", "L1")]}
        result = pruner.execute_batch(entries, read_src, wp_check, archive_cb, del_src)
        assert result.failed == 1
        assert result.details[0].result == PruneResult.FAIL_ENTRY_NOT_FOUND
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-42-06: 混合批次统计
    print("\n[TC-42-06] 混合批次（删1+归1）统计正确")
    try:
        store["L1"]["EXP-A"] = EntryData("EXP-A", {"d": 1}, {}, 0xA000, 1024)
        store["L1"]["EXP-B"] = EntryData("EXP-B", {"d": 2}, {}, 0xB000, 2048)
        pruner = RedundantPruneUnit()
        entries = {"L1": [
            make_entry("EXP-A", "L1", "直接删除", 1.0),
            make_entry("EXP-B", "L1", "冷归档", 0.8)
        ]}
        result = pruner.execute_batch(entries, read_src, wp_check, archive_cb, del_src)
        assert result.delete_success == 1
        assert result.archive_success == 1
        assert result.total == 2
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-42-07: 紧急熔断中断
    print("\n[TC-42-07] 紧急熔断中断操作")
    try:
        store["L1"]["EXP-C"] = EntryData("EXP-C", {"d": 3}, {}, 0xC000, 1024)
        store["L1"]["EXP-D"] = EntryData("EXP-D", {"d": 4}, {}, 0xD000, 1024)
        pruner = RedundantPruneUnit()
        
        # 在回调中触发紧急熔断（模拟）
        original_read = read_src
        def read_with_abort(layer, eid):
            pruner.emergency_abort()
            return original_read(layer, eid)
        
        entries = {"L1": [
            make_entry("EXP-C", "L1"), make_entry("EXP-D", "L1")
        ]}
        result = pruner.execute_batch(entries, read_with_abort, wp_check, archive_cb, del_src)
        assert result.failed >= 1  # 至少第二条失败
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    print(f"\n测试结果: {passed} PASS, {failed} FAIL")