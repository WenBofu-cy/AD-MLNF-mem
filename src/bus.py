#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AD-mlnf-mem 内部总线 MemoryBus

为双漏斗记忆中枢 51 个模块提供松耦合的标准化通信层。
在完整系统中，MemoryBus 由中间件实现（如 ZeroMQ / DDS / 共享内存）。
此处为最小可运行实现，用于模块联调、单元测试和最小闭环验证。

特性:
- 点对点消息投递（模块间精确通信）
- 广播消息（一对多）
- 消息优先级队列（紧急/高/普通三级）
- 消息日志记录与统计
- 模块注册与回调机制

与 HR-mlnf-mem 的 MemoryBus 风格统一，增加优先级队列和更完善的统计功能。
"""

from typing import Dict, List, Any, Callable, Optional, Tuple
from dataclasses import dataclass, field
from collections import defaultdict, deque
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class MessagePriority(Enum):
    """消息优先级"""
    CRITICAL = 3   # 紧急：安全急停、碰撞、熔断
    HIGH = 2        # 高：模式切换、经验查询、晋升写入
    NORMAL = 1      # 普通：周期性上报、统计、日志
    LOW = 0         # 低：调试信息


class MessageType(Enum):
    """AD-mlnf-mem 消息类型枚举"""
    # 顶层调度
    MODE_SWITCH = "mode_switch"
    GLOBAL_DISPATCH = "global_dispatch"
    
    # 漏斗一：驾驶员画像
    DRIVER_ID_RESULT = "driver_id_result"
    SLOT_ACTIVATE = "slot_activate"
    SLOT_CREATE = "slot_create"
    SLOT_DELETE = "slot_delete"
    BEHAVIOR_OBSERVE = "behavior_observe"
    BEHAVIOR_JUDGE = "behavior_judge"
    STATISTICS_REPORT = "statistics_report"
    REMINDER_GENERATE = "reminder_generate"
    
    # 漏斗二：自成长经验
    EXPERIENCE_WRITE = "experience_write"
    EXPERIENCE_QUERY = "experience_query"
    PROMOTION_CANDIDATE = "promotion_candidate"
    PROMOTION_EXECUTE = "promotion_execute"
    FORGET_CANDIDATE = "forget_candidate"
    FORGET_EXECUTE = "forget_execute"
    MERGE_REQUEST = "merge_request"
    ARBITRATION_REQUEST = "arbitration_request"
    ARBITRATION_RESULT = "arbitration_result"
    
    # 重要度计算
    I_VALUE_UPDATE = "i_value_update"
    S_VALUE_CALC = "s_value_calc"
    V_VALUE_CALC = "v_value_calc"
    C_VALUE_UPDATE = "c_value_update"
    I0_ASSIGN = "i0_assign"
    WEIGHT_CONFIG = "weight_config"
    I_REFRESH_TRIGGER = "i_refresh_trigger"
    
    # 外挂模块
    WM_QUERY = "wm_query"
    WM_UPDATE = "wm_update"
    LAW_QUERY = "law_query"
    LAW_APPEND = "law_append"
    EMOTION_QUERY = "emotion_query"
    QUESTION_SUBMIT = "question_submit"
    QUESTION_QUERY = "question_query"
    
    # 运维
    QUOTA_ALERT = "quota_alert"
    BATCH_CLEAN = "batch_clean"
    ARCHIVE_REQUEST = "archive_request"
    ARCHIVE_CONFIRM = "archive_confirm"
    IMPORT_EXPORT = "import_export"
    CHANGE_LOG = "change_log"
    
    # 安全
    SAFETY_EVENT = "safety_event"
    EMERGENCY_SHUTDOWN = "emergency_shutdown"
    
    # 通用
    STATUS_REPORT = "status_report"
    ACK = "ack"
    ERROR = "error"


# ==================== 数据结构 ====================

@dataclass
class BusMessage:
    """总线消息"""
    msg_id: str
    source_module: str
    target_module: str
    msg_type: MessageType
    payload: Dict[str, Any] = field(default_factory=dict)
    priority: MessagePriority = MessagePriority.NORMAL
    timestamp: float = field(default_factory=time.time)
    correlation_id: Optional[str] = None  # 关联请求ID，用于请求-响应匹配


@dataclass
class BusStats:
    """总线统计"""
    total_sent: int = 0
    total_delivered: int = 0
    total_errors: int = 0
    by_priority: Dict[MessagePriority, int] = field(default_factory=lambda: defaultdict(int))
    by_type: Dict[MessageType, int] = field(default_factory=lambda: defaultdict(int))
    pending_messages: int = 0
    registered_modules: int = 0
    uptime_seconds: float = 0.0


# ==================== 主类定义 ====================

class MemoryBus:
    """
    MemoryBus 内部总线
    
    为 AD-mlnf-mem 51 个模块提供松耦合的通信层。
    支持点对点消息投递、广播、优先级队列和消息日志。
    
    使用示例:
        bus = MemoryBus()
        bus.register_module("ad-01", my_callback)
        bus.send("ad-01", "ad-02", MessageType.EXPERIENCE_QUERY, 
                 payload={"scene": "highway"}, priority=MessagePriority.HIGH)
    """
    
    MAX_LOG_SIZE = 2000          # 消息日志最大保留条数
    MAX_PENDING_PER_MODULE = 500  # 每个模块收件箱最大未处理消息数
    
    def __init__(self):
        # 模块收件箱：按优先级分队列
        self._inboxes: Dict[str, Dict[MessagePriority, deque]] = defaultdict(
            lambda: {p: deque() for p in MessagePriority}
        )
        # 模块回调
        self._subscribers: Dict[str, Callable] = {}
        # 消息日志
        self._message_log: List[BusMessage] = []
        # 统计
        self._stats = BusStats()
        self._start_time = time.time()
        # 请求-响应追踪
        self._pending_requests: Dict[str, BusMessage] = {}
        
        print("[MemoryBus] AD-mlnf-mem 内部总线初始化完成")
    
    # ========== 模块管理 ==========
    
    def register_module(self, module_id: str, callback: Optional[Callable] = None) -> None:
        """
        注册模块到总线
        
        Args:
            module_id: 模块编号，如 "ad-01"
            callback: 可选的消息回调函数，签名为 callback(BusMessage) -> None
        """
        self._subscribers[module_id] = callback
        self._stats.registered_modules = len(self._subscribers)
        print(f"[MemoryBus] 注册模块: {module_id} (总计 {self._stats.registered_modules} 个)")
    
    def unregister_module(self, module_id: str) -> None:
        """注销模块"""
        if module_id in self._subscribers:
            del self._subscribers[module_id]
            if module_id in self._inboxes:
                del self._inboxes[module_id]
            self._stats.registered_modules = len(self._subscribers)
            print(f"[MemoryBus] 注销模块: {module_id}")
    
    # ========== 消息发送 ==========
    
    def send(self, source: str, target: str, msg_type: MessageType,
             payload: Dict[str, Any] = None,
             priority: MessagePriority = MessagePriority.NORMAL,
             correlation_id: Optional[str] = None) -> str:
        """
        发送点对点消息
        
        Args:
            source: 来源模块编号
            target: 目标模块编号
            msg_type: 消息类型
            payload: 消息负载
            priority: 消息优先级
            correlation_id: 关联请求ID
            
        Returns:
            消息ID
        """
        msg = BusMessage(
            msg_id=self._generate_msg_id(),
            source_module=source,
            target_module=target,
            msg_type=msg_type,
            payload=payload or {},
            priority=priority,
            correlation_id=correlation_id
        )
        
        # 投递到目标模块收件箱
        self._inboxes[target][priority].append(msg)
        
        # 更新统计
        self._stats.total_sent += 1
        self._stats.by_priority[priority] += 1
        self._stats.by_type[msg_type] += 1
        
        # 记录日志（循环缓冲区）
        if len(self._message_log) >= self.MAX_LOG_SIZE:
            self._message_log = self._message_log[-self.MAX_LOG_SIZE // 2:]
        self._message_log.append(msg)
        
        return msg.msg_id
    
    def broadcast(self, source: str, msg_type: MessageType,
                  payload: Dict[str, Any] = None,
                  priority: MessagePriority = MessagePriority.NORMAL,
                  exclude_self: bool = True) -> List[str]:
        """
        广播消息到所有已注册模块
        
        Args:
            source: 来源模块编号
            msg_type: 消息类型
            payload: 消息负载
            priority: 消息优先级
            exclude_self: 是否排除来源模块
            
        Returns:
            消息ID列表
        """
        msg_ids = []
        for target in list(self._subscribers.keys()):
            if exclude_self and target == source:
                continue
            msg_id = self.send(source, target, msg_type, payload, priority)
            msg_ids.append(msg_id)
        return msg_ids
    
    def request(self, source: str, target: str, msg_type: MessageType,
                payload: Dict[str, Any] = None,
                priority: MessagePriority = MessagePriority.NORMAL) -> Tuple[str, str]:
        """
        发送请求并返回关联ID，用于请求-响应模式
        
        Args:
            source: 来源模块编号
            target: 目标模块编号
            msg_type: 消息类型
            payload: 消息负载
            priority: 消息优先级
            
        Returns:
            (消息ID, 关联ID)
        """
        correlation_id = self._generate_msg_id()
        msg_id = self.send(source, target, msg_type, payload, priority, correlation_id)
        self._pending_requests[correlation_id] = None  # 标记等待响应
        return msg_id, correlation_id
    
    def respond(self, original_msg: BusMessage, response_type: MessageType,
                payload: Dict[str, Any] = None,
                priority: MessagePriority = MessagePriority.NORMAL) -> str:
        """
        对收到的消息发送响应
        
        Args:
            original_msg: 原始请求消息
            response_type: 响应消息类型
            payload: 响应负载
            priority: 响应优先级
            
        Returns:
            响应消息ID
        """
        return self.send(
            source=original_msg.target_module,
            target=original_msg.source_module,
            msg_type=response_type,
            payload=payload,
            priority=priority,
            correlation_id=original_msg.correlation_id
        )
    
    # ========== 消息接收 ==========
    
    def poll(self, module_id: str, max_messages: int = 10,
             priority_filter: Optional[MessagePriority] = None) -> List[BusMessage]:
        """
        轮询模块收件箱，按优先级从高到低获取消息
        
        Args:
            module_id: 模块编号
            max_messages: 单次最大获取消息数
            priority_filter: 可选，仅获取指定优先级的消息
            
        Returns:
            消息列表（按优先级降序排列）
        """
        if module_id not in self._inboxes:
            return []
        
        messages = []
        inbox = self._inboxes[module_id]
        
        # 按优先级从高到低遍历
        priorities = [MessagePriority.CRITICAL, MessagePriority.HIGH,
                      MessagePriority.NORMAL, MessagePriority.LOW]
        
        if priority_filter:
            priorities = [priority_filter]
        
        for priority in priorities:
            queue = inbox[priority]
            while queue and len(messages) < max_messages:
                messages.append(queue.popleft())
                self._stats.total_delivered += 1
        
        return messages
    
    def poll_all(self, module_id: str) -> List[BusMessage]:
        """
        获取模块收件箱中的所有消息
        
        Args:
            module_id: 模块编号
            
        Returns:
            所有未处理消息（按优先级降序）
        """
        return self.poll(module_id, max_messages=self.MAX_PENDING_PER_MODULE)
    
    # ========== 统计与日志 ==========
    
    def get_stats(self) -> BusStats:
        """获取总线统计"""
        self._stats.pending_messages = sum(
            sum(len(queue) for queue in inbox.values())
            for inbox in self._inboxes.values()
        )
        self._stats.uptime_seconds = time.time() - self._start_time
        return self._stats
    
    def get_message_log(self, limit: int = 100,
                        msg_type: Optional[MessageType] = None,
                        source: Optional[str] = None,
                        target: Optional[str] = None) -> List[BusMessage]:
        """
        查询消息日志
        
        Args:
            limit: 返回最大条数
            msg_type: 可选，按消息类型筛选
            source: 可选，按来源模块筛选
            target: 可选，按目标模块筛选
            
        Returns:
            符合条件的消息列表
        """
        result = self._message_log
        
        if msg_type:
            result = [m for m in result if m.msg_type == msg_type]
        if source:
            result = [m for m in result if m.source_module == source]
        if target:
            result = [m for m in result if m.target_module == target]
        
        return result[-limit:]
    
    def reset_stats(self) -> None:
        """重置统计（保留模块注册信息）"""
        self._stats = BusStats(registered_modules=len(self._subscribers))
        self._message_log = []
    
    # ========== 内部方法 ==========
    
    def _generate_msg_id(self) -> str:
        """生成唯一消息ID"""
        return f"msg-{uuid.uuid4().hex[:12]}"


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("MemoryBus 内部总线 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    # --- TC-BUS-01: 点对点消息投递 ---
    print("\n[TC-BUS-01] 发送点对点消息")
    try:
        bus = MemoryBus()
        bus.register_module("ad-01")
        bus.register_module("ad-02")
        
        msg_id = bus.send("ad-01", "ad-02", MessageType.STATUS_REPORT,
                          payload={"data": "hello"})
        messages = bus.poll("ad-02")
        
        assert len(messages) == 1, f"应收到1条消息，实际{len(messages)}"
        assert messages[0].payload["data"] == "hello"
        assert messages[0].source_module == "ad-01"
        assert messages[0].target_module == "ad-02"
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-BUS-02: 广播消息 ---
    print("\n[TC-BUS-02] 广播消息到所有模块")
    try:
        bus = MemoryBus()
        bus.register_module("ad-01")
        bus.register_module("ad-02")
        bus.register_module("ad-03")
        
        msg_ids = bus.broadcast("ad-01", MessageType.SAFETY_EVENT,
                                payload={"alert": "test"})
        assert len(msg_ids) == 2, f"应广播到2个模块，实际{len(msg_ids)}"
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-BUS-03: 优先级队列 ---
    print("\n[TC-BUS-03] 消息优先级排序")
    try:
        bus = MemoryBus()
        bus.register_module("ad-01")
        bus.register_module("ad-02")
        
        # 先发低优先级，再发高优先级
        bus.send("ad-01", "ad-02", MessageType.STATUS_REPORT,
                 priority=MessagePriority.LOW)
        bus.send("ad-01", "ad-02", MessageType.SAFETY_EVENT,
                 priority=MessagePriority.CRITICAL)
        bus.send("ad-01", "ad-02", MessageType.EXPERIENCE_QUERY,
                 priority=MessagePriority.HIGH)
        
        messages = bus.poll_all("ad-02")
        # 应该是 CRITICAL, HIGH, LOW 的顺序
        assert messages[0].priority == MessagePriority.CRITICAL
        assert messages[1].priority == MessagePriority.HIGH
        assert messages[2].priority == MessagePriority.LOW
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-BUS-04: 请求-响应模式 ---
    print("\n[TC-BUS-04] 请求-响应关联ID")
    try:
        bus = MemoryBus()
        bus.register_module("ad-01")
        bus.register_module("ad-02")
        
        msg_id, corr_id = bus.request("ad-01", "ad-02", MessageType.EXPERIENCE_QUERY,
                                       payload={"scene": "highway"})
        
        # 获取请求消息
        requests = bus.poll("ad-02")
        assert len(requests) == 1
        assert requests[0].correlation_id == corr_id
        
        # 发送响应
        resp_id = bus.respond(requests[0], MessageType.ACK,
                              payload={"result": "found"})
        responses = bus.poll("ad-01")
        assert len(responses) == 1
        assert responses[0].correlation_id == corr_id
        assert responses[0].payload["result"] == "found"
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-BUS-05: 统计功能 ---
    print("\n[TC-BUS-05] 总线统计")
    try:
        bus = MemoryBus()
        bus.register_module("ad-01")
        bus.register_module("ad-02")
        bus.register_module("ad-03")
        
        bus.send("ad-01", "ad-02", MessageType.SAFETY_EVENT,
                 priority=MessagePriority.CRITICAL)
        bus.send("ad-01", "ad-03", MessageType.STATUS_REPORT,
                 priority=MessagePriority.NORMAL)
        
        stats = bus.get_stats()
        assert stats.total_sent == 2
        assert stats.registered_modules == 3
        assert stats.by_priority[MessagePriority.CRITICAL] == 1
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