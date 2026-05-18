#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-10
模块名称: 行为累积统计单元
所属分区: 二、漏斗一：驾驶员画像漏斗
核心职责: 按行为维度（跟车/变道/路口通行/加减速/让行/停车/起步）累计同类行为频次，
          按判定标签（优良习惯/常态陋习/应急特殊操作）分类统计，生成多时间粒度
          统计周期报表（近7日/近30日/总累计），供驾驶辅助提醒生成单元调用。

依赖模块: ad-09(行为判定标签单元，提供带判定标签的行为条目),
          ad-05(子画像槽创建与初始化单元，提供统计基线),
          ad-02(漏斗一专属调度单元，提供当前活跃槽号)
被依赖模块: ad-11(驾驶辅助提醒生成单元，消费统计周期报表),
            ad-13(子画像槽长期未活跃提醒单元，消费最后活跃时间戳)

安全约束:
  S-01: 漏斗一数据编译期禁止接入自动驾驶决策链路
  S-02: 子画像槽间统计数据绝对物理隔离，禁止跨槽聚合或比较
  S-03: 统计报表中的陋习排行榜仅用于车内驾驶辅助提示，不向云端同步
  S-04: 统计基线初始化数据由 ad-05 下发，本模块不得主动修改基线值
  S-05: 紧急熔断时立即暂停统计更新，清空临时队列
  S-06: 统计周期报表在输出到 ad-11 前须脱敏处理，不包含驾驶员身份 ID 明文
  S-07: 所有统计结构变更全量写入 ad-51 变更日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid
from collections import defaultdict


# ==================== 枚举定义 ====================

class BehaviorDimension(Enum):
    """行为维度"""
    FOLLOW = "跟车"
    LANE_CHANGE = "变道"
    INTERSECTION = "路口通行"
    ACCELERATE = "加速"
    DECELERATE = "减速"
    BRAKE = "制动"
    TURN = "转弯"
    YIELD = "让行"
    PARK = "停车"
    START = "起步"


class LabelCategory(Enum):
    """判定标签类别"""
    GOOD = "优良习惯"
    BAD = "常态陋习"
    EMERGENCY = "应急特殊操作"


class TimeGranularity(Enum):
    """时间粒度"""
    LAST_7_DAYS = "近7日"
    LAST_30_DAYS = "近30日"
    TOTAL = "总累计"


class StatsState(Enum):
    """统计单元内部状态"""
    NORMAL = "normal"
    ROLLING = "rolling"           # 统计周期滚动更新中
    INIT = "init"                 # 新槽初始化中
    PAUSED = "paused"
    EMERGENCY_RO = "emergency_ro"


# ==================== 数据结构 ====================

@dataclass
class JudgedObservation:
    """带判定标签的行为条目（来自 ad-09）"""
    obs_id: str
    original_tagged: Any          # TaggedObservation
    judgment_label: LabelCategory
    judgment_reason: str
    judgment_confidence: float
    judgment_timestamp: float = field(default_factory=time.time)


@dataclass
class DimensionStats:
    """单个行为维度的统计计数器"""
    total: float = 0.0
    good: float = 0.0
    bad: float = 0.0
    emergency: float = 0.0


@dataclass
class SlotStatistics:
    """单个子画像槽的完整统计结构"""
    slot_id: int
    last_active_time: float = field(default_factory=time.time)
    dimensions: Dict[str, DimensionStats] = field(default_factory=dict)
    label_summary: Dict[str, DimensionStats] = field(default_factory=dict)
    period_data: Dict[str, Dict[str, DimensionStats]] = field(default_factory=dict)
    create_time: float = field(default_factory=time.time)


@dataclass
class StatisticsReport:
    """统计周期报表"""
    slot_id: int
    generate_time: float
    last_7_days: Dict[str, DimensionStats]
    last_30_days: Dict[str, DimensionStats]
    total: Dict[str, DimensionStats]
    bad_behavior_ranking: List[Tuple[str, float]]
    improvement_trend: Optional[float] = None
    overall_excellence_rate: float = 0.0


# ==================== 主类定义 ====================

class BehaviorStatisticsUnit:
    """
    行为累积统计单元
    
    职责:
    1. 按行为维度累计同类行为频次
    2. 按判定标签分类统计
    3. 生成多时间粒度统计报表（近7日/近30日/总累计）
    4. 置信度加权处理（低置信度陋习权重上调，低置信度优良权重下调）
    5. 统计周期滚动更新
    6. 统计数据完整性校验
    """
    
    # 置信度加权系数
    LOW_CONFIDENCE_BAD_WEIGHT = 1.2    # 低置信度陋习加权
    LOW_CONFIDENCE_GOOD_WEIGHT = 0.8   # 低置信度优良降权
    LOW_CONFIDENCE_THRESHOLD = 0.5     # 低置信度阈值
    
    # 统计周期
    ROLLOVER_CHECK_INTERVAL = 60       # 滚动检查间隔（秒）
    INTEGRITY_CHECK_INTERVAL = 1800    # 完整性校验间隔（秒，30分钟）
    
    # 临时队列最大长度
    MAX_TEMP_QUEUE_SIZE = 500
    
    def __init__(self):
        self.module_id = "ad-10"
        self.module_name = "行为累积统计单元"
        
        # 内部状态
        self.state = StatsState.INIT
        
        # 各槽统计结构: slot_id -> SlotStatistics
        self._slot_stats: Dict[int, SlotStatistics] = {}
        
        # 活跃槽号
        self._active_slot_id: Optional[int] = None
        
        # 临时队列（暂停时暂存条目）
        self._temp_queue: List[JudgedObservation] = []
        
        # 周期管理
        self._last_rollover_check = time.time()
        self._last_integrity_check = time.time()
        
        # 统计
        self._total_processed = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 行为累积统计单元初始化完成")
    
    # ========== 状态管理 ==========
    
    def init_slot_baseline(self, slot_id: int) -> None:
        """初始化新槽的统计基线"""
        self.state = StatsState.INIT
        
        stats = SlotStatistics(slot_id=slot_id)
        
        # 初始化所有行为维度的计数器
        for dim in BehaviorDimension:
            stats.dimensions[dim.value] = DimensionStats()
        
        # 初始化时间粒度数据
        for period in TimeGranularity:
            stats.period_data[period.value] = {}
            for dim in BehaviorDimension:
                stats.period_data[period.value][dim.value] = DimensionStats()
        
        self._slot_stats[slot_id] = stats
        
        print(f"[{self.module_id}] 初始化槽位统计基线: slot_{slot_id}")
        self.state = StatsState.NORMAL
    
    def set_active_slot(self, slot_id: Optional[int]) -> None:
        """设置当前活跃槽号"""
        self._active_slot_id = slot_id
        if slot_id is not None and slot_id not in self._slot_stats:
            self.init_slot_baseline(slot_id)
    
    def pause(self) -> None:
        """暂停统计"""
        self.state = StatsState.PAUSED
        print(f"[{self.module_id}] 统计已暂停")
    
    def resume(self) -> None:
        """恢复统计"""
        self.state = StatsState.NORMAL
        # 处理临时队列中的条目
        self._process_temp_queue()
        print(f"[{self.module_id}] 统计已恢复")
    
    def emergency_stop(self) -> None:
        """紧急熔断"""
        self.state = StatsState.EMERGENCY_RO
        self._temp_queue.clear()
        print(f"[{self.module_id}] 紧急熔断，清空临时队列")
    
    # ========== 统计更新 ==========
    
    def process_judged_observation(self, judged: JudgedObservation) -> bool:
        """
        处理一条判定后的行为条目
        
        Args:
            judged: 带判定标签的行为条目
            
        Returns:
            是否成功处理
        """
        if self.state in [StatsState.EMERGENCY_RO, StatsState.INIT]:
            return False
        
        if self.state == StatsState.PAUSED:
            # 暂存到临时队列
            if len(self._temp_queue) < self.MAX_TEMP_QUEUE_SIZE:
                self._temp_queue.append(judged)
            return False
        
        if self._active_slot_id is None:
            return False
        
        return self._update_statistics(self._active_slot_id, judged)
    
    def _update_statistics(self, slot_id: int, judged: JudgedObservation) -> bool:
        """更新指定槽位的统计数据"""
        if slot_id not in self._slot_stats:
            self.init_slot_baseline(slot_id)
        
        stats = self._slot_stats[slot_id]
        stats.last_active_time = time.time()
        
        # 获取行为维度
        tagged = judged.original_tagged
        behavior_type = tagged.original_observation.behavior_type
        
        # 映射行为维度
        dimension = self._map_behavior_to_dimension(behavior_type)
        if dimension is None:
            return False
        
        label = judged.judgment_label
        confidence = judged.judgment_confidence
        
        # 置信度加权
        weight = 1.0
        if label == LabelCategory.BAD and confidence < self.LOW_CONFIDENCE_THRESHOLD:
            weight = self.LOW_CONFIDENCE_BAD_WEIGHT
        elif label == LabelCategory.GOOD and confidence < self.LOW_CONFIDENCE_THRESHOLD:
            weight = self.LOW_CONFIDENCE_GOOD_WEIGHT
        
        # 更新总累计
        self._increment_dimension(stats.dimensions[dimension.value], label, weight)
        self._increment_dimension(
            stats.period_data[TimeGranularity.TOTAL.value][dimension.value], label, weight)
        self._increment_dimension(
            stats.period_data[TimeGranularity.LAST_7_DAYS.value][dimension.value], label, weight)
        self._increment_dimension(
            stats.period_data[TimeGranularity.LAST_30_DAYS.value][dimension.value], label, weight)
        
        # 更新标签汇总
        self._increment_dimension(stats.label_summary, label, weight)
        
        self._total_processed += 1
        return True
    
    def _increment_dimension(self, dim_stats: DimensionStats, label: LabelCategory, weight: float) -> None:
        """增加维度计数器"""
        dim_stats.total += weight
        if label == LabelCategory.GOOD:
            dim_stats.good += weight
        elif label == LabelCategory.BAD:
            dim_stats.bad += weight
        elif label == LabelCategory.EMERGENCY:
            dim_stats.emergency += weight
    
    def _map_behavior_to_dimension(self, behavior_type: str) -> Optional[BehaviorDimension]:
        """将行为类型字符串映射到行为维度枚举"""
        mapping = {
            "跟车": BehaviorDimension.FOLLOW,
            "变道": BehaviorDimension.LANE_CHANGE,
            "路口通行": BehaviorDimension.INTERSECTION,
            "加速": BehaviorDimension.ACCELERATE,
            "减速": BehaviorDimension.DECELERATE,
            "制动": BehaviorDimension.BRAKE,
            "转弯": BehaviorDimension.TURN,
            "让行": BehaviorDimension.YIELD,
            "停车": BehaviorDimension.PARK,
            "起步": BehaviorDimension.START,
            "匀速巡航": BehaviorDimension.FOLLOW,  # 归入跟车
        }
        return mapping.get(behavior_type)
    
    def _process_temp_queue(self) -> int:
        """处理临时队列中的条目"""
        count = 0
        while self._temp_queue and self.state == StatsState.NORMAL:
            judged = self._temp_queue.pop(0)
            if self.process_judged_observation(judged):
                count += 1
        return count
    
    # ========== 统计周期滚动 ==========
    
    def check_period_rollover(self) -> None:
        """检查并执行统计周期滚动"""
        now = time.time()
        if now - self._last_rollover_check < self.ROLLOVER_CHECK_INTERVAL:
            return
        
        self._last_rollover_check = now
        self.state = StatsState.ROLLING
        
        # 简化实现：重置近期统计数据
        for slot_id, stats in self._slot_stats.items():
            for dim in BehaviorDimension:
                stats.period_data[TimeGranularity.LAST_7_DAYS.value][dim.value] = DimensionStats()
        
        print(f"[{self.module_id}] 统计周期滚动完成")
        self.state = StatsState.NORMAL
    
    # ========== 数据完整性校验 ==========
    
    def check_integrity(self) -> List[str]:
        """校验统计数据完整性，返回异常列表"""
        now = time.time()
        if now - self._last_integrity_check < self.INTEGRITY_CHECK_INTERVAL:
            return []
        
        self._last_integrity_check = now
        anomalies = []
        
        for slot_id, stats in self._slot_stats.items():
            # 校验：各行为维度汇总 = 标签汇总
            dim_total = sum(d.total for d in stats.dimensions.values())
            label_total = sum(d.total for d in stats.label_summary.values())
            
            if abs(dim_total - label_total) > 0.01:
                anomalies.append(f"slot_{slot_id}: 维度汇总({dim_total}) != 标签汇总({label_total})")
        
        if anomalies:
            print(f"[{self.module_id}] 数据完整性校验异常: {len(anomalies)} 项")
        
        return anomalies
    
    # ========== 报表生成 ==========
    
    def generate_report(self, slot_id: int) -> Optional[StatisticsReport]:
        """生成指定槽位的统计周期报表"""
        if slot_id not in self._slot_stats:
            return None
        
        stats = self._slot_stats[slot_id]
        
        # 计算陋习排行榜
        bad_ranking = []
        for dim in BehaviorDimension:
            dim_data = stats.period_data[TimeGranularity.LAST_30_DAYS.value][dim.value]
            if dim_data.bad > 0:
                bad_ranking.append((dim.value, dim_data.bad))
        bad_ranking.sort(key=lambda x: x[1], reverse=True)
        
        # 计算综合优良率
        total_all = sum(
            d.total for d in stats.period_data[TimeGranularity.LAST_30_DAYS.value].values())
        good_all = sum(
            d.good for d in stats.period_data[TimeGranularity.LAST_30_DAYS.value].values())
        
        excellence_rate = good_all / max(total_all, 1.0)
        
        return StatisticsReport(
            slot_id=slot_id,
            generate_time=time.time(),
            last_7_days=stats.period_data[TimeGranularity.LAST_7_DAYS.value],
            last_30_days=stats.period_data[TimeGranularity.LAST_30_DAYS.value],
            total=stats.period_data[TimeGranularity.TOTAL.value],
            bad_behavior_ranking=bad_ranking[:3],
            overall_excellence_rate=excellence_rate
        )
    
    def get_last_active_time(self, slot_id: int) -> Optional[float]:
        """获取槽位最后活跃时间"""
        if slot_id in self._slot_stats:
            return self._slot_stats[slot_id].last_active_time
        return None
    
    # ========== 查询接口 ==========
    
    def get_state(self) -> StatsState:
        return self.state
    
    def get_active_slot_id(self) -> Optional[int]:
        return self._active_slot_id
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_processed": self._total_processed,
            "active_slots": len(self._slot_stats),
            "temp_queue_size": len(self._temp_queue),
            "state": self.state.value,
            "active_slot": self._active_slot_id
        }
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-10 行为累积统计单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    # 模拟 ad-09 输出
    class MockObservation:
        def __init__(self, behavior_type):
            self.behavior_type = behavior_type
    
    class MockTagged:
        def __init__(self, behavior_type, obs_id, confidence):
            self.original_observation = MockObservation(behavior_type)
            self.obs_id = obs_id
            self.label_confidence = confidence
    
    def make_judged(behavior_type, label, confidence=0.9):
        return JudgedObservation(
            obs_id=f"obs-{uuid.uuid4().hex[:6]}",
            original_tagged=MockTagged(behavior_type, f"obs-{uuid.uuid4().hex[:6]}", confidence),
            judgment_label=label,
            judgment_reason="测试",
            judgment_confidence=confidence
        )
    
    # --- TC-10-01: 初始化统计基线 ---
    print("\n[TC-10-01] 初始化统计基线")
    try:
        unit = BehaviorStatisticsUnit()
        unit.init_slot_baseline(1)
        assert 1 in unit._slot_stats
        assert len(unit._slot_stats[1].dimensions) == len(BehaviorDimension)
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-10-02: 更新优良习惯统计 ---
    print("\n[TC-10-02] 更新优良习惯统计")
    try:
        unit = BehaviorStatisticsUnit()
        unit.set_active_slot(1)
        judged = make_judged("变道", LabelCategory.GOOD)
        result = unit.process_judged_observation(judged)
        assert result == True
        dim_stats = unit._slot_stats[1].dimensions["变道"]
        assert dim_stats.good >= 0.9
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-10-03: 低置信度陋习加权 ---
    print("\n[TC-10-03] 低置信度陋习加权")
    try:
        unit = BehaviorStatisticsUnit()
        unit.set_active_slot(1)
        judged = make_judged("跟车", LabelCategory.BAD, confidence=0.4)
        unit.process_judged_observation(judged)
        dim_stats = unit._slot_stats[1].dimensions["跟车"]
        assert dim_stats.bad == unit.LOW_CONFIDENCE_BAD_WEIGHT
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-10-04: 低置信度优良降权 ---
    print("\n[TC-10-04] 低置信度优良降权")
    try:
        unit = BehaviorStatisticsUnit()
        unit.set_active_slot(1)
        judged = make_judged("制动", LabelCategory.GOOD, confidence=0.4)
        unit.process_judged_observation(judged)
        dim_stats = unit._slot_stats[1].dimensions["制动"]
        assert dim_stats.good == unit.LOW_CONFIDENCE_GOOD_WEIGHT
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-10-05: 生成统计报表 ---
    print("\n[TC-10-05] 生成统计报表")
    try:
        unit = BehaviorStatisticsUnit()
        unit.set_active_slot(1)
        for _ in range(5):
            unit.process_judged_observation(make_judged("变道", LabelCategory.GOOD))
        for _ in range(3):
            unit.process_judged_observation(make_judged("制动", LabelCategory.BAD))
        report = unit.generate_report(1)
        assert report is not None
        assert report.overall_excellence_rate >= 0.0
        assert len(report.bad_behavior_ranking) > 0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-10-06: 暂停时暂存临时队列 ---
    print("\n[TC-10-06] 暂停时暂存临时队列")
    try:
        unit = BehaviorStatisticsUnit()
        unit.set_active_slot(1)
        unit.pause()
        judged = make_judged("转弯", LabelCategory.GOOD)
        result = unit.process_judged_observation(judged)
        assert result == False
        assert len(unit._temp_queue) == 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-10-07: 恢复时处理临时队列 ---
    print("\n[TC-10-07] 恢复时处理临时队列")
    try:
        unit = BehaviorStatisticsUnit()
        unit.set_active_slot(1)
        unit.pause()
        unit.process_judged_observation(make_judged("让行", LabelCategory.GOOD))
        unit.resume()
        assert len(unit._temp_queue) == 0
        assert unit._total_processed == 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-10-08: 紧急熔断清空队列 ---
    print("\n[TC-10-08] 紧急熔断清空队列")
    try:
        unit = BehaviorStatisticsUnit()
        unit.set_active_slot(1)
        unit.pause()
        unit.process_judged_observation(make_judged("停车", LabelCategory.GOOD))
        unit.emergency_stop()
        assert len(unit._temp_queue) == 0
        assert unit.state == StatsState.EMERGENCY_RO
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