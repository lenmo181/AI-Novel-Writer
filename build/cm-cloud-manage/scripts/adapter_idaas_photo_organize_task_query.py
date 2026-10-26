import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/api/openclaw/photoOrganize/task/query"
_TIMEOUT_SECONDS = 15


class IdaasPhotoOrganizeTaskQueryError(RuntimeError):
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


def call(*, openclaw_id, task_id, user_id=None, request_headers):
    body = {
        "openclawId": _require_text(openclaw_id, "openclaw_id"),
        "taskId": _require_text(task_id, "task_id"),
    }
    if user_id is not None:
        # userId 省略时由服务端从 token 获取，第三方平台调用时必填
        body["userId"] = _require_text(user_id, "user_id")
    payload = _post(body, request_headers)
    # sourceChannel 无论是否传入都被渠道配置覆盖；样例 data.taskInfo/data.results，taskInfo.status 示例为整数 3；
    # 无状态枚举，不把 3 硬编码为成功、不借用 task_get 状态；保留完整任务结果
    return payload


def _require_text(value, name):
    if not isinstance(value, str) or not value:
        raise IdaasPhotoOrganizeTaskQueryError("%s 必须是非空字符串" % name)
    return value


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise IdaasPhotoOrganizeTaskQueryError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise IdaasPhotoOrganizeTaskQueryError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise IdaasPhotoOrganizeTaskQueryError("request_headers 的值必须是字符串")
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
        raise IdaasPhotoOrganizeTaskQueryError("请求地址校验失败")
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
        raise IdaasPhotoOrganizeTaskQueryError(
            "HTTP 状态码 %s: %s" % (err.code, str(fields[1]) if fields[1] is not None else "无平台消息"),
            code=fields[0],
            http_status=err.code,
        )
    except OSError as err:
        raise IdaasPhotoOrganizeTaskQueryError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise IdaasPhotoOrganizeTaskQueryError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
    if content_type is not None and "json" not in content_type.lower():
        raise IdaasPhotoOrganizeTaskQueryError("响应媒体类型不是 JSON: %s" % content_type)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise IdaasPhotoOrganizeTaskQueryError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise IdaasPhotoOrganizeTaskQueryError("平台响应不是 JSON 对象")
    return upstream_trace.attach(payload, trace_id)
