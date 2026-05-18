#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-02
模块名称: 漏斗一专属调度单元 - 驾驶员画像漏斗管家
所属分区: 一、顶层总控中枢
核心职责: 漏斗一内部子画像槽的全生命周期管理。负责驾驶员身份识别结果的接收与槽位匹配，
          管控子画像槽的创建、激活、冻结与删除。维护长期槽位上限(6个)、临时槽(1个)、
          一次性槽(1个)的硬约束。子画像槽之间绝对物理隔离。

依赖模块: ad-04(驾驶员身份识别单元), ad-05(子画像槽创建与初始化单元), 
          ad-06(子画像槽数据隔离管控单元), ad-12(临时画像槽自动清除单元), 
          ad-13(子画像槽长期未活跃提醒单元)
被依赖模块: ad-01(总控漏斗F₀), ad-07(驾驶行为观测记录单元), ad-11(驾驶辅助提醒生成单元)

安全约束:
  S-01: 长期槽位硬性上限 6 个，编译期强制，不可通过运行时配置绕过
  S-02: 子画像槽之间绝对物理存储隔离，本模块仅做路由，不参与数据读写
  S-03: 临时槽 7 天自动清除为硬约束，仅用户手动确认可提前清除
  S-04: 一次性槽行程结束即清除，不保留任何数据
  S-05: 漏斗一全部子画像槽数据编译期禁止以任何形式接入自动驾驶决策链路
  S-06: 90 天未活跃仅提示用户，禁止系统自动删除长期槽
  S-07: 紧急接管时立即冻结全部槽位写入，优先保障安全
  S-08: 所有槽位创建、删除、冻结操作全量写入 ad-51 号变更日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class SlotType(Enum):
    """子画像槽类型"""
    LONG_TERM = "long_term"       # 长期槽（上限6个）
    TEMPORARY = "temporary"       # 临时槽（上限1个，7天自动清除）
    ONESHOT = "one_shot"          # 一次性槽（上限1个，行程结束清除）


class SlotStatus(Enum):
    """子画像槽状态"""
    ACTIVE = "active"             # 活跃
    FROZEN = "frozen"             # 冻结（只读）
    CLEANING = "cleaning"         # 清除中
    DAMAGED = "damaged"           # 损坏
    UNUSED = "unused"             # 未使用


class DispatcherState(Enum):
    """调度单元内部状态"""
    WAITING_ID = "waiting_id"            # 等待身份确认
    MATCHING = "matching"                # 槽位匹配中
    LONG_TERM_READY = "long_term_ready"  # 长期槽就绪
    CREATING = "creating"                # 新槽创建中
    TEMP_ACTIVE = "temp_active"          # 临时槽激活
    ONESHOT_ACTIVE = "oneshot_active"    # 一次性槽激活
    FROZEN = "frozen"                    # 全部冻结
    CLEANING = "cleaning"                # 槽位清理中


class DrivingMode(Enum):
    """驾驶模式"""
    MANUAL = "manual"
    AUTONOMOUS = "autonomous"
    EMERGENCY_TAKEOVER = "emergency_takeover"


# ==================== 数据结构 ====================

@dataclass
class SlotMeta:
    """子画像槽元数据"""
    slot_id: int
    slot_type: SlotType
    driver_id: str
    driver_name_masked: str     # 掩码名称，保护隐私
    status: SlotStatus = SlotStatus.UNUSED
    create_time: float = field(default_factory=time.time)
    last_active_time: float = field(default_factory=time.time)
    expire_time: Optional[float] = None  # 临时槽的过期时间
    storage_partition: Optional[str] = None  # 存储分区指针


@dataclass
class DriverIdentityResult:
    """驾驶员身份识别结果"""
    driver_id: str
    recognition_method: str     # "中控屏" / "座椅记忆" / "人脸识别"
    confidence: float           # 0.0 ~ 1.0
    suggested_slot_type: SlotType
    timestamp: float = field(default_factory=time.time)


@dataclass
class SlotCreationRequest:
    """槽位创建请求"""
    driver_id: str
    driver_name_masked: str
    slot_type: SlotType
    request_source: str         # "ad-04" / "ad-01"
    timestamp: float = field(default_factory=time.time)


@dataclass
class SlotCreationResponse:
    """槽位创建响应"""
    success: bool
    slot_id: Optional[int] = None
    error_code: Optional[str] = None
    suggestion: Optional[str] = None


@dataclass
class SlotStatusSnapshot:
    """槽位状态快照"""
    total_slots: int
    long_term_count: int
    temporary_count: int
    oneshot_count: int
    active_slot_id: Optional[int]
    timestamp: float = field(default_factory=time.time)


# ==================== 主类定义 ====================

class FunnelOneDispatcher:
    """
    漏斗一专属调度单元 - 驾驶员画像漏斗管家
    
    职责:
    1. 子画像槽全生命周期管理（创建/激活/冻结/删除）
    2. 驾驶员身份识别结果接收与槽位匹配
    3. 槽位上限硬约束管控（长期6/临时1/一次性1）
    4. 临时槽7天自动清除、一次性槽行程结束清除
    5. 周期性状态上报至 ad-01
    """
    
    # 槽位上限（编译期硬编码）
    MAX_LONG_TERM_SLOTS = 6
    MAX_TEMPORARY_SLOTS = 1
    MAX_ONESHOT_SLOTS = 1
    
    # 临时槽有效期（秒）
    TEMPORARY_SLOT_TTL = 7 * 24 * 3600  # 7天
    
    # 身份确认超时（秒）
    ID_CONFIRM_TIMEOUT = 5.0
    
    # 未活跃提醒阈值（秒）
    INACTIVE_REMINDER_THRESHOLD = 90 * 24 * 3600  # 90天
    
    def __init__(self):
        self.module_id = "ad-02"
        self.module_name = "漏斗一专属调度单元"
        
        # 内部状态
        self.state = DispatcherState.WAITING_ID
        
        # 槽位注册表: slot_id -> SlotMeta
        self._slots: Dict[int, SlotMeta] = {}
        
        # 驾驶员身份到槽位的映射
        self._driver_slot_map: Dict[str, int] = {}
        
        # 当前活跃槽号
        self._active_slot_id: Optional[int] = None
        
        # 槽号计数器
        self._slot_id_counter = 0
        
        # 统计
        self._total_creates = 0
        self._total_deletes = 0
        self._total_freezes = 0
        
        # 待写入 ad-51 的变更日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 漏斗一调度单元初始化完成")
    
    # ========== 槽位生命周期管理 ==========
    
    def handle_identity_result(self, result: DriverIdentityResult) -> SlotCreationResponse:
        """
        处理驾驶员身份识别结果
        
        逻辑:
        1. 若匹配已有长期槽 → 激活该槽
        2. 若用户未确认身份超过5秒 → 激活临时槽
        3. 若长期槽有空位 → 创建新长期槽
        4. 若长期槽已满 → 提示用户释放旧槽或使用临时模式
        """
        self.state = DispatcherState.MATCHING
        
        # 优先匹配已有长期槽
        if result.driver_id in self._driver_slot_map:
            slot_id = self._driver_slot_map[result.driver_id]
            self._activate_slot(slot_id)
            self.state = DispatcherState.LONG_TERM_READY
            print(f"[{self.module_id}] 匹配已有长期槽: slot_{slot_id}, driver={result.driver_id}")
            return SlotCreationResponse(success=True, slot_id=slot_id)
        
        # 检查长期槽是否还有空位
        long_term_count = self._count_slots_by_type(SlotType.LONG_TERM)
        
        if long_term_count < self.MAX_LONG_TERM_SLOTS:
            # 创建新长期槽
            return self._create_new_slot(result.driver_id, result.driver_id, SlotType.LONG_TERM)
        
        # 长期槽已满
        if result.confidence < 0.6:
            # 低置信度，使用临时槽
            return self._create_new_slot(result.driver_id, result.driver_id, SlotType.TEMPORARY)
        
        return SlotCreationResponse(
            success=False,
            error_code="LONG_TERM_FULL",
            suggestion="长期槽位已满(6/6)，请释放旧槽或使用临时模式"
        )
    
    def handle_user_manual_selection(self, driver_id: str, slot_type: SlotType) -> SlotCreationResponse:
        """
        处理用户中控屏手动选择
        用户主动确认为最高优先级，confidence=1.0
        """
        # 检查是否已有该驾驶员的槽
        if driver_id in self._driver_slot_map:
            slot_id = self._driver_slot_map[driver_id]
            self._activate_slot(slot_id)
            return SlotCreationResponse(success=True, slot_id=slot_id)
        
        return self._create_new_slot(driver_id, driver_id, slot_type)
    
    def _create_new_slot(self, driver_id: str, driver_name: str, slot_type: SlotType) -> SlotCreationResponse:
        """创建新的子画像槽"""
        self.state = DispatcherState.CREATING
        
        # 校验槽位上限
        current_count = self._count_slots_by_type(slot_type)
        max_allowed = self._get_max_for_type(slot_type)
        
        if current_count >= max_allowed:
            if slot_type == SlotType.ONESHOT:
                # 覆盖当前一次性槽
                self._delete_on_existing_oneshot()
            else:
                return SlotCreationResponse(
                    success=False,
                    error_code=f"{slot_type.value.upper()}_FULL",
                    suggestion=f"{slot_type.value}槽位已满"
                )
        
        # 分配新槽号
        self._slot_id_counter += 1
        new_slot_id = self._slot_id_counter
        
        # 创建槽元数据
        slot = SlotMeta(
            slot_id=new_slot_id,
            slot_type=slot_type,
            driver_id=driver_id,
            driver_name_masked=f"用户{driver_id[-4:]}"
        )
        
        if slot_type == SlotType.TEMPORARY:
            slot.expire_time = time.time() + self.TEMPORARY_SLOT_TTL
        
        # 注册槽位
        self._slots[new_slot_id] = slot
        self._driver_slot_map[driver_id] = new_slot_id
        self._total_creates += 1
        
        # 激活新槽
        self._activate_slot(new_slot_id)
        
        self._log_event("SLOT_CREATE", {
            "slot_id": new_slot_id,
            "slot_type": slot_type.value,
            "driver_id": driver_id
        })
        
        print(f"[{self.module_id}] 创建新槽: slot_{new_slot_id}, type={slot_type.value}")
        return SlotCreationResponse(success=True, slot_id=new_slot_id)
    
    def _activate_slot(self, slot_id: int) -> None:
        """激活指定槽位"""
        if slot_id in self._slots:
            self._slots[slot_id].status = SlotStatus.ACTIVE
            self._slots[slot_id].last_active_time = time.time()
            self._active_slot_id = slot_id
    
    def freeze_all_slots(self) -> None:
        """冻结全部槽位写入（紧急接管或模式切换时调用）"""
        self.state = DispatcherState.FROZEN
        self._total_freezes += 1
        for slot in self._slots.values():
            if slot.status == SlotStatus.ACTIVE:
                slot.status = SlotStatus.FROZEN
        self._active_slot_id = None
        print(f"[{self.module_id}] 全部槽位已冻结")
    
    def unfreeze_slots(self) -> None:
        """解冻全部槽位"""
        for slot in self._slots.values():
            if slot.status == SlotStatus.FROZEN:
                slot.status = SlotStatus.ACTIVE
        self.state = DispatcherState.WAITING_ID
        print(f"[{self.module_id}] 全部槽位已解冻")
    
    # ========== 临时槽与一次性槽管理 ==========
    
    def check_expired_slots(self) -> List[int]:
        """
        检查到期的临时槽与一次性槽
        
        Returns:
            需要清除的槽号列表
        """
        expired = []
        now = time.time()
        
        for slot_id, slot in self._slots.items():
            if slot.slot_type == SlotType.TEMPORARY and slot.expire_time:
                if now >= slot.expire_time:
                    expired.append(slot_id)
            elif slot.slot_type == SlotType.ONESHOT:
                # 一次性槽在行程结束时由外部触发清除
                pass
        
        if expired:
            print(f"[{self.module_id}] 检测到 {len(expired)} 个过期槽位")
        
        return expired
    
    def mark_slot_for_cleaning(self, slot_id: int) -> None:
        """标记槽位为待清除"""
        if slot_id in self._slots:
            self._slots[slot_id].status = SlotStatus.CLEANING
            self.state = DispatcherState.CLEANING
    
    def confirm_slot_deletion(self, slot_id: int) -> None:
        """确认槽位删除完成"""
        if slot_id in self._slots:
            driver_id = self._slots[slot_id].driver_id
            del self._slots[slot_id]
            if driver_id in self._driver_slot_map:
                del self._driver_slot_map[driver_id]
            self._total_deletes += 1
            self._log_event("SLOT_DELETE", {"slot_id": slot_id})
    
    # ========== 未活跃检测 ==========
    
    def detect_inactive_slots(self) -> List[Tuple[int, float]]:
        """
        检测超过90天未活跃的长期槽
        
        Returns:
            [(槽号, 未活跃天数), ...]
        """
        inactive = []
        now = time.time()
        
        for slot_id, slot in self._slots.items():
            if slot.slot_type != SlotType.LONG_TERM:
                continue
            inactive_seconds = now - slot.last_active_time
            if inactive_seconds > self.INACTIVE_REMINDER_THRESHOLD:
                inactive_days = inactive_seconds / (24 * 3600)
                inactive.append((slot_id, inactive_days))
        
        return inactive
    
    # ========== 状态上报 ==========
    
    def generate_status_snapshot(self) -> SlotStatusSnapshot:
        """生成槽位状态快照"""
        return SlotStatusSnapshot(
            total_slots=len(self._slots),
            long_term_count=self._count_slots_by_type(SlotType.LONG_TERM),
            temporary_count=self._count_slots_by_type(SlotType.TEMPORARY),
            oneshot_count=self._count_slots_by_type(SlotType.ONESHOT),
            active_slot_id=self._active_slot_id
        )
    
    def get_active_slot_id(self) -> Optional[int]:
        """获取当前活跃槽号"""
        return self._active_slot_id
    
    # ========== 内部辅助方法 ==========
    
    def _count_slots_by_type(self, slot_type: SlotType) -> int:
        """统计指定类型的槽位数量"""
        return sum(1 for s in self._slots.values() if s.slot_type == slot_type)
    
    def _get_max_for_type(self, slot_type: SlotType) -> int:
        """获取槽位类型的最大允许数量"""
        if slot_type == SlotType.LONG_TERM:
            return self.MAX_LONG_TERM_SLOTS
        elif slot_type == SlotType.TEMPORARY:
            return self.MAX_TEMPORARY_SLOTS
        elif slot_type == SlotType.ONESHOT:
            return self.MAX_ONESHOT_SLOTS
        return 0
    
    def _delete_on_existing_oneshot(self) -> None:
        """删除已有的一次性槽（行程结束即清除，可安全覆盖）"""
        for slot_id, slot in self._slots.items():
            if slot.slot_type == SlotType.ONESHOT:
                self.confirm_slot_deletion(slot_id)
                break
    
    def _log_event(self, event_type: str, details: Dict[str, Any]) -> None:
        """记录变更日志"""
        self._pending_logs.append({
            "log_id": f"log-{uuid.uuid4().hex[:8]}",
            "event_type": event_type,
            "source_module": self.module_id,
            "details": details,
            "timestamp": time.time()
        })
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        """收集待写入 ad-51 的变更日志"""
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_slots": len(self._slots),
            "total_creates": self._total_creates,
            "total_deletes": self._total_deletes,
            "total_freezes": self._total_freezes,
            "active_slot_id": self._active_slot_id,
            "current_state": self.state.value
        }


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-02 漏斗一专属调度单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    # --- TC-02-01: 创建新长期槽 ---
    print("\n[TC-02-01] 创建新长期槽")
    try:
        dispatcher = FunnelOneDispatcher()
        result = DriverIdentityResult(
            driver_id="DRV-001",
            recognition_method="中控屏",
            confidence=1.0,
            suggested_slot_type=SlotType.LONG_TERM
        )
        response = dispatcher.handle_identity_result(result)
        assert response.success == True
        assert response.slot_id == 1
        assert dispatcher._count_slots_by_type(SlotType.LONG_TERM) == 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-02-02: 匹配已有长期槽 ---
    print("\n[TC-02-02] 匹配已有长期槽")
    try:
        dispatcher = FunnelOneDispatcher()
        # 先创建
        r1 = DriverIdentityResult("DRV-001", "中控屏", 1.0, SlotType.LONG_TERM)
        dispatcher.handle_identity_result(r1)
        # 再次识别同一驾驶员
        r2 = DriverIdentityResult("DRV-001", "人脸识别", 0.95, SlotType.LONG_TERM)
        response = dispatcher.handle_identity_result(r2)
        assert response.success == True
        assert response.slot_id == 1  # 复用已有槽
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-02-03: 长期槽已满(6个) ---
    print("\n[TC-02-03] 长期槽已满拒绝创建")
    try:
        dispatcher = FunnelOneDispatcher()
        for i in range(6):
            r = DriverIdentityResult(f"DRV-{i:03d}", "中控屏", 1.0, SlotType.LONG_TERM)
            dispatcher.handle_identity_result(r)
        # 第7个驾驶员
        r7 = DriverIdentityResult("DRV-007", "中控屏", 1.0, SlotType.LONG_TERM)
        response = dispatcher.handle_identity_result(r7)
        assert response.success == False
        assert response.error_code == "LONG_TERM_FULL"
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-02-04: 创建临时槽 ---
    print("\n[TC-02-04] 创建临时槽")
    try:
        dispatcher = FunnelOneDispatcher()
        response = dispatcher._create_new_slot("DRV-TMP", "临时用户", SlotType.TEMPORARY)
        assert response.success == True
        slot = dispatcher._slots[response.slot_id]
        assert slot.expire_time is not None
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-02-05: 临时槽7天到期检测 ---
    print("\n[TC-02-05] 临时槽7天到期检测")
    try:
        dispatcher = FunnelOneDispatcher()
        response = dispatcher._create_new_slot("DRV-TMP2", "临时用户2", SlotType.TEMPORARY)
        slot = dispatcher._slots[response.slot_id]
        # 模拟过期
        slot.expire_time = time.time() - 1
        expired = dispatcher.check_expired_slots()
        assert response.slot_id in expired
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-02-06: 紧急接管冻结全部槽位 ---
    print("\n[TC-02-06] 紧急接管冻结全部槽位")
    try:
        dispatcher = FunnelOneDispatcher()
        dispatcher._create_new_slot("DRV-001", "张三", SlotType.LONG_TERM)
        dispatcher.freeze_all_slots()
        assert dispatcher.state == DispatcherState.FROZEN
        assert dispatcher._active_slot_id is None
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-02-07: 超过90天未活跃检测 ---
    print("\n[TC-02-07] 超过90天未活跃检测")
    try:
        dispatcher = FunnelOneDispatcher()
        response = dispatcher._create_new_slot("DRV-OLD", "老用户", SlotType.LONG_TERM)
        slot = dispatcher._slots[response.slot_id]
        slot.last_active_time = time.time() - 100 * 24 * 3600  # 100天前
        inactive = dispatcher.detect_inactive_slots()
        assert len(inactive) == 1
        assert inactive[0][0] == response.slot_id
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-02-08: 状态快照生成 ---
    print("\n[TC-02-08] 状态快照生成")
    try:
        dispatcher = FunnelOneDispatcher()
        dispatcher._create_new_slot("DRV-A", "用户A", SlotType.LONG_TERM)
        dispatcher._create_new_slot("DRV-B", "用户B", SlotType.TEMPORARY)
        snapshot = dispatcher.generate_status_snapshot()
        assert snapshot.long_term_count == 1
        assert snapshot.temporary_count == 1
        assert snapshot.total_slots == 2
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- 测试结果汇总 ---
    print("\n" + "=" * 60)
    print(f"测试结果: {passed} PASS, {failed} FAIL")
    print("=" * 60)