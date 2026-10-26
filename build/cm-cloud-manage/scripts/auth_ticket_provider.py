"""业务接口票据 Provider：取票、按 profile 隔离存放并生成请求头。

``creditToken`` 随 profile 存入系统凭据库或加密 cmcloud_state.json（与 SDK 授权文件
同目录，按 agent + profile + skill 隔离），不落明文文件，也不进入 CLI 输出、operation ledger
或诊断日志。长期 ``accessToken`` / ``uuid`` / ``clawUid`` 仍由既有 SDK 存储后端管理。

设备身份（clawUid）在本文件里区分三种来源：宿主实例源（host）、本地兜底文件
（fallback）与取票时现场生成（generated）。全新安装尚无身份记录时允许 SDK 在首次取票
建立身份；已有身份元数据后若副本缺失或指纹变化则明确失败，不做静默降级。
"""

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone

from cmcloud_core import result as result_module
from cmcloud_auth import credential_store, idaas_common, idaas_profile


SCHEMA_VERSION = "1.0"
REFRESH_MARGIN_SECONDS = 60
TICKET_MAX_AGE_SECONDS = 1740
MIN_TTL_FACTOR = 2

# 本地到期上限 29 分钟，提前 60 秒换票，正常可复用 28 分钟；平台更短时取较短值。
EXPIRE_TIME_UNIT = "minute"
EXPIRE_TIME_UNIT_ENV = "CMCLOUD_TICKET_EXPIRE_TIME_UNIT"
EXPIRE_TIME_UNITS = ("minute", "second", "absolute")
MAX_TTL_DAYS = 90

SOURCE_HOST = "host"
SOURCE_FALLBACK = "fallback"
SOURCE_GENERATED = "generated"

_LEGACY_CACHE_PREFIX = "credit_token_"
_LEGACY_CACHE_SUFFIX = ".json"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

_FINGERPRINT_LENGTH = 12


def legacy_cache_dir():
    """旧版明文票据目录（仅迁移时读取，新版本不再写入）。"""
    return os.path.dirname(os.path.abspath(__file__))


def _legacy_cache_path(auth_profile):
    safe = _SAFE_NAME.sub("_", str(auth_profile.profile_ref)).strip("._") or "default"
    return os.path.join(
        legacy_cache_dir(), "%s%s%s" % (_LEGACY_CACHE_PREFIX, safe, _LEGACY_CACHE_SUFFIX)
    )


class CredentialTicketStore:
    """复用凭据存储：file 为加密状态文件，keychain 为系统凭据库。"""

    def load(self, auth_profile):
        if reauthorization_pending(auth_profile):
            return None
        document = credential_store.load(auth_profile, credential_store.KIND_TICKET, fresh=True)
        if document is None:
            document = _migrate_legacy_ticket(auth_profile)
        if document is None:
            return None
        return _validated_ticket(auth_profile, document)

    def save(self, auth_profile, ticket):
        now = datetime.now(timezone.utc)
        raw_expires_at = ticket.get("expiresAt")
        expires_at = (
            _parse_absolute_time(raw_expires_at)
            if raw_expires_at is not None
            else _expiry_deadline(ticket["expireTime"], now)
        )
        if expires_at is None:
            raise _contract_error("expireTime 无法换算为票据到期时间")
        acquired_at = _parse_absolute_time(ticket.get("acquiredAt")) or now
        expires_at = min(expires_at, acquired_at + timedelta(seconds=TICKET_MAX_AGE_SECONDS))
        document = {
            "profileRef": auth_profile.profile_ref,
            "skillId": auth_profile.skill_id,
            "creditToken": ticket["creditToken"],
            "expireTime": str(ticket["expireTime"]),
            "expireTimeUnit": EXPIRE_TIME_UNIT,
            "expiresAt": _iso(expires_at),
            "savedAt": _iso(now),
            "acquiredAt": _iso(acquired_at),
            "source": "preauth",
        }
        credential_store.save(auth_profile, credential_store.KIND_TICKET, document)
        return document

    def clear(self, auth_profile):
        return credential_store.delete(auth_profile, credential_store.KIND_TICKET)


def _validated_ticket(auth_profile, document):
    """票据条目自校验：结构、归属与必需字段不符即视为不可用。"""
    if not isinstance(document, dict):
        return None
    if document.get("profileRef") != auth_profile.profile_ref:
        return None
    if document.get("skillId") != auth_profile.skill_id:
        return None
    for field in ("creditToken", "expireTime"):
        value = document.get(field)
        if not isinstance(value, str) or not value.strip():
            return None
    if _parse_absolute_time(document.get("expiresAt")) is None:
        return None
    return document


def _credit_token_of(ticket):
    """票据字典里的 creditToken（去空白）；结构不符返回 None。"""
    token = ticket.get("creditToken") if isinstance(ticket, dict) else None
    if not isinstance(token, str) or not token.strip():
        return None
    return token.strip()


def _migrate_legacy_ticket(auth_profile):
    """一次性迁移旧版明文票据：写入凭据库成功后才删除原文件。"""
    path = _legacy_cache_path(auth_profile)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        idaas_common.diagnostic("旧版本地票据不可读，已忽略并保留原文件")
        return None
    validated = _validated_ticket(auth_profile, document)
    if validated is None:
        return None
    try:
        credential_store.save(auth_profile, credential_store.KIND_TICKET, dict(validated))
    except result_module.ExecutionError:
        idaas_common.diagnostic("旧版本地票据迁移失败，已保留原文件")
        return None
    try:
        os.remove(path)
        idaas_common.diagnostic("旧版本地票据已迁入凭据库并删除原文件")
    except OSError:
        idaas_common.diagnostic("旧版本地票据已迁入凭据库，原文件删除失败")
    return validated


# ---------------------------------------------------------------------------
# 设备身份：来源判定、指纹记录与副本校验
# ---------------------------------------------------------------------------


def _fingerprint(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:_FINGERPRINT_LENGTH]


def _load_identity_meta(auth_profile):
    return credential_store.load(auth_profile, credential_store.KIND_CLAWUID_META)


def _save_identity_meta(auth_profile, source_kind, origin_ref, fingerprint, now):
    from cmcloud_auth import agent_scope

    agent_code = credential_store.agent_segment(auth_profile)
    document = {
        # 记录本次身份所属的智能体命名空间：识别结果变化时据此提示，不静默复用
        "agentCode": agent_code,
        "namespace": agent_scope.scope_namespace(agent_code, auth_profile.profile_ref),
        "profileRef": auth_profile.profile_ref,
        "skillId": auth_profile.skill_id,
        "sourceKind": source_kind,
        "originRef": origin_ref or "",
        "originFingerprint": fingerprint,
        "verifiedAt": _iso(now),
        "lastServerAcceptAt": _iso(now),
        "canRebuild": source_kind != SOURCE_HOST,
    }
    credential_store.save(auth_profile, credential_store.KIND_CLAWUID_META, document)


def _fallback_device_id(path):
    """本地兜底文件里的设备 ID；文件缺失或不可解析返回 None。"""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    value = document.get("deviceId") if isinstance(document, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _host_claw_uid():
    """宿主实例源：与 SDK 的智能体识别规则一致，未命中规则时返回空。"""
    module = _device_info_module()
    if module is None:
        return None, ""
    try:
        value = module.detect_claw_uid()
    except Exception:
        return None, ""
    if not isinstance(value, str) or not value.strip():
        return None, ""
    return value.strip(), _host_origin_ref(module)


def _device_info_module():
    try:
        return idaas_common._import_sdk_module(idaas_common.DEVICE_INFO_MODULE)
    except result_module.ExecutionError:
        return None


def _host_origin_ref(module):
    """宿主身份来源文件（尽力而为，取不到时留空，仅用于排障展示）。"""
    try:
        code, _path, _instance = module._detect_agent()
    except Exception:
        return ""
    for rule in getattr(module, "_AGENT_RULES", ()) or ():
        if isinstance(rule, dict) and rule.get("code") == code:
            source = rule.get("instance_source")
            return source if isinstance(source, str) else ""
    return ""


def _source_of(auth_profile):
    """按 SDK 的读取顺序判定本次身份来源：兜底文件优先，其次宿主源，最后现场生成。"""
    fallback_path = auth_profile.fallback_claw_uid_path
    if _fallback_device_id(fallback_path):
        return SOURCE_FALLBACK, fallback_path or ""
    _value, origin_ref = _host_claw_uid()
    if origin_ref:
        return SOURCE_HOST, origin_ref
    return SOURCE_GENERATED, fallback_path or ""


class BusinessHeaderProvider:
    """为 RuntimeContext 提供当前 profile 的五项业务请求头。"""

    def __init__(self, auth_profile, *, store=None, modules=None, clock=None):
        self._auth_profile = (
            auth_profile if isinstance(auth_profile, idaas_profile.AuthProfile) else None
        )
        self._profile_ref = (
            auth_profile.profile_ref
            if isinstance(auth_profile, idaas_profile.AuthProfile)
            else auth_profile
        )
        self.store = store or CredentialTicketStore()
        self.modules = modules
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._ticket = None
        self._lock = threading.Lock()

    @property
    def auth_profile(self):
        if self._auth_profile is None:
            self._auth_profile = idaas_profile.load_profile(self._profile_ref)
        return self._auth_profile

    def headers(self):
        with self._lock:
            if reauthorization_pending(self.auth_profile):
                self._ticket = None
                self._ticket = self._acquire()
                return self._headers_from_ticket(self._ticket)
            claw_uid = credential_store.sdk_claw_uid(self.auth_profile)
            identity_meta = _load_identity_meta(self.auth_profile)
            if claw_uid is None:
                # 全新安装允许 SDK 在首次业务取票时建立设备身份；如果已经存在身份元数据，
                # 则说明 SDK 的身份副本异常丢失，不能静默生成另一套身份。
                if isinstance(identity_meta, dict):
                    raise _identity_error(
                        "本机设备身份副本缺失，本地凭据不完整，请重新完成授权"
                    )
                ticket = self._acquire()
                self._ticket = ticket
                return self._headers_from_ticket(ticket)
            # 以持久状态为准，其他进程完成授权后也不能继续复用内存旧票据。
            ticket = self.store.load(self.auth_profile)
            if not _ticket_valid(ticket, self.clock()) or not self._identity_unchanged(claw_uid):
                ticket = self._acquire(credit_token=_credit_token_of(ticket))
                claw_uid = ticket["clawUid"]
            self._ticket = ticket
            return self._headers_from_ticket(ticket, claw_uid=claw_uid)

    def refresh_headers(self, *, reason_code=None):
        """明确作废当前短票据并重新取票；供业务接口鉴权失败后的单次恢复使用。"""
        with self._lock:
            if reauthorization_pending(self.auth_profile):
                self._ticket = None
                self._ticket = self._acquire()
                return self._headers_from_ticket(self._ticket)
            previous = self.store.load(self.auth_profile)
            self._ticket = None
            self.store.clear(self.auth_profile)
            ticket = self._acquire(
                credit_token=_credit_token_of(previous),
                negotiate=reason_code == idaas_common.SDK_CODE_SCOPE_CHANGED,
            )
            self._ticket = ticket
            return self._headers_from_ticket(ticket)

    def invalidate_ticket(self):
        """只清除短期业务票据，不删除长期授权、设备身份或专属根缓存。"""
        with self._lock:
            self._ticket = None
            return self.store.clear(self.auth_profile)

    def _headers_from_ticket(self, ticket, claw_uid=None):
        return {
            "creditToken": ticket["creditToken"],
            "skillId": self.auth_profile.skill_id,
            "clientId": self.auth_profile.client_id,
            "strategyId": self.auth_profile.strategy_id,
            "clawUid": claw_uid or ticket["clawUid"],
        }

    def _identity_unchanged(self, claw_uid):
        """缓存票据只在身份指纹与上次记录一致时才可用，避免新旧身份混用。"""
        meta = _load_identity_meta(self.auth_profile)
        if not isinstance(meta, dict):
            return False
        previous = meta.get("originFingerprint")
        if not isinstance(previous, str) or not previous:
            return False
        return previous == _fingerprint(claw_uid)

    def _acquire(self, credit_token=None, negotiate=False):
        trace_id = idaas_common.new_trace_id()
        sdk = idaas_common.configure_sdk(
            self.auth_profile, modules=self.modules, trace_id=trace_id
        )
        raw = idaas_common.acquire_token(
            sdk,
            self.auth_profile,
            command="business.ticket",
            credit_token=credit_token,
            negotiate=negotiate,
        )
        if not isinstance(raw, dict):
            raise _contract_error("SDK 取票响应不是对象")
        code = idaas_common.normalize_sdk_code(raw.get("resultCode"))
        if idaas_common.is_auth_entry_result(code):
            if reauthorization_pending(self.auth_profile):
                # 入口过期后的普通取票可能下发新 uuid；保存新入口，继续保持停用旧票据。
                data = raw.get("data") or {}
                url = idaas_common.validate_auth_page_url(data.get("authPageUrl"))
                session = idaas_common.session_ref(data)
                challenge = idaas_common.challenge_ref(
                    self.auth_profile, skill_id=self.auth_profile.skill_id,
                    scope=self.auth_profile.scope_ids, nonce=idaas_common.challenge_nonce(session))
                remember_pending_entry(self.auth_profile, url, challenge,
                                       session_ref=session, reauthorization=True)
            note_identity_source(self.auth_profile)
            # 授权入口只由 auth.login 回执提供：业务信封只下发 nextAction=auth.login。
            raise idaas_common.make_error(
                result_module.AUTH_REQUIRED,
                idaas_common.AUTH_REQUIRED_MESSAGE,
            )
        if not idaas_common.is_sdk_success(code):
            idaas_common.diagnostic_sdk_result("business.ticket", raw)
            raise idaas_common.error_from_sdk(code, operation_type=result_module.OPERATION_READ)
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        return persist_sdk_ticket(
            self.auth_profile,
            data,
            store=self.store,
            clock=self.clock,
        )


def note_identity_source(auth_profile):
    """记录本次设备身份来源，不再阻止下发授权入口。

    历史上此处是"身份闸门"：身份只能现场生成（既无兜底文件、宿主识别也未命中）时直接按
    本地安全存储不可用失败，不展示入口。现改为只写一条脱敏诊断，授权入口照常下发；设备
    身份的一致性由后续取票自行校验（身份变化只会拒绝复用旧票据，不会写坏既有凭据）。
    """
    source_kind, _origin = _source_of(auth_profile)
    if source_kind == SOURCE_GENERATED:
        idaas_common.diagnostic(
            "设备身份来源为现场生成（兜底文件与宿主标识都不可用），授权入口照常下发"
        )
    return source_kind


def persist_sdk_ticket(auth_profile, data, *, store=None, clock=None, reauthorized=False):
    """校验 SDK 成功数据，把短票据与身份元数据写入系统凭据库并**回读校验**。

    ``auth.login`` 走 ``reauthorized=True``：用户明确重新授权时，设备身份若已变化就以本次
    授权为准原子更新元数据；业务侧自动取票保持默认 ``False``，身份变化仍然拒绝旧凭据。
    """
    if not isinstance(auth_profile, idaas_profile.AuthProfile):
        auth_profile = idaas_profile.load_profile(auth_profile)
    pending = reauthorization_pending(auth_profile)
    if not isinstance(data, dict):
        raise _contract_error("取票成功响应缺少 data 对象")
    credit_token = data.get("creditToken")
    expire_time = data.get("expireTime")
    if not isinstance(credit_token, str) or not credit_token.strip():
        raise _contract_error("取票成功响应缺少 creditToken")
    if expire_time is None or not str(expire_time).strip():
        raise _contract_error("取票成功响应缺少 expireTime")
    claw_uid = credential_store.sdk_claw_uid(auth_profile)
    if claw_uid is None:
        raise _identity_error("取票成功，但本机设备身份副本缺失，请重新完成授权")
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    unit = _expire_time_unit()
    _assert_sane_ttl(expire_time, now, unit=unit)
    expires_at = _expiry_deadline(expire_time, now, unit=unit)
    if expires_at is None:
        raise _contract_error("expireTime 无法换算为票据到期时间")
    expires_at = min(expires_at, now + timedelta(seconds=TICKET_MAX_AGE_SECONDS))
    _assert_ttl(expires_at, now)
    _verify_and_record_identity(auth_profile, claw_uid, now, reauthorized=reauthorized or pending)
    ticket = {
        "creditToken": credit_token.strip(),
        "expireTime": str(expire_time).strip(),
        "expiresAt": _iso(expires_at),
        "clawUid": claw_uid,
        "acquiredAt": _iso(now),
    }
    ticket_store = store or CredentialTicketStore()
    saved = ticket_store.save(auth_profile, ticket)
    saved["clawUid"] = claw_uid
    if pending and isinstance(ticket_store, CredentialTicketStore):
        # 常规 load 在待完成期间拒绝票据复用；这里直接回读刚保存的新分区验证。
        reloaded = credential_store.load(auth_profile, credential_store.KIND_TICKET, fresh=True)
        if (_validated_ticket(auth_profile, reloaded) is None
                or reloaded.get("creditToken") != saved.get("creditToken")):
            raise _store_error("新票据写入后回读不一致，重新授权仍待完成")
    else:
        _verify_persisted_ticket(auth_profile, ticket_store, saved)
    if pending:
        # 同一 clawUid 也可能换账号，成功后清除旧目录缓存和待完成标记。
        credential_store.delete_many(
            auth_profile, (credential_store.KIND_PENDING_AUTH, credential_store.KIND_DEDICATED_ROOT))
    return saved


def _verify_persisted_ticket(auth_profile, store, saved):
    """写后回读：保存失败或回读不一致一律按本地安全存储不可用处理，不伪装成成功。"""
    try:
        reloaded = store.load(auth_profile)
    except Exception as err:
        raise _store_error("票据写入后回读失败: %s" % err.__class__.__name__)
    if not isinstance(reloaded, dict):
        raise _store_error("票据未能写入本机安全存储，本地授权状态不可用")
    if reloaded.get("profileRef") != auth_profile.profile_ref:
        raise _store_error("回读到的票据与当前 profile 不一致，已停止使用")
    if _fingerprint(reloaded.get("creditToken")) != _fingerprint(saved.get("creditToken")):
        raise _store_error("回读到的票据与本次取票结果不一致，已停止使用")
    return reloaded


def _verify_and_record_identity(auth_profile, claw_uid, now, *, reauthorized=False):
    """核对身份指纹并刷新来源元数据。

    业务侧自动取票（``reauthorized=False``）：指纹变化即拒绝，避免新旧身份混用。
    用户明确重新授权（``reauthorized=True``）：以本次授权为准更新元数据并清掉旧的专属根
    缓存；否则"重新授权仍被旧指纹拒绝"会形成无法自愈的循环。
    """
    fingerprint = _fingerprint(claw_uid)
    meta = _load_identity_meta(auth_profile)
    if isinstance(meta, dict):
        previous = meta.get("originFingerprint")
        if isinstance(previous, str) and previous and previous != fingerprint:
            if not reauthorized:
                raise _identity_error(
                    "检测到本机设备身份已变更（%s → %s），本地凭据需重新完成授权后使用"
                    % (previous, fingerprint)
                )
            idaas_common.diagnostic(
                "用户重新授权：设备身份指纹已更新（%s → %s）" % (previous, fingerprint)
            )
            credential_store.delete(auth_profile, credential_store.KIND_DEDICATED_ROOT)
    source_kind, origin_ref = _source_of(auth_profile)
    _save_identity_meta(auth_profile, source_kind, origin_ref, fingerprint, now)


def clear_cached_ticket(auth_profile, *, store=None):
    """清除票据、设备身份元数据、待授权入口、专属目录根与已保存 scopeIds 缓存。"""
    if not isinstance(auth_profile, idaas_profile.AuthProfile):
        auth_profile = idaas_profile.load_profile(auth_profile)
    auxiliary = (
        credential_store.KIND_CLAWUID_META,
        credential_store.KIND_PENDING_AUTH,
        credential_store.KIND_DEDICATED_ROOT,
        credential_store.KIND_SCOPE_IDS,
    )
    if store is None:
        removed = credential_store.delete_many(
            auth_profile,
            (credential_store.KIND_TICKET,) + auxiliary,
        )
        return credential_store.KIND_TICKET in removed
    cleared = store.clear(auth_profile)
    credential_store.delete_many(auth_profile, auxiliary)
    return cleared


# ---------------------------------------------------------------------------
# 待授权入口缓存：让 130088（会话尚未完成）复用同一次会话
# ---------------------------------------------------------------------------


# 重放窗口：窗口内回放同一次会话的入口，用户完成的授权才能被本机认领；超出窗口说明该
# 会话很可能已在服务端失效，改为重新下发入口（否则会在失效会话上永久打转）。
# 2026-09-20 实测：130088 表示"该 uuid 尚未完成授权"，此时服务端**不下发**入口，
# 因此不缓存入口就只能靠"作废会话 + 重开一次"来展示入口，那会把用户正在完成的授权丢掉。
PENDING_ENTRY_TTL_SECONDS = 1800


def remember_pending_entry(auth_profile, auth_page_url, challenge_ref, *, session_ref=None, now=None,
                           reauthorization=False):
    """保存本次下发的授权入口，供同一会话后续的 130088 重放（不落 uuid 明文）。

    ``session_ref`` 只以指纹形式入库：重放时要先确认缓存入口与 SDK 当前 uuid 同源，
    避免把上一次会话的入口展示给用户（那样的入口即使被完成也无法被本机认领）。
    """
    if not isinstance(auth_profile, idaas_profile.AuthProfile):
        auth_profile = idaas_profile.load_profile(auth_profile)
    reauthorization = reauthorization or reauthorization_pending(auth_profile)
    moment = _now_utc(now)
    normalized_url = _pending_text(auth_page_url, "授权入口地址")
    normalized_challenge = _pending_text(challenge_ref, "挑战引用")
    session_fingerprint = _fingerprint(session_ref) if session_ref else None

    # 同一待授权会话仍在重放窗口内时，保留第一次落盘的时间与过期时间。否则每次
    # auth login 都会因为时间戳变化重写整个 file-backend 状态文件。
    existing = load_pending_entry(auth_profile, now=moment)
    if existing is not None and (
        existing.get("authPageUrl") == normalized_url
        and existing.get("challengeRef") == normalized_challenge
        and existing.get("sessionFingerprint") == session_fingerprint
        and bool(existing.get("reauthorizationPending")) == reauthorization
    ):
        return existing
    document = {
        "profileRef": auth_profile.profile_ref,
        "skillId": auth_profile.skill_id,
        "authPageUrl": normalized_url,
        "challengeRef": normalized_challenge,
        "sessionFingerprint": session_fingerprint,
        "savedAt": _iso(moment),
        "expiresAt": _iso(moment + timedelta(seconds=PENDING_ENTRY_TTL_SECONDS)),
    }
    if reauthorization:
        document["reauthorizationPending"] = True
        credential_store.update_many(
            auth_profile, {credential_store.KIND_PENDING_AUTH: document},
            # 新会话已保存并校验，旧票据和旧协商范围在同一事务中失效。
            remove=(credential_store.KIND_TICKET, credential_store.KIND_SCOPE_IDS))
    else:
        credential_store.save(auth_profile, credential_store.KIND_PENDING_AUTH, document)
    return document


def reauthorization_pending(auth_profile):
    """持久业务标记独立于链接的30分钟重放期限，只在新票据验证成功后解除。"""
    document = credential_store.load(auth_profile, credential_store.KIND_PENDING_AUTH, fresh=True)
    return (isinstance(document, dict)
            and document.get("profileRef") == auth_profile.profile_ref
            and document.get("skillId") == auth_profile.skill_id
            and document.get("reauthorizationPending") is True)


def load_pending_entry(auth_profile, *, now=None):
    """读取仍可重放的授权入口；结构不符、归属不符或已超出重放窗口一律返回 None。"""
    if not isinstance(auth_profile, idaas_profile.AuthProfile):
        auth_profile = idaas_profile.load_profile(auth_profile)
    document = credential_store.load(auth_profile, credential_store.KIND_PENDING_AUTH)
    if not isinstance(document, dict):
        return None
    if document.get("profileRef") != auth_profile.profile_ref:
        return None
    if document.get("skillId") != auth_profile.skill_id:
        return None
    for field in ("authPageUrl", "challengeRef"):
        value = document.get(field)
        if not isinstance(value, str) or not value.strip():
            return None
    deadline = _parse_absolute_time(document.get("expiresAt"))
    if deadline is None:
        return None
    if _now_utc(now) >= deadline:
        idaas_common.diagnostic("本机待授权入口已超出重放窗口，改为重新下发授权入口")
        return None
    return document


def clear_pending_entry(auth_profile):
    """删除待授权入口缓存：授权完成、退出账号与重新下发入口后都不再重放旧入口。"""
    if not isinstance(auth_profile, idaas_profile.AuthProfile):
        auth_profile = idaas_profile.load_profile(auth_profile)
    return credential_store.delete(auth_profile, credential_store.KIND_PENDING_AUTH)


def _pending_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise _contract_error("待授权入口缺少%s" % label)
    return value.strip()


def _now_utc(now=None):
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _ticket_valid(ticket, now):
    if not isinstance(ticket, dict):
        return False
    for field in ("creditToken", "expireTime"):
        value = ticket.get(field)
        if not isinstance(value, str) or not value.strip():
            return False
    expires = _parse_absolute_time(ticket.get("expiresAt"))
    acquired = _parse_absolute_time(ticket.get("acquiredAt") or ticket.get("savedAt"))
    if expires is None or acquired is None:
        return False
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    if current.astimezone(timezone.utc) < acquired:
        return False
    expires = min(expires, acquired + timedelta(seconds=TICKET_MAX_AGE_SECONDS))
    return current.astimezone(timezone.utc) < expires - timedelta(seconds=REFRESH_MARGIN_SECONDS)


def _assert_ttl(expires_at, now):
    """有效期必须明显大于刷新余量，否则按上游契约不符处理。"""
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    margin = timedelta(seconds=REFRESH_MARGIN_SECONDS * MIN_TTL_FACTOR)
    if expires_at - current.astimezone(timezone.utc) <= margin:
        raise _contract_error("票据有效期不足以覆盖刷新余量，已停止使用该票据")


def _expiry_deadline(value, now, unit=None):
    text = str(value).strip()
    if not text:
        return None
    resolved = _expire_time_unit() if unit is None else unit
    if resolved not in EXPIRE_TIME_UNITS:
        raise _contract_error("expireTime 单位契约未登记: %s" % resolved)
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    if resolved == "absolute":
        return _parse_absolute_time(text)
    try:
        amount = float(text)
    except ValueError:
        return None
    if amount <= 0:
        return None
    try:
        if resolved == "second":
            return current.astimezone(timezone.utc) + timedelta(seconds=amount)
        return current.astimezone(timezone.utc) + timedelta(minutes=amount)
    except OverflowError:
        return None


def _expire_time_unit():
    """相对有效期单位：受控环境变量覆盖，未登记取值直接按契约不符拒绝。"""
    raw = os.environ.get(EXPIRE_TIME_UNIT_ENV)
    if raw is None or not str(raw).strip():
        return EXPIRE_TIME_UNIT
    value = str(raw).strip().lower()
    if value not in EXPIRE_TIME_UNITS:
        raise _contract_error(
            "发布配置 %s 取值未登记: %s" % (EXPIRE_TIME_UNIT_ENV, value)
        )
    return value


def _assert_sane_ttl(value, now, unit=None):
    """有效期上限校验：单位误解（如秒当成分钟）会放大 60 倍，此处必须显式失败。"""
    expires_at = _expiry_deadline(value, now, unit=unit)
    if expires_at is None:
        return
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    if expires_at - current.astimezone(timezone.utc) > timedelta(days=MAX_TTL_DAYS):
        raise _contract_error(
            "票据有效期超出可解释范围（> %d 天），请确认 expireTime 单位契约" % MAX_TTL_DAYS
        )


def _parse_absolute_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        moment = datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S"):
            try:
                moment = datetime.strptime(text, fmt)
                break
            except ValueError:
                moment = None
        if moment is None:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(moment):
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _contract_error(message):
    return idaas_common.make_error(result_module.UPSTREAM_CONTRACT_MISMATCH, message)


def _identity_error(message):
    return idaas_common.make_error(result_module.FAILED, message)


def _store_error(message):
    """本地安全存储不可用：必须显式失败，不得伪装成"已保存/已清理"。"""
    return idaas_common.make_error(result_module.SECURE_STORE_UNAVAILABLE, message)


__all__ = (
    "BusinessHeaderProvider",
    "CredentialTicketStore",
    "clear_cached_ticket",
    "clear_pending_entry",
    "legacy_cache_dir",
    "load_pending_entry",
    "persist_sdk_ticket",
    "remember_pending_entry",
    "reauthorization_pending",
)
