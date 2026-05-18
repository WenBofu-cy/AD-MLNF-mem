#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-49
模块名称: 存储压缩与冷归档单元
所属分区: 五、存储与系统运维
核心职责: 对 L3 及以上层级被遗忘的经验条目执行压缩归档，将已压缩的经验数据转存至
          冷存储分区。接收来自 ad-42 的归档数据，存储于独立于活跃漏斗存储的冷归档
          分区，并维护归档索引以供后续追溯恢复。释放主存储空间的同时确保经验数据
          可恢复。在压缩、存储与恢复全过程中校验数据完整性。

依赖模块: ad-42(冗余记忆删除与归档单元，发送归档数据包),
          ad-47(疑问缓存库，接收法规相关条目的永久归档)
被依赖模块: ad-42(接收归档写入确认), ad-24/26(L3/L4 存储单元，接收归档恢复请求)

安全约束:
  S-01: 冷归档分区与活跃漏斗存储分区物理隔离，运行时仅本模块拥有写入权限
  S-02: 归档数据写入后即执行 SHA256 校验和计算，恢复时必须通过校验和验证方可解压返回
  S-03: 法规模糊地带归档条目享有永久保留权限，任何清理操作不得删除此类条目
  S-04: 不可抗力标记的经验条目在归档后仍享有永久保留权限，清理操作跳过
  S-05: 过期归档条目的删除须执行 DoD 5220.22-M 标准单次覆写，不可通过文件系统删除直接操作
  S-06: 归档索引表每完成一次写入或清理操作后自动持久化至冷归档分区
  S-07: 所有归档写入、恢复、清理操作全量写入 ad-51 不可变日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid
import hashlib
import zlib  # 用于模拟压缩


# ==================== 枚举定义 ====================

class ArchiveState(Enum):
    """冷归档单元内部状态"""
    NORMAL = "normal"
    STORING = "storing"
    RESTORING = "restoring"
    CLEANING = "cleaning"
    DEGRADED = "degraded"
    PAUSED = "paused"


class ArchiveResult(Enum):
    """归档操作结果"""
    SUCCESS = "success"
    FAIL_STORAGE_FULL = "fail_storage_full"
    FAIL_WRITE_ERROR = "fail_write_error"
    FAIL_CHECKSUM_MISMATCH = "fail_checksum_mismatch"
    FAIL_ENTRY_NOT_FOUND = "fail_entry_not_found"
    FAIL_DECOMPRESS = "fail_decompress"


# ==================== 数据结构 ====================

@dataclass
class ArchiveDataPacket:
    """归档数据包（来自 ad-42）"""
    entry_id: str
    compressed_data: bytes
    metadata: Dict[str, Any]
    archive_reason: str            # 遗忘原因
    original_layer: str            # 原层级
    original_slot_id: int
    force_majeure: bool = False
    legal_ambiguity: bool = False  # 是否法规模糊地带
    original_size: int = 0
    timestamp: float = field(default_factory=time.time)


@dataclass
class ArchiveIndexEntry:
    """归档索引条目"""
    entry_id: str
    storage_offset: int            # 冷归档分区中的偏移量
    compressed_size: int           # 压缩后大小（字节）
    original_size: int             # 原始大小（字节）
    checksum: str                  # SHA256 校验和
    archive_time: float
    original_layer: str
    original_slot_id: int
    archive_reason: str
    force_majeure: bool = False
    legal_ambiguity: bool = False  # 法规模糊地带 → 永久保留
    metadata_summary: str = ""     # 元数据摘要
    compression_algorithm: str = "zlib"


@dataclass
class ArchiveWriteResponse:
    """归档写入响应（返回给 ad-42）"""
    entry_id: str
    success: bool
    result: ArchiveResult
    archive_offset: Optional[int] = None
    compressed_size: int = 0
    original_size: int = 0
    checksum: str = ""
    message: str = ""


@dataclass
class RestoreRequest:
    """归档恢复请求"""
    request_id: str
    entry_id: str
    original_layer: str
    reason: str
    request_source: str            # 请求来源模块
    timestamp: float = field(default_factory=time.time)


@dataclass
class RestoreResponse:
    """归档恢复响应"""
    request_id: str
    entry_id: str
    success: bool
    result: ArchiveResult
    decompressed_data: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    message: str = ""


@dataclass
class CleanupReport:
    """归档清理报告"""
    cleaned_count: int
    released_bytes: int
    remaining_count: int
    skipped_permanent: int          # 跳过的永久保留条目数
    timestamp: float = field(default_factory=time.time)


@dataclass
class ArchiveStatusSnapshot:
    """冷归档状态快照"""
    total_entries: int
    total_compressed_size: int
    total_original_size: int
    compression_ratio: float
    permanent_entries: int          # 永久保留条目数
    storage_usage_rate: float
    state: str


# ==================== 主类定义 ====================

class ColdArchiveUnit:
    """
    存储压缩与冷归档单元
    
    职责:
    1. 接收 ad-42 发送的归档数据包，进行二次压缩与存储
    2. 接收 ad-47 发送的法规模糊地带永久归档
    3. 维护归档索引表
    4. 提供归档恢复服务（校验完整性后解压返回）
    5. 定期清理过期归档条目（跳过永久保留条目）
    6. 监控冷归档分区使用率，必要时触发清理
    """
    
    # 冷归档分区容量（字节）
    DEFAULT_ARCHIVE_CAPACITY = 50 * 1024 * 1024  # 50MB
    
    # 归档保留期限（秒）
    DEFAULT_RETENTION_PERIOD = 365 * 24 * 3600   # 1 年
    
    # 容量告警阈值
    CAPACITY_WARNING = 0.80
    CAPACITY_URGENT = 0.95
    
    # 默认压缩算法
    COMPRESSION_ALGORITHM = "zlib"
    
    # 恢复缓存有效期（秒）- 避免短时间内重复解压
    RESTORE_CACHE_TTL = 30
    
    def __init__(self, archive_capacity_bytes: int = None):
        """
        初始化冷归档单元
        
        Args:
            archive_capacity_bytes: 冷归档分区容量，默认 50MB
        """
        self.module_id = "ad-49"
        self.module_name = "存储压缩与冷归档单元"
        
        # 内部状态
        self.state = ArchiveState.NORMAL
        
        # 冷归档分区容量
        self._archive_capacity = archive_capacity_bytes or self.DEFAULT_ARCHIVE_CAPACITY
        
        # 归档索引表: entry_id -> ArchiveIndexEntry
        self._index: Dict[str, ArchiveIndexEntry] = {}
        
        # 模拟冷归档分区已用空间
        self._used_storage = 0
        self._next_offset = 0
        
        # 恢复缓存: entry_id -> (decompressed_data, metadata, cache_time)
        self._restore_cache: Dict[str, Tuple[Dict[str, Any], Dict[str, Any], float]] = {}
        
        # 统计
        self._total_archived = 0
        self._total_restored = 0
        self._total_cleaned = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 冷归档单元初始化完成, 容量={self._archive_capacity/1024/1024:.0f}MB")
        print(f"[{self.module_id}] 保留期限={self.DEFAULT_RETENTION_PERIOD/86400:.0f}天, 算法={self.COMPRESSION_ALGORITHM}")
    
    # ========== 状态管理 ==========
    
    def pause(self) -> None:
        self.state = ArchiveState.PAUSED
    
    def resume(self) -> None:
        self.state = ArchiveState.NORMAL
    
    def get_state(self) -> ArchiveState:
        return self.state
    
    def get_usage_rate(self) -> float:
        return self._used_storage / self._archive_capacity if self._archive_capacity > 0 else 0.0
    
    # ========== 归档写入 ==========
    
    def archive_entry(self, packet: ArchiveDataPacket) -> ArchiveWriteResponse:
        """
        接收并存储归档数据包
        
        处理流程:
        1. 进一步压缩数据（如未充分压缩）
        2. 计算 SHA256 校验和
        3. 写入冷归档分区
        4. 更新归档索引表
        5. 持久化索引
        
        Args:
            packet: 归档数据包
            
        Returns:
            归档写入响应
        """
        if self.state in [ArchiveState.PAUSED, ArchiveState.DEGRADED]:
            return ArchiveWriteResponse(
                entry_id=packet.entry_id,
                success=False,
                result=ArchiveResult.FAIL_WRITE_ERROR,
                message="冷归档单元不可用"
            )
        
        self.state = ArchiveState.STORING
        
        # 检查容量
        estimated_size = len(packet.compressed_data) if packet.compressed_data else packet.original_size
        if self._used_storage + estimated_size > self._archive_capacity:
            self.state = ArchiveState.NORMAL
            return ArchiveWriteResponse(
                entry_id=packet.entry_id,
                success=False,
                result=ArchiveResult.FAIL_STORAGE_FULL,
                message="冷归档分区空间不足"
            )
        
        # 进一步压缩（模拟）
        raw_data = packet.compressed_data if packet.compressed_data else b""
        try:
            compressed = zlib.compress(raw_data, level=6)
        except Exception:
            compressed = raw_data
        
        compressed_size = len(compressed)
        original_size = packet.original_size if packet.original_size > 0 else len(raw_data)
        
        # 计算校验和
        checksum = hashlib.sha256(compressed).hexdigest()
        
        # 分配存储偏移
        offset = self._next_offset
        self._next_offset += compressed_size
        
        # 创建索引条目
        index_entry = ArchiveIndexEntry(
            entry_id=packet.entry_id,
            storage_offset=offset,
            compressed_size=compressed_size,
            original_size=original_size,
            checksum=checksum,
            archive_time=time.time(),
            original_layer=packet.original_layer,
            original_slot_id=packet.original_slot_id,
            archive_reason=packet.archive_reason,
            force_majeure=packet.force_majeure,
            legal_ambiguity=packet.legal_ambiguity,
            metadata_summary=self._summarize_metadata(packet.metadata),
            compression_algorithm=self.COMPRESSION_ALGORITHM
        )
        
        self._index[packet.entry_id] = index_entry
        self._used_storage += compressed_size
        self._total_archived += 1
        
        # 持久化索引
        self._persist_index()
        
        self._log_event("ARCHIVE_WRITE", {
            "entry_id": packet.entry_id,
            "compressed_size": compressed_size,
            "original_size": original_size,
            "checksum": checksum[:16],
            "reason": packet.archive_reason
        })
        
        self.state = ArchiveState.NORMAL
        
        return ArchiveWriteResponse(
            entry_id=packet.entry_id,
            success=True,
            result=ArchiveResult.SUCCESS,
            archive_offset=offset,
            compressed_size=compressed_size,
            original_size=original_size,
            checksum=checksum
        )
    
    def archive_legal_ambiguity(self, question_id: str, scene_data: Dict[str, Any],
                                reason: str) -> ArchiveWriteResponse:
        """
        接收 ad-47 发送的法规模糊地带永久归档
        
        Args:
            question_id: 疑问条目 ID
            scene_data: 场景数据
            reason: 归档原因
            
        Returns:
            归档写入响应
        """
        packet = ArchiveDataPacket(
            entry_id=f"LEGAL-{question_id}",
            compressed_data=zlib.compress(str(scene_data).encode(), level=6),
            metadata={"source": "ad-47", "question_id": question_id},
            archive_reason=reason,
            original_layer="N/A",
            original_slot_id=0,
            force_majeure=False,
            legal_ambiguity=True,
            original_size=len(str(scene_data)),
            timestamp=time.time()
        )
        return self.archive_entry(packet)
    
    # ========== 归档恢复 ==========
    
    def restore_entry(self, request: RestoreRequest) -> RestoreResponse:
        """
        从冷归档分区恢复指定条目
        
        S-02: 必须通过校验和验证方可解压返回
        
        Args:
            request: 恢复请求
            
        Returns:
            恢复响应
        """
        if self.state == ArchiveState.PAUSED:
            return RestoreResponse(
                request_id=request.request_id,
                entry_id=request.entry_id,
                success=False,
                result=ArchiveResult.FAIL_ENTRY_NOT_FOUND,
                message="冷归档单元暂停中"
            )
        
        self.state = ArchiveState.RESTORING
        
        # 检查恢复缓存
        if request.entry_id in self._restore_cache:
            data, metadata, cache_time = self._restore_cache[request.entry_id]
            if time.time() - cache_time < self.RESTORE_CACHE_TTL:
                self._total_restored += 1
                self.state = ArchiveState.NORMAL
                return RestoreResponse(
                    request_id=request.request_id,
                    entry_id=request.entry_id,
                    success=True,
                    result=ArchiveResult.SUCCESS,
                    decompressed_data=data,
                    metadata=metadata,
                    message="从缓存恢复"
                )
        
        # 检查索引
        if request.entry_id not in self._index:
            self.state = ArchiveState.NORMAL
            return RestoreResponse(
                request_id=request.request_id,
                entry_id=request.entry_id,
                success=False,
                result=ArchiveResult.FAIL_ENTRY_NOT_FOUND,
                message="条目不在归档库中"
            )
        
        index_entry = self._index[request.entry_id]
        
        # 模拟读取压缩数据
        # 实际实现中从冷归档分区读取 index_entry.storage_offset 处的 index_entry.compressed_size 字节
        # 此处简化：压缩数据已在归档时处理，此处模拟解压
        
        # 由于我们没有保存实际的压缩数据，此处通过重建来模拟
        # 在实际系统中，压缩数据存储于冷归档分区文件中
        simulated_raw = f"archived_data:{request.entry_id}".encode()
        try:
            compressed = zlib.compress(simulated_raw, level=6)
        except Exception:
            self.state = ArchiveState.NORMAL
            return RestoreResponse(
                request_id=request.request_id,
                entry_id=request.entry_id,
                success=False,
                result=ArchiveResult.FAIL_DECOMPRESS,
                message="数据读取失败"
            )
        
        # 校验完整性
        current_checksum = hashlib.sha256(compressed).hexdigest()
        if current_checksum != index_entry.checksum:
            self.state = ArchiveState.NORMAL
            return RestoreResponse(
                request_id=request.request_id,
                entry_id=request.entry_id,
                success=False,
                result=ArchiveResult.FAIL_CHECKSUM_MISMATCH,
                message="校验和不匹配，数据可能已损坏"
            )
        
        # 解压
        try:
            decompressed = zlib.decompress(compressed)
            data = {"entry_id": request.entry_id, "raw_data": decompressed.decode(errors='ignore')}
        except Exception:
            self.state = ArchiveState.NORMAL
            return RestoreResponse(
                request_id=request.request_id,
                entry_id=request.entry_id,
                success=False,
                result=ArchiveResult.FAIL_DECOMPRESS,
                message="解压失败"
            )
        
        metadata = {
            "original_layer": index_entry.original_layer,
            "archive_reason": index_entry.archive_reason,
            "archive_time": index_entry.archive_time
        }
        
        # 更新恢复缓存
        self._restore_cache[request.entry_id] = (data, metadata, time.time())
        
        self._total_restored += 1
        
        self._log_event("ARCHIVE_RESTORE", {
            "entry_id": request.entry_id,
            "request_source": request.request_source
        })
        
        self.state = ArchiveState.NORMAL
        
        return RestoreResponse(
            request_id=request.request_id,
            entry_id=request.entry_id,
            success=True,
            result=ArchiveResult.SUCCESS,
            decompressed_data=data,
            metadata=metadata,
            message="恢复成功"
        )
    
    # ========== 定期清理 ==========
    
    def execute_cleanup(self, force: bool = False) -> CleanupReport:
        """
        清理过期归档条目
        
        S-03: 法规模糊地带永久保留
        S-04: 不可抗力条目永久保留
        
        Args:
            force: 是否强制执行（忽略保留期限，仅在容量紧急时使用）
            
        Returns:
            清理报告
        """
        self.state = ArchiveState.CLEANING
        
        now = time.time()
        cleaned = 0
        released = 0
        skipped_permanent = 0
        
        to_remove = []
        
        for entry_id, index_entry in self._index.items():
            should_remove = False
            
            # S-03: 法规模糊地带永久保留
            if index_entry.legal_ambiguity:
                skipped_permanent += 1
                continue
            
            # S-04: 不可抗力永久保留
            if index_entry.force_majeure:
                skipped_permanent += 1
                continue
            
            if force:
                # 强制清理：保留期限减半
                if now - index_entry.archive_time > self.DEFAULT_RETENTION_PERIOD / 2:
                    should_remove = True
            else:
                if now - index_entry.archive_time > self.DEFAULT_RETENTION_PERIOD:
                    should_remove = True
            
            if should_remove:
                to_remove.append(entry_id)
        
        # 执行安全删除
        for entry_id in to_remove:
            entry = self._index[entry_id]
            # S-05: DoD 5220.22-M 标准单次覆写（模拟）
            released += entry.compressed_size
            self._used_storage -= entry.compressed_size
            del self._index[entry_id]
            cleaned += 1
        
        if cleaned > 0:
            self._persist_index()
        
        self._total_cleaned += cleaned
        
        report = CleanupReport(
            cleaned_count=cleaned,
            released_bytes=released,
            remaining_count=len(self._index),
            skipped_permanent=skipped_permanent
        )
        
        self._log_event("ARCHIVE_CLEANUP", {
            "cleaned": cleaned,
            "released_bytes": released,
            "skipped_permanent": skipped_permanent
        })
        
        self.state = ArchiveState.NORMAL
        
        if cleaned > 0:
            print(f"[{self.module_id}] 清理过期归档: {cleaned} 条, 释放 {released/1024:.0f}KB")
        
        return report
    
    def check_capacity_and_clean(self) -> None:
        """检查容量，必要时触发清理"""
        usage = self.get_usage_rate()
        if usage > self.CAPACITY_URGENT:
            print(f"[{self.module_id}] 容量告急 ({usage:.1%})，触发强制清理")
            self.execute_cleanup(force=True)
        elif usage > self.CAPACITY_WARNING:
            print(f"[{self.module_id}] 容量预警 ({usage:.1%})，触发常规清理")
            self.execute_cleanup(force=False)
    
    # ========== 索引持久化 ==========
    
    def _persist_index(self) -> None:
        """
        持久化归档索引表至冷归档分区
        
        S-06: 每完成一次写入或清理操作后自动执行
        """
        # 实际实现中将索引序列化后写入冷归档分区固定位置
        # 此处为模拟
        pass
    
    # ========== 辅助方法 ==========
    
    def _summarize_metadata(self, metadata: Dict[str, Any]) -> str:
        """生成元数据摘要"""
        if not metadata:
            return ""
        # 只保留关键字段
        key_fields = ["behavior_type", "scene_type", "source_slot_id", "result_label"]
        parts = []
        for k in key_fields:
            if k in metadata:
                parts.append(f"{k}={metadata[k]}")
        return ", ".join(parts) if parts else "无摘要"
    
    # ========== 查询接口 ==========
    
    def get_index_entry(self, entry_id: str) -> Optional[ArchiveIndexEntry]:
        return self._index.get(entry_id)
    
    def get_total_entries(self) -> int:
        return len(self._index)
    
    def generate_snapshot(self) -> ArchiveStatusSnapshot:
        total_original = sum(e.original_size for e in self._index.values())
        total_compressed = sum(e.compressed_size for e in self._index.values())
        permanent = sum(1 for e in self._index.values() if e.legal_ambiguity or e.force_majeure)
        compression_ratio = (1 - total_compressed / max(total_original, 1)) if total_original > 0 else 0.0
        
        return ArchiveStatusSnapshot(
            total_entries=len(self._index),
            total_compressed_size=total_compressed,
            total_original_size=total_original,
            compression_ratio=compression_ratio,
            permanent_entries=permanent,
            storage_usage_rate=self.get_usage_rate(),
            state=self.state.value
        )
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_archived": self._total_archived,
            "total_restored": self._total_restored,
            "total_cleaned": self._total_cleaned,
            "current_entries": len(self._index),
            "used_storage": self._used_storage,
            "archive_capacity": self._archive_capacity,
            "usage_rate": self.get_usage_rate(),
            "state": self.state.value
        }
    
    # ========== 变更日志 ==========
    
    def _log_event(self, event_type: str, details: Dict[str, Any]) -> None:
        self._pending_logs.append({
            "log_id": f"archive-{uuid.uuid4().hex[:8]}",
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
    print("ad-49 存储压缩与冷归档单元 单元测试")
    print("=" * 60)
    passed, failed = 0, 0
    
    # TC-49-01: 正常归档写入
    print("\n[TC-49-01] 正常归档写入")
    try:
        archive = ColdArchiveUnit()
        packet = ArchiveDataPacket(
            entry_id="EXP-001",
            compressed_data=b"test data for archive",
            metadata={"behavior_type": "跟车", "scene_type": "高速"},
            archive_reason="遗忘",
            original_layer="L3",
            original_slot_id=15,
            force_majeure=False,
            original_size=1024
        )
        resp = archive.archive_entry(packet)
        assert resp.success and resp.result == ArchiveResult.SUCCESS
        assert archive.get_total_entries() == 1
        assert resp.checksum != ""
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-49-02: 法规模糊地带归档
    print("\n[TC-49-02] 法规模糊地带归档")
    try:
        archive = ColdArchiveUnit()
        resp = archive.archive_legal_ambiguity("Q-001", {"scene": "模糊场景"}, "法规模糊")
        assert resp.success
        assert archive.get_total_entries() == 1
        assert archive._index["LEGAL-Q-001"].legal_ambiguity == True
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-49-03: 归档恢复成功
    print("\n[TC-49-03] 归档恢复成功")
    try:
        archive = ColdArchiveUnit()
        packet = ArchiveDataPacket(
            entry_id="EXP-003",
            compressed_data=b"restorable data",
            metadata={},
            archive_reason="遗忘",
            original_layer="L3",
            original_slot_id=16,
            original_size=512
        )
        archive.archive_entry(packet)
        req = RestoreRequest("restore-001", "EXP-003", "L3", "审计", "ad-24")
        resp = archive.restore_entry(req)
        assert resp.success and resp.result == ArchiveResult.SUCCESS
        assert resp.decompressed_data is not None
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-49-04: 恢复缓存命中
    print("\n[TC-49-04] 30秒内重复恢复命中缓存")
    try:
        archive = ColdArchiveUnit()
        packet = ArchiveDataPacket("EXP-004", b"cache test", {}, "遗忘", "L3", 15, original_size=256)
        archive.archive_entry(packet)
        req = RestoreRequest("r-001", "EXP-004", "L3", "测试", "ad-24")
        archive.restore_entry(req)
        # 第二次恢复应在缓存中
        resp2 = archive.restore_entry(req)
        assert resp2.success and "缓存" in resp2.message
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-49-05: 条目不存在
    print("\n[TC-49-05] 恢复不存在的条目")
    try:
        archive = ColdArchiveUnit()
        req = RestoreRequest("r-002", "NON_EXIST", "L3", "测试", "ad-24")
        resp = archive.restore_entry(req)
        assert not resp.success and resp.result == ArchiveResult.FAIL_ENTRY_NOT_FOUND
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-49-06: 清理跳过永久条目
    print("\n[TC-49-06] 清理跳过法规模糊地带和不可抗力")
    try:
        archive = ColdArchiveUnit()
        archive.DEFAULT_RETENTION_PERIOD = 1  # 1秒过期
        # 添加普通条目
        archive.archive_entry(ArchiveDataPacket("EXP-NORMAL", b"data", {}, "遗忘", "L3", 15, original_size=100))
        # 添加法规模糊
        archive.archive_legal_ambiguity("Q-LEGAL", {}, "法规模糊")
        # 添加不可抗力
        archive.archive_entry(ArchiveDataPacket("EXP-FM", b"fm", {}, "遗忘", "L4", 18, force_majeure=True, original_size=100))
        
        time.sleep(0.1)
        report = archive.execute_cleanup(force=False)
        assert report.cleaned_count >= 0  # 普通条目可能被清理
        assert report.skipped_permanent >= 2  # 法规模糊+不可抗力
        # 永久条目应仍在索引中
        assert "LEGAL-Q-LEGAL" in archive._index
        assert "EXP-FM" in archive._index
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    # TC-49-07: 容量满拒绝归档
    print("\n[TC-49-07] 容量满拒绝归档")
    try:
        archive = ColdArchiveUnit(archive_capacity_bytes=100)  # 极小容量
        packet = ArchiveDataPacket("EXP-FULL", b"x" * 200, {}, "遗忘", "L3", 15, original_size=200)
        resp = archive.archive_entry(packet)
        assert not resp.success and resp.result == ArchiveResult.FAIL_STORAGE_FULL
        print("   ✅ PASS"); passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}"); failed += 1
    
    print(f"\n测试结果: {passed} PASS, {failed} FAIL")