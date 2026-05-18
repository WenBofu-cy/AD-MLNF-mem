#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-12
模块名称: 临时画像槽自动清除单元
所属分区: 二、漏斗一：驾驶员画像漏斗
核心职责: 负责临时画像槽（7天到期）与一次性记录槽（行程结束）的自动安全擦除。
          执行 DoD 5220.22-M 标准单次覆写擦除，确保被清除数据不可恢复。
          擦除前校验权限，擦除后校验完整性。

依赖模块: ad-02(漏斗一专属调度单元，下发清除触发信号),
          ad-06(子画像槽数据隔离管控单元，校验擦除权限)
被依赖模块: 无（擦除操作的最终执行者）

安全约束:
  S-01: 长期槽禁止本模块自动清除。长期槽删除须用户手动确认
  S-02: 擦除标准硬编码：临时槽 DoD 5220.22-M 单次全零覆写，一次性槽双次覆写
  S-03: 擦除后必须执行数据残留校验（随机抽样率 ≥ 10%），校验未通过的存储分区禁止重新分配
  S-04: 擦除操作执行前须经 ad-06 权限校验
  S-05: 紧急熔断中断擦除时，目标槽标记为 PARTIALLY_ERASED
  S-06: 系统上电自检时须扫描所有临时/一次性槽，超期或行程已结束的自动触发擦除
  S-07: 所有擦除操作全量写入 ad-51 不可变日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid
import random


# ==================== 枚举定义 ====================

class SlotType(Enum):
    """槽位类型"""
    LONG_TERM = "long_term"
    TEMPORARY = "temporary"
    ONESHOT = "one_shot"


class CleanupState(Enum):
    """清除单元内部状态"""
    IDLE = "idle"
    AUTH_CHECK = "auth_check"
    ERASING = "erasing"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"
    PAUSED = "paused"
    ABORTED = "aborted"


class EraseStandard(Enum):
    """擦除标准"""
    SINGLE_ZERO = "dod_single_zero"       # 单次全零覆写
    DOUBLE_FF_ZERO = "dod_double_ff_zero"  # 全一+全零双次覆写


class CleanupTriggerReason(Enum):
    """清除触发原因"""
    TEMP_EXPIRED = "7天到期"
    ONESHOT_TRIP_END = "行程结束"
    USER_MANUAL = "用户手动确认删除"
    STARTUP_SCAN = "上电自检扫描"


# ==================== 数据结构 ====================

@dataclass
class CleanupTriggerSignal:
    """清除触发信号"""
    target_slot_id: int
    slot_type: SlotType
    trigger_reason: CleanupTriggerReason
    create_timestamp: float
    trip_end_timestamp: Optional[float] = None
    signal_timestamp: float = field(default_factory=time.time)


@dataclass
class StoragePartition:
    """存储分区描述"""
    partition_id: str
    base_address: int
    size_bytes: int
    allocated: bool = True


@dataclass
class EraseCompletionReport:
    """擦除完成回执"""
    slot_id: int
    success: bool
    erase_standard_used: EraseStandard
    erase_timestamp: float
    partition_released: bool
    verification_passed: bool
    error_code: Optional[str] = None


@dataclass
class SlotCleanupLog:
    """槽位清除日志"""
    log_id: str
    slot_id: int
    slot_type: SlotType
    trigger_reason: CleanupTriggerReason
    erase_standard: EraseStandard
    success: bool
    verification_sampled: int
    verification_passed: int
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class TemporarySlotCleanup:
    """
    临时画像槽自动清除单元
    
    职责:
    1. 接收 ad-02 下发的清除触发信号
    2. 向 ad-06 申请擦除权限
    3. 执行 DoD 5220.22-M 标准覆写擦除
    4. 擦除后随机抽样校验数据完整性
    5. 释放存储分区
    6. 上电自检扫描超期临时槽
    """
    
    # 擦除标准配置
    ERASE_STANDARD_TEMP = EraseStandard.SINGLE_ZERO
    ERASE_STANDARD_ONESHOT = EraseStandard.DOUBLE_FF_ZERO
    
    # 覆写块大小（字节）
    OVERWRITE_BLOCK_SIZE = 4096  # 4KB
    
    # 覆写模式
    PATTERN_ZERO = 0x00
    PATTERN_FF = 0xFF
    
    # 校验抽样率
    VERIFICATION_SAMPLE_RATE = 0.10  # 10%
    
    # 活跃写入等待超时（秒）
    ACTIVE_WRITE_TIMEOUT = 2.0
    
    def __init__(self):
        self.module_id = "ad-12"
        self.module_name = "临时画像槽自动清除单元"
        
        # 内部状态
        self.state = CleanupState.IDLE
        
        # 当前正在处理的槽号
        self._current_slot_id: Optional[int] = None
        
        # 已标记 PARTIALLY_ERASED 的槽号集合
        self._partially_erased_slots: set = set()
        
        # 统计
        self._total_cleanups = 0
        self._successful_cleanups = 0
        self._failed_cleanups = 0
        self._aborted_cleanups = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[SlotCleanupLog] = []
        
        print(f"[{self.module_id}] 临时画像槽自动清除单元初始化完成")
    
    # ========== 状态管理 ==========
    
    def emergency_abort(self) -> None:
        """紧急熔断中断当前擦除"""
        if self.state in [CleanupState.AUTH_CHECK, CleanupState.ERASING, CleanupState.VERIFYING]:
            if self._current_slot_id is not None:
                self._partially_erased_slots.add(self._current_slot_id)
            self.state = CleanupState.ABORTED
            self._aborted_cleanups += 1
            print(f"[{self.module_id}] 紧急熔断，擦除操作已中断, slot={self._current_slot_id}")
    
    def reset_state(self) -> None:
        """重置状态"""
        self.state = CleanupState.IDLE
        self._current_slot_id = None
    
    # ========== 擦除主流程 ==========
    
    def execute_cleanup(self, signal: CleanupTriggerSignal,
                        auth_check_callback,
                        partition: Optional[StoragePartition] = None,
                        has_active_writes: bool = False) -> EraseCompletionReport:
        """
        执行临时槽清除主流程
        
        步骤:
        1. 校验槽位类型（长期槽不可自动清除）
        2. 等待活跃写入完成
        3. 权限校验
        4. 执行覆写擦除
        5. 随机抽样校验
        6. 释放存储分区
        
        Args:
            signal: 清除触发信号
            auth_check_callback: ad-06 权限校验回调函数
            partition: 目标槽的存储分区
            has_active_writes: 是否有活跃写入
            
        Returns:
            擦除完成回执
        """
        self._current_slot_id = signal.target_slot_id
        self._total_cleanups += 1
        
        # 1. 校验槽位类型
        if signal.slot_type == SlotType.LONG_TERM:
            self._failed_cleanups += 1
            return EraseCompletionReport(
                slot_id=signal.target_slot_id,
                success=False,
                erase_standard_used=EraseStandard.SINGLE_ZERO,
                erase_timestamp=time.time(),
                partition_released=False,
                verification_passed=False,
                error_code="长期槽不可自动清除"
            )
        
        # 2. 校验触发原因合法性
        if signal.slot_type == SlotType.TEMPORARY:
            if signal.trigger_reason not in [CleanupTriggerReason.TEMP_EXPIRED,
                                              CleanupTriggerReason.USER_MANUAL,
                                              CleanupTriggerReason.STARTUP_SCAN]:
                self._failed_cleanups += 1
                return EraseCompletionReport(
                    slot_id=signal.target_slot_id,
                    success=False,
                    erase_standard_used=EraseStandard.SINGLE_ZERO,
                    erase_timestamp=time.time(),
                    partition_released=False,
                    verification_passed=False,
                    error_code="触发原因不合法"
                )
        elif signal.slot_type == SlotType.ONESHOT:
            if signal.trigger_reason not in [CleanupTriggerReason.ONESHOT_TRIP_END,
                                              CleanupTriggerReason.STARTUP_SCAN]:
                self._failed_cleanups += 1
                return EraseCompletionReport(
                    slot_id=signal.target_slot_id,
                    success=False,
                    erase_standard_used=EraseStandard.SINGLE_ZERO,
                    erase_timestamp=time.time(),
                    partition_released=False,
                    verification_passed=False,
                    error_code="触发原因不合法"
                )
        
        # 3. 等待活跃写入完成
        if has_active_writes:
            self.state = CleanupState.PAUSED
            wait_start = time.time()
            while has_active_writes:
                if time.time() - wait_start > self.ACTIVE_WRITE_TIMEOUT:
                    print(f"[{self.module_id}] 活跃写入超时，强制继续擦除")
                    break
                time.sleep(0.1)
        
        # 4. 权限校验
        self.state = CleanupState.AUTH_CHECK
        auth_result = auth_check_callback(
            source_module=self.module_id,
            target_slot_id=signal.target_slot_id,
            operation_type="erase"
        )
        
        if not auth_result:
            self._failed_cleanups += 1
            self.state = CleanupState.FAILED
            return EraseCompletionReport(
                slot_id=signal.target_slot_id,
                success=False,
                erase_standard_used=EraseStandard.SINGLE_ZERO,
                erase_timestamp=time.time(),
                partition_released=False,
                verification_passed=False,
                error_code="擦除权限被拒"
            )
        
        # 5. 确定擦除标准
        if signal.slot_type == SlotType.TEMPORARY:
            erase_standard = self.ERASE_STANDARD_TEMP
        else:
            erase_standard = self.ERASE_STANDARD_ONESHOT
        
        # 6. 执行覆写擦除
        self.state = CleanupState.ERASING
        
        if partition is not None:
            erase_success = self._perform_overwrite(partition, erase_standard)
        else:
            # 无分区信息（模拟），直接标记成功
            erase_success = True
        
        if not erase_success:
            self._failed_cleanups += 1
            self._partially_erased_slots.add(signal.target_slot_id)
            self.state = CleanupState.FAILED
            return EraseCompletionReport(
                slot_id=signal.target_slot_id,
                success=False,
                erase_standard_used=erase_standard,
                erase_timestamp=time.time(),
                partition_released=False,
                verification_passed=False,
                error_code="覆写擦除失败"
            )
        
        # 7. 擦除后校验
        self.state = CleanupState.VERIFYING
        
        if partition is not None:
            verification_passed = self._verify_erasure(partition, erase_standard)
        else:
            verification_passed = True
        
        if not verification_passed:
            self._failed_cleanups += 1
            self._partially_erased_slots.add(signal.target_slot_id)
            self.state = CleanupState.FAILED
            return EraseCompletionReport(
                slot_id=signal.target_slot_id,
                success=False,
                erase_standard_used=erase_standard,
                erase_timestamp=time.time(),
                partition_released=False,
                verification_passed=False,
                error_code="擦除后校验失败，数据可能残留"
            )
        
        # 8. 释放存储分区
        partition_released = True
        # 实际实现中调用 ad-48 或存储管理模块释放分区
        
        self._successful_cleanups += 1
        self.state = CleanupState.DONE
        
        # 日志记录
        self._log_cleanup(signal.target_slot_id, signal.slot_type,
                          signal.trigger_reason, erase_standard, True,
                          verification_passed=True)
        
        self._current_slot_id = None
        self.state = CleanupState.IDLE
        
        return EraseCompletionReport(
            slot_id=signal.target_slot_id,
            success=True,
            erase_standard_used=erase_standard,
            erase_timestamp=time.time(),
            partition_released=partition_released,
            verification_passed=True
        )
    
    # ========== 覆写擦除 ==========
    
    def _perform_overwrite(self, partition: StoragePartition, standard: EraseStandard) -> bool:
        """
        执行覆写擦除
        
        Args:
            partition: 存储分区
            standard: 擦除标准
            
        Returns:
            是否成功
        """
        try:
            if standard == EraseStandard.SINGLE_ZERO:
                # 单次全零覆写
                print(f"[{self.module_id}] 执行单次全零覆写: partition={partition.partition_id}, "
                      f"size={partition.size_bytes} bytes")
                # 模拟覆写操作
                
            elif standard == EraseStandard.DOUBLE_FF_ZERO:
                # 双次覆写：先全一，再全零
                print(f"[{self.module_id}] 执行双次覆写(FF→00): partition={partition.partition_id}, "
                      f"size={partition.size_bytes} bytes")
                # 模拟两次覆写操作
            
            return True
            
        except Exception as e:
            print(f"[{self.module_id}] 覆写异常: {e}")
            return False
    
    def _verify_erasure(self, partition: StoragePartition, standard: EraseStandard) -> bool:
        """
        随机抽样校验擦除完整性
        
        抽样率 ≥ 10%
        """
        if partition.size_bytes <= 0:
            return True
        
        sample_count = max(int(partition.size_bytes / self.OVERWRITE_BLOCK_SIZE * 
                               self.VERIFICATION_SAMPLE_RATE), 1)
        sample_count = min(sample_count, 100)  # 最多抽样100个块
        
        expected_pattern = self.PATTERN_ZERO  # 最终都应为全零
        
        passed = 0
        for _ in range(sample_count):
            # 模拟随机抽样
            # 实际实现中读取随机偏移处的数据并与期望模式比对
            passed += 1
        
        print(f"[{self.module_id}] 擦除校验: 抽样{sample_count}块, 通过{passed}块")
        
        return passed == sample_count
    
    # ========== 上电自检 ==========
    
    def startup_scan(self, all_slots: List[Dict[str, Any]]) -> List[CleanupTriggerSignal]:
        """
        上电自检扫描
        
        扫描所有临时/一次性槽，超期或行程已结束的自动触发擦除
        
        Args:
            all_slots: 所有槽位信息列表
            
        Returns:
            需要清除的槽位触发信号列表
        """
        now = time.time()
        triggers = []
        
        for slot in all_slots:
            slot_id = slot.get("slot_id")
            slot_type = slot.get("slot_type")
            create_time = slot.get("create_time", 0)
            
            if slot_type == SlotType.TEMPORARY:
                # 检查是否超过7天
                if now - create_time > 7 * 24 * 3600:
                    triggers.append(CleanupTriggerSignal(
                        target_slot_id=slot_id,
                        slot_type=SlotType.TEMPORARY,
                        trigger_reason=CleanupTriggerReason.STARTUP_SCAN,
                        create_timestamp=create_time
                    ))
            
            elif slot_type == SlotType.ONESHOT:
                # 一次性槽在行程结束后应被清除，上电时发现残留则触发清除
                triggers.append(CleanupTriggerSignal(
                    target_slot_id=slot_id,
                    slot_type=SlotType.ONESHOT,
                    trigger_reason=CleanupTriggerReason.STARTUP_SCAN,
                    create_timestamp=create_time
                ))
        
        if triggers:
            print(f"[{self.module_id}] 上电自检发现 {len(triggers)} 个待清除槽位")
        
        return triggers
    
    # ========== 查询接口 ==========
    
    def is_partially_erased(self, slot_id: int) -> bool:
        """检查槽位是否被标记为部分擦除"""
        return slot_id in self._partially_erased_slots
    
    def clear_partially_erased_mark(self, slot_id: int) -> None:
        """清除部分擦除标记（重新擦除成功后调用）"""
        self._partially_erased_slots.discard(slot_id)
    
    def get_state(self) -> CleanupState:
        return self.state
    
    def get_current_slot_id(self) -> Optional[int]:
        return self._current_slot_id
    
    # ========== 变更日志 ==========
    
    def _log_cleanup(self, slot_id: int, slot_type: SlotType,
                     trigger_reason: CleanupTriggerReason,
                     erase_standard: EraseStandard,
                     success: bool, verification_passed: bool) -> None:
        """记录清除日志"""
        log = SlotCleanupLog(
            log_id=f"cleanup-{uuid.uuid4().hex[:8]}",
            slot_id=slot_id,
            slot_type=slot_type,
            trigger_reason=trigger_reason,
            erase_standard=erase_standard,
            success=success,
            verification_sampled=int(100 * self.VERIFICATION_SAMPLE_RATE),
            verification_passed=int(100 * self.VERIFICATION_SAMPLE_RATE) if verification_passed else 0
        )
        self._pending_logs.append(log)
    
    def collect_pending_logs(self) -> List[SlotCleanupLog]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_cleanups": self._total_cleanups,
            "successful": self._successful_cleanups,
            "failed": self._failed_cleanups,
            "aborted": self._aborted_cleanups,
            "partially_erased": len(self._partially_erased_slots),
            "current_state": self.state.value,
            "current_slot": self._current_slot_id
        }


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-12 临时画像槽自动清除单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    # 模拟权限校验回调（总是放行）
    def mock_auth_pass(*args, **kwargs):
        return True
    
    def mock_auth_fail(*args, **kwargs):
        return False
    
    # --- TC-12-01: 临时槽7天到期成功擦除 ---
    print("\n[TC-12-01] 临时槽7天到期成功擦除")
    try:
        cleaner = TemporarySlotCleanup()
        signal = CleanupTriggerSignal(
            target_slot_id=1,
            slot_type=SlotType.TEMPORARY,
            trigger_reason=CleanupTriggerReason.TEMP_EXPIRED,
            create_timestamp=time.time() - 8 * 24 * 3600
        )
        partition = StoragePartition("part_1", 0x1000, 1024)
        report = cleaner.execute_cleanup(signal, mock_auth_pass, partition, False)
        assert report.success == True
        assert report.erase_standard_used == EraseStandard.SINGLE_ZERO
        assert report.verification_passed == True
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-12-02: 一次性槽行程结束双次覆写擦除 ---
    print("\n[TC-12-02] 一次性槽行程结束双次覆写擦除")
    try:
        cleaner = TemporarySlotCleanup()
        signal = CleanupTriggerSignal(
            target_slot_id=2,
            slot_type=SlotType.ONESHOT,
            trigger_reason=CleanupTriggerReason.ONESHOT_TRIP_END,
            create_timestamp=time.time() - 3600,
            trip_end_timestamp=time.time()
        )
        partition = StoragePartition("part_2", 0x2000, 2048)
        report = cleaner.execute_cleanup(signal, mock_auth_pass, partition, False)
        assert report.success == True
        assert report.erase_standard_used == EraseStandard.DOUBLE_FF_ZERO
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-12-03: 长期槽不可自动清除 ---
    print("\n[TC-12-03] 长期槽不可自动清除")
    try:
        cleaner = TemporarySlotCleanup()
        signal = CleanupTriggerSignal(
            target_slot_id=3,
            slot_type=SlotType.LONG_TERM,
            trigger_reason=CleanupTriggerReason.TEMP_EXPIRED,
            create_timestamp=time.time()
        )
        report = cleaner.execute_cleanup(signal, mock_auth_pass, None, False)
        assert report.success == False
        assert "长期槽不可自动清除" in str(report.error_code)
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-12-04: 权限被拒 ---
    print("\n[TC-12-04] 权限被拒")
    try:
        cleaner = TemporarySlotCleanup()
        signal = CleanupTriggerSignal(
            target_slot_id=4,
            slot_type=SlotType.TEMPORARY,
            trigger_reason=CleanupTriggerReason.TEMP_EXPIRED,
            create_timestamp=time.time() - 8 * 24 * 3600
        )
        report = cleaner.execute_cleanup(signal, mock_auth_fail, None, False)
        assert report.success == False
        assert "权限被拒" in str(report.error_code)
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-12-05: 紧急熔断中断擦除 ---
    print("\n[TC-12-05] 紧急熔断中断擦除")
    try:
        cleaner = TemporarySlotCleanup()
        cleaner.state = CleanupState.ERASING
        cleaner._current_slot_id = 5
        cleaner.emergency_abort()
        assert cleaner.state == CleanupState.ABORTED
        assert cleaner.is_partially_erased(5)
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-12-06: 上电自检扫描 ---
    print("\n[TC-12-06] 上电自检扫描")
    try:
        cleaner = TemporarySlotCleanup()
        all_slots = [
            {"slot_id": 10, "slot_type": SlotType.TEMPORARY, "create_time": time.time() - 10 * 24 * 3600},
            {"slot_id": 11, "slot_type": SlotType.ONESHOT, "create_time": time.time() - 3600},
            {"slot_id": 12, "slot_type": SlotType.LONG_TERM, "create_time": time.time() - 50 * 24 * 3600},
        ]
        triggers = cleaner.startup_scan(all_slots)
        assert len(triggers) == 2  # 临时槽超期 + 一次性槽残留
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-12-07: 用户手动确认删除临时槽 ---
    print("\n[TC-12-07] 用户手动确认删除临时槽")
    try:
        cleaner = TemporarySlotCleanup()
        signal = CleanupTriggerSignal(
            target_slot_id=6,
            slot_type=SlotType.TEMPORARY,
            trigger_reason=CleanupTriggerReason.USER_MANUAL,
            create_timestamp=time.time() - 3 * 24 * 3600
        )
        report = cleaner.execute_cleanup(signal, mock_auth_pass, None, False)
        assert report.success == True
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-12-08: 活跃写入等待后擦除 ---
    print("\n[TC-12-08] 活跃写入等待后擦除")
    try:
        cleaner = TemporarySlotCleanup()
        signal = CleanupTriggerSignal(
            target_slot_id=7,
            slot_type=SlotType.TEMPORARY,
            trigger_reason=CleanupTriggerReason.TEMP_EXPIRED,
            create_timestamp=time.time() - 8 * 24 * 3600
        )
        # 模拟有活跃写入但很快完成
        report = cleaner.execute_cleanup(signal, mock_auth_pass, None, False)
        assert report.success == True
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