#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-45
模块名称: 交通法律法规库
所属分区: 四、漏斗外挂扩展区（物理隔离）
核心职责: 存储法定通行刚性规则与通行逻辑，以"场景条件-强制执行动作"形式固化，
          为 ECC 认知大脑提供法规合规底线查询。法规库独立于双漏斗记忆系统运行，
          不参与记忆的沉淀、筛选、晋升与遗忘机制。法规条目固化不可更改，
          仅接受经审批的追加写入。是自动驾驶系统决策的最终合规终审依据。

依赖模块: ECC-05 伦理仲裁模块（主要查询方）、ECC-03 因果推理模块（查询方）、
          ECC-04 心智模拟模块（查询方）、ad-09（行为判定标签单元）
被依赖模块: ECC-05（消费法规条目进行伦理仲裁）、ECC-03/04（消费法规约束进行推理与模拟）、
            ad-09（消费法规基准判定驾驶行为）

法规分级:
  - 硬约束: 基础通行禁令、特种车辆优先、路权划分标准。编译期固化，不可违抗。
  - 软约束: 非铺装道路通行规则，结合场景判断。
  - 软追加: 地方临时管制、阶段性新规，需人工审核后追加。

安全约束:
  S-01: 交通法规库独立于双漏斗记忆系统，不参与记忆机制
  S-02: 硬约束法规条目编译期固化，不可通过运行时 OTA 修改或删除
  S-03: 软约束条目可经审批追加，须通过数字签名校验与冲突检测
  S-04: 极端安全困境下的临时伦理豁免须 ECC-05 发起，有效期仅 10 秒，强制事后审计
  S-05: 法规库数据须每 24 小时校验一次数字签名与完整性
  S-06: 法规库为自动驾驶系统决策的最终合规底线，任何自主学习的驾驶策略不得突破法规硬约束
  S-07: 所有法规查询、豁免、追加操作全量写入 ad-51 不可变日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid
import hashlib


# ==================== 枚举定义 ====================

class LawRigidity(Enum):
    """法规刚性等级"""
    HARD = "硬约束"        # 编译期固化，不可违抗
    SOFT = "软约束"        # 结合场景判断
    SOFT_APPEND = "软追加" # 审批后追加


class LawCategory(Enum):
    """法规类别"""
    BASIC_BAN = "基础通行禁令"
    SPECIAL_VEHICLE = "特种车辆优先"
    RIGHT_OF_WAY = "路权划分标准"
    UNPAVED_ROAD = "非铺装道路通行规则"
    DYNAMIC_APPEND = "动态更新通道"


class LawDBState(Enum):
    """法规库内部状态"""
    NORMAL = "normal"
    QUERYING = "querying"
    APPENDING = "appending"
    VALIDATING = "validating"
    DEGRADED = "degraded"
    PAUSED = "paused"


# ==================== 数据结构 ====================

@dataclass
class LawEntry:
    """法规条目"""
    law_id: str
    category: LawCategory
    content: str                     # 规则内容
    rigidity: LawRigidity
    scene_conditions: Dict[str, Any] # 场景匹配条件
    mandatory_action: str            # 强制执行动作
    violation_consequence: str = ""  # 违规后果
    effective_date: float = field(default_factory=time.time)


@dataclass
class LawQueryRequest:
    """法规查询请求"""
    query_id: str
    scene_conditions: Dict[str, Any]  # 场景条件（路口类型、信号灯状态、道路等级等）
    query_type: str = "full"          # "full" / "hard_only" / "baseline"
    behavior_type: Optional[str] = None  # 行为类型（用于基准查询）
    source_module: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class LawQueryResponse:
    """法规查询响应"""
    query_id: str
    success: bool
    applicable_laws: List[LawEntry] = field(default_factory=list)
    baseline_params: Optional[Dict[str, Any]] = None  # 法规基准判定参数
    message: str = ""


@dataclass
class EthicalExemptionRequest:
    """临时伦理豁免请求（来自 ECC-05）"""
    request_id: str
    target_law_id: str               # 需要豁免的法规条目 ID
    reason: str                      # 豁免原因（必须为"避免不可逆重度人身伤害"）
    scene_snapshot: Dict[str, Any]   # 场景快照
    source_module: str = "ECC-05"
    timestamp: float = field(default_factory=time.time)


@dataclass
class EthicalExemptionCertificate:
    """临时伦理豁免凭证"""
    certificate_id: str
    exempted_law_id: str
    reason: str
    valid_until: float               # 有效期截止时间（10秒）
    audit_required: bool = True
    scene_hash: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class LawAppendRequest:
    """法规追加请求（经 ECC-12 审批）"""
    request_id: str
    new_law: LawEntry
    approval_token: str
    digital_signature: str           # 数字签名
    reason: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class LawDBStatusSnapshot:
    """法规库状态快照"""
    total_laws: int
    hard_laws: int
    soft_laws: int
    soft_append_laws: int
    law_version: int
    data_hash: str
    state: str


# ==================== 默认法规条目库 ====================

DEFAULT_LAWS: List[Dict[str, Any]] = [
    # === 基础通行禁令（硬约束） ===
    {"law_id": "LAW-001", "category": LawCategory.BASIC_BAN, "rigidity": LawRigidity.HARD,
     "content": "红灯必须停车，不得越过停止线",
     "scene_conditions": {"signal_light": "红灯", "has_stop_line": True},
     "mandatory_action": "在停止线前完全停车"},
    {"law_id": "LAW-002", "category": LawCategory.BASIC_BAN, "rigidity": LawRigidity.HARD,
     "content": "人行横道上有行人时，机动车必须停车让行",
     "scene_conditions": {"crosswalk": True, "pedestrian_present": True},
     "mandatory_action": "停车让行，等待行人通过"},
    {"law_id": "LAW-003", "category": LawCategory.BASIC_BAN, "rigidity": LawRigidity.HARD,
     "content": "路段最高限速硬性约束，任何情况下不得突破",
     "scene_conditions": {"has_speed_limit": True},
     "mandatory_action": "车速 ≤ 路段限速"},
    {"law_id": "LAW-004", "category": LawCategory.BASIC_BAN, "rigidity": LawRigidity.HARD,
     "content": "实线区域禁止变道",
     "scene_conditions": {"lane_line": "实线"},
     "mandatory_action": "保持在当前车道"},
    {"law_id": "LAW-005", "category": LawCategory.BASIC_BAN, "rigidity": LawRigidity.HARD,
     "content": "禁止逆向行驶",
     "scene_conditions": {"road_direction": "逆行"},
     "mandatory_action": "立即纠正方向"},
    {"law_id": "LAW-006", "category": LawCategory.BASIC_BAN, "rigidity": LawRigidity.HARD,
     "content": "禁止占用非机动车道行驶",
     "scene_conditions": {"lane_type": "非机动车道"},
     "mandatory_action": "驶离非机动车道"},
    {"law_id": "LAW-007", "category": LawCategory.BASIC_BAN, "rigidity": LawRigidity.HARD,
     "content": "禁止在高速公路上停车（紧急情况除外）",
     "scene_conditions": {"road_level": "高速公路", "is_emergency": False},
     "mandatory_action": "不得无故停车"},
    # === 特种车辆优先（硬约束） ===
    {"law_id": "LAW-010", "category": LawCategory.SPECIAL_VEHICLE, "rigidity": LawRigidity.HARD,
     "content": "检测到救护车/消防车/警车声光信号，立即执行主动让行",
     "scene_conditions": {"emergency_vehicle_detected": True},
     "mandatory_action": "靠右减速或停车让行"},
    {"law_id": "LAW-011", "category": LawCategory.SPECIAL_VEHICLE, "rigidity": LawRigidity.HARD,
     "content": "特种车辆接近时，本车应靠右减速或停车让行",
     "scene_conditions": {"emergency_vehicle_approaching": True},
     "mandatory_action": "靠右减速或停车让行"},
    # === 路权划分标准（硬约束） ===
    {"law_id": "LAW-020", "category": LawCategory.RIGHT_OF_WAY, "rigidity": LawRigidity.HARD,
     "content": "直行车辆优先于转弯车辆",
     "scene_conditions": {"intersection": True, "self_turning": True, "other_straight": True},
     "mandatory_action": "让直行车先行"},
    {"law_id": "LAW-021", "category": LawCategory.RIGHT_OF_WAY, "rigidity": LawRigidity.HARD,
     "content": "转弯车辆让直行车辆",
     "scene_conditions": {"intersection": True, "self_turning": True},
     "mandatory_action": "让直行车先行"},
    {"law_id": "LAW-022", "category": LawCategory.RIGHT_OF_WAY, "rigidity": LawRigidity.HARD,
     "content": "辅路车辆让主路车辆",
     "scene_conditions": {"road_type": "辅路", "main_road_vehicle": True},
     "mandatory_action": "让主路车先行"},
    {"law_id": "LAW-023", "category": LawCategory.RIGHT_OF_WAY, "rigidity": LawRigidity.HARD,
     "content": "环形路口内车辆优先",
     "scene_conditions": {"intersection_type": "环形路口", "inside_vehicle": True},
     "mandatory_action": "让环岛内车辆先行"},
    {"law_id": "LAW-024", "category": LawCategory.RIGHT_OF_WAY, "rigidity": LawRigidity.HARD,
     "content": "右转让左转（相对方向）",
     "scene_conditions": {"intersection": True, "self_right_turning": True, "other_left_turning": True},
     "mandatory_action": "让左转车先行"},
    # === 非铺装道路通行规则（软约束） ===
    {"law_id": "LAW-030", "category": LawCategory.UNPAVED_ROAD, "rigidity": LawRigidity.SOFT,
     "content": "无标线道路默认靠右行驶",
     "scene_conditions": {"road_marking": "无标线"},
     "mandatory_action": "靠右行驶"},
    {"law_id": "LAW-031", "category": LawCategory.UNPAVED_ROAD, "rigidity": LawRigidity.SOFT,
     "content": "窄路会车：下坡车让上坡车",
     "scene_conditions": {"narrow_road": True, "slope": True, "self_downhill": True},
     "mandatory_action": "让上坡车先行"},
    {"law_id": "LAW-032", "category": LawCategory.UNPAVED_ROAD, "rigidity": LawRigidity.SOFT,
     "content": "狭窄桥段：先到先过",
     "scene_conditions": {"narrow_bridge": True},
     "mandatory_action": "先到桥口者优先通行"},
    {"law_id": "LAW-033", "category": LawCategory.UNPAVED_ROAD, "rigidity": LawRigidity.SOFT,
     "content": "乡村非铺装道路：无明确限速时默认 ≤ 40km/h",
     "scene_conditions": {"road_type": "非铺装", "has_speed_limit": False},
     "mandatory_action": "车速 ≤ 40km/h"},
]


# ==================== 主类定义 ====================

class TrafficLawDatabase:
    """
    交通法律法规库 - 漏斗外挂扩展区
    
    职责:
    1. 存储法定通行刚性规则与通行逻辑
    2. 提供场景条件匹配的法规查询
    3. 提供法规基准判定参数（供 ad-09 使用）
    4. 处理极端安全困境的临时伦理豁免
    5. 处理经 ECC-12 审批的法规追加
    6. 数据完整性定期校验
    """
    
    # 授权查询的模块列表
    AUTHORIZED_QUERY_MODULES = {
        "ECC-03", "ECC-04", "ECC-05", "ad-09", "ad-16", "ad-43"
    }
    
    # 临时伦理豁免有效期（秒）
    EXEMPTION_VALIDITY_SECONDS = 10
    
    def __init__(self):
        self.module_id = "ad-45"
        self.module_name = "交通法律法规库"
        
        # 内部状态
        self.state = LawDBState.NORMAL
        
        # 法规条目库: law_id -> LawEntry
        self._laws: Dict[str, LawEntry] = {}
        
        # 法规版本号
        self._law_version = 1
        
        # 数据完整性哈希
        self._data_hash = ""
        
        # 初始化默认法规
        self._init_default_laws()
        
        # 统计
        self._total_queries = 0
        self._total_exemptions = 0
        self._total_appends = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 交通法律法规库初始化完成")
        print(f"[{self.module_id}] 法规总数: {len(self._laws)}, 硬约束: {self._count_by_rigidity(LawRigidity.HARD)}")
    
    def _init_default_laws(self) -> None:
        """初始化默认法规条目"""
        for law_data in DEFAULT_LAWS:
            entry = LawEntry(
                law_id=law_data["law_id"],
                category=law_data["category"],
                content=law_data["content"],
                rigidity=law_data["rigidity"],
                scene_conditions=law_data["scene_conditions"],
                mandatory_action=law_data["mandatory_action"]
            )
            self._laws[entry.law_id] = entry
        self._update_data_hash()
    
    def _count_by_rigidity(self, rigidity: LawRigidity) -> int:
        return sum(1 for law in self._laws.values() if law.rigidity == rigidity)
    
    def _update_data_hash(self) -> None:
        """更新数据完整性哈希"""
        raw = "".join(f"{lid}{str(law)}" for lid, law in sorted(self._laws.items()))
        self._data_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
    
    # ========== 状态管理 ==========
    
    def pause(self) -> None:
        self.state = LawDBState.PAUSED
    
    def resume(self) -> None:
        self.state = LawDBState.NORMAL
    
    def get_state(self) -> LawDBState:
        return self.state
    
    # ========== 法规查询 ==========
    
    def query_laws(self, request: LawQueryRequest) -> LawQueryResponse:
        """
        根据场景条件查询适用法规条目
        
        Args:
            request: 法规查询请求
            
        Returns:
            法规查询响应
        """
        self._total_queries += 1
        
        if request.source_module not in self.AUTHORIZED_QUERY_MODULES:
            return LawQueryResponse(
                query_id=request.query_id,
                success=False,
                message="未授权模块"
            )
        
        self.state = LawDBState.QUERYING
        
        # 匹配场景条件
        applicable = []
        for law in self._laws.values():
            if request.query_type == "hard_only" and law.rigidity != LawRigidity.HARD:
                continue
            if self._match_conditions(request.scene_conditions, law.scene_conditions):
                applicable.append(law)
        
        # 按刚性等级排序（硬约束优先）
        rigidity_order = {LawRigidity.HARD: 0, LawRigidity.SOFT: 1, LawRigidity.SOFT_APPEND: 2}
        applicable.sort(key=lambda x: rigidity_order.get(x.rigidity, 99))
        
        self.state = LawDBState.NORMAL
        
        return LawQueryResponse(
            query_id=request.query_id,
            success=True,
            applicable_laws=applicable
        )
    
    def query_baseline(self, behavior_type: str, scene_label: str, road_level: str) -> Optional[Dict[str, Any]]:
        """
        查询法规基准判定参数（供 ad-09 行为判定使用）
        
        Args:
            behavior_type: 行为类型（变道/跟车/制动等）
            scene_label: 场景标签
            road_level: 道路等级
            
        Returns:
            法规基准参数
        """
        # 内置判定基准
        baselines = {
            "变道": {"合规阈值": {"转向灯提前": 3.0, "转角速率上限": 200.0},
                     "违规判定": "转向灯提前 < 1.0秒 或 未开启"},
            "跟车": {"合规阈值": {"跟车时距": 2.0},
                     "违规判定": "跟车时距 < 1.5秒"},
            "制动": {"合规阈值": {"制动减速度": 3.0},
                     "违规判定": "制动减速度 > 5.0m/s²（非紧急）"},
            "加速": {"合规阈值": {"纵向冲击度": 3.0},
                     "违规判定": "纵向冲击度 > 5.0m/s³"},
            "转弯": {"合规阈值": {"车速限速比": 0.7, "转角速率上限": 200.0},
                     "违规判定": "车速限速比 > 0.9"},
            "让行": {"合规阈值": {"行人优先": True},
                     "违规判定": "未礼让人行横道行人（硬约束）"},
        }
        
        return baselines.get(behavior_type)
    
    def _match_conditions(self, query_conditions: Dict[str, Any], law_conditions: Dict[str, Any]) -> bool:
        """检查场景条件是否匹配法规条件"""
        for key, value in law_conditions.items():
            if key not in query_conditions:
                return False
            if query_conditions[key] != value:
                return False
        return True
    
    # ========== 临时伦理豁免 ==========
    
    def request_exemption(self, request: EthicalExemptionRequest) -> Tuple[bool, Optional[EthicalExemptionCertificate], str]:
        """
        处理极端安全困境的临时伦理豁免请求
        
        S-04: 豁免有效期仅 10 秒，强制事后审计
        
        Args:
            request: 豁免请求
            
        Returns:
            (是否批准, 豁免凭证, 消息)
        """
        # 验证豁免条件
        if request.reason != "避免不可逆重度人身伤害":
            return False, None, "豁免条件不满足：原因必须为'避免不可逆重度人身伤害'"
        
        # 检查目标法规是否为硬约束
        target_law = self._laws.get(request.target_law_id)
        if target_law is None:
            return False, None, f"法规条目 {request.target_law_id} 不存在"
        
        if target_law.rigidity != LawRigidity.HARD:
            return False, None, "仅硬约束法规可申请临时豁免"
        
        # 生成豁免凭证
        self._total_exemptions += 1
        certificate = EthicalExemptionCertificate(
            certificate_id=f"exempt-{uuid.uuid4().hex[:8]}",
            exempted_law_id=request.target_law_id,
            reason=request.reason,
            valid_until=time.time() + self.EXEMPTION_VALIDITY_SECONDS,
            audit_required=True,
            scene_hash=hashlib.sha256(str(request.scene_snapshot).encode()).hexdigest()[:16]
        )
        
        self._log_event("ETHICAL_EXEMPTION", {
            "target_law": request.target_law_id,
            "reason": request.reason,
            "valid_until": certificate.valid_until,
            "scene_hash": certificate.scene_hash
        })
        
        print(f"[{self.module_id}] 临时伦理豁免: {request.target_law_id}, 有效期 {self.EXEMPTION_VALIDITY_SECONDS}s")
        return True, certificate, "临时伦理豁免已批准，有效期10秒，强制事后审计"
    
    # ========== 审批追加 ==========
    
    def append_law(self, request: LawAppendRequest,
                   signature_validator, token_validator) -> Tuple[bool, str]:
        """
        处理经 ECC-12 审批的法规追加
        
        S-03: 须通过数字签名校验与冲突检测
        S-02: 仅允许追加软约束或软追加条目
        
        Args:
            request: 法规追加请求
            signature_validator: 数字签名验证回调
            token_validator: 令牌验证回调
            
        Returns:
            (成功, 消息)
        """
        # 验证审批令牌
        if not token_validator(request.approval_token):
            return False, "审批令牌无效"
        
        # 验证数字签名
        if not signature_validator(request.digital_signature, request.new_law):
            return False, "数字签名无效"
        
        # 仅允许追加软约束或软追加
        if request.new_law.rigidity not in [LawRigidity.SOFT, LawRigidity.SOFT_APPEND]:
            return False, "仅允许追加软约束或软追加条目，硬约束仅可通过固件升级修改"
        
        # 冲突检测：新条目不得与已有硬约束冲突
        for existing_law in self._laws.values():
            if existing_law.rigidity == LawRigidity.HARD:
                if self._is_conflicting(request.new_law, existing_law):
                    return False, f"与已有硬约束 {existing_law.law_id} 冲突"
        
        self.state = LawDBState.APPENDING
        
        # 追加
        self._laws[request.new_law.law_id] = request.new_law
        self._law_version += 1
        self._update_data_hash()
        self._total_appends += 1
        
        self._log_event("LAW_APPEND", {
            "law_id": request.new_law.law_id,
            "rigidity": request.new_law.rigidity.value,
            "version": self._law_version
        })
        
        self.state = LawDBState.NORMAL
        return True, f"法规追加成功，版本号: {self._law_version}"
    
    def _is_conflicting(self, new_law: LawEntry, existing_law: LawEntry) -> bool:
        """检测新法规与已有法规是否冲突"""
        # 简化实现：检查场景条件重叠且强制动作矛盾
        cond_overlap = bool(set(new_law.scene_conditions.keys()) & set(existing_law.scene_conditions.keys()))
        action_conflict = new_law.mandatory_action != existing_law.mandatory_action
        return cond_overlap and action_conflict
    
    # ========== 数据校验 ==========
    
    def validate_integrity(self) -> bool:
        """
        校验法规库数据完整性
        
        S-05: 每 24 小时执行一次
        """
        self.state = LawDBState.VALIDATING
        
        current_hash = self._data_hash
        raw = "".join(f"{lid}{str(law)}" for lid, law in sorted(self._laws.items()))
        computed_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
        
        if current_hash != computed_hash:
            self.state = LawDBState.DEGRADED
            print(f"[{self.module_id}] 法规库数据完整性校验失败！")
            return False
        
        self.state = LawDBState.NORMAL
        return True
    
    # ========== 查询接口 ==========
    
    def get_law(self, law_id: str) -> Optional[LawEntry]:
        return self._laws.get(law_id)
    
    def get_all_laws(self) -> List[LawEntry]:
        return list(self._laws.values())
    
    def get_hard_laws(self) -> List[LawEntry]:
        return [law for law in self._laws.values() if law.rigidity == LawRigidity.HARD]
    
    def generate_snapshot(self) -> LawDBStatusSnapshot:
        return LawDBStatusSnapshot(
            total_laws=len(self._laws),
            hard_laws=self._count_by_rigidity(LawRigidity.HARD),
            soft_laws=self._count_by_rigidity(LawRigidity.SOFT),
            soft_append_laws=self._count_by_rigidity(LawRigidity.SOFT_APPEND),
            law_version=self._law_version,
            data_hash=self._data_hash,
            state=self.state.value
        )
    
    # ========== 变更日志 ==========
    
    def _log_event(self, event_type: str, details: Dict[str, Any]) -> None:
        self._pending_logs.append({
            "log_id": f"law-{uuid.uuid4().hex[:8]}",
            "event_type": event_type,
            "details": details,
            "timestamp": time.time()
        })
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_laws": len(self._laws),
            "hard_laws": self._count_by_rigidity(LawRigidity.HARD),
            "soft_laws": self._count_by_rigidity(LawRigidity.SOFT),
            "soft_append_laws": self._count_by_rigidity(LawRigidity.SOFT_APPEND),
            "law_version": self._law_version,
            "total_queries": self._total_queries,
            "total_exemptions": self._total_exemptions,
            "total_appends": self._total_appends,
            "state": self.state.value
        }


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-45 交通法律法规库 单元测试")
    print("=" * 60)
    passed, failed = 0, 0
    
    # TC-45-01: 红灯场景查询
    print("\n[TC-45-01] 红灯场景查询返回 LAW-001")
    try:
        law_db = TrafficLawDatabase()
        req = LawQueryRequest("q-001", {"signal_light": "红灯", "has_stop_line": True},
                              source_module="ECC-05")
        resp = law_db.query_laws(req)
        assert resp.success
        assert any(law.law_id == "LAW-001" for law in resp.applicable_laws)
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-45-02: 查询仅硬约束
    print("\n[TC-45-02] hard_only 查询仅返回硬约束")
    try:
        law_db = TrafficLawDatabase()
        req = LawQueryRequest("q-002", {"road_marking": "无标线"},
                              query_type="hard_only", source_module="ECC-03")
        resp = law_db.query_laws(req)
        for law in resp.applicable_laws:
            assert law.rigidity == LawRigidity.HARD
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-45-03: 临时伦理豁免批准
    print("\n[TC-45-03] 临时伦理豁免批准（行人优先豁免）")
    try:
        law_db = TrafficLawDatabase()
        req = EthicalExemptionRequest(
            "ex-001", "LAW-002", "避免不可逆重度人身伤害",
            {"scene": "行人突然横穿，制动距离不足"}
        )
        ok, cert, msg = law_db.request_exemption(req)
        assert ok and cert is not None
        assert cert.exempted_law_id == "LAW-002"
        assert cert.valid_until > time.time()
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-45-04: 豁免条件不满足被拒
    print("\n[TC-45-04] 豁免条件不满足被拒（原因不是避免不可逆重伤）")
    try:
        law_db = TrafficLawDatabase()
        req = EthicalExemptionRequest("ex-002", "LAW-001", "提升通行效率", {})
        ok, cert, msg = law_db.request_exemption(req)
        assert not ok
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-45-05: 审批追加软约束成功
    print("\n[TC-45-05] 审批追加软约束成功")
    try:
        law_db = TrafficLawDatabase()
        new_law = LawEntry("LAW-NEW", LawCategory.UNPAVED_ROAD,
                           "测试规则", LawRigidity.SOFT,
                           {"test": True}, "测试动作")
        req = LawAppendRequest("app-001", new_law, "valid_token", "valid_sig", "测试")
        ok, msg = law_db.append_law(req,
                                    lambda sig, law: sig == "valid_sig",
                                    lambda tok: tok == "valid_token")
        assert ok and "LAW-NEW" in law_db._laws
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-45-06: 追加硬约束被拒
    print("\n[TC-45-06] 追加硬约束被拒")
    try:
        law_db = TrafficLawDatabase()
        new_law = LawEntry("LAW-HARD-NEW", LawCategory.BASIC_BAN,
                           "测试", LawRigidity.HARD, {}, "测试")
        req = LawAppendRequest("app-002", new_law, "valid_token", "valid_sig", "")
        ok, msg = law_db.append_law(req,
                                    lambda sig, law: sig == "valid_sig",
                                    lambda tok: tok == "valid_token")
        assert not ok
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-45-07: 数据完整性校验
    print("\n[TC-45-07] 数据完整性校验通过")
    try:
        law_db = TrafficLawDatabase()
        assert law_db.validate_integrity() == True
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    print(f"\n测试结果: {passed} PASS, {failed} FAIL")