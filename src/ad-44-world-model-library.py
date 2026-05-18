#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-44
模块名称: 独立世界模型库
所属分区: 四、漏斗外挂扩展区（物理隔离）
核心职责: 对接自动驾驶类脑全域客观世界模型，提供五大类目标分类、三维风险标签、
          动态重分类规则的查询接口。作为 ECC 认知大脑的客观认知底座，独立于双漏斗
          记忆系统运行，不参与记忆的沉淀、筛选、晋升与遗忘机制。只读查询为主，
          仅接受经审批的增量追加更新，禁止覆盖删除已有条目。

依赖模块: ECC-01 情境解析模块（主要查询方）、ECC-03 因果推理模块（查询方）、
          ECC-04 心智模拟模块（查询方）
被依赖模块: ECC-01/03/04/05（提供客观环境认知数据）、
            ad-08（上下文场景标记单元，提供场景特征查询）、
            ad-14（场景判定与分槽路由单元，提供道路属性查询）

安全约束:
  S-01: 世界模型独立于双漏斗记忆系统，不参与记忆机制
  S-02: 仅接受经 ECC-12 审批的永久性结构变更
  S-03: 世界模型数据存储于只读分区，仅允许增量追加
  S-04: 数据完整性校验每 24 小时执行一次
  S-05: 世界模型数据为客观认知底座，不受任何驾驶员偏好影响
  S-06: 漏斗一模块无权查询世界模型数据
  S-07: 世界模型数据版本号每次审批更新后递增
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid
import hashlib
import math


# ==================== 枚举定义 ====================

class TargetClass(Enum):
    """五大互斥目标分类"""
    CLASS_1 = "第一类：静态固定无生命实体"
    CLASS_2 = "第二类：机动动态实体"
    CLASS_3 = "第三类：非人生物活体动态实体"
    CLASS_4 = "第四类：人类及低速非机动交通参与者"
    CLASS_5 = "第五类：非生物动态环境要素与路面异常"


class SceneCategory(Enum):
    """场景类别"""
    HIGHWAY = "highway_cruise"
    URBAN = "urban_intersection"
    PARKING = "parking_low_speed"
    SPECIAL = "special_environment"
    GENERAL = "general_driving"
    RURAL = "rural_road"


class RiskLevel(Enum):
    """风险等级"""
    LOW = "低"
    MEDIUM = "中"
    HIGH = "高"


class WMState(Enum):
    """世界模型内部状态"""
    NORMAL = "normal"
    UPDATING = "updating"
    APPROVED_UPDATE = "approved_update"
    VALIDATING = "validating"
    DEGRADED = "degraded"
    PAUSED = "paused"


# ==================== 数据结构 ====================

@dataclass
class RiskVector:
    """三维风险向量"""
    occurrence_prob: float       # 出现概率 0.0-1.0
    lane_intrusion_prob: float   # 车道侵入概率 0.0-1.0
    damage_severity: float       # 碰撞伤害严重度 0.0-1.0
    composite_score: float = 0.0 # 综合风险评分

    def __post_init__(self):
        self.composite_score = (self.occurrence_prob * 0.4 +
                                self.lane_intrusion_prob * 0.35 +
                                self.damage_severity * 0.25)


@dataclass
class PhysicalAttributes:
    """物理属性"""
    hardness: int = 3            # 硬度 1-5
    brittleness: int = 3         # 脆性 1-5
    mass: int = 3                # 质量等级 1-5
    friction_coefficient: float = 0.7
    movable: bool = True
    max_speed: Optional[float] = None


@dataclass
class EntityEntry:
    """实体条目"""
    entity_id: str
    target_class: TargetClass
    risk_vector: RiskVector
    physical_attrs: PhysicalAttributes
    description: str = ""
    registered_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)


@dataclass
class CausalRule:
    """因果规则（IF-THEN 形式）"""
    rule_id: str
    condition: str               # IF 条件
    consequence: str             # THEN 后果
    confidence: float = 1.0
    source: str = "出厂预置"


@dataclass
class TargetQueryRequest:
    """目标分类查询请求"""
    query_id: str
    query_type: str              # "target" / "scene"
    entity_ids: Optional[List[str]] = None
    scene_features: Optional[Dict[str, Any]] = None
    source_module: str = ""
    priority: str = "normal"


@dataclass
class TargetQueryResponse:
    """目标分类查询响应"""
    query_id: str
    success: bool
    entities: Optional[List[EntityEntry]] = None
    scene_category: Optional[SceneCategory] = None
    scene_confidence: float = 0.0
    message: str = ""


@dataclass
class ApprovedUpdateRequest:
    """审批更新请求"""
    request_id: str
    update_type: str             # "add_entity" / "add_rule" / "update_entity"
    update_data: Dict[str, Any]
    approval_token: str
    reason: str


@dataclass
class WMStatusSnapshot:
    """世界模型状态快照"""
    entity_count: int
    rule_count: int
    data_version: int
    data_hash: str
    state: str
    uptime_seconds: float


# ==================== 主类定义 ====================

class WorldModelLibrary:
    """
    独立世界模型库 - 漏斗外挂扩展区
    
    职责:
    1. 存储五大类目标实体及其三维风险标签和物理属性
    2. 存储因果规则库（IF-THEN 形式）
    3. 提供目标分类查询（按 ID 或按场景特征）
    4. 场景特征判定（根据道路属性判定场景类别）
    5. 接收感知模块的动态更新
    6. 处理经 ECC-12 审批的永久性结构变更（仅增量追加）
    7. 数据完整性定期校验
    """
    
    # 授权查询的模块列表
    AUTHORIZED_QUERY_MODULES = {
        "ECC-01", "ECC-03", "ECC-04", "ECC-05",
        "ad-08", "ad-14", "ad-16", "ad-31", "ad-43"
    }
    
    # 禁止查询的模块（漏斗一）
    FORBIDDEN_MODULES = {
        "ad-02", "ad-04", "ad-05", "ad-06", "ad-07",
        "ad-09", "ad-10", "ad-11", "ad-13"
    }
    
    # 默认实体库
    DEFAULT_ENTITIES: Dict[str, dict] = {
        "static_stone": {
            "target_class": TargetClass.CLASS_1,
            "risk_vector": {"occurrence_prob": 0.5, "lane_intrusion_prob": 0.1, "damage_severity": 0.6},
            "physical_attrs": {"hardness": 5, "brittleness": 1, "mass": 3, "movable": False},
            "description": "路面石块"
        },
        "vehicle_car": {
            "target_class": TargetClass.CLASS_2,
            "risk_vector": {"occurrence_prob": 0.9, "lane_intrusion_prob": 0.5, "damage_severity": 0.8},
            "physical_attrs": {"hardness": 3, "brittleness": 2, "mass": 4, "friction_coefficient": 0.7, "movable": True, "max_speed": 200.0},
            "description": "轿车"
        },
        "animal_dog": {
            "target_class": TargetClass.CLASS_3,
            "risk_vector": {"occurrence_prob": 0.3, "lane_intrusion_prob": 0.8, "damage_severity": 0.5},
            "physical_attrs": {"hardness": 1, "brittleness": 2, "mass": 1, "movable": True, "max_speed": 40.0},
            "description": "狗"
        },
        "pedestrian_adult": {
            "target_class": TargetClass.CLASS_4,
            "risk_vector": {"occurrence_prob": 0.8, "lane_intrusion_prob": 0.7, "damage_severity": 0.7},
            "physical_attrs": {"hardness": 1, "brittleness": 5, "mass": 2, "movable": True, "max_speed": 10.0},
            "description": "成年行人"
        },
        "env_water": {
            "target_class": TargetClass.CLASS_5,
            "risk_vector": {"occurrence_prob": 0.4, "lane_intrusion_prob": 0.6, "damage_severity": 0.7},
            "physical_attrs": {"hardness": 0, "brittleness": 0, "mass": 2, "movable": True},
            "description": "路面积水"
        },
    }
    
    # 默认因果规则库
    DEFAULT_RULES: List[dict] = [
        {"rule_id": "R001", "condition": "物体易碎 AND 掉落高度>0.5m", "consequence": "破碎概率极高", "confidence": 1.0},
        {"rule_id": "R002", "condition": "容器有孔 AND 盛装液体", "consequence": "液体泄漏", "confidence": 1.0},
        {"rule_id": "R003", "condition": "物体高温 AND 直接接触", "consequence": "烫伤", "confidence": 1.0},
        {"rule_id": "R004", "condition": "可移动 AND 施加外力>质量等级", "consequence": "位置改变", "confidence": 0.9},
        {"rule_id": "R005", "condition": "路面湿滑 AND 高速行驶", "consequence": "制动距离延长", "confidence": 1.0},
    ]
    
    def __init__(self):
        self.module_id = "ad-44"
        self.module_name = "独立世界模型库"
        
        # 内部状态
        self.state = WMState.NORMAL
        
        # 实体库: entity_id -> EntityEntry
        self._entities: Dict[str, EntityEntry] = {}
        
        # 因果规则库: rule_id -> CausalRule
        self._rules: Dict[str, CausalRule] = {}
        
        # 数据版本号
        self._data_version = 1
        
        # 数据完整性哈希
        self._data_hash = ""
        
        # 初始化默认数据
        self._init_default_data()
        
        # 统计
        self._total_queries = 0
        self._total_dynamic_updates = 0
        self._total_approved_updates = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 独立世界模型库初始化完成")
        print(f"[{self.module_id}] 实体: {len(self._entities)} 个, 规则: {len(self._rules)} 条, 版本: {self._data_version}")
    
    def _init_default_data(self) -> None:
        """初始化默认实体和规则"""
        for eid, data in self.DEFAULT_ENTITIES.items():
            self._entities[eid] = EntityEntry(
                entity_id=eid,
                target_class=data["target_class"],
                risk_vector=RiskVector(**data["risk_vector"]),
                physical_attrs=PhysicalAttributes(**data["physical_attrs"]),
                description=data["description"]
            )
        
        for rule_data in self.DEFAULT_RULES:
            rule = CausalRule(**rule_data)
            self._rules[rule.rule_id] = rule
        
        self._update_data_hash()
    
    def _update_data_hash(self) -> None:
        """更新数据完整性哈希"""
        raw = ""
        for eid in sorted(self._entities.keys()):
            raw += eid + str(self._entities[eid])
        for rid in sorted(self._rules.keys()):
            raw += rid + str(self._rules[rid])
        self._data_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
    
    # ========== 状态管理 ==========
    
    def pause(self) -> None:
        self.state = WMState.PAUSED
    
    def resume(self) -> None:
        self.state = WMState.NORMAL
    
    def get_state(self) -> WMState:
        return self.state
    
    # ========== 目标分类查询 ==========
    
    def query_target(self, request: TargetQueryRequest) -> TargetQueryResponse:
        """
        处理目标分类查询请求
        
        Args:
            request: 查询请求
            
        Returns:
            查询响应
        """
        self._total_queries += 1
        
        # S-06: 漏斗一模块禁止查询
        if request.source_module in self.FORBIDDEN_MODULES:
            return TargetQueryResponse(
                query_id=request.query_id,
                success=False,
                message="漏斗一模块无权查询世界模型数据"
            )
        
        if request.query_type == "target":
            return self._query_by_ids(request)
        elif request.query_type == "scene":
            return self._query_scene(request)
        else:
            return TargetQueryResponse(
                query_id=request.query_id,
                success=False,
                message=f"不支持的查询类型: {request.query_type}"
            )
    
    def _query_by_ids(self, request: TargetQueryRequest) -> TargetQueryResponse:
        """按实体 ID 查询"""
        if not request.entity_ids:
            return TargetQueryResponse(query_id=request.query_id, success=False, message="未指定实体ID列表")
        
        entities = []
        for eid in request.entity_ids:
            entity = self._entities.get(eid)
            if entity:
                entities.append(entity)
        
        return TargetQueryResponse(
            query_id=request.query_id,
            success=True,
            entities=entities
        )
    
    def _query_scene(self, request: TargetQueryRequest) -> TargetQueryResponse:
        """根据场景特征判定场景类别"""
        if not request.scene_features:
            return TargetQueryResponse(query_id=request.query_id, success=False, message="未提供场景特征")
        
        features = request.scene_features
        road_level = features.get("road_level", "")
        road_type = features.get("road_type", "")
        weather = features.get("weather", "晴")
        lane_count = features.get("lane_count", 2)
        traffic_sign_density = features.get("traffic_sign_density", "中")
        
        # 场景判定逻辑
        if weather in ["暴雨", "暴雪", "大雾", "沙尘暴"] or road_type in ["积水", "结冰", "积雪"]:
            scene = SceneCategory.SPECIAL
            confidence = 0.95
        elif road_level in ["高速公路", "城市快速路"]:
            scene = SceneCategory.HIGHWAY
            confidence = 0.90
        elif road_level in ["城市主干道", "次干道", "支路"]:
            scene = SceneCategory.URBAN
            confidence = 0.85
        elif road_type in ["泥土", "碎石", "沙土"] or (lane_count == 1 and traffic_sign_density == "无"):
            scene = SceneCategory.RURAL
            confidence = 0.80
        else:
            scene = SceneCategory.GENERAL
            confidence = 0.70
        
        return TargetQueryResponse(
            query_id=request.query_id,
            success=True,
            scene_category=scene,
            scene_confidence=confidence,
            message="场景判定完成"
        )
    
    # ========== 动态更新 ==========
    
    def dynamic_update(self, entity_id: str, update_data: Dict[str, Any]) -> bool:
        """
        接收感知模块的环境动态更新
        
        Args:
            entity_id: 目标 ID
            update_data: 更新数据（位置、速度等实时状态）
            
        Returns:
            是否成功
        """
        if self.state != WMState.NORMAL:
            return False
        
        self._total_dynamic_updates += 1
        
        if entity_id not in self._entities:
            # 新目标临时注册
            self._entities[entity_id] = EntityEntry(
                entity_id=entity_id,
                target_class=TargetClass.CLASS_1,
                risk_vector=RiskVector(0.3, 0.1, 0.3),
                physical_attrs=PhysicalAttributes(),
                description="临时注册目标"
            )
        
        self._entities[entity_id].last_updated = time.time()
        return True
    
    # ========== 审批更新 ==========
    
    def approved_update(self, request: ApprovedUpdateRequest,
                        token_validator) -> Tuple[bool, str]:
        """
        处理经 ECC-12 审批的永久性结构变更
        
        S-02: 必须验证审批令牌
        S-03: 仅允许增量追加
        
        Args:
            request: 审批更新请求
            token_validator: 令牌验证回调
            
        Returns:
            (成功, 消息)
        """
        if not token_validator(request.approval_token):
            return False, "审批令牌无效"
        
        if request.update_type not in ["add_entity", "add_rule"]:
            return False, f"不支持的更新类型: {request.update_type}（仅允许增量追加）"
        
        self.state = WMState.APPROVED_UPDATE
        self._total_approved_updates += 1
        
        if request.update_type == "add_entity":
            data = request.update_data
            entity = EntityEntry(
                entity_id=data.get("entity_id", f"entity-{uuid.uuid4().hex[:8]}"),
                target_class=data.get("target_class", TargetClass.CLASS_1),
                risk_vector=RiskVector(**data.get("risk_vector", {"occurrence_prob": 0.3, "lane_intrusion_prob": 0.2, "damage_severity": 0.3})),
                physical_attrs=PhysicalAttributes(**data.get("physical_attrs", {})),
                description=data.get("description", "")
            )
            self._entities[entity.entity_id] = entity
            
        elif request.update_type == "add_rule":
            data = request.update_data
            rule = CausalRule(
                rule_id=data.get("rule_id", f"rule-{uuid.uuid4().hex[:8]}"),
                condition=data.get("condition", ""),
                consequence=data.get("consequence", ""),
                confidence=data.get("confidence", 1.0),
                source="审批追加"
            )
            self._rules[rule.rule_id] = rule
        
        self._data_version += 1
        self._update_data_hash()
        self.state = WMState.NORMAL
        
        self._log_event("APPROVED_UPDATE", {"type": request.update_type, "version": self._data_version})
        return True, f"审批更新成功，版本号: {self._data_version}"
    
    # ========== 数据校验 ==========
    
    def validate_integrity(self) -> bool:
        """
        校验世界模型数据完整性
        
        S-04: 每 24 小时执行一次
        """
        self.state = WMState.VALIDATING
        
        current_hash = self._data_hash
        # 重新计算哈希
        raw = ""
        for eid in sorted(self._entities.keys()):
            raw += eid + str(self._entities[eid])
        for rid in sorted(self._rules.keys()):
            raw += rid + str(self._rules[rid])
        computed_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
        
        if current_hash != computed_hash:
            self.state = WMState.DEGRADED
            print(f"[{self.module_id}] 数据完整性校验失败！哈希不匹配")
            return False
        
        self.state = WMState.NORMAL
        return True
    
    # ========== 查询接口 ==========
    
    def get_entity(self, entity_id: str) -> Optional[EntityEntry]:
        return self._entities.get(entity_id)
    
    def get_all_entities(self) -> List[EntityEntry]:
        return list(self._entities.values())
    
    def get_rules(self) -> List[CausalRule]:
        return list(self._rules.values())
    
    def get_data_version(self) -> int:
        return self._data_version
    
    def generate_snapshot(self) -> WMStatusSnapshot:
        return WMStatusSnapshot(
            entity_count=len(self._entities),
            rule_count=len(self._rules),
            data_version=self._data_version,
            data_hash=self._data_hash,
            state=self.state.value,
            uptime_seconds=0.0
        )
    
    # ========== 变更日志 ==========
    
    def _log_event(self, event_type: str, details: Dict[str, Any]) -> None:
        self._pending_logs.append({
            "log_id": f"wm-{uuid.uuid4().hex[:8]}",
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
            "entity_count": len(self._entities),
            "rule_count": len(self._rules),
            "data_version": self._data_version,
            "total_queries": self._total_queries,
            "total_dynamic_updates": self._total_dynamic_updates,
            "total_approved_updates": self._total_approved_updates,
            "state": self.state.value
        }


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-44 独立世界模型库 单元测试")
    print("=" * 60)
    passed, failed = 0, 0
    
    # TC-44-01: 按ID查询目标分类
    print("\n[TC-44-01] 按ID查询目标分类")
    try:
        wm = WorldModelLibrary()
        req = TargetQueryRequest("q-001", "target", entity_ids=["vehicle_car"], source_module="ECC-01")
        resp = wm.query_target(req)
        assert resp.success and len(resp.entities) == 1
        assert resp.entities[0].target_class == TargetClass.CLASS_2
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-44-02: 场景特征判定
    print("\n[TC-44-02] 场景特征判定（高速→HIGHWAY）")
    try:
        wm = WorldModelLibrary()
        req = TargetQueryRequest("q-002", "scene", scene_features={
            "road_level": "高速公路", "road_type": "沥青", "lane_count": 3,
            "weather": "晴", "traffic_sign_density": "高"
        }, source_module="ad-14")
        resp = wm.query_target(req)
        assert resp.success and resp.scene_category == SceneCategory.HIGHWAY
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-44-03: 漏斗一模块被拒
    print("\n[TC-44-03] 漏斗一模块查询被拒")
    try:
        wm = WorldModelLibrary()
        req = TargetQueryRequest("q-003", "target", entity_ids=["vehicle_car"], source_module="ad-07")
        resp = wm.query_target(req)
        assert not resp.success
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-44-04: 审批追加实体
    print("\n[TC-44-04] 审批追加新实体")
    try:
        wm = WorldModelLibrary()
        req = ApprovedUpdateRequest("upd-001", "add_entity", {
            "entity_id": "new_entity",
            "target_class": TargetClass.CLASS_2,
            "description": "测试实体"
        }, "valid_token", "测试")
        ok, msg = wm.approved_update(req, lambda t: t == "valid_token")
        assert ok and "new_entity" in wm._entities
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-44-05: 审批令牌无效拒绝
    print("\n[TC-44-05] 审批令牌无效拒绝")
    try:
        wm = WorldModelLibrary()
        req = ApprovedUpdateRequest("upd-002", "add_entity", {"entity_id": "bad"}, "bad_token", "")
        ok, msg = wm.approved_update(req, lambda t: t == "valid_token")
        assert not ok
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-44-06: 数据完整性校验
    print("\n[TC-44-06] 数据完整性校验通过")
    try:
        wm = WorldModelLibrary()
        assert wm.validate_integrity() == True
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-44-07: 动态更新临时注册
    print("\n[TC-44-07] 动态更新临时注册新目标")
    try:
        wm = WorldModelLibrary()
        ok = wm.dynamic_update("unknown_target", {"position": (1, 2)})
        assert ok and "unknown_target" in wm._entities
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-44-08: 恶劣天气判定为特殊环境
    print("\n[TC-44-08] 暴雨→特殊环境")
    try:
        wm = WorldModelLibrary()
        req = TargetQueryRequest("q-008", "scene", scene_features={
            "road_level": "高速公路", "weather": "暴雨"
        }, source_module="ECC-01")
        resp = wm.query_target(req)
        assert resp.scene_category == SceneCategory.SPECIAL
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    print(f"\n测试结果: {passed} PASS, {failed} FAIL")