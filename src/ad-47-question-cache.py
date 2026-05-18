#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-47
模块名称: 疑问缓存库
所属分区: 四、漏斗外挂扩展区（物理隔离）
核心职责: 暂存 ECC 认知大脑推理过程中产生的逻辑断点、未确认认知项、低置信度场景
          及待补全信息。为离线复盘、根因分析、策略修正、云端大模型异步识别提供
          结构化数据源。独立于双漏斗记忆系统运行，不参与记忆的沉淀、筛选、晋升与
          遗忘机制。任务周期结束后可选清理或归档。

依赖模块: ECC-01 情境解析模块（提交未知识别目标）、
          ECC-03 因果推理模块（提交推理断点）、
          ECC-04 心智模拟模块（提交低置信度方案）、
          ECC-08 元认知模块（提交认知偏差与能力缺口）、
          ECC-12 资源全域调度模块（查询疑问缓存用于云端异步处理）
被依赖模块: ECC-08（消费疑问缓存进行能力缺口分析）、
            ECC-12（向云端大模型提交未知识别请求）、
            离线复盘系统（消费疑问缓存进行根因分析与策略修正）、
            ad-27（L4 抽象提炼单元，参考疑问缓存进行规则归纳）

安全约束:
  S-01: 疑问缓存独立于双漏斗记忆系统，不参与记忆的沉淀、筛选、晋升与遗忘机制
  S-02: 疑问条目中的场景快照数据须脱敏处理
  S-03: 云端大模型识别请求须经 ECC-12 安全校验后发出，原始感知数据不得直接上传云端
  S-04: 疑问缓存数据在生命周期到期后须安全清理，法规模糊地带例外（永久归档）
  S-05: 疑问缓存数据仅供离线复盘、根因分析与系统优化使用，不作为实时驾驶决策的依据
  S-06: 离线回放数据导出须在车辆安全停车状态（P 档）且经维护授权后方可执行
  S-07: 所有疑问创建、处理、清理操作全量写入 ad-51 不可变日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid
import hashlib


# ==================== 枚举定义 ====================

class QuestionType(Enum):
    """疑问类型"""
    UNKNOWN_TARGET = "未知识别目标"
    REASONING_GAP = "推理逻辑断点"
    LOW_CONFIDENCE_SCENE = "低置信度场景"
    COGNITIVE_GAP = "认知能力缺口"
    LEGAL_AMBIGUITY = "法规模糊地带"
    SENSOR_ANOMALY = "传感器数据异常"


class QuestionState(Enum):
    """疑问状态"""
    WAITING = "等待处理"
    PROCESSING = "处理中"
    RESOLVED = "已处理"
    EXPIRED = "已过期"


class CacheState(Enum):
    """缓存库内部状态"""
    NORMAL = "normal"
    RECORDING = "recording"
    QUERYING = "querying"
    CLEANING = "cleaning"
    REPLAY = "replay"
    PAUSED = "paused"


# ==================== 数据结构 ====================

@dataclass
class QuestionEntry:
    """疑问条目"""
    question_id: str
    question_type: QuestionType
    submitter_module: str                     # 提交来源模块
    scene_snapshot_hash: str                  # 场景快照哈希（脱敏后）
    scene_summary: str                        # 场景摘要
    confidence: float                         # 关联置信度
    urgency: str = "normal"                   # 紧急程度: normal / high / critical
    state: QuestionState = QuestionState.WAITING
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0                   # 过期时间
    frequency: int = 1                        # 出现频率（用于去重统计）
    related_question_ids: List[str] = field(default_factory=list)
    cloud_response: Optional[Dict[str, Any]] = None  # 云端识别回写结果
    resolved_at: Optional[float] = None


@dataclass
class QuestionSubmitRequest:
    """疑问提交请求"""
    submitter_module: str
    question_type: QuestionType
    scene_snapshot: Dict[str, Any]             # 原始场景快照（将进行脱敏处理）
    confidence: float
    urgency: str = "normal"
    target_id: Optional[str] = None            # 关联目标 ID（用于去重）
    timestamp: float = field(default_factory=time.time)


@dataclass
class QuestionSubmitResponse:
    """疑问提交响应"""
    question_id: str
    is_new: bool                               # 是否新建（false 表示更新了已有疑问）
    expires_at: float


@dataclass
class CloudRecognitionResult:
    """云端大模型识别结果回写"""
    original_question_id: str
    recognition_result: Dict[str, Any]
    confidence: float
    suggested_classification: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class QuestionQueryRequest:
    """疑问查询请求"""
    query_type: Optional[QuestionType] = None   # 按类型筛选
    time_range: Optional[Tuple[float, float]] = None  # 按时间范围筛选
    state: Optional[QuestionState] = None       # 按状态筛选
    max_results: int = 100


@dataclass
class QuestionQueryResponse:
    """疑问查询响应"""
    questions: List[QuestionEntry]
    total_count: int


@dataclass
class CleanupReport:
    """清理报告"""
    cleaned_count: int
    archived_count: int              # 法规模糊地带归档数
    released_bytes: int
    remaining_count: int
    timestamp: float = field(default_factory=time.time)


@dataclass
class ReplayDataPackage:
    """离线回放数据包"""
    package_id: str
    time_range: Tuple[float, float]
    questions: List[QuestionEntry]
    total_count: int
    digital_signature: str
    exported_at: float = field(default_factory=time.time)


# ==================== 生命周期配置 ====================

# 各类型疑问的默认生命周期（秒）
DEFAULT_LIFECYCLES: Dict[QuestionType, float] = {
    QuestionType.UNKNOWN_TARGET: 72 * 3600,         # 72 小时
    QuestionType.REASONING_GAP: 7 * 24 * 3600,      # 7 日
    QuestionType.LOW_CONFIDENCE_SCENE: 14 * 24 * 3600,  # 14 日
    QuestionType.COGNITIVE_GAP: 30 * 24 * 3600,     # 30 日
    QuestionType.LEGAL_AMBIGUITY: float('inf'),      # 永久
    QuestionType.SENSOR_ANOMALY: 48 * 3600,         # 48 小时
}

# 清理优先级（数字越小越先清理）
CLEANUP_PRIORITY: Dict[QuestionType, int] = {
    QuestionType.SENSOR_ANOMALY: 0,
    QuestionType.UNKNOWN_TARGET: 1,
    QuestionType.REASONING_GAP: 2,
    QuestionType.LOW_CONFIDENCE_SCENE: 3,
    QuestionType.COGNITIVE_GAP: 4,
    QuestionType.LEGAL_AMBIGUITY: 99,  # 永不清理
}

# 已处理条目保留时间（秒）
RESOLVED_RETENTION = 7 * 24 * 3600  # 7 日


# ==================== 主类定义 ====================

class QuestionCache:
    """
    疑问缓存库 - 漏斗外挂扩展区
    
    职责:
    1. 接收 ECC 各模块提交的疑问条目
    2. 去重处理（同类型同目标 ID 的疑问合并更新）
    3. 提供多维条件查询接口
    4. 处理云端大模型识别结果回写
    5. 按生命周期策略定期清理过期疑问
    6. 法规模糊地带永久归档至 ad-49
    7. 支持离线回放数据导出（需授权）
    """
    
    # 最大缓存容量（条目数）
    MAX_CACHE_SIZE = 10000
    
    # 容量紧急清理阈值
    URGENT_CLEAN_THRESHOLD = 10000
    NORMAL_CLEAN_THRESHOLD = 5000
    
    # 容量告警使用率
    CAPACITY_WARNING_RATE = 0.80
    CAPACITY_URGENT_RATE = 0.90
    
    # 模拟存储大小（字节/条目）
    AVG_ENTRY_SIZE_BYTES = 2048
    
    # 未识别目标定期推送间隔（秒）
    UNKNOWN_TARGET_PUSH_INTERVAL = 30 * 60  # 30 分钟
    
    def __init__(self, max_capacity_bytes: float = 50 * 1024 * 1024):  # 默认 50MB
        self.module_id = "ad-47"
        self.module_name = "疑问缓存库"
        
        # 内部状态
        self.state = CacheState.NORMAL
        
        # 疑问条目字典: question_id -> QuestionEntry
        self._questions: Dict[str, QuestionEntry] = {}
        
        # 疑问计数器
        self._total_created = 0
        
        # 最大容量
        self._max_capacity_bytes = max_capacity_bytes
        
        # 上次推送未识别目标时间
        self._last_unknown_target_push = 0.0
        
        # 统计
        self._total_creates = 0
        self._total_updates = 0
        self._total_cloud_responses = 0
        self._total_cleaned = 0
        self._total_archived = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 疑问缓存库初始化完成")
        print(f"[{self.module_id}] 最大容量: {self._max_capacity_bytes/1024/1024:.0f}MB")
    
    # ========== 状态管理 ==========
    
    def pause(self) -> None:
        self.state = CacheState.PAUSED
    
    def resume(self) -> None:
        self.state = CacheState.NORMAL
    
    def get_state(self) -> CacheState:
        return self.state
    
    def get_usage_rate(self) -> float:
        """获取容量使用率"""
        current_size = len(self._questions) * self.AVG_ENTRY_SIZE_BYTES
        return current_size / self._max_capacity_bytes if self._max_capacity_bytes > 0 else 0.0
    
    # ========== 疑问提交 ==========
    
    def submit_question(self, request: QuestionSubmitRequest) -> QuestionSubmitResponse:
        """
        提交新的疑问条目
        
        去重逻辑：同类型且同目标 ID 的疑问合并，更新频率和时间戳
        
        Args:
            request: 疑问提交请求
            
        Returns:
            提交响应（含疑问 ID 和是否新建）
        """
        if self.state == CacheState.PAUSED:
            return QuestionSubmitResponse("", False, 0.0)
        
        self.state = CacheState.RECORDING
        
        # 去重检查
        if request.question_type == QuestionType.UNKNOWN_TARGET and request.target_id:
            for existing_id, existing_q in self._questions.items():
                if (existing_q.question_type == QuestionType.UNKNOWN_TARGET and
                        existing_q.state == QuestionState.WAITING and
                        existing_q.submitter_module == request.submitter_module):
                    # 更新已有疑问
                    existing_q.frequency += 1
                    existing_q.created_at = request.timestamp
                    existing_q.scene_summary = self._generate_summary(request.scene_snapshot)
                    self._total_updates += 1
                    self.state = CacheState.NORMAL
                    return QuestionSubmitResponse(
                        question_id=existing_id,
                        is_new=False,
                        expires_at=existing_q.expires_at
                    )
        
        # 创建新疑问
        question_id = f"Q-{uuid.uuid4().hex[:8]}"
        
        # 脱敏场景快照，仅保留哈希和摘要
        scene_hash = self._hash_scene(request.scene_snapshot)
        scene_summary = self._generate_summary(request.scene_snapshot)
        
        # 计算生命周期
        lifecycle = DEFAULT_LIFECYCLES.get(request.question_type, 7 * 24 * 3600)
        expires_at = request.timestamp + lifecycle if lifecycle != float('inf') else float('inf')
        
        entry = QuestionEntry(
            question_id=question_id,
            question_type=request.question_type,
            submitter_module=request.submitter_module,
            scene_snapshot_hash=scene_hash,
            scene_summary=scene_summary,
            confidence=request.confidence,
            urgency=request.urgency,
            state=QuestionState.WAITING,
            created_at=request.timestamp,
            expires_at=expires_at,
            frequency=1
        )
        
        self._questions[question_id] = entry
        self._total_creates += 1
        self._total_created += 1
        
        # 检查容量
        if self.get_usage_rate() > self.CAPACITY_URGENT_RATE:
            self._urgent_clean()
        
        self.state = CacheState.NORMAL
        
        return QuestionSubmitResponse(
            question_id=question_id,
            is_new=True,
            expires_at=expires_at
        )
    
    def _hash_scene(self, scene_snapshot: Dict[str, Any]) -> str:
        """对场景快照进行哈希（脱敏处理）"""
        # S-02: 脱敏处理——剔除 GPS 精确坐标、行人/车辆特征
        sanitized = {}
        for key, value in scene_snapshot.items():
            if key in ["gps_coordinates", "license_plate", "face_features"]:
                continue
            sanitized[key] = str(value)
        raw = str(sorted(sanitized.items()))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
    
    def _generate_summary(self, scene_snapshot: Dict[str, Any]) -> str:
        """生成场景摘要（脱敏）"""
        parts = []
        if "road_type" in scene_snapshot:
            parts.append(f"道路: {scene_snapshot['road_type']}")
        if "weather" in scene_snapshot:
            parts.append(f"天气: {scene_snapshot['weather']}")
        if "target_class" in scene_snapshot:
            parts.append(f"目标: {scene_snapshot['target_class']}")
        return ", ".join(parts) if parts else "场景摘要不可用"
    
    def _urgent_clean(self) -> None:
        """紧急清理：按优先级清理非等待状态的条目"""
        self.state = CacheState.CLEANING
        
        # 优先清理已处理条目
        resolved = [
            qid for qid, q in self._questions.items()
            if q.state == QuestionState.RESOLVED
        ]
        
        # 按清理优先级排序（低优先级先清理）
        sorted_questions = sorted(
            self._questions.items(),
            key=lambda x: (CLEANUP_PRIORITY.get(x[1].question_type, 50), x[1].created_at)
        )
        
        target_remove = max(1, int(len(self._questions) * 0.2))
        removed = 0
        
        for qid, q in sorted_questions:
            if q.state == QuestionState.RESOLVED or q.question_type != QuestionType.LEGAL_AMBIGUITY:
                if q.question_type == QuestionType.LEGAL_AMBIGUITY:
                    self._archive_to_ad49(qid)
                    self._total_archived += 1
                else:
                    self._total_cleaned += 1
                del self._questions[qid]
                removed += 1
                if removed >= target_remove:
                    break
        
        self.state = CacheState.NORMAL
        print(f"[{self.module_id}] 紧急清理: {removed} 条")
    
    def _archive_to_ad49(self, question_id: str) -> None:
        """将法规模糊地带条目永久归档至 ad-49"""
        if question_id in self._questions:
            entry = self._questions[question_id]
            self._log_event("ARCHIVE_TO_AD49", {
                "question_id": question_id,
                "type": entry.question_type.value,
                "summary": entry.scene_summary
            })
    
    # ========== 云端识别结果回写 ==========
    
    def handle_cloud_response(self, response: CloudRecognitionResult) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        处理云端大模型识别结果回写
        
        Args:
            response: 云端识别结果
            
        Returns:
            (是否成功, 世界模型更新建议)
        """
        original_id = response.original_question_id
        
        if original_id not in self._questions:
            return False, None
        
        entry = self._questions[original_id]
        entry.cloud_response = response.recognition_result
        entry.state = QuestionState.RESOLVED
        entry.resolved_at = response.timestamp
        self._total_cloud_responses += 1
        
        # 高置信度识别结果 → 生成世界模型更新建议
        update_suggestion = None
        if response.confidence > 0.7 and response.suggested_classification:
            update_suggestion = {
                "source_question_id": original_id,
                "suggested_classification": response.suggested_classification,
                "confidence": response.confidence,
                "recognition_result": response.recognition_result
            }
        
        self._log_event("CLOUD_RESPONSE", {
            "question_id": original_id,
            "confidence": response.confidence,
            "suggested_classification": response.suggested_classification
        })
        
        return True, update_suggestion
    
    # ========== 查询接口 ==========
    
    def query_questions(self, request: QuestionQueryRequest) -> QuestionQueryResponse:
        """
        按条件查询疑问条目
        
        Args:
            request: 查询请求
            
        Returns:
            查询响应
        """
        self.state = CacheState.QUERYING
        
        results = list(self._questions.values())
        
        # 按类型筛选
        if request.question_type:
            results = [q for q in results if q.question_type == request.question_type]
        
        # 按时间范围筛选
        if request.time_range:
            start, end = request.time_range
            results = [q for q in results if start <= q.created_at <= end]
        
        # 按状态筛选
        if request.state:
            results = [q for q in results if q.state == request.state]
        
        # 按创建时间降序排列
        results.sort(key=lambda x: x.created_at, reverse=True)
        
        total = len(results)
        results = results[:request.max_results]
        
        self.state = CacheState.NORMAL
        
        return QuestionQueryResponse(
            questions=results,
            total_count=total
        )
    
    def get_unknown_targets_for_push(self) -> List[QuestionEntry]:
        """
        获取待推送至云端的未识别目标清单
        
        每 30 分钟推送一次
        
        Returns:
            未识别目标疑问列表
        """
        now = time.time()
        if now - self._last_unknown_target_push < self.UNKNOWN_TARGET_PUSH_INTERVAL:
            return []
        
        self._last_unknown_target_push = now
        
        unknown = [
            q for q in self._questions.values()
            if q.question_type == QuestionType.UNKNOWN_TARGET
            and q.state == QuestionState.WAITING
        ]
        
        # 按出现频率降序排列
        unknown.sort(key=lambda x: x.frequency, reverse=True)
        
        return unknown
    
    # ========== 定期清理 ==========
    
    def execute_cleanup(self) -> CleanupReport:
        """
        执行定期清理
        
        清理规则:
        - 已处理条目保留 7 日后清理
        - 未处理条目超过生命周期后清理
        - 法规模糊地带不清理，转归档至 ad-49
        - 容量告急时按优先级清理
        
        Returns:
            清理报告
        """
        self.state = CacheState.CLEANING
        
        now = time.time()
        cleaned = 0
        archived = 0
        released = 0
        
        to_remove = []
        to_archive = []
        
        for qid, q in self._questions.items():
            should_remove = False
            should_archive = False
            
            if q.state == QuestionState.RESOLVED:
                if q.resolved_at and now - q.resolved_at > RESOLVED_RETENTION:
                    should_remove = True
            
            elif q.state == QuestionState.WAITING:
                if q.expires_at != float('inf') and now > q.expires_at:
                    if q.question_type == QuestionType.LEGAL_AMBIGUITY:
                        should_archive = True
                    else:
                        should_remove = True
            
            if should_archive:
                to_archive.append(qid)
            elif should_remove:
                to_remove.append(qid)
        
        # 容量告急时追加清理
        if self.get_usage_rate() > self.CAPACITY_WARNING_RATE:
            extra_remove = max(0, int(len(self._questions) * 0.1))
            sorted_for_clean = sorted(
                [(qid, q) for qid, q in self._questions.items() if qid not in to_remove],
                key=lambda x: CLEANUP_PRIORITY.get(x[1].question_type, 50)
            )
            for qid, q in sorted_for_clean[:extra_remove]:
                if q.question_type != QuestionType.LEGAL_AMBIGUITY:
                    to_remove.append(qid)
        
        # 执行清理
        for qid in to_archive:
            self._archive_to_ad49(qid)
            del self._questions[qid]
            archived += 1
        
        for qid in to_remove:
            del self._questions[qid]
            cleaned += 1
        
        released = (cleaned + archived) * self.AVG_ENTRY_SIZE_BYTES
        self._total_cleaned += cleaned
        self._total_archived += archived
        
        report = CleanupReport(
            cleaned_count=cleaned,
            archived_count=archived,
            released_bytes=released,
            remaining_count=len(self._questions)
        )
        
        self.state = CacheState.NORMAL
        
        if cleaned + archived > 0:
            print(f"[{self.module_id}] 定期清理: 删除{cleaned}条, 归档{archived}条")
        
        return report
    
    # ========== 离线回放 ==========
    
    def export_replay_data(self, time_range: Tuple[float, float],
                           authorization_token: str,
                           token_validator) -> Optional[ReplayDataPackage]:
        """
        导出离线回放数据包
        
        S-06: 须在车辆安全停车状态（P 档）且经维护授权后方可执行
        
        Args:
            time_range: 时间范围
            authorization_token: 授权令牌
            token_validator: 令牌验证回调
            
        Returns:
            回放数据包，或 None（授权失败）
        """
        if not token_validator(authorization_token, "replay_export"):
            print(f"[{self.module_id}] 离线回放导出授权失败")
            return None
        
        self.state = CacheState.REPLAY
        
        start, end = time_range
        matching = [
            q for q in self._questions.values()
            if start <= q.created_at <= end
        ]
        
        package = ReplayDataPackage(
            package_id=f"replay-{uuid.uuid4().hex[:8]}",
            time_range=time_range,
            questions=matching,
            total_count=len(matching),
            digital_signature=self._generate_signature(matching)
        )
        
        self._log_event("REPLAY_EXPORT", {
            "package_id": package.package_id,
            "total_questions": len(matching),
            "time_range": f"{start}-{end}"
        })
        
        self.state = CacheState.NORMAL
        return package
    
    def _generate_signature(self, questions: List[QuestionEntry]) -> str:
        """生成数据包数字签名"""
        raw = "".join(q.question_id for q in questions)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
    
    # ========== 查询接口 ==========
    
    def get_question(self, question_id: str) -> Optional[QuestionEntry]:
        return self._questions.get(question_id)
    
    def get_total_count(self) -> int:
        return len(self._questions)
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_created": self._total_creates,
            "total_updated": self._total_updates,
            "total_cloud_responses": self._total_cloud_responses,
            "total_cleaned": self._total_cleaned,
            "total_archived": self._total_archived,
            "current_count": len(self._questions),
            "usage_rate": self.get_usage_rate(),
            "state": self.state.value
        }
    
    # ========== 变更日志 ==========
    
    def _log_event(self, event_type: str, details: Dict[str, Any]) -> None:
        self._pending_logs.append({
            "log_id": f"qc-{uuid.uuid4().hex[:8]}",
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
    print("ad-47 疑问缓存库 单元测试")
    print("=" * 60)
    passed, failed = 0, 0
    
    # TC-47-01: 提交新疑问
    print("\n[TC-47-01] 提交新疑问")
    try:
        cache = QuestionCache(max_capacity_bytes=10*1024*1024)
        req = QuestionSubmitRequest(
            submitter_module="ECC-01",
            question_type=QuestionType.UNKNOWN_TARGET,
            scene_snapshot={"road_type": "高速", "target_class": "未知物体"},
            confidence=0.3
        )
        resp = cache.submit_question(req)
        assert resp.is_new == True
        assert cache.get_total_count() == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-47-02: 同类型去重更新
    print("\n[TC-47-02] 同类型同目标去重更新")
    try:
        cache = QuestionCache(max_capacity_bytes=10*1024*1024)
        req1 = QuestionSubmitRequest("ECC-01", QuestionType.UNKNOWN_TARGET,
                                      {"road_type": "高速"}, 0.3, target_id="OBJ-001")
        resp1 = cache.submit_question(req1)
        req2 = QuestionSubmitRequest("ECC-01", QuestionType.UNKNOWN_TARGET,
                                      {"road_type": "高速"}, 0.35, target_id="OBJ-001")
        resp2 = cache.submit_question(req2)
        assert resp2.is_new == False
        assert cache._questions[resp1.question_id].frequency == 2
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-47-03: 云端识别回写
    print("\n[TC-47-03] 云端识别回写高置信度生成更新建议")
    try:
        cache = QuestionCache(max_capacity_bytes=10*1024*1024)
        req = QuestionSubmitRequest("ECC-01", QuestionType.UNKNOWN_TARGET,
                                     {"road_type": "高速"}, 0.3)
        resp = cache.submit_question(req)
        cloud = CloudRecognitionResult(
            original_question_id=resp.question_id,
            recognition_result={"name": "轮胎碎片"},
            confidence=0.85,
            suggested_classification="第一类：静态固定无生命实体"
        )
        ok, suggestion = cache.handle_cloud_response(cloud)
        assert ok and suggestion is not None
        assert cache._questions[resp.question_id].state == QuestionState.RESOLVED
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-47-04: 法规模糊地带不清理
    print("\n[TC-47-04] 法规模糊地带过期归档不删除")
    try:
        cache = QuestionCache(max_capacity_bytes=10*1024*1024)
        req = QuestionSubmitRequest("ECC-05", QuestionType.LEGAL_AMBIGUITY,
                                     {"road_type": "高速"}, 0.5)
        resp = cache.submit_question(req)
        # 手动设置过期
        cache._questions[resp.question_id].expires_at = time.time() - 1
        report = cache.execute_cleanup()
        assert report.archived_count == 1
        assert report.cleaned_count == 0
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-47-05: 容量告急紧急清理
    print("\n[TC-47-05] 容量使用率>90%触发紧急清理")
    try:
        cache = QuestionCache(max_capacity_bytes=1024)  # 极小容量
        for i in range(20):
            req = QuestionSubmitRequest("ECC-01", QuestionType.SENSOR_ANOMALY,
                                         {"sensor": f"S{i}"}, 0.3)
            cache.submit_question(req)
        assert cache.get_total_count() < 20
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    print(f"\n测试结果: {passed} PASS, {failed} FAIL")