# S16 创建公开分享：为指定文件或目录创建公开外链。
# 流程：资源核验 -> 分享 DTO/计划 -> 可信确认 -> 创建外链 -> 回执核验。
# CLI: cmcloud share create --format json

from cmcloud_adapters import file_get, idaas_get_out_link_new
from cmcloud_workflows import common

PUB_TYPE_PUBLIC = 1
PERIOD_UNIT_TO_CODE = {"day": 1}

def prepare(context, input_data):
    file_ids, folder_ids = _split_resources(context, input_data["resources"])
    validity = input_data.get("validity") or {}
    plan = {
        "summary": "为 %d 个文件、%d 个目录创建公开外链" % (len(file_ids), len(folder_ids)),
        "resolvedIds": {"coIdList": file_ids, "caIdList": folder_ids},
        "resources": list(input_data["resources"]),
        "pubType": PUB_TYPE_PUBLIC,
        "encrypt": input_data.get("encrypt"),
        "period": validity.get("period"),
        "periodUnit": validity.get("periodUnit"),
    }
    return plan


def execute(context, plan):
    kwargs = {"pub_type": plan["pubType"]}
    if plan["resolvedIds"]["coIdList"]:
        kwargs["co_id_list"] = plan["resolvedIds"]["coIdList"]
    if plan["resolvedIds"]["caIdList"]:
        kwargs["ca_id_list"] = plan["resolvedIds"]["caIdList"]
    if plan["encrypt"] is not None:
        kwargs["encrypt"] = int(plan["encrypt"])
    if plan["period"] is not None:
        kwargs["period"] = plan["period"]
        kwargs["period_unit"] = PERIOD_UNIT_TO_CODE[plan["periodUnit"]]
    payload = common.guard_upstream(context, idaas_get_out_link_new.call, **kwargs)
    data = common.payload_data(context, payload)
    links = data.get("links")
    if not isinstance(links, list) or not links:
        common.contract_mismatch(context, "公开外链回执缺少 links")
    output = []
    for item in links:
        if not isinstance(item, dict):
            common.contract_mismatch(context, "公开外链回执 links 项不是对象")
        link_id = common.field_text(context, item, "linkId")
        link_url = common.field_text(context, item, "linkUrl")
        row = dict(item)
        row.pop("linkId", None)
        row["linkRef"] = context.issue_ref("link", link_id, common.SCOPE_OUTER_LINK)
        row["linkUrl"] = link_url
        output.append(row)
    context.note_step("create", "已请求创建公开外链")
    return common.success(data={"linkCreated": True, "links": output}, counts={"created": len(output)})


def _split_resources(context, resources):
    file_ids = []
    folder_ids = []
    for token in resources:
        entry = context.ref_entry(token)
        if entry["kind"] == "file":
            file_ids.append(entry["realId"])
            continue
        if entry["kind"] == "folder":
            folder_ids.append(entry["realId"])
            continue
        file_id = context.real_id(token, None)
        node = common.payload_data(
            context, common.guard_upstream(context, file_get.call, file_id=file_id)
        )
        if node.get("type") == "folder":
            folder_ids.append(file_id)
        elif node.get("type") == "file":
            file_ids.append(file_id)
        else:
            common.contract_mismatch(context, "分享资源 type 取值非法")
    return file_ids, folder_ids
