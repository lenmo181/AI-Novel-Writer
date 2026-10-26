import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/personalSaas/file/get"
_TIMEOUT_SECONDS = 15

_THUMBNAIL_STYLES = ("Small", "Middle", "Big", "Large")


class FileGetError(RuntimeError):
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


def call(*, file_id, thumbnail_style_list=None, expire_sec=None, request_headers):
    _require_id(file_id, "file_id")
    body = {"fileId": file_id}
    if thumbnail_style_list is not None:
        styles = _require_thumbnail_styles(thumbnail_style_list)
        if styles:
            body["thumbnailStyleList"] = styles
    if expire_sec is not None:
        _require_expire_sec(expire_sec)
        body["expireSec"] = expire_sec
    payload = _post(body, request_headers)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise FileGetError("响应缺少业务 data 对象")
    for key in ("fileId", "parentFileId", "name"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise FileGetError("响应缺少字符串字段: %s" % key)
    if data.get("type") not in ("file", "folder"):
        raise FileGetError("响应 type 取值非法")
    # 文档未提供完整路径单字段，调用方需沿 parentFileId 逐级查询，不得按名称拼接路径
    return payload


def _require_id(value, name):
    if not isinstance(value, str) or not value or len(value) > 64:
        raise FileGetError("%s 必须是长度不超过 64 的非空字符串" % name)


def _require_thumbnail_styles(value):
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise FileGetError("thumbnail_style_list 必须是列表")
    for item in value:
        if item not in _THUMBNAIL_STYLES:
            raise FileGetError("thumbnail_style_list 仅支持 Small/Middle/Big/Large")
    return list(value)


def _require_expire_sec(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 86400:
        raise FileGetError("expire_sec 必须是 1~86400 的整数（秒）")


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise FileGetError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise FileGetError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise FileGetError("request_headers 的值必须是字符串")
        headers[key] = item
    return headers


def _build_url():
    config = platform_config.load_platform_config()
    base = config.require("personal_saas_base_url").rstrip("/")
    url = base + _PATH
    parsed = urllib.parse.urlsplit(url)
    base_parsed = urllib.parse.urlsplit(base)
    # 地址来自 EXE 旁 cmcloud.json 的受批准配置，不接受任何调用参数覆盖
    if parsed.scheme != "https" or parsed.netloc != base_parsed.netloc:
        raise FileGetError("请求地址校验失败")
    return url


def _failure_message(code, message):
    return "平台返回失败(code=%s): %s" % (code, str(message) if message is not None else "无平台消息")


def _validate_response(payload):
    success = payload.get("success")
    code = payload.get("code")
    message = payload.get("message")
    if success is None and code is None:
        raise FileGetError("平台响应缺少 success 与 code，不能判定为成功")
    if success is not None and str(success).strip().lower() not in ("true", "1"):
        raise FileGetError(_failure_message(code, message), code=code)
    if code is not None and str(code).strip().lower() not in ("0000", "0"):
        raise FileGetError(_failure_message(code, message), code=code)
    return payload


def _post(body, request_headers):
    url = _build_url()
    payload_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
    headers.update(_require_headers(request_headers))
    request = core_control_http.query_request(url, data=payload_bytes, headers=headers)  # 只读查询：显式声明，可安全重放
    try:
        response = _OPENER.open(request, timeout=_TIMEOUT_SECONDS)
        try:
            status = response.getcode()
            trace_id = upstream_trace.read(response.headers)
            raw = response.read()
        finally:
            response.close()
    except urllib.error.HTTPError as err:
        code = message = None
        try:
            failed = json.loads(err.read().decode("utf-8"))
            if isinstance(failed, dict):
                code = failed.get("code")
                message = failed.get("message")
        except (UnicodeDecodeError, ValueError):
            pass
        raise FileGetError(
            "HTTP 状态码 %s: %s" % (err.code, str(message) if message is not None else "无平台消息"),
            code=code,
            http_status=err.code,
        )
    except OSError as err:
        raise FileGetError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise FileGetError("HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise FileGetError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise FileGetError("平台响应不是 JSON 对象")
    return _validate_response(upstream_trace.attach(payload, trace_id))
