"""一次确认后强制发起新授权，停用旧票据，由普通取票承接完成；不解绑。"""

from datetime import timedelta

from cmcloud_auth import idaas_common as ai, idaas_profile
from cmcloud_auth import idaas_get_token as login
from cmcloud_core import operation_ledger as ledger_module, result as result_module

COMMAND_ID = "auth.reauthorize"
PLAN_TTL_SECONDS = 300
PLAN_MODE = "forcedAuthWithTicketReset"


def run(profile=None, *, operation_ref=None, plan_hash=None, ledger=None, modules=None, clock=None):
    """无执行参数只生成计划；消费确认先于 SDK 调用，重复执行只读返回。"""
    trace = ai.new_trace_id()
    try:
        if (operation_ref is None) != (plan_hash is None):
            raise result_module.ExecutionError(
                result_module.INVALID_ARGUMENT,
                "auth.reauthorize 执行阶段必须同时提供 --operationRef 与 --planHash")
        resolved = idaas_profile.load_profile(profile)
        store = ledger if ledger is not None else ledger_module.LedgerStore(clock=clock)
        if operation_ref is None:
            return _plan(resolved, store, trace)
        record = store.get_operation(operation_ref, resolved.profile_ref)
        if record["commandId"] != COMMAND_ID or record["planHash"] != plan_hash:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "重新授权计划不匹配")
        if record["plan"].get("mode") != PLAN_MODE or _digest(resolved, record["plan"]) != plan_hash:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "重新授权计划或登记已变化，请重新生成计划")
        if record["status"] != ledger_module.OPERATION_PENDING:
            if record.get("lastResult"):
                return _saved_envelope(record["lastResult"])
            raise ai.make_error(result_module.UNKNOWN,
                                "重新授权请求已开始，结果尚未确认；请用 auth login 核对，不要重复强制授权",
                                next_action=None)
        token = store.confirm_operation(operation_ref, profile=resolved.profile_ref, plan_hash=plan_hash)
        record = store.consume_confirmation_token(operation_ref, profile=resolved.profile_ref, token=token)
        return _execute(resolved, record, store, modules, trace)
    except result_module.ExecutionError as err:
        return ai.error_envelope(COMMAND_ID, err, trace_id=trace)
    except Exception:
        return ai.error_envelope(COMMAND_ID, ai.make_error(
            result_module.UNKNOWN, "重新授权状态无法确认，请用 auth login 核对，不要重复强制授权",
            next_action=None), trace_id=trace)


def _digest(resolved, plan):
    return ai.plan_hash(resolved.profile, command=COMMAND_ID, plan=plan,
                        skill_id=resolved.skill_id, scope="all")


def _plan(resolved, store, trace):
    now = store.now()
    plan = {"summary": "强制发起新的云盘授权，取得入口后停用旧票据，完成授权后恢复使用",
            "mode": PLAN_MODE, "skillId": resolved.skill_id}
    digest = _digest(resolved, plan)
    ref = store.create_operation(profile=resolved.profile_ref, scenario_id=COMMAND_ID,
                                 command_id=COMMAND_ID, normalized_input={}, resolved_ids={},
                                 plan=plan, plan_hash=digest)
    expires = (now + timedelta(seconds=PLAN_TTL_SECONDS)).isoformat()
    store.update_operation(ref, expiresAt=expires)
    return result_module.Envelope(COMMAND_ID, result_module.STATUS_CONFIRMATION_REQUIRED, data={
        "profileRef": resolved.profile_ref, "operationRef": ref, "planHash": digest,
        "expiresAt": expires, "summary": plan["summary"], "steps": ["force-auth"]},
        error=result_module.build_error(result_module.CONFIRMATION_REQUIRED,
                                        "取得新授权入口后将停用旧票据，完成授权前无法正常使用；请确认后使用原命令执行"),
        next_action=None, trace_id=trace)


def _execute(resolved, record, store, modules, trace):
    data = {"operationRef": record["operationRef"], "profileRef": resolved.profile_ref,
            "authorizationStarted": False}
    # 请求前持久标记；中断、超时、重复确认均不重发 forcedAuth=True。
    marker = ai.error_envelope(COMMAND_ID, ai.make_error(
        result_module.UNKNOWN, "重新授权请求结果尚未确认，请用 auth login 核对，不要重复强制授权",
        data=dict(data), next_action=None), trace_id=trace)
    store.update_operation(record["operationRef"], lastResult=marker.to_dict())
    response = login.run(profile=resolved.profile_ref, modules=modules, trace_id=trace, forced_auth=True)
    payload = response.to_dict()
    data.update(payload.get("data") or {})
    data["authorizationStarted"] = (response.status == result_module.STATUS_AUTH_REQUIRED
                                    and bool(data.get("authPageUrl")))
    data["oldTicketInvalidated"] = data["authorizationStarted"]
    outcome = result_module.Envelope(COMMAND_ID, response.status, data=data,
                                     error=payload.get("error"), next_action=None, trace_id=trace)
    saved = outcome.to_dict()
    if data["authorizationStarted"]:
        # 授权链接与二维码不进入操作台账；重复计划通过普通登录恢复入口。
        saved = ai.error_envelope(COMMAND_ID, ai.make_error(
            result_module.UNKNOWN, "新授权已发起；请用 auth login 确认或获取同一入口，不要重复强制授权",
            data={key: data[key] for key in ("operationRef", "profileRef", "authorizationStarted", "oldTicketInvalidated")},
            next_action=None), trace_id=trace).to_dict()
    store.update_operation(record["operationRef"], status=ledger_module.OPERATION_EXECUTED, lastResult=saved)
    return outcome


def _saved_envelope(saved):
    return result_module.Envelope(COMMAND_ID, saved["status"], data=saved.get("data"),
                                 error=saved.get("error"), next_action=None, trace_id=saved.get("traceId"))
