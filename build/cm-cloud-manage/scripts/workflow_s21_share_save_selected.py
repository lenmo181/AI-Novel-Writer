# S21 转存指定外链资源：把指定文件或目录转存到 AI 空间目标目录。
# 流程：浏览并核验外链资产 -> 目标核验 -> 可信确认 -> 创建任务 -> 轮询结果。
# CLI: cmcloud share save --format json

from cmcloud_core import result as result_module
from cmcloud_adapters import idaas_create_outer_link_batch_task_v2, idaas_query_batch_task_detail
from cmcloud_workflows import common

# 判别字段由命令 Schema 固定；此处再次校验，防止被注册表以外的入口绕过。
SAVE_KIND = "selected-resources"

# 创建任务契约已冻结；任务详情按 contentList/catalogList 的 rstId/reason 逐项核算结果。
TASK_CONTRACT_FROZEN = True
# queryBatchOprTaskDetail 契约中 taskStatus=2 表示任务结束；是否成功仍以逐项结果为准。
TERMINAL_STATUSES = frozenset((2,))


def prepare(context, input_data):
    if input_data.get("saveKind") != SAVE_KIND:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "saveKind 必须为 %s" % SAVE_KIND
        )
    if not TASK_CONTRACT_FROZEN:
        raise result_module.ExecutionError(
            result_module.CAPABILITY_UNAVAILABLE, "XML V2 任务契约与外链资产归属核验未验收"
        )
    link_id = context.real_id(input_data["linkRef"], "link")
    destination_id, destination = common.resolve_dedicated_write_destination(
        context, input_data.get("destinationRef")
    )
    content_ids = [context.ref_entry(token)["realId"] for token in input_data.get("contentRefs", [])]
    catalog_ids = [context.ref_entry(token)["realId"] for token in input_data.get("catalogRefs", [])]
    for token in list(input_data.get("contentRefs", [])) + list(input_data.get("catalogRefs", [])):
        _assert_link_asset(context, link_id, token)
    target_label = "默认专属目录" if destination["selection"] == "default" else "指定目录"
    return {
        "summary": "转存外链 %d 个文件与 %d 个目录到%s：%s"
        % (len(content_ids), len(catalog_ids), target_label, destination["path"]),
        "resolvedIds": {
            "linkId": link_id,
            "newCatalogId": destination_id,
            "contentIds": content_ids,
            "catalogIds": catalog_ids,
        },
        "linkRef": input_data["linkRef"],
        "saveKind": SAVE_KIND,
        "destination": destination,
    }


def execute(context, plan):
    payload = common.guard_upstream(
        context,
        idaas_create_outer_link_batch_task_v2.call,
        task_type=_task_type(),
        link_id=plan["resolvedIds"]["linkId"],
        content_info_list=plan["resolvedIds"]["contentIds"] or None,
        catalog_info_list=plan["resolvedIds"]["catalogIds"] or None,
        new_catalog_id=plan["resolvedIds"]["newCatalogId"],
    )
    data = common.payload_data(context, payload)
    task_id = common.field_text(context, data, "taskId")
    context.bind_task(task_id, task_type=common.TASK_TYPE_OUTER_LINK_V2)
    context.note_step("submit", "已提交批量转存任务", task_ref=task_id)
    return _status_outcome(context, plan, task_id)


def query_status(context, ledger_entry):
    task_id = _task_ref(ledger_entry)
    plan = ledger_entry.get("plan") if isinstance(ledger_entry.get("plan"), dict) else {}
    return _status_outcome(context, plan, task_id)


def _status_outcome(context, plan, task_id):
    resolved_ids = plan.get("resolvedIds") if isinstance(plan.get("resolvedIds"), dict) else {}
    return common.outer_link_task_outcome(
        context, idaas_query_batch_task_detail.call, task_id, resolved_ids,
        destination=plan.get("destination"), is_terminal=_batch_terminal,
        max_calls=common.POLL_MAX_CALLS if TERMINAL_STATUSES else 1,
        timeout_next_action=result_module.NEXT_ACTION_OPERATION_STATUS,
    )


def _batch_terminal(state):
    if state.get("confirmedStatus") == "CONFIRMED_FAILURE":
        return True
    return state.get("taskStatus") in TERMINAL_STATUSES


def _task_ref(ledger_entry):
    task_ref = ledger_entry.get("taskRef")
    if not isinstance(task_ref, str) or not task_ref:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "台账中缺少下游 taskId")
    return task_ref


def _assert_link_asset(context, link_id, token):
    """只接受 S25 浏览该外链时签发的资源引用。

    密码保护外链不能在 S21 中无密码重放浏览请求；因此 S25 在受控引用台账中
    固化 parentRealId，S21 用该绑定关系完成归属校验。
    """
    entry = context.ref_entry(token)
    if entry.get("sourceCommand") != "share.browse" or entry.get("scope") != common.SCOPE_OUTER_LINK:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "所选资源不是由外链浏览命令签发"
        )
    if entry.get("parentRealId") != link_id:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "所选资源不属于当前外链"
        )


def _task_type():
    return "1"
