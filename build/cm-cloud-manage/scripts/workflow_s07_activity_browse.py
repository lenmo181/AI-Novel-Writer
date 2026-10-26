# S07 听过动态：查询用户听过/听歌产生的播放动态。
# 流程：条件校验 -> 固定类型 9 -> 查询动态 -> 受控分页输出。
# CLI: cmcloud activity list --eventKinds browse --format json

from cmcloud_workflows import common

# 接口材料中的“版权播放”只是 dynamicType=9 的技术枚举名，对外统一表述为听过/听歌动态
DYNAMIC_TYPE = common.DYNAMIC_TYPE_PLAY


def run(context, input_data):
    window = common.activity_window(context, input_data, dynamic_type=DYNAMIC_TYPE)
    items, next_cursor, total = common.activity_page(context, window)
    return common.activity_outcome(
        context, window, items, next_cursor, total=total, extra={"dynamicType": DYNAMIC_TYPE}
    )
