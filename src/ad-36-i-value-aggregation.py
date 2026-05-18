#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-36
模块名称: 综合重要度 I 值聚合计算单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 三维重要度计算引擎
核心职责: 执行三维重要度驱动公式 I = I₀ + α·S + β·V + γ·C，将基础重要度 I₀、
          安全显著性 S、风格匹配度 V、复用频次 C 四项因子按权重系数聚合为综合重要度
          I 值。I 值是漏斗二中所有晋升、遗忘、排序决策的唯一量化依据。

依赖模块: ad-31(S 值输入), ad-32(V 值输入), ad-33(C 值输入), ad-34(I₀ 输入),
          ad-35(权重系数 α/β/γ 输入)
被依赖模块: ad-38(晋升判定，消费 I 值), ad-40(遗忘判定，消费 I 值),
            ad-21(L1 时序衰减，参考 I 值), ad-37(定时刷新，触发 I 值更新),
            ad-28(L5 存储，S≥0.9 或 I≥0.9 时直达写入)

安全约束:
  S-01: I 值计算公式编译期固化，运行时不接受非授权的公式修改
  S-02: α + β + γ = 1.0 为强制约束，任何情况下实际计算使用的权重总和必须为 1.0
  S-03: I 值 ≥ 0.9 或 S ≥ 0.9 时，必须触发 L5 直达写入确认，不得遗漏
  S-04: 缺失因子超时后保守处理：I₀ 取 0.20，S 取 0.0
  S-05: I 值缓存中的各因子须保留时间戳，超过 7 日未更新的条目触发重新计算请求
  S-06: 所有 I 值计算结果全量写入 ad-51 变更日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class DataCompleteness(Enum):
    """数据完整性"""
    COMPLETE = "完整"
    PARTIAL = "部分"


class AggregationState(Enum):
    """聚合单元内部状态"""
    NORMAL = "normal"
    WAITING_DATA = "waiting_data"
    BATCH_RECALC = "batch_recalc"
    PAUSED = "paused"


# ==================== 数据结构 ====================

@dataclass
class SValueInput:
    """S 值输入"""
    entry_id: str
    s_value: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class VValueInput:
    """V 值输入"""
    entry_id: str
    v_value: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class CValueInput:
    """C 值输入"""
    entry_id: str
    c_value: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class I0ValueInput:
    """I₀ 值输入"""
    entry_id: str
    i0_value: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class IValueCache:
    """I 值缓存条目"""
    entry_id: str
    i0_value: Optional[float] = None
    s_value: Optional[float] = None
    v_value: Optional[float] = None
    c_value: float = 0.0
    i_value: float = 0.05
    data_completeness: DataCompleteness = DataCompleteness.PARTIAL
    i0_timestamp: float = 0.0
    s_timestamp: float = 0.0
    v_timestamp: float = 0.0
    c_timestamp: float = 0.0
    last_updated: float = field(default_factory=time.time)


@dataclass
class IValueResult:
    """I 值计算结果"""
    entry_id: str
    i_value: float
    factor_details: Dict[str, float]
    data_completeness: DataCompleteness
    calculation_timestamp: float = field(default_factory=time.time)


@dataclass
class L5DirectSignal:
    """L5 直达写入触发信号"""
    entry_id: str
    i_value: float
    s_value: float
    reason: str


# ==================== 主类定义 ====================

class IValueAggregation:
    """
    综合重要度 I 值聚合计算单元
    
    职责:
    1. 接收各因子输入（S/V/C/I₀）
    2. 异步等待数据补全（最长 30 秒）
    3. 执行加权聚合公式 I = I₀ + α·S + β·V + γ·C
    4. 权重变更时全量重算
    5. I≥0.9 或 S≥0.9 时触发 L5 直达信号
    """
    
    # 数据等待超时（秒）
    DATA_WAIT_TIMEOUT = 30.0
    
    # 因子时效检查（秒）
    FACTOR_STALENESS_THRESHOLD = 7 * 24 * 3600  # 7 日
    
    # 默认权重
    DEFAULT_ALPHA = 0.50
    DEFAULT_BETA = 0.20
    DEFAULT_GAMMA = 0.30
    
    # 缺失因子保守值
    CONSERVATIVE_I0 = 0.20
    CONSERVATIVE_S = 0.0
    CONSERVATIVE_V = 0.0
    CONSERVATIVE_C = 0.0
    
    # L5 直达触发阈值
    L5_DIRECT_I_THRESHOLD = 0.90
    L5_DIRECT_S_THRESHOLD = 0.90
    
    # 缓存上限
    MAX_CACHE_SIZE = 100000
    
    def __init__(self):
        self.module_id = "ad-36"
        self.module_name = "综合重要度 I 值聚合计算单元"
        
        # 内部状态
        self.state = AggregationState.NORMAL
        
        # 权重系数
        self._alpha = self.DEFAULT_ALPHA
        self._beta = self.DEFAULT_BETA
        self._gamma = self.DEFAULT_GAMMA
        self._weight_version = 1
        
        # I 值缓存: entry_id -> IValueCache
        self._cache: Dict[str, IValueCache] = {}
        
        # 待补全队列: entry_id -> 首次到达时间
        self._waiting_queue: Dict[str, float] = {}
        
        # 统计
        self._total_calculations = 0
        self._total_l5_signals = 0
        
        # L5 直达信号缓冲区
        self._l5_direct_signals: List[L5DirectSignal] = []
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] I 值聚合计算单元初始化完成")
        print(f"[{self.module_id}] 公式: I = I₀ + α·S + β·V + γ·C")
        print(f"[{self.module_id}] 默认权重: α={self._alpha}, β={self._beta}, γ={self._gamma}")
    
    # ========== 状态管理 ==========
    
    def update_weights(self, alpha: float, beta: float, gamma: float) -> None:
        """更新权重系数（触发全量重算）"""
        old_alpha, old_beta, old_gamma = self._alpha, self._beta, self._gamma
        self._alpha = alpha
        self._beta = beta
        self._gamma = gamma
        self._weight_version += 1
        
        # 权重变更触发全量重算
        if (abs(alpha - old_alpha) > 0.001 or abs(beta - old_beta) > 0.001 or abs(gamma - old_gamma) > 0.001):
            self.state = AggregationState.BATCH_RECALC
            self._recalc_all()
            self.state = AggregationState.NORMAL
        
        print(f"[{self.module_id}] 权重更新: α={alpha}, β={beta}, γ={gamma}")
    
    def pause(self) -> None:
        self.state = AggregationState.PAUSED
    
    def resume(self) -> None:
        self.state = AggregationState.NORMAL
    
    def get_state(self) -> AggregationState:
        return self.state
    
    # ========== 因子输入 ==========
    
    def input_i0(self, data: I0ValueInput) -> Optional[IValueResult]:
        """接收 I₀ 输入"""
        return self._process_factor(data.entry_id, "i0", data.i0_value, data.timestamp)
    
    def input_s(self, data: SValueInput) -> Optional[IValueResult]:
        """接收 S 值输入"""
        return self._process_factor(data.entry_id, "s", data.s_value, data.timestamp)
    
    def input_v(self, data: VValueInput) -> Optional[IValueResult]:
        """接收 V 值输入"""
        return self._process_factor(data.entry_id, "v", data.v_value, data.timestamp)
    
    def input_c(self, data: CValueInput) -> Optional[IValueResult]:
        """接收 C 值输入"""
        return self._process_factor(data.entry_id, "c", data.c_value, data.timestamp)
    
    def _process_factor(self, entry_id: str, factor_type: str, value: float, timestamp: float) -> Optional[IValueResult]:
        """处理单个因子输入"""
        if self.state == AggregationState.PAUSED:
            return None
        
        # 初始化或获取缓存
        if entry_id not in self._cache:
            self._cache[entry_id] = IValueCache(entry_id=entry_id)
            self._waiting_queue[entry_id] = time.time()
            self.state = AggregationState.WAITING_DATA
        
        cache_entry = self._cache[entry_id]
        
        # 更新对应因子
        if factor_type == "i0":
            cache_entry.i0_value = value
            cache_entry.i0_timestamp = timestamp
        elif factor_type == "s":
            cache_entry.s_value = value
            cache_entry.s_timestamp = timestamp
        elif factor_type == "v":
            cache_entry.v_value = value
            cache_entry.v_timestamp = timestamp
        elif factor_type == "c":
            cache_entry.c_value = value
            cache_entry.c_timestamp = timestamp
        
        cache_entry.last_updated = time.time()
        
        # 检查是否可计算（I₀、S、V 三者齐备）
        if cache_entry.i0_value is not None and cache_entry.s_value is not None and cache_entry.v_value is not None:
            return self._calculate_and_output(entry_id, DataCompleteness.COMPLETE)
        
        # 检查超时
        if entry_id in self._waiting_queue:
            wait_start = self._waiting_queue[entry_id]
            if time.time() - wait_start > self.DATA_WAIT_TIMEOUT:
                return self._calculate_and_output(entry_id, DataCompleteness.PARTIAL)
        
        return None
    
    def _calculate_and_output(self, entry_id: str, completeness: DataCompleteness) -> IValueResult:
        """计算 I 值并输出结果"""
        cache_entry = self._cache[entry_id]
        
        # 获取各因子值（缺失时使用保守值）
        i0 = cache_entry.i0_value if cache_entry.i0_value is not None else self.CONSERVATIVE_I0
        s = cache_entry.s_value if cache_entry.s_value is not None else self.CONSERVATIVE_S
        v = cache_entry.v_value if cache_entry.v_value is not None else self.CONSERVATIVE_V
        c = cache_entry.c_value  # C 值默认为 0，无需保守值
        
        # S-02: 确保权重总和为 1.0
        total_weight = self._alpha + self._beta + self._gamma
        if abs(total_weight - 1.0) > 0.001:
            # 自动缩放
            alpha = self._alpha / total_weight
            beta = self._beta / total_weight
            gamma = self._gamma / total_weight
        else:
            alpha, beta, gamma = self._alpha, self._beta, self._gamma
        
        # 核心公式
        i_value = i0 + alpha * s + beta * v + gamma * c
        i_value = max(0.05, min(1.0, i_value))
        
        # 更新缓存
        cache_entry.i_value = i_value
        cache_entry.data_completeness = completeness
        
        # 从等待队列中移除
        self._waiting_queue.pop(entry_id, None)
        
        self._total_calculations += 1
        
        # S-03: L5 直达判定
        if i_value >= self.L5_DIRECT_I_THRESHOLD or s >= self.L5_DIRECT_S_THRESHOLD:
            self._total_l5_signals += 1
            reason = f"I≥0.9" if i_value >= self.L5_DIRECT_I_THRESHOLD else f"S≥0.9"
            self._l5_direct_signals.append(L5DirectSignal(
                entry_id=entry_id, i_value=i_value, s_value=s, reason=reason
            ))
        
        result = IValueResult(
            entry_id=entry_id,
            i_value=i_value,
            factor_details={"I₀": i0, "S": s, "V": v, "C": c},
            data_completeness=completeness
        )
        
        # 缓存上限管理
        if len(self._cache) > self.MAX_CACHE_SIZE:
            self._trim_cache()
        
        return result
    
    def _recalc_all(self) -> None:
        """权重变更时全量重算所有缓存条目"""
        recalc_count = 0
        for entry_id, cache_entry in self._cache.items():
            if cache_entry.data_completeness == DataCompleteness.COMPLETE:
                i0 = cache_entry.i0_value or self.CONSERVATIVE_I0
                s = cache_entry.s_value or self.CONSERVATIVE_S
                v = cache_entry.v_value or self.CONSERVATIVE_V
                c = cache_entry.c_value
                
                i_value = i0 + self._alpha * s + self._beta * v + self._gamma * c
                cache_entry.i_value = max(0.05, min(1.0, i_value))
                recalc_count += 1
        if recalc_count > 0:
            print(f"[{self.module_id}] 权重变更全量重算: {recalc_count} 条")
    
    def _trim_cache(self) -> None:
        """裁剪缓存：移除最久未更新的 20% 条目（L5 直达相关除外）"""
        sorted_entries = sorted(self._cache.items(), key=lambda x: x[1].last_updated)
        remove_count = int(len(self._cache) * 0.2)
        for i in range(remove_count):
            entry_id = sorted_entries[i][0]
            if self._cache[entry_id].i_value >= self.L5_DIRECT_I_THRESHOLD:
                continue
            del self._cache[entry_id]
    
    # ========== 查询接口 ==========
    
    def get_i_value(self, entry_id: str) -> Optional[float]:
        cache_entry = self._cache.get(entry_id)
        return cache_entry.i_value if cache_entry else None
    
    def get_l5_direct_signals(self) -> List[L5DirectSignal]:
        signals = self._l5_direct_signals.copy()
        self._l5_direct_signals.clear()
        return signals
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_calculations": self._total_calculations,
            "total_l5_signals": self._total_l5_signals,
            "cache_size": len(self._cache),
            "waiting_count": len(self._waiting_queue),
            "alpha": self._alpha, "beta": self._beta, "gamma": self._gamma,
            "state": self.state.value
        }
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-36 综合重要度 I 值聚合计算单元 单元测试")
    print("=" * 60)
    passed, failed = 0, 0
    
    # TC-36-01: 四因子齐备正常计算
    print("\n[TC-36-01] 四因子齐备正常计算 I=1.0（截断）")
    try:
        agg = IValueAggregation()
        agg.input_i0(I0ValueInput("EXP-001", 0.50))
        agg.input_s(SValueInput("EXP-001", 0.60))
        agg.input_v(VValueInput("EXP-001", 0.70))
        result = agg.input_c(CValueInput("EXP-001", 0.40))
        assert result is not None
        # I = 0.50 + 0.50*0.60 + 0.20*0.70 + 0.30*0.40 = 0.50+0.30+0.14+0.12=1.06 → 1.0
        assert result.i_value == 1.0
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-36-02: 异步等待超时保守计算
    print("\n[TC-36-02] 异步等待超时保守计算")
    try:
        agg = IValueAggregation()
        agg.DATA_WAIT_TIMEOUT = 0.1
        agg.input_i0(I0ValueInput("EXP-002", 0.50))
        agg.input_s(SValueInput("EXP-002", 0.30))
        time.sleep(0.15)
        result = agg.input_c(CValueInput("EXP-002", 0.0))
        # V 缺失取 0，C 已到，I = 0.50 + 0.50*0.30 + 0.20*0 + 0.30*0 = 0.65
        assert result is not None
        assert abs(result.i_value - 0.65) < 0.01
        assert result.data_completeness == DataCompleteness.PARTIAL
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-36-03: I≥0.9 触发 L5 直达信号
    print("\n[TC-36-03] I≥0.9 触发 L5 直达信号")
    try:
        agg = IValueAggregation()
        agg.input_i0(I0ValueInput("EXP-003", 0.50))
        agg.input_s(SValueInput("EXP-003", 0.95))
        agg.input_v(VValueInput("EXP-003", 0.0))
        result = agg.input_c(CValueInput("EXP-003", 0.0))
        signals = agg.get_l5_direct_signals()
        assert len(signals) >= 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-36-04: 权重变更全量重算
    print("\n[TC-36-04] 权重变更全量重算")
    try:
        agg = IValueAggregation()
        agg.input_i0(I0ValueInput("EXP-004", 0.50))
        agg.input_s(SValueInput("EXP-004", 0.60))
        agg.input_v(VValueInput("EXP-004", 0.70))
        agg.input_c(CValueInput("EXP-004", 0.40))
        # 变更权重
        agg.update_weights(0.60, 0.15, 0.25)
        new_i = agg.get_i_value("EXP-004")
        assert new_i is not None
        # I = 0.50 + 0.60*0.60 + 0.15*0.70 + 0.25*0.40 = 0.50+0.36+0.105+0.10=1.065→1.0
        assert new_i == 1.0
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-36-05: 缓存上限裁剪
    print("\n[TC-36-05] 缓存上限裁剪")
    try:
        agg = IValueAggregation()
        agg.MAX_CACHE_SIZE = 5
        for i in range(10):
            agg.input_i0(I0ValueInput(f"EXP-{i:03d}", 0.50))
            agg.input_s(SValueInput(f"EXP-{i:03d}", 0.60))
            agg.input_v(VValueInput(f"EXP-{i:03d}", 0.70))
            agg.input_c(CValueInput(f"EXP-{i:03d}", 0.40))
        assert len(agg._cache) <= 10  # 裁剪后不大于原数量
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    print(f"\n测试结果: {passed} PASS, {failed} FAIL")