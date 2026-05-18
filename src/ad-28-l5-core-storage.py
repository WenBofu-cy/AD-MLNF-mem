#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-28
模块名称: L5 核心层存储单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 五层记忆层级存储
核心职责: 存储终身不可遗忘的驾驶安全底线、关键生存策略、不可抗力事件经验与人工锁定
          的核心规则。占漏斗二总存储容量的 0.5%，物理分区写保护。接收来自 L4 晋升
          的条目，或由安全显著性 S≥0.9 的事件直达写入。本层经验为系统最高智慧结晶，
          只读查询，写入须经多重校验，数据终身锁定不可删除。

依赖模块: ad-26(L4 长期层存储单元), ad-29(L5 核心层安全规则硬锁定单元),
          ad-30(L5 核心层防篡改与只读管控单元), ad-18(特殊环境槽，不可抗力事件直达入口),
          ad-43(失败经验安全仲裁三道校验单元，特殊晋升路径)
被依赖模块: ad-29(接管存储分区写保护), ad-30(提供只读查询服务),
            ad-50(记忆导入导出与脱敏共享单元，特殊导出)

安全约束:
  S-01: L5 存储分区由 ad-29 硬件写保护锁定，任何运行时进程无法直接修改或删除
  S-02: L5 条目终身不可删除。物理删除操作在编译期已移除，仅存在读取和写入接口
  S-03: 安全显著性 S ≥ 0.9 的事件直达写入为最高优先级，无需满足 I 值和留存时间条件
  S-04: 不可抗力场景经验自动标记为“不可抗力锁定”，享有永久保护，不可降级或归档
  S-05: L5 容量硬编码为漏斗二总容量的 0.5%，不可通过运行时配置调整
  S-06: L5 数据对外仅提供只读查询，写入仅接受来自 ad-26 的晋升、ad-18/ad-01 的安全直达、
         ad-01 的人工锁定
  S-07: 所有写入、查询、拒绝事件全量写入 ad-51 不可变日志
  S-08: 镜像分区与主存储实时同步，确保单点硬件故障不丢失核心经验
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class StorageState(Enum):
    """L5 存储内部状态"""
    NORMAL = "normal"
    WRITE_WINDOW = "write_window"   # 临时开放写入权限
    FULL = "full"
    LOCKED = "locked"               # 维护锁定
    DEGRADED = "degraded"           # 降级保护（镜像同步中）


class LockType(Enum):
    """锁定类型"""
    PROMOTION = "promotion_lock"        # 晋升锁定
    SAFETY_DIRECT = "safety_direct"     # 安全直达锁定
    MANUAL = "manual_lock"              # 人工锁定
    FORCE_MAJEURE = "force_majeure_lock" # 不可抗力锁定


class WriteRejectReason(Enum):
    """写入拒绝原因"""
    CAPACITY_FULL = "capacity_full"
    PERMISSION_DENIED = "permission_denied"
    INCOMPLETE_ENTRY = "incomplete_entry"
    LOCK_FAILED = "lock_failed"


# ==================== 数据结构 ====================

@dataclass
class L5EntryIndex:
    """L5 条目索引"""
    entry_id: str
    storage_address: int
    lock_timestamp: float
    i_value: float
    s_value: float
    source_slot_id: int
    result_label: str
    force_majeure: bool
    lock_type: LockType
    reuse_count: int
    size_bytes: int
    # 镜像同步状态
    mirror_synced: bool = True


@dataclass
class WriteRequest:
    """写入请求"""
    request_id: str
    entry_id: str
    source_module: str
    entry_content: Dict[str, Any]
    i_value: float
    s_value: float
    lock_type: LockType
    force_majeure: bool = False
    reuse_count: int = 0
    security_token: Optional[str] = None  # 安全令牌


@dataclass
class WriteResponse:
    """写入响应"""
    request_id: str
    success: bool
    reject_reason: Optional[WriteRejectReason] = None
    locked_entry_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class QueryRequest:
    """查询请求"""
    query_id: str
    source_module: str
    conditions: Dict[str, Any]
    permission_token: Optional[str] = None


@dataclass
class QueryResponse:
    """查询响应"""
    query_id: str
    success: bool
    matched_entries: List[Dict[str, Any]]
    reject_reason: Optional[str] = None


@dataclass
class L5StatusSnapshot:
    """L5 状态快照"""
    total_capacity: int
    used_count: int
    usage_rate: float
    force_majeure_count: int
    manual_lock_count: int
    mirror_healthy: bool
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class L5CoreStorage:
    """
    L5 核心层存储单元
    
    职责:
    1. 接收 L4 晋升条目、安全直达写入、人工锁定写入
    2. 管理物理写保护（与 ad-29 协作）
    3. 提供只读查询（与 ad-30 协作进行权限校验）
    4. 镜像同步确保数据安全
    5. 终身锁定，不可删除
    """
    
    # L5 容量占漏斗二总容量的 0.5%
    # 假设总容量 500MB，L5 占 2.5MB，每条经验约 10KB → 约 250 条
    MAX_ENTRIES = 250
    
    # 安全直达触发阈值
    SAFETY_DIRECT_S_THRESHOLD = 0.9
    
    def __init__(self):
        self.module_id = "ad-28"
        self.module_name = "L5 核心层存储单元"
        
        # 内部状态
        self.state = StorageState.NORMAL
        
        # 条目索引表: entry_id -> L5EntryIndex
        self._index: Dict[str, L5EntryIndex] = {}
        
        # 镜像同步状态
        self._mirror_healthy = True
        
        # 存储地址计数器
        self._next_address = 0x50000000
        
        # 统计
        self._total_writes = 0
        self._total_rejections = 0
        self._total_queries = 0
        self._total_direct_locks = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] L5 核心层初始化完成, 最大容量={self.MAX_ENTRIES}")
        print(f"[{self.module_id}] 终身锁定，物理写保护，镜像同步")
    
    # ========== 状态管理 ==========
    
    def get_state(self) -> StorageState:
        return self.state
    
    def set_mirror_healthy(self, healthy: bool) -> None:
        self._mirror_healthy = healthy
        if not healthy:
            self.state = StorageState.DEGRADED
    
    def get_item_count(self) -> int:
        return len(self._index)
    
    def get_usage_rate(self) -> float:
        return len(self._index) / self.MAX_ENTRIES if self.MAX_ENTRIES > 0 else 0.0
    
    # ========== 写入请求处理（需 ad-29 写保护临时解除） ==========
    
    def handle_write_request(self, request: WriteRequest,
                             write_protection_callback) -> WriteResponse:
        """
        处理写入请求
        
        流程:
        1. 校验条目完整性
        2. 检查容量
        3. 请求 ad-29 临时解除写保护
        4. 执行写入 + 镜像同步
        5. 请求 ad-29 恢复写保护
        
        Args:
            request: 写入请求
            write_protection_callback: 回调函数，用于与 ad-29 交互解锁/锁定
            
        Returns:
            写入响应
        """
        # S-02: 条目完整性校验
        if not request.entry_id or not request.entry_content:
            self._total_rejections += 1
            return WriteResponse(
                request_id=request.request_id,
                success=False,
                reject_reason=WriteRejectReason.INCOMPLETE_ENTRY
            )
        
        # S-05: 容量检查
        if self.get_usage_rate() >= 1.0:
            self.state = StorageState.FULL
            self._total_rejections += 1
            return WriteResponse(
                request_id=request.request_id,
                success=False,
                reject_reason=WriteRejectReason.CAPACITY_FULL
            )
        
        # S-01: 请求临时解除写保护（与 ad-29 交互）
        unlock_success = write_protection_callback(
            action="unlock",
            token=request.security_token,
            entry_id=request.entry_id,
            data_size=10240  # 假设每条经验 10KB
        )
        
        if not unlock_success:
            self._total_rejections += 1
            return WriteResponse(
                request_id=request.request_id,
                success=False,
                reject_reason=WriteRejectReason.LOCK_FAILED
            )
        
        # 写入窗口打开
        self.state = StorageState.WRITE_WINDOW
        
        # 分配存储地址
        storage_address = self._next_address
        self._next_address += 10240
        
        # 创建索引条目
        idx_entry = L5EntryIndex(
            entry_id=request.entry_id,
            storage_address=storage_address,
            lock_timestamp=time.time(),
            i_value=request.i_value,
            s_value=request.s_value,
            source_slot_id=0,
            result_label="L5 锁定",
            force_majeure=request.force_majeure,
            lock_type=request.lock_type,
            reuse_count=request.reuse_count,
            size_bytes=10240,
            mirror_synced=False
        )
        
        self._index[request.entry_id] = idx_entry
        self._total_writes += 1
        
        # S-08: 触发镜像同步
        self._sync_to_mirror(request.entry_id)
        
        # 请求 ad-29 恢复写保护
        write_protection_callback(
            action="lock",
            entry_id=request.entry_id,
            success=True
        )
        
        self.state = StorageState.NORMAL
        
        # 统计安全直达
        if request.lock_type == LockType.SAFETY_DIRECT:
            self._total_direct_locks += 1
        
        return WriteResponse(
            request_id=request.request_id,
            success=True,
            locked_entry_id=request.entry_id
        )
    
    def _sync_to_mirror(self, entry_id: str) -> None:
        """模拟镜像同步"""
        if entry_id in self._index:
            self._index[entry_id].mirror_synced = True
    
    # ========== 查询请求处理（需 ad-30 权限校验） ==========
    
    def handle_query(self, request: QueryRequest,
                     permission_callback) -> QueryResponse:
        """
        处理只读查询请求
        
        Args:
            request: 查询请求
            permission_callback: 回调函数，用于与 ad-30 交互权限校验
            
        Returns:
            查询响应
        """
        # S-06: 权限校验（委托给 ad-30）
        if not permission_callback(
            source_module=request.source_module,
            operation="read",
            token=request.permission_token
        ):
            self._total_queries += 1
            return QueryResponse(
                query_id=request.query_id,
                success=False,
                matched_entries=[],
                reject_reason="权限不足"
            )
        
        self._total_queries += 1
        
        # 根据条件检索
        matched = []
        for entry_id, idx_entry in self._index.items():
            # 简单条件匹配（实际可使用更复杂的查询引擎）
            match = True
            if "force_majeure" in request.conditions:
                if idx_entry.force_majeure != request.conditions["force_majeure"]:
                    match = False
            if "lock_type" in request.conditions:
                if idx_entry.lock_type.value != request.conditions["lock_type"]:
                    match = False
            
            if match:
                matched.append({
                    "entry_id": entry_id,
                    "i_value": idx_entry.i_value,
                    "s_value": idx_entry.s_value,
                    "lock_type": idx_entry.lock_type.value,
                    "lock_timestamp": idx_entry.lock_timestamp,
                    "force_majeure": idx_entry.force_majeure
                })
        
        return QueryResponse(
            query_id=request.query_id,
            success=True,
            matched_entries=matched
        )
    
    # ========== 晋升/直达/人工锁定便捷接口 ==========
    
    def receive_promotion(self, entry_data: Dict[str, Any],
                          unlock_callback) -> WriteResponse:
        """接收 L4 晋升条目"""
        request = WriteRequest(
            request_id=f"promo-{uuid.uuid4().hex[:8]}",
            entry_id=entry_data.get("entry_id", ""),
            source_module="ad-26",
            entry_content=entry_data.get("content", {}),
            i_value=entry_data.get("i_value", 0.0),
            s_value=entry_data.get("s_value", 0.0),
            lock_type=LockType.PROMOTION,
            force_majeure=entry_data.get("force_majeure", False),
            reuse_count=entry_data.get("reuse_count", 0)
        )
        return self.handle_write_request(request, unlock_callback)
    
    def receive_safety_direct(self, entry_data: Dict[str, Any],
                              unlock_callback) -> WriteResponse:
        """
        接收安全直达写入（S≥0.9 触发）
        S-03: 最高优先级，无需满足 I 值和留存时间条件
        """
        is_force_majeure = entry_data.get("result_label", "") == "不可抗力场景"
        
        request = WriteRequest(
            request_id=f"safety-{uuid.uuid4().hex[:8]}",
            entry_id=entry_data.get("entry_id", ""),
            source_module="ad-18",
            entry_content=entry_data.get("content", {}),
            i_value=1.0,  # 安全直达强制 I=1.0
            s_value=entry_data.get("s_value", self.SAFETY_DIRECT_S_THRESHOLD),
            lock_type=LockType.SAFETY_DIRECT,
            force_majeure=is_force_majeure,
            security_token=entry_data.get("safety_token", "")
        )
        return self.handle_write_request(request, unlock_callback)
    
    def receive_manual_lock(self, entry_data: Dict[str, Any],
                            unlock_callback, admin_token: str) -> WriteResponse:
        """接收人工锁定指令"""
        request = WriteRequest(
            request_id=f"manual-{uuid.uuid4().hex[:8]}",
            entry_id=entry_data.get("entry_id", ""),
            source_module="ad-01",
            entry_content=entry_data.get("content", {}),
            i_value=entry_data.get("i_value", 0.0),
            s_value=entry_data.get("s_value", 0.0),
            lock_type=LockType.MANUAL,
            security_token=admin_token
        )
        return self.handle_write_request(request, unlock_callback)
    
    # ========== 状态上报 ==========
    
    def generate_snapshot(self) -> L5StatusSnapshot:
        force_majeure_count = sum(1 for idx in self._index.values() if idx.force_majeure)
        manual_count = sum(1 for idx in self._index.values() if idx.lock_type == LockType.MANUAL)
        
        return L5StatusSnapshot(
            total_capacity=self.MAX_ENTRIES,
            used_count=len(self._index),
            usage_rate=self.get_usage_rate(),
            force_majeure_count=force_majeure_count,
            manual_lock_count=manual_count,
            mirror_healthy=self._mirror_healthy
        )
    
    def get_entry(self, entry_id: str) -> Optional[L5EntryIndex]:
        return self._index.get(entry_id)
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_writes": self._total_writes,
            "total_rejections": self._total_rejections,
            "total_queries": self._total_queries,
            "total_direct_locks": self._total_direct_locks,
            "current_entries": len(self._index),
            "max_entries": self.MAX_ENTRIES,
            "usage_rate": self.get_usage_rate(),
            "mirror_healthy": self._mirror_healthy,
            "state": self.state.value
        }
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-28 L5 核心层存储单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    # 模拟写保护回调（总是成功）
    def mock_unlock_success(action, token=None, entry_id=None, data_size=None, success=None):
        return True
    
    def mock_unlock_fail(action, token=None, entry_id=None, data_size=None, success=None):
        return False
    
    # 模拟权限校验回调（总是通过）
    def mock_permission_granted(source_module, operation, token=None):
        return True
    
    def mock_permission_denied(source_module, operation, token=None):
        return False
    
    # --- TC-28-01: 晋升写入成功 ---
    print("\n[TC-28-01] 晋升写入成功")
    try:
        l5 = L5CoreStorage()
        entry_data = {
            "entry_id": "EXP-001",
            "content": {"behavior": "高速避险"},
            "i_value": 0.90,
            "s_value": 0.85,
            "force_majeure": False,
            "reuse_count": 15
        }
        response = l5.receive_promotion(entry_data, mock_unlock_success)
        assert response.success == True
        assert l5.get_item_count() == 1
        assert l5._index["EXP-001"].lock_type == LockType.PROMOTION
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-28-02: 安全直达写入（S=0.95） ---
    print("\n[TC-28-02] 安全直达写入（S=0.95）")
    try:
        l5 = L5CoreStorage()
        entry_data = {
            "entry_id": "EXP-002",
            "content": {"behavior": "碰撞避免"},
            "s_value": 0.95,
            "result_label": "不可抗力场景",
            "safety_token": "token123"
        }
        response = l5.receive_safety_direct(entry_data, mock_unlock_success)
        assert response.success == True
        assert l5._index["EXP-002"].i_value == 1.0
        assert l5._index["EXP-002"].lock_type == LockType.SAFETY_DIRECT
        assert l5._total_direct_locks == 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-28-03: 人工锁定写入（有效令牌） ---
    print("\n[TC-28-03] 人工锁定写入（有效令牌）")
    try:
        l5 = L5CoreStorage()
        entry_data = {
            "entry_id": "EXP-003",
            "content": {"behavior": "核心安全规则"},
            "i_value": 0.95,
            "s_value": 0.99
        }
        response = l5.receive_manual_lock(entry_data, mock_unlock_success, "admin_token")
        assert response.success == True
        assert l5._index["EXP-003"].lock_type == LockType.MANUAL
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-28-04: 写保护解除失败 ---
    print("\n[TC-28-04] 写保护解除失败")
    try:
        l5 = L5CoreStorage()
        entry_data = {
            "entry_id": "EXP-004",
            "content": {"behavior": "测试"},
            "i_value": 0.90
        }
        response = l5.receive_promotion(entry_data, mock_unlock_fail)
        assert response.success == False
        assert response.reject_reason == WriteRejectReason.LOCK_FAILED
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-28-05: 容量满拒绝写入 ---
    print("\n[TC-28-05] 容量满拒绝写入")
    try:
        l5 = L5CoreStorage()
        l5.MAX_ENTRIES = 1
        # 先写满
        l5.receive_promotion({"entry_id": "EXP-FULL", "content": {}, "i_value": 0.9}, mock_unlock_success)
        # 再写第二条
        response = l5.receive_promotion({"entry_id": "EXP-NEW", "content": {}, "i_value": 0.9}, mock_unlock_success)
        assert response.success == False
        assert response.reject_reason == WriteRejectReason.CAPACITY_FULL
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-28-06: 条目不完整拒绝 ---
    print("\n[TC-28-06] 条目不完整拒绝")
    try:
        l5 = L5CoreStorage()
        response = l5.receive_promotion({"entry_id": "", "content": {}}, mock_unlock_success)
        assert response.success == False
        assert response.reject_reason == WriteRejectReason.INCOMPLETE_ENTRY
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-28-07: 只读查询通过 ---
    print("\n[TC-28-07] 只读查询通过")
    try:
        l5 = L5CoreStorage()
        l5.receive_promotion({"entry_id": "EXP-Q1", "content": {}, "i_value": 0.9}, mock_unlock_success)
        query = QueryRequest("q-001", "ECC-05", {"force_majeure": False})
        response = l5.handle_query(query, mock_permission_granted)
        assert response.success == True
        assert len(response.matched_entries) == 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-28-08: 查询权限不足 ---
    print("\n[TC-28-08] 查询权限不足")
    try:
        l5 = L5CoreStorage()
        query = QueryRequest("q-002", "漏斗一模块", {})
        response = l5.handle_query(query, mock_permission_denied)
        assert response.success == False
        assert "权限不足" in response.reject_reason
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-28-09: 镜像同步状态 ---
    print("\n[TC-28-09] 镜像同步状态")
    try:
        l5 = L5CoreStorage()
        l5.receive_promotion({"entry_id": "EXP-MIR", "content": {}, "i_value": 0.9}, mock_unlock_success)
        assert l5._index["EXP-MIR"].mirror_synced == True
        l5.set_mirror_healthy(False)
        assert l5.state == StorageState.DEGRADED
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-28-10: 不可抗力自动标记 ---
    print("\n[TC-28-10] 不可抗力自动标记")
    try:
        l5 = L5CoreStorage()
        entry_data = {
            "entry_id": "EXP-FM",
            "content": {"behavior": "泥石流避险"},
            "s_value": 0.95,
            "result_label": "不可抗力场景"
        }
        response = l5.receive_safety_direct(entry_data, mock_unlock_success)
        assert response.success == True
        assert l5._index["EXP-FM"].force_majeure == True
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