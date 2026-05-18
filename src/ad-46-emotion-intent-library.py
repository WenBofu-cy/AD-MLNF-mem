#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-46
模块名称: 道路参与者情绪意图感知库
所属分区: 四、漏斗外挂扩展区（物理隔离）
核心职责: 对接外部情绪感知与意图推断模块，提供所有路上可见人员（四轮驾驶员、摩托车骑手、
          电单车骑手、自行车骑手、行人）的实时情绪状态与行为意图推断结果的查询接口。
          独立于双漏斗记忆系统运行，不参与记忆的沉淀、筛选、晋升与遗忘机制。
          仅提供决策辅助参考，不作为安全关键决策的唯一依据。

依赖模块: 外部情绪感知模块（摄像头面部表情/姿态分析）、外部意图推断引擎（行为轨迹预测）
被依赖模块: ECC-03 因果推理模块（查询情绪意图以辅助预判）、
            ECC-05 伦理仲裁模块（查询情绪意图以辅助风险评估）、
            ad-08 上下文场景标记单元（查询行人情绪以辅助应急标记）、
            ad-09 行为判定标签单元（查询驾驶员情绪以辅助行为判定）

安全约束:
  S-01: 情绪意图数据独立于双漏斗记忆系统，不参与记忆的沉淀、筛选、晋升与遗忘机制
  S-02: 情绪感知原始数据（面部表情特征向量、语音特征、姿态特征）为只读参考，不本地落盘
  S-03: 情绪意图数据仅作为决策辅助参考，不可作为安全关键决策的唯一依据
  S-04: 行人/非机动车情绪查询结果不得包含任何可用于身份识别的生物特征数据
  S-05: 本模块不得主动采集或存储任何车内驾驶员/乘客的情绪数据
  S-06: 外部感知模块不可用时，进入降级模式，仅提供基于目标类别的保守推断
  S-07: 所有高风险目标（TTC < 3s）的情绪意图评估报告全量写入 ad-51 不可变日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class EmotionState(Enum):
    """情绪状态标签"""
    # 行人
    CALM = "镇定从容"
    ANXIOUS = "慌张急躁"
    HESITANT = "犹豫徘徊"
    DISTRACTED = "注意力涣散"
    ANGRY = "暴怒冲动"
    ELDERLY = "老人特殊状态"
    CHILD = "儿童特殊状态"
    # 非机动车
    NORMAL_RIDE = "正常骑行"
    FATIGUED = "疲劳/注意力下降"
    RUSHING = "焦急赶路"
    STARTLED = "受惊/恐慌"
    VIOLATION_PRONE = "违规倾向"
    # 机动车驾驶员
    NORMAL_DRIVE = "正常驾驶"
    ROAD_RAGE = "路怒倾向"
    INDECISIVE = "犹豫不决"
    FATIGUE_DRIVE = "疲劳驾驶"
    FRIENDLY_YIELD = "友好让行"
    # 摩托车
    NORMAL_MOTO = "正常骑行"
    AGGRESSIVE_MOTO = "激进驾驶"
    UNSTABLE_MOTO = "受惊/不稳"


class IntentLabel(Enum):
    """行为意图标签"""
    WILLING_TO_YIELD = "可能让行"
    MAY_CROSS = "可能横穿"
    MAY_SWERVE = "可能偏离车道"
    MAY_STOP = "可能突然停车"
    MAY_RUN_RED = "可能闯红灯"
    MAY_CUT_IN = "可能强行加塞"
    UNCERTAIN = "行为不确定"


class LibraryState(Enum):
    """感知库内部状态"""
    NORMAL = "normal"
    QUERYING = "querying"
    UPDATING = "updating"
    DEGRADED = "degraded"
    PAUSED = "paused"


# ==================== 数据结构 ====================

@dataclass
class EmotionQueryRequest:
    """情绪意图查询请求"""
    query_id: str
    target_id: str
    target_class: str              # "第二类"/"第四类" 等
    source_module: str
    priority: str = "normal"       # "normal" / "high"
    ttc: Optional[float] = None    # 碰撞时间，用于高风险判定


@dataclass
class EmotionQueryResult:
    """情绪状态查询结果"""
    target_id: str
    emotion_state: EmotionState
    emotion_intensity: float       # 0.0-1.0
    perception_confidence: float   # 0.0-1.0
    data_age_ms: float             # 数据时效（毫秒）
    is_stale: bool = False         # 是否可能过时
    query_timestamp: float = field(default_factory=time.time)


@dataclass
class IntentQueryResult:
    """行为意图推断结果"""
    target_id: str
    intent_label: IntentLabel
    intent_confidence: float       # 0.0-1.0
    predicted_behavior: str        # 预测行为描述
    prediction_window_s: float = 3.0  # 预测时间窗口（秒）
    query_timestamp: float = field(default_factory=time.time)


@dataclass
class ComprehensiveAssessment:
    """综合情绪意图评估报告（高风险目标）"""
    target_id: str
    emotion_state: EmotionState
    intent_label: IntentLabel
    risk_suggestion: str           # 风险关联建议
    composite_confidence: float    # 综合置信度
    assessment_timestamp: float = field(default_factory=time.time)


@dataclass
class EmotionCacheEntry:
    """情绪缓存条目"""
    target_id: str
    emotion_state: Optional[EmotionState] = None
    emotion_intensity: float = 0.0
    perception_confidence: float = 0.0
    intent_label: Optional[IntentLabel] = None
    intent_confidence: float = 0.0
    predicted_trajectory: Optional[List[float]] = None
    update_timestamp: float = field(default_factory=time.time)


@dataclass
class DegradedNotification:
    """降级模式通知"""
    reason: str
    available_capabilities: List[str]
    suggested_alternatives: List[str]
    timestamp: float = field(default_factory=time.time)


# ==================== 情绪与意图标签映射 ====================

# 行人情绪-意图关联
PEDESTRIAN_EMOTION_INTENT_MAP: Dict[EmotionState, List[IntentLabel]] = {
    EmotionState.CALM: [IntentLabel.WILLING_TO_YIELD],
    EmotionState.ANXIOUS: [IntentLabel.MAY_CROSS],
    EmotionState.HESITANT: [IntentLabel.MAY_CROSS, IntentLabel.UNCERTAIN],
    EmotionState.DISTRACTED: [IntentLabel.MAY_CROSS, IntentLabel.UNCERTAIN],
    EmotionState.ANGRY: [IntentLabel.MAY_CROSS],
    EmotionState.ELDERLY: [IntentLabel.UNCERTAIN],
    EmotionState.CHILD: [IntentLabel.MAY_CROSS, IntentLabel.UNCERTAIN],
}

# 非机动车情绪-意图关联
CYCLIST_EMOTION_INTENT_MAP: Dict[EmotionState, List[IntentLabel]] = {
    EmotionState.NORMAL_RIDE: [IntentLabel.WILLING_TO_YIELD],
    EmotionState.FATIGUED: [IntentLabel.MAY_SWERVE],
    EmotionState.RUSHING: [IntentLabel.MAY_RUN_RED, IntentLabel.MAY_CUT_IN],
    EmotionState.STARTLED: [IntentLabel.MAY_SWERVE, IntentLabel.MAY_STOP],
    EmotionState.VIOLATION_PRONE: [IntentLabel.MAY_RUN_RED, IntentLabel.MAY_SWERVE],
}

# 机动车驾驶员情绪-意图关联
DRIVER_EMOTION_INTENT_MAP: Dict[EmotionState, List[IntentLabel]] = {
    EmotionState.NORMAL_DRIVE: [IntentLabel.WILLING_TO_YIELD],
    EmotionState.ROAD_RAGE: [IntentLabel.MAY_CUT_IN],
    EmotionState.INDECISIVE: [IntentLabel.MAY_STOP, IntentLabel.UNCERTAIN],
    EmotionState.FATIGUE_DRIVE: [IntentLabel.MAY_SWERVE, IntentLabel.MAY_STOP],
    EmotionState.FRIENDLY_YIELD: [IntentLabel.WILLING_TO_YIELD],
}

# 目标类别到情绪意图映射表的映射
TARGET_CLASS_EMOTION_MAP = {
    "第四类": PEDESTRIAN_EMOTION_INTENT_MAP,
    "非机动车": CYCLIST_EMOTION_INTENT_MAP,
    "第二类": DRIVER_EMOTION_INTENT_MAP,
}


# ==================== 主类定义 ====================

class EmotionIntentLibrary:
    """
    道路参与者情绪意图感知库 - 漏斗外挂扩展区
    
    职责:
    1. 接收外部情绪感知模块推送的原始数据并更新缓存
    2. 接收外部意图推断引擎推送的意图数据并更新缓存
    3. 提供情绪状态与行为意图查询接口
    4. 高风险目标（TTC < 3s）生成综合评估报告
    5. 外部感知模块不可用时自动降级
    6. 定期清理过期缓存（10秒）
    """
    
    # 授权查询的模块列表
    AUTHORIZED_QUERY_MODULES = {
        "ECC-03", "ECC-05", "ad-08", "ad-09"
    }
    
    # 禁止查询的模块（漏斗一）
    FORBIDDEN_MODULES = {
        "ad-02", "ad-04", "ad-05", "ad-06", "ad-07",
        "ad-10", "ad-11", "ad-13"
    }
    
    # 缓存有效期（毫秒）
    CACHE_VALIDITY_MS = 2000  # 2秒
    
    # 缓存最长保留时间（秒）
    MAX_CACHE_RETENTION_S = 10
    
    # 降级置信度阈值
    DEGRADE_CONFIDENCE_THRESHOLD = 0.5
    DEGRADE_CONSECUTIVE_COUNT = 15  # 连续 15 次低置信度 → 降级
    
    # 降级恢复阈值
    RECOVER_CONSECUTIVE_COUNT = 5
    
    # 高风险 TTC 阈值（秒）
    HIGH_RISK_TTC_THRESHOLD = 3.0
    
    def __init__(self):
        self.module_id = "ad-46"
        self.module_name = "道路参与者情绪意图感知库"
        
        # 内部状态
        self.state = LibraryState.NORMAL
        
        # 情绪意图缓存: target_id -> EmotionCacheEntry
        self._cache: Dict[str, EmotionCacheEntry] = {}
        
        # 降级检查计数器
        self._degrade_counter = 0
        self._recover_counter = 0
        
        # 统计
        self._total_queries = 0
        self._total_high_risk_assessments = 0
        self._total_degraded_periods = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 道路参与者情绪意图感知库初始化完成")
        print(f"[{self.module_id}] 缓存有效期: {self.CACHE_VALIDITY_MS}ms, 最长保留: {self.MAX_CACHE_RETENTION_S}s")
    
    # ========== 状态管理 ==========
    
    def pause(self) -> None:
        self.state = LibraryState.PAUSED
    
    def resume(self) -> None:
        self.state = LibraryState.NORMAL
    
    def get_state(self) -> LibraryState:
        return self.state
    
    # ========== 外部感知数据接收 ==========
    
    def update_emotion(self, target_id: str, emotion_state: EmotionState,
                       intensity: float, confidence: float) -> None:
        """接收外部情绪感知数据并更新缓存"""
        if self.state == LibraryState.PAUSED:
            return
        
        self.state = LibraryState.UPDATING
        
        if confidence < 0.3:
            self.state = LibraryState.NORMAL
            return
        
        if target_id not in self._cache:
            self._cache[target_id] = EmotionCacheEntry(target_id=target_id)
        
        entry = self._cache[target_id]
        entry.emotion_state = emotion_state
        entry.emotion_intensity = intensity
        entry.perception_confidence = confidence
        entry.update_timestamp = time.time()
        
        # 降级/恢复检测
        if confidence < self.DEGRADE_CONFIDENCE_THRESHOLD:
            self._degrade_counter += 1
            self._recover_counter = 0
            if self._degrade_counter >= self.DEGRADE_CONSECUTIVE_COUNT and self.state != LibraryState.DEGRADED:
                self._enter_degraded()
        else:
            self._recover_counter += 1
            self._degrade_counter = max(0, self._degrade_counter - 1)
            if self._recover_counter >= self.RECOVER_CONSECUTIVE_COUNT and self.state == LibraryState.DEGRADED:
                self._exit_degraded()
        
        self.state = LibraryState.NORMAL
    
    def update_intent(self, target_id: str, intent_label: IntentLabel,
                      confidence: float, trajectory: Optional[List[float]] = None) -> None:
        """接收外部意图推断数据并更新缓存"""
        if self.state == LibraryState.PAUSED:
            return
        
        self.state = LibraryState.UPDATING
        
        if confidence < 0.4:
            self.state = LibraryState.NORMAL
            return
        
        if target_id not in self._cache:
            self._cache[target_id] = EmotionCacheEntry(target_id=target_id)
        
        entry = self._cache[target_id]
        entry.intent_label = intent_label
        entry.intent_confidence = confidence
        if trajectory:
            entry.predicted_trajectory = trajectory
        entry.update_timestamp = time.time()
        
        self.state = LibraryState.NORMAL
    
    def _enter_degraded(self) -> None:
        """进入降级模式"""
        self.state = LibraryState.DEGRADED
        self._total_degraded_periods += 1
        notification = DegradedNotification(
            reason="外部感知模块连续低置信度",
            available_capabilities=["基于目标类别的保守推断"],
            suggested_alternatives=["依赖世界模型 TTC 和法规约束进行决策"]
        )
        print(f"[{self.module_id}] 进入降级模式: {notification.reason}")
    
    def _exit_degraded(self) -> None:
        """退出降级模式"""
        self.state = LibraryState.NORMAL
        self._degrade_counter = 0
        print(f"[{self.module_id}] 退出降级模式，感知质量恢复")
    
    # ========== 查询接口 ==========
    
    def query_emotion(self, request: EmotionQueryRequest) -> Optional[EmotionQueryResult]:
        """
        查询目标情绪状态
        
        Args:
            request: 查询请求
            
        Returns:
            情绪状态结果，或 None（拒绝查询）
        """
        self._total_queries += 1
        
        # 模块权限检查
        if request.source_module in self.FORBIDDEN_MODULES:
            print(f"[{self.module_id}] 拒绝查询: 漏斗一模块 {request.source_module}")
            return None
        
        if request.source_module not in self.AUTHORIZED_QUERY_MODULES:
            print(f"[{self.module_id}] 拒绝查询: 未授权模块 {request.source_module}")
            return None
        
        # 紧急熔断检查
        if self.state == LibraryState.PAUSED:
            return None
        
        self.state = LibraryState.QUERYING
        
        cache_entry = self._cache.get(request.target_id)
        
        # 降级模式或无缓存：返回保守推断
        if self.state == LibraryState.DEGRADED or cache_entry is None:
            result = self._conservative_inference(request.target_id, request.target_class)
            self.state = LibraryState.NORMAL
            return result
        
        # 检查数据时效
        now = time.time()
        data_age_ms = (now - cache_entry.update_timestamp) * 1000
        is_stale = data_age_ms > self.CACHE_VALIDITY_MS
        
        result = EmotionQueryResult(
            target_id=request.target_id,
            emotion_state=cache_entry.emotion_state or EmotionState.CALM,
            emotion_intensity=cache_entry.emotion_intensity,
            perception_confidence=cache_entry.perception_confidence,
            data_age_ms=data_age_ms,
            is_stale=is_stale
        )
        
        self.state = LibraryState.NORMAL
        return result
    
    def query_intent(self, request: EmotionQueryRequest) -> Optional[IntentQueryResult]:
        """
        查询目标行为意图
        
        Args:
            request: 查询请求
            
        Returns:
            意图推断结果，或 None
        """
        if request.source_module in self.FORBIDDEN_MODULES:
            return None
        
        self._total_queries += 1
        self.state = LibraryState.QUERYING
        
        cache_entry = self._cache.get(request.target_id)
        
        if self.state == LibraryState.DEGRADED or cache_entry is None:
            # 保守推断
            conservative_intent = IntentLabel.UNCERTAIN
            result = IntentQueryResult(
                target_id=request.target_id,
                intent_label=conservative_intent,
                intent_confidence=0.3,
                predicted_behavior="数据不足，行为不确定"
            )
            self.state = LibraryState.NORMAL
            return result
        
        # 如果意图数据缺失，根据情绪状态推断
        if cache_entry.intent_label is None and cache_entry.emotion_state is not None:
            intent_label = self._infer_intent_from_emotion(
                cache_entry.emotion_state, request.target_class
            )
        else:
            intent_label = cache_entry.intent_label or IntentLabel.UNCERTAIN
        
        result = IntentQueryResult(
            target_id=request.target_id,
            intent_label=intent_label,
            intent_confidence=cache_entry.intent_confidence if cache_entry.intent_label else 0.4,
            predicted_behavior=self._describe_intent(intent_label, request.target_class)
        )
        
        self.state = LibraryState.NORMAL
        return result
    
    def query_comprehensive(self, request: EmotionQueryRequest) -> Optional[ComprehensiveAssessment]:
        """
        高风险目标（TTC < 3s）综合评估
        
        S-07: 评估报告全量写入 ad-51
        
        Args:
            request: 查询请求（应标记为高优先级且 TTC < 3s）
            
        Returns:
            综合评估报告
        """
        if request.ttc is None or request.ttc >= self.HIGH_RISK_TTC_THRESHOLD:
            return None
        
        self._total_high_risk_assessments += 1
        
        emotion_result = self.query_emotion(request)
        intent_result = self.query_intent(request)
        
        if emotion_result is None or intent_result is None:
            return None
        
        # 生成风险建议
        risk_suggestion = self._generate_risk_suggestion(
            request.target_class,
            emotion_result.emotion_state,
            intent_result.intent_label,
            request.ttc
        )
        
        composite_confidence = min(
            emotion_result.perception_confidence,
            intent_result.intent_confidence
        )
        
        assessment = ComprehensiveAssessment(
            target_id=request.target_id,
            emotion_state=emotion_result.emotion_state,
            intent_label=intent_result.intent_label,
            risk_suggestion=risk_suggestion,
            composite_confidence=composite_confidence
        )
        
        # S-07: 高风险评估日志
        self._log_high_risk(assessment, request.ttc)
        
        return assessment
    
    # ========== 保守推断 ==========
    
    def _conservative_inference(self, target_id: str, target_class: str) -> EmotionQueryResult:
        """生成基于目标类别的保守推断"""
        # S-04: 不包含任何身份识别数据
        if "第四类" in target_class:
            emotion = EmotionState.CALM
            intensity = 0.3
        elif "非机动车" in target_class:
            emotion = EmotionState.NORMAL_RIDE
            intensity = 0.3
        elif "第二类" in target_class:
            emotion = EmotionState.NORMAL_DRIVE
            intensity = 0.3
        else:
            emotion = EmotionState.CALM
            intensity = 0.2
        
        return EmotionQueryResult(
            target_id=target_id,
            emotion_state=emotion,
            emotion_intensity=intensity,
            perception_confidence=0.2,
            data_age_ms=999999,
            is_stale=True
        )
    
    def _infer_intent_from_emotion(self, emotion: EmotionState, target_class: str) -> IntentLabel:
        """根据情绪状态推断意图"""
        intent_map = TARGET_CLASS_EMOTION_MAP.get(target_class, {})
        intents = intent_map.get(emotion, [IntentLabel.UNCERTAIN])
        return intents[0] if intents else IntentLabel.UNCERTAIN
    
    def _describe_intent(self, intent: IntentLabel, target_class: str) -> str:
        """生成意图描述文本"""
        descriptions = {
            IntentLabel.WILLING_TO_YIELD: "可能让行",
            IntentLabel.MAY_CROSS: "可能突然横穿，需减速",
            IntentLabel.MAY_SWERVE: "可能偏离当前车道",
            IntentLabel.MAY_STOP: "可能突然停车",
            IntentLabel.MAY_RUN_RED: "可能闯红灯",
            IntentLabel.MAY_CUT_IN: "可能强行加塞变道",
            IntentLabel.UNCERTAIN: "行为不确定，保持警惕",
        }
        return descriptions.get(intent, "未知意图")
    
    def _generate_risk_suggestion(self, target_class: str, emotion: EmotionState,
                                  intent: IntentLabel, ttc: float) -> str:
        """生成风险关联建议"""
        if intent == IntentLabel.MAY_CROSS and ttc < 2.0:
            return "高风险：行人可能突然横穿，TTC紧张，建议主动减速"
        elif intent == IntentLabel.MAY_CUT_IN:
            return "注意：目标可能强行变道，保持安全距离"
        elif emotion == EmotionState.DISTRACTED:
            return "注意：目标注意力涣散，可能未注意本车"
        elif emotion == EmotionState.ROAD_RAGE:
            return "注意：目标有路怒倾向，保持距离避免冲突"
        else:
            return "一般风险：持续观察目标动态"
    
    # ========== 缓存清理 ==========
    
    def clean_expired_cache(self) -> int:
        """清理超过最长保留时间的缓存条目"""
        now = time.time()
        expired = []
        for target_id, entry in self._cache.items():
            if now - entry.update_timestamp > self.MAX_CACHE_RETENTION_S:
                expired.append(target_id)
        
        for target_id in expired:
            del self._cache[target_id]
        
        if expired:
            print(f"[{self.module_id}] 清理过期缓存: {len(expired)} 条")
        
        return len(expired)
    
    # ========== 变更日志 ==========
    
    def _log_high_risk(self, assessment: ComprehensiveAssessment, ttc: float) -> None:
        """记录高风险评估日志"""
        self._pending_logs.append({
            "log_id": f"emo-{uuid.uuid4().hex[:8]}",
            "event_type": "HIGH_RISK_ASSESSMENT",
            "target_id": assessment.target_id,
            "emotion": assessment.emotion_state.value,
            "intent": assessment.intent_label.value,
            "risk_suggestion": assessment.risk_suggestion,
            "ttc": ttc,
            "timestamp": assessment.assessment_timestamp
        })
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs
    
    # ========== 查询接口 ==========
    
    def get_cache_size(self) -> int:
        return len(self._cache)
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_queries": self._total_queries,
            "total_high_risk_assessments": self._total_high_risk_assessments,
            "total_degraded_periods": self._total_degraded_periods,
            "cache_size": len(self._cache),
            "state": self.state.value
        }


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-46 道路参与者情绪意图感知库 单元测试")
    print("=" * 60)
    passed, failed = 0, 0
    
    # TC-46-01: 查询有缓存的目标
    print("\n[TC-46-01] 查询有缓存的目标返回情绪和意图")
    try:
        lib = EmotionIntentLibrary()
        lib.update_emotion("PED-001", EmotionState.ANXIOUS, 0.8, 0.85)
        lib.update_intent("PED-001", IntentLabel.MAY_CROSS, 0.75)
        req = EmotionQueryRequest("q-001", "PED-001", "第四类", "ECC-03")
        emotion = lib.query_emotion(req)
        intent = lib.query_intent(req)
        assert emotion is not None and emotion.emotion_state == EmotionState.ANXIOUS
        assert intent is not None and intent.intent_label == IntentLabel.MAY_CROSS
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-46-02: 无缓存返回保守推断
    print("\n[TC-46-02] 无缓存目标返回保守推断")
    try:
        lib = EmotionIntentLibrary()
        req = EmotionQueryRequest("q-002", "UNKNOWN", "第四类", "ECC-03")
        emotion = lib.query_emotion(req)
        assert emotion is not None
        assert emotion.perception_confidence == 0.2  # 保守
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-46-03: 高风险目标综合评估
    print("\n[TC-46-03] TTC=1.5s 行人慌张 → 综合评估报告")
    try:
        lib = EmotionIntentLibrary()
        lib.update_emotion("PED-RISK", EmotionState.ANXIOUS, 0.9, 0.9)
        lib.update_intent("PED-RISK", IntentLabel.MAY_CROSS, 0.85)
        req = EmotionQueryRequest("q-003", "PED-RISK", "第四类", "ECC-05",
                                  priority="high", ttc=1.5)
        assessment = lib.query_comprehensive(req)
        assert assessment is not None
        assert "高风险" in assessment.risk_suggestion
        assert lib._total_high_risk_assessments == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-46-04: 连续低置信度进入降级
    print("\n[TC-46-04] 连续低置信度进入降级模式")
    try:
        lib = EmotionIntentLibrary()
        for i in range(15):
            lib.update_emotion(f"TGT-{i}", EmotionState.CALM, 0.5, 0.4)
        assert lib.state == LibraryState.DEGRADED
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-46-05: 降级后恢复
    print("\n[TC-46-05] 置信度恢复后退出降级")
    try:
        lib = EmotionIntentLibrary()
        for i in range(15):
            lib.update_emotion(f"TGT-{i}", EmotionState.CALM, 0.5, 0.4)
        for i in range(5):
            lib.update_emotion(f"REC-{i}", EmotionState.CALM, 0.8, 0.7)
        assert lib.state == LibraryState.NORMAL
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-46-06: 缓存超时标记过时
    print("\n[TC-46-06] 缓存超时标记过时")
    try:
        lib = EmotionIntentLibrary()
        lib.CACHE_VALIDITY_MS = 100  # 100ms
        lib.update_emotion("OLD-TGT", EmotionState.CALM, 0.5, 0.8)
        import time
        time.sleep(0.15)
        req = EmotionQueryRequest("q-006", "OLD-TGT", "第四类", "ECC-03")
        emotion = lib.query_emotion(req)
        assert emotion.is_stale == True
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-46-07: 漏斗一模块被拒
    print("\n[TC-46-07] 漏斗一模块查询被拒绝")
    try:
        lib = EmotionIntentLibrary()
        req = EmotionQueryRequest("q-007", "TGT", "第四类", "ad-07")
        emotion = lib.query_emotion(req)
        assert emotion is None
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-46-08: 紧急熔断拒绝查询
    print("\n[TC-46-08] 紧急熔断拒绝查询")
    try:
        lib = EmotionIntentLibrary()
        lib.pause()
        req = EmotionQueryRequest("q-008", "TGT", "第四类", "ECC-03")
        emotion = lib.query_emotion(req)
        assert emotion is None
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-46-09: 情绪到意图推断（无直接意图数据）
    print("\n[TC-46-09] 根据情绪推断意图（慌张→可能横穿）")
    try:
        lib = EmotionIntentLibrary()
        lib.update_emotion("INFER-TGT", EmotionState.ANXIOUS, 0.8, 0.8)
        # 不提供意图数据
        req = EmotionQueryRequest("q-009", "INFER-TGT", "第四类", "ECC-03")
        intent = lib.query_intent(req)
        assert intent is not None
        assert intent.intent_label == IntentLabel.MAY_CROSS
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-46-10: 缓存过期清理
    print("\n[TC-46-10] 缓存过期清理")
    try:
        lib = EmotionIntentLibrary()
        lib.MAX_CACHE_RETENTION_S = 0.1
        lib.update_emotion("TEMP", EmotionState.CALM, 0.5, 0.8)
        time.sleep(0.2)
        cleaned = lib.clean_expired_cache()
        assert cleaned == 1
        assert lib.get_cache_size() == 0
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    print(f"\n测试结果: {passed} PASS, {failed} FAIL")