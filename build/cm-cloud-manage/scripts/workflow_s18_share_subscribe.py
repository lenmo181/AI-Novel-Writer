# S18 订阅外链：订阅用户提供或已解析的他人分享链接。
# 流程：链接解析 -> 提取码校验 -> 可信确认 -> 创建订阅 -> 回执核验。
# CLI: cmcloud share subscribe --format json

from cmcloud_core import result as result_module
from cmcloud_adapters import idaas_sub_out_link
from cmcloud_workflows import common

def prepare(context, input_data):
    if input_data.get("linkRef"):
        link_id = context.real_id(input_data["linkRef"], "link")
        source = "linkRef"
    else:
        link_id = common.parse_share_link_url(input_data["linkUrl"])
        source = "linkUrl"
    return {
        "summary": "订阅指定外链并保存提取码",
        "resolvedIds": {"linkId": link_id},
        "source": source,
        "passwd": input_data["passwd"],
    }


def execute(context, plan):
    payload = common.guard_upstream(
        context, idaas_sub_out_link.call, link_id=plan["resolvedIds"]["linkId"], passwd=plan["passwd"]
    )
    data = common.payload_data(context, payload, required=False) or {}
    context.note_step("subscribe", "已请求创建订阅")
    return common.success(
        data={
            "linkRef": context.issue_ref("link", plan["resolvedIds"]["linkId"], common.SCOPE_OUTER_LINK),
            "subscribed": True,
            "counts": {key: data[key] for key in ("total", "successCount", "failCount") if key in data},
            "note": "订阅不等于转存，转存需另行执行 share save（saveKind=subscription-updates）",
        },
        counts={"subscribed": 1},
    )

