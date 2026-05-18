#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-33
模块名称: 复用频次 C 值统计单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 三维重要度计算引擎
核心职责: 统计每条驾驶经验条目在同类场景下被成功查询调用的标准化次数，计算复用频次
          分值 C（0–1）。C 值代表该经验的实战验证程度，复用次数越高，经验越成熟可靠，
          重要度权重越应得到强化。

依赖模块: ad-36(综合重要度 I 值聚合计算单元，接收 C 值),
          各场景分槽及 ECC 模块(提供经验查询命中日志)
被依赖模块: ad-36(消费 C 值参与 I 值计算), ad-37(重要度增量定时刷新单元，触发周期更新)

C 值计算模型:
  核心公式: C = 累计复用次数 / (累计复用次数 + K)，其中 K = 5（半衰常数）
  衰减变体: C_decay = C × 时间衰减因子（仅供遗忘判定参考）

有效复用条件: 经验被 ECC 查询后，该经验提供的策略被 ECC 实际采用并执行成功，
              结果标签为"成功优化"或"不可抗力场景"。

安全约束:
  S-01: 复用频次计数必须基于"采用成功"，仅查询未采用的经验不得计数
  S-02: C 值仅作为经验成熟度的辅助指标，不可替代安全显著性 S 值
  S-03: 冷条目清单仅供遗忘判定的辅助参考，最终遗忘决策仍由 ad-40 综合多维因素做出
  S-04: C 值衰减变体 C_decay 仅用于遗忘辅助，不参与正式 I 值计算
  S-05: 查询日志中的来源模块与场景信息不可包含驾驶员身份数据
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class AdoptResult(Enum):
    """采用结果"""
    SUCCESS_OPTIMIZED = "成功优化"
    FORCE_MAJEURE = "不可抗力场景"
    STRATEGY_MISTAKE = "策略失误"
    NOT_ADOPTED = "未采用"


class StatsState(Enum):
    """统计单元内部状态"""
    NORMAL = "normal"
    BATCH_REFRESH = "batch_refresh"
    PAUSED = "paused"


# ==================== 数据结构 ====================

@dataclass
class QueryHitLog:
    """经验查询命中日志"""
    entry_id: str
    query_timestamp: float
    source_module: str
    scene_label: str
    adopt_result: AdoptResult
    log_id: str = field(default_factory=lambda: f"hitlog-{uuid.uuid4().hex[:8]}")


@dataclass
class CValueCache:
    """C 值缓存条目"""
    entry_id: str
    cumulative_adopt_count: int = 0    # 累计成功采用次数
    last_adopt_time: Optional[float] = None  # 最近成功采用时间
    c_value: float = 0.0               # 标准 C 值
    c_decay: float = 0.0               # 时间衰减 C 值（仅供遗忘参考）
    last_updated: float = field(default_factory=time.time)


@dataclass
class CValueUpdate:
    """C 值更新结果"""
    entry_id: str
    c_value: float
    cumulative_adopt_count: int
    last_adopt_time: Optional[float]
    update_timestamp: float = field(default_factory=time.time)


@dataclass
class ColdEntryInfo:
    """冷条目信息"""
    entry_id: str
    last_adopt_age_days: float         # 距上次成功采用的天数
    c_value: float
    c_decay: float


# ==================== 主类定义 ====================

class CValueStatistics:
    """
    复用频次 C 值统计单元
    
    职责:
    1. 接收经验查询命中日志
    2. 判定有效复用（须为成功采用）
    3. 实时更新各条目的累计复用次数与 C 值
    4. 周期性批量刷新 C 值
    5. 识别并输出冷条目清单（供遗忘判定参考）
    """
    
    # C 值计算半衰常数
    HALF_LIFE_K = 5
    
    # 时间衰减因子阈值（天）
    DECAY_THRESHOLD_RECENT = 30       # 最近 30 日：衰减因子 1.0
    DECAY_THRESHOLD_MEDIUM = 90       # 30–90 日：衰减因子 0.8
    DECAY_THRESHOLD_OLD = 180         # 90–180 日：衰减因子 0.5
                                        # > 180 日：衰减因子 0.2
    
    # 冷条目判定阈值（天）
    COLD_ENTRY_THRESHOLD_DAYS = 90
    
    # 批量刷新间隔（秒）
    BATCH_REFRESH_INTERVAL = 24 * 3600  # 24 小时
    
    def __init__(self):
        self.module_id = "ad-33"
        self.module_name = "复用频次 C 值统计单元"
        
        # 内部状态
        self.state = StatsState.NORMAL
        
        # C 值缓存: entry_id -> CValueCache
        self._cache: Dict[str, CValueCache] = {}
        
        # 上次批量刷新时间
        self._last_batch_refresh_time = 0.0
        
        # 统计
        self._total_logs_processed = 0
        self._total_valid_adopts = 0
        self._total_rejected = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] C 值统计单元初始化完成")
        print(f"[{self.module_id}] 半衰常数 K={self.HALF_LIFE_K}")
    
    # ========== 状态管理 ==========
    
    def pause(self) -> None:
        self.state = StatsState.PAUSED
    
    def resume(self) -> None:
        self.state = StatsState.NORMAL
    
    def get_state(self) -> StatsState:
        return self.state
    
    # ========== 查询日志处理 ==========
    
    def process_query_log(self, log: QueryHitLog) -> Optional[CValueUpdate]:
        """
        处理一条查询命中日志
        
        S-01: 仅当 adopt_result 为"成功优化"或"不可抗力场景"时计入复用
        
        Args:
            log: 查询命中日志
            
        Returns:
            C 值更新结果，或 None（未计入）
        """
        if self.state == StatsState.PAUSED:
            return None
        
        self._total_logs_processed += 1
        
        # S-01: 仅成功采用才计数
        if log.adopt_result not in [AdoptResult.SUCCESS_OPTIMIZED, AdoptResult.FORCE_MAJEURE]:
            self._total_rejected += 1
            return None
        
        self._total_valid_adopts += 1
        
        # 获取或创建缓存条目
        if log.entry_id not in self._cache:
            self._cache[log.entry_id] = CValueCache(entry_id=log.entry_id)
        
        cache_entry = self._cache[log.entry_id]
        
        # 更新累计采用次数
        cache_entry.cumulative_adopt_count += 1
        cache_entry.last_adopt_time = log.query_timestamp
        cache_entry.last_updated = time.time()
        
        # 重新计算 C 值
        count = cache_entry.cumulative_adopt_count
        cache_entry.c_value = count / (count + self.HALF_LIFE_K)
        
        # 计算时间衰减 C 值
        cache_entry.c_decay = self._calc_decay_c(cache_entry)
        
        update = CValueUpdate(
            entry_id=log.entry_id,
            c_value=cache_entry.c_value,
            cumulative_adopt_count=count,
            last_adopt_time=cache_entry.last_adopt_time
        )
        
        return update
    
    def process_query_logs_batch(self, logs: List[QueryHitLog]) -> List[CValueUpdate]:
        """批量处理查询日志"""
        updates = []
        for log in logs:
            update = self.process_query_log(log)
            if update:
                updates.append(update)
        return updates
    
    # ========== 批量刷新 ==========
    
    def batch_refresh(self, force: bool = False) -> Tuple[List[CValueUpdate], List[ColdEntryInfo]]:
        """
        周期性批量刷新所有条目的 C 值
        
        Args:
            force: 是否强制执行（忽略时间间隔）
            
        Returns:
            (C 值更新列表, 冷条目清单)
        """
        now = time.time()
        
        if not force:
            if now - self._last_batch_refresh_time < self.BATCH_REFRESH_INTERVAL:
                return [], []
        
        self._last_batch_refresh_time = now
        self.state = StatsState.BATCH_REFRESH
        
        updates = []
        cold_entries = []
        
        for entry_id, cache_entry in self._cache.items():
            # 重新计算 C 值（确保一致性）
            count = cache_entry.cumulative_adopt_count
            cache_entry.c_value = count / (count + self.HALF_LIFE_K)
            cache_entry.c_decay = self._calc_decay_c(cache_entry)
            cache_entry.last_updated = now
            
            updates.append(CValueUpdate(
                entry_id=entry_id,
                c_value=cache_entry.c_value,
                cumulative_adopt_count=count,
                last_adopt_time=cache_entry.last_adopt_time
            ))
            
            # 识别冷条目
            if cache_entry.last_adopt_time is not None:
                age_days = (now - cache_entry.last_adopt_time) / (24 * 3600)
                if age_days > self.COLD_ENTRY_THRESHOLD_DAYS:
                    cold_entries.append(ColdEntryInfo(
                        entry_id=entry_id,
                        last_adopt_age_days=age_days,
                        c_value=cache_entry.c_value,
                        c_decay=cache_entry.c_decay
                    ))
            else:
                # 从未被成功采用
                cold_entries.append(ColdEntryInfo(
                    entry_id=entry_id,
                    last_adopt_age_days=999.0,
                    c_value=0.0,
                    c_decay=0.0
                ))
        
        self.state = StatsState.NORMAL
        
        if updates:
            print(f"[{self.module_id}] 批量刷新: {len(updates)} 条, 冷条目={len(cold_entries)}")
        
        return updates, cold_entries
    
    def _calc_decay_c(self, cache_entry: CValueCache) -> float:
        """
        计算时间衰减 C 值（仅供遗忘判定参考）
        
        衰减因子:
        - 最近 30 日: 1.0
        - 30–90 日: 0.8
        - 90–180 日: 0.5
        - > 180 日: 0.2
        """
        if cache_entry.last_adopt_time is None:
            return 0.0
        
        now = time.time()
        age_days = (now - cache_entry.last_adopt_time) / (24 * 3600)
        
        if age_days <= self.DECAY_THRESHOLD_RECENT:
            decay_factor = 1.0
        elif age_days <= self.DECAY_THRESHOLD_MEDIUM:
            decay_factor = 0.8
        elif age_days <= self.DECAY_THRESHOLD_OLD:
            decay_factor = 0.5
        else:
            decay_factor = 0.2
        
        return cache_entry.c_value * decay_factor
    
    # ========== 查询接口 ==========
    
    def get_c_value(self, entry_id: str) -> float:
        """获取条目的标准 C 值"""
        cache_entry = self._cache.get(entry_id)
        return cache_entry.c_value if cache_entry else 0.0
    
    def get_c_decay(self, entry_id: str) -> float:
        """获取条目的时间衰减 C 值（仅供遗忘参考）"""
        cache_entry = self._cache.get(entry_id)
        return cache_entry.c_decay if cache_entry else 0.0
    
    def get_adopt_count(self, entry_id: str) -> int:
        """获取条目的累计成功采用次数"""
        cache_entry = self._cache.get(entry_id)
        return cache_entry.cumulative_adopt_count if cache_entry else 0
    
    def get_cold_entries(self) -> List[ColdEntryInfo]:
        """获取当前冷条目清单"""
        now = time.time()
        cold = []
        for entry_id, cache_entry in self._cache.items():
            if cache_entry.last_adopt_time is None:
                cold.append(ColdEntryInfo(entry_id, 999.0, 0.0, 0.0))
            else:
                age_days = (now - cache_entry.last_adopt_time) / (24 * 3600)
                if age_days > self.COLD_ENTRY_THRESHOLD_DAYS:
                    cold.append(ColdEntryInfo(
                        entry_id, age_days, cache_entry.c_value, cache_entry.c_decay
                    ))
        return cold
    
    def get_cache_size(self) -> int:
        return len(self._cache)
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_logs_processed": self._total_logs_processed,
            "total_valid_adopts": self._total_valid_adopts,
            "total_rejected": self._total_rejected,
            "cache_size": len(self._cache),
            "state": self.state.value
        }
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-33 复用频次 C 值统计单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    now = time.time()
    
    # --- TC-33-01: 首次成功采用 C≈0.167 ---
    print("\n[TC-33-01] 首次成功采用 C=1/6≈0.167")
    try:
        stats = CValueStatistics()
        log = QueryHitLog("EXP-001", now, "ECC-04", "高速巡航", AdoptResult.SUCCESS_OPTIMIZED)
        update = stats.process_query_log(log)
        assert update is not None
        assert abs(update.c_value - 1/6) < 0.01
        assert update.cumulative_adopt_count == 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-33-02: 5 次成功采用 C=0.5 ---
    print("\n[TC-33-02] 5 次成功采用 C=5/10=0.5")
    try:
        stats = CValueStatistics()
        for i in range(5):
            log = QueryHitLog("EXP-002", now, "ECC-04", "高速巡航", AdoptResult.SUCCESS_OPTIMIZED)
            stats.process_query_log(log)
        assert abs(stats.get_c_value("EXP-002") - 0.5) < 0.01
        assert stats.get_adopt_count("EXP-002") == 5
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-33-03: 未采用不计数 ---
    print("\n[TC-33-03] 仅查询未采用不计数")
    try:
        stats = CValueStatistics()
        log = QueryHitLog("EXP-003", now, "ECC-04", "高速巡航", AdoptResult.NOT_ADOPTED)
        update = stats.process_query_log(log)
        assert update is None
        assert stats.get_c_value("EXP-003") == 0.0
        assert stats.get_adopt_count("EXP-003") == 0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-33-04: 策略失误不计数 ---
    print("\n[TC-33-04] 策略失误不计数")
    try:
        stats = CValueStatistics()
        log = QueryHitLog("EXP-004", now, "ECC-04", "高速巡航", AdoptResult.STRATEGY_MISTAKE)
        update = stats.process_query_log(log)
        assert update is None
        assert stats.get_c_value("EXP-004") == 0.0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-33-05: 20 次成功采用 C≈0.8 ---
    print("\n[TC-33-05] 20 次成功采用 C=20/25=0.8")
    try:
        stats = CValueStatistics()
        for i in range(20):
            log = QueryHitLog("EXP-005", now, "ECC-04", "高速巡航", AdoptResult.SUCCESS_OPTIMIZED)
            stats.process_query_log(log)
        assert abs(stats.get_c_value("EXP-005") - 0.8) < 0.01
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-33-06: 批量刷新识别冷条目 ---
    print("\n[TC-33-06] 批量刷新识别冷条目（100 天前采用）")
    try:
        stats = CValueStatistics()
        old_time = now - 100 * 24 * 3600
        log = QueryHitLog("EXP-006", old_time, "ECC-04", "高速巡航", AdoptResult.SUCCESS_OPTIMIZED)
        stats.process_query_log(log)
        # 手动设置 last_adopt_time 为旧时间（因为 process_query_log 会用 log 中的时间）
        stats._cache["EXP-006"].last_adopt_time = old_time
        
        _, cold = stats.batch_refresh(force=True)
        assert len(cold) >= 1
        assert cold[0].entry_id == "EXP-006"
        assert cold[0].c_decay < cold[0].c_value  # 衰减后应更小
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-33-07: 最近 10 天采用不衰减 ---
    print("\n[TC-33-07] 最近 10 天采用不衰减（C_decay=C）")
    try:
        stats = CValueStatistics()
        recent_time = now - 10 * 24 * 3600
        for i in range(5):
            log = QueryHitLog("EXP-007", recent_time, "ECC-04", "高速巡航", AdoptResult.SUCCESS_OPTIMIZED)
            stats.process_query_log(log)
        stats._cache["EXP-007"].last_adopt_time = recent_time
        
        _, cold = stats.batch_refresh(force=True)
        entry = stats._cache["EXP-007"]
        assert entry.c_decay == entry.c_value
        assert len(cold) == 0  # 10 天不是冷条目
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-33-08: 从未采用为冷条目 ---
    print("\n[TC-33-08] 从未采用为冷条目（C=0）")
    try:
        stats = CValueStatistics()
        stats._cache["EXP-008"] = CValueCache(entry_id="EXP-008")
        _, cold = stats.batch_refresh(force=True)
        assert len(cold) >= 1
        assert cold[0].c_value == 0.0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-33-09: 暂停状态不处理 ---
    print("\n[TC-33-09] 暂停状态不处理")
    try:
        stats = CValueStatistics()
        stats.pause()
        log = QueryHitLog("EXP-009", now, "ECC-04", "高速巡航", AdoptResult.SUCCESS_OPTIMIZED)
        update = stats.process_query_log(log)
        assert update is None
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-33-10: 新条目缓存初始化 ---
    print("\n[TC-33-10] 新条目缓存初始化")
    try:
        stats = CValueStatistics()
        assert stats.get_c_value("NON_EXIST") == 0.0
        assert stats.get_adopt_count("NON_EXIST") == 0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- 测试结果汇总 ---
    print("\n" + "=" * 60)
    print(f"测试结果: {passed} PASS, {failed} FAIL")
    print("=" * 60)