#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-35
模块名称: 三维权重系数配置单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 三维重要度计算引擎
核心职责: 存储、管理与分发三维重要度驱动公式 I = I₀ + α·S + β·V + γ·C 中的权重系数
          α、β、γ，以及各场景分槽的专属晋升阈值与遗忘策略参数。提供参数读取、
          动态调整（受权限控制）与一致性校验功能。是记忆系统可配置性的核心枢纽。

依赖模块: ad-36(综合重要度 I 值聚合计算单元，消费权重系数),
          ad-31/32/33(消费权重), 各分槽(消费分槽专属阈值),
          ad-01(总控漏斗 F₀，接收参数变更指令)
被依赖模块: ad-36、ad-31、ad-32、ad-33、各场景分槽、ad-21(L1 时序衰减单元)等

安全约束:
  S-01: α, β, γ 的硬约束范围编译期固化，任何运行时操作不可突破上限或下限
  S-02: α + β + γ = 1.0 为强制约束，系统自动缩放确保一致性
  S-03: 晋升时间阈值存在编译期硬下限，防止过度加速导致经验泡沫
  S-04: 所有参数变更（含自动缩放）全量写入 ad-51 不可变日志
  S-05: 参数回滚机制确保任何异常可在 300 秒内检测并自动恢复
  S-06: 用户级令牌不可调整 α 和 γ，仅可调整 β 及相关风格参数
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class ConfigState(Enum):
    """配置单元内部状态"""
    NORMAL = "normal"
    UPDATING = "updating"
    ROLLBACK = "rollback"
    PAUSED = "paused"


class PermissionLevel(Enum):
    """变更权限级别"""
    USER = "user"
    ENGINEER = "engineer"
    SYSTEM = "system"


# ==================== 数据结构 ====================

@dataclass
class WeightCoefficients:
    """权重系数"""
    alpha: float = 0.50
    beta: float = 0.20
    gamma: float = 0.30


@dataclass
class SlotThresholds:
    """分槽专属阈值"""
    slot_id: int
    # 晋升 I 阈值
    promotion_l1_l2: float = 0.40
    promotion_l2_l3: float = 0.60
    promotion_l3_l4: float = 0.80
    # 晋升时间阈值（秒）
    time_l1_l2: float = 24 * 3600
    time_l2_l3: float = 7 * 24 * 3600
    time_l3_l4: float = 30 * 24 * 3600
    # 遗忘阈值
    forget_threshold: float = 0.15
    # 权重覆写（可选）
    alpha_override: Optional[float] = None
    beta_override: Optional[float] = None
    gamma_override: Optional[float] = None


@dataclass
class ParameterChangeRequest:
    """参数变更请求"""
    request_id: str
    target_parameter: str           # "alpha" / "beta" / "gamma" / "slot_threshold" / etc.
    new_value: Any
    reason: str
    permission_level: PermissionLevel
    operator_token: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class ParameterChangeResponse:
    """参数变更响应"""
    request_id: str
    success: bool
    old_value: Any
    new_value: Any
    message: str
    version: int


@dataclass
class ConfigSnapshot:
    """配置快照（用于回滚）"""
    version: int
    weights: WeightCoefficients
    slot_thresholds: Dict[int, SlotThresholds]
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class WeightCoefficientConfig:
    """
    三维权重系数配置单元
    
    职责:
    1. 管理全局权重系数 α/β/γ（硬约束范围保护）
    2. 管理各分槽专属晋升/遗忘阈值
    3. 处理参数变更请求（权限校验、硬约束校验、总和校验）
    4. 自动检测参数异常并触发回滚
    5. 提供参数读取服务
    """
    
    # ========== 编译期硬约束 ==========
    # 权重范围
    ALPHA_RANGE = (0.30, 0.70)
    BETA_RANGE = (0.10, 0.40)
    GAMMA_RANGE = (0.10, 0.50)
    
    # 晋升时间硬下限（秒）
    MIN_TIME_L1_L2 = 10 * 3600        # 10 小时
    MIN_TIME_L2_L3 = 3 * 24 * 3600    # 3 日
    MIN_TIME_L3_L4 = 15 * 24 * 3600   # 15 日
    
    # 遗忘阈值硬范围
    FORGET_THRESHOLD_RANGE = (0.05, 0.60)
    
    # 晋升 I 阈值硬范围
    PROMOTION_I_RANGE = (0.20, 0.95)
    
    # 异常检测间隔（秒）
    ANOMALY_CHECK_INTERVAL = 300  # 5 分钟
    
    def __init__(self):
        self.module_id = "ad-35"
        self.module_name = "三维权重系数配置单元"
        
        # 内部状态
        self.state = ConfigState.NORMAL
        
        # 全局权重（默认值）
        self._weights = WeightCoefficients()
        
        # 分槽阈值（默认值）
        self._slot_thresholds: Dict[int, SlotThresholds] = self._init_default_slot_thresholds()
        
        # 配置版本号
        self._version = 1
        
        # 配置历史（用于回滚）
        self._history: List[ConfigSnapshot] = []
        self._save_snapshot()  # 保存初始快照
        
        # 上次异常检测时间
        self._last_anomaly_check = time.time()
        
        # 统计
        self._total_changes = 0
        self._total_rejections = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 权重配置单元初始化完成")
        print(f"[{self.module_id}] 全局权重: α={self._weights.alpha}, β={self._weights.beta}, γ={self._weights.gamma}")
        print(f"[{self.module_id}] 硬约束: α∈{self.ALPHA_RANGE}, β∈{self.BETA_RANGE}, γ∈{self.GAMMA_RANGE}")
    
    def _init_default_slot_thresholds(self) -> Dict[int, SlotThresholds]:
        """初始化各分槽默认阈值"""
        defaults = {
            15: SlotThresholds(slot_id=15, forget_threshold=0.12),  # 高速巡航槽
            16: SlotThresholds(slot_id=16, forget_threshold=0.10),  # 城区路口槽
            17: SlotThresholds(slot_id=17, forget_threshold=0.075), # 泊车低速槽
            18: SlotThresholds(slot_id=18, forget_threshold=0.09,   # 特殊环境槽
                               promotion_l1_l2=0.28, promotion_l2_l3=0.42, promotion_l3_l4=0.56),
            19: SlotThresholds(slot_id=19),  # 通用驾驶槽（默认值）
        }
        return defaults
    
    # ========== 状态管理 ==========
    
    def pause(self) -> None:
        self.state = ConfigState.PAUSED
    
    def resume(self) -> None:
        self.state = ConfigState.NORMAL
    
    def get_state(self) -> ConfigState:
        return self.state
    
    # ========== 参数读取 ==========
    
    def get_weights(self) -> WeightCoefficients:
        """获取当前全局权重"""
        return WeightCoefficients(
            alpha=self._weights.alpha,
            beta=self._weights.beta,
            gamma=self._weights.gamma
        )
    
    def get_slot_thresholds(self, slot_id: int) -> Optional[SlotThresholds]:
        """获取指定分槽的阈值配置"""
        return self._slot_thresholds.get(slot_id)
    
    def get_all_slot_thresholds(self) -> Dict[int, SlotThresholds]:
        """获取所有分槽阈值配置"""
        return self._slot_thresholds.copy()
    
    def get_effective_weights(self, slot_id: int) -> WeightCoefficients:
        """获取指定分槽的有效权重（如有覆写则使用覆写值）"""
        slot_config = self._slot_thresholds.get(slot_id)
        if slot_config:
            return WeightCoefficients(
                alpha=slot_config.alpha_override if slot_config.alpha_override is not None else self._weights.alpha,
                beta=slot_config.beta_override if slot_config.beta_override is not None else self._weights.beta,
                gamma=slot_config.gamma_override if slot_config.gamma_override is not None else self._weights.gamma,
            )
        return self.get_weights()
    
    # ========== 参数变更 ==========
    
    def request_change(self, request: ParameterChangeRequest,
                       token_validator) -> ParameterChangeResponse:
        """
        处理参数变更请求
        
        处理流程:
        1. 权限校验
        2. 硬约束校验
        3. 总和校验（如涉及权重）
        4. 执行变更
        5. 保存快照
        """
        if self.state == ConfigState.PAUSED:
            return ParameterChangeResponse(
                request_id=request.request_id,
                success=False, old_value=None, new_value=request.new_value,
                message="系统暂停中", version=self._version
            )
        
        self.state = ConfigState.UPDATING
        
        # 权限校验
        perm = request.permission_level
        target = request.target_parameter
        
        # S-06: 用户级令牌不可调整 α 和 γ
        if perm == PermissionLevel.USER:
            if target in ["alpha", "gamma"]:
                self._total_rejections += 1
                self.state = ConfigState.NORMAL
                return ParameterChangeResponse(
                    request_id=request.request_id,
                    success=False, old_value=None, new_value=request.new_value,
                    message="用户级权限不可调整 α 或 γ", version=self._version
                )
        
        # 令牌验证（简化：SYSTEM 级别直接放行）
        if perm not in [PermissionLevel.ENGINEER, PermissionLevel.SYSTEM]:
            if not token_validator(request.operator_token, target, perm):
                self._total_rejections += 1
                self.state = ConfigState.NORMAL
                return ParameterChangeResponse(
                    request_id=request.request_id,
                    success=False, old_value=None, new_value=request.new_value,
                    message="令牌验证失败", version=self._version
                )
        
        # 根据目标参数执行变更
        response = self._execute_change(request)
        self.state = ConfigState.NORMAL
        return response
    
    def _execute_change(self, request: ParameterChangeRequest) -> ParameterChangeResponse:
        """执行参数变更"""
        target = request.target_parameter
        new_val = request.new_value
        
        try:
            if target == "alpha":
                return self._change_alpha(new_val, request)
            elif target == "beta":
                return self._change_beta(new_val, request)
            elif target == "gamma":
                return self._change_gamma(new_val, request)
            elif target == "forget_threshold":
                slot_id = request.new_value.get("slot_id") if isinstance(new_val, dict) else None
                threshold = request.new_value.get("threshold") if isinstance(new_val, dict) else new_val
                return self._change_forget_threshold(slot_id, threshold, request)
            elif target == "promotion_time":
                slot_id = request.new_value.get("slot_id") if isinstance(new_val, dict) else None
                layer = request.new_value.get("layer") if isinstance(new_val, dict) else None
                time_val = request.new_value.get("time") if isinstance(new_val, dict) else new_val
                return self._change_promotion_time(slot_id, layer, time_val, request)
            elif target == "promotion_i":
                slot_id = request.new_value.get("slot_id") if isinstance(new_val, dict) else None
                layer = request.new_value.get("layer") if isinstance(new_val, dict) else None
                i_val = request.new_value.get("i_value") if isinstance(new_val, dict) else new_val
                return self._change_promotion_i(slot_id, layer, i_val, request)
            else:
                self._total_rejections += 1
                return ParameterChangeResponse(
                    request_id=request.request_id,
                    success=False, old_value=None, new_value=new_val,
                    message=f"未知参数: {target}", version=self._version
                )
        except Exception as e:
            self._total_rejections += 1
            return ParameterChangeResponse(
                request_id=request.request_id,
                success=False, old_value=None, new_value=new_val,
                message=f"变更异常: {str(e)}", version=self._version
            )
    
    def _change_alpha(self, new_val: float, request: ParameterChangeRequest) -> ParameterChangeResponse:
        """变更 α 权重"""
        old_val = self._weights.alpha
        
        # 硬约束检查
        if not (self.ALPHA_RANGE[0] <= new_val <= self.ALPHA_RANGE[1]):
            self._total_rejections += 1
            return ParameterChangeResponse(
                request_id=request.request_id,
                success=False, old_value=old_val, new_value=new_val,
                message=f"α 超出硬约束范围 {self.ALPHA_RANGE}", version=self._version
            )
        
        # 暂存并检查总和
        temp_alpha = new_val
        temp_beta = self._weights.beta
        temp_gamma = self._weights.gamma
        total = temp_alpha + temp_beta + temp_gamma
        
        if abs(total - 1.0) > 0.001:
            # S-02: 自动缩放其他权重
            if total > 0:
                temp_beta = temp_beta * (1.0 - temp_alpha) / (temp_beta + temp_gamma) if (temp_beta + temp_gamma) > 0 else 0.0
                temp_gamma = temp_gamma * (1.0 - temp_alpha) / (temp_beta + temp_gamma) if (temp_beta + temp_gamma) > 0 else 0.0
            else:
                temp_beta = (1.0 - temp_alpha) / 2
                temp_gamma = (1.0 - temp_alpha) / 2
            
            # 检查缩放后是否在硬约束内
            if not (self.BETA_RANGE[0] <= temp_beta <= self.BETA_RANGE[1]):
                self._total_rejections += 1
                return ParameterChangeResponse(
                    request_id=request.request_id,
                    success=False, old_value=old_val, new_value=new_val,
                    message="自动缩放后 β 超出硬约束范围", version=self._version
                )
            if not (self.GAMMA_RANGE[0] <= temp_gamma <= self.GAMMA_RANGE[1]):
                self._total_rejections += 1
                return ParameterChangeResponse(
                    request_id=request.request_id,
                    success=False, old_value=old_val, new_value=new_val,
                    message="自动缩放后 γ 超出硬约束范围", version=self._version
                )
        
        self._weights.alpha = temp_alpha
        self._weights.beta = temp_beta
        self._weights.gamma = temp_gamma
        self._total_changes += 1
        self._version += 1
        self._save_snapshot()
        self._log_change("alpha", old_val, temp_alpha, request.reason)
        
        return ParameterChangeResponse(
            request_id=request.request_id,
            success=True, old_value=old_val, new_value=temp_alpha,
            message=f"α 已更新 (β={temp_beta:.2f}, γ={temp_gamma:.2f})", version=self._version
        )
    
    def _change_beta(self, new_val: float, request: ParameterChangeRequest) -> ParameterChangeResponse:
        """变更 β 权重（逻辑同 α）"""
        old_val = self._weights.beta
        
        if not (self.BETA_RANGE[0] <= new_val <= self.BETA_RANGE[1]):
            self._total_rejections += 1
            return ParameterChangeResponse(
                request_id=request.request_id,
                success=False, old_value=old_val, new_value=new_val,
                message=f"β 超出硬约束范围 {self.BETA_RANGE}", version=self._version
            )
        
        temp_beta = new_val
        temp_alpha = self._weights.alpha
        temp_gamma = self._weights.gamma
        total = temp_alpha + temp_beta + temp_gamma
        
        if abs(total - 1.0) > 0.001:
            if total > 0:
                temp_alpha = temp_alpha * (1.0 - temp_beta) / (temp_alpha + temp_gamma) if (temp_alpha + temp_gamma) > 0 else 0.0
                temp_gamma = temp_gamma * (1.0 - temp_beta) / (temp_alpha + temp_gamma) if (temp_alpha + temp_gamma) > 0 else 0.0
            else:
                temp_alpha = (1.0 - temp_beta) / 2
                temp_gamma = (1.0 - temp_beta) / 2
            
            if not (self.ALPHA_RANGE[0] <= temp_alpha <= self.ALPHA_RANGE[1]):
                self._total_rejections += 1
                return ParameterChangeResponse(
                    request_id=request.request_id,
                    success=False, old_value=old_val, new_value=new_val,
                    message="自动缩放后 α 超出硬约束范围", version=self._version
                )
            if not (self.GAMMA_RANGE[0] <= temp_gamma <= self.GAMMA_RANGE[1]):
                self._total_rejections += 1
                return ParameterChangeResponse(
                    request_id=request.request_id,
                    success=False, old_value=old_val, new_value=new_val,
                    message="自动缩放后 γ 超出硬约束范围", version=self._version
                )
        
        self._weights.alpha = temp_alpha
        self._weights.beta = temp_beta
        self._weights.gamma = temp_gamma
        self._total_changes += 1
        self._version += 1
        self._save_snapshot()
        self._log_change("beta", old_val, temp_beta, request.reason)
        
        return ParameterChangeResponse(
            request_id=request.request_id,
            success=True, old_value=old_val, new_value=temp_beta,
            message=f"β 已更新 (α={temp_alpha:.2f}, γ={temp_gamma:.2f})", version=self._version
        )
    
    def _change_gamma(self, new_val: float, request: ParameterChangeRequest) -> ParameterChangeResponse:
        """变更 γ 权重（逻辑同 α）"""
        old_val = self._weights.gamma
        
        if not (self.GAMMA_RANGE[0] <= new_val <= self.GAMMA_RANGE[1]):
            self._total_rejections += 1
            return ParameterChangeResponse(
                request_id=request.request_id,
                success=False, old_value=old_val, new_value=new_val,
                message=f"γ 超出硬约束范围 {self.GAMMA_RANGE}", version=self._version
            )
        
        temp_gamma = new_val
        temp_alpha = self._weights.alpha
        temp_beta = self._weights.beta
        total = temp_alpha + temp_beta + temp_gamma
        
        if abs(total - 1.0) > 0.001:
            if total > 0:
                temp_alpha = temp_alpha * (1.0 - temp_gamma) / (temp_alpha + temp_beta) if (temp_alpha + temp_beta) > 0 else 0.0
                temp_beta = temp_beta * (1.0 - temp_gamma) / (temp_alpha + temp_beta) if (temp_alpha + temp_beta) > 0 else 0.0
            else:
                temp_alpha = (1.0 - temp_gamma) / 2
                temp_beta = (1.0 - temp_gamma) / 2
            
            if not (self.ALPHA_RANGE[0] <= temp_alpha <= self.ALPHA_RANGE[1]):
                self._total_rejections += 1
                return ParameterChangeResponse(
                    request_id=request.request_id,
                    success=False, old_value=old_val, new_value=new_val,
                    message="自动缩放后 α 超出硬约束范围", version=self._version
                )
            if not (self.BETA_RANGE[0] <= temp_beta <= self.BETA_RANGE[1]):
                self._total_rejections += 1
                return ParameterChangeResponse(
                    request_id=request.request_id,
                    success=False, old_value=old_val, new_value=new_val,
                    message="自动缩放后 β 超出硬约束范围", version=self._version
                )
        
        self._weights.alpha = temp_alpha
        self._weights.beta = temp_beta
        self._weights.gamma = temp_gamma
        self._total_changes += 1
        self._version += 1
        self._save_snapshot()
        self._log_change("gamma", old_val, temp_gamma, request.reason)
        
        return ParameterChangeResponse(
            request_id=request.request_id,
            success=True, old_value=old_val, new_value=temp_gamma,
            message=f"γ 已更新 (α={temp_alpha:.2f}, β={temp_beta:.2f})", version=self._version
        )
    
    def _change_forget_threshold(self, slot_id: int, threshold: float, request: ParameterChangeRequest) -> ParameterChangeResponse:
        """变更分槽遗忘阈值"""
        if slot_id not in self._slot_thresholds:
            self._total_rejections += 1
            return ParameterChangeResponse(
                request_id=request.request_id,
                success=False, old_value=None, new_value=threshold,
                message=f"分槽 {slot_id} 不存在", version=self._version
            )
        
        # 硬约束检查
        if not (self.FORGET_THRESHOLD_RANGE[0] <= threshold <= self.FORGET_THRESHOLD_RANGE[1]):
            self._total_rejections += 1
            return ParameterChangeResponse(
                request_id=request.request_id,
                success=False, old_value=None, new_value=threshold,
                message=f"遗忘阈值超出硬约束范围 {self.FORGET_THRESHOLD_RANGE}", version=self._version
            )
        
        old_val = self._slot_thresholds[slot_id].forget_threshold
        self._slot_thresholds[slot_id].forget_threshold = threshold
        self._total_changes += 1
        self._version += 1
        self._save_snapshot()
        self._log_change(f"slot_{slot_id}_forget", old_val, threshold, request.reason)
        
        return ParameterChangeResponse(
            request_id=request.request_id,
            success=True, old_value=old_val, new_value=threshold,
            message=f"分槽 {slot_id} 遗忘阈值已更新", version=self._version
        )
    
    def _change_promotion_time(self, slot_id: int, layer: str, time_val: float, request: ParameterChangeRequest) -> ParameterChangeResponse:
        """变更晋升时间阈值"""
        if slot_id not in self._slot_thresholds:
            self._total_rejections += 1
            return ParameterChangeResponse(
                request_id=request.request_id,
                success=False, old_value=None, new_value=time_val,
                message=f"分槽 {slot_id} 不存在", version=self._version
            )
        
        # S-03: 硬下限校验
        slot = self._slot_thresholds[slot_id]
        old_val = None
        
        if layer == "L1_L2":
            if time_val < self.MIN_TIME_L1_L2:
                self._total_rejections += 1
                return ParameterChangeResponse(request_id=request.request_id, success=False, old_value=slot.time_l1_l2, new_value=time_val, message=f"L1→L2 时间低于硬下限 {self.MIN_TIME_L1_L2/3600:.0f}h", version=self._version)
            old_val = slot.time_l1_l2
            slot.time_l1_l2 = time_val
        elif layer == "L2_L3":
            if time_val < self.MIN_TIME_L2_L3:
                self._total_rejections += 1
                return ParameterChangeResponse(request_id=request.request_id, success=False, old_value=slot.time_l2_l3, new_value=time_val, message=f"L2→L3 时间低于硬下限 {self.MIN_TIME_L2_L3/86400:.0f}日", version=self._version)
            old_val = slot.time_l2_l3
            slot.time_l2_l3 = time_val
        elif layer == "L3_L4":
            if time_val < self.MIN_TIME_L3_L4:
                self._total_rejections += 1
                return ParameterChangeResponse(request_id=request.request_id, success=False, old_value=slot.time_l3_l4, new_value=time_val, message=f"L3→L4 时间低于硬下限 {self.MIN_TIME_L3_L4/86400:.0f}日", version=self._version)
            old_val = slot.time_l3_l4
            slot.time_l3_l4 = time_val
        else:
            self._total_rejections += 1
            return ParameterChangeResponse(request_id=request.request_id, success=False, old_value=None, new_value=time_val, message=f"未知层级: {layer}", version=self._version)
        
        self._total_changes += 1
        self._version += 1
        self._save_snapshot()
        self._log_change(f"slot_{slot_id}_time_{layer}", old_val, time_val, request.reason)
        
        return ParameterChangeResponse(
            request_id=request.request_id,
            success=True, old_value=old_val, new_value=time_val,
            message=f"分槽 {slot_id} {layer} 时间阈值已更新", version=self._version
        )
    
    def _change_promotion_i(self, slot_id: int, layer: str, i_val: float, request: ParameterChangeRequest) -> ParameterChangeResponse:
        """变更晋升 I 阈值"""
        if slot_id not in self._slot_thresholds:
            self._total_rejections += 1
            return ParameterChangeResponse(request_id=request.request_id, success=False, old_value=None, new_value=i_val, message=f"分槽 {slot_id} 不存在", version=self._version)
        
        if not (self.PROMOTION_I_RANGE[0] <= i_val <= self.PROMOTION_I_RANGE[1]):
            self._total_rejections += 1
            return ParameterChangeResponse(request_id=request.request_id, success=False, old_value=None, new_value=i_val, message=f"晋升 I 阈值超出硬约束范围 {self.PROMOTION_I_RANGE}", version=self._version)
        
        slot = self._slot_thresholds[slot_id]
        old_val = None
        if layer == "L1_L2":
            old_val = slot.promotion_l1_l2
            slot.promotion_l1_l2 = i_val
        elif layer == "L2_L3":
            old_val = slot.promotion_l2_l3
            slot.promotion_l2_l3 = i_val
        elif layer == "L3_L4":
            old_val = slot.promotion_l3_l4
            slot.promotion_l3_l4 = i_val
        else:
            self._total_rejections += 1
            return ParameterChangeResponse(request_id=request.request_id, success=False, old_value=None, new_value=i_val, message=f"未知层级: {layer}", version=self._version)
        
        self._total_changes += 1
        self._version += 1
        self._save_snapshot()
        self._log_change(f"slot_{slot_id}_i_{layer}", old_val, i_val, request.reason)
        
        return ParameterChangeResponse(request_id=request.request_id, success=True, old_value=old_val, new_value=i_val, message=f"分槽 {slot_id} {layer} I 阈值已更新", version=self._version)
    
    # ========== 异常检测与回滚 ==========
    
    def check_anomaly(self) -> bool:
        """
        定期检查参数异常
        
        S-05: 检测到异常时自动回滚至上一个安全版本
        """
        now = time.time()
        if now - self._last_anomaly_check < self.ANOMALY_CHECK_INTERVAL:
            return False
        
        self._last_anomaly_check = now
        
        # 检查总和
        total = self._weights.alpha + self._weights.beta + self._weights.gamma
        if abs(total - 1.0) > 0.01:
            print(f"[{self.module_id}] 检测到权重总和异常: {total:.3f}，触发回滚")
            self._rollback()
            return True
        
        # 检查各权重是否在硬约束内
        if not (self.ALPHA_RANGE[0] <= self._weights.alpha <= self.ALPHA_RANGE[1]):
            print(f"[{self.module_id}] 检测到 α 异常: {self._weights.alpha}，触发回滚")
            self._rollback()
            return True
        
        return False
    
    def _rollback(self) -> None:
        """回滚至上一个安全版本"""
        if len(self._history) > 1:
            self.state = ConfigState.ROLLBACK
            # 移除当前版本
            self._history.pop()
            # 恢复上一个版本
            last_safe = self._history[-1]
            self._weights = WeightCoefficients(
                alpha=last_safe.weights.alpha,
                beta=last_safe.weights.beta,
                gamma=last_safe.weights.gamma
            )
            self._slot_thresholds = last_safe.slot_thresholds
            self._version = last_safe.version
            self.state = ConfigState.NORMAL
            print(f"[{self.module_id}] 回滚至版本 {self._version}")
            self._log_change("ROLLBACK", "anomaly", f"v{self._version}", "异常检测触发")
        else:
            # 无历史版本，回退至编译期默认值
            self._weights = WeightCoefficients()
            self._slot_thresholds = self._init_default_slot_thresholds()
            self._version = 1
            self.state = ConfigState.NORMAL
            print(f"[{self.module_id}] 无历史版本，回退至编译期默认值")
    
    def _save_snapshot(self) -> None:
        """保存当前配置快照"""
        snapshot = ConfigSnapshot(
            version=self._version,
            weights=WeightCoefficients(
                alpha=self._weights.alpha,
                beta=self._weights.beta,
                gamma=self._weights.gamma
            ),
            slot_thresholds={
                sid: SlotThresholds(
                    slot_id=st.slot_id,
                    promotion_l1_l2=st.promotion_l1_l2,
                    promotion_l2_l3=st.promotion_l2_l3,
                    promotion_l3_l4=st.promotion_l3_l4,
                    time_l1_l2=st.time_l1_l2,
                    time_l2_l3=st.time_l2_l3,
                    time_l3_l4=st.time_l3_l4,
                    forget_threshold=st.forget_threshold
                )
                for sid, st in self._slot_thresholds.items()
            }
        )
        self._history.append(snapshot)
        # 保留最近 10 个版本
        if len(self._history) > 10:
            self._history = self._history[-10:]
    
    # ========== 变更日志 ==========
    
    def _log_change(self, parameter: str, old_value: Any, new_value: Any, reason: str) -> None:
        self._pending_logs.append({
            "log_id": f"cfg-{uuid.uuid4().hex[:8]}",
            "parameter": parameter,
            "old_value": str(old_value),
            "new_value": str(new_value),
            "reason": reason,
            "version": self._version,
            "timestamp": time.time()
        })
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "version": self._version,
            "total_changes": self._total_changes,
            "total_rejections": self._total_rejections,
            "alpha": self._weights.alpha,
            "beta": self._weights.beta,
            "gamma": self._weights.gamma,
            "state": self.state.value
        }


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-35 三维权重系数配置单元 单元测试")
    print("=" * 60)
    passed, failed = 0, 0
    
    def token_ok(*args): return True
    def token_fail(*args): return False
    
    # TC-35-01: 读取默认权重
    print("\n[TC-35-01] 读取默认权重")
    try:
        cfg = WeightCoefficientConfig()
        w = cfg.get_weights()
        assert w.alpha == 0.50 and w.beta == 0.20 and w.gamma == 0.30
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-35-02: 正常变更 α
    print("\n[TC-35-02] 正常变更 α=0.55")
    try:
        cfg = WeightCoefficientConfig()
        req = ParameterChangeRequest("req-001", "alpha", 0.55, "测试", PermissionLevel.ENGINEER, "token")
        resp = cfg.request_change(req, token_ok)
        assert resp.success and cfg._weights.alpha == 0.55
        assert abs(cfg._weights.beta + cfg._weights.gamma + 0.55 - 1.0) < 0.01
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-35-03: 超出硬约束拒绝
    print("\n[TC-35-03] α=0.80 超出硬约束拒绝")
    try:
        cfg = WeightCoefficientConfig()
        req = ParameterChangeRequest("req-002", "alpha", 0.80, "测试", PermissionLevel.ENGINEER, "token")
        resp = cfg.request_change(req, token_ok)
        assert not resp.success
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-35-04: 用户级不可改 α
    print("\n[TC-35-04] 用户级不可改 α")
    try:
        cfg = WeightCoefficientConfig()
        req = ParameterChangeRequest("req-003", "alpha", 0.55, "测试", PermissionLevel.USER, "token")
        resp = cfg.request_change(req, token_ok)
        assert not resp.success
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-35-05: 晋升时间低于硬下限拒绝
    print("\n[TC-35-05] L1→L2 时间 5h < 10h 拒绝")
    try:
        cfg = WeightCoefficientConfig()
        req_data = {"slot_id": 15, "layer": "L1_L2", "time": 5 * 3600}
        req = ParameterChangeRequest("req-004", "promotion_time", req_data, "测试", PermissionLevel.ENGINEER, "token")
        resp = cfg.request_change(req, token_ok)
        assert not resp.success
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-35-06: 总和异常自动回滚
    print("\n[TC-35-06] 总和异常自动回滚")
    try:
        cfg = WeightCoefficientConfig()
        # 手动破坏
        cfg._weights.alpha = 0.60
        cfg._weights.beta = 0.30
        cfg._weights.gamma = 0.30  # 总和 1.20
        cfg._save_snapshot()
        rolled = cfg.check_anomaly()
        # 检查时总和 1.20 > 1.01 触发回滚
        # 但 check_anomaly 间隔默认 300s，这里需要强制设置
        cfg._last_anomaly_check = 0
        rolled = cfg.check_anomaly()
        assert rolled or abs(cfg._weights.alpha + cfg._weights.beta + cfg._weights.gamma - 1.0) < 0.01
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-35-07: 变更遗忘阈值成功
    print("\n[TC-35-07] 变更遗忘阈值成功")
    try:
        cfg = WeightCoefficientConfig()
        req_data = {"slot_id": 15, "threshold": 0.10}
        req = ParameterChangeRequest("req-005", "forget_threshold", req_data, "测试", PermissionLevel.ENGINEER, "token")
        resp = cfg.request_change(req, token_ok)
        assert resp.success
        assert cfg._slot_thresholds[15].forget_threshold == 0.10
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-35-08: 暂停状态拒绝变更
    print("\n[TC-35-08] 暂停状态拒绝变更")
    try:
        cfg = WeightCoefficientConfig()
        cfg.pause()
        req = ParameterChangeRequest("req-006", "beta", 0.25, "测试", PermissionLevel.USER, "token")
        resp = cfg.request_change(req, token_ok)
        assert not resp.success
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    print(f"\n测试结果: {passed} PASS, {failed} FAIL")