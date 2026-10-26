import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/personalSaas/file/listUploadedParts"
_TIMEOUT_SECONDS = 15


class FileListUploadedPartsError(RuntimeError):
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


def call(*, file_id, upload_id, page_info=None, request_headers):
    _require_id(file_id, "file_id")
    _require_id(upload_id, "upload_id")
    body = {"fileId": file_id, "uploadId": upload_id}
    page = _normalize_page_info(page_info)
    if page:
        body["pageInfo"] = page
    payload = _post(body, request_headers)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise FileListUploadedPartsError("响应缺少业务 data 对象")
    for key in ("fileId", "uploadId"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise FileListUploadedPartsError("响应缺少字符串字段: %s" % key)
    uploaded = data.get("uploadedParts")
    if not isinstance(uploaded, list):
        raise FileListUploadedPartsError("响应缺少 uploadedParts 列表")
    for item in uploaded:
        number = item.get("partNumber") if isinstance(item, dict) else None
        if not isinstance(item, dict) or isinstance(number, bool) or not isinstance(number, int):
            raise FileListUploadedPartsError("响应 uploadedParts 项缺少整数 partNumber")
    if _as_bool(data.get("parallelUpload")) is None:
        raise FileListUploadedPartsError("响应缺少布尔字段 parallelUpload")
    cursor = data.get("nextPageCursor")
    if cursor is not None and not isinstance(cursor, str):
        raise FileListUploadedPartsError("响应 nextPageCursor 类型非法")
    # 空 uploadedParts 不代表已查完，是否结束只依据 nextPageCursor 是否为空
    return payload


def _normalize_page_info(page_info):
    if page_info is None:
        return None
    if not isinstance(page_info, dict):
        raise FileListUploadedPartsError("page_info 必须是对象")
    normalized = {}
    for key, value in page_info.items():
        if key in ("page_size", "pageSize"):
            target = "pageSize"
        elif key in ("page_cursor", "pageCursor"):
            target = "pageCursor"
        else:
            raise FileListUploadedPartsError("page_info 含未支持字段: %s" % key)
        if value is None:
            continue
        if target == "pageSize":
            if isinstance(value, bool) or not isinstance(value, int) or not 10 <= value <= 100:
                raise FileListUploadedPartsError("page_info.page_size 必须是 10~100 的整数")
        elif not isinstance(value, str) or not value:
            raise FileListUploadedPartsError("page_info.page_cursor 必须是非空字符串")
        normalized[target] = value
    return normalized or None


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1"):
            return True
        if text in ("false", "0"):
            return False
    return None


def _require_id(value, name):
    if not isinstance(value, str) or not value or len(value) > 64:
        raise FileListUploadedPartsError("%s 必须是长度不超过 64 的非空字符串" % name)


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise FileListUploadedPartsError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise FileListUploadedPartsError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise FileListUploadedPartsError("request_headers 的值必须是字符串")
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
        raise FileListUploadedPartsError("请求地址校验失败")
    return url


def _failure_message(code, message):
    return "平台返回失败(code=%s): %s" % (code, str(message) if message is not None else "无平台消息")


def _validate_response(payload):
    success = payload.get("success")
    code = payload.get("code")
    message = payload.get("message")
    if success is None and code is None:
        raise FileListUploadedPartsError("平台响应缺少 success 与 code，不能判定为成功")
    if success is not None and str(success).strip().lower() not in ("true", "1"):
        raise FileListUploadedPartsError(_failure_message(code, message), code=code)
    if code is not None and str(code).strip().lower() not in ("0000", "0"):
        raise FileListUploadedPartsError(_failure_message(code, message), code=code)
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
        raise FileListUploadedPartsError(
            "HTTP 状态码 %s: %s" % (err.code, str(message) if message is not None else "无平台消息"),
            code=code,
            http_status=err.code,
        )
    except OSError as err:
        raise FileListUploadedPartsError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise FileListUploadedPartsError("HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise FileListUploadedPartsError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise FileListUploadedPartsError("平台响应不是 JSON 对象")
    return _validate_response(upstream_trace.attach(payload, trace_id))
