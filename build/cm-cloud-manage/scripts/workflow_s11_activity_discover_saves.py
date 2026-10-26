# S11 发现转存动态：查询发现来源产生的转存事件。
# 流程：映射类型 7 -> 查询动态 -> 校验业务码 -> 受控分页输出。
# CLI: cmcloud activity list --eventKinds discover-save --format json

from cmcloud_workflows import common

DYNAMIC_TYPE = common.DYNAMIC_TYPE_DISCOVER


def run(context, input_data):
    window = common.activity_window(context, input_data, dynamic_type=DYNAMIC_TYPE)
    items, next_cursor, total = common.activity_page(context, window)
    return common.activity_outcome(
        context, window, items, next_cursor, total=total, extra={"dynamicType": DYNAMIC_TYPE}
    )
