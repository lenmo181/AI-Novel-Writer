"""KMS-01 登录取票包装：`cmcloud auth login` 的唯一 SDK 调用边界。

设计依据与边界（执行 md 第 3、4、5、9、10 节）：
- 本文件只调用一个 SDK 公共接口 ``token_service.getToken(...)``；配置下发、设备识别、
  凭据复用、数字信封、preAuth 全部留在 SDK 内部，不在此拆分为独立步骤或文件；
- 入参来源固定：``devId/clientId/skillId/skillName/strategyId/templateId/scopeIds`` 取自受控
  profile 登记；``clawUid`` 由 SDK 从可信宿主/私有状态自动识别；``uuid_param`` 由 SDK 读取
  私有授权状态。以上均不接受 Agent 提交，命中即 INVALID_ARGUMENT；
- ``setConfig`` 初始化复用 :func:`auth_idaas_common.configure_sdk`：Keychain 不可用
  直接返回 ``SECURE_STORE_UNAVAILABLE``，禁止任何明文降级；
- 统一信封、错误码、状态、退出码与脱敏一律复用 ``core_result.py`` 与 ``auth_idaas_common.py``，
  本文件不新增错误码枚举、不维护第二套状态映射；
- SDK 的 ``_log``/``print`` 由 ``call_sdk`` 捕获并脱敏写入 stderr，保证 stdout 只有一个 JSON；
- 服务端绑定类正式码尚未冻结，故不臆造码值：表外或缺失码按 md 第 5.8/4.3.2 节统一归一为
  ``UNKNOWN``；已冻结的绑定映射可通过受控注入 ``binding_codes`` 生效；
- 完整 ``creditToken`` 默认不外发、不落盘、不打日志，仅作为本次取票就绪判据。
"""

from cmcloud_core import result as result_module
from cmcloud_auth import idaas_common, idaas_profile, ticket_provider

DOMAIN = "auth"
ACTION = "login"
COMMAND_ID = "auth.login"

DEFAULT_FORMAT = "json"
SUPPORTED_FORMATS = (DEFAULT_FORMAT,)

OPTION_PROFILE = "profile"
OPTION_FORMAT = "format"

AUTHORIZATION_STATE_AUTHORIZED = "AUTHORIZED"
AUTHORIZATION_STATE_PENDING = idaas_common.AUTHORIZATION_STATE_PENDING

AUTH_REQUIRED_MESSAGE = idaas_common.AUTH_REQUIRED_MESSAGE

# 绑定不一致码表：服务端“关系类（实例/应用/账号/设备）”与“签名/IP/时钟校验类”的正式码
# 未冻结，默认空表 → 未冻结码按 md 5.8 归一为 UNKNOWN，绝不猜测码值。受控运行环境可通过
# ``binding_codes`` 注入已冻结映射（{"<SDK 码>": "relationship"|"verification"}）。
DEFAULT_BINDING_CODES = {}

BINDING_MESSAGES = {
    result_module.BINDING_RELATIONSHIP: "当前账号、设备或应用与云盘授权绑定不一致，请重新授权或联系管理员",
    result_module.BINDING_VERIFICATION: "请求校验（签名/IP/时钟）与云盘授权绑定不一致，请检查运行环境后重试",
}

# authPageUrl 受控主机白名单：空表示只做结构校验（http(s) + 合法主机 + 无凭据 + 无控制字符）。
ALLOWED_AUTH_HOSTS = ()


def run(
    profile=None,
    *,
    format=DEFAULT_FORMAT,
    extra_options=None,
    modules=None,
    binding_codes=None,
    allowed_auth_hosts=None,
    trace_id=None,
    forced_auth=False,
):
    """`cmcloud auth login` 入口：始终返回统一九字段信封，不向上抛未处理异常。

    Args:
        profile: ``--profile`` 受控身份环境引用；``None`` 取登记文件的 defaultProfile。
        format: ``--format``，当前只允许 ``json``。
        extra_options: 调用方额外收到的 CLI 参数（映射或字段名列表）；命中敏感/受控字段或
            未登记参数即返回 INVALID_ARGUMENT。
        modules: 受控测试用 SDK 句柄注入（默认由公共底座动态加载）。
        binding_codes: 受控注入的已冻结「SDK 码 → 绑定类型」映射（默认空表）。
        allowed_auth_hosts: 受控注入的授权页主机白名单（默认空表示仅结构校验）。
        trace_id: 编排层已有的 traceId；缺省由本次命令生成。
        forced_auth: 仅已确认的 auth.reauthorize 内部使用；CLI 不开放此参数。

    Returns:
        core_result.Envelope：SUCCESS / AUTH_REQUIRED / 统一错误信封之一。
    """
    resolved_trace = trace_id or idaas_common.new_trace_id()
    with idaas_common.trace_scope(resolved_trace):
        try:
            return _execute(
                profile,
                format_value=format,
                extra_options=extra_options,
                modules=modules,
                binding_codes=binding_codes,
                allowed_auth_hosts=allowed_auth_hosts,
                trace_id=resolved_trace,
                forced_auth=forced_auth,
            )
        except result_module.ExecutionError as err:
            return idaas_common.error_envelope(COMMAND_ID, err, trace_id=resolved_trace)
        except Exception as err:  # 兜底：任何未预期异常都归一为 UNKNOWN，不让枚举异常冒泡
            idaas_common.diagnostic("auth.login 未预期异常: %s" % err.__class__.__name__)
            return idaas_common.error_envelope(
                COMMAND_ID,
                idaas_common.make_error(
                    result_module.UNKNOWN, "auth.login 结果无法可靠判定，请携带 traceId 复核"
                ),
                trace_id=resolved_trace,
            )


# CLI 分派别名：与 run 完全等价，便于注册表按 action 名直接引用。
login = run


def _execute(profile, *, format_value, extra_options, modules, binding_codes, allowed_auth_hosts, trace_id,
             forced_auth=False):
    """校验入参 → 加载受控 profile → setConfig 初始化 → getToken → 结果映射。"""
    profile, format_value = _resolve_control_options(extra_options, profile, format_value)
    _assert_format(format_value)
    profile_ref = _normalize_profile_ref(profile)
    auth_profile = idaas_profile.load_profile(profile_ref)
    try:
        sdk = idaas_common.configure_sdk(auth_profile, modules=modules, trace_id=trace_id)
    except result_module.ExecutionError as err:
        raise _configuration_error(err, auth_profile)
    raw = idaas_common.acquire_token(sdk, auth_profile, command=COMMAND_ID, forced_auth=forced_auth)
    if forced_auth:
        code = idaas_common.normalize_sdk_code(raw.get("resultCode")) if isinstance(raw, dict) else ""
        if code in ("103000", "130088"):
            return _unknown_envelope(trace_id, "服务端未确认发起新的授权会话，请用 auth login 核对，不要重复强制授权")
        if code == idaas_common.SDK_CODE_AUTH_REQUIRED:
            data = raw.get("data") or {}
            session = data.get("uuid")
            if (not isinstance(session, str) or not session.strip()
                    or idaas_common._current_pending_uuid_fingerprint(sdk, auth_profile)
                    != idaas_common.fingerprint(session.strip())):
                raise idaas_common.make_error(
                    result_module.SECURE_STORE_UNAVAILABLE,
                    "新授权会话未能保存或回读，请用 auth login 核对，不要重复强制授权", next_action=None)
            raw = dict(raw, _cmcloudForcedAuth=True)
    return _map_result(
        auth_profile,
        raw,
        _binding_table(binding_codes),
        _host_allowlist(allowed_auth_hosts),
        trace_id,
    )


# ---------------------------------------------------------------------------
# 入参校验（CLI 只允许 --profile / --format）
# ---------------------------------------------------------------------------


def _assert_format(format_value):
    value = DEFAULT_FORMAT if format_value is None else format_value
    if not isinstance(value, str) or value.strip() not in SUPPORTED_FORMATS:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "auth.login 当前仅支持 --format json"
        )


def _resolve_control_options(extra_options, profile, format_value):
    """解析 --profile/--format：拒绝 Agent 提交登记参数/身份材料，并拒绝未登记参数。"""
    provided = _option_mapping(extra_options)
    idaas_profile.assert_no_agent_fields(provided)
    unknown = sorted(name for name in provided if name not in (OPTION_PROFILE, OPTION_FORMAT))
    if unknown:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "auth.login 只接受 --profile 与 --format，收到未登记参数: %s" % ", ".join(unknown),
        )
    resolved_profile = _resolve_duplicate(profile, provided.get(OPTION_PROFILE), OPTION_PROFILE)
    resolved_format = _resolve_duplicate(format_value, provided.get(OPTION_FORMAT), OPTION_FORMAT)
    return resolved_profile, resolved_format


def _option_mapping(extra_options):
    if extra_options is None:
        return {}
    if isinstance(extra_options, dict):
        return {str(name): value for name, value in extra_options.items()}
    if isinstance(extra_options, (list, tuple, set, frozenset)):
        return {str(name): None for name in extra_options}
    raise result_module.ExecutionError(
        result_module.INVALID_ARGUMENT, "auth.login 的命令参数必须是映射或字段名列表"
    )


def _resolve_duplicate(primary, duplicate, option_name):
    """控制参数可来自显式入参或 extra_options；两者同时出现必须一致，不做静默取舍。"""
    if duplicate is None:
        return primary
    if primary is None:
        return duplicate
    if str(duplicate) != str(primary):
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "auth.login 的 %s 参数重复且取值不一致" % option_name
        )
    return primary


def _normalize_profile_ref(profile):
    if profile is None:
        return None
    if not isinstance(profile, str) or not profile.strip():
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "--profile 必须是非空字符串"
        )
    return profile.strip()


# ---------------------------------------------------------------------------
# 结果映射（md 5.6 / 5.7 / 5.8）
# ---------------------------------------------------------------------------


def _map_result(auth_profile, raw, binding_codes, allowed_hosts, trace_id):
    if not isinstance(raw, dict):
        return _unknown_envelope(trace_id, "SDK 返回结构无法识别，请携带 traceId 复核")
    code = idaas_common.normalize_sdk_code(raw.get("resultCode"))
    if not code:
        return _unknown_envelope(trace_id, "SDK 未返回可判定的结果码，请携带 traceId 复核")

    binding_kind = binding_codes.get(code)
    if binding_kind is not None:
        idaas_common.diagnostic(
            "auth.login 绑定不一致 profileRef=%s bindingType=%s errorCode=%s traceId=%s"
            % (auth_profile.profile_ref, binding_kind, result_module.BINDING_MISMATCH, trace_id)
        )
        return idaas_common.error_envelope(
            COMMAND_ID,
            idaas_common.make_error(
                result_module.BINDING_MISMATCH,
                BINDING_MESSAGES[binding_kind],
                binding_type=binding_kind,
            ),
            trace_id=trace_id,
        )

    if idaas_common.is_sdk_success(code):
        return _authorized_envelope(auth_profile, raw, trace_id)
    if idaas_common.is_auth_entry_result(code):
        return _pending_envelope(auth_profile, raw, allowed_hosts, trace_id)
    if idaas_common.is_pending_session(code) and idaas_common.has_auth_entry(raw):
        # 130088：本机待授权会话仍未完成。入口由本机缓存重放（见 acquire_token），
        # 会话保持不变——用户刚完成的那次授权才能被后续取票认领。
        return _pending_envelope(auth_profile, raw, allowed_hosts, trace_id)
    idaas_common.diagnostic_sdk_result(COMMAND_ID, raw)
    return _failed_envelope(auth_profile, code, trace_id)


def _authorized_envelope(auth_profile, raw, trace_id):
    """103000：校验并安全保存新票据；完整票据不外发。

    这是用户明确重新授权的路径，设备身份若已变化以本次授权为准更新元数据；
    授权已就绪，待授权入口缓存同时作废，避免后续重放已经用掉的入口。
    """
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    # 票据有效期属凭据信息，不进回执（references/auth.md「输出追加项」的已授权口径）。
    ticket_provider.persist_sdk_ticket(auth_profile, data, reauthorized=True)
    ticket_provider.clear_pending_entry(auth_profile)
    idaas_common.diagnostic(
        "auth.login 成功 profileRef=%s ticketReady=true traceId=%s" % (auth_profile.profile_ref, trace_id)
    )
    return idaas_common.success_envelope(
        COMMAND_ID,
        data={
            "profileRef": auth_profile.profile_ref,
            "authorizationState": AUTHORIZATION_STATE_AUTHORIZED,
            "ticketReady": True,
        },
        trace_id=trace_id,
    )


def _pending_envelope(auth_profile, raw, allowed_hosts, trace_id):
    """待授权回执：校验授权入口来源，返回受控 challengeRef 与授权入口。

    本回执本身就是授权入口来源，`nextAction` 保持 null，不重复下发 auth.login。
    clawUid 由 SDK 内部解析，包装层不读取 SDK 私有状态，故不做 clawUid 绑定。
    来源分两种：`103001` 服务端现取现发（缓存下来供后续 130088 重放）；
    `130088` 重放本机缓存入口（会话未变，不刷新缓存、也不换会话）。
    """
    payload = idaas_common.pending_authorization_payload(
        auth_profile, raw, allowed_hosts=allowed_hosts
    )
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    if idaas_common.is_auth_entry_result(raw.get("resultCode")):
        ticket_provider.remember_pending_entry(
            auth_profile,
            idaas_common.validate_auth_page_url(data.get("authPageUrl"), allowed_hosts),
            payload["challengeRef"],
            session_ref=idaas_common.session_ref(data),
            reauthorization=raw.get("_cmcloudForcedAuth") is True,
        )
    # 记录设备身份来源；身份只能现场生成时不再阻断，授权入口照常下发。
    ticket_provider.note_identity_source(auth_profile)
    idaas_common.diagnostic(
        "auth.login 需要用户授权 profileRef=%s challengeRef=%s traceId=%s"
        % (auth_profile.profile_ref, payload["challengeRef"], trace_id)
    )
    credential_state = raw.get("_cmcloudCredentialFileState")
    message = idaas_common._SDK_CREDENTIAL_FAILURE_MESSAGES.get(
        credential_state, AUTH_REQUIRED_MESSAGE
    )
    return idaas_common.auth_required_envelope(
        COMMAND_ID,
        data=payload,
        message=message,
        trace_id=trace_id,
        next_action=None,
    )


def _failed_envelope(auth_profile, code, trace_id):
    """其他码全部走公共映射；取票查询超时保留只读可重试语义并返回 UNKNOWN。"""
    mapped = idaas_common.map_sdk_result_code(code)
    operation = (
        result_module.OPERATION_READ
        if mapped == result_module.UPSTREAM_TIMEOUT
        else None
    )
    error = idaas_common.error_from_sdk(code, operation_type=operation)
    idaas_common.diagnostic(
        "auth.login 失败 profileRef=%s errorCode=%s traceId=%s"
        % (auth_profile.profile_ref, error.code, trace_id)
    )
    return idaas_common.error_envelope(COMMAND_ID, error, trace_id=trace_id)


def _unknown_envelope(trace_id, message):
    return idaas_common.error_envelope(
        COMMAND_ID,
        idaas_common.make_error(result_module.UNKNOWN, message),
        trace_id=trace_id,
    )


# 受控初始化失败文案：公共底座对 SECURE_STORE_UNAVAILABLE 的说明可能带上 AuthProfile 明细
# （含本机私有目录与登记参数），此处按对外脱敏要求重建，只保留 profileRef。
_CONFIGURATION_MESSAGES = {
    result_module.CAPABILITY_UNAVAILABLE: "auth 运行环境不可用（SDK 文件或依赖不完整）",
}


def _store_unavailable_message(auth_profile):
    """存储不可用说明必须与 profile 的 storage_backend 一致，否则排障被引向错误的恢复动作。"""
    if auth_profile.uses_keychain:
        return "系统 Keychain 存储后端不可用，已拒绝任何明文降级"
    return "本机状态目录不可写，已拒绝任何明文降级"


def _configuration_error(err, auth_profile):
    if err.code == result_module.SECURE_STORE_UNAVAILABLE:
        base = _store_unavailable_message(auth_profile)
    else:
        base = _CONFIGURATION_MESSAGES.get(err.code)
    if base is None:
        return err
    return idaas_common.make_error(
        err.code, "%s（profileRef: %s）" % (base, auth_profile.profile_ref), next_action=None
    )


# ---------------------------------------------------------------------------
# 授权入口来源校验与受控注入
# ---------------------------------------------------------------------------


def _host_allowlist(allowed_auth_hosts):
    """auth.login 侧的授权页主机白名单：结构校验复用公共底座，默认空表。"""
    raw = ALLOWED_AUTH_HOSTS if allowed_auth_hosts is None else allowed_auth_hosts
    return idaas_common.normalize_auth_hosts(raw)


def _binding_table(binding_codes):
    """已冻结绑定码表：默认空，只有显式注入的码才会映射为 BINDING_MISMATCH。"""
    table = dict(DEFAULT_BINDING_CODES)
    if binding_codes is None:
        return table
    if not isinstance(binding_codes, dict):
        raise result_module.ResultError("binding_codes 必须是「SDK 码 → 绑定类型」的映射")
    for raw_code, kind in binding_codes.items():
        code = idaas_common.normalize_sdk_code(raw_code)
        if not code:
            continue
        table[code] = idaas_common.binding_type(kind)
    return table


__all__ = (
    "ACTION",
    "COMMAND_ID",
    "DOMAIN",
    "login",
    "run",
)
