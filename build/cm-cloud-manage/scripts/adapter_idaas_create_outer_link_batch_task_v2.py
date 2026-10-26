import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
import xml.etree.ElementTree as ET
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/devapp/createOuterLinkBatchOprTaskV2"
_TIMEOUT_SECONDS = 15


class IdaasCreateOuterLinkBatchTaskV2Error(RuntimeError):
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
    task_type,
    link_id=None,
    link_ids=None,
    content_info_list=None,
    catalog_info_list=None,
    new_catalog_id=None,
    request_headers,
):
    task_type = _require_text(task_type, "task_type")
    if task_type not in ("1", "2"):
        raise IdaasCreateOuterLinkBatchTaskV2Error("task_type 当前仅支持 1、2")
    if task_type == "1":
        if link_id is None:
            raise IdaasCreateOuterLinkBatchTaskV2Error("task_type=1 时必须提供 link_id")
        if link_ids is not None:
            raise IdaasCreateOuterLinkBatchTaskV2Error("task_type=1 时不得提供 link_ids")
    else:
        if link_ids is None:
            raise IdaasCreateOuterLinkBatchTaskV2Error("task_type=2 时必须提供 link_ids")
        if link_id is not None:
            raise IdaasCreateOuterLinkBatchTaskV2Error("task_type=2 时不得提供 link_id")
    # 新版接口地址增加 /idaasV3，但 XML 契约沿用旧版；以正式示例为准，
    # 外层根节点仍为 getOutLinkInfo。
    root = ET.Element("getOutLinkInfo")
    req = ET.SubElement(root, "createOuterLinkBatchOprTaskReq")
    ET.SubElement(req, "taskType").text = task_type
    info = ET.SubElement(req, "taskInfo")
    if link_id is not None:
        ET.SubElement(info, "linkID").text = _require_text(link_id, "link_id")
    if link_ids is not None:
        _append_list(info, "linkIDs", "string", link_ids)
    if content_info_list is not None:
        _append_list(info, "contentInfoList", "ID", content_info_list)
    if catalog_info_list is not None:
        _append_list(info, "catalogInfoList", "ID", catalog_info_list)
    if new_catalog_id is not None:
        ET.SubElement(info, "newCatalogID").text = _require_text(new_catalog_id, "new_catalog_id")
    raw, trace_id = _post(ET.tostring(root, encoding="utf-8"), request_headers)
    return upstream_trace.attach(_parse_task_response(raw), trace_id)


def _parse_task_response(raw):
    root = ET.fromstring(raw)
    if root.tag != "result":
        raise IdaasCreateOuterLinkBatchTaskV2Error("成功响应根节点必须为 result")
    result_node = root.find("createOuterLinkBatchOprTaskRes")
    if result_node is None:
        raise IdaasCreateOuterLinkBatchTaskV2Error("成功响应缺少 createOuterLinkBatchOprTaskRes")
    task_node = result_node.find("taskID")
    task_id = (task_node.text or "").strip() if task_node is not None else ""
    if not task_id or task_id.lower() == "string":
        raise IdaasCreateOuterLinkBatchTaskV2Error("createOuterLinkBatchOprTaskRes 缺少真实 taskID")
    return {"data": {"taskId": task_id}, "resultCode": root.get("resultCode"), "rawXml": raw.decode("utf-8")}


def _append_list(parent, container_tag, item_tag, values):
    items = _require_str_list(values, container_tag)
    container = ET.SubElement(parent, container_tag)
    container.set("length", str(len(items)))
    for item in items:
        ET.SubElement(container, item_tag).text = item


def _require_text(value, name):
    if not isinstance(value, str) or not value:
        raise IdaasCreateOuterLinkBatchTaskV2Error("%s 必须是非空字符串" % name)
    return value


def _require_str_list(value, name):
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)) or not value:
        raise IdaasCreateOuterLinkBatchTaskV2Error("%s 必须是非空字符串列表" % name)
    items = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise IdaasCreateOuterLinkBatchTaskV2Error("%s 每项必须是非空字符串" % name)
        items.append(item)
    return items


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise IdaasCreateOuterLinkBatchTaskV2Error("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise IdaasCreateOuterLinkBatchTaskV2Error("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise IdaasCreateOuterLinkBatchTaskV2Error("request_headers 的值必须是字符串")
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
        raise IdaasCreateOuterLinkBatchTaskV2Error("请求地址校验失败")
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
    return IdaasCreateOuterLinkBatchTaskV2Error(
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
            trace_id = upstream_trace.read(response.headers)
            raw = response.read()
        finally:
            response.close()
    except urllib.error.HTTPError as err:
        raise _http_error(err)
    except OSError as err:
        raise IdaasCreateOuterLinkBatchTaskV2Error("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise IdaasCreateOuterLinkBatchTaskV2Error(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
    if content_type is not None:
        lowered = content_type.lower()
        if "json" in lowered:
            fields = _json_error_fields(raw) or (None, None)
            raise IdaasCreateOuterLinkBatchTaskV2Error(
                "响应为 JSON 错误外壳: %s" % (str(fields[1]) if fields[1] is not None else "无平台消息"),
                code=fields[0],
            )
        if "xml" not in lowered:
            raise IdaasCreateOuterLinkBatchTaskV2Error("响应媒体类型不是 XML: %s" % content_type)
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise IdaasCreateOuterLinkBatchTaskV2Error("响应包含不支持的 DTD/实体声明")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise IdaasCreateOuterLinkBatchTaskV2Error("平台响应不是合法 XML")
    result_code = root.get("resultCode")
    # 根为 result、resultCode 存在且非 "0" 即按业务码失败处理（错误消息保留 desc），与是否携带子节点无关
    if root.tag == "result" and result_code is not None and result_code != "0":
        raise IdaasCreateOuterLinkBatchTaskV2Error(_failure_message(result_code, root.get("desc")), code=result_code)
    return raw, trace_id
