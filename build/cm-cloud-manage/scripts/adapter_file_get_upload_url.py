import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/personalSaas/file/getUploadUrl"
_TIMEOUT_SECONDS = 15

_PART_KEYS = {
    "part_number": "partNumber",
    "partNumber": "partNumber",
    "part_size": "partSize",
    "partSize": "partSize",
    "part_offset": "partOffset",
    "partOffset": "partOffset",
    "parallel_hash_ctx": "parallelHashCtx",
    "parallelHashCtx": "parallelHashCtx",
}

_PARALLEL_CTX_KEYS = {
    "hash_ctx": "hashCtx",
    "hashCtx": "hashCtx",
    "part_offset": "partOffset",
    "partOffset": "partOffset",
}
_HASH_CTX_LENGTH = 8


class FileGetUploadUrlError(RuntimeError):
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


def call(*, file_id, upload_id, part_infos, request_headers):
    _require_id(file_id, "file_id")
    _require_id(upload_id, "upload_id")
    body = {
        "fileId": file_id,
        "uploadId": upload_id,
        "partInfos": _normalize_parts(part_infos),
    }
    payload = _post(body, request_headers)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise FileGetUploadUrlError("响应缺少业务 data 对象")
    for key in ("fileId", "uploadId"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise FileGetUploadUrlError("响应缺少字符串字段: %s" % key)
    parts = data.get("partInfos")
    if not isinstance(parts, list):
        raise FileGetUploadUrlError("响应缺少 partInfos 列表")
    for item in parts:
        if not isinstance(item, dict) or isinstance(item.get("partNumber"), bool) or not isinstance(item.get("partNumber"), int):
            raise FileGetUploadUrlError("响应 partInfos 项缺少整数 partNumber")
        if not isinstance(item.get("uploadUrl"), str) or not item["uploadUrl"]:
            raise FileGetUploadUrlError("响应 partInfos 项缺少 uploadUrl")
    # partInfos[].uploadUrl 为敏感预签名地址，仅限调用方内部上传使用，禁止输出到日志或 Agent
    return payload


def _normalize_parts(part_infos):
    if isinstance(part_infos, (str, bytes)) or not isinstance(part_infos, (list, tuple)):
        raise FileGetUploadUrlError("part_infos 必须是列表")
    if not 1 <= len(part_infos) <= 100:
        raise FileGetUploadUrlError("part_infos 单次最多 100 个分片")
    parts = []
    for item in part_infos:
        if not isinstance(item, dict):
            raise FileGetUploadUrlError("part_infos 每项必须是对象")
        part = {}
        for key, value in item.items():
            target = _PART_KEYS.get(key)
            if target is None:
                raise FileGetUploadUrlError("part_infos 含未支持字段: %s" % key)
            if value is not None:
                part[target] = value
        number = part.get("partNumber")
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 10000:
            raise FileGetUploadUrlError("partNumber 必须是 1~10000 的整数")
        size = part.get("partSize")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise FileGetUploadUrlError("partSize 必须是正整数")
        # 是否并行由字段是否存在决定，不按 partNumber 猜测；两个并行字段必须成组提供
        if ("partOffset" in part) != ("parallelHashCtx" in part):
            raise FileGetUploadUrlError("partOffset 与 parallelHashCtx 必须成组提供")
        if "partOffset" in part:
            offset = part["partOffset"]
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                raise FileGetUploadUrlError("partOffset 必须是非负整数")
            part["parallelHashCtx"] = _normalize_parallel_ctx(part["parallelHashCtx"])
        parts.append(part)
    return parts


def _normalize_parallel_ctx(value):
    if not isinstance(value, dict) or not value:
        raise FileGetUploadUrlError("parallelHashCtx 必须是非空对象")
    context = {}
    for key, item in value.items():
        target = _PARALLEL_CTX_KEYS.get(key)
        if target is None:
            raise FileGetUploadUrlError("parallelHashCtx 含未支持字段: %s" % key)
        if item is None:
            continue
        if target == "hashCtx":
            if isinstance(item, (str, bytes)) or not isinstance(item, (list, tuple)) or len(item) != _HASH_CTX_LENGTH:
                raise FileGetUploadUrlError("parallelHashCtx.hashCtx 必须是 %d 个整数的数组" % _HASH_CTX_LENGTH)
            numbers = []
            for entry in item:
                if isinstance(entry, bool) or not isinstance(entry, int):
                    raise FileGetUploadUrlError("parallelHashCtx.hashCtx 的元素必须是整数")
                numbers.append(entry)
            context["hashCtx"] = numbers
        else:
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise FileGetUploadUrlError("parallelHashCtx.partOffset 必须是非负整数")
            if item % 64 != 0:
                raise FileGetUploadUrlError("parallelHashCtx.partOffset 必须是 64 的倍数")
            context["partOffset"] = item
    if not context:
        raise FileGetUploadUrlError("parallelHashCtx 不能为空")
    return context


def _require_id(value, name):
    if not isinstance(value, str) or not value or len(value) > 64:
        raise FileGetUploadUrlError("%s 必须是长度不超过 64 的非空字符串" % name)


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise FileGetUploadUrlError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise FileGetUploadUrlError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise FileGetUploadUrlError("request_headers 的值必须是字符串")
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
        raise FileGetUploadUrlError("请求地址校验失败")
    return url


def _failure_message(code, message):
    return "平台返回失败(code=%s): %s" % (code, str(message) if message is not None else "无平台消息")


def _validate_response(payload):
    success = payload.get("success")
    code = payload.get("code")
    message = payload.get("message")
    if success is None and code is None:
        raise FileGetUploadUrlError("平台响应缺少 success 与 code，不能判定为成功")
    if success is not None and str(success).strip().lower() not in ("true", "1"):
        raise FileGetUploadUrlError(_failure_message(code, message), code=code)
    if code is not None and str(code).strip().lower() not in ("0000", "0"):
        raise FileGetUploadUrlError(_failure_message(code, message), code=code)
    return payload


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
        raise FileGetUploadUrlError(
            "HTTP 状态码 %s: %s" % (err.code, str(message) if message is not None else "无平台消息"),
            code=code,
            http_status=err.code,
        )
    except OSError as err:
        raise FileGetUploadUrlError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise FileGetUploadUrlError("HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise FileGetUploadUrlError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise FileGetUploadUrlError("平台响应不是 JSON 对象")
    return _validate_response(upstream_trace.attach(payload, trace_id))
