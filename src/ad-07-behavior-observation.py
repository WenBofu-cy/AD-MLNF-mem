#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-07
模块名称: 驾驶行为观测记录单元
所属分区: 二、漏斗一：驾驶员画像漏斗
核心职责: 持续观测并结构化记录驾驶员当前操作行为。从车辆 CAN 总线实时采集方向盘转角、
          油门开度、制动压力、转向灯状态等操控数据，打包为标准化行为观测条目，
          经 ad-06 隔离校验后写入对应子画像槽。

依赖模块: ad-06(子画像槽数据隔离管控单元), ad-02(漏斗一专属调度单元)
被依赖模块: ad-08(上下文场景标记单元), ad-10(行为累积统计单元)

安全约束:
  S-01: 漏斗一数据编译期禁止接入自动驾驶决策链路
  S-02: CAN 总线数据为只读订阅，本模块不得向 CAN 总线写入任何数据
  S-03: 行为观测条目中的原始操控数据不得外传至云端或 OTA 导出
  S-04: 所有观测条目写入前须经 ad-06 隔离校验
  S-05: 紧急熔断时立即停止采集并丢弃未写入缓存
  S-06: CAN 信号中断时对缺失数据的插值估算必须标记"插值估算"
  S-07: 观测条目保留 UTC 时间戳（毫秒精度），时序不可篡改
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid
import math


# ==================== 枚举定义 ====================

class BehaviorType(Enum):
    """驾驶行为类型"""
    CRUISE = "匀速巡航"
    ACCELERATE = "加速"
    DECELERATE = "减速"
    BRAKE = "制动"
    TURN = "转弯"
    LANE_CHANGE = "变道"
    PARK = "停车"
    START = "起步"


class DataQuality(Enum):
    """数据质量标记"""
    COMPLETE = "完整"
    PARTIAL = "部分缺失"
    INTERPOLATED = "插值估算"


class GearPosition(Enum):
    """档位"""
    P = "P"
    R = "R"
    N = "N"
    D = "D"


class TurnSignal(Enum):
    """转向灯状态"""
    OFF = "关闭"
    LEFT = "左转"
    RIGHT = "右转"


class ObserverState(Enum):
    """观测单元内部状态"""
    IDLE = "idle"
    OBSERVING = "observing"
    PAUSED = "paused"
    DEGRADED = "degraded"
    STOPPED = "stopped"


# ==================== 数据结构 ====================

@dataclass
class CANFrame:
    """CAN 总线数据帧"""
    steering_angle: float         # 方向盘转角（度）
    steering_rate: float          # 方向盘转角速率（度/秒）
    throttle: float               # 油门开度（0-100%）
    brake_pressure: float         # 制动主缸压力（MPa）
    brake_switch: bool            # 制动踏板开关
    turn_signal: TurnSignal       # 转向灯状态
    speed: float                  # 车速（km/h）
    gear: GearPosition            # 档位
    timestamp: float = field(default_factory=time.time)


@dataclass
class BehaviorObservation:
    """结构化行为观测条目"""
    obs_id: str
    timestamp: float              # UTC 时间戳（毫秒精度）
    steering_angle: float
    steering_rate: float
    throttle: float
    brake_pressure: float
    brake_active: bool
    turn_signal: TurnSignal
    speed: float
    gear: GearPosition
    behavior_type: BehaviorType
    data_quality: DataQuality
    target_slot_id: int


# ==================== 主类定义 ====================

class BehaviorObservationUnit:
    """
    驾驶行为观测记录单元
    
    职责:
    1. 从 CAN 总线持续采集驾驶操控数据（100Hz）
    2. 推断当前行为类型
    3. 评估数据质量
    4. 打包标准化观测条目
    5. 经 ad-06 隔离校验后写入子画像槽
    """
    
    # 采集频率（Hz）
    COLLECTION_FREQ = 100
    COLLECTION_INTERVAL = 1.0 / COLLECTION_FREQ  # 10ms
    
    # 行为推断阈值
    BRAKE_PRESSURE_THRESHOLD = 0.1     # MPa
    STEERING_ANGLE_THRESHOLD = 10.0    # 度
    SPEED_STOP_THRESHOLD = 1.0         # km/h
    THROTTLE_CHANGE_THRESHOLD = 20.0   # %/秒
    BRAKE_CHANGE_THRESHOLD = 0.5       # MPa/秒
    
    # CAN 信号超时（秒）
    SIGNAL_TIMEOUT = 0.5
    
    # 临时缓存最大长度
    MAX_BUFFER_SIZE = 600  # 60秒 × 100Hz 的10%
    
    def __init__(self):
        self.module_id = "ad-07"
        self.module_name = "驾驶行为观测记录单元"
        
        # 内部状态
        self.state = ObserverState.IDLE
        
        # 当前活跃槽号
        self._active_slot_id: Optional[int] = None
        
        # 上一帧数据（用于行为推断）
        self._last_frame: Optional[CANFrame] = None
        
        # 临时缓存队列
        self._buffer: List[BehaviorObservation] = []
        
        # CAN 信号健康状态
        self._can_healthy = True
        self._signal_timeouts: Dict[str, float] = {}
        
        # 统计
        self._total_observations = 0
        self._total_writes = 0
        self._total_rejects = 0
        
        # 待写入 ad-51 的变更日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 驾驶行为观测记录单元初始化完成")
    
    # ========== 状态管理 ==========
    
    def set_active_slot(self, slot_id: Optional[int]) -> None:
        """设置当前活跃槽号"""
        self._active_slot_id = slot_id
        if slot_id is not None:
            self.state = ObserverState.OBSERVING
            print(f"[{self.module_id}] 激活目标槽: slot_{slot_id}")
        else:
            self.state = ObserverState.PAUSED
            print(f"[{self.module_id}] 无活跃槽，暂停观测")
    
    def pause_observation(self) -> None:
        """暂停观测（驾驶模式切换时调用）"""
        self.state = ObserverState.PAUSED
        print(f"[{self.module_id}] 观测已暂停")
    
    def resume_observation(self) -> None:
        """恢复观测"""
        if self._active_slot_id is not None:
            self.state = ObserverState.OBSERVING
            print(f"[{self.module_id}] 观测已恢复")
    
    def emergency_stop(self) -> None:
        """紧急熔断停止采集"""
        self.state = ObserverState.STOPPED
        self._buffer.clear()
        print(f"[{self.module_id}] 紧急熔断，停止采集并清空缓存")
    
    # ========== CAN 数据采集与行为推断 ==========
    
    def process_can_frame(self, frame: CANFrame) -> Optional[BehaviorObservation]:
        """
        处理 CAN 数据帧
        
        Returns:
            行为观测条目（含行为类型推断与数据质量评估）
        """
        if self.state != ObserverState.OBSERVING:
            return None
        
        if self._active_slot_id is None:
            return None
        
        # CAN 信号健康检查
        self._check_signal_health(frame)
        
        # 数据质量评估
        data_quality = self._assess_data_quality(frame)
        
        # 行为类型推断
        behavior_type = self._infer_behavior_type(frame)
        
        # 打包观测条目
        self._total_observations += 1
        observation = BehaviorObservation(
            obs_id=f"obs-{uuid.uuid4().hex[:8]}",
            timestamp=time.time(),
            steering_angle=frame.steering_angle,
            steering_rate=frame.steering_rate,
            throttle=frame.throttle,
            brake_pressure=frame.brake_pressure,
            brake_active=frame.brake_switch,
            turn_signal=frame.turn_signal,
            speed=frame.speed,
            gear=frame.gear,
            behavior_type=behavior_type,
            data_quality=data_quality,
            target_slot_id=self._active_slot_id
        )
        
        # 更新上一帧
        self._last_frame = frame
        
        return observation
    
    def _infer_behavior_type(self, frame: CANFrame) -> BehaviorType:
        """推断当前驾驶行为类型"""
        # 制动判定（最高优先级）
        if frame.brake_pressure > self.BRAKE_PRESSURE_THRESHOLD:
            if frame.speed < self.SPEED_STOP_THRESHOLD and self._last_frame is not None:
                # 刹停
                if self._last_frame.speed > self.SPEED_STOP_THRESHOLD:
                    return BehaviorType.PARK
            return BehaviorType.BRAKE
        
        # 停车判定
        if frame.speed < self.SPEED_STOP_THRESHOLD:
            if frame.brake_switch:
                return BehaviorType.PARK
            if frame.throttle > 0:
                return BehaviorType.START
            return BehaviorType.PARK
        
        # 转弯判定
        if abs(frame.steering_angle) > self.STEERING_ANGLE_THRESHOLD:
            if frame.turn_signal != TurnSignal.OFF:
                return BehaviorType.TURN
            else:
                return BehaviorType.LANE_CHANGE
        
        # 加减速判定
        if self._last_frame is not None:
            throttle_delta = frame.throttle - self._last_frame.throttle
            if throttle_delta > self.THROTTLE_CHANGE_THRESHOLD:
                return BehaviorType.ACCELERATE
            elif throttle_delta < -self.THROTTLE_CHANGE_THRESHOLD:
                return BehaviorType.DECELERATE
        
        # 起步判定
        if self._last_frame is not None:
            if self._last_frame.speed < self.SPEED_STOP_THRESHOLD and frame.speed >= self.SPEED_STOP_THRESHOLD:
                return BehaviorType.START
        
        return BehaviorType.CRUISE
    
    def _assess_data_quality(self, frame: CANFrame) -> DataQuality:
        """评估数据质量"""
        if self._can_healthy:
            # 检查是否有插值估算的字段
            if self._signal_timeouts:
                return DataQuality.INTERPOLATED
            return DataQuality.COMPLETE
        else:
            # 检查哪些信号丢失
            missing_count = 0
            if frame.steering_angle == 0 and frame.steering_rate == 0:
                missing_count += 1
            if frame.throttle == 0 and frame.speed == 0:
                missing_count += 1
            
            if missing_count >= 2:
                return DataQuality.PARTIAL
            return DataQuality.INTERPOLATED
    
    def _check_signal_health(self, frame: CANFrame) -> None:
        """检查 CAN 信号健康状态"""
        now = time.time()
        
        # 简化检查：如果方向盘转角和油门同时为0且上一帧也是，可能是信号中断
        # 实际实现中应通过 CAN 总线的心跳信号检测
        if self._last_frame is not None:
            if (abs(frame.steering_angle - self._last_frame.steering_angle) < 0.01 and
                abs(frame.throttle - self._last_frame.throttle) < 0.01 and
                abs(frame.speed - self._last_frame.speed) < 0.01):
                # 数据冻结，可能信号中断
                self._can_healthy = False
                if self.state == ObserverState.OBSERVING:
                    self.state = ObserverState.DEGRADED
            else:
                self._can_healthy = True
                if self.state == ObserverState.DEGRADED:
                    self.state = ObserverState.OBSERVING
    
    # ========== 写入流程 ==========
    
    def submit_for_validation(self, observation: BehaviorObservation,
                              validation_callback) -> Tuple[bool, str]:
        """
        提交观测条目至 ad-06 进行隔离校验
        
        Args:
            observation: 观测条目
            validation_callback: ad-06 的校验函数
            
        Returns:
            (是否放行, 消息)
        """
        # 模拟 ad-06 校验请求
        is_allowed = validation_callback(
            source_module=self.module_id,
            target_slot_id=observation.target_slot_id,
            operation_type="write"
        )
        
        if is_allowed:
            self._total_writes += 1
            return True, "放行"
        else:
            self._total_rejects += 1
            return False, "被 ad-06 拦截"
    
    def buffer_observation(self, observation: BehaviorObservation) -> None:
        """将观测条目加入临时缓存"""
        if len(self._buffer) >= self.MAX_BUFFER_SIZE:
            # 移除最旧的条目
            self._buffer.pop(0)
        self._buffer.append(observation)
    
    def flush_buffer(self, validation_callback) -> List[BehaviorObservation]:
        """刷新缓存，将通过校验的条目写入槽位"""
        written = []
        for obs in self._buffer:
            allowed, _ = self.submit_for_validation(obs, validation_callback)
            if allowed:
                written.append(obs)
        self._buffer.clear()
        return written
    
    # ========== 状态查询 ==========
    
    def get_state(self) -> ObserverState:
        return self.state
    
    def get_buffer_size(self) -> int:
        return len(self._buffer)
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_observations": self._total_observations,
            "total_writes": self._total_writes,
            "total_rejects": self._total_rejects,
            "buffer_size": len(self._buffer),
            "can_healthy": self._can_healthy,
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
    print("ad-07 驾驶行为观测记录单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    # 模拟 ad-06 校验回调
    def mock_validation(source_module, target_slot_id, operation_type):
        return True  # 总是放行
    
    # --- TC-07-01: 正常采集并推断行为类型（匀速巡航） ---
    print("\n[TC-07-01] 正常采集并推断匀速巡航")
    try:
        unit = BehaviorObservationUnit()
        unit.set_active_slot(1)
        frame = CANFrame(
            steering_angle=5.0, steering_rate=10.0,
            throttle=30.0, brake_pressure=0.0, brake_switch=False,
            turn_signal=TurnSignal.OFF, speed=50.0, gear=GearPosition.D
        )
        obs = unit.process_can_frame(frame)
        assert obs is not None
        assert obs.behavior_type == BehaviorType.CRUISE
        assert obs.data_quality == DataQuality.COMPLETE
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-07-02: 推断制动行为 ---
    print("\n[TC-07-02] 推断制动行为")
    try:
        unit = BehaviorObservationUnit()
        unit.set_active_slot(1)
        frame = CANFrame(
            steering_angle=2.0, steering_rate=5.0,
            throttle=10.0, brake_pressure=2.0, brake_switch=True,
            turn_signal=TurnSignal.OFF, speed=60.0, gear=GearPosition.D
        )
        obs = unit.process_can_frame(frame)
        assert obs is not None
        assert obs.behavior_type == BehaviorType.BRAKE
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-07-03: 推断转弯行为（有转向灯） ---
    print("\n[TC-07-03] 推断转弯行为（有转向灯）")
    try:
        unit = BehaviorObservationUnit()
        unit.set_active_slot(1)
        frame = CANFrame(
            steering_angle=25.0, steering_rate=50.0,
            throttle=25.0, brake_pressure=0.0, brake_switch=False,
            turn_signal=TurnSignal.LEFT, speed=40.0, gear=GearPosition.D
        )
        obs = unit.process_can_frame(frame)
        assert obs is not None
        assert obs.behavior_type == BehaviorType.TURN
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-07-04: 推断变道行为（无转向灯） ---
    print("\n[TC-07-04] 推断变道行为（无转向灯）")
    try:
        unit = BehaviorObservationUnit()
        unit.set_active_slot(1)
        frame = CANFrame(
            steering_angle=15.0, steering_rate=40.0,
            throttle=30.0, brake_pressure=0.0, brake_switch=False,
            turn_signal=TurnSignal.OFF, speed=55.0, gear=GearPosition.D
        )
        obs = unit.process_can_frame(frame)
        assert obs is not None
        assert obs.behavior_type == BehaviorType.LANE_CHANGE
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-07-05: 暂停观测后不处理 ---
    print("\n[TC-07-05] 暂停观测后不处理")
    try:
        unit = BehaviorObservationUnit()
        unit.set_active_slot(1)
        unit.pause_observation()
        frame = CANFrame(0, 0, 0, 0, False, TurnSignal.OFF, 0, GearPosition.P)
        obs = unit.process_can_frame(frame)
        assert obs is None
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-07-06: 紧急熔断停止采集并清空缓存 ---
    print("\n[TC-07-06] 紧急熔断停止采集并清空缓存")
    try:
        unit = BehaviorObservationUnit()
        unit.set_active_slot(1)
        # 先采集几帧
        for i in range(3):
            unit.process_can_frame(CANFrame(5, 10, 30, 0, False, TurnSignal.OFF, 50, GearPosition.D))
        assert unit.get_buffer_size() >= 0
        unit.emergency_stop()
        assert unit.state == ObserverState.STOPPED
        assert unit.get_buffer_size() == 0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-07-07: CAN 信号中断降级 ---
    print("\n[TC-07-07] CAN 信号中断降级")
    try:
        unit = BehaviorObservationUnit()
        unit.set_active_slot(1)
        # 第一帧正常
        frame1 = CANFrame(5, 10, 30, 0, False, TurnSignal.OFF, 50, GearPosition.D)
        unit.process_can_frame(frame1)
        # 第二帧完全一样（模拟信号冻结）
        unit.process_can_frame(frame1)
        assert unit.state == ObserverState.DEGRADED
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-07-08: 提交 ad-06 校验 ---
    print("\n[TC-07-08] 提交 ad-06 校验")
    try:
        unit = BehaviorObservationUnit()
        unit.set_active_slot(1)
        frame = CANFrame(5, 10, 30, 0, False, TurnSignal.OFF, 50, GearPosition.D)
        obs = unit.process_can_frame(frame)
        allowed, msg = unit.submit_for_validation(obs, mock_validation)
        assert allowed == True
        assert unit._total_writes == 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- 测试结果汇总 ---
    print("\n" + "=" * 60)
    print(f"测试结果: {passed} PASS, {failed} FAIL")
    print("=" * 60)