import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/personalSaas/album/story/memory/playlist/list"
_TIMEOUT_SECONDS = 15


class IdaasStoryMemoryPlaylistListError(RuntimeError):
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


def call(*, album_id, page_info=None, owner_type=None, request_headers):
    body = {"albumId": _require_text(album_id, "album_id")}
    if page_info is not None:
        body["pageInfo"] = _require_page_info(page_info)
    if owner_type is not None:
        body["ownerType"] = _require_text(owner_type, "owner_type")
    payload = _post(body, request_headers)
    # 成功外壳为 success:true/code:"0000"（同组协议）；data.nextPageCursor、data.list[].fileId；
    # 根据实际出现的列表做消费，不凭示例断言一定有下一页；userId/belongUserId 由服务端覆盖，其余原始 JSON 结构
    return payload


def _require_text(value, name):
    if not isinstance(value, str) or not value:
        raise IdaasStoryMemoryPlaylistListError("%s 必须是非空字符串" % name)
    return value


def _require_page_info(value):
    if not isinstance(value, Mapping):
        raise IdaasStoryMemoryPlaylistListError("page_info 必须是映射")
    page_info = dict(value)
    if "pageCursor" in page_info and (not isinstance(page_info["pageCursor"], str) or not page_info["pageCursor"]):
        raise IdaasStoryMemoryPlaylistListError("pageInfo.pageCursor 必须是非空字符串")
    if "pageSize" in page_info:
        page_size = page_info["pageSize"]
        if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size <= 0:
            raise IdaasStoryMemoryPlaylistListError("pageInfo.pageSize 必须是正整数")
    return page_info


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise IdaasStoryMemoryPlaylistListError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise IdaasStoryMemoryPlaylistListError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise IdaasStoryMemoryPlaylistListError("request_headers 的值必须是字符串")
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
        raise IdaasStoryMemoryPlaylistListError("请求地址校验失败")
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
    request = core_control_http.query_request(url, data=payload_bytes, headers=headers, method="POST")
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
        raise IdaasStoryMemoryPlaylistListError(
            "HTTP 状态码 %s: %s" % (err.code, str(fields[1]) if fields[1] is not None else "无平台消息"),
            code=fields[0],
            http_status=err.code,
        )
    except OSError as err:
        raise IdaasStoryMemoryPlaylistListError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise IdaasStoryMemoryPlaylistListError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
    if content_type is not None and "json" not in content_type.lower():
        raise IdaasStoryMemoryPlaylistListError("响应媒体类型不是 JSON: %s" % content_type)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise IdaasStoryMemoryPlaylistListError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise IdaasStoryMemoryPlaylistListError("平台响应不是 JSON 对象")
    return upstream_trace.attach(payload, trace_id)
