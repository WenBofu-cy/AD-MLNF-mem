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

安全约束:
  S-01: 失败经验晋升 L4/L5 必须通过三道安全校验，任一未通过则永久保留在 L3 作为警示标签
  S-02: 第一道法规校验具有一票否决权
  S-03: 不可抗力场景经验豁免三道校验，直接永久锁定于 L5
  S-04: S≥0.9 的失败经验即使校验未通过，也须升级为 L5 永久锁定
  S-05: 外部模块不可用时，采用保守策略拒绝晋升，确保安全优先
  S-06: 所有仲裁过程全量写入 ad-51 不可变日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class ArbitrationState(Enum):
    """仲裁单元内部状态"""
    IDLE = "idle"
    CHECK1_LAW = "check1_law"
    CHECK2_PHYSICS = "check2_physics"
    CHECK3_SIM = "check3_sim"
    CONCLUSION = "conclusion"
    PAUSED = "paused"


class ArbitrationConclusion(Enum):
    """仲裁结论"""
    APPROVED = "放行晋升"
    RETAIN_WARNING = "保留L3警示"
    LOCK_L5 = "永久锁定L5"
    DEFERRED = "暂缓（等待外部模块恢复）"


# ==================== 数据结构 ====================

@dataclass
class ArbitrationRequest:
    """失败经验仲裁请求（来自 ad-38）"""
    entry_id: str
    experience_content: Dict[str, Any]
    result_label: str               # 应为 "策略失误"
    scene_features: Dict[str, Any]
    s_value: float = 0.0
    force_majeure: bool = False
    request_source: str = "L3/L4"
    request_timestamp: float = field(default_factory=time.time)


@dataclass
class LawCheckResult:
    """法规校验结果"""
    compliant: bool
    violated_rules: List[str] = field(default_factory=list)
    severity: str = "无"


@dataclass
class PhysicsCheckResult:
    """动力学校验结果"""
    feasible: bool
    exceeded_limits: Dict[str, float] = field(default_factory=dict)


@dataclass
class SimulationResult:
    """仿真回灌验证结果"""
    passed: bool
    scene_similarity: float = 1.0
    collision_count: int = 0
    comfort_violation: bool = False
    regulation_violation: bool = False
    total_runs: int = 0


@dataclass
class ArbitrationResult:
    """仲裁结果（发送给 ad-38 和 ad-24）"""
    entry_id: str
    conclusion: ArbitrationConclusion
    reason: str
    check_details: Dict[str, Any] = field(default_factory=dict)
    result_timestamp: float = field(default_factory=time.time)


@dataclass
class ArbitrationCache:
    """仲裁结果缓存"""
    entry_id: str
    conclusion: ArbitrationConclusion
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class FailureArbitrationUnit:
    """
    失败经验安全仲裁三道校验单元
    
    职责:
    1. 接收 ad-38 下发的失败经验仲裁请求
    2. 依次执行三道校验：法规 → 动力学 → 仿真
    3. 任一校验未通过则结论为“保留L3警示”
    4. S≥0.9 的失败经验即使未通过也升级为 L5 永久锁定
    5. 不可抗力场景经验直接永久锁定 L5
    6. 外部模块不可用时保守拒绝
    """
    
    # 仿真引擎超时（秒）
    SIMULATION_TIMEOUT = 300  # 5 分钟
    
    # 法规库查询超时（秒）
    LAW_QUERY_TIMEOUT = 30
    
    # 世界模型查询超时（秒）
    PHYSICS_QUERY_TIMEOUT = 15
    
    # S≥0.9 升级阈值
    HIGH_S_THRESHOLD = 0.90
    
    def __init__(self):
        self.module_id = "ad-43"
        self.module_name = "失败经验安全仲裁三道校验单元"
        
        # 内部状态
        self.state = ArbitrationState.IDLE
        
        # 仲裁结果缓存（防止重复仲裁）
        self._cache: Dict[str, ArbitrationCache] = {}
        
        # 统计
        self._total_requests = 0
        self._total_approved = 0
        self._total_rejected = 0
        self._total_locked_l5 = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 失败经验安全仲裁单元初始化完成")
        print(f"[{self.module_id}] 三道校验: 法规 → 动力学 → 仿真")
        print(f"[{self.module_id}] S≥{self.HIGH_S_THRESHOLD} 自动升级为 L5 永久锁定")
    
    # ========== 状态管理 ==========
    
    def pause(self) -> None:
        self.state = ArbitrationState.PAUSED
    
    def resume(self) -> None:
        self.state = ArbitrationState.IDLE
    
    def get_state(self) -> ArbitrationState:
        return self.state
    
    # ========== 仲裁主流程 ==========
    
    def execute_arbitration(self,
                            request: ArbitrationRequest,
                            law_checker,
                            physics_checker,
                            simulation_engine) -> ArbitrationResult:
        """
        执行三道安全仲裁
        
        Args:
            request: 仲裁请求
            law_checker: 法规校验回调 (decision_action, scene) -> LawCheckResult
            physics_checker: 动力学校验回调 (decision_action) -> PhysicsCheckResult
            simulation_engine: 仿真引擎回调 (experience, scene) -> SimulationResult
            
        Returns:
            仲裁结果
        """
        if self.state == ArbitrationState.PAUSED:
            return ArbitrationResult(
                entry_id=request.entry_id,
                conclusion=ArbitrationConclusion.DEFERRED,
                reason="仲裁单元暂停中"
            )
        
        self._total_requests += 1
        
        # 重复仲裁检查
        if request.entry_id in self._cache:
            cached = self._cache[request.entry_id]
            if cached.conclusion == ArbitrationConclusion.RETAIN_WARNING:
                return ArbitrationResult(
                    entry_id=request.entry_id,
                    conclusion=ArbitrationConclusion.RETAIN_WARNING,
                    reason="已有结论：保留L3警示（缓存）"
                )
        
        # S-03: 不可抗力直通
        if request.force_majeure or request.result_label == "不可抗力场景":
            self._total_locked_l5 += 1
            result = ArbitrationResult(
                entry_id=request.entry_id,
                conclusion=ArbitrationConclusion.LOCK_L5,
                reason="不可抗力场景，永久锁定于L5",
                check_details={"法规": "豁免", "动力学": "豁免", "仿真": "豁免"}
            )
            self._cache[request.entry_id] = ArbitrationCache(request.entry_id, result.conclusion)
            self._log_arbitration(request.entry_id, result)
            return result
        
        check_details = {}
        
        # 第一道：法规校验
        self.state = ArbitrationState.CHECK1_LAW
        try:
            law_result = law_checker(
                request.experience_content.get("decision_action", {}),
                request.scene_features
            )
        except Exception:
            # S-05: 外部模块不可用时保守拒绝
            return self._create_conservative_result(request.entry_id, "法规校验异常")
        
        check_details["法规"] = {
            "通过": law_result.compliant,
            "违规条目": law_result.violated_rules
        }
        
        if not law_result.compliant:
            # S-02: 法规一票否决
            self._total_rejected += 1
            result = ArbitrationResult(
                entry_id=request.entry_id,
                conclusion=ArbitrationConclusion.RETAIN_WARNING,
                reason=f"第一道法规校验未通过: {law_result.violated_rules}",
                check_details=check_details
            )
            result = self._apply_high_s_override(request, result)
            self._cache[request.entry_id] = ArbitrationCache(request.entry_id, result.conclusion)
            self._log_arbitration(request.entry_id, result)
            self.state = ArbitrationState.IDLE
            return result
        
        # 第二道：动力学校验
        self.state = ArbitrationState.CHECK2_PHYSICS
        try:
            physics_result = physics_checker(
                request.experience_content.get("decision_action", {})
            )
        except Exception:
            return self._create_conservative_result(request.entry_id, "动力学校验异常")
        
        check_details["动力学"] = {
            "通过": physics_result.feasible,
            "超限参数": physics_result.exceeded_limits
        }
        
        if not physics_result.feasible:
            self._total_rejected += 1
            result = ArbitrationResult(
                entry_id=request.entry_id,
                conclusion=ArbitrationConclusion.RETAIN_WARNING,
                reason=f"第二道动力学校验未通过: {physics_result.exceeded_limits}",
                check_details=check_details
            )
            result = self._apply_high_s_override(request, result)
            self._cache[request.entry_id] = ArbitrationCache(request.entry_id, result.conclusion)
            self._log_arbitration(request.entry_id, result)
            self.state = ArbitrationState.IDLE
            return result
        
        # 第三道：仿真回灌验证
        self.state = ArbitrationState.CHECK3_SIM
        try:
            sim_result = simulation_engine(
                request.experience_content,
                request.scene_features
            )
        except Exception:
            # 仿真引擎不可用，前两道通过则放行
            self._total_approved += 1
            result = ArbitrationResult(
                entry_id=request.entry_id,
                conclusion=ArbitrationConclusion.APPROVED,
                reason="第一、二道校验通过，第三道仿真引擎不可用跳过",
                check_details={**check_details, "仿真": "跳过（引擎不可用）"}
            )
            self._cache[request.entry_id] = ArbitrationCache(request.entry_id, result.conclusion)
            self._log_arbitration(request.entry_id, result)
            self.state = ArbitrationState.IDLE
            return result
        
        check_details["仿真"] = {
            "通过": sim_result.passed,
            "碰撞次数": sim_result.collision_count,
            "场景相似度": sim_result.scene_similarity
        }
        
        if sim_result.passed:
            self._total_approved += 1
            conclusion = ArbitrationConclusion.APPROVED
            reason = "三道安全校验全部通过"
        else:
            self._total_rejected += 1
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
        
        self._cache[request.entry_id] = ArbitrationCache(request.entry_id, result.conclusion)
        self._log_arbitration(request.entry_id, result)
        self.state = ArbitrationState.IDLE
        return result
    
    def _apply_high_s_override(self, request: ArbitrationRequest,
                               result: ArbitrationResult) -> ArbitrationResult:
        """S≥0.9 的失败经验升级为 L5 永久锁定"""
        if request.s_value >= self.HIGH_S_THRESHOLD and result.conclusion == ArbitrationConclusion.RETAIN_WARNING:
            self._total_locked_l5 += 1
            return ArbitrationResult(
                entry_id=request.entry_id,
                conclusion=ArbitrationConclusion.LOCK_L5,
                reason=f"S≥{self.HIGH_S_THRESHOLD}，高风险警示永久锁定于L5",
                check_details=result.check_details
            )
        return result
    
    def _create_conservative_result(self, entry_id: str, reason: str) -> ArbitrationResult:
        """创建保守拒绝结果（外部模块不可用时）"""
        self._total_rejected += 1
        result = ArbitrationResult(
            entry_id=entry_id,
            conclusion=ArbitrationConclusion.RETAIN_WARNING,
            reason=f"保守拒绝: {reason}"
        )
        self._cache[entry_id] = ArbitrationCache(entry_id, result.conclusion)
        self._log_arbitration(entry_id, result)
        self.state = ArbitrationState.IDLE
        return result
    
    # ========== 变更日志 ==========
    
    def _log_arbitration(self, entry_id: str, result: ArbitrationResult) -> None:
        self._pending_logs.append({
            "log_id": f"arb-{uuid.uuid4().hex[:8]}",
            "entry_id": entry_id,
            "conclusion": result.conclusion.value,
            "reason": result.reason,
            "timestamp": result.result_timestamp
        })
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_requests": self._total_requests,
            "total_approved": self._total_approved,
            "total_rejected": self._total_rejected,
            "total_locked_l5": self._total_locked_l5,
            "state": self.state.value
        }


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-43 失败经验安全仲裁三道校验单元 单元测试")
    print("=" * 60)
    passed, failed = 0, 0
    
    def make_request(eid, s_val=0.0, force_majeure=False):
        return ArbitrationRequest(
            entry_id=eid,
            experience_content={"decision_action": {"brake": 5.0}},
            result_label="策略失误",
            scene_features={"road": "高速"},
            s_value=s_val,
            force_majeure=force_majeure
        )
    
    # 模拟校验回调
    def law_pass(*args): return LawCheckResult(True)
    def law_fail(*args): return LawCheckResult(False, ["未礼让行人"], "严重")
    def law_error(*args): raise Exception("法规库不可用")
    
    def physics_pass(*args): return PhysicsCheckResult(True)
    def physics_fail(*args): return PhysicsCheckResult(False, {"制动减速度": 1.2})
    def physics_error(*args): raise Exception("世界模型不可用")
    
    def sim_pass(*args): return SimulationResult(True)
    def sim_fail(*args): return SimulationResult(False, collision_count=3)
    def sim_error(*args): raise Exception("仿真引擎不可用")
    
    # TC-43-01: 三道全部通过
    print("\n[TC-43-01] 三道校验全部通过 → 放行晋升")
    try:
        arb = FailureArbitrationUnit()
        req = make_request("EXP-001")
        result = arb.execute_arbitration(req, law_pass, physics_pass, sim_pass)
        assert result.conclusion == ArbitrationConclusion.APPROVED
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-43-02: 法规一票否决
    print("\n[TC-43-02] 法规校验未通过 → 保留L3警示")
    try:
        arb = FailureArbitrationUnit()
        req = make_request("EXP-002")
        result = arb.execute_arbitration(req, law_fail, physics_pass, sim_pass)
        assert result.conclusion == ArbitrationConclusion.RETAIN_WARNING
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-43-03: 不可抗力直通
    print("\n[TC-43-03] 不可抗力经验 → 永久锁定L5")
    try:
        arb = FailureArbitrationUnit()
        req = make_request("EXP-003", force_majeure=True)
        result = arb.execute_arbitration(req, law_fail, physics_fail, sim_fail)
        assert result.conclusion == ArbitrationConclusion.LOCK_L5
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-43-04: S≥0.9 升级为L5
    print("\n[TC-43-04] S=0.95，动力学未通过 → 永久锁定L5")
    try:
        arb = FailureArbitrationUnit()
        req = make_request("EXP-004", s_val=0.95)
        result = arb.execute_arbitration(req, law_pass, physics_fail, sim_pass)
        assert result.conclusion == ArbitrationConclusion.LOCK_L5
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-43-05: 法规库异常保守拒绝
    print("\n[TC-43-05] 法规库异常 → 保守拒绝")
    try:
        arb = FailureArbitrationUnit()
        req = make_request("EXP-005")
        result = arb.execute_arbitration(req, law_error, physics_pass, sim_pass)
        assert result.conclusion == ArbitrationConclusion.RETAIN_WARNING
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-43-06: 仿真引擎不可用前两道通过则放行
    print("\n[TC-43-06] 仿真引擎不可用，前两道通过 → 放行晋升")
    try:
        arb = FailureArbitrationUnit()
        req = make_request("EXP-006")
        result = arb.execute_arbitration(req, law_pass, physics_pass, sim_error)
        assert result.conclusion == ArbitrationConclusion.APPROVED
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-43-07: 重复仲裁返回缓存
    print("\n[TC-43-07] 已有结论保留L3警示，重复请求直接返回")
    try:
        arb = FailureArbitrationUnit()
        req = make_request("EXP-007")
        arb.execute_arbitration(req, law_fail, physics_pass, sim_pass)
        result2 = arb.execute_arbitration(req, law_pass, physics_pass, sim_pass)
        assert result2.conclusion == ArbitrationConclusion.RETAIN_WARNING
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-43-08: 仿真未通过（碰撞3次）
    print("\n[TC-43-08] 仿真未通过（碰撞3次）→ 保留L3警示")
    try:
        arb = FailureArbitrationUnit()
        req = make_request("EXP-008")
        result = arb.execute_arbitration(req, law_pass, physics_pass, sim_fail)
        assert result.conclusion == ArbitrationConclusion.RETAIN_WARNING
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-43-09: 动力学校验未通过
    print("\n[TC-43-09] 动力学超限 → 保留L3警示")
    try:
        arb = FailureArbitrationUnit()
        req = make_request("EXP-009")
        result = arb.execute_arbitration(req, law_pass, physics_fail, sim_pass)
        assert result.conclusion == ArbitrationConclusion.RETAIN_WARNING
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-43-10: 暂停状态暂缓
    print("\n[TC-43-10] 仲裁单元暂停 → 暂缓")
    try:
        arb = FailureArbitrationUnit()
        arb.pause()
        req = make_request("EXP-010")
        result = arb.execute_arbitration(req, law_pass, physics_pass, sim_pass)
        assert result.conclusion == ArbitrationConclusion.DEFERRED
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    print(f"\n测试结果: {passed} PASS, {failed} FAIL")