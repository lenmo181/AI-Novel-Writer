"""KMS-05 Keychain 凭据状态包装：``cmcloud auth status`` 的唯一 SDK 调用边界。

规格边界（requirement.md 第 8、3、4.1~4.3.5、9、10、11、12 节）：
- 本文件只调用一个 SDK 公共接口 ``keychain_store.status(skill_id=...)``：
  ``--all true`` 时调用 ``status(None)``，由 SDK 内部 ``list_skills()`` 枚举全部已登记 skill；
  ``--all false``（默认）时从受控 profile 解析目标 ``skillId`` 后调用 ``status(skillId)``；
  不额外暴露 ``list_skills`` / ``get_auth_bundle`` / ``get_claw_uid_masked`` 等内部能力；
- 按 profile 的 ``storage_backend`` 分流，两种后端**不互相静默改查**：``keychain`` 走上述唯一
  SDK 公共接口；``file`` 不查 Keychain 槽位，改为回本机凭据文件的存在性与 ``localCredentials``
  摘要（``backend="file"``）；系统 Keychain 不可用时返回 ``SECURE_STORE_UNAVAILABLE``，
  绝不降级到明文文件；
- ``status`` 的返回值只说明 Keychain 中对应键是否存在，**不等同于凭据当前可用**：
  由于 ``revoke_access_token`` 在 Keychain 后端会写入空字符串，本文件不得把
  ``access_token=true`` 解释成“存在可用 token”，也不得把 ``auth status`` 当作取票接口；
  file 后端的 ``skills`` 同义——只说明本机凭据文件是否存在；
- 输出 data 固定为 ``backend`` / ``skills`` / ``note`` 三个字段（单 profile 模式另带
  ``localCredentials``：本机票据是否存在与到期时间、设备身份来源摘要、本机凭据文件读取状态
  ``credentialFile``），不含任何凭据值、完整 clawUid 明文或 Keychain key/value；``counts``
  默认 ``null``，仅 ``--all true`` 允许使用 ``counts.total``；``page`` 恒为 ``null``；
- 统一信封、错误码、状态、退出码与脱敏一律复用 ``core_result.py`` 与 ``auth_idaas_common.py``，
  本文件不新增错误码枚举、不维护第二套状态映射、不新增第三方依赖；
- 只读查询：超时按 ``operation_type="read"`` 映射为 ``UPSTREAM_TIMEOUT``（默认可重试），
  本地 Keychain 读写不可用映射为 ``SECURE_STORE_UNAVAILABLE``，表外/缺失结果按公共层
  归一为 ``UNKNOWN`` 且 ``nextAction`` 保持 ``null``。

CLI 分派约定（T006）::

    from cmcloud_auth import idaas_keychain_status

    envelope = idaas_keychain_status.run(
        profile=None,          # --profile <ref>，缺省取登记文件 defaultProfile
        all_skills=False,      # --all true|false（只接受小写 true/false）
        format="json",         # --format，仅允许 json
        extra_options=None,    # 其余 CLI 参数（映射或字段名列表）
    )
    print(envelope.to_json())
    raise SystemExit(envelope.exit_code())

本模块只返回 ``result.Envelope``：stdout 单一 JSON 与进程退出码由 CLI 分派层输出，
诊断日志经 ``idaas_common.diagnostic`` 写 stderr 且脱敏。
"""

import os

from cmcloud_core import result as result_module
from cmcloud_auth import credential_store
from cmcloud_auth import idaas_common as ai
from cmcloud_auth import idaas_profile

# 对外命令标识，与 CLI 分派层 ``cmcloud auth status`` 对齐。
COMMAND_ID = "auth.status"

# 本文件唯一调用的 SDK 模块与公共接口。
SDK_MODULE = "keychain_store"
SDK_INTERFACE = "status"

DEFAULT_FORMAT = "json"
SUPPORTED_FORMATS = (DEFAULT_FORMAT,)

OPTION_PROFILE = "profile"
OPTION_ALL = "all"
OPTION_FORMAT = "format"

# ``--all`` 只接受小写布尔字面量，其余取值一律 INVALID_ARGUMENT。
ALL_VALUES = ("true", "false")

# 输出 data.backend 取值：按 profile 的 storage_backend 分流，两者不互相静默改查。
BACKEND_KEYCHAIN = idaas_profile.STORAGE_BACKEND_KEYCHAIN
BACKEND_FILE = idaas_profile.STORAGE_BACKEND_FILE

# Keychain 中每个 skill 的三个槽位（与 SDK status 返回的键名一致）。
SLOT_KEYS = ("claw_uid", "access_token", "uuid")

# file 后端下同一语义：本机凭据文件是否存在（不解析内容，只查存在性）。
FILE_SLOT_KEYS = ("auth_info", "claw_uid")
AUTH_INFO_FILENAME = "auth_info_ydrz.json"

# 固定提示：键存在不等于凭据可用（见 requirement.md 8.3/8.4 与验收标准）。
STATUS_NOTE = (
    "字段表示 Keychain 键是否存在，不等同于凭据当前可用；"
    "localCredentials 为本机票据与设备身份来源摘要，不含任何凭据值"
)

# 设备身份来源取值：无法判定时统一按 unknown 输出。
IDENTITY_SOURCE_UNKNOWN = "unknown"

FILE_STATUS_NOTE = (
    "file 后端：字段表示本机凭据文件是否存在，不等同于凭据当前可用；"
    "localCredentials 为本机票据与设备身份来源摘要，不含任何凭据值"
)
SECURE_STORE_MESSAGE = "系统 Keychain 存储后端不可用，已拒绝任何明文降级，也不继续查询凭据状态"
CONTRACT_MESSAGE = "SDK keychain_store.status 返回值不符合冻结契约"
TIMEOUT_MESSAGE = "本机 Keychain 状态查询超时，可稍后重试"
UNKNOWN_MESSAGE = "auth.status 结果无法可靠判定，请携带 traceId 复核"


def run(
    profile=None,
    *,
    all_skills=False,
    format=DEFAULT_FORMAT,
    extra_options=None,
    modules=None,
    secure_store_probe=None,
    profiles_path=None,
    overrides=None,
    trace_id=None,
):
    """``cmcloud auth status`` 入口：始终返回统一九字段信封，不向上抛未处理异常。

    Args:
        profile: ``--profile`` 受控身份环境引用；``None`` 取登记文件的 defaultProfile。
            仅 ``--all false`` 的自单 profile 语义下参与 skillId 解析。
        all_skills: ``--all``；``True`` 走全局诊断（``status(None)`` 枚举全部已登记 skill），
            只接受布尔或小写字面量 ``"true"`` / ``"false"``。
        format: ``--format``，当前只允许 ``json``。
        extra_options: CLI 分派层收到的其余参数（映射或字段名列表）；只允许
            ``profile`` / ``all`` / ``format``，命中受控/敏感字段或未登记参数即 INVALID_ARGUMENT。
        modules: 受控测试用 SDK 句柄注入：可传 ``idaas_common.SdkModules``，
            也可直接传提供 ``status(skill_id)`` 的 keychain_store 句柄。
        secure_store_probe: 受控测试用 Keychain 可用性探测；注入 modules 时默认视为可用。
        profiles_path: 受控登记文件路径覆盖（受控测试用）。
        overrides: 预留的登记覆盖位；任何受控/敏感字段都会在底座被拒绝。
        trace_id: 编排层已有的 traceId；缺省由本次命令生成。

    Returns:
        result.Envelope：SUCCESS / EMPTY（空注册表）/ 统一错误信封之一。
    """
    resolved_trace = trace_id or ai.new_trace_id()
    with ai.trace_scope(resolved_trace):
        try:
            return _execute(
                profile,
                all_skills=all_skills,
                format_value=format,
                extra_options=extra_options,
                modules=modules,
                secure_store_probe=secure_store_probe,
                profiles_path=profiles_path,
                overrides=overrides,
                trace_id=resolved_trace,
            )
        except result_module.ExecutionError as err:
            return ai.error_envelope(COMMAND_ID, err, trace_id=resolved_trace)
        except Exception as err:  # 兜底：未预期异常归一为 UNKNOWN，不让枚举异常冒泡
            ai.diagnostic("auth.status 未预期异常: %s" % err.__class__.__name__)
            return ai.error_envelope(
                COMMAND_ID,
                ai.make_error(result_module.UNKNOWN, UNKNOWN_MESSAGE),
                trace_id=resolved_trace,
            )


# CLI 分派别名：与 run 完全等价，便于注册表按 action 名直接引用。
status = run


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _execute(
    profile,
    *,
    all_skills,
    format_value,
    extra_options,
    modules,
    secure_store_probe,
    profiles_path,
    overrides,
    trace_id,
):
    """校验入参 → 判定模式 → 校验存储可用性 → 调用唯一 SDK 接口 → 结果映射。"""
    options = _option_mapping(extra_options)
    _assert_allowed_options(options)
    profile_value = _resolve_duplicate(profile, options.get(OPTION_PROFILE), OPTION_PROFILE)
    all_value = _resolve_all_option(options, all_skills)
    format_value = _resolve_duplicate(format_value, options.get(OPTION_FORMAT), OPTION_FORMAT)
    _assert_format(format_value)
    profile_ref = _normalize_profile_ref(profile_value)
    probe = _probe_for(secure_store_probe, modules)

    if all_value:
        _reject_profile_conflict(profile_ref)
        # 全局诊断：不解析单一 profile，只要求本机 Keychain 可用。
        idaas_profile.require_secure_store(idaas_profile.DEFAULT_PROFILE, probe=probe)
        raw = _call_status(_resolve_keychain_store(modules), None, trace_id=trace_id)
        return _status_envelope(raw, all_skills=True, auth_profile=None, trace_id=trace_id)

    auth_profile = _load_profile(profile_ref, profiles_path, overrides)
    if not auth_profile.uses_keychain:
        # file 后端：不查 Keychain 槽位，改为回本机凭据文件存在性与本地摘要。
        return _file_status_envelope(auth_profile, trace_id)
    idaas_profile.require_profile_storage(auth_profile, probe=probe)
    raw = _call_status(_resolve_keychain_store(modules), auth_profile.skill_id, trace_id=trace_id)
    return _status_envelope(raw, all_skills=False, auth_profile=auth_profile, trace_id=trace_id)


# ---------------------------------------------------------------------------
# 入参校验（CLI 只允许 --profile / --all / --format）
# ---------------------------------------------------------------------------


def _assert_allowed_options(options):
    """拒绝 Agent 提交登记参数/身份材料，并拒绝未登记参数。"""
    idaas_profile.assert_no_agent_fields(options)
    unknown = sorted(
        name for name in options if name not in (OPTION_PROFILE, OPTION_ALL, OPTION_FORMAT)
    )
    if unknown:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "auth.status 只接受 --profile / --all / --format，收到未登记参数: %s" % ", ".join(unknown),
        )


def _assert_format(format_value):
    value = DEFAULT_FORMAT if format_value is None else format_value
    if not isinstance(value, str) or value.strip() not in SUPPORTED_FORMATS:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "auth.status 当前仅支持 --format json"
        )


def _resolve_all_option(options, all_skills):
    """合并 ``--all``：显式入参与 CLI 映射同时出现且不一致时直接拒绝。"""
    if OPTION_ALL in options:
        value = _normalize_all(options.get(OPTION_ALL))
        if all_skills and not value:
            raise result_module.ExecutionError(
                result_module.INVALID_ARGUMENT, "auth.status 的 --all 参数重复且取值不一致"
            )
        return value
    return _normalize_all(all_skills)


def _normalize_all(value):
    """``--all`` 只接受布尔或小写 ``true`` / ``false``；其余取值 INVALID_ARGUMENT。"""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip() in ALL_VALUES:
        return value.strip() == ALL_VALUES[0]
    raise result_module.ExecutionError(
        result_module.INVALID_ARGUMENT, "auth.status 的 --all 取值只接受小写 true 或 false"
    )


def _option_mapping(extra_options):
    if extra_options is None:
        return {}
    if isinstance(extra_options, dict):
        return {str(name): value for name, value in extra_options.items()}
    if isinstance(extra_options, (list, tuple, set, frozenset)):
        return {str(name): None for name in extra_options}
    raise result_module.ExecutionError(
        result_module.INVALID_ARGUMENT, "auth.status 的命令参数必须是映射或字段名列表"
    )


def _resolve_duplicate(primary, duplicate, option_name):
    """控制参数可来自显式入参或 extra_options；两者同时出现必须一致，不做静默取舍。"""
    if duplicate is None:
        return primary
    if primary is None:
        return duplicate
    if str(duplicate) != str(primary):
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "auth.status 的 %s 参数重复且取值不一致" % option_name
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


def _reject_profile_conflict(profile_ref):
    """``--all true`` 是全局诊断，不能与非默认 profile 的单 skill 语义混用。"""
    if profile_ref is not None and profile_ref != idaas_profile.DEFAULT_PROFILE:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "auth.status 的 --all true 为全局诊断，不能与非默认 profile 的单 skill 语义混用，"
            "请二选一",
        )


def _load_profile(profile_ref, profiles_path, overrides):
    """统一入口：任何受控/敏感字段经 overrides 提交都会在底座被拒绝。"""
    return idaas_profile.load_profile(profile_ref, path=profiles_path, overrides=overrides)


# ---------------------------------------------------------------------------
# SDK 句柄与可用性探测
# ---------------------------------------------------------------------------


def _resolve_keychain_store(modules):
    """取 keychain_store 句柄：受控注入优先，否则按公共底座动态加载。"""
    if modules is None:
        return ai.load_keychain_store()
    store = getattr(modules, "keychain_store", None)
    if callable(getattr(store, "status", None)):
        return store
    if callable(getattr(modules, "status", None)):
        return modules
    return ai.load_keychain_store()


def _probe_for(secure_store_probe, modules):
    """探测优先级：显式探测 → 注入 SDK 句柄（受控测试，视为可用）→ 底座真实探测。"""
    if secure_store_probe is not None:
        return secure_store_probe
    if modules is not None:
        return _always_available
    return None


def _always_available():
    return True


def _call_status(store, skill_id, *, trace_id):
    """在 stdout 隔离下调用唯一 SDK 公共接口，并把底层异常映射到统一错误码。"""
    try:
        return ai.call_sdk(store.status, skill_id, command=SDK_INTERFACE)
    except result_module.ExecutionError:
        raise
    except TimeoutError:
        # 只读查询超时：由公共结果层得到 retryable=true、status=UNKNOWN。
        raise ai.make_error(
            result_module.UPSTREAM_TIMEOUT,
            TIMEOUT_MESSAGE,
            operation_type=result_module.OPERATION_READ,
            next_action=None,
        )
    except RuntimeError as err:
        ai.diagnostic("keychain_store.status 本机存储不可用: %s" % err.__class__.__name__)
        raise ai.make_error(result_module.SECURE_STORE_UNAVAILABLE, SECURE_STORE_MESSAGE, next_action=None)
    except Exception as err:
        ai.diagnostic("keychain_store.status 未预期异常: %s" % err.__class__.__name__)
        raise ai.make_error(result_module.UNKNOWN, UNKNOWN_MESSAGE, next_action=None)


# ---------------------------------------------------------------------------
# 结果映射与输出
# ---------------------------------------------------------------------------


def _status_envelope(raw, *, all_skills, auth_profile, trace_id):
    """把 SDK 返回值转换为统一信封；空注册表按 EMPTY 返回（skills 为空对象）。"""
    skills = _extract_skills(raw)
    data = {"backend": BACKEND_KEYCHAIN, "skills": skills, "note": STATUS_NOTE}
    if auth_profile is not None:
        data["localCredentials"] = _local_credential_summary(auth_profile)
    counts = {"total": len(skills)} if all_skills else None
    _diagnose(all_skills, auth_profile, len(skills))
    if not skills:
        return ai.empty_envelope(COMMAND_ID, data=data, counts=counts, page=None, trace_id=trace_id)
    return ai.success_envelope(COMMAND_ID, data=data, counts=counts, page=None, trace_id=trace_id)


def _file_status_envelope(auth_profile, trace_id):
    """file 后端：只回本机凭据文件存在性与本地摘要，不接触 Keychain 槽位。"""
    skills = {auth_profile.skill_id: _file_slot_summary(auth_profile)}
    data = {
        "backend": BACKEND_FILE,
        "skills": skills,
        "note": FILE_STATUS_NOTE,
        "localCredentials": _local_credential_summary(auth_profile),
    }
    ai.diagnostic(
        "auth.status backend=%s mode=single profileRef=%s skillId=%s（仅表示本机凭据文件是否存在）"
        % (BACKEND_FILE, auth_profile.profile_ref, auth_profile.skill_id)
    )
    return ai.success_envelope(COMMAND_ID, data=data, counts=None, page=None, trace_id=trace_id)


def _file_slot_summary(auth_profile):
    """file 后端槽位摘要：只查存在性，不读取、不解析文件内容。"""
    base = getattr(auth_profile, "auth_info_path", None)
    if not isinstance(base, str) or not base:
        return {slot: False for slot in FILE_SLOT_KEYS}
    return {
        "auth_info": os.path.exists(os.path.join(base, AUTH_INFO_FILENAME)),
        "claw_uid": os.path.exists(
            os.path.join(base, credential_store.FILE_BACKEND_CLAW_UID_FILENAME)
        ),
    }


def _local_credential_summary(auth_profile):
    """本机票据与设备身份摘要：只回存在性与时间/来源，不含凭据值。

    摘要读取失败不影响 SDK 槽位状态输出：本地凭据属诊断附加信息，读不到按 null 返回。
    """
    try:
        ticket = credential_store.load(auth_profile, credential_store.KIND_TICKET)
        meta = credential_store.load(auth_profile, credential_store.KIND_CLAWUID_META)
    except result_module.ExecutionError:
        ai.diagnostic("auth.status 本地凭据摘要读取失败，已按未知处理")
        return None
    present = _ticket_present(ticket)
    from cmcloud_auth import agent_scope

    agent_code = getattr(auth_profile, "agent_code", None) or agent_scope.FALLBACK_AGENT_CODE
    namespace = agent_scope.scope_namespace(agent_code, auth_profile.profile_ref)
    recorded_namespace = (meta or {}).get("namespace")
    return {
        "ticket": {
            "present": present,
            "expiresAt": ticket.get("expiresAt") if present else None,
        },
        # 本机凭据文件的读取状态：区分"没登录(absent)/文件损坏(corrupt)/换机指纹不符
        # (fingerprint_mismatch)/版本不兼容(version_unsupported)"，避免一律当成未登录。
        "credentialFile": credential_store.credential_state(auth_profile),
        "deviceIdentity": {
            "sourceKind": (meta or {}).get("sourceKind") or IDENTITY_SOURCE_UNKNOWN,
            "verifiedAt": (meta or {}).get("verifiedAt"),
            # 平台与命名空间：授权按智能体隔离，这里让"这次落在哪个智能体"可见
            "agentCode": agent_code,
            "agentName": agent_scope.agent_display_name(agent_code),
            "agentSource": getattr(auth_profile, "agent_source", None),
            "namespace": namespace,
            # 识别结果相对上次记录发生变化：不是静默失效，而是提示可能需要重新授权
            "agentChanged": bool(recorded_namespace) and recorded_namespace != namespace,
        },
    }


def _ticket_present(ticket):
    if not isinstance(ticket, dict):
        return False
    token = ticket.get("creditToken")
    return isinstance(token, str) and bool(token.strip())


def _extract_skills(raw):
    """严格校验 SDK 返回结构，只保留三个槽位的键存在性布尔值。"""
    if not isinstance(raw, dict) or not isinstance(raw.get("skills"), dict):
        raise _contract_error("SDK keychain_store.status 缺少 skills 对象")
    entries = {}
    for skill_id, entry in raw["skills"].items():
        if not isinstance(skill_id, str) or not skill_id.strip():
            raise _contract_error("SDK keychain_store.status 的 skill 标识必须是非空字符串")
        if not isinstance(entry, dict):
            raise _contract_error("SDK keychain_store.status 的 skill 状态必须是对象")
        entries[skill_id] = {slot: _slot_present(entry.get(slot), slot) for slot in SLOT_KEYS}
    return {skill_id: entries[skill_id] for skill_id in sorted(entries)}


def _slot_present(value, slot):
    """槽位状态只允许布尔；None 视为键不存在（SDK 缺失该键时），其他类型视为契约不符。"""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise _contract_error("SDK keychain_store.status 的 %s 槽位状态必须是布尔值" % slot)


def _contract_error(message):
    return ai.make_error(result_module.UPSTREAM_CONTRACT_MISMATCH, message, next_action=None)


def _diagnose(all_skills, auth_profile, skill_count):
    """诊断只写脱敏摘要：模式、profileRef/skillId 与数量，不含任何键值或凭据。"""
    if all_skills:
        ai.diagnostic("auth.status backend=%s mode=all skills=%d（仅表示 Keychain 键是否存在）"
                      % (BACKEND_KEYCHAIN, skill_count))
        return
    ai.diagnostic(
        "auth.status backend=%s mode=single profileRef=%s skillId=%s skills=%d（仅表示 Keychain 键是否存在）"
        % (BACKEND_KEYCHAIN, auth_profile.profile_ref, auth_profile.skill_id, skill_count)
    )
