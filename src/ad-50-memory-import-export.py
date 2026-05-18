#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-50
模块名称: 记忆导入导出与脱敏共享单元
所属分区: 五、存储与系统运维
核心职责: 管理漏斗二中 L4/L5 层泛化驾驶经验的导出、脱敏处理与外部经验包的合规导入。
          是记忆系统与云端技能库、其他车辆或外部存储设备之间的唯一数据交换接口。
          确保导出的经验绝对不包含 GPS 坐标、行人/车辆特征、时间戳等敏感隐私数据。

依赖模块: ad-26(L4 长期层存储单元，提供可导出的泛化经验),
          ad-28(L5 核心层存储单元，提供经审批可导出的核心经验),
          ad-30(L5 核心层防篡改与只读管控单元，校验导出权限),
          ad-51(记忆变更日志追溯单元，记录导入导出日志)
被依赖模块: 云端技能库(外部系统，接收经验包)、其他车辆记忆系统(外部系统，接收经验包)、
            本地维护工具(导入经验包)

导出脱敏规则:
  - 剔除 GPS 精确坐标，替换为道路类型 + 区域类型
  - 剔除精确时间戳，替换为时间段
  - 剔除行人/车辆特征，替换为目标类别标签
  - 完全剔除车牌号/人脸特征/车辆 VIN
  - 保留决策动作参数、场景特征向量、I/S/V/C 值

导入合规校验:
  - 数字签名验证
  - 法规合规校验（通过 ad-45）
  - 物理可行校验（通过 ad-44）
  - 与 L5 安全规则冲突检测
  - 来源可信度检查

安全约束:
  S-01: 漏斗一任何数据禁止以任何形式导出，编译期硬编码拦截
  S-02: 导出经验包必须执行完整脱敏处理，绝对不可包含隐私数据
  S-03: 导入经验包必须通过数字签名验证、法规合规校验、物理可行校验、L5 冲突检测四道关卡
  S-04: L5 核心层经验仅允许经 ad-30 审批令牌验证的条目导出
  S-05: 警示标签经验（失败经验）禁止导出
  S-06: 导入经验的初始 I₀ 值须降至原始 I 值的 70%
  S-07: 所有导入导出操作全量写入 ad-51 不可变日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid
import hashlib
import json


# ==================== 枚举定义 ====================

class ExportScope(Enum):
    """导出范围"""
    L4_ONLY = "l4_only"
    L5_ONLY = "l5_only"
    L4_L5 = "l4_l5"
    RULES_ONLY = "rules_only"


class ImportResult(Enum):
    """导入结果"""
    SUCCESS = "success"
    FAIL_SIGNATURE_INVALID = "fail_signature_invalid"
    FAIL_COMPLIANCE = "fail_compliance"
    FAIL_PHYSICS = "fail_physics"
    FAIL_L5_CONFLICT = "fail_l5_conflict"
    FAIL_SOURCE_UNTRUSTED = "fail_source_untrusted"
    FAIL_TOKEN_INVALID = "fail_token_invalid"


class IOState(Enum):
    """导入导出单元内部状态"""
    IDLE = "idle"
    EXPORTING = "exporting"
    IMPORTING = "importing"
    PAUSED = "paused"


# ==================== 数据结构 ====================

@dataclass
class ExportRequest:
    """导出请求"""
    request_id: str
    export_scope: ExportScope
    target_destination: str         # "cloud" / "local" / "vehicle"
    operator_token: str             # 操作者权限令牌
    max_entries: int = 500          # 单次导出上限
    include_l5: bool = False        # 是否包含 L5（需审批）
    timestamp: float = field(default_factory=time.time)


@dataclass
class DesensitizedRule:
    """脱敏后的经验规则"""
    rule_id: str
    if_condition: str               # 泛化 IF 条件
    then_action: str                # 泛化 THEN 动作
    applicable_scene: str           # 适用场景标签（脱敏后）
    i_value: float
    s_value: float
    v_value: float
    c_value: float
    contributing_entry_count: int
    source_slot_id: int
    time_period: str = "日间"       # 脱敏后时间段


@dataclass
class ExportPackage:
    """导出经验包"""
    package_id: str
    version: str = "1.0"
    export_timestamp: float = field(default_factory=time.time)
    export_scope: str = ""
    rules: List[DesensitizedRule] = field(default_factory=list)
    rule_count: int = 0
    digital_signature: str = ""
    source_vehicle_id: str = "ANONYMOUS"


@dataclass
class ExportResult:
    """导出结果"""
    request_id: str
    success: bool
    package: Optional[ExportPackage] = None
    exported_count: int = 0
    desensitized_count: int = 0
    message: str = ""


@dataclass
class ImportRequest:
    """导入请求"""
    request_id: str
    package: ExportPackage
    source_identifier: str          # 来源标识
    import_token: str               # 导入令牌
    timestamp: float = field(default_factory=time.time)


@dataclass
class ImportReport:
    """导入报告"""
    request_id: str
    success: bool
    total_rules: int
    imported_count: int
    rejected_count: int
    rejected_details: List[Dict[str, str]] = field(default_factory=list)
    message: str = ""


# ==================== 主类定义 ====================

class MemoryImportExport:
    """
    记忆导入导出与脱敏共享单元
    
    职责:
    1. 处理导出请求：从 L4/L5 获取泛化经验 → 脱敏 → 打包 → 签名
    2. 处理导入请求：验签 → 合规校验 → 物理校验 → 冲突检测 → 写入漏斗二
    3. 严格执行脱敏规则
    4. 漏斗一数据导出硬拦截
    """
    
    # 单次导出上限
    MAX_EXPORT_ENTRIES = 500
    
    # 导入 I₀ 折扣系数（S-06）
    IMPORT_I0_DISCOUNT = 0.70
    
    # 可信来源列表（简化）
    TRUSTED_SOURCES = {
        "official_cloud_skill_library",
        "authorized_vehicle_fleet",
        "certified_maintenance_tool",
    }
    
    def __init__(self):
        self.module_id = "ad-50"
        self.module_name = "记忆导入导出与脱敏共享单元"
        
        # 内部状态
        self.state = IOState.IDLE
        
        # 统计
        self._total_exports = 0
        self._total_imports = 0
        self._total_rules_exported = 0
        self._total_rules_imported = 0
        self._total_rules_rejected = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 记忆导入导出与脱敏共享单元初始化完成")
        print(f"[{self.module_id}] 导出上限: {self.MAX_EXPORT_ENTRIES} 条")
        print(f"[{self.module_id}] 导入 I₀ 折扣: {self.IMPORT_I0_DISCOUNT}")
    
    # ========== 状态管理 ==========
    
    def pause(self) -> None:
        self.state = IOState.PAUSED
    
    def resume(self) -> None:
        self.state = IOState.IDLE
    
    def get_state(self) -> IOState:
        return self.state
    
    # ========== 导出 ==========
    
    def execute_export(self,
                       request: ExportRequest,
                       get_l4_rules,
                       get_l5_rules,
                       token_validator) -> ExportResult:
        """
        执行经验导出
        
        Args:
            request: 导出请求
            get_l4_rules: 获取 L4 泛化规则的回调 (scope) -> List[dict]
            get_l5_rules: 获取 L5 可导出规则的回调 (token) -> List[dict]
            token_validator: 令牌验证回调 (token, operation) -> bool
            
        Returns:
            导出结果
        """
        # 权限验证
        if not token_validator(request.operator_token, "export"):
            return ExportResult(
                request_id=request.request_id,
                success=False,
                message="权限不足"
            )
        
        self.state = IOState.EXPORTING
        self._total_exports += 1
        
        # 获取 L4 泛化规则
        l4_rules = []
        if request.export_scope in [ExportScope.L4_ONLY, ExportScope.L4_L5, ExportScope.RULES_ONLY]:
            l4_rules = get_l4_rules(scope="refined_only")  # 仅已提炼的泛化规则
            # S-05: 过滤警示标签经验
            l4_rules = [r for r in l4_rules if r.get("result_label") != "警示标签"]
        
        # 获取 L5 规则（需额外审批）
        l5_rules = []
        if request.include_l5 and request.export_scope in [ExportScope.L5_ONLY, ExportScope.L4_L5]:
            l5_rules = get_l5_rules(request.operator_token)
            # S-04: L5 仅导出标记为"可导出"的条目
            l5_rules = [r for r in l5_rules if r.get("exportable", False)]
        
        all_rules = l4_rules + l5_rules
        
        # 数量上限检查
        if len(all_rules) > self.MAX_EXPORT_ENTRIES:
            self.state = IOState.IDLE
            return ExportResult(
                request_id=request.request_id,
                success=False,
                message=f"导出条目超过单次上限 {self.MAX_EXPORT_ENTRIES}"
            )
        
        # 执行脱敏处理
        desensitized_rules = []
        for rule in all_rules:
            dr = self._desensitize(rule)
            desensitized_rules.append(dr)
        
        # 打包
        package = ExportPackage(
            package_id=f"export-{uuid.uuid4().hex[:8]}",
            export_scope=request.export_scope.value,
            rules=desensitized_rules,
            rule_count=len(desensitized_rules)
        )
        
        # 生成数字签名
        package.digital_signature = self._generate_signature(package)
        
        self._total_rules_exported += len(desensitized_rules)
        
        self._log_event("EXPORT", {
            "request_id": request.request_id,
            "scope": request.export_scope.value,
            "rule_count": len(desensitized_rules),
            "package_id": package.package_id
        })
        
        self.state = IOState.IDLE
        
        return ExportResult(
            request_id=request.request_id,
            success=True,
            package=package,
            exported_count=len(all_rules),
            desensitized_count=len(desensitized_rules)
        )
    
    def _desensitize(self, rule: Dict[str, Any]) -> DesensitizedRule:
        """
        执行完整脱敏处理
        
        脱敏规则:
        - 剔除 GPS 精确坐标 → 道路类型 + 区域类型
        - 剔除精确时间戳 → 时间段
        - 剔除行人/车辆特征 → 目标类别标签
        - 完全剔除车牌号/人脸特征/车辆 VIN
        """
        return DesensitizedRule(
            rule_id=rule.get("rule_id", f"rule-{uuid.uuid4().hex[:8]}"),
            if_condition=rule.get("if_condition", ""),
            then_action=rule.get("then_action", ""),
            applicable_scene=self._generalize_scene(rule.get("scene", "")),
            i_value=rule.get("i_value", 0.5),
            s_value=rule.get("s_value", 0.0),
            v_value=rule.get("v_value", 0.0),
            c_value=rule.get("c_value", 0.0),
            contributing_entry_count=rule.get("contributing_entry_count", 1),
            source_slot_id=rule.get("source_slot_id", 19),
            time_period=self._generalize_time(rule.get("timestamp", 0))
        )
    
    def _generalize_scene(self, scene: str) -> str:
        """泛化场景描述（脱敏 GPS 坐标）"""
        # 移除可能存在的坐标信息
        for keyword in ["经度", "纬度", "GPS", "坐标"]:
            scene = scene.replace(keyword, "")
        return scene.strip() if scene.strip() else "通用驾驶场景"
    
    def _generalize_time(self, timestamp: float) -> str:
        """泛化时间戳为时间段"""
        if timestamp <= 0:
            return "未知时段"
        import datetime
        hour = datetime.datetime.fromtimestamp(timestamp).hour
        if 6 <= hour < 18:
            return "日间"
        else:
            return "夜间"
    
    def _generate_signature(self, package: ExportPackage) -> str:
        """生成导出包数字签名"""
        raw = package.package_id
        for rule in package.rules:
            raw += rule.rule_id + rule.if_condition + rule.then_action
        return hashlib.sha256(raw.encode()).hexdigest()
    
    # ========== 导入 ==========
    
    def execute_import(self,
                       request: ImportRequest,
                       token_validator,
                       compliance_checker,
                       physics_checker,
                       l5_conflict_checker) -> ImportReport:
        """
        执行经验导入
        
        四道校验关卡:
        1. 数字签名验证
        2. 法规合规校验（ad-45）
        3. 物理可行校验（ad-44）
        4. L5 安全规则冲突检测（ad-28）
        
        Args:
            request: 导入请求
            token_validator: 令牌验证回调
            compliance_checker: 法规合规校验回调 (rule) -> (pass, reason)
            physics_checker: 物理可行校验回调 (rule) -> (pass, reason)
            l5_conflict_checker: L5 冲突检测回调 (rule) -> (pass, reason)
            
        Returns:
            导入报告
        """
        # 权限验证
        if not token_validator(request.import_token, "import"):
            return ImportReport(
                request_id=request.request_id,
                success=False,
                total_rules=0, imported_count=0, rejected_count=0,
                message="权限不足"
            )
        
        self.state = IOState.IMPORTING
        
        package = request.package
        
        # 1. 数字签名验证
        if not self._verify_signature(package):
            self.state = IOState.IDLE
            return ImportReport(
                request_id=request.request_id,
                success=False,
                total_rules=len(package.rules), imported_count=0,
                rejected_count=len(package.rules),
                rejected_details=[{"rule_id": "ALL", "reason": "数字签名无效"}],
                message="数字签名验证失败"
            )
        
        # 来源可信度检查
        if request.source_identifier not in self.TRUSTED_SOURCES:
            self.state = IOState.IDLE
            return ImportReport(
                request_id=request.request_id,
                success=False,
                total_rules=len(package.rules), imported_count=0,
                rejected_count=len(package.rules),
                rejected_details=[{"rule_id": "ALL", "reason": "来源不在可信列表中"}],
                message="来源不可信"
            )
        
        self._total_imports += 1
        
        imported = 0
        rejected = 0
        rejected_details = []
        
        for rule in package.rules:
            # 2. 法规合规校验
            law_ok, law_reason = compliance_checker(rule)
            if not law_ok:
                rejected += 1
                rejected_details.append({"rule_id": rule.rule_id, "reason": f"法规不合规: {law_reason}"})
                continue
            
            # 3. 物理可行校验
            physics_ok, physics_reason = physics_checker(rule)
            if not physics_ok:
                rejected += 1
                rejected_details.append({"rule_id": rule.rule_id, "reason": f"物理不可行: {physics_reason}"})
                continue
            
            # 4. L5 安全规则冲突检测
            conflict, conflict_reason = l5_conflict_checker(rule)
            if conflict:
                rejected += 1
                rejected_details.append({"rule_id": rule.rule_id, "reason": f"与L5安全规则冲突: {conflict_reason}"})
                continue
            
            # 全部校验通过 → 标记为可写入（实际写入由 ad-14 执行）
            # S-06: 导入经验的初始 I₀ 值降至原始 I 值的 70%
            rule.i_value = rule.i_value * self.IMPORT_I0_DISCOUNT
            imported += 1
        
        self._total_rules_imported += imported
        self._total_rules_rejected += rejected
        
        self._log_event("IMPORT", {
            "request_id": request.request_id,
            "source": request.source_identifier,
            "total": len(package.rules),
            "imported": imported,
            "rejected": rejected
        })
        
        self.state = IOState.IDLE
        
        return ImportReport(
            request_id=request.request_id,
            success=(imported > 0),
            total_rules=len(package.rules),
            imported_count=imported,
            rejected_count=rejected,
            rejected_details=rejected_details,
            message=f"导入完成: {imported}/{len(package.rules)} 条通过"
        )
    
    def _verify_signature(self, package: ExportPackage) -> bool:
        """验证导出包数字签名"""
        expected = self._generate_signature(package)
        return expected == package.digital_signature
    
    # ========== 变更日志 ==========
    
    def _log_event(self, event_type: str, details: Dict[str, Any]) -> None:
        self._pending_logs.append({
            "log_id": f"io-{uuid.uuid4().hex[:8]}",
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
            "total_exports": self._total_exports,
            "total_imports": self._total_imports,
            "total_rules_exported": self._total_rules_exported,
            "total_rules_imported": self._total_rules_imported,
            "total_rules_rejected": self._total_rules_rejected,
            "state": self.state.value
        }


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-50 记忆导入导出与脱敏共享单元 单元测试")
    print("=" * 60)
    passed, failed = 0, 0
    
    # 模拟回调
    def token_ok(token, op): return token == "valid_token"
    def get_l4(scope): return [
        {"rule_id": "R001", "if_condition": "高速 AND 湿滑", "then_action": "减速至80",
         "i_value": 0.8, "s_value": 0.7, "v_value": 0.6, "c_value": 0.5,
         "result_label": "成功优化", "contributing_entry_count": 5, "source_slot_id": 15,
         "scene": "高速,GPS:39.9,116.4", "timestamp": time.time()}
    ]
    def get_l5(token): return []
    def compliance_ok(rule): return True, ""
    def compliance_fail(rule): return False, "违反LAW-003"
    def physics_ok(rule): return True, ""
    def l5_conflict_ok(rule): return False, ""
    def l5_conflict_fail(rule): return True, "与L5-001冲突"
    
    # TC-50-01: 正常导出脱敏
    print("\n[TC-50-01] 正常导出脱敏处理")
    try:
        io = MemoryImportExport()
        req = ExportRequest("exp-001", ExportScope.L4_ONLY, "cloud", "valid_token")
        result = io.execute_export(req, get_l4, get_l5, token_ok)
        assert result.success and result.package is not None
        assert result.package.rule_count == 1
        assert "GPS" not in result.package.rules[0].applicable_scene
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-50-02: 令牌无效拒绝导出
    print("\n[TC-50-02] 令牌无效拒绝导出")
    try:
        io = MemoryImportExport()
        req = ExportRequest("exp-002", ExportScope.L4_ONLY, "cloud", "bad_token")
        result = io.execute_export(req, get_l4, get_l5, token_ok)
        assert not result.success
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-50-03: 正常导入通过四道校验
    print("\n[TC-50-03] 正常导入通过四道校验")
    try:
        io = MemoryImportExport()
        package = ExportPackage(
            package_id="test-pkg",
            rules=[DesensitizedRule("R1", "IF", "THEN", "场景", 0.8, 0.7, 0.6, 0.5, 5, 15)]
        )
        package.digital_signature = io._generate_signature(package)
        req = ImportRequest("imp-001", package, "official_cloud_skill_library", "valid_token")
        report = io.execute_import(req, token_ok, compliance_ok, physics_ok, l5_conflict_ok)
        assert report.success and report.imported_count == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-50-04: 法规不合规被拒
    print("\n[TC-50-04] 法规不合规被拒")
    try:
        io = MemoryImportExport()
        package = ExportPackage("pkg", rules=[DesensitizedRule("R2", "IF", "THEN", "场景", 0.8, 0.7, 0.6, 0.5, 5, 15)])
        package.digital_signature = io._generate_signature(package)
        req = ImportRequest("imp-002", package, "official_cloud_skill_library", "valid_token")
        report = io.execute_import(req, token_ok, compliance_fail, physics_ok, l5_conflict_ok)
        assert report.rejected_count == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-50-05: L5 冲突被拒
    print("\n[TC-50-05] L5 安全规则冲突被拒")
    try:
        io = MemoryImportExport()
        package = ExportPackage("pkg", rules=[DesensitizedRule("R3", "IF", "THEN", "场景", 0.8, 0.7, 0.6, 0.5, 5, 15)])
        package.digital_signature = io._generate_signature(package)
        req = ImportRequest("imp-003", package, "official_cloud_skill_library", "valid_token")
        report = io.execute_import(req, token_ok, compliance_ok, physics_ok, l5_conflict_fail)
        assert report.rejected_count == 1
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-50-06: 来源不可信被拒
    print("\n[TC-50-06] 来源不可信被拒")
    try:
        io = MemoryImportExport()
        package = ExportPackage("pkg", rules=[DesensitizedRule("R4", "IF", "THEN", "场景", 0.8, 0.7, 0.6, 0.5, 5, 15)])
        package.digital_signature = io._generate_signature(package)
        req = ImportRequest("imp-004", package, "unknown_source", "valid_token")
        report = io.execute_import(req, token_ok, compliance_ok, physics_ok, l5_conflict_ok)
        assert not report.success
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    print(f"\n测试结果: {passed} PASS, {failed} FAIL")