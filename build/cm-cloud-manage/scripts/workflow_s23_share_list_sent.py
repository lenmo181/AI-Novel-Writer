# S23 查询订阅外链分享记录：按 searchType=1 搜索该分区记录或分享内文件（见 SEARCH_TYPE_SENT 说明）。
# 流程：范围映射 -> 选择唯一搜索接口 -> 受控分页 -> 范围说明输出。
# CLI: cmcloud share list --format json

from cmcloud_core import result as result_module
from cmcloud_adapters import idaas_search_non_file_share_record, idaas_search_share_record
from cmcloud_workflows import common

# 判别字段由命令 Schema 固定；此处再次校验，防止被注册表以外的入口绕过。
LIST_KIND = "sent"

SCOPE_RECORDS = "records"
SCOPE_FILES = "files"

# 契约 conditions.searchType：不传＝只搜 AI 通讯录分享记录，"0"＝搜 AI 通讯录分享外链，
# "1"＝搜订阅的外链分享记录。
# 本场景固定用 "1"：平台把 CLI 账号可见的分享记录登记在订阅分区（2026-09-19 实测：同一条记录
# 用 "1" 可查、"0"/不传/typeList 全为 0 条），用 "0" 会让 sent 分支恒为空。
# 契约原文把 0 写成字母 O，但字母 O 会让 SearchShareRecord 直接返回 code=500 System error，只发数字。
SEARCH_TYPE_SENT = "1"


def run(context, input_data):
    if input_data.get("listKind") != LIST_KIND:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "listKind 必须为 %s" % LIST_KIND
        )
    search_scope = input_data["searchScope"]
    page_size = input_data.get("pageSize") or context.default_page_size()
    conditions = {"searchType": SEARCH_TYPE_SENT}
    if input_data.get("keyword"):
        conditions["keyword"] = input_data["keyword"]
    show_info = {"pageSize": page_size}
    digest = common.conditions_digest({"scope": search_scope, "conditions": conditions, "pageSize": page_size})
    if input_data.get("cursor"):
        state = context.resolve_cursor(input_data["cursor"], digest)
        page_after = state["upstreamState"].get("pageAfter")
        if isinstance(page_after, list) and page_after:
            show_info["pageAfter"] = list(page_after)
    if search_scope == SCOPE_RECORDS:
        action = idaas_search_non_file_share_record.call
        coverage = "仅覆盖订阅外链分享记录，不代表我发出的全部公开外链"
    else:
        action = idaas_search_share_record.call
        coverage = "仅覆盖订阅外链分享内的文件，不代表我发出的全部公开外链"
    payload = common.guard_upstream(context, action, conditions=conditions, show_info=show_info)
    data = common.payload_data(context, payload, required=False) or {}
    # 现网 V3 网关把集合放在 data.rows（与 S02 同一口径，2026-09-19 实测），契约文档写作 data.data；
    # 两种口径都必须兼容，否则平台明明返回了记录也会被当成空结果。
    items = None
    for key in ("rows", "data"):
        value = data.get(key)
        if isinstance(value, list):
            items = value
            break
    if items is None:
        if "rows" in data or "data" in data:
            common.contract_mismatch(context, "分享搜索响应 rows/data 必须是数组")
        items = []
    total = data.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        total = None
    page_after = data.get("pageAfter")
    has_cursor = isinstance(page_after, list) and bool(page_after)
    exhausted = common.page_exhausted(page_size, len(items), has_cursor)
    cursor_token = None
    if has_cursor and not exhausted:
        cursor_token = context.issue_cursor(digest, {"pageAfter": list(page_after)}, complete=False)
    page = common.page_info(
        page_size,
        len(items),
        total=total,
        has_more=bool(cursor_token),
        next_cursor=cursor_token,
        complete=exhausted,
    )
    result_data = {
        "listKind": LIST_KIND,
        "searchScope": search_scope,
        "coverage": coverage,
        "items": [_record_item(context, item) for item in items],
    }
    if not items:
        return common.empty(data=result_data, page=page)
    return common.success(data=result_data, counts={"returned": len(items)}, page=page)


def _record_item(context, item):
    if not isinstance(item, dict):
        common.contract_mismatch(context, "分享搜索响应项必须是对象")
    # 与 S24 同一事实源：分享记录行返回 shareId，分享内文件行返回同值空间的 shareCode
    # （两者与 linkId 同一取值空间，2026-09-18 实测）；取到即签发 linkRef，供下游场景承接。
    link_id = item.get("shareId") or item.get("shareCode") or item.get("linkId") or item.get("linkID")
    cleaned = common.strip_sensitive(item)
    for key in ("shareId", "shareCode", "linkId", "linkID"):
        cleaned.pop(key, None)
    # filePathId 是含真实目录 ID 的路径串，不属于可对外输出字段，只保留可读路径与文件名。
    cleaned.pop("filePathId", None)
    if isinstance(link_id, str) and link_id:
        cleaned["linkRef"] = context.issue_ref("link", link_id, common.SCOPE_OUTER_LINK)
    return cleaned
