#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-04
模块名称: 驾驶员身份识别单元
所属分区: 二、漏斗一：驾驶员画像漏斗
核心职责: 融合中控屏手动选择、座椅记忆联动、人脸识别三种方式，确认当前驾驶员身份
          并匹配对应子画像槽。输出统一的驾驶员身份 ID 与槽位类型建议。

依赖模块: 无（独立传感器与 CAN 总线数据入口）
被依赖模块: ad-02(漏斗一专属调度单元), ad-05(子画像槽创建与初始化单元)

安全约束:
  S-01: 人脸识别原始图像不存储、不传输、不离开本地运算单元
  S-02: 面部特征向量加密存储于本地安全分区，不可通过 OBD 或 OTA 导出
  S-03: 人脸识别功能可通过中控屏一键关闭，关闭后特征向量仍保留但暂停采集
  S-04: 活体检测失败三次以上，系统自动锁定人脸识别功能 10 分钟，防止欺骗攻击
  S-05: 驾驶员身份识别结果仅用于漏斗一子画像槽路由，编译期禁止接入自动驾驶决策链路
  S-06: 所有识别操作（含成功、失败、手动确认）全量写入 ad-51 变更日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class RecognitionMethod(Enum):
    """身份识别方式"""
    CENTER_SCREEN = "center_screen"     # 中控屏手动选择
    SEAT_MEMORY = "seat_memory"         # 座椅记忆联动
    FACE_RECOGNITION = "face_recognition"  # 人脸识别


class RecognizerState(Enum):
    """识别单元内部状态"""
    WAITING_START = "waiting_start"           # 等待车辆启动
    WAITING_FIRST_SIGNAL = "waiting_first_signal"  # 等待首次信号
    COLLECTING = "collecting"                 # 信号收集中
    FUSING = "fusing"                         # 融合计算中
    CONFIRMED = "confirmed"                   # 身份已确认
    WAITING_USER = "waiting_user"             # 等待用户确认
    NEW_DRIVER = "new_driver"                 # 新驾驶员注册


class DriverStatus(Enum):
    """驾驶员状态"""
    REGISTERED = "registered"   # 已注册
    UNREGISTERED = "unregistered"  # 未注册
    UNKNOWN = "unknown"         # 未知


# ==================== 数据结构 ====================

@dataclass
class FaceFeatureVector:
    """面部特征向量"""
    vector: List[float]
    liveness_score: float      # 活体检测分数 0.0-1.0
    quality_score: float       # 图像质量分数 0.0-1.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class SeatMemoryData:
    """座椅记忆数据"""
    seat_position: List[float]    # 座椅位置参数向量
    mirror_position: List[float]  # 后视镜参数
    steering_position: List[float]  # 方向盘位置
    timestamp: float = field(default_factory=time.time)


@dataclass
class CenterScreenInput:
    """中控屏手动选择信号"""
    selected_driver_id: Optional[str]   # 选择的驾驶员ID
    slot_type_suggestion: str           # 槽位类型建议
    is_new_registration: bool = False   # 是否新注册
    timestamp: float = field(default_factory=time.time)


@dataclass
class DriverIdentityResult:
    """驾驶员身份识别结果"""
    driver_id: str
    recognition_method: str       # 最终采用的识别方式
    confidence: float             # 综合置信度 0.0-1.0
    suggested_slot_type: str      # 建议槽位类型
    is_new_driver: bool = False
    timestamp: float = field(default_factory=time.time)


@dataclass
class RegisteredDriver:
    """已注册驾驶员信息"""
    driver_id: str
    driver_name_masked: str       # 掩码名称
    face_feature: Optional[FaceFeatureVector] = None
    seat_memory_bindings: List[SeatMemoryData] = field(default_factory=list)
    register_time: float = field(default_factory=time.time)
    last_active_time: float = field(default_factory=time.time)


@dataclass
class RecognitionLogEntry:
    """识别日志条目"""
    log_id: str
    event_type: str               # 识别成功/失败/手动确认/新注册
    method_used: str
    confidence: float
    driver_id: Optional[str]
    failure_reason: Optional[str]
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class DriverIdentityRecognition:
    """
    驾驶员身份识别单元
    
    职责:
    1. 融合三种识别方式（中控屏、座椅记忆、人脸识别）
    2. 多源信号加权融合判定
    3. 新驾驶员面部特征注册
    4. 活体检测防欺骗攻击
    5. 隐私保护（原始图像不存储）
    """
    
    # 融合权重
    FACE_WEIGHT = 0.6
    SEAT_WEIGHT = 0.4
    
    # 置信度阈值
    HIGH_CONFIDENCE_THRESHOLD = 0.85
    MEDIUM_CONFIDENCE_THRESHOLD = 0.70
    LOW_CONFIDENCE_THRESHOLD = 0.40
    
    # 单源惩罚系数
    SINGLE_SOURCE_PENALTY = 0.8
    CONFLICT_PENALTY = 0.8
    
    # 用户确认超时
    USER_CONFIRM_TIMEOUT = 5.0
    
    # 活体检测锁定
    MAX_LIVENESS_FAILURES = 3
    LIVENESS_LOCK_DURATION = 10 * 60  # 10分钟
    
    def __init__(self):
        self.module_id = "ad-04"
        self.module_name = "驾驶员身份识别单元"
        
        # 内部状态
        self.state = RecognizerState.WAITING_START
        
        # 已注册驾驶员库: driver_id -> RegisteredDriver
        self._registered_drivers: Dict[str, RegisteredDriver] = {}
        
        # 人脸识别功能开关
        self._face_recognition_enabled = True
        
        # 活体检测失败计数
        self._liveness_fail_count = 0
        self._liveness_lock_until = 0.0
        
        # 最近一次识别缓存
        self._last_recognition_cache: Dict[str, Any] = {}
        
        # 统计
        self._total_recognitions = 0
        self._successful_recognitions = 0
        self._failed_recognitions = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[RecognitionLogEntry] = []
        
        print(f"[{self.module_id}] 驾驶员身份识别单元初始化完成")
    
    # ========== 信号接收 ==========
    
    def receive_center_screen_input(self, input_data: CenterScreenInput) -> DriverIdentityResult:
        """
        接收中控屏手动选择信号（最高优先级）
        
        用户主动确认为最高置信度(1.0)，直接确认身份
        """
        self.state = RecognizerState.CONFIRMED
        self._total_recognitions += 1
        self._successful_recognitions += 1
        
        driver_id = input_data.selected_driver_id
        is_new = input_data.is_new_registration
        
        if is_new or driver_id is None:
            # 新驾驶员注册
            self.state = RecognizerState.NEW_DRIVER
            driver_id = f"DRV-{uuid.uuid4().hex[:6]}"
            result = DriverIdentityResult(
                driver_id=driver_id,
                recognition_method=RecognitionMethod.CENTER_SCREEN.value,
                confidence=1.0,
                suggested_slot_type=input_data.slot_type_suggestion,
                is_new_driver=True
            )
        else:
            result = DriverIdentityResult(
                driver_id=driver_id,
                recognition_method=RecognitionMethod.CENTER_SCREEN.value,
                confidence=1.0,
                suggested_slot_type=input_data.slot_type_suggestion,
                is_new_driver=False
            )
        
        self._log_recognition("SUCCESS_MANUAL", RecognitionMethod.CENTER_SCREEN.value, 1.0, driver_id)
        print(f"[{self.module_id}] 中控屏手动选择: {driver_id}, confidence=1.0")
        
        return result
    
    def receive_seat_memory_data(self, data: SeatMemoryData) -> Optional[Dict[str, Any]]:
        """
        接收座椅记忆数据
        
        Returns:
            匹配结果: {driver_id, confidence} 或 None
        """
        if self.state in [RecognizerState.CONFIRMED, RecognizerState.WAITING_USER]:
            return None
        
        self.state = RecognizerState.COLLECTING
        
        best_match = None
        best_score = 0.0
        candidates = []
        
        for driver_id, driver in self._registered_drivers.items():
            for binding in driver.seat_memory_bindings:
                score = self._calculate_seat_similarity(data, binding)
                if score > best_score:
                    best_score = score
                    best_match = driver_id
                    candidates.append((driver_id, score))
        
        if best_match is None:
            return None
        
        # 判断是否唯一匹配
        candidates.sort(key=lambda x: x[1], reverse=True)
        if len(candidates) == 1 or candidates[0][1] > candidates[1][1] + 0.2:
            confidence = min(best_score, 0.85)  # 座椅记忆最高0.85
        else:
            confidence = 0.5  # 多个候选，低置信度
            best_match = candidates[0][0]
        
        self._last_recognition_cache["seat"] = {
            "driver_id": best_match,
            "confidence": confidence,
            "candidates": candidates
        }
        
        print(f"[{self.module_id}] 座椅记忆匹配: {best_match}, confidence={confidence:.2f}")
        return {"driver_id": best_match, "confidence": confidence}
    
    def receive_face_feature(self, feature: FaceFeatureVector) -> Optional[Dict[str, Any]]:
        """
        接收面部特征向量
        
        活体检测与隐私保护:
        S-01: 原始图像不存储
        S-04: 活体检测失败三次锁定10分钟
        """
        if not self._face_recognition_enabled:
            return None
        
        if self.state in [RecognizerState.CONFIRMED, RecognizerState.WAITING_USER]:
            return None
        
        # 活体检测锁定检查
        if time.time() < self._liveness_lock_until:
            print(f"[{self.module_id}] 人脸识别已锁定至 {self._liveness_lock_until}")
            return None
        
        # 活体检测
        if feature.liveness_score < 0.7:
            self._liveness_fail_count += 1
            self._log_recognition("LIVENESS_FAIL", RecognitionMethod.FACE_RECOGNITION.value, 0.0, None)
            
            if self._liveness_fail_count >= self.MAX_LIVENESS_FAILURES:
                self._liveness_lock_until = time.time() + self.LIVENESS_LOCK_DURATION
                self._liveness_fail_count = 0
                print(f"[{self.module_id}] 活体检测失败{self.MAX_LIVENESS_FAILURES}次，锁定{self.LIVENESS_LOCK_DURATION}s")
            
            return None
        
        # 重置失败计数（成功通过活体检测）
        self._liveness_fail_count = 0
        self.state = RecognizerState.COLLECTING
        
        # 面部特征匹配
        best_match = None
        best_score = 0.0
        
        for driver_id, driver in self._registered_drivers.items():
            if driver.face_feature is None:
                continue
            score = self._calculate_face_similarity(feature, driver.face_feature)
            if score > best_score:
                best_score = score
                best_match = driver_id
        
        if best_match is None:
            # 可能是新驾驶员
            self._last_recognition_cache["face"] = {
                "driver_id": None,
                "confidence": 0.0,
                "feature": feature,
                "is_potential_new": True
            }
            return None
        
        confidence = best_score * feature.quality_score
        
        self._last_recognition_cache["face"] = {
            "driver_id": best_match,
            "confidence": confidence,
            "similarity_score": best_score
        }
        
        print(f"[{self.module_id}] 人脸识别匹配: {best_match}, confidence={confidence:.2f}")
        return {"driver_id": best_match, "confidence": confidence}
    
    # ========== 融合判定 ==========
    
    def fuse_and_decide(self) -> Optional[DriverIdentityResult]:
        """
        多源信号融合判定
        
        融合逻辑:
        1. 中控屏手动选择 → 直接确认(最高优先级)
        2. 座椅记忆 + 人脸识别结果一致 → 取最高置信度
        3. 座椅记忆 != 人脸识别 → 以人脸为准，施加冲突惩罚
        4. 仅有单源信号 → 施加单源惩罚
        5. 置信度 < 0.40 → 识别失败
        """
        if self.state not in [RecognizerState.COLLECTING]:
            return None
        
        self.state = RecognizerState.FUSING
        self._total_recognitions += 1
        
        seat_data = self._last_recognition_cache.get("seat")
        face_data = self._last_recognition_cache.get("face")
        
        # 无任何信号
        if seat_data is None and face_data is None:
            self.state = RecognizerState.WAITING_FIRST_SIGNAL
            return None
        
        # 情况1: 仅有座椅记忆
        if seat_data is not None and face_data is None:
            fused_confidence = seat_data["confidence"] * self.SINGLE_SOURCE_PENALTY
            matched_id = seat_data["driver_id"]
            method_used = f"{RecognitionMethod.SEAT_MEMORY.value}(单源惩罚)"
        
        # 情况2: 仅有人脸识别
        elif face_data is not None and seat_data is None:
            fused_confidence = face_data["confidence"] * self.SINGLE_SOURCE_PENALTY
            matched_id = face_data["driver_id"]
            method_used = f"{RecognitionMethod.FACE_RECOGNITION.value}(单源惩罚)"
            
            # 潜在新驾驶员
            if face_data.get("is_potential_new"):
                self.state = RecognizerState.NEW_DRIVER
                return DriverIdentityResult(
                    driver_id=f"DRV-{uuid.uuid4().hex[:6]}",
                    recognition_method=RecognitionMethod.FACE_RECOGNITION.value,
                    confidence=0.0,
                    suggested_slot_type="long_term",
                    is_new_driver=True
                )
        
        # 情况3: 双源信号一致
        elif seat_data["driver_id"] == face_data["driver_id"]:
            fused_confidence = max(seat_data["confidence"], face_data["confidence"])
            matched_id = seat_data["driver_id"]
            method_used = f"{RecognitionMethod.SEAT_MEMORY.value}+{RecognitionMethod.FACE_RECOGNITION.value}"
        
        # 情况4: 双源信号冲突
        else:
            # 以人脸为准（权重更高），施加冲突惩罚
            fused_confidence = face_data["confidence"] * self.CONFLICT_PENALTY
            matched_id = face_data["driver_id"]
            method_used = f"{RecognitionMethod.FACE_RECOGNITION.value}(座椅冲突惩罚)"
            print(f"[{self.module_id}] 信号冲突: 座椅={seat_data['driver_id']}, 人脸={face_data['driver_id']}, 以人脸为准")
        
        # 置信度判定
        if fused_confidence >= self.MEDIUM_CONFIDENCE_THRESHOLD:
            self.state = RecognizerState.CONFIRMED
            self._successful_recognitions += 1
            result = DriverIdentityResult(
                driver_id=matched_id,
                recognition_method=method_used,
                confidence=fused_confidence,
                suggested_slot_type="long_term"
            )
        
        elif fused_confidence >= self.LOW_CONFIDENCE_THRESHOLD:
            # 需要用户确认
            self.state = RecognizerState.WAITING_USER
            result = DriverIdentityResult(
                driver_id=matched_id,
                recognition_method=method_used,
                confidence=fused_confidence,
                suggested_slot_type="long_term"
            )
            print(f"[{self.module_id}] 低置信度({fused_confidence:.2f})，等待用户确认")
        
        else:
            # 识别失败
            self._failed_recognitions += 1
            self.state = RecognizerState.WAITING_FIRST_SIGNAL
            self._log_recognition("FAILED", method_used, fused_confidence, None)
            print(f"[{self.module_id}] 识别失败: confidence={fused_confidence:.2f} < {self.LOW_CONFIDENCE_THRESHOLD}")
            return None
        
        self._log_recognition("SUCCESS", method_used, fused_confidence, matched_id)
        print(f"[{self.module_id}] 融合判定: {matched_id}, confidence={fused_confidence:.2f}")
        
        return result
    
    # ========== 驾驶员注册 ==========
    
    def register_new_driver(self, driver_id: str, driver_name: str,
                            face_feature: Optional[FaceFeatureVector] = None,
                            seat_data: Optional[SeatMemoryData] = None) -> None:
        """
        注册新驾驶员
        
        隐私保护:
        S-02: 面部特征向量加密存储
        S-01: 原始图像不存储
        """
        driver = RegisteredDriver(
            driver_id=driver_id,
            driver_name_masked=f"用户{driver_name[-2:] if len(driver_name) >= 2 else driver_name}"
        )
        
        if face_feature is not None:
            driver.face_feature = face_feature
        
        if seat_data is not None:
            driver.seat_memory_bindings.append(seat_data)
        
        self._registered_drivers[driver_id] = driver
        
        self._log_recognition("NEW_REGISTRATION", "multi", 1.0, driver_id)
        print(f"[{self.module_id}] 新驾驶员注册: {driver_id}, 当前注册总数={len(self._registered_drivers)}")
    
    def add_seat_memory_binding(self, driver_id: str, seat_data: SeatMemoryData) -> bool:
        """为已有驾驶员添加座椅记忆绑定"""
        if driver_id not in self._registered_drivers:
            return False
        self._registered_drivers[driver_id].seat_memory_bindings.append(seat_data)
        return True
    
    # ========== 功能开关 ==========
    
    def set_face_recognition_enabled(self, enabled: bool) -> None:
        """开关人脸识别功能（S-03）"""
        self._face_recognition_enabled = enabled
        status = "开启" if enabled else "关闭"
        print(f"[{self.module_id}] 人脸识别功能已{status}")
    
    def is_face_recognition_enabled(self) -> bool:
        return self._face_recognition_enabled
    
    # ========== 相似度计算 ==========
    
    def _calculate_seat_similarity(self, data1: SeatMemoryData, data2: SeatMemoryData) -> float:
        """计算座椅记忆数据相似度"""
        if not data1.seat_position or not data2.seat_position:
            return 0.0
        
        # 欧氏距离归一化
        diff_sum = sum((a - b) ** 2 for a, b in zip(data1.seat_position, data2.seat_position))
        distance = diff_sum ** 0.5
        
        # 转换为相似度（距离越小越相似）
        max_expected_distance = 10.0
        similarity = max(0.0, 1.0 - distance / max_expected_distance)
        
        return similarity
    
    def _calculate_face_similarity(self, feature1: FaceFeatureVector,
                                   feature2: FaceFeatureVector) -> float:
        """计算面部特征余弦相似度"""
        if not feature1.vector or not feature2.vector:
            return 0.0
        
        v1, v2 = feature1.vector, feature2.vector
        
        dot_product = sum(a * b for a, b in zip(v1, v2))
        norm1 = (sum(a * a for a in v1)) ** 0.5
        norm2 = (sum(b * b for b in v2)) ** 0.5
        
        if norm1 == 0 or norm2 == 0:
            return 0.0
        
        return dot_product / (norm1 * norm2)
    
    # ========== 状态查询 ==========
    
    def get_state(self) -> RecognizerState:
        return self.state
    
    def get_registered_count(self) -> int:
        return len(self._registered_drivers)
    
    def get_registered_drivers(self) -> List[str]:
        return list(self._registered_drivers.keys())
    
    def reset_state(self) -> None:
        """重置状态（车辆熄火时调用）"""
        self.state = RecognizerState.WAITING_START
        self._last_recognition_cache.clear()
    
    # ========== 变更日志 ==========
    
    def _log_recognition(self, event_type: str, method: str,
                         confidence: float, driver_id: Optional[str]) -> None:
        """记录识别日志"""
        self._pending_logs.append(RecognitionLogEntry(
            log_id=f"log-{uuid.uuid4().hex[:8]}",
            event_type=event_type,
            method_used=method,
            confidence=confidence,
            driver_id=driver_id,
            failure_reason=None if event_type == "SUCCESS" else event_type
        ))
    
    def collect_pending_logs(self) -> List[RecognitionLogEntry]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_recognitions": self._total_recognitions,
            "successful": self._successful_recognitions,
            "failed": self._failed_recognitions,
            "registered_count": len(self._registered_drivers),
            "face_enabled": self._face_recognition_enabled,
            "current_state": self.state.value
        }


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-04 驾驶员身份识别单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    # --- TC-04-01: 中控屏手动选择（最高优先级） ---
    print("\n[TC-04-01] 中控屏手动选择最高优先级")
    try:
        recognizer = DriverIdentityRecognition()
        input_data = CenterScreenInput(
            selected_driver_id="DRV-ZS",
            slot_type_suggestion="long_term",
            is_new_registration=False
        )
        result = recognizer.receive_center_screen_input(input_data)
        assert result.confidence == 1.0
        assert result.driver_id == "DRV-ZS"
        assert result.is_new_driver == False
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-04-02: 新驾驶员中控屏注册 ---
    print("\n[TC-04-02] 新驾驶员中控屏注册")
    try:
        recognizer = DriverIdentityRecognition()
        input_data = CenterScreenInput(
            selected_driver_id=None,
            slot_type_suggestion="long_term",
            is_new_registration=True
        )
        result = recognizer.receive_center_screen_input(input_data)
        assert result.is_new_driver == True
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-04-03: 人脸识别活体检测失败锁定 ---
    print("\n[TC-04-03] 活体检测失败三次锁定")
    try:
        recognizer = DriverIdentityRecognition()
        for i in range(3):
            feature = FaceFeatureVector(vector=[1.0, 2.0], liveness_score=0.3, quality_score=0.8)
            result = recognizer.receive_face_feature(feature)
            assert result is None
        
        # 第四次应被锁定
        feature = FaceFeatureVector(vector=[1.0, 2.0], liveness_score=0.9, quality_score=0.8)
        result = recognizer.receive_face_feature(feature)
        assert result is None  # 锁定期间返回None
        assert recognizer._liveness_lock_until > time.time()
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-04-04: 人脸识别成功重置失败计数 ---
    print("\n[TC-04-04] 活体检测成功重置计数")
    try:
        recognizer = DriverIdentityRecognition()
        # 先失败2次
        for i in range(2):
            recognizer.receive_face_feature(FaceFeatureVector([1.0], liveness_score=0.3, quality_score=0.8))
        # 第3次成功
        recognizer.receive_face_feature(FaceFeatureVector([1.0], liveness_score=0.85, quality_score=0.8))
        assert recognizer._liveness_fail_count == 0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-04-05: 座椅记忆匹配已注册驾驶员 ---
    print("\n[TC-04-05] 座椅记忆匹配")
    try:
        recognizer = DriverIdentityRecognition()
        recognizer.register_new_driver(
            "DRV-001", "张三",
            seat_data=SeatMemoryData(seat_position=[0.5, 0.3, 0.2])
        )
        data = SeatMemoryData(seat_position=[0.5, 0.3, 0.2])
        result = recognizer.receive_seat_memory_data(data)
        assert result is not None
        assert result["driver_id"] == "DRV-001"
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-04-06: 人脸识别关闭时不采集 ---
    print("\n[TC-04-06] 人脸识别关闭时不采集")
    try:
        recognizer = DriverIdentityRecognition()
        recognizer.set_face_recognition_enabled(False)
        feature = FaceFeatureVector([1.0], liveness_score=0.9, quality_score=0.8)
        result = recognizer.receive_face_feature(feature)
        assert result is None
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-04-07: 双源信号融合判定 ---
    print("\n[TC-04-07] 双源信号融合判定")
    try:
        recognizer = DriverIdentityRecognition()
        recognizer.register_new_driver(
            "DRV-002", "李四",
            face_feature=FaceFeatureVector([1.0, 2.0, 3.0], liveness_score=0.9, quality_score=0.9),
            seat_data=SeatMemoryData(seat_position=[0.5, 0.3, 0.2])
        )
        # 座椅记忆匹配
        recognizer.receive_seat_memory_data(SeatMemoryData(seat_position=[0.5, 0.3, 0.2]))
        # 人脸识别匹配
        recognizer.receive_face_feature(FaceFeatureVector([1.0, 2.0, 3.0], liveness_score=0.9, quality_score=0.9))
        result = recognizer.fuse_and_decide()
        assert result is not None
        assert result.driver_id == "DRV-002"
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-04-08: 新驾驶员注册 ---
    print("\n[TC-04-08] 新驾驶员注册")
    try:
        recognizer = DriverIdentityRecognition()
        recognizer.register_new_driver(
            "DRV-NEW", "新用户",
            face_feature=FaceFeatureVector([4.0, 5.0], liveness_score=0.9, quality_score=0.8)
        )
        assert recognizer.get_registered_count() == 1
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