import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
import xml.etree.ElementTree as ET
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/devapp/queryBatchOprTaskDetail"
_TIMEOUT_SECONDS = 15


class IdaasQueryBatchTaskDetailError(RuntimeError):
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


def call(*, task_id, msisdn=None, request_headers):
    root = ET.Element("queryBatchOprTaskDetailV2")
    req = ET.SubElement(root, "queryBatchOprTaskDetailReq")
    if msisdn is not None:
        # 上下文已有 msisdn 时覆盖此字段；否则该值必须是可按应用协议解密的 AES-GCM 密文
        ET.SubElement(req, "msisdn").text = _require_text(msisdn, "msisdn")
    ET.SubElement(req, "taskID").text = _require_text(task_id, "task_id")
    raw, trace_id = _post(ET.tostring(root, encoding="utf-8"), request_headers)
    return upstream_trace.attach(_parse_success(raw), trace_id)


def _parse_success(raw):
    root = ET.fromstring(raw)
    _require_success_root(root)
    response = root.find("queryBatchOprTaskDetailRes")
    if response is None:
        raise IdaasQueryBatchTaskDetailError("成功响应缺少 queryBatchOprTaskDetailRes")
    task = response.find("batchOprTask")
    if task is None:
        raise IdaasQueryBatchTaskDetailError("成功响应缺少 batchOprTask")
    return {
        "status": "CONFIRMED_SUCCESS",
        "resultCode": root.get("resultCode"),
        "desc": root.get("desc"),
        "contentList": _id_results(response.find("contentList")),
        "catalogList": _id_results(response.find("catalogList")),
        "linkIDlist": _id_results(response.find("linkIDlist")),
        "batchOprTask": {child.tag: _scalar(child.text) for child in task},
        "rawXml": raw.decode("utf-8"),
    }


def _require_success_root(root):
    if root.tag != "result":
        raise IdaasQueryBatchTaskDetailError("响应根元素不是 result")
    result_code = root.get("resultCode")
    if result_code is None:
        raise IdaasQueryBatchTaskDetailError("响应缺少 resultCode，无法确认查询成功")
    if result_code != "0":
        raise IdaasQueryBatchTaskDetailError(
            _failure_message(result_code, root.get("desc")), code=result_code
        )


def _id_results(container):
    if container is None:
        return []
    rows = []
    for entry in container.findall("idRspInfo"):
        rows.append({child.tag: child.text or "" for child in entry})
    return rows


def _scalar(value):
    text = value or ""
    if text.isdigit():
        return int(text)
    return text


def _require_text(value, name):
    if not isinstance(value, str) or not value:
        raise IdaasQueryBatchTaskDetailError("%s 必须是非空字符串" % name)
    return value


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise IdaasQueryBatchTaskDetailError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise IdaasQueryBatchTaskDetailError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise IdaasQueryBatchTaskDetailError("request_headers 的值必须是字符串")
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
        raise IdaasQueryBatchTaskDetailError("请求地址校验失败")
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
    return IdaasQueryBatchTaskDetailError(
        "HTTP 状态码 %s: %s" % (err.code, str(fields[1]) if fields[1] is not None else "无平台消息"),
        code=fields[0],
        http_status=err.code,
    )


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
        raise IdaasQueryBatchTaskDetailError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise IdaasQueryBatchTaskDetailError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
    if content_type is not None:
        lowered = content_type.lower()
        if "json" in lowered:
            fields = _json_error_fields(raw) or (None, None)
            raise IdaasQueryBatchTaskDetailError(
                "响应为 JSON 错误外壳: %s" % (str(fields[1]) if fields[1] is not None else "无平台消息"),
                code=fields[0],
            )
        if "xml" not in lowered:
            raise IdaasQueryBatchTaskDetailError("响应媒体类型不是 XML: %s" % content_type)
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise IdaasQueryBatchTaskDetailError("响应包含不支持的 DTD/实体声明")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise IdaasQueryBatchTaskDetailError("平台响应不是合法 XML")
    # 查询本身必须明确成功；不凭任务内部字段推断查询成功。
    _require_success_root(root)
    return raw, trace_id
