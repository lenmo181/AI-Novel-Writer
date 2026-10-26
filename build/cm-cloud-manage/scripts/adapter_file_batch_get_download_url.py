import datetime
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/personalSaas/file/batchGetDownloadUrl"
_TIMEOUT_SECONDS = 15

_RFC3339_PATTERN = re.compile(
    r"\A\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})\Z"
)


class FileBatchGetDownloadUrlError(RuntimeError):
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


def call(*, file_ids, request_headers):
    body = {"fileIds": _require_file_ids(file_ids)}
    payload = _post(body, request_headers)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise FileBatchGetDownloadUrlError("响应缺少业务 data 对象")
    items = data.get("items")
    if not isinstance(items, list):
        # 原文表格称 items 为数组、示例却写成对象，联调前按数组契约校验，冲突保持待确认
        raise FileBatchGetDownloadUrlError("响应缺少 items 列表")
    for item in items:
        if not isinstance(item, dict):
            raise FileBatchGetDownloadUrlError("items 每项必须是对象")
        if not isinstance(item.get("fileId"), str) or not item["fileId"]:
            raise FileBatchGetDownloadUrlError("items 项缺少 fileId")
        if "errCode" in item and not isinstance(item["errCode"], str):
            raise FileBatchGetDownloadUrlError("items 项 errCode 类型非法")
        if "message" in item and not isinstance(item["message"], str):
            raise FileBatchGetDownloadUrlError("items 项 message 类型非法")
        err_code = item.get("errCode")
        if err_code is None or err_code == "0000":
            # 成功项必须包含可用的下载信息；失败项允许没有地址和大小
            for key in ("url", "expiration"):
                if not isinstance(item.get(key), str) or not item[key]:
                    raise FileBatchGetDownloadUrlError("成功项缺少字符串字段: %s" % key)
            if not _is_rfc3339(item["expiration"]):
                raise FileBatchGetDownloadUrlError("成功项 expiration 不是 RFC3339 时间")
            size = item.get("size")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise FileBatchGetDownloadUrlError("成功项 size 必须是非负整数")
    # items[].url 为敏感预签名地址；逐项 errCode/message 必须原样保留，超过 100 个 ID 由调用方分批
    return payload


def _is_rfc3339(value):
    if _RFC3339_PATTERN.match(value) is None:
        return False
    normalized = value.replace("t", "T").replace("Z", "+00:00").replace("z", "+00:00")
    try:
        datetime.datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return True


def _require_file_ids(value):
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise FileBatchGetDownloadUrlError("file_ids 必须是列表")
    if not 1 <= len(value) <= 100:
        raise FileBatchGetDownloadUrlError("file_ids 数量必须在 1~100（adapter 不隐式分批）")
    ids = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 64:
            raise FileBatchGetDownloadUrlError("file_ids 每项必须是长度不超过 64 的非空字符串")
        ids.append(item)
    return ids


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise FileBatchGetDownloadUrlError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise FileBatchGetDownloadUrlError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise FileBatchGetDownloadUrlError("request_headers 的值必须是字符串")
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
        raise FileBatchGetDownloadUrlError("请求地址校验失败")
    return url


def _failure_message(code, message):
    return "平台返回失败(code=%s): %s" % (code, str(message) if message is not None else "无平台消息")


def _validate_response(payload):
    success = payload.get("success")
    code = payload.get("code")
    message = payload.get("message")
    if success is None and code is None:
        raise FileBatchGetDownloadUrlError("平台响应缺少 success 与 code，不能判定为成功")
    if success is not None and str(success).strip().lower() not in ("true", "1"):
        raise FileBatchGetDownloadUrlError(_failure_message(code, message), code=code)
    if code is not None and str(code).strip().lower() not in ("0000", "0"):
        raise FileBatchGetDownloadUrlError(_failure_message(code, message), code=code)
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
        raise FileBatchGetDownloadUrlError(
            "HTTP 状态码 %s: %s" % (err.code, str(message) if message is not None else "无平台消息"),
            code=code,
            http_status=err.code,
        )
    except OSError as err:
        raise FileBatchGetDownloadUrlError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise FileBatchGetDownloadUrlError("HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise FileBatchGetDownloadUrlError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise FileBatchGetDownloadUrlError("平台响应不是 JSON 对象")
    return _validate_response(upstream_trace.attach(payload, trace_id))
