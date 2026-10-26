"""智能体平台识别与命名空间。

编码以产品表为准（14 项，见 `AGENT_TABLE`）。SDK `device_info._AGENT_RULES` 的
识别结果只作为兜底来源：识别失败、识别为空或识别出表外编码时一律归为 `other`，
保证命名空间在任何环境下都是确定值，不会因为一次环境噪声漂到另一个命名空间。

受控覆盖：`CMCLOUD_AGENT_CODE`（取值限本表编码或产品名称），由宿主/发布配置显式
指定，优先级高于自动识别；非法取值按本地受控配置错误处理，不静默退化。
"""

import os

from cmcloud_core import result as result_module
from cmcloud_auth import idaas_common

# 产品表（顺序即产品顺序）：编码 -> 产品名称
AGENT_TABLE = (
    ("work_buddy", "Workbuddy"),
    ("claw_x", "ClawX"),
    ("easy_claw", "EasyClaw"),
    ("q_claw", "QClaw"),
    ("auto_claw", "AutoClaw"),
    ("kimi_claw", "KimiClaw"),
    ("open_claw", "OpenClaw"),
    ("hermes", "Hermes"),
    ("claude_code", "Claudecode"),
    ("codex", "Codex"),
    ("doubao_work", "豆包工作"),
    ("qianwen_work", "千问办公"),
    ("baidu_buddy", "百度搭子"),
    ("other", "其他"),
)

AGENT_CODES = tuple(code for code, _name in AGENT_TABLE)
AGENT_NAMES = dict(AGENT_TABLE)
AGENT_CODES_BY_NAME = {name.strip().lower(): code for code, name in AGENT_TABLE}

# 未识别或识别失败的统一归属，保证命名空间始终有确定值
FALLBACK_AGENT_CODE = "other"

# 受控覆盖：宿主/发布配置显式指定当前智能体编码
AGENT_CODE_ENV = "CMCLOUD_AGENT_CODE"

SOURCE_OVERRIDE = "override"
SOURCE_DETECTED = "detected"
SOURCE_FALLBACK = "fallback"

_CONFIG_ERROR_PREFIX = "受控智能体编码配置错误: "

_current_namespace = None


def normalize_agent_code(value):
    """把编码或产品名称归一为表内编码；无法归一返回 None。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text in AGENT_CODES:
        return text
    return AGENT_CODES_BY_NAME.get(text.lower())


def agent_display_name(code):
    """表内编码对应的产品名称；表外编码返回 None。"""
    return AGENT_NAMES.get(code)


def _config_error(message):
    return result_module.ExecutionError(result_module.FAILED, _CONFIG_ERROR_PREFIX + message)


def overridden_agent_code():
    """读取受控覆盖；未设置返回 None，取值非法直接报错。"""
    raw = os.environ.get(AGENT_CODE_ENV)
    if raw is None or not raw.strip():
        return None
    code = normalize_agent_code(raw)
    if code is None:
        raise _config_error(
            "环境变量 %s 取值不在允许的智能体编码内（允许：%s）"
            % (AGENT_CODE_ENV, ", ".join(AGENT_CODES))
        )
    return code


def _agent_info_module():
    return idaas_common._import_sdk_module(idaas_common.DEVICE_INFO_MODULE)


def detected_agent_code():
    """按 SDK 识别规则取编码；失败或表外一律返回兜底编码。"""
    try:
        info = _agent_info_module().collect_agent_info()
    except Exception:
        return FALLBACK_AGENT_CODE
    raw = info.get("agentPlatform") or info.get("agentCode")
    code = normalize_agent_code(raw)
    return code if code is not None else FALLBACK_AGENT_CODE


def resolve_agent_code():
    """返回 (编码, 来源)；来源为 override / detected / fallback。"""
    override = overridden_agent_code()
    if override is not None:
        return override, SOURCE_OVERRIDE
    code = detected_agent_code()
    if code == FALLBACK_AGENT_CODE:
        return code, SOURCE_FALLBACK
    return code, SOURCE_DETECTED


def scope_namespace(agent_code, profile_ref):
    """存储命名空间：智能体编码 + profile，两者都不得为空。"""
    agent = normalize_agent_code(agent_code) or FALLBACK_AGENT_CODE
    profile = profile_ref if isinstance(profile_ref, str) and profile_ref.strip() else None
    if profile is None:
        raise _config_error("缺少 profile 标识，无法确定智能体命名空间")
    return "%s::%s" % (agent, profile.strip())


def bind(agent_code, profile_ref):
    """绑定当前调用的命名空间；profile 未确定前保持未绑定（透传旧行为）。"""
    global _current_namespace
    _current_namespace = scope_namespace(agent_code, profile_ref)
    return _current_namespace


def current_namespace():
    """当前绑定的命名空间；未绑定时返回 None。"""
    return _current_namespace


def describe(profile_ref=None):
    """可供回执/诊断输出的智能体摘要（不含路径等本机信息）。"""
    code, source = resolve_agent_code()
    summary = {
        "agentCode": code,
        "agentName": agent_display_name(code),
        "agentSource": source,
    }
    if isinstance(profile_ref, str) and profile_ref.strip():
        summary["namespace"] = scope_namespace(code, profile_ref)
    return summary


__all__ = [
    "AGENT_CODES",
    "AGENT_CODE_ENV",
    "AGENT_NAMES",
    "AGENT_TABLE",
    "FALLBACK_AGENT_CODE",
    "SOURCE_DETECTED",
    "SOURCE_FALLBACK",
    "SOURCE_OVERRIDE",
    "agent_display_name",
    "bind",
    "current_namespace",
    "describe",
    "detected_agent_code",
    "normalize_agent_code",
    "overridden_agent_code",
    "resolve_agent_code",
    "scope_namespace",
]
