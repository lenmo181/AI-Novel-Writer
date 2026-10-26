# S29 新建相簿：创建一个普通自定义相簿（不加入图片）。
# 流程：名称校验 -> 固定归属 -> 可信确认 -> 创建相簿 -> ID 核验。
# CLI: cmcloud album create --format json

from cmcloud_core import result as result_module
from cmcloud_adapters import idaas_photo_customization_add
from cmcloud_workflows import common

# 判别字段由命令 Schema 固定；此处再次校验，防止被注册表以外的入口绕过。
KIND = "album"


def prepare(context, input_data):
    if input_data.get("kind") != KIND:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "kind 必须为 %s" % KIND)
    name = input_data["name"]
    return {
        "summary": "新建相簿 %s" % name,
        "resolvedIds": {"ownerType": context.publication.album_owner_type},
        "kind": KIND,
        "name": name,
    }


def execute(context, plan):
    kwargs = {"name": plan["name"]}
    if plan["resolvedIds"]["ownerType"] is not None:
        kwargs["owner_type"] = plan["resolvedIds"]["ownerType"]
    payload = common.guard_upstream(context, idaas_photo_customization_add.call, **kwargs)
    data = common.json_success_data(context, payload)
    album_id = data.get("id") or data.get("albumId")
    if not isinstance(album_id, str) or not album_id:
        common.contract_mismatch(context, "创建相簿回执缺少 data.id")
    context.note_step("create", "已创建相簿")
    return common.success(
        data={"albumRef": context.issue_ref("album", album_id, common.SCOPE_ALBUM), "name": plan["name"]},
        counts={"created": 1},
    )
