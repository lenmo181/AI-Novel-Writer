import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/personalSaas/album/share/getAlbumShareInfo"
_TIMEOUT_SECONDS = 15

# 契约枚举：albumType 1-回忆相册/2-普通相册；shareTimeType 1-一天/2-七天/3-永久；开关类 0-关/1-开
_ALBUM_TYPES = (1, 2)
_SHARE_TIME_TYPES = (1, 2, 3)
_SWITCH_FLAGS = (0, 1)


class IdaasGetAlbumShareInfoError(RuntimeError):
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
    album_name,
    album_showtime,
    album_type,
    story_id,
    mood=None,
    photos=None,
    need_watermark=None,
    seq_no=None,
    share_time_type=None,
    visit_unlimited=None,
    request_headers,
):
    headers = _require_headers(request_headers)
    if "x-deviceinfo" not in {key.lower() for key in headers}:
        raise IdaasGetAlbumShareInfoError("getAlbumShareInfo 必须提供 x-DeviceInfo 请求头")
    body = {
        "albumName": _require_text(album_name, "album_name"),
        "albumShowtime": _require_text(album_showtime, "album_showtime"),
        "albumType": _require_enum(album_type, _ALBUM_TYPES, "album_type"),
        "storyId": _require_text(story_id, "story_id"),
    }
    if mood is not None:
        body["mood"] = _require_text(mood, "mood")
    if photos is not None:
        body["photos"] = _require_photos(photos)
    if need_watermark is not None:
        body["needWatermark"] = _require_enum(need_watermark, _SWITCH_FLAGS, "need_watermark")
    if seq_no is not None:
        body["seqNo"] = _require_text(seq_no, "seq_no")
    if share_time_type is not None:
        body["shareTimeType"] = _require_enum(share_time_type, _SHARE_TIME_TYPES, "share_time_type")
    if visit_unlimited is not None:
        body["visitUnlimited"] = _require_enum(visit_unlimited, _SWITCH_FLAGS, "visit_unlimited")
    # 成功与失败信封由场景层判定：实测平台包络为 success/code=0000/message/data，
    # 契约历史口径为 code/msg/ret；adapter 只保证传输层与入参符合契约。
    return _post(body, headers)


def _require_enum(value, allowed, name):
    if isinstance(value, bool) or not isinstance(value, int) or value not in allowed:
        raise IdaasGetAlbumShareInfoError(
            "%s 必须是 %s 之一" % (name, "/".join(str(item) for item in allowed))
        )
    return value


def _require_text(value, name):
    if not isinstance(value, str) or not value:
        raise IdaasGetAlbumShareInfoError("%s 必须是非空字符串" % name)
    return value


def _require_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise IdaasGetAlbumShareInfoError("%s 必须是整数" % name)
    return value


def _require_photos(value):
    # 契约中 photos 为可选：允许省略或空列表
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise IdaasGetAlbumShareInfoError("photos 必须是列表")
    photos = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise IdaasGetAlbumShareInfoError("photos 每项必须是映射")
        unknown = set(entry) - {"sort", "contentID"}
        if unknown:
            raise IdaasGetAlbumShareInfoError(
                "photos 项包含未定义字段: %s" % ", ".join(sorted(str(key) for key in unknown))
            )
        photos.append(
            {
                "sort": _require_int(entry.get("sort"), "photos.sort"),
                "contentID": _require_text(entry.get("contentID"), "photos.contentID"),
            }
        )
    return photos


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise IdaasGetAlbumShareInfoError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise IdaasGetAlbumShareInfoError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise IdaasGetAlbumShareInfoError("request_headers 的值必须是字符串")
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
        raise IdaasGetAlbumShareInfoError("请求地址校验失败")
    return url


def _extract_error_fields(raw):
    try:
        failed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(failed, dict):
        return None
    return failed.get("code"), _error_text(failed)


def _error_text(payload):
    """取平台错误文案：message 优先，兼容历史 msg 口径。"""
    for key in ("message", "msg"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _post(body, request_headers):
    url = _build_url()
    payload_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
    headers.update(request_headers)
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
        raise IdaasGetAlbumShareInfoError(
            "HTTP 状态码 %s: %s" % (err.code, str(fields[1]) if fields[1] is not None else "无平台消息"),
            code=fields[0],
            http_status=err.code,
        )
    except OSError as err:
        raise IdaasGetAlbumShareInfoError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise IdaasGetAlbumShareInfoError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
    if content_type is not None and "json" not in content_type.lower():
        raise IdaasGetAlbumShareInfoError("响应媒体类型不是 JSON: %s" % content_type)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise IdaasGetAlbumShareInfoError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise IdaasGetAlbumShareInfoError("平台响应不是 JSON 对象")
    return upstream_trace.attach(payload, trace_id)
