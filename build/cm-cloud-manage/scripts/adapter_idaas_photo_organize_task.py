import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
import uuid
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/api/openclaw/photoOrganize/task"
_TIMEOUT_SECONDS = 600

_ORGANIZE_TYPES = (1, 2, 3)


class IdaasPhotoOrganizeTaskError(RuntimeError):
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


class _MultipartBody:
    def __init__(self, segments, length):
        self._segments = list(segments)
        self._index = 0
        self._pending = b""
        self.length = length

    def read(self, size=-1):
        if size is None or size < 0:
            chunks = [self._pending]
            self._pending = b""
            for index in range(self._index, len(self._segments)):
                chunks.append(_read_segment_all(self._segments[index]))
            self._index = len(self._segments)
            return b"".join(chunks)
        chunks = []
        remaining = size
        if self._pending:
            head = self._pending[:remaining]
            self._pending = self._pending[remaining:]
            chunks.append(head)
            remaining -= len(head)
        while remaining > 0 and self._index < len(self._segments):
            segment = self._segments[self._index]
            if isinstance(segment, (bytes, bytearray, memoryview)):
                self._index += 1
                payload = bytes(segment)
                chunks.append(payload[:remaining])
                self._pending = payload[remaining:]
                remaining -= len(payload[:remaining])
            else:
                chunk = segment.read(max(remaining, 65536))
                if not chunk:
                    self._index += 1
                    continue
                chunks.append(chunk[:remaining])
                self._pending = chunk[remaining:]
                remaining -= len(chunk[:remaining])
        return b"".join(chunks)


def _read_segment_all(segment):
    if isinstance(segment, (bytes, bytearray, memoryview)):
        return bytes(segment)
    chunks = []
    while True:
        chunk = segment.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def call(
    *,
    organize_type,
    openclaw_id,
    file,
    content_length=None,
    session_id=None,
    message_id=None,
    processing_hint=None,
    request_headers,
):
    _require_organize_type(organize_type)
    fields = [("type", str(organize_type)), ("openclawId", _require_text(openclaw_id, "openclaw_id"))]
    if session_id is not None:
        fields.append(("sessionId", _require_text(session_id, "session_id")))
    if message_id is not None:
        fields.append(("messageId", _require_text(message_id, "message_id")))
    if processing_hint is not None:
        fields.append(("processingHint", _require_text(processing_hint, "processing_hint")))
    body, length, boundary = _build_multipart(fields, file, content_length)
    payload = _post(body, length, boundary, request_headers)
    # userId、sourceChannel 由服务端注入；文件格式与大小限制依赖部署；
    # 成功仅表示任务受理（code:"0000"/message/data.taskId），不表示照片整理完成，轮询仅使用本接口 ID
    return payload


def _build_multipart(fields, file_data, content_length):
    boundary = "----cmcloud%s" % uuid.uuid4().hex
    segments = []
    for name, value in fields:
        segments.append(
            ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, name, value)).encode("utf-8")
        )
    segments.append(
        (
            '--%s\r\nContent-Disposition: form-data; name="file"; filename="blob"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n" % boundary
        ).encode("utf-8")
    )
    payload, payload_length = _prepare_file(file_data, content_length)
    segments.append(payload)
    segments.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    length = sum(
        len(segment) if isinstance(segment, (bytes, bytearray, memoryview)) else payload_length for segment in segments
    )
    return _MultipartBody(segments, length), length, boundary


def _prepare_file(file_data, content_length):
    if isinstance(file_data, (bytes, bytearray, memoryview)):
        payload = bytes(file_data)
        if not payload:
            raise IdaasPhotoOrganizeTaskError("file 不能为空")
        length = len(payload)
        if content_length is not None:
            if isinstance(content_length, bool) or not isinstance(content_length, int) or content_length <= 0:
                raise IdaasPhotoOrganizeTaskError("content_length 必须是正整数")
            if content_length != length:
                raise IdaasPhotoOrganizeTaskError("content_length 与实际字节数不一致")
        return payload, length
    if hasattr(file_data, "read"):
        if isinstance(content_length, bool) or not isinstance(content_length, int) or content_length <= 0:
            raise IdaasPhotoOrganizeTaskError("文件对象必须提供正整数 content_length")
        return file_data, content_length
    raise IdaasPhotoOrganizeTaskError("file 必须是二进制数据或可读文件对象")


def _require_organize_type(value):
    if isinstance(value, bool) or not isinstance(value, int) or value not in _ORGANIZE_TYPES:
        raise IdaasPhotoOrganizeTaskError("organize_type 仅支持 1 归档/2 相册/3 故事")


def _require_text(value, name):
    if not isinstance(value, str) or not value:
        raise IdaasPhotoOrganizeTaskError("%s 必须是非空字符串" % name)
    return value


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise IdaasPhotoOrganizeTaskError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise IdaasPhotoOrganizeTaskError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise IdaasPhotoOrganizeTaskError("request_headers 的值必须是字符串")
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
        raise IdaasPhotoOrganizeTaskError("请求地址校验失败")
    return url


def _extract_error_fields(raw):
    try:
        failed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(failed, dict):
        return None
    return failed.get("code"), failed.get("message")


def _post(body, length, boundary, request_headers):
    url = _build_url()
    headers = {
        "Content-Type": "multipart/form-data; boundary=%s" % boundary,
        "Content-Length": str(length),
        "Accept": "application/json",
    }
    headers.update(_require_headers(request_headers))
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
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
        raise IdaasPhotoOrganizeTaskError(
            "HTTP 状态码 %s: %s" % (err.code, str(fields[1]) if fields[1] is not None else "无平台消息"),
            code=fields[0],
            http_status=err.code,
        )
    except OSError as err:
        raise IdaasPhotoOrganizeTaskError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise IdaasPhotoOrganizeTaskError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
    if content_type is not None and "json" not in content_type.lower():
        raise IdaasPhotoOrganizeTaskError("响应媒体类型不是 JSON: %s" % content_type)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise IdaasPhotoOrganizeTaskError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise IdaasPhotoOrganizeTaskError("平台响应不是 JSON 对象")
    return upstream_trace.attach(payload, trace_id)
