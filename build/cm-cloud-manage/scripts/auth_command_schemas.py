"""auth 域命令 Schema：``cmcloud schema --command-id auth.*`` 与 ``help --topic auth`` 的真源。

auth 域不经过 S01~S35 场景路由（见 ``main.py``），字段表与 Schema 因此独立登记在本模块，
并与分发层实际接受的参数一一对应：不再出现"文档要求取 schema、CLI 却回未注册 command-id"
的断链。字段全部按 string 读取、不接受 alias；登记参数与身份材料一律不接受 Agent 提交
（见 ``idaas_profile.FORBIDDEN_AGENT_FIELDS``）。
"""

AUTH_ACTIONS = ("login", "reauthorize", "revoke", "logout", "status")

AUTH_DOMAIN_SUMMARY = "授权与身份就绪：登录/取票、远端解绑、本地退出、本机凭据状态"

# 与 CLI 分发层保持一致的字段表：唯一字段来源，不接受任何 alias。
AUTH_COMMAND_FIELDS = {
    "auth.reauthorize": {
        "profile": {"type": "string", "required": False},
        "operationRef": {"type": "string", "required": False},
        "planHash": {"type": "string", "required": False},
        "format": {"type": "string", "required": False},
    },
    "auth.login": {
        "profile": {"type": "string", "required": False},
        "format": {"type": "string", "required": False},
    },
    "auth.revoke": {
        "profile": {"type": "string", "required": False},
        "scope": {"type": "string", "required": True},
        "operationRef": {"type": "string", "required": False},
        "planHash": {"type": "string", "required": False},
        "format": {"type": "string", "required": False},
    },
    "auth.logout": {
        "profile": {"type": "string", "required": False},
        "operationRef": {"type": "string", "required": False},
        "planHash": {"type": "string", "required": False},
        "format": {"type": "string", "required": False},
    },
    "auth.status": {
        "profile": {"type": "string", "required": False},
        "all": {"type": "string", "required": False},
        "format": {"type": "string", "required": False},
    },
}

# 每个命令的补充说明：取值域与两阶段约定写在这里，供 help/schema 消费。
AUTH_COMMAND_NOTES = {
    "auth.reauthorize": "重新授权：一次确认后强制发起新授权，新会话确认后清除旧票据并记录待完成状态，不解绑；使用原命令携带 operationRef + planHash 执行或只读复查，不走 operation.confirm/execute/status；失败或未知不重复强制请求，后续业务或普通 auth login 通过普通取票承接完成。",
    "auth.login": "取票/发起授权：103000 授权就绪；103001 需授权并随信封返回授权入口。",
    "auth.revoke": "远端解绑：两阶段，先用本命令生成计划，再用 --operationRef + --planHash 重调执行；不走 operation.confirm。",
    "auth.logout": "本地退出：两阶段，同上；只清本机凭据，不含远端解绑。",
    "auth.status": "只回答本机是否还有登录凭据，不回答「云盘授权了吗」；--all 只接受 true/false。",
}

AUTH_SCHEMA_FORMAT_VALUES = ("json",)


def _field_schema(name, spec):
    schema = {"type": spec.get("type", "string")}
    if name == "format":
        schema["enum"] = list(AUTH_SCHEMA_FORMAT_VALUES)
    if name == "all":
        schema["enum"] = ["true", "false"]
    return schema


def command_schema(command_id):
    """单条 auth 命令的 JSON-Schema 形态入参定义；未登记 command-id 抛 KeyError。"""
    specs = AUTH_COMMAND_FIELDS.get(command_id)
    if specs is None:
        raise KeyError("未注册的 command-id: %s" % command_id)
    properties = {}
    required = []
    for name, spec in specs.items():
        properties[name] = _field_schema(name, spec)
        if spec.get("required"):
            required.append(name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def schema_payload(command_id):
    """``cmcloud schema --command-id auth.*`` 的 data 载荷。"""
    schema = command_schema(command_id)
    return {
        "commandId": command_id,
        "command": command_id.replace(".", " "),
        "shared": False,
        "selectorFields": [],
        "schemaId": command_id,
        "capability": "enabled",
        "disabledReason": None,
        "note": AUTH_COMMAND_NOTES[command_id],
        "schema": schema,
        "scenes": [],
    }


def help_payload():
    """``cmcloud help --topic auth`` 的 data 载荷。"""
    return {
        "domain": "auth",
        "summary": AUTH_DOMAIN_SUMMARY,
        "commands": [
            {
                "commandId": command_id,
                "action": command_id.split(".", 1)[1],
                "summary": AUTH_COMMAND_NOTES[command_id],
                "schemaId": command_id,
            }
            for command_id in ("auth.%s" % action for action in AUTH_ACTIONS)
        ],
        "nextStep": "cmcloud schema --command-id <command-id>",
    }
