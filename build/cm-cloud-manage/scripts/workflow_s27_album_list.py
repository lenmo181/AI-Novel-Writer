# S27 相簿/故事列表：列出相簿或故事对象，不查询图片明细。
# 流程：kind 分支 -> 调唯一列表接口 -> 真实 ID/分页输出。
# CLI: cmcloud album list --format json

from cmcloud_adapters import idaas_photo_customization_list, idaas_story_memory_list
from cmcloud_workflows import common


def run(context, input_data):
    kind = input_data["kind"]
    # 相簿/故事列表接口最小页为 10；发布配置默认页可能小于该值，进入下游前统一收敛。
    page_size = common.clamp_page_size(input_data.get("pageSize") or context.default_page_size())
    page_cursor = {"pageSize": page_size}
    digest = common.conditions_digest({"kind": kind, "pageSize": page_size})
    if input_data.get("cursor"):
        state = context.resolve_cursor(input_data["cursor"], digest)
        cursor = state["upstreamState"].get("pageCursor")
        if isinstance(cursor, str) and cursor:
            page_cursor["pageCursor"] = cursor
    action = _list_action(kind)
    kwargs = {"page_info": page_cursor}
    if context.publication.album_owner_type is not None:
        kwargs["owner_type"] = context.publication.album_owner_type
    payload = common.guard_upstream(context, action, **kwargs)
    data = common.json_success_data(context, payload, required=False) or {}
    items = data.get("list")
    if items is None:
        items = []
    if not isinstance(items, list):
        common.contract_mismatch(context, "列表响应 list 必须是数组")
    next_cursor = data.get("nextPageCursor")
    cursor_token = None
    if isinstance(next_cursor, str) and next_cursor:
        cursor_token = context.issue_cursor(digest, {"pageCursor": next_cursor}, complete=False)
    total = data.get("totalCount")
    if isinstance(total, bool) or not isinstance(total, int):
        total = None
    page = common.page_info(
        page_size,
        len(items),
        total=total,
        has_more=True if cursor_token else None,
        next_cursor=cursor_token,
        complete=not cursor_token,
    )
    result_data = {"kind": kind, "view": "list", "items": [_object_item(context, kind, item) for item in items]}
    if not items:
        return common.empty(data=result_data, page=page)
    return common.success(data=result_data, counts={"returned": len(items)}, page=page)


def _list_action(kind):
    if kind == "album":
        return idaas_photo_customization_list.call
    if kind == "story":
        return idaas_story_memory_list.call
    from cmcloud_core import result as result_module

    raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "kind 取值非法")


def lookup_objects(context, kind, object_ids, max_pages=20):
    """按真实主键核对当前对象；不按名称猜 ID，不把扫描上限当作对象不存在。"""
    wanted = set(object_ids)
    found = {}
    if not wanted:
        return found, True
    page_info = {"pageSize": common.MAX_PAGE_SIZE}
    seen_cursors = set()
    for _ in range(max_pages):
        kwargs = {"page_info": dict(page_info)}
        if context.publication.album_owner_type is not None:
            kwargs["owner_type"] = context.publication.album_owner_type
        payload = common.guard_upstream(context, _list_action(kind), **kwargs)
        data = common.json_success_data(context, payload, required=False) or {}
        items = data.get("list")
        if items is None:
            items = []
        if not isinstance(items, list):
            common.contract_mismatch(context, "相簿/故事列表 list 必须是数组")
        for item in items:
            if not isinstance(item, dict):
                common.contract_mismatch(context, "相簿/故事列表项必须是对象")
            object_id = item.get("id") or item.get("albumId")
            if not isinstance(object_id, str) or not object_id:
                common.contract_mismatch(context, "相簿/故事列表项缺少 id")
            if object_id in wanted:
                found[object_id] = item
        if wanted <= found.keys():
            return found, True
        cursor = data.get("nextPageCursor")
        if cursor is None or cursor == "":
            return found, True
        if not isinstance(cursor, str) or cursor in seen_cursors:
            common.contract_mismatch(context, "相簿/故事列表游标非法或重复，无法核验对象")
        seen_cursors.add(cursor)
        page_info["pageCursor"] = cursor
    return found, False


def _object_item(context, kind, item):
    if not isinstance(item, dict):
        common.contract_mismatch(context, "相簿/故事列表项不是对象")
    object_id = item.get("id") or item.get("albumId")
    if not isinstance(object_id, str) or not object_id:
        common.contract_mismatch(context, "相簿/故事列表项缺少 id")
    cleaned = common.strip_sensitive(item)
    cleaned.pop("id", None)
    cleaned.pop("albumId", None)
    cleaned.pop("createUserId", None)
    cleaned["objectRef"] = context.issue_ref(kind, object_id, common.SCOPE_ALBUM)
    cover = cleaned.get("cover")
    if isinstance(cover, dict):
        cover_id = cover.pop("fileId", None)
        cover.pop("parentFileId", None)
        if isinstance(cover_id, str) and cover_id:
            cover["fileRef"] = context.issue_ref("image", cover_id, common.SCOPE_ALBUM)
    return cleaned
