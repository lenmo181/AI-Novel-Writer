import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/personalSaas/album/photo/customization/update"
_TIMEOUT_SECONDS = 15


class IdaasPhotoCustomizationUpdateError(RuntimeError):
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


def call(*, album_id, name=None, file_id=None, owner_type=None, request_headers):
    body = {"albumId": _require_text(album_id, "album_id")}
    if name is not None:
        body["name"] = _require_text(name, "name")
    if file_id is not None:
        body["fileId"] = _require_text(file_id, "file_id")
    if owner_type is not None:
        body["ownerType"] = _require_text(owner_type, "owner_type")
    payload = _post(body, request_headers)
    # userId/belongUserId/updateUserId 由服务端覆盖；name、fileId 的组合含义以相册协议为准；
    # 成功样例仅 success:true/code:"0000"/message，不要求 data
    return payload


def _require_text(value, name):
    if not isinstance(value, str) or not value:
        raise IdaasPhotoCustomizationUpdateError("%s 必须是非空字符串" % name)
    return value


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise IdaasPhotoCustomizationUpdateError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise IdaasPhotoCustomizationUpdateError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise IdaasPhotoCustomizationUpdateError("request_headers 的值必须是字符串")
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
        raise IdaasPhotoCustomizationUpdateError("请求地址校验失败")
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
        raise IdaasPhotoCustomizationUpdateError(
            "HTTP 状态码 %s: %s" % (err.code, str(fields[1]) if fields[1] is not None else "无平台消息"),
            code=fields[0],
            http_status=err.code,
        )
    except OSError as err:
        raise IdaasPhotoCustomizationUpdateError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise IdaasPhotoCustomizationUpdateError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
    if content_type is not None and "json" not in content_type.lower():
        raise IdaasPhotoCustomizationUpdateError("响应媒体类型不是 JSON: %s" % content_type)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise IdaasPhotoCustomizationUpdateError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise IdaasPhotoCustomizationUpdateError("平台响应不是 JSON 对象")
    return upstream_trace.attach(payload, trace_id)
