import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/personalSaas/task/get"
_TIMEOUT_SECONDS = 15

_TASK_STATUSES = ("Failed", "Running", "PartialSucceed", "Succeed")


class TaskGetError(RuntimeError):
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


def call(*, task_id, request_headers):
    _require_id(task_id, "task_id")
    payload = _post({"taskId": task_id}, request_headers)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise TaskGetError("响应缺少业务 data 对象")
    task_info = data.get("taskInfo")
    if not isinstance(task_info, dict):
        raise TaskGetError("响应缺少 taskInfo 对象")
    _validate_task_info(task_info)
    results = data.get("batchFileResults")
    if results is not None:
        if not isinstance(results, list):
            raise TaskGetError("batchFileResults 必须是数组")
        for item in results:
            if not isinstance(item, dict):
                raise TaskGetError("batchFileResults 每项必须是对象")
            if "errCode" in item and not isinstance(item["errCode"], str):
                raise TaskGetError("batchFileResults 项 errCode 类型非法")
            if "message" in item and not isinstance(item["message"], str):
                raise TaskGetError("batchFileResults 项 message 类型非法")
    # 文档说明 batchFileResults 仅批量移动/批量彻底删除任务返回，且不含子目录/子文件结果
    return payload


def _validate_task_info(value):
    task_id = value.get("taskId")
    if not isinstance(task_id, str) or not task_id or len(task_id) > 64:
        raise TaskGetError("taskInfo.taskId 必须是长度不超过 64 的非空字符串")
    task_type = value.get("taskType")
    if isinstance(task_type, bool) or not isinstance(task_type, int):
        raise TaskGetError("taskInfo.taskType 必须是整数")
    status = value.get("status")
    if not isinstance(status, str) or status not in _TASK_STATUSES:
        raise TaskGetError("taskInfo.status 取值非法")
    progress = value.get("progress")
    if isinstance(progress, bool) or not isinstance(progress, int):
        raise TaskGetError("taskInfo.progress 必须是整数")
    for key in ("code", "message"):
        if key in value and not isinstance(value[key], str):
            raise TaskGetError("taskInfo.%s 类型非法" % key)


def _require_id(value, name):
    if not isinstance(value, str) or not value or len(value) > 64:
        raise TaskGetError("%s 必须是长度不超过 64 的非空字符串" % name)


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise TaskGetError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise TaskGetError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise TaskGetError("request_headers 的值必须是字符串")
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
        raise TaskGetError("请求地址校验失败")
    return url


def _failure_message(code, message):
    return "平台返回失败(code=%s): %s" % (code, str(message) if message is not None else "无平台消息")


def _validate_response(payload):
    success = payload.get("success")
    code = payload.get("code")
    message = payload.get("message")
    if success is None and code is None:
        raise TaskGetError("平台响应缺少 success 与 code，不能判定为成功")
    if success is not None and str(success).strip().lower() not in ("true", "1"):
        raise TaskGetError(_failure_message(code, message), code=code)
    if code is not None and str(code).strip().lower() not in ("0000", "0"):
        raise TaskGetError(_failure_message(code, message), code=code)
    return payload


def _post(body, request_headers):
    url = _build_url()
    payload_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
    headers.update(_require_headers(request_headers))
    request = core_control_http.query_request(url, data=payload_bytes, headers=headers)  # 只读查询：显式声明，可安全重放
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
        raise TaskGetError(
            "HTTP 状态码 %s: %s" % (err.code, str(message) if message is not None else "无平台消息"),
            code=code,
            http_status=err.code,
        )
    except OSError as err:
        raise TaskGetError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise TaskGetError("HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise TaskGetError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise TaskGetError("平台响应不是 JSON 对象")
    return _validate_response(upstream_trace.attach(payload, trace_id))
