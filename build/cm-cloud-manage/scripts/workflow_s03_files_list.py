# S03 查看文件列表：分页列出指定真实目录的直接子项。
# 流程：目录核验 -> 单页列目录 -> 返回续页游标与路径。
# CLI: cmcloud files list --format json

from cmcloud_adapters import file_list
from cmcloud_workflows import common

MIN_PAGE_SIZE = 10
MAX_PAGE_SIZE = 100


def run(context, input_data):
    supplied_ref = input_data.get("folderRef")
    root_scope = input_data.get("rootScope") or "personal"
    if supplied_ref:
        # rootScope 只选择省略 folderRef 时的起点；显式目录均按个人云文件处理。
        root_scope = "personal"
        folder_id = context.real_id(supplied_ref, "folder")
        node = common.ensure_directory(context, folder_id)
        path = common.personal_path_segments(context, folder_id, node_hint=node)
        folder_ref = supplied_ref
    elif root_scope == "personal":
        # /file/list 的契约以 "/" 表示当前授权用户的个人云根。
        folder_id, path = "/", []
        folder_ref = context.issue_ref("folder", folder_id, common.SCOPE_AUTHORIZED_PERSONAL)
    else:
        folder_id = context.dedicated_root_id()
        folder_ref = context.issue_ref("folder", folder_id, common.SCOPE_AUTHORIZED_PERSONAL)
        common.ensure_directory(context, folder_id)
        # 专属根路径直接取用户目录接口返回的 namePath：授权收窄后父级可能已不在授权范围内，
        # 逐级 file/get 走祖先链会报 16110013，令专属目录列表整体不可用。
        path = _root_path_segments(context.dedicated_root_name())
    item_scope = common.SCOPE_AUTHORIZED_PERSONAL
    page_size = _page_size(context, input_data)
    sort = input_data.get("sort") or {}
    digest = common.conditions_digest(
        {
            # Public refs are freshly issued per request; bind pagination to
            # the resolved directory instead.
            "folderId": folder_id,
            "rootScope": root_scope,
            "sort": sort,
            "pageSize": page_size,
        }
    )
    return _single_layer(context, folder_id, folder_ref, page_size, sort, path, input_data.get("cursor"), digest, root_scope, item_scope)


def _single_layer(context, folder_id, folder_ref, page_size, sort, path, cursor, digest, root_scope, item_scope):
    page_info = {"page_size": page_size}
    if cursor:
        state = context.resolve_cursor(cursor, digest)
        upstream_cursor = state["upstreamState"].get("pageCursor")
        if isinstance(upstream_cursor, str) and upstream_cursor:
            page_info["page_cursor"] = upstream_cursor
    payload = _list_page(context, folder_id, page_info, sort)
    data = common.payload_data(context, payload)
    items = common.list_field(context, data, "items")
    next_cursor = data.get("nextPageCursor")
    has_cursor = isinstance(next_cursor, str) and bool(next_cursor)
    normalized = common.normalize_file_items(context, items, item_scope)
    issued = None
    if has_cursor:
        issued = context.issue_cursor(digest, {"pageCursor": next_cursor}, complete=False)
    page = common.page_info(
        page_size,
        len(normalized),
        total=None,
        has_more=True if has_cursor else None,
        next_cursor=issued,
        complete=not has_cursor,
    )
    data_out = {
        "rootScope": root_scope,
        "folderRef": folder_ref,
        "path": path,
        "items": normalized,
    }
    if root_scope == "dedicated":
        data_out["dedicatedRoot"] = context.dedicated_root_public()
    if not normalized:
        return common.empty(data=data_out, page=page)
    return common.success(data=data_out, counts={"returned": len(normalized)}, page=page)


def _list_page(context, parent_id, page_info, sort):
    kwargs = {"parent_file_id": parent_id, "page_info": page_info}
    if sort.get("orderBy"):
        kwargs["order_by"] = sort["orderBy"]
    if sort.get("orderDirection"):
        kwargs["order_direction"] = sort["orderDirection"]
    return common.guard_upstream(context, file_list.call, **kwargs)


def _page_size(context, input_data):
    requested = input_data.get("pageSize") or context.default_page_size()
    return max(MIN_PAGE_SIZE, min(MAX_PAGE_SIZE, int(requested)))


def _root_path_segments(name_path):
    """把用户目录接口的 namePath（如 /AI空间/第三方智能体/测试111）拆成逐级名称。"""
    if not isinstance(name_path, str):
        return []
    return [segment for segment in name_path.split("/") if segment]
