"""CLI 入口：两级命令解析与路由（V1.8）。

只做：解析 domain/action、业务动态 `--<Schema字段名> <值>`、非业务控制命令的 `--字段名 值`、
--profile、--format json；按 Schema 完成类型转换；查注册表路由；按判别字段确定性选择唯一场景；
把 SceneSpec + input + profile 交给 executor。本模块不调用任何 adapter。

`auth` 域是注册表之外的独立分支：`cmcloud auth <action>` 不经过 S01~S36 场景路由（不改变
`registry.SCENARIO_COUNT` 与场景完整性校验），只解析 `--key value` 后分派给 `scripts/auth_*.py`
下的 SDK 包装入口，并原样输出其九字段信封与退出码。

业务参数与控制命令参数都必须是字段原名（区分大小写），不做 camelCase 命名转换，
不再支持 `--set FIELD=VALUE` / `--input FILE|-`；控制命令一律用 `--字段名 值`，不接受位置参数。

`operation status --operation-ref <ref>` 是 S22 业务场景的统一入口：其输入由 CLI 组装，Agent 不能
提交下游 taskId。
"""

import json
import math
import os
import re
import sys

from cmcloud_core import debug_log
from cmcloud_core import executor
from cmcloud_core import orchestrator
from cmcloud_core import registry
from cmcloud_core import result as result_module
from cmcloud_auth import command_schemas as auth_command_schemas
from cmcloud_auth import idaas_clear_credentials
from cmcloud_auth import idaas_get_token
from cmcloud_auth import idaas_keychain_status
from cmcloud_auth import idaas_profile
from cmcloud_auth import idaas_unbinding
from cmcloud_auth import reauthorize
from cmcloud_auth import ticket_provider
from cmcloud_validation import command_schemas

USAGE = """cmcloud <domain> <action> [--<schema-field> VALUE ...] [--profile P] [--format json]
cmcloud auth login [--profile P] [--format json]
cmcloud auth reauthorize [--profile P] [--operationRef REF --planHash H] [--format json]
cmcloud auth revoke --scope all [--profile P] [--operationRef REF --planHash H] [--format json]
cmcloud auth logout [--profile P] [--operationRef REF --planHash H] [--format json]
cmcloud auth status [--profile P] [--all true|false] [--format json]
cmcloud capabilities [--domain D] [--format json]
cmcloud help [--topic T] [--action A] [--format json]
cmcloud schema --command-id <command-id> [--format json]
cmcloud operation confirm --operation-ref <ref> [--plan-hash H --confirmation CONFIRM] [--profile P] [--format json]
cmcloud operation execute --operation-ref <ref> --confirm-token <ref> [--profile P] [--format json]
cmcloud operation status --operation-ref <ref> [--profile P] [--format json]
cmcloud orchestrate --step "<domain> <action> --<schema-field> VALUE" [--step ...] [--profile P] [--format json]
"""

OPERATION_ACTIONS = ("confirm", "execute", "status")
ORCHESTRATE_OPTIONS = ("step", "profile", "format")
CONFIRMATION_EVENT_FILE_ENV = "CMCLOUD_CONFIRMATION_EVENT_FILE"

STATUS_ROUTE = ("operation", "status")

# 非业务控制命令字段表：唯一字段来源，全部按 string 读取；不接受任何 alias。
CONTROL_COMMAND_FIELDS = {
    "capabilities": {
        "domain": {"type": "string", "required": False},
        "format": {"type": "string", "required": False},
    },
    "help": {
        "topic": {"type": "string", "required": False},
        "action": {"type": "string", "required": False},
        "format": {"type": "string", "required": False},
    },
    "schema": {
        "command-id": {"type": "string", "required": True},
        "format": {"type": "string", "required": False},
    },
    "operation.confirm": {
        "operation-ref": {"type": "string", "required": True},
        "plan-hash": {"type": "string", "required": False},
        "confirmation": {"type": "string", "required": False},
        "profile": {"type": "string", "required": False},
        "format": {"type": "string", "required": False},
    },
    "operation.execute": {
        "operation-ref": {"type": "string", "required": True},
        "confirm-token": {"type": "string", "required": True},
        "profile": {"type": "string", "required": False},
        "format": {"type": "string", "required": False},
    },
    "operation.status": {
        "operation-ref": {"type": "string", "required": True},
        "profile": {"type": "string", "required": False},
        "format": {"type": "string", "required": False},
    },
}

# auth 域命令与字段表：唯一真源在 scripts/auth_command_schemas.py，同一份定义同时供
# `help --topic auth` 与 `schema --command-id auth.*` 下发，避免文档与分发层再次脱节。
AUTH_ACTIONS = auth_command_schemas.AUTH_ACTIONS
AUTH_COMMAND_FIELDS = auth_command_schemas.AUTH_COMMAND_FIELDS

# auth 域禁止 Agent 提交的受控/敏感字段（accessToken/uuid/clawUid/devId/clientId/
# skillId/skillName/strategyId/scopeIds/templateId/Keychain key·value 等）。
# 复用受控 profile 的字段清单，出现即按「无法识别的参数」拒绝，绝不进入分派。
AUTH_FORBIDDEN_OPTIONS = frozenset(idaas_profile.FORBIDDEN_AGENT_FIELDS)

# `auth.status --all` 只接受小写布尔字面量，取值口径与包装层保持一致。
AUTH_ALL_VALUES = idaas_keychain_status.ALL_VALUES


class CliUsageError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        envelope = _dispatch(argv)
    except CliUsageError as err:
        envelope = result_module.Envelope(
            None,
            result_module.STATUS_FAILED,
            error=result_module.build_error(result_module.INVALID_ARGUMENT, err.message),
        )
    except result_module.ExecutionError as err:
        envelope = result_module.envelope_from_error(None, err)
    exit_code = envelope.exit_code()
    debug_log.emit("envelope", **envelope_debug_fields(envelope, exit_code))
    _emit(envelope)
    return exit_code


def envelope_debug_fields(envelope, exit_code):
    """只暴露信封的结论字段：data 与明细不进排障日志。"""
    payload = envelope.to_dict()
    return {
        "command": payload["command"],
        "status": payload["status"],
        "counts": payload["counts"],
        "page": payload["page"],
        "nextAction": payload["nextAction"],
        "error": payload["error"],
        "traceId": payload["traceId"],
        "exit_code": exit_code,
    }


def _emit(envelope):
    text = envelope.to_json() + "\n"
    stream = sys.stdout
    try:
        stream.write(text)
        stream.flush()
    except UnicodeEncodeError:
        # 终端编码不支持中文时按 UTF-8 字节直写，保证机器调用始终拿到完整 JSON
        buffer = getattr(stream, "buffer", None)
        if buffer is None:
            raise
        buffer.write(text.encode("utf-8"))
        buffer.flush()


def _dispatch(argv):
    if not argv:
        raise CliUsageError("缺少命令，用法: %s" % USAGE.strip().splitlines()[0])
    head = argv[0]
    debug_log.emit(
        "dispatch",
        head=head,
        action=argv[1] if len(argv) > 1 and not argv[1].startswith("-") else None,
        command_id=head if head in ("capabilities", "help", "schema") else None,
        arg_keys=[token[2:] for token in argv if token.startswith("--")],
    )
    if head in ("-h", "--help", "help"):
        return _help_command(argv[1:])
    if head == "capabilities":
        return _capabilities_command(argv[1:])
    if head == "schema":
        return _schema_command(argv[1:])
    if head == "operation":
        return _operation_command(argv[1:])
    if head == "orchestrate":
        return _orchestrate_command(argv[1:])
    if head == "auth":
        return _auth_command(argv[1:])
    return _business_command(argv)


# ---------------------------------------------------------------------------
# auth 域分派：注册表之外的独立分支，只解析 `--key value` 后交给 SDK 包装入口
# ---------------------------------------------------------------------------


def _auth_command(argv):
    """`cmcloud auth <action>` 分派：输出包装层信封，由 main() 统一输出与取退出码。"""
    if not argv or argv[0] not in AUTH_ACTIONS:
        raise CliUsageError("auth 仅支持: %s" % ", ".join(AUTH_ACTIONS))
    action, rest = argv[0], argv[1:]
    values = _parse_auth_arguments("auth.%s" % action, rest)
    if action == "login":
        return idaas_get_token.run(values.get("profile"), format=values.get("format", "json"))
    if action == "status":
        return idaas_keychain_status.run(
            values.get("profile"),
            all_skills=_auth_all_flag(values.get("all")),
            format=values.get("format", "json"),
        )
    if action == "revoke":
        return _auth_revoke(values)
    if action == "reauthorize":
        return reauthorize.run(
            values.get("profile"), operation_ref=values.get("operationRef"),
            plan_hash=values.get("planHash"),
        )
    return _auth_logout(values)


def _parse_auth_arguments(command_id, args):
    """auth 命令参数解析：字段表来自 AUTH_COMMAND_FIELDS，全部按 string 读取。

    复用统一 tokenizer 的 `--key value` 消费与「未知/重复/缺值」拒绝规则；受控与敏感字段
    在 tokenizer 内即被拒绝，任何情况下都不会被当作合法入参传入包装层。
    """
    specs = AUTH_COMMAND_FIELDS[command_id]
    values = _parse_named_arguments(
        args, specs, command_id, rejected_options=AUTH_FORBIDDEN_OPTIONS
    )
    if values.get("format", "json") != "json":
        raise CliUsageError("当前仅支持 --format json")
    for name, spec in specs.items():
        if spec.get("required") and name not in values:
            raise CliUsageError("%s 需要 --%s" % (command_id, name))
    return values


def _auth_all_flag(raw_value):
    """`auth.status --all` 取值归一：缺省 false，只接受 true/false。"""
    if raw_value is None:
        return False
    if raw_value not in AUTH_ALL_VALUES:
        raise CliUsageError("auth.status 的 --all 只接受 true 或 false")
    return raw_value == "true"


def _auth_revoke(values):
    """KMS-02 两阶段：无确认参数只冻结计划；operationRef 与 planHash 同时给出才执行。"""
    profile = values.get("profile")
    scope = values["scope"]
    if scope not in idaas_unbinding.ALLOWED_SCOPES:
        raise CliUsageError(
            "auth.revoke 的 --scope 只接受: %s" % ", ".join(idaas_unbinding.ALLOWED_SCOPES)
        )
    operation_ref = values.get("operationRef")
    plan_hash = values.get("planHash")
    if operation_ref is None and plan_hash is None:
        # 阶段一：只生成不可变计划，绝不调用 SDK。
        return idaas_unbinding.plan_unbinding(profile, scope=scope)
    if operation_ref is None or plan_hash is None:
        raise CliUsageError("auth.revoke 执行阶段必须同时提供 --operationRef 与 --planHash")
    return idaas_unbinding.execute_unbinding(
        profile,
        scope=scope,
        confirmation=_auth_confirmation(profile, scope, operation_ref, plan_hash),
    )


def _auth_logout(values):
    """KMS-04 两阶段：本地退出同样必须由 operationRef + planHash 绑定触发。"""
    profile = values.get("profile")
    operation_ref = values.get("operationRef")
    plan_hash = values.get("planHash")
    if operation_ref is None and plan_hash is None:
        # 阶段一：只生成不可变计划，不删除任何本地凭据、不触碰 SDK。
        return idaas_clear_credentials.plan_logout(profile)
    if operation_ref is None or plan_hash is None:
        raise CliUsageError("auth.logout 执行阶段必须同时提供 --operationRef 与 --planHash")
    return idaas_clear_credentials.execute_logout(
        profile,
        confirmation=_auth_confirmation(
            profile, idaas_clear_credentials.LOGOUT_SCOPE, operation_ref, plan_hash
        ),
    )


def _auth_confirmation(profile, scope, operation_ref, plan_hash):
    """阶段二可信确认事件：只携带与阶段一计划绑定的字段。

    profileRef 用与包装层完全一致的受控登记解析口径（缺省取 defaultProfile），
    planHash 由包装层按 profile/scope/operationRef 逐字节复算校验；不一致即
    INVALID_ARGUMENT，且校验先于任何 SDK 调用。此处不接受任何未绑定的布尔开关。
    """
    return {
        "profileRef": idaas_profile.load_profile(profile).profile_ref,
        "scope": scope,
        "operationRef": operation_ref,
        "planHash": plan_hash,
    }


def _business_command(argv):
    if len(argv) < 2:
        raise CliUsageError("必须提供 domain 与 action")
    domain, action, rest = argv[0], argv[1], argv[2:]
    # 先确定路由：已删除/未注册命令稳定返回「未注册」，不受其后动态字段影响。
    route = registry.get_route(domain, action)
    if route is None:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "未注册的命令: %s %s" % (domain, action)
        )
    options = _parse_business_options(route, rest)
    input_data = options["business"]
    spec = registry.select_scene(route, input_data)
    debug_log.emit(
        "route",
        domain=domain,
        action=action,
        command_id=route.command_id,
        scenario_id=spec.scenario_id,
        operation_type=spec.operation_type,
        input_keys=sorted(input_data),
    )
    runtime = _build_runtime(spec, options)
    return executor.run_command(spec, input_data, runtime)


# 业务命令保留的控制参数：不进入 input_data，不参与场景 selector。
BUSINESS_CONTROL_OPTIONS = frozenset(("profile", "format"))

# 旧业务输入入口已移除；出现即按无法识别拒绝，不保留任何兼容别名。
LEGACY_BUSINESS_OPTIONS = frozenset(("set", "input"))

# CLI 支持的字段解析类型，必须由 Schema 显式或可推导得到。
_SUPPORTED_FIELD_KINDS = ("string", "integer", "number", "boolean", "array", "object")

_INTEGER_PATTERN = re.compile(r"^-?\d+$")

_NUMBER_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def _parse_named_arguments(args, field_specs, command_name, rejected_options=()):
    """统一 `--字段名 值` tokenizer：业务动态字段与控制命令字段共用同一套消费规则。

    - 参数必须写成 `--字段名 值`，每个参数消费后续一个 argv，缺值立即拒绝；
    - 重复字段、未在 field_specs 中登记的字段一律拒绝，参数名区分大小写、无别名；
    - 值解析由字段规格决定：业务字段走 Schema 类型转换，控制命令字段按 string 原样保留。
    """
    values = {}
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("--") or token == "--":
            raise CliUsageError("无法识别的参数: %s" % token)
        name = token[2:]
        if name in rejected_options:
            raise CliUsageError("无法识别的参数: %s" % token)
        index += 1
        if index >= len(args):
            raise CliUsageError("--%s 需要取值" % name)
        raw_value = args[index]
        spec = field_specs.get(name)
        if spec is None:
            # 业务字段来自 JSON Schema，名称通常是 camelCase。CLI 有意不接受
            # kebab-case 别名，但在能够无歧义映射时给出原字段提示，避免调用方
            # 把“本地参数未通过”误判成下游 HTTP 接口失败。
            camel_case_name = _kebab_to_camel_case(name)
            if camel_case_name != name and camel_case_name in field_specs:
                message = (
                    "字段未在 %s 的命令 Schema 中声明: %s；"
                    "参数名区分大小写，请使用 %s"
                    % (command_name, name, camel_case_name)
                )
            else:
                message = "字段未在 %s 的命令 Schema 中声明: %s" % (
                    command_name,
                    name,
                )
            raise result_module.ExecutionError(
                result_module.INVALID_ARGUMENT,
                message,
            )
        if name in values:
            raise result_module.ExecutionError(
                result_module.INVALID_ARGUMENT, "字段重复: %s" % name
            )
        values[name] = _parse_named_value(name, raw_value, spec)
        index += 1
    return values


def _kebab_to_camel_case(name):
    """仅用于错误提示：把 kebab-case 候选映射为 Schema 的 camelCase 名称。"""
    parts = name.split("-")
    if len(parts) < 2 or any(not part for part in parts):
        return name
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def _parse_named_value(name, raw_value, spec):
    """按字段规格解析单个参数值：业务字段按 Schema 类型，控制命令字段为 string。"""
    candidates = spec.get("schemas")
    if candidates is not None:
        return _parse_schema_value(name, raw_value, candidates)
    if spec.get("type") == "string":
        return raw_value
    raise _invalid_value(name, spec.get("type", "unknown"))


def _command_field_specs(command_id):
    """业务命令字段规格：command Schema 全部分支字段 + 控制参数（string）。"""
    specs = {
        name: {"schemas": candidates}
        for name, candidates in _command_field_schemas(command_id).items()
    }
    for name in BUSINESS_CONTROL_OPTIONS:
        specs[name] = {"type": "string"}
    return specs


def _parse_business_options(route, args):
    """把业务命令的 `--<Schema字段名> <值>` 解析为控制参数与业务字典。

    返回 `{"flags": {...}, "business": {...}}`；复用统一 tokenizer 消费 argv。
    """
    values = _parse_named_arguments(
        args,
        _command_field_specs(route.command_id),
        route.command_id,
        rejected_options=LEGACY_BUSINESS_OPTIONS,
    )
    options = {"flags": {}, "business": {}}
    for name, value in values.items():
        if name in BUSINESS_CONTROL_OPTIONS:
            options["flags"][name] = value
        else:
            options["business"][name] = value
    _validate_format(options)
    return options


def _validate_format(options):
    if options["flags"].get("format", "json") != "json":
        raise CliUsageError("当前仅支持 --format json")


def _command_field_schemas(command_id):
    """按 command-id 收集字段 Schema 的候选分支（含共享主命令各 oneOf 分支）。"""
    return _declared_field_schemas(command_schemas.command_schema(command_id))


def _declared_field_schemas(schema, output=None):
    """递归收集「字段名 → 候选字段 Schema 列表」；同名字段可来自多个分支。"""
    output = {} if output is None else output
    if isinstance(schema, bool) or not isinstance(schema, dict):
        return output
    for name, field_schema in (schema.get("properties") or {}).items():
        output.setdefault(name, []).append(field_schema)
    for keyword in ("allOf", "anyOf", "oneOf"):
        for branch in schema.get(keyword, ()) or ():
            _declared_field_schemas(branch, output)
    for keyword in ("if", "then", "else"):
        branch = schema.get(keyword)
        if isinstance(branch, (dict, bool)):
            _declared_field_schemas(branch, output)
    return output


def _parse_schema_value(field_name, raw_value, candidate_schemas):
    """按 Schema 推导出的基础类型把 argv 字符串转换为 Python 值。"""
    kind = _derive_field_kind(field_name, candidate_schemas)
    if kind == "string":
        # ref/cursor/date-time/uri/timezone 等一律原样保留，禁止走 json.loads。
        return raw_value
    if kind == "boolean":
        if raw_value not in ("true", "false"):
            raise _invalid_value(field_name, kind)
        return raw_value == "true"
    if kind == "integer":
        if not _INTEGER_PATTERN.match(raw_value):
            raise _invalid_value(field_name, kind)
        try:
            # Python ≥3.11 对超长数字串的 int() 转换会抛 ValueError，必须受控兜底。
            return int(raw_value)
        except (ValueError, TypeError, RecursionError):
            raise _invalid_value(field_name, kind)
    if kind == "number":
        if not _NUMBER_PATTERN.match(raw_value):
            raise _invalid_value(field_name, kind)
        try:
            number = float(raw_value)
        except (ValueError, TypeError, RecursionError):
            raise _invalid_value(field_name, kind)
        if not math.isfinite(number):
            raise _invalid_value(field_name, kind)
        return number
    if kind in ("array", "object"):
        try:
            value = json.loads(raw_value)
        except (ValueError, TypeError, RecursionError):
            # RecursionError：深层嵌套 JSON 触发解释器递归上限，同样必须受控拒绝。
            raise _invalid_value(field_name, kind)
        expected = list if kind == "array" else dict
        if not isinstance(value, expected):
            raise _invalid_value(field_name, kind)
        return value
    raise _invalid_value(field_name, kind)


def _derive_field_kind(field_name, candidate_schemas):
    """统一推导字段解析类型；跨分支推导出不同类型时直接失败，不猜测。"""
    kinds = set()
    for schema in candidate_schemas:
        kind = _schema_kind(schema)
        if kind is not None:
            kinds.add(kind)
    if not kinds:
        raise _invalid_value(field_name, "unknown")
    if len(kinds) > 1:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "字段 %s 在命令 Schema 中类型不一致: %s" % (field_name, ", ".join(sorted(kinds))),
        )
    kind = kinds.pop()
    if kind not in _SUPPORTED_FIELD_KINDS:
        raise _invalid_value(field_name, kind)
    return kind


def _schema_kind(schema):
    """单个字段 Schema 的类型推导：type → const → enum → x-ref-kind/format。"""
    if not isinstance(schema, dict):
        return None
    declared = schema.get("type")
    if isinstance(declared, str):
        return declared
    if "const" in schema:
        return _python_kind(schema["const"])
    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and enum_values:
        enum_kinds = {_python_kind(value) for value in enum_values}
        if len(enum_kinds) == 1:
            return enum_kinds.pop()
        return None
    if "x-ref-kind" in schema or isinstance(schema.get("format"), str):
        return "string"
    return None


def _python_kind(value):
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return None


def _invalid_value(field_name, kind):
    # 只回显字段名与期待类型，不回显敏感字段的完整取值。
    return result_module.ExecutionError(
        result_module.INVALID_ARGUMENT,
        "字段 %s 的取值不符合 Schema 类型 %s" % (field_name, kind),
    )


def _parse_control_arguments(command_id, args):
    """非业务控制命令参数解析：字段表来自 CONTROL_COMMAND_FIELDS，全部按 string 读取。"""
    specs = CONTROL_COMMAND_FIELDS[command_id]
    values = _parse_named_arguments(args, specs, command_id)
    fmt = values.get("format", "json")
    if fmt != "json":
        raise CliUsageError("当前仅支持 --format json")
    for name, spec in specs.items():
        if spec.get("required") and name not in values:
            raise CliUsageError("%s 需要 --%s" % (command_id, name))
    return values


def _build_runtime(
    spec, options, *, confirmation_provider=None, ledger=None, profile=None,
    publication=None,
):
    resolved_profile = profile or options["flags"].get("profile", "default")
    publication = publication or executor.PublicationConfig.from_env(profile=resolved_profile)
    header_provider = ticket_provider.BusinessHeaderProvider(resolved_profile)
    return executor.RuntimeContext(
        spec=spec,
        profile=resolved_profile,
        publication=publication,
        ledger=ledger,
        header_provider=header_provider,
        confirmation_provider=confirmation_provider,
    )


def _capabilities_command(args):
    values = _parse_control_arguments("capabilities", args)
    payload = executor.capabilities_payload(values.get("domain"))
    return result_module.Envelope("capabilities", result_module.STATUS_SUCCESS, data=payload)


def _help_command(args):
    values = _parse_control_arguments("help", args)
    topic = values.get("topic")
    action = values.get("action")
    if action is not None and topic is None:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "help 使用 --action 时必须同时提供 --topic",
        )
    if topic == "auth":
        return _auth_help_command(action)
    if topic == registry.ORCHESTRATION_COMMAND_ID:
        return result_module.Envelope(
            "help", result_module.STATUS_SUCCESS, data=orchestrator.help_payload(action)
        )
    payload = executor.help_payload(topic, action)
    if topic is None:
        payload["usage"] = USAGE.strip().splitlines()
        payload["orchestration"] = orchestrator.help_payload()
    payload["domainsHelp"] = dict(registry.DOMAIN_HELP)
    return result_module.Envelope("help", result_module.STATUS_SUCCESS, data=payload)


def _auth_help_command(action):
    """`help --topic auth`：auth 域不进 registry，单独下发（字段与 schema 同源）。"""
    if action is not None and action not in auth_command_schemas.AUTH_ACTIONS:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "auth 仅支持: %s" % ", ".join(auth_command_schemas.AUTH_ACTIONS)
        )
    payload = auth_command_schemas.help_payload()
    if action is not None:
        payload["schema"] = auth_command_schemas.command_schema("auth.%s" % action)
        payload["usage"] = USAGE.strip().splitlines()
    return result_module.Envelope("help", result_module.STATUS_SUCCESS, data=payload)


def _schema_command(args):
    values = _parse_control_arguments("schema", args)
    command_id = values["command-id"]
    if command_id == registry.ORCHESTRATION_COMMAND_ID:
        return result_module.Envelope(
            "schema", result_module.STATUS_SUCCESS, data=orchestrator.schema_payload()
        )
    if command_id.startswith("auth."):
        try:
            payload = auth_command_schemas.schema_payload(command_id)
        except KeyError as err:
            raise result_module.ExecutionError(
                result_module.INVALID_ARGUMENT,
                str(err).strip("'\""),
            )
        return result_module.Envelope("schema", result_module.STATUS_SUCCESS, data=payload)
    try:
        payload = executor.schema_payload(command_id)
    except KeyError as err:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            str(err).strip("'\""),
        )
    return result_module.Envelope("schema", result_module.STATUS_SUCCESS, data=payload)


def _operation_command(args):
    if not args or args[0] not in OPERATION_ACTIONS:
        raise CliUsageError("operation 仅支持: %s" % ", ".join(OPERATION_ACTIONS))
    action, rest = args[0], args[1:]
    values = _parse_control_arguments("operation.%s" % action, rest)
    operation_ref = values["operation-ref"]
    profile = values.get("profile", "default")
    plan_hash = values.get("plan-hash") if action == "confirm" else None
    confirmation = values.get("confirmation") if action == "confirm" else None
    if action == "confirm" and ((plan_hash is None) != (confirmation is None)):
        raise CliUsageError(
            "operation.confirm 的 --plan-hash 与 --confirmation 必须同时提供"
        )
    ledger = _ledger()
    record = ledger.get_operation(operation_ref, profile)
    if record.get("commandId") == "auth.reauthorize":
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "重新授权计划请使用 auth reauthorize 携带 --operationRef 与 --planHash 执行或复查，不走 operation 命令",
        )
    if action == "status":
        # S22：统一任务状态入口，从台账恢复任务类型，不接受 Agent 传下游 taskId。
        route = registry.get_route(*STATUS_ROUTE)
        spec = registry.select_scene(route, {"operation-ref": operation_ref})
        runtime = _build_runtime(spec, {"flags": values}, ledger=ledger, profile=profile)
        return executor.run_command(spec, {"operation-ref": operation_ref}, runtime)
    record = ledger.get_operation(operation_ref, profile)
    spec = registry.get_scene(record["scenarioId"])
    if action == "confirm":
        publication = executor.PublicationConfig.from_env(profile=profile)
        event_path = os.environ.get(CONFIRMATION_EVENT_FILE_ENV)
        if event_path:
            provider = executor.FileConfirmationProvider(event_path)
        elif plan_hash is not None:
            if not publication.allow_conversation_confirmation:
                raise result_module.ExecutionError(
                    result_module.CAPABILITY_UNAVAILABLE,
                    "当前发布配置未开启对话确认",
                )
            provider = executor.ConversationConfirmationProvider(
                operation_ref=operation_ref,
                plan_hash=plan_hash,
                profile=profile,
                confirmation=confirmation,
            )
        elif getattr(sys.stdin, "isatty", lambda: False)():
            provider = executor.InteractiveConfirmationProvider(record)
        else:
            provider = executor.NullConfirmationProvider()
        runtime = _build_runtime(
            spec, {"flags": values}, confirmation_provider=provider,
            ledger=ledger, profile=profile, publication=publication,
        )
        if plan_hash is None:
            return executor.confirm_operation(operation_ref, runtime)
        return executor.confirm_operation(operation_ref, runtime, plan_hash=plan_hash)
    runtime = _build_runtime(spec, {"flags": values}, ledger=ledger, profile=profile)
    return executor.execute_operation(
        operation_ref, runtime, confirm_token=values["confirm-token"]
    )


def _orchestrate_command(args):
    """`cmcloud orchestrate --step "..."`：把多个云端写动作冻结为一个计划、一次确认。"""
    values = _parse_orchestrate_arguments(args)
    profile = values.get("profile", "default")
    spec = registry.get_scene(registry.ORCHESTRATION_SCENARIO_ID)
    runtime = _build_runtime(spec, {"flags": values}, ledger=_ledger(), profile=profile)
    return executor.prepare_orchestration(runtime, values["step"])


def _parse_orchestrate_arguments(args):
    """编排参数解析：`--step` 可重复，字段名区分大小写、无别名。"""
    values = {"step": []}
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("--") or token == "--":
            raise CliUsageError("无法识别的参数: %s" % token)
        name = token[2:]
        if name not in ORCHESTRATE_OPTIONS:
            raise CliUsageError("无法识别的参数: %s" % token)
        index += 1
        if index >= len(args):
            raise CliUsageError("--%s 需要取值" % name)
        value = args[index]
        index += 1
        if name == "step":
            values["step"].append(value)
            continue
        if name in values:
            raise CliUsageError("字段重复: %s" % name)
        values[name] = value
    if not values["step"]:
        raise CliUsageError("orchestrate 至少需要一个 --step")
    if values.get("format", "json") != "json":
        raise CliUsageError("当前仅支持 --format json")
    return values


def _ledger():
    from cmcloud_core import operation_ledger

    return operation_ledger.LedgerStore()


if __name__ == "__main__":
    sys.exit(main())
