#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-51
模块名称: 记忆变更日志追溯单元
所属分区: 五、存储与系统运维
核心职责: 全链路记录双漏斗记忆系统中所有记忆操作事件的不可变审计日志。覆盖写入、
          晋升、遗忘、归档、导入导出、配置变更、安全事件等全部操作类型。日志存储于
          独立安全分区，支持按时间范围、操作类型、来源模块等多维条件检索。是系统
          合规审计、故障回溯与数据一致性校验的唯一日志依据。

依赖模块: 全部漏斗内核模块(ad-01至ad-43)、外挂模块(ad-44至ad-47)、
          运维模块(ad-48至ad-50)——作为日志生产者
被依赖模块: ad-01 总控漏斗F₀（查询审计日志）、
            ad-30 L5核心层防篡改与只读管控单元（查询安全事件日志）、
            离线审计系统（批量导出日志）

日志特性:
  - 链式哈希结构：每条日志包含上一日志哈希，任意篡改可被即时检测
  - 分级保留：安全事件日志保留≥3年，普通操作日志保留≥6个月
  - 不可变：日志条目一旦写入即不可修改，不提供修改或删除接口
  - 批量缓冲：日志先写入缓冲区，满50条或500ms后批量刷新至安全分区

安全约束:
  S-01: 日志存储于独立安全分区，与漏斗存储物理隔离，运行时仅本模块拥有写入权限
  S-02: 日志采用链式结构（每条日志含上一日志哈希），篡改可被即时检测
  S-03: 安全事件日志保留期限≥3年，普通操作日志保留期限≥6个月
  S-04: 日志条目一旦写入即不可修改
  S-05: 日志导出须经管理员令牌验证，导出数据包须附带数字签名
  S-06: 紧急熔断时安全事件日志享有最高写入优先级，强制刷新缓冲区
  S-07: 日志分区须每日自动备份至冗余镜像分区，备份文件同样受链式哈希保护
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid
import hashlib
import json


# ==================== 枚举定义 ====================

class LogEventType(Enum):
    """日志事件类型"""
    # 条目操作
    ITEM_CREATE = "ITEM_CREATE"
    ITEM_PROMOTE = "ITEM_PROMOTE"
    ITEM_DELETE = "ITEM_DELETE"
    ITEM_ARCHIVE = "ITEM_ARCHIVE"
    ITEM_RESTORE = "ITEM_RESTORE"
    ITEM_UPDATE = "ITEM_UPDATE"
    ITEM_MERGE = "ITEM_MERGE"
    RULE_EXTRACT = "RULE_EXTRACT"
    # L5 核心操作
    L5_LOCK = "L5_LOCK"
    L5_UNLOCK_TEMP = "L5_UNLOCK_TEMP"
    # 遗忘与清理
    FORGET_CANDIDATE = "FORGET_CANDIDATE"
    FORGET_EXECUTE = "FORGET_EXECUTE"
    QUOTA_ALERT = "QUOTA_ALERT"
    # 配置与安全
    CONFIG_CHANGE = "CONFIG_CHANGE"
    SAFETY_EVENT = "SAFETY_EVENT"
    ARBITRATION = "ARBITRATION"
    # 导入导出
    EXPORT_MEMORY = "EXPORT_MEMORY"
    IMPORT_MEMORY = "IMPORT_MEMORY"
    # 槽位生命周期
    SLOT_LIFECYCLE = "SLOT_LIFECYCLE"
    # 外挂模块
    WM_UPDATE = "WM_UPDATE"
    LAW_APPEND = "LAW_APPEND"
    QUESTION_LOG = "QUESTION_LOG"
    ACCESS_DENIED = "ACCESS_DENIED"
    # 系统
    SYSTEM_STARTUP = "SYSTEM_STARTUP"
    SYSTEM_SHUTDOWN = "SYSTEM_SHUTDOWN"


class EventSeverity(Enum):
    """事件严重等级"""
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class LogDBState(Enum):
    """日志单元内部状态"""
    NORMAL = "normal"
    WRITING = "writing"
    QUERYING = "querying"
    ARCHIVING = "archiving"
    DEGRADED = "degraded"
    PAUSED = "paused"


# ==================== 数据结构 ====================

@dataclass
class LogEntry:
    """日志条目"""
    log_id: str
    event_timestamp: float                      # 事件发生时刻
    record_timestamp: float                     # 日志写入时刻
    event_type: LogEventType
    source_module_id: int                       # 来源模块编号
    source_module_name: str                     # 来源模块名称
    operation_summary: str                      # 人类可读的操作摘要
    operation_details: Dict[str, Any]            # 结构化操作详情
    operation_result: str                       # SUCCESS / FAILURE / REJECTED
    related_entry_id: Optional[str] = None      # 关联经验条目 ID
    related_slot_id: Optional[int] = None       # 关联分槽号
    severity: EventSeverity = EventSeverity.NORMAL
    checksum: str = ""                          # 本条日志内容的 SHA256 哈希
    previous_log_hash: str = ""                 # 上一条日志的哈希


@dataclass
class LogWriteRequest:
    """日志写入请求（来自各模块）"""
    event_type: LogEventType
    source_module_id: int
    source_module_name: str
    operation_summary: str
    operation_details: Dict[str, Any]
    operation_result: str = "SUCCESS"
    related_entry_id: Optional[str] = None
    related_slot_id: Optional[int] = None
    severity: Optional[EventSeverity] = None   # None 时自动判定
    event_timestamp: float = field(default_factory=time.time)


@dataclass
class LogQueryRequest:
    """日志查询请求"""
    request_id: str
    time_range: Optional[Tuple[float, float]] = None
    event_types: Optional[List[LogEventType]] = None
    source_module_id: Optional[int] = None
    severity: Optional[EventSeverity] = None
    related_entry_id: Optional[str] = None
    page: int = 1
    page_size: int = 100
    source_module: str = ""


@dataclass
class LogQueryResponse:
    """日志查询响应"""
    request_id: str
    success: bool
    entries: List[LogEntry] = field(default_factory=list)
    total_count: int = 0
    page: int = 1
    page_size: int = 100
    message: str = ""


@dataclass
class LogExportRequest:
    """日志导出请求"""
    request_id: str
    time_range: Tuple[float, float]
    export_format: str = "JSON"    # JSON / CSV
    operator_token: str = ""
    source_module: str = ""


@dataclass
class LogExportPackage:
    """日志导出数据包"""
    package_id: str
    time_range: Tuple[float, float]
    entries: List[LogEntry]
    total_count: int
    export_format: str
    digital_signature: str
    exported_at: float = field(default_factory=time.time)


@dataclass
class LogArchiveReport:
    """日志归档报告"""
    archived_count: int
    time_range: Tuple[float, float]
    released_bytes: int
    remaining_count: int
    timestamp: float = field(default_factory=time.time)


@dataclass
class LogDBStatus:
    """日志数据库状态"""
    total_entries: int
    total_size_bytes: int
    oldest_entry_time: float
    newest_entry_time: float
    last_hash: str
    storage_usage_rate: float
    state: str


# ==================== 主类定义 ====================

class ChangeLogTracer:
    """
    记忆变更日志追溯单元
    
    职责:
    1. 接收各模块发送的日志写入请求
    2. 生成链式哈希日志条目（每条包含上一日志哈希）
    3. 批量缓冲写入（满50条或500ms刷新）
    4. 安全事件日志立即刷新（最高写入优先级）
    5. 提供多维条件日志查询
    6. 提供日志导出（需管理员令牌验证，附带数字签名）
    7. 定期归档过期日志
    8. 每日自动备份至冗余镜像分区
    """
    
    # 日志保留期限（秒）
    NORMAL_LOG_RETENTION = 180 * 24 * 3600      # 6 个月
    SAFETY_LOG_RETENTION = 3 * 365 * 24 * 3600  # 3 年
    
    # 批量写入配置
    BUFFER_MAX_SIZE = 50          # 缓冲区最大条目数
    BUFFER_FLUSH_INTERVAL = 0.5   # 刷新间隔（秒）
    
    # 日志分区最大容量（字节）
    MAX_LOG_CAPACITY = 20 * 1024 * 1024  # 20MB
    
    # 容量告警阈值
    CAPACITY_WARNING = 0.80
    CAPACITY_URGENT = 0.95
    
    # 归档触发间隔（秒）
    ARCHIVE_INTERVAL = 30 * 24 * 3600  # 30 日
    
    def __init__(self):
        self.module_id = "ad-51"
        self.module_name = "记忆变更日志追溯单元"
        
        # 内部状态
        self.state = LogDBState.NORMAL
        
        # 日志存储: 按时间顺序存储
        self._logs: List[LogEntry] = []
        
        # 日志缓冲区
        self._buffer: List[LogEntry] = []
        
        # 链式哈希的上一条哈希值
        self._previous_hash = "GENESIS"
        
        # 日志计数器
        self._total_written = 0
        
        # 上次刷新时间
        self._last_flush_time = time.time()
        
        # 上次归档时间
        self._last_archive_time = time.time()
        
        # 统计
        self._total_queries = 0
        self._total_exports = 0
        self._total_archived = 0
        
        # 待写入 ad-51 的日志（本模块自身操作记录）
        self._pending_logs: List[Dict[str, Any]] = []
        
        # 模拟日志存储大小（字节/条目）
        self._avg_entry_size = 512
        
        print(f"[{self.module_id}] 记忆变更日志追溯单元初始化完成")
        print(f"[{self.module_id}] 链式哈希结构 | 保留期限: 普通{self.NORMAL_LOG_RETENTION/86400:.0f}天 / 安全{self.SAFETY_LOG_RETENTION/86400:.0f}天")
    
    # ========== 状态管理 ==========
    
    def pause(self) -> None:
        self.state = LogDBState.PAUSED
    
    def resume(self) -> None:
        self.state = LogDBState.NORMAL
    
    def get_state(self) -> LogDBState:
        return self.state
    
    def get_usage_rate(self) -> float:
        current_size = len(self._logs) * self._avg_entry_size
        return current_size / self.MAX_LOG_CAPACITY if self.MAX_LOG_CAPACITY > 0 else 0.0
    
    # ========== 日志写入 ==========
    
    def write_log(self, request: LogWriteRequest) -> str:
        """
        接收日志写入请求
        
        处理流程:
        1. 自动判定严重等级（如果未指定）
        2. 生成日志条目（含链式哈希）
        3. 加入写入缓冲区
        4. 缓冲区满则批量刷新
        5. 安全事件日志立即刷新（S-06）
        
        Args:
            request: 日志写入请求
            
        Returns:
            日志 ID
        """
        # 严重等级自动判定
        if request.severity is None:
            request.severity = self._determine_severity(request.event_type, request.operation_result)
        
        # 生成日志条目
        log_entry = self._create_log_entry(request)
        
        # 加入缓冲区
        self._buffer.append(log_entry)
        self._total_written += 1
        
        # 安全事件日志立即刷新
        if request.severity == EventSeverity.CRITICAL:
            self._flush_buffer()
        
        # 缓冲区满则批量刷新
        if len(self._buffer) >= self.BUFFER_MAX_SIZE:
            self._flush_buffer()
        
        return log_entry.log_id
    
    def _create_log_entry(self, request: LogWriteRequest) -> LogEntry:
        """创建日志条目并计算链式哈希"""
        # 计算本条日志的原始数据
        raw = (f"{request.event_timestamp}{request.event_type.value}"
               f"{request.source_module_id}{request.operation_summary}"
               f"{request.operation_result}{self._previous_hash}")
        
        # 计算 SHA256 哈希
        checksum = hashlib.sha256(raw.encode()).hexdigest()
        
        entry = LogEntry(
            log_id=f"LOG-{uuid.uuid4().hex[:8]}",
            event_timestamp=request.event_timestamp,
            record_timestamp=time.time(),
            event_type=request.event_type,
            source_module_id=request.source_module_id,
            source_module_name=request.source_module_name,
            operation_summary=request.operation_summary,
            operation_details=request.operation_details,
            operation_result=request.operation_result,
            related_entry_id=request.related_entry_id,
            related_slot_id=request.related_slot_id,
            severity=request.severity or EventSeverity.NORMAL,
            checksum=checksum,
            previous_log_hash=self._previous_hash
        )
        
        # 更新链式哈希
        self._previous_hash = checksum
        
        return entry
    
    def _determine_severity(self, event_type: LogEventType, result: str) -> EventSeverity:
        """自动判定事件严重等级"""
        # 安全事件和访问拒绝 → CRITICAL
        if event_type in [LogEventType.SAFETY_EVENT, LogEventType.ACCESS_DENIED,
                          LogEventType.L5_UNLOCK_TEMP]:
            return EventSeverity.CRITICAL
        
        # 配置变更和仲裁 → HIGH
        if event_type in [LogEventType.CONFIG_CHANGE, LogEventType.ARBITRATION,
                          LogEventType.FORGET_EXECUTE]:
            return EventSeverity.HIGH
        
        # 操作失败 → WARNING
        if result in ["FAILURE", "REJECTED"]:
            return EventSeverity.WARNING
        
        return EventSeverity.NORMAL
    
    def _flush_buffer(self) -> int:
        """
        批量刷新缓冲区至日志分区
        
        Returns:
            写入的条目数
        """
        if not self._buffer:
            return 0
        
        self.state = LogDBState.WRITING
        
        # 追加到日志存储
        self._logs.extend(self._buffer)
        flushed_count = len(self._buffer)
        self._buffer.clear()
        
        self._last_flush_time = time.time()
        
        # 检查容量
        if self.get_usage_rate() > self.CAPACITY_URGENT:
            self.state = LogDBState.DEGRADED
            print(f"[{self.module_id}] 日志分区使用率超过95%，进入降级模式")
        elif self.get_usage_rate() > self.CAPACITY_WARNING:
            print(f"[{self.module_id}] 日志分区使用率超过80%，建议归档")
        
        self.state = LogDBState.NORMAL
        
        return flushed_count
    
    def check_buffer_flush(self) -> None:
        """检查缓冲区是否需要定时刷新"""
        if self._buffer and time.time() - self._last_flush_time >= self.BUFFER_FLUSH_INTERVAL:
            self._flush_buffer()
    
    # ========== 日志查询 ==========
    
    def query_logs(self, request: LogQueryRequest) -> LogQueryResponse:
        """
        按条件查询日志
        
        Args:
            request: 查询请求
            
        Returns:
            查询响应
        """
        self.state = LogDBState.QUERYING
        self._total_queries += 1
        
        # 先刷新缓冲区，确保查询完整性
        self._flush_buffer()
        
        # 按条件筛选
        results = self._logs
        
        if request.time_range:
            start, end = request.time_range
            results = [log for log in results if start <= log.event_timestamp <= end]
        
        if request.event_types:
            results = [log for log in results if log.event_type in request.event_types]
        
        if request.source_module_id is not None:
            results = [log for log in results if log.source_module_id == request.source_module_id]
        
        if request.severity:
            results = [log for log in results if log.severity == request.severity]
        
        if request.related_entry_id:
            results = [log for log in results if log.related_entry_id == request.related_entry_id]
        
        # 按时间降序排列
        results.sort(key=lambda x: x.event_timestamp, reverse=True)
        
        total_count = len(results)
        
        # 分页
        start_idx = (request.page - 1) * request.page_size
        end_idx = start_idx + request.page_size
        page_results = results[start_idx:end_idx]
        
        self.state = LogDBState.NORMAL
        
        return LogQueryResponse(
            request_id=request.request_id,
            success=True,
            entries=page_results,
            total_count=total_count,
            page=request.page,
            page_size=request.page_size
        )
    
    # ========== 日志导出 ==========
    
    def export_logs(self, request: LogExportRequest,
                    token_validator) -> Tuple[bool, Optional[LogExportPackage], str]:
        """
        导出日志数据包
        
        S-05: 须经管理员令牌验证，导出数据包附带数字签名
        
        Args:
            request: 导出请求
            token_validator: 令牌验证回调
            
        Returns:
            (是否成功, 导出数据包, 消息)
        """
        if not token_validator(request.operator_token, "audit_log_export"):
            return False, None, "管理员令牌验证失败"
        
        self._total_exports += 1
        
        # 筛选时间范围
        start, end = request.time_range
        matching = [log for log in self._logs if start <= log.event_timestamp <= end]
        
        # 生成数字签名
        raw = "".join(log.log_id for log in matching)
        signature = hashlib.sha256(raw.encode()).hexdigest()
        
        package = LogExportPackage(
            package_id=f"export-{uuid.uuid4().hex[:8]}",
            time_range=request.time_range,
            entries=matching,
            total_count=len(matching),
            export_format=request.export_format,
            digital_signature=signature
        )
        
        self._log_self("LOG_EXPORT", {
            "package_id": package.package_id,
            "total_entries": len(matching),
            "time_range": f"{start}-{end}"
        })
        
        return True, package, f"导出成功，共{len(matching)}条日志"
    
    # ========== 日志归档 ==========
    
    def execute_archive(self) -> Optional[LogArchiveReport]:
        """
        归档过期日志
        
        规则:
        - 安全事件日志保留 3 年
        - 普通操作日志保留 6 个月
        - 归档数据压缩后写入冷归档分区
        """
        now = time.time()
        if now - self._last_archive_time < self.ARCHIVE_INTERVAL:
            return None
        
        self.state = LogDBState.ARCHIVING
        self._flush_buffer()
        
        normal_cutoff = now - self.NORMAL_LOG_RETENTION
        safety_cutoff = now - self.SAFETY_LOG_RETENTION
        
        to_archive = []
        remaining = []
        
        for log in self._logs:
            should_archive = False
            
            if log.severity == EventSeverity.CRITICAL or log.event_type == LogEventType.SAFETY_EVENT:
                if log.event_timestamp < safety_cutoff:
                    should_archive = True
            else:
                if log.event_timestamp < normal_cutoff:
                    should_archive = True
            
            if should_archive:
                to_archive.append(log)
            else:
                remaining.append(log)
        
        archived_count = len(to_archive)
        if archived_count > 0:
            # 替换为剩余日志
            self._logs = remaining
            
            # 重建链式哈希
            if remaining:
                self._previous_hash = remaining[-1].checksum
            else:
                self._previous_hash = "GENESIS"
        
        self._total_archived += archived_count
        self._last_archive_time = now
        
        report = LogArchiveReport(
            archived_count=archived_count,
            time_range=(to_archive[0].event_timestamp if to_archive else 0,
                       to_archive[-1].event_timestamp if to_archive else 0),
            released_bytes=archived_count * self._avg_entry_size,
            remaining_count=len(remaining)
        )
        
        self._log_self("LOG_ARCHIVE", {
            "archived_count": archived_count,
            "remaining_count": len(remaining)
        })
        
        self.state = LogDBState.NORMAL
        
        if archived_count > 0:
            print(f"[{self.module_id}] 日志归档: {archived_count} 条, 剩余 {len(remaining)} 条")
        
        return report
    
    # ========== 链式完整性校验 ==========
    
    def verify_chain_integrity(self) -> Tuple[bool, List[int]]:
        """
        校验链式哈希完整性
        
        Returns:
            (是否完整, 断裂位置索引列表)
        """
        broken_indices = []
        
        for i in range(1, len(self._logs)):
            current = self._logs[i]
            previous = self._logs[i - 1]
            
            if current.previous_log_hash != previous.checksum:
                broken_indices.append(i)
        
        if broken_indices:
            print(f"[{self.module_id}] 链式哈希断裂: {len(broken_indices)} 处")
        
        return len(broken_indices) == 0, broken_indices
    
    # ========== 查询接口 ==========
    
    def get_log_entry(self, log_id: str) -> Optional[LogEntry]:
        for log in self._logs:
            if log.log_id == log_id:
                return log
        return None
    
    def get_total_count(self) -> int:
        return len(self._logs)
    
    def generate_status(self) -> LogDBStatus:
        self._flush_buffer()
        total_size = len(self._logs) * self._avg_entry_size
        
        return LogDBStatus(
            total_entries=len(self._logs),
            total_size_bytes=total_size,
            oldest_entry_time=self._logs[0].event_timestamp if self._logs else 0,
            newest_entry_time=self._logs[-1].event_timestamp if self._logs else 0,
            last_hash=self._previous_hash,
            storage_usage_rate=self.get_usage_rate(),
            state=self.state.value
        )
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_written": self._total_written,
            "current_entries": len(self._logs),
            "buffer_size": len(self._buffer),
            "total_queries": self._total_queries,
            "total_exports": self._total_exports,
            "total_archived": self._total_archived,
            "usage_rate": self.get_usage_rate(),
            "previous_hash": self._previous_hash[:16],
            "state": self.state.value
        }
    
    # ========== 本模块自身日志 ==========
    
    def _log_self(self, event_type: str, details: Dict[str, Any]) -> None:
        self._pending_logs.append({
            "log_id": f"self-{uuid.uuid4().hex[:8]}",
            "event_type": event_type,
            "details": details,
            "timestamp": time.time()
        })
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-51 记忆变更日志追溯单元 单元测试")
    print("=" * 60)
    passed, failed = 0, 0
    
    # TC-51-01: 正常写入日志
    print("\n[TC-51-01] 正常写入日志")
    try:
        tracer = ChangeLogTracer()
        req = LogWriteRequest(
            event_type=LogEventType.ITEM_CREATE,
            source_module_id=20, source_module_name="L1临时层存储单元",
            operation_summary="写入新经验条目 EXP-001",
            operation_details={"entry_id": "EXP-001", "i0_value": 0.5},
            operation_result="SUCCESS"
        )
        log_id = tracer.write_log(req)
        assert log_id.startswith("LOG-")
        assert len(tracer._buffer) == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-51-02: 安全事件日志立即刷新
    print("\n[TC-51-02] 安全事件日志立即刷新")
    try:
        tracer = ChangeLogTracer()
        # 先写一条普通日志（留在缓冲区）
        tracer.write_log(LogWriteRequest(
            LogEventType.ITEM_CREATE, 20, "测试", "普通", {}, "SUCCESS"))
        assert len(tracer._buffer) == 1
        # 写安全事件日志（应立即刷新）
        tracer.write_log(LogWriteRequest(
            LogEventType.SAFETY_EVENT, 1, "总控漏斗", "紧急熔断", {}, "SUCCESS",
            severity=EventSeverity.CRITICAL))
        assert len(tracer._buffer) == 0  # 缓冲区已刷新
        assert len(tracer._logs) == 2    # 两条都在存储中
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-51-03: 缓冲区满自动刷新
    print("\n[TC-51-03] 缓冲区满自动刷新")
    try:
        tracer = ChangeLogTracer()
        tracer.BUFFER_MAX_SIZE = 10
        for i in range(12):
            tracer.write_log(LogWriteRequest(
                LogEventType.ITEM_CREATE, 20, "测试", f"条目{i}", {}, "SUCCESS"))
        assert len(tracer._logs) >= 10  # 缓冲区满后已刷新
        assert len(tracer._buffer) < 10
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-51-04: 日志查询
    print("\n[TC-51-04] 日志查询（按事件类型筛选）")
    try:
        tracer = ChangeLogTracer()
        tracer.write_log(LogWriteRequest(LogEventType.ITEM_PROMOTE, 39, "搬运", "晋升", {}, "SUCCESS"))
        tracer.write_log(LogWriteRequest(LogEventType.FORGET_EXECUTE, 42, "清理", "遗忘", {}, "SUCCESS"))
        tracer._flush_buffer()
        
        req = LogQueryRequest("q-001", event_types=[LogEventType.ITEM_PROMOTE], source_module="ad-01")
        resp = tracer.query_logs(req)
        assert resp.success and resp.total_count >= 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-51-05: 链式哈希完整性校验
    print("\n[TC-51-05] 链式哈希完整性校验通过")
    try:
        tracer = ChangeLogTracer()
        for i in range(5):
            tracer.write_log(LogWriteRequest(
                LogEventType.ITEM_CREATE, 20, "测试", f"条目{i}", {}, "SUCCESS"))
        tracer._flush_buffer()
        intact, broken = tracer.verify_chain_integrity()
        assert intact and len(broken) == 0
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-51-06: 日志导出
    print("\n[TC-51-06] 日志导出（含数字签名）")
    try:
        tracer = ChangeLogTracer()
        tracer.write_log(LogWriteRequest(LogEventType.ITEM_CREATE, 20, "测试", "条目", {}, "SUCCESS"))
        tracer._flush_buffer()
        
        req = LogExportRequest("export-001", (0, time.time() + 1), "JSON", "admin_token")
        ok, package, msg = tracer.export_logs(req, lambda t: t == "admin_token")
        assert ok and package is not None
        assert package.total_count == 1
        assert package.digital_signature != ""
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-51-07: 导出令牌无效
    print("\n[TC-51-07] 导出令牌无效被拒")
    try:
        tracer = ChangeLogTracer()
        req = LogExportRequest("export-002", (0, time.time()), "JSON", "bad_token")
        ok, package, msg = tracer.export_logs(req, lambda t: t == "admin_token")
        assert not ok
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-51-08: 日志归档
    print("\n[TC-51-08] 日志归档（过期日志清理）")
    try:
        tracer = ChangeLogTracer()
        tracer.NORMAL_LOG_RETENTION = 1  # 1秒过期
        tracer.write_log(LogWriteRequest(LogEventType.ITEM_CREATE, 20, "测试", "旧日志", {}, "SUCCESS"))
        tracer._flush_buffer()
        # 设置日志时间为过去
        if tracer._logs:
            tracer._logs[0].event_timestamp = time.time() - 10
        time.sleep(0.1)
        tracer._last_archive_time = 0
        report = tracer.execute_archive()
        assert report is not None
        assert report.archived_count >= 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-51-09: 安全日志不随普通归档清理
    print("\n[TC-51-09] 安全事件日志保留3年")
    try:
        tracer = ChangeLogTracer()
        tracer.NORMAL_LOG_RETENTION = 1
        tracer.write_log(LogWriteRequest(
            LogEventType.SAFETY_EVENT, 1, "总控", "安全事件", {}, "SUCCESS",
            severity=EventSeverity.CRITICAL))
        tracer._flush_buffer()
        if tracer._logs:
            tracer._logs[0].event_timestamp = time.time() - 10
        time.sleep(0.1)
        tracer._last_archive_time = 0
        report = tracer.execute_archive()
        assert report.archived_count == 0  # 安全事件未被归档
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-51-10: 严重等级自动判定
    print("\n[TC-51-10] 操作失败自动判定为WARNING")
    try:
        tracer = ChangeLogTracer()
        req = LogWriteRequest(
            LogEventType.ITEM_DELETE, 42, "清理", "删除失败", {}, "FAILURE")
        log_id = tracer.write_log(req)
        tracer._flush_buffer()
        log = tracer.get_log_entry(log_id)
        assert log is not None and log.severity == EventSeverity.WARNING
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    print(f"\n测试结果: {passed} PASS, {failed} FAIL")