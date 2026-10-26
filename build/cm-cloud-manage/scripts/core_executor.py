"""公共执行器：capability 门禁、输入校验、受控引用解析、读/写执行与状态查询。

顺序固定：
1. 检查 capability（发布期开关）
2. 校验输入 Schema（未知字段、互斥/依赖、RFC 3339、IANA 时区）
3. 接收宿主运行时提供的可信 RuntimeContext（本模块不实现授权、登录或取票来源）
4. 解析受控引用为真实资源 ID；按 Schema 声明隔离批量项错误，其余引用错误整单拒绝
5. 校验 logical scope 与发布配置就绪状态
6. READ/STATUS 直接运行 workflow
7. WRITE/LOCAL_WRITE 首次只调用 prepare() 生成冻结计划
8. operation confirm 接受宿主签名事件、人工 TTY 确认，或发布配置显式开启的对话确认
9. operation execute 从台账加载已确认计划并调用同一 workflow 的 execute()
10. 每次 adapter 调用都通过 RuntimeContext.call() 取得该请求专用的 request_headers
11. 收集每次 adapter 响应的 traceId，多接口场景按调用顺序以英文逗号拼接
"""

import inspect
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from cmcloud_core import debug_log
from cmcloud_core import operation_ledger as ledger_module
from cmcloud_core import platform_config
from cmcloud_core import client_info as client_info_module
from cmcloud_core import dedicated_root_cache
from cmcloud_core import device_info as device_info_module
from cmcloud_core import registry
from cmcloud_core import result as result_module
from cmcloud_adapters import idaas_user_folder_query
from cmcloud_validation import command_schemas
from cmcloud_validation import file_name_rules
from cmcloud_validation import schema_validator
from cmcloud_workflows import common as workflow_common

DEVICE_INFO_ENV = "CMCLOUD_DEVICE_INFO"
CLIENT_INFO_ENV = "CMCLOUD_CLIENT_INFO"
DEDICATED_ROOT_ENV = "CMCLOUD_DEDICATED_ROOT_ID"
DEDICATED_ROOT_NAME_ENV = "CMCLOUD_DEDICATED_ROOT_NAME"
SAVE_DESTINATION_ENV = "CMCLOUD_SAVE_DESTINATION_ID"
COLLECTION_LIMIT_ENV = "CMCLOUD_COLLECTION_LIMIT"
DEFAULT_PAGE_SIZE_ENV = "CMCLOUD_DEFAULT_PAGE_SIZE"
ACTIVITY_WINDOW_DAYS_ENV = "CMCLOUD_ACTIVITY_WINDOW_DAYS"
ALBUM_OWNER_TYPE_ENV = "CMCLOUD_ALBUM_OWNER_TYPE"
UPLOAD_HASH_ALGORITHM_ENV = "CMCLOUD_UPLOAD_HASH_ALGORITHM"
ALLOW_CONVERSATION_CONFIRMATION_ENV = "CMCLOUD_ALLOW_CONVERSATION_CONFIRMATION"
SLOW_ADAPTER_WARN_MS_ENV = "CMCLOUD_SLOW_WARN_MS"

DEFAULT_COLLECTION_LIMIT = 200
DEFAULT_PAGE_SIZE = 10
DEFAULT_ACTIVITY_WINDOW_DAYS = 30
DEFAULT_SLOW_ADAPTER_WARN_MS = 10000

# 目录平铺前 adapter 位于 ``cmcloud.adapters`` 包内，平铺后模块名增加
# ``adapter_`` 前缀。请求头路由同时识别两种 basename，避免文件布局变化导致
# 受控客户端信息或设备信息静默漏注入。
ALBUM_SHARE_INFO_ADAPTER_MODULES = frozenset(
    ("idaas_get_album_share_info", "adapter_idaas_get_album_share_info")
)
ALBUM_FILE_LIST_ADAPTER_MODULES = frozenset(
    (
        "idaas_photo_customization_file_list",
        "adapter_idaas_photo_customization_file_list",
    )
)

# 上传摘要算法：pds 底座只认 sha1、云空间底座只认 sha256（见 file/create 契约），
# 由发布配置声明；缺省保持 sha256。
UPLOAD_HASH_ALGORITHMS = ("sha1", "sha256")
DEFAULT_UPLOAD_HASH_ALGORITHM = "sha256"


@dataclass(frozen=True)
class PublicationConfig:
    """发布配置：由外部运行时/发布包提供，场景与 Agent 都不能覆盖。"""

    profile: str = "default"
    dedicated_root_id: str | None = None
    dedicated_root_name: str | None = None
    save_destination_id: str | None = None
    collection_limit: int = DEFAULT_COLLECTION_LIMIT
    default_page_size: int = DEFAULT_PAGE_SIZE
    device_info: str | None = None
    client_info: str | None = None
    activity_window_days: int = DEFAULT_ACTIVITY_WINDOW_DAYS
    album_owner_type: str | None = None
    upload_hash_algorithm: str = DEFAULT_UPLOAD_HASH_ALGORITHM
    allow_conversation_confirmation: bool = True

    @classmethod
    def from_env(cls, profile="default", environ=None):
        source = os.environ if environ is None else environ
        return cls(
            profile=profile,
            dedicated_root_id=source.get(DEDICATED_ROOT_ENV) or None,
            dedicated_root_name=source.get(DEDICATED_ROOT_NAME_ENV) or None,
            save_destination_id=source.get(SAVE_DESTINATION_ENV) or None,
            collection_limit=_int_env(source, COLLECTION_LIMIT_ENV, DEFAULT_COLLECTION_LIMIT),
            default_page_size=_int_env(source, DEFAULT_PAGE_SIZE_ENV, DEFAULT_PAGE_SIZE),
            device_info=source.get(DEVICE_INFO_ENV) or None,
            client_info=source.get(CLIENT_INFO_ENV) or None,
            activity_window_days=_int_env(source, ACTIVITY_WINDOW_DAYS_ENV, DEFAULT_ACTIVITY_WINDOW_DAYS),
            album_owner_type=source.get(ALBUM_OWNER_TYPE_ENV) or None,
            upload_hash_algorithm=_hash_algorithm_env(source),
            allow_conversation_confirmation=_bool_env(
                source, ALLOW_CONVERSATION_CONFIRMATION_ENV, True
            ),
        )


def _int_env(source, name, default):
    raw = source.get(name)
    if not raw:
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise result_module.ExecutionError(result_module.FAILED, "发布配置 %s 不是整数" % name)
    if value <= 0:
        raise result_module.ExecutionError(result_module.FAILED, "发布配置 %s 必须为正整数" % name)
    return value


def _hash_algorithm_env(source, name=UPLOAD_HASH_ALGORITHM_ENV):
    raw = source.get(name)
    if raw is None or not str(raw).strip():
        return DEFAULT_UPLOAD_HASH_ALGORITHM
    value = str(raw).strip().lower()
    if value not in UPLOAD_HASH_ALGORITHMS:
        raise result_module.ExecutionError(
            result_module.FAILED,
            "发布配置 %s 只接受 %s" % (name, "/".join(UPLOAD_HASH_ALGORITHMS)),
        )
    return value


def _bool_env(source, name, default):
    raw = source.get(name)
    if raw is None or not str(raw).strip():
        return default
    normalized = str(raw).strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise result_module.ExecutionError(
        result_module.FAILED, "发布配置 %s 只接受 true 或 false" % name
    )


@dataclass(frozen=True)
class HostContext:
    """宿主调用上下文；没有正式字段时不伪造，一律保持 None。"""

    openclaw_id: str | None = None
    session_id: str | None = None
    message_id: str | None = None
    source_channel: str | None = None
    channel_id: str | None = None
    user_id: str | None = None

    def as_dict(self):
        return {
            "openclawId": self.openclaw_id,
            "sessionId": self.session_id,
            "messageId": self.message_id,
            "sourceChannel": self.source_channel,
            "channelId": self.channel_id,
            "userId": self.user_id,
        }


class HeaderProvider:
    """请求专用 headers 供给端口；具体取票实现由本任务之外的运行时提供。"""

    def headers(self):
        raise NotImplementedError


class NullHeaderProvider(HeaderProvider):
    def headers(self):
        raise result_module.ExecutionError(
            result_module.FAILED,
            "当前运行时未提供请求专用 headers，业务接口不可调用",
        )


class StaticHeaderProvider(HeaderProvider):
    """仅用于受控测试或已在宿主内完成取票的进程内注入。"""

    def __init__(self, factory):
        if not callable(factory):
            raise ValueError("factory 必须可调用")
        self._factory = factory

    def headers(self):
        return self._factory()


class ConfirmationProvider:
    """可信宿主确认事件供给端口。"""

    def confirmation_for(self, operation_ref):
        raise NotImplementedError


class NullConfirmationProvider(ConfirmationProvider):
    def confirmation_for(self, operation_ref):
        return None


class FileConfirmationProvider(ConfirmationProvider):
    """从宿主提供的确认事件文件读取绑定信息（含 planHash/profile/影响范围摘要）。"""

    def __init__(self, path):
        self._path = os.path.abspath(path)

    def confirmation_for(self, operation_ref):
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError) as err:
            raise result_module.ExecutionError(
                result_module.FAILED,
                "确认事件文件不可读取: %s" % err.__class__.__name__,
            )
        if not isinstance(document, dict):
            raise result_module.ExecutionError(result_module.FAILED, "确认事件文件结构非法")
        if document.get("operationRef") != operation_ref:
            raise result_module.ExecutionError(
                result_module.INVALID_ARGUMENT, "确认事件与当前操作不匹配"
            )
        return document


class InteractiveConfirmationProvider(ConfirmationProvider):
    """人工 CLI 确认；提示写 stderr，stdout 仍只输出一个 JSON 文档。"""

    def __init__(self, record, input_stream=None, output_stream=None):
        self._record = dict(record)
        self._input = input_stream or sys.stdin
        self._output = output_stream or sys.stderr

    def confirmation_for(self, operation_ref):
        if self._record.get("operationRef") != operation_ref:
            raise result_module.ExecutionError(
                result_module.INVALID_ARGUMENT, "确认事件与当前操作不匹配"
            )
        self._output.write("即将执行：%s\n" % (self._record.get("planSummary") or "已冻结计划"))
        self._output.write("输入 CONFIRM 确认，其他输入将取消：")
        self._output.flush()
        answer = self._input.readline().strip()
        if answer != "CONFIRM":
            raise result_module.ExecutionError(result_module.CONSENT_DENIED, "用户取消了本次操作")
        return {
            "operationRef": operation_ref,
            "planHash": self._record.get("planHash"),
            "profileRef": self._record.get("profileRef"),
        }


class ConversationConfirmationProvider(ConfirmationProvider):
    """非交互式 Agent 对话确认。

    该提供者仅在发布配置显式允许时由 CLI 选用。它能把上一轮用户确认
    绑定到 operationRef/planHash/profile，但不提供宿主签名级别的用户来源证明。

    **边界声明**：这是模型侧流程约束，不是不可越过的安全边界——调用者提交固定字符串
    ``CONFIRM`` 与计划引用即可走该确认路径，无法证明"用户确在另一轮真实同意"。发布要求
    程序级验证时，应改用宿主签名事件或人工 TTY，并设置
    ``CMCLOUD_ALLOW_CONVERSATION_CONFIRMATION=false``。
    """

    def __init__(self, *, operation_ref, plan_hash, profile, confirmation):
        self._operation_ref = operation_ref
        self._plan_hash = plan_hash
        self._profile = profile
        self._confirmation = confirmation

    def confirmation_for(self, operation_ref):
        if operation_ref != self._operation_ref:
            raise result_module.ExecutionError(
                result_module.INVALID_ARGUMENT, "确认与当前操作不匹配"
            )
        if self._confirmation != "CONFIRM":
            raise result_module.ExecutionError(
                result_module.CONSENT_DENIED, "本次对话确认未被接受"
            )
        return {
            "operationRef": operation_ref,
            "planHash": self._plan_hash,
            "profileRef": self._profile,
        }


class RuntimeContext:
    """场景与 adapter 之间的唯一调用通道。"""

    def __init__(
        self,
        *,
        spec,
        profile,
        header_provider=None,
        ledger=None,
        publication=None,
        host_context=None,
        confirmation_provider=None,
        trace_id=None,
        clock=None,
        sleeper=None,
        dedicated_root_store=None,
    ):
        self.spec = spec
        self.profile = profile
        self._header_provider = header_provider or NullHeaderProvider()
        self.ledger = ledger or ledger_module.LedgerStore()
        self.publication = publication or PublicationConfig(profile=profile)
        self._dedicated_root_id = self.publication.dedicated_root_id
        self._dedicated_root_name = (
            self.publication.dedicated_root_name
            or os.environ.get(DEDICATED_ROOT_NAME_ENV)
            or None
        )
        self._dedicated_root_id_path = None
        self._dedicated_root_store = (
            dedicated_root_store or dedicated_root_cache.CredentialDedicatedRootStore()
        )
        self.host_context = host_context.as_dict() if isinstance(host_context, HostContext) else dict(host_context or {})
        self._host_context_obj = host_context if isinstance(host_context, HostContext) else HostContext()
        self.confirmation_provider = confirmation_provider or NullConfirmationProvider()
        self.trace_id = trace_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleeper = sleeper or time.sleep
        self._resolved_refs = {}
        self._ref_errors = {}
        self._issued_refs = {}
        self.call_log = []
        self.operation_ref = None
        self._partial_step = None

    # ---- 基础 ----

    def now(self):
        return self._clock()

    def sleep(self, seconds):
        if seconds <= 0:
            return
        self._sleeper(seconds)

    @property
    def command_id(self):
        return self.spec.command_id

    def agent_platform_name(self):
        """优先使用当前取票身份已确定的平台，供实例名称缺省时兜底。"""
        from cmcloud_auth import agent_scope

        auth_profile = getattr(self._header_provider, "auth_profile", None)
        code = agent_scope.normalize_agent_code(getattr(auth_profile, "agent_code", None))
        if code is None:
            code, _source = agent_scope.resolve_agent_code()
        return agent_scope.agent_display_name(code) or agent_scope.agent_display_name(
            agent_scope.FALLBACK_AGENT_CODE
        )

    def dedicated_root_id(self):
        self.ensure_dedicated_root()
        return self._dedicated_root_id

    def dedicated_root_name(self):
        self.ensure_dedicated_root(require_name=True)
        return self._dedicated_root_name

    def dedicated_root_public(self):
        root_id = self.dedicated_root_id()
        name_path = self.dedicated_root_name()
        return {
            "rootRef": self.issue_ref(
                "folder", root_id, workflow_common.SCOPE_DEDICATED_ROOT
            ),
            "namePath": name_path,
        }

    def query_user_folder(self):
        """agent info 与内部目录流程共用的实时接口入口；身份与重试统一由运行时处理。"""
        try:
            response = self.call(idaas_user_folder_query.call)
        except result_module.ExecutionError:
            raise
        except idaas_user_folder_query.IdaasUserFolderQueryError as err:
            # 目录异常不能统一解释为未授权；只认传输层 401，业务 code=401 不算。
            status = getattr(err, "http_status", None)
            retryable = False
            if _is_unauthorized_code(status):
                code = result_module.AUTH_EXPIRED
                message = "目录查询鉴权未通过，业务票据已失效"
            elif err.timeout:
                code = result_module.UPSTREAM_TIMEOUT
                message = "专属目录查询超时，请稍后重试"
                retryable = True
            elif err.network:
                code = result_module.FAILED
                message = "无法连接专属目录服务，请检查网络或稍后重试"
                retryable = True
            elif status is not None:
                code = result_module.FAILED
                message = "专属目录接口请求失败（HTTP %s），请检查服务或接口配置" % status
                retryable = status == 429 or (isinstance(status, int) and status >= 500)
            elif err.code is not None:
                code = result_module.FAILED
                message = "专属目录服务返回业务错误，请联系维护方排查"
            else:
                code = result_module.UPSTREAM_CONTRACT_MISMATCH
                message = "专属目录接口数据不符合约定，请联系维护方排查"
            raise result_module.ExecutionError(
                code, message, retryable=retryable,
                data=workflow_common._structured_error_data(err),
            ) from err
        return response

    def ensure_dedicated_root(self, *, require_name=False, force=False):
        """从发布配置、系统凭据库或当前票据查询取得专属目录根。

        ``force=True`` 跳过进程内值和缓存，直接重查当前可操作目录并覆盖缓存。
        """
        if not force and self._dedicated_root_id and (not require_name or self._dedicated_root_name):
            return
        headers = self._fresh_headers()
        auth_profile = getattr(self._header_provider, "auth_profile", None)
        fingerprint = dedicated_root_cache.identity_fingerprint(headers)
        if not force and auth_profile is not None and fingerprint:
            cached = self._dedicated_root_store.load(auth_profile, fingerprint, self.now())
            if cached is not None:
                self._apply_dedicated_root(cached)
                return
        response = self.query_user_folder()
        data = response.get("data") if isinstance(response, dict) else None
        self.cache_user_folder(data)

    def cache_user_folder(self, data):
        """校验同一次查询的目录结果并更新缓存，复用时不再次调用接口。"""
        folder_id = data.get("folderId") if isinstance(data, dict) else None
        id_path = data.get("idPath") if isinstance(data, dict) else None
        name_path = data.get("namePath") if isinstance(data, dict) else None
        if not isinstance(folder_id, str) or not folder_id:
            raise result_module.ExecutionError(
                result_module.UPSTREAM_CONTRACT_MISMATCH,
                "专属目录查询响应缺少 folderId",
            )
        if not isinstance(name_path, str) or not name_path:
            raise result_module.ExecutionError(
                result_module.UPSTREAM_CONTRACT_MISMATCH,
                "专属目录查询响应缺少 namePath",
            )
        resolved = {"folderId": folder_id, "idPath": id_path, "namePath": name_path}
        headers = self._fresh_headers()
        auth_profile = getattr(self._header_provider, "auth_profile", None)
        fingerprint = dedicated_root_cache.identity_fingerprint(headers)
        if auth_profile is not None and fingerprint:
            self._dedicated_root_store.save(
                auth_profile, fingerprint, resolved, self.now()
            )
        self._apply_dedicated_root(resolved)

    def _apply_dedicated_root(self, data):
        folder_id = data["folderId"]
        name_path = data["namePath"]
        id_path = data.get("idPath")
        os.environ[DEDICATED_ROOT_ENV] = folder_id
        os.environ[DEDICATED_ROOT_NAME_ENV] = name_path
        self._dedicated_root_id = folder_id
        self._dedicated_root_name = name_path
        self._dedicated_root_id_path = id_path if isinstance(id_path, str) and id_path else None

    def collection_limit(self):
        return self.publication.collection_limit

    def save_destination_id(self):
        """S20 固定的“转存文件”目录真实 ID；未配置返回 None（场景按运行时不可用处理）。"""
        return self.publication.save_destination_id

    def default_page_size(self):
        return self.publication.default_page_size

    def device_info(self):
        # 发布环境可显式覆盖为固定值；未覆盖时按文档《客户端设备信息》规则实时生成，
        # 二者都不接受 Agent 输入。
        return self.publication.device_info or device_info_module.build_device_info()

    def client_info(self):
        # 发布环境可显式覆盖；未覆盖时每次调用均从当前设备实时采集。
        return self.publication.client_info or client_info_module.collect_client_info()

    def activity_window_days(self):
        return self.publication.activity_window_days

    def upload_hash_algorithm(self):
        """上传摘要算法：由发布配置声明（pds=sha1、云空间=sha256），Agent 不可覆盖。"""
        return self.publication.upload_hash_algorithm

    # ---- adapter 调用 ----

    def call(self, action, **kwargs):
        if "request_headers" in kwargs:
            raise RuntimeError("场景代码不得自行传 request_headers")
        target = action
        if _accepts_request_headers(target):
            kwargs["request_headers"] = self._request_headers(target)
        try:
            response = self._invoke_action(target, kwargs)
        except Exception as err:
            if _is_explicit_ticket_rejection(err):
                return self._retry_after_ticket_refresh(target, kwargs, original_error=err)
            if _is_stale_path_rejection(err):
                return self._recover_stale_path(original_error=err)
            raise
        if _is_explicit_ticket_rejection(response):
            return self._retry_after_ticket_refresh(target, kwargs, original_response=response)
        if _is_stale_path_rejection(response):
            return self._recover_stale_path(original_response=response)
        return response

    def _retry_after_ticket_refresh(
        self, target, kwargs, original_error=None, original_response=None
    ):
        """只对明确票据鉴权失败刷新并重放一次；第二次结果原样交给上层。"""
        refresh = getattr(self._header_provider, "refresh_headers", None)
        if not callable(refresh) or not _accepts_request_headers(target):
            # 运行时没有刷新能力时保持原失败语义，不伪装成已恢复。
            if original_error is not None:
                raise original_error
            return original_response
        retried = dict(kwargs)
        retried["request_headers"] = self._request_headers(
            target,
            refresh=True,
            reason_code=_rejection_code(original_error, original_response),
        )
        try:
            response = self._invoke_action(target, retried)
        except Exception as err:
            if _is_explicit_ticket_rejection(err):
                self._invalidate_rejected_ticket()
            if _is_stale_path_rejection(err):
                return self._recover_stale_path(original_error=err)
            raise
        if _is_explicit_ticket_rejection(response):
            self._invalidate_rejected_ticket()
        if _is_stale_path_rejection(response):
            return self._recover_stale_path(original_response=response)
        return response

    def _invalidate_rejected_ticket(self):
        invalidate = getattr(self._header_provider, "invalidate_ticket", None)
        if callable(invalidate):
            invalidate()

    def _refresh_operable_root(self):
        """16110013 后强制重查当前可操作专属目录；失败时保持原失败语义。"""
        if getattr(self, "_refreshing_operable_root", False):
            return None
        self._refreshing_operable_root = True
        try:
            self.ensure_dedicated_root(force=True)
        except result_module.ExecutionError as err:
            debug_log.emit("stalePath.refresh", error=getattr(err, "code", None))
            return None
        finally:
            self._refreshing_operable_root = False
        folder_id = self._dedicated_root_id
        name_path = self._dedicated_root_name
        if not isinstance(folder_id, str) or not folder_id:
            return None
        if not isinstance(name_path, str) or not name_path:
            return None
        return {
            "rootRef": self.issue_ref(
                "folder", folder_id, workflow_common.SCOPE_DEDICATED_ROOT
            ),
            "folderId": folder_id,
            "idPath": self._dedicated_root_id_path,
            "namePath": name_path,
        }

    def _recover_stale_path(self, original_error=None, original_response=None):
        """重查目录并挂到失败对象；本次不重试、不改写用户目标。"""
        operable = self._refresh_operable_root()
        rejected = original_error if original_error is not None else original_response
        _attach_operable_path(rejected, operable)
        if isinstance(rejected, BaseException):
            raise rejected
        return original_response

    def _request_headers(self, target, refresh=False, reason_code=None):
        headers = self._fresh_headers(refresh=refresh, reason_code=reason_code)
        module_name = _adapter_module_basename(target)
        # 仅相簿分享接口需要 x-DeviceInfo；值来自可信发布配置，不能由 Agent 传入，
        # 也不应扩散到其他业务接口。
        if module_name in ALBUM_SHARE_INFO_ADAPTER_MODULES:
            headers["x-DeviceInfo"] = self.device_info()
        if module_name in ALBUM_FILE_LIST_ADAPTER_MODULES:
            headers["x-yun-client-info"] = self.client_info()
        return headers

    def _invoke_action(self, target, kwargs):
        name = "%s.%s" % (
            getattr(target, "__module__", "").rsplit(".", 1)[-1] or "adapter",
            getattr(target, "__name__", repr(target)),
        )
        self.call_log.append({"action": getattr(target, "__name__", repr(target)), "args": kwargs})
        started = time.monotonic()
        debug_log.emit("adapter.start", action=name, arg_keys=sorted(str(key) for key in kwargs))
        try:
            response = target(**kwargs)
        except Exception as err:
            duration_ms = int((time.monotonic() - started) * 1000)
            debug_log.emit(
                "adapter.error",
                action=name,
                code=_adapter_error_code(err),
                message=str(err)[:200],
                duration_ms=duration_ms,
            )
            _warn_slow_adapter(name, duration_ms)
            raise
        self._collect_trace_id(response)
        duration_ms = int((time.monotonic() - started) * 1000)
        debug_log.emit(
            "adapter.end",
            action=name,
            ok=True,
            trace_id=_response_trace_id(response),
            duration_ms=duration_ms,
        )
        _warn_slow_adapter(name, duration_ms)
        return response

    def _collect_trace_id(self, response):
        """透传下游接口顶层 traceId。

        场景可能串行调用多个接口；统一信封只有一个 traceId 字段，
        因此按实际调用顺序用英文逗号拼接。响应未携带有效 traceId 时不追加。
        """
        if not isinstance(response, dict):
            return
        trace_id = response.get("traceId")
        if not isinstance(trace_id, str) or not trace_id.strip():
            return
        trace_id = trace_id.strip()
        self.trace_id = "%s,%s" % (self.trace_id, trace_id) if self.trace_id else trace_id

    def _fresh_headers(self, refresh=False, reason_code=None):
        if refresh:
            refresh_headers = getattr(self._header_provider, "refresh_headers", None)
            if not callable(refresh_headers):
                raise result_module.ExecutionError(result_module.AUTH_EXPIRED, "业务票据已失效")
            headers = (
                refresh_headers(reason_code=reason_code)
                if _accepts_reason_code(refresh_headers)
                else refresh_headers()
            )
        else:
            headers = self._header_provider.headers()
        if not isinstance(headers, dict):
            raise result_module.ExecutionError(result_module.FAILED, "运行时返回的 headers 必须是映射")
        clean = {}
        for key, value in headers.items():
            if not isinstance(key, str) or not key:
                raise result_module.ExecutionError(result_module.FAILED, "运行时 headers 键非法")
            if value is None:
                continue
            if not isinstance(value, str):
                raise result_module.ExecutionError(result_module.FAILED, "运行时 headers 值非法")
            clean[key] = value
        # 只在统一业务调用入口叠加公共头；预签名对象存储传输不接收 request_headers，
        # 因而不会被附加业务 API 头，避免影响其签名校验。
        clean.update(platform_config.load_platform_config().request_common_headers)
        return clean

    # ---- 受控引用与游标 ----

    def preload_ref(self, token, entry):
        self._resolved_refs[token] = entry

    def preload_ref_error(self, field_path, item_index, error):
        """按字段路径与项索引记录批量引用错误；无效引用绝不进入下游。"""
        self._ref_errors[(tuple(field_path), item_index)] = error

    def ref_error(self, field_path, item_index):
        return self._ref_errors.get((tuple(field_path), item_index))

    def ref_entry(self, token):
        entry = self._resolved_refs.get(token)
        if entry is None:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "引用未经公共前置解析")
        return entry

    def real_id(self, token, expected_kind=None):
        entry = self.ref_entry(token)
        if expected_kind is not None and entry.get("kind") != expected_kind:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "引用类型与目标参数不匹配")
        return entry["realId"]

    def issue_ref(self, kind, real_id, scope, ttl_seconds=None, parent_real_id=None, resource_path=None):
        key = (kind, real_id, scope, parent_real_id, resource_path)
        cached = self._issued_refs.get(key)
        if cached:
            return cached
        kwargs = {
            "kind": kind,
            "real_id": real_id,
            "profile": self.profile,
            "source_command": self.spec.command_id,
            "scope": scope,
            "parent_real_id": parent_real_id,
        }
        if ttl_seconds is not None:
            kwargs["ttl_seconds"] = ttl_seconds
        if resource_path is not None:
            kwargs["resource_path"] = resource_path
        token = self.ledger.issue_ref(**kwargs)
        self._issued_refs[key] = token
        return token

    def resolve_cursor(self, token, conditions_digest):
        return self.ledger.resolve_cursor(
            token=token,
            profile=self.profile,
            command_id=self.spec.command_id,
            conditions_digest=conditions_digest,
        )

    def issue_cursor(self, conditions_digest, upstream_state, complete):
        return self.ledger.issue_cursor(
            command_id=self.spec.command_id,
            profile=self.profile,
            conditions_digest=conditions_digest,
            upstream_state=upstream_state,
            complete=complete,
        )

    # ---- 步骤记录 ----

    def note_step(self, name, summary, status=ledger_module.STEP_OK, task_ref=None):
        if self.operation_ref:
            self.ledger.append_step(self.operation_ref, name=name, status=status, summary=summary, task_ref=task_ref)

    def bind_task(self, task_ref, *, task_type=None, query_command_id=None):
        """把下游真实 taskId 绑定到台账，供 operation.status 恢复查询。"""
        if not isinstance(task_ref, str) or not task_ref:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "下游 taskId 非法")
        if not self.operation_ref:
            raise RuntimeError("只有写场景执行阶段可以绑定下游 taskId")
        self.ledger.update_operation(
            self.operation_ref,
            taskRef=task_ref,
            taskType=task_type,
            queryCommandId=query_command_id or self.spec.command_id,
        )


def _accepts_request_headers(action):
    try:
        signature = inspect.signature(action)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.name == "request_headers":
            return True
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def _adapter_module_basename(action):
    """返回 adapter 模块 basename，兼容包内模块和平铺模块。"""
    module_name = getattr(action, "__module__", "")
    if not isinstance(module_name, str):
        return ""
    return module_name.rsplit(".", 1)[-1]


def _slow_warn_threshold_ms():
    raw = os.environ.get(SLOW_ADAPTER_WARN_MS_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_SLOW_ADAPTER_WARN_MS
    try:
        return int(str(raw).strip())
    except ValueError:
        return DEFAULT_SLOW_ADAPTER_WARN_MS


def _warn_slow_adapter(action, duration_ms):
    """adapter 调用超过阈值时在 stderr 提示一行，便于直接从工具日志定位慢调用。"""
    threshold = _slow_warn_threshold_ms()
    if threshold <= 0 or duration_ms < threshold:
        return
    try:
        sys.stderr.write(
            "[cmcloud] 慢调用 %s 用时 %.1fs（阈值 %.1fs）\n"
            % (action, duration_ms / 1000.0, threshold / 1000.0)
        )
    except OSError:
        pass


def _is_explicit_ticket_rejection(value):
    """HTTP 401 或能开已明确的票据失效码才允许换票重试一次。

    110044: 缓存信息失效；130090: uid失效；16110038: Token有效期不足；
    01000003: 令牌失效。103082: 权限已变更，刷新时先经 validate 协商 scopeIds。
    来源为能开 IdaasAuthErrorCodeEnum / BaseResultCodeEnum。
    业务 code=401 可能表示参数错误，不视为 HTTP 401；不按错误文案猜测。
    """
    ticket_codes = {"110044", "130090", "16110038", "01000003", "103082"}
    def is_ticket_code(code):
        return not isinstance(code, bool) and code is not None and str(code).strip() in ticket_codes
    if isinstance(value, BaseException):
        return (_is_unauthorized_code(getattr(value, "http_status", None))
                or is_ticket_code(getattr(value, "code", None)))
    if not isinstance(value, dict):
        return False
    return (_is_unauthorized_code(value.get("httpStatus"))
            or any(is_ticket_code(value.get(field)) for field in ("code", "resultCode")))


def _is_unauthorized_code(value):
    if isinstance(value, bool) or value is None:
        return False
    return str(value).strip() == "401"


def _is_stale_path_rejection(value):
    """仅按结构化业务码识别 16110013，不按错误文案猜测。"""
    if isinstance(value, BaseException):
        return _is_out_of_scope_code(getattr(value, "code", None))
    if not isinstance(value, dict):
        return False
    return _is_out_of_scope_code(value.get("code")) or _is_out_of_scope_code(
        value.get("resultCode")
    )


def _is_out_of_scope_code(code):
    if isinstance(code, bool) or code is None:
        return False
    return str(code).strip() == "16110013"


def _attach_operable_path(rejected, operable):
    """把重查结果挂到异常或响应，回执层只投影 rootRef/namePath。"""
    if not isinstance(operable, dict):
        return
    try:
        if isinstance(rejected, BaseException):
            rejected._cmcloudOperablePath = operable
        elif isinstance(rejected, dict):
            rejected["_cmcloudOperablePath"] = operable
    except Exception:
        pass


def _rejection_code(error=None, response=None):
    """读取触发本次换票的结构化拒绝码。"""
    if isinstance(error, BaseException):
        code = getattr(error, "code", None)
    elif isinstance(response, dict):
        code = response.get("code")
        if code is None:
            code = response.get("resultCode")
    else:
        return None
    if isinstance(code, bool) or code is None:
        return None
    text = str(code).strip()
    return text or None


def _accepts_reason_code(action):
    """兼容仍只接受无参调用的旧 refresh_headers 实现。"""
    try:
        signature = inspect.signature(action)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.name == "reason_code":
            return True
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return True
    return False


# ---- 输入校验 ----

def validate_input(spec, input_data):
    """拒绝 Agent 自报的可信字段，并按 Schema 严格校验。"""
    if input_data is None:
        input_data = {}
    if not isinstance(input_data, dict):
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "业务输入必须是 JSON 对象")
    forbidden = sorted(set(input_data) & set(command_schemas.FORBIDDEN_CLIENT_FIELDS))
    if forbidden:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "以下字段由可信运行时或服务端提供，不接受 Agent 提交: %s" % ", ".join(forbidden),
        )
    schema = command_schemas.SCENARIO_SCHEMAS[spec.scenario_id]
    _reject_forbidden_name_characters(schema, input_data)
    try:
        schema_validator.validate_or_raise(schema, input_data)
    except schema_validator.SchemaValidationError as err:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "输入校验失败: %s" % err)
    return input_data


def _reject_forbidden_name_characters(schema, input_data):
    """命名类字段的禁用字符先给点名提示（哪些符号不能用），Schema pattern 仍是最终兜底。"""
    for field, field_schema in (schema.get("properties") or {}).items():
        if not isinstance(field_schema, dict) or not field_schema.get(file_name_rules.FORBIDDEN_MARKER):
            continue
        message = file_name_rules.name_error_message(input_data.get(field))
        if message is not None:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, message)


@dataclass(frozen=True)
class RefOccurrence:
    field_path: tuple
    item_index: int | None
    token: str
    expected_kind: str | None
    allowed_kinds: tuple
    item_error_policy: str | None


def _collect_ref_tokens(
    schema,
    instance,
    field_path=(),
    *,
    item_index=None,
    item_error_policy=None,
    allowed_kinds=(),
):
    """按 Schema 注解收集受控引用及其逐项错误策略。"""
    occurrences = []
    if isinstance(schema, bool) or not isinstance(schema, dict):
        return occurrences
    for keyword in ("allOf", "anyOf", "oneOf"):
        for branch in schema.get(keyword, ()) or ():
            occurrences.extend(
                _collect_ref_tokens(
                    branch,
                    instance,
                    field_path,
                    item_index=item_index,
                    item_error_policy=item_error_policy,
                    allowed_kinds=allowed_kinds,
                )
            )
    for keyword in ("if", "then", "else"):
        branch = schema.get(keyword)
        if isinstance(branch, (dict, bool)):
            occurrences.extend(
                _collect_ref_tokens(
                    branch,
                    instance,
                    field_path,
                    item_index=item_index,
                    item_error_policy=item_error_policy,
                    allowed_kinds=allowed_kinds,
                )
            )
    if isinstance(instance, dict):
        for name, subschema in (schema.get("properties") or {}).items():
            if name not in instance:
                continue
            occurrences.extend(
                _collect_ref_tokens(
                    subschema,
                    instance[name],
                    field_path + (name,),
                    item_index=item_index,
                    item_error_policy=item_error_policy,
                    allowed_kinds=allowed_kinds,
                )
            )
    elif isinstance(instance, list):
        item_schema = schema.get("items")
        if item_schema is not None:
            policy = schema.get(
                command_schemas.REF_ITEM_ERROR_POLICY_KEY, item_error_policy
            )
            declared_kinds = schema.get(
                command_schemas.REF_ALLOWED_KINDS_KEY, allowed_kinds
            )
            for index, item in enumerate(instance):
                occurrences.extend(
                    _collect_ref_tokens(
                        item_schema,
                        item,
                        field_path,
                        item_index=index,
                        item_error_policy=policy,
                        allowed_kinds=tuple(declared_kinds or ()),
                    )
                )
    elif isinstance(instance, str):
        kind = schema.get("x-ref-kind")
        if kind:
            occurrences.append(
                RefOccurrence(
                    field_path=tuple(field_path),
                    item_index=item_index,
                    token=instance,
                    expected_kind=None if kind == "*" else kind,
                    allowed_kinds=tuple(allowed_kinds or ()),
                    item_error_policy=item_error_policy,
                )
            )
    return occurrences


def preload_refs(spec, input_data, runtime):
    schema = command_schemas.SCENARIO_SCHEMAS[spec.scenario_id]
    for occurrence in _collect_ref_tokens(schema, input_data):
        try:
            entry = runtime.ledger.resolve_ref(
                token=occurrence.token,
                profile=runtime.profile,
                expected_kind=occurrence.expected_kind,
            )
            if (
                occurrence.allowed_kinds
                and entry.get("kind") not in occurrence.allowed_kinds
            ):
                raise result_module.ExecutionError(
                    result_module.INVALID_ARGUMENT,
                    "引用类型与目标参数不匹配",
                )
        except result_module.ExecutionError as err:
            if (
                occurrence.item_error_policy
                == command_schemas.REF_ITEM_ERROR_ISOLATE
                and occurrence.item_index is not None
            ):
                runtime.preload_ref_error(
                    occurrence.field_path,
                    occurrence.item_index,
                    err,
                )
                continue
            raise
        runtime.preload_ref(occurrence.token, entry)


def _assert_publication_ready(spec, runtime):
    for scope in spec.logical_scopes:
        if scope == "dedicated_root":
            runtime.ensure_dedicated_root()


def _assert_capability(spec, runtime):
    """只保留发布期开关：契约不符不再做能力级停用（每次请求各自返回错误）。"""
    if not spec.enabled:
        raise result_module.ExecutionError(
            result_module.CAPABILITY_UNAVAILABLE,
            "命令 %s 已注册但 capability 关闭: %s" % (spec.command_id, spec.disabled_reason),
        )


# ---- 执行入口 ----

def run_command(spec, input_data, runtime):
    """按命令类型分发；返回统一信封。"""
    try:
        if spec.operation_type == registry.OPERATION_STATUS:
            return _run_status(spec, input_data, runtime)
        if spec.is_write_like:
            return _prepare(spec, input_data, runtime)
        return _run_read(spec, input_data, runtime)
    except result_module.ExecutionError as err:
        debug_log.emit(
            "run.error", code=err.code, message=err.message, retryable=err.retryable
        )
        return result_module.envelope_from_error(spec.command_id, err, runtime.trace_id)
    except schema_validator.SchemaError as err:
        raise
    except Exception as err:  # 未预期异常不向上暴露堆栈
        wrapped = result_module.ExecutionError(
            result_module.UNKNOWN, "执行出现未预期错误: %s" % err.__class__.__name__
        )
        debug_log.emit(
            "run.error",
            code=wrapped.code,
            message=wrapped.message,
            retryable=wrapped.retryable,
            exception=err.__class__.__name__,
            traceback=_traceback_tail(),
        )
        return result_module.envelope_from_error(spec.command_id, wrapped, runtime.trace_id)


def _traceback_tail(limit=10, max_chars=4000):
    """未预期异常的栈信息（只进日志文件，不进信封与 stderr 摘要）。"""
    try:
        import traceback

        return traceback.format_exc(limit=limit)[-max_chars:]
    except Exception:
        return None


def _stage(name):
    """记录执行阶段：命令失败时，最后一条 stage 即失败位置。"""
    debug_log.emit("stage", name=name)


def _run_read(spec, input_data, runtime):
    _assert_capability(spec, runtime)
    _stage("capability")
    validate_input(spec, input_data)
    _stage("validate")
    _assert_publication_ready(spec, runtime)
    _stage("publication")
    preload_refs(spec, input_data, runtime)
    _stage("refs")
    entry = registry.workflow_callable(spec, "run")
    outcome = entry(runtime, input_data)
    return _envelope(spec, outcome, runtime)


def _run_status(spec, input_data, runtime):
    """S22 operation.status：从台账恢复任务并查询；未知状态只输出 UNKNOWN。"""
    _assert_capability(spec, runtime)
    _stage("capability")
    validate_input(spec, input_data)
    _stage("validate")
    preload_refs(spec, input_data, runtime)
    _stage("refs")
    entry = registry.workflow_callable(spec, "run")
    outcome = entry(runtime, input_data)
    return _envelope(spec, outcome, runtime)


def _prepare(spec, input_data, runtime):
    _assert_capability(spec, runtime)
    _stage("capability")
    validate_input(spec, input_data)
    _stage("validate")
    _assert_publication_ready(spec, runtime)
    _stage("publication")
    preload_refs(spec, input_data, runtime)
    _stage("refs")
    prepare = registry.workflow_callable(spec, "prepare")
    plan = prepare(runtime, input_data)
    if not isinstance(plan, dict):
        raise RuntimeError("%s 的 prepare 必须返回计划对象" % spec.command_id)
    if not isinstance(plan.get("summary"), str) or not plan["summary"]:
        raise RuntimeError("%s 的计划缺少 summary" % spec.command_id)
    if (spec.scenario_id == "S05"
            and spec.confirmation == registry.CONFIRMATION_LOCAL_OVERWRITE
            and plan.get("conflictPolicy") in ("fail", "rename")):
        # Explicit download intent needs no second cloud-write confirmation.
        # All validation/preparation above remains mandatory; overwrite still
        # uses the frozen plan and one-time confirmation mechanism below.
        execute = registry.workflow_callable(spec, "execute")
        return _envelope(spec, execute(runtime, plan), runtime)
    plan_hash = ledger_module.plan_hash_of(plan)
    operation_ref = runtime.ledger.create_operation(
        profile=runtime.profile,
        scenario_id=spec.scenario_id,
        command_id=spec.command_id,
        normalized_input=input_data,
        resolved_ids=plan.get("resolvedIds", {}),
        plan=plan,
        plan_hash=plan_hash,
    )
    if not spec.requires_confirmation:
        # 只写本地的写类命令（如下载）：计划照常冻结并登记台账，但不占用用户确认窗口，
        # 由 CLI 在同一轮内自证确认后直接执行。
        confirm_token = runtime.ledger.confirm_operation(
            operation_ref,
            profile=runtime.profile,
            plan_hash=plan_hash,
            confirmed_at=_iso_now(runtime),
        )
        return execute_operation(operation_ref, runtime, confirm_token=confirm_token)
    envelope = result_module.Envelope(
        spec.command_id,
        result_module.STATUS_CONFIRMATION_REQUIRED,
        data={
            "operationRef": operation_ref,
            "planHash": plan_hash,
            "plan": plan,
        },
        next_action=result_module.NEXT_ACTION_OPERATION_CONFIRM,
        trace_id=runtime.trace_id,
    )
    return envelope


def prepare_orchestration(runtime, step_texts):
    """冻结多步写计划：一个 operationRef、一次确认覆盖全部步骤。"""
    from cmcloud_core import orchestrator

    plan = orchestrator.plan_steps(runtime, step_texts)
    if not isinstance(plan.get("summary"), str) or not plan["summary"]:
        raise RuntimeError("orchestrate 的计划缺少 summary")
    plan_hash = ledger_module.plan_hash_of(plan)
    operation_ref = runtime.ledger.create_operation(
        profile=runtime.profile,
        scenario_id=runtime.spec.scenario_id,
        command_id=runtime.spec.command_id,
        normalized_input={"steps": list(step_texts)},
        resolved_ids=plan.get("resolvedIds") or {},
        plan=plan,
        plan_hash=plan_hash,
    )
    return result_module.Envelope(
        runtime.spec.command_id,
        result_module.STATUS_CONFIRMATION_REQUIRED,
        data={"operationRef": operation_ref, "planHash": plan_hash, "plan": plan},
        next_action=result_module.NEXT_ACTION_OPERATION_CONFIRM,
        trace_id=runtime.trace_id,
    )


def confirm_operation(operation_ref, runtime, *, plan_hash=None):
    """确认冻结计划；对话确认只在发布配置显式开启时由 CLI 提供。"""
    record = runtime.ledger.get_operation(operation_ref, runtime.profile)
    if record["status"] != ledger_module.OPERATION_PENDING:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "该 operationRef 不在待确认状态")
    if _parse_expiry(record["expiresAt"]) <= runtime.now():
        raise result_module.ExecutionError(result_module.PLAN_EXPIRED, "该计划的确认窗口已过期")
    if plan_hash is not None and plan_hash != record["planHash"]:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "planHash 与冻结计划不一致")
    event = runtime.confirmation_provider.confirmation_for(operation_ref)
    if not isinstance(event, dict):
        raise result_module.ExecutionError(
            result_module.CONFIRMATION_REQUIRED, "未取得与 planHash/profile 绑定的可信宿主确认事件"
        )
    if event.get("operationRef") != operation_ref:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "确认事件与当前操作不匹配")
    if event.get("planHash") != record["planHash"]:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "确认事件与冻结计划不一致")
    if event.get("profileRef") != record["profileRef"]:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "确认事件与当前 profile 不一致")
    if isinstance(runtime.confirmation_provider, FileConfirmationProvider):
        event = runtime.ledger.verify_confirmation_event(
            event,
            operation_ref=operation_ref,
            profile=runtime.profile,
            plan_hash=record["planHash"],
        )
    confirm_token = runtime.ledger.confirm_operation(
        operation_ref,
        profile=runtime.profile,
        plan_hash=record["planHash"],
        confirmed_at=event.get("confirmedAt") or _iso_now(runtime),
    )
    return result_module.Envelope(
        record["commandId"],
        result_module.STATUS_SUCCESS,
        data={"operationRef": operation_ref, "confirmed": True, "confirmToken": confirm_token},
        next_action=result_module.NEXT_ACTION_OPERATION_EXECUTE,
        trace_id=runtime.trace_id,
    )


def execute_operation(operation_ref, runtime, *, confirm_token):
    """从台账加载已确认计划并执行；不重新理解用户意图。"""
    record = runtime.ledger.get_operation(operation_ref, runtime.profile)
    spec = registry.get_scene(record["scenarioId"])
    if record["status"] != ledger_module.OPERATION_CONFIRMED:
        raise result_module.ExecutionError(
            result_module.CONFIRMATION_REQUIRED, "该 operationRef 尚未取得可信确认"
        )
    if record["planHash"] != ledger_module.plan_hash_of(record["plan"]):
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "冻结计划已被篡改")
    _assert_capability(spec, runtime)
    runtime.ledger.consume_confirmation_token(
        operation_ref, profile=runtime.profile, token=confirm_token
    )
    runtime.operation_ref = operation_ref
    try:
        outcome = _execute_frozen_plan(spec, runtime, record["plan"])
    except result_module.ExecutionError as err:
        last_result = {}
        if isinstance(err.data, dict):
            last_result.update(err.data)
        if isinstance(err.counts, dict):
            last_result["counts"] = err.counts
        last_result["code"] = err.code
        last_result["message"] = err.message
        current_record = runtime.ledger.get_operation(operation_ref, runtime.profile)
        unresolved_task = (
            spec.scenario_id in ("S20", "S21")
            and bool(current_record.get("taskRef"))
            and err.code in (
                result_module.UNKNOWN, result_module.UPSTREAM_TIMEOUT,
                result_module.TASK_STATUS_QUERY_FAILED,
            )
        )
        runtime.ledger.update_operation(
            operation_ref,
            status=ledger_module.OPERATION_EXECUTING if unresolved_task else ledger_module.OPERATION_FAILED,
            lastResult=last_result,
        )
        # Keep the business command and collected upstream trace on execution
        # failures instead of losing both in the outer control-command handler.
        return result_module.envelope_from_error(spec.command_id, err, runtime.trace_id)
    envelope = _envelope(spec, outcome, runtime)
    if envelope.status in (result_module.STATUS_SUCCESS, result_module.STATUS_EMPTY):
        status = ledger_module.OPERATION_EXECUTED
    elif envelope.status == result_module.STATUS_PARTIAL:
        status = ledger_module.OPERATION_PARTIAL
    elif envelope.status in (result_module.STATUS_RUNNING, result_module.STATUS_UNKNOWN):
        # 异步任务尚未取得终态：台账保持“已受理”，不得写成 FAILED（否则
        # operation.status 会短路返回伪造的失败，且掩盖仍可恢复的任务）。
        status = ledger_module.OPERATION_EXECUTING
    else:
        status = ledger_module.OPERATION_FAILED
    envelope_payload = envelope.to_dict()
    last_result = envelope_payload.get("data")
    if isinstance(last_result, dict):
        last_result = dict(last_result)
    elif last_result is None:
        last_result = {}
    else:
        last_result = {"data": last_result}
    if isinstance(envelope_payload.get("counts"), dict):
        last_result["counts"] = envelope_payload["counts"]
    runtime.ledger.update_operation(operation_ref, status=status, lastResult=last_result)
    return envelope


def _execute_frozen_plan(spec, runtime, plan):
    """执行已确认的冻结计划：编排计划交给 orchestrator，其余走本场景 workflow。"""
    if spec.scenario_id == registry.ORCHESTRATION_SCENARIO_ID:
        from cmcloud_core import orchestrator

        return orchestrator.execute_frozen(runtime, plan)
    return registry.workflow_callable(spec, "execute")(runtime, plan)


def query_operation_by_ref(operation_ref, runtime, spec=None):
    """按 operationRef 查询写入场景自己的异步任务；状态未知只查原任务，绝不重交写请求。"""
    if not operation_ref:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "缺少 operationRef")
    record = runtime.ledger.get_operation(operation_ref, runtime.profile)
    if spec is None:
        spec = registry.get_scene(record["scenarioId"])
    module = registry.workflow_module(spec)
    query_status = getattr(module, "query_status", None)
    if not callable(query_status):
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "该命令不支持异步状态查询")
    _assert_capability(spec, runtime)
    runtime.operation_ref = operation_ref
    outcome = query_status(runtime, record)
    return _envelope(spec, outcome, runtime)


def _envelope(spec, outcome, runtime):
    if isinstance(outcome, result_module.Envelope):
        return outcome
    if not isinstance(outcome, workflow_common.Outcome):
        raise RuntimeError("%s 的 workflow 必须返回 Outcome" % spec.command_id)
    return result_module.Envelope(spec.command_id, **outcome.envelope_kwargs(), trace_id=runtime.trace_id)


def _parse_expiry(text):
    return datetime.fromisoformat(text)


def _iso_now(runtime):
    return runtime.now().astimezone(timezone.utc).replace(microsecond=0).isoformat()


def capabilities_payload(domain=None):
    """按场景输出 capability：共享主命令的每个场景独立一行，共 35 行。"""
    scenes = registry.scenes_for_domain(domain) if domain is not None else registry.all_commands()
    items = []
    for scene in scenes:
        described = scene.describe()
        described["async"] = registry.supports_async(scene)
        items.append(described)
    payload = {"count": len(items), "commands": items}
    if domain is None or domain == registry.ORCHESTRATION_COMMAND_ID:
        payload["orchestration"] = orchestration_capability()
    return payload


def orchestration_capability():
    """写动作编排能力：Skill 依此决定是否把多个云端写动作合并为一次确认。

    `maxSteps` 为 `null` 表示不限制步数：同一请求的写动作有多少就编排多少。
    """
    return {
        "supported": True,
        "commandId": registry.ORCHESTRATION_COMMAND_ID,
        "maxSteps": None,
        "stepCommands": list(
            dict.fromkeys(
                scene.command_id
                for scene in registry.all_commands()
                if scene.requires_confirmation
            )
        ),
        "requiresConfirmation": True,
    }


def help_payload(domain=None, action=None):
    if domain is None:
        return {
            "domains": [
                {"domain": name, "summary": registry.DOMAIN_HELP.get(name)}
                for name in registry.domains()
            ],
            "commands": [scene.describe() for scene in registry.all_commands()],
        }
    if domain not in registry.DOMAIN_HELP:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "未登记的领域: %s" % domain
        )
    if action is not None:
        route = registry.get_route(domain, action)
        if route is None:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "未注册的命令: %s %s" % (domain, action))
        described = route.describe()
        described["scenes"] = [
            _describe_scene(scene) for scene in route.scenes
        ]
        described["schema"] = command_schemas.command_schema(route.command_id)
        return described
    routes = registry.commands_for_domain(domain)
    return {
        "domain": domain,
        "summary": registry.DOMAIN_HELP.get(domain),
        "commands": [scene.describe() for scene in registry.scenes_for_domain(domain)],
        "routes": [route.describe() for route in routes],
        "nextStep": "cmcloud schema --command-id <command-id> --format json",
    }


def _describe_scene(scene):
    described = scene.describe()
    described["schema"] = command_schemas.SCENARIO_SCHEMAS[scene.scenario_id]
    described["async"] = registry.supports_async(scene)
    return described


def schema_payload(command_id):
    route = registry.get_route_by_id(command_id)
    scenes = []
    for scene in route.scenes:
        scenes.append(
            {
                "scenarioId": scene.scenario_id,
                "schemaId": scene.schema_id,
                "capability": scene.capability_state,
                "disabledReason": scene.disabled_reason,
                "selector": [selector.describe() for selector in scene.selector],
                "schema": command_schemas.SCENARIO_SCHEMAS[scene.scenario_id],
            }
        )
    return {
        "commandId": route.command_id,
        "command": route.command,
        "shared": route.shared,
        "selectorFields": list(route.selector_fields),
        "schemaId": scenes[0]["schemaId"] if len(scenes) == 1 else None,
        "capability": scenes[0]["capability"] if len(scenes) == 1 else None,
        "disabledReason": scenes[0]["disabledReason"] if len(scenes) == 1 else None,
        "schema": command_schemas.command_schema(route.command_id),
        "scenes": scenes,
    }


def _adapter_error_code(err):
    """adapter 异常的错误码：优先取显式 code，否则用异常类名。"""
    code = getattr(err, "code", None)
    if code:
        return str(code)
    return err.__class__.__name__


def _response_trace_id(response):
    """单次 adapter 响应自身的 traceId（区别于 RuntimeContext 里按调用顺序拼接的值）。"""
    if isinstance(response, dict):
        trace_id = response.get("traceId")
        if isinstance(trace_id, str) and trace_id.strip():
            return trace_id.strip()
    return None
