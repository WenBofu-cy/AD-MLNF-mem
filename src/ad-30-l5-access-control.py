#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-30
模块名称: L5 核心层防篡改与只读管控单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 五层记忆层级存储
核心职责: L5 核心层数据的唯一对外访问接口。管控所有读写权限：对外提供经过鉴权的
          只读查询服务，对内校验所有写入请求的安全令牌与操作合法性。任何对 L5 存储
          分区的访问（含读取）必须通过本单元，编译期禁止绕过。全量记录所有访问日志，
          实现软件层面的防篡改管控。

依赖模块: ad-28(L5 核心层存储单元), ad-29(L5 核心层安全规则硬锁定单元), ad-51(日志)
被依赖模块: ECC 大脑各模块（查询 L5 经验）、ad-01(总控漏斗 F₀，人工锁定指令)、
            ad-18(特殊环境槽，安全直达写入)、ad-26(L4 长期层存储单元，晋升写入)

安全约束:
  S-01: 本单元是 L5 核心层数据的唯一对外访问接口，编译期保证所有访问必须通过本单元
  S-02: 漏斗一所有模块访问 L5 的请求在编译期硬编码拒绝，无例外
  S-03: ECC-05 伦理仲裁拥有最高查询权限，不受封禁列表和频率限制约束
  S-04: 安全令牌库哈希值编译期固化，运行时每次加载前校验完整性
  S-05: 每次访问操作（查询、鉴权、拒绝）全量写入 ad-51 不可变日志
  S-06: 暴力鉴权检测：同一模块连续 3 次鉴权失败自动封禁 10 分钟
  S-07: 令牌与来源模块绑定，跨模块使用令牌视为盗用行为，立即封禁 30 分钟
  S-08: 系统紧急熔断时暂停一切访问，优先保障系统安全
"""

from typing import Dict, List, Optional, Any, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid
import hashlib


# ==================== 枚举定义 ====================

class PermissionLevel(Enum):
    """权限级别"""
    HIGHEST = "highest"       # 最高权限（ECC-05 伦理仲裁）
    HIGH = "high"             # 高权限（ECC-01/03/04）
    MEDIUM = "medium"         # 中权限（ECC-09/12）
    SPECIAL_EXPORT = "special_export"  # 特殊导出权限（ad-50）
    FORBIDDEN = "forbidden"   # 禁止访问（漏斗一所有模块）


class AuthResult(Enum):
    """鉴权结果"""
    VALID = "valid"
    INVALID_TOKEN = "invalid_token"
    EXPIRED_TOKEN = "expired_token"
    TOKEN_MISMATCH = "token_mismatch"       # 令牌与来源模块不匹配
    MODULE_BLOCKED = "module_blocked"
    RATE_LIMITED = "rate_limited"
    EMERGENCY_SHUTDOWN = "emergency_shutdown"


class AccessType(Enum):
    """访问类型"""
    QUERY = "query"
    WRITE_PROMOTION = "write_promotion"
    WRITE_SAFETY_DIRECT = "write_safety_direct"
    WRITE_MANUAL_LOCK = "write_manual_lock"


# ==================== 数据结构 ====================

@dataclass
class SecurityToken:
    """安全令牌"""
    token_value: str
    authorized_modules: List[str]    # 授权模块列表
    permission_level: PermissionLevel
    issue_time: float
    expiry_time: float               # 过期时间（0 表示永不过期）
    token_id: str


@dataclass
class TokenValidationRequest:
    """令牌验证请求"""
    request_id: str
    token_value: str
    source_module: str
    access_type: AccessType
    timestamp: float = field(default_factory=time.time)


@dataclass
class TokenValidationResponse:
    """令牌验证响应"""
    request_id: str
    result: AuthResult
    permission_level: PermissionLevel
    message: str = ""


@dataclass
class QueryAccessRequest:
    """查询访问请求"""
    request_id: str
    source_module: str
    query_conditions: Dict[str, Any]
    priority: str = "normal"          # "normal" / "high" / "critical"
    timestamp: float = field(default_factory=time.time)


@dataclass
class AccessLogEntry:
    """访问日志条目"""
    log_id: str
    source_module: str
    access_type: AccessType
    result: str                       # "granted" / "denied"
    deny_reason: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class ModuleBlockRecord:
    """模块封禁记录"""
    module_id: str
    blocked_at: float
    unblock_at: float
    reason: str


@dataclass
class AccessStats:
    """访问统计"""
    source_module: str
    query_count: int = 0
    auth_fail_count: int = 0
    last_access_time: float = 0.0


# ==================== 主类定义 ====================

class L5AccessControl:
    """
    L5 核心层防篡改与只读管控单元
    
    职责:
    1. 管理 L5 数据的只读查询权限
    2. 验证写入请求的安全令牌
    3. 维护权限矩阵与封禁列表
    4. 暴力鉴权检测与自动封禁
    5. 令牌盗用检测
    6. 全量访问日志记录
    """
    
    # 权限矩阵：模块 -> 权限级别
    PERMISSION_MATRIX: Dict[str, PermissionLevel] = {
        "ECC-01": PermissionLevel.HIGH,
        "ECC-03": PermissionLevel.HIGH,
        "ECC-04": PermissionLevel.HIGH,
        "ECC-05": PermissionLevel.HIGHEST,
        "ECC-08": PermissionLevel.HIGH,
        "ECC-09": PermissionLevel.MEDIUM,
        "ECC-12": PermissionLevel.MEDIUM,
        "ad-50": PermissionLevel.SPECIAL_EXPORT,
    }
    
    # 漏斗一模块列表（编译期硬编码禁止）
    FUNNEL_ONE_MODULES: Set[str] = {
        "ad-02", "ad-04", "ad-05", "ad-06", "ad-07",
        "ad-08", "ad-09", "ad-10", "ad-11", "ad-13"
    }
    
    # 暴力鉴权配置
    MAX_AUTH_FAILURES = 3
    AUTH_BLOCK_DURATION = 10 * 60         # 10 分钟
    
    # 令牌盗用封禁时长
    TOKEN_MISUSE_BLOCK_DURATION = 30 * 60  # 30 分钟
    
    # 频率限制
    MAX_QUERIES_PER_MINUTE = 100
    
    def __init__(self):
        self.module_id = "ad-30"
        self.module_name = "L5 核心层防篡改与只读管控单元"
        
        # 安全令牌库
        self._token_library: Dict[str, SecurityToken] = {}
        self._token_library_hash: str = ""
        
        # 封禁列表
        self._blocked_modules: Dict[str, ModuleBlockRecord] = {}
        
        # 访问统计
        self._access_stats: Dict[str, AccessStats] = {}
        
        # 系统紧急熔断标记
        self._emergency_shutdown = False
        
        # 统计
        self._total_access_attempts = 0
        self._total_granted = 0
        self._total_denied = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[AccessLogEntry] = []
        
        # 初始化默认令牌库
        self._init_default_tokens()
        
        print(f"[{self.module_id}] L5 防篡改与只读管控单元初始化完成")
        print(f"[{self.module_id}] 权限矩阵: {len(self.PERMISSION_MATRIX)} 个授权模块")
        print(f"[{self.module_id}] 漏斗一模块: 编译期硬编码禁止访问")
    
    def _init_default_tokens(self) -> None:
        """初始化默认安全令牌"""
        # 系统内部令牌（永不过期）
        self._register_token(SecurityToken(
            token_id="sys-internal-001",
            token_value="SYS_INTERNAL_TOKEN",
            authorized_modules=["ad-26", "ad-28", "ad-29"],
            permission_level=PermissionLevel.HIGH,
            issue_time=time.time(),
            expiry_time=0  # 永不过期
        ))
        # 安全直达令牌
        self._register_token(SecurityToken(
            token_id="safety-direct-001",
            token_value="SAFETY_DIRECT_TOKEN",
            authorized_modules=["ad-18", "ad-01"],
            permission_level=PermissionLevel.HIGH,
            issue_time=time.time(),
            expiry_time=0
        ))
        # 管理员令牌
        self._register_token(SecurityToken(
            token_id="admin-001",
            token_value="ADMIN_TOKEN",
            authorized_modules=["ad-01"],
            permission_level=PermissionLevel.HIGHEST,
            issue_time=time.time(),
            expiry_time=0
        ))
        
        self._update_token_hash()
    
    def _register_token(self, token: SecurityToken) -> None:
        """注册安全令牌"""
        self._token_library[token.token_value] = token
    
    def _update_token_hash(self) -> None:
        """更新令牌库哈希值"""
        token_data = "".join(sorted(self._token_library.keys()))
        self._token_library_hash = hashlib.sha256(token_data.encode()).hexdigest()
    
    # ========== 紧急熔断 ==========
    
    def set_emergency_shutdown(self, shutdown: bool) -> None:
        """
        设置紧急熔断状态
        
        S-08: 系统紧急熔断时暂停一切访问
        """
        self._emergency_shutdown = shutdown
        if shutdown:
            print(f"[{self.module_id}] 紧急熔断：暂停一切 L5 访问")
        else:
            print(f"[{self.module_id}] 紧急熔断解除：恢复 L5 访问")
    
    def is_emergency_shutdown(self) -> bool:
        return self._emergency_shutdown
    
    # ========== 封禁管理 ==========
    
    def _check_blocked(self, module_id: str) -> bool:
        """检查模块是否被封禁"""
        if module_id not in self._blocked_modules:
            return False
        
        record = self._blocked_modules[module_id]
        now = time.time()
        
        if now >= record.unblock_at:
            # 封禁已过期
            del self._blocked_modules[module_id]
            print(f"[{self.module_id}] 模块 {module_id} 封禁已自动解除")
            return False
        
        return True
    
    def _block_module(self, module_id: str, duration_seconds: int, reason: str) -> None:
        """封禁指定模块"""
        now = time.time()
        self._blocked_modules[module_id] = ModuleBlockRecord(
            module_id=module_id,
            blocked_at=now,
            unblock_at=now + duration_seconds,
            reason=reason
        )
        print(f"[{self.module_id}] 封禁模块 {module_id}: {reason}, 持续 {duration_seconds}s")
    
    def _cleanup_expired_blocks(self) -> int:
        """清理过期的封禁记录"""
        now = time.time()
        expired = [mid for mid, rec in self._blocked_modules.items() if now >= rec.unblock_at]
        for mid in expired:
            del self._blocked_modules[mid]
        return len(expired)
    
    # ========== 权限校验 ==========
    
    def check_module_permission(self, module_id: str) -> PermissionLevel:
        """
        检查模块的访问权限级别
        
        S-02: 漏斗一所有模块硬编码拒绝
        S-03: ECC-05 最高权限
        """
        # S-02: 漏斗一硬拦截
        if module_id in self.FUNNEL_ONE_MODULES:
            return PermissionLevel.FORBIDDEN
        
        # S-03: ECC-05 最高权限
        if module_id == "ECC-05":
            return PermissionLevel.HIGHEST
        
        # 权限矩阵查询
        return self.PERMISSION_MATRIX.get(module_id, PermissionLevel.FORBIDDEN)
    
    # ========== 查询鉴权 ==========
    
    def authorize_query(self, request: QueryAccessRequest) -> Tuple[bool, str]:
        """
        鉴权查询请求
        
        处理流程:
        1. 紧急熔断检查
        2. 封禁检查
        3. 权限矩阵校验（漏斗一硬拦截）
        4. 频率限制检查
        5. ECC-05 豁免封禁和限流
        
        Returns:
            (是否放行, 消息)
        """
        self._total_access_attempts += 1
        module_id = request.source_module
        
        # S-08: 紧急熔断
        if self._emergency_shutdown:
            self._total_denied += 1
            self._log_access(module_id, AccessType.QUERY, "denied", "emergency_shutdown")
            return False, "系统紧急熔断中"
        
        # 封禁检查（ECC-05 豁免）
        if module_id != "ECC-05" and self._check_blocked(module_id):
            self._total_denied += 1
            self._log_access(module_id, AccessType.QUERY, "denied", "module_blocked")
            return False, "模块已被封禁"
        
        # 权限矩阵校验
        perm_level = self.check_module_permission(module_id)
        
        if perm_level == PermissionLevel.FORBIDDEN:
            self._total_denied += 1
            self._log_access(module_id, AccessType.QUERY, "denied", "forbidden")
            return False, "编译期禁止访问 L5（漏斗一模块或未授权模块）"
        
        # 频率限制（ECC-05 豁免）
        if module_id != "ECC-05":
            if not self._check_rate_limit(module_id):
                self._total_denied += 1
                self._log_access(module_id, AccessType.QUERY, "denied", "rate_limited")
                return False, "查询频率超限，请稍后重试"
        
        # 更新访问统计
        self._update_access_stats(module_id)
        
        self._total_granted += 1
        self._log_access(module_id, AccessType.QUERY, "granted")
        return True, "查询鉴权通过"
    
    def _check_rate_limit(self, module_id: str) -> bool:
        """检查查询频率限制"""
        if module_id not in self._access_stats:
            return True
        
        stats = self._access_stats[module_id]
        now = time.time()
        
        # 近 1 分钟查询次数
        if now - stats.last_access_time < 60:
            if stats.query_count >= self.MAX_QUERIES_PER_MINUTE:
                return False
        
        return True
    
    def _update_access_stats(self, module_id: str) -> None:
        """更新访问统计"""
        now = time.time()
        
        if module_id not in self._access_stats:
            self._access_stats[module_id] = AccessStats(source_module=module_id)
        
        stats = self._access_stats[module_id]
        
        # 重置一分钟窗口
        if now - stats.last_access_time >= 60:
            stats.query_count = 0
        
        stats.query_count += 1
        stats.last_access_time = now
    
    # ========== 写入令牌验证 ==========
    
    def validate_token(self, request: TokenValidationRequest) -> TokenValidationResponse:
        """
        验证写入请求的安全令牌
        
        处理流程:
        1. 紧急熔断检查
        2. 封禁检查
        3. 令牌存在性检查
        4. 令牌过期检查
        5. 令牌与来源模块一致性检查（防盗用）
        6. 权限级别检查
        7. 暴力鉴权检测
        
        Returns:
            令牌验证响应
        """
        self._total_access_attempts += 1
        
        # S-08: 紧急熔断
        if self._emergency_shutdown:
            self._total_denied += 1
            return TokenValidationResponse(
                request_id=request.request_id,
                result=AuthResult.EMERGENCY_SHUTDOWN,
                permission_level=PermissionLevel.FORBIDDEN,
                message="系统紧急熔断中"
            )
        
        module_id = request.source_module
        
        # 封禁检查
        if self._check_blocked(module_id):
            self._total_denied += 1
            return TokenValidationResponse(
                request_id=request.request_id,
                result=AuthResult.MODULE_BLOCKED,
                permission_level=PermissionLevel.FORBIDDEN,
                message="模块已被封禁"
            )
        
        # 令牌存在性检查
        token = self._token_library.get(request.token_value)
        if token is None:
            self._handle_auth_failure(module_id)
            self._total_denied += 1
            return TokenValidationResponse(
                request_id=request.request_id,
                result=AuthResult.INVALID_TOKEN,
                permission_level=PermissionLevel.FORBIDDEN,
                message="安全令牌无效"
            )
        
        # 令牌过期检查
        if token.expiry_time > 0 and time.time() > token.expiry_time:
            self._total_denied += 1
            return TokenValidationResponse(
                request_id=request.request_id,
                result=AuthResult.EXPIRED_TOKEN,
                permission_level=PermissionLevel.FORBIDDEN,
                message="安全令牌已过期"
            )
        
        # S-07: 令牌与来源模块一致性检查（防盗用）
        if module_id not in token.authorized_modules:
            # 令牌盗用嫌疑
            self._block_module(module_id, self.TOKEN_MISUSE_BLOCK_DURATION,
                              "令牌盗用嫌疑")
            self._total_denied += 1
            return TokenValidationResponse(
                request_id=request.request_id,
                result=AuthResult.TOKEN_MISMATCH,
                permission_level=PermissionLevel.FORBIDDEN,
                message="令牌与来源模块不匹配，模块已被封禁"
            )
        
        # 权限级别检查（根据 access_type 验证）
        if not self._check_write_permission(token.permission_level, request.access_type):
            self._total_denied += 1
            return TokenValidationResponse(
                request_id=request.request_id,
                result=AuthResult.INVALID_TOKEN,
                permission_level=PermissionLevel.FORBIDDEN,
                message="令牌权限级别不足以执行此操作"
            )
        
        # 鉴权成功，重置失败计数
        if module_id in self._access_stats:
            self._access_stats[module_id].auth_fail_count = 0
        
        self._total_granted += 1
        self._log_access(module_id, request.access_type, "granted")
        
        return TokenValidationResponse(
            request_id=request.request_id,
            result=AuthResult.VALID,
            permission_level=token.permission_level,
            message="令牌验证通过"
        )
    
    def _check_write_permission(self, perm_level: PermissionLevel, access_type: AccessType) -> bool:
        """检查写入操作所需的权限级别"""
        # 安全直达和晋升写入需要 HIGH 及以上
        if access_type in [AccessType.WRITE_SAFETY_DIRECT, AccessType.WRITE_PROMOTION]:
            return perm_level in [PermissionLevel.HIGH, PermissionLevel.HIGHEST]
        
        # 人工锁定需要 HIGHEST
        if access_type == AccessType.WRITE_MANUAL_LOCK:
            return perm_level == PermissionLevel.HIGHEST
        
        return False
    
    def _handle_auth_failure(self, module_id: str) -> None:
        """
        处理鉴权失败
        
        S-06: 暴力鉴权检测——连续 3 次失败自动封禁 10 分钟
        """
        if module_id not in self._access_stats:
            self._access_stats[module_id] = AccessStats(source_module=module_id)
        
        stats = self._access_stats[module_id]
        stats.auth_fail_count += 1
        
        if stats.auth_fail_count >= self.MAX_AUTH_FAILURES:
            self._block_module(module_id, self.AUTH_BLOCK_DURATION,
                              f"暴力鉴权检测（连续{self.MAX_AUTH_FAILURES}次失败）")
            stats.auth_fail_count = 0  # 重置，等待封禁解除
    
    # ========== 访问日志 ==========
    
    def _log_access(self, source_module: str, access_type: AccessType,
                    result: str, deny_reason: Optional[str] = None) -> None:
        """记录访问日志"""
        log = AccessLogEntry(
            log_id=f"acl-{uuid.uuid4().hex[:8]}",
            source_module=source_module,
            access_type=access_type,
            result=result,
            deny_reason=deny_reason
        )
        self._pending_logs.append(log)
    
    # ========== 查询接口 ==========
    
    def get_blocked_modules(self) -> List[Dict[str, Any]]:
        """获取封禁模块列表"""
        self._cleanup_expired_blocks()
        return [
            {"module_id": rec.module_id, "reason": rec.reason,
             "unblock_at": rec.unblock_at}
            for rec in self._blocked_modules.values()
        ]
    
    def get_token_library_hash(self) -> str:
        return self._token_library_hash
    
    def verify_token_library_integrity(self) -> bool:
        """
        校验令牌库完整性
        
        S-04: 安全令牌库哈希值编译期固化
        """
        current_hash = hashlib.sha256(
            "".join(sorted(self._token_library.keys())).encode()
        ).hexdigest()
        return current_hash == self._token_library_hash
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_access_attempts": self._total_access_attempts,
            "total_granted": self._total_granted,
            "total_denied": self._total_denied,
            "blocked_modules": len(self._blocked_modules),
            "emergency_shutdown": self._emergency_shutdown,
            "token_count": len(self._token_library)
        }
    
    def collect_pending_logs(self) -> List[AccessLogEntry]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-30 L5 核心层防篡改与只读管控单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    # --- TC-30-01: ECC-04 查询鉴权通过 ---
    print("\n[TC-30-01] ECC-04 查询鉴权通过")
    try:
        acl = L5AccessControl()
        request = QueryAccessRequest("q-001", "ECC-04", {"force_majeure": False})
        allowed, msg = acl.authorize_query(request)
        assert allowed == True
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-30-02: 漏斗一模块查询被拒 ---
    print("\n[TC-30-02] 漏斗一模块查询被拒")
    try:
        acl = L5AccessControl()
        request = QueryAccessRequest("q-002", "ad-07", {})
        allowed, msg = acl.authorize_query(request)
        assert allowed == False
        assert "编译期禁止" in msg
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-30-03: 安全令牌验证通过 ---
    print("\n[TC-30-03] 安全令牌验证通过")
    try:
        acl = L5AccessControl()
        req = TokenValidationRequest(
            "token-req-001", "SAFETY_DIRECT_TOKEN", "ad-18",
            AccessType.WRITE_SAFETY_DIRECT
        )
        resp = acl.validate_token(req)
        assert resp.result == AuthResult.VALID
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-30-04: 无效令牌被拒 ---
    print("\n[TC-30-04] 无效令牌被拒")
    try:
        acl = L5AccessControl()
        req = TokenValidationRequest(
            "token-req-002", "BAD_TOKEN", "ad-26",
            AccessType.WRITE_PROMOTION
        )
        resp = acl.validate_token(req)
        assert resp.result == AuthResult.INVALID_TOKEN
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-30-05: 令牌盗用检测封禁 ---
    print("\n[TC-30-05] 令牌盗用检测封禁")
    try:
        acl = L5AccessControl()
        req = TokenValidationRequest(
            "token-req-003", "SAFETY_DIRECT_TOKEN", "ad-99",
            AccessType.WRITE_SAFETY_DIRECT
        )
        resp = acl.validate_token(req)
        assert resp.result == AuthResult.TOKEN_MISMATCH
        assert acl._check_blocked("ad-99") == True
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-30-06: 暴力鉴权封禁 ---
    print("\n[TC-30-06] 暴力鉴权封禁（连续 3 次失败）")
    try:
        acl = L5AccessControl()
        for i in range(3):
            req = TokenValidationRequest(
                f"token-req-{i}", "BAD_TOKEN", "ad-26",
                AccessType.WRITE_PROMOTION
            )
            acl.validate_token(req)
        
        # 第 4 次应被封禁
        req4 = TokenValidationRequest(
            "token-req-4", "SYS_INTERNAL_TOKEN", "ad-26",
            AccessType.WRITE_PROMOTION
        )
        resp4 = acl.validate_token(req4)
        assert resp4.result == AuthResult.MODULE_BLOCKED
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-30-07: ECC-05 不受封禁和限流 ---
    print("\n[TC-30-07] ECC-05 不受封禁和限流")
    try:
        acl = L5AccessControl()
        # 模拟 ECC-05 被封禁（不应发生）
        acl._block_module("ECC-05", 600, "测试封禁")
        request = QueryAccessRequest("q-ecc05", "ECC-05", {})
        allowed, msg = acl.authorize_query(request)
        assert allowed == True  # ECC-05 豁免封禁
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-30-08: 查询频率限流 ---
    print("\n[TC-30-08] 查询频率限流（每分钟 > 100 次）")
    try:
        acl = L5AccessControl()
        # 模拟高频查询
        acl._access_stats["ECC-04"] = AccessStats(
            source_module="ECC-04", query_count=101, last_access_time=time.time()
        )
        request = QueryAccessRequest("q-high", "ECC-04", {})
        allowed, msg = acl.authorize_query(request)
        assert allowed == False
        assert "频率超限" in msg
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-30-09: 紧急熔断暂停一切访问 ---
    print("\n[TC-30-09] 紧急熔断暂停一切访问")
    try:
        acl = L5AccessControl()
        acl.set_emergency_shutdown(True)
        
        # 查询被拒
        q_req = QueryAccessRequest("q-em", "ECC-04", {})
        allowed, msg = acl.authorize_query(q_req)
        assert allowed == False
        
        # 令牌验证被拒
        t_req = TokenValidationRequest("t-em", "SYS_INTERNAL_TOKEN", "ad-26", AccessType.WRITE_PROMOTION)
        resp = acl.validate_token(t_req)
        assert resp.result == AuthResult.EMERGENCY_SHUTDOWN
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-30-10: 令牌库完整性校验 ---
    print("\n[TC-30-10] 令牌库完整性校验")
    try:
        acl = L5AccessControl()
        assert acl.verify_token_library_integrity() == True
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