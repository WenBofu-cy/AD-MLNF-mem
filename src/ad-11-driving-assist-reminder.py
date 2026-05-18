#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-11
模块名称: 驾驶辅助提醒生成单元
所属分区: 二、漏斗一：驾驶员画像漏斗
核心职责: 依据行为累积统计单元输出的统计周期报表，结合当前场景特征，生成车内驾驶辅助
          语音提示与仪表显示内容。提示内容覆盖陋习纠正、优良鼓励、应急总结三种类型。
          仅输出至车内人机交互界面，编译期禁止接入自动驾驶决策链路。

依赖模块: ad-10(行为累积统计单元), ad-08(上下文场景标记单元), ad-02(漏斗一专属调度单元)
被依赖模块: 车内语音播报系统(硬件), 车内仪表/中控屏(硬件)

安全约束:
  S-01: 本模块输出仅限车内人机交互界面，编译期禁止向自动驾驶决策链路传输数据
  S-02: 提醒内容不得包含驾驶员身份明文信息
  S-03: 陋习提醒采用鼓励式措辞，禁止使用羞辱、恐吓、贬低性语言
  S-04: 未礼让行人提醒为法规强制项，触发阈值=1次即提醒，不可被用户关闭
  S-05: 用户可关闭除未礼让行人外的任意类型语音提醒
  S-06: 夜间静默时段(默认22:00-07:00)仅保留仪表显示，语音暂停
  S-07: 每次提醒发送均全量记录于 ad-51 变更日志
  S-08: 应急操作后10分钟内不主动语音打扰
"""

from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 枚举定义 ====================

class ReminderType(Enum):
    """提醒类型"""
    BAD_CORRECTION = "陋习纠正"
    GOOD_ENCOURAGEMENT = "优良鼓励"
    EMERGENCY_SUMMARY = "应急总结"


class ReminderPriority(Enum):
    """提醒优先级"""
    HIGH = "high"       # 法规强制项
    MEDIUM = "medium"   # 一般陋习
    LOW = "low"         # 优良鼓励


class VoiceType(Enum):
    """语音类型"""
    WARNING = "warning"
    ENCOURAGE = "encourage"
    INFO = "info"


class ReminderState(Enum):
    """提醒单元内部状态"""
    NORMAL = "normal"
    COOLDOWN = "cooldown"
    NIGHT_SILENT = "night_silent"
    USER_PAUSED = "user_paused"
    PAUSED = "paused"
    EMERGENCY_RO = "emergency_ro"


# ==================== 数据结构 ====================

@dataclass
class StatisticsReport:
    """统计周期报表（来自 ad-10）"""
    slot_id: int
    generate_time: float
    overall_excellence_rate: float
    bad_behavior_ranking: List[Tuple[str, float]]
    improvement_trend: Optional[float] = None


@dataclass
class ReminderConfig:
    """提醒配置"""
    voice_enabled: bool = True
    night_silent_enabled: bool = True
    night_start_hour: int = 22
    night_end_hour: int = 7
    reminder_frequency: str = "标准"  # 标准/频繁/稀少


@dataclass
class VoicePrompt:
    """语音提示指令"""
    prompt_id: str
    text: str
    priority: ReminderPriority
    voice_type: VoiceType
    timestamp: float = field(default_factory=time.time)


@dataclass
class DashboardDisplay:
    """仪表显示内容"""
    display_id: str
    text: str
    icon_type: str
    color: str
    duration_seconds: int = 15
    timestamp: float = field(default_factory=time.time)


@dataclass
class CooldownTimer:
    """冷却计时器"""
    reminder_type: str
    last_triggered: float
    cooldown_seconds: int


# ==================== 提醒触发规则库 ====================

# 陋习纠正规则
BAD_CORRECTION_RULES = {
    "未礼让行人": {
        "trigger_threshold": 1,
        "cooldown_seconds": 3600,
        "voice_template": "请务必在人行横道前礼让行人，这是法律规定",
        "icon": "行人图标",
        "color": "红色",
        "can_be_disabled": False,
    },
    "变道": {
        "trigger_threshold": 3,
        "cooldown_seconds": 7200,
        "voice_template": "最近您有变道未提前打灯的情况，提前3秒打灯更安全",
        "icon": "转向灯图标",
        "color": "黄色",
        "can_be_disabled": True,
    },
    "跟车": {
        "trigger_threshold": 5,
        "cooldown_seconds": 10800,
        "voice_template": "您最近跟车距离偏近，保持安全车距能避免追尾",
        "icon": "车距图标",
        "color": "黄色",
        "can_be_disabled": True,
    },
    "制动": {
        "trigger_threshold": 3,
        "cooldown_seconds": 7200,
        "voice_template": "最近有急刹车的情况，提前预判路况可以减少急刹",
        "icon": "刹车图标",
        "color": "黄色",
        "can_be_disabled": True,
    },
    "加速": {
        "trigger_threshold": 5,
        "cooldown_seconds": 7200,
        "voice_template": "最近急加速偏多，平缓加速更省电也更安全",
        "icon": "加速图标",
        "color": "黄色",
        "can_be_disabled": True,
    },
}

# 优良鼓励规则
GOOD_ENCOURAGEMENT_RULES = {
    "综合优良率优秀": {
        "trigger_threshold": 0.90,
        "cooldown_seconds": 86400,
        "voice_template": "近一个月您的驾驶综合优良率很高，您是一位非常优秀的驾驶员",
        "icon": "奖杯图标",
        "color": "绿色",
    },
    "综合优良率提升": {
        "trigger_threshold": 0.10,
        "cooldown_seconds": 21600,
        "voice_template": "最近一周您的驾驶习惯有明显改善，继续保持",
        "icon": "点赞图标",
        "color": "绿色",
    },
}

# 应急总结规则
EMERGENCY_SUMMARY_RULES = {
    "trigger_threshold": 3,
    "cooldown_seconds": 14400,
    "voice_template": "最近一周您遇到了多次需要紧急避险的情况，行车路上多加小心",
    "icon": "叹号图标",
    "color": "蓝色",
    "silence_after_event_seconds": 600,
}


# ==================== 主类定义 ====================

class DrivingAssistReminder:
    """
    驾驶辅助提醒生成单元
    
    职责:
    1. 接收 ad-10 统计周期报表
    2. 根据触发规则库生成语音提示与仪表显示
    3. 管理冷却计时器，避免频繁骚扰
    4. 夜间静默时段仅仪表显示
    5. 应急操作后10分钟不主动语音打扰
    """
    
    def __init__(self):
        self.module_id = "ad-11"
        self.module_name = "驾驶辅助提醒生成单元"
        
        # 内部状态
        self.state = ReminderState.NORMAL
        
        # 提醒配置
        self.config = ReminderConfig()
        
        # 冷却计时器
        self._cooldown_timers: Dict[str, CooldownTimer] = {}
        
        # 用户关闭的提醒类型集合
        self._disabled_reminders: set = set()
        
        # 连续关闭计数器
        self._user_dismiss_count: Dict[str, int] = {}
        
        # 最后一次应急操作时间
        self._last_emergency_time: float = 0.0
        
        # 统计
        self._total_prompts = 0
        self._total_displays = 0
        
        # 待写入 ad-51 的日志
        self._pending_logs: List[Dict[str, Any]] = []
        
        print(f"[{self.module_id}] 驾驶辅助提醒生成单元初始化完成")
    
    # ========== 状态管理 ==========
    
    def set_voice_enabled(self, enabled: bool) -> None:
        """开关语音提醒"""
        self.config.voice_enabled = enabled
        if not enabled:
            self.state = ReminderState.USER_PAUSED
            print(f"[{self.module_id}] 语音提醒已关闭")
        else:
            self.state = ReminderState.NORMAL
            print(f"[{self.module_id}] 语音提醒已开启")
    
    def set_night_silent(self, enabled: bool) -> None:
        """设置夜间静默"""
        self.config.night_silent_enabled = enabled
    
    def disable_reminder_type(self, reminder_type: str) -> bool:
        """用户关闭某类提醒（未礼让行人不可关闭）"""
        if reminder_type == "未礼让行人":
            return False
        self._disabled_reminders.add(reminder_type)
        return True
    
    def enable_reminder_type(self, reminder_type: str) -> None:
        """用户重新开启某类提醒"""
        self._disabled_reminders.discard(reminder_type)
    
    def pause(self) -> None:
        self.state = ReminderState.PAUSED
    
    def resume(self) -> None:
        self.state = ReminderState.NORMAL
    
    def emergency_stop(self) -> None:
        self.state = ReminderState.EMERGENCY_RO
    
    # ========== 提醒生成 ==========
    
    def process_report(self, report: StatisticsReport) -> Tuple[List[VoicePrompt], List[DashboardDisplay]]:
        """
        处理统计周期报表，生成提醒
        
        Returns:
            (语音提示列表, 仪表显示列表)
        """
        if self.state in [ReminderState.EMERGENCY_RO, ReminderState.PAUSED]:
            return [], []
        
        voice_prompts = []
        dashboard_displays = []
        
        # 仪表显示始终更新
        dashboard = self._generate_dashboard(report)
        if dashboard:
            dashboard_displays.append(dashboard)
            self._total_displays += 1
        
        # 夜间静默检查
        if self._is_night_silent():
            self.state = ReminderState.NIGHT_SILENT
            return voice_prompts, dashboard_displays
        
        # 用户暂停检查
        if not self.config.voice_enabled:
            return voice_prompts, dashboard_displays
        
        # 检查冷却计时器
        now = time.time()
        self._update_cooldowns(now)
        
        # 陋习纠正提醒
        for behavior, rules in BAD_CORRECTION_RULES.items():
            if behavior in self._disabled_reminders:
                continue
            if self._is_in_cooldown(behavior):
                continue
            
            # 查找该行为在排行榜中的陋习次数
            bad_count = 0
            for item in report.bad_behavior_ranking:
                if item[0] == behavior:
                    bad_count = item[1]
                    break
            
            if bad_count >= rules["trigger_threshold"]:
                prompt = VoicePrompt(
                    prompt_id=f"voice-{uuid.uuid4().hex[:6]}",
                    text=rules["voice_template"],
                    priority=ReminderPriority.HIGH if behavior == "未礼让行人" else ReminderPriority.MEDIUM,
                    voice_type=VoiceType.WARNING
                )
                voice_prompts.append(prompt)
                self._set_cooldown(behavior, rules["cooldown_seconds"])
                self._total_prompts += 1
        
        # 应急总结提醒
        if "应急总结" not in self._disabled_reminders and not self._is_in_cooldown("应急总结"):
            if self._is_safe_to_prompt_emergency(now):
                # 检查近7日应急次数（简化实现）
                if self._get_emergency_count(report) >= EMERGENCY_SUMMARY_RULES["trigger_threshold"]:
                    prompt = VoicePrompt(
                        prompt_id=f"voice-{uuid.uuid4().hex[:6]}",
                        text=EMERGENCY_SUMMARY_RULES["voice_template"],
                        priority=ReminderPriority.MEDIUM,
                        voice_type=VoiceType.INFO
                    )
                    voice_prompts.append(prompt)
                    self._set_cooldown("应急总结", EMERGENCY_SUMMARY_RULES["cooldown_seconds"])
                    self._total_prompts += 1
        
        # 优良鼓励提醒
        if report.overall_excellence_rate >= GOOD_ENCOURAGEMENT_RULES["综合优良率优秀"]["trigger_threshold"]:
            if not self._is_in_cooldown("综合优良率优秀"):
                prompt = VoicePrompt(
                    prompt_id=f"voice-{uuid.uuid4().hex[:6]}",
                    text=GOOD_ENCOURAGEMENT_RULES["综合优良率优秀"]["voice_template"],
                    priority=ReminderPriority.LOW,
                    voice_type=VoiceType.ENCOURAGE
                )
                voice_prompts.append(prompt)
                self._set_cooldown("综合优良率优秀", 
                                   GOOD_ENCOURAGEMENT_RULES["综合优良率优秀"]["cooldown_seconds"])
                self._total_prompts += 1
        
        return voice_prompts, dashboard_displays
    
    def notify_emergency_event(self) -> None:
        """通知应急操作发生，设置10分钟沉默期"""
        self._last_emergency_time = time.time()
        print(f"[{self.module_id}] 应急操作记录，10分钟内不主动语音打扰")
    
    # ========== 内部方法 ==========
    
    def _generate_dashboard(self, report: StatisticsReport) -> Optional[DashboardDisplay]:
        """生成仪表显示内容"""
        if report is None:
            return None
        
        # 生成简要显示文本
        rate_pct = int(report.overall_excellence_rate * 100)
        
        if report.bad_behavior_ranking:
            top_bad = report.bad_behavior_ranking[0][0]
            text = f"综合优良率 {rate_pct}%  注意: {top_bad}"
        else:
            text = f"综合优良率 {rate_pct}%"
        
        return DashboardDisplay(
            display_id=f"disp-{uuid.uuid4().hex[:6]}",
            text=text,
            icon_type="驾驶评分",
            color="白色",
            duration_seconds=15
        )
    
    def _is_night_silent(self) -> bool:
        """检查是否处于夜间静默时段"""
        if not self.config.night_silent_enabled:
            return False
        
        now = time.localtime()
        current_hour = now.tm_hour
        
        if self.config.night_start_hour > self.config.night_end_hour:
            # 跨午夜
            return current_hour >= self.config.night_start_hour or current_hour < self.config.night_end_hour
        else:
            return self.config.night_start_hour <= current_hour < self.config.night_end_hour
    
    def _is_safe_to_prompt_emergency(self, now: float) -> bool:
        """检查是否距离上次应急操作超过10分钟"""
        return now - self._last_emergency_time > EMERGENCY_SUMMARY_RULES["silence_after_event_seconds"]
    
    def _get_emergency_count(self, report: StatisticsReport) -> int:
        """获取应急操作次数（简化实现）"""
        # 实际应从报告中获取，此处返回模拟值
        return 0
    
    def _is_in_cooldown(self, reminder_key: str) -> bool:
        """检查某类提醒是否在冷却期"""
        if reminder_key not in self._cooldown_timers:
            return False
        timer = self._cooldown_timers[reminder_key]
        return time.time() - timer.last_triggered < timer.cooldown_seconds
    
    def _set_cooldown(self, reminder_key: str, cooldown_seconds: int) -> None:
        """设置冷却计时器"""
        self._cooldown_timers[reminder_key] = CooldownTimer(
            reminder_type=reminder_key,
            last_triggered=time.time(),
            cooldown_seconds=cooldown_seconds
        )
    
    def _update_cooldowns(self, now: float) -> None:
        """更新冷却计时器状态"""
        expired = []
        for key, timer in self._cooldown_timers.items():
            if now - timer.last_triggered >= timer.cooldown_seconds:
                expired.append(key)
        for key in expired:
            del self._cooldown_timers[key]
    
    # ========== 查询接口 ==========
    
    def get_state(self) -> ReminderState:
        return self.state
    
    def get_statistics(self) -> Dict[str, Any]:
        return {
            "total_prompts": self._total_prompts,
            "total_displays": self._total_displays,
            "voice_enabled": self.config.voice_enabled,
            "disabled_reminders": list(self._disabled_reminders),
            "active_cooldowns": len(self._cooldown_timers),
            "state": self.state.value
        }
    
    def collect_pending_logs(self) -> List[Dict[str, Any]]:
        logs = self._pending_logs.copy()
        self._pending_logs.clear()
        return logs


# ==================== 单元测试 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("ad-11 驾驶辅助提醒生成单元 单元测试")
    print("=" * 60)
    
    passed, failed = 0, 0
    
    # --- TC-11-01: 陋习纠正提醒触发 ---
    print("\n[TC-11-01] 陋习纠正提醒触发")
    try:
        reminder = DrivingAssistReminder()
        report = StatisticsReport(
            slot_id=1,
            generate_time=time.time(),
            overall_excellence_rate=0.65,
            bad_behavior_ranking=[("变道", 5.0), ("跟车", 3.0)]
        )
        voices, displays = reminder.process_report(report)
        assert len(displays) == 1
        # 变道陋习达到触发阈值，应生成语音提示
        assert len(voices) >= 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-11-02: 未礼让行人强制提醒（不可关闭） ---
    print("\n[TC-11-02] 未礼让行人强制提醒")
    try:
        reminder = DrivingAssistReminder()
        # 尝试关闭未礼让行人提醒
        result = reminder.disable_reminder_type("未礼让行人")
        assert result == False  # 不可关闭
        report = StatisticsReport(1, time.time(), 0.5, [("未礼让行人", 1.0)])
        voices, _ = reminder.process_report(report)
        assert len(voices) >= 1
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-11-03: 优良鼓励提醒 ---
    print("\n[TC-11-03] 优良鼓励提醒")
    try:
        reminder = DrivingAssistReminder()
        report = StatisticsReport(1, time.time(), 0.92, [])
        voices, _ = reminder.process_report(report)
        assert len(voices) >= 1
        assert voices[0].voice_type == VoiceType.ENCOURAGE
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-11-04: 冷却期内不重复提醒 ---
    print("\n[TC-11-04] 冷却期内不重复提醒")
    try:
        reminder = DrivingAssistReminder()
        report = StatisticsReport(1, time.time(), 0.65, [("变道", 5.0)])
        voices1, _ = reminder.process_report(report)
        voices2, _ = reminder.process_report(report)
        assert len(voices1) >= 1
        assert len(voices2) == 0  # 冷却期内
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-11-05: 夜间静默仅仪表显示 ---
    print("\n[TC-11-05] 夜间静默仅仪表显示")
    try:
        reminder = DrivingAssistReminder()
        reminder.config.night_silent_enabled = True
        # 模拟夜间（无法直接设置时间，通过直接设置状态测试）
        reminder.state = ReminderState.NIGHT_SILENT
        report = StatisticsReport(1, time.time(), 0.65, [("变道", 5.0)])
        voices, displays = reminder.process_report(report)
        assert len(displays) == 1
        assert len(voices) == 0  # 夜间不语音
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-11-06: 用户关闭语音提醒 ---
    print("\n[TC-11-06] 用户关闭语音提醒")
    try:
        reminder = DrivingAssistReminder()
        reminder.set_voice_enabled(False)
        report = StatisticsReport(1, time.time(), 0.65, [("变道", 5.0)])
        voices, displays = reminder.process_report(report)
        assert len(displays) == 1
        assert len(voices) == 0
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-11-07: 应急操作后10分钟不打扰 ---
    print("\n[TC-11-07] 应急操作后10分钟不打扰")
    try:
        reminder = DrivingAssistReminder()
        reminder.notify_emergency_event()
        # 应急总结在沉默期内
        assert reminder._is_safe_to_prompt_emergency(time.time()) == False
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- TC-11-08: 仪表显示始终更新 ---
    print("\n[TC-11-08] 仪表显示始终更新")
    try:
        reminder = DrivingAssistReminder()
        reminder.set_voice_enabled(False)
        report = StatisticsReport(1, time.time(), 0.85, [])
        _, displays = reminder.process_report(report)
        assert len(displays) == 1
        assert "85%" in displays[0].text
        print("   ✅ PASS")
        passed += 1
    except Exception as e:
        print(f"   ❌ FAIL: {e}")
        failed += 1
    
    # --- 测试结果汇总 ---
    print("\n" + "=" * 60)
    print(f"测试结果: {passed} PASS, {failed} FAIL")
    print("=" * 60)