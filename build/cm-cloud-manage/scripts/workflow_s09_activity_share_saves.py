# S09 分享转存动态：查询能够证明来源为分享的转存事件。
# 流程：查询类型 5 -> 校验分享来源标志 -> 输出已证明结果。
# CLI: cmcloud activity list --eventKinds share-save --format json

from cmcloud_workflows import common

DYNAMIC_TYPE = common.DYNAMIC_TYPE_COPY

def run(context, input_data):
    # 产品口径已确认：dynamicType=5 的全部事件均按分享转存处理。
    window = common.activity_window(context, input_data, dynamic_type=DYNAMIC_TYPE)
    items, next_cursor, total = common.activity_page(context, window)
    return common.activity_outcome(
        context, window, items, next_cursor, total=total, extra={"dynamicType": DYNAMIC_TYPE}
    )
