# S15 月度上传统计：按上游时间月份统计观察到的普通/美图上传事件。
# 流程：分支采集 -> 稳定标识去重 -> 按上游时间字符串分月汇总；不筛图片或转换时区。
# CLI: cmcloud activity stats --format json

from cmcloud_workflows import common

UPLOAD_KIND_TO_TYPE = {
    "normal": common.DYNAMIC_TYPE_UPLOAD_NORMAL,
    "beauty": common.DYNAMIC_TYPE_UPLOAD_BEAUTY,
}

# 月度统计的硬性分页兜底：一次 `activity stats` 命令内全部来源分支共享该额度，
# 不是每个分支各 500 页；达到额度即停止取页并标记不完整，不发第 501 次请求。
MAX_MONTHLY_STATS_PAGES = 500

def run(context, input_data):
    timezone_name = input_data.get("timezone") or "Asia/Shanghai"
    kinds = input_data.get("uploadKinds") or ["normal", "beauty"]
    dynamic_types = [UPLOAD_KIND_TO_TYPE[kind] for kind in kinds]
    window = common.activity_window(
        context, input_data, digest_extra={"dynamicTypes": dynamic_types}, dynamic_type=dynamic_types[0]
    )
    merged = []
    branch_complete = {}
    branch_stopped = {}
    # 全部分支共享同一份额度；顺序采集，前一分支耗尽额度后后续分支不再发请求。
    budget = {"remaining": MAX_MONTHLY_STATS_PAGES, "used": 0}
    for dynamic_type in dynamic_types:

        def fetch(cursor, dynamic_type=dynamic_type):
            branch_window = dict(window)
            branch_window["dynamicType"] = dynamic_type
            branch_window["nextPageCursor"] = cursor
            items, next_cursor, _total = common.activity_page(context, branch_window)
            return items, next_cursor

        # 月度统计的用途是给出完整计数，因此不沿用列表场景的 50 页保护；但为避免上游
        # 持续下发不同游标导致无限翻页，这里施加共享的 500 页硬性兜底（见 MAX_MONTHLY_STATS_PAGES）。
        collected = _collect_complete(context, fetch, budget)
        merged.extend(collected["items"])
        branch_complete[str(dynamic_type)] = collected["complete"]
        branch_stopped[str(dynamic_type)] = collected["stoppedBy"]
    deduped = common.dedupe_by(merged, common.stable_item_key)
    months = {}
    for item in deduped:
        value = None
        if isinstance(item, dict):
            for key in ("dynamicTime", "eventTime", "createTime", "takenTime"):
                if isinstance(item.get(key), str) and item[key]:
                    value = item[key]
                    break
        month = value[:7] if isinstance(value, str) and len(value) >= 7 else "unknown"
        months[month] = months.get(month, 0) + 1
    complete = all(branch_complete.values()) if branch_complete else True
    data = {
        "timezone": timezone_name,
        "from": window["startTime"],
        "to": window["endTime"],
        "observedEvents": len(merged),
        "observedUnique": len(deduped),
        "months": months,
        "branchComplete": branch_complete,
        "branchStopped": branch_stopped,
        "truncated": not complete,
        "truncatedBranches": sorted(
            name for name, done in branch_complete.items() if not done
        ),
        "complete": complete,
        "basis": "full-window" if complete else "observed-only",
        "pagesFetched": budget["used"],
        "maxPages": MAX_MONTHLY_STATS_PAGES,
    }
    page = common.page_info(
        window["pageSize"], len(deduped), total=None, has_more=None, next_cursor=None, complete=complete
    )
    return common.success(data=data, counts={"observed": len(deduped)}, page=page)


def _collect_complete(context, fetch_page, budget):
    """在共享页数额度内收集一个统计分支，直至上游明确结束或额度耗尽。

    统计需要精确月度口径，因此不套用 ``common.collect_all`` 的 50 页保护；改用调用方
    传入的共享 ``budget``（一次 stats 命令内所有分支共用）作为硬性兜底。每次真正取页前
    先扣减额度，空页只要实际发起也计入；额度耗尽即停止取页并回报 ``page-cap``，不发
    第 N+1 次请求。重复或非法游标仍按契约异常处理，不因触及上限被吞掉。
    """
    items = []
    cursor = None
    seen_cursors = set()
    while True:
        if budget["remaining"] <= 0:
            return {"items": items, "complete": False, "stoppedBy": "page-cap"}
        budget["remaining"] -= 1
        budget["used"] += 1
        page_items, next_cursor = fetch_page(cursor)
        items.extend(page_items)
        if not next_cursor:
            return {"items": items, "complete": True, "stoppedBy": "exhausted"}
        if not isinstance(next_cursor, str) or next_cursor in seen_cursors:
            common.contract_mismatch(context, "月度统计分页游标重复或格式非法")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
