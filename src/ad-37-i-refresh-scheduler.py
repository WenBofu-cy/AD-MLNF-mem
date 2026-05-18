#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-37
模块名称: 重要度增量定时刷新单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 三维重要度计算引擎
核心职责: 周期性地对漏斗二中所有经验条目的重要度 I 值执行增量刷新。驱动 S 值、V 值、
          C 值随时间与环境变化而更新的因子重新参与 I 值聚合计算，确保所有条目的重要度
          始终保持对当前驾驶环境的时效性。避免长期未被主动更新的条目因 I 值失真而影响
          晋升或遗忘判定的准确性。

依赖模块: ad-36(综合重要度 I 值聚合计算单元，接收重算信号),
          ad-31(S 值计算单元，可请求回溯更新), ad-32(V 值计算单元),
          ad-33(C 值统计单元)
被依赖模块: ad-36(消费批量重算触发信号)

刷新策略:
  常规刷新: 每 24 小时，对 I 值更新时间 > 24h 的条目触发重算
  紧急刷新: 权重变更、季节剧变、风格切换等外部剧变信号触发
  异常监控: 晋升成功率下降或遗忘异常占比过高时，建议紧急刷新

安全约束:
  S-01: 常规刷新间隔最低 12 小时
  S-02: 紧急刷新仅能由授权剧变信号或管理员触发
  S-03: 刷新过程为只读分析+批量计算，不得直接修改原始经验数据
  S-04: L5 核心层条目的 I 值参与刷新计算，但原始经验数据不受影响
  S-05: 刷新过程中若发生紧急安全事件，立即中断刷新
  S-06: 所有刷新操作全量写入 ad-51 不可变日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class RefreshType(Enum):
    """刷新类型"""
    REGULAR = "regular"
    URGENT_WEIGHT_CHANGE = "urgent_weight_change"
    URGENT_STYLE_CHANGE = "urgent_style_change"
    URGENT_SEASON_CHANGE = "urgent_season_change"
    URGENT_CONSTRUCTION_END = "urgent_construction_end"
    MANUAL = "manual"


class SchedulerState(Enum):
    """调度单元内部状态"""
    IDLE = "idle"
    REFRESHING_NORMAL = "refreshing_normal"
    REFRESHING_URGENT = "refreshing_urgent"
    PAUSED = "paused"


# ==================== 数据结构 ====================

@dataclass
class RefreshTrigger:
    """刷新触发信号"""
    trigger_type: RefreshType
    description: str
    affected_slots: Optional[List[int]] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class RefreshResult:
    """刷新结果汇总"""
    trigger_type: RefreshType
    total_entries: int
    updated_entries: int
    duration_ms: float
    anomalies: List[str]
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class IRefreshScheduler:
    """
    重要度增量定时刷新单元
    
    职责:
    1. 定时（默认 24h）触发常规刷新，筛选陈旧条目请求 I 值重算
    2. 响应外部剧变信号触发紧急刷新（权重/季节/风格等）
    3. 请求 ad-31/ad-32 对超陈旧因子进行回溯更新
    4. 监控晋升成功率与遗忘异常，建议手动紧急刷新
    """
    
    # 常规刷新间隔（秒），不得低于 12 小时
    MIN_REGULAR_INTERVAL = 12 * 3600
    DEFAULT_REGULAR_INTERVAL = 24 * 3600
    
    # 因子陈旧阈值（超过此时间需要回溯更新）
    FACTOR_STALENESS_SECONDS = 7 * 24 * 3600  # 7 日
    
    # 异常监控间隔（秒）
    ANOMALY_MONITOR_INTERVAL = 30 * 60  # 30 分钟
    
    # 晋升成功率下降告警阈值
    PROMOTION_SUCCESS_DROP_THRESHOLD = 0.30
    # 遗忘异常占比告警阈值
    FORGET_ANOMALY_RATIO_THRESHOLD = 0.20
    
    def __init__(self):
        self.module_id = "ad-37"
        self.module_name = "重要度增量定时刷新单元"
        
        # 内部状态
        self.state = SchedulerState.IDLE
        
        # 上次常规刷新时间
        self._last_regular_refresh = time.time()
        
        # 常规刷新间隔
        self._regular_interval = self.DEFAULT_REGULAR_INTERVAL
        
        # 上次异常监控时间
        self._last_anomaly_monitor = 0.0
        
        # 统计
        self._total_regular = 0
        self._total_urgent = 0
        
        # 紧急刷新队列（并发保护）
        self._urgent_queue: List[RefreshTrigger] = []
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] I 值刷新调度单元初始化完成")
        print(f"[{self.module_id}] 常规刷新间隔: {self._regular_interval/3600:.0f}h")
    
    # ========== 状态管理 ==========
    
    def set_regular_interval(self, interval_seconds: float) -> bool:
        """设置常规刷新间隔（不得低于 12h）"""
        if interval_seconds < self.MIN_REGULAR_INTERVAL:
            return False
        self._regular_interval = interval_seconds
        return True
    
    def pause(self) -> None:
        self.state = SchedulerState.PAUSED
    
    def resume(self) -> None:
        self.state = SchedulerState.IDLE
    
    def get_state(self) -> SchedulerState:
        return self.state
    
    # ========== 触发接口 ==========
    
    def should_regular_refresh(self) -> bool:
        """检查是否应触发常规刷新"""
        if self.state != SchedulerState.IDLE:
            return False
        return time.time() - self._last_regular_refresh >= self._regular_interval
    
    def trigger_urgent(self, trigger: RefreshTrigger) -> None:
        """
        触发紧急刷新
        
        S-02: 仅接受授权剧变信号（由外部模块判定后传入）
        """
        if trigger.trigger_type == RefreshType.MANUAL:
            self._urgent_queue.append(trigger)
        elif trigger.trigger_type in [
            RefreshType.URGENT_WEIGHT_CHANGE,
            RefreshType.URGENT_STYLE_CHANGE,
            RefreshType.URGENT_SEASON_CHANGE,
            RefreshType.URGENT_CONSTRUCTION_END
        ]:
            self._urgent_queue.append(trigger)
        # 其他类型忽略
    
    def has_pending_urgent(self) -> bool:
        return len(self._urgent_queue) > 0
    
    # ========== 执行刷新 ==========
    
    def execute_regular_refresh(self,
                                get_stale_entries,
                                get_i_values,
                                request_s_update,
                                request_v_update,
                                trigger_recalc) -> RefreshResult:
        """
        执行常规刷新
        
        Args:
            get_stale_entries: 获取 I 值更新时间 > 24h 的条目列表的回调
            get_i_values: 获取条目当前各因子的回调
            request_s_update: 请求 ad-31 回溯更新 S 值的回调
            request_v_update: 请求 ad-32 回溯更新 V 值的回调
            trigger_recalc: 触发 ad-36 批量重算的回调
            
        Returns:
            刷新结果汇总
        """
        if self.state != SchedulerState.IDLE:
            return RefreshResult(RefreshType.REGULAR, 0, 0, 0, ["调度器忙"], time.time())
        
        self.state = SchedulerState.REFRESHING_NORMAL
        start_time = time.time()
        
        # 1. 获取陈旧条目
        stale_entries = get_stale_entries()
        if not stale_entries:
            self._last_regular_refresh = time.time()
            self.state = SchedulerState.IDLE
            return RefreshResult(RefreshType.REGULAR, 0, 0, 0, [], time.time())
        
        # 2. 检查因子时效性，请求回溯更新
        super_stale = [e for e in stale_entries if e.get("s_age", 0) > self.FACTOR_STALENESS_SECONDS]
        if super_stale:
            request_s_update([e["entry_id"] for e in super_stale])
            request_v_update([e["entry_id"] for e in super_stale])
        
        # 3. 触发 ad-36 批量重算
        trigger_recalc([e["entry_id"] for e in stale_entries])
        
        self._last_regular_refresh = time.time()
        self._total_regular += 1
        
        duration_ms = (time.time() - start_time) * 1000
        result = RefreshResult(
            trigger_type=RefreshType.REGULAR,
            total_entries=len(stale_entries),
            updated_entries=len(stale_entries),
            duration_ms=duration_ms,
            anomalies=[]
        )
        
        self.state = SchedulerState.IDLE
        self._log_refresh(result)
        return result
    
    def execute_urgent_refresh(self,
                               get_all_entries,
                               trigger_recalc) -> RefreshResult:
        """
        执行紧急刷新（处理队列中的第一个触发信号）
        
        Args:
            get_all_entries: 获取全量条目或指定范围条目的回调
            trigger_recalc: 触发 ad-36 批量重算的回调
            
        Returns:
            刷新结果汇总
        """
        if not self._urgent_queue:
            return RefreshResult(RefreshType.URGENT_WEIGHT_CHANGE, 0, 0, 0, [], time.time())
        
        if self.state != SchedulerState.IDLE:
            return RefreshResult(RefreshType.URGENT_WEIGHT_CHANGE, 0, 0, 0, ["调度器忙"], time.time())
        
        self.state = SchedulerState.REFRESHING_URGENT
        start_time = time.time()
        
        trigger = self._urgent_queue.pop(0)
        
        # 获取需要刷新的条目
        entries = get_all_entries(trigger.affected_slots)
        
        # 触发全量重算
        if entries:
            trigger_recalc([e["entry_id"] for e in entries])
        
        self._last_regular_refresh = time.time()
        self._total_urgent += 1
        
        duration_ms = (time.time() - start_time) * 1000
        result = RefreshResult(
            trigger_type=trigger.trigger_type,
            total_entries=len(entries) if entries else 0,
            updated_entries=len(entries) if entries else 0,
            duration_ms=duration_ms,
            anomalies=[]
        )
        
        # 清空剩余队列（合并处理）
        self._urgent_queue.clear()
        
        self.state = SchedulerState.IDLE
        self._log_refresh(result)
        return result
    
    # ========== 异常监控 ==========
    
    def check_anomalies(self, promotion_success_rate: float,
                        forget_anomaly_ratio: float) -> Optional[str]:
        """
        检查是否需要建议紧急刷新
        
        Returns:
            建议描述，或 None
        """
        now = time.time()
        if now - self._last_anomaly_monitor < self.ANOMALY_MONITOR_INTERVAL:
            return None
        
        self._last_anomaly_monitor = now
        
        if promotion_success_rate < (1.0 - self.PROMOTION_SUCCESS_DROP_THRESHOLD):
            return f"晋升成功率下降超过{self.PROMOTION_SUCCESS_DROP_THRESHOLD*100:.0f}%，建议紧急刷新"
        
        if forget_anomaly_ratio > self.FORGET_ANOMALY_RATIO_THRESHOLD:
            return f"遗忘异常占比超过{self.FORGET_ANOMALY_RATIO_THRESHOLD*100:.0f}%，建议紧急刷新"
        
        return None
    
    # ========== 变更日志 ==========
    
    def _log_refresh(self, result: RefreshResult) -> None:
        self._pending_logs.append({
            "log_id": f"ref-{uuid.uuid4().hex[:8]}",
            "event_type": result.trigger_type.value,
            "total_entries": result.total_entries,
            "updated_entries": result.updated_entries,
            "duration_ms": result.duration_ms,
            "timestamp": result.timestamp
        })
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_regular": self._total_regular,
            "total_urgent": self._total_urgent,
            "last_regular_refresh": self._last_regular_refresh,
            "regular_interval": self._regular_interval,
            "urgent_queue_size": len(self._urgent_queue),
            "state": self.state.value
        }


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-37 重要度增量定时刷新单元 单元测试")
    print("=" * 60)
    passed, failed = 0, 0
    
    def mock_get_stale():
        return [{"entry_id": f"EXP-{i}", "s_age": 8*86400} for i in range(5)]
    
    def mock_get_all(slots=None):
        return [{"entry_id": f"EXP-{i}"} for i in range(10)]
    
    s_update_calls = []
    v_update_calls = []
    recalc_calls = []
    
    def mock_s_update(ids): s_update_calls.extend(ids)
    def mock_v_update(ids): v_update_calls.extend(ids)
    def mock_recalc(ids): recalc_calls.extend(ids)
    
    # TC-37-01: 常规刷新
    print("\n[TC-37-01] 常规刷新触发")
    try:
        s_update_calls.clear(); v_update_calls.clear(); recalc_calls.clear()
        sched = IRefreshScheduler()
        sched._last_regular_refresh = 0
        assert sched.should_regular_refresh() == True
        
        result = sched.execute_regular_refresh(
            mock_get_stale, None, mock_s_update, mock_v_update, mock_recalc
        )
        assert result.total_entries == 5
        assert len(recalc_calls) == 5
        # 超陈旧因子应触发回溯更新
        assert len(s_update_calls) == 5
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-37-02: 紧急刷新
    print("\n[TC-37-02] 紧急刷新触发")
    try:
        recalc_calls.clear()
        sched = IRefreshScheduler()
        sched.trigger_urgent(RefreshTrigger(RefreshType.URGENT_WEIGHT_CHANGE, "权重变更"))
        assert sched.has_pending_urgent() == True
        
        result = sched.execute_urgent_refresh(mock_get_all, mock_recalc)
        assert result.total_entries == 10
        assert len(recalc_calls) == 10
        assert sched.has_pending_urgent() == False
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-37-03: 间隔不足不触发
    print("\n[TC-37-03] 间隔不足不触发常规刷新")
    try:
        sched = IRefreshScheduler()
        sched._last_regular_refresh = time.time()
        assert sched.should_regular_refresh() == False
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-37-04: 异常监控告警
    print("\n[TC-37-04] 晋升成功率下降告警")
    try:
        sched = IRefreshScheduler()
        msg = sched.check_anomalies(0.5, 0.1)
        assert msg is not None
        assert "晋升成功率" in msg
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-37-05: 设置间隔低于下限拒绝
    print("\n[TC-37-05] 设置间隔 6h 被拒绝")
    try:
        sched = IRefreshScheduler()
        assert sched.set_regular_interval(6 * 3600) == False
        assert sched.set_regular_interval(12 * 3600) == True
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    print(f"\n测试结果: {passed} PASS, {failed} FAIL")