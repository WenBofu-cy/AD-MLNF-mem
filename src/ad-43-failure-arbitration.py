#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-43
模块名称: 失败经验安全仲裁三道校验单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 晋升与遗忘执行机制
核心职责: 对所有结果标签为“策略失误”的失败经验，在晋升至 L4/L5 之前依次执行三道
          安全校验：法规校验、动力学校验、仿真回灌验证。任一校验未通过，该经验仅保留
          在 L3 层作为警示标签，不得晋升。同一场景连续三次无警示安全通过后，警示标签
          自动降级为普通经验。

依赖模块: ad-38(晋升双条件判定单元，发送仲裁请求),
          ad-45(交通法律法规库，第一道校验),
          ad-44(独立世界模型库，第二道校验物理参数),
          仿真回灌引擎(外部模块，第三道校验),
          ad-24(L3 中期层存储单元，更新警示标签状态)
被依赖模块: ad-38(消费仲裁结果，决定是否放行晋升), ad-24(消费警示标签降级指令)

三道安全校验规则:
  第一道 法规校验: 失败经验的决策动作是否违反任何交通法规硬约束。
                   任一违规则直接终止仲裁，结论="保留L3警示"。
  第二道 动力学校验: 失败经验的决策动作是否超出车辆物理动力学极限。
                     任一超限则不通过。
  第三道 仿真回灌: 将失败经验在仿真环境中回放，验证修正策略是否安全有效。
                   仿真不通过则保留L3警示。

特殊处理规则:
  - 不可抗力场景: 豁免三道校验，直接结论="永久锁定L5"。
  - S≥0.9 的失败经验: 即使校验未通过，仲裁结论升级为"永久锁定L5"。
  - 仿真引擎不可用: 仅执行第一、二道校验，若均通过则放行晋升。
  - 外部模块(法规库/世界模型)不可用: 采用保守策略拒绝晋升。

安全约束:
  S-01: 失败经验晋升 L4/L5 必须通过三道安全校验，任一未通过则永久保留在 L3 作为警示标签
  S-02: 第一道法规校验具有一票否决权
  S-03: 不可抗力场景经验豁免三道校验，直接永久锁定于 L5
  S-04: S≥0.9 的失败经验即使校验未通过，也须升级为 L5 永久锁定
  S-05: 外部模块不可用时，采用保守策略拒绝晋升，确保安全优先
  S-06: 所有仲裁过程全量写入 ad-51 不可变日志
"""

from typing import Dict, List, Optional, Any, Tuple, Callable
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 全局枚举定义 ====================

class ArbitrationState(Enum):
    """仲裁单元内部状态机"""
    IDLE = "空闲等待"
    CHECK1_LAW = "第一道法规校验执行中"
    CHECK2_PHYSICS = "第二道动力学校验执行中"
    CHECK3_SIM = "第三道仿真回灌验证执行中"
    CONCLUSION = "仲裁结论生成中"
    PAUSED = "暂停服务"


class ArbitrationConclusion(Enum):
    """仲裁最终结论"""
    APPROVED = "放行晋升"            # 三道校验全部通过
    RETAIN_WARNING = "保留L3警示"    # 任一校验未通过
    LOCK_L5 = "永久锁定L5"           # 不可抗力或S≥0.9升级
    DEFERRED = "暂缓"                # 外部模块不可用或仲裁单元暂停


class LawCheckResult(Enum):
    """法规校验结果"""
    COMPLIANT = "合规"
    VIOLATED = "违规"


class PhysicsCheckResult(Enum):
    """动力学校验结果"""
    FEASIBLE = "可行"
    EXCEEDED = "超出极限"


class SimulationResult(Enum):
    """仿真回灌验证结果"""
    PASSED = "通过"
    FAILED = "未通过"
    UNAVAILABLE = "引擎不可用"


# ==================== 标准化数据结构 ====================

@dataclass
class ArbitrationRequest:
    """失败经验仲裁请求（来自 ad-38）"""
    entry_id: str
    experience_content: Dict[str, Any]         # 经验内容（含决策动作）
    result_label: str                          # "策略失误"
    scene_features: Dict[str, Any]             # 场景特征
    s_value: float = 0.0                       # 安全显著性 S 值
    force_majeure: bool = False                # 是否不可抗力
    request_source: str = ""                   # "L3→L4" 或 "L4→L5"
    request_timestamp: float = field(default_factory=time.time)


@dataclass
class LawCheckResultDetail:
    """第一道法规校验详细结果"""
    overall: LawCheckResult
    violated_rules: List[str] = field(default_factory=list)
    violation_severity: str = "无"
    check_timestamp: float = field(default_factory=time.time)


@dataclass
class PhysicsCheckResultDetail:
    """第二道动力学校验详细结果"""
    overall: PhysicsCheckResult
    exceeded_parameters: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    check_timestamp: float = field(default_factory=time.time)


@dataclass
class SimulationCheckResultDetail:
    """第三道仿真回灌验证详细结果"""
    overall: SimulationResult
    scene_similarity: float = 1.0              # 场景还原相似度
    collision_count: int = 0                   # 碰撞次数
    total_runs: int = 0                        # 仿真运行总次数
    comfort_violation: bool = False            # 是否舒适度违规
    regulation_violation: bool = False         # 是否法规违规
    check_timestamp: float = field(default_factory=time.time)


@dataclass
class ArbitrationResult:
    """仲裁最终结果"""
    entry_id: str
    conclusion: ArbitrationConclusion
    reason: str
    check_details: Dict[str, Any] = field(default_factory=dict)
    result_timestamp: float = field(default_factory=time.time)


@dataclass
class ArbitrationCacheEntry:
    """仲裁结果缓存（防止重复仲裁）"""
    entry_id: str
    conclusion: ArbitrationConclusion
    reason: str
    cached_at: float = field(default_factory=time.time)


@dataclass
class ArbitrationStatistics:
    """仲裁运行统计"""
    total_requests: int = 0
    total_approved: int = 0
    total_rejected: int = 0
    total_locked_l5: int = 0
    total_deferred: int = 0
    avg_duration_ms: float = 0.0


# ==================== 全局运行配置常量 ====================

class ArbitrationConfig:
    """仲裁单元全局配置"""
    # 各道校验超时（秒）
    LAW_CHECK_TIMEOUT_SEC = 30.0
    PHYSICS_CHECK_TIMEOUT_SEC = 15.0
    SIMULATION_TIMEOUT_SEC = 300.0         # 仿真最耗时，5分钟

    # S≥0.9 高风险阈值
    HIGH_S_THRESHOLD = 0.90

    # 法规硬约束违规判定关键字
    LAW_VIOLATION_KEYWORDS = [
        "闯红灯", "未礼让行人", "超速", "实线变道",
        "逆行", "占用非机动车道", "高速公路停车", "未让行特种车辆"
    ]

    # 动力学极限判定阈值
    MAX_BRAKE_DECEL = 1.0                  # g
    MAX_LATERAL_ACCEL = 0.85               # g
    MAX_STEERING_RATE = 500.0              # 度/秒
    MIN_RESPONSE_DELAY = 0.100             # 秒

    # 仿真回灌判定阈值
    MIN_SCENE_SIMILARITY = 0.85
    MAX_ALLOWED_COLLISIONS = 0
    MAX_LONGITUDINAL_JERK = 5.0            # m/s³
    MAX_LATERAL_JERK = 3.0                 # m/s³

    # 缓存保留时间（秒）
    CACHE_RETENTION_SEC = 30 * 24 * 3600   # 30天


# ==================== 主类：三道安全仲裁核心单元 ====================

class FailureArbitrationUnit:
    """
    失败经验安全仲裁三道校验单元

    职责:
    1. 接收 ad-38 下发的失败经验仲裁请求
    2. 依次执行三道安全校验：法规 → 动力学 → 仿真
    3. 任一校验未通过则结论为"保留L3警示"
    4. 不可抗力场景豁免三道校验，直接永久锁定 L5
    5. S≥0.9 的失败经验即使未通过校验也升级为 L5 永久锁定
    6. 外部模块不可用时采用保守策略，安全优先
    """

    def __init__(self):
        self.module_id = "ad-43"
        self.module_name = "失败经验安全仲裁三道校验单元"
        self.config = ArbitrationConfig()

        # 核心状态
        self.state = ArbitrationState.IDLE

        # 仲裁结果缓存（防止重复仲裁）
        self._arbitration_cache: Dict[str, ArbitrationCacheEntry] = {}

        # 运行统计
        self._stats = ArbitrationStatistics()

        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []

        print(f"[{self.module_id}] 失败经验安全仲裁单元初始化完成")
        print(f"[{self.module_id}] 三道校验: 法规 → 动力学 → 仿真")
        print(f"[{self.module_id}] S≥{self.config.HIGH_S_THRESHOLD} 自动升级 L5 永久锁定")
        print(f"[{self.module_id}] 不可抗力场景豁免三道校验")

    # ========== 对外状态管理接口 ==========

    def pause(self) -> None:
        """暂停仲裁服务"""
        self.state = ArbitrationState.PAUSED
        print(f"[{self.module_id}] 仲裁服务已暂停")

    def resume(self) -> None:
        """恢复仲裁服务"""
        self.state = ArbitrationState.IDLE
        print(f"[{self.module_id}] 仲裁服务已恢复")

    def get_state(self) -> ArbitrationState:
        return self.state

    # ========== 核心仲裁主流程 ==========

    def execute_arbitration(self,
                            request: ArbitrationRequest,
                            law_checker: Callable[[Dict[str, Any], Dict[str, Any]], LawCheckResultDetail],
                            physics_checker: Callable[[Dict[str, Any]], PhysicsCheckResultDetail],
                            simulation_engine: Callable[[Dict[str, Any], Dict[str, Any]], SimulationCheckResultDetail]
                            ) -> ArbitrationResult:
        """
        执行三道安全仲裁主流程

        Args:
            request: 仲裁请求
            law_checker: 法规校验回调 (decision_action, scene_features) -> LawCheckResultDetail
            physics_checker: 动力学校验回调 (decision_action) -> PhysicsCheckResultDetail
            simulation_engine: 仿真引擎回调 (experience, scene_features) -> SimulationCheckResultDetail

        Returns:
            仲裁最终结果
        """
        # 状态检查
        if self.state == ArbitrationState.PAUSED:
            return ArbitrationResult(
                entry_id=request.entry_id,
                conclusion=ArbitrationConclusion.DEFERRED,
                reason="仲裁单元暂停服务"
            )

        start_time = time.time()
        self._stats.total_requests += 1

        # ====== 重复仲裁缓存检查 ======
        if request.entry_id in self._arbitration_cache:
            cached = self._arbitration_cache[request.entry_id]
            if cached.conclusion == ArbitrationConclusion.RETAIN_WARNING:
                return ArbitrationResult(
                    entry_id=request.entry_id,
                    conclusion=ArbitrationConclusion.RETAIN_WARNING,
                    reason=f"已有结论：保留L3警示（缓存于 {cached.cached_at:.0f}）"
                )

        # ====== S-03: 不可抗力场景豁免 ======
        if request.force_majeure or request.result_label == "不可抗力场景":
            self._stats.total_locked_l5 += 1
            result = ArbitrationResult(
                entry_id=request.entry_id,
                conclusion=ArbitrationConclusion.LOCK_L5,
                reason="不可抗力场景，豁免三道校验，永久锁定于L5",
                check_details={"法规": "豁免", "动力学": "豁免", "仿真": "豁免"}
            )
            self._cache_result(request.entry_id, result)
            self._log_arbitration(request.entry_id, result)
            return result

        # ====== 逐道执行校验 ======
        check_details = {}

        # --- 第一道：法规校验 ---
        self.state = ArbitrationState.CHECK1_LAW
        try:
            decision_action = request.experience_content.get("decision_action", {})
            scene_features = request.scene_features
            law_result = law_checker(decision_action, scene_features)
        except Exception:
            # S-05: 法规库不可用，保守拒绝
            return self._create_conservative_result(request.entry_id, "法规库不可用")

        check_details["法规"] = {
            "结论": law_result.overall.value,
            "违规条目": law_result.violated_rules,
            "严重等级": law_result.violation_severity
        }

        # S-02: 法规一票否决
        if law_result.overall == LawCheckResult.VIOLATED:
            self._stats.total_rejected += 1
            result = ArbitrationResult(
                entry_id=request.entry_id,
                conclusion=ArbitrationConclusion.RETAIN_WARNING,
                reason=f"第一道法规校验未通过: {law_result.violated_rules}",
                check_details=check_details
            )
            # S-04: S≥0.9 升级为 L5
            result = self._apply_high_s_override(request, result)
            self._cache_result(request.entry_id, result)
            self._log_arbitration(request.entry_id, result)
            self.state = ArbitrationState.IDLE
            return result

        # --- 第二道：动力学校验 ---
        self.state = ArbitrationState.CHECK2_PHYSICS
        try:
            decision_action = request.experience_content.get("decision_action", {})
            physics_result = physics_checker(decision_action)
        except Exception:
            return self._create_conservative_result(request.entry_id, "世界模型不可用")

        check_details["动力学"] = {
            "结论": physics_result.overall.value,
            "超限参数": {
                k: f"实际{v[0]:.2f} > 极限{v[1]:.2f}"
                for k, v in physics_result.exceeded_parameters.items()
            }
        }

        if physics_result.overall == PhysicsCheckResult.EXCEEDED:
            self._stats.total_rejected += 1
            result = ArbitrationResult(
                entry_id=request.entry_id,
                conclusion=ArbitrationConclusion.RETAIN_WARNING,
                reason=f"第二道动力学校验未通过: {list(physics_result.exceeded_parameters.keys())}",
                check_details=check_details
            )
            result = self._apply_high_s_override(request, result)
            self._cache_result(request.entry_id, result)
            self._log_arbitration(request.entry_id, result)
            self.state = ArbitrationState.IDLE
            return result

        # --- 第三道：仿真回灌验证 ---
        self.state = ArbitrationState.CHECK3_SIM
        try:
            sim_result = simulation_engine(
                request.experience_content, request.scene_features
            )
        except Exception:
            # 仿真引擎不可用，但前两道已通过 → 放行
            self._stats.total_approved += 1
            result = ArbitrationResult(
                entry_id=request.entry_id,
                conclusion=ArbitrationConclusion.APPROVED,
                reason="第一、二道校验通过，第三道仿真引擎不可用，放行晋升",
                check_details={**check_details, "仿真": "跳过（引擎不可用）"}
            )
            self._cache_result(request.entry_id, result)
            self._log_arbitration(request.entry_id, result)
            self.state = ArbitrationState.IDLE
            return result

        check_details["仿真"] = {
            "结论": sim_result.overall.value,
            "场景相似度": f"{sim_result.scene_similarity:.2f}",
            "碰撞次数": sim_result.collision_count,
            "运行次数": sim_result.total_runs,
            "舒适度违规": sim_result.comfort_violation,
            "法规违规": sim_result.regulation_violation
        }

        if sim_result.overall == SimulationResult.PASSED:
            self._stats.total_approved += 1
            conclusion = ArbitrationConclusion.APPROVED
            reason = "三道安全校验全部通过，批准晋升"
        else:
            self._stats.total_rejected += 1
            conclusion = ArbitrationConclusion.RETAIN_WARNING
            reason = f"第三道仿真校验未通过（碰撞{sim_result.collision_count}次）"

        result = ArbitrationResult(
            entry_id=request.entry_id,
            conclusion=conclusion,
            reason=reason,
            check_details=check_details
        )

        # S-04: S≥0.9 升级为 L5 永久锁定
        result = self._apply_high_s_override(request, result)

        # 缓存结果
        self._cache_result(request.entry_id, result)

        # 更新平均耗时
        elapsed_ms = (time.time() - start_time) * 1000
        n = self._stats.total_requests
        self._stats.avg_duration_ms = (
            (self._stats.avg_duration_ms * (n - 1) + elapsed_ms) / n
        )

        self._log_arbitration(request.entry_id, result)
        self.state = ArbitrationState.IDLE
        return result

    # ========== S-04 高风险自动升级处理 ==========

    def _apply_high_s_override(self, request: ArbitrationRequest,
                               result: ArbitrationResult) -> ArbitrationResult:
        """
        S≥0.9 的失败经验，即使校验未通过也升级为 L5 永久锁定
        """
        if (request.s_value >= self.config.HIGH_S_THRESHOLD and
                result.conclusion == ArbitrationConclusion.RETAIN_WARNING):
            self._stats.total_locked_l5 += 1
            return ArbitrationResult(
                entry_id=request.entry_id,
                conclusion=ArbitrationConclusion.LOCK_L5,
                reason=f"S={request.s_value:.2f}≥{self.config.HIGH_S_THRESHOLD}，高风险警示永久锁定L5",
                check_details=result.check_details
            )
        return result

    # ========== S-05 保守拒绝处理 ==========

    def _create_conservative_result(self, entry_id: str, reason: str) -> ArbitrationResult:
        """外部模块不可用时，保守拒绝"""
        self._stats.total_rejected += 1
        result = ArbitrationResult(
            entry_id=entry_id,
            conclusion=ArbitrationConclusion.RETAIN_WARNING,
            reason=f"保守拒绝: {reason}"
        )
        self._cache_result(entry_id, result)
        self._log_arbitration(entry_id, result)
        self.state = ArbitrationState.IDLE
        return result

    # ========== 仲裁结果缓存管理 ==========

    def _cache_result(self, entry_id: str, result: ArbitrationResult) -> None:
        """缓存仲裁结果"""
        self._arbitration_cache[entry_id] = ArbitrationCacheEntry(
            entry_id=entry_id,
            conclusion=result.conclusion,
            reason=result.reason
        )

    def clear_cache_for_entry(self, entry_id: str) -> None:
        """清除指定条目的仲裁缓存（警示标签降级后调用）"""
        self._arbitration_cache.pop(entry_id, None)

    def clean_expired_cache(self) -> int:
        """清理过期的仲裁缓存"""
        now = time.time()
        expired = []
        for eid, entry in self._arbitration_cache.items():
            if now - entry.cached_at > self.config.CACHE_RETENTION_SEC:
                expired.append(eid)
        for eid in expired:
            del self._arbitration_cache[eid]
        return len(expired)

    # ========== 变更日志 ==========

    def _log_arbitration(self, entry_id: str, result: ArbitrationResult) -> None:
        """记录仲裁事件日志"""
        self._pending_logs.append({
            "log_id": f"arb-{uuid.uuid4().hex[:8]}",
            "event_type": "ARBITRATION_COMPLETE",
            "entry_id": entry_id,
            "conclusion": result.conclusion.value,
            "reason": result.reason,
            "check_details": result.check_details,
            "timestamp": result.result_timestamp
        })

    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs

    # ========== 查询接口 ==========

    def get_cached_conclusion(self, entry_id: str) -> Optional[ArbitrationConclusion]:
        """查询缓存的仲裁结论"""
        cached = self._arbitration_cache.get(entry_id)
        return cached.conclusion if cached else None

    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_requests": self._stats.total_requests,
            "total_approved": self._stats.total_approved,
            "total_rejected": self._stats.total_rejected,
            "total_locked_l5": self._stats.total_locked_l5,
            "total_deferred": self._stats.total_deferred,
            "avg_duration_ms": round(self._stats.avg_duration_ms, 2),
            "cache_size": len(self._arbitration_cache),
            "state": self.state.value
        }


# ==================== 全覆盖单元测试 ====================

if __name__ == "__main__":
    print("=" * 70)
    print("ad-43 失败经验安全仲裁三道校验单元 单元测试")
    print("=" * 70)
    passed, failed = 0, 0

    # ====== 模拟校验回调函数 ======

    def make_request(eid, s_val=0.0, force_majeure=False, decision_action=None):
        return ArbitrationRequest(
            entry_id=eid,
            experience_content={"decision_action": decision_action or {"brake": 5.0}},
            result_label="策略失误",
            scene_features={"road": "高速", "weather": "晴"},
            s_value=s_val,
            force_majeure=force_majeure,
            request_source="L3→L4"
        )

    def law_pass(action, scene):
        return LawCheckResultDetail(overall=LawCheckResult.COMPLIANT)

    def law_fail(action, scene):
        return LawCheckResultDetail(
            overall=LawCheckResult.VIOLATED,
            violated_rules=["未礼让行人"],
            violation_severity="严重"
        )

    def law_error(action, scene):
        raise Exception("法规库连接超时")

    def physics_pass(action):
        return PhysicsCheckResultDetail(overall=PhysicsCheckResult.FEASIBLE)

    def physics_fail(action):
        return PhysicsCheckResultDetail(
            overall=PhysicsCheckResult.EXCEEDED,
            exceeded_parameters={"制动减速度": (1.2, 1.0)}
        )

    def physics_error(action):
        raise Exception("世界模型服务不可用")

    def sim_pass(exp, scene):
        return SimulationCheckResultDetail(
            overall=SimulationResult.PASSED,
            scene_similarity=0.92,
            collision_count=0,
            total_runs=100
        )

    def sim_fail(exp, scene):
        return SimulationCheckResultDetail(
            overall=SimulationResult.FAILED,
            scene_similarity=0.88,
            collision_count=3,
            total_runs=100
        )

    def sim_error(exp, scene):
        raise Exception("仿真引擎离线")

    # ====== 测试用例 ======

    # TC-43-01: 三道全部通过 → 放行晋升
    print("\n[TC-43-01] 三道校验全部通过 → 放行晋升")
    try:
        arb = FailureArbitrationUnit()
        req = make_request("EXP-001")
        result = arb.execute_arbitration(req, law_pass, physics_pass, sim_pass)
        assert result.conclusion == ArbitrationConclusion.APPROVED
        assert arb._stats.total_approved == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1

    # TC-43-02: 法规一票否决 → 保留L3警示
    print("\n[TC-43-02] 法规校验未通过（未礼让行人）→ 保留L3警示")
    try:
        arb = FailureArbitrationUnit()
        req = make_request("EXP-002")
        result = arb.execute_arbitration(req, law_fail, physics_pass, sim_pass)
        assert result.conclusion == ArbitrationConclusion.RETAIN_WARNING
        assert "未礼让行人" in result.reason
        assert arb._stats.total_rejected == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1

    # TC-43-03: 动力学超限 → 保留L3警示
    print("\n[TC-43-03] 动力学校验超限（制动减速度1.2g>1.0g）→ 保留L3警示")
    try:
        arb = FailureArbitrationUnit()
        req = make_request("EXP-003")
        result = arb.execute_arbitration(req, law_pass, physics_fail, sim_pass)
        assert result.conclusion == ArbitrationConclusion.RETAIN_WARNING
        assert "动力学" in result.reason
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1

    # TC-43-04: 仿真未通过 → 保留L3警示
    print("\n[TC-43-04] 仿真回灌未通过（碰撞3次）→ 保留L3警示")
    try:
        arb = FailureArbitrationUnit()
        req = make_request("EXP-004")
        result = arb.execute_arbitration(req, law_pass, physics_pass, sim_fail)
        assert result.conclusion == ArbitrationConclusion.RETAIN_WARNING
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1

    # TC-43-05: 不可抗力直通 → 永久锁定L5
    print("\n[TC-43-05] 不可抗力场景 → 豁免三道校验，永久锁定L5")
    try:
        arb = FailureArbitrationUnit()
        req = make_request("EXP-005", force_majeure=True)
        result = arb.execute_arbitration(req, law_fail, physics_fail, sim_fail)
        assert result.conclusion == ArbitrationConclusion.LOCK_L5
        assert arb._stats.total_locked_l5 == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1

    # TC-43-06: S≥0.9 升级为L5
    print("\n[TC-43-06] S=0.95，动力学未通过 → 升级为永久锁定L5")
    try:
        arb = FailureArbitrationUnit()
        req = make_request("EXP-006", s_val=0.95)
        result = arb.execute_arbitration(req, law_pass, physics_fail, sim_pass)
        assert result.conclusion == ArbitrationConclusion.LOCK_L5
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1

    # TC-43-07: 法规库异常 → 保守拒绝
    print("\n[TC-43-07] 法规库异常 → 保守拒绝")
    try:
        arb = FailureArbitrationUnit()
        req = make_request("EXP-007")
        result = arb.execute_arbitration(req, law_error, physics_pass, sim_pass)
        assert result.conclusion == ArbitrationConclusion.RETAIN_WARNING
        assert "保守拒绝" in result.reason
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1

    # TC-43-08: 仿真引擎不可用，前两道通过 → 放行
    print("\n[TC-43-08] 仿真引擎不可用，前两道通过 → 放行晋升")
    try:
        arb = FailureArbitrationUnit()
        req = make_request("EXP-008")
        result = arb.execute_arbitration(req, law_pass, physics_pass, sim_error)
        assert result.conclusion == ArbitrationConclusion.APPROVED
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1

    # TC-43-09: 重复仲裁返回缓存
    print("\n[TC-43-09] 已有结论保留L3警示，重复请求直接返回缓存")
    try:
        arb = FailureArbitrationUnit()
        req = make_request("EXP-009")
        # 第一次：法规不通过 → 保留L3警示
        arb.execute_arbitration(req, law_fail, physics_pass, sim_pass)
        # 第二次：即使传入全通过的校验器，也应返回缓存结论
        result2 = arb.execute_arbitration(req, law_pass, physics_pass, sim_pass)
        assert result2.conclusion == ArbitrationConclusion.RETAIN_WARNING
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1

    # TC-43-10: 暂停状态暂缓
    print("\n[TC-43-10] 仲裁单元暂停 → 返回暂缓")
    try:
        arb = FailureArbitrationUnit()
        arb.pause()
        req = make_request("EXP-010")
        result = arb.execute_arbitration(req, law_pass, physics_pass, sim_pass)
        assert result.conclusion == ArbitrationConclusion.DEFERRED
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1

    # TC-43-11: 缓存过期清理
    print("\n[TC-43-11] 缓存过期清理")
    try:
        arb = FailureArbitrationUnit()
        arb.config.CACHE_RETENTION_SEC = 0  # 立即过期
        req = make_request("EXP-011")
        arb.execute_arbitration(req, law_fail, physics_pass, sim_pass)
        cleaned = arb.clean_expired_cache()
        assert cleaned == 1
        assert arb.get_cached_conclusion("EXP-011") is None
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1

    # TC-43-12: 统计信息完整
    print("\n[TC-43-12] 运行统计信息完整")
    try:
        arb = FailureArbitrationUnit()
        for i in range(3):
            arb.execute_arbitration(make_request(f"STAT-{i}"), law_pass, physics_pass, sim_pass)
        stats = arb.get_statistics()
        assert stats["total_requests"] == 3
        assert stats["total_approved"] == 3
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1

    print(f"\n测试结果: {passed} PASS, {failed} FAIL")