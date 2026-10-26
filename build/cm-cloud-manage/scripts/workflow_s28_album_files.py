# S28 相簿/故事图片明细：按受控对象引用查询指定对象的图片明细。
# 流程：对象引用核验 -> kind 分支 -> 调唯一图片明细接口 -> 真实 ID/分页输出。
# CLI: cmcloud album files --format json

from cmcloud_core import result as result_module
from cmcloud_adapters import idaas_photo_customization_file_list, idaas_story_memory_file_list
from cmcloud_workflows import common


def run(context, input_data):
    kind = input_data["kind"]
    album_ref = input_data.get("albumRef")
    if not album_ref:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "查询图片明细必须提供对象引用")
    album_id = context.real_id(album_ref, "album" if kind == "album" else "story")
    # 相簿/故事图片列表接口最小页为 10；发布配置默认页可能小于该值，进入下游前统一收敛。
    page_size = common.clamp_page_size(input_data.get("pageSize") or context.default_page_size())
    page_cursor = {"pageSize": page_size}
    digest = common.conditions_digest({"kind": kind, "albumRef": album_ref, "pageSize": page_size})
    if input_data.get("cursor"):
        state = context.resolve_cursor(input_data["cursor"], digest)
        cursor = state["upstreamState"].get("pageCursor")
        if isinstance(cursor, str) and cursor:
            page_cursor["pageCursor"] = cursor
    action = _files_action(kind)
    kwargs = {"album_id": album_id, "page_info": page_cursor}
    if kind == "album":
        # 相簿图片列表接口要求排序字段必填；故事图片列表接口不接受这两个字段
        kwargs["order_by"] = common.ALBUM_PHOTO_ORDER_BY
        kwargs["order_direction"] = common.ALBUM_PHOTO_ORDER_DIRECTION
    if context.publication.album_owner_type is not None:
        kwargs["owner_type"] = context.publication.album_owner_type
    payload = common.guard_upstream(context, action, **kwargs)
    data = common.json_success_data(context, payload, required=False) or {}
    holder = data.get("file") if kind == "story" and isinstance(data.get("file"), dict) else data
    items = holder.get("list")
    if items is None:
        items = []
    if not isinstance(items, list):
        common.contract_mismatch(context, "图片明细响应 list 必须是数组")
    next_cursor = holder.get("nextPageCursor")
    cursor_token = None
    if isinstance(next_cursor, str) and next_cursor:
        cursor_token = context.issue_cursor(digest, {"pageCursor": next_cursor}, complete=False)
    page = common.page_info(
        page_size,
        len(items),
        total=holder.get("totalCount") if isinstance(holder.get("totalCount"), int) else None,
        has_more=True if cursor_token else None,
        next_cursor=cursor_token,
        complete=not cursor_token,
    )
    normalized = []
    for item in items:
        node = item.get("fileInfo") if isinstance(item, dict) and isinstance(item.get("fileInfo"), dict) else item
        normalized.append(common.normalize_file_item(context, node, common.SCOPE_ALBUM, ref_kind="image"))
    result_data = {
        "kind": kind,
        "view": "files",
        "objectRef": context.issue_ref(kind, album_id, common.SCOPE_ALBUM),
        "items": normalized,
    }
    if not items:
        return common.empty(data=result_data, page=page)
    return common.success(data=result_data, counts={"returned": len(items)}, page=page)


def _files_action(kind):
    if kind == "album":
        return idaas_photo_customization_file_list.call
    if kind == "story":
        return idaas_story_memory_file_list.call
    raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "kind 取值非法")
