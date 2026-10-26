# S02 关键词搜索：按关键词及可选类型、后缀、时间搜索当前账号的个人云文件。
# 流程：关键词搜索 -> 可选详情补查 -> 受控游标输出。
# CLI: cmcloud files search --format json

from cmcloud_core import file_types
from cmcloud_adapters import file_get
from cmcloud_adapters import idaas_search_skill_file
from cmcloud_core import result as result_module
from cmcloud_workflows import common

# 单边时间条件的补齐值：只有 to 时从 1970 起搜，只有 from 时搜到当前时刻
_EPOCH_TEXT = "19700101000000"
_FILE_TYPE_CODES = tuple(code for code in file_types.TYPE_CODES if code != "100")
_DIRECTORY_SIZE_WARNING = "目录类型不支持大小筛选，已忽略目录，仅查询文件。"


def _now_text(context):
    return common.idaas_time_text(context.now().isoformat())


def run(context, input_data):
    page_size = input_data.get("pageSize") or context.default_page_size()
    show_info = {"pageSize": page_size}
    # Bind user filters, not the moving "now" used to fill a missing end time.
    filters = {key: value for key, value in input_data.items() if key not in ("cursor", "pageSize")}
    digest = common.conditions_digest({"filters": filters, "scope": common.SCOPE_AUTHORIZED_PERSONAL, "pageSize": page_size})
    if input_data.get("cursor"):
        state = context.resolve_cursor(input_data["cursor"], digest)
        conditions = state["upstreamState"].get("conditions")
        if not isinstance(conditions, dict):
            common.contract_mismatch(context, "搜索游标缺少冻结查询条件，请重新查询")
        warnings = state["upstreamState"].get("warnings") or []
        if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
            common.contract_mismatch(context, "搜索游标中的提示信息不合法，请重新查询")
        page_after = state["upstreamState"].get("pageAfter")
        if isinstance(page_after, list) and page_after:
            show_info["pageAfter"] = list(page_after)
    else:
        conditions, warnings = _conditions(context, input_data)
    payload = common.guard_upstream(
        context, idaas_search_skill_file.call, conditions=conditions, show_info=show_info
    )
    data = common.payload_data(context, payload)
    items = _search_items(context, data)
    # 搜索结果不按专属根收窄：授权个人云范围内的文件一律保留（范围由回执与 normalize 的 scope 标注）。
    upstream_returned = len(items)
    items = _canonical_search_types(context, items)
    normalized = common.normalize_file_items(context, items, common.SCOPE_AUTHORIZED_PERSONAL)
    total = _search_total(context, data)
    next_after = data.get("pageAfter")
    if next_after is not None and not _is_page_after(next_after):
        common.contract_mismatch(context, "关键词搜索响应 pageAfter 必须是对象数组或非空字符串数组")
    has_cursor = isinstance(next_after, list) and bool(next_after)
    full_page = upstream_returned >= page_size
    exhausted = (not full_page) or (not has_cursor)
    next_cursor = None
    if has_cursor and not exhausted:
        next_cursor = context.issue_cursor(
            digest,
            {
                "pageAfter": list(next_after),
                "conditions": conditions,
                "warnings": list(warnings),
            },
            complete=False,
        )
    page = common.page_info(
        page_size,
        len(normalized),
        total=total,
        has_more=bool(has_cursor and full_page),
        next_cursor=next_cursor,
        complete=exhausted,
    )
    result_data = _result_data(normalized, conditions, warnings)
    result = common.success(
        data=result_data, counts={"returned": len(normalized)}, page=page
    )
    if not normalized:
        return common.empty(data=result_data, page=page)
    return result


def _result_data(items, conditions, warnings):
    data = {"items": items}
    if "sizeRange" in conditions:
        data["appliedFilters"] = {
            "typeList": list(conditions.get("typeList") or ()),
            "sizeRange": dict(conditions["sizeRange"]),
        }
    if warnings:
        data["warnings"] = list(warnings)
    return data


def _search_items(context, data):
    # 兼容两种回执口径：V3 网关实际返回 data.rows，契约文档口径为 data.skillFileList。
    for key in ("rows", "skillFileList"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    common.contract_mismatch(context, "关键词搜索响应缺少 rows/skillFileList 列表")


def _search_total(context, data):
    """只在上游给出可信非负整数总数时对外返回；缺失保持 null。"""
    total = data.get("total")
    if total is None:
        return None
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        common.contract_mismatch(context, "关键词搜索响应 total 必须是非负整数")
    return total


def _is_page_after(value):
    """兼容文档对象数组与 SearchSkillFile 现网字符串数组的续页游标。"""
    if not isinstance(value, list):
        return False
    if not value:
        return True
    if all(isinstance(item, dict) for item in value):
        return True
    return all(isinstance(item, str) and item for item in value)


def _conditions(context, input_data):
    conditions = {}
    warnings = []
    folder_refs = input_data.get("folderRefs") or ([input_data["folderRef"]] if input_data.get("folderRef") else [])
    if folder_refs:
        folder_ids = []
        for token in folder_refs:
            folder_id = context.real_id(token, "folder")
            node = common.ensure_source(context, folder_id)
            if node["fileId"] != folder_id:
                common.contract_mismatch(context, "搜索目录详情与请求的目录不一致")
            if node["type"] != "folder":
                raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "搜索范围必须是目录")
            if folder_id not in folder_ids:
                folder_ids.append(folder_id)
        conditions["includeFileIdList"] = folder_ids
    if input_data.get("keyword"):
        conditions["keyword"] = input_data["keyword"]
    if input_data.get("typeList"):
        conditions["typeList"] = list(input_data["typeList"])
    elif input_data.get("contentType"):
        # 中文规范词与数值码都收；归一化后再进上游 typeList，取值码表见 cmcloud/file_types.py。
        try:
            type_code = file_types.normalize_content_type(input_data["contentType"])
        except ValueError:
            raise result_module.ExecutionError(
                result_module.INVALID_ARGUMENT,
                "类型筛选只支持：%s" % file_types.describe_choices(),
                )
        conditions["typeList"] = [type_code]
    if input_data.get("extensionList"):
        conditions["extensionList"] = list(input_data["extensionList"])
    elif input_data.get("extension"):
        conditions["extensionList"] = [input_data["extension"]]
    for key in ("addressList", "tagList", "sizeRange", "isMatchFolder"):
        if key in input_data:
            conditions[key] = input_data[key]
    if input_data.get("timeList"):
        conditions["timeList"] = [dict(item) for item in input_data["timeList"]]
    elif input_data.get("from") or input_data.get("to"):
        # 契约 timeList 的 startTime/endTime 均必填且为 yyyyMMddHHmmss；CLI 的 from/to 允许单边，
        # 缺的一端用纪元下限/"现在"补齐，保持原半开区间语义而不是发半开字段。
        start_time = common.idaas_time_text(input_data["from"]) if input_data.get("from") else _EPOCH_TEXT
        end_time = common.idaas_time_text(input_data["to"]) if input_data.get("to") else _now_text(context)
        conditions["timeList"] = [
            {
                "filed": input_data.get("timeField") or "createdAt",
                "startTime": start_time,
                "endTime": end_time,
            }
        ]
    _normalize_size_range_types(conditions, warnings)
    _assert_search_condition_compatibility(context, conditions)
    return conditions, warnings


def _normalize_size_range_types(conditions, warnings):
    if "sizeRange" not in conditions:
        return
    types = conditions.get("typeList")
    if not types:
        conditions["typeList"] = list(_FILE_TYPE_CODES)
        return
    if "100" not in types:
        return
    file_types_only = [item for item in types if item != "100"]
    if not file_types_only:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "文件夹不支持按文件大小筛选，请选择文件类型或移除大小条件。",
        )
    conditions["typeList"] = file_types_only
    warnings.append(_DIRECTORY_SIZE_WARNING)


def _assert_search_condition_compatibility(context, conditions):
    types = set(conditions.get("typeList") or ())
    time_fields = {item.get("filed") for item in conditions.get("timeList", ()) if isinstance(item, dict)}
    if "takenAt" in time_fields and types != {"1"}:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "takenAt 仅支持 typeList 仅包含图片类型 1 的搜索",
        )
    if "sizeRange" in conditions and (not types or "100" in types):
        # 正常入口会先由 _normalize_size_range_types 补齐或剔除；保留此处作为内部防线。
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "sizeRange 只能用于明确的文件类型搜索"
        )


def _canonical_search_types(context, items):
    """Search numeric types are not the file/get file-vs-folder contract.

    Never interpret search type=1 as folder. Resolve ambiguous numeric values
    through one file/batchGet per 100 ids; entries the batch does not cover
    (missing row, missing type field, batch failure) fall back to file/get,
    so the verification strength stays the same as before.
    """
    pending = {}
    for index, item in enumerate(items):
        if item.get("type") not in (None, "file", "folder"):
            pending[index] = common.field_text(context, item, "fileId")
    resolved = common.batch_file_types(context, list(pending.values())) if pending else {}
    result = []
    for index, item in enumerate(items):
        if index in pending:
            node_type = resolved.get(pending[index])
            if node_type is None:
                # 批量未覆盖到的条目回退逐条核验，保持原有核验强度。
                node = common.payload_data(
                    context,
                    common.guard_upstream(context, file_get.call, file_id=pending[index]),
                )
                node_type = node.get("type")
            if node_type not in ("file", "folder"):
                common.contract_mismatch(context, "搜索资源详情缺少明确的文件/目录类型")
            # category is a different upstream enum too; don't let it override type.
            item = dict(item, type=node_type)
            item.pop("category", None)
        result.append(item)
    return result
