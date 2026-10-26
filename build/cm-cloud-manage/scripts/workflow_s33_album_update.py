# S33 修改相簿：修改已有相簿的名称或封面。
# 流程：读取当前值 -> 生成差异计划 -> 可信确认 -> 更新 -> 变更核验。
# CLI: cmcloud album update --format json

from cmcloud_core import result as result_module
from cmcloud_adapters import idaas_photo_customization_list, idaas_photo_customization_update
from cmcloud_workflows import common

# 判别字段由命令 Schema 固定；此处再次校验，防止被注册表以外的入口绕过。
KIND = "album"

# 相簿列表项中的封面字段名待真实样本冻结；不按别名猜测
COVER_FIELD = "fileId"


def prepare(context, input_data):
    if input_data.get("kind") != KIND:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "kind 必须为 %s" % KIND)
    album_id = context.real_id(input_data["albumRef"], "album")
    changes = {}
    if "name" in input_data:
        changes["name"] = _require_non_empty(input_data["name"], "name")
    if "coverRef" in input_data:
        changes["fileId"] = common.real_id_for_kinds(context, input_data["coverRef"], ("file", "image"), "coverRef")
    if not changes:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "至少提供 name 或 coverRef 一项")
    current = _current_album(context, album_id)
    diff = {key: value for key, value in changes.items() if current.get(key) != value}
    return {
        "summary": "修改相簿 %s 的 %s" % (current.get("name") or album_id, "、".join(sorted(diff)) or "无变化字段"),
        "resolvedIds": {"albumId": album_id, "changes": changes, "ownerType": context.publication.album_owner_type},
        "kind": KIND,
        "albumRef": input_data["albumRef"],
        "current": current,
        "unchanged": sorted(set(changes) - set(diff)),
    }


def execute(context, plan):
    changes = plan["resolvedIds"]["changes"]
    kwargs = {"album_id": plan["resolvedIds"]["albumId"]}
    # 未提供字段不得出现在更新请求中；空字符串不解释为清空
    if "name" in changes:
        kwargs["name"] = changes["name"]
    if "fileId" in changes:
        kwargs["file_id"] = changes["fileId"]
    if plan["resolvedIds"]["ownerType"] is not None:
        kwargs["owner_type"] = plan["resolvedIds"]["ownerType"]
    common.guard_upstream(context, idaas_photo_customization_update.call, **kwargs)
    context.note_step("update", "已提交相簿更新")
    after = _current_album(context, plan["resolvedIds"]["albumId"])
    verified = all(after.get(key) == value for key, value in changes.items())
    return common.success(
        data={
            "albumRef": plan["albumRef"],
            "changed": sorted(changes),
            "unchanged": plan["unchanged"],
            "verified": verified,
        },
        counts={"changed": len(changes)},
    )


def _require_non_empty(value, field):
    if not isinstance(value, str) or not value:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "%s 必须是非空字符串" % field)
    return value


def _current_album(context, album_id):
    """用相簿列表读取当前值；未匹配到该相簿时返回空映射，不猜测字段。"""
    page_info = {"pageSize": 100}
    cursor = None
    for _ in range(20):
        if cursor:
            page_info["pageCursor"] = cursor
        kwargs = {"page_info": page_info}
        if context.publication.album_owner_type is not None:
            kwargs["owner_type"] = context.publication.album_owner_type
        payload = common.guard_upstream(context, idaas_photo_customization_list.call, **kwargs)
        data = common.payload_data(context, payload, required=False) or {}
        items = data.get("list")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("id") == album_id:
                    return {
                        "name": item.get("name"),
                        "fileId": item.get(COVER_FIELD),
                        "albumId": album_id,
                    }
        cursor = data.get("nextPageCursor")
        if not isinstance(cursor, str) or not cursor:
            break
    return {"albumId": album_id}
