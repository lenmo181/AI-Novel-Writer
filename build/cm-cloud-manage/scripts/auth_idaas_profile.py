"""auth 域受控 profile 登记：SDK 登记参数与私有隔离目录的唯一可信来源。

约束（与执行 md 第 4.1 / 10 节一致）：
- devId/clientId/skillId/skillName/strategyId/templateId/scopeIds 与存储后端、
  SDK 私有目录一律来自受控登记文件 scripts/auth_profiles.json，Agent 不能覆盖；
- accessToken/uuid/完整 clawUid/Keychain key-value 等身份材料不接受 Agent 提交；
- storage_backend 缺失/null/空串/纯空白时默认 file；显式 keychain 且系统 Keychain 不可用
  时返回 SECURE_STORE_UNAVAILABLE，禁止任何明文降级（不做 file 后端静默兜底）；
- SDK 私有目录（auth_info_path、fallback_clawUid_path）按智能体 + profile + skill 三重
  隔离：同一台机器上的不同智能体（OpenClaw / Codex / 豆包工作等）各自持有设备身份，
  不共用凭据文件、不共用 clawUid 与专属目录。

本模块只读登记文件与探测本机存储后端，不调用任何 SDK 公共接口，也不新增错误码枚举。
"""

import importlib.util
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass

from cmcloud_core import result as result_module

DEFAULT_PROFILE = "default"

STORAGE_BACKEND_KEYCHAIN = "keychain"
STORAGE_BACKEND_FILE = "file"
STORAGE_BACKENDS = (STORAGE_BACKEND_KEYCHAIN, STORAGE_BACKEND_FILE)


def _resolve_storage_backend(value, profile):
    """归一 storage_backend：缺失/null/空串/纯空白 → file；仅接受 file/keychain。

    非字符串（含 bool/数字/对象/数组）与枚举外字符串一律明确拒绝，避免用真假判断
    把 ``false`` / ``0`` 之类错误值当成"未配置"。显式 keychain 仍按原系统凭据库流程执行。
    """
    if value is None:
        return STORAGE_BACKEND_FILE
    if not isinstance(value, str):
        raise _registration_error(
            "profile %s 的 storage_backend 必须是 keychain 或 file 字符串" % profile
        )
    text = value.strip()
    if not text:
        return STORAGE_BACKEND_FILE
    if text not in STORAGE_BACKENDS:
        raise _registration_error("profile %s 的 storage_backend 必须是 keychain 或 file" % profile)
    return text

AUTH_PROFILES_FILENAME = "auth_profiles.json"

# SDK 私有目录默认模板：占位符同时出现才能保证 智能体 + profile + skill 三重隔离。
AUTH_INFO_PATH_TEMPLATE = "~/.cmcloud/auth/{agent}/{profile}/{skillId}"
FALLBACK_CLAW_UID_PATH_TEMPLATE = "~/.cmcloud/auth/{agent}/{profile}/{skillId}/fallback_clawUid.json"
DEFAULT_FALLBACK_FILENAME = "fallback_clawUid.json"

# 私有路径占位符白名单：未知占位符直接拒绝，避免 .format 抛裸 KeyError。
ALLOWED_PATH_PLACEHOLDERS = ("agent", "profile", "skillId")


# 受控登记字段：Agent 不得提交，也不得作为入参覆盖。
REGISTRATION_FIELDS = (
    "devId",
    "clientId",
    "skillId",
    "skillName",
    "strategyId",
    "templateId",
    "scopeIds",
)

# 完整身份材料：既不在登记文件里存放，也不接受 Agent 提交。
IDENTITY_FIELDS = (
    "accessToken",
    "creditToken",
    "uuid",
    "uuid_param",
    "clawUid",
    "sign",
    "token",
    "deviceId",
    "key",
    "value",
)

# 存储与目录字段：只能来自受控登记文件。
STORAGE_FIELDS = ("storage_backend", "auth_info_path", "clawUid_path", "fallback_clawUid_path")

FORBIDDEN_AGENT_FIELDS = frozenset(REGISTRATION_FIELDS + IDENTITY_FIELDS + STORAGE_FIELDS)

_KNOWN_PROFILE_KEYS = frozenset(REGISTRATION_FIELDS + STORAGE_FIELDS)

_KNOWN_DOCUMENT_KEYS = frozenset(("schemaVersion", "defaultProfile", "profiles"))


@dataclass(frozen=True)
class AuthProfile:
    """一个 profile 的不可变登记结果；凭据值不落在此对象中。"""

    profile: str
    dev_id: str
    client_id: str
    skill_id: str
    skill_name: str
    strategy_id: str
    template_id: str | None
    scope_ids: tuple
    storage_backend: str
    auth_info_path: str
    claw_uid_path: str | None
    fallback_claw_uid_path: str
    agent_code: str
    agent_source: str

    @property
    def profile_ref(self):
        return profile_ref(self.profile)

    @property
    def uses_keychain(self):
        return self.storage_backend == STORAGE_BACKEND_KEYCHAIN

    def registration_fields(self):
        """SDK getToken/unbinding 的登记入参；Agent 不可覆盖。"""
        fields = {
            "devId": self.dev_id,
            "clientId": self.client_id,
            "skillId": self.skill_id,
            "skillName": self.skill_name,
            "strategyId": self.strategy_id,
        }
        if self.template_id:
            fields["templateId"] = self.template_id
        if self.scope_ids:
            fields["scopeIds"] = list(self.scope_ids)
        return fields

    def setconfig_kwargs(self):
        """token_service.setConfig 的受控初始化参数（日志恒关闭）。"""
        return {
            "auth_info_path": self.auth_info_path,
            "storage_backend": self.storage_backend,
            "clawUid_path": self.claw_uid_path,
            "fallback_clawUid_path": self.fallback_claw_uid_path,
            "log_enabled": False,
        }

    def describe(self):
        """对外可输出的 profile 摘要：不含 devId/clientId 与任何身份材料。"""
        from cmcloud_auth import agent_scope

        return {
            "profileRef": self.profile_ref,
            "skillId": self.skill_id,
            "skillName": self.skill_name,
            "storageBackend": self.storage_backend,
            "agentCode": self.agent_code,
            "agentName": agent_scope.agent_display_name(self.agent_code),
            "agentSource": self.agent_source,
        }


def profile_ref(profile):
    """profileRef 语义与台账一致：就是受控 profile 名。

    入参只接受「profile 名字符串」或 `AuthProfile`（None/空串归一为默认 profile）；
    其他类型属内部编程错误，抛 `ResultError` 由顶层统一兜底。此处绝不返回对象本身，
    避免 dataclass repr 把 devId/clientId/skillName 与本机私有路径拼进对外 message。
    """
    if isinstance(profile, AuthProfile):
        return profile.profile
    if profile is None or profile == "":
        return DEFAULT_PROFILE
    if not isinstance(profile, str):
        raise result_module.ResultError("profile 必须是 str 或 AuthProfile")
    return profile


# 受控登记文件内容/字段非法属「受控配置异常」，按 §5.8 统一映射 FAILED，
# 不让 Agent 误以为可以「补正后重新发起」（Agent 无权修改登记值）。
_REGISTRATION_ERROR_PREFIX = "auth 受控登记配置异常，需管理员修正："


def _registration_error(message):
    """受控登记配置/环境错误的统一错误载体（不含路径与身份材料）。"""
    return result_module.ExecutionError(result_module.FAILED, _REGISTRATION_ERROR_PREFIX + message)


def default_profiles_path():
    """受控登记文件位置：打包运行取 EXE 同目录，源码运行取代码包内同名文件。"""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), AUTH_PROFILES_FILENAME)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), AUTH_PROFILES_FILENAME)


def load_profiles(path=None):
    """读取受控登记文件；未知顶层/登记字段一律拒绝，避免拼写错误被静默忽略。"""
    resolved = os.path.abspath(path) if path is not None else default_profiles_path()
    try:
        with open(resolved, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except OSError as err:
        raise result_module.ExecutionError(
            result_module.CAPABILITY_UNAVAILABLE,
            "auth 受控登记文件不可读取，auth 命令不可用: %s" % err.__class__.__name__,
        )
    try:
        document = json.loads(raw)
    except ValueError:
        raise _registration_error("登记文件不是合法 JSON")
    if not isinstance(document, dict):
        raise _registration_error("登记文件顶层必须是对象")
    unknown = sorted(set(document) - _KNOWN_DOCUMENT_KEYS)
    if unknown:
        raise _registration_error("登记文件含未登记顶层字段: %s" % ", ".join(unknown))
    profiles = document.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise _registration_error("登记文件缺少 profiles 对象")
    for name, entry in profiles.items():
        if not isinstance(name, str) or not name:
            raise _registration_error("profile 名必须是非空字符串")
        if not isinstance(entry, dict):
            raise _registration_error("profile %s 必须是对象" % name)
        extra = sorted(set(entry) - _KNOWN_PROFILE_KEYS)
        if extra:
            raise _registration_error("profile %s 含未登记字段: %s" % (name, ", ".join(extra)))
    default_profile = document.get("defaultProfile") or DEFAULT_PROFILE
    if not isinstance(default_profile, str) or default_profile not in profiles:
        raise _registration_error("defaultProfile 必须是 profiles 中已登记的 key")
    return {
        "schemaVersion": document.get("schemaVersion"),
        "defaultProfile": default_profile,
        "profiles": profiles,
        "path": resolved,
    }


def load_profile(profile=DEFAULT_PROFILE, *, path=None, overrides=None):
    """按 profile 名解析受控登记；overrides 非空即视为 Agent 越权覆盖，直接拒绝。"""
    assert_no_agent_fields(overrides or ())
    document = load_profiles(path)
    name = profile or document["defaultProfile"]
    if not isinstance(name, str) or not name:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "profile 名必须是非空字符串")
    entry = document["profiles"].get(name)
    if entry is None:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "未登记的 profile: %s" % name)
    skill_id = _require_text(entry, "skillId", name)
    storage_backend = _resolve_storage_backend(entry.get("storage_backend"), name)
    from cmcloud_auth import agent_scope, scoped_keyring

    agent_code, agent_source = agent_scope.resolve_agent_code()
    # 绑定本进程的智能体命名空间；keychain 后端再确保凭据库后端已按命名空间包装，
    # 使 SDK 与包装层的每次 keychain 读写都落在本智能体自己的槽位。
    # file 后端靠目录（{agent}/{profile}/{skillId}）隔离，不注入包装器，也就不解析系统
    # 凭据库——钥匙串不可用时 file 模式的取票与落盘仍然可用。
    namespace = agent_scope.scope_namespace(agent_code, name)
    agent_scope.bind(agent_code, name)
    scoped_keyring.bind(namespace)
    if storage_backend == STORAGE_BACKEND_KEYCHAIN:
        scoped_keyring.ensure_installed()
    auth_info_path = isolate_directory(
        entry.get("auth_info_path"),
        agent=agent_code,
        profile=name,
        skill_id=skill_id,
        template=AUTH_INFO_PATH_TEMPLATE,
    )
    fallback_claw_uid_path = isolate_file(
        entry.get("fallback_clawUid_path"),
        agent=agent_code,
        profile=name,
        skill_id=skill_id,
        template=FALLBACK_CLAW_UID_PATH_TEMPLATE,
        default_filename=DEFAULT_FALLBACK_FILENAME,
    )
    return AuthProfile(
        profile=name,
        dev_id=_require_text(entry, "devId", name),
        client_id=_require_text(entry, "clientId", name),
        skill_id=skill_id,
        skill_name=_require_text(entry, "skillName", name),
        strategy_id=_require_text(entry, "strategyId", name),
        template_id=_optional_text(entry, "templateId", name),
        scope_ids=_optional_text_list(entry, "scopeIds", name),
        storage_backend=storage_backend,
        auth_info_path=auth_info_path,
        claw_uid_path=_optional_host_path(entry.get("clawUid_path"), name),
        fallback_claw_uid_path=fallback_claw_uid_path,
        agent_code=agent_code,
        agent_source=agent_source,
    )





def assert_no_agent_fields(field_names):
    """拒绝 Agent 提交登记参数或完整身份材料；命中即 INVALID_ARGUMENT。"""
    provided = {str(name) for name in (field_names or ())}
    hit = sorted(provided & FORBIDDEN_AGENT_FIELDS)
    if hit:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "以下字段由受控 profile 登记或可信运行时提供，不接受 Agent 提交: %s" % ", ".join(hit),
        )
    return True


def secure_store_available(probe=None):
    """探测系统 Keychain 后端是否可用；探测实现可注入，便于受控测试。"""
    checker = probe or _default_secure_store_probe
    try:
        return bool(checker())
    except Exception:
        return False


def require_secure_store(profile, probe=None):
    """Keychain 后端不可用时返回 SECURE_STORE_UNAVAILABLE，绝不降级到明文文件。"""
    if not secure_store_available(probe):
        raise result_module.ExecutionError(
            result_module.SECURE_STORE_UNAVAILABLE,
            "系统 Keychain 存储后端不可用，已拒绝任何明文降级（profile: %s）" % profile_ref(profile),
        )
    return True


def require_profile_storage(profile, probe=None):
    """按 profile 的 storage_backend 校验存储可用性（不可用即失败，绝不降级）。"""
    if profile.uses_keychain:
        return require_secure_store(profile, probe=probe)
    checker = probe or (lambda _profile: _default_state_dir_probe(_profile))
    if not checker(profile):
        label = getattr(profile, "profile_ref", None) or profile
        raise result_module.ExecutionError(
            result_module.SECURE_STORE_UNAVAILABLE,
            "本机状态目录不可写，已拒绝任何明文降级（profile: %s）" % label,
        )
    return True


def isolate_directory(raw_path, *, profile, skill_id, template, agent=None):
    """目录类私有路径：占位符缺失时强制追加智能体/profile/skill 隔离层。"""
    if isinstance(raw_path, str) and raw_path.strip():
        return _render_or_nest(raw_path.strip(), profile, skill_id, fallback_filename=None, agent=agent)
    return _render_or_nest(template, profile, skill_id, fallback_filename=None, agent=agent)


def isolate_file(raw_path, *, profile, skill_id, template, default_filename, agent=None):
    """文件类私有路径：占位符缺失时把智能体/profile/skill 插到目录与文件名之间。"""
    if isinstance(raw_path, str) and raw_path.strip():
        return _render_or_nest(
            raw_path.strip(), profile, skill_id, fallback_filename=default_filename, agent=agent
        )
    return _render_or_nest(template, profile, skill_id, fallback_filename=default_filename, agent=agent)


def _validate_placeholders(raw_path, profile):
    """私有路径的占位符白名单：未知占位符直接拒绝，不落到 .format 的裸 KeyError。"""
    for token in re.findall(r"\{([^{}]*)\}", str(raw_path or "")):
        if token and token not in ALLOWED_PATH_PLACEHOLDERS:
            raise _registration_error(
                "profile %s 的私有路径含未登记占位符: {%s}（允许 %s）"
                % (profile, token, ", ".join(ALLOWED_PATH_PLACEHOLDERS))
            )


def _render_or_nest(raw_path, profile, skill_id, fallback_filename, agent=None):
    _validate_placeholders(raw_path, profile)
    expanded = os.path.expanduser(raw_path)
    segment = _agent_segment(agent)
    if "{agent}" in expanded:
        return os.path.normpath(
            expanded.format(agent=segment, profile=profile, skillId=skill_id)
        )
    # 自定义私有路径缺少 {agent} 时，把智能体层插到 profile/skill 之上，保证同一
    # 配置值在不同智能体下解析出互不共用的路径。
    pattern = None
    if "{profile}" in expanded:
        pattern = expanded.replace("{profile}", "{agent}/{profile}", 1)
    elif "{skillId}" in expanded:
        pattern = expanded.replace("{skillId}", "{agent}/{skillId}", 1)
    if pattern is not None:
        return os.path.normpath(pattern.format(agent=segment, profile=profile, skillId=skill_id))
    if fallback_filename is None:
        # 目录：配置值视为基目录，追加智能体/profile/skill 三层隔离
        return os.path.normpath(os.path.join(expanded, segment, profile, skill_id))
    if expanded.lower().endswith(".json"):
        directory, filename = os.path.split(expanded)
    else:
        directory, filename = expanded, fallback_filename
    return os.path.normpath(os.path.join(directory, segment, profile, skill_id, filename))


def _agent_segment(agent):
    """智能体路径段：外部直接构造 profile 时未给 agent 也不得退化成共用目录。"""
    if isinstance(agent, str) and agent.strip():
        return agent.strip()
    from cmcloud_auth import agent_scope

    return agent_scope.FALLBACK_AGENT_CODE


_PROBE_SERVICE = "cmcloud.secure-store.probe"
_PROBE_ACCOUNT = "probe"


def _import_keyring():
    """按需导入 keyring；依赖缺失或导入失败返回 None。"""
    try:
        spec = importlib.util.find_spec("keyring")
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    try:
        import keyring
    except Exception:
        return None
    return keyring


def _default_secure_store_probe(importer=None):
    """安全存储可用性探测：必须**试写 + 回读**成功才算可用。

    仅探测后端对象存在（旧实现）不能证明后续写入会成功：写入被拒绝或后端只读时旧实现
    仍报可用，取票失败会被误判成"环境可用"。探针条目写完立即删除，写入被拒即判不可用。
    """
    keyring = (importer or _import_keyring)()
    if keyring is None:
        return False
    token = uuid.uuid4().hex
    try:
        keyring.set_password(_PROBE_SERVICE, _PROBE_ACCOUNT, token)
        value = keyring.get_password(_PROBE_SERVICE, _PROBE_ACCOUNT)
    except Exception:
        return False
    finally:
        try:
            keyring.delete_password(_PROBE_SERVICE, _PROBE_ACCOUNT)
        except Exception:
            pass
    return value == token


def _default_state_dir_probe(auth_profile):
    """只读检查 file 后端路径；实际写入失败由凭据写入处报告。

    WorkBuddy 会把删除探针文件当作用户文件删除并逐次要求确认，因此这里
    不能创建/删除临时文件。该检查只排除路径结构或显式权限错误，不能替代
    真正写入时的权限、磁盘空间与回读校验。
    """
    from cmcloud_auth import credential_store

    path = credential_store.state_file_path(auth_profile)
    directory = os.path.dirname(path)
    while not os.path.lexists(directory):
        parent = os.path.dirname(directory)
        if parent == directory:
            return False
        directory = parent
    return os.path.isdir(directory) and os.access(directory, os.W_OK | os.X_OK)


def _require_text(entry, key, profile_name):
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _registration_error("profile %s 缺少合法字段 %s" % (profile_name, key))
    return value.strip()


def _optional_text(entry, key, profile_name):
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _registration_error("profile %s 的 %s 必须是非空字符串或 null" % (profile_name, key))
    return value.strip()


def _optional_text_list(entry, key, profile_name):
    value = entry.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _registration_error("profile %s 的 %s 必须是字符串数组" % (profile_name, key))
    items = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise _registration_error("profile %s 的 %s 只允许非空字符串" % (profile_name, key))
        if item.strip() not in items:
            items.append(item.strip())
    return tuple(items)


def _optional_host_path(raw_path, profile_name):
    """宿主主路径（OpenCLAW device.json）：仅当登记文件显式给出时才固定，且不参与隔离。"""
    if raw_path is None:
        return None
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise _registration_error("profile %s 的 clawUid_path 必须是非空字符串或 null" % profile_name)
    return os.path.normpath(os.path.expanduser(raw_path.strip()))
