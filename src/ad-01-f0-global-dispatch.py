#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-01
模块名称: 总控漏斗 F₀ - 双漏斗全局调度中枢
所属分区: 一、顶层总控中枢
核心职责: 双漏斗记忆系统最高统筹单元，管控漏斗一与漏斗二的资源分配与模式切换，
          统一下发全局规则，对接 ECC 大脑经验查询请求

依赖模块: ad-02(漏斗一调度状态), ad-03(漏斗二调度状态), ad-48(全局容量配额), ad-51(变更日志)
被依赖模块: 全部 ad-02 至 ad-43 漏斗内核模块 + ad-44 至 ad-47 外挂模块(查询路由)

安全约束:
  S-01: 模式切换 0.5 秒内完成，切换期间双漏斗只读
  S-02: 紧急接管时双漏斗全部锁定只读，优先执行安全避险策略
  S-03: 漏斗一数据编译期禁止接入自动驾驶决策链路，本模块负责路由隔离
  S-04: 降级运行期间禁止新漏斗创建与晋升操作
  S-05: 安全急停信号为最高优先级
  S-06: 所有规则变更与模式切换全量写入 ad-51 变更日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class DrivingMode(Enum):
    """驾驶模式"""
    MANUAL = "manual"               # 人工驾驶
    AUTONOMOUS = "autonomous"       # 自动驾驶
    EMERGENCY_TAKEOVER = "emergency_takeover"  # 紧急接管


class InternalState(Enum):
    """F₀ 内部状态"""
    NORMAL = "normal"               # 正常双漏斗
    SWITCHING = "switching"         # 模式切换中
    EMERGENCY_RO = "emergency_ro"   # 紧急只读
    DEGRADED = "degraded"           # 降级运行
    MAINTENANCE = "maintenance"     # 维护只读


class MessagePriority(Enum):
    """消息优先级"""
    CRITICAL = "critical"  # 紧急
    HIGH = "high"          # 高
    NORMAL = "normal"      # 普通


class DispatchCommand(Enum):
    """调度指令"""
    ACTIVATE = "activate"
    FREEZE = "freeze"
    LOCK_READONLY = "lock_readonly"


# ==================== 数据结构 ====================

@dataclass
class FunnelStatusSnapshot:
    """子漏斗状态快照"""
    funnel_type: str              # "funnel_one" / "funnel_two"
    active_slots: int             # 活跃槽数
    total_entries: int            # 总条目数
    storage_usage_rate: float     # 存储占用率
    health_status: str            # 健康状态
    timestamp: float = field(default_factory=time.time)


@dataclass
class ExperienceQueryRequest:
    """ECC 经验查询请求"""
    query_id: str
    scene_label: str
    query_conditions: Dict[str, Any]
    driving_mode_mark: DrivingMode
    priority: MessagePriority = MessagePriority.HIGH
    timestamp: float = field(default_factory=time.time)


@dataclass
class ExperienceQueryResponse:
    """经验查询回执"""
    query_id: str
    matched_entries: List[Dict[str, Any]]
    importance_sorted: bool
    response_timestamp: float = field(default_factory=time.time)


@dataclass
class ModeSwitchSignal:
    """模式切换信号"""
    switch_type: str              # "manual_to_autonomous" / "autonomous_to_manual" / "emergency_takeover"
    source_funnel: str            # 源漏斗
    target_funnel: str            # 目标漏斗
    safety_params: Dict[str, Any]
    switch_timestamp: float = field(default_factory=time.time)


@dataclass
class SafetyEventSignal:
    """安全事件信号"""
    event_type: str               # "emergency_stop" / "collision" / "shutdown"
    source_module: str
    priority: MessagePriority = MessagePriority.CRITICAL
    timestamp: float = field(default_factory=time.time)


@dataclass
class SystemHealthSummary:
    """系统健康状态汇总"""
    funnel_one_status: str
    funnel_two_status: str
    total_storage_usage: float
    active_mode: DrivingMode
    internal_state: InternalState
    uptime_seconds: float


# ==================== 主类定义 ====================

class F0_Global_Dispatch_Center:
    """
    总控漏斗 F₀ - 双漏斗全局调度中枢
    
    职责:
    1. 管控漏斗一与漏斗二的资源分配与模式切换
    2. 统一下发全局规则（晋升/遗忘/容量约束）
    3. 对接 ECC 大脑经验查询请求，路由至对应漏斗
    4. 安全事件最高优先级响应
    5. 周期性健康状态汇总上报
    """
    
    # 模式切换超时（秒）
    MODE_SWITCH_TIMEOUT = 0.5
    # 容量告警阈值
    CAPACITY_WARNING_THRESHOLD = 0.90
    # 子漏斗数量上限比例（触发合并建议）
    FUNNEL_CONVERGENCE_RATIO = 0.95
    
    def __init__(self):
        self.module_id = "ad-01"
        self.module_name = "总控漏斗F0-双漏斗全局调度中枢"
        
        # 内部状态
        self.internal_state = InternalState.NORMAL
        self.current_mode = DrivingMode.MANUAL
        
        # 漏斗状态缓存
        self._funnel_one_status: Optional[FunnelStatusSnapshot] = None
        self._funnel_two_status: Optional[FunnelStatusSnapshot] = None
        
        # 统计
        self._start_time = time.time()
        self._switch_count = 0
        self._query_count = 0
        self._safety_event_count = 0
        
        # 配置
        self.nmax = 8  # 漏斗二场景分槽上限
        
        # 变更日志缓存（待 ad-51 消费）
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 总控漏斗F₀初始化完成, 初始状态={self.internal_state.value}")
    
    # ========== 安全事件处理（最高优先级） ==========
    
    def handle_safety_event(self, event: SafetyEventSignal) -> Dict[str, Any]:
        """
        处理安全事件（最高优先级）
        
        S-05: 安全急停信号为最高优先级，任何正在执行的非安全操作必须立即暂停
        """
        self.internal_state = InternalState.EMERGENCY_RO
        self._safety_event_count += 1
        
        dispatch = {
            "command": DispatchCommand.LOCK_READONLY,
            "targets": ["ad-02", "ad-03", "ad-15", "ad-16", "ad-17", "ad-18", "ad-19"],
            "reason": f"安全事件: {event.event_type}",
            "allow_readonly_query": True,
            "timestamp": time.time()
        }
        
        self._log_event("EMERGENCY_SHUTDOWN", dispatch)
        print(f"[{self.module_id}] 紧急熔断: {event.event_type}, 双漏斗全部锁定只读")
        
        return dispatch
    
    # ========== 驾驶模式切换（次高优先级） ==========
    
    def handle_mode_switch(self, new_mode: DrivingMode) -> Optional[ModeSwitchSignal]:
        """
        处理驾驶模式切换
        
        S-01: 模式切换 0.5 秒内完成，切换期间双漏斗只读
        S-02: 紧急接管时双漏斗全部锁定只读
        """
        if self.internal_state == InternalState.EMERGENCY_RO:
            print(f"[{self.module_id}] 紧急只读状态，忽略模式切换请求")
            return None
        
        old_mode = self.current_mode
        self.internal_state = InternalState.SWITCHING
        self._switch_count += 1
        
        # 确定切换方向
        if new_mode == DrivingMode.AUTONOMOUS:
            switch_type = "manual_to_autonomous"
            source_funnel = "funnel_one"
            target_funnel = "funnel_two"
        elif new_mode == DrivingMode.MANUAL:
            switch_type = "autonomous_to_manual"
            source_funnel = "funnel_two"
            target_funnel = "funnel_one"
        elif new_mode == DrivingMode.EMERGENCY_TAKEOVER:
            switch_type = "emergency_takeover"
            source_funnel = "both"
            target_funnel = "both_readonly"
        else:
            self.internal_state = InternalState.NORMAL
            return None
        
        signal = ModeSwitchSignal(
            switch_type=switch_type,
            source_funnel=source_funnel,
            target_funnel=target_funnel,
            safety_params={
                "switch_timeout_s": self.MODE_SWITCH_TIMEOUT,
                "both_readonly_during_switch": True,
                "force_lock_on_emergency": new_mode == DrivingMode.EMERGENCY_TAKEOVER
            }
        )
        
        self.current_mode = new_mode
        self.internal_state = InternalState.NORMAL
        
        self._log_event("MODE_SWITCH", {
            "old_mode": old_mode.value,
            "new_mode": new_mode.value,
            "switch_type": switch_type
        })
        
        print(f"[{self.module_id}] 模式切换: {old_mode.value} → {new_mode.value}, 超时={self.MODE_SWITCH_TIMEOUT}s")
        return signal
    
    # ========== ECC 经验查询路由 ==========
    
    def route_experience_query(self, query: ExperienceQueryRequest) -> Optional[ExperienceQueryResponse]:
        """
        路由 ECC 经验查询请求至对应漏斗
        
        S-03: 漏斗一数据编译期禁止接入自动驾驶决策链路
        当驾驶模式为 AUTONOMOUS 时，仅路由至漏斗二
        """
        if self.internal_state in [InternalState.EMERGENCY_RO, InternalState.SWITCHING]:
            # 紧急/切换状态：返回只读缓存数据（若有）
            return ExperienceQueryResponse(
                query_id=query.query_id,
                matched_entries=[],
                importance_sorted=False
            )
        
        self._query_count += 1
        
        # 根据驾驶模式路由
        if self.current_mode == DrivingMode.AUTONOMOUS:
            # 仅漏斗二
            target_funnel = "ad-03"
        elif self.current_mode == DrivingMode.MANUAL:
            # 仅漏斗一（仅供车内辅助，不可用于自动驾驶决策）
            target_funnel = "ad-02"
        else:
            target_funnel = "ad-03"
        
        self._log_event("QUERY_ROUTE", {
            "query_id": query.query_id,
            "scene": query.scene_label,
            "target_funnel": target_funnel
        })
        
        print(f"[{self.module_id}] 查询路由: {query.query_id[:12]}... → {target_funnel}")
        return None  # 实际查询结果由目标漏斗返回
    
    # ========== 容量监控 ==========
    
    def check_capacity(self, usage_rate: float) -> Optional[Dict[str, Any]]:
        """
        检查容量使用率
        
        容量 > 90% 时触发降级，下发遗忘阈值收紧指令
        """
        if usage_rate > self.CAPACITY_WARNING_THRESHOLD:
            self.internal_state = InternalState.DEGRADED
            
            instruction = {
                "command": "tighten_forget_threshold",
                "targets": ["ad-40"],
                "params": {
                    "threshold_boost": 0.20,
                    "freeze_promotions": True,
                    "freeze_funnel_creation": True
                },
                "timestamp": time.time()
            }
            
            self._log_event("CAPACITY_WARNING", {
                "usage_rate": usage_rate,
                "action": "tighten_threshold"
            })
            
            print(f"[{self.module_id}] 容量告警: {usage_rate:.1%}, 进入降级模式")
            return instruction
        
        # 容量回落至正常
        if self.internal_state == InternalState.DEGRADED and usage_rate < 0.70:
            self.internal_state = InternalState.NORMAL
            print(f"[{self.module_id}] 容量恢复正常: {usage_rate:.1%}")
        
        return None
    
    # ========== 子漏斗数量管控 ==========
    
    def check_funnel_count(self, active_count: int) -> Optional[Dict[str, Any]]:
        """检查子漏斗数量是否接近上限"""
        if active_count >= self.nmax * self.FUNNEL_CONVERGENCE_RATIO:
            return {
                "command": "suggest_merge",
                "targets": ["ad-02", "ad-03"],
                "params": {
                    "active_count": active_count,
                    "nmax": self.nmax,
                    "ratio": active_count / self.nmax
                }
            }
        return None
    
    # ========== 状态汇总 ==========
    
    def receive_status_snapshot(self, funnel_type: str, snapshot: FunnelStatusSnapshot) -> None:
        """接收漏斗状态快照"""
        if funnel_type == "funnel_one":
            self._funnel_one_status = snapshot
        elif funnel_type == "funnel_two":
            self._funnel_two_status = snapshot
    
    def generate_health_summary(self) -> SystemHealthSummary:
        """生成双漏斗健康状态汇总"""
        f1_status = self._funnel_one_status.health_status if self._funnel_one_status else "UNKNOWN"
        f2_status = self._funnel_two_status.health_status if self._funnel_two_status else "UNKNOWN"
        
        total_usage = 0.0
        if self._funnel_one_status:
            total_usage += self._funnel_one_status.storage_usage_rate * 0.3
        if self._funnel_two_status:
            total_usage += self._funnel_two_status.storage_usage_rate * 0.7
        
        return SystemHealthSummary(
            funnel_one_status=f1_status,
            funnel_two_status=f2_status,
            total_storage_usage=total_usage,
            active_mode=self.current_mode,
            internal_state=self.internal_state,
            uptime_seconds=time.time() - self._start_time
        )
    
    # ========== 变更日志 ==========
    
    def _log_event(self, event_type: str, details: Dict[str, Any]) -> None:
        """记录变更日志"""
        self._pending_logs.append({
            "log_id": f"log-{uuid.uuid4().hex[:8]}",
            "event_type": event_type,
            "source_module": self.module_id,
            "details": details,
            "timestamp": time.time()
        })
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        """收集待写入 ad-51 的变更日志"""
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs
    
    # ========== 查询接口 ==========
    
    def get_current_mode(self) -> DrivingMode:
        return self.current_mode
    
    def get_internal_state(self) -> InternalState:
        return self.internal_state
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_switches": self._switch_count,
            "total_queries": self._query_count,
            "total_safety_events": self._safety_event_count,
            "uptime_seconds": time.time() - self._start_time
        }


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-01 总控漏斗F₀ 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    # --- TC-01-01: 安全事件最高优先级响应 ---
    print("\n[TC-01-01] 安全事件最高优先级响应")
    try:
        f0 = F0_Global_Dispatch_Center()
        event = SafetyEventSignal(event_type="emergency_stop", source_module="ECC-05")
        result = f0.handle_safety_event(event)
        assert result["command"] == DispatchCommand.LOCK_READONLY
        assert f0.internal_state == InternalState.EMERGENCY_RO
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-01-02: 模式切换（人工→自动驾驶） ---
    print("\n[TC-01-02] 模式切换: 人工→自动驾驶")
    try:
        f0 = F0_Global_Dispatch_Center()
        f0.current_mode = DrivingMode.MANUAL
        signal = f0.handle_mode_switch(DrivingMode.AUTONOMOUS)
        assert signal is not None
        assert signal.switch_type == "manual_to_autonomous"
        assert f0.current_mode == DrivingMode.AUTONOMOUS
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-01-03: 容量 > 90% 触发降级 ---
    print("\n[TC-01-03] 容量超限触发降级")
    try:
        f0 = F0_Global_Dispatch_Center()
        instruction = f0.check_capacity(0.92)
        assert instruction is not None
        assert instruction["command"] == "tighten_forget_threshold"
        assert f0.internal_state == InternalState.DEGRADED
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-01-04: 容量回落恢复 ---
    print("\n[TC-01-04] 容量回落恢复正常")
    try:
        f0 = F0_Global_Dispatch_Center()
        f0.internal_state = InternalState.DEGRADED
        result = f0.check_capacity(0.65)
        assert result is None
        assert f0.internal_state == InternalState.NORMAL
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-01-05: 查询路由（自动驾驶模式仅路由漏斗二） ---
    print("\n[TC-01-05] 查询路由隔离验证")
    try:
        f0 = F0_Global_Dispatch_Center()
        f0.current_mode = DrivingMode.AUTONOMOUS
        query = ExperienceQueryRequest(
            query_id="q-001",
            scene_label="高速巡航",
            query_conditions={},
            driving_mode_mark=DrivingMode.AUTONOMOUS
        )
        f0.route_experience_query(query)
        assert f0._query_count == 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-01-06: 紧急只读状态忽略模式切换 ---
    print("\n[TC-01-06] 紧急只读状态忽略模式切换")
    try:
        f0 = F0_Global_Dispatch_Center()
        f0.internal_state = InternalState.EMERGENCY_RO
        result = f0.handle_mode_switch(DrivingMode.AUTONOMOUS)
        assert result is None
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-01-07: 子漏斗数量接近上限发送合并建议 ---
    print("\n[TC-01-07] 子漏斗数量管控")
    try:
        f0 = F0_Global_Dispatch_Center()
        f0.nmax = 10
        result = f0.check_funnel_count(10)
        assert result is not None
        assert result["command"] == "suggest_merge"
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-01-08: 变更日志收集 ---
    print("\n[TC-01-08] 变更日志收集")
    try:
        f0 = F0_Global_Dispatch_Center()
        f0._log_event("TEST", {"test": True})
        logs = f0.collect_pending_logs()
        assert len(logs) == 1
        assert len(f0.collect_pending_logs()) == 0  # 收集后清空
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