import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import file_types, platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/search/SearchSkillFile"
_TIMEOUT_SECONDS = 15

# 资产类型码表以 cmcloud/file_types.py 为唯一真源（schema 与本校验共用同一份）。
_TYPE_LIST = file_types.TYPE_CODES
_TIME_FIELDS = ("createdAt", "updatedAt", "takenAt")
_TIME_TEXT_LENGTH = 14
_PAGE_SIZE_MAX = 1000
_PAGE_SIZE_MAX_WITH_FILE_IDS = 5000


class IdaasSearchSkillFileError(RuntimeError):
    def __init__(self, message, *, code=None, http_status=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = core_control_http.build_opener(
    _NoRedirectHandler,
    urllib.request.HTTPSHandler(context=ssl.create_default_context()),
)


def call(*, conditions, show_info, request_headers):
    validated = _require_conditions(conditions)
    body = {
        "conditions": validated,
        "showInfo": _require_show_info(show_info, validated),
    }
    payload = _post(body, request_headers)
    code = payload.get("code")
    if not isinstance(code, str) or code != "0":
        raise IdaasSearchSkillFileError(_failure_message(code, payload.get("message")), code=code)
    if payload.get("success") is not True:
        raise IdaasSearchSkillFileError("success 非 true，不能判定为成功")
    data = payload.get("data")
    if data is not None:
        if not isinstance(data, dict):
            raise IdaasSearchSkillFileError("data 必须是对象")
        # 现网 V3 网关把集合放在 data.rows，契约文档写作 data.skillFileList（2026-09-19 实测），两种口径都接受
        for key in ("rows", "skillFileList"):
            candidate = data.get(key)
            if candidate is None:
                continue
            if not isinstance(candidate, list) or any(not isinstance(item, dict) for item in candidate):
                raise IdaasSearchSkillFileError("data.%s 必须是对象数组" % key)
        page_after = data.get("pageAfter")
        if page_after is not None and not _is_page_after(page_after):
            # 接口文档写作对象数组，但现网 SearchSkillFile 返回非空字符串数组。
            # 两种同构游标都可被下一页原样回传，不能因文档/现网差异丢弃成功响应。
            raise IdaasSearchSkillFileError("data.pageAfter 必须是对象数组或非空字符串数组")
        for key in ("total", "cont"):
            value = data.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise IdaasSearchSkillFileError("data.%s 必须是非负整数" % key)
    # userId 由服务端注入；返回原始 JSON 结构并保留未知字段
    return payload


def _require_conditions(value):
    if not isinstance(value, Mapping):
        raise IdaasSearchSkillFileError("conditions 必须是映射")
    conditions = dict(value)
    if "typeList" in conditions:
        type_list = _require_str_list(conditions["typeList"], "typeList", None)
        for item in type_list:
            if item not in _TYPE_LIST:
                raise IdaasSearchSkillFileError("typeList 取值不在文档枚举内")
        conditions["typeList"] = type_list
    if "extensionList" in conditions:
        conditions["extensionList"] = _require_str_list(conditions["extensionList"], "extensionList", None)
    for key, limit in (("addressList", 10), ("tagList", 10), ("includeFileIdList", 20), ("fileIdList", 5000)):
        if key in conditions:
            conditions[key] = _require_str_list(conditions[key], key, limit)
    if "timeList" in conditions:
        time_list = conditions["timeList"]
        if isinstance(time_list, (str, bytes)) or not isinstance(time_list, (list, tuple)):
            raise IdaasSearchSkillFileError("timeList 必须是列表")
        if len(time_list) > 10:
            raise IdaasSearchSkillFileError("timeList 最多 10 条")
        items = []
        for entry in time_list:
            if not isinstance(entry, Mapping):
                raise IdaasSearchSkillFileError("timeList 每项必须是映射")
            item = dict(entry)
            filed = item.get("filed")
            if filed not in _TIME_FIELDS:
                raise IdaasSearchSkillFileError("timeList.filed 必须是契约必填的 createdAt/updatedAt/takenAt")
            # 契约 timeList[].startTime/endTime 均为必填，格式 yyyyMMddHHmmss
            for key in ("startTime", "endTime"):
                text = item.get(key)
                if not isinstance(text, str) or len(text) != _TIME_TEXT_LENGTH or not text.isdigit():
                    raise IdaasSearchSkillFileError("timeList.%s 必须是契约必填的 yyyyMMddHHmmss" % key)
            items.append(item)
        conditions["timeList"] = items
    if "sizeRange" in conditions:
        size_range = conditions["sizeRange"]
        if not isinstance(size_range, Mapping):
            raise IdaasSearchSkillFileError("sizeRange 必须是映射")
        item = dict(size_range)
        for key in ("startSize", "endSize"):
            if key not in item:
                raise IdaasSearchSkillFileError("sizeRange.%s 是契约必填字段" % key)
            item[key] = _require_long(item[key], "sizeRange.%s" % key)
        conditions["sizeRange"] = item
    if "isMatchFolder" in conditions and not isinstance(conditions["isMatchFolder"], bool):
        raise IdaasSearchSkillFileError("isMatchFolder 必须是布尔值")
    if "keyword" in conditions and (not isinstance(conditions["keyword"], str) or not conditions["keyword"]):
        raise IdaasSearchSkillFileError("keyword 必须是非空字符串")
    if conditions.get("isMatchFolder") is True and not conditions.get("keyword"):
        raise IdaasSearchSkillFileError("isMatchFolder=true 时 keyword 必填")
    if "userId" in conditions:
        raise IdaasSearchSkillFileError("userId 由服务端覆盖，不接受调用方传入")
    _assert_condition_compatibility(conditions)
    return conditions


def _assert_condition_compatibility(conditions):
    types = set(conditions.get("typeList") or ())
    time_fields = {item.get("filed") for item in conditions.get("timeList", ()) if isinstance(item, Mapping)}
    if "takenAt" in time_fields and types != {"1"}:
        raise IdaasSearchSkillFileError("takenAt 仅支持 typeList 仅包含图片类型 1 的搜索")
    if "sizeRange" in conditions and not types:
        raise IdaasSearchSkillFileError("sizeRange 必须同时携带明确的文件类型 typeList")
    if "sizeRange" in conditions and "100" in types:
        raise IdaasSearchSkillFileError("sizeRange 不支持包含目录类型 100 的搜索")


def _require_show_info(value, conditions=None):
    if not isinstance(value, Mapping):
        raise IdaasSearchSkillFileError("show_info 必须是映射")
    show_info = dict(value)
    if "pageAfter" in show_info:
        page_after = show_info["pageAfter"]
        if isinstance(page_after, (str, bytes)) or not isinstance(page_after, (list, tuple)):
            raise IdaasSearchSkillFileError("pageAfter 必须是数组")
        show_info["pageAfter"] = list(page_after)
    # 契约 pageSize 必填：传 fileIdList 时上限 5000，其余不超过 1000
    max_page_size = _PAGE_SIZE_MAX_WITH_FILE_IDS if (conditions or {}).get("fileIdList") else _PAGE_SIZE_MAX
    page_size = show_info.get("pageSize")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 0 < page_size <= max_page_size:
        raise IdaasSearchSkillFileError("pageSize 必须是契约必填的 1~%d 整数" % max_page_size)
    return show_info


def _is_page_after(value):
    """SearchSkillFile 的分页游标兼容文档对象数组与现网字符串数组。"""
    if not isinstance(value, list):
        return False
    if not value:
        return True
    if all(isinstance(item, Mapping) for item in value):
        return True
    return all(isinstance(item, str) and item for item in value)


def _require_str_list(value, name, limit):
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise IdaasSearchSkillFileError("%s 必须是字符串列表" % name)
    if limit is not None and len(value) > limit:
        raise IdaasSearchSkillFileError("%s 最多 %d 条" % (name, limit))
    items = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise IdaasSearchSkillFileError("%s 每项必须是非空字符串" % name)
        items.append(item)
    return items


def _require_long(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise IdaasSearchSkillFileError("%s 必须是整数" % name)
    return value


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise IdaasSearchSkillFileError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise IdaasSearchSkillFileError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise IdaasSearchSkillFileError("request_headers 的值必须是字符串")
        headers[key] = item
    return headers


def _build_url():
    config = platform_config.load_platform_config()
    base = config.require("base_url").rstrip("/")
    url = base + _PATH
    parsed = urllib.parse.urlsplit(url)
    base_parsed = urllib.parse.urlsplit(base)
    # 地址来自 EXE 旁 cmcloud.json 的受批准配置，不接受任何调用参数覆盖
    if parsed.scheme != base_parsed.scheme or parsed.netloc != base_parsed.netloc:
        raise IdaasSearchSkillFileError("请求地址校验失败")
    return url


def _failure_message(code, message):
    return "平台返回失败(code=%s): %s" % (code, str(message) if message is not None else "无平台消息")


def _extract_error_fields(raw):
    try:
        failed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(failed, dict):
        return None
    return failed.get("code"), failed.get("message")


def _post(body, request_headers):
    url = _build_url()
    payload_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
    headers.update(_require_headers(request_headers))
    request = core_control_http.query_request(url, data=payload_bytes, headers=headers, method="POST")
    try:
        response = _OPENER.open(request, timeout=_TIMEOUT_SECONDS)
        try:
            status = response.getcode()
            trace_id = upstream_trace.read(response.headers)
            content_type = response.headers.get("Content-Type") if response.headers else None
            raw = response.read()
        finally:
            response.close()
    except urllib.error.HTTPError as err:
        fields = _extract_error_fields(err.read(65536)) or (None, None)
        raise IdaasSearchSkillFileError(
            "HTTP 状态码 %s: %s" % (err.code, str(fields[1]) if fields[1] is not None else "无平台消息"),
            code=fields[0],
            http_status=err.code,
        )
    except OSError as err:
        raise IdaasSearchSkillFileError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise IdaasSearchSkillFileError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
    if content_type is not None and "json" not in content_type.lower():
        raise IdaasSearchSkillFileError("响应媒体类型不是 JSON: %s" % content_type)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise IdaasSearchSkillFileError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise IdaasSearchSkillFileError("平台响应不是 JSON 对象")
    return upstream_trace.attach(payload, trace_id)
