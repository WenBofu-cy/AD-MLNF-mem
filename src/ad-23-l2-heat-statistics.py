#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-23
模块名称: L2 近期层热度统计单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 五层记忆层级存储
核心职责: 统计 L2 近期层各经验条目的查询命中频率与最近访问时间，计算热度权重。
          热度值作为条目向 L3 晋升的参考权重之一，与重要度 I 值共同决定晋升优先级。
          识别高频复用的经验，加速其向中期层固化；标记长期未被查询的"冷"条目，
          为其遗忘判定提供辅助依据。

依赖模块: ad-22(L2 近期层存储单元，提供 L2 条目索引与查询日志),
          ad-38(晋升双条件判定单元，接收热度加权后的晋升建议)
被依赖模块: ad-22(接收热度统计结果，影响条目排序),
            ad-40(遗忘阈值判定单元，参考热度数据辅助遗忘判定)

安全约束:
  S-01: 热度统计数据仅用于漏斗二内部晋升参考与遗忘辅助判定，不向漏斗一传输
  S-02: 查询日志缓冲区仅保留 7 天，超期自动清除，不持久化存储
  S-03: 热度加权仅影响晋升优先级，不可替代重要度 I 值作为晋升的主判定依据
  S-04: 冷条目清单仅作为遗忘判定的辅助参考，最终遗忘决策仍由 ad-40 综合多维因素做出
  S-05: 热度统计过程中使用快照数据隔离实时变更，防止并发冲突
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid
from collections import defaultdict


# ==================== 枚举定义 ====================

class HeatLevel(Enum):
    """热度等级"""
    HOT = "热"       # H ≥ 0.7
    WARM = "温"      # 0.3 ≤ H < 0.7
    COOL = "凉"      # 0.1 ≤ H < 0.3
    COLD = "冷"      # H < 0.1


class StatisticsState(Enum):
    """热度统计单元内部状态"""
    NORMAL = "normal"
    BATCH_PROCESSING = "batch_processing"
    PAUSED = "paused"


# ==================== 数据结构 ====================

@dataclass
class QueryLogEntry:
    """查询日志条目"""
    entry_id: str
    query_timestamp: float
    source_module: str
    query_result: str        # "hit" / "miss"


@dataclass
class HeatRecord:
    """热度记录"""
    entry_id: str
    heat_value: float
    recent_query_count: int          # 近 72 小时查询次数
    total_query_count: int           # 累计查询次数
    last_query_time: Optional[float] # 最近查询时间
    heat_level: HeatLevel
    assessment_timestamp: float = field(default_factory=time.time)


@dataclass
class ColdEntryInfo:
    """冷条目信息"""
    entry_id: str
    last_query_age_hours: float      # 距上次查询的小时数
    suggestion: str = "建议关注"


@dataclass
class HeatStatisticsReport:
    """热度统计报告"""
    report_id: str
    period_start: float
    period_end: float
    total_queries: int
    hot_entries: List[str]           # 热条目 ID 列表
    cold_entries: List[ColdEntryInfo]
    heat_distribution: Dict[HeatLevel, int]
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class L2HeatStatistics:
    """
    L2 近期层热度统计单元
    
    职责:
    1. 接收并累积 L2 条目查询日志
    2. 周期性计算各条目热度值
    3. 按热度等级分类条目
    4. 输出热度排行至 ad-22、ad-38
    5. 输出冷条目清单至 ad-40
    6. 定期生成热度统计报告
    """
    
    # 热度计算公式权重
    WEIGHT_RECENT_FREQ = 0.5     # 近期查询频率权重
    WEIGHT_CUMULATIVE = 0.3      # 累计查询次数衰减值权重
    WEIGHT_RECENCY = 0.2         # 最近查询时间衰减因子权重
    
    # 热度等级阈值
    HOT_THRESHOLD = 0.7
    WARM_THRESHOLD = 0.3
    COOL_THRESHOLD = 0.1
    
    # 热条目 I 值加权
    HOT_I_BOOST = 0.05
    
    # 统计间隔（秒）
    STATISTICS_INTERVAL = 60          # 60 秒
    REPORT_INTERVAL = 30 * 60         # 30 分钟
    
    # 批量处理阈值
    BATCH_THRESHOLD = 500
    
    # 查询日志保留时间（秒）
    LOG_RETENTION_SECONDS = 7 * 24 * 3600  # 7 天
    
    # 日志缓冲区上限
    MAX_LOG_BUFFER = 100000
    
    def __init__(self):
        self.module_id = "ad-23"
        self.module_name = "L2 近期层热度统计单元"
        
        # 内部状态
        self.state = StatisticsState.NORMAL
        
        # 查询日志缓冲区
        self._query_log_buffer: List[QueryLogEntry] = []
        
        # 热度记录缓存: entry_id -> HeatRecord
        self._heat_records: Dict[str, HeatRecord] = {}
        
        # 上次统计时间
        self._last_stats_time = 0.0
        self._last_report_time = 0.0
        
        # 统计
        self._total_queries_processed = 0
        self._total_stats_cycles = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] L2 热度统计单元初始化完成")
        print(f"[{self.module_id}] 热度权重: w1={self.WEIGHT_RECENT_FREQ}, "
              f"w2={self.WEIGHT_CUMULATIVE}, w3={self.WEIGHT_RECENCY}")
    
    # ========== 状态管理 ==========
    
    def pause(self) -> None:
        self.state = StatisticsState.PAUSED
    
    def resume(self) -> None:
        self.state = StatisticsState.NORMAL
    
    def get_state(self) -> StatisticsState:
        return self.state
    
    # ========== 查询日志接收 ==========
    
    def receive_query_log(self, log_entry: QueryLogEntry) -> None:
        """
        接收查询日志条目
        
        Args:
            log_entry: 查询日志条目
        """
        if self.state == StatisticsState.PAUSED:
            return
        
        self._query_log_buffer.append(log_entry)
        self._total_queries_processed += 1
        
        # 缓冲区溢出保护
        if len(self._query_log_buffer) > self.MAX_LOG_BUFFER:
            self._clean_oldest_logs()
    
    def receive_query_logs_batch(self, log_entries: List[QueryLogEntry]) -> None:
        """批量接收查询日志"""
        for entry in log_entries:
            self.receive_query_log(entry)
    
    def _clean_oldest_logs(self) -> None:
        """清理最旧的 20% 日志"""
        remove_count = int(len(self._query_log_buffer) * 0.2)
        if remove_count > 0:
            self._query_log_buffer = self._query_log_buffer[remove_count:]
            print(f"[{self.module_id}] 日志缓冲区溢出，清理最旧 {remove_count} 条")
    
    # ========== 热度计算 ==========
    
    def calculate_heat(self, l2_index_snapshot: List[Any]) -> List[HeatRecord]:
        """
        计算 L2 各条目的热度值
        
        Args:
            l2_index_snapshot: L2 条目索引快照
            
        Returns:
            热度记录列表
        """
        if self.state == StatisticsState.PAUSED:
            return []
        
        now = time.time()
        
        # 检查统计间隔
        if now - self._last_stats_time < self.STATISTICS_INTERVAL:
            return []
        
        self._last_stats_time = now
        self._total_stats_cycles += 1
        
        if len(l2_index_snapshot) > self.BATCH_THRESHOLD:
            self.state = StatisticsState.BATCH_PROCESSING
        
        # 清理过期日志
        self._clean_expired_logs(now)
        
        # 按条目 ID 分组统计查询日志
        entry_query_stats = self._aggregate_query_logs(now)
        
        heat_records = []
        cold_entries = []
        
        for idx_entry in l2_index_snapshot:
            entry_id = idx_entry.entry_id
            stats = entry_query_stats.get(entry_id, {
                "recent_count": 0,
                "total_count": 0,
                "last_query_time": None
            })
            
            # 计算近期查询频率（近 72 小时）
            recent_freq = min(stats["recent_count"] / 72.0, 1.0)
            
            # 计算累计查询次数衰减值（半衰点 = 5 次）
            total_count = stats["total_count"]
            cumulative_decay = total_count / (total_count + 5)
            
            # 计算最近查询时间衰减因子
            last_time = stats["last_query_time"]
            if last_time is None:
                recency_factor = 0.0
            elif now - last_time < 6 * 3600:
                recency_factor = 1.0
            elif now - last_time < 24 * 3600:
                recency_factor = 0.8
            elif now - last_time < 72 * 3600:
                recency_factor = 0.4
            else:
                recency_factor = 0.1
            
            # 综合热度值
            heat_value = (self.WEIGHT_RECENT_FREQ * recent_freq +
                          self.WEIGHT_CUMULATIVE * cumulative_decay +
                          self.WEIGHT_RECENCY * recency_factor)
            
            # 热度等级判定
            if heat_value >= self.HOT_THRESHOLD:
                heat_level = HeatLevel.HOT
            elif heat_value >= self.WARM_THRESHOLD:
                heat_level = HeatLevel.WARM
            elif heat_value >= self.COOL_THRESHOLD:
                heat_level = HeatLevel.COOL
            else:
                heat_level = HeatLevel.COLD
            
            record = HeatRecord(
                entry_id=entry_id,
                heat_value=heat_value,
                recent_query_count=stats["recent_count"],
                total_query_count=total_count,
                last_query_time=last_time,
                heat_level=heat_level
            )
            heat_records.append(record)
            self._heat_records[entry_id] = record
            
            # 收集冷条目
            if heat_level == HeatLevel.COLD:
                age_hours = ((now - last_time) / 3600) if last_time else 999.0
                cold_entries.append(ColdEntryInfo(
                    entry_id=entry_id,
                    last_query_age_hours=age_hours
                ))
        
        # 按热度值降序排列
        heat_records.sort(key=lambda x: x.heat_value, reverse=True)
        
        if self.state == StatisticsState.BATCH_PROCESSING:
            self.state = StatisticsState.NORMAL
        
        return heat_records
    
    def _aggregate_query_logs(self, now: float) -> Dict[str, Dict[str, Any]]:
        """汇总查询日志"""
        stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "recent_count": 0,
            "total_count": 0,
            "last_query_time": None
        })
        
        for log in self._query_log_buffer:
            entry_id = log.entry_id
            entry_stats = stats[entry_id]
            entry_stats["total_count"] += 1
            
            # 近 72 小时
            if now - log.query_timestamp <= 72 * 3600:
                entry_stats["recent_count"] += 1
            
            # 更新最近查询时间
            if (entry_stats["last_query_time"] is None or
                    log.query_timestamp > entry_stats["last_query_time"]):
                entry_stats["last_query_time"] = log.query_timestamp
        
        return stats
    
    def _clean_expired_logs(self, now: float) -> None:
        """清理超过 7 天的查询日志"""
        cutoff = now - self.LOG_RETENTION_SECONDS
        original_len = len(self._query_log_buffer)
        self._query_log_buffer = [
            log for log in self._query_log_buffer
            if log.query_timestamp >= cutoff
        ]
        removed = original_len - len(self._query_log_buffer)
        if removed > 0:
            print(f"[{self.module_id}] 清理过期日志: {removed} 条")
    
    # ========== 获取冷条目清单 ==========
    
    def get_cold_entries(self) -> List[ColdEntryInfo]:
        """获取冷条目清单（供 ad-40 参考）"""
        cold = []
        now = time.time()
        for record in self._heat_records.values():
            if record.heat_level == HeatLevel.COLD:
                age = ((now - record.last_query_time) / 3600) if record.last_query_time else 999.0
                cold.append(ColdEntryInfo(
                    entry_id=record.entry_id,
                    last_query_age_hours=age
                ))
        return cold
    
    # ========== 获取热度加权建议 ==========
    
    def get_hot_i_boost_suggestions(self) -> Dict[str, float]:
        """
        获取热条目 I 值加权建议
        
        Returns:
            {entry_id: i_boost, ...}
        """
        suggestions = {}
        for record in self._heat_records.values():
            if record.heat_level == HeatLevel.HOT:
                suggestions[record.entry_id] = self.HOT_I_BOOST
        return suggestions
    
    # ========== 报告生成 ==========
    
    def generate_report(self) -> Optional[HeatStatisticsReport]:
        """生成热度统计报告"""
        now = time.time()
        if now - self._last_report_time < self.REPORT_INTERVAL:
            return None
        
        self._last_report_time = now
        
        distribution = defaultdict(int)
        hot_entries = []
        cold_entries = []
        
        for record in self._heat_records.values():
            distribution[record.heat_level] += 1
            if record.heat_level == HeatLevel.HOT:
                hot_entries.append(record.entry_id)
            elif record.heat_level == HeatLevel.COLD:
                age = ((now - record.last_query_time) / 3600) if record.last_query_time else 999.0
                cold_entries.append(ColdEntryInfo(record.entry_id, age))
        
        report = HeatStatisticsReport(
            report_id=f"heat-report-{uuid.uuid4().hex[:8]}",
            period_start=now - self.REPORT_INTERVAL,
            period_end=now,
            total_queries=self._total_queries_processed,
            hot_entries=hot_entries[:10],
            cold_entries=cold_entries[:10],
            heat_distribution=dict(distribution)
        )
        
        return report
    
    # ========== 查询接口 ==========
    
    def get_heat_record(self, entry_id: str) -> Optional[HeatRecord]:
        return self._heat_records.get(entry_id)
    
    def get_heat_value(self, entry_id: str) -> float:
        record = self._heat_records.get(entry_id)
        return record.heat_value if record else 0.0
    
    def get_buffer_size(self) -> int:
        return len(self._query_log_buffer)
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_queries_processed": self._total_queries_processed,
            "total_stats_cycles": self._total_stats_cycles,
            "buffer_size": len(self._query_log_buffer),
            "tracked_entries": len(self._heat_records),
            "state": self.state.value
        }
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-23 L2 近期层热度统计单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    class MockIndexEntry:
        def __init__(self, entry_id):
            self.entry_id = entry_id
    
    # --- TC-23-01: 热条目（高频查询） ---
    print("\n[TC-23-01] 热条目（高频查询）")
    try:
        stats = L2HeatStatistics()
        # 模拟 5 次近 72 小时查询，最近 2 小时
        now = time.time()
        for i in range(5):
            stats.receive_query_log(QueryLogEntry(
                "EXP-A", now - 3600 * i, "ECC-03", "hit"
            ))
        # 还需累计次数达到半衰点以上
        for i in range(10):
            stats.receive_query_log(QueryLogEntry(
                "EXP-A", now - 100 * 3600, "ECC-03", "hit"
            ))
        
        idx = [MockIndexEntry("EXP-A")]
        records = stats.calculate_heat(idx)
        assert len(records) == 1
        assert records[0].heat_level == HeatLevel.HOT
        assert records[0].heat_value >= stats.HOT_THRESHOLD
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-23-02: 冷条目（从未查询） ---
    print("\n[TC-23-02] 冷条目（从未查询）")
    try:
        stats = L2HeatStatistics()
        idx = [MockIndexEntry("EXP-B")]
        records = stats.calculate_heat(idx)
        assert len(records) == 1
        assert records[0].heat_level == HeatLevel.COLD
        assert records[0].heat_value == 0.0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-23-03: 暖条目 ---
    print("\n[TC-23-03] 暖条目")
    try:
        stats = L2HeatStatistics()
        now = time.time()
        # 1 次近期查询，累计 5 次
        stats.receive_query_log(QueryLogEntry("EXP-C", now - 10 * 3600, "ECC-03", "hit"))
        for i in range(4):
            stats.receive_query_log(QueryLogEntry("EXP-C", now - 100 * 3600, "ECC-03", "hit"))
        idx = [MockIndexEntry("EXP-C")]
        records = stats.calculate_heat(idx)
        assert records[0].heat_level in [HeatLevel.WARM, HeatLevel.HOT]
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-23-04: 热度排行降序 ---
    print("\n[TC-23-04] 热度排行降序")
    try:
        stats = L2HeatStatistics()
        now = time.time()
        # EXP-1 更多查询
        for i in range(10):
            stats.receive_query_log(QueryLogEntry("EXP-1", now - i * 3600, "ECC-03", "hit"))
        # EXP-2 较少查询
        stats.receive_query_log(QueryLogEntry("EXP-2", now - 50 * 3600, "ECC-03", "hit"))
        
        idx = [MockIndexEntry("EXP-1"), MockIndexEntry("EXP-2")]
        records = stats.calculate_heat(idx)
        assert records[0].heat_value >= records[1].heat_value
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-23-05: 热条目 I 值加权建议 ---
    print("\n[TC-23-05] 热条目 I 值加权建议")
    try:
        stats = L2HeatStatistics()
        now = time.time()
        for i in range(10):
            stats.receive_query_log(QueryLogEntry("EXP-D", now - i * 3600, "ECC-03", "hit"))
        idx = [MockIndexEntry("EXP-D")]
        stats.calculate_heat(idx)
        boosts = stats.get_hot_i_boost_suggestions()
        assert "EXP-D" in boosts
        assert boosts["EXP-D"] == stats.HOT_I_BOOST
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-23-06: 冷条目清单 ---
    print("\n[TC-23-06] 冷条目清单")
    try:
        stats = L2HeatStatistics()
        idx = [MockIndexEntry("EXP-E")]
        stats.calculate_heat(idx)
        cold = stats.get_cold_entries()
        assert len(cold) == 1
        assert cold[0].entry_id == "EXP-E"
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-23-07: 日志缓冲区溢出保护 ---
    print("\n[TC-23-07] 日志缓冲区溢出保护")
    try:
        stats = L2HeatStatistics()
        stats.MAX_LOG_BUFFER = 10
        now = time.time()
        for i in range(15):
            stats.receive_query_log(QueryLogEntry(f"EXP-{i}", now, "ECC-03", "hit"))
        assert stats.get_buffer_size() <= 10
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-23-08: 过期日志清理 ---
    print("\n[TC-23-08] 过期日志清理（7 天前）")
    try:
        stats = L2HeatStatistics()
        now = time.time()
        old_time = now - 8 * 24 * 3600
        stats.receive_query_log(QueryLogEntry("EXP-F", old_time, "ECC-03", "hit"))
        stats.receive_query_log(QueryLogEntry("EXP-G", now, "ECC-03", "hit"))
        # 触发热度计算时清理
        idx = [MockIndexEntry("EXP-F"), MockIndexEntry("EXP-G")]
        stats.calculate_heat(idx)
        assert stats.get_buffer_size() == 1  # 旧日志被清理
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-23-09: 暂停状态不处理 ---
    print("\n[TC-23-09] 暂停状态不处理")
    try:
        stats = L2HeatStatistics()
        stats.pause()
        stats.receive_query_log(QueryLogEntry("EXP-H", time.time(), "ECC-03", "hit"))
        assert stats.get_buffer_size() == 0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-23-10: 热度统计报告 ---
    print("\n[TC-23-10] 热度统计报告生成")
    try:
        stats = L2HeatStatistics()
        now = time.time()
        for i in range(10):
            stats.receive_query_log(QueryLogEntry(f"EXP-{i}", now - i * 3600, "ECC-03", "hit"))
        idx = [MockIndexEntry(f"EXP-{i}") for i in range(10)]
        stats.calculate_heat(idx)
        stats._last_report_time = 0
        report = stats.generate_report()
        assert report is not None
        assert report.total_queries == 10
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