# S32 新建回忆故事：用标题、封面和图片集合创建回忆故事。
# 流程：字段/图片核验 -> 可信确认 -> 创建故事 -> 回执 ID 核验。
# CLI: cmcloud album create --format json

from cmcloud_core import result as result_module
from cmcloud_adapters import idaas_story_memory_add
from cmcloud_workflows import common

# 判别字段由命令 Schema 固定；此处再次校验，防止被注册表以外的入口绕过。
KIND = "story"


def prepare(context, input_data):
    if input_data.get("kind") != KIND:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "kind 必须为 %s" % KIND)
    # 云盘 story/memory/add 要求 secondTitle 字段存在且非空：缺失返回 01000001 参数错误（2026-09-19 实测）。
    # CLI 入参保持可选：用户未提供时按产品约定默认用故事标题填充，保证发往下游的字段非空。
    second_title = input_data.get("secondTitle")
    if not isinstance(second_title, str) or not second_title:
        second_title = input_data["name"]
    cover_id = common.real_id_for_kinds(context, input_data["coverRef"], ("file", "image"), "coverRef")
    file_ids = common.dedupe([
        common.real_id_for_kinds(context, token, ("file", "image"), "fileRefs")
        for token in input_data["fileRefs"]
    ])
    if cover_id not in file_ids:
        file_ids.insert(0, cover_id)
    return {
        "summary": "新建回忆故事 %s，包含 %d 张图片" % (input_data["name"], len(file_ids)),
        "resolvedIds": {"coverId": cover_id, "fileIds": file_ids, "ownerType": context.publication.album_owner_type},
        "kind": KIND,
        "name": input_data["name"],
        "secondTitle": second_title,
    }


def execute(context, plan):
    kwargs = {
        "name": plan["name"],
        "file_ids": plan["resolvedIds"]["fileIds"],
        "cover": plan["resolvedIds"]["coverId"],
        "second_title": plan["secondTitle"],
    }
    if plan["resolvedIds"]["ownerType"] is not None:
        kwargs["owner_type"] = plan["resolvedIds"]["ownerType"]
    payload = common.guard_upstream(context, idaas_story_memory_add.call, **kwargs)
    data = common.json_success_data(context, payload)
    story_id = data.get("id") or data.get("albumId")
    if not isinstance(story_id, str) or not story_id:
        common.contract_mismatch(context, "创建故事回执缺少 data.id")
    context.note_step("create", "已创建回忆故事")
    return common.success(
        data={"storyRef": context.issue_ref("story", story_id, common.SCOPE_ALBUM), "name": plan["name"]},
        counts={"created": 1},
    )
