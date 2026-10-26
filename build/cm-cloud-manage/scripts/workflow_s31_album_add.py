# S31 向相簿加图：把指定真实图片加入已有相簿。
# 流程：相簿/图片核验 -> 可信确认 -> 批量加图 -> 回执或明细复核。
# CLI: cmcloud album add --format json

from cmcloud_adapters import idaas_photo_customization_file_add
from cmcloud_workflows import common

def prepare(context, input_data):
    album_id = context.real_id(input_data["albumRef"], "album")
    file_ids = common.dedupe([
        common.real_id_for_kinds(context, token, ("file", "image"), "fileRefs")
        for token in input_data["fileRefs"]
    ])
    return {
        "summary": "向相簿加入 %d 张图片" % len(file_ids),
        "resolvedIds": {"albumId": album_id, "fileIds": file_ids, "ownerType": context.publication.album_owner_type},
        "albumRef": input_data["albumRef"],
    }


def execute(context, plan):
    kwargs = {"album_id": plan["resolvedIds"]["albumId"], "file_ids": plan["resolvedIds"]["fileIds"]}
    if plan["resolvedIds"]["ownerType"] is not None:
        kwargs["owner_type"] = plan["resolvedIds"]["ownerType"]
    payload = common.guard_upstream(context, idaas_photo_customization_file_add.call, **kwargs)
    data = common.json_success_data(context, payload, required=False) or {}
    context.note_step("add", "已提交加图请求")
    result_data = {
        "albumRef": plan["albumRef"],
        "requested": len(plan["resolvedIds"]["fileIds"]),
        # 加图成功回执没有逐文件结果；图片列表接口不是提交加图的前置条件，
        # 不再因为可选的计数复核失败而阻断已冻结的写接口。
        "reportedAdded": data.get("successCount") if isinstance(data.get("successCount"), int) else None,
        "reported": {key: data[key] for key in ("total", "successCount", "failCount") if key in data},
        "verified": None,
    }
    return common.success(data=result_data, counts={"requested": len(plan["resolvedIds"]["fileIds"])})
