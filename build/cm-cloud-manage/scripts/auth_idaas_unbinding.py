"""auth 域 KMS-02 远端解绑包装：本文件只调用一个 SDK 公共接口 token_service.unbinding。

边界（与需求文档第 3、4.1~4.3.5、6、9、10、11、12 节一致）：
- 只调用 ``token_service.unbinding``；本地 accessToken 读取、配置下发、设备识别、签名与
  数字信封全部由 SDK 内部完成，本文件不拆分、也不暴露这些内部步骤；
- devId/clientId/skillId/strategyId/scopeIds 一律来自受控 profile 登记，Agent 不能提交；
  accessToken 与 clawUid 既不接收也不回显（clawUid 由 SDK 自动识别）；
- 两阶段契约：``plan_unbinding`` 只生成不可变计划并返回 CONFIRMATION_REQUIRED，
  ``execute_unbinding`` 必须在取得与 profile/planHash 绑定的可信确认后才调用 SDK；
  未取得可信确认时绝不调用 ``unbinding``；
- 失败绝不删除本地凭据伪装成功，本文件不调用 clear_credentials；
- 复用 ``core_result.py`` 的统一信封、18 种错误码、状态与退出码；本模块只提供
  「SDK 原始码 → 统一错误码」的接口级映射，不新建枚举、不新建状态/退出码表；
- 所有 SDK 调用都在 stdout 隔离下完成，诊断日志只写 stderr 且脱敏；不新增第三方依赖。
"""

import inspect
from datetime import datetime, timedelta, timezone

from cmcloud_core import result as result_module
from cmcloud_auth import idaas_common as ai
from cmcloud_auth import idaas_profile
from cmcloud_auth import ticket_provider

# 对外命令/接口标识：本文件只包装这一个 SDK 公共接口。
COMMAND_ID = "auth.revoke"
SDK_INTERFACE = "token_service.unbinding"

# CLI scope 到服务端 scopeIds 的正式范围映射。
# 初期正式契约只开放全量解绑 ``all``；其他值一律 INVALID_ARGUMENT。
# None 表示不向 SDK 传 scopeIds，由服务端按当前 skill 的全量授权关系处理解绑。
SCOPE_SERVER_VALUES = {"all": None}
ALLOWED_SCOPES = tuple(SCOPE_SERVER_VALUES)

# 冻结计划的确认窗口（秒）；plan_unbinding 默认写入 data.expiresAt，
# 超窗后必须重新生成计划，禁止复用旧确认（与 idaas_clear_credentials 口径一致）。
PLAN_TTL_SECONDS = 300

# SDK 成功码 / 本地无凭据码。
SDK_CODE_SUCCESS = "103000"
SDK_CODE_NO_LOCAL_CREDENTIALS = "200016"

# SDK 原始码 → (统一错误码, make_error 附加参数)。
# 只登记需求第 6.8 节明确要求的码；表外或缺失码统一归一为 UNKNOWN。
SDK_CODE_ERROR_MAP = {
    # 受控登记参数非法：参数来自 profile（Agent 不可提交），属受控配置错误 → FAILED。
    "200001": (result_module.FAILED, {}),
    "200002": (result_module.FAILED, {}),
    "200003": (result_module.FAILED, {}),
    "200014": (result_module.FAILED, {}),
    # 写请求可能已送达 → UNKNOWN，不得自动重发。
    "200005": (result_module.UNKNOWN, {}),
    # 写请求超时：显式传 operation_type=write，公共结果层自动得到 retryable=false、status=UNKNOWN。
    "200006": (result_module.UPSTREAM_TIMEOUT, {"operation_type": result_module.OPERATION_WRITE}),
    "200007": (result_module.UPSTREAM_CONTRACT_MISMATCH, {}),
    "200008": (result_module.UNKNOWN, {}),
    "200013": (result_module.CAPABILITY_UNAVAILABLE, {}),
}

# 自建文案：不回显 SDK resultDesc，避免夹带身份材料或原始响应。
UNBINDING_MESSAGES = {
    SDK_CODE_NO_LOCAL_CREDENTIALS: "本地没有可用云盘授权凭据，未向服务端提交解绑请求",
    "200001": "远端解绑的受控登记参数不合法（devId），未向服务端提交解绑请求",
    "200002": "远端解绑的受控登记参数不合法（clientId），未向服务端提交解绑请求",
    "200003": "远端解绑的受控登记参数不合法（skillId），未向服务端提交解绑请求",
    "200014": "远端解绑的受控登记参数不合法（strategyId），未向服务端提交解绑请求",
    "200005": "远端解绑请求网络异常，服务端是否已解绑无法判定，请勿自动重发",
    "200006": "远端解绑请求超时，服务端是否已解绑无法判定，请勿自动重发",
    "200007": "远端解绑响应不符合冻结契约，已暂停该能力",
    "200008": "IDaaS 内部异常，远端解绑结果无法判定",
    "200013": "本机加密能力不可用，远端解绑未执行",
}

DEFAULT_UNBINDING_MESSAGE = "IDaaS 返回未登记结果，远端解绑结果无法判定"

CONFIRMATION_MESSAGE = "远端解绑会使当前云盘授权失效，需要确认后执行"

NO_CONFIRMATION_MESSAGE = "未取得与 profile/planHash 绑定的可信确认事件，远端解绑未执行"

PLAN_EXPIRED_MESSAGE = "原冻结计划已过期，请重新生成计划后再执行 operation.confirm，不得复用旧 operationRef"

__all__ = [
    "COMMAND_ID",
    "SDK_INTERFACE",
    "ALLOWED_SCOPES",
    "SCOPE_SERVER_VALUES",
    "PLAN_TTL_SECONDS",
    "plan_unbinding",
    "execute_unbinding",
    "confirmation_from",
]


# ---------------------------------------------------------------------------
# 阶段一：生成不可变计划（不调用任何 SDK 接口）
# ---------------------------------------------------------------------------


def plan_unbinding(
    profile=idaas_profile.DEFAULT_PROFILE,
    *,
    scope=None,
    profiles_path=None,
    pending_operations=None,
    ttl_seconds=PLAN_TTL_SECONDS,
    clock=None,
    trace_id=None,
    overrides=None,
):
    """阶段一：校验 profile/scope，生成不可变计划并返回 CONFIRMATION_REQUIRED。

    只做受控登记读取与计划冻结，不加载 SDK、不访问 Keychain、不发起任何网络请求，
    因此计划结果可重复复算（planHash 与 profile/skill/scope/operationRef 逐字节绑定）。

    Args:
        profile: 受控身份环境引用，默认 default。
        scope: 必填；正式影响范围，初期仅允许 all。
        profiles_path: 受控登记文件路径覆盖（受控测试用）。
        pending_operations: 未完成任务数；未知时保持 None，绝不臆造。
        ttl_seconds: 计划确认窗口秒数；默认 PLAN_TTL_SECONDS，写入 data.expiresAt，
            供编排层与执行阶段做计划过期校验。
        clock: 可选；返回当前时间的可调用对象，便于受控测试。
        trace_id: 可选；未传时由 CLI 生成。
        overrides: 预留的登记覆盖位；任何受控/敏感字段都会直接 INVALID_ARGUMENT。

    Returns:
        result.Envelope：CONFIRMATION_REQUIRED（nextAction=operation.confirm）或统一错误信封。
    """
    trace = trace_id or ai.new_trace_id()
    with ai.trace_scope(trace):
        try:
            ttl = _require_ttl(ttl_seconds)
            scope_value = _require_scope(scope)
            resolved = _load(resolved_profile=profile, profiles_path=profiles_path, overrides=overrides)
            pending = _normalize_pending_operations(pending_operations)
            operation_ref = ai.operation_ref(
                resolved.profile,
                command=COMMAND_ID,
                skill_id=resolved.skill_id,
                scope=scope_value,
            )
            plan = _plan_payload(resolved, scope_value, operation_ref)
            plan_hash = _plan_hash(resolved, scope_value, plan)
            data = {
                "profileRef": resolved.profile_ref,
                "scope": scope_value,
                "operationRef": operation_ref,
                "planHash": plan_hash,
                "impactSummary": _impact_summary(pending),
                "expiresAt": _iso(_now(clock) + timedelta(seconds=ttl)),
            }
            _diagnose_impact(resolved, scope_value, pending)
            return ai.confirmation_envelope(
                COMMAND_ID, data=data, message=CONFIRMATION_MESSAGE, trace_id=trace
            )
        except result_module.ExecutionError as err:
            return ai.error_envelope(COMMAND_ID, err, trace_id=trace)
        except Exception as err:  # 未预期异常不向上暴露堆栈
            return ai.error_envelope(
                COMMAND_ID,
                ai.make_error(
                    result_module.UNKNOWN, "生成远端解绑计划失败（%s）" % err.__class__.__name__
                ),
                trace_id=trace,
            )


# ---------------------------------------------------------------------------
# 阶段二：可信确认后执行（唯一调用 token_service.unbinding 的入口）
# ---------------------------------------------------------------------------


def execute_unbinding(
    profile=idaas_profile.DEFAULT_PROFILE,
    *,
    scope=None,
    confirmation=None,
    confirmation_provider=None,
    profiles_path=None,
    modules=None,
    clock=None,
    trace_id=None,
    overrides=None,
):
    """阶段二：校验可信确认后调用 ``token_service.unbinding``。

    可信确认必须与 profile/planHash/影响范围绑定：
    - 缺少确认、或确认未携带 profileRef/planHash 绑定信息 → CONFIRMATION_REQUIRED；
    - 确认与当前 profile 或影响范围不一致 → INVALID_ARGUMENT；
    - 确认计划已过期 → PLAN_EXPIRED（nextAction=null，禁止复用旧 operationRef）。

    Args:
        profile: 受控身份环境引用，默认 default。
        scope: 必填；必须与计划阶段一致。
        confirmation: 可信确认映射（也可直接传计划信封或 ``Envelope``），或返回映射的可调用提供者。
        confirmation_provider: 备用确认提供者（映射或可调用）；confirmation 为空时使用。
        profiles_path: 受控登记文件路径覆盖（受控测试用）。
        modules: 可选；已加载的 SDK 句柄（受控测试注入，避免真实依赖）。
        clock: 可选；返回当前时间的可调用对象。
        trace_id: 可选；未传时由 CLI 生成。
        overrides: 预留的登记覆盖位；任何受控/敏感字段都会直接 INVALID_ARGUMENT。

    Returns:
        result.Envelope：统一九字段信封；失败绝不删除本地凭据。
    """
    trace = trace_id or ai.new_trace_id()
    with ai.trace_scope(trace):
        try:
            scope_value = _require_scope(scope)
            resolved = _load(resolved_profile=profile, profiles_path=profiles_path, overrides=overrides)
            event = _resolve_confirmation(
                resolved.profile_ref, confirmation, confirmation_provider
            )
            _assert_bound_confirmation(event, resolved, scope_value, clock)
            ai.diagnostic(
                "已取得与 planHash/profile 绑定的可信确认，调用 %s profileRef=%s skillId=%s scope=%s"
                % (SDK_INTERFACE, resolved.profile_ref, resolved.skill_id, scope_value)
            )
            sdk = ai.configure_sdk(resolved, modules=modules, trace_id=trace)
            raw = ai.call_sdk(
                sdk.token_service.unbinding, command=SDK_INTERFACE, **_sdk_kwargs(resolved, scope_value)
            )
            return _envelope_for_result(raw, resolved, scope_value, trace)
        except result_module.ExecutionError as err:
            return ai.error_envelope(COMMAND_ID, err, trace_id=trace)
        except Exception as err:  # 未预期异常不向上暴露堆栈
            return ai.error_envelope(
                COMMAND_ID,
                ai.make_error(
                    result_module.UNKNOWN, "远端解绑执行出现未预期错误（%s）" % err.__class__.__name__
                ),
                trace_id=trace,
            )


# ---------------------------------------------------------------------------
# 编排辅助：由计划信封 data 构造可信确认
# ---------------------------------------------------------------------------


def confirmation_from(plan_data, *, expires_at=None, confirmed_at=None):
    """由计划信封（或信封 data）构造可信确认映射，供 operation.confirm 后回传执行阶段。"""
    source = _unwrap_plan_data(plan_data)
    if source is None:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "计划 data 必须是映射")
    event = {
        "profileRef": source.get("profileRef"),
        "scope": source.get("scope"),
        "operationRef": source.get("operationRef"),
        "planHash": source.get("planHash"),
    }
    expires = expires_at if expires_at is not None else source.get("expiresAt")
    if expires is not None:
        event["expiresAt"] = expires
    if confirmed_at is not None:
        event["confirmedAt"] = confirmed_at
    return event


# ---------------------------------------------------------------------------
# 计划内容：不可变、可复算、不含任何身份材料
# ---------------------------------------------------------------------------


def _plan_payload(resolved, scope_value, operation_ref):
    """冻结计划：字段全部为登记值/字面量，确认时可逐字节复算 planHash。"""
    return {
        "action": "revoke",
        "command": COMMAND_ID,
        "sdkInterface": SDK_INTERFACE,
        "profileRef": resolved.profile_ref,
        "skillId": resolved.skill_id,
        "scope": scope_value,
        "remoteUnbind": True,
        "clearsLocalCredentials": False,
        "operationRef": operation_ref,
    }


def _plan_hash(resolved, scope_value, plan):
    # profile 传 profile 名字符串（底层按 profileRef/skillId/scope 绑定），clawUid 不参与。
    return ai.plan_hash(
        resolved.profile,
        command=COMMAND_ID,
        plan=plan,
        skill_id=resolved.skill_id,
        scope=scope_value,
    )


def _impact_summary(pending_operations):
    # 本次解绑恰好影响 1 条授权绑定；未完成任务数未知时保持 null，绝不臆造。
    return {
        "authorizationBinding": 1,
        "pendingOperations": pending_operations,
    }


def _diagnose_impact(resolved, scope_value, pending_operations):
    """影响范围只写脱敏诊断：环境/Skill/权限/未完成任务摘要，不含任何凭据。"""
    ai.diagnostic(
        "远端解绑影响范围 profileRef=%s skillId=%s skillName=%s scope=%s scopeIds=%s storageBackend=%s pendingOperations=%s"
        % (
            resolved.profile_ref,
            resolved.skill_id,
            resolved.skill_name,
            scope_value,
            list(resolved.scope_ids),
            resolved.storage_backend,
            pending_operations,
        )
    )


# ---------------------------------------------------------------------------
# 参数与确认校验
# ---------------------------------------------------------------------------


def _require_scope(scope):
    if not isinstance(scope, str) or not scope.strip():
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "缺少必填参数 --scope，当前版本仅支持 all"
        )
    value = scope.strip().lower()
    if value not in SCOPE_SERVER_VALUES:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "不支持的解绑影响范围 --scope，当前版本仅支持 all",
        )
    return value


def _normalize_pending_operations(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "pendingOperations 必须是非负整数或 null"
        )
    return value


def _require_ttl(ttl_seconds):
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "ttl_seconds 必须是正整数秒")
    return ttl_seconds


def _load(resolved_profile, profiles_path, overrides):
    """统一入口：任何受控/敏感字段经 overrides 提交都会在底座被拒绝。"""
    return idaas_profile.load_profile(resolved_profile, path=profiles_path, overrides=overrides)


def _resolve_confirmation(profile_ref, confirmation, confirmation_provider):
    """按优先级解析可信确认事件：显式 confirmation → confirmation_provider。"""
    event = _resolve_from(profile_ref, confirmation)
    if event is not None:
        return event
    return _resolve_from(profile_ref, confirmation_provider)


def _resolve_from(profile_ref, source):
    if source is None:
        return None
    if isinstance(source, result_module.Envelope):
        source = source.to_dict()
    if isinstance(source, dict):
        return _unwrap_confirmation(source)
    if callable(source):
        produced = source(profile_ref) if _takes_argument(source) else source()
        return _unwrap_confirmation(produced)
    raise result_module.ExecutionError(
        result_module.INVALID_ARGUMENT, "confirmation 必须是可信确认映射或可调用提供者"
    )


def _unwrap_confirmation(value):
    if value is None:
        return None
    if isinstance(value, result_module.Envelope):
        value = value.to_dict()
    if not isinstance(value, dict):
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "可信确认事件必须是映射"
        )
    # 容忍直接传入计划信封或其 to_dict()：解出 data 并把信封级有效信息一并带上。
    if "operationRef" not in value and isinstance(value.get("data"), dict):
        merged = dict(value["data"])
        for key in ("expiresAt", "expired", "confirmedAt"):
            if key in value and key not in merged:
                merged[key] = value[key]
        return merged
    return value


def _unwrap_plan_data(value):
    if isinstance(value, result_module.Envelope):
        value = value.to_dict()
    if isinstance(value, dict) and isinstance(value.get("data"), dict):
        value = value["data"]
    return value if isinstance(value, dict) else None


def _takes_argument(provider):
    try:
        signature = inspect.signature(provider)
    except (TypeError, ValueError):
        return True
    for parameter in signature.parameters.values():
        if parameter.kind == parameter.VAR_POSITIONAL:
            return True
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD):
            if parameter.default is parameter.empty:
                return True
    return False


def _assert_bound_confirmation(event, resolved, scope_value, clock):
    if not isinstance(event, dict):
        raise result_module.ExecutionError(
            result_module.CONFIRMATION_REQUIRED, NO_CONFIRMATION_MESSAGE
        )
    _reject_forbidden_fields(event)
    if event.get("expired") is True:
        raise _plan_expired(resolved, scope_value)
    _assert_not_expired(event.get("expiresAt"), resolved, scope_value, clock)
    if event.get("profileRef") != resolved.profile_ref:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "可信确认事件与当前 profile 不一致"
        )
    if _normalize_event_scope(event.get("scope")) != scope_value:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "可信确认事件与本次解绑影响范围不一致"
        )
    operation_ref = event.get("operationRef")
    plan_hash = event.get("planHash")
    if not _is_text(operation_ref) or not _is_text(plan_hash):
        raise result_module.ExecutionError(
            result_module.CONFIRMATION_REQUIRED, NO_CONFIRMATION_MESSAGE
        )
    plan = _plan_payload(resolved, scope_value, operation_ref.strip())
    if _plan_hash(resolved, scope_value, plan) != plan_hash.strip():
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "可信确认事件与冻结计划不一致，请重新生成计划"
        )


def _reject_forbidden_fields(event):
    hit = sorted({str(name) for name in event} & idaas_profile.FORBIDDEN_AGENT_FIELDS)
    if hit:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "可信确认事件含受控字段，不接受 Agent 提交: %s" % ", ".join(hit),
        )


def _assert_not_expired(expires_at, resolved, scope_value, clock):
    if expires_at is None:
        return
    if not _is_text(expires_at):
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "可信确认事件的 expiresAt 必须是非空字符串或 null"
        )
    try:
        moment = datetime.fromisoformat(expires_at.strip())
    except ValueError:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "可信确认事件的 expiresAt 不是合法时间"
        )
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if moment <= _now(clock):
        raise _plan_expired(resolved, scope_value)


def _plan_expired(resolved, scope_value):
    # 不回显旧 operationRef/planHash，避免被重新复用。
    return result_module.ExecutionError(
        result_module.PLAN_EXPIRED,
        PLAN_EXPIRED_MESSAGE,
        data={"profileRef": resolved.profile_ref, "scope": scope_value, "planExpired": True},
    )


def _normalize_event_scope(scope):
    if not isinstance(scope, str):
        return None
    return scope.strip().lower()


def _is_text(value):
    return isinstance(value, str) and bool(value.strip())


# ---------------------------------------------------------------------------
# SDK 入参与结果映射
# ---------------------------------------------------------------------------


def _sdk_kwargs(resolved, scope_value):
    """从受控 profile 适配 SDK 入参；clawUid 与 accessToken 不由 CLI 传入。"""
    registration = resolved.registration_fields()
    kwargs = {
        "devId": registration["devId"],
        "clientId": registration["clientId"],
        "skillId": registration["skillId"],
        "strategyId": registration["strategyId"],
    }
    scope_ids = SCOPE_SERVER_VALUES.get(scope_value)
    if scope_ids is not None:
        kwargs["scopeIds"] = scope_ids
    return kwargs


def _envelope_for_result(raw, resolved, scope_value, trace):
    if not isinstance(raw, dict):
        raw = {}
    code = ai.normalize_sdk_code(raw.get("resultCode"))
    ai.diagnostic("%s 返回原始码: %s" % (SDK_INTERFACE, code or "<missing>"))
    if code == SDK_CODE_SUCCESS:
        # 远端解绑成功；本地凭据一律不由本文件清理（localCredentialsCleared 恒为 false）。
        ticket_provider.clear_cached_ticket(resolved)
        return ai.success_envelope(
            COMMAND_ID,
            data={
                "profileRef": resolved.profile_ref,
                "scope": scope_value,
                "serverUnbound": True,
                "localCredentialsCleared": False,
            },
            trace_id=trace,
        )
    if code == SDK_CODE_NO_LOCAL_CREDENTIALS:
        return ai.error_envelope(COMMAND_ID, _no_local_credentials_error(resolved, scope_value), trace_id=trace)
    mapped, extra = SDK_CODE_ERROR_MAP.get(code, (result_module.UNKNOWN, {}))
    return ai.error_envelope(
        COMMAND_ID, ai.make_error(mapped, _message_for(code), **extra), trace_id=trace
    )


def _no_local_credentials_error(resolved, scope_value):
    """200016 语义选择：按 AUTH_REQUIRED 处理。

    SDK 在 200016 时尚未发起任何服务端请求（本地预检 accessToken 失败即返回），服务端
    没有给出权威的“未绑定”结论，因此不能声称远端已解绑；按“需要先完成云盘授权”处理，
    并显式标注 serverUnbound=false、localCredentialsCleared=false。
    """
    return ai.make_error(
        result_module.AUTH_REQUIRED,
        UNBINDING_MESSAGES[SDK_CODE_NO_LOCAL_CREDENTIALS],
        # 退出账号途中不拉起授权：本域 AUTH_REQUIRED 只表示"远端未解绑、本地仍需清理"。
        next_action=None,
        data={
            "profileRef": resolved.profile_ref,
            "scope": scope_value,
            "serverUnbound": False,
            "localCredentialsCleared": False,
            "note": "SDK 本地预检未找到 accessToken（原始码 200016），未向服务端提交解绑请求，不能判定远端已解绑",
        },
    )


def _message_for(code):
    if code in UNBINDING_MESSAGES:
        return UNBINDING_MESSAGES[code]
    return DEFAULT_UNBINDING_MESSAGE


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------


def _now(clock=None):
    moment = clock() if clock is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _iso(moment):
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()
