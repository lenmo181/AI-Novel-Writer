import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/search/SearchSubscribeShareCode"
_TIMEOUT_SECONDS = 15

_TYPE_LIST = ("0", "1", "2", "3", "4", "100")
_TIME_FIELDS = ("shareTime", "subscribeTime")


class IdaasSearchSubscribeShareCodeError(RuntimeError):
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
    # userId 由服务端覆盖；已确认 code 为字符串、"0" 表示成功（若后续新增业务判码须按字符串 "0"）；
    # data.total/pageAfter/rows/count/requestId 均为可选字段，分页消费者只读取已出现字段；返回完整响应并保留未知字段
    return payload


def _require_conditions(value):
    if not isinstance(value, Mapping):
        raise IdaasSearchSubscribeShareCodeError("conditions 必须是映射")
    conditions = dict(value)
    if "userId" in conditions:
        raise IdaasSearchSubscribeShareCodeError("conditions.userId 由服务端覆盖，不接受调用方传入")
    # 契约 SubscribeShareCodeConditionDTO：keyword 为可选筛选维度，未提供时不发送该字段
    if "keyword" in conditions and (not isinstance(conditions["keyword"], str) or not conditions["keyword"]):
        raise IdaasSearchSubscribeShareCodeError("keyword 必须是非空字符串")
    if "typeList" in conditions:
        type_list = conditions["typeList"]
        if isinstance(type_list, (str, bytes)) or not isinstance(type_list, (list, tuple)):
            raise IdaasSearchSubscribeShareCodeError("typeList 必须是字符串列表")
        for item in type_list:
            if item not in _TYPE_LIST:
                raise IdaasSearchSubscribeShareCodeError("typeList 取值不在文档枚举内")
        conditions["typeList"] = list(type_list)
    if "timeList" in conditions:
        time_list = conditions["timeList"]
        if isinstance(time_list, (str, bytes)) or not isinstance(time_list, (list, tuple)):
            raise IdaasSearchSubscribeShareCodeError("timeList 必须是列表")
        if len(time_list) > 3:
            raise IdaasSearchSubscribeShareCodeError("timeList 最多 3 条")
        items = []
        for entry in time_list:
            if not isinstance(entry, Mapping):
                raise IdaasSearchSubscribeShareCodeError("timeList 每项必须是映射")
            item = dict(entry)
            filed = item.get("filed")
            if filed is None or filed not in _TIME_FIELDS:
                raise IdaasSearchSubscribeShareCodeError("timeList.filed 仅支持 shareTime/subscribeTime")
            # 契约 SubscribeShareCodeTimeRangeDTO 把 startTime/endTime 都标为必填，不接受半开区间
            for key in ("startTime", "endTime"):
                if not isinstance(item.get(key), str) or not item[key]:
                    raise IdaasSearchSubscribeShareCodeError("timeList.%s 必须是契约必填的非空字符串" % key)
            items.append(item)
        conditions["timeList"] = items
    return conditions


def _require_show_info(value):
    if not isinstance(value, Mapping):
        raise IdaasSearchSubscribeShareCodeError("show_info 必须是映射")
    show_info = dict(value)
    if "pageAfter" in show_info:
        page_after = show_info["pageAfter"]
        if isinstance(page_after, (str, bytes)) or not isinstance(page_after, (list, tuple)):
            raise IdaasSearchSubscribeShareCodeError("pageAfter 必须是数组")
        show_info["pageAfter"] = list(page_after)
    if "pageSize" in show_info:
        page_size = show_info["pageSize"]
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 0 < page_size <= 100:
            raise IdaasSearchSubscribeShareCodeError("pageSize 必须是 1~100 的整数")
    return show_info


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise IdaasSearchSubscribeShareCodeError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise IdaasSearchSubscribeShareCodeError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise IdaasSearchSubscribeShareCodeError("request_headers 的值必须是字符串")
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
        raise IdaasSearchSubscribeShareCodeError("请求地址校验失败")
    return url


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
        raise IdaasSearchSubscribeShareCodeError(
            "HTTP 状态码 %s: %s" % (err.code, str(fields[1]) if fields[1] is not None else "无平台消息"),
            code=fields[0],
            http_status=err.code,
        )
    except OSError as err:
        raise IdaasSearchSubscribeShareCodeError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise IdaasSearchSubscribeShareCodeError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
    if content_type is not None and "json" not in content_type.lower():
        raise IdaasSearchSubscribeShareCodeError("响应媒体类型不是 JSON: %s" % content_type)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise IdaasSearchSubscribeShareCodeError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise IdaasSearchSubscribeShareCodeError("平台响应不是 JSON 对象")
    return upstream_trace.attach(payload, trace_id)
