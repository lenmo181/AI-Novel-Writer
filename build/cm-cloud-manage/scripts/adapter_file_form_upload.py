import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from email.utils import formatdate

import core_transport_curl

_TIMEOUT_SECONDS = 600


class FileFormUploadError(RuntimeError):
    def __init__(self, message, *, code=None, request_id=None, http_status=None):
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.http_status = http_status


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(
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


def call(*, upload_url, data_field, data, form_data=None, content_length=None):
    url, _host = _require_presigned_url(upload_url, "upload_url")
    _require_data_field(data_field)
    fields = _require_form_data(form_data)
    if data_field in [name for name, _ in fields]:
        raise FileFormUploadError("form_data 不得包含 data_field 字段")
    body, length, boundary = _build_multipart(data_field, fields, data, content_length)
    _post(url, body, length, boundary)
    # 表单域来自 file/create 响应的 formInfo.formData，文件名段固定为最后一个表单域，adapter 不改写签名参数
    return {"uploaded": True, "size": length}


def _require_presigned_url(value, name):
    if not isinstance(value, str) or not value.strip():
        raise FileFormUploadError("%s 必须是非空字符串" % name)
    url = value.strip()
    if any(ch.isspace() for ch in url):
        raise FileFormUploadError("%s 不得包含空白字符" % name)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise FileFormUploadError("%s 必须是 HTTPS 预签名地址" % name)
    if parsed.username is not None or parsed.password is not None:
        raise FileFormUploadError("%s 不得包含用户名或密码" % name)
    if not parsed.query:
        raise FileFormUploadError("%s 缺少签名查询参数" % name)
    if parsed.fragment:
        raise FileFormUploadError("%s 不得包含片段" % name)
    return url, parsed.hostname


def _require_data_field(value):
    if not isinstance(value, str) or not value:
        raise FileFormUploadError("data_field 必须是非空字符串")
    if any(ch in value for ch in ('"', "\\", "\r", "\n")):
        raise FileFormUploadError("data_field 不得包含引号、反斜杠或换行")


def _require_form_data(value):
    if value is None:
        return []
    if not isinstance(value, Mapping):
        raise FileFormUploadError("form_data 必须是映射")
    fields = []
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise FileFormUploadError("form_data 的键必须是非空字符串")
        if any(ch in key for ch in ('"', "\\", "\r", "\n")):
            raise FileFormUploadError("form_data 的键不得包含引号、反斜杠或换行")
        if not isinstance(item, str):
            raise FileFormUploadError("form_data 的值必须是字符串")
        if "\r" in item or "\n" in item:
            raise FileFormUploadError("form_data 的值不得包含换行")
        fields.append((key, item))
    return fields


def _prepare_data(data, content_length):
    if isinstance(data, (bytes, bytearray, memoryview)):
        body = bytes(data)
        if not body:
            raise FileFormUploadError("data 不能为空")
        length = len(body)
        if content_length is not None:
            if isinstance(content_length, bool) or not isinstance(content_length, int) or content_length <= 0:
                raise FileFormUploadError("content_length 必须是正整数")
            if content_length != length:
                raise FileFormUploadError("content_length 与实际字节数不一致")
        return body, length
    if hasattr(data, "read"):
        if isinstance(content_length, bool) or not isinstance(content_length, int) or content_length <= 0:
            raise FileFormUploadError("文件对象必须提供正整数 content_length")
        return data, content_length
    raise FileFormUploadError("data 必须是二进制数据或可读文件对象")


def _build_multipart(data_field, fields, data, content_length):
    boundary = "----cmcloud%s" % uuid.uuid4().hex
    segments = []
    for key, value in fields:
        segments.append(
            (
                '--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, key, value)
            ).encode("utf-8")
        )
    segments.append(
        (
            '--%s\r\nContent-Disposition: form-data; name="%s"; filename="blob"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n" % (boundary, data_field)
        ).encode("utf-8")
    )
    payload, payload_length = _prepare_data(data, content_length)
    segments.append(payload)
    segments.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    length = sum(len(segment) if isinstance(segment, (bytes, bytearray, memoryview)) else payload_length for segment in segments)
    return _MultipartBody(segments, length), length, boundary


def _parse_error_xml(raw):
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    if root.tag != "Error":
        return None
    fields = {}
    for child in root:
        if child.tag in ("Code", "Message", "RequestId", "HostId", "Resource") and isinstance(child.text, str):
            fields[child.tag] = child.text
    return fields


def _host_suffix(url):
    """失败报错定位用：只输出预签名地址的主机名，不输出路径与签名查询串。"""
    try:
        host = urllib.parse.urlsplit(url).hostname
    except ValueError:
        host = None
    return " (host: %s)" % host if host else ""


def _http_error(err, url):
    try:
        raw = err.read(65536)
    except OSError:
        raw = b""
    fields = _parse_error_xml(raw) or {}
    message = fields.get("Message")
    text = "HTTP 状态码 %s" % err.code
    text += ": %s" % message if message else ": 无对象存储错误消息"
    text += _host_suffix(url)
    return FileFormUploadError(
        text,
        code=fields.get("Code"),
        request_id=fields.get("RequestId"),
        http_status=err.code,
    )


def _post(url, body, length, boundary):
    # 二阶段表单上传契约要求 Date 使用 GMT 格式；Host 由传输通道按预签名 URL 自动生成。
    # 传输通道：curl -4 优先（TLS 指纹放行 + 规避 IPv6 黑洞），失败回落 urllib（IPv4 优先）。
    headers = {
        "Content-Type": "multipart/form-data; boundary=%s" % boundary,
        "Content-Length": str(length),
        "Date": formatdate(usegmt=True),
    }
    try:
            # 表单上传的目标地址与对象 key 由服务端下发的策略固定，重发同一份表单即覆盖
            # 同一对象（对象级幂等）；控制面"创建文件记录"仍走独立的安全门禁。
            status, _headers, _body = core_transport_curl.request_bytes(
                "POST", url, headers=headers, body=body, replay_safe=True,
            )
    except core_transport_curl.CurlTransportError as err:
        raise FileFormUploadError(str(err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise FileFormUploadError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
