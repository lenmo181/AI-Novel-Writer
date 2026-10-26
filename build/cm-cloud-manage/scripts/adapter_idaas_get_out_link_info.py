import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
import xml.etree.ElementTree as ET
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/devapp/getOutLinkInfo"
_TIMEOUT_SECONDS = 15

# §3.16 返回示例及同域 XML 协议确认 resultCode="0" 为成功。
# 非空且非 "0" 的 resultCode 与同域 getOutLink / getOutLinkNew 同口径，一律判为业务码失败
# （原因在 desc）；不枚举失败码，避免未登记的业务码被误判成契约异常。
_CONFIRMED_SUCCESS_CODES = frozenset(("0",))


class IdaasGetOutLinkInfoError(RuntimeError):
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
    link_id,
    passwd=None,
    p_ca_id=None,
    ca_srt=None,
    co_srt=None,
    srt_dr=None,
    b_num=None,
    e_num=None,
    aes=None,
    request_headers,
):
    root = ET.Element("getOutLinkInfo")
    req = ET.SubElement(root, "getOutLinkInfoReq")
    ET.SubElement(req, "linkID").text = _bounded_text(link_id, "link_id", 32)
    if passwd is not None:
        ET.SubElement(req, "passwd").text = _bounded_text(passwd, "passwd", 32)
    # pCaID may be a full path, longer than the documented single-ID length.
    parent = _require_text(p_ca_id, "p_ca_id")
    if any(not part.strip() or part in (".", "..") for part in parent.split("/")):
        raise IdaasGetOutLinkInfoError("p_ca_id 路径非法")
    ET.SubElement(req, "pCaID").text = parent
    if ca_srt is not None:
        ET.SubElement(req, "caSrt").text = _enum_number(ca_srt, "ca_srt", (0, 1))
    if co_srt is not None:
        ET.SubElement(req, "coSrt").text = _enum_number(co_srt, "co_srt", (0, 1, 2, 3))
    if srt_dr is not None:
        ET.SubElement(req, "srtDr").text = _enum_number(srt_dr, "srt_dr", (0, 1))
    start = -1
    if b_num is not None:
        start = int(_require_number_text(b_num, "b_num"))
        if start != -1 and start <= 0:
            raise IdaasGetOutLinkInfoError("b_num 必须为 -1 或正整数")
        ET.SubElement(req, "bNum").text = str(start)
    # bNum omitted/-1 means the first 200 entries; eNum is ignored by contract.
    if e_num is not None and start != -1:
        end = int(_require_number_text(e_num, "e_num"))
        if end < start:
            raise IdaasGetOutLinkInfoError("e_num 不得小于 b_num")
        ET.SubElement(req, "eNum").text = str(end)
    if aes is not None:
        ET.SubElement(req, "aes").text = _require_text(aes, "aes")
    result, raw, trace_id = _post(ET.tostring(root, encoding="utf-8"), request_headers)
    if result.tag != "result":
        raise IdaasGetOutLinkInfoError("响应根元素不是 result")
    result_code = result.get("resultCode")
    desc = result.get("desc")
    data = _response_data(result)
    if result_code in _CONFIRMED_SUCCESS_CODES:
        status = "CONFIRMED_SUCCESS"
    elif result_code is None:
        # 根元素没有 resultCode：无法判定结果，按契约异常交上层处理。
        status = "UNKNOWN"
    else:
        # 非 "0" 即业务码失败（提取码错误、链接失效等），原因保留在 desc。
        status = "CONFIRMED_FAILURE"
    payload = {
        "status": status,
        "resultCode": result_code,
        "desc": desc,
        "data": data,
        "rawXml": raw.decode("utf-8"),
    }
    return upstream_trace.attach(payload, trace_id)


def _response_data(result):
    response = result.find("getOutLinkInfoRes")
    if response is None:
        return None
    return {
        "parentCatalogId": _text(response, "pCaID"),
        "nodeCount": _integer(_text(response, "nodNum")),
        "linkName": _text(response, "lkName"),
        "creator": _text(response, "creator"),
        "owner": _text(response, "owner"),
        "nickname": _text(response, "nikeName"),
        "expireTime": _text(response, "expireTime"),
        "catalogs": _rows(response.find("caLst"), "outLinkCaInfo"),
        "contents": _rows(response.find("coLst"), "outLinkCoInfo"),
    }


def _rows(container, item_tag):
    if container is None:
        return []
    return [{child.tag: child.text or "" for child in item} for item in container.findall(item_tag)]


def _text(parent, tag):
    node = parent.find(tag)
    return node.text if node is not None else None


def _integer(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _require_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise IdaasGetOutLinkInfoError("%s 必须是非空字符串" % name)
    return value


def _bounded_text(value, name, limit):
    value = _require_text(value, name)
    if len(value) > limit:
        raise IdaasGetOutLinkInfoError("%s 长度不得超过 %d" % (name, limit))
    return value


def _enum_number(value, name, allowed):
    number = int(_require_number_text(value, name))
    if number not in allowed:
        raise IdaasGetOutLinkInfoError("%s 取值非法" % name)
    return str(number)


def _require_number_text(value, name):
    if isinstance(value, bool):
        raise IdaasGetOutLinkInfoError("%s 必须是数字或数字字符串" % name)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value):
        return value
    raise IdaasGetOutLinkInfoError("%s 必须是数字或数字字符串" % name)


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise IdaasGetOutLinkInfoError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise IdaasGetOutLinkInfoError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise IdaasGetOutLinkInfoError("request_headers 的值必须是字符串")
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
        raise IdaasGetOutLinkInfoError("请求地址校验失败")
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
    return IdaasGetOutLinkInfoError(
        "HTTP 状态码 %s: %s" % (err.code, str(fields[1]) if fields[1] is not None else "无平台消息"),
        code=fields[0],
        http_status=err.code,
    )


def _parse_xml_response(raw, content_type):
    if content_type is not None:
        lowered = content_type.lower()
        if "json" in lowered:
            fields = _json_error_fields(raw) or (None, None)
            raise IdaasGetOutLinkInfoError(
                "响应为 JSON 错误外壳: %s" % (str(fields[1]) if fields[1] is not None else "无平台消息"),
                code=fields[0],
            )
        if "xml" not in lowered:
            raise IdaasGetOutLinkInfoError("响应媒体类型不是 XML: %s" % content_type)
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise IdaasGetOutLinkInfoError("响应包含不支持的 DTD/实体声明")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise IdaasGetOutLinkInfoError("平台响应不是合法 XML")
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
        raise IdaasGetOutLinkInfoError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise IdaasGetOutLinkInfoError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
    result, raw = _parse_xml_response(raw, content_type)
    return result, raw, trace_id
