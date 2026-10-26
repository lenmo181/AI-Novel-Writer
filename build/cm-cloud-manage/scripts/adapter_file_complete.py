import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/personalSaas/file/complete"
_TIMEOUT_SECONDS = 15

_HASH_PATTERN = re.compile(r"\A[0-9a-fA-F]{64}\Z")
# 摘要算法必须与创建上传任务时一致：pds 底座只认 sha1、云空间底座只认 sha256，
# 调用方按发布配置传入；缺省保持 sha256（既有行为）。
_HASH_PATTERNS = {
    "sha1": re.compile(r"\A[0-9a-fA-F]{40}\Z"),
    "sha256": _HASH_PATTERN,
}
_HASH_LENGTHS = {"sha1": 40, "sha256": 64}


class FileCompleteError(RuntimeError):
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


def call(*, file_id, upload_id, content_hash, content_hash_algorithm="sha256", request_headers):
    _require_id(file_id, "file_id")
    _require_id(upload_id, "upload_id")
    algorithm = _require_algorithm(content_hash_algorithm)
    if not isinstance(content_hash, str) or _HASH_PATTERNS[algorithm].fullmatch(content_hash) is None:
        raise FileCompleteError(
            "content_hash 必须是 %d 位十六进制 %s 摘要" % (_HASH_LENGTHS[algorithm], algorithm.upper())
        )
    body = {
        "fileId": file_id,
        "uploadId": upload_id,
        "contentHash": content_hash,
        "contentHashAlgorithm": algorithm,
    }
    payload = _post(body, request_headers)
    # 文档未定义可依赖的业务 data；成功时返回完整平台响应，文件详情由调用方另行调用查询详情接口核验
    return payload


def _require_algorithm(value):
    if not isinstance(value, str) or value.strip().lower() not in _HASH_PATTERNS:
        raise FileCompleteError("content_hash_algorithm 仅支持 sha1/sha256")
    return value.strip().lower()


def _require_id(value, name):
    if not isinstance(value, str) or not value or len(value) > 64:
        raise FileCompleteError("%s 必须是长度不超过 64 的非空字符串" % name)


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise FileCompleteError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise FileCompleteError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise FileCompleteError("request_headers 的值必须是字符串")
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
        raise FileCompleteError("请求地址校验失败")
    return url


def _failure_message(code, message):
    return "平台返回失败(code=%s): %s" % (code, str(message) if message is not None else "无平台消息")


def _validate_response(payload):
    success = payload.get("success")
    code = payload.get("code")
    message = payload.get("message")
    if success is None and code is None:
        raise FileCompleteError("平台响应缺少 success 与 code，不能判定为成功")
    if success is not None and str(success).strip().lower() not in ("true", "1"):
        raise FileCompleteError(_failure_message(code, message), code=code)
    if code is not None and str(code).strip().lower() not in ("0000", "0"):
        raise FileCompleteError(_failure_message(code, message), code=code)
    return payload


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
        raise FileCompleteError(
            "HTTP 状态码 %s: %s" % (err.code, str(message) if message is not None else "无平台消息"),
            code=code,
            http_status=err.code,
        )
    except OSError as err:
        raise FileCompleteError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise FileCompleteError("HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise FileCompleteError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise FileCompleteError("平台响应不是 JSON 对象")
    return _validate_response(upstream_trace.attach(payload, trace_id))
