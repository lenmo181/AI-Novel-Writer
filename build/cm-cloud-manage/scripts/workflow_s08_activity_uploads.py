# S08 上传动态：查询普通上传或美图上传产生的动态。
# 流程：来源映射 -> 分支查询动态 -> 按真实 ID 合并 -> 分支游标输出。
# CLI: cmcloud activity list --eventKinds upload --format json

from cmcloud_workflows import common

UPLOAD_KIND_TO_TYPE = {
    "normal": common.DYNAMIC_TYPE_UPLOAD_NORMAL,
    "beauty": common.DYNAMIC_TYPE_UPLOAD_BEAUTY,
}


def run(context, input_data):
    upload_kind = input_data.get("uploadKind")
    if upload_kind is None:
        # 未指定 uploadKind 时固定查询普通与美图两个分支，不纳入类型 8
        dynamic_types = [UPLOAD_KIND_TO_TYPE["normal"], UPLOAD_KIND_TO_TYPE["beauty"]]
    else:
        dynamic_types = [UPLOAD_KIND_TO_TYPE[upload_kind]]
    window = common.activity_window(
        context, input_data, digest_extra={"dynamicTypes": dynamic_types}, dynamic_type=dynamic_types[0]
    )
    items, next_cursor, total = common.activity_page(
        context, window, dynamic_types=dynamic_types if len(dynamic_types) > 1 else None
    )
    deduped = common.dedupe_by(items, common.stable_item_key)
    extra = {
        "dynamicTypes": dynamic_types,
        "uploadKind": upload_kind,
        # 本查询时间窗内是否取尽：上游不再下发后续游标且本轮无数据时判定取尽
        "complete": common.activity_terminal(deduped, next_cursor),
    }
    return common.activity_outcome(
        context,
        window,
        deduped,
        next_cursor,
        total=total,
        extra=extra,
    )
