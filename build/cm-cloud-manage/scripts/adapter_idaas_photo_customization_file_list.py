import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/personalSaas/album/photo/customization/file/list"
_TIMEOUT_SECONDS = 15


class IdaasPhotoCustomizationFileListError(RuntimeError):
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
    album_id,
    order_by,
    order_direction,
    owner_type=None,
    image_thumbnail_style_list=None,
    page_info=None,
    request_headers,
):
    headers = _require_headers(request_headers)
    if "x-yun-client-info" not in {key.lower() for key in headers}:
        raise IdaasPhotoCustomizationFileListError(
            "photo customization file list 必须提供 x-yun-client-info 请求头"
        )
    # orderBy/orderDirection 为必填：服务端缺任一个即返回 01000001 参数错误（文档标可选，实测必填）；
    # 本接口 orderBy 为 Integer，故事图片列表对应字段为 String 且不传
    body = {
        "albumId": _require_text(album_id, "album_id"),
        "orderBy": _require_int(order_by, "order_by"),
        "orderDirection": _require_text(order_direction, "order_direction"),
    }
    if owner_type is not None:
        body["ownerType"] = _require_text(owner_type, "owner_type")
    if image_thumbnail_style_list is not None:
        body["imageThumbnailStyleList"] = _require_str_list(image_thumbnail_style_list, "image_thumbnail_style_list")
    if page_info is not None:
        body["pageInfo"] = _require_page_info(page_info)
    payload = _post(body, headers)
    # 成功外壳为 success:true/code:"0000"（限本组协议，不推广至其他接口）；data.nextPageCursor/list 为素材列表；
    # userId/belongUserId 由服务端覆盖，其余返回原始 JSON 结构
    return payload


def _require_text(value, name):
    if not isinstance(value, str) or not value:
        raise IdaasPhotoCustomizationFileListError("%s 必须是非空字符串" % name)
    return value


def _require_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise IdaasPhotoCustomizationFileListError("%s 必须是整数" % name)
    return value


def _require_str_list(value, name):
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise IdaasPhotoCustomizationFileListError("%s 必须是字符串列表" % name)
    items = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise IdaasPhotoCustomizationFileListError("%s 每项必须是非空字符串" % name)
        items.append(item)
    return items


def _require_page_info(value):
    if not isinstance(value, Mapping):
        raise IdaasPhotoCustomizationFileListError("page_info 必须是映射")
    page_info = dict(value)
    if "pageCursor" in page_info and (not isinstance(page_info["pageCursor"], str) or not page_info["pageCursor"]):
        raise IdaasPhotoCustomizationFileListError("pageInfo.pageCursor 必须是非空字符串")
    if "pageSize" in page_info:
        page_size = page_info["pageSize"]
        if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size <= 0:
            raise IdaasPhotoCustomizationFileListError("pageInfo.pageSize 必须是正整数")
    return page_info


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise IdaasPhotoCustomizationFileListError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise IdaasPhotoCustomizationFileListError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise IdaasPhotoCustomizationFileListError("request_headers 的值必须是字符串")
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
        raise IdaasPhotoCustomizationFileListError("请求地址校验失败")
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
        raise IdaasPhotoCustomizationFileListError(
            "HTTP 状态码 %s: %s" % (err.code, str(fields[1]) if fields[1] is not None else "无平台消息"),
            code=fields[0],
            http_status=err.code,
        )
    except OSError as err:
        raise IdaasPhotoCustomizationFileListError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise IdaasPhotoCustomizationFileListError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
    if content_type is not None and "json" not in content_type.lower():
        raise IdaasPhotoCustomizationFileListError("响应媒体类型不是 JSON: %s" % content_type)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise IdaasPhotoCustomizationFileListError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise IdaasPhotoCustomizationFileListError("平台响应不是 JSON 对象")
    return upstream_trace.attach(payload, trace_id)
