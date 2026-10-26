import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/search/SearchAIStory"
_TIMEOUT_SECONDS = 15

_STORY_TYPES = (0, 1)


class IdaasSearchAIStoryError(RuntimeError):
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
    if isinstance(code, bool) or not isinstance(code, int) or code != 0:
        raise IdaasSearchAIStoryError(_failure_message(code, payload.get("message")), code=code)
    for key in ("totalRows", "count"):
        if key in payload:
            value = payload[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise IdaasSearchAIStoryError("%s 必须是整数" % key)
    data = payload.get("data")
    if data is not None and not isinstance(data, list):
        raise IdaasSearchAIStoryError("data 必须是数组")
    # owner/userId 由服务端注入；返回原始 JSON 结构并保留未知字段
    return payload


def _require_conditions(value):
    if not isinstance(value, Mapping):
        raise IdaasSearchAIStoryError("conditions 必须是映射")
    conditions = dict(value)
    for key in ("owner", "userId"):
        if key in conditions:
            raise IdaasSearchAIStoryError("conditions.%s 由服务端注入，不接受调用方传入" % key)
    keywords = conditions.get("keywords")
    if keywords is not None:
        # 文档：keywords 未声明必填，未传时不向请求注入该字段；
        # 下游仅支持单关键字，服务层取第一个关键字透传给下游 keyword，"为兼容请只传一个关键词"
        if isinstance(keywords, (str, bytes)) or not isinstance(keywords, (list, tuple)) or not keywords:
            raise IdaasSearchAIStoryError("keywords 必须是非空字符串列表")
        if len(keywords) != 1:
            raise IdaasSearchAIStoryError("keywords 当前仅支持单关键字")
        for item in keywords:
            if not isinstance(item, str) or not item:
                raise IdaasSearchAIStoryError("keywords 每项必须是非空字符串")
        conditions["keywords"] = list(keywords)
    if "type" in conditions:
        story_type = conditions["type"]
        if isinstance(story_type, bool) or not isinstance(story_type, int) or story_type not in _STORY_TYPES:
            raise IdaasSearchAIStoryError("type 仅支持 0（个人云相册）/1（相册故事）")
    return conditions


def _require_show_info(value):
    if not isinstance(value, Mapping):
        raise IdaasSearchAIStoryError("show_info 必须是映射")
    show_info = dict(value)
    for key in ("startNum", "stopNum"):
        if key in show_info:
            number = show_info[key]
            if isinstance(number, bool) or not isinstance(number, int):
                raise IdaasSearchAIStoryError("%s 必须是整数" % key)
    if "returnTotalCountFlag" in show_info and not isinstance(show_info["returnTotalCountFlag"], bool):
        raise IdaasSearchAIStoryError("returnTotalCountFlag 必须是布尔值")
    if "sortInfos" in show_info:
        sort_infos = show_info["sortInfos"]
        if isinstance(sort_infos, (str, bytes)) or not isinstance(sort_infos, (list, tuple)):
            raise IdaasSearchAIStoryError("sortInfos 必须是列表")
        items = []
        for entry in sort_infos:
            if not isinstance(entry, Mapping):
                raise IdaasSearchAIStoryError("sortInfos 每项必须是映射")
            item = dict(entry)
            if "field" in item and (not isinstance(item["field"], str) or not item["field"]):
                raise IdaasSearchAIStoryError("sortInfos.field 必须是非空字符串")
            if "reverse" in item and not isinstance(item["reverse"], bool):
                raise IdaasSearchAIStoryError("sortInfos.reverse 必须是布尔值")
            if "fieldType" in item:
                field_type = item["fieldType"]
                if isinstance(field_type, bool) or not isinstance(field_type, int):
                    raise IdaasSearchAIStoryError("sortInfos.fieldType 必须是整数")
            items.append(item)
        show_info["sortInfos"] = items
    return show_info


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise IdaasSearchAIStoryError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise IdaasSearchAIStoryError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise IdaasSearchAIStoryError("request_headers 的值必须是字符串")
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
        raise IdaasSearchAIStoryError("请求地址校验失败")
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
        raise IdaasSearchAIStoryError(
            "HTTP 状态码 %s: %s" % (err.code, str(fields[1]) if fields[1] is not None else "无平台消息"),
            code=fields[0],
            http_status=err.code,
        )
    except OSError as err:
        raise IdaasSearchAIStoryError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise IdaasSearchAIStoryError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
    if content_type is not None and "json" not in content_type.lower():
        raise IdaasSearchAIStoryError("响应媒体类型不是 JSON: %s" % content_type)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise IdaasSearchAIStoryError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise IdaasSearchAIStoryError("平台响应不是 JSON 对象")
    return upstream_trace.attach(payload, trace_id)
