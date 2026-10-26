import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/personalSaas/file/batchCopySync"
_TIMEOUT_SECONDS = 15

_RFC3339_PATTERN = re.compile(
    r"\A\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})\Z"
)


class IdaasFileBatchCopySyncError(RuntimeError):
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


def call(*, file_ids, to_parent_file_id, file_ext_infos=None, tag_list=None, request_headers):
    headers = _require_headers(request_headers)
    body = {
        "fileIds": _require_file_ids(file_ids),
        "toParentFileId": _require_text(to_parent_file_id, "to_parent_file_id"),
    }
    if file_ext_infos is not None:
        body["fileExtInfos"] = _require_file_ext_infos(file_ext_infos)
    if tag_list is not None:
        body["tagList"] = _require_tag_list(tag_list)
    payload = _post(body, headers)
    # 同步入口仅表示本次调用结束，不代表整批成功；返回原始 JSON 结构，不投影文件 ID 或从不强取 taskId
    _raise_if_explicit_failure(payload)
    return payload


def _raise_if_explicit_failure(payload):
    success = payload.get("success")
    if success is not None and success is not True:
        # success 字段显式 false 时按明确失败信号处理；同步成功 DTO/成功码未冻结，其余响应原样返回
        raise IdaasFileBatchCopySyncError(
            _failure_message(payload.get("code"), payload.get("message")), code=payload.get("code")
        )
    code = payload.get("code")
    # 文档 §7.1：迁移异常外壳的特征是整数 code 且 totalRows=0；
    # 不接受“任意非零整数 code 即失败”的推断，未知整数/字符串码原样返回由上层确认
    if (
        isinstance(code, int)
        and not isinstance(code, bool)
        and "totalRows" in payload
        and payload.get("totalRows") == 0
    ):
        raise IdaasFileBatchCopySyncError(_failure_message(code, payload.get("message")), code=code)


def _require_file_ids(value):
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)) or not value:
        raise IdaasFileBatchCopySyncError("file_ids 必须是非空字符串列表")
    ids = []
    for item in value:
        if not isinstance(item, str) or not item or not item.strip():
            raise IdaasFileBatchCopySyncError("file_ids 每项必须是非空字符串")
        ids.append(item)
    return ids


def _require_text(value, name):
    if not isinstance(value, str) or not value:
        raise IdaasFileBatchCopySyncError("%s 必须是非空字符串" % name)
    return value


def _require_file_ext_infos(value):
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)) or not value:
        raise IdaasFileBatchCopySyncError("file_ext_infos 必须是非空列表")
    infos = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise IdaasFileBatchCopySyncError("file_ext_infos 每项必须是映射")
        unknown = set(entry) - {"fileId", "localOperatedAt"}
        if unknown:
            raise IdaasFileBatchCopySyncError(
                "file_ext_infos 项包含未定义字段: %s" % ", ".join(sorted(str(key) for key in unknown))
            )
        item = {"fileId": _require_text(entry.get("fileId"), "fileExtInfos.fileId")}
        operated_at = entry.get("localOperatedAt")
        if operated_at is not None:
            if not isinstance(operated_at, str) or _RFC3339_PATTERN.match(operated_at) is None:
                raise IdaasFileBatchCopySyncError("localOperatedAt 必须是带时区的 RFC3339 时间")
            item["localOperatedAt"] = operated_at
        infos.append(item)
    return infos


def _require_tag_list(value):
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)) or not value:
        raise IdaasFileBatchCopySyncError("tag_list 必须是非空列表")
    tags = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise IdaasFileBatchCopySyncError("tag_list 每项必须是映射")
        unknown = set(entry) - {"key", "value"}
        if unknown:
            raise IdaasFileBatchCopySyncError(
                "tag_list 项包含未定义字段: %s" % ", ".join(sorted(str(key) for key in unknown))
            )
        key = entry.get("key")
        if not isinstance(key, str) or not key:
            raise IdaasFileBatchCopySyncError("tagList.key 必须是非空字符串")
        if "#" in key:
            raise IdaasFileBatchCopySyncError("tagList.key 不得包含 #")
        if "value" not in entry or not isinstance(entry.get("value"), str):
            raise IdaasFileBatchCopySyncError("tagList.value 必须是字符串")
        if "#" in entry["value"]:
            raise IdaasFileBatchCopySyncError("tagList.value 不得包含 #")
        tags.append({"key": key, "value": entry["value"]})
    return tags


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise IdaasFileBatchCopySyncError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise IdaasFileBatchCopySyncError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise IdaasFileBatchCopySyncError("request_headers 的值必须是字符串")
        headers[key] = item
    if not any(key.lower() == "credittoken" and value for key, value in headers.items()):
        raise IdaasFileBatchCopySyncError("正式鉴权要求非空 creditToken 请求头")
    return headers


def _build_url():
    config = platform_config.load_platform_config()
    base = config.require("base_url").rstrip("/")
    url = base + _PATH
    parsed = urllib.parse.urlsplit(url)
    base_parsed = urllib.parse.urlsplit(base)
    # 地址来自 EXE 旁 cmcloud.json 的受批准配置，不接受任何调用参数覆盖
    if parsed.scheme != base_parsed.scheme or parsed.netloc != base_parsed.netloc:
        raise IdaasFileBatchCopySyncError("请求地址校验失败")
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
    headers.update(request_headers)
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
        raise IdaasFileBatchCopySyncError(
            "HTTP 状态码 %s: %s" % (err.code, str(fields[1]) if fields[1] is not None else "无平台消息"),
            code=fields[0],
            http_status=err.code,
        )
    except OSError as err:
        raise IdaasFileBatchCopySyncError(
            "网络请求失败，同步结果未知（不得自动重试）: %s" % (getattr(err, "reason", None) or err)
        )
    if not isinstance(status, int) or not 200 <= status < 300:
        raise IdaasFileBatchCopySyncError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
    if content_type is not None and "json" not in content_type.lower():
        raise IdaasFileBatchCopySyncError("响应媒体类型不是 JSON: %s" % content_type)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise IdaasFileBatchCopySyncError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise IdaasFileBatchCopySyncError("平台响应不是 JSON 对象")
    return upstream_trace.attach(payload, trace_id)
