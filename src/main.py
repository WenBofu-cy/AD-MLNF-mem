#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AD-mlnf-mem 双漏斗记忆中枢 · 最小闭环入口

演示核心流程:
  漏斗一：驾驶员身份识别 → 行为观测 → 判定标签 → 统计 → 辅助提醒
  漏斗二：场景判定 → 经验写入L1 → 重要度计算 → 晋升判定 → 遗忘判定
  外挂模块：世界模型查询 → 法规库查询 → 情绪意图查询

版本：V1.0
原创提出者：文波福
开源协议：CC BY 4.0
"""

from bus import MemoryBus, MessageType, MessagePriority, BusMessage
from module_registry import get_module_info, get_module_count, list_all_modules
import time
import uuid
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum


# ============================================================
# 枚举定义
# ============================================================

class DrivingMode(Enum):
    """驾驶模式"""
    MANUAL = "manual"
    AUTONOMOUS = "autonomous"
    EMERGENCY_TAKEOVER = "emergency_takeover"


class SlotType(Enum):
    """子画像槽类型"""
    LONG_TERM = "long_term"
    TEMPORARY = "temporary"
    ONESHOT = "one_shot"


class BehaviorLabel(Enum):
    """行为判定标签"""
    GOOD = "优良习惯"
    BAD = "常态陋习"
    EMERGENCY = "应急特殊操作"


class SceneCategory(Enum):
    """场景类别"""
    HIGHWAY = "高速巡航"
    URBAN = "城区路口"
    PARKING = "泊车低速"
    SPECIAL = "特殊环境"
    GENERAL = "通用驾驶"
    RURAL = "乡村道路"


class ForgetMethod(Enum):
    """遗忘方式"""
    DIRECT_DELETE = "直接删除"
    COLD_ARCHIVE = "冷归档"


# ============================================================
# 数据结构
# ============================================================

@dataclass
class DriverIdentity:
    """驾驶员身份"""
    driver_id: str
    name: str
    recognition_method: str
    confidence: float


@dataclass
class BehaviorObservation:
    """驾驶行为观测"""
    obs_id: str
    timestamp: float
    steering_angle: float
    throttle: float
    brake_pressure: float
    speed: float
    gear: str
    turn_signal: str
    behavior_type: str


@dataclass
class ExperienceEntry:
    """经验条目"""
    entry_id: str
    scene_category: SceneCategory
    slot_id: int
    sub_label: str
    behavior: Dict[str, Any]
    result_label: str
    i0: float
    s_value: float
    v_value: float
    c_value: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class WorldModelResult:
    """世界模型查询结果"""
    target_class: str
    risk_vector: Dict[str, str]
    physical_props: Dict[str, float]
    scene_label: SceneCategory


@dataclass
class LawQueryResult:
    """法规查询结果"""
    law_ids: List[str]
    rigidity_levels: List[str]
    applicable_rules: List[str]


# ============================================================
# 模块模拟桩
# ============================================================

class F0_Controller:
    """总控漏斗 F₀（简化版）"""
    
    def __init__(self):
        self.module_id = "ad-01"
        self.current_mode = DrivingMode.MANUAL
        self.emergency_active = False
        print(f"[ad-01] 总控漏斗 F₀ 初始化完成")
    
    def set_mode(self, mode: DrivingMode):
        self.current_mode = mode
        print(f"[ad-01] 驾驶模式切换: {mode.value}")


class DriverProfileSlot:
    """漏斗一：子画像槽（简化版）"""
    
    def __init__(self, slot_id: int, slot_type: SlotType, driver_name: str = ""):
        self.slot_id = slot_id
        self.slot_type = slot_type
        self.driver_name = driver_name
        self.behaviors: List[BehaviorObservation] = []
        self.statistics: Dict[str, Dict[str, int]] = {}  # 行为类型 -> {优良/陋习/应急: 计数}
        print(f"[ad-05] 子画像槽创建: slot={slot_id}, type={slot_type.value}, driver={driver_name}")


class L1_Storage:
    """L1 临时层存储（简化版）"""
    
    def __init__(self, max_items: int = 100):
        self.module_id = "ad-20"
        self.max_items = max_items
        self.entries: Dict[str, ExperienceEntry] = {}
        self._entry_count = 0
        print(f"[ad-20] L1临时层初始化, 最大容量={max_items}")
    
    def write(self, entry: ExperienceEntry) -> bool:
        if len(self.entries) >= self.max_items:
            print(f"[ad-20] L1存储满，无法写入")
            return False
        self.entries[entry.entry_id] = entry
        self._entry_count += 1
        print(f"[ad-20] 经验写入L1: {entry.entry_id[:12]}..., 场景={entry.scene_category.value}")
        return True
    
    def get_item_count(self) -> int:
        return len(self.entries)


class I_Calc_Unit:
    """综合重要度计算单元（简化版）"""
    
    def __init__(self):
        self.module_id = "ad-36"
        self.alpha = 0.50
        self.beta = 0.20
        self.gamma = 0.30
        print(f"[ad-36] 重要度计算单元初始化, α={self.alpha}, β={self.beta}, γ={self.gamma}")
    
    def calculate(self, i0: float, s: float, v: float, c: float) -> float:
        i = i0 + self.alpha * s + self.beta * v + self.gamma * c
        return min(max(i, 0.05), 1.0)


class PromotionJudge:
    """晋升判定单元（简化版）"""
    
    def __init__(self):
        self.module_id = "ad-38"
        self.thresholds = {
            "L1_to_L2": {"time": 24 * 3600, "i": 0.40},
        }
        print(f"[ad-38] 晋升判定单元初始化")
    
    def judge(self, entry_id: str, layer: str, retention: float, i_value: float) -> Tuple[bool, str]:
        if layer == "L1":
            threshold = self.thresholds["L1_to_L2"]
            time_ok = retention >= threshold["time"]
            i_ok = i_value >= threshold["i"]
            
            if time_ok and i_ok:
                return True, "满足晋升L2条件"
            elif not time_ok:
                return False, f"留存时长不足: {retention/3600:.1f}h < {threshold['time']/3600:.0f}h"
            else:
                return False, f"I值不足: {i_value:.2f} < {threshold['i']:.2f}"
        return False, "未知层级"


class WorldModelSim:
    """世界模型模拟桩"""
    
    def __init__(self):
        self.module_id = "ad-44"
        print(f"[ad-44] 世界模型库初始化（模拟桩）")
    
    def query(self, scene_desc: str) -> WorldModelResult:
        # 简化模拟：根据场景描述返回分类结果
        if "高速" in scene_desc or "highway" in scene_desc:
            scene = SceneCategory.HIGHWAY
            target = "第二类：机动动态实体"
        elif "城区" in scene_desc or "路口" in scene_desc:
            scene = SceneCategory.URBAN
            target = "第四类：人类及非机动交通参与者"
        elif "泊车" in scene_desc or "停车" in scene_desc:
            scene = SceneCategory.PARKING
            target = "第一类：静态固定无生命实体"
        else:
            scene = SceneCategory.GENERAL
            target = "第二类：机动动态实体"
        
        return WorldModelResult(
            target_class=target,
            risk_vector={"出现概率": "高", "车道侵入概率": "中", "碰撞伤害严重度": "高"},
            physical_props={"摩擦系数": 0.8, "质量等级": 3},
            scene_label=scene
        )


class LawLibrarySim:
    """法规库模拟桩"""
    
    def __init__(self):
        self.module_id = "ad-45"
        print(f"[ad-45] 交通法规库初始化（模拟桩）")
    
    def query(self, scene_type: str) -> LawQueryResult:
        return LawQueryResult(
            law_ids=["LAW-001", "LAW-003"],
            rigidity_levels=["硬约束", "硬约束"],
            applicable_rules=["红灯停车", f"{scene_type}限速规定"]
        )


# ============================================================
# 最小闭环演示
# ============================================================

def print_separator(title: str):
    """打印分隔标题"""
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def main():
    print("=" * 70)
    print("  AD-mlnf-mem 双漏斗记忆中枢 · 最小闭环演示")
    print("  51模块 · 端到端流程验证")
    print("=" * 70)
    print(f"  已注册模块总数: {get_module_count()}")
    
    # ========== 初始化 ==========
    print_separator("STEP 1: 初始化核心模块")
    
    bus = MemoryBus()
    f0 = F0_Controller()
    
    # 注册模块到总线
    for mid in ["ad-01", "ad-02", "ad-03", "ad-04", "ad-05", "ad-07",
                 "ad-09", "ad-10", "ad-11", "ad-14", "ad-20", "ad-31",
                 "ad-36", "ad-38", "ad-40", "ad-44", "ad-45", "ad-46"]:
        bus.register_module(mid)
    
    print(f"  已初始化核心模块并注册到总线")
    print(f"  模块编号: F0(ad-01), L1(ad-20), I_Calc(ad-36), Promotion(ad-38)")
    print(f"  外挂模块: 世界模型(ad-44), 法规库(ad-45), 情绪库(ad-46)")
    
    # ========== 漏斗一演示 ==========
    print_separator("STEP 2: 漏斗一 · 驾驶员画像")
    
    # 驾驶员身份识别
    driver = DriverIdentity(
        driver_id="DRV-001",
        name="张三",
        recognition_method="中控屏手动选择",
        confidence=1.0
    )
    print(f"  驾驶员身份识别: {driver.name}")
    print(f"  识别方式: {driver.recognition_method}")
    print(f"  置信度: {driver.confidence}")
    
    # 创建长期子画像槽
    slot = DriverProfileSlot(slot_id=1, slot_type=SlotType.LONG_TERM, driver_name=driver.name)
    
    # 模拟驾驶行为观测
    behaviors = [
        BehaviorObservation("obs-001", time.time(), -5.0, 30.0, 0.0, 50.0, "D", "左转", "转弯"),
        BehaviorObservation("obs-002", time.time(), 2.0, 25.0, 0.0, 55.0, "D", "关闭", "匀速巡航"),
        BehaviorObservation("obs-003", time.time(), 8.0, 15.0, 1.5, 40.0, "D", "右转", "变道"),
    ]
    
    for obs in behaviors:
        slot.behaviors.append(obs)
    
    # 行为判定
    labels = {
        "obs-001": BehaviorLabel.GOOD,
        "obs-002": BehaviorLabel.GOOD,
        "obs-003": BehaviorLabel.BAD,  # 变道未提前打灯
    }
    
    print(f"  观测行为 {len(behaviors)} 条")
    for obs_id, label in labels.items():
        print(f"    {obs_id}: {label.value}")
    
    # 统计
    for obs_id, label in labels.items():
        obs = next(o for o in behaviors if o.obs_id == obs_id)
        bt = obs.behavior_type
        if bt not in slot.statistics:
            slot.statistics[bt] = {"优良习惯": 0, "常态陋习": 0, "应急特殊操作": 0}
        slot.statistics[bt][label.value] += 1
    
    print(f"  行为统计: {slot.statistics}")
    
    # ========== 漏斗二演示 ==========
    print_separator("STEP 3: 漏斗二 · 自动驾驶自成长经验")
    
    # 切换到自动驾驶模式
    f0.set_mode(DrivingMode.AUTONOMOUS)
    
    # 世界模型查询
    wm = WorldModelSim()
    scene_result = wm.query("高速巡航场景")
    print(f"  世界模型查询结果:")
    print(f"    目标分类: {scene_result.target_class}")
    print(f"    场景类别: {scene_result.scene_label.value}")
    print(f"    风险向量: {scene_result.risk_vector}")
    
    # 法规库查询
    law = LawLibrarySim()
    law_result = law.query("高速")
    print(f"  法规库查询结果: {law_result.applicable_rules}")
    
    # 创建自动驾驶经验条目
    experience = ExperienceEntry(
        entry_id=f"EXP-{uuid.uuid4().hex[:8]}",
        scene_category=scene_result.scene_label,
        slot_id=15,  # 高速巡航槽
        sub_label="常规通用",
        behavior={
            "type": "跟车",
            "speed": 100.0,
            "follow_distance": 2.3,
            "ttc": 3.5,
            "abs_triggered": False,
            "esc_triggered": False,
        },
        result_label="成功优化",
        i0=0.55,
        s_value=0.30,
        v_value=0.60,
        c_value=0.0,
    )
    
    # 写入L1
    l1 = L1_Storage(max_items=100)
    l1.write(experience)
    
    # 计算重要度
    i_calc = I_Calc_Unit()
    i_value = i_calc.calculate(
        i0=experience.i0,
        s=experience.s_value,
        v=experience.v_value,
        c=experience.c_value
    )
    print(f"\n  三维重要度计算:")
    print(f"    I₀={experience.i0}, S={experience.s_value}, V={experience.v_value}, C={experience.c_value}")
    print(f"    α={i_calc.alpha}, β={i_calc.beta}, γ={i_calc.gamma}")
    print(f"    I = {experience.i0} + {i_calc.alpha}×{experience.s_value} + "
          f"{i_calc.beta}×{experience.v_value} + {i_calc.gamma}×{experience.c_value}")
    print(f"    I = {i_value:.3f}")
    
    # 晋升判定
    promotion = PromotionJudge()
    # 模拟留存26小时
    retention_seconds = 26 * 3600
    can_promote, reason = promotion.judge(
        experience.entry_id, "L1", retention_seconds, i_value
    )
    print(f"\n  晋升判定 (L1→L2):")
    print(f"    留存时长: {retention_seconds/3600:.0f}h")
    print(f"    判定结果: {'✅ 可晋升' if can_promote else '✗ 暂缓'}")
    print(f"    原因: {reason}")
    
    # ========== 汇总 ==========
    print_separator("闭环演示完成")
    print(f"  驾驶模式: {f0.current_mode.value}")
    print(f"  漏斗一活跃槽: slot_{slot.slot_id} ({slot.driver_name})")
    print(f"  漏斗一行为统计: {len(slot.behaviors)}条观测")
    print(f"  漏斗二L1条目数: {l1.get_item_count()}")
    print(f"  综合重要度 I: {i_value:.3f}")
    print(f"  晋升判定: {'通过' if can_promote else '暂缓'}")
    
    print("\n" + "=" * 70)
    print("  ✅ AD-mlnf-mem 最小闭环验证通过")
    print("  双漏斗记忆中枢核心流程: 身份识别→行为观测→经验写入→重要度→晋升")
    print("=" * 70)


# ============================================================
# 单元测试
# ============================================================

if __name__ == "__main__":
    import sys
    
    # 如果传入 --test 参数则运行单元测试
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        print("=" * 60)
        print("AD-mlnf-mem 最小闭环 单元测试")
        print("=" * 60)
        
        passed, failed = 0, 0
        
        # --- TC-MAIN-01: 模块注册表51个模块 ---
        print("\n[TC-MAIN-01] 模块注册表包含51个模块")
        try:
            assert get_module_count() == 51
            print("   ✅ PASS")
            passed += 1
        except Exception as e:
            print(f"   ❌ FAIL: {e}")
            failed += 1
        
        # --- TC-MAIN-02: L1写入成功 ---
        print("\n[TC-MAIN-02] L1经验写入")
        try:
            l1 = L1_Storage(max_items=10)
            entry = ExperienceEntry(
                entry_id="test-001",
                scene_category=SceneCategory.HIGHWAY,
                slot_id=15, sub_label="常规通用",
                behavior={}, result_label="成功优化",
                i0=0.5, s_value=0.0, v_value=0.0, c_value=0.0
            )
            assert l1.write(entry) == True
            assert l1.get_item_count() == 1
            print("   ✅ PASS")
            passed += 1
        except Exception as e:
            print(f"   ❌ FAIL: {e}")
            failed += 1
        
        # --- TC-MAIN-03: 重要度计算 ---
        print("\n[TC-MAIN-03] 综合重要度I值计算")
        try:
            calc = I_Calc_Unit()
            i = calc.calculate(i0=0.50, s=0.60, v=0.70, c=0.40)
            expected = 0.50 + 0.50*0.60 + 0.20*0.70 + 0.30*0.40
            assert abs(i - min(expected, 1.0)) < 0.01
            print("   ✅ PASS")
            passed += 1
        except Exception as e:
            print(f"   ❌ FAIL: {e}")
            failed += 1
        
        # --- TC-MAIN-04: 晋升判定 ---
        print("\n[TC-MAIN-04] 晋升双条件判定")
        try:
            judge = PromotionJudge()
            can, reason = judge.judge("test-001", "L1", 25*3600, 0.45)
            assert can == True
            can2, reason2 = judge.judge("test-002", "L1", 20*3600, 0.45)
            assert can2 == False
            print("   ✅ PASS")
            passed += 1
        except Exception as e:
            print(f"   ❌ FAIL: {e}")
            failed += 1
        
        # --- TC-MAIN-05: 世界模型模拟桩 ---
        print("\n[TC-MAIN-05] 世界模型场景判定")
        try:
            wm = WorldModelSim()
            result = wm.query("高速场景")
            assert result.scene_label == SceneCategory.HIGHWAY
            result2 = wm.query("城区路口")
            assert result2.scene_label == SceneCategory.URBAN
            print("   ✅ PASS")
            passed += 1
        except Exception as e:
            print(f"   ❌ FAIL: {e}")
            failed += 1
        
        print("\n" + "=" * 60)
        print(f"测试结果: {passed} PASS, {failed} FAIL")
        print("=" * 60)
    else:
        main()
```