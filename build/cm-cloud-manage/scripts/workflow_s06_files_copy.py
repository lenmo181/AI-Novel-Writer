# S06 批量复制：把已授权个人云资源复制到用户指定的目录。
# 流程：源/目标核验 -> 可信确认 -> 异步复制 -> 任务轮询 -> 逐项汇总。
# CLI: cmcloud files copy --format json

from cmcloud_core import result as result_module
from cmcloud_adapters import idaas_file_batch_copy_async, task_get
from cmcloud_workflows import common

# task_get 状态枚举已冻结（官方文档 + 2026-09-18 现网样本）：
# Running 为非终态，Succeed/PartialSucceed/Failed 为终态。
TERMINAL_STATUSES = common.FILE_TASK_TERMINAL_STATUSES


def prepare(context, input_data):
    destination_id, destination = common.resolve_dedicated_write_destination(
        context, input_data["destinationRef"]
    )
    sources = []
    preflight_failures = []
    for item_index, token in enumerate(input_data["resources"]):
        ref_error = common.preloaded_ref_error(context, "resources", item_index)
        if ref_error is not None:
            preflight_failures.append(_failed_item(item_index, ref_error))
            continue
        file_id = common.real_id_for_kinds(context, token, ("file", "folder", "resource"), "resources")
        try:
            node = common.ensure_source(context, file_id)
        except result_module.ExecutionError as err:
            if _is_request_level_error(err):
                raise
            preflight_failures.append(_failed_item(item_index, err))
            continue
        sources.append(
            {
                "fileRef": token,
                "fileId": file_id,
                "itemIndex": item_index,
                "name": node.get("name"),
            }
        )
    if not sources:
        counts = {"requested": len(input_data["resources"]), "succeeded": 0, "failed": len(preflight_failures)}
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "没有可复制的有效资源",
            data={"results": preflight_failures},
            counts=counts,
        )
    return {
        "summary": "把 %d 个已授权资源复制到目标目录：%s"
        % (len(sources), destination["path"]),
        "resolvedIds": {
            "fileIds": [source["fileId"] for source in sources],
            "toParentFileId": destination_id,
        },
        "sources": sources,
        "requested": len(input_data["resources"]),
        "preflightFailures": preflight_failures,
        "destinationRef": input_data["destinationRef"],
        "destination": destination,
    }


def execute(context, plan):
    payload = common.guard_upstream(
        context,
        idaas_file_batch_copy_async.call,
        file_ids=plan["resolvedIds"]["fileIds"],
        to_parent_file_id=plan["resolvedIds"]["toParentFileId"],
    )
    data = common.payload_data(context, payload)
    task_id = common.field_text(context, data, "taskId")
    context.bind_task(task_id, task_type=common.TASK_TYPE_FILE_COPY)
    context.note_step("submit", "已提交异步复制任务", task_ref=task_id)
    return _status_outcome(context, plan, task_id)


def query_status(context, ledger_entry):
    task_id = _task_ref(ledger_entry)
    plan = ledger_entry.get("plan") if isinstance(ledger_entry.get("plan"), dict) else {}
    return _status_outcome(context, plan, task_id)


def _status_outcome(context, plan, task_id):
    state = common.poll_task(
        context,
        task_get.call,
        is_terminal=_is_terminal,
        state_of=_task_state,
        extra_kwargs={"task_id": task_id},
        max_calls=common.POLL_MAX_CALLS,
    )
    data = {"taskRef": task_id, "taskState": state.get("status")}
    if isinstance(plan.get("destination"), dict):
        data["destination"] = dict(plan["destination"])
    results = _results_for_state(plan, state)
    data["results"] = results
    counts = _result_counts(plan, results)
    if state.get("timedOut"):
        # 轮询预算耗尽：本轮等待结束（不是失败）；可按 nextAction 经 operation.status 续查一次。
        return common.running(
            data=data,
            counts=counts,
            next_action=result_module.NEXT_ACTION_OPERATION_STATUS,
        )
    if state.get("status") == "Failed":
        raise result_module.ExecutionError(
            result_module.FAILED, "复制任务执行失败", data=data, counts=counts
        )
    succeeded = counts.get("succeeded") or 0
    failed = counts.get("failed") or 0
    unknown_count = counts.get("unknown") or 0
    if unknown_count:
        # 任务已结束但逐项结果不完整：未知优先，不据此报成功或部分成功。
        return common.unknown(
            data=data,
            counts=counts,
            message="复制任务已结束，但部分逐项结果无法判定",
        )
    if succeeded and failed:
        return common.partial(data=data, counts=counts)
    if failed:
        raise result_module.ExecutionError(
            result_module.FAILED, "复制任务执行失败", data=data, counts=counts
        )
    if succeeded:
        return common.success(data=data, counts=counts)
    return common.unknown(data=data, counts=counts, message="复制任务已结束，但未返回逐项结果")


def _mapped_results(plan, results):
    """把 batchFileResults 折算为受控引用结果，绝不外泄下游真实 fileId。"""
    if not isinstance(results, list):
        return None
    sources = plan.get("sources") if isinstance(plan.get("sources"), list) else []
    sources_of = {
        source.get("fileId"): source
        for source in sources
        if isinstance(source, dict)
    }
    mapped = []
    for item in results:
        if not isinstance(item, dict):
            continue
        src_file = item.get("srcFile")
        file_id = src_file.get("fileId") if isinstance(src_file, dict) else None
        source = sources_of.get(file_id)
        if not isinstance(source, dict):
            continue
        succeeded = item.get("errCode") == "0000"
        mapped.append(
            {
                "itemIndex": source.get("itemIndex"),
                "fileRef": source.get("fileRef"),
                "name": source.get("name"),
                "status": "SUCCEEDED" if succeeded else "FAILED",
                **({} if succeeded else {"errorCode": result_module.FAILED, "reason": "复制失败"}),
            }
        )
    return mapped or None


def _results_for_state(plan, state):
    failures = list(plan.get("preflightFailures") or ())
    sources = plan.get("sources") if isinstance(plan.get("sources"), list) else []
    task_status = state.get("status")
    mapped = _mapped_results(plan, state.get("results"))
    if task_status == "Succeed":
        task_items = [_source_item(source, "SUCCEEDED") for source in sources]
    elif task_status == "Failed":
        task_items = [_source_item(source, "FAILED", "复制任务执行失败") for source in sources]
    elif mapped is not None:
        task_items = mapped
        mapped_indexes = {item.get("itemIndex") for item in mapped}
        task_items.extend(
            _source_item(source, "UNKNOWN", "平台未返回该项结果")
            for source in sources
            if source.get("itemIndex") not in mapped_indexes
        )
    else:
        pending_status = "RUNNING" if state.get("timedOut") else "UNKNOWN"
        task_items = [_source_item(source, pending_status, "复制结果尚未确认") for source in sources]
    combined = failures + task_items
    combined.sort(key=lambda item: item.get("itemIndex", 0))
    return combined


def _source_item(source, status, reason=None):
    item = {
        "itemIndex": source.get("itemIndex"),
        "fileRef": source.get("fileRef"),
        "name": source.get("name"),
        "status": status,
    }
    if reason is not None:
        item["errorCode"] = result_module.FAILED if status == "FAILED" else result_module.UNKNOWN
        item["reason"] = reason
    return item


def _result_counts(plan, results):
    return {
        "requested": plan.get("requested", len(results)),
        "submitted": _submitted(plan),
        "succeeded": sum(item.get("status") == "SUCCEEDED" for item in results),
        "failed": sum(item.get("status") == "FAILED" for item in results),
        "unknown": sum(item.get("status") in ("RUNNING", "UNKNOWN") for item in results),
    }


def _is_request_level_error(error):
    return error.code in (
        result_module.AUTH_REQUIRED,
        result_module.AUTH_EXPIRED,
        result_module.SCOPE_DENIED,
        result_module.UPSTREAM_CONTRACT_MISMATCH,
    )


def _failed_item(item_index, error):
    return {
        "itemIndex": item_index,
        "name": "第%d项" % (item_index + 1),
        "status": "FAILED",
        "errorCode": error.code,
        "reason": common.batch_resource_failure_reason(error),
    }


def _submitted(plan):
    resolved = plan.get("resolvedIds") if isinstance(plan.get("resolvedIds"), dict) else {}
    file_ids = resolved.get("fileIds")
    return len(file_ids) if isinstance(file_ids, list) else None


def _task_ref(ledger_entry):
    task_ref = ledger_entry.get("taskRef")
    if not isinstance(task_ref, str) or not task_ref:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "台账中缺少下游 taskId")
    return task_ref


def _task_state(context, payload):
    data = common.payload_data(context, payload)
    info = data.get("taskInfo")
    if not isinstance(info, dict):
        common.contract_mismatch(context, "任务查询响应缺少 taskInfo 对象")
    return {
        "taskId": common.field_text(context, info, "taskId"),
        "status": info.get("status"),
        "progress": info.get("progress"),
        "results": data.get("batchFileResults"),
    }


def _is_terminal(state):
    return state.get("status") in TERMINAL_STATUSES
