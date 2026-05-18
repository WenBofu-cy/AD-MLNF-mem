#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-48
模块名称: 全局容量配额管控单元
所属分区: 五、存储与系统运维
核心职责: 监控双漏斗记忆系统的总存储占用量，执行 500MB 终身容量硬约束。
          当总使用率超过安全阈值时，向遗忘阈值判定单元发出收紧指令，并在必要时
          直接触发低重要度条目的批量清除。保护 L4/L5 及不可抗力标记条目不被强制清除。

依赖模块: ad-20/22/24/26（各层级存储单元，上报容量使用数据）、
          ad-40（遗忘阈值判定单元，接收阈值收紧指令）、
          ad-42（冗余记忆删除与归档单元，接收批量清除指令）、
          ad-01（总控漏斗 F₀，接收容量告警）
被依赖模块: ad-40（消费容量告急信号以动态调节遗忘阈值）、
            ad-42（消费批量清除指令）、ad-01（接收容量状态汇报）

安全约束:
  S-01: 总容量上限 500MB 为编译期默认值，OTA 可调整范围 100MB–1GB
  S-02: L4/L5 层级条目在任何告急等级下均不被纳入批量清除范围，L5 条目终身不可清除
  S-03: 不可抗力标记条目与 S≥0.7 的安全高价值条目在一级保护中
  S-04: 容量告急时仅能通过收紧遗忘阈值或触发低重要度清除来释放空间
  S-05: 所有容量告急、批量清除、配额调整操作全量写入 ad-51 不可变日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class CapacityLevel(Enum):
    """容量告急等级"""
    NORMAL = "normal"
    YELLOW = "yellow"      # 85%-90%
    ORANGE = "orange"      # 90%-95%
    RED = "red"            # >95%


class QuotaState(Enum):
    """配额管控单元内部状态"""
    NORMAL = "normal"
    YELLOW_ALERT = "yellow_alert"
    ORANGE_ALERT = "orange_alert"
    RED_ALERT = "red_alert"
    PAUSED = "paused"


# ==================== 数据结构 ====================

@dataclass
class LayerStorageSnapshot:
    """单层级存储占用快照"""
    layer: str                     # "L1"/"L2"/"L3"/"L4"/"L5"
    used_capacity: int             # 已用容量（字节）
    entry_count: int               # 条目数
    max_capacity: int              # 最大容量（字节）


@dataclass
class FunnelOneStorageSnapshot:
    """漏斗一存储占用快照"""
    total_used: int                # 总使用量（字节）
    slot_details: Dict[int, int]   # slot_id -> used_bytes


@dataclass
class CapacityAlertSignal:
    """容量告急信号（发送至 ad-40）"""
    usage_rate: float              # 总使用率 0.0-1.0
    alert_level: CapacityLevel
    threshold_adjust_factor: float # 遗忘阈值调整系数
    timestamp: float = field(default_factory=time.time)


@dataclass
class BatchClearInstruction:
    """批量清除指令（发送至 ad-42）"""
    instruction_id: str
    target_layers: List[str]       # 目标层级 ["L1", "L2"]
    clear_ratio: float             # 清除比例
    protected_s_values: float      # S≥该值的条目受保护
    protected_force_majeure: bool  # 不可抗力条目受保护
    priority_rule: str             # 优先级规则
    timestamp: float = field(default_factory=time.time)


@dataclass
class QuotaStatusReport:
    """容量状态汇报（发送至 ad-01、ad-03）"""
    total_usage_rate: float
    funnel_one_usage: int
    funnel_two_usage: int
    alert_level: CapacityLevel
    estimated_remaining_days: float
    layer_distribution: Dict[str, float]  # 层级 -> 占比
    timestamp: float = field(default_factory=time.time)


@dataclass
class QuotaAdjustmentSuggestion:
    """配额调整建议"""
    target_layer: str
    current_usage: int
    current_quota: int
    suggested_quota: int
    reason: str


# ==================== 主类定义 ====================

class GlobalQuotaControl:
    """
    全局容量配额管控单元
    
    职责:
    1. 接收各层级存储占用快照，计算总使用率
    2. 根据使用率判定告急等级（正常/黄色/橙色/红色）
    3. 向 ad-40 发送容量告急信号，动态调节遗忘阈值
    4. 红色告警时向 ad-42 发送批量清除指令
    5. 定期向 ad-01 汇报容量状态
    6. 检测层级配额不平衡时建议调整
    """
    
    # 容量阈值
    YELLOW_THRESHOLD = 0.85
    ORANGE_THRESHOLD = 0.90
    RED_THRESHOLD = 0.95
    
    # 容量恢复阈值（低于此值降级）
    RECOVER_THRESHOLD = 0.70
    
    # 遗忘阈值调整系数（对应各等级）
    ADJUST_FACTORS = {
        CapacityLevel.NORMAL: 1.0,
        CapacityLevel.YELLOW: 1.2,
        CapacityLevel.ORANGE: 1.5,
        CapacityLevel.RED: 2.0,
    }
    
    # 总容量上限（编译期默认值，OTA 可调）
    TOTAL_CAPACITY_BYTES = 500 * 1024 * 1024  # 500MB
    
    # OTA 可调范围
    MIN_CAPACITY = 100 * 1024 * 1024   # 100MB
    MAX_CAPACITY = 1024 * 1024 * 1024  # 1GB
    
    # 各层级默认配额比例
    DEFAULT_LAYER_RATIOS = {
        "L1": 0.60,
        "L2": 0.25,
        "L3": 0.10,
        "L4": 0.045,
        "L5": 0.005,
    }
    
    # 批量清除保护条件
    CLEAR_PROTECTED_S_THRESHOLD = 0.7
    
    # 清除后目标使用率
    CLEAR_TARGET_USAGE = 0.85
    
    # 配额检查间隔（秒）
    QUOTA_CHECK_INTERVAL = 3600  # 1小时
    
    def __init__(self):
        self.module_id = "ad-48"
        self.module_name = "全局容量配额管控单元"
        
        # 内部状态
        self.state = QuotaState.NORMAL
        
        # 总容量上限
        self._total_capacity = self.TOTAL_CAPACITY_BYTES
        
        # 各层级存储使用量: layer -> used_bytes
        self._layer_usage: Dict[str, int] = {
            "L1": 0, "L2": 0, "L3": 0, "L4": 0, "L5": 0
        }
        
        # 漏斗一使用量
        self._funnel_one_usage: int = 0
        
        # 上次配额检查时间
        self._last_quota_check = 0.0
        
        # 上次清除时间
        self._last_clear_time = 0.0
        self._min_clear_interval = 60  # 至少间隔60秒
        
        # 统计
        self._total_alerts = 0
        self._total_clears = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 全局容量配额管控单元初始化完成")
        print(f"[{self.module_id}] 总容量上限: {self._total_capacity/1024/1024:.0f}MB")
    
    # ========== 状态管理 ==========
    
    def set_total_capacity(self, new_capacity: int) -> Tuple[bool, str]:
        """
        设置新的总容量上限（OTA 调整）
        
        S-01: 范围校验 100MB–1GB，须为 10MB 整数倍，不可低于当前已使用量
        
        Args:
            new_capacity: 新容量上限（字节）
            
        Returns:
            (是否成功, 消息)
        """
        if new_capacity < self.MIN_CAPACITY:
            return False, f"容量下限为 {self.MIN_CAPACITY/1024/1024:.0f}MB"
        if new_capacity > self.MAX_CAPACITY:
            return False, f"容量上限为 {self.MAX_CAPACITY/1024/1024:.0f}MB"
        if new_capacity % (10 * 1024 * 1024) != 0:
            return False, "容量须为 10MB 整数倍"
        
        current_used = self._get_total_used()
        if new_capacity < current_used:
            return False, f"新容量 {new_capacity/1024/1024:.0f}MB 低于当前已使用量 {current_used/1024/1024:.0f}MB"
        
        old_capacity = self._total_capacity
        self._total_capacity = new_capacity
        
        self._log_event("CAPACITY_CHANGED", {
            "old": old_capacity,
            "new": new_capacity,
            "current_used": current_used
        })
        
        print(f"[{self.module_id}] 容量上限已调整: {old_capacity/1024/1024:.0f}MB → {new_capacity/1024/1024:.0f}MB")
        return True, f"容量上限已更新为 {new_capacity/1024/1024:.0f}MB"
    
    def pause(self) -> None:
        self.state = QuotaState.PAUSED
    
    def resume(self) -> None:
        self.state = QuotaState.NORMAL
    
    def get_state(self) -> QuotaState:
        return self.state
    
    # ========== 存储占用快照接收 ==========
    
    def update_layer_usage(self, snapshots: List[LayerStorageSnapshot]) -> None:
        """
        更新各层级存储使用量
        
        Args:
            snapshots: 各层级存储占用快照列表
        """
        for snap in snapshots:
            if snap.layer in self._layer_usage:
                self._layer_usage[snap.layer] = snap.used_capacity
    
    def update_funnel_one_usage(self, snapshot: FunnelOneStorageSnapshot) -> None:
        """
        更新漏斗一存储使用量
        
        Args:
            snapshot: 漏斗一存储占用快照
        """
        self._funnel_one_usage = snapshot.total_used
    
    # ========== 容量监控与告警 ==========
    
    def evaluate_capacity(self) -> Tuple[Optional[CapacityAlertSignal], Optional[BatchClearInstruction]]:
        """
        评估当前容量状态，返回相应的告警信号或清除指令
        
        Returns:
            (告警信号, 清除指令) — 至少一个为 None
        """
        if self.state == QuotaState.PAUSED:
            return None, None
        
        usage_rate = self._get_usage_rate()
        alert_level = self._determine_alert_level(usage_rate)
        
        # 更新内部状态
        self._update_state(alert_level)
        
        alert_signal = None
        clear_instruction = None
        
        # 生成告警信号
        if alert_level != CapacityLevel.NORMAL:
            adjust_factor = self.ADJUST_FACTORS.get(alert_level, 1.0)
            alert_signal = CapacityAlertSignal(
                usage_rate=usage_rate,
                alert_level=alert_level,
                threshold_adjust_factor=adjust_factor
            )
            self._total_alerts += 1
        
        # 红色告警且距上次清除超过间隔 → 生成批量清除指令
        if alert_level == CapacityLevel.RED:
            now = time.time()
            if now - self._last_clear_time >= self._min_clear_interval:
                clear_instruction = self._generate_clear_instruction(usage_rate)
                self._last_clear_time = now
                self._total_clears += 1
        
        return alert_signal, clear_instruction
    
    def _get_usage_rate(self) -> float:
        """计算总使用率"""
        funnel_two_usage = sum(self._layer_usage.values())
        total_used = self._funnel_one_usage + funnel_two_usage
        return total_used / self._total_capacity if self._total_capacity > 0 else 0.0
    
    def _get_total_used(self) -> int:
        """获取总已使用量"""
        return self._funnel_one_usage + sum(self._layer_usage.values())
    
    def _determine_alert_level(self, usage_rate: float) -> CapacityLevel:
        """根据使用率判定告急等级"""
        if usage_rate >= self.RED_THRESHOLD:
            return CapacityLevel.RED
        elif usage_rate >= self.ORANGE_THRESHOLD:
            return CapacityLevel.ORANGE
        elif usage_rate >= self.YELLOW_THRESHOLD:
            return CapacityLevel.YELLOW
        else:
            return CapacityLevel.NORMAL
    
    def _update_state(self, alert_level: CapacityLevel) -> None:
        """更新内部状态"""
        state_map = {
            CapacityLevel.NORMAL: QuotaState.NORMAL,
            CapacityLevel.YELLOW: QuotaState.YELLOW_ALERT,
            CapacityLevel.ORANGE: QuotaState.ORANGE_ALERT,
            CapacityLevel.RED: QuotaState.RED_ALERT,
        }
        new_state = state_map.get(alert_level, QuotaState.NORMAL)
        
        # 恢复检测
        if self.state in [QuotaState.YELLOW_ALERT, QuotaState.ORANGE_ALERT, QuotaState.RED_ALERT]:
            if self._get_usage_rate() < self.RECOVER_THRESHOLD:
                new_state = QuotaState.NORMAL
        
        if new_state != self.state:
            old_state = self.state
            self.state = new_state
            print(f"[{self.module_id}] 状态变更: {old_state.value} → {new_state.value}, 使用率={self._get_usage_rate():.1%}")
    
    def _generate_clear_instruction(self, usage_rate: float) -> BatchClearInstruction:
        """
        生成批量清除指令
        
        目标：将使用率降至 CLEAR_TARGET_USAGE 以下
        仅清除 L1/L2 中非保护条目
        """
        target_reduction = (usage_rate - self.CLEAR_TARGET_USAGE) * self._total_capacity
        l1_l2_usage = self._layer_usage.get("L1", 0) + self._layer_usage.get("L2", 0)
        
        if l1_l2_usage <= 0:
            clear_ratio = 0.20
        else:
            clear_ratio = min(target_reduction / l1_l2_usage, 0.30)  # 最多清除30%
        
        instruction = BatchClearInstruction(
            instruction_id=f"clear-{uuid.uuid4().hex[:8]}",
            target_layers=["L1", "L2"],
            clear_ratio=clear_ratio,
            protected_s_values=self.CLEAR_PROTECTED_S_THRESHOLD,
            protected_force_majeure=True,
            priority_rule="按I值升序，跳过L4/L5，跳过不可抗力，跳过S≥0.7"
        )
        
        print(f"[{self.module_id}] 生成批量清除指令: 清除率={clear_ratio:.1%}, 目标释放={target_reduction/1024/1024:.1f}MB")
        return instruction
    
    # ========== 配额调整建议 ==========
    
    def check_quota_balance(self) -> List[QuotaAdjustmentSuggestion]:
        """
        检查各层级配额是否平衡
        
        Returns:
            配额调整建议列表
        """
        now = time.time()
        if now - self._last_quota_check < self.QUOTA_CHECK_INTERVAL:
            return []
        
        self._last_quota_check = now
        suggestions = []
        
        for layer, ratio in self.DEFAULT_LAYER_RATIOS.items():
            current_quota = int(self._total_capacity * ratio)
            current_usage = self._layer_usage.get(layer, 0)
            
            if current_quota > 0 and current_usage > current_quota * 1.2:
                suggestions.append(QuotaAdjustmentSuggestion(
                    target_layer=layer,
                    current_usage=current_usage,
                    current_quota=current_quota,
                    suggested_quota=int(current_usage * 1.2),
                    reason=f"{layer} 使用量超过配额 20%"
                ))
        
        return suggestions
    
    # ========== 状态汇报 ==========
    
    def generate_status_report(self) -> QuotaStatusReport:
        """生成容量状态汇报"""
        usage_rate = self._get_usage_rate()
        alert_level = self._determine_alert_level(usage_rate)
        
        # 计算层级分布
        total_used = self._get_total_used()
        distribution = {}
        if total_used > 0:
            for layer, usage in self._layer_usage.items():
                distribution[layer] = usage / total_used
        
        # 估算剩余天数（简化：假设增长速率为每日1%总容量）
        daily_growth = self._total_capacity * 0.01
        remaining_bytes = self._total_capacity * self.CLEAR_TARGET_USAGE - total_used
        estimated_days = remaining_bytes / daily_growth if daily_growth > 0 and remaining_bytes > 0 else 30
        
        return QuotaStatusReport(
            total_usage_rate=usage_rate,
            funnel_one_usage=self._funnel_one_usage,
            funnel_two_usage=sum(self._layer_usage.values()),
            alert_level=alert_level,
            estimated_remaining_days=estimated_days,
            layer_distribution=distribution
        )
    
    # ========== 查询接口 ==========
    
    def get_usage_rate(self) -> float:
        return self._get_usage_rate()
    
    def get_alert_level(self) -> CapacityLevel:
        return self._determine_alert_level(self._get_usage_rate())
    
    def get_total_capacity(self) -> int:
        return self._total_capacity
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_capacity": self._total_capacity,
            "total_used": self._get_total_used(),
            "usage_rate": self._get_usage_rate(),
            "alert_level": self.get_alert_level().value,
            "total_alerts": self._total_alerts,
            "total_clears": self._total_clears,
            "layer_usage": self._layer_usage.copy(),
            "funnel_one_usage": self._funnel_one_usage,
            "state": self.state.value
        }
    
    # ========== 变更日志 ==========
    
    def _log_event(self, event_type: str, details: Dict[str, Any]) -> None:
        self._pending_logs.append({
            "log_id": f"quota-{uuid.uuid4().hex[:8]}",
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
    print("ad-48 全局容量配额管控单元 单元测试")
    print("=" * 60)
    passed, failed = 0, 0
    
    # TC-48-01: 正常使用率无告警
    print("\n[TC-48-01] 使用率 50% 无告警")
    try:
        quota = GlobalQuotaControl()
        quota.update_layer_usage([
            LayerStorageSnapshot("L1", int(300*1024*1024*0.5), 100, int(300*1024*1024)),
        ])
        alert, clear = quota.evaluate_capacity()
        assert alert is None and clear is None
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-48-02: 使用率 87% 黄色告警
    print("\n[TC-48-02] 使用率 87% 黄色告警")
    try:
        quota = GlobalQuotaControl()
        quota.update_layer_usage([
            LayerStorageSnapshot("L1", int(500*1024*1024*0.87), 100, int(500*1024*1024)),
        ])
        alert, clear = quota.evaluate_capacity()
        assert alert is not None
        assert alert.alert_level == CapacityLevel.YELLOW
        assert alert.threshold_adjust_factor == 1.2
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-48-03: 使用率 96% 红色告警并触发清除
    print("\n[TC-48-03] 使用率 96% 红色告警 + 批量清除指令")
    try:
        quota = GlobalQuotaControl()
        quota.update_layer_usage([
            LayerStorageSnapshot("L1", int(500*1024*1024*0.96), 200, int(500*1024*1024)),
        ])
        alert, clear = quota.evaluate_capacity()
        assert alert is not None and alert.alert_level == CapacityLevel.RED
        assert clear is not None
        assert "L1" in clear.target_layers
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-48-04: OTA 调整容量上限成功
    print("\n[TC-48-04] OTA 调整容量上限至 200MB")
    try:
        quota = GlobalQuotaControl()
        ok, msg = quota.set_total_capacity(200 * 1024 * 1024)
        assert ok
        assert quota._total_capacity == 200 * 1024 * 1024
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-48-05: OTA 调整低于下限被拒
    print("\n[TC-48-05] OTA 调整至 50MB 被拒（低于下限）")
    try:
        quota = GlobalQuotaControl()
        ok, msg = quota.set_total_capacity(50 * 1024 * 1024)
        assert not ok
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-48-06: 状态恢复检测
    print("\n[TC-48-06] 使用率从 90% 回落至 60% 恢复 NORMAL")
    try:
        quota = GlobalQuotaControl()
        quota.update_layer_usage([
            LayerStorageSnapshot("L1", int(500*1024*1024*0.90), 100, int(500*1024*1024)),
        ])
        quota.evaluate_capacity()
        assert quota.state == QuotaState.ORANGE_ALERT
        
        quota.update_layer_usage([
            LayerStorageSnapshot("L1", int(500*1024*1024*0.60), 50, int(500*1024*1024)),
        ])
        quota.evaluate_capacity()
        assert quota.state == QuotaState.NORMAL
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-48-07: 容量状态汇报生成
    print("\n[TC-48-07] 容量状态汇报生成")
    try:
        quota = GlobalQuotaControl()
        quota.update_layer_usage([
            LayerStorageSnapshot("L1", 100*1024*1024, 50, 300*1024*1024),
            LayerStorageSnapshot("L2", 50*1024*1024, 30, 125*1024*1024),
        ])
        report = quota.generate_status_report()
        assert report.total_usage_rate > 0
        assert len(report.layer_distribution) >= 2
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    print(f"\n测试结果: {passed} PASS, {failed} FAIL")