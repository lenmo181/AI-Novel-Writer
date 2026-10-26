# S12 全部转存动态：内部采集类型 5、6、7 的转存事件并合并。
# 流程：三分支分页采集 -> 分支完整性检查 -> 稳定 ID 去重 -> 汇总输出。
# CLI: cmcloud activity list --eventKinds all-save --format json

from cmcloud_workflows import common

DYNAMIC_TYPES = (common.DYNAMIC_TYPE_COPY, common.DYNAMIC_TYPE_GROUP, common.DYNAMIC_TYPE_DISCOVER)


def run(context, input_data):
    # collectionLimit 由发布配置固定；pageSize 遵循动态接口默认值或用户显式值。
    limit = context.collection_limit()
    window = common.activity_window(
        context, input_data, digest_extra={"dynamicTypes": list(DYNAMIC_TYPES)}, dynamic_type=DYNAMIC_TYPES[0]
    )
    page_size = window["pageSize"]
    stored = window.get("upstreamState", {}).get("branchCursors") or {}
    merged = []
    branch_complete = {}
    branch_cursors = {}
    for dynamic_type in DYNAMIC_TYPES:

        def fetch(cursor, dynamic_type=dynamic_type):
            branch_window = dict(window)
            branch_window["dynamicType"] = dynamic_type
            branch_window["nextPageCursor"] = cursor
            items, next_cursor, _total = common.activity_page(context, branch_window)
            return items, next_cursor

        collected = common.collect_all(
            context, fetch, limit=limit, initial_cursor=stored.get(str(dynamic_type))
        )
        branch_items = collected["items"]
        merged.extend(branch_items)
        branch_complete[str(dynamic_type)] = collected["complete"] and len(branch_items) < limit
        if collected["cursor"]:
            branch_cursors[str(dynamic_type)] = collected["cursor"]
    deduped = common.dedupe_by(merged, common.stable_item_key)
    complete = all(branch_complete.values()) and not branch_cursors
    normalized = [common.normalize_dynamic_item(context, item, common.SCOPE_ACTIVITY) for item in deduped]
    common.apply_dynamic_names(context, deduped, normalized)
    data = {
        "items": normalized,
        "dynamicTypes": list(DYNAMIC_TYPES),
        "branchComplete": branch_complete,
        "observed": len(deduped),
    }
    page = common.page_info(
        page_size,
        len(data["items"]),
        total=None if not complete else len(data["items"]),
        has_more=True if branch_cursors else None,
        next_cursor=context.issue_cursor(
            window["digest"], {"window": window.get("windowState"), "branchCursors": branch_cursors}, complete=False
        )
        if branch_cursors
        else None,
        complete=complete,
    )
    if not data["items"]:
        return common.empty(data=data, page=page)
    return common.success(data=data, counts={"returned": len(data["items"])}, page=page)
