import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import core_control_http
from collections.abc import Mapping
from datetime import datetime

from cmcloud_core import platform_config, upstream_trace

_PATH = "/richlifeApp/idaasV3/personalDynamic/queryPersonalDynamic"
_TIMEOUT_SECONDS = 15

_DYNAMIC_TYPES = (1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13)
_BATCH_PERSONAL_DYNAMIC_TYPES = frozenset((1, 2, 4, 5, 6, 7, 8, 11))
_RECENT_READING_DYNAMIC_TYPES = frozenset((1, 10, 13))
_CONTENT_TYPES = (1, 2, 3, 4)
_FILE_TYPES = ("0", "1")
_CATEGORIES = ("0", "1", "2", "3", "4", "5", "6", "100")
_DYNAMIC_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# 公共包络里表示"本次请求未产生业务副作用、稍后再试有意义"的网关业务码。
# 16110030「有其他授权在处理中」为 2026-09-21 原始 HTTP 实测命中；只标注可重试，不做自动重试。
_GATEWAY_TRANSIENT_CODES = frozenset(("16110030",))
# 公共包络成功时用于识别业务数据形态的键；都不出现即视为未验收形态，不猜字段。
_RESULT_DATA_MARKERS = frozenset(("total", "skillFileList"))


class IdaasPersonalDynamicQueryError(RuntimeError):
    def __init__(self, message, *, code=None, http_status=None, retryable=False, contract=False):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        # 只有“响应壳缺失”这类后端抖动可安全重试；业务失败仍为不可重试。
        self.retryable = retryable
        # 上游响应不符合已冻结契约：由 workflows.common.guard_upstream 统一收敛为契约错误。
        self.contract = bool(contract)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = core_control_http.build_opener(
    _NoRedirectHandler,
    urllib.request.HTTPSHandler(context=ssl.create_default_context()),
)


def call(
    *,
    start_time,
    end_time,
    keyword,
    dynamic_type,
    page_size,
    next_page_cursor=None,
    content_type=None,
    dynamic_types=None,
    request_headers,
):
    body = {
        "startTime": _require_dynamic_time(start_time, "start_time"),
        "endTime": _require_dynamic_time(end_time, "end_time"),
        # 最新 V3 契约要求 keyword 必传；无筛选词时传空字符串表示不按名称过滤。
        "keyword": _require_keyword(keyword),
        "dynamicType": _require_dynamic_type(dynamic_type),
        "pageSize": _require_positive_int(page_size, "page_size"),
    }
    if next_page_cursor is not None:
        body["nextPageCursor"] = _require_text(next_page_cursor, "next_page_cursor")
    if content_type is not None:
        body["contentType"] = _require_content_type(content_type)
    if dynamic_types is not None:
        body["dynamicTypes"] = _require_dynamic_types(dynamic_types)
    payload = _post(body, request_headers)
    result_code = payload.get("resultCode")
    if result_code is None:
        # 鉴权/网关层拒绝时改用公共包络（success/code/message/data），没有 resultCode；
        # 这是可解释的上游失败，必须如实外发业务码，不能笼统说成"响应壳缺失"。
        return _accept_common_envelope(payload)
    # 最新契约标注为 String，但现网 V3 会返回数值 200；两种成功编码语义相同。
    # 兼容传输层类型差异，仍拒绝布尔和其它非成功值。
    if (
        isinstance(result_code, bool)
        or not isinstance(result_code, (str, int))
        or str(result_code) != "200"
    ):
        raise IdaasPersonalDynamicQueryError(
            _failure_message(result_code, payload.get("resultMsg")), code=result_code
        )
    result_msg = payload.get("resultMsg")
    if result_msg is not None and not isinstance(result_msg, str):
        _contract_violation("resultMsg 必须是字符串")
    return _validate_result_data(payload)


def _accept_common_envelope(payload):
    """处理没有 resultCode 的公共 JSON 包络（success/code/message/data）。

    2026-09-21 原始 HTTP 实测（绕过 CLI，40 并发打本接口）：33 次返回
    ``{"success": false, "code": "16110030", "message": "有其他授权在处理中", "data": null}``；
    同接口的业务成功、以及业务失败（参数非法返回 ``resultCode=500``）都带 resultCode。
    即：缺 resultCode 不是"响应壳损坏"，而是请求在鉴权/网关层被拒绝，用了另一套包络。
    这类响应是明确失败，按真实业务码与原因外发，不再报"缺少 resultCode 的瞬时故障"。
    """
    success = payload.get("success")
    code = payload.get("code")
    if success is None and code is None:
        # 两套包络都没有：无法判定结果，保留可重试语义，不冒充业务失败
        raise IdaasPersonalDynamicQueryError(
            "平台响应缺少 resultCode 与 success/code，无法判定结果", code=None, retryable=True
        )
    if success is True or str(code) == "0000":
        data = payload.get("data")
        # 只接受能识别的业务数据形态；形态未验收时报错，不猜字段、也不报成"无记录"
        if not isinstance(data, dict) or not (_RESULT_DATA_MARKERS & set(data)):
            raise IdaasPersonalDynamicQueryError(
                "平台公共包络的成功响应缺少可识别的动态数据，已停止解析", code=code
            )
        normalized = dict(payload)
        normalized["resultData"] = data
        return _validate_result_data(normalized)
    raise IdaasPersonalDynamicQueryError(
        _failure_message(code, payload.get("message")),
        code=code,
        retryable=str(code) in _GATEWAY_TRANSIENT_CODES,
    )


def _validate_result_data(payload):
    """校验结果容器；userId 由服务端覆盖，未知字段原样保留，业务数据在 resultData。"""
    result_data = payload.get("resultData")
    if result_data is not None and not isinstance(result_data, dict):
        _contract_violation("resultData 必须是对象")
    if isinstance(result_data, dict):
        skill_files = result_data.get("skillFileList")
        if skill_files is not None:
            if not isinstance(skill_files, list) or any(not isinstance(item, dict) for item in skill_files):
                _contract_violation("resultData.skillFileList 必须是对象数组")
            for index, item in enumerate(skill_files):
                _validate_skill_file_item(item, index)
        cursor = result_data.get("nextPageCursor")
        if cursor is not None and not isinstance(cursor, str):
            _contract_violation("resultData.nextPageCursor 必须是字符串")
    return payload


def _contract_violation(message):
    """动态接口响应异常只使本次查询失败，不影响后续查询。"""
    raise IdaasPersonalDynamicQueryError(message)


def _require_text(value, name):
    if not isinstance(value, str) or not value:
        raise IdaasPersonalDynamicQueryError("%s 必须是非空字符串" % name)
    return value


def _require_keyword(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise IdaasPersonalDynamicQueryError("keyword 必须是字符串")
    return value


def _require_dynamic_time(value, name):
    text = _require_text(value, name)
    try:
        datetime.strptime(text, _DYNAMIC_TIME_FORMAT)
    except ValueError:
        raise IdaasPersonalDynamicQueryError("%s 格式必须为 yyyy-MM-dd HH:mm:ss" % name)
    return text


def _require_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise IdaasPersonalDynamicQueryError("%s 必须是整数" % name)
    return value


def _require_positive_int(value, name):
    number = _require_int(value, name)
    if number <= 0:
        raise IdaasPersonalDynamicQueryError("%s 必须是正整数" % name)
    return number


def _require_dynamic_type(value):
    number = _require_int(value, "dynamic_type")
    if number not in _DYNAMIC_TYPES:
        raise IdaasPersonalDynamicQueryError("dynamic_type 不在文档枚举内")
    return number


def _require_content_type(value):
    number = _require_int(value, "content_type")
    if number not in _CONTENT_TYPES:
        raise IdaasPersonalDynamicQueryError("content_type 仅支持 1/2/3/4")
    return number


def _require_dynamic_types(value):
    """按接口文档约束校验 dynamicTypes。

    文档允许三种用法：批量个人云文件类型 1/2/4/5/6/7/8/11 的任意组合；
    其它类型只支持单个值；或最近在读 [1,10,13]（与顺序无关）。
    不附加“dynamicType 必须包含在 dynamicTypes 中”之类的文档外约束。
    """
    if not isinstance(value, list) or not value:
        raise IdaasPersonalDynamicQueryError("dynamic_types 必须是非空整数数组")
    normalized = [_require_dynamic_type(item) for item in value]
    if len(set(normalized)) != len(normalized):
        raise IdaasPersonalDynamicQueryError("dynamic_types 不得包含重复值")
    candidates = set(normalized)
    if candidates == _RECENT_READING_DYNAMIC_TYPES:
        return normalized
    if candidates.issubset(_BATCH_PERSONAL_DYNAMIC_TYPES):
        return normalized
    if len(normalized) == 1:
        return normalized
    raise IdaasPersonalDynamicQueryError(
        "dynamic_types 仅支持批量个人云类型 1/2/4/5/6/7/8/11 的任意组合、其它类型的单个值，或最近在读 1/10/13"
    )


def _validate_skill_file_item(item, index):
    """逐条校验动态文件；格式异常仅影响本次请求。"""
    _validate_skill_file_fields(item, index)


def _validate_skill_file_fields(item, index):
    prefix = "resultData.skillFileList[%d]" % index
    # 现网历史条目可能漏传 name。保留该条记录及其时间、类型和动态引用，
    # 让调用方如实标注“名称未返回”，而不是暂停整个上传/浏览查询能力。
    for key in ("contentId", "type", "dynamicTime"):
        value = item.get(key)
        if not isinstance(value, str) or not value:
            raise IdaasPersonalDynamicQueryError("%s.%s 必须是非空字符串" % (prefix, key))
    name = item.get("name")
    if name is not None and (not isinstance(name, str) or not name):
        raise IdaasPersonalDynamicQueryError("%s.name 必须是非空字符串或省略" % prefix)
    if item["type"] not in _FILE_TYPES:
        raise IdaasPersonalDynamicQueryError("%s.type 仅支持 0/1" % prefix)
    _require_dynamic_time(item["dynamicTime"], "%s.dynamicTime" % prefix)
    _require_dynamic_type(item.get("dynamicType"))
    for key in ("parentFileId", "category", "batchNewId"):
        value = item.get(key)
        if value is not None and not isinstance(value, str):
            raise IdaasPersonalDynamicQueryError("%s.%s 必须是字符串" % (prefix, key))
    category = item.get("category")
    if category is not None and category not in _CATEGORIES:
        raise IdaasPersonalDynamicQueryError(
            "%s.category 不在 0/1/2/3/4/5/6/100 中" % prefix
        )
    reading_record = item.get("readingRecord")
    if reading_record is not None:
        if not isinstance(reading_record, Mapping):
            raise IdaasPersonalDynamicQueryError("%s.readingRecord 必须是对象" % prefix)
        for key in ("chapterId", "chapterOffset", "progress", "thirdContentId", "cId"):
            value = reading_record.get(key)
            if value is not None and not isinstance(value, str):
                raise IdaasPersonalDynamicQueryError(
                    "%s.readingRecord.%s 必须是字符串" % (prefix, key)
                )
        progress = reading_record.get("progress")
        if progress is not None:
            try:
                progress_value = int(progress)
            except ValueError:
                raise IdaasPersonalDynamicQueryError(
                    "%s.readingRecord.progress 必须是 0~10000 的整数字符串" % prefix
                )
            if progress_value < 0 or progress_value > 10000:
                raise IdaasPersonalDynamicQueryError(
                    "%s.readingRecord.progress 必须在 0~10000 范围内" % prefix
                )


def _require_headers(value):
    if not isinstance(value, Mapping):
        raise IdaasPersonalDynamicQueryError("request_headers 必须是映射")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise IdaasPersonalDynamicQueryError("request_headers 的键必须是非空字符串")
        if item is None:
            continue
        if not isinstance(item, str):
            raise IdaasPersonalDynamicQueryError("request_headers 的值必须是字符串")
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
        raise IdaasPersonalDynamicQueryError("请求地址校验失败")
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
        raise IdaasPersonalDynamicQueryError(
            "HTTP 状态码 %s: %s" % (err.code, str(fields[1]) if fields[1] is not None else "无平台消息"),
            code=fields[0],
            http_status=err.code,
        )
    except OSError as err:
        raise IdaasPersonalDynamicQueryError("网络请求失败: %s" % (getattr(err, "reason", None) or err))
    if not isinstance(status, int) or not 200 <= status < 300:
        raise IdaasPersonalDynamicQueryError(
            "HTTP 状态码不可用于成功判定", http_status=status if isinstance(status, int) else None
        )
    if content_type is not None and "json" not in content_type.lower():
        raise IdaasPersonalDynamicQueryError("响应媒体类型不是 JSON: %s" % content_type)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise IdaasPersonalDynamicQueryError("平台响应不是合法 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise IdaasPersonalDynamicQueryError("平台响应不是 JSON 对象")
    return upstream_trace.attach(payload, trace_id)
