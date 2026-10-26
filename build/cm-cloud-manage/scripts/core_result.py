"""统一 JSON 结果信封、错误码与退出码。

对外只允许顶层字段：schemaVersion、command、status、data、counts、page、error、nextAction、traceId。
page 只允许 pageSize/returned/total/hasMore/nextCursor/complete；error 只允许 code/message/retryable。
本模块不依赖任何 adapter、workflow 或台账实现。
"""

SCHEMA_VERSION = "1.0"

STATUS_SUCCESS = "SUCCESS"
STATUS_EMPTY = "EMPTY"
STATUS_PARTIAL = "PARTIAL"
STATUS_RUNNING = "RUNNING"
STATUS_CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
STATUS_AUTH_REQUIRED = "AUTH_REQUIRED"
STATUS_REJECTED = "REJECTED"
STATUS_FAILED = "FAILED"
STATUS_UNKNOWN = "UNKNOWN"

STATUS_VALUES = (
    STATUS_SUCCESS,
    STATUS_EMPTY,
    STATUS_PARTIAL,
    STATUS_RUNNING,
    STATUS_CONFIRMATION_REQUIRED,
    STATUS_AUTH_REQUIRED,
    STATUS_REJECTED,
    STATUS_FAILED,
    STATUS_UNKNOWN,
)

# 对外错误码统一登记；任务状态查询失败独立于任务业务失败。
INVALID_ARGUMENT = "INVALID_ARGUMENT"
AUTH_REQUIRED = "AUTH_REQUIRED"
AUTH_EXPIRED = "AUTH_EXPIRED"
CONSENT_DENIED = "CONSENT_DENIED"
BINDING_MISMATCH = "BINDING_MISMATCH"
SCOPE_DENIED = "SCOPE_DENIED"
CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
NOT_FOUND = "NOT_FOUND"
AMBIGUOUS_RESOURCE = "AMBIGUOUS_RESOURCE"
CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
PLAN_EXPIRED = "PLAN_EXPIRED"
CONFLICT = "CONFLICT"
RATE_LIMITED = "RATE_LIMITED"
UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
TASK_STATUS_QUERY_FAILED = "TASK_STATUS_QUERY_FAILED"
UNKNOWN = "UNKNOWN"
UPSTREAM_CONTRACT_MISMATCH = "UPSTREAM_CONTRACT_MISMATCH"
SECURE_STORE_UNAVAILABLE = "SECURE_STORE_UNAVAILABLE"
FAILED = "FAILED"

ERROR_CODES = (
    INVALID_ARGUMENT,
    AUTH_REQUIRED,
    AUTH_EXPIRED,
    CONSENT_DENIED,
    BINDING_MISMATCH,
    SCOPE_DENIED,
    CAPABILITY_UNAVAILABLE,
    NOT_FOUND,
    AMBIGUOUS_RESOURCE,
    CONFIRMATION_REQUIRED,
    PLAN_EXPIRED,
    CONFLICT,
    RATE_LIMITED,
    UPSTREAM_TIMEOUT,
    TASK_STATUS_QUERY_FAILED,
    UNKNOWN,
    UPSTREAM_CONTRACT_MISMATCH,
    SECURE_STORE_UNAVAILABLE,
    FAILED,
)

DEFAULT_RETRYABLE = {code: False for code in ERROR_CODES}
DEFAULT_RETRYABLE[RATE_LIMITED] = True
DEFAULT_RETRYABLE[TASK_STATUS_QUERY_FAILED] = True

OPERATION_READ = "read"
OPERATION_WRITE = "write"
OPERATION_TYPES = (OPERATION_READ, OPERATION_WRITE)

BINDING_RELATIONSHIP = "relationship"
BINDING_VERIFICATION = "verification"
BINDING_TYPES = (BINDING_RELATIONSHIP, BINDING_VERIFICATION)

EXIT_OK = 0
EXIT_INVALID_ARGUMENT = 2
EXIT_AUTH_OR_CONFIRM = 3
EXIT_REJECTED = 4
EXIT_FAILED = 5
EXIT_UNKNOWN = 6
EXIT_FACILITY = 7

EXIT_CODES = {
    INVALID_ARGUMENT: EXIT_INVALID_ARGUMENT,
    AMBIGUOUS_RESOURCE: EXIT_INVALID_ARGUMENT,
    NOT_FOUND: EXIT_INVALID_ARGUMENT,
    CONFLICT: EXIT_INVALID_ARGUMENT,
    CONFIRMATION_REQUIRED: EXIT_AUTH_OR_CONFIRM,
    PLAN_EXPIRED: EXIT_AUTH_OR_CONFIRM,
    AUTH_REQUIRED: EXIT_AUTH_OR_CONFIRM,
    AUTH_EXPIRED: EXIT_AUTH_OR_CONFIRM,
    CONSENT_DENIED: EXIT_REJECTED,
    CAPABILITY_UNAVAILABLE: EXIT_REJECTED,
    SCOPE_DENIED: EXIT_REJECTED,
    BINDING_MISMATCH: EXIT_FAILED,
    FAILED: EXIT_FAILED,
    UPSTREAM_CONTRACT_MISMATCH: EXIT_FAILED,
    RATE_LIMITED: EXIT_UNKNOWN,
    UPSTREAM_TIMEOUT: EXIT_UNKNOWN,
    TASK_STATUS_QUERY_FAILED: EXIT_UNKNOWN,
    UNKNOWN: EXIT_UNKNOWN,
    SECURE_STORE_UNAVAILABLE: EXIT_FACILITY,
}

TOP_LEVEL_KEYS = (
    "schemaVersion",
    "command",
    "status",
    "data",
    "counts",
    "page",
    "error",
    "nextAction",
    "traceId",
)

PAGE_KEYS = ("pageSize", "returned", "total", "hasMore", "nextCursor", "complete")

ERROR_KEYS = ("code", "message", "retryable")

# 「调用方未指定」哨兵：与显式传 next_action=None（要求保持 null）区分。
_AUTO_NEXT_ACTION = object()

NEXT_ACTION_AUTH_LOGIN = "auth.login"
NEXT_ACTION_OPERATION_CONFIRM = "operation.confirm"
NEXT_ACTION_OPERATION_EXECUTE = "operation.execute"
NEXT_ACTION_OPERATION_STATUS = "operation.status"


class ResultError(RuntimeError):
    """信封或错误码使用不当（仅内部编程错误，不面向 Agent 输出）。"""


class ExecutionError(Exception):
    """场景执行失败的统一异常载体。

    code 取本模块错误码常量；message 必须是可外发的脱敏说明，不得包含票据、
    手机号明文、下游原始响应、堆栈或预签名 URL。
    """

    __slots__ = (
        "code",
        "message",
        "retryable",
        "operation_type",
        "binding_type",
        "next_action",
        "next_action_explicit",
        "data",
        "counts",
        "page",
    )

    def __init__(
        self,
        code,
        message,
        *,
        retryable=None,
        operation_type=None,
        binding_type=None,
        next_action=_AUTO_NEXT_ACTION,
        data=None,
        counts=None,
        page=None,
    ):
        code = normalize_error_code(code)
        operation_type = _normalize_operation_type(operation_type)
        binding_type = _normalize_binding_type(binding_type)
        super().__init__(message)
        self.code = code
        self.message = message
        self.operation_type = operation_type
        self.binding_type = binding_type
        self.retryable = _normalize_retryable(
            code,
            retryable,
            operation_type=operation_type,
            binding_type=binding_type,
        )
        # 显式传 next_action=None 表示"要求保持 null"，不再按错误码兜底推导。
        self.next_action_explicit = next_action is not _AUTO_NEXT_ACTION
        self.next_action = None if next_action is _AUTO_NEXT_ACTION else next_action
        self.data = data
        self.counts = counts
        self.page = page

    @property
    def exit_code(self):
        return EXIT_CODES[self.code]


def exit_code_for(code):
    code = normalize_error_code(code)
    return EXIT_CODES[code]


def normalize_page(page):
    """把场景返回的 page 归一化为允许字段；未知总数保持 None，不得写成 0。"""
    if page is None:
        return None
    if not isinstance(page, dict):
        raise ResultError("page 必须是对象")
    for key in page:
        if key not in PAGE_KEYS:
            raise ResultError("page 含未允许字段: %s" % key)
    normalized = {key: page.get(key) for key in PAGE_KEYS}
    returned = normalized["returned"]
    if returned is None or isinstance(returned, bool) or not isinstance(returned, int) or returned < 0:
        raise ResultError("page.returned 必须是非负整数")
    page_size = normalized["pageSize"]
    if page_size is not None and (isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 0):
        raise ResultError("page.pageSize 必须是非负整数或 null")
    total = normalized["total"]
    if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < 0):
        raise ResultError("page.total 必须是未知(null)或非负整数")
    has_more = normalized["hasMore"]
    if has_more is not None and not isinstance(has_more, bool):
        raise ResultError("page.hasMore 必须是布尔值或 null（未知）")
    complete = normalized["complete"]
    if not isinstance(complete, bool):
        raise ResultError("page.complete 必须是布尔值")
    next_cursor = normalized["nextCursor"]
    if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor):
        raise ResultError("page.nextCursor 必须是非空字符串或 null")
    return normalized


def _normalize_counts(counts):
    if counts is None:
        return None
    if not isinstance(counts, dict):
        raise ResultError("counts 必须是对象")
    normalized = {}
    for key, value in counts.items():
        if not isinstance(key, str) or not key:
            raise ResultError("counts 的键必须是非空字符串")
        if value is None:
            normalized[key] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ResultError("counts.%s 必须是非负整数或 null" % key)
        normalized[key] = value
    return normalized


def normalize_error_code(code):
    """表外或缺失错误码统一收敛为 UNKNOWN，不把内部枚举错误暴露给 CLI。"""
    return code if code in ERROR_CODES else UNKNOWN


def _normalize_operation_type(operation_type):
    if operation_type is None:
        return None
    if operation_type not in OPERATION_TYPES:
        raise ResultError("operation_type 必须是 read、write 或 null")
    return operation_type


def _normalize_binding_type(binding_type):
    if binding_type is None:
        return None
    if binding_type not in BINDING_TYPES:
        raise ResultError("binding_type 必须是 relationship、verification 或 null")
    return binding_type


def _normalize_retryable(code, retryable, *, operation_type=None, binding_type=None):
    if retryable is None:
        if code == BINDING_MISMATCH:
            return binding_type == BINDING_VERIFICATION
        if code == UPSTREAM_TIMEOUT:
            return operation_type == OPERATION_READ
        return DEFAULT_RETRYABLE[code]
    return bool(retryable)


def build_error(code, message, retryable=None):
    code = normalize_error_code(code)
    if not isinstance(message, str) or not message:
        raise ResultError("error.message 必须是非空字符串")
    return {"code": code, "message": message, "retryable": _normalize_retryable(code, retryable)}


class Envelope:
    """统一结果信封；构造后字段不可再增删。"""

    __slots__ = ("_payload",)

    def __init__(self, command, status, *, data=None, counts=None, page=None, error=None, next_action=None, trace_id=None):
        if status not in STATUS_VALUES:
            raise ResultError("未知状态: %s" % status)
        if command is not None and (not isinstance(command, str) or not command):
            raise ResultError("command 必须是非空字符串")
        if error is not None:
            error = build_error(error.get("code"), error.get("message"), error.get("retryable"))
        if next_action is not None and (not isinstance(next_action, str) or not next_action):
            raise ResultError("nextAction 必须是非空字符串或 null")
        if trace_id is not None and not isinstance(trace_id, str):
            raise ResultError("traceId 必须是字符串或 null")
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "command": command,
            "status": status,
            "data": data,
            "counts": _normalize_counts(counts),
            "page": normalize_page(page),
            "error": error,
            "nextAction": next_action,
            "traceId": trace_id,
        }
        if tuple(payload.keys()) != TOP_LEVEL_KEYS:
            raise ResultError("信封字段集合与约定不一致")
        self._payload = payload

    @property
    def status(self):
        return self._payload["status"]

    @property
    def command(self):
        return self._payload["command"]

    @property
    def error_code(self):
        error = self._payload["error"]
        return error["code"] if error else None

    def to_dict(self):
        return dict(self._payload)

    def to_json(self):
        import json

        return json.dumps(self._payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False)

    def exit_code(self):
        status = self._payload["status"]
        error = self._payload["error"]
        if error is not None:
            return exit_code_for(error["code"])
        if status in (STATUS_SUCCESS, STATUS_EMPTY):
            return EXIT_OK
        if status == STATUS_CONFIRMATION_REQUIRED:
            return EXIT_AUTH_OR_CONFIRM
        if status == STATUS_AUTH_REQUIRED:
            return EXIT_AUTH_OR_CONFIRM
        if status == STATUS_REJECTED:
            return EXIT_REJECTED
        if status == STATUS_PARTIAL:
            return EXIT_FAILED
        if status == STATUS_FAILED:
            return EXIT_FAILED
        if status in (STATUS_RUNNING, STATUS_UNKNOWN):
            return EXIT_UNKNOWN
        raise ResultError("状态缺少退出码映射: %s" % status)


def envelope_from_error(command, error, trace_id=None):
    """把 ExecutionError 转为信封，并按通用输出契约确定状态与恢复动作。"""
    if error.code in (CONFIRMATION_REQUIRED, PLAN_EXPIRED):
        status = STATUS_CONFIRMATION_REQUIRED
    elif error.code in (AUTH_REQUIRED, AUTH_EXPIRED):
        status = STATUS_AUTH_REQUIRED
    elif error.code in (CAPABILITY_UNAVAILABLE, SCOPE_DENIED, CONSENT_DENIED):
        status = STATUS_REJECTED
    elif error.code == RATE_LIMITED:
        status = STATUS_UNKNOWN
    elif error.code == UPSTREAM_TIMEOUT:
        # 超时统一收敛为 UNKNOWN；retryable 仍区分只读可重试与写操作不可重发。
        status = STATUS_UNKNOWN
    elif error.code in (UNKNOWN, TASK_STATUS_QUERY_FAILED):
        status = STATUS_UNKNOWN
    else:
        status = STATUS_FAILED
    next_action = error.next_action
    if next_action is None and not error.next_action_explicit:
        if error.code in (AUTH_REQUIRED, AUTH_EXPIRED):
            # 未授权时本轮唯一可执行动作是取授权入口；入口由 auth.login 回执携带。
            next_action = NEXT_ACTION_AUTH_LOGIN
        elif error.code == CONFIRMATION_REQUIRED:
            next_action = NEXT_ACTION_OPERATION_CONFIRM
    return Envelope(
        command,
        status,
        data=error.data,
        counts=error.counts,
        page=error.page,
        error={"code": error.code, "message": error.message, "retryable": error.retryable},
        next_action=next_action,
        trace_id=trace_id,
    )
