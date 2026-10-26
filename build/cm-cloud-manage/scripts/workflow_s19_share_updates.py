# S19 外链更新数量：查询某个已订阅外链的真实更新回执。
# 流程：订阅引用解析 -> 查询更新 -> 按已验收口径输出数量。
# CLI: cmcloud share updates --format json

from cmcloud_adapters import idaas_get_update_assets
from cmcloud_workflows import common


def run(context, input_data):
    link_id = context.real_id(input_data["linkRef"], "link")
    payload = common.guard_upstream(context, idaas_get_update_assets.call, link_id=link_id)
    data = common.payload_data(context, payload)
    count = common.field_int(context, data, "count")
    # 待转存数量口径未验收：返回 null，不填 0
    pending = data.get("pendingCount")
    if isinstance(pending, bool) or not isinstance(pending, int):
        pending = None
    result_data = {
        "linkRef": context.issue_ref("link", link_id, common.SCOPE_OUTER_LINK),
        "updateCount": count,
        "pendingCount": pending,
    }
    if count == 0:
        return common.empty(data=result_data)
    return common.success(data=result_data, counts={"updates": count})
