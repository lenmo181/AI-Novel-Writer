"""workflows 公共机械能力。

只放跨场景复用的机械逻辑：参数转换、受控引用解析、分页、范围核验、结果归一化、轮询。
禁止在 common.py 中生成任何新的业务动作，禁止在其中拼接 HTTP/XML/multipart。
"""

import hashlib
import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from cmcloud_core import result as result_module

POLL_INTERVAL_SECONDS = 5
POLL_MAX_SECONDS = 300
POLL_MAX_CALLS = 60

# 下游任务类型：写入台账 taskType，供 S22 operation.status 选择状态查询接口。
TASK_TYPE_OUTER_LINK_V2 = "outer-link-v2"
TASK_TYPE_PHOTO_ORGANIZE = "photo-organize"
TASK_TYPE_FILE_COPY = "file-copy"

# 个人云任务（task_get）状态枚举：官方文档与现网样本（2026-09-18）一致，
# Running 为非终态，其余三个为终态。
FILE_TASK_TERMINAL_STATUSES = frozenset(("Succeed", "PartialSucceed", "Failed"))

UNREGISTERED_REF_MESSAGE = "引用未登记或已被清理"
MISSING_FILE_MESSAGE = "该文件不存在"

DYNAMIC_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# 动态查询的 startTime/endTime 与响应 dynamicTime 都是北京时间墙钟（2026-09-19 实测），
# 窗口换算必须固定到该时区；直接用宿主或 UTC 墙钟会让窗口整体偏移 8 小时。
DYNAMIC_TIME_ZONE = timezone(timedelta(hours=8))

# IDaaS V3 搜索类 timeList 的 startTime/endTime 契约格式（如 20260401000000），
# 与动态查询的 DYNAMIC_TIME_FORMAT 不同，不能互相替代。
IDAAS_TIME_FORMAT = "%Y%m%d%H%M%S"

MAX_ANCESTOR_DEPTH = 32

# 动态类型：仅为接口枚举，场景语义见各 workflow
DYNAMIC_TYPE_COPY = 5
DYNAMIC_TYPE_GROUP = 6
DYNAMIC_TYPE_DISCOVER = 7
DYNAMIC_TYPE_PLAY = 9
DYNAMIC_TYPE_UPLOAD_NORMAL = 2
DYNAMIC_TYPE_UPLOAD_BEAUTY = 4

# 相簿图片列表接口的服务端要求排序字段必填（接口文档标注为可选），缺任一个返回 01000001 参数错误；
# 故事图片列表接口不接受这两个字段，取值不具备跨接口通用性。
ALBUM_PHOTO_ORDER_BY = 1
ALBUM_PHOTO_ORDER_DIRECTION = "DESC"

# 相簿/故事列表族服务端最小页为 10：pageSize 1/3/5/9 均返回 01000001 参数错误（2026-09-18 实测），
# 与文件列表一致统一收敛到 [10, 100]。
MIN_PAGE_SIZE = 10
MAX_PAGE_SIZE = 100


def clamp_page_size(requested):
    return max(MIN_PAGE_SIZE, min(MAX_PAGE_SIZE, int(requested)))


def batch_resource_failure_reason(error):
    """把批量资源的内部引用错误转换为稳定的用户提示。"""
    if (
        error.code == result_module.INVALID_ARGUMENT
        and error.message == UNREGISTERED_REF_MESSAGE
    ):
        return MISSING_FILE_MESSAGE
    return error.message


def preloaded_ref_error(context, field_path, item_index):
    """读取 executor 按字段路径与项索引登记的批量引用错误。"""
    lookup = getattr(context, "ref_error", None)
    if not callable(lookup):
        return None
    path = (field_path,) if isinstance(field_path, str) else tuple(field_path)
    error = lookup(path, item_index)
    return error if isinstance(error, result_module.ExecutionError) else None


ROOT_SENTINELS = ("", "0", "-1", "root", "/")

_SHARE_LINK_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_SHARE_ROUTE_MARKERS = (("w", "i"), ("g", "i"), ("s",))


def parse_share_link_url(link_url):
    """校验中国移动云盘分享 URL，并从已知网页路由提取 linkId。"""
    try:
        parsed = urllib.parse.urlsplit(link_url)
        port = parsed.port
    except (TypeError, ValueError):
        raise _invalid_share_link_url()
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() != "https"
        or not host
        or (host != "yun.139.com" and not host.endswith(".yun.139.com"))
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise _invalid_share_link_url()
    for route in (parsed.fragment, parsed.path):
        segments = [urllib.parse.unquote(item) for item in route.split("/") if item]
        for marker in _SHARE_ROUTE_MARKERS:
            size = len(marker)
            for index in range(0, len(segments) - size):
                if tuple(segments[index:index + size]) != marker:
                    continue
                candidate = segments[index + size]
                if index + size == len(segments) - 1 and _SHARE_LINK_ID_PATTERN.fullmatch(candidate):
                    return candidate
    raise _invalid_share_link_url()


def _invalid_share_link_url():
    return result_module.ExecutionError(
        result_module.INVALID_ARGUMENT,
        "linkUrl 必须是中国移动云盘 HTTPS 分享链接，且包含受支持的分享路由",
    )


def real_id_for_kinds(context, token, allowed_kinds, field_name="引用"):
    """解析由上游 CLI 签发的多态引用，并在工作流层收敛允许类型。"""
    entry = context.ref_entry(token)
    if entry.get("kind") not in tuple(allowed_kinds):
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "%s 类型不匹配，允许类型为 %s" % (field_name, "/".join(allowed_kinds)),
        )
    return entry["realId"]


class Outcome:
    """workflow 归一化结果；executor 负责转成统一信封。"""

    __slots__ = ("status", "data", "counts", "page", "error", "next_action")

    def __init__(self, status, data=None, counts=None, page=None, error=None, next_action=None):
        self.status = status
        self.data = data
        self.counts = counts
        self.page = page
        self.error = error
        self.next_action = next_action

    def envelope_kwargs(self):
        return {
            "status": self.status,
            "data": self.data,
            "counts": self.counts,
            "page": self.page,
            "error": self.error,
            "next_action": self.next_action,
        }


def success(data=None, counts=None, page=None):
    return Outcome(result_module.STATUS_SUCCESS, data=data, counts=counts, page=page)


def empty(data=None, page=None):
    return Outcome(result_module.STATUS_EMPTY, data=data, page=page)


def partial(data=None, counts=None, page=None, error=None):
    if error is None:
        error = {
            "code": result_module.FAILED,
            "message": "部分步骤失败，已完成结果已保留",
            "retryable": False,
        }
    return Outcome(result_module.STATUS_PARTIAL, data=data, counts=counts, page=page, error=error)


def running(data=None, counts=None, page=None, message="任务仍在执行，需继续查询", next_action=None):
    """本轮等待已达上限（非失败）：写链可带 nextAction=operation.status 续查一次。"""
    return Outcome(
        result_module.STATUS_RUNNING,
        data=data,
        counts=counts,
        page=page,
        error={"code": result_module.UPSTREAM_TIMEOUT, "message": message, "retryable": True},
        next_action=next_action,
    )


def unknown(data=None, counts=None, message="下游任务状态未知，本次命令不再自动续查"):
    """V1.8：状态未知时顶层 status=UNKNOWN，nextAction 保持 null。"""
    return Outcome(
        result_module.STATUS_UNKNOWN,
        data=data,
        counts=counts,
        error={"code": result_module.UNKNOWN, "message": message, "retryable": False},
    )


def page_info(page_size, returned, total=None, has_more=None, next_cursor=None, complete=False):
    """未知总数保持 None，不得写成 0；未知是否还有更多保持 None。"""
    return {
        "pageSize": page_size,
        "returned": returned,
        "total": total,
        "hasMore": has_more,
        "nextCursor": next_cursor,
        "complete": bool(complete),
    }


def page_exhausted(page_size, returned, has_cursor):
    """搜索平台分页终止判定：本页不满或上游无游标即已取尽。

    returned 必须是**上游本次返回的原始条数**（本地过滤前的行数）；本地过滤
    会让条数变少，用它判定会把中间页误当末页。
    """
    return (returned < page_size) or (not has_cursor)


def require(condition, code, message, retryable=False):
    if not condition:
        raise result_module.ExecutionError(code, message, retryable=retryable)


def contract_mismatch(context, message):
    """adapter 返回不符合已冻结契约：抛出统一错误码。

    只影响本次请求，不做能力级停用（历史实现会写 1 小时熔断，已移除）。
    ``context`` 保留仅为调用方签名稳定。
    """
    raise result_module.ExecutionError(result_module.UPSTREAM_CONTRACT_MISMATCH, message)


def upstream_failure(message, retryable=False):
    raise result_module.ExecutionError(result_module.FAILED, message, retryable=retryable)


def guard_upstream(context, action, **kwargs):
    """统一调用 adapter 并把 adapter 异常转为受控错误码，不泄露下游原始响应。"""
    try:
        return context.call(action, **kwargs)
    except result_module.ExecutionError:
        raise
    except Exception as err:  # adapter 自带异常类型，统一收敛
        if bool(getattr(err, "contract", False)):
            # adapter 已判定响应不符合契约（必填字段缺失/类型非法等）：抛出契约错误
            contract_mismatch(context, _safe_message(err))
        if _transport_status(err) == "401":
            raise result_module.ExecutionError(
                result_module.AUTH_EXPIRED,
                "云盘业务票据已失效，自动刷新后仍未通过鉴权",
                retryable=False,
                data=_structured_error_data(err),
            )
        if _transport_status(err) == "403" or str(getattr(err, "code", "")) == "13000006":
            raise result_module.ExecutionError(
                result_module.SCOPE_DENIED,
                "平台拒绝当前资源操作，请核对资源权限",
                retryable=False,
                data=_structured_error_data(err),
            )
        network = _network_error(err)
        if network is not None and network.outcome_unknown:
            raise result_module.ExecutionError(
                result_module.UNKNOWN,
                "请求可能已送达，执行结果未知；请先核查原操作，不要直接重发",
                retryable=False, data=_structured_error_data(err),
            ) from err
        message = _safe_message(err)
        guidance = _operable_path_guidance(_operable_path(err))
        if guidance:
            message = "%s；%s" % (message, guidance)
        raise result_module.ExecutionError(
            result_module.FAILED,
            "下游接口调用失败: %s" % message,
            retryable=_looks_retryable(err),
            data=_structured_error_data(err),
        )


def _safe_message(err):
    text = str(err).strip() or err.__class__.__name__
    return text if len(text) <= 300 else text[:300] + "..."


def _looks_retryable(err):
    network = _network_error(err)
    if network is not None:
        return network.retryable
    # adapter 可显式声明瞬时故障（如动态查询的响应壳缺失）；其余按异常类型名推断。
    if bool(getattr(err, "retryable", False)):
        return True
    name = err.__class__.__name__
    return "Timeout" in name or "Network" in name


def _network_error(err):
    """沿异常因果链找传输状态，避免适配器包装后退化为文案匹配。"""
    pending, seen = [err], set()
    while pending and len(seen) < 32:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        if getattr(current, "cmcloud_network_error", False):
            return current
        pending.extend(getattr(current, name, None)
                       for name in ("__context__", "reason", "__cause__"))
    return None


def _transport_status(err):
    """只取传输层 HTTP 状态；业务包络码不参与鉴权判定。

    搜索类接口把参数缺失也报成包络 code=401（如 "keywords is null"，2026-09-19 实测），
    传票据时下游返回的是 110044/16110014 一类的业务码，因此 401 只有来自 HTTP 状态
    才说明票据问题；否则会把参数错误伪装成"票据失效"并要求用户重新授权。
    """
    http_status = getattr(err, "http_status", None)
    if http_status is None or isinstance(http_status, bool):
        return ""
    return str(http_status).strip()


_OPERABLE_PATH_FIELDS = ("rootRef", "namePath")


def _structured_error_data(err):
    """只保留可公开的结构化状态，不透传下游响应体或敏感请求信息。"""
    details = {}
    network = _network_error(err)
    if network is not None:
        details.update(sendEvidence=network.evidence, outcomeUnknown=network.outcome_unknown,
                       attempts=[dict(item) for item in network.attempts])
    http_status = getattr(err, "http_status", None)
    if isinstance(http_status, int) and not isinstance(http_status, bool):
        details["httpStatus"] = http_status
    code = getattr(err, "code", None)
    if isinstance(code, (str, int)) and not isinstance(code, bool):
        text = str(code).strip()
        if text:
            details["upstreamCode"] = text[:64]
    data = {"upstream": details} if details else {}
    operable = _operable_path(err)
    if operable:
        data["dedicatedRoot"] = operable
    return data or None


def _project_operable_path(operable):
    """只外发不透明引用和展示路径，不输出真实目录 ID 或 ID 路径。"""
    if not isinstance(operable, dict):
        return None
    entry = {}
    for key in _OPERABLE_PATH_FIELDS:
        value = operable.get(key)
        if isinstance(value, str) and value.strip():
            entry[key] = value.strip()
    return entry or None


def _operable_path(err):
    return _project_operable_path(getattr(err, "_cmcloudOperablePath", None))


def _payload_operable_path(payload):
    source = payload.get("_cmcloudOperablePath") if isinstance(payload, dict) else None
    return _project_operable_path(source)


def _operable_path_guidance(operable):
    name_path = (operable or {}).get("namePath")
    if not isinstance(name_path, str) or not name_path.strip():
        return ""
    return (
        "当前可操作的专属目录为「%s」；请与用户确认后重新发起操作，"
        "确认后本次操作将在该目录内完成" % name_path.strip()
    )


def payload_data(context, payload, required=True):
    """从平台响应取业务 data；缺 data 时先区分「业务失败」与「契约不符」。

    平台只在**成功**响应里带 data；失败响应形如
    ``{"success": false, "code": "50010002", "message": "枚举值有误"}``，本来就没有 data。
    把这种可解释的下游失败判成「接口结构变了」会给用户错误归因，
    因此缺 data 时先识别失败标记：命中即按 FAILED 返回可读原因；
    只有「明确成功却缺 data」或「没有任何成功/失败标记」才按契约不符处理。
    """
    if not isinstance(payload, dict):
        contract_mismatch(context, "平台响应不是对象")
    data = payload.get("data")
    if not isinstance(data, dict):
        failure = _business_failure(payload)
        if failure is not None:
            if str(failure[0]) == "13000006":
                raise result_module.ExecutionError(
                    result_module.SCOPE_DENIED,
                    "平台拒绝当前资源操作，请核对资源权限",
                    retryable=False,
                    data={"upstream": {"upstreamCode": "13000006"}},
                )
            message = _failure_text(*failure)
            operable = _payload_operable_path(payload)
            guidance = _operable_path_guidance(operable)
            if guidance:
                message = "%s；%s" % (message, guidance)
            raise result_module.ExecutionError(
                result_module.FAILED,
                message,
                data={"dedicatedRoot": operable} if operable else None,
            )
        if data is None and not required:
            return None
        contract_mismatch(context, "平台响应缺少业务 data 对象")
    return data


def _business_failure(payload):
    """识别下游失败响应，返回 (code, message)；成功响应或无成功/失败标记时返回 None。

    只在缺 data 时调用：成功响应带 data，不会进入本判断。
    """
    success = payload.get("success")
    code = payload.get("code")
    result_code = payload.get("resultCode")
    if success is not None:
        if success is True or str(success).strip().lower() in ("true", "1"):
            # 明确成功却缺 data：属真正的契约问题，交由上层按契约不符处理
            return None
        return code, payload.get("message")
    # 部分链路（如个人云 XML 域）以 resultCode="0" 表示成功，失败原因在 desc
    if code is not None and str(code).strip().lower() not in ("0000", "0"):
        return code, payload.get("message")
    if result_code is not None and str(result_code).strip() not in ("", "0"):
        return result_code, payload.get("desc") or payload.get("message")
    return None


def _failure_text(code, message):
    """统一的失败说明口径；code 缺失时用 <missing> 占位，不猜值。"""
    text = "下游接口返回失败: code=%s" % (code if code is not None else "<missing>")
    if isinstance(message, str) and message:
        text += " message=%s" % message
    return text


def json_success_data(context, payload, required=True):
    """校验相簿域统一 JSON 包络，再返回业务 data。"""
    if not isinstance(payload, dict):
        contract_mismatch(context, "平台响应不是对象")
    success = payload.get("success")
    code = payload.get("code")
    if success is not True or str(code) != "0000":
        raise result_module.ExecutionError(
            result_module.FAILED, _failure_text(code, payload.get("message"))
        )
    return payload_data(context, payload, required=required)


def field_text(context, mapping, key, message=None):
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        contract_mismatch(context, message or "响应字段 %s 缺失或类型非法" % key)
    return value


def field_int(context, mapping, key, message=None):
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        contract_mismatch(context, message or "响应字段 %s 缺失或类型非法" % key)
    return value


def optional_field_text(mapping, key):
    value = mapping.get(key)
    return value if isinstance(value, str) and value else None


def list_field(context, mapping, key, message=None):
    value = mapping.get(key)
    if not isinstance(value, list):
        contract_mismatch(context, message or "响应字段 %s 必须为数组" % key)
    return value


def dedupe(values):
    seen = set()
    ordered = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def dedupe_by(items, key_func):
    seen = set()
    ordered = []
    for item in items:
        key = key_func(item)
        if key is None or key in seen:
            continue
        seen.add(key)
        ordered.append(item)
    return ordered


def time_range_text(from_value, to_value):
    """RFC 3339 入参转下游 yyyy-MM-dd HH:mm:ss；缺省端由调用方按固定规则补齐。"""
    return (_to_dynamic_time(from_value), _to_dynamic_time(to_value))


def idaas_time_text(value):
    """RFC 3339 入参转 IDaaS V3 timeList 的 yyyyMMddHHmmss；保留入参时区偏移下的墙上时间。"""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    return parsed.strftime(IDAAS_TIME_FORMAT)


def _to_dynamic_time(value):
    """RFC 3339 入参按动态查询时区（北京时间）渲染成墙钟；无偏移入参视为已是该时区墙钟。"""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=DYNAMIC_TIME_ZONE)
    return parsed.astimezone(DYNAMIC_TIME_ZONE).strftime(DYNAMIC_TIME_FORMAT)


def default_time_window(context, days=7):
    now = context.now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=DYNAMIC_TIME_ZONE)
    else:
        now = now.astimezone(DYNAMIC_TIME_ZONE)
    return (
        (now - timedelta(days=days)).strftime(DYNAMIC_TIME_FORMAT),
        now.strftime(DYNAMIC_TIME_FORMAT),
    )


def utcnow():
    return datetime.now(timezone.utc)


def conditions_digest(conditions):
    """查询条件摘要：绑定游标，条件改变即拒绝复用。"""
    canonical = json.dumps(conditions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def dedicated_boundary_id(context):
    """当前可操作边界（专属根）真实 ID；解析失败时返回 None，按个人云全链还原。"""
    try:
        return context.dedicated_root_id()
    except result_module.ExecutionError:
        return None


def personal_cloud_path(context, file_id, *, node_hint=None):
    """用服务端文件详情逐级还原当前用户个人云中的路径（目标节点在前）。

    专属根是当前授权的可操作边界：祖先链走到专属根即停，不再对其父级逐级
    file/get——授权收窄后父级可能已不在授权范围内，继续走会报 16110013，
    令专属目录内的路径还原整体不可用。专属根之上的路径段由
    personal_path_segments 用 namePath 补齐（真实 ID/ID 路径不进 CLI 输出）。
    """
    from cmcloud_adapters import file_get

    if file_id == "/":
        return []
    boundary = dedicated_boundary_id(context)
    nodes = []
    visited = set()
    current = file_id
    node = node_hint
    for _ in range(MAX_ANCESTOR_DEPTH):
        if current in visited:
            contract_mismatch(context, "文件父链存在循环")
        visited.add(current)
        if node is None:
            node = payload_data(context, guard_upstream(context, file_get.call, file_id=current))
        if field_text(context, node, "fileId") != current:
            contract_mismatch(context, "文件父链 ID 与查询目标不一致")
        nodes.append(node)
        if boundary is not None and current == boundary:
            return nodes
        parent_id = node.get("parentFileId")
        if not isinstance(parent_id, str):
            contract_mismatch(context, "响应缺少 parentFileId")
        if parent_id.strip() in ROOT_SENTINELS:
            return nodes
        current, node = parent_id, None
    contract_mismatch(context, "文件父链超过最大深度")


def personal_path_segments(context, file_id, *, node_hint=None):
    """还原目标的逐级名称路径；祖先链在专属根截断时，其上由 namePath 补齐。"""
    ancestors = personal_cloud_path(context, file_id, node_hint=node_hint)
    names = []
    for node in ancestors:
        name = node.get("name") if isinstance(node, dict) else None
        if isinstance(name, str) and name:
            names.append(name)
    names.reverse()
    boundary = dedicated_boundary_id(context)
    if boundary and ancestors and isinstance(ancestors[-1], dict) and ancestors[-1].get("fileId") == boundary:
        prefix = _dedicated_root_prefix(context)
        if prefix:
            names = prefix + names
    return names


def _dedicated_root_prefix(context):
    """专属根之上的路径段（来自用户目录接口 namePath）；不可得时返回空，展示字段尽力而为。"""
    try:
        name_path = context.dedicated_root_name()
    except Exception:
        return []
    if not isinstance(name_path, str):
        return []
    segments = [segment for segment in name_path.split("/") if segment]
    return segments[:-1] if segments else []


def ensure_directory(context, file_id):
    from cmcloud_adapters import file_get

    if file_id == "/":
        # /file/list 与文件写接口以此值表示当前授权用户的个人云根。
        return {"fileId": "/", "parentFileId": "/", "name": "/", "type": "folder"}
    payload = guard_upstream(context, file_get.call, file_id=file_id)
    node = payload_data(context, payload)
    if node.get("type") != "folder":
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "目标引用不是目录")
    return node


def resolve_dedicated_write_destination(context, destination_ref=None):
    """解析云端写落点；省略时使用专属根，并拒绝专属目录范围外的目标。"""
    dedicated = context.dedicated_root_public()
    root_id = context.dedicated_root_id()
    root_ref = dedicated.get("rootRef") if isinstance(dedicated, dict) else None
    root_path = dedicated.get("namePath") if isinstance(dedicated, dict) else None
    if not isinstance(root_ref, str) or not root_ref:
        contract_mismatch(context, "专属目录响应缺少 rootRef")
    if not isinstance(root_path, str) or not root_path:
        contract_mismatch(context, "专属目录响应缺少 namePath")
    if not isinstance(root_id, str) or not root_id:
        contract_mismatch(context, "专属目录响应缺少 folderId")

    if destination_ref is None:
        return root_id, {
            "folderRef": root_ref,
            "path": root_path,
            "selection": "default",
        }

    destination_id = context.real_id(destination_ref, "folder")
    node = ensure_directory(context, destination_id)
    ancestors = personal_cloud_path(context, destination_id, node_hint=node)
    root_position = next(
        (
            position
            for position, item in enumerate(ancestors)
            if isinstance(item, dict) and item.get("fileId") == root_id
        ),
        None,
    )
    if root_position is None:
        raise result_module.ExecutionError(
            result_module.SCOPE_DENIED,
            "转存目标不在智能体专属目录范围内",
            data={"dedicatedRoot": dedicated},
        )
    names = []
    for item in reversed(ancestors[:root_position]):
        name = item.get("name") if isinstance(item, dict) else None
        if isinstance(name, str) and name.strip() and name.strip() != "/":
            names.append(name.strip())
    path = root_path.rstrip("/")
    if names:
        path += "/" + "/".join(names)
    return destination_id, {
        "folderRef": destination_ref,
        "path": path,
        "selection": "user",
    }


def ensure_file(context, file_id):
    from cmcloud_adapters import file_get

    payload = guard_upstream(context, file_get.call, file_id=file_id)
    node = payload_data(context, payload)
    if node.get("type") != "file":
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "目标引用不是文件")
    return node


def ensure_source(context, file_id):
    """核验已授权个人云源资源真实存在；不要求其位于专属目录内。"""
    from cmcloud_adapters import file_get

    payload = guard_upstream(context, file_get.call, file_id=file_id)
    node = payload_data(context, payload)
    field_text(context, node, "fileId")
    if node.get("type") not in ("file", "folder"):
        contract_mismatch(context, "源资源 type 取值非法")
    return node


def resolve_refs(context, refs, expected_kind):
    resolved = []
    for token in refs:
        resolved.append(context.resolve_ref(token, expected_kind))
    return resolved


def resolve_optional_ref(context, token, expected_kind):
    if token is None:
        return None
    return context.resolve_ref(token, expected_kind)


def skill_file_list(context, payload, key="skillFileList", container="data"):
    """关键词搜索/动态查询共用：取出已确认的列表字段。"""
    if not isinstance(payload, dict):
        contract_mismatch(context, "平台响应不是对象")
    holder = payload.get(container)
    if not isinstance(holder, dict):
        contract_mismatch(context, "平台响应缺少 %s 对象" % container)
    items = holder.get(key)
    if not isinstance(items, list):
        contract_mismatch(context, "平台响应缺少 %s 列表" % key)
    return items


def items_of(data, key="items"):
    value = data.get(key)
    return value if isinstance(value, list) else None


def poll_task(context, query_action, *, is_terminal, state_of, extra_kwargs=None, max_calls=None):
    """固定 5 秒/单次最多 300 秒/最多 60 次轮询；终态立即停止，绝不重交写请求。

    max_calls 仅供“状态枚举尚未冻结”的场景收敛为单次查询：此时只查原任务，
    不判定终态，返回 RUNNING 交给上层继续 operation.status。
    """
    kwargs = dict(extra_kwargs or {})
    limit = POLL_MAX_CALLS if max_calls is None else max(1, int(max_calls))
    deadline = time.monotonic() + POLL_MAX_SECONDS
    last_state = None
    for attempt in range(limit):
        payload = guard_upstream(context, query_action, **kwargs)
        state = state_of(context, payload)
        last_state = state
        if is_terminal(state):
            state["timedOut"] = False
            state["attempts"] = attempt + 1
            return state
        if attempt == limit - 1 or time.monotonic() >= deadline:
            break
        remaining = deadline - time.monotonic()
        context.sleep(min(POLL_INTERVAL_SECONDS, max(remaining, 0.0)))
    if last_state is None:
        last_state = {}
    last_state["timedOut"] = True
    last_state["attempts"] = limit
    return last_state


def submitted_counts(resolved_ids):
    """按冻结计划的 resolvedIds 推导已提交条目数；未知保持 null，不写 0。"""
    if not isinstance(resolved_ids, dict):
        return None
    total = None
    for key in ("fileIds", "contentIds", "catalogIds"):
        value = resolved_ids.get(key)
        if isinstance(value, list):
            total = (total or 0) + len(value)
    return {"submitted": total} if total else None


def requested_counts(resolved_ids):
    """按冻结计划给出标准批量结果计数；终态前成功/失败保持未知。"""
    submitted = submitted_counts(resolved_ids)
    requested = submitted.get("submitted") if isinstance(submitted, dict) else None
    return {"requested": requested, "succeeded": None, "failed": None}


def outer_link_task_outcome(
    context, query_action, task_id, resolved_ids, *, destination=None,
    is_terminal=None, max_calls=None, timeout_next_action=None,
):
    """查询已提交的转存任务；查询异常不能作为转存失败证据。"""
    try:
        state = poll_task(
            context, query_action,
            is_terminal=is_terminal or (lambda value: value.get("taskStatus") == 2),
            state_of=lambda current_context, payload: outer_link_batch_state(
                current_context, payload, resolved_ids
            ),
            extra_kwargs={"task_id": task_id}, max_calls=max_calls,
        )
    except Exception as err:
        # 只包围查询及响应解析，明确的逐项业务失败在下方判定，不被吞掉。
        data = {
            "taskRef": task_id, "submitted": True,
            "queryError": {"code": result_module.normalize_error_code(getattr(err, "code", None))},
        }
        operation_ref = getattr(context, "operation_ref", None)
        if isinstance(operation_ref, str) and operation_ref:
            data["operationRef"] = operation_ref
        if isinstance(destination, dict):
            data["destination"] = dict(destination)
        return Outcome(
            result_module.STATUS_UNKNOWN, data=data, counts=requested_counts(resolved_ids),
            error=result_module.build_error(
                result_module.TASK_STATUS_QUERY_FAILED,
                "转存任务已提交，但状态查询失败，转存结果尚未确认；可稍后查询原任务，不要重新提交转存",
                retryable=True,
            ),
        )
    data = outer_link_batch_data(
        task_id, state, include_results=not state.get("timedOut"), destination=destination
    )
    if state.get("timedOut"):
        return running(
            data=data, counts=requested_counts(resolved_ids), next_action=timeout_next_action
        )
    return outer_link_batch_outcome(data, state)


def outer_link_batch_state(context, payload, resolved_ids=None):
    """按 queryBatchOprTaskDetail 契约解析外链批量任务逐项结果。

    契约规定 reason 为失败原因、成功时为空。保留现网成功码 0/0000 兼容
    （实网成功转存仍返回 reason=0000）。其他非空 reason 优先判失败，
    即使同时返回 rstId；落点引用仍保留，但不据此宣称没有副作用或内部文件全失败。
    reason 为空且 rstId 非空时判成功。
    两者都为空的条目以及终态中缺失的请求条目保持 UNKNOWN，不能当作成功或失败。
    """
    if not isinstance(payload, dict):
        contract_mismatch(context, "任务详情响应不是结构化对象")
    confirmed_status = payload.get("status")
    if confirmed_status not in ("CONFIRMED_SUCCESS", "CONFIRMED_FAILURE", "UNKNOWN"):
        contract_mismatch(context, "任务详情响应缺少已确认的状态标记")
    task = payload.get("batchOprTask")
    if not isinstance(task, dict):
        contract_mismatch(context, "任务详情响应缺少 batchOprTask")

    resolved_ids = resolved_ids if isinstance(resolved_ids, dict) else {}
    link_id = resolved_ids.get("linkId")
    submitted = submitted_counts(resolved_ids)
    requested = submitted.get("submitted") if isinstance(submitted, dict) else None
    results = []
    succeeded = 0
    failed = 0
    unknown_count = 0
    for list_name, source_kind, result_kind in (
        ("contentList", "content", "file"),
        ("catalogList", "catalog", "folder"),
    ):
        rows = payload.get(list_name)
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            contract_mismatch(context, "任务详情响应的 %s 不是列表" % list_name)
        for position, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                contract_mismatch(context, "任务详情响应的 %s 条目不是对象" % list_name)
            source_id = _optional_result_text(context, row.get("srcId"), "srcId")
            if source_id is None:
                contract_mismatch(context, "任务详情逐项结果缺少 srcId")
            result_id = _optional_result_text(context, row.get("rstId"), "rstId")
            reason = _optional_result_text(context, row.get("reason"), "reason")
            item = {"kind": source_kind, "position": position}
            if isinstance(link_id, str) and link_id:
                item["sourceRef"] = context.issue_ref(
                    source_kind,
                    source_id,
                    SCOPE_OUTER_LINK,
                    parent_real_id=link_id,
                )
            if reason in ("0", "0000"):
                item["status"] = result_module.STATUS_SUCCESS
                if result_id is not None:
                    item["resultRef"] = context.issue_ref(
                        result_kind, result_id, SCOPE_AUTHORIZED_PERSONAL
                    )
                succeeded += 1
            elif reason is not None:
                item["reason"] = reason
                item["status"] = result_module.STATUS_FAILED
                failed += 1
                if result_id is not None:
                    # 返回了目标 ID 不覆盖失败原因；仅保留已产生落点的证据。
                    item["resultRef"] = context.issue_ref(
                        result_kind, result_id, SCOPE_AUTHORIZED_PERSONAL
                    )
            elif result_id is not None:
                item["status"] = result_module.STATUS_SUCCESS
                item["resultRef"] = context.issue_ref(
                    result_kind, result_id, SCOPE_AUTHORIZED_PERSONAL
                )
                succeeded += 1
            else:
                item["status"] = result_module.STATUS_UNKNOWN
                unknown_count += 1
            results.append(item)

    if requested is not None and len(results) < requested:
        unknown_count += requested - len(results)
    return {
        "confirmedStatus": confirmed_status,
        "taskStatus": task.get("taskStatus"),
        "resultCode": task.get("resultCode"),
        "taskResultCode": task.get("taskResultCode"),
        "progress": task.get("progress"),
        "results": results,
        "unknownCount": unknown_count,
        "counts": {
            "requested": requested,
            "succeeded": succeeded,
            "failed": failed,
        },
    }


def outer_link_batch_data(task_ref, state, *, include_results=True, destination=None):
    data = {"taskRef": task_ref, "taskStatus": state.get("taskStatus")}
    for key in ("resultCode", "taskResultCode", "progress"):
        if state.get(key) is not None:
            data[key] = state.get(key)
    if include_results:
        data["results"] = list(state.get("results") or ())
        data["unknownCount"] = state.get("unknownCount", 0)
        data["savedDirectories"] = [
            {"folderRef": item["resultRef"]}
            for item in data["results"]
            if item.get("kind") == "catalog"
            and item.get("status") == result_module.STATUS_SUCCESS
            and isinstance(item.get("resultRef"), str)
        ]
    if isinstance(destination, dict):
        data["destination"] = dict(destination)
    return data


def outer_link_batch_outcome(data, state):
    """把已到终态的逐项结果折算为统一信封状态。"""
    counts = state.get("counts") if isinstance(state.get("counts"), dict) else None
    if state.get("confirmedStatus") == "CONFIRMED_FAILURE":
        raise result_module.ExecutionError(
            result_module.FAILED,
            "转存任务执行失败；可能已生成目标目录或部分文件，不能据此认定没有落地内容",
            data=data, counts=counts,
        )
    succeeded = counts.get("succeeded") if counts else 0
    failed = counts.get("failed") if counts else 0
    unknown_count = state.get("unknownCount")
    if not isinstance(unknown_count, int) or unknown_count < 0:
        unknown_count = 0
    if unknown_count:
        return unknown(
            data=data,
            counts=counts,
            message="转存任务已结束，但部分逐项结果无法判定",
        )
    if failed and succeeded:
        return partial(data=data, counts=counts)
    if failed:
        raise result_module.ExecutionError(
            result_module.FAILED,
            "转存任务返回的请求项均报告失败；可能已生成目标目录或部分文件，内部完成数量未知",
            data=data, counts=counts,
        )
    if succeeded:
        return success(data=data, counts=counts)
    return unknown(data=data, counts=counts, message="转存任务已结束，但未返回逐项结果")


def _optional_result_text(context, value, field_name):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        contract_mismatch(context, "任务详情逐项结果的 %s 不是字符串" % field_name)
    text = value.strip()
    return text or None


def file_task_counts(results, submitted):
    """按 batchFileResults 逐项 errCode 推导结果计数；无逐项结果时只报提交数。

    个人云任务仅在部分任务类型返回 batchFileResults（批量复制不返回），
    此时成功/失败数保持未知（不写 0）。
    """
    if submitted is None and not isinstance(results, list):
        return None
    counts = {"submitted": submitted}
    if isinstance(results, list):
        succeeded = 0
        failed = 0
        for item in results:
            if not isinstance(item, dict):
                continue
            if item.get("errCode") == "0000":
                succeeded += 1
            else:
                failed += 1
        counts["succeeded"] = succeeded
        counts["failed"] = failed
    return counts


MAX_PAGE_FETCHES = 50
DYNAMIC_DEFAULT_PAGE_SIZE = 10


def collect_all(context, fetch_page, *, limit, initial_cursor=None):
    """按真实 cursor 逐页采集，带条数保护上限与页数保护上限。

    fetch_page(cursor) 必须返回 (items, next_cursor)。
    只有上游明确给出空 next_cursor 才算可靠终止（complete=True）。
    `stoppedBy` 区分终止原因：`exhausted`（上游取尽）/ `limit`（触到条数上限）/
    `page-cap`（触到页数上限），供调用方判断"是否被上限截断"而不是靠条数反推。
    """
    items = []
    cursor = initial_cursor
    pages = 0
    complete = False
    stopped_by = "exhausted"
    while True:
        page_items, next_cursor = fetch_page(cursor)
        items.extend(page_items)
        pages += 1
        if not next_cursor:
            complete = True
            cursor = None
            break
        cursor = next_cursor
        if len(items) >= limit:
            stopped_by = "limit"
            break
        if pages >= MAX_PAGE_FETCHES:
            stopped_by = "page-cap"
            break
    return {
        "items": items,
        "complete": complete,
        "cursor": cursor,
        "pages": pages,
        "stoppedBy": stopped_by,
    }


SCOPE_DEDICATED_ROOT = "dedicated_root"
SCOPE_AUTHORIZED_PERSONAL = "authorized_personal_cloud"
SCOPE_OUTER_LINK = "outer_link"
SCOPE_SHARE_RECORD = "share_record"
SCOPE_ALBUM = "album_story"
SCOPE_ACTIVITY = "activity"
SCOPE_LOCAL = "local_filesystem"

FILE_ITEM_FIELDS = ("name", "type", "size", "createdAt", "updatedAt", "takenAt")
DYNAMIC_ITEM_FIELDS = (
    "name",
    "dynamicType",
    "dynamicTime",
    "eventTime",
    "createTime",
    "takenTime",
    "businessType",
    "contentType",
    "category",
    "type",
)
DYNAMIC_TIME_FIELDS = ("dynamicTime", "eventTime", "createTime", "takenTime")
# 补名：动态接口不返回 name，按 contentId 调 file/batchGet 补齐；接口单次上限 100
DYNAMIC_NAME_BATCH = 100
DYNAMIC_NAME_ID_MAX = 64
ID_KEY_CANDIDATES = ("dynamicId", "fileId", "contentId", "albumId", "storyId", "recordId", "linkId", "id")
SCALAR_TYPES = (str, int, float, bool)


def normalize_file_item(context, item, scope, *, id_key="fileId", ref_kind="file"):
    """按可选白名单提取文件字段，真实 ID 换成 CLI 引用后再对外输出。"""
    if not isinstance(item, dict):
        contract_mismatch(context, "列表项不是对象")
    normalized = {}
    real_id = item.get(id_key)
    issued_kind = ref_kind
    output_key = "fileRef"
    if ref_kind == "file" and (item.get("type") in ("folder", "1", 1) or item.get("category") in ("folder", "100", 100)):
        issued_kind = "folder"
        output_key = "folderRef"
    if isinstance(real_id, str) and real_id:
        normalized[output_key] = context.issue_ref(issued_kind, real_id, scope)
    else:
        normalized[output_key] = None
    for key in FILE_ITEM_FIELDS:
        value = item.get(key)
        if value is None:
            continue
        if isinstance(value, SCALAR_TYPES):
            normalized[key] = value
    parent_id = item.get("parentFileId")
    if isinstance(parent_id, str) and parent_id and parent_id.strip() not in ROOT_SENTINELS:
        normalized["parentRef"] = context.issue_ref("folder", parent_id, scope)
    return normalized


def normalize_file_items(context, items, scope, *, id_key="fileId", ref_kind="file"):
    return [normalize_file_item(context, item, scope, id_key=id_key, ref_kind=ref_kind) for item in items]


def stable_item_key(item):
    """稳定去重键：优先已存在的主键候选，全部缺失时回退到内容摘要。"""
    if isinstance(item, dict):
        for key in ID_KEY_CANDIDATES:
            value = item.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
                return "%s:%s" % (key, value)
        canonical = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "hash:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return "value:" + repr(item)


def normalize_dynamic_item(context, item, scope):
    """动态条目：输出白名单字段，并把真实标识替换为用途受限的受控引用。"""
    if not isinstance(item, dict):
        contract_mismatch(context, "动态列表项不是对象")
    normalized = {}
    content_id = item.get("contentId")
    if isinstance(content_id, str) and content_id:
        # contentId 只证明“这条动态关联的内容”，尚未证明它等同于个人云 fileId。
        # 因此签发 dynamicRef 供展示与后续受控映射使用，绝不冒充 fileRef。
        normalized["dynamicRef"] = context.issue_ref("dynamic", content_id, scope)
    parent_id = item.get("parentFileId")
    if isinstance(parent_id, str) and parent_id:
        # 动态接口只证明父目录标识存在；仍须先核验真实目录，再用于文件操作。
        normalized["parentRef"] = context.issue_ref("dynamic-parent", parent_id, scope)
    batch_id = item.get("batchNewId")
    if isinstance(batch_id, str) and batch_id:
        normalized["batchRef"] = context.issue_ref("dynamic-batch", batch_id, scope)
    file_id = item.get("fileId")
    if isinstance(file_id, str) and file_id:
        normalized["fileRef"] = context.issue_ref("file", file_id, SCOPE_AUTHORIZED_PERSONAL)
    for key in DYNAMIC_ITEM_FIELDS:
        value = item.get(key)
        if isinstance(value, SCALAR_TYPES):
            normalized[key] = value
    for key in DYNAMIC_TIME_FIELDS:
        value = item.get(key)
        if isinstance(value, str) and value:
            normalized["occurredAt"] = value
            break
    reading_record = item.get("readingRecord")
    if isinstance(reading_record, dict):
        normalized_record = {}
        for key in ("chapterOffset", "progress"):
            value = reading_record.get(key)
            if isinstance(value, str):
                normalized_record[key] = value
        for source_key, output_key, kind in (
            ("chapterId", "chapterRef", "reading-chapter"),
            ("thirdContentId", "thirdContentRef", "reading-third-content"),
            ("cId", "contentSquareRef", "reading-content-square"),
        ):
            value = reading_record.get(source_key)
            if isinstance(value, str) and value:
                normalized_record[output_key] = context.issue_ref(kind, value, scope)
        if normalized_record:
            normalized["readingRecord"] = normalized_record
    return normalized


def query_dynamic_page(
    context,
    *,
    dynamic_type,
    start_time,
    end_time,
    keyword,
    content_type,
    page_size,
    next_cursor=None,
    dynamic_types=None,
):
    """单类型动态分页查询：返回 (items, next_cursor, total)。

    上游游标字段未冻结前，只有响应真实给出非空 nextPageCursor 才继续翻页；
    否则按“无法证明取全”处理，由调用方置 complete=false。
    """
    from cmcloud_adapters import idaas_personal_dynamic_query

    payload = guard_upstream(
        context,
        idaas_personal_dynamic_query.call,
        start_time=start_time,
        end_time=end_time,
        keyword=keyword,
        dynamic_type=dynamic_type,
        page_size=page_size,
        next_page_cursor=next_cursor,
        content_type=content_type,
        dynamic_types=dynamic_types,
    )
    result_data = payload.get("resultData")
    if result_data is None:
        result_data = {}
    elif not isinstance(result_data, dict):
        contract_mismatch(context, "动态查询响应缺少 resultData 对象")
    items = result_data.get("skillFileList")
    # 下游在 total=0 时可能省略业务可选的 skillFileList，而不是返回空数组。
    # 成功包络下将这种表示归一化为空结果，避免把正常空集误判为契约不符。
    if items is None:
        items = []
    if not isinstance(items, list):
        contract_mismatch(context, "动态查询响应缺少 skillFileList 列表")
    cursor = result_data.get("nextPageCursor")
    cursor = cursor if isinstance(cursor, str) and cursor else None
    total = result_data.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        total = None
    return items, cursor, total


def local_path(context, value, *, must_be_file=False, must_be_dir=False):
    """本地路径核验：必须是绝对路径，并按调用场景校验文件或目录类型。"""
    import os

    if not isinstance(value, str) or not value.strip():
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "本地路径必须是非空字符串")
    if not os.path.isabs(value):
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "本地路径必须是绝对路径")
    if "\x00" in value:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "本地路径含非法字符")
    path = os.path.normpath(value)
    if must_be_file and not os.path.isfile(path):
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "本地路径不是普通文件")
    if must_be_dir and not os.path.isdir(path):
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "本地路径不是目录")
    return path


_WINDOWS_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM%d" % index for index in range(1, 10)]
    + ["LPT%d" % index for index in range(1, 10)]
)
_WINDOWS_ILLEGAL_CHARS = ('"', "<", ">", "|", "?", "*", ":")


def safe_local_file_name(name):
    """云端文件名的本地落盘校验：只接受单一文件名，越界或本机非法名一律拒绝。

    name 来自下游响应，不能信任：可能带路径分隔符（拼路径后越出所选目录），也可能是本机
    非法/保留名。这里只**拒绝**、不静默改名——改名会让落盘名与云端不一致，需要新名字时应
    由上层重新生成计划并再次确认。

    校验按平台分流：路径分隔符取 os.sep/os.altsep（Windows 上 "\\" 与 "/" 都拒绝，macOS/Linux
    上只有 "/" 构成越界）；保留设备名、结尾点/空格与 ('"', '<', '>', '|', '?', '*', ':')
    都是 Windows 文件系统的限制，在 POSIX 上会误拒合法文件名，因此只在 Windows 生效。
    """
    if not isinstance(name, str) or not name.strip():
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "云端文件名不可用于本地保存（空文件名）"
        )
    if name != name.strip():
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "云端文件名首尾含空白，已拒绝")
    if len(name) > 255:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "云端文件名过长，已拒绝")
    if name in (".", ".."):
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "云端文件名是目录占位名，已拒绝")
    if os.sep in name or (os.altsep is not None and os.altsep in name):
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "云端文件名含路径分隔符，已拒绝写出到所选目录之外"
        )
    if any(ord(char) < 0x20 for char in name):
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "云端文件名含控制字符，已拒绝")
    if os.name == "nt":
        if any(char in name for char in _WINDOWS_ILLEGAL_CHARS):
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "云端文件名含本机非法字符，已拒绝")
        if name.endswith(".") or name.endswith(" "):
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "云端文件名以点或空格结尾，已拒绝")
        if name.split(".")[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "云端文件名是本机保留名，已拒绝")
    return name


def ensure_within_directory(directory, target):
    """规范化（含符号链接/目录联接解析）后校验目标仍在授权目录内。"""
    import os

    base = os.path.realpath(directory)
    candidate = os.path.realpath(target)
    if candidate != base and not candidate.startswith(base + os.sep):
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "本地目标越出所选目录，已拒绝"
        )
    return candidate


def local_file_info(context, value, algorithms=("sha256",)):
    """本地普通文件核验 + 元数据（大小、摘要）；只读，不产生副作用。

    摘要算法由调用方按发布配置声明：上传底座不同，要求的算法不同
    （pds 底座只认 sha1、云空间底座只认 sha256），因此这里只算被要求的算法，
    并把算法名作为结果键返回（如 result["sha1"]）。
    """
    import hashlib
    import os

    path = local_path(context, value, must_be_file=True)
    requested = tuple(algorithms)
    for algorithm in requested:
        if algorithm not in ("sha1", "sha256"):
            raise result_module.ExecutionError(
                result_module.FAILED, "不支持的本地文件摘要算法: %s" % algorithm
            )
    try:
        size = os.path.getsize(path)
        digests = {algorithm: hashlib.new(algorithm) for algorithm in requested}
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                for digest in digests.values():
                    digest.update(chunk)
    except OSError as err:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "本地文件不可读: %s" % err.__class__.__name__
        )
    info = {"path": path, "size": size, "name": os.path.basename(path)}
    for algorithm, digest in digests.items():
        info[algorithm] = digest.hexdigest()
    return info


def activity_window(context, input_data, *, digest_extra=None, dynamic_type=None):
    """动态场景公共前置：默认时间窗、页大小、游标解析与条件摘要。

    摘要只包含 Agent 实际提交的条件；缺省时间窗一旦确定便写入游标状态并在续页时复用，
    避免“默认窗口跟随当前时间漂移”导致游标不可复用。
    """
    page_size = input_data.get("pageSize") or DYNAMIC_DEFAULT_PAGE_SIZE
    conditions = {
        "dynamicType": dynamic_type,
        "keyword": input_data.get("keyword"),
        "contentType": input_data.get("contentType"),
        "from": input_data.get("from"),
        "to": input_data.get("to"),
        "pageSize": page_size,
    }
    if digest_extra:
        conditions.update(digest_extra)
    digest = conditions_digest(conditions)
    upstream_state = {}
    if input_data.get("cursor"):
        upstream_state = context.resolve_cursor(input_data["cursor"], digest).get("upstreamState") or {}
    stored_window = upstream_state.get("window") or {}
    start_time = stored_window.get("startTime")
    end_time = stored_window.get("endTime")
    if not isinstance(start_time, str) or not isinstance(end_time, str):
        start_time, end_time = default_time_window(context, days=context.activity_window_days())
        if input_data.get("from"):
            start_time = _to_dynamic_time(input_data["from"])
        if input_data.get("to"):
            end_time = _to_dynamic_time(input_data["to"])
    candidate = upstream_state.get("nextPageCursor")
    return {
        "digest": digest,
        "pageSize": page_size,
        "keyword": input_data.get("keyword"),
        "contentType": input_data.get("contentType"),
        "startTime": start_time,
        "endTime": end_time,
        "dynamicType": dynamic_type,
        "nextPageCursor": candidate if isinstance(candidate, str) and candidate else None,
        "upstreamState": upstream_state,
        "windowState": {"startTime": start_time, "endTime": end_time},
    }


def activity_page(context, window, *, dynamic_type=None, dynamic_types=None):
    return query_dynamic_page(
        context,
        dynamic_type=dynamic_type or window["dynamicType"],
        start_time=window["startTime"],
        end_time=window["endTime"],
        keyword=window["keyword"],
        content_type=window["contentType"],
        page_size=window["pageSize"],
        next_cursor=window["nextPageCursor"],
        dynamic_types=dynamic_types,
    )


def activity_terminal(items, next_cursor, branch_cursors=None):
    """判断本查询时间窗内是否已取尽。

    上游不再下发任何后续游标、且本轮没有任何数据（含续页返回空）时，该窗内已取尽；
    本轮仍有数据却没有后续游标时无法证明取尽，保持未知，不得宣称查全。
    """
    if next_cursor or branch_cursors:
        return False
    return not items


FILE_TYPE_BATCH = 100  # file/batchGet 单次上限（adapter 不隐式分批）


def batch_file_types(context, file_ids):
    """按 file/batchGet 批量取文件类型：fileId -> "file"/"folder"。

    只采纳逐项成功（errCode 为空/0000/0）、srcFile.fileId 与请求回显一致，且
    type 为 file/folder 的条目；批量异常、整批失败或字段缺失只返回已解析的
    部分，由调用方决定是否逐条回退核验。批量取类型是弱依赖，不因接口异常让
    读命令直接失败。
    """
    from cmcloud_adapters import file_batch_get

    ids = []
    for file_id in file_ids:
        if isinstance(file_id, str) and file_id and file_id not in ids:
            ids.append(file_id)
    types_by_id = {}
    for start in range(0, len(ids), FILE_TYPE_BATCH):
        chunk = ids[start : start + FILE_TYPE_BATCH]
        try:
            payload = context.call(file_batch_get.call, file_ids=chunk)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        data = payload.get("data")
        results = data.get("batchFileResults") if isinstance(data, dict) else None
        if not isinstance(results, list):
            continue
        for row in results:
            if not isinstance(row, dict):
                continue
            if row.get("errCode") not in (None, "0000", "0"):
                continue
            source = row.get("srcFile")
            if not isinstance(source, dict):
                continue
            file_id = source.get("fileId")
            node_type = source.get("type")
            if isinstance(file_id, str) and file_id in chunk and node_type in ("file", "folder"):
                types_by_id[file_id] = node_type
    return types_by_id


def dynamic_name_map(context, raw_items):
    """按动态条目的 contentId 批量补查文件名。

    动态接口不返回 name，但 contentId 与个人云 fileId 一致（2026-09-20 实测
    type 2/4/5 共 29 条，返回 fileId 与传入 id 一一对应）。只采纳逐项成功且
    fileId 回显一致的 name；整批失败、逐项失败或未命中一律不填，读命令不因
    补名失败而失败，也不借此签发文件引用（位置与范围未核验）。
    """
    from cmcloud_adapters import file_batch_get

    ids = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        value = item.get("contentId")
        if isinstance(value, str) and value and len(value) <= DYNAMIC_NAME_ID_MAX and value not in ids:
            ids.append(value)
    names = {}
    for start in range(0, len(ids), DYNAMIC_NAME_BATCH):
        chunk = ids[start : start + DYNAMIC_NAME_BATCH]
        try:
            payload = context.call(file_batch_get.call, file_ids=chunk)
        except Exception:
            # 补名是弱依赖：失败即降级为“名称未返回”，也不中断查询
            continue
        if not isinstance(payload, dict):
            continue
        data = payload.get("data")
        results = data.get("batchFileResults") if isinstance(data, dict) else None
        if not isinstance(results, list):
            continue
        for row in results:
            if not isinstance(row, dict):
                continue
            if row.get("errCode") not in (None, "0000", "0"):
                continue
            source = row.get("srcFile")
            if not isinstance(source, dict):
                continue
            file_id = source.get("fileId")
            name = source.get("name")
            if isinstance(file_id, str) and file_id in chunk and isinstance(name, str) and name:
                names[file_id] = name
    return names


def apply_dynamic_names(context, raw_items, normalized_items):
    """把补查到的文件名写回统一后的动态条目；只补 name，已有 name 不覆盖。"""
    names = dynamic_name_map(context, raw_items)
    if not names:
        return normalized_items
    for source, item in zip(raw_items, normalized_items):
        if not isinstance(item, dict) or item.get("name") or not isinstance(source, dict):
            continue
        content_id = source.get("contentId")
        name = names.get(content_id) if isinstance(content_id, str) else None
        if name:
            item["name"] = name
    return normalized_items


def activity_outcome(
    context, window, items, next_cursor, *, total=None, extra=None, branch_cursors=None
):
    """统一输出：窗内取尽时 complete=true；仍有后续游标时 hasMore=true、complete=false。

    complete 只描述**本查询时间窗**（默认最近 30 天或用户指定区间）内是否取尽，
    不代表更早历史；该范围提示由调用方按 `references/activity.md` 输出追加项给出。
    """
    normalized = [normalize_dynamic_item(context, item, SCOPE_ACTIVITY) for item in items]
    apply_dynamic_names(context, items, normalized)
    upstream_state = {"window": window.get("windowState")}
    if next_cursor:
        upstream_state["nextPageCursor"] = next_cursor
    if branch_cursors:
        upstream_state["branchCursors"] = branch_cursors
    has_state = bool(next_cursor or branch_cursors)
    done = activity_terminal(normalized, next_cursor, branch_cursors)
    token = None
    if has_state:
        token = context.issue_cursor(window["digest"], upstream_state, complete=False)
    page = page_info(
        window["pageSize"],
        len(normalized),
        total=total,
        has_more=True if has_state else (False if done else None),
        next_cursor=token,
        complete=done,
    )
    data = {"items": normalized}
    if extra:
        data.update(extra)
    counts = {"returned": len(normalized)}
    if not normalized:
        return empty(data=data, page=page)
    return success(data=data, counts=counts, page=page)


SENSITIVE_KEY_PATTERN = re.compile(
    r"(msisdn|phone|mobile|passwd|password|secret|token|credential|account|cipher|aes|sign|url)",
    re.IGNORECASE,
)


def strip_sensitive(value):
    """递归移除敏感键（手机号、票据、密码、账号密文、预签名地址等）。"""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if isinstance(key, str) and SENSITIVE_KEY_PATTERN.search(key):
                continue
            cleaned[key] = strip_sensitive(item)
        return cleaned
    if isinstance(value, list):
        return [strip_sensitive(item) for item in value]
    return value


def sanize_items(items):
    """按真实 data 元素透传时统一去除敏感字段。"""
    return [strip_sensitive(item) for item in items if isinstance(item, dict)]


def require_host_context(context, name):
    value = context.host_context.get(name)
    if not isinstance(value, str) or not value:
        raise result_module.ExecutionError(
            result_module.FAILED, "缺少可信宿主上下文: %s" % name
        )
    return value
