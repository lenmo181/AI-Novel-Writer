import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/personalSaas/file/batchCopy"
_TIMEOUT_SECONDS = 15

_RFC3339_PATTERN = re.compile(
    r"\A\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})\Z"
)


class IdaasFileBatchCopyAsyncError(RuntimeError):
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
    # 异步受理成功不代表复制完成；返回原始 JSON 结构，taskId 参考协议位于 data.taskId（待真实样本确认）
    _raise_if_explicit_failure(payload)
    return payload


def _raise_if_explicit_failure(payload):
    success = payload.get("success")
    if success is not None and success is not True:
        # §5.2 参考协议：success=false 属明确失败信号；code="0000"/data.taskId 仍需真实样本核对
        raise IdaasFileBatchCopyAsyncError(
            _failure_message(payload.get("code"), payload.get("message")), code=payload.get("code")
        )
    code = payload.get("code")
    # §7.1：迁移异常外壳特征为整数 code 且 totalRows=0；未知整数/字符串码不判成功也不判失败，原样返回
    if (
        isinstance(code, int)
        and not isinstance(code, bool)
        and "totalRows" in payload
        and payload.get("totalRows") == 0
    ):
        raise IdaasFileBatchCopyAsyncError(_failure_message(code, payload.get("message")), code=code)


def _require_file_ids(value):
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)) or not value:
        raise IdaasFileBatchCopyAsyncError("file_ids 必须是非空字符串列表")
    ids = []
    for item in value:
        if not isinstance(item, str) or not item or not item.strip():
            raise IdaasFileBatchCopyAsyncError("file_ids 每项必须是非空字符串")
        ids.append(item)
    return ids


def _require_text(value, name):
    if not isinstance(value, str) or not value:
        raise IdaasFileBatchCopyAsyncError("%s 必须是非空字符串" % name)
    return value


def _require_file_ext_infos(value):
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)) or not value:
        raise IdaasFileBatchCopyAsyncError("file_ext_infos 必须是非空列表")
    infos = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise IdaasFileBatchCopyAsyncError("file_ext_infos 每项必须是映射")
        unknown = set(entry) - {"fileId", "localOperatedAt"}
        if unknown:
            raise IdaasFileBatchCopyAsyncError(
                "file_ext_infos 项包含未定义字段: %s" % ", ".join(sorted(str(key) for key in unknown))
            )
        item = {"fileId": _require_text(entry.get("fileId"), "fileExtInfos.fileId")}
        operated_at = entry.get("localOperatedAt")
        if operated_at is not None:
            if not isinstance(operated_at, str) or _RFC3339_PATTERN.match(operated_at) is None:
                raise IdaasFileBatchCopyAsyncError("localOperatedAt 必须是带时区的 RFC3339 时间")
            item["localOperatedAt"] = operated_at
        infos.append(item)
    return infos


def _require_tag_list(value):
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)) or not value:
        raise IdaasFileBatchCopyAsyncError("tag_list 必须是非空列表")
    tags = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise IdaasFileBatchCopyAsyncError("tag_list 每项必须是映射")
        unknown = set(entry) - {"key", "value"}
        if unknown:
            raise IdaasFileBatchCopyAsyncError(
                "tag_list 项包含未定义字段: %s" % ", ".join(sorted(str(key) for key in unknown))
            )
        key = entry.get("key")
        if not isinstance(key, str) or not key:
            raise IdaasFileBatchCopyAsyncError("tagList.key 必须是非空字符串")
        if "#" in key:
            raise IdaasFileBatchCopyAsyncError("tagList.key 不得包含 #")
        if "value" not in entry or not isinstance(entry.get("value"), str):
            raise IdaasFileBatchCopyAsyncError("tagList.value 必须是字符串")
        if "#" in entry["value"]:
            raise IdaasFileBatchCopyAsyncError("tagList.value 不得包含 #")
        tags.append({"key": key, "value": entry["value"]})
    return tags


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise IdaasFileBatchCopyAsyncError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise IdaasFileBatchCopyAsyncError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise IdaasFileBatchCopyAsyncError("request_headers 的值必须是字符串")
        headers[key] = item
    if not any(key.lower() == "credittoken" and value for key, value in headers.items()):
        raise IdaasFileBatchCopyAsyncError("正式鉴权要求非空 creditToken 请求头")
    return headers


def _build_url():
    config = platform_config.load_platform_config()
    base = config.require("base_url").rstrip("/")
    url = base + _PATH
    parsed = urllib.parse.urlsplit(url)
    base_parsed = urllib.parse.urlsplit(base)
    # 地址来自 EXE 旁 cmcloud.json 的受批准配置，不接受任何调用参数覆盖
    if parsed.scheme != base_parsed.scheme or parsed.netloc != base_parsed.netloc:
        raise IdaasFileBatchCopyAsyncError("请求地址校验失败")
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
        raise IdaasFileBatchCopyAsyncError(
            "HTTP 状态码 %s: %s" % (err.code, str(fields[1]) if fields[1] is not None else "无平台消息"),
            code=fields[0],
            http_status=err.code,
        )
    except OSError as err:
        raise IdaasFileBatchCopyAsyncError(
            "网络请求失败，异步提交结果未知（不得自动重试）: %s" % (getattr(err, "reason", None) or err)
        )
    if not isinstance(status, int) or not 200 <= status < 300:
        raise IdaasFileBatchCopyAsyncError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
    if content_type is not None and "json" not in content_type.lower():
        raise IdaasFileBatchCopyAsyncError("响应媒体类型不是 JSON: %s" % content_type)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise IdaasFileBatchCopyAsyncError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise IdaasFileBatchCopyAsyncError("平台响应不是 JSON 对象")
    return upstream_trace.attach(payload, trace_id)
