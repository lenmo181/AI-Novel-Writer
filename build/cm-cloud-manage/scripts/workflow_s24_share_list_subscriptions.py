# S24 查询订阅外链：按关键词、类型和时间查询订阅记录。
# 流程：条件映射 -> 查询订阅搜索 -> 保存 pageAfter -> 结果归一化。
# CLI: cmcloud share list --format json

from cmcloud_core import result as result_module
from cmcloud_adapters import idaas_search_subscribe_share_code
from cmcloud_workflows import common

# 判别字段由命令 Schema 固定；此处再次校验，防止被注册表以外的入口绕过。
LIST_KIND = "subscriptions"


def run(context, input_data):
    if input_data.get("listKind") != LIST_KIND:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "listKind 必须为 %s" % LIST_KIND
        )
    page_size = input_data.get("pageSize") or context.default_page_size()
    conditions = _conditions(input_data)
    show_info = {"pageSize": page_size}
    digest = common.conditions_digest({"conditions": conditions, "pageSize": page_size})
    if input_data.get("cursor"):
        state = context.resolve_cursor(input_data["cursor"], digest)
        page_after = state["upstreamState"].get("pageAfter")
        if isinstance(page_after, list) and page_after:
            show_info["pageAfter"] = list(page_after)
    payload = common.guard_upstream(
        context, idaas_search_subscribe_share_code.call, conditions=conditions, show_info=show_info
    )
    data = common.payload_data(context, payload, required=False) or {}
    rows = data.get("rows")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        common.contract_mismatch(context, "订阅搜索响应 rows 必须是数组")
    total = data.get("total")
    total = total if isinstance(total, int) and not isinstance(total, bool) else None
    page_after = data.get("pageAfter")
    has_cursor = isinstance(page_after, list) and bool(page_after)
    exhausted = common.page_exhausted(page_size, len(rows), has_cursor)
    cursor_token = None
    if has_cursor and not exhausted:
        cursor_token = context.issue_cursor(digest, {"pageAfter": list(page_after)}, complete=False)
    page = common.page_info(
        page_size,
        len(rows),
        total=total,
        has_more=bool(cursor_token),
        next_cursor=cursor_token,
        complete=exhausted,
    )
    result_data = {"listKind": LIST_KIND, "items": [_subscription_item(context, row) for row in rows]}
    if not rows:
        return common.empty(data=result_data, page=page)
    return common.success(data=result_data, counts={"returned": len(rows)}, page=page)


def _subscription_item(context, row):
    if not isinstance(row, dict):
        common.contract_mismatch(context, "订阅搜索响应行必须是对象")
    cleaned = common.strip_sensitive(row)
    # 平台订阅记录只返回 shareId（与分享链接内的 linkId 同一取值空间，2026-09-18 实测），
    # 无 linkId/linkID 字段；三种键都兼容，取到即签发 linkRef，供 share updates/browse 承接。
    link_id = row.get("linkId") or row.get("linkID") or row.get("shareId")
    for key in ("linkId", "linkID", "shareId"):
        cleaned.pop(key, None)
    if isinstance(link_id, str) and link_id:
        cleaned["linkRef"] = context.issue_ref("link", link_id, common.SCOPE_OUTER_LINK)
    return cleaned


def _conditions(input_data):
    conditions = {}
    if input_data.get("keyword"):
        conditions["keyword"] = input_data["keyword"]
    if input_data.get("typeList"):
        conditions["typeList"] = list(input_data["typeList"])
    if input_data.get("from") or input_data.get("to"):
        # 契约 SubscribeShareCodeTimeRangeDTO 的 startTime/endTime 均为必填；Schema 已要求
        # from/to 成对，这里再校验一次，防止经注册表以外的入口拼出半开区间。
        if not (input_data.get("from") and input_data.get("to")):
            raise result_module.ExecutionError(
                result_module.INVALID_ARGUMENT, "from 与 to 必须成对提供"
            )
        conditions["timeList"] = [
            {
                "filed": input_data.get("timeField") or "shareTime",
                "startTime": common.idaas_time_text(input_data["from"]),
                "endTime": common.idaas_time_text(input_data["to"]),
            }
        ]
    return conditions
