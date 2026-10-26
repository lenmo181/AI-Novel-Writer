# S34 修改故事：修改已有故事的标题、副标题或封面。
# 流程：读取当前值 -> 生成差异计划 -> 可信确认 -> 更新 -> 变更核验。
# CLI: cmcloud album update --format json

from cmcloud_core import result as result_module
from cmcloud_adapters import idaas_story_memory_update
from cmcloud_workflows import common, s27_album_list

# 判别字段由命令 Schema 固定；此处再次校验，防止被注册表以外的入口绕过。
KIND = "story"

# 故事列表项中的封面字段名待真实样本冻结；不按别名猜测
COVER_FIELD = "cover"


def prepare(context, input_data):
    if input_data.get("kind") != KIND:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "kind 必须为 %s" % KIND)
    story_id = context.real_id(input_data["storyRef"], "story")
    changes = {}
    if "name" in input_data:
        changes["name"] = _require_non_empty(input_data["name"], "name")
    if "secondTitle" in input_data:
        changes["secondTitle"] = _require_non_empty(input_data["secondTitle"], "secondTitle")
    if "coverRef" in input_data:
        changes["cover"] = common.real_id_for_kinds(context, input_data["coverRef"], ("file", "image"), "coverRef")
    if not changes:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "至少提供 name、secondTitle 或 coverRef 一项"
        )
    current = _current_story(context, story_id)
    diff = {key: value for key, value in changes.items() if current.get(key) != value}
    return {
        "summary": "修改故事 %s 的 %s" % (current.get("name") or story_id, "、".join(sorted(diff)) or "无变化字段"),
        "resolvedIds": {"storyId": story_id, "changes": changes, "ownerType": context.publication.album_owner_type},
        "kind": KIND,
        "storyRef": input_data["storyRef"],
        "current": current,
        "unchanged": sorted(set(changes) - set(diff)),
    }


def execute(context, plan):
    changes = plan["resolvedIds"]["changes"]
    # 确认期间对象可能被删除；写接口调用前再次核验，避免执行陈旧计划。
    _current_story(context, plan["resolvedIds"]["storyId"])
    kwargs = {"album_id": plan["resolvedIds"]["storyId"]}
    # 下游是否支持 PATCH 语义未冻结。使用读取到的当前值合并变更，至少始终
    # 携带有效 name，避免“只改副标题”被下游按完整对象校验时判为名称非法。
    merged = dict(plan.get("current") or {})
    merged.update(changes)
    kwargs["name"] = _require_non_empty(merged.get("name"), "name")
    if isinstance(merged.get("secondTitle"), str) and merged["secondTitle"]:
        kwargs["second_title"] = merged["secondTitle"]
    if isinstance(merged.get("cover"), str) and merged["cover"]:
        kwargs["cover"] = merged["cover"]
    if plan["resolvedIds"]["ownerType"] is not None:
        kwargs["owner_type"] = plan["resolvedIds"]["ownerType"]
    update_payload = common.guard_upstream(context, idaas_story_memory_update.call, **kwargs)
    common.json_success_data(context, update_payload, required=False)
    context.note_step("update", "已提交故事更新")
    after = _current_story(context, plan["resolvedIds"]["storyId"])
    verified = all(after.get(key) == value for key, value in changes.items())
    return common.success(
        data={
            "storyRef": plan["storyRef"],
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


def _current_story(context, story_id):
    objects, checked = s27_album_list.lookup_objects(context, KIND, [story_id])
    if not checked:
        raise result_module.ExecutionError(
            result_module.FAILED, "已到故事列表扫描上限，无法核验该故事；请从故事列表重新选择"
        )
    item = objects.get(story_id)
    if item is None:
        raise result_module.ExecutionError(
            result_module.NOT_FOUND, "该故事已不在当前故事列表中，请重新查询并选择要修改的故事"
        )
    _require_non_empty(item.get("name"), "当前故事名称")
    cover = item.get(COVER_FIELD)
    if isinstance(cover, dict):
        cover = cover.get("fileId")
    return {
        "name": item["name"],
        "secondTitle": item.get("secondTitle"),
        "cover": cover,
        "albumId": story_id,
    }
