import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import core_transport_curl

_TIMEOUT_SECONDS = 600


class FileDownloadError(RuntimeError):
    def __init__(self, message, *, code=None, request_id=None, http_status=None):
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.http_status = http_status


class FileDownloadStream:
    """受控下载流：调用方负责 close 或使用 with，避免文件内容被整体读入内存。"""

    def __init__(self, response, status, content_length, content_range):
        self._response = response
        self.status = status
        self.content_length = content_length
        self.content_range = content_range

    def read(self, size=-1):
        return self._response.read(size)

    def close(self):
        self._response.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(
    _NoRedirectHandler,
    urllib.request.HTTPSHandler(context=ssl.create_default_context()),
)


def call(*, download_url, range_start=None, range_end=None):
    url = _require_presigned_url(download_url, "download_url")
    range_header = _build_range(range_start, range_end)
    headers = {"Range": range_header} if range_header else {}
    # 传输通道：curl -4 优先（TLS 指纹放行 + 规避 IPv6 黑洞），响应落临时文件保持流式读语义；
    # curl 不可用时回落 urllib（IPv4 优先）。两者失败均带 host 定位后缀。
    if core_transport_curl.curl_channel_enabled():
        status, resp_headers, body_path = core_transport_curl.download_to_file(
            "GET", url, headers=headers or None,
        )
        if status is None:
            raise FileDownloadError(str(body_path))  # body_path 携带 errno 语义的失败信息
        if status not in (200, 206):
            _cleanup(body_path)
            raise FileDownloadError("HTTP 状态码不可用于下载",
                                    http_status=status if isinstance(status, int) else None)
        _cl = _header_from(dict(resp_headers), "content-length")
        _cr = _header_from(dict(resp_headers), "content-range")
        return _FileBody(body_path, status, _cl, _cr)

    # urllib 回退：保持原流式语义
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        response = _OPENER.open(request, timeout=_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as err:
        raise _http_error(err, url)
    except OSError as err:
        raise FileDownloadError(
            "网络请求失败: %s%s" % (getattr(err, "reason", None) or err, _host_suffix(url))
        )
    status = response.getcode()
    if status not in (200, 206):
        try:
            response.read(65536)
        finally:
            response.close()
        raise FileDownloadError("HTTP 状态码不可用于下载", http_status=status if isinstance(status, int) else None)
    # 200 为全量内容，206 为 Range 分段内容；状态与 Content-Range 原样暴露，由调用方按 Range 语义处理
    return FileDownloadStream(response, status, _content_length(response), _header(response, "Content-Range"))


def _cleanup(path):
    try:
        os.unlink(path)
    except OSError:
        pass


class _FileBody:
    """curl 通道落盘的受控下载体：按 1MB 语义 read(size) 推进游标，close 时清理临时文件。"""

    _CHUNK = 1024 * 1024

    def __init__(self, path, status, content_length, content_range):
        self._path = path
        self._fh = open(path, "rb")
        self.status = status
        self.content_length = content_length
        self.content_range = content_range

    def read(self, size=-1):
        return self._fh.read(size)

    def close(self):
        try:
            self._fh.close()
        finally:
            _cleanup(self._path)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def _header_from(headers, name):
    for key, value in (headers or {}).items():
        if key.lower() == name:
            return value
    return None


def _require_presigned_url(value, name):
    if not isinstance(value, str) or not value.strip():
        raise FileDownloadError("%s 必须是非空字符串" % name)
    url = value.strip()
    if any(ch.isspace() for ch in url):
        raise FileDownloadError("%s 不得包含空白字符" % name)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise FileDownloadError("%s 必须是 HTTPS 预签名地址" % name)
    if parsed.username is not None or parsed.password is not None:
        raise FileDownloadError("%s 不得包含用户名或密码" % name)
    if not parsed.query:
        raise FileDownloadError("%s 缺少签名查询参数" % name)
    if parsed.fragment:
        raise FileDownloadError("%s 不得包含片段" % name)
    return url


def _build_range(range_start, range_end):
    if range_start is None and range_end is None:
        return None
    if isinstance(range_start, bool) or not isinstance(range_start, int) or range_start < 0:
        raise FileDownloadError("range_start 必须是非负整数")
    if range_end is None:
        return "bytes=%d-" % range_start
    if isinstance(range_end, bool) or not isinstance(range_end, int) or range_end < range_start:
        raise FileDownloadError("range_end 必须是不小于 range_start 的整数")
    return "bytes=%d-%d" % (range_start, range_end)


def _header(response, name):
    value = response.headers.get(name)
    if isinstance(value, str) and value:
        return value
    return None


def _content_length(response):
    value = _header(response, "Content-Length")
    if value is None or not value.isdigit():
        return None
    return int(value)


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
    return FileDownloadError(
        text,
        code=fields.get("Code"),
        request_id=fields.get("RequestId"),
        http_status=err.code,
    )
