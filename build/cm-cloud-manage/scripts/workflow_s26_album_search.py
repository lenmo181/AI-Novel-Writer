# S26 搜索相簿/故事：按单个关键词搜索指定对象类型。
# 流程：类型/位置分页映射 -> 搜索 -> 校验 code=0 -> 归一化输出。
# CLI: cmcloud album search --format json

from cmcloud_adapters import idaas_search_ai_story
from cmcloud_workflows import common

KIND_TO_TYPE = {"album": 0, "story": 1}
DEFAULT_PAGE_SIZE = 10
DEFAULT_SORT_INFOS = [{"field": "updatedAt", "reverse": True, "fieldType": 1}]


def run(context, input_data):
    page_size = input_data.get("pageSize") or DEFAULT_PAGE_SIZE
    conditions = {"keywords": [input_data["keyword"]], "type": KIND_TO_TYPE[input_data["kind"]]}
    show_info = {
        "startNum": 1,
        "stopNum": page_size,
        "sortInfos": [dict(item) for item in DEFAULT_SORT_INFOS],
        "returnTotalCountFlag": True,
    }
    digest = common.conditions_digest({"conditions": conditions, "pageSize": page_size})
    if input_data.get("cursor"):
        state = context.resolve_cursor(input_data["cursor"], digest)
        start = state["upstreamState"].get("startNum")
        if isinstance(start, int) and not isinstance(start, bool) and start >= 1:
            show_info["startNum"] = start
            show_info["stopNum"] = start + page_size - 1
    payload = common.guard_upstream(
        context, idaas_search_ai_story.call, conditions=conditions, show_info=show_info
    )
    data = payload.get("data")
    items = data if isinstance(data, list) else []
    total = payload.get("totalRows")
    total = total if isinstance(total, int) and not isinstance(total, bool) else None
    next_start = show_info["startNum"] + len(items) if items else None
    full_page = len(items) >= page_size
    cursor_token = None
    if full_page and next_start is not None and (total is None or next_start <= total):
        cursor_token = context.issue_cursor(digest, {"startNum": next_start}, complete=False)
    # Search type=1 can include ordinary albums. 2026-09-22 fixtures,
    # cross-checked with the separate album/story list endpoints, identify
    # storyType=1 as album and storyType=2 as the supported memory story.
    # Advance using raw rows above; filtering must never change the offset.
    if input_data["kind"] == "story":
        matching = []
        for item in items:
            if not isinstance(item, dict) or str(item.get("storyType")) not in ("1", "2"):
                common.contract_mismatch(context, "故事搜索返回未确认的 storyType，不能签发故事引用")
            if str(item["storyType"]) == "2":
                matching.append(item)
        items = matching
        # The upstream total includes albums. Only a complete first page
        # establishes the filtered total without traversing earlier pages.
        total = len(items) if show_info["startNum"] == 1 and cursor_token is None else None
    page = common.page_info(
        page_size,
        len(items),
        total=total,
        has_more=bool(cursor_token),
        next_cursor=cursor_token,
        complete=(not full_page) or cursor_token is None,
    )
    result_data = {
        "kind": input_data["kind"],
        "items": [_search_item(context, input_data["kind"], item) for item in items],
    }
    if not items:
        return common.empty(data=result_data, page=page)
    return common.success(data=result_data, counts={"returned": len(items)}, page=page)


def _search_item(context, kind, item):
    object_id = _object_id(context, item)
    cleaned = common.strip_sensitive(item)
    for key in ("albumId", "storyId", "id"):
        cleaned.pop(key, None)
    cleaned.pop("name", None)
    cleaned["storyName"] = _object_name(context, item)
    cleaned["objectRef"] = context.issue_ref(kind, object_id, common.SCOPE_ALBUM)
    return cleaned


def _object_id(context, item):
    if not isinstance(item, dict):
        common.contract_mismatch(context, "相簿搜索响应项必须是对象")
    object_id = item.get("albumId") or item.get("storyId") or item.get("id")
    if not isinstance(object_id, str) or not object_id:
        common.contract_mismatch(context, "相簿搜索响应项缺少对象 ID")
    return object_id


def _object_name(context, item):
    for key in ("name", "storyName"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
    common.contract_mismatch(context, "相簿/故事响应项缺少名称")
