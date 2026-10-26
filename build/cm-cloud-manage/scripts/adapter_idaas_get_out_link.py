import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
import xml.etree.ElementTree as ET
from collections.abc import Mapping

from cmcloud_core import platform_config

_PATH = "/richlifeApp/idaasV3/devapp/getOutLink"
_TIMEOUT_SECONDS = 15


class IdaasGetOutLinkError(RuntimeError):
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


def call(
    *,
    link_type=None,
    sub_link_type=None,
    encrypt=None,
    co_id_list=None,
    ca_id_list=None,
    tag_id=None,
    tag_type=None,
    pub_type=None,
    desc=None,
    viewer_list=None,
    dedicated_name=None,
    path=None,
    period=None,
    period_unit=None,
    is_send_sms=None,
    is_unlimited_times=None,
    request_headers,
):
    if not co_id_list and not ca_id_list and not tag_id:
        raise IdaasGetOutLinkError("co_id_list、ca_id_list、tag_id 至少一个非空")
    if pub_type == 2 and not viewer_list:
        raise IdaasGetOutLinkError("pub_type=2 时 viewer_list 必须非空")
    root = ET.Element("getOutLink")
    req = ET.SubElement(root, "getOutLinkReq")
    for name, value in (
        ("linkType", link_type),
        ("subLinkType", sub_link_type),
        ("encrypt", encrypt),
        ("pubType", pub_type),
        ("isSendSMS", is_send_sms),
        ("isUnlimitedTimes", is_unlimited_times),
    ):
        if value is not None:
            ET.SubElement(req, name).text = str(_require_int(value, name))
    for name, value in (("tagID", tag_id), ("tagType", tag_type), ("desc", desc), ("dedicatedName", dedicated_name), ("path", path)):
        if value is not None:
            ET.SubElement(req, name).text = _require_text(value, name)
    if co_id_list is not None:
        _append_item_list(req, "coIDLst", co_id_list)
    if ca_id_list is not None:
        _append_item_list(req, "caIDLst", ca_id_list)
    if viewer_list is not None:
        _append_item_list(req, "viewerLst", viewer_list)
    for name, value in (("period", period), ("periodUnit", period_unit)):
        if value is not None:
            ET.SubElement(req, name).text = str(_require_int(value, name))
    raw = _post(ET.tostring(root, encoding="utf-8"), request_headers)
    # 分享域成功响应未定义固定 DTO，返回原始 XML 字符串；isSendSMS 未传时由平台默认流程处理。
    # 成功样例为 <result resultCode="0">，含 getOutLinkResSet/@length、getOutLinkResOne；
    # 样例 linkUrl 中 "https: //" 含空格，不能作为有效 URL 常量
    return raw.decode("utf-8")


def _append_item_list(parent, tag, values):
    items = _require_str_list(values, tag)
    container = ET.SubElement(parent, tag)
    container.set("length", str(len(items)))
    for item in items:
        ET.SubElement(container, "item").text = item


def _require_text(value, name):
    if not isinstance(value, str) or not value:
        raise IdaasGetOutLinkError("%s 必须是非空字符串" % name)
    return value


def _require_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise IdaasGetOutLinkError("%s 必须是整数" % name)
    return value


def _require_str_list(value, name):
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)) or not value:
        raise IdaasGetOutLinkError("%s 必须是非空字符串列表" % name)
    items = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise IdaasGetOutLinkError("%s 每项必须是非空字符串" % name)
        items.append(item)
    return items


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise IdaasGetOutLinkError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise IdaasGetOutLinkError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise IdaasGetOutLinkError("request_headers 的值必须是字符串")
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
        raise IdaasGetOutLinkError("请求地址校验失败")
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
    return IdaasGetOutLinkError(
        "HTTP 状态码 %s: %s" % (err.code, str(fields[1]) if fields[1] is not None else "无平台消息"),
        code=fields[0],
        http_status=err.code,
    )


def _post(xml_bytes, request_headers):
    url = _build_url()
    headers = {"Content-Type": "application/xml; charset=utf-8", "Accept": "application/xml"}
    headers.update(_require_headers(request_headers))
    request = urllib.request.Request(url, data=xml_bytes, headers=headers, method="POST")
    try:
        response = _OPENER.open(request, timeout=_TIMEOUT_SECONDS)
        try:
            status = response.getcode()
            content_type = response.headers.get("Content-Type") if response.headers else None
            raw = response.read()
        finally:
            response.close()
    except urllib.error.HTTPError as err:
        raise _http_error(err)
    except OSError as err:
        raise IdaasGetOutLinkError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise IdaasGetOutLinkError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
    if content_type is not None:
        lowered = content_type.lower()
        if "json" in lowered:
            fields = _json_error_fields(raw) or (None, None)
            raise IdaasGetOutLinkError(
                "响应为 JSON 错误外壳: %s" % (str(fields[1]) if fields[1] is not None else "无平台消息"),
                code=fields[0],
            )
        if "xml" not in lowered:
            raise IdaasGetOutLinkError("响应媒体类型不是 XML: %s" % content_type)
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise IdaasGetOutLinkError("响应包含不支持的 DTD/实体声明")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise IdaasGetOutLinkError("平台响应不是合法 XML")
    result_code = root.get("resultCode")
    # 根为 result、resultCode 存在且非 "0" 即按业务码失败处理（错误消息保留 desc），与是否携带子节点无关
    if root.tag == "result" and result_code is not None and result_code != "0":
        raise IdaasGetOutLinkError(_failure_message(result_code, root.get("desc")), code=result_code)
    return raw
