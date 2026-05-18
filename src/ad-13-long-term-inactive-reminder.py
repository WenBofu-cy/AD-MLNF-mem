#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-13
模块名称: 子画像槽长期未活跃提醒单元
所属分区: 二、漏斗一：驾驶员画像漏斗
核心职责: 检测超过 90 天未活跃的长期槽，向用户发出提醒，仅执行用户手动确认的删除。
          绝对禁止自动删除长期槽。定期扫描各长期槽的最后活跃时间戳，生成未活跃槽
          清单并推送至中控屏。

依赖模块: ad-10(行为累积统计单元，提供最后活跃时间戳更新),
          ad-02(漏斗一专属调度单元，获取长期槽列表与用户反馈)
被依赖模块: 中控屏交互模块(硬件，接收提醒显示), ad-02(接收用户确认删除指令)

安全约束:
  S-01: 绝对禁止自动删除任何长期槽。删除的唯一触发条件是用户手动在中控屏确认
  S-02: 用户确认删除后，本模块仅转发建议至 ad-02，最终执行由 ad-02 调度 ad-12 完成
  S-03: 提醒内容不得包含驾驶员的真实姓名或身份 ID 明文，仅以掩码名称或槽编号显示
  S-04: 提醒频率最低为 30 天一次，避免频繁骚扰用户
  S-05: 所有提醒及用户确认删除操作均全量写入 ad-51 不可变日志
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class ReminderLevel(Enum):
    """提醒等级"""
    SUGGESTION = "建议删除"
    STRONG_SUGGESTION = "强烈建议删除"


class ReminderState(Enum):
    """提醒单元内部状态"""
    NORMAL = "normal"
    REMINDER_COOLDOWN = "reminder_cooldown"
    AWAIT_DELETION = "await_deletion"
    PAUSED = "paused"


# ==================== 数据结构 ====================

@dataclass
class SlotActivityInfo:
    """槽位活跃信息"""
    slot_id: int
    slot_type: str
    driver_name_masked: str       # 掩码名称
    create_time: float
    last_active_time: float
    inactive_days: int = 0


@dataclass
class InactiveReminderRequest:
    """未活跃槽提醒请求"""
    request_id: str
    inactive_slots: List[SlotActivityInfo]
    reminder_level: ReminderLevel
    suggested_action: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class UserDeletionConfirmation:
    """用户确认删除信号"""
    target_slot_id: int
    confirmed: bool
    confirmation_timestamp: float = field(default_factory=time.time)


@dataclass
class ReminderRecord:
    """提醒记录"""
    slot_id: int
    last_reminder_time: float
    reminder_count: int
    user_responded: bool = False


# ==================== 主类定义 ====================

class LongTermInactiveReminder:
    """
    子画像槽长期未活跃提醒单元
    
    职责:
    1. 跟踪各长期槽的最后活跃时间
    2. 定期扫描超过 90 天未活跃的长期槽
    3. 生成提醒推送至中控屏
    4. 处理用户确认删除
    5. 管理提醒冷却期（最低 30 天一次）
    """
    
    # 未活跃阈值（秒）
    INACTIVE_THRESHOLD = 90 * 24 * 3600  # 90天
    
    # 提醒冷却期（秒）
    REMINDER_COOLDOWN = 30 * 24 * 3600   # 30天
    
    # 扫描间隔（秒）
    SCAN_INTERVAL = 24 * 3600            # 24小时
    
    # 提醒等级升级阈值（秒）
    ESCALATION_THRESHOLD = 180 * 24 * 3600  # 180天，升级为强烈建议
    
    def __init__(self):
        self.module_id = "ad-13"
        self.module_name = "子画像槽长期未活跃提醒单元"
        
        # 内部状态
        self.state = ReminderState.NORMAL
        
        # 各槽活跃时间记录: slot_id -> last_active_time
        self._slot_activity: Dict[int, float] = {}
        
        # 各槽创建时间记录: slot_id -> create_time
        self._slot_create_times: Dict[int, float] = {}
        
        # 提醒记录: slot_id -> ReminderRecord
        self._reminder_records: Dict[int, ReminderRecord] = {}
        
        # 上次扫描时间
        self._last_scan_time: float = 0.0
        
        # 首次扫描标记
        self._first_scan_done: bool = False
        
        # 统计
        self._total_scans = 0
        self._total_reminders = 0
        self._total_user_deletions = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 子画像槽长期未活跃提醒单元初始化完成")
    
    # ========== 状态管理 ==========
    
    def update_active_time(self, slot_id: int, active_time: float) -> None:
        """更新槽位最后活跃时间（接收自 ad-10）"""
        self._slot_activity[slot_id] = active_time
        
        # 如果该槽曾被提醒但重新活跃了，清除提醒记录
        if slot_id in self._reminder_records:
            record = self._reminder_records[slot_id]
            if record.last_reminder_time > active_time:
                # 之前提醒过，但用户重新活跃了
                record.user_responded = True
                print(f"[{self.module_id}] slot_{slot_id} 已重新活跃，清除提醒状态")
    
    def register_slot(self, slot_id: int, create_time: float, driver_name_masked: str) -> None:
        """注册长期槽信息"""
        self._slot_activity[slot_id] = create_time
        self._slot_create_times[slot_id] = create_time
        print(f"[{self.module_id}] 注册长期槽: slot_{slot_id}")
    
    def unregister_slot(self, slot_id: int) -> None:
        """注销槽位（槽被删除后调用）"""
        self._slot_activity.pop(slot_id, None)
        self._slot_create_times.pop(slot_id, None)
        self._reminder_records.pop(slot_id, None)
    
    def pause(self) -> None:
        """暂停服务"""
        self.state = ReminderState.PAUSED
        print(f"[{self.module_id}] 提醒服务已暂停")
    
    def resume(self) -> None:
        """恢复服务"""
        self.state = ReminderState.NORMAL
        # 恢复后立即执行一次扫描
        self._first_scan_done = False
        print(f"[{self.module_id}] 提醒服务已恢复")
    
    # ========== 未活跃检测 ==========
    
    def scan_inactive_slots(self, force: bool = False) -> List[SlotActivityInfo]:
        """
        扫描超过 90 天未活跃的长期槽
        
        Args:
            force: 是否强制扫描（忽略冷却期）
            
        Returns:
            未活跃槽位信息列表
        """
        now = time.time()
        
        # 检查扫描间隔
        if not force and self._first_scan_done:
            if now - self._last_scan_time < self.SCAN_INTERVAL:
                return []
        
        self._last_scan_time = now
        self._first_scan_done = True
        self._total_scans += 1
        
        inactive_slots = []
        
        for slot_id, last_active in self._slot_activity.items():
            inactive_seconds = now - last_active
            inactive_days = int(inactive_seconds / (24 * 3600))
            
            if inactive_seconds > self.INACTIVE_THRESHOLD:
                # 检查冷却期
                if slot_id in self._reminder_records:
                    record = self._reminder_records[slot_id]
                    if record.user_responded:
                        continue
                    if now - record.last_reminder_time < self.REMINDER_COOLDOWN:
                        continue
                
                inactive_slots.append(SlotActivityInfo(
                    slot_id=slot_id,
                    slot_type="long_term",
                    driver_name_masked=f"用户{slot_id}",
                    create_time=self._slot_create_times.get(slot_id, now),
                    last_active_time=last_active,
                    inactive_days=inactive_days
                ))
        
        if inactive_slots:
            print(f"[{self.module_id}] 扫描发现 {len(inactive_slots)} 个超过90天未活跃的长期槽")
        
        return inactive_slots
    
    # ========== 提醒生成 ==========
    
    def generate_reminder(self, inactive_slots: List[SlotActivityInfo]) -> Optional[InactiveReminderRequest]:
        """
        生成未活跃槽提醒
        
        Returns:
            提醒请求，或 None（无需提醒）
        """
        if not inactive_slots:
            return None
        
        self.state = ReminderState.REMINDER_COOLDOWN
        self._total_reminders += 1
        
        # 确定提醒等级
        max_inactive_days = max(s.inactive_days for s in inactive_slots)
        if max_inactive_days > 180:
            level = ReminderLevel.STRONG_SUGGESTION
        else:
            level = ReminderLevel.SUGGESTION
        
        # 更新提醒记录
        now = time.time()
        for slot in inactive_slots:
            if slot.slot_id in self._reminder_records:
                self._reminder_records[slot.slot_id].last_reminder_time = now
                self._reminder_records[slot.slot_id].reminder_count += 1
            else:
                self._reminder_records[slot.slot_id] = ReminderRecord(
                    slot_id=slot.slot_id,
                    last_reminder_time=now,
                    reminder_count=1
                )
        
        request = InactiveReminderRequest(
            request_id=f"reminder-{uuid.uuid4().hex[:8]}",
            inactive_slots=inactive_slots,
            reminder_level=level,
            suggested_action="长期未使用，建议删除以释放空间"
        )
        
        print(f"[{self.module_id}] 生成提醒: {len(inactive_slots)} 个槽, 等级={level.value}")
        self._log_event("REMINDER_GENERATED", {
            "slot_count": len(inactive_slots),
            "level": level.value
        })
        
        self.state = ReminderState.NORMAL
        return request
    
    # ========== 用户确认处理 ==========
    
    def handle_user_confirmation(self, confirmation: UserDeletionConfirmation,
                                 current_inactive_days: int) -> bool:
        """
        处理用户手动确认删除
        
        安全约束: 再次确认该槽确实超过 90 天未活跃
        
        Args:
            confirmation: 用户确认信号
            current_inactive_days: 当前未活跃天数
            
        Returns:
            是否应执行删除
        """
        if not confirmation.confirmed:
            return False
        
        # 二次确认未活跃天数（防止用户误操作或界面延迟）
        if current_inactive_days < 90:
            print(f"[{self.module_id}] slot_{confirmation.target_slot_id} 未活跃{current_inactive_days}天，"
                  f"不足90天，拒绝删除")
            return False
        
        self.state = ReminderState.AWAIT_DELETION
        self._total_user_deletions += 1
        
        # 清除提醒记录
        if confirmation.target_slot_id in self._reminder_records:
            del self._reminder_records[confirmation.target_slot_id]
        
        self._log_event("USER_CONFIRMED_DELETION", {
            "slot_id": confirmation.target_slot_id,
            "inactive_days": current_inactive_days
        })
        
        print(f"[{self.module_id}] 用户确认删除 slot_{confirmation.target_slot_id}, "
              f"未活跃{current_inactive_days}天")
        
        self.state = ReminderState.NORMAL
        return True
    
    def cancel_deletion(self, slot_id: int) -> None:
        """用户取消删除"""
        if self.state == ReminderState.AWAIT_DELETION:
            self.state = ReminderState.NORMAL
            self._total_user_deletions -= 1
            print(f"[{self.module_id}] 用户取消删除 slot_{slot_id}")
    
    # ========== 查询接口 ==========
    
    def get_inactive_days(self, slot_id: int) -> int:
        """获取指定槽位的未活跃天数"""
        if slot_id not in self._slot_activity:
            return 0
        last_active = self._slot_activity[slot_id]
        return int((time.time() - last_active) / (24 * 3600))
    
    def get_state(self) -> ReminderState:
        return self.state
    
    def is_in_cooldown(self, slot_id: int) -> bool:
        """检查指定槽位是否在提醒冷却期"""
        if slot_id not in self._reminder_records:
            return False
        record = self._reminder_records[slot_id]
        return time.time() - record.last_reminder_time < self.REMINDER_COOLDOWN
    
    # ========== 变更日志 ==========
    
    def _log_event(self, event_type: str, details: Dict[str, Any]) -> None:
        """记录事件日志"""
        self._pending_logs.append({
            "log_id": f"log-{uuid.uuid4().hex[:8]}",
            "event_type": event_type,
            "source_module": self.module_id,
            "details": details,
            "timestamp": time.time()
        })
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_scans": self._total_scans,
            "total_reminders": self._total_reminders,
            "total_user_deletions": self._total_user_deletions,
            "tracked_slots": len(self._slot_activity),
            "reminder_records": len(self._reminder_records),
            "state": self.state.value
        }


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-13 子画像槽长期未活跃提醒单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    # --- TC-13-01: 超过90天未活跃生成提醒 ---
    print("\n[TC-13-01] 超过90天未活跃生成提醒")
    try:
        reminder = LongTermInactiveReminder()
        old_time = time.time() - 95 * 24 * 3600
        reminder.register_slot(1, old_time, "用户1")
        reminder._slot_activity[1] = old_time  # 95天前活跃
        
        inactive = reminder.scan_inactive_slots(force=True)
        assert len(inactive) == 1
        assert inactive[0].inactive_days >= 90
        
        req = reminder.generate_reminder(inactive)
        assert req is not None
        assert req.reminder_level == ReminderLevel.SUGGESTION
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-13-02: 不足90天不提醒 ---
    print("\n[TC-13-02] 不足90天不提醒")
    try:
        reminder = LongTermInactiveReminder()
        recent_time = time.time() - 30 * 24 * 3600
        reminder.register_slot(2, recent_time, "用户2")
        reminder._slot_activity[2] = recent_time
        
        inactive = reminder.scan_inactive_slots(force=True)
        assert len(inactive) == 0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-13-03: 冷却期内不重复提醒 ---
    print("\n[TC-13-03] 冷却期内不重复提醒")
    try:
        reminder = LongTermInactiveReminder()
        old_time = time.time() - 100 * 24 * 3600
        reminder.register_slot(3, old_time, "用户3")
        reminder._slot_activity[3] = old_time
        
        # 第一次扫描，生成提醒
        inactive1 = reminder.scan_inactive_slots(force=True)
        reminder.generate_reminder(inactive1)
        
        # 立即第二次扫描，应在冷却期
        inactive2 = reminder.scan_inactive_slots(force=True)
        assert len(inactive2) == 0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-13-04: 用户确认删除 ---
    print("\n[TC-13-04] 用户确认删除")
    try:
        reminder = LongTermInactiveReminder()
        old_time = time.time() - 95 * 24 * 3600
        reminder.register_slot(4, old_time, "用户4")
        
        confirmation = UserDeletionConfirmation(
            target_slot_id=4,
            confirmed=True
        )
        result = reminder.handle_user_confirmation(confirmation, 95)
        assert result == True
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-13-05: 不足90天拒绝删除 ---
    print("\n[TC-13-05] 不足90天拒绝删除")
    try:
        reminder = LongTermInactiveReminder()
        reminder.register_slot(5, time.time(), "用户5")
        
        confirmation = UserDeletionConfirmation(
            target_slot_id=5,
            confirmed=True
        )
        result = reminder.handle_user_confirmation(confirmation, 60)
        assert result == False
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-13-06: 超过180天升级为强烈建议 ---
    print("\n[TC-13-06] 超过180天升级为强烈建议")
    try:
        reminder = LongTermInactiveReminder()
        old_time = time.time() - 200 * 24 * 3600
        reminder.register_slot(6, old_time, "用户6")
        reminder._slot_activity[6] = old_time
        
        inactive = reminder.scan_inactive_slots(force=True)
        req = reminder.generate_reminder(inactive)
        assert req.reminder_level == ReminderLevel.STRONG_SUGGESTION
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-13-07: 重新活跃后清除提醒状态 ---
    print("\n[TC-13-07] 重新活跃后清除提醒状态")
    try:
        reminder = LongTermInactiveReminder()
        old_time = time.time() - 100 * 24 * 3600
        reminder.register_slot(7, old_time, "用户7")
        reminder._slot_activity[7] = old_time
        
        # 先触发提醒
        inactive = reminder.scan_inactive_slots(force=True)
        reminder.generate_reminder(inactive)
        assert reminder._reminder_records[7].user_responded == False
        
        # 用户重新活跃
        reminder.update_active_time(7, time.time())
        assert reminder._reminder_records[7].user_responded == True
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-13-08: 暂停后恢复立即扫描 ---
    print("\n[TC-13-08] 暂停后恢复立即扫描")
    try:
        reminder = LongTermInactiveReminder()
        old_time = time.time() - 100 * 24 * 3600
        reminder.register_slot(8, old_time, "用户8")
        reminder._slot_activity[8] = old_time
        
        reminder.pause()
        assert reminder.state == ReminderState.PAUSED
        reminder.resume()
        assert reminder.state == ReminderState.NORMAL
        # 恢复后首次扫描应强制执行
        assert reminder._first_scan_done == False
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-13-09: 从未活跃的槽位使用创建时间计算 ---
    print("\n[TC-13-09] 从未活跃的槽位使用创建时间")
    try:
        reminder = LongTermInactiveReminder()
        create_time = time.time() - 100 * 24 * 3600
        reminder.register_slot(9, create_time, "用户9")
        # 未调用 update_active_time，使用创建时间
        
        inactive = reminder.scan_inactive_slots(force=True)
        assert len(inactive) == 1
        assert inactive[0].inactive_days >= 90
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- 测试结果汇总 ---
    print("\n" + "=" * 60)
    print(f"测试结果: {passed} PASS, {failed} FAIL")
    print("=" * 60)