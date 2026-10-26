import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
import xml.etree.ElementTree as ET
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/devapp/getContentInfo"
_TIMEOUT_SECONDS = 15


class IdaasGetContentInfoError(RuntimeError):
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


def call(*, content_id, path=None, entry_share_catalog_id=None, owner_msisdn=None, aes=None, request_headers):
    root = ET.Element("getContentInfo")
    ET.SubElement(root, "ContentID").text = _require_text(content_id, "content_id")
    if path is not None:
        ET.SubElement(root, "Path").text = _require_text(path, "path")
    if entry_share_catalog_id is not None:
        ET.SubElement(root, "entryShareCatalogID").text = _require_text(entry_share_catalog_id, "entry_share_catalog_id")
    if owner_msisdn is not None:
        ET.SubElement(root, "ownerMSISDN").text = _require_text(owner_msisdn, "owner_msisdn")
    if aes is not None:
        ET.SubElement(root, "aes").text = _require_text(aes, "aes")
    result, raw, trace_id = _post(ET.tostring(root, encoding="utf-8"), request_headers)
    if result.tag != "result":
        raise IdaasGetContentInfoError("响应根元素不是 result")
    result_code = result.get("resultCode")
    if result_code != "0":
        raise IdaasGetContentInfoError(
            _failure_message(result_code, result.get("desc")), code=result_code
        )
    # §3.3：成功码 "0" 是 /result 属性而非子元素；业务码/描述结构化可见，
    # 原始 XML 文本可取回，供上层按正式响应契约自行解析；不再输出字典化业务对象
    payload = {
        "resultCode": result_code,
        "desc": result.get("desc"),
        "rawXml": raw.decode("utf-8"),
    }
    return upstream_trace.attach(payload, trace_id)


def _require_text(value, name):
    if not isinstance(value, str) or not value:
        raise IdaasGetContentInfoError("%s 必须是非空字符串" % name)
    return value


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise IdaasGetContentInfoError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise IdaasGetContentInfoError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise IdaasGetContentInfoError("request_headers 的值必须是字符串")
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
        raise IdaasGetContentInfoError("请求地址校验失败")
    return url


def _failure_message(code, message):
    return "平台返回失败(code=%s): %s" % (code, str(message) if message is not None else "无平台消息")


def _json_error_fields(raw):
    try:
        failed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(failed, dict):
        return None
    return failed.get("code"), failed.get("message")


def _xml_error_fields(raw):
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    if root.tag != "result":
        return None
    return root.get("resultCode"), root.get("desc")


def _http_error(err):
    raw = err.read(65536)
    fields = _json_error_fields(raw)
    if fields is None:
        fields = _xml_error_fields(raw)
    if fields is None:
        fields = (None, None)
    return IdaasGetContentInfoError(
        "HTTP 状态码 %s: %s" % (err.code, str(fields[1]) if fields[1] is not None else "无平台消息"),
        code=fields[0],
        http_status=err.code,
    )


def _parse_xml_response(raw, content_type):
    if content_type is not None:
        lowered = content_type.lower()
        if "json" in lowered:
            fields = _json_error_fields(raw) or (None, None)
            raise IdaasGetContentInfoError(
                "响应为 JSON 错误外壳: %s" % (str(fields[1]) if fields[1] is not None else "无平台消息"),
                code=fields[0],
            )
        if "xml" not in lowered:
            raise IdaasGetContentInfoError("响应媒体类型不是 XML: %s" % content_type)
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise IdaasGetContentInfoError("响应包含不支持的 DTD/实体声明")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise IdaasGetContentInfoError("平台响应不是合法 XML")
    # 同时返回解析树（用于业务码判定）与原始字节（用于 rawXml）
    return root, raw


def _post(xml_bytes, request_headers):
    url = _build_url()
    headers = {"Content-Type": "application/xml; charset=utf-8", "Accept": "application/xml"}
    headers.update(_require_headers(request_headers))
    request = core_control_http.query_request(url, data=xml_bytes, headers=headers, method="POST")
    try:
        response = _OPENER.open(request, timeout=_TIMEOUT_SECONDS)
        try:
            status = response.getcode()
            content_type = response.headers.get("Content-Type") if response.headers else None
            trace_id = upstream_trace.read(response.headers)
            raw = response.read()
        finally:
            response.close()
    except urllib.error.HTTPError as err:
        raise _http_error(err)
    except OSError as err:
        raise IdaasGetContentInfoError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise IdaasGetContentInfoError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
    result, raw = _parse_xml_response(raw, content_type)
    return result, raw, trace_id
