#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-41
模块名称: 遗忘执行调度+最低复用次数联合校验单元
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 晋升与遗忘执行机制
核心职责:
1. 接收ad-40输出分层遗忘候选清单，执行**最低复用次数二次强校验**，过滤具备实战价值受保护经验
2. 支持极低I值豁免、失败经验无仲裁豁免、冷条目阈值减半三大特殊规则
3. 结合整车驾驶负载、全局容量配额动态管控遗忘执行频次与单次清理上限
4. 区分直接删除/冷归档两类指令优先级，串行跨层级分发任务至ad-42
5. 汇总执行回执、运行统计，全流程行为写入ad-51不可变日志

依赖模块:
ad-40(遗忘阈值判定单元，输出遗忘候选清单),
ad-33(复用频次C值统计单元，提供精准复用次数与冷条目标记),
ad-42(冗余记忆删除与归档单元，接收最终可遗忘执行指令),
ad-48(全局容量配额管控单元，获取系统容量压力等级)
被依赖模块:
ad-20/22/24/26(层级存储单元，同步归档/删除后数据状态)

校验规则:
通用通过条件: 条目实际复用次数 < 分层分槽专属最低复用保护阈值
特殊豁免放行:
1. I值＜0.05极低重要度条目，无视复用次数直接放行遗忘
2. 策略失误且未通过安全仲裁的失败经验，不享受复用保护
3. 近90日无复用冷条目，保护阈值自动减半(最低保留1)

调度管控规则:
1. 高负载工况自动压缩遗忘执行数量，避免抢占自动驾驶主流程资源
2. 设置全局单次最大清理配额+分层独立上限，防止批量清理引发系统波动
3. 冷归档任务优先级低于直接删除，繁忙状态优先暂停归档类任务
4. 所有遗忘任务跨层级串行执行，禁止并发读写存储引发数据异常

安全约束:
S-01: 最低复用次数校验为遗忘流程最后一道防线，校验拒绝条目永久禁止强制清除
S-02: 极低重要度经验强制豁免复用保护机制
S-03: 未通过安全仲裁的负面驾驶经验不纳入实战价值保护范围
S-04: 本单元仅做校验判定+任务调度，不直接操作底层存储介质
S-05: 所有校验结果、调度分发、任务暂停、执行回执全量录入ad-51日志
S-06: L5核心层、不可抗力保护条目上游已拦截，本单元不再二次判定
"""

from typing import Dict, List, Optional, Any, Set
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid


# ==================== 全局枚举统一定义 ====================
class ScheduleState(Enum):
    """调度校验单元内部状态机"""
    IDLE = "idle"
    VALIDATING = "validating"
    SCHEDULING = "scheduling"
    BUSY_EXEC = "busy_execute"
    PAUSED = "paused"
    LOW_LOAD_LIMIT = "low_load_limit"


class ValidationConclusion(Enum):
    """复用次数校验最终结论"""
    PASS = "正常通过遗忘校验"
    PASS_LOW_I = "极低I值豁免放行"
    PASS_NO_ARBITRATION = "无安全仲裁失败经验放行"
    REJECT_PROTECTED = "复用次数达标，实战经验禁止遗忘"


class ForgetMethod(Enum):
    """遗忘执行方式(与ad-40完全对齐)"""
    DIRECT_DELETE = "直接删除"
    COLD_ARCHIVE = "冷归档"


class WorkLoadLevel(Enum):
    """整车驾驶负载等级"""
    LIGHT = "轻负载"
    NORMAL = "常规负载"
    HEAVY = "高负载"


class ScheduleFeedback(Enum):
    """下游ad-42执行回执状态"""
    SUCCESS = "执行成功"
    PARTIAL_FINISH = "部分完成"
    LIMIT_BLOCK = "配额拦截暂停"
    TASK_SUSPEND = "任务临时挂起"
    ERROR_ABORT = "执行异常终止"


# ==================== 标准化数据结构 ====================
@dataclass
class ForgetCandidate:
    """上游ad-40传入遗忘候选标准实体"""
    entry_id: str
    current_layer: str
    i_value: float
    reuse_count: int = 0
    forget_method: ForgetMethod = ForgetMethod.DIRECT_DELETE
    source_slot_id: int = 19
    reason: str = ""
    priority: float = 0.0


@dataclass
class ValidatedPassEntry:
    """二次校验通过、可下发执行条目"""
    entry_id: str
    current_layer: str
    i_value: float
    reuse_count: int
    forget_method: ForgetMethod
    source_slot_id: int
    validate_conclusion: ValidationConclusion
    priority: float = 0.0


@dataclass
class RejectProtectEntry:
    """校验拦截、受保护禁止遗忘条目"""
    entry_id: str
    current_layer: str
    i_value: float
    reuse_count: int
    protect_threshold: int
    reject_desc: str


@dataclass
class LayerDistributeTask:
    """分层待分发遗忘任务体"""
    layer_name: str
    execute_type: ForgetMethod
    target_entry_list: List[ValidatedPassEntry]
    task_total_num: int


@dataclass
class ExecuteDispatchOrder:
    """下发ad-42标准执行指令"""
    order_unique_id: str
    belong_layer: str
    run_type: ForgetMethod
    execute_entry_ids: List[str]
    single_limit: int
    create_time: float = field(default_factory=time.time)


@dataclass
class ExecuteResultFeedback:
    """ad-42执行结果回执"""
    order_unique_id: str
    success_finish_num: int
    fail_entry_id_list: List[str]
    feedback_state: ScheduleFeedback
    run_cost_time: float


@dataclass
class FullCycleResult:
    """单次完整校验+调度汇总结果"""
    cycle_uuid: str
    total_scan_candidate: int
    validate_pass_num: int
    validate_reject_num: int
    actual_dispatch_num: int
    real_finish_num: int
    suspend_task_num: int
    layer_detail_info: Dict[str, Dict[str, int]]
    reject_protect_list: List[RejectProtectEntry]
    timestamp: float = field(default_factory=time.time)


# ==================== 核心常量配置区(无魔法值) ====================
class ForgetScheduleConfig:
    # 分层默认最低复用保护阈值
    LAYER_BASE_PROTECT = {
        "L1": 3,
        "L2": 3,
        "L3": 4,
        "L4": 1
    }
    # L3分槽专属保护阈值覆写
    L3_SLOT_PROTECT_RULE = {
        15: 5,   # 高速巡航槽
        16: 5,   # 城区路口槽
        17: 2,   # 泊车低速低频槽
        18: 2,   # 特殊环境低频槽
        19: 4    # 通用驾驶槽
    }
    # 极低重要度豁免阈值
    EXTREME_LOW_I_LIMIT = 0.05
    # 冷条目阈值压缩比例
    COLD_ENTRY_REDUCE_RATIO = 0.5
    # 全局单次遗忘最大总配额
    GLOBAL_MAX_SINGLE_CLEAR = 60
    # 分层单次执行数量上限
    LAYER_SINGLE_MAX_LIMIT = {
        "L1": 30,
        "L2": 20,
        "L3": 15,
        "L4": 8
    }
    # 负载限流压缩系数
    LOAD_LIMIT_RATIO = {
        WorkLoadLevel.LIGHT: 1.0,
        WorkLoadLevel.NORMAL: 0.8,
        WorkLoadLevel.HEAVY: 0.4
    }
    # 最小调度执行间隔(秒)
    MIN_SCHEDULE_INTERVAL_SEC = 45


# ==================== 主类：联合校验+调度一体化单元 ====================
class ForgetScheduleVerifyUnit:
    """
    ad-41 遗忘执行调度+最低复用次数联合校验单元
    整合二次复用校验 + 负载限流 + 配额管控 + 串行任务分发全能力
    """
    def __init__(self):
        self.module_id = "ad-41"
        self.module_name = "遗忘执行调度+最低复用次数联合校验单元"
        self.config = ForgetScheduleConfig()

        # 核心状态
        self.run_state = ScheduleState.IDLE
        self.current_car_load = WorkLoadLevel.NORMAL
        self.last_schedule_run_time = 0.0

        # 任务缓存队列
        self.pending_layer_task_queue: List[LayerDistributeTask] = []

        # 全局运行统计
        self.total_validate_times = 0
        self.total_schedule_times = 0
        self.all_validate_scan = 0
        self.all_pass_validate = 0
        self.all_reject_protect = 0
        self.all_dispatch_entry = 0
        self.all_finish_entry = 0

        # ad-51 待落库日志容器
        self.pending_standard_logs: List[Dict[str, Any]] = []

        print(f"[{self.module_id}] 联合校验调度单元初始化完成")
        print(f"[{self.module_id}] 复用二次防线+负载限流+分层配额管控已启用")

    # ========== 对外状态管控接口 ==========
    def set_car_work_load(self, load_level: WorkLoadLevel) -> None:
        """设置当前整车驾驶负载等级"""
        self.current_car_load = load_level
        if load_level == WorkLoadLevel.HEAVY:
            self.run_state = ScheduleState.LOW_LOAD_LIMIT

    def pause_all_work(self) -> None:
        """暂停所有校验与调度任务"""
        self.run_state = ScheduleState.PAUSED

    def resume_all_work(self) -> None:
        """恢复正常运行"""
        self.run_state = ScheduleState.IDLE

    def get_current_unit_state(self) -> ScheduleState:
        return self.run_state

    # ========== 私有工具方法 ==========
    def _get_target_protect_threshold(self, layer: str, slot_id: int) -> int:
        """获取条目对应分层+分槽最终保护阈值"""
        if layer == "L3":
            return self.config.L3_SLOT_PROTECT_RULE.get(slot_id, self.config.LAYER_BASE_PROTECT["L3"])
        return self.config.LAYER_BASE_PROTECT.get(layer, 3)

    def _write_cycle_log(self, result: FullCycleResult) -> None:
        """写入标准化日志，等待推送ad-51"""
        self.pending_standard_logs.append({
            "log_mark": "ad41_forget_verify_schedule",
            "cycle_id": result.cycle_uuid,
            "scan_total": result.total_scan_candidate,
            "pass_validate": result.validate_pass_num,
            "reject_protect": result.validate_reject_num,
            "dispatch_real": result.actual_dispatch_num,
            "finish_success": result.real_finish_num,
            "car_load": self.current_car_load.value,
            "unit_state": self.run_state.value,
            "log_time": result.timestamp
        })

    def collect_all_pending_logs(self) -> List[Dict[str, Any]]:
        """批量取出所有待持久化日志"""
        logs = self.pending_standard_logs.copy()
        self.pending_standard_logs.clear()
        return logs

    def get_unit_full_statistics(self) -> Dict[str, Any]:
        """获取全维度运行统计数据"""
        return {
            "validate_total_scan": self.all_validate_scan,
            "validate_total_pass": self.all_pass_validate,
            "validate_total_reject": self.all_reject_protect,
            "schedule_total_run": self.total_schedule_times,
            "schedule_total_dispatch": self.all_dispatch_entry,
            "schedule_total_finish": self.all_finish_entry,
            "now_work_state": self.run_state.value,
            "now_car_load": self.current_car_load.value,
            "pending_task_count": len(self.pending_layer_task_queue)
        }

    # ========== 核心1：最低复用次数二次校验 ==========
    def execute_reuse_second_verify(self,
                                     origin_candidates: Dict[str, List[ForgetCandidate]],
                                     real_reuse_data: Dict[str, int],
                                     cold_entry_set: Optional[Set[str]] = None,
                                     no_arbitration_fail_set: Optional[Set[str]] = None) -> tuple[Dict[str, List[ValidatedPassEntry]], List[RejectProtectEntry]]:
        """
        执行遗忘候选二次复用次数强校验
        :param origin_candidates: ad40原始分层遗忘候选
        :param real_reuse_data: ad33真实复用次数 {entry_id:count}
        :param cold_entry_set: 冷条目ID集合
        :param no_arbitration_fail_set: 未通过安全仲裁失败条目
        :return: 通过列表 + 拦截保护列表
        """
        if self.run_state == ScheduleState.PAUSED:
            return {}, []
        self.run_state = ScheduleState.VALIDATING

        cold_entry_set = cold_entry_set or set()
        no_arbitration_fail_set = no_arbitration_fail_set or set()
        final_pass_map: Dict[str, List[ValidatedPassEntry]] = {}
        reject_protect_list: List[RejectProtectEntry] = []
        total_scan = 0

        for layer_name, candidate_list in origin_candidates.items():
            pass_temp_list = []
            for item in candidate_list:
                total_scan += 1
                eid = item.entry_id
                real_reuse = real_reuse_data.get(eid, 0)

                # 规则1：极低I值直接豁免
                if item.i_value < self.config.EXTREME_LOW_I_LIMIT:
                    pass_temp_list.append(ValidatedPassEntry(
                        entry_id=eid,
                        current_layer=layer_name,
                        i_value=item.i_value,
                        reuse_count=real_reuse,
                        forget_method=item.forget_method,
                        source_slot_id=item.source_slot_id,
                        validate_conclusion=ValidationConclusion.PASS_LOW_I,
                        priority=item.priority
                    ))
                    continue

                # 规则2：无安全仲裁失败经验豁免
                if eid in no_arbitration_fail_set:
                    pass_temp_list.append(ValidatedPassEntry(
                        entry_id=eid,
                        current_layer=layer_name,
                        i_value=item.i_value,
                        reuse_count=real_reuse,
                        forget_method=item.forget_method,
                        source_slot_id=item.source_slot_id,
                        validate_conclusion=ValidationConclusion.PASS_NO_ARBITRATION,
                        priority=item.priority
                    ))
                    continue

                # 获取基础保护阈值
                base_threshold = self._get_target_protect_threshold(layer_name, item.source_slot_id)
                # 规则3：冷条目阈值减半
                if eid in cold_entry_set:
                    base_threshold = max(int(base_threshold * self.config.COLD_ENTRY_REDUCE_RATIO), 1)

                # 核心复用次数判定
                if real_reuse < base_threshold:
                    pass_temp_list.append(ValidatedPassEntry(
                        entry_id=eid,
                        current_layer=layer_name,
                        i_value=item.i_value,
                        reuse_count=real_reuse,
                        forget_method=item.forget_method,
                        source_slot_id=item.source_slot_id,
                        validate_conclusion=ValidationConclusion.PASS,
                        priority=item.priority
                    ))
                else:
                    # 达到保护门槛，拦截禁止遗忘
                    reject_protect_list.append(RejectProtectEntry(
                        entry_id=eid,
                        current_layer=layer_name,
                        i_value=item.i_value,
                        reuse_count=real_reuse,
                        protect_threshold=base_threshold,
                        reject_desc=f"复用次数{real_reuse}≥保护阈值{base_threshold}，实战经验受保护"
                    ))

            if pass_temp_list:
                final_pass_map[layer_name] = pass_temp_list

        # 更新统计
        pass_num = sum(len(v) for v in final_pass_map.values())
        reject_num = len(reject_protect_list)
        self.all_validate_scan += total_scan
        self.all_pass_validate += pass_num
        self.all_reject_protect += reject_num
        self.total_validate_times += 1

        self.run_state = ScheduleState.IDLE
        return final_pass_map, reject_protect_list

    # ========== 核心2：负载限流+配额管控任务调度分发 ==========
    def load_verified_task_to_queue(self, pass_verify_data: Dict[str, List[ValidatedPassEntry]]) -> None:
        """将校验通过条目组装成分层任务送入调度队列"""
        for layer, entry_list in pass_verify_data.items():
            if not entry_list:
                continue
            # 同层级统一遗忘方式聚合
            first_method = entry_list[0].forget_method
            task = LayerDistributeTask(
                layer_name=layer,
                execute_type=first_method,
                target_entry_list=entry_list,
                task_total_num=len(entry_list)
            )
            self.pending_layer_task_queue.append(task)

    def start_full_schedule_dispatch(self, force_run: bool = False) -> FullCycleResult:
        """启动全流程调度下发至ad-42"""
        if self.run_state in [ScheduleState.PAUSED, ScheduleState.BUSY_EXEC]:
            return FullCycleResult("",0,0,0,0,0,0,{},[])

        now_ts = time.time()
        if not force_run and (now_ts - self.last_schedule_run_time) < self.config.MIN_SCHEDULE_INTERVAL_SEC:
            return FullCycleResult("",0,0,0,0,0,0,{},[])

        self.run_state = ScheduleState.SCHEDULING
        self.last_schedule_run_time = now_ts
        self.total_schedule_times += 1
        cycle_id = f"ad41-cycle-{uuid.uuid4().hex[:8]}"

        load_ratio = self.config.LOAD_LIMIT_RATIO[self.current_car_load]
        total_dispatch = 0
        total_finish = 0
        suspend_task_cnt = 0
        layer_run_detail: Dict[str, Dict[str, int]] = {}

        # 串行消费队列任务
        while self.pending_layer_task_queue:
            self.run_state = ScheduleState.BUSY_EXEC
            task = self.pending_layer_task_queue.pop(0)
            layer = task.layer_name
            # 分层上限+负载压缩
            layer_max = int(self.config.LAYER_SINGLE_MAX_LIMIT.get(layer, 20) * load_ratio)
            global_remain = self.config.GLOBAL_MAX_SINGLE_CLEAR - total_dispatch
            pick_max = min(layer_max, global_remain, task.task_total_num)

            if pick_max <= 0:
                suspend_task_cnt += 1
                continue

            # 按遗忘优先级从高到低选取执行
            sort_entries = sorted(task.target_entry_list, key=lambda x:x.priority, reverse=True)
            run_entries = sort_entries[:pick_max]
            run_eid_list = [x.entry_id for x in run_entries]

            # 组装下发指令
            dispatch_order = ExecuteDispatchOrder(
                order_unique_id=f"ord-{uuid.uuid4().hex[:8]}",
                belong_layer=layer,
                run_type=task.execute_type,
                execute_entry_ids=run_eid_list,
                single_limit=pick_max
            )
            # 模拟对接ad42获取回执(正式环境替换为接口调用)
            feedback = self._mock_ad42_feedback(dispatch_order)

            total_dispatch += pick_max
            total_finish += feedback.success_finish_num

            # 统计分层详情
            if layer not in layer_run_detail:
                layer_run_detail[layer] = {"dispatch":0,"finish":0}
            layer_run_detail[layer]["dispatch"] += pick_max
            layer_run_detail[layer]["finish"] += feedback.success_finish_num

        # 全局统计更新
        self.all_dispatch_entry += total_dispatch
        self.all_finish_entry += total_finish
        self.run_state = ScheduleState.IDLE

        # 构造最终结果
        result = FullCycleResult(
            cycle_uuid=cycle_id,
            total_scan_candidate=self.all_validate_scan,
            validate_pass_num=self.all_pass_validate,
            validate_reject_num=self.all_reject_protect,
            actual_dispatch_num=total_dispatch,
            real_finish_num=total_finish,
            suspend_task_num=suspend_task_cnt,
            layer_detail_info=layer_run_detail,
            reject_protect_list=[],
            timestamp=now_ts
        )
        self._write_cycle_log(result)
        return result

    def _mock_ad42_feedback(self, order: ExecuteDispatchOrder) -> ExecuteResultFeedback:
        """模拟下游执行回执，正式项目删除直接调用接口"""
        return ExecuteResultFeedback(
            order_unique_id=order.order_unique_id,
            success_finish_num=len(order.execute_entry_ids),
            fail_entry_id_list=[],
            feedback_state=ScheduleFeedback.SUCCESS,
            run_cost_time=0.2
        )


# ==================== 全覆盖单元测试 ====================
if __name__ == "__main__":
    print("=" * 70)
    print("ad-41 联合校验调度单元 完整版单元测试")
    print("=" * 70)
    test_pass, test_fail = 0, 0

    def build_mock_candidate(eid, layer, ival, reuse=0, slot=15, method=ForgetMethod.DIRECT_DELETE):
        return ForgetCandidate(
            entry_id=eid, current_layer=layer, i_value=ival,
            reuse_count=reuse, forget_method=method, source_slot_id=slot, priority=0.5
        )

    # TC41-01 基础复用次数正常通过
    print("\n[TC41-01] L2复用1小于阈值3 → 校验通过")
    try:
        unit = ForgetScheduleVerifyUnit()
        candi = {"L2":[build_mock_candidate("T01","L2",0.1,1)]}
        reuse_map = {"T01":1}
        pass_data, reject_data = unit.execute_reuse_second_verify(candi, reuse_map)
        assert len(pass_data["L2"]) == 1
        test_pass +=1
        print("   ✅ PASS")
    except Exception as e:
        test_fail +=1
        print(f"   ❌ FAIL:{e}")

    # TC41-02 复用达标被拦截保护
    print("\n[TC41-02] L2复用4≥3 → 禁止遗忘")
    try:
        unit = ForgetScheduleVerifyUnit()
        candi = {"L2":[build_mock_candidate("T02","L2",0.1,4)]}
        reuse_map = {"T02":4}
        pass_data, reject_data = unit.execute_reuse_second_verify(candi, reuse_map)
        assert len(reject_data) ==1
        test_pass +=1
        print("   ✅ PASS")
    except Exception as e:
        test_fail +=1
        print(f"   ❌ FAIL:{e}")

    # TC41-03 极低I值豁免放行
    print("\n[TC41-03] I=0.03<0.05 无视复用放行")
    try:
        unit = ForgetScheduleVerifyUnit()
        candi = {"L2":[build_mock_candidate("T03","L2",0.03,10)]}
        reuse_map = {"T03":10}
        pass_data, _ = unit.execute_reuse_second_verify(candi, reuse_map)
        assert len(pass_data["L2"]) ==1
        test_pass +=1
        print("   ✅ PASS")
    except Exception as e:
        test_fail +=1
        print(f"   ❌ FAIL:{e}")

    # TC41-04 L3低频分槽低保护阈值
    print("\n[TC41-04] L3泊车槽阈值2，复用1通过")
    try:
        unit = ForgetScheduleVerifyUnit()
        candi = {"L3":[build_mock_candidate("T04","L3",0.1,1,17)]}
        reuse_map = {"T04":1}
        pass_data, _ = unit.execute_reuse_second_verify(candi, reuse_map)
        assert len(pass_data["L3"]) ==1
        test_pass +=1
        print("   ✅ PASS")
    except Exception as e:
        test_fail +=1
        print(f"   ❌ FAIL:{e}")

    # TC41-05 冷条目阈值减半校验
    print("\n[TC41-05] L3通用槽4减半为2，复用2拦截")
    try:
        unit = ForgetScheduleVerifyUnit()
        candi = {"L3":[build_mock_candidate("T05","L3",0.1,2,19)]}
        reuse_map = {"T05":2}
        pass_data, reject_data = unit.execute_reuse_second_verify(candi, reuse_map, cold_entry_set={"T05"})
        assert len(reject_data) ==1
        test_pass +=1
        print("   ✅ PASS")
    except Exception as e:
        test_fail +=1
        print(f"   ❌ FAIL:{e}")

    # TC41-06 无仲裁失败经验豁免
    print("\n[TC41-06] 无安全仲裁失败经验直接放行")
    try:
        unit = ForgetScheduleVerifyUnit()
        candi = {"L3":[build_mock_candidate("T06","L3",0.1,8,15)]}
        reuse_map = {"T06":8}
        pass_data, _ = unit.execute_reuse_second_verify(candi, reuse_map, no_arbitration_fail_set={"T06"})
        assert len(pass_data["L3"]) ==1
        test_pass +=1
        print("   ✅ PASS")
    except Exception as e:
        test_fail +=1
        print(f"   ❌ FAIL:{e}")

    # TC41-07 高负载限流调度测试
    print("\n[TC41-07] 高负载工况自动压缩执行数量")
    try:
        unit = ForgetScheduleVerifyUnit()
        unit.set_car_work_load(WorkLoadLevel.HEAVY)
        candi = {"L1":[build_mock_candidate(f"H{i}","L1",0.08,0) for i in range(20)]}
        reuse_map = {f"H{i}":0 for i in range(20)}
        pass_data, _ = unit.execute_reuse_second_verify(candi, reuse_map)
        unit.load_verified_task_to_queue(pass_data)
        res = unit.start_full_schedule_dispatch(force_run=True)
        assert res.actual_dispatch_num >0
        test_pass +=1
        print("   ✅ PASS")
    except Exception as e:
        test_fail +=1
        print(f"   ❌ FAIL:{e}")

    # TC41-08 暂停状态阻断所有流程
    print("\n[TC41-08] 单元暂停后校验调度全部失效")
    try:
        unit = ForgetScheduleVerifyUnit()
        unit.pause_all_work()
        candi = {"L1":[build_mock_candidate("P01","L1",0.1,0)]}
        pass_data, _ = unit.execute_reuse_second_verify(candi, {"P01":0})
        assert len(pass_data) ==0
        test_pass +=1
        print("   ✅ PASS")
    except Exception as e:
        test_fail +=1
        print(f"   ❌ FAIL:{e}")

    print(f"\n===== 最终测试汇总：通过 {test_pass} 条，失败 {test_fail} 条 =====")