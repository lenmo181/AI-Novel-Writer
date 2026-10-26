# S22 查询原操作状态：按 operationRef 恢复台账任务并折算为顶层状态。
# 流程：读取任务台账 -> 可信终态短路 -> 查询既有任务 -> 终态回写台账 -> 输出。
# CLI: cmcloud operation status --operation-ref <operationRef>

from cmcloud_core import operation_ledger as ledger_module
from cmcloud_core import result as result_module
from cmcloud_adapters import idaas_photo_organize_task_query, idaas_query_batch_task_detail, task_get
from cmcloud_workflows import common

# queryBatchOprTaskDetail 契约中 taskStatus=2 表示任务结束；任务成败按逐项 rstId/reason 核算。
# confirmedStatus=CONFIRMED_FAILURE 仍为接口级终局失败。
OUTER_LINK_TERMINAL_STATUSES = frozenset((2,))
FILE_TERMINAL_STATUSES = common.FILE_TASK_TERMINAL_STATUSES

# 台账已处于可信终态时直接折算返回，不重复调用下游。
TERMINAL_OPERATION_STATUSES = frozenset((
    ledger_module.OPERATION_EXECUTED,
    ledger_module.OPERATION_PARTIAL,
    ledger_module.OPERATION_FAILED,
    ledger_module.OPERATION_REJECTED,
))

# V1.8 已移除的旧场景：其历史 operationRef 不执行、不映射到其它场景、不调用下游接口。
REMOVED_COMMAND_IDS = ("share.send", "album.send-contact")
REMOVED_SCENARIOS = ("S37", "S17")


def run(context, input_data):
    # operation-ref 是 CLI 写操作直接返回的台账键（cmop_*），不是 cmr_* 资源引用。
    # 字段名用 kebab，与 S22 Schema 及分派层接受的 --operation-ref 保持一致。
    operation_ref = input_data["operation-ref"]
    record = context.ledger.get_operation(operation_ref, context.profile)
    if record.get("scenarioId") == _orchestration_scenario_id():
        from cmcloud_core import orchestrator

        return orchestrator.query_frozen_status(context, record)
    return _query(context, record)


def _orchestration_scenario_id():
    # registry 在加载 workflow 模块时尚未初始化完成，此处延迟导入。
    from cmcloud_core import registry

    return registry.ORCHESTRATION_SCENARIO_ID


def _query(context, record):
    command_id = record.get("commandId")
    scenario_id = record.get("scenarioId")
    if command_id in REMOVED_COMMAND_IDS or scenario_id in REMOVED_SCENARIOS:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "该 operationRef 指向已移除的旧场景，计划不可执行且不会调用任何下游接口",
        )
    operation_status = record.get("status")
    last_result = record.get("lastResult") if isinstance(record.get("lastResult"), dict) else {}
    data = {
        "operationRef": record.get("operationRef"),
        "operationStatus": operation_status,
        "taskRef": _task_ref(record),
        "taskStatus": last_result.get("taskStatus"),
    }
    plan_destination = _plan_of(record).get("destination")
    if isinstance(plan_destination, dict):
        data["destination"] = dict(plan_destination)
    for key in ("results", "unknownCount", "savedDirectories"):
        if key in last_result:
            data[key] = last_result.get(key)
    if operation_status == ledger_module.OPERATION_PENDING:
        # 计划已生成、等待可信确认：按状态机折算为 CONFIRMATION_REQUIRED。
        return common.Outcome(
            result_module.STATUS_CONFIRMATION_REQUIRED,
            data=data,
            next_action=result_module.NEXT_ACTION_OPERATION_CONFIRM,
        )
    if operation_status == ledger_module.OPERATION_CONFIRMED:
        return common.Outcome(
            result_module.STATUS_RUNNING,
            data=data,
            next_action=result_module.NEXT_ACTION_OPERATION_EXECUTE,
        )
    if operation_status in TERMINAL_OPERATION_STATUSES:
        # 已在可信终态：直接折算，不重复调用下游。
        return _terminal_outcome(operation_status, data, last_result)
    if data["taskRef"] is None:
        # 未登记下游任务：只返回台账中的可信状态，不调用下游、不重交写请求。
        return common.running(data=data)
    return _poll(context, record, data)


def _terminal_outcome(operation_status, data, last_result):
    counts = _counts_of(last_result)
    if operation_status == ledger_module.OPERATION_EXECUTED:
        return common.success(data=data, counts=counts)
    if operation_status == ledger_module.OPERATION_PARTIAL:
        return common.partial(data=data, counts=counts)
    if operation_status == ledger_module.OPERATION_REJECTED:
        return common.Outcome(
            result_module.STATUS_REJECTED,
            data=data,
            counts=counts,
            error=result_module.build_error(result_module.SCOPE_DENIED, "该操作已被拒绝"),
        )
    message = last_result.get("message")
    if not isinstance(message, str) or not message:
        message = "该操作已失败"
    raise result_module.ExecutionError(result_module.FAILED, message, data=data, counts=counts)


def _poll(context, record, data):
    task_type = record.get("taskType")
    if task_type == common.TASK_TYPE_OUTER_LINK_V2:
        return _poll_outer_link(context, record, data)
    if task_type == common.TASK_TYPE_FILE_COPY:
        return _poll_file_copy(context, record, data)
    if task_type == common.TASK_TYPE_PHOTO_ORGANIZE:
        return _poll_organize(context, record, data)
    raise result_module.ExecutionError(
        result_module.INVALID_ARGUMENT, "台账中的任务类型未登记，无法查询状态"
    )


def _poll_outer_link(context, record, data):
    resolved_ids = _plan_of(record).get("resolvedIds")
    if not isinstance(resolved_ids, dict):
        resolved_ids = {}
    try:
        outcome = common.outer_link_task_outcome(
            context, idaas_query_batch_task_detail.call, data["taskRef"], resolved_ids,
            destination=_plan_of(record).get("destination"),
            is_terminal=_outer_link_terminal, max_calls=common.POLL_MAX_CALLS,
        )
        data = outcome.data
    except result_module.ExecutionError as err:
        _write_back(
            context,
            record,
            ledger_module.OPERATION_FAILED,
            err.data if isinstance(err.data, dict) else data,
            err.counts,
            err.message,
        )
        raise
    if outcome.status == result_module.STATUS_PARTIAL:
        operation_status = ledger_module.OPERATION_PARTIAL
    elif outcome.status == result_module.STATUS_SUCCESS:
        operation_status = ledger_module.OPERATION_EXECUTED
    else:
        # 台账没有 UNKNOWN 终态；保留 EXECUTING，避免把无法判定的结果伪造成成功或失败。
        return outcome
    _write_back(context, record, operation_status, data, outcome.counts)
    return outcome


def _poll_file_copy(context, record, data):
    state = common.poll_task(
        context,
        task_get.call,
        is_terminal=lambda state: state.get("taskStatus") in FILE_TERMINAL_STATUSES,
        state_of=_file_task_state,
        extra_kwargs={"task_id": data["taskRef"]},
        max_calls=common.POLL_MAX_CALLS,
    )
    data["taskStatus"] = state.get("taskStatus")
    counts = common.file_task_counts(
        state.get("results"), _submitted(_plan_of(record).get("resolvedIds"))
    )
    if state.get("timedOut"):
        return common.running(data=data, counts=counts)
    if state.get("taskStatus") == "Failed":
        message = "复制任务执行失败"
        _write_back(context, record, ledger_module.OPERATION_FAILED, data, counts, message)
        raise result_module.ExecutionError(result_module.FAILED, message, data=data, counts=counts)
    if state.get("taskStatus") == "PartialSucceed":
        _write_back(context, record, ledger_module.OPERATION_PARTIAL, data, counts)
        return common.partial(data=data, counts=counts)
    _write_back(context, record, ledger_module.OPERATION_EXECUTED, data, counts)
    return common.success(data=data, counts=counts)


def _poll_organize(context, record, data):
    # 整理任务状态枚举未验收：单次只查原任务，无法判定终态时按状态未知输出。
    state = common.poll_task(
        context,
        idaas_photo_organize_task_query.call,
        is_terminal=lambda state: False,
        state_of=_organize_state,
        extra_kwargs={"task_id": data["taskRef"], "openclaw_id": _openclaw_id(context)},
        max_calls=1,
    )
    data["taskStatus"] = state.get("taskStatus")
    return common.unknown(data=data)


def _write_back(context, record, operation_status, data, counts, message=None):
    """把本次观测到的终态写回台账，后续 operation.status 不再重复查询下游。"""
    last_result = {"taskStatus": data.get("taskStatus")}
    if counts:
        last_result["counts"] = counts
    for key in (
        "results",
        "unknownCount",
        "savedDirectories",
        "destination",
        "resultCode",
        "taskResultCode",
        "progress",
    ):
        if key in data:
            last_result[key] = data.get(key)
    if message:
        last_result["code"] = result_module.FAILED
        last_result["message"] = message
    context.ledger.update_operation(
        record["operationRef"], status=operation_status, lastResult=last_result
    )


def _outer_link_terminal(state):
    if state.get("confirmedStatus") == "CONFIRMED_FAILURE":
        return True
    return state.get("taskStatus") in OUTER_LINK_TERMINAL_STATUSES


def _organize_state(context, payload):
    data = common.payload_data(context, payload)
    info = data.get("taskInfo")
    if not isinstance(info, dict):
        common.contract_mismatch(context, "整理任务查询响应缺少 taskInfo 对象")
    return {
        "taskId": common.field_text(context, info, "taskId"),
        "taskStatus": info.get("status"),
    }


def _file_task_state(context, payload):
    data = common.payload_data(context, payload)
    info = data.get("taskInfo")
    if not isinstance(info, dict):
        common.contract_mismatch(context, "文件任务查询响应缺少 taskInfo 对象")
    return {
        "taskId": common.field_text(context, info, "taskId"),
        "taskStatus": info.get("status"),
        "resultCode": info.get("code"),
        "progress": info.get("progress"),
        "results": data.get("batchFileResults"),
    }


def _task_ref(record):
    task_ref = record.get("taskRef")
    return task_ref if isinstance(task_ref, str) and task_ref else None


def _counts_of(last_result):
    counts = last_result.get("counts")
    return counts if isinstance(counts, dict) else None


def _plan_of(record):
    plan = record.get("plan")
    return plan if isinstance(plan, dict) else {}


def _submitted(resolved_ids):
    if not isinstance(resolved_ids, dict):
        return None
    file_ids = resolved_ids.get("fileIds")
    return len(file_ids) if isinstance(file_ids, list) else None


def _openclaw_id(context):
    return common.require_host_context(context, "openclawId")
