# S20 转存订阅更新：把选定的订阅更新资源转存到 AI 空间目标目录。
# 流程：外链/资产/目标核验 -> 可信确认 -> 创建 XML V2 任务 -> 轮询任务结果。
# CLI: cmcloud share save --format json

from cmcloud_core import result as result_module
from cmcloud_adapters import idaas_create_outer_link_batch_task_v2, idaas_query_batch_task_detail
from cmcloud_workflows import common

# 判别字段由命令 Schema 固定；此处再次校验，防止被注册表以外的入口绕过。
SAVE_KIND = "subscription-updates"

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
            result_module.CAPABILITY_UNAVAILABLE, "XML V2 任务契约未验收，转存订阅更新不可提交"
        )
    link_id = context.real_id(input_data["linkRef"], "link")
    destination_id, destination = common.resolve_dedicated_write_destination(
        context, input_data.get("destinationRef")
    )
    content_ids, catalog_ids = _split_assets(context, input_data["selectedResources"])
    target_label = "默认专属目录" if destination["selection"] == "default" else "指定目录"
    return {
        "summary": "把 %d 项订阅更新转存到%s：%s"
        % (len(input_data["selectedResources"]), target_label, destination["path"]),
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


def _task_type():
    return "1"


def _split_assets(context, refs):
    """按 CLI 自有引用类型拆分下游 content/catalog 列表。

    `selectedResources` 在 Schema 中声明为通用 `resource` 引用（CLI 签发的资源型引用），
    已显式标注为 `content`/`catalog` 的引用按标注分类；通用 `resource` 按文件型资产处理，
    下游真正的资产分类规则在契约冻结前不生效（本场景 capability 保持关闭）。
    """
    content_ids = []
    catalog_ids = []
    for token in refs:
        entry = context.ref_entry(token)
        if entry["kind"] in ("content", "resource"):
            content_ids.append(entry["realId"])
        elif entry["kind"] == "catalog":
            catalog_ids.append(entry["realId"])
        else:
            common.contract_mismatch(context, "选中资源引用类型非法")
    return content_ids, catalog_ids
