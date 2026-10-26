import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/search/SearchNonFileShareRecord"
_TIMEOUT_SECONDS = 15

_RECORD_TYPES = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11")
# 契约 searchType：不传＝只搜 AI 通讯录分享记录；"O"/"0"＝搜 AI 通讯录分享外链；"1"＝搜订阅的外链分享记录。
# 契约原文写作字母 O，但字母 O 会让同组 SearchShareRecord 返回 code=500 System error（2026-09-19 实测），
# 这里只接受数字 "0"/"1"，避免把会在下游炸掉的字面量透传出去。
_SEARCH_TYPES = ("0", "1")


class IdaasSearchNonFileShareRecordError(RuntimeError):
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
    body = {
        "conditions": _require_conditions(conditions),
        "showInfo": _require_show_info(show_info),
    }
    payload = _post(body, request_headers)
    code = payload.get("code")
    if not isinstance(code, str) or code != "0":
        raise IdaasSearchNonFileShareRecordError(_failure_message(code, payload.get("message")), code=code)
    if payload.get("success") is not True:
        raise IdaasSearchNonFileShareRecordError("success 非 true，不能判定为成功")
    data = payload.get("data")
    if data is not None:
        if not isinstance(data, dict):
            raise IdaasSearchNonFileShareRecordError("data 必须是对象")
        # 现网 V3 网关把集合放在 data.rows，契约文档写作 data.data（2026-09-19 实测），两种口径都接受
        for key in ("rows", "data"):
            records = data.get(key)
            if records is None:
                continue
            if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
                raise IdaasSearchNonFileShareRecordError("data.%s 必须是对象数组" % key)
    # userId 由服务端覆盖；返回原始 JSON 结构并保留未知字段
    return payload


def _require_conditions(value):
    if not isinstance(value, Mapping):
        raise IdaasSearchNonFileShareRecordError("conditions 必须是映射")
    conditions = dict(value)
    if "userId" in conditions:
        raise IdaasSearchNonFileShareRecordError("conditions.userId 由服务端覆盖，不接受调用方传入")
    if "typeList" in conditions:
        type_list = conditions["typeList"]
        if isinstance(type_list, (str, bytes)) or not isinstance(type_list, (list, tuple)):
            raise IdaasSearchNonFileShareRecordError("typeList 必须是字符串列表")
        for item in type_list:
            if item not in _RECORD_TYPES:
                raise IdaasSearchNonFileShareRecordError("typeList 取值不在文档枚举内")
        conditions["typeList"] = list(type_list)
    # 契约把 keyword 标为必填；漏传或空串会被下游当作无效搜索（空串实测 code=401）
    keyword = conditions.get("keyword")
    if not isinstance(keyword, str) or not keyword:
        raise IdaasSearchNonFileShareRecordError("keyword 必须是契约必填的非空字符串")
    if "searchType" in conditions:
        search_type = conditions["searchType"]
        if not isinstance(search_type, str) or search_type not in _SEARCH_TYPES:
            raise IdaasSearchNonFileShareRecordError("searchType 仅支持 0/1（字母 O 不被接受）")
    return conditions


def _require_show_info(value):
    if not isinstance(value, Mapping):
        raise IdaasSearchNonFileShareRecordError("show_info 必须是映射")
    show_info = dict(value)
    if "pageAfter" in show_info:
        page_after = show_info["pageAfter"]
        if isinstance(page_after, (str, bytes)) or not isinstance(page_after, (list, tuple)):
            raise IdaasSearchNonFileShareRecordError("pageAfter 必须是数组")
        show_info["pageAfter"] = list(page_after)
    # 契约 pageNum 标为必填，但当前下游 DTO 不接收该字段（忽略或误判页码），续页只认 pageAfter
    if "pageNum" in show_info:
        raise IdaasSearchNonFileShareRecordError("showInfo.pageNum 当前 DTO 不接收，分页只使用 pageAfter")
    page_size = show_info.get("pageSize")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 0 < page_size <= 1000:
        raise IdaasSearchNonFileShareRecordError("pageSize 必须是契约必填的 1~1000 整数")
    return show_info


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise IdaasSearchNonFileShareRecordError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise IdaasSearchNonFileShareRecordError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise IdaasSearchNonFileShareRecordError("request_headers 的值必须是字符串")
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
        raise IdaasSearchNonFileShareRecordError("请求地址校验失败")
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
        raise IdaasSearchNonFileShareRecordError(
            "HTTP 状态码 %s: %s" % (err.code, str(fields[1]) if fields[1] is not None else "无平台消息"),
            code=fields[0],
            http_status=err.code,
        )
    except OSError as err:
        raise IdaasSearchNonFileShareRecordError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise IdaasSearchNonFileShareRecordError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
    if content_type is not None and "json" not in content_type.lower():
        raise IdaasSearchNonFileShareRecordError("响应媒体类型不是 JSON: %s" % content_type)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise IdaasSearchNonFileShareRecordError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise IdaasSearchNonFileShareRecordError("平台响应不是 JSON 对象")
    return upstream_trace.attach(payload, trace_id)
