"""KMS-04 本地退出包装：只调用 ``token_service.clear_credentials(skillId=...)``。

规格边界（requirement.md 第 7、4.2、4.3、9、10、11、12 节）：
- 本文件是 ``cmcloud auth logout`` 的 SDK 调用边界，只调用一个 SDK 公共接口；
  不调用任何服务端解绑接口（不调用 ``unbinding``），远端授权关系可能仍然存在；
- 该命令是破坏性本地写操作：没有与冻结计划（profileRef/scope/operationRef/planHash）
  绑定的可信确认时，绝不调用 ``clear_credentials``，也不接受 ``--yes`` 这类未绑定的布尔参数；
- Keychain 后端清理当前 skill 的 clawUid / accessToken / uuid；文件后端删除 profile +
  skill 隔离目录下的 auth_info_ydrz.json 与 clawUid.json；
  宿主的 ``config.clawUid_path`` / ``config.fallback_clawUid_path`` 与其他 skill 的凭据
  一律不动（路径隔离与存储可用性由受控 profile 与底层 ``setConfig`` 保证）；
- 状态、错误码、退出码、信封与脱敏一律复用 core_result.py 与 auth_idaas_common.py，
  不新增错误码枚举、不新增第三方依赖；
- 本模块只返回 Envelope：stdout 单一 JSON 文档与进程退出码由 CLI 分派层输出，
  诊断日志经 ``idaas_common.diagnostic`` 写 stderr 且脱敏。

两阶段调用约定（T006 CLI 分派）::

    # 阶段一：冻结影响范围，等待可信确认（不加载 SDK、不删除任何文件）
    envelope = plan_logout(profile="default")
    # status="CONFIRMATION_REQUIRED"、error.code="CONFIRMATION_REQUIRED"、
    # nextAction="operation.confirm"、exit_code()==3
    # data = {profileRef, scope, operationRef, planHash, impactSummary, expiresAt}

    # 阶段二：确认后执行（把计划信封/data 经 confirmation_from 转换后回传）
    event = confirmation_from(envelope)
    envelope = execute_logout(profile="default", confirmation=event)
    # status="SUCCESS"、data={profileRef, localCredentialsCleared, serverUnbound}

确认载体必须是映射、JSON 字符串、Envelope 或返回映射的可调用提供者，且必须携带
``profileRef``、``scope``、``operationRef``、``planHash``；可选 ``expiresAt`` 与
``confirmedAt``。planHash 与 command/profile/skill/scope/operationRef/冻结内容逐字节
绑定并可在执行阶段复算，因此未绑定计划、被改动或过期的确认都会被拒绝。
"""

import inspect
import json
import os
from datetime import datetime, timedelta, timezone

from cmcloud_core import result as result_module
from cmcloud_auth import idaas_common as ai
from cmcloud_auth import idaas_profile
from cmcloud_auth import ticket_provider

# 对外命令标识，与 CLI 分派层 ``cmcloud auth logout`` 对齐。
COMMAND_ID = "auth.logout"

# 本文件唯一调用的 SDK 公共接口。
SDK_INTERFACE = "clear_credentials"

# 冻结计划的确认窗口（秒）；超窗后必须重新生成计划，禁止复用旧确认。
PLAN_TTL_SECONDS = 300

# 本地退出的固定影响范围：只清理本机凭据，绝不含服务端解绑。
LOGOUT_SCOPE = "local"

# 文件后端下 SDK 清理的授权凭据目标（与 token_service 的布局一致）。
# fallback_clawUid.json 是本机+智能体的稳定实例标识，不是授权凭据；退出后保留，
# 以便下次重新授权仍使用相同 clawUid。
FILE_BACKEND_TARGET_FILENAMES = ("auth_info_ydrz.json", "clawUid.json")

CONFIRMATION_MESSAGE = "本地退出会清理本机该 profile/skill 的凭据，需要与 planHash 绑定的可信确认"
NO_CONFIRMATION_MESSAGE = "未取得与 profile/planHash 绑定的可信确认事件，本地凭据未清理"
PLAN_EXPIRED_MESSAGE = "原冻结计划已过期，请重新生成计划后再执行 operation.confirm，不得复用旧 operationRef"

_RETAINED_ITEMS = (
    "宿主配置的 clawUid_path 与本技能 fallback_clawUid_path 保持不动，实例标识不会因退出而重置",
    "其他 skill 与其他 profile 的本地凭据保持不动",
    "服务端授权关系（远端绑定）保持不动",
)

_IMPACT_NOTE = "该操作只清理本机该 profile/skill 的本地凭据，不等同于云端解绑"


# ---------------------------------------------------------------------------
# 阶段一：生成不可变计划（唯一入口是 plan_logout，绝不触碰 SDK）
# ---------------------------------------------------------------------------


def plan_logout(
    profile=idaas_profile.DEFAULT_PROFILE,
    *,
    profiles_path=None,
    ttl_seconds=PLAN_TTL_SECONDS,
    clock=None,
    trace_id=None,
    overrides=None,
):
    """阶段一：校验受控 profile，生成不可变计划并返回 CONFIRMATION_REQUIRED。

    只做受控登记读取与计划冻结：不加载 SDK、不探测 Keychain、不删除任何文件，
    因此计划结果可重复复算（planHash 与 profile/skill/scope/operationRef 及冻结内容
    逐字节绑定）。

    Args:
        profile: 受控身份环境引用，默认 default。
        profiles_path: 受控登记文件路径覆盖（受控测试用）。
        ttl_seconds: 计划确认窗口秒数；默认 PLAN_TTL_SECONDS，写入 data.expiresAt。
        clock: 可选；返回当前时间的可调用对象（或 epoch 秒），便于受控测试。
        trace_id: 可选；未传时由 CLI 生成。
        overrides: 预留的登记覆盖位；任何受控/敏感字段都会直接 INVALID_ARGUMENT。

    Returns:
        result.Envelope：CONFIRMATION_REQUIRED（nextAction=operation.confirm）或统一错误信封。
    """
    trace = trace_id or ai.new_trace_id()
    with ai.trace_scope(trace):
        try:
            ttl = _require_ttl(ttl_seconds)
            resolved = _load(profile, profiles_path, overrides)
            operation_ref = ai.operation_ref(
                resolved.profile,
                command=COMMAND_ID,
                skill_id=resolved.skill_id,
                scope=LOGOUT_SCOPE,
            )
            plan = _plan_payload(resolved, operation_ref)
            data = {
                "profileRef": resolved.profile_ref,
                "scope": LOGOUT_SCOPE,
                "operationRef": operation_ref,
                "planHash": _plan_hash(resolved, plan),
                "impactSummary": _impact_summary(resolved),
                "expiresAt": _iso(_now(clock) + timedelta(seconds=ttl)),
            }
            _diagnose_impact(resolved)
            return ai.confirmation_envelope(
                COMMAND_ID, data=data, message=CONFIRMATION_MESSAGE, trace_id=trace
            )
        except result_module.ExecutionError as err:
            return ai.error_envelope(COMMAND_ID, err, trace_id=trace)
        except Exception as err:  # 未预期异常不向上暴露堆栈
            return ai.error_envelope(
                COMMAND_ID,
                ai.make_error(
                    result_module.UNKNOWN, "生成本地退出计划失败（%s）" % err.__class__.__name__
                ),
                trace_id=trace,
            )


# ---------------------------------------------------------------------------
# 阶段二：可信确认后执行（唯一调用 token_service.clear_credentials 的入口）
# ---------------------------------------------------------------------------


def execute_logout(
    profile=idaas_profile.DEFAULT_PROFILE,
    *,
    confirmation=None,
    confirmation_provider=None,
    profiles_path=None,
    modules=None,
    clock=None,
    trace_id=None,
    overrides=None,
    verify_local_files=True,
):
    """阶段二：校验可信确认后调用 ``token_service.clear_credentials(skillId=...)``。

    可信确认必须与 profile/scope/planHash 绑定：
    - 缺少确认、确认未携带 profileRef/scope/operationRef/planHash 绑定信息 → CONFIRMATION_REQUIRED；
    - 确认与当前 profile、影响范围或冻结计划不一致 → INVALID_ARGUMENT；
    - 确认携带 `expired=true` 或 expiresAt 已过期 → PLAN_EXPIRED（nextAction=null，禁止复用旧确认）；
    - Keychain 不可用或清理失败 → SECURE_STORE_UNAVAILABLE（禁止任何明文降级）；
    - 文件后端清理后目标文件仍在 → FAILED；文件本来不存在视为幂等成功 → SUCCESS。

    Args:
        profile: 受控身份环境引用，默认 default，必须与确认内的 profileRef 一致。
        confirmation: 可信确认映射 / JSON 字符串 / Envelope / 计划 data。
        confirmation_provider: 备用确认提供者（映射或可调用），confirmation 为空时使用。
        profiles_path: 受控登记文件路径覆盖（受控测试用）。
        modules: 可选；已加载的 SDK 句柄（受控测试注入，避免真实依赖）。
            可传 ``idaas_common.SdkModules``，也可直接传提供 ``setConfig`` /
            ``clear_credentials`` 的 token_service 句柄。
        clock: 可选；返回当前时间的可调用对象（或 epoch 秒）。
        trace_id: 可选；未传时由 CLI 生成。
        overrides: 预留的登记覆盖位；任何受控/敏感字段都会直接 INVALID_ARGUMENT。
        verify_local_files: 文件后端是否在清理后复核目标文件确实不存在（默认复核，
            以便本地结果可明确判定为 SUCCESS/FAILED）。

    Returns:
        result.Envelope：成功输出 profileRef / localCredentialsCleared / serverUnbound。
    """
    trace = trace_id or ai.new_trace_id()
    with ai.trace_scope(trace):
        try:
            resolved = _load(profile, profiles_path, overrides)
            event = _resolve_confirmation(resolved.profile_ref, confirmation, confirmation_provider)
            _assert_bound_confirmation(event, resolved, clock)
            ai.diagnostic(
                "%s 已取得与 planHash/profile 绑定的可信确认 profileRef=%s skillId=%s backend=%s"
                % (SDK_INTERFACE, resolved.profile_ref, resolved.skill_id, resolved.storage_backend)
            )
            sdk = ai.configure_sdk(resolved, modules=_resolve_modules(modules), trace_id=trace)
            cleared = ai.call_sdk(
                sdk.token_service.clear_credentials, command=SDK_INTERFACE, **_sdk_kwargs(resolved)
            )
            return _envelope_for_result(cleared, resolved, verify_local_files, trace)
        except result_module.ExecutionError as err:
            return ai.error_envelope(COMMAND_ID, err, trace_id=trace)
        except Exception as err:  # 未预期异常不向上暴露堆栈
            return ai.error_envelope(
                COMMAND_ID,
                ai.make_error(
                    result_module.UNKNOWN, "本地退出执行出现未预期错误（%s）" % err.__class__.__name__
                ),
                trace_id=trace,
            )


# ---------------------------------------------------------------------------
# 编排辅助：由计划信封/data 构造可信确认
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


def _plan_payload(resolved, operation_ref):
    """冻结计划：字段全部为登记值/字面量，确认时可逐字节复算 planHash。"""
    return {
        "action": "logout",
        "command": COMMAND_ID,
        "sdkInterface": SDK_INTERFACE,
        "profileRef": resolved.profile_ref,
        "skillId": resolved.skill_id,
        "scope": LOGOUT_SCOPE,
        "localOnly": True,
        "clearsLocalCredentials": True,
        "serverUnbound": False,
        "operationRef": operation_ref,
    }


def _plan_hash(resolved, plan):
    # profile 传 profile 名字符串（底层按 profileRef/skillId/scope 绑定），clawUid 不参与。
    return ai.plan_hash(
        resolved.profile,
        command=COMMAND_ID,
        plan=plan,
        skill_id=resolved.skill_id,
        scope=LOGOUT_SCOPE,
    )


def _impact_summary(resolved):
    """影响摘要：清理目标与明确不受影响的范围，不含宿主路径与任何凭据值。"""
    return {
        "skillName": resolved.skill_name,
        "storageBackend": resolved.storage_backend,
        "localCredentialsCleared": True,
        "serverUnbound": False,
        "targets": _target_descriptions(resolved),
        "retained": list(_RETAINED_ITEMS),
        "note": _IMPACT_NOTE,
    }


def _target_descriptions(resolved):
    """被清理的目标：只输出逻辑标识，不回显目录与凭据值。"""
    if resolved.uses_keychain:
        return {
            "backend": idaas_profile.STORAGE_BACKEND_KEYCHAIN,
            "scope": "skillId",
            "slots": ["clawUid", "accessToken", "uuid"],
        }
    return {
        "backend": idaas_profile.STORAGE_BACKEND_FILE,
        "isolatedDirectory": True,
        "files": list(FILE_BACKEND_TARGET_FILENAMES),
    }


def _diagnose_impact(resolved):
    """影响范围只写脱敏诊断：环境/Skill/存储后端与不变项，不含任何凭据。"""
    ai.diagnostic(
        "本地退出影响范围 profileRef=%s skillId=%s skillName=%s storageBackend=%s "
        "scope=%s localOnly=true serverUnbound=false"
        % (
            resolved.profile_ref,
            resolved.skill_id,
            resolved.skill_name,
            resolved.storage_backend,
            LOGOUT_SCOPE,
        )
    )


# ---------------------------------------------------------------------------
# 确认解析与校验
# ---------------------------------------------------------------------------


def _resolve_confirmation(profile_ref, confirmation, confirmation_provider):
    """按优先级解析可信确认事件：显式 confirmation → confirmation_provider。"""
    event = _resolve_from(profile_ref, confirmation)
    if event is not None:
        return event
    return _resolve_from(profile_ref, confirmation_provider)


def _resolve_from(profile_ref, source):
    if source is None:
        return None
    if isinstance(source, bool):
        # 未绑定计划的布尔参数（如 --yes）一律无效。
        raise result_module.ExecutionError(
            result_module.CONFIRMATION_REQUIRED, NO_CONFIRMATION_MESSAGE
        )
    if isinstance(source, result_module.Envelope):
        source = source.to_dict()
    if isinstance(source, dict):
        return _unwrap_confirmation(source)
    if isinstance(source, str):
        return _unwrap_confirmation(_parse_json_object(source))
    if callable(source):
        produced = source(profile_ref) if _takes_argument(source) else source()
        return _unwrap_confirmation(produced)
    raise result_module.ExecutionError(
        result_module.INVALID_ARGUMENT, "confirmation 必须是可信确认映射或可调用提供者"
    )


def _parse_json_object(text):
    try:
        parsed = json.loads(text)
    except ValueError:
        raise result_module.ExecutionError(
            result_module.CONFIRMATION_REQUIRED, NO_CONFIRMATION_MESSAGE
        )
    if not isinstance(parsed, dict):
        raise result_module.ExecutionError(
            result_module.CONFIRMATION_REQUIRED, NO_CONFIRMATION_MESSAGE
        )
    return parsed


def _unwrap_confirmation(value):
    if value is None:
        return None
    if isinstance(value, bool):
        # 未绑定计划的布尔参数（如 --yes）一律无效。
        raise result_module.ExecutionError(
            result_module.CONFIRMATION_REQUIRED, NO_CONFIRMATION_MESSAGE
        )
    if isinstance(value, result_module.Envelope):
        value = value.to_dict()
    if isinstance(value, str):
        value = _parse_json_object(value)
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


def _assert_bound_confirmation(event, resolved, clock):
    if not isinstance(event, dict):
        raise result_module.ExecutionError(
            result_module.CONFIRMATION_REQUIRED, NO_CONFIRMATION_MESSAGE
        )
    _reject_forbidden_fields(event)
    if event.get("expired") is True:
        raise _plan_expired(resolved)
    _assert_not_expired(event.get("expiresAt"), resolved, clock)
    if event.get("profileRef") != resolved.profile_ref:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "可信确认事件与当前 profile 不一致"
        )
    if _normalize_event_scope(event.get("scope")) != LOGOUT_SCOPE:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "可信确认事件与本地退出的影响范围不一致"
        )
    operation_ref = event.get("operationRef")
    plan_hash = event.get("planHash")
    if not _is_text(operation_ref) or not _is_text(plan_hash):
        raise result_module.ExecutionError(
            result_module.CONFIRMATION_REQUIRED, NO_CONFIRMATION_MESSAGE
        )
    plan = _plan_payload(resolved, operation_ref.strip())
    if _plan_hash(resolved, plan) != plan_hash.strip():
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


def _assert_not_expired(expires_at, resolved, clock):
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
        raise _plan_expired(resolved)


def _plan_expired(resolved):
    # 不回显旧 operationRef/planHash，避免被重新复用。
    return result_module.ExecutionError(
        result_module.PLAN_EXPIRED,
        PLAN_EXPIRED_MESSAGE,
        data={"profileRef": resolved.profile_ref, "scope": LOGOUT_SCOPE, "planExpired": True},
    )


def _normalize_event_scope(scope):
    if not isinstance(scope, str):
        return None
    return scope.strip().lower()


def _is_text(value):
    return isinstance(value, str) and bool(value.strip())


def _require_ttl(ttl_seconds):
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "ttl_seconds 必须是正整数秒"
        )
    return ttl_seconds


def _load(resolved_profile, profiles_path, overrides):
    """统一入口：任何受控/敏感字段经 overrides 提交都会在底座被拒绝。"""
    return idaas_profile.load_profile(resolved_profile, path=profiles_path, overrides=overrides)


def _resolve_modules(modules):
    """SDK 句柄归一：受控测试可注入 SdkModules，也可直接注入 token_service。"""
    if modules is None:
        return None
    if hasattr(modules, "token_service"):
        return modules
    return ai.SdkModules(token_service=modules, keychain_store=None)


def _now(clock=None):
    if clock is None:
        return datetime.now(timezone.utc)
    moment = clock() if callable(clock) else clock
    if isinstance(moment, bool):
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "clock 必须是返回时间的可调用对象或 epoch 秒"
        )
    if isinstance(moment, (int, float)):
        moment = datetime.fromtimestamp(moment, timezone.utc)
    if not isinstance(moment, datetime):
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "clock 必须是返回时间的可调用对象或 epoch 秒"
        )
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _iso(moment):
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# SDK 入参与结果映射
# ---------------------------------------------------------------------------


def _sdk_kwargs(resolved):
    """从受控 profile 适配 SDK 入参；clawUid / accessToken / uuid 不由 CLI 传入。"""
    return {"skillId": resolved.skill_id}


def _envelope_for_result(cleared, resolved, verify_local_files, trace):
    if cleared is not True:
        raise _clear_failure(resolved, cleared, verify_local_files)
    if verify_local_files:
        remaining = _remaining_local_files(resolved)
        if remaining:
            raise result_module.ExecutionError(
                result_module.FAILED,
                "本地凭据清理未完成，仍然存在: %s" % ", ".join(remaining),
                next_action=None,
            )
    ticket_provider.clear_cached_ticket(resolved)
    ai.diagnostic(
        "%s 本地凭据已清理 profileRef=%s skillId=%s backend=%s（未调用服务端解绑）"
        % (SDK_INTERFACE, resolved.profile_ref, resolved.skill_id, resolved.storage_backend)
    )
    return ai.success_envelope(
        COMMAND_ID,
        data={
            "profileRef": resolved.profile_ref,
            "localCredentialsCleared": True,
            "serverUnbound": False,
        },
        trace_id=trace,
    )


def _clear_failure(resolved, cleared, verify_local_files):
    """SDK 清理结果不是 True 时的统一失败映射。"""
    if cleared is False:
        if resolved.uses_keychain:
            return result_module.ExecutionError(
                result_module.SECURE_STORE_UNAVAILABLE,
                "系统 Keychain 凭据清理失败，已拒绝任何明文降级（profileRef=%s）" % resolved.profile_ref,
                next_action=None,
            )
        if verify_local_files:
            remaining = _remaining_local_files(resolved)
            if remaining:
                return result_module.ExecutionError(
                    result_module.FAILED,
                    "本地凭据清理未完成，仍然存在: %s" % ", ".join(remaining),
                    next_action=None,
                )
        return result_module.ExecutionError(
            result_module.FAILED,
            "本地凭据清理失败，请检查本机存储目录权限后重试",
            next_action=None,
        )
    return result_module.ExecutionError(
        result_module.UNKNOWN, "SDK 返回的本地凭据清理结果无法判定", next_action=None
    )


def _remaining_local_files(resolved):
    """文件后端的目标文件是否仍然存在（Keychain 后端不检查文件，避免误判）。"""
    if resolved.uses_keychain:
        return ()
    return tuple(
        name
        for name in FILE_BACKEND_TARGET_FILENAMES
        if os.path.exists(os.path.join(resolved.auth_info_path, name))
    )
