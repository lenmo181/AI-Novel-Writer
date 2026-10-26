import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/personalSaas/file/createFolder"
_TIMEOUT_SECONDS = 15

_FORBIDDEN_NAME_CHARS = '\\/:*?"<>|'
_RENAME_MODES = ("force_rename", "refuse")
_PARENT_PATH_MAX_LEVELS = 29


class FileCreateFolderError(RuntimeError):
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


def call(*, name, request_headers, parent_file_id=None, parent_path=None, description=None, file_rename_mode=None):
    # §3.1 字段表：parentFileId 与 parentPath 互斥，二者必须且只能提供一个；
    # parentPath 目标不存在时平台会自动创建，非必要场景不使用，是否允许由上层按获准目录范围决定
    if (parent_file_id is None) == (parent_path is None):
        raise FileCreateFolderError("parent_file_id 与 parent_path 必须二选一")
    _require_name(name)
    if parent_file_id is not None:
        _require_id(parent_file_id, "parent_file_id")
    else:
        _require_parent_path(parent_path)
    if description is not None:
        _require_description(description)
    # 文档：省略 fileRenameMode 时平台默认 force_rename；未指定时不发送该字段；目录类型不支持 auto_rename
    if file_rename_mode is not None and file_rename_mode not in _RENAME_MODES:
        raise FileCreateFolderError("file_rename_mode 仅支持 force_rename/refuse")
    body = {"name": name}
    if parent_file_id is not None:
        body["parentFileId"] = parent_file_id
    else:
        body["parentPath"] = parent_path
    if description is not None:
        body["description"] = description
    if file_rename_mode is not None:
        body["fileRenameMode"] = file_rename_mode
    payload = _post(body, request_headers)
    data = payload.get("data")
    _validate_data(data)
    return payload


def _validate_data(data):
    if not isinstance(data, dict):
        raise FileCreateFolderError("响应缺少业务 data 对象")
    for key in ("fileId", "fileName"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise FileCreateFolderError("响应缺少字符串字段: %s" % key)
    if "exist" in data and _as_bool(data["exist"]) is None:
        raise FileCreateFolderError("响应 exist 类型非法")
    if "type" in data and data["type"] not in ("file", "folder"):
        raise FileCreateFolderError("响应 type 取值非法")
    # exist=true 是同名目录已存在的文档定义结果，返回完整响应由上层决定复用或结束，不在此抛错


def _require_id(value, name):
    if not isinstance(value, str) or not value or len(value) > 64:
        raise FileCreateFolderError("%s 必须是长度不超过 64 的非空字符串" % name)


def _require_name(value):
    if not isinstance(value, str) or not value:
        raise FileCreateFolderError("name 必须是非空字符串")
    if len(value.encode("utf-8")) > 255:
        raise FileCreateFolderError("name 的 UTF-8 长度不得超过 255 字节")
    if any(ch in value for ch in _FORBIDDEN_NAME_CHARS):
        raise FileCreateFolderError("name 含平台禁止的非法字符")


def _require_parent_path(value):
    if not isinstance(value, str) or not value:
        raise FileCreateFolderError("parent_path 必须是非空字符串")
    if not value.startswith("/"):
        raise FileCreateFolderError("parent_path 必须以 / 开头")
    if value != "/" and value.endswith("/"):
        raise FileCreateFolderError("parent_path 不得以 / 结尾")
    if "//" in value:
        raise FileCreateFolderError("parent_path 不得包含空层级")
    if len([segment for segment in value.split("/") if segment]) > _PARENT_PATH_MAX_LEVELS:
        raise FileCreateFolderError("parent_path 最大 %d 层级" % _PARENT_PATH_MAX_LEVELS)


def _require_description(value):
    if not isinstance(value, str):
        raise FileCreateFolderError("description 必须是字符串")
    if len(value) > 1024:
        raise FileCreateFolderError("description 不得超过 1024 字符")


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1"):
            return True
        if text in ("false", "0"):
            return False
    return None


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise FileCreateFolderError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise FileCreateFolderError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise FileCreateFolderError("request_headers 的值必须是字符串")
        headers[key] = item
    return headers


def _build_url():
    config = platform_config.load_platform_config()
    base = config.require("personal_saas_base_url").rstrip("/")
    url = base + _PATH
    parsed = urllib.parse.urlsplit(url)
    base_parsed = urllib.parse.urlsplit(base)
    # 地址来自 EXE 旁 cmcloud.json 的受批准配置，不接受任何调用参数覆盖
    if parsed.scheme != "https" or parsed.netloc != base_parsed.netloc:
        raise FileCreateFolderError("请求地址校验失败")
    return url


def _failure_message(code, message):
    return "平台返回失败(code=%s): %s" % (code, str(message) if message is not None else "无平台消息")


def _validate_response(payload):
    success = payload.get("success")
    code = payload.get("code")
    message = payload.get("message")
    if success is None and code is None:
        raise FileCreateFolderError("平台响应缺少 success 与 code，不能判定为成功")
    if success is not None and str(success).strip().lower() not in ("true", "1"):
        raise FileCreateFolderError(_failure_message(code, message), code=code)
    if code is not None and str(code).strip().lower() not in ("0000", "0"):
        raise FileCreateFolderError(_failure_message(code, message), code=code)
    return payload


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
            raw = response.read()
        finally:
            response.close()
    except urllib.error.HTTPError as err:
        code = message = None
        try:
            failed = json.loads(err.read().decode("utf-8"))
            if isinstance(failed, dict):
                code = failed.get("code")
                message = failed.get("message")
        except (UnicodeDecodeError, ValueError):
            pass
        raise FileCreateFolderError(
            "HTTP 状态码 %s: %s" % (err.code, str(message) if message is not None else "无平台消息"),
            code=code,
            http_status=err.code,
        )
    except OSError as err:
        raise FileCreateFolderError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise FileCreateFolderError("HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise FileCreateFolderError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise FileCreateFolderError("平台响应不是 JSON 对象")
    return _validate_response(upstream_trace.attach(payload, trace_id))
