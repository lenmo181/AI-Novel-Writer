# S25 浏览外链：浏览外链根目录或真实子目录中的文件和目录。
# 流程：链接/目录引用校验 -> XML 区间查询 -> 解析目录文件 -> 受控分页输出。
# CLI: cmcloud share browse --format json

from cmcloud_core import result as result_module
from cmcloud_adapters import idaas_get_out_link_info
from cmcloud_workflows import common

MAX_RANGE = 200
DEFAULT_RANGE = 50

OUT_LINK_CONTENT_PARSER_READY = True


def run(context, input_data):
    if not OUT_LINK_CONTENT_PARSER_READY:
        raise result_module.ExecutionError(
            result_module.CAPABILITY_UNAVAILABLE, "外链内容结构化解析缺失，无法输出目录与文件列表"
        )
    supplied_ref = input_data.get("linkRef")
    link_id = context.real_id(supplied_ref, "link") if supplied_ref else common.parse_share_link_url(input_data["linkUrl"])
    link_ref = supplied_ref or context.issue_ref("link", link_id, common.SCOPE_OUTER_LINK)
    begin, end = _range(input_data)
    # 现网根目录查询实际要求显式传小写 root；省略 pCaID 会返回 200000400。
    # 子目录场景随后用 S25 返回的 catalogRef 覆盖该值。
    kwargs = {"link_id": link_id, "p_ca_id": "root", "b_num": begin, "e_num": end}
    if input_data.get("passwd"):
        kwargs["passwd"] = input_data["passwd"]
    if input_data.get("parentCatalogRef"):
        token = input_data["parentCatalogRef"]
        catalog_id = context.real_id(token, "catalog")
        entry = context.ref_entry(token)
        if entry.get("sourceCommand") != "share.browse" or entry.get("parentRealId") != link_id:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "目录引用不属于当前外链")
        path = entry.get("resourcePath")
        if not _valid_catalog_path(path, catalog_id):
            raise result_module.ExecutionError(
                result_module.INVALID_ARGUMENT, "目录引用缺少完整路径，请重新浏览外链并使用新返回的目录引用"
            )
        kwargs["p_ca_id"] = path
    _sort_kwargs(context, input_data.get("sort"), kwargs)
    payload = common.guard_upstream(context, idaas_get_out_link_info.call, **kwargs)
    if payload.get("status") == "CONFIRMED_FAILURE":
        raise result_module.ExecutionError(result_module.FAILED, "外链浏览查询失败")
    if payload.get("status") != "CONFIRMED_SUCCESS":
        common.contract_mismatch(context, "外链浏览结果业务码尚未确认")
    data = payload.get("data")
    if not isinstance(data, dict):
        common.contract_mismatch(context, "外链浏览响应缺少结构化 data")
    catalogs = [_catalog(context, item, link_id) for item in data.get("catalogs", [])]
    contents = [_content(context, item, link_id) for item in data.get("contents", [])]
    items = catalogs + contents
    total = data.get("nodeCount") if isinstance(data.get("nodeCount"), int) else None
    page = common.page_info(
        end - begin + 1,
        len(items),
        total=total,
        has_more=(end < total) if total is not None else None,
        complete=(end >= total) if total is not None else False,
    )
    output = {
        "linkRef": link_ref,
        "items": items,
        "parentCatalogId": data.get("parentCatalogId"),
        "linkName": data.get("linkName"),
    }
    if not items:
        return common.empty(data=output, page=page)
    return common.success(data=output, counts={"returned": len(items)}, page=page)


def _catalog(context, item, link_id):
    if not isinstance(item, dict) or not isinstance(item.get("caID"), str) or not item.get("caID"):
        common.contract_mismatch(context, "外链目录项缺少 caID")
    path = item.get("path")
    if not _valid_catalog_path(path, item["caID"]):
        common.contract_mismatch(context, "外链目录项缺少合法完整路径")
    return {
        "type": "folder",
        "catalogRef": context.issue_ref(
            "catalog", item["caID"], common.SCOPE_OUTER_LINK, parent_real_id=link_id,
            resource_path=path,
        ),
        "name": item.get("caName"),
        "createdAt": item.get("ctTime"),
        "updatedAt": item.get("udTime"),
        "path": item.get("path"),
    }


def _valid_catalog_path(path, catalog_id):
    if not isinstance(path, str):
        return False
    parts = path.split("/")
    return len(parts) >= 2 and parts[-1] == catalog_id and all(
        part.strip() and part not in (".", "..") for part in parts
    )


def _content(context, item, link_id):
    if not isinstance(item, dict) or not isinstance(item.get("coID"), str) or not item.get("coID"):
        common.contract_mismatch(context, "外链文件项缺少 coID")
    size = item.get("coSize")
    try:
        size = int(size) if size not in (None, "") else None
    except (TypeError, ValueError):
        size = None
    return {
        "type": "file",
        "contentRef": context.issue_ref(
            "content", item["coID"], common.SCOPE_OUTER_LINK, parent_real_id=link_id
        ),
        "name": item.get("coName"),
        "contentType": item.get("coType"),
        "extension": item.get("coSuffix"),
        "size": size,
        "updatedAt": item.get("udTime"),
        "path": item.get("path"),
    }


def _range(input_data):
    begin = input_data.get("begin") or 1
    end = input_data.get("end") or (begin + DEFAULT_RANGE - 1)
    if end < begin:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "begin/end 必须成对且 end 不小于 begin")
    if end - begin + 1 > MAX_RANGE:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "单次区间不得超过 %d 条" % MAX_RANGE)
    return begin, end


def _sort_kwargs(context, sort, kwargs):
    if not sort:
        return
    # getOutLinkInfo 契约：目录仅支持修改时间/名称，文件另支持类型/大小。
    # 类型/大小排序只作用于文件，目录按修改时间排序；两类仍分组输出。
    kwargs.update(
        ca_srt="1" if sort["by"] == "name" else "0",
        co_srt={"time": "0", "name": "1", "type": "2", "size": "3"}[sort["by"]],
        srt_dr="1" if sort.get("order", "asc") == "desc" else "0",
    )
