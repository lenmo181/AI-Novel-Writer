"""auth 域公共底座：IDaaS SDK 隔离加载、setConfig 初始化、stdout 捕获与统一信封适配。

设计边界（与执行 md 第 3、4.2、4.3、9、10 节一致）：
- 包装文件只调用一个 SDK 公共接口；SDK 动态加载、配置注入、输出隔离、traceId、信封与
  错误码适配、脱敏全部收敛在本模块，包装层不得各自另建实现；
- 状态与退出码一律走 core_result.py，不新增错误码枚举或状态映射；
- error.code 只能是 result.py 的 18 种；SDK 表外/缺失原始码统一归一为 UNKNOWN；
- nextAction 默认 null，CONFIRMATION_REQUIRED→operation.confirm、AUTH_REQUIRED→auth.login
  （未授权时本轮唯一动作是取一次授权入口）；auth.login 自身的回执携带入口，不下发
  nextAction，避免自循环；
- SDK 的 print/_log 一律重定向到 stderr 且脱敏，保证 stdout 只有一个 JSON 文档；
- 不新增第三方依赖：SDK 依赖（requests/pycryptodome/keyring）缺失按能力不可用处理。
"""

import contextlib
import hashlib
import io
import ipaddress
import json
import os
import re
import sys
import urllib.parse
import uuid
from dataclasses import dataclass

from cmcloud_core import result as result_module
from cmcloud_auth import idaas_profile, qr_image

SDK_DIR_NAME = "IDaaS_AUT_KMS_v3.1.1"
TOKEN_SERVICE_MODULE = "token_service"
KEYCHAIN_STORE_MODULE = "keychain_store"
DEVICE_INFO_MODULE = "device_info"

SDK_CODE_SUCCESS = "103000"
SDK_CODE_AUTH_REQUIRED = "103001"

# 2026-09-20 实测：本机存有"已下发、用户尚未在浏览器完成"的授权 uuid 时，服务端对后续
# getToken 返回 130088(uuid未授权) 且**不下发授权入口**。该码在结果码表外，若不登记会被
# 归一成 UNKNOWN，授权入口永远拿不回来；按"仍需完成授权"处理（AUTH_REQUIRED），入口由
# acquire_token 用缓存的上一次入口重放。
#
# ★ 注意 130088 不是"会话已损坏"：清掉本机 uuid 后服务端会立刻下发**另一个全新会话**与
# 新入口（实测同等条件复现），也就是说清 uuid 等于丢弃用户正在（或已经）完成的那次授权
# ——这正是"授权完成后仍被要求重新授权"的根因。故此处只在**没有入口可重放**时才作废会话。
SDK_CODE_PENDING_UUID = "130088"

# 授权范围变化：优先从 desc 解析服务端要求的 scopeIds；缺失时用当前短票据调用
# validate 查询。协商值保存后，后续取票始终使用保存值。
SDK_CODE_SCOPE_CHANGED = "103082"

VALIDATE_PATH = "/keyManage/custody/validate"
VALIDATE_TYPE = "139"
VALIDATE_TIMEOUT_SECONDS = 10

AUTH_REQUIRED_MESSAGE = "需要先完成中国移动云盘授权"

AUTHORIZATION_STATE_PENDING = "PENDING"

# SDK 原始码 → result.py 的 18 种错误码；成功码不在表内，由 map_sdk_result_code 单独返回 None。
#
# 200001~200004/200014 映射 FAILED：这些码只描述受控登记参数（devId/clientId/skillId/
# skillName/strategyId），Agent 越权字段在进入 SDK 前已被 assert_no_agent_fields 拒绝，
# 运行期出现即属「受控配置或运行环境错误」，按 §5.8 使用 FAILED，不让 Agent 去补正登记值。
#
# 429 → RATE_LIMITED：SDK 未冻结专用限流码（HTTP_STATUS_DESC 无 429），但 token_service
# 会把非 200 的 HTTP 状态码以字符串形式作为 resultCode 返回（token_service.py:246-249），
# 因此 HTTP 429 是当前唯一可用的限流来源，映射 RATE_LIMITED（retryable 由 result.py 计算）。
SDK_RESULT_CODE_MAP = {
    SDK_CODE_AUTH_REQUIRED: result_module.AUTH_REQUIRED,
    "200001": result_module.FAILED,
    "200002": result_module.FAILED,
    "200003": result_module.FAILED,
    "200004": result_module.FAILED,
    "200005": result_module.FAILED,
    "200006": result_module.UPSTREAM_TIMEOUT,
    "200007": result_module.UPSTREAM_CONTRACT_MISMATCH,
    "200008": result_module.UNKNOWN,
    "200009": result_module.FAILED,
    "200010": result_module.FAILED,
    "200011": result_module.FAILED,
    "200012": result_module.UPSTREAM_CONTRACT_MISMATCH,
    "200013": result_module.CAPABILITY_UNAVAILABLE,
    "200014": result_module.FAILED,
    "200015": result_module.SECURE_STORE_UNAVAILABLE,
    "200016": result_module.AUTH_REQUIRED,
    SDK_CODE_PENDING_UUID: result_module.AUTH_REQUIRED,
    SDK_CODE_SCOPE_CHANGED: result_module.AUTH_REQUIRED,
    "401": result_module.AUTH_EXPIRED,
    "403": result_module.SCOPE_DENIED,
    "429": result_module.RATE_LIMITED,
}

# 本地自建的错误说明：不回显下游 resultDesc / 原始响应，避免夹带身份材料。
SDK_RESULT_CODE_MESSAGES = {
    SDK_CODE_SUCCESS: "成功",
    SDK_CODE_AUTH_REQUIRED: "需要用户在浏览器完成授权",
    "200001": "授权登记参数 devId 非法",
    "200002": "授权登记参数 clientId 非法",
    "200003": "授权登记参数 skillId 非法",
    "200004": "授权登记参数 skillName 非法",
    "200005": "IDaaS 接口网络异常",
    "200006": "IDaaS 接口网络超时",
    "200007": "IDaaS 接口响应数据解析异常",
    "200008": "IDaaS 接口内部系统异常",
    "200009": "未找到可信设备标识文件",
    "200010": "读取可信设备标识文件失败",
    "200011": "IDaaS 配置下发失败",
    "200012": "IDaaS 配置下发数据不完整",
    "200013": "IDaaS SDK 加密能力不可用",
    "200014": "授权登记参数 strategyId 非法",
    "200015": "系统安全存储后端不可用",
    "200016": "本地未找到可用授权凭据，请先完成授权",
    SDK_CODE_PENDING_UUID: "本机授权会话尚未完成，请在浏览器完成授权后再确认",
    SDK_CODE_SCOPE_CHANGED: "云盘授权权限已变更，需要重新完成授权",
    "401": "IDaaS 授权已过期",
    "403": "IDaaS 授权范围不足",
    "429": "请求过于频繁，请稍后再试",
}

DEFAULT_SDK_MESSAGE = "IDaaS 接口返回未登记结果码"

_WHITESPACE_ONLY = re.compile(r"\s+")

_PEM_BLOCK_PATTERN = re.compile(r"-----BEGIN [A-Z ]*KEY-----.*?-----END [A-Z ]*KEY-----", re.S)

# 值的字符集刻意排除引号/空白/分隔符与 `:` `=` `&`，避免把紧随其后的下一个键名一并吃掉。
# 键名采用「词内包含敏感词」而非全等白名单：既能覆盖 ACCESS_TOKEN / MY_ACCESS_TOKEN /
# AUTH_TOKEN / SESSION_TOKEN / CREDIT_TOKEN 等变体，也解除了 `\b` 对前导下划线的限制。
_SENSITIVE_KEY_WORDS = r"token|secret|password|passwd|apikey|api_key|uuid|sign|ticket"

_KEY_VALUE_PATTERN = re.compile(
    r"(?i)([A-Za-z0-9_\-]*(?:" + _SENSITIVE_KEY_WORDS + r")[A-Za-z0-9_\-]*)"
    r'(\s*["\']?\s*[:=]\s*["\']?)'
    r"([^\"'\s,;:}\]&=]{1,})"
)

_DEVICE_KEY_PATTERN = re.compile(
    r"(?i)([A-Za-z0-9_\-]*(?:clawuid|claw_uid|deviceid|device_id)[A-Za-z0-9_\-]*)"
    r'(\s*["\']?\s*[:=]\s*["\']?)'
    r"([0-9A-Za-z._\-]{1,})"
)

_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{8,}")

_AUTO_NEXT_ACTION = object()

_TRACE_STATE = {"current": None}


@dataclass(frozen=True)
class SdkModules:
    """动态加载后的 SDK 公共模块句柄。"""

    token_service: object
    keychain_store: object


# ---------------------------------------------------------------------------
# SDK 动态加载
# ---------------------------------------------------------------------------


def sdk_dir():
    """随包发布的 IDaaS SDK 版本目录。"""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), SDK_DIR_NAME
    )


class _NullBuffer:
    """导入期占位 stdout 的 buffer。"""

    def write(self, data):
        return 0

    def flush(self):
        return None


class _ImportStdoutGuard:
    """SDK 导入期占位 stdout。

    token_service 顶层会执行 ``sys.stdout.reconfigure(encoding="utf-8")``；直接导入会把
    真实 stdout 的编码改掉，因此导入期间用占位流吞掉输出并让 reconfigure 变成空操作。
    """

    encoding = "utf-8"
    errors = "replace"

    def write(self, text):
        return len(text) if isinstance(text, str) else 0

    def flush(self):
        return None

    def isatty(self):
        return False

    def reconfigure(self, **_kwargs):
        return None

    @property
    def buffer(self):
        return _NullBuffer()


def _ensure_sdk_directory():
    """核验随包 SDK 目录存在并返回；不修改模块搜索路径。"""
    directory = sdk_dir()
    if not os.path.isdir(directory):
        raise make_error(
            result_module.CAPABILITY_UNAVAILABLE,
            "IDaaS SDK 未随包发布，auth 命令不可用",
            next_action=None,
        )
    return directory


# SKILL-036：SDK 模块间以绝对名互相导入（token_service -> device_info/keychain_store）。
# 按显式文件路径加载时，必须先把被依赖模块装入 sys.modules，SDK 内部的 import 才能命中。
_SDK_MODULE_DEPENDENCIES = {
    TOKEN_SERVICE_MODULE: (DEVICE_INFO_MODULE, KEYCHAIN_STORE_MODULE),
    KEYCHAIN_STORE_MODULE: (),
    DEVICE_INFO_MODULE: (),
}


def _import_sdk_module(name):
    """按显式文件路径加载 SDK 模块，全程不修改模块搜索路径。

    依赖先于被依赖者加载并预注册进 sys.modules，令 SDK 内部的绝对导入
    （如 ``import keychain_store``）命中已加载对象，而不是去搜索模块路径。
    """
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    directory = _ensure_sdk_directory()
    for dependency in _SDK_MODULE_DEPENDENCIES.get(name, ()):
        _import_sdk_module(dependency)
    import importlib.util

    path = os.path.join(directory, name + ".py")
    original_stdout = sys.stdout
    sys.stdout = _ImportStdoutGuard()
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError("无法定位 IDaaS SDK 模块文件: %s" % name)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    except Exception as err:  # 缺依赖或不兼容一律按能力不可用处理，不向上抛堆栈
        sys.modules.pop(name, None)
        diagnostic("加载 IDaaS SDK 模块 %s 失败: %s" % (name, err.__class__.__name__))
        raise make_error(
            result_module.CAPABILITY_UNAVAILABLE,
            "IDaaS SDK 无法加载（%s），请确认已安装 SDK 依赖 requests/pycryptodome/keyring" % name,
            next_action=None,
        )
    finally:
        sys.stdout = original_stdout


def load_keychain_store():
    """仅加载 keychain_store（KMS-05 状态查询不需要 requests/pycryptodome）。"""
    return _import_sdk_module(KEYCHAIN_STORE_MODULE)


def load_token_service():
    """加载 token_service 及其同目录依赖。"""
    module = _import_sdk_module(TOKEN_SERVICE_MODULE)
    _import_sdk_module(KEYCHAIN_STORE_MODULE)
    return module


def load_sdk():
    """同时加载 token_service 与 keychain_store，返回可复用句柄。"""
    return SdkModules(token_service=load_token_service(), keychain_store=load_keychain_store())


def reset_sdk_cache():
    """受控测试用：清掉已缓存的 SDK 模块。"""
    for name in (TOKEN_SERVICE_MODULE, KEYCHAIN_STORE_MODULE, DEVICE_INFO_MODULE):
        sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# traceId 与诊断输出
# ---------------------------------------------------------------------------


def new_trace_id():
    return str(uuid.uuid4())


def current_trace_id():
    return _TRACE_STATE["current"]


@contextlib.contextmanager
def trace_scope(trace_id=None):
    """把 traceId 绑定到当前调用链，便于回灌 setConfig 与下游请求。"""
    previous = _TRACE_STATE["current"]
    _TRACE_STATE["current"] = trace_id or new_trace_id()
    try:
        yield _TRACE_STATE["current"]
    finally:
        _TRACE_STATE["current"] = previous


def diagnostic(message):
    """诊断日志只写 stderr 且脱敏；stdout 永远只有一个 JSON 文档。"""
    stream = sys.stderr
    if stream is None:
        return
    try:
        stream.write("[cmcloud-auth] %s\n" % desensitize_text(str(message)))
        stream.flush()
    except Exception:
        pass


class _SanitizingStream:
    """按行缓冲 + 脱敏后再写入目标流，避免半截敏感串被拆包写出去。"""

    def __init__(self, target):
        self._target = target
        self._pending = ""
        self.encoding = getattr(target, "encoding", None) or "utf-8"
        self.errors = getattr(target, "errors", None) or "replace"

    def write(self, text):
        if not isinstance(text, str):
            text = str(text)
        self._pending += text
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self._emit(line + "\n")
        return len(text)

    def flush(self):
        if self._pending:
            self._emit(self._pending)
            self._pending = ""
        try:
            self._target.flush()
        except Exception:
            pass

    def isatty(self):
        return False

    def reconfigure(self, **_kwargs):
        return None

    @property
    def buffer(self):
        return _SanitizingBuffer(self)

    def _emit(self, text):
        if not text.strip():
            return
        try:
            self._target.write(desensitize_text(text))
        except Exception:
            pass


class _SanitizingBuffer:
    """SDK 若直接写 stdout.buffer 时同样经脱敏管道。"""

    def __init__(self, stream):
        self._stream = stream

    def write(self, data):
        if isinstance(data, (bytes, bytearray)):
            self._stream.write(bytes(data).decode("utf-8", "replace"))
        else:
            self._stream.write(str(data))
        return len(data)

    def flush(self):
        self._stream.flush()


@contextlib.contextmanager
def sdk_output_capture(command=None):
    """SDK 调用期 stdout 隔离：SDK 的 print/_log 合并进 stderr 且脱敏。"""
    target = sys.stderr if sys.stderr is not None else io.StringIO()
    sanitizer = _SanitizingStream(target)
    diagnostic("开始调用 SDK 接口: %s" % (command or "unknown"))
    with contextlib.redirect_stdout(sanitizer):
        try:
            yield sanitizer
        finally:
            sanitizer.flush()


def call_sdk(method, *args, command=None, **kwargs):
    """在 stdout 隔离下调用单个 SDK 公共接口。"""
    with sdk_output_capture(command or getattr(method, "__name__", None)):
        return method(*args, **kwargs)


def configure_sdk(profile, *, modules=None, trace_id=None):
    """按受控 profile 初始化 SDK 全局配置（日志恒关闭，私有目录按 profile+skill 隔离）。

    storage_backend=keychain 且系统 Keychain 不可用时直接返回 SECURE_STORE_UNAVAILABLE，
    不做任何明文降级。
    """
    idaas_profile.require_profile_storage(profile)
    resolved_trace = trace_id or current_trace_id() or new_trace_id()
    if profile.uses_keychain:
        # 交给 SDK 之前再做一次幂等自检：SDK 的 keychain 槽位必须落在本智能体命名空间内。
        # file 后端不注入包装器，也不解析系统凭据库（见 idaas_profile.load_profile）。
        from cmcloud_auth import scoped_keyring

        scoped_keyring.ensure_installed()
    sdk = modules or load_sdk()
    with sdk_output_capture("setConfig"):
        sdk.token_service.setConfig(**profile.setconfig_kwargs())
    diagnostic(
        "setConfig profileRef=%s skillId=%s traceId=%s"
        % (profile.profile_ref, profile.skill_id, resolved_trace)
    )
    return sdk


# ---------------------------------------------------------------------------
# 取票与授权入口（auth.login 与业务取票共用同一套解析）
# ---------------------------------------------------------------------------

_AUTH_PAGE_SCHEMES = ("https", "http")
_HOST_PATTERN = re.compile(r"\A[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?\Z")


def get_token_arguments(auth_profile):
    """getToken 入参：登记参数来自受控 profile，clawUid/uuid 由 SDK 内部解析。"""
    arguments = dict(auth_profile.registration_fields())
    arguments["clawUid"] = None
    arguments["uuid_param"] = None
    return arguments


def acquire_token(sdk, auth_profile, *, command, credit_token=None, negotiate=False, forced_auth=False):
    """取票；`130088`（本机待授权会话尚未完成）必须**保持同一次会话**，只重放入口。

    130088 的语义是"你给我的这个 uuid，用户在浏览器里还没授权完成"，即正常的待授权状态
    （实测：刚下发、绝无可能被授权的 uuid 立即复取同样是 130088），**不是**"会话已坏"。
    一旦清掉本机 uuid 再取票，服务端会下发另一个全新会话与新入口，用户刚完成的那次授权
    就再也认领不回来了（表现为"授权完成后仍被要求重新授权"，且每次确认都换一次会话）。

    因此：本机仍有可重放的入口（且与当前 uuid 同源）时，把入口补回本次回执，uuid 槽原样
    不动；只有确实没有入口可重放（历史遗留会话、超出重放窗口、入口与当前 uuid 不同源）
    才作废该会话并重新下发入口，保证任何情况下都不会卡死在同一个会话上。
    """
    credential_state = sdk_file_credential_state(sdk, auth_profile)
    if credential_state in _SDK_CREDENTIAL_FAILURE_MESSAGES and command != "auth.login" and not forced_auth:
        raise make_error(
            result_module.FAILED,
            _SDK_CREDENTIAL_FAILURE_MESSAGES[credential_state],
            next_action=None,
        )
    arguments = get_token_arguments(auth_profile)
    if forced_auth:
        # 用户明确重新授权时使用当前登记范围，不沿用历史协商范围。
        # 强制发起只调用一次，不走普通取票的 scope 协商或清 uuid 重试。
        arguments["forcedAuth"] = True
        return call_sdk(sdk.token_service.getToken, command=command, **arguments)
    saved_scope_ids = _load_saved_scope_ids(auth_profile)
    if saved_scope_ids:
        arguments["scopeIds"] = saved_scope_ids
    if negotiate:
        negotiated = _query_scope_ids_via_validate(
            sdk, auth_profile, credit_token=credit_token
        )
        if negotiated:
            arguments["scopeIds"] = negotiated
            _store_scope_ids(auth_profile, negotiated, source="validate")
            diagnostic("103082 换票：按 validate 查询到的 scopeIds 替换取票入参")
    raw = call_sdk(sdk.token_service.getToken, command=command, **arguments)
    # auth.login is deliberately allowed to obtain a fresh authorization session.  Preserve the
    # preflight state so its wrapper can explain why a new authorization is needed rather than
    # presenting a damaged local credential as an ordinary "not logged in" outcome.
    if credential_state in _SDK_CREDENTIAL_FAILURE_MESSAGES and isinstance(raw, dict):
        raw["_cmcloudCredentialFileState"] = credential_state
    if not isinstance(raw, dict):
        return raw
    if negotiate_scope_ids(
        raw, arguments, sdk=sdk, auth_profile=auth_profile, credit_token=credit_token
    ):
        _store_scope_ids(auth_profile, arguments.get("scopeIds"), source="negotiate")
        diagnostic("103082 权限已变更：按服务端下发的 scopeIds 重新取票（单次协商重试）")
        raw = call_sdk(sdk.token_service.getToken, command=command, **arguments)
        if credential_state in _SDK_CREDENTIAL_FAILURE_MESSAGES and isinstance(raw, dict):
            raw["_cmcloudCredentialFileState"] = credential_state
        if not isinstance(raw, dict):
            return raw
    if normalize_sdk_code(raw.get("resultCode")) != SDK_CODE_PENDING_UUID:
        return raw
    if attach_cached_pending_entry(sdk, auth_profile, raw):
        diagnostic("本机待授权会话尚未完成，重放同一次会话的授权入口（不作废该会话）")
        return raw
    if not _clear_pending_uuid(sdk, auth_profile):
        return raw
    return call_sdk(sdk.token_service.getToken, command=command, **arguments)


def negotiate_scope_ids(raw, arguments, *, sdk=None, auth_profile=None, credit_token=None):
    """处理 103082：以服务端要求的 scopeIds 替换本次取票入参，最多协商一次。"""
    if not isinstance(raw, dict):
        return False
    if normalize_sdk_code(raw.get("resultCode")) != SDK_CODE_SCOPE_CHANGED:
        return False
    negotiated = _scope_ids_from_desc(raw.get("resultDesc"))
    if not negotiated and sdk is not None and auth_profile is not None:
        negotiated = _query_scope_ids_via_validate(
            sdk, auth_profile, credit_token=credit_token
        )
    if not negotiated or negotiated == _arguments_scope_ids(arguments):
        return False
    arguments["scopeIds"] = negotiated
    return True


def _scope_ids_from_desc(document):
    """从字符串化 JSON 或对象形态的 desc 中读取 scopeIds。"""
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except ValueError:
            return None
    if not isinstance(document, dict):
        return None
    return _normalized_scope_ids(document.get("scopeIds"))


def _normalized_scope_ids(value):
    if isinstance(value, list):
        parts = [str(item).strip() for item in value]
    elif isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
    else:
        return None
    normalized = ",".join(part for part in parts if part)
    return normalized or None


def _query_scope_ids_via_validate(sdk, auth_profile, *, credit_token=None):
    """用当前短票据调用 IDaaS validate 查询服务端要求的 scopeIds。"""
    token = _ticket_token(credit_token) or _stored_credit_token(auth_profile)
    if not token:
        diagnostic("103082 未携带 scopeIds 且本机没有可用短票据，跳过 validate 查询")
        return None
    token_service = getattr(sdk, "token_service", None)
    base_url = getattr(token_service, "TOKEN_SERVICE_BASE_URL", None)
    interface_version = getattr(token_service, "INTERFACE_VERSION", None)
    client_id = getattr(auth_profile, "client_id", None)
    if not isinstance(base_url, str) or not base_url.strip():
        diagnostic("SDK 未提供 validate 请求地址，跳过 validate 查询")
        return None
    if not isinstance(interface_version, str) or not interface_version.strip():
        diagnostic("SDK 未提供接口版本，跳过 validate 查询")
        return None
    if not isinstance(client_id, str) or not client_id.strip():
        diagnostic("profile 未登记 clientId，跳过 validate 查询")
        return None
    url = base_url.rstrip("/") + VALIDATE_PATH
    parsed = urllib.parse.urlsplit(url)
    base_parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != "https" or parsed.netloc != base_parsed.netloc:
        diagnostic("validate 请求地址校验失败，跳过 validate 查询")
        return None
    trace_id = current_trace_id() or new_trace_id()
    timestamp = _validate_timestamp()
    sign = hashlib.md5(
        (client_id + interface_version + timestamp + trace_id).encode("utf-8")
    ).hexdigest()
    headers = {
        "clientId": client_id,
        "timestamp": timestamp,
        "interfaceVersion": interface_version,
        "traceId": trace_id,
        "Content-Type": "application/json;charset=UTF-8",
    }
    body = {"creditToken": token, "type": VALIDATE_TYPE, "sign": sign}
    try:
        document = _http_post_json(url, body, headers, VALIDATE_TIMEOUT_SECONDS)
    except Exception as err:
        diagnostic("validate 查询失败(%s)，按原协商路径兜底" % err.__class__.__name__)
        return None
    code = normalize_sdk_code(document.get("resultCode"))
    if code != SDK_CODE_SCOPE_CHANGED:
        diagnostic("validate 未返回 103082(code=%s)，按原协商路径兜底" % (code or "(缺失)"))
        return None
    scope_ids = _scope_ids_from_desc(document.get("desc"))
    if not scope_ids:
        diagnostic("validate 回执未携带 scopeIds，按原协商路径兜底")
    return scope_ids


def _load_saved_scope_ids(auth_profile):
    """读取此前协商并保存的 scopeIds；读取失败时退回 profile 登记值。"""
    try:
        from cmcloud_auth import credential_store

        document = credential_store.load(auth_profile, credential_store.KIND_SCOPE_IDS)
    except Exception as err:
        diagnostic("读取保存的 scopeIds 失败(%s)，本次按登记值取票" % err.__class__.__name__)
        return None
    if not isinstance(document, dict):
        return None
    return _normalized_scope_ids(document.get("scopeIds"))


def _store_scope_ids(auth_profile, scope_ids, *, source):
    """保存或更新服务端协商出的 scopeIds；失败不阻断本次取票。"""
    normalized = _normalized_scope_ids(scope_ids)
    if not normalized:
        return
    try:
        from cmcloud_auth import credential_store

        credential_store.save(
            auth_profile,
            credential_store.KIND_SCOPE_IDS,
            {"scopeIds": normalized, "source": source, "cachedAt": _validate_timestamp()},
        )
        diagnostic("scopeIds 已保存（source=%s），后续取票使用保存值" % source)
    except Exception as err:
        diagnostic("保存 scopeIds 失败(%s)，本次仍用协商值取票" % err.__class__.__name__)


def _arguments_scope_ids(arguments):
    if not isinstance(arguments, dict):
        return None
    return _normalized_scope_ids(arguments.get("scopeIds"))


def _stored_credit_token(auth_profile):
    try:
        from cmcloud_auth import ticket_provider

        ticket = ticket_provider.CredentialTicketStore().load(auth_profile)
    except Exception as err:
        diagnostic("读取本地短票据失败(%s)，validate 查询跳过" % err.__class__.__name__)
        return None
    return _ticket_token(ticket.get("creditToken") if isinstance(ticket, dict) else None)


def _ticket_token(value):
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _validate_timestamp():
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d%H%M%S%f")[:-3]


_VALIDATE_OPENER = None


def _validate_opener():
    """validate 是异常恢复路径；延迟加载 TLS/HTTP，避免影响普通命令启动。"""
    global _VALIDATE_OPENER
    if _VALIDATE_OPENER is not None:
        return _VALIDATE_OPENER
    import ssl
    import urllib.request

    class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    _VALIDATE_OPENER = urllib.request.build_opener(
        _NoRedirectHandler(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    return _VALIDATE_OPENER


def _http_post_json(url, payload, headers, timeout):
    import urllib.request

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    response = _validate_opener().open(request, timeout=timeout)
    try:
        status = response.getcode()
        raw = response.read()
    finally:
        response.close()
    if not isinstance(status, int) or not 200 <= status < 300:
        raise ValueError("validate 响应状态码不可用: %s" % status)
    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict):
        raise ValueError("validate 响应不是 JSON 对象")
    return document


def has_auth_entry(raw):
    """回执里是否带可直接展示的授权入口（103001 现取现发，130088 复用缓存入口）。"""
    if not isinstance(raw, dict):
        return False
    data = raw.get("data")
    if not isinstance(data, dict):
        return False
    url = data.get("authPageUrl")
    return isinstance(url, str) and bool(url.strip())


def is_pending_session(raw_code):
    """本机待授权会话尚未完成（`130088`）：服务端不下发入口，入口由本机缓存重放。"""
    return normalize_sdk_code(raw_code) == SDK_CODE_PENDING_UUID


def attach_cached_pending_entry(sdk, auth_profile, raw):
    """把上次下发的授权入口补回 `130088` 回执；命中返回 True（会话保持不变）。

    同源校验按后端读当前 uuid（keychain 读 ``<skillId>:uuid`` 槽，file 读 ``auth_info_ydrz.json``）：
    缓存入口若与当前 uuid 指向不同会话，重放出去的入口即使被用户完成也无法被本机认领，
    故按"没有入口可重放"处理并让调用方重新下发入口。读不到 uuid 时按无法判定处理、
    照常重放（不阻断）。
    """
    if has_auth_entry(raw):
        return True
    from cmcloud_auth import ticket_provider

    entry = ticket_provider.load_pending_entry(auth_profile)
    if entry is None:
        return False
    recorded = entry.get("sessionFingerprint")
    current = _current_pending_uuid_fingerprint(sdk, auth_profile)
    if current is not None and recorded and current != recorded:
        diagnostic("缓存入口与当前待授权 uuid 不同源，改为重新下发授权入口")
        return False
    raw["data"] = {"authPageUrl": entry["authPageUrl"], "sessionRef": recorded or ""}
    return True


def _file_pending_uuid(sdk, auth_profile):
    """file 后端：读 SDK 写在 ``auth_info_ydrz.json`` 里的待授权 uuid；读不到返回 None。

    与 keychain 分支的 ``<skillId>:uuid`` 槽等价——SDK 在 file 后端把 uuid 写在该文件里，
    这里按同一套信封解密后取 ``uuid`` 字段。文件缺失/损坏/解密失败一律返回 None
    （按"无法判定"处理，由调用方决定是否照常重放）。
    """
    from cmcloud_auth import secure_file

    getter = getattr(getattr(sdk, "token_service", None), "get_auth_info_path", None)
    if not callable(getter):
        return None
    try:
        path = getter(auth_profile.skill_id)
    except Exception as err:
        diagnostic("读取 auth_info 路径失败: %s" % err.__class__.__name__)
        return None
    document, state = secure_file.read_secure_bundle_state(path)
    if state not in secure_file.STATE_READABLE:
        diagnostic("SDK 授权状态文件不可读: state=%s" % state)
        return None
    if not isinstance(document, dict):
        return None
    value = document.get("uuid")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


_SDK_CREDENTIAL_FAILURE_MESSAGES = {
    "corrupt": "本地授权凭据文件已损坏；请先通过 auth login 获取新的授权，不要将其视为未登录",
    "fingerprint_mismatch": "本地授权凭据与当前设备指纹不符（机器标识变化或 SDK 指纹方案升级）；请在当前环境重新授权",
    "version_unsupported": "本地授权凭据格式不受当前版本支持；请升级或重新授权",
}


def sdk_file_credential_state(sdk, auth_profile):
    """读取 SDK 文件后端的授权文件状态，而不改动 SDK 自身的兼容读取逻辑."""
    if getattr(auth_profile, "storage_backend", None) != idaas_profile.STORAGE_BACKEND_FILE:
        return None
    from cmcloud_auth import secure_file

    getter = getattr(getattr(sdk, "token_service", None), "get_auth_info_path", None)
    if not callable(getter):
        return None
    try:
        path = getter(auth_profile.skill_id)
    except Exception as err:
        diagnostic("读取 SDK auth_info 路径失败: %s" % err.__class__.__name__)
        return None
    _document, state = secure_file.read_secure_bundle_state(path)
    return state


def _current_pending_uuid_fingerprint(sdk, auth_profile):
    """本机当前待授权 uuid 的指纹；读不到返回 None（无法判定）。

    keychain 后端读 ``<skillId>:uuid`` 槽；file 后端读 ``auth_info_ydrz.json``。
    """
    backend = getattr(auth_profile, "storage_backend", None)
    if backend == idaas_profile.STORAGE_BACKEND_KEYCHAIN:
        getter = getattr(getattr(sdk, "keychain_store", None), "get", None)
        value = None
        if callable(getter):
            try:
                value = getter("%s:uuid" % auth_profile.skill_id)
            except Exception:
                value = None
    elif backend == idaas_profile.STORAGE_BACKEND_FILE:
        value = _file_pending_uuid(sdk, auth_profile)
    else:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return fingerprint(value.strip())


def _clear_pending_uuid(sdk, auth_profile):
    """作废本机该 skill 的待授权 uuid；清理无效时返回 False（不再重试）。

    只在**没有入口可重放**时走到这里（见 acquire_token）：目的是重新拿到一个带
    authPageUrl 的新会话，而不是"清理陈旧会话"。清理后不应把旧 uuid 当成已清掉：
    "写失败"与"写成功"必须区分——底层拒写（keychain 不可用、后端只读）时若当成已清理，
    会带着旧 uuid 重试并再次得到 130088，掩盖真实故障。keychain 后端额外做一次回读。
    """
    save_auth_info = getattr(sdk.token_service, "save_auth_info", None)
    if not callable(save_auth_info):
        diagnostic("SDK 未提供 save_auth_info，无法清理未完成授权 uuid")
        return False
    try:
        cleared = call_sdk(
            save_auth_info,
            command="auth.clearPendingUuid",
            skillId=auth_profile.skill_id,
            clear_uuid=True,
        )
    except Exception as err:  # 清理失败只降级为"不重试"，不得让未预期异常冒泡
        diagnostic("清理未完成授权 uuid 失败: %s" % err.__class__.__name__)
        return False
    if cleared is False:
        diagnostic("清理未完成授权 uuid 被底层拒绝，已放弃重试")
        return False
    if not _pending_uuid_absent(sdk, auth_profile):
        diagnostic("清理后仍能读到未完成授权 uuid，已放弃重试")
        return False
    return True


def _pending_uuid_absent(sdk, auth_profile):
    """回读校验：当前后端读不到 uuid 时按"已不存在"处理；读失败按无法判定处理、不阻断。"""
    backend = getattr(auth_profile, "storage_backend", None)
    if backend == idaas_profile.STORAGE_BACKEND_KEYCHAIN:
        getter = getattr(getattr(sdk, "keychain_store", None), "get", None)
        if not callable(getter):
            return True
        try:
            value = getter("%s:uuid" % auth_profile.skill_id)
        except Exception:
            return True
    elif backend == idaas_profile.STORAGE_BACKEND_FILE:
        value = _file_pending_uuid(sdk, auth_profile)
    else:
        return True
    return not (isinstance(value, str) and value.strip())


def normalize_auth_hosts(raw):
    """授权页主机白名单归一：只接受非空字符串，统一小写去点。"""
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = (raw,)
    hosts = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise result_module.ResultError("allowed_auth_hosts 只允许非空字符串")
        hosts.append(item.strip().strip(".").lower())
    return tuple(hosts)


def validate_auth_page_url(raw_url, allowed_hosts=None):
    """校验授权入口来源：协议、主机、端口与凭据结构，可选受控主机白名单。"""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise _contract_error("取票响应缺少授权入口地址，已停止继续调用")
    url = raw_url.strip()
    if any(ch.isspace() for ch in url) or any(ord(ch) < 0x20 for ch in url):
        raise _contract_error("授权入口地址含非法字符，已拒绝继续调用")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in _AUTH_PAGE_SCHEMES:
        raise _contract_error("授权入口地址协议不受支持，已拒绝继续调用")
    host = parsed.hostname
    if not host:
        raise _contract_error("授权入口地址缺少主机名，已拒绝继续调用")
    if parsed.username is not None or parsed.password is not None:
        raise _contract_error("授权入口地址不得携带用户名或密码，已拒绝继续调用")
    if _HOST_PATTERN.match(host) is None and not _is_ip_literal(host):
        raise _contract_error("授权入口地址主机名不合法，已拒绝继续调用")
    try:
        parsed.port
    except ValueError:
        raise _contract_error("授权入口地址端口不合法，已拒绝继续调用")
    hosts = normalize_auth_hosts(allowed_hosts)
    if hosts and not _host_allowed(host, hosts):
        raise _contract_error("授权入口地址不在受控主机白名单内，已拒绝继续调用")
    return url


def _is_ip_literal(host):
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _host_allowed(host, allowed_hosts):
    target = host.lower()
    for item in allowed_hosts:
        if target == item or target.endswith("." + item):
            return True
    return False


def challenge_nonce(session_ref):
    """把 SDK 授权会话标识并入一次性随机因子：绑定会话且不回显 uuid 原文。"""
    session_part = fingerprint(session_ref) if session_ref else "nosession"
    return "%s.%s" % (session_part, uuid.uuid4().hex)


def agent_platform_payload(auth_profile):
    """当前智能体平台摘要：只给编码与名称，不含本机安装路径等本机信息。"""
    from cmcloud_auth import agent_scope

    code = getattr(auth_profile, "agent_code", None) or agent_scope.FALLBACK_AGENT_CODE
    return {
        "agentCode": code,
        "agentName": agent_scope.agent_display_name(code),
    }


def pending_authorization_payload(auth_profile, raw, *, allowed_hosts=None, claw_uid=None):
    """待授权载荷：校验授权入口来源，返回受控 challengeRef 与授权入口（无凭据）。

    `103001` 用服务端本次下发的 uuid、`130088` 重放时用缓存里的会话指纹作会话引用；
    两者都只以指纹形式参与 challengeRef 绑定，uuid 原文既不落库也不外发。
    """
    data = raw.get("data") if isinstance(raw, dict) else None
    data = data if isinstance(data, dict) else {}
    auth_page_url = validate_auth_page_url(data.get("authPageUrl"), allowed_hosts)
    payload = {
        "profileRef": idaas_profile.profile_ref(auth_profile),
        "authorizationState": AUTHORIZATION_STATE_PENDING,
        "challengeRef": challenge_ref(
            auth_profile,
            skill_id=auth_profile.skill_id,
            claw_uid=claw_uid,
            scope=auth_profile.scope_ids,
            nonce=challenge_nonce(session_ref(data)),
        ),
        "authPageUrl": auth_page_url,
    }
    # 链接与二维码均使用 SDK 返回的原授权入口，宿主能内联图片就用 data URI，
    # 只能引用本地文件就用路径。两个字段都可缺省（生成失败不影响短链下发）；
    # 本载荷只在"需要授权"时构造，成功回执不带入口，因此也不会带上二维码。
    payload.update(qr_image.render_entry_qr(auth_page_url))
    # 授权入口按智能体隔离：回执里给出当前平台，便于用户确认"这次授权落在哪个智能体"
    payload.update(agent_platform_payload(auth_profile))
    return payload


def session_ref(data):
    """待授权会话引用：服务端本次下发的 uuid，或重放时缓存里的会话指纹。"""
    if not isinstance(data, dict):
        return None
    return data.get("sessionRef") or data.get("uuid")


def _contract_error(message):
    """上游响应与冻结契约不匹配：暂停取票能力，不透传给 Agent。"""
    return make_error(result_module.UPSTREAM_CONTRACT_MISMATCH, message)


# ---------------------------------------------------------------------------
# SDK 结果码 → 统一错误契约
# ---------------------------------------------------------------------------


def normalize_sdk_code(raw_code):
    """SDK 结果码归一为字符串；None/缺失归一为空串（表外 → UNKNOWN）。"""
    if raw_code is None:
        return ""
    return str(raw_code).strip()


def is_sdk_success(raw_code):
    return normalize_sdk_code(raw_code) == SDK_CODE_SUCCESS


def is_auth_entry_result(raw_code):
    """服务端本次是否**现取现发**了授权入口（仅 `103001` 成立）。

    只表示"服务端本次回执里带 authPageUrl"。它**不是**"是否需要授权"的判据：`130088`
    （本机待授权会话尚未完成）与 `200016`（本地无可用凭据）同样需要授权，但不带入口——
    判断"要不要拉起授权"请用 `needs_authorization()` 或 `map_sdk_result_code()`；
    `130088` 的入口由 `acquire_token` 用本机缓存重放（回执里同样会有 authPageUrl），
    因此判断"回执里有没有可直接展示的入口"请用 `has_auth_entry()`。
    """
    return normalize_sdk_code(raw_code) == SDK_CODE_AUTH_REQUIRED


def needs_authorization(raw_code):
    """原始码是否表示「需要完成/重新完成授权」。

    从码表派生，因此天然覆盖 `103001`、`130088`、`200016`（→ `AUTH_REQUIRED`）与
    `401`（→ `AUTH_EXPIRED`）；网络、签名、IP、时钟类失败不在其中，不得据此拉起授权页。
    """
    return map_sdk_result_code(raw_code) in (
        result_module.AUTH_REQUIRED,
        result_module.AUTH_EXPIRED,
    )


def map_sdk_result_code(raw_code):
    """SDK 原始码映射：成功码返回 None；表外/缺失归一为 UNKNOWN。"""
    code = normalize_sdk_code(raw_code)
    if code == SDK_CODE_SUCCESS:
        return None
    return SDK_RESULT_CODE_MAP.get(code, result_module.UNKNOWN)


def diagnostic_sdk_result(command, raw):
    """失败回执的脱敏诊断：原始码、映射结果与 SDK 自述（含失败阶段线索）。

    只写 stderr：让"配置下发失败 / preAuth 失败 / 网络异常"这类阶段信息可排障，同时
    SDK 自述先脱敏再截断，凭据不会进日志。
    """
    if not isinstance(raw, dict):
        diagnostic("%s SDK 回执结构异常: %s" % (command, type(raw).__name__))
        return
    code = normalize_sdk_code(raw.get("resultCode"))
    desc = desensitize_text(str(raw.get("resultDesc") or ""))[:200]
    diagnostic(
        "%s SDK 回执 code=%s mapped=%s desc=%s"
        % (command, code or "(缺失)", map_sdk_result_code(code), desc or "(无)")
    )


def sdk_message(raw_code, message=None):
    """错误说明优先用本地自建文案；不使用下游 resultDesc，避免夹带原始响应。"""
    if message:
        return safe_message(message)
    return SDK_RESULT_CODE_MESSAGES.get(normalize_sdk_code(raw_code), DEFAULT_SDK_MESSAGE)


def next_action_for(error_code):
    """确认类下发 operation.confirm，需要授权时下发 auth.login。

    AUTH_REQUIRED 表示本轮没有可用票据：编排层必须取一次授权入口并展示，故下发
    auth.login；入口本身由 auth.login 的回执携带（该回执显式传 next_action=None，
    不重复下发，避免自循环）。
    """
    code = result_module.normalize_error_code(error_code)
    if code in (result_module.AUTH_REQUIRED, result_module.AUTH_EXPIRED):
        return result_module.NEXT_ACTION_AUTH_LOGIN
    if code == result_module.CONFIRMATION_REQUIRED:
        return result_module.NEXT_ACTION_OPERATION_CONFIRM
    return None


def make_error(
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
    """统一错误构造：表外码归一为 UNKNOWN，nextAction 默认按契约推导。"""
    normalized = result_module.normalize_error_code(code)
    return result_module.ExecutionError(
        normalized,
        safe_message(message),
        retryable=retryable,
        operation_type=operation_type,
        binding_type=binding_type,
        next_action=next_action_for(normalized) if next_action is _AUTO_NEXT_ACTION else next_action,
        data=data,
        counts=counts,
        page=page,
    )


def error_from_sdk(
    raw_code,
    *,
    message=None,
    retryable=None,
    operation_type=None,
    binding_type=None,
    data=None,
):
    """把 SDK 结果码映射为统一 ExecutionError；成功码不得走此入口。"""
    code = normalize_sdk_code(raw_code)
    mapped = map_sdk_result_code(code)
    if mapped is None:
        raise result_module.ResultError("SDK 成功码不能构造错误信封")
    return make_error(
        mapped,
        sdk_message(code, message),
        retryable=retryable,
        operation_type=operation_type,
        binding_type=binding_type,
        data=data,
    )


def operation_type(value):
    """operation_type 传参封装：只允许 result.py 登记的 read / write（None 表示不指定）。"""
    if value is None:
        return None
    if value not in result_module.OPERATION_TYPES:
        raise result_module.ResultError("operation_type 必须是 read 或 write")
    return value


def binding_type(value):
    """binding_type 传参封装：relationship（关系类）/ verification（签名·IP·时钟类）。"""
    if value is None:
        return None
    if value not in result_module.BINDING_TYPES:
        raise result_module.ResultError("binding_type 必须是 relationship 或 verification")
    return value


# ---------------------------------------------------------------------------
# 统一信封构造
# ---------------------------------------------------------------------------


def build_error(code, message, retryable=None):
    """复用 result.build_error：对外错误对象只有 code/message/retryable。"""
    return result_module.build_error(code, safe_message(message), retryable)


def error_envelope(command, error, trace_id=None):
    """把 ExecutionError 转为九字段信封；nextAction 由 error 自身携带。

    `make_error` 建错时已按错误码算出 nextAction（含 AUTH_REQUIRED→auth.login），
    显式传 None 的调用方（如退出账号、auth.login 回执）必须保持 null，故此处不覆盖。
    """
    return result_module.envelope_from_error(command, error, trace_id=trace_id)


def success_envelope(command, data=None, *, counts=None, page=None, trace_id=None):
    return result_module.Envelope(
        command, result_module.STATUS_SUCCESS, data=data, counts=counts, page=page, trace_id=trace_id
    )


def empty_envelope(command, data=None, *, counts=None, page=None, trace_id=None):
    return result_module.Envelope(
        command, result_module.STATUS_EMPTY, data=data, counts=counts, page=page, trace_id=trace_id
    )


def confirmation_envelope(command, data=None, *, message=None, trace_id=None):
    """CONFIRMATION_REQUIRED 信封：nextAction 固定 operation.confirm。"""
    return result_module.Envelope(
        command,
        result_module.STATUS_CONFIRMATION_REQUIRED,
        data=data,
        error=build_error(result_module.CONFIRMATION_REQUIRED, message or "该操作需要可信确认后才能执行"),
        next_action=result_module.NEXT_ACTION_OPERATION_CONFIRM,
        trace_id=trace_id,
    )


def auth_required_envelope(command, data=None, *, message=None, trace_id=None,
                           next_action=_AUTO_NEXT_ACTION):
    """AUTH_REQUIRED 信封。

    缺省下发 nextAction=auth.login（业务命令用：本轮唯一动作是取授权入口）；
    auth.login 自身的回执携带授权入口，调用方显式传 next_action=None 结束本轮，
    否则会形成 auth.login → auth.login 的自循环。
    """
    if next_action is _AUTO_NEXT_ACTION:
        next_action = next_action_for(result_module.AUTH_REQUIRED)
    return result_module.Envelope(
        command,
        result_module.STATUS_AUTH_REQUIRED,
        data=data,
        error=build_error(result_module.AUTH_REQUIRED, message or "需要用户完成授权"),
        next_action=next_action,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# challengeRef / operationRef / planHash（与 profile·skill·clawUid·scope 绑定）
# ---------------------------------------------------------------------------


def normalize_scope(scope):
    """scope 归一为去重排序的字符串元组，保证绑定哈希稳定。"""
    if scope is None:
        return ()
    if isinstance(scope, str):
        values = [scope]
    elif isinstance(scope, (list, tuple, set, frozenset)):
        values = [str(item) for item in scope]
    else:
        raise result_module.ResultError("scope 只允许字符串或字符串集合")
    cleaned = {value.strip() for value in values if isinstance(value, str) and value.strip()}
    return tuple(sorted(cleaned))


def _canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(payload):
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _binding_material(profile, skill_id=None, claw_uid=None, scope=None):
    # profile 允许「profile 名字符串」或 `AuthProfile`，统一经 idaas_profile.profile_ref
    # 归一为 profile 名；非法类型会抛 ResultError（受控内部错误），绝不会抛未捕获 TypeError。
    return {
        "profileRef": idaas_profile.profile_ref(profile),
        "skillId": skill_id,
        "clawUid": fingerprint(claw_uid) if claw_uid else None,
        "scope": list(normalize_scope(scope)),
    }


def binding_fingerprint(profile, *, skill_id=None, claw_uid=None, scope=None):
    """绑定指纹：只保留 clawUid 哈希指纹，绝不落完整身份材料。"""
    return _digest(_binding_material(profile, skill_id, claw_uid, scope))


def challenge_ref(profile, *, skill_id=None, claw_uid=None, scope=None, nonce=None):
    """授权挑战引用：绑定 profile/skill/clawUid/scope，另加一次性随机因子。"""
    material = _binding_material(profile, skill_id, claw_uid, scope)
    material["kind"] = "challenge"
    material["nonce"] = nonce or uuid.uuid4().hex
    return "chal_" + _digest(material)[:32]


def operation_ref(profile, *, command, skill_id=None, claw_uid=None, scope=None, nonce=None):
    """操作引用：同样绑定 profile/skill/clawUid/scope，确保不可跨 profile 复用。"""
    _require_command(command)
    material = _binding_material(profile, skill_id, claw_uid, scope)
    material["kind"] = "operation"
    material["command"] = command
    material["nonce"] = nonce or uuid.uuid4().hex
    return "op_" + _digest(material)[:32]


def plan_hash(profile, *, command, plan, skill_id=None, claw_uid=None, scope=None):
    """冻结计划摘要：与 profile/skill/clawUid/scope 及计划内容逐字节绑定（可重算校验）。"""
    _require_command(command)
    material = _binding_material(profile, skill_id, claw_uid, scope)
    material["command"] = command
    material["plan"] = plan
    return _digest(material)


def _require_command(command):
    if not isinstance(command, str) or not command:
        raise result_module.ResultError("command 必须是非空字符串")


# ---------------------------------------------------------------------------
# 敏感字段脱敏
# ---------------------------------------------------------------------------


def fingerprint(value, length=12):
    """稳定指纹：用于绑定与诊断，不可逆。"""
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:length]


def mask_secret(value):
    """凭据值一律不可回显。"""
    if value is None:
        return None
    if not str(value):
        return ""
    return "***"


def mask_claw_uid(value, prefix_len=8):
    """clawUid 只允许前缀掩码，禁止完整输出。"""
    if value is None:
        return None
    text = str(value)
    if not text:
        return ""
    if len(text) <= prefix_len:
        return "***"
    return text[:prefix_len] + "..."


def desensitize_text(text):
    """对可能夹带凭据的文本做脱敏（错误说明、SDK 诊断输出共用）。"""
    if not text:
        return ""
    cleaned = _PEM_BLOCK_PATTERN.sub("-----REDACTED KEY-----", str(text))
    cleaned = _DEVICE_KEY_PATTERN.sub(r"\1\2***", cleaned)
    cleaned = _KEY_VALUE_PATTERN.sub(r"\1\2***", cleaned)
    cleaned = _BEARER_PATTERN.sub("Bearer ***", cleaned)
    return cleaned


def safe_message(message):
    """对外 message：脱敏 + 空白归一 + 非空兜底。"""
    if message is None:
        return "IDaaS 授权操作失败"
    text = _WHITESPACE_ONLY.sub(" ", desensitize_text(str(message))).strip()
    return text or "IDaaS 授权操作失败"
