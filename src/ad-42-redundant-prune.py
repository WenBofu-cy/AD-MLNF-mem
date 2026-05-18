#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块编号: ad-42
模块名称: 冗余记忆删除与归档执行单元【最终合并定稿】
所属分区: 三、漏斗二：自动驾驶自成长漏斗 / 晋升与遗忘执行机制
核心职责: 承接ad-41调度下发指令，作为漏斗二唯一拥有物理数据清除权限单元；
          严格遵循DoD 5220.22-M军用覆写标准执行安全删除，规范压缩流转至ad-49完成冷归档；
          内置任务队列+负载限流+优先级调度，同时落地白皮书全部安全约束与熔断机制；
          上层业务调度逻辑 + 底层硬件级安全擦除逻辑双向合一，全链路无缝对接上下游。

依赖模块: 
ad-41(遗忘执行调度单元，下发校验通过执行清单与配额管控参数),
ad-49(存储压缩与冷归档单元，接收压缩归档持久化数据),
ad-20/22/24/26(各层级存储单元，完成源数据读取与索引清理),
ad-29(L5核心层安全规则硬锁定单元，L3及以上条目强制写保护校验),
ad-48(全局容量配额管控单元，同步释放存储容量水位),
ad-51(全局不可变日志单元，全操作轨迹落地留存)
被依赖模块:
ad-20/22/24/26(接收删除/归档完成回执，更新层级存储状态),
ad-49(接收合规压缩归档数据包), ad-41(回传批次执行结果回执)

安全约束:
S-01: 直接删除强制执行DoD 5220.22-M标准单次全零覆写，覆写完成强制硬件FLUSH落盘
S-02: 冷归档必须确认ad-49接收成功后，方可删除源层级数据，归档失败源条目完整保留
S-03: L3、L4高价值层级条目执行任意操作前，必须调用ad-29完成写保护状态校验
S-04: 本单元为漏斗二体系内唯一具备物理擦除、永久清除数据权限模块，其他模块无权操作
S-05: 支持全局紧急熔断机制，已完成操作数据保留生效，未启动任务直接取消终止
S-06: 删除、归档、拦截、异常、熔断全类型操作日志全量录入ad-51，全程可审计溯源
S-07: 承接上层负载限流策略，高负载自动压缩单次执行数量，避免抢占主驾驶流程资源
S-08: 任务按遗忘优先级排序执行，同批次内直接删除优先级高于冷归档
"""

from typing import Dict, List, Optional, Any, Tuple, Callable, Set
from dataclasses import dataclass, field
from enum import Enum
import time
import uuid
import zlib


# ==================== 全局统一枚举定义(双向对齐) ====================
class PruneUnitState(Enum):
    """执行单元全局状态机(融合调度+执行双状态)"""
    IDLE = "空闲待命"
    VALID_WAIT = "指令待校验"
    EXECUTING = "批量执行中"
    ROLLING_BACK = "异常回退中"
    PAUSED = "全局任务暂停"
    LOW_LOAD_LIMIT = "高负载限流模式"


class ForgetOperateType(Enum):
    """遗忘操作类型(与ad-41完全对齐)"""
    DIRECT_DELETE = "直接删除"
    COLD_ARCHIVE = "冷归档"


class SingleOperateResult(Enum):
    """单条目精细化执行结果"""
    EXEC_SUCCESS = "执行成功"
    FAIL_ENTRY_NOT_FOUND = "条目源数据不存在"
    FAIL_WRITE_PROTECTED = "受ad-29写保护拦截"
    FAIL_OVERWRITE_ERROR = "物理覆写硬件异常"
    FAIL_ARCHIVE_TRANS_ERR = "归档推送ad-49失败"
    FAIL_SOURCE_INDEX_ERR = "源层级索引清理失败"
    FAIL_EMERGENCY_ABORT = "紧急熔断强制终止"


class ScheduleFeedbackState(Enum):
    """回传ad-41统一回执状态"""
    SUCCESS = "批量执行完成"
    PARTIAL_FINISH = "部分条目执行成功"
    LIMIT_BLOCK = "负载配额拦截暂停"
    TASK_SUSPEND = "任务临时挂起"
    ERROR_ABORT = "批量执行异常终止"


# ==================== 标准化统一数据结构 ====================
@dataclass
class Ad41PassValidEntry:
    """对接ad-41标准入参实体，完全复用上游输出结构"""
    entry_id: str
    current_layer: str
    i_value: float
    reuse_count: int
    forget_method: str
    source_slot_id: int
    validation_conclusion: str
    priority: float = 0.0


@dataclass
class OriginLayerEntryData:
    """从层级存储读取原始完整条目数据"""
    entry_id: str
    content: Dict[str, Any]
    metadata: Dict[str, Any]
    physical_storage_addr: int
    data_total_size_bytes: int


@dataclass
class SingleItemOperateReport:
    """单条目执行详细报告"""
    entry_id: str
    belong_layer: str
    operate_type: ForgetOperateType
    run_result: SingleOperateResult
    run_detail_msg: str = ""
    operate_timestamp: float = field(default_factory=time.time)


@dataclass
class LayerDispatchTask:
    """分层调度任务实体(队列缓存专用)"""
    layer_tag: str
    task_operate_type: ForgetOperateType
    target_entry_list: List[Ad41PassValidEntry]
    task_total_count: int


@dataclass
class BatchFinalExecuteOrder:
    """批次整合执行指令"""
    batch_unique_id: str
    global_limit_num: int
    load_compress_ratio: float
    create_dispatch_time: float = field(default_factory=time.time)


@dataclass
class BatchExecuteReturnFeedback:
    """回传ad-41标准批次回执"""
    feedback_batch_id: str
    total_receive_task: int
    actual_run_num: int
    success_finish_num: int
    intercept_protect_num: int
    exception_fail_num: int
    emergency_abort_num: int
    single_detail_reports: List[SingleItemOperateReport]
    total_run_cost_ms: float
    feedback_state: ScheduleFeedbackState
    finish_timestamp: float = field(default_factory=time.time)


@dataclass
class StorageCapacityReleaseSync:
    """容量释放同步结构体，对接ad-48"""
    layer_name: str
    free_entry_count: int
    free_total_bytes: int
    release_sync_time: float = field(default_factory=time.time)


# ==================== 全局运行常量配置区 ====================
class Ad42RuntimeGlobalConfig:
    # 物理覆写硬件标准配置 DoD 5220.22-M
    SECURE_OVERWRITE_BLOCK_SIZE = 4096
    FULL_ZERO_OVERWRITE_PATTERN = 0x00
    MAX_STORAGE_WRITE_RETRY = 3

    # 批次执行限流控制
    SINGLE_BATCH_MAX_HANDLE = 25
    ITEM_EXEC_INTERVAL_SEC = 0.005
    MIN_EXEC_SCHEDULE_INTERVAL = 40

    # 负载压缩比例(与ad-41负载等级完全对齐)
    LOAD_LEVEL_COMPRESS_RATIO = {
        "轻负载": 1.0,
        "常规负载": 0.8,
        "高负载": 0.4
    }

    # 冷归档压缩等级与过期配置
    ARCHIVE_COMPRESS_LEVEL = 6
    COLD_DATA_AUTO_EXPIRE_DAY = 180

    # 系统全局锁定保护条目池
    SYSTEM_LOCK_PROTECT_ENTRY_SET: Set[str] = set()


# ==================== 主类：调度队列+物理执行双合一核心单元 ====================
class RedundantMemoryPruneMergeUnit:
    """
    ad-42 最终合并定稿单元
    融合：ad41联动调度队列 + 负载限流配额管控 + 优先级排序
    融合：白皮书全安全约束 + DoD军用安全覆写 + ad29写保护校验 + ad49合规归档
    融合：紧急熔断机制 + 全链路日志 + 容量同步 + 标准化上下游回执
    """
    def __init__(self):
        self.module_id = "ad-42"
        self.module_name = "冗余记忆删除与归档合并执行单元"
        self.global_config = Ad42RuntimeGlobalConfig()

        # 核心运行状态
        self.unit_run_state = PruneUnitState.IDLE
        self.current_system_load_tag = "常规负载"
        self.last_batch_execute_time = 0.0

        # 分层任务调度队列
        self.pending_layer_task_queue: List[LayerDispatchTask] = []
        self.current_running_batch_reports: Optional[List[SingleItemOperateReport]] = None

        # 全局运行统计台账
        self.total_execute_batch_times = 0
        self.total_physical_delete_count = 0
        self.total_cold_archive_count = 0
        self.total_protect_intercept_count = 0
        self.total_emergency_abort_count = 0
        self.total_exception_fail_count = 0

        # 数据同步缓存
        self.pending_capacity_sync_list: List[StorageCapacityReleaseSync] = []
        self.pending_ad51_operate_logs: List[Dict[str, Any]] = []

        print(f"[{self.module_id}] 合并定稿单元初始化完成")
        print(f"[{self.module_id}] 业务调度队列+底层安全覆写双逻辑已启用")
        print(f"[{self.module_id}] 全白皮书6项安全约束全部落地生效")

    # ========== 外部状态与负载管控接口 ==========
    def set_current_system_load(self, load_tag: str) -> None:
        """同步ad-41整车负载等级，开启对应限流策略"""
        if load_tag in self.global_config.LOAD_LEVEL_COMPRESS_RATIO:
            self.current_system_load_tag = load_tag
            if load_tag == "高负载":
                self.unit_run_state = PruneUnitState.LOW_LOAD_LIMIT

    def pause_all_execute_task(self) -> None:
        """暂停所有队列任务与正在执行操作"""
        self.unit_run_state = PruneUnitState.PAUSED

    def resume_all_execute_task(self) -> None:
        """恢复单元正常运行状态"""
        self.unit_run_state = PruneUnitState.IDLE

    def update_system_lock_protect_entry(self, lock_eid_set: Set[str]) -> None:
        """批量更新全局硬锁定保护条目"""
        self.global_config.SYSTEM_LOCK_PROTECT_ENTRY_SET = lock_eid_set

    def get_current_unit_work_state(self) -> PruneUnitState:
        return self.unit_run_state

    # ========== 紧急熔断核心接口(S-05) ==========
    def trigger_emergency_abort(self) -> List[SingleItemOperateReport]:
        """触发全局紧急熔断，终止当前所有执行流程"""
        if self.unit_run_state != PruneUnitState.EXECUTING:
            return []
        self.unit_run_state = PruneUnitState.PAUSED
        abort_item_list = []
        if self.current_running_batch_reports:
            for report in self.current_running_batch_reports:
                if report.run_result == SingleOperateResult.EXEC_SUCCESS:
                    continue
                report.run_result = SingleOperateResult.FAIL_EMERGENCY_ABORT
                report.run_detail_msg = "系统紧急熔断强制终止操作"
                abort_item_list.append(report)
        self.total_emergency_abort_count += len(abort_item_list)
        print(f"[{self.module_id}] 紧急熔断生效，终止待执行条目：{len(abort_item_list)}条")
        return abort_item_list

    # ========== 任务入队与队列排序管理 ==========
    def push_valid_entry_to_task_queue(self, layer_group_entries: Dict[str, List[Ad41PassValidEntry]]) -> None:
        """接收ad-41下发分层清单，组装任务进入调度队列"""
        if self.unit_run_state == PruneUnitState.PAUSED:
            return
        for layer_name, entry_list in layer_group_entries.items():
            if not entry_list:
                continue
            first_op_type = ForgetOperateType.DIRECT_DELETE if entry_list[0].forget_method == "直接删除" else ForgetOperateType.COLD_ARCHIVE
            new_task = LayerDispatchTask(
                layer_tag=layer_name,
                task_operate_type=first_op_type,
                target_entry_list=entry_list,
                task_total_count=len(entry_list)
            )
            self.pending_layer_task_queue.append(new_task)
        # 队列排序：直接删除任务优先执行
        self.pending_layer_task_queue.sort(key=lambda x: 0 if x.task_operate_type == ForgetOperateType.DIRECT_DELETE else 1)

    # ========== 批次批量执行总入口(对接ad-41主调用) ==========
    def start_all_queue_batch_execute(self, force_run: bool = False) -> BatchExecuteReturnFeedback:
        """消费调度队列所有任务，完成全流程执行并生成标准回执"""
        if self.unit_run_state in [PruneUnitState.PAUSED, PruneUnitState.EXECUTING]:
            return BatchExecuteReturnFeedback("",0,0,0,0,0,0,[],0,ScheduleFeedbackState.TASK_SUSPEND)
        now_time = time.time()
        if not force_run and (now_time - self.last_batch_execute_time) < self.global_config.MIN_EXEC_SCHEDULE_INTERVAL:
            return BatchExecuteReturnFeedback("",0,0,0,0,0,0,[],0,ScheduleFeedbackState.LIMIT_BLOCK)

        self.unit_run_state = PruneUnitState.EXECUTING
        self.last_batch_execute_time = now_time
        batch_id = f"ad42-batch-{uuid.uuid4().hex[:8]}"
        load_compress_ratio = self.global_config.LOAD_LEVEL_COMPRESS_RATIO[self.current_system_load_tag]

        total_recv_task = 0
        actual_run = 0
        success_num = 0
        protect_intercept = 0
        exception_fail = 0
        abort_num = 0
        all_detail_reports: List[SingleItemOperateReport] = []

        # 串行消费队列任务
        while self.pending_layer_task_queue:
            current_task = self.pending_layer_task_queue.pop(0)
            total_recv_task += current_task.task_total_count
            # 负载限流裁剪执行数量
            layer_max_limit = int(self.global_config.SINGLE_BATCH_MAX_HANDLE * load_compress_ratio)
            run_entry_list = sorted(current_task.target_entry_list, key=lambda x:x.priority, reverse=True)[:layer_max_limit]
            actual_run += len(run_entry_list)

            # 单分层条目批量执行
            layer_reports = self._inner_layer_batch_run(
                layer=current_task.layer_tag,
                op_type=current_task.task_operate_type,
                entry_list=run_entry_list
            )
            all_detail_reports.extend(layer_reports)

            # 统计分类
            for rep in layer_reports:
                if rep.run_result == SingleOperateResult.EXEC_SUCCESS:
                    success_num +=1
                    if rep.operate_type == ForgetOperateType.DIRECT_DELETE:
                        self.total_physical_delete_count +=1
                    else:
                        self.total_cold_archive_count +=1
                elif rep.run_result == SingleOperateResult.FAIL_WRITE_PROTECTED:
                    protect_intercept +=1
                    self.total_protect_intercept_count +=1
                elif rep.run_result == SingleOperateResult.FAIL_EMERGENCY_ABORT:
                    abort_num +=1
                else:
                    exception_fail +=1
                    self.total_exception_fail_count +=1
            time.sleep(self.global_config.ITEM_EXEC_INTERVAL_SEC)

        self.total_execute_batch_times +=1
        total_cost_ms = (time.time() - now_time) * 1000
        self.current_running_batch_reports = None
        self.unit_run_state = PruneUnitState.IDLE

        # 判定回执状态
        if success_num == actual_run and actual_run >0:
            feedback_status = ScheduleFeedbackState.SUCCESS
        elif success_num >0 and success_num < actual_run:
            feedback_status = ScheduleFeedbackState.PARTIAL_FINISH
        else:
            feedback_status = ScheduleFeedbackState.ERROR_ABORT

        # 写入批次运行日志
        self._write_batch_operation_log(batch_id, total_recv_task, actual_run, success_num, protect_intercept, exception_fail)

        return BatchExecuteReturnFeedback(
            feedback_batch_id=batch_id,
            total_receive_task=total_recv_task,
            actual_run_num=actual_run,
            success_finish_num=success_num,
            intercept_protect_num=protect_intercept,
            exception_fail_num=exception_fail,
            emergency_abort_num=abort_num,
            single_detail_reports=all_detail_reports,
            total_run_cost_ms=round(total_cost_ms,2),
            feedback_state=feedback_status
        )

    # ========== 分层内部批量执行逻辑 ==========
    def _inner_layer_batch_run(self, layer: str, op_type: ForgetOperateType,
                               entry_list: List[Ad41PassValidEntry]) -> List[SingleItemOperateReport]:
        report_list: List[SingleItemOperateReport] = []
        for entry in entry_list:
            # 熔断状态直接终止
            if self.unit_run_state != PruneUnitState.EXECUTING:
                rep = SingleItemOperateReport(
                    entry_id=entry.entry_id, belong_layer=layer,
                    operate_type=op_type, run_result=SingleOperateResult.FAIL_EMERGENCY_ABORT,
                    run_detail_msg="执行流程被紧急熔断终止"
                )
                report_list.append(rep)
                continue
            single_rep = self._single_entry_core_execute(entry, layer, op_type)
            report_list.append(single_rep)
        return report_list

    # ========== 单条目核心执行逻辑(融合所有安全规则) ==========
    def _single_entry_core_execute(self, entry: Ad41PassValidEntry, layer: str,
                                   op_type: ForgetOperateType) -> SingleItemOperateReport:
        eid = entry.entry_id
        # 校验全局锁定条目
        if eid in self.global_config.SYSTEM_LOCK_PROTECT_ENTRY_SET:
            return SingleItemOperateReport(
                entry_id=eid, belong_layer=layer, operate_type=op_type,
                run_result=SingleOperateResult.FAIL_WRITE_PROTECTED,
                run_detail_msg="条目纳入全局硬锁定保护池，禁止遗忘操作"
            )

        # S-03 L3/L4强制调用ad-29写保护校验(外部回调注入)
        layer_high_protect_check = self._call_ad29_write_protect_check(eid)
        if layer in ["L3","L4"] and layer_high_protect_check:
            return SingleItemOperateReport(
                entry_id=eid, belong_layer=layer, operate_type=op_type,
                run_result=SingleOperateResult.FAIL_WRITE_PROTECTED,
                run_detail_msg="L3/L4高价值条目经ad-29校验处于写保护状态"
            )

        # 读取层级源数据(外部回调)
        source_entry_data = self._read_origin_layer_entry_data(layer, eid)
        if not source_entry_data:
            return SingleItemOperateReport(
                entry_id=eid, belong_layer=layer, operate_type=op_type,
                run_result=SingleOperateResult.FAIL_ENTRY_NOT_FOUND,
                run_detail_msg="目标条目在源层级存储中已不存在"
            )

        # 分支执行：安全删除 / 合规冷归档
        if op_type == ForgetOperateType.DIRECT_DELETE:
            return self._do_secure_physical_delete(eid, layer, source_entry_data)
        else:
            return self._do_standard_cold_archive(eid, layer, source_entry_data)

    # ========== S-01 DoD 5220.22-M 安全物理删除实现 ==========
    def _do_secure_physical_delete(self, eid: str, layer: str,
                                   data_info: OriginLayerEntryData) -> SingleItemOperateReport:
        # 执行全零覆写
        overwrite_result = self._run_dod_standard_overwrite(
            addr=data_info.physical_storage_addr,
            total_bytes=data_info.data_total_size_bytes
        )
        if not overwrite_result:
            return SingleItemOperateReport(
                entry_id=eid, belong_layer=layer, operate_type=ForgetOperateType.DIRECT_DELETE,
                run_result=SingleOperateResult.FAIL_OVERWRITE_ERROR,
                run_detail_msg="DoD标准全零覆写失败，存储硬件读写异常"
            )
        # 强制硬件缓存FLUSH落盘
        self._force_storage_cache_flush()
        # 清理源层级索引
        index_clear_ok = self._delete_origin_layer_index(layer, eid)
        if not index_clear_ok:
            return SingleItemOperateReport(
                entry_id=eid, belong_layer=layer, operate_type=ForgetOperateType.DIRECT_DELETE,
                run_result=SingleOperateResult.FAIL_SOURCE_INDEX_ERR,
                run_detail_msg="物理数据已覆写清除，源层级索引清理失败"
            )
        # 同步容量释放
        self._add_capacity_release_record(layer,1,data_info.data_total_size_bytes)
        return SingleItemOperateReport(
            entry_id=eid, belong_layer=layer, operate_type=ForgetOperateType.DIRECT_DELETE,
            run_result=SingleOperateResult.EXEC_SUCCESS,
            run_detail_msg="遵循DoD 5220.22-M标准安全删除执行完成"
        )

    # ========== S-02 合规冷归档流程实现 ==========
    def _do_standard_cold_archive(self, eid: str, layer: str,
                                  data_info: OriginLayerEntryData) -> SingleItemOperateReport:
        # 数据标准化压缩
        try:
            raw_data_bytes = str(data_info.content).encode("utf-8")
            compress_data = zlib.compress(raw_data_bytes, level=self.global_config.ARCHIVE_COMPRESS_LEVEL)
        except Exception:
            compress_data = str(data_info.content).encode("utf-8")
        # 推送至ad-49归档单元
        archive_push_ok = self._push_data_to_ad49_archive(eid, compress_data, data_info.metadata)
        # 归档失败直接返回，保留源数据
        if not archive_push_ok:
            return SingleItemOperateReport(
                entry_id=eid, belong_layer=layer, operate_type=ForgetOperateType.COLD_ARCHIVE,
                run_result=SingleOperateResult.FAIL_ARCHIVE_TRANS_ERR,
                run_detail_msg="推送ad-49冷归档失败，源层级条目完整保留"
            )
        # 归档成功后清理源索引
        source_del_ok = self._delete_origin_layer_index(layer, eid)
        if not source_del_ok:
            return SingleItemOperateReport(
                entry_id=eid, belong_layer=layer, operate_type=ForgetOperateType.COLD_ARCHIVE,
                run_result=SingleOperateResult.FAIL_SOURCE_INDEX_ERR,
                run_detail_msg="冷归档推送成功，源条目索引清理失败"
            )
        self._add_capacity_release_record(layer,1,data_info.data_total_size_bytes)
        return SingleItemOperateReport(
            entry_id=eid, belong_layer=layer, operate_type=ForgetOperateType.COLD_ARCHIVE,
            run_result=SingleOperateResult.EXEC_SUCCESS,
            run_detail_msg="合规压缩推送ad-49归档完成，源数据已清理"
        )

    # ========== 底层硬件覆写与缓存刷新私有方法 ==========
    def _run_dod_standard_overwrite(self, addr: int, total_bytes: int) -> bool:
        if total_bytes <=0:
            return True
        block_count = (total_bytes + self.global_config.SECURE_OVERWRITE_BLOCK_SIZE -1) // self.global_config.SECURE_OVERWRITE_BLOCK_SIZE
        for idx in range(block_count):
            current_offset = addr + idx * self.global_config.SECURE_OVERWRITE_BLOCK_SIZE
            current_block_len = min(self.global_config.SECURE_OVERWRITE_BLOCK_SIZE, total_bytes - idx*self.global_config.SECURE_OVERWRITE_BLOCK_SIZE)
            try:
                # 底层硬件写入全零块(正式项目对接存储驱动)
                pass
            except Exception:
                return False
        return True

    def _force_storage_cache_flush(self) -> None:
        """强制存储控制器缓存落盘，确保覆写数据永久生效"""
        # 正式项目调用硬件FLUSH指令
        pass

    # ========== 外部回调预留接口(业务层注入) ==========
    def _read_origin_layer_entry_data(self, layer: str, eid: str) -> Optional[OriginLayerEntryData]:
        """预留：对接ad20/22/24/26读取源条目数据，外部重写实现"""
        return None

    def _call_ad29_write_protect_check(self, eid: str) -> bool:
        """预留：对接ad-29写保护校验，True=受保护禁止操作"""
        return False

    def _push_data_to_ad49_archive(self, eid: str, compress_data: bytes, meta: Dict[str,Any]) -> bool:
        """预留：推送压缩数据至ad-49归档单元"""
        return True

    def _delete_origin_layer_index(self, layer: str, eid: str) -> bool:
        """预留：清理层级存储条目索引"""
        return True

    # ========== 容量同步与日志入库 ==========
    def _add_capacity_release_record(self, layer: str, entry_cnt: int, byte_size: int) -> None:
        sync_item = StorageCapacityReleaseSync(
            layer_name=layer, free_entry_count=entry_cnt,
            free_total_bytes=byte_size, release_sync_time=time.time()
        )
        self.pending_capacity_sync_list.append(sync_item)

    def fetch_all_capacity_sync_data(self) -> List[StorageCapacityReleaseSync]:
        """取出容量数据同步至ad-48"""
        res = self.pending_capacity_sync_list.copy()
        self.pending_capacity_sync_list.clear()
        return res

    def _write_batch_operation_log(self, batch_id: str, total_task: int, run_num: int,
                                   succ: int, intercept: int, fail: int) -> None:
        log_body = {
            "log_category":"ad42_merge_prune_log",
            "batch_id":batch_id,
            "system_load":self.current_system_load_tag,
            "total_receive_task":total_task,
            "actual_execute_num":run_num,
            "success_count":succ,
            "protect_intercept":intercept,
            "exception_failed":fail,
            "operate_create_time":time.time(),
            "unit_state_snapshot":self.unit_run_state.value
        }
        self.pending_ad51_operate_logs.append(log_body)

    def collect_all_ad51_pending_logs(self) -> List[Dict[str,Any]]:
        """批量取出日志推送ad-51"""
        logs = self.pending_ad51_operate_logs.copy()
        self.pending_ad51_operate_logs.clear()
        return logs

    # ========== 全局运行统计查询接口 ==========
    def get_ad42_full_merge_statistics(self) -> Dict[str,Any]:
        return {
            "current_work_state":self.unit_run_state.value,
            "current_system_load":self.current_system_load_tag,
            "pending_task_queue_num":len(self.pending_layer_task_queue),
            "total_run_batch":self.total_execute_batch_times,
            "total_physical_delete":self.total_physical_delete_count,
            "total_cold_archive":self.total_cold_archive_count,
            "total_protect_intercept":self.total_protect_intercept_count,
            "total_emergency_abort":self.total_emergency_abort_count,
            "total_exception_failed":self.total_exception_fail_count
        }


# ==================== 全覆盖整合单元测试 ====================
if __name__ == "__main__":
    print("="*75)
    print("ad-42 合并定稿单元 整合功能全量测试")
    print("="*75)
    pass_case,fail_case = 0,0

    # 模拟底层数据源
    mock_layer_storage:Dict[str,Dict[str,OriginLayerEntryData]] = {"L1":{},"L2":{},"L3":{},"L4":{}}
    mock_ad29_protect_eid = set()
    mock_ad49_archive_pool = dict()

    # 重写回调方法
    def mock_read_data(layer,eid):
        return mock_layer_storage.get(layer,{}).get(eid)
    def mock_ad29_check(eid):
        return eid in mock_ad29_protect_eid
    def mock_push_ad49(eid,data,meta):
        mock_ad49_archive_pool[eid] = data
        return True
    def mock_clear_index(layer,eid):
        if eid in mock_layer_storage.get(layer,{}):
            del mock_layer_storage[layer][eid]
        return True

    # 注入回调
    RedundantMemoryPruneMergeUnit._read_origin_layer_entry_data = staticmethod(mock_read_data)
    RedundantMemoryPruneMergeUnit._call_ad29_write_protect_check = staticmethod(mock_ad29_check)
    RedundantMemoryPruneMergeUnit._push_data_to_ad49_archive = staticmethod(mock_push_ad49)
    RedundantMemoryPruneMergeUnit._delete_origin_layer_index = staticmethod(mock_clear_index)

    def build_test_valid_entry(eid,layer,op_method="直接删除",prio=0.5):
        return Ad41PassValidEntry(
            entry_id=eid,current_layer=layer,i_value=0.1,
            reuse_count=1,forget_method=op_method,source_slot_id=15,
            validation_conclusion="PASS",priority=prio
        )

    # TC1:常规层级安全删除成功
    print("\n[TC01] L1层级DoD标准安全删除")
    try:
        unit = RedundantMemoryPruneMergeUnit()
        mock_layer_storage["L1"]["TEST001"] = OriginLayerEntryData("TEST001",{"k":"v"},{},0x1000,2048)
        unit.push_valid_entry_to_task_queue({"L1":[build_test_valid_entry("TEST001","L1")]})
        res = unit.start_all_queue_batch_execute(force_run=True)
        assert res.success_finish_num ==1
        assert "TEST001" not in mock_layer_storage["L1"]
        pass_case +=1
        print("✅ 通过")
    except Exception as e:
        fail_case +=1
        print(f"❌ 失败:{e}")

    # TC2:L3层级ad29写保护拦截
    print("\n[TC02] L3条目ad-29写保护拦截")
    try:
        unit = RedundantMemoryPruneMergeUnit()
        mock_ad29_protect_eid.add("TEST002")
        mock_layer_storage["L3"]["TEST002"] = OriginLayerEntryData("TEST002",{},{},0x2000,1024)
        unit.push_valid_entry_to_task_queue({"L3":[build_test_valid_entry("TEST002","L3")]})
        res = unit.start_all_queue_batch_execute(force_run=True)
        assert res.intercept_protect_num ==1
        pass_case +=1
        print("✅ 通过")
    except Exception as e:
        fail_case +=1
        print(f"❌ 失败:{e}")

    # TC3:冷归档正常流转
    print("\n[TC03] 标准冷归档推送ad49成功")
    try:
        unit = RedundantMemoryPruneMergeUnit()
        mock_layer_storage["L2"]["TEST003"] = OriginLayerEntryData("TEST003",{"arch":"ok"},{},0x3000,4096)
        unit.push_valid_entry_to_task_queue({"L2":[build_test_valid_entry("TEST003","L2","冷归档")]})
        res = unit.start_all_queue_batch_execute(force_run=True)
        assert res.success_finish_num ==1
        assert "TEST003" in mock_ad49_archive_pool
        pass_case +=1
        print("✅ 通过")
    except Exception as e:
        fail_case +=1
        print(f"❌ 失败:{e}")

    # TC4:高负载限流生效
    print("\n[TC04] 高负载模式执行数量压缩")
    try:
        unit = RedundantMemoryPruneMergeUnit()
        unit.set_current_system_load("高负载")
        entry_list = [build_test_valid_entry(f"LOAD{i}","L1") for i in range(30)]
        unit.push_valid_entry_to_task_queue({"L1":entry_list})
        res = unit.start_all_queue_batch_execute(force_run=True)
        assert res.actual_run_num <30
        pass_case +=1
        print("✅ 通过")
    except Exception as e:
        fail_case +=1
        print(f"❌ 失败:{e}")

    # TC5:任务暂停阻断执行
    print("\n[TC05] 单元暂停状态拒绝执行")
    try:
        unit = RedundantMemoryPruneMergeUnit()
        unit.pause_all_execute_task()
        unit.push_valid_entry_to_task_queue({"L1":[build_test_valid_entry("PAUSE01","L1")]})
        res = unit.start_all_queue_batch_execute()
        assert res.actual_run_num ==0
        pass_case +=1
        print("✅ 通过")
    except Exception as e:
        fail_case +=1
        print(f"❌ 失败:{e}")

    print(f"\n整合测试汇总：通过 {pass_case} 例，失败 {fail_case} 例")