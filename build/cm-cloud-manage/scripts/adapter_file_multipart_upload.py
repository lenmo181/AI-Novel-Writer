import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import formatdate

import core_transport_curl

_TIMEOUT_SECONDS = 600


class FileMultipartUploadError(RuntimeError):
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


def call(*, upload_url, data, content_length=None):
    url, _host = _require_presigned_url(upload_url, "upload_url")
    body, length = _prepare_body(data, content_length)
    _put(url, body, length)
    # 上传地址来自 file/create 或 file/getUploadUrl 的可信响应，adapter 不追加业务鉴权头，也不改写签名查询串
    return {"uploaded": True, "size": length}


def _require_presigned_url(value, name):
    if not isinstance(value, str) or not value.strip():
        raise FileMultipartUploadError("%s 必须是非空字符串" % name)
    url = value.strip()
    if any(ch.isspace() for ch in url):
        raise FileMultipartUploadError("%s 不得包含空白字符" % name)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise FileMultipartUploadError("%s 必须是 HTTPS 预签名地址" % name)
    if parsed.username is not None or parsed.password is not None:
        raise FileMultipartUploadError("%s 不得包含用户名或密码" % name)
    if not parsed.query:
        raise FileMultipartUploadError("%s 缺少签名查询参数" % name)
    if parsed.fragment:
        raise FileMultipartUploadError("%s 不得包含片段" % name)
    return url, parsed.hostname


def _prepare_body(data, content_length):
    if isinstance(data, (bytes, bytearray, memoryview)):
        body = bytes(data)
        if not body:
            raise FileMultipartUploadError("data 不能为空")
        length = len(body)
        if content_length is not None:
            if isinstance(content_length, bool) or not isinstance(content_length, int) or content_length <= 0:
                raise FileMultipartUploadError("content_length 必须是正整数")
            if content_length != length:
                raise FileMultipartUploadError("content_length 与实际字节数不一致")
        return body, length
    if hasattr(data, "read"):
        if isinstance(content_length, bool) or not isinstance(content_length, int) or content_length <= 0:
            raise FileMultipartUploadError("文件对象必须提供正整数 content_length")
        return data, content_length
    raise FileMultipartUploadError("data 必须是二进制数据或可读文件对象")


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
    return FileMultipartUploadError(
        text,
        code=fields.get("Code"),
        request_id=fields.get("RequestId"),
        http_status=err.code,
    )


def _put(url, body, length):
    # 独立空间 SaaS 二阶段分片上传契约要求显式携带 Date、Content-Length，
    # 且 Content-Type 固定为 application/octet-stream。若不显式设置 Content-Type，
    # 传输通道会把带 data 的请求补成 application/x-www-form-urlencoded，可能导致
    # 预签名校验失败或被对象存储按非法参数拒绝。
    # 传输通道：curl -4 优先（TLS 指纹放行 + 规避 IPv6 黑洞），失败回落 urllib（IPv4 优先）。
    headers = {
        "Content-Length": str(length),
        "Content-Type": "application/octet-stream",
        "Date": formatdate(usegmt=True),
    }
    try:
            # 分片 PUT 指向预签名地址上的同一对象（对象级幂等），重传即覆盖；控制面
            # "合并/完成"仍走独立的安全门禁。
            status, _headers, _body = core_transport_curl.request_bytes(
                "PUT", url, headers=headers, body=body, replay_safe=True,
            )
    except core_transport_curl.CurlTransportError as err:
        raise FileMultipartUploadError(str(err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise FileMultipartUploadError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
