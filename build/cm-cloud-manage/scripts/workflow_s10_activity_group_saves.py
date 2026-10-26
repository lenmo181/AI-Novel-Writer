# S10 圈子转存动态：当前已冻结契约只证明 dynamicType=6 表示圈子转存。
# 流程：固定圈子来源 -> 查询动态类型 6 -> 输出已证明结果。
# CLI: cmcloud activity list --eventKinds group-save --format json
# 群组、家庭云尚无独立请求枚举或响应判别字段，不在当前场景中承诺。

from cmcloud_core import result as result_module
from cmcloud_workflows import common

DYNAMIC_TYPE = common.DYNAMIC_TYPE_GROUP

SOURCE_KIND = "circle"


def run(context, input_data):
    source_kind = input_data.get("sourceKind") or SOURCE_KIND
    if source_kind != SOURCE_KIND:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "sourceKind 当前仅支持 circle")
    window = common.activity_window(
        context, input_data, digest_extra={"sourceKind": source_kind}, dynamic_type=DYNAMIC_TYPE
    )
    items, next_cursor, total = common.activity_page(context, window)
    return common.activity_outcome(
        context,
        window,
        items,
        next_cursor,
        total=total,
        extra={"dynamicType": DYNAMIC_TYPE, "sourceKind": source_kind},
    )
