import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/devapp/share/createSpecShareLink"
_TIMEOUT_SECONDS = 15


class IdaasCreateSpecShareLinkError(RuntimeError):
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
    share_channel=None,
    share_content=None,
    pub_type=None,
    viewer_list=None,
    share_title=None,
    ext_info=None,
    encrypt=None,
    period=None,
    period_unit=None,
    request_headers,
):
    body = {}
    if share_channel is not None:
        body["shareChannel"] = _require_int(share_channel, "share_channel")
    if share_content is not None:
        body["shareContent"] = _require_text(share_content, "share_content")
    if pub_type is not None:
        body["pubType"] = _require_int(pub_type, "pub_type")
    if viewer_list is not None:
        body["viewerList"] = _require_viewers(viewer_list)
    if share_title is not None:
        body["shareTitle"] = _require_text(share_title, "share_title")
    if ext_info is not None:
        body["extInfo"] = _require_text(ext_info, "ext_info")
    if encrypt is not None:
        body["encrypt"] = _require_int(encrypt, "encrypt")
    if period is not None:
        body["period"] = _require_int(period, "period")
    if period_unit is not None:
        body["periodUnit"] = _require_int(period_unit, "period_unit")
    if pub_type == 2 and not body.get("viewerList"):
        raise IdaasCreateSpecShareLinkError("pub_type=2 时 viewer_list 必须非空")
    payload = _post(body, request_headers)
    # viewerList[].account 是 AES-GCM 密文，由可信调用方提供；shareChannel/shareContent 编码待下游确认；
    # 已确认成功返回为裸对象（linkId/linkName/linkUrl/accessCode，均可能缺省），无 code/success 外壳，不得因缺通用成功码误报失败；
    # 原样返回完整响应并保留未知字段
    return payload


def _require_text(value, name):
    if not isinstance(value, str) or not value:
        raise IdaasCreateSpecShareLinkError("%s 必须是非空字符串" % name)
    return value


def _require_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise IdaasCreateSpecShareLinkError("%s 必须是整数" % name)
    return value


def _require_viewers(value):
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)) or not value:
        raise IdaasCreateSpecShareLinkError("viewer_list 必须是非空列表")
    viewers = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise IdaasCreateSpecShareLinkError("viewer_list 每项必须是映射")
        unknown = set(entry) - {"account"}
        if unknown:
            raise IdaasCreateSpecShareLinkError(
                "viewer_list 项包含未定义字段: %s" % ", ".join(sorted(str(key) for key in unknown))
            )
        account = entry.get("account")
        if not isinstance(account, str) or not account or len(account) > 64:
            raise IdaasCreateSpecShareLinkError("viewerList.account 必须是长度不超过 64 的非空字符串")
        viewers.append({"account": account})
    return viewers


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise IdaasCreateSpecShareLinkError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise IdaasCreateSpecShareLinkError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise IdaasCreateSpecShareLinkError("request_headers 的值必须是字符串")
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
        raise IdaasCreateSpecShareLinkError("请求地址校验失败")
    return url


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
    headers.update(_require_headers(request_headers))
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
        raise IdaasCreateSpecShareLinkError(
            "HTTP 状态码 %s: %s" % (err.code, str(fields[1]) if fields[1] is not None else "无平台消息"),
            code=fields[0],
            http_status=err.code,
        )
    except OSError as err:
        raise IdaasCreateSpecShareLinkError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise IdaasCreateSpecShareLinkError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
    if content_type is not None and "json" not in content_type.lower():
        raise IdaasCreateSpecShareLinkError("响应媒体类型不是 JSON: %s" % content_type)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise IdaasCreateSpecShareLinkError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise IdaasCreateSpecShareLinkError("平台响应不是 JSON 对象")
    return upstream_trace.attach(payload, trace_id)
