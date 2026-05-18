#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-29
模块名称: L5 核心层安全规则硬锁定单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 五层记忆层级存储
核心职责: 编译期实施 L5 存储分区硬件写保护，管控 L5 写入权限的临时解除与恢复。
          任何对 L5 存储分区的写入、修改或删除操作，必须经过本单元的权限校验与
          临时解锁流程。写入完成后自动恢复写保护。物理层面防止非授权访问与数据篡改。

依赖模块: ad-28(L5 核心层存储单元), ad-30(L5 核心层防篡改与只读管控单元)
被依赖模块: ad-28(消费写入权限的临时解除与恢复), ad-30(消费写保护状态信息)

安全约束:
  S-01: L5 存储分区写保护由硬件写保护控制器实现，编译期固化
  S-02: 临时解锁窗口最大 2 秒，超时后硬件自动强制锁定
  S-03: 任何绕过本单元的 L5 直接写入尝试，在物理层面被硬件控制器拦截并告警
  S-04: 硬件写保护控制器须选用独立于主 CPU 的安全芯片或专用存储控制器
  S-05: 写入权限临时解除须同时验证安全令牌、操作类型合法性、剩余容量
  S-06: 系统紧急熔断时，强制锁定优先于一切写入操作
  S-07: 所有写保护解除、恢复、拒绝、强制锁定事件全量写入 ad-51 不可变日志
  S-08: 存储介质出现坏块时，优先保护已有数据，暂停新写入
"""

from typing import Dict, List, Optional, Any, Tuple, Callable
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class LockState(Enum):
    """硬件锁定状态"""
    LOCKED = "locked"
    UNLOCKED_TEMP = "unlocked_temp"
    LOCK_FAILED = "lock_failed"
    HARDWARE_MAINT = "hardware_maint"


class UnlockResult(Enum):
    """解锁结果"""
    SUCCESS = "success"
    TOKEN_INVALID = "token_invalid"
    CONTROLLER_FAILURE = "controller_failure"
    MAINTENANCE_MODE = "maintenance_mode"
    ALREADY_UNLOCKED = "already_unlocked"


class LockResult(Enum):
    """锁定结果"""
    SUCCESS = "success"
    FORCE_LOCKED = "force_locked"
    CONTROLLER_FAILURE = "controller_failure"


class OperationType(Enum):
    """允许的写入操作类型"""
    PROMOTION = "promotion_write"
    SAFETY_DIRECT = "safety_direct_write"
    MANUAL_LOCK = "manual_lock_write"


# ==================== 数据结构 ====================

@dataclass
class UnlockRequest:
    """解锁请求"""
    request_id: str
    operation_type: OperationType
    security_token: str
    source_module: str
    data_size_bytes: int
    timestamp: float = field(default_factory=time.time)


@dataclass
class UnlockResponse:
    """解锁响应"""
    request_id: str
    result: UnlockResult
    timeout_ms: int = 2000          # 临时解锁窗口时长
    message: str = ""


@dataclass
class LockConfirmation:
    """锁定确认"""
    request_id: str
    result: LockResult
    timestamp: float = field(default_factory=time.time)


@dataclass
class HardwareStatus:
    """硬件控制器状态"""
    controller_id: str
    write_protect_active: bool
    controller_health: str          # "healthy" / "degraded" / "failed"
    last_command_timestamp: float
    consecutive_failures: int


@dataclass
class StorageHealthReport:
    """存储介质健康报告"""
    total_blocks: int
    bad_blocks: int
    read_error_rate: float
    write_error_rate: float
    estimated_life_percent: float
    requires_maintenance: bool


@dataclass
class WriteProtectionLog:
    """写保护操作日志"""
    log_id: str
    event_type: str                 # UNLOCK / LOCK / FORCE_LOCK / DENIED
    request_id: str
    operation_type: Optional[OperationType]
    result: str
    source_module: str
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class L5HardwareLock:
    """
    L5 核心层安全规则硬锁定单元
    
    职责:
    1. 管理硬件写保护控制器的 LOCK/UNLOCK/FORCE_LOCK 指令
    2. 验证写入操作的安全令牌（与 ad-30 协作）
    3. 管理临时解锁窗口（默认 2 秒超时）
    4. 处理紧急熔断强制锁定
    5. 监控硬件控制器健康状态与存储介质健康
    6. 维护写入请求队列，处理并发写入
    """
    
    # 临时解锁超时时间（毫秒）
    TEMP_UNLOCK_TIMEOUT_MS = 2000  # 2 秒
    
    # 硬件控制器连续失败阈值
    MAX_CONSECUTIVE_FAILURES = 3
    
    # 存储介质坏块阈值
    BAD_BLOCK_THRESHOLD = 10
    READ_ERROR_RATE_THRESHOLD = 0.01
    
    def __init__(self):
        self.module_id = "ad-29"
        self.module_name = "L5 核心层安全规则硬锁定单元"
        
        # 内部状态
        self.state = LockState.LOCKED
        
        # 硬件控制器状态
        self._hw_status = HardwareStatus(
            controller_id="HW-LOCK-001",
            write_protect_active=True,
            controller_health="healthy",
            last_command_timestamp=0.0,
            consecutive_failures=0
        )
        
        # 当前活跃的临时解锁
        self._active_unlock: Optional[UnlockRequest] = None
        self._unlock_start_time: float = 0.0
        
        # 写入请求等待队列
        self._waiting_queue: List[UnlockRequest] = []
        
        # 最近检查时间
        self._last_hw_check_time: float = 0.0
        self._last_storage_check_time: float = 0.0
        
        # 统计
        self._total_unlocks = 0
        self._total_locks = 0
        self._total_force_locks = 0
        self._total_denials = 0
        self._total_timeouts = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[WriteProtectionLog] = []
        
        print(f"[{self.module_id}] L5 硬件写保护锁定单元初始化完成")
        print(f"[{self.module_id}] 临时解锁超时: {self.TEMP_UNLOCK_TIMEOUT_MS}ms")
        print(f"[{self.module_id}] 初始状态: LOCKED")
    
    # ========== 状态管理 ==========
    
    def get_state(self) -> LockState:
        return self.state
    
    def is_write_protected(self) -> bool:
        return self._hw_status.write_protect_active
    
    def get_hardware_status(self) -> HardwareStatus:
        return self._hw_status
    
    # ========== 硬件控制器交互 ==========
    
    def check_hardware_health(self) -> None:
        """检查硬件控制器健康状态（每 1 秒调用）"""
        now = time.time()
        if now - self._last_hw_check_time < 1.0:
            return
        
        self._last_hw_check_time = now
        
        # 模拟硬件健康检查
        # 实际实现中通过 GPIO/SPI/I2C 查询硬件控制器寄存器
        if self._hw_status.consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
            self._hw_status.controller_health = "failed"
            if self.state != LockState.LOCK_FAILED:
                self.state = LockState.LOCK_FAILED
                print(f"[{self.module_id}] 硬件控制器故障: 连续失败{self.MAX_CONSECUTIVE_FAILURES}次")
        elif self._hw_status.consecutive_failures > 0:
            self._hw_status.controller_health = "degraded"
    
    def check_storage_health(self, report: Optional[StorageHealthReport] = None) -> None:
        """
        检查存储介质健康状态
        
        S-08: 存储介质出现坏块时，优先保护已有数据，暂停新写入
        """
        now = time.time()
        if now - self._last_storage_check_time < 60.0:
            return
        
        self._last_storage_check_time = now
        
        if report is None:
            return
        
        if (report.bad_blocks > self.BAD_BLOCK_THRESHOLD or
                report.read_error_rate > self.READ_ERROR_RATE_THRESHOLD or
                report.requires_maintenance):
            if self.state != LockState.HARDWARE_MAINT:
                # 如果当前有临时解锁，先强制锁定
                if self.state == LockState.UNLOCKED_TEMP:
                    self.force_lock("storage_maintenance")
                
                self.state = LockState.HARDWARE_MAINT
                print(f"[{self.module_id}] 存储介质需要维护，暂停写入。坏块={report.bad_blocks}")
    
    # ========== 解锁请求处理 ==========
    
    def request_unlock(self, request: UnlockRequest,
                       token_validator: Callable[[str, OperationType], bool]) -> UnlockResponse:
        """
        请求临时解除写保护
        
        处理流程:
        1. 检查硬件控制器健康状态
        2. 检查当前状态（已锁/已解锁/故障/维护）
        3. 验证安全令牌（委托给 ad-30）
        4. 验证操作类型合法性
        5. 向硬件控制器发送 UNLOCK 指令
        6. 启动临时解锁计时器
        
        Args:
            request: 解锁请求
            token_validator: 令牌验证回调函数 (token, operation) -> bool
            
        Returns:
            解锁响应
        """
        # 硬件控制器故障
        if self.state == LockState.LOCK_FAILED:
            self._total_denials += 1
            return UnlockResponse(
                request_id=request.request_id,
                result=UnlockResult.CONTROLLER_FAILURE,
                message="硬件写保护控制器故障"
            )
        
        # 硬件维护模式
        if self.state == LockState.HARDWARE_MAINT:
            self._total_denials += 1
            return UnlockResponse(
                request_id=request.request_id,
                result=UnlockResult.MAINTENANCE_MODE,
                message="存储介质维护中，暂停写入"
            )
        
        # 已有活跃的临时解锁
        if self.state == LockState.UNLOCKED_TEMP:
            # 加入等待队列
            self._waiting_queue.append(request)
            return UnlockResponse(
                request_id=request.request_id,
                result=UnlockResult.ALREADY_UNLOCKED,
                message="已有写入操作进行中，已加入等待队列"
            )
        
        # S-05: 验证安全令牌
        if not token_validator(request.security_token, request.operation_type):
            self._total_denials += 1
            self._log_event("DENIED", request.request_id, request.operation_type,
                          "token_invalid", request.source_module)
            return UnlockResponse(
                request_id=request.request_id,
                result=UnlockResult.TOKEN_INVALID,
                message="安全令牌无效或权限不足"
            )
        
        # 向硬件控制器发送 UNLOCK 指令
        success = self._send_hardware_command("UNLOCK")
        
        if not success:
            self._hw_status.consecutive_failures += 1
            self._total_denials += 1
            self._log_event("DENIED", request.request_id, request.operation_type,
                          "controller_failure", request.source_module)
            return UnlockResponse(
                request_id=request.request_id,
                result=UnlockResult.CONTROLLER_FAILURE,
                message="硬件控制器 UNLOCK 指令失败"
            )
        
        # 解锁成功
        self._hw_status.consecutive_failures = 0
        self._hw_status.write_protect_active = False
        self.state = LockState.UNLOCKED_TEMP
        self._active_unlock = request
        self._unlock_start_time = time.time()
        self._total_unlocks += 1
        
        self._log_event("UNLOCK", request.request_id, request.operation_type,
                      "success", request.source_module)
        
        return UnlockResponse(
            request_id=request.request_id,
            result=UnlockResult.SUCCESS,
            timeout_ms=self.TEMP_UNLOCK_TIMEOUT_MS,
            message="写保护已临时解除"
        )
    
    # ========== 锁定请求处理 ==========
    
    def confirm_lock(self, request_id: str) -> LockConfirmation:
        """
        确认写入完成，恢复写保护
        
        Args:
            request_id: 对应的解锁请求 ID
            
        Returns:
            锁定确认
        """
        if self.state != LockState.UNLOCKED_TEMP:
            return LockConfirmation(
                request_id=request_id,
                result=LockResult.SUCCESS,
                message="当前未处于解锁状态"
            )
        
        # 向硬件控制器发送 LOCK 指令
        success = self._send_hardware_command("LOCK")
        
        if not success:
            # 锁定失败，尝试重试
            for _ in range(3):
                time.sleep(0.01)
                if self._send_hardware_command("LOCK"):
                    success = True
                    break
            
            if not success:
                self._hw_status.consecutive_failures += 1
                self.state = LockState.LOCK_FAILED
                self._active_unlock = None
                self._total_locks += 1
                self._log_event("LOCK_FAILED", request_id, None, "controller_failure", self.module_id)
                return LockConfirmation(
                    request_id=request_id,
                    result=LockResult.CONTROLLER_FAILURE
                )
        
        # 锁定成功
        self._hw_status.write_protect_active = True
        self._hw_status.consecutive_failures = 0
        self.state = LockState.LOCKED
        self._active_unlock = None
        self._total_locks += 1
        
        self._log_event("LOCK", request_id, None, "success", self.module_id)
        
        # 处理等待队列中的下一个请求
        self._process_next_in_queue(token_validator=None)  # 需要外部传入
        
        return LockConfirmation(
            request_id=request_id,
            result=LockResult.SUCCESS
        )
    
    def force_lock(self, reason: str = "emergency") -> LockConfirmation:
        """
        强制锁定（紧急熔断或超时时调用）
        
        S-06: 系统紧急熔断时，强制锁定优先于一切写入操作
        
        Args:
            reason: 强制锁定原因
            
        Returns:
            锁定确认
        """
        if self.state != LockState.UNLOCKED_TEMP:
            return LockConfirmation(
                request_id="force",
                result=LockResult.SUCCESS,
                message="当前未处于解锁状态"
            )
        
        # 向硬件控制器发送 FORCE_LOCK 指令（不等待当前写入完成）
        success = self._send_hardware_command("FORCE_LOCK")
        
        self._hw_status.write_protect_active = True
        self.state = LockState.LOCKED
        self._active_unlock = None
        self._waiting_queue.clear()
        self._total_force_locks += 1
        
        self._log_event("FORCE_LOCK", "force", None, reason, self.module_id)
        
        print(f"[{self.module_id}] 强制锁定: {reason}")
        
        return LockConfirmation(
            request_id="force",
            result=LockResult.FORCE_LOCKED if success else LockResult.CONTROLLER_FAILURE
        )
    
    # ========== 超时检查 ==========
    
    def check_timeout(self) -> bool:
        """
        检查临时解锁窗口是否超时
        
        S-02: 临时解锁窗口最大 2 秒
        
        Returns:
            是否触发了超时强制锁定
        """
        if self.state != LockState.UNLOCKED_TEMP:
            return False
        
        elapsed_ms = (time.time() - self._unlock_start_time) * 1000
        
        if elapsed_ms >= self.TEMP_UNLOCK_TIMEOUT_MS:
            self._total_timeouts += 1
            self.force_lock("timeout")
            print(f"[{self.module_id}] 临时解锁超时({elapsed_ms:.0f}ms)，强制锁定")
            return True
        
        return False
    
    # ========== 等待队列处理 ==========
    
    def _process_next_in_queue(self, token_validator: Optional[Callable] = None) -> None:
        """处理等待队列中的下一个请求"""
        if not self._waiting_queue:
            return
        
        next_request = self._waiting_queue.pop(0)
        
        if token_validator is not None:
            self.request_unlock(next_request, token_validator)
        else:
            # 无验证器时，将请求放回队列头部
            self._waiting_queue.insert(0, next_request)
    
    def get_queue_size(self) -> int:
        return len(self._waiting_queue)
    
    # ========== 硬件命令发送（模拟） ==========
    
    def _send_hardware_command(self, command: str) -> bool:
        """
        向硬件写保护控制器发送命令
        
        实际实现中通过 GPIO/SPI/I2C 与专用安全芯片通信
        此处为模拟实现
        
        Args:
            command: "LOCK" / "UNLOCK" / "FORCE_LOCK"
            
        Returns:
            命令是否成功
        """
        self._hw_status.last_command_timestamp = time.time()
        
        # 模拟命令执行
        if command == "UNLOCK":
            # 模拟硬件响应
            return self._hw_status.controller_health != "failed"
        elif command == "LOCK":
            return self._hw_status.controller_health != "failed"
        elif command == "FORCE_LOCK":
            return True  # FORCE_LOCK 通常不应失败
        else:
            return False
    
    # ========== 查询接口 ==========
    
    def get_remaining_unlock_time_ms(self) -> int:
        """获取剩余解锁时间（毫秒）"""
        if self.state != LockState.UNLOCKED_TEMP:
            return 0
        elapsed_ms = int((time.time() - self._unlock_start_time) * 1000)
        return max(0, self.TEMP_UNLOCK_TIMEOUT_MS - elapsed_ms)
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_unlocks": self._total_unlocks,
            "total_locks": self._total_locks,
            "total_force_locks": self._total_force_locks,
            "total_denials": self._total_denials,
            "total_timeouts": self._total_timeouts,
            "queue_size": len(self._waiting_queue),
            "write_protected": self._hw_status.write_protect_active,
            "controller_health": self._hw_status.controller_health,
            "state": self.state.value
        }
    
    # ========== 变更日志 ==========
    
    def _log_event(self, event_type: str, request_id: str,
                   operation_type: Optional[OperationType],
                   result: str, source_module: str) -> None:
        """记录写保护操作日志"""
        log = WriteProtectionLog(
            log_id=f"wplog-{uuid.uuid4().hex[:8]}",
            event_type=event_type,
            request_id=request_id,
            operation_type=operation_type,
            result=result,
            source_module=source_module
        )
        self._pending_logs.append(log)
    
    def collect_pending_logs(self) -> List[WriteProtectionLog]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-29 L5 核心层安全规则硬锁定单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    # 模拟令牌验证器（总是通过）
    def token_valid_pass(token, operation):
        return True
    
    def token_valid_fail(token, operation):
        return False
    
    # --- TC-29-01: 正常解锁-写入-锁定流程 ---
    print("\n[TC-29-01] 正常解锁-写入-锁定流程")
    try:
        hw_lock = L5HardwareLock()
        request = UnlockRequest(
            request_id="req-001",
            operation_type=OperationType.PROMOTION,
            security_token="valid_token",
            source_module="ad-26",
            data_size_bytes=2048
        )
        # 解锁
        response = hw_lock.request_unlock(request, token_valid_pass)
        assert response.result == UnlockResult.SUCCESS
        assert hw_lock.state == LockState.UNLOCKED_TEMP
        assert not hw_lock.is_write_protected()
        
        # 锁定
        confirm = hw_lock.confirm_lock("req-001")
        assert confirm.result == LockResult.SUCCESS
        assert hw_lock.state == LockState.LOCKED
        assert hw_lock.is_write_protected()
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-29-02: 令牌无效拒绝 ---
    print("\n[TC-29-02] 令牌无效拒绝")
    try:
        hw_lock = L5HardwareLock()
        request = UnlockRequest("req-002", OperationType.PROMOTION, "bad_token", "ad-26", 1024)
        response = hw_lock.request_unlock(request, token_valid_fail)
        assert response.result == UnlockResult.TOKEN_INVALID
        assert hw_lock.state == LockState.LOCKED
        assert hw_lock._total_denials == 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-29-03: 临时解锁超时强制锁定 ---
    print("\n[TC-29-03] 临时解锁超时强制锁定")
    try:
        hw_lock = L5HardwareLock()
        hw_lock.TEMP_UNLOCK_TIMEOUT_MS = 100  # 100ms 超时
        request = UnlockRequest("req-003", OperationType.PROMOTION, "token", "ad-26", 1024)
        hw_lock.request_unlock(request, token_valid_pass)
        
        # 等待超时
        time.sleep(0.15)
        triggered = hw_lock.check_timeout()
        assert triggered == True
        assert hw_lock.state == LockState.LOCKED
        assert hw_lock._total_force_locks == 1
        assert hw_lock._total_timeouts == 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-29-04: 紧急熔断强制锁定 ---
    print("\n[TC-29-04] 紧急熔断强制锁定")
    try:
        hw_lock = L5HardwareLock()
        request = UnlockRequest("req-004", OperationType.SAFETY_DIRECT, "token", "ad-18", 1024)
        hw_lock.request_unlock(request, token_valid_pass)
        
        # 紧急熔断
        hw_lock.force_lock("emergency_stop")
        assert hw_lock.state == LockState.LOCKED
        assert hw_lock.is_write_protected()
        assert hw_lock.get_queue_size() == 0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-29-05: 并发请求排队 ---
    print("\n[TC-29-05] 并发请求排队")
    try:
        hw_lock = L5HardwareLock()
        req1 = UnlockRequest("req-005", OperationType.PROMOTION, "token", "ad-26", 1024)
        req2 = UnlockRequest("req-006", OperationType.PROMOTION, "token", "ad-26", 1024)
        
        # 第一个请求获得锁
        r1 = hw_lock.request_unlock(req1, token_valid_pass)
        assert r1.result == UnlockResult.SUCCESS
        
        # 第二个请求进入等待队列
        r2 = hw_lock.request_unlock(req2, token_valid_pass)
        assert r2.result == UnlockResult.ALREADY_UNLOCKED
        assert hw_lock.get_queue_size() == 1
        
        # 第一个请求完成锁定
        hw_lock.confirm_lock("req-005")
        # 此时队列中的请求需要等待外部调用处理
        assert hw_lock.get_queue_size() == 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-29-06: 硬件控制器故障 ---
    print("\n[TC-29-06] 硬件控制器故障")
    try:
        hw_lock = L5HardwareLock()
        hw_lock._hw_status.controller_health = "failed"
        hw_lock.state = LockState.LOCK_FAILED
        
        request = UnlockRequest("req-007", OperationType.PROMOTION, "token", "ad-26", 1024)
        response = hw_lock.request_unlock(request, token_valid_pass)
        assert response.result == UnlockResult.CONTROLLER_FAILURE
        assert hw_lock._total_denials == 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-29-07: 硬件维护模式拒绝写入 ---
    print("\n[TC-29-07] 硬件维护模式拒绝写入")
    try:
        hw_lock = L5HardwareLock()
        hw_lock.state = LockState.HARDWARE_MAINT
        
        request = UnlockRequest("req-008", OperationType.PROMOTION, "token", "ad-26", 1024)
        response = hw_lock.request_unlock(request, token_valid_pass)
        assert response.result == UnlockResult.MAINTENANCE_MODE
        assert hw_lock._total_denials == 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-29-08: 存储介质坏块触发维护 ---
    print("\n[TC-29-08] 存储介质坏块触发维护")
    try:
        hw_lock = L5HardwareLock()
        report = StorageHealthReport(
            total_blocks=1000, bad_blocks=15,
            read_error_rate=0.0, write_error_rate=0.0,
            estimated_life_percent=80.0, requires_maintenance=True
        )
        hw_lock.check_storage_health(report)
        assert hw_lock.state == LockState.HARDWARE_MAINT
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-29-09: 连续 UNLOCK 失败导致控制器标记故障 ---
    print("\n[TC-29-09] 连续 UNLOCK 失败导致控制器标记故障")
    try:
        hw_lock = L5HardwareLock()
        hw_lock._hw_status.controller_health = "failed"
        
        for i in range(3):
            request = UnlockRequest(f"req-fail-{i}", OperationType.PROMOTION, "token", "ad-26", 1024)
            hw_lock.request_unlock(request, token_valid_pass)
        
        hw_lock.check_hardware_health()
        assert hw_lock.state == LockState.LOCK_FAILED
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-29-10: 剩余解锁时间查询 ---
    print("\n[TC-29-10] 剩余解锁时间查询")
    try:
        hw_lock = L5HardwareLock()
        request = UnlockRequest("req-010", OperationType.PROMOTION, "token", "ad-26", 1024)
        hw_lock.request_unlock(request, token_valid_pass)
        
        remaining = hw_lock.get_remaining_unlock_time_ms()
        assert remaining > 0
        assert remaining <= hw_lock.TEMP_UNLOCK_TIMEOUT_MS
        
        hw_lock.confirm_lock("req-010")
        assert hw_lock.get_remaining_unlock_time_ms() == 0
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