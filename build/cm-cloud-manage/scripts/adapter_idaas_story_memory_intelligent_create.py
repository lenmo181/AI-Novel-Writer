import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/personalSaas/album/story/memory/intelligent/create"
_TIMEOUT_SECONDS = 15

_EXTRACT_RANGES = (0, 1, 2, 3, 4)


class IdaasStoryMemoryIntelligentCreateError(RuntimeError):
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


def call(*, content, extract_range=None, extract_size=None, request_headers):
    body = {"content": _require_text(content, "content")}
    if extract_range is not None:
        range_value = _require_int(extract_range, "extract_range")
        if range_value not in _EXTRACT_RANGES:
            raise IdaasStoryMemoryIntelligentCreateError("extract_range 仅支持 0~4")
        body["extractRange"] = range_value
    if extract_size is not None:
        size = _require_int(extract_size, "extract_size")
        if size < 0:
            raise IdaasStoryMemoryIntelligentCreateError("extract_size 必须是非负整数")
        body["extractSize"] = size
    payload = _post(body, request_headers)
    success = payload.get("success")
    if success is not True:
        raise IdaasStoryMemoryIntelligentCreateError("success 非 true，不能判定为成功")
    code = payload.get("code")
    if not isinstance(code, str) or code != "0000":
        raise IdaasStoryMemoryIntelligentCreateError(_failure_message(code, payload.get("message")), code=code)
    data = payload.get("data")
    if data is not None:
        if not isinstance(data, dict):
            raise IdaasStoryMemoryIntelligentCreateError("data 必须是对象")
        album_info = data.get("albumInfo")
        if album_info is not None and not isinstance(album_info, dict):
            raise IdaasStoryMemoryIntelligentCreateError("data.albumInfo 必须是对象")
        search_files = data.get("searchFileList")
        if search_files is not None:
            if not isinstance(search_files, list):
                raise IdaasStoryMemoryIntelligentCreateError("data.searchFileList 必须是数组")
            if len(search_files) > 100:
                raise IdaasStoryMemoryIntelligentCreateError("data.searchFileList 不得超过 100 项")
            if any(not isinstance(item, dict) for item in search_files):
                raise IdaasStoryMemoryIntelligentCreateError("data.searchFileList 每项必须是对象")
    # userId、channelId 由服务端覆盖；返回原始 JSON 结构并保留未知字段
    return payload


def _require_text(value, name):
    if not isinstance(value, str) or not value:
        raise IdaasStoryMemoryIntelligentCreateError("%s 必须是非空字符串" % name)
    return value


def _require_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise IdaasStoryMemoryIntelligentCreateError("%s 必须是整数" % name)
    return value


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise IdaasStoryMemoryIntelligentCreateError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise IdaasStoryMemoryIntelligentCreateError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise IdaasStoryMemoryIntelligentCreateError("request_headers 的值必须是字符串")
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
        raise IdaasStoryMemoryIntelligentCreateError("请求地址校验失败")
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
    request = urllib.request.Request(url, data=payload_bytes, headers=headers, method="POST")
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
        raise IdaasStoryMemoryIntelligentCreateError(
            "HTTP 状态码 %s: %s" % (err.code, str(fields[1]) if fields[1] is not None else "无平台消息"),
            code=fields[0],
            http_status=err.code,
        )
    except OSError as err:
        raise IdaasStoryMemoryIntelligentCreateError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise IdaasStoryMemoryIntelligentCreateError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
    if content_type is not None and "json" not in content_type.lower():
        raise IdaasStoryMemoryIntelligentCreateError("响应媒体类型不是 JSON: %s" % content_type)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise IdaasStoryMemoryIntelligentCreateError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise IdaasStoryMemoryIntelligentCreateError("平台响应不是 JSON 对象")
    return upstream_trace.attach(payload, trace_id)
