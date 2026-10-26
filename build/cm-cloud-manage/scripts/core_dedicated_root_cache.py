"""专属目录根的本地缓存。

缓存按 profile + skill 隔离，并额外绑定当前 clawUid 的不可逆摘要；落盘经统一存储层
（`credential_store`）按 profile 选择加密 JSON 文件或系统凭据库。真实目录 ID、ID 路径
和身份材料不进入普通文件、环境持久配置、CLI 输出或 operation ledger。
"""

import hashlib
from datetime import datetime, timedelta, timezone

from cmcloud_auth import credential_store


DEFAULT_TTL_SECONDS = 24 * 60 * 60


def identity_fingerprint(request_headers):
    claw_uid = None
    if isinstance(request_headers, dict):
        for key, value in request_headers.items():
            if isinstance(key, str) and key.lower() == "clawuid":
                claw_uid = value
                break
    if not isinstance(claw_uid, str) or not claw_uid.strip():
        return None
    return hashlib.sha256(claw_uid.strip().encode("utf-8")).hexdigest()


class CredentialDedicatedRootStore:
    """通过统一存储层按 profile 使用加密文件或系统凭据库；不提供明文降级。"""

    def __init__(self, *, ttl_seconds=DEFAULT_TTL_SECONDS):
        self.ttl_seconds = int(ttl_seconds)

    def load(self, auth_profile, fingerprint, now):
        document = credential_store.load(auth_profile, credential_store.KIND_DEDICATED_ROOT)
        if not _valid(document, auth_profile, fingerprint, now):
            return None
        return {
            "folderId": document["folderId"],
            "idPath": document.get("idPath"),
            "namePath": document["namePath"],
        }

    def save(self, auth_profile, fingerprint, data, now):
        current = _utc(now)
        document = {
            "agentCode": credential_store.agent_segment(auth_profile),
            "profileRef": auth_profile.profile_ref,
            "skillId": auth_profile.skill_id,
            "identityFingerprint": fingerprint,
            "folderId": data["folderId"],
            "idPath": data.get("idPath"),
            "namePath": data["namePath"],
            "cachedAt": _iso(current),
            "expiresAt": _iso(current + timedelta(seconds=self.ttl_seconds)),
        }
        credential_store.save(auth_profile, credential_store.KIND_DEDICATED_ROOT, document)

    def clear(self, auth_profile):
        return credential_store.delete(auth_profile, credential_store.KIND_DEDICATED_ROOT)


def _valid(document, auth_profile, fingerprint, now):
    if not isinstance(document, dict) or not fingerprint:
        return False
    if document.get("agentCode") != credential_store.agent_segment(auth_profile):
        return False
    if document.get("profileRef") != auth_profile.profile_ref:
        return False
    if document.get("skillId") != auth_profile.skill_id:
        return False
    if document.get("identityFingerprint") != fingerprint:
        return False
    for key in ("folderId", "namePath"):
        value = document.get(key)
        if not isinstance(value, str) or not value:
            return False
    id_path = document.get("idPath")
    if id_path is not None and (not isinstance(id_path, str) or not id_path):
        return False
    expires_at = _parse_time(document.get("expiresAt"))
    return expires_at is not None and _utc(now) < expires_at


def _parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _utc(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value):
    return _utc(value).replace(microsecond=0).isoformat()


__all__ = (
    "CredentialDedicatedRootStore",
    "DEFAULT_TTL_SECONDS",
    "identity_fingerprint",
)
