#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-06
模块名称: 子画像槽数据隔离管控单元
所属分区: 二、漏斗一：驾驶员画像漏斗
核心职责: 编译期强制实施子画像槽间物理存储隔离，管控所有跨槽数据访问请求的放行与拦截。
          确保漏斗一数据绝对不泄露至自动驾驶决策链路。数据访问白名单仅包含漏斗一内部模块。

依赖模块: ad-02(漏斗一专属调度单元), ad-05(子画像槽创建与初始化单元)
被依赖模块: ad-07(驾驶行为观测记录单元), ad-11(驾驶辅助提醒生成单元), 
          ad-12(临时画像槽自动清除单元)

安全约束:
  S-01: 漏斗一数据编译期禁止接入自动驾驶决策链路
  S-02: 子画像槽间绝对物理存储隔离，跨槽读取请求无条件拦截
  S-03: 数据访问白名单为编译期常量，仅包含漏斗一内部模块(ad-07, ad-10, ad-11)
  S-04: 紧急熔断状态下仅放行只读查询
  S-05: 全部冻结状态下仅放行只读查询
  S-06: 每一次拦截事件均写入 ad-51 不可变安全日志
  S-07: 隔离规则表存储于本地安全分区，定期自动备份至冗余分区
"""

from typing import Dict, List, Optional, Any, Set
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class AccessOperation(Enum):
    """访问操作类型"""
    READ = "read"
    WRITE = "write"
    ERASE = "erase"


class GateState(Enum):
    """隔离管控单元内部状态"""
    NORMAL = "normal"
    EMERGENCY_RO = "emergency_ro"
    ALL_FROZEN = "all_frozen"
    SLOT_MAINTENANCE = "slot_maintenance"
    RULE_UPDATING = "rule_updating"


class InterceptReason(Enum):
    """拦截原因码"""
    EMERGENCY_RO = "emergency_ro"
    ALL_FROZEN = "all_frozen"
    AUTONOMOUS_DECISION_MODULE = "autonomous_decision_module"
    INVALID_SLOT = "invalid_slot"
    CROSS_SLOT_ACCESS = "cross_slot_access"
    SLOT_MAINTENANCE = "slot_maintenance"
    NOT_IN_WHITELIST = "not_in_whitelist"


# ==================== 数据结构 ====================

@dataclass
class IsolationRule:
    """隔离规则条目"""
    slot_id: int
    partition_base: int
    partition_size: int
    allowed_modules: Set[str]          # 访问白名单模块集合
    cross_slot_access: bool = False    # 跨槽访问是否允许（默认禁止）
    autonomous_access: bool = False    # 自动驾驶模块是否可访问（默认禁止）
    status: str = "ACTIVE"


@dataclass
class AccessRequest:
    """数据访问请求"""
    request_id: str
    source_module: str
    target_slot_id: int
    operation_type: AccessOperation
    source_slot_id: Optional[int] = None  # 仅跨槽访问时有值
    data_content: Optional[Dict[str, Any]] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class AccessResponse:
    """访问请求响应"""
    request_id: str
    allowed: bool
    intercept_reason: Optional[InterceptReason] = None
    data: Optional[Dict[str, Any]] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class InterceptLogEntry:
    """拦截日志条目"""
    log_id: str
    request_id: str
    source_module: str
    target_slot_id: int
    operation_type: AccessOperation
    reason: InterceptReason
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class SlotIsolationGate:
    """
    子画像槽数据隔离管控单元
    
    职责:
    1. 编译期强制实施子画像槽间物理存储隔离
    2. 管控所有跨槽数据访问请求的放行与拦截
    3. 确保漏斗一数据绝对不泄露至自动驾驶决策链路
    4. 维护数据访问白名单
    5. 全量记录拦截事件至安全日志
    """
    
    # 编译期禁止访问漏斗一的模块列表（自动驾驶决策相关）
    FORBIDDEN_MODULES: Set[str] = {
        "ad-14", "ad-15", "ad-16", "ad-17", "ad-18", "ad-19",  # 漏斗二场景分槽
        "ad-31", "ad-32", "ad-33", "ad-36", "ad-38", "ad-40",  # 漏斗二计算引擎
        "ECC-01", "ECC-03", "ECC-04", "ECC-05", "ECC-08", "ECC-09",  # ECC决策模块
    }
    
    # 漏斗一内部模块白名单（编译期常量）
    FUNNEL_ONE_WHITELIST: Set[str] = {
        "ad-07",   # 驾驶行为观测记录单元
        "ad-10",   # 行为累积统计单元
        "ad-11",   # 驾驶辅助提醒生成单元
    }
    
    def __init__(self):
        self.module_id = "ad-06"
        self.module_name = "子画像槽数据隔离管控单元"
        
        # 内部状态
        self.state = GateState.NORMAL
        
        # 隔离规则表: slot_id -> IsolationRule
        self._rules: Dict[int, IsolationRule] = {}
        
        # 维护中的槽号集合
        self._maintenance_slots: Set[int] = set()
        
        # 统计
        self._total_requests = 0
        self._allowed_count = 0
        self._intercepted_count = 0
        
        # 拦截日志缓存
        self._pending_logs: List[InterceptLogEntry] = []
        
        print(f"[{self.module_id}] 子画像槽数据隔离管控单元初始化完成")
        print(f"[{self.module_id}] 漏斗一白名单: {self.FUNNEL_ONE_WHITELIST}")
        print(f"[{self.module_id}] 自动驾驶模块访问禁止列表: {len(self.FORBIDDEN_MODULES)} 个模块")
    
    # ========== 隔离规则管理 ==========
    
    def register_slot(self, slot_id: int, partition_base: int, partition_size: int) -> None:
        """
        注册新子画像槽的隔离规则
        
        由 ad-05 在槽创建完成后调用
        """
        self.state = GateState.RULE_UPDATING
        
        self._rules[slot_id] = IsolationRule(
            slot_id=slot_id,
            partition_base=partition_base,
            partition_size=partition_size,
            allowed_modules=self.FUNNEL_ONE_WHITELIST.copy(),
            cross_slot_access=False,
            autonomous_access=False
        )
        
        self.state = GateState.NORMAL
        print(f"[{self.module_id}] 注册槽位隔离规则: slot_{slot_id}, base=0x{partition_base:X}")
    
    def unregister_slot(self, slot_id: int) -> None:
        """注销子画像槽的隔离规则"""
        if slot_id in self._rules:
            del self._rules[slot_id]
            print(f"[{self.module_id}] 注销槽位隔离规则: slot_{slot_id}")
    
    def set_slot_maintenance(self, slot_id: int, in_maintenance: bool) -> None:
        """设置槽位维护状态"""
        if in_maintenance:
            self._maintenance_slots.add(slot_id)
            self.state = GateState.SLOT_MAINTENANCE
        else:
            self._maintenance_slots.discard(slot_id)
            if not self._maintenance_slots:
                self.state = GateState.NORMAL
        print(f"[{self.module_id}] 槽位维护状态: slot_{slot_id}={in_maintenance}")
    
    # ========== 状态管控 ==========
    
    def set_emergency_readonly(self) -> None:
        """设置紧急只读状态"""
        self.state = GateState.EMERGENCY_RO
        print(f"[{self.module_id}] 进入紧急只读状态")
    
    def set_all_frozen(self) -> None:
        """设置全部冻结状态"""
        self.state = GateState.ALL_FROZEN
        print(f"[{self.module_id}] 进入全部冻结状态")
    
    def restore_normal(self) -> None:
        """恢复正常状态"""
        self.state = GateState.NORMAL
        print(f"[{self.module_id}] 恢复正常状态")
    
    # ========== 访问校验 ==========
    
    def validate_request(self, request: AccessRequest) -> AccessResponse:
        """
        校验数据访问请求
        
        校验规则优先级（从高到低）:
        1. 紧急熔断状态仅放行只读查询
        2. 全部冻结状态禁止写入和擦除
        3. 编译期禁止自动驾驶决策模块访问漏斗一
        4. 校验目标槽号是否存在
        5. 跨槽访问检测（无条件拦截）
        6. 槽位维护状态检查
        7. 来源模块白名单校验
        """
        self._total_requests += 1
        
        # 规则1: 紧急熔断状态仅放行只读查询
        if self.state == GateState.EMERGENCY_RO:
            if request.operation_type != AccessOperation.READ:
                return self._intercept(request, InterceptReason.EMERGENCY_RO)
        
        # 规则2: 全部冻结状态禁止写入和擦除
        if self.state == GateState.ALL_FROZEN:
            if request.operation_type in [AccessOperation.WRITE, AccessOperation.ERASE]:
                return self._intercept(request, InterceptReason.ALL_FROZEN)
        
        # 规则3: 编译期禁止自动驾驶决策模块访问漏斗一
        if request.source_module in self.FORBIDDEN_MODULES:
            return self._intercept(request, InterceptReason.AUTONOMOUS_DECISION_MODULE)
        
        # 规则4: 校验目标槽号是否存在
        if request.target_slot_id not in self._rules:
            return self._intercept(request, InterceptReason.INVALID_SLOT)
        
        # 规则5: 跨槽访问检测
        if request.source_slot_id is not None and request.source_slot_id != request.target_slot_id:
            return self._intercept(request, InterceptReason.CROSS_SLOT_ACCESS)
        
        # 规则6: 槽位维护状态检查
        if request.target_slot_id in self._maintenance_slots:
            return self._intercept(request, InterceptReason.SLOT_MAINTENANCE)
        
        # 规则7: 来源模块白名单校验
        target_rule = self._rules[request.target_slot_id]
        if request.source_module not in target_rule.allowed_modules:
            return self._intercept(request, InterceptReason.NOT_IN_WHITELIST)
        
        # 全部校验通过
        self._allowed_count += 1
        return AccessResponse(
            request_id=request.request_id,
            allowed=True
        )
    
    def _intercept(self, request: AccessRequest, reason: InterceptReason) -> AccessResponse:
        """拦截访问请求并记录安全日志"""
        self._intercepted_count += 1
        
        # 记录拦截日志
        log_entry = InterceptLogEntry(
            log_id=f"log-{uuid.uuid4().hex[:8]}",
            request_id=request.request_id,
            source_module=request.source_module,
            target_slot_id=request.target_slot_id,
            operation_type=request.operation_type,
            reason=reason,
            timestamp=time.time()
        )
        self._pending_logs.append(log_entry)
        
        print(f"[{self.module_id}] 拦截访问: {request.source_module} → slot_{request.target_slot_id}, "
              f"操作={request.operation_type.value}, 原因={reason.value}")
        
        return AccessResponse(
            request_id=request.request_id,
            allowed=False,
            intercept_reason=reason
        )
    
    # ========== 查询接口 ==========
    
    def is_slot_accessible(self, slot_id: int, module_id: str, operation: AccessOperation) -> bool:
        """查询指定槽位是否对指定模块可访问"""
        if self.state == GateState.EMERGENCY_RO and operation != AccessOperation.READ:
            return False
        if self.state == GateState.ALL_FROZEN and operation in [AccessOperation.WRITE, AccessOperation.ERASE]:
            return False
        if module_id in self.FORBIDDEN_MODULES:
            return False
        if slot_id not in self._rules:
            return False
        if slot_id in self._maintenance_slots:
            return False
        if module_id not in self._rules[slot_id].allowed_modules:
            return False
        return True
    
    def get_state(self) -> GateState:
        return self.state
    
    def get_all_registered_slots(self) -> List[int]:
        return list(self._rules.keys())
    
    # ========== 变更日志 ==========
    
    def collect_pending_logs(self) -> List[InterceptLogEntry]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_requests": self._total_requests,
            "allowed_count": self._allowed_count,
            "intercepted_count": self._intercepted_count,
            "intercept_rate": self._intercepted_count / max(self._total_requests, 1),
            "registered_slots": len(self._rules),
            "maintenance_slots": len(self._maintenance_slots),
            "current_state": self.state.value
        }


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-06 子画像槽数据隔离管控单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    # --- TC-06-01: 白名单模块写入放行 ---
    print("\n[TC-06-01] 白名单模块写入放行")
    try:
        gate = SlotIsolationGate()
        gate.register_slot(1, 0x1000, 1024)
        request = AccessRequest(
            request_id="req-001",
            source_module="ad-07",
            target_slot_id=1,
            operation_type=AccessOperation.WRITE
        )
        response = gate.validate_request(request)
        assert response.allowed == True
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-06-02: 自动驾驶决策模块被拦截 ---
    print("\n[TC-06-02] 自动驾驶决策模块访问被拦截")
    try:
        gate = SlotIsolationGate()
        gate.register_slot(1, 0x1000, 1024)
        request = AccessRequest(
            request_id="req-002",
            source_module="ECC-03",
            target_slot_id=1,
            operation_type=AccessOperation.READ
        )
        response = gate.validate_request(request)
        assert response.allowed == False
        assert response.intercept_reason == InterceptReason.AUTONOMOUS_DECISION_MODULE
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-06-03: 跨槽访问被拦截 ---
    print("\n[TC-06-03] 跨槽访问被拦截")
    try:
        gate = SlotIsolationGate()
        gate.register_slot(1, 0x1000, 1024)
        gate.register_slot(2, 0x2000, 1024)
        request = AccessRequest(
            request_id="req-003",
            source_module="ad-07",
            target_slot_id=2,
            operation_type=AccessOperation.READ,
            source_slot_id=1
        )
        response = gate.validate_request(request)
        assert response.allowed == False
        assert response.intercept_reason == InterceptReason.CROSS_SLOT_ACCESS
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-06-04: 紧急熔断状态仅放行只读 ---
    print("\n[TC-06-04] 紧急熔断状态仅放行只读")
    try:
        gate = SlotIsolationGate()
        gate.register_slot(1, 0x1000, 1024)
        gate.set_emergency_readonly()
        
        # 只读应放行
        read_request = AccessRequest("req-004", "ad-07", 1, AccessOperation.READ)
        assert gate.validate_request(read_request).allowed == True
        
        # 写入应拦截
        write_request = AccessRequest("req-005", "ad-07", 1, AccessOperation.WRITE)
        assert gate.validate_request(write_request).allowed == False
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-06-05: 非白名单模块被拦截 ---
    print("\n[TC-06-05] 非白名单模块被拦截")
    try:
        gate = SlotIsolationGate()
        gate.register_slot(1, 0x1000, 1024)
        request = AccessRequest(
            request_id="req-006",
            source_module="ad-99",
            target_slot_id=1,
            operation_type=AccessOperation.READ
        )
        response = gate.validate_request(request)
        assert response.allowed == False
        assert response.intercept_reason == InterceptReason.NOT_IN_WHITELIST
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-06-06: 无效槽号被拦截 ---
    print("\n[TC-06-06] 无效槽号被拦截")
    try:
        gate = SlotIsolationGate()
        gate.register_slot(1, 0x1000, 1024)
        request = AccessRequest(
            request_id="req-007",
            source_module="ad-07",
            target_slot_id=999,
            operation_type=AccessOperation.WRITE
        )
        response = gate.validate_request(request)
        assert response.allowed == False
        assert response.intercept_reason == InterceptReason.INVALID_SLOT
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-06-07: 槽位维护状态拦截 ---
    print("\n[TC-06-07] 槽位维护状态拦截")
    try:
        gate = SlotIsolationGate()
        gate.register_slot(1, 0x1000, 1024)
        gate.set_slot_maintenance(1, True)
        request = AccessRequest("req-008", "ad-07", 1, AccessOperation.WRITE)
        response = gate.validate_request(request)
        assert response.allowed == False
        assert response.intercept_reason == InterceptReason.SLOT_MAINTENANCE
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-06-08: 新槽注册后白名单模块可访问 ---
    print("\n[TC-06-08] 新槽注册后白名单模块可访问")
    try:
        gate = SlotIsolationGate()
        gate.register_slot(3, 0x3000, 2048)
        request = AccessRequest("req-009", "ad-10", 3, AccessOperation.READ)
        response = gate.validate_request(request)
        assert response.allowed == True
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- 测试结果汇总 ---
    print("\n" + "=" * 60)
    print(f"测试结果: {passed} PASS, {failed} FAIL")
    print("=" * 60)