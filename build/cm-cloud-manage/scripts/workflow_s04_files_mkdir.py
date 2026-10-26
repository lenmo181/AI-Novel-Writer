# S04 新建文件夹：在用户指定的个人云目录创建一个普通文件夹。
# 流程：父目录核验 -> 可信确认 -> 创建目录 -> 回执核验。
# CLI: cmcloud files mkdir --format json

from cmcloud_adapters import file_create_folder
from cmcloud_workflows import common


def prepare(context, input_data):
    parent_id = context.real_id(input_data["parentRef"], "folder")
    common.ensure_directory(context, parent_id)
    name = input_data["name"]
    return {
        "summary": "在目标目录内新建文件夹 %s" % name,
        "resolvedIds": {"parentFileId": parent_id},
        "name": name,
        "parentRef": input_data["parentRef"],
    }


def execute(context, plan):
    payload = common.guard_upstream(
        context,
        file_create_folder.call,
        name=plan["name"],
        parent_file_id=plan["resolvedIds"]["parentFileId"],
        file_rename_mode="refuse",
    )
    data = common.payload_data(context, payload)
    folder_id = common.field_text(context, data, "fileId")
    name = data.get("name") if isinstance(data.get("name"), str) else plan["name"]
    context.note_step("create", "已创建目录")
    return common.success(
        data={"folderRef": context.issue_ref("folder", folder_id, common.SCOPE_AUTHORIZED_PERSONAL), "name": name},
        counts={"created": 1},
    )
