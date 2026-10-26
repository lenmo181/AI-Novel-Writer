# S30 建册并加图：先建相簿，再用回执 albumId 把指定图片加入新相簿。
# 流程：名称/图片核验 -> 可信确认 -> 创建相簿 -> 使用回执 ID 加图 -> 分步结果。
# CLI: cmcloud album create --format json

from cmcloud_core import operation_ledger as ledger_module
from cmcloud_core import result as result_module
from cmcloud_adapters import idaas_photo_customization_add, idaas_photo_customization_file_add
from cmcloud_workflows import common

# 判别字段由命令 Schema 固定；此处再次校验，防止被注册表以外的入口绕过。
KIND = "album"


def prepare(context, input_data):
    if input_data.get("kind") != KIND:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "kind 必须为 %s" % KIND)
    file_ids = [common.real_id_for_kinds(context, token, ("file", "image"), "fileRefs") for token in input_data["fileRefs"]]
    if not file_ids:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "fileRefs 不得为空")
    return {
        "summary": "新建相簿 %s 并加入 %d 张图片" % (input_data["name"], len(file_ids)),
        "resolvedIds": {"fileIds": common.dedupe(file_ids), "ownerType": context.publication.album_owner_type},
        "kind": KIND,
        "name": input_data["name"],
        "fileRefs": list(input_data["fileRefs"]),
    }


def execute(context, plan):
    create_kwargs = {"name": plan["name"]}
    if plan["resolvedIds"]["ownerType"] is not None:
        create_kwargs["owner_type"] = plan["resolvedIds"]["ownerType"]
    payload = common.guard_upstream(context, idaas_photo_customization_add.call, **create_kwargs)
    data = common.json_success_data(context, payload)
    album_id = data.get("id") or data.get("albumId")
    if not isinstance(album_id, str) or not album_id:
        common.contract_mismatch(context, "创建相簿回执缺少 data.id")
    album_ref = context.issue_ref("album", album_id, common.SCOPE_ALBUM)
    context.note_step("create", "已创建相簿")
    add_kwargs = {"album_id": album_id, "file_ids": plan["resolvedIds"]["fileIds"]}
    if plan["resolvedIds"]["ownerType"] is not None:
        add_kwargs["owner_type"] = plan["resolvedIds"]["ownerType"]
    try:
        add_payload = common.guard_upstream(context, idaas_photo_customization_file_add.call, **add_kwargs)
        add_data = common.json_success_data(context, add_payload, required=False) or {}
    except result_module.ExecutionError as err:
        # 第二步失败：保留已创建相簿引用，不自动删除相簿
        context.note_step("add-photos", "加图失败", status=ledger_module.STEP_FAILED)
        return common.partial(
            data={
                "albumRef": album_ref,
                "name": plan["name"],
                "addedCount": 0,
                "message": "创建相簿成功，但上传图片失败",
                "failedCode": err.code,
            },
            counts={"created": 1, "added": 0, "requested": len(plan["resolvedIds"]["fileIds"])},
        )
    context.note_step("add-photos", "已把图片加入新相簿")
    return common.success(
        data={
            "albumRef": album_ref,
            "name": plan["name"],
            "requested": len(plan["resolvedIds"]["fileIds"]),
            "result": {key: add_data[key] for key in ("total", "successCount", "failCount") if key in add_data},
        },
        counts={"created": 1, "added": len(plan["resolvedIds"]["fileIds"])},
    )
