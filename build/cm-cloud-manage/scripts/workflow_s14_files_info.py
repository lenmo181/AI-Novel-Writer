# S14 动态文件详情：为动态结果或真实文件补充名称、大小、时间和位置。
# 流程：引用/ID 映射核验 -> 内容详情或文件详情 -> 父链补查 -> 结果归一化。
# CLI: cmcloud files info --format json

from cmcloud_core import result as result_module
from cmcloud_adapters import file_get, idaas_get_content_info
from cmcloud_workflows import common

# 2026-09-20 已实测 type 2/4/5 的动态 contentId 可直接用于 file/batchGet（29/29 一一对应），
# 名称补齐已由 activity 域在列表输出时完成（见 common.apply_dynamic_names）。
# 但动态文件是否位于专属目录、其父链是否可核验尚未验收：dynamicRefs 分支仍需位置/范围契约，
# 保持关闭。
DYNAMIC_TO_FILE_MAPPING_FROZEN = False

TIME_FIELDS = ("createdAt", "updatedAt", "takenAt")


def run(context, input_data):
    requested = set(input_data.get("fields") or ["name", "size", "time", "location"])
    if input_data.get("dynamicRefs"):
        if not DYNAMIC_TO_FILE_MAPPING_FROZEN:
            raise result_module.ExecutionError(
                result_module.CAPABILITY_UNAVAILABLE, "动态 contentId 与 fileId 的映射未冻结"
            )
        items = [_dynamic_item(context, token, requested) for token in input_data["dynamicRefs"]]
    else:
        items = [_file_item(context, token, requested) for token in input_data["fileIds"]]
    data = {"items": items}
    if not items:
        return common.empty(data=data)
    return common.success(data=data, counts={"returned": len(items)})


def _file_item(context, token, requested):
    file_id = context.real_id(token, "file")
    payload = common.guard_upstream(context, file_get.call, file_id=file_id)
    node = common.payload_data(context, payload)
    item = {"fileRef": context.issue_ref("file", file_id, common.SCOPE_AUTHORIZED_PERSONAL), "type": node.get("type")}
    if "name" in requested:
        item["name"] = node.get("name")
    if "size" in requested and "size" in node:
        item["size"] = node.get("size")
    if "time" in requested:
        for key in TIME_FIELDS:
            if key in node:
                item[key] = node[key]
    if "location" in requested:
        item["path"] = common.personal_path_segments(context, file_id, node_hint=node)
    return item


def _dynamic_item(context, token, requested):
    entry = context.ref_entry(token)
    content_id = entry["realId"]
    payload = common.guard_upstream(context, idaas_get_content_info.call, content_id=content_id)
    data = common.payload_data(context, payload)
    item = {"dynamicRef": token}
    for key in ("name", "size", "contentType", "dynamicType"):
        if key in data and isinstance(data[key], (str, int)):
            item[key] = data[key]
    return item
