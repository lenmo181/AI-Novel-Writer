import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaas/user/folder/query"
_TIMEOUT_SECONDS = 15


class IdaasUserFolderQueryError(RuntimeError):
    def __init__(self, message, *, code=None, http_status=None, network=False, timeout=False):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.network = network
        self.timeout = timeout
        self.contract = code is None and http_status is None and not network and not timeout


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = core_control_http.build_opener(
    _NoRedirectHandler,
    urllib.request.HTTPSHandler(context=ssl.create_default_context()),
)


def call(*, request_headers):
    # 该接口不接收业务入参：身份只由请求头（creditToken / clawUid）承载，因此不发请求体
    headers = _require_headers(request_headers)
    payload = _post(headers)
    code = payload.get("code")
    if not isinstance(code, str) or code != "0":
        raise IdaasUserFolderQueryError(_failure_message(code, payload.get("message")), code=code)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise IdaasUserFolderQueryError("data 必须是对象")
    # 样例 data 为 {folderId, idPath, namePath}；folderId 是上层唯一的真实目录标识，缺失即不可用；
    # idPath / namePath 缺失或扩展字段按原样保留，不做投影。
    # data 一级的 terminalAgentName 为可变实例名称，由 agent.info 校验；不影响目录查询。
    folder_id = data.get("folderId")
    if not isinstance(folder_id, str) or not folder_id:
        raise IdaasUserFolderQueryError("data.folderId 必须是非空字符串")
    for key in ("idPath", "namePath"):
        value = data.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise IdaasUserFolderQueryError("data.%s 必须是非空字符串" % key)
    normalized_data = dict(data)
    id_path = normalized_data.get("idPath")
    if id_path is not None:
        normalized_data["idPath"] = id_path.removeprefix("root:")
    name_path = normalized_data.get("namePath")
    if name_path is not None:
        normalized_data["namePath"] = name_path.removeprefix("root:")
    return {"data": normalized_data}


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise IdaasUserFolderQueryError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise IdaasUserFolderQueryError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise IdaasUserFolderQueryError("request_headers 的值必须是字符串")
        headers[key] = item
    if not any(key.lower() == "credittoken" and value for key, value in headers.items()):
        raise IdaasUserFolderQueryError("正式鉴权要求非空 creditToken 请求头")
    return headers


def _build_url():
    config = platform_config.load_platform_config()
    base = config.require("base_url").rstrip("/")
    url = base + _PATH
    parsed = urllib.parse.urlsplit(url)
    base_parsed = urllib.parse.urlsplit(base)
    # 地址来自 EXE 旁 cmcloud.json 的受批准配置，不接受任何调用参数覆盖
    if parsed.scheme != base_parsed.scheme or parsed.netloc != base_parsed.netloc:
        raise IdaasUserFolderQueryError("请求地址校验失败")
    return url


def _failure_message(code, message):
    return "平台返回失败(code=%s): %s" % (code, str(message) if message is not None else "无平台消息")


def _extract_error_fields(raw):
    try:
        failed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(failed, dict):
        return None
    return failed.get("code"), failed.get("message")


def _post(request_headers):
    url = _build_url()
    headers = {"Accept": "application/json"}
    headers.update(request_headers)
    request = core_control_http.query_request(url, headers=headers)  # 只读查询：显式声明，可安全重放
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
        raise IdaasUserFolderQueryError(
            "HTTP 状态码 %s: %s" % (err.code, str(fields[1]) if fields[1] is not None else "无平台消息"),
            code=fields[0],
            http_status=err.code,
        )
    except OSError as err:
        reason = getattr(err, "reason", None) or err
        raise IdaasUserFolderQueryError(
            "目录服务网络请求失败", network=True,
            timeout=isinstance(reason, TimeoutError),
        ) from err
    if not isinstance(status, int) or not 200 <= status < 300:
        raise IdaasUserFolderQueryError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
    if content_type is not None and "json" not in content_type.lower():
        raise IdaasUserFolderQueryError("响应媒体类型不是 JSON: %s" % content_type)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise IdaasUserFolderQueryError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise IdaasUserFolderQueryError("平台响应不是 JSON 对象")
    return upstream_trace.attach(payload, trace_id)
