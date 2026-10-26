"""auth 域本地可信数据保管层：票据、设备身份元数据、待授权入口与专属目录根缓存的唯一存储后端。

设计边界：
- 后端由 profile 的 ``storage_backend`` 决定：``keychain`` 走系统凭据库（Windows 凭据管理器 /
  macOS Keychain / Linux Secret Service，与 SDK 的 keychain 槽位共用 service 名
  ``skill_auth_code``），``file`` 走 ``<auth_info_path>/cmcloud_state.json`` 与固定加密恢复副本；
- 两种后端都不提供明文：file 后端使用与 SDK 同格式的信封加密（v=2 / pbkdf2-sha256 /
  AES-256-GCM，见 ``secure_file``）；读到不可用时**按状态分流**（``absent`` 视为未登录，
  ``corrupt`` 说明本地文件损坏并把原文件留证备份，``fingerprint_mismatch`` 说明本机环境
  变化，``version_unsupported`` 说明格式不兼容），不再一律当作"没有凭据"；
- 后端不可用时一律 ``SECURE_STORE_UNAVAILABLE``，禁止明文降级（与
  ``idaas_profile.require_profile_storage`` 口径一致）；
- 唯一的明文落盘例外是 ``<auth_info_path>/fallback_clawUid.json``（SDK 兜底写入的
  ``deviceId``，不含凭据），登出/卸载时随本地凭据清理；不得对外声称「全部落盘内容均已加密」；
- 每个 ``agent + profile + skill`` 只使用一个物理钥匙串条目 / 一个逻辑状态（含恢复副本），四类自有状态
  作为 JSON bundle 内的逻辑分区；这能避免 macOS 为多个外观相同的条目反复弹授权框；
- 旧版 ``<agentCode>/<profileRef>/<skillId>/<kind>`` 条目在对应 kind 首次读取时按需迁移，
  SDK 自己的 ``<skillId>:*`` 槽位不在本模块迁移或改写范围内；
- 读到损坏状态文件时优先从同内容的加密副本恢复；两份均不可用时留证备份后
  按空状态重建。写入失败明确报错；指纹漂移与版本不兼容不冒充"登录过期"。
"""

import contextlib
import hashlib
import json
import os
import tempfile
import threading
import time

from cmcloud_core import result as result_module
from cmcloud_auth import idaas_common as ai
from cmcloud_auth import secure_file

SERVICE_NAME = "skill_auth_code"
SCHEMA_VERSION = "1.0"
BUNDLE_SCHEMA_VERSION = "2.0"
BUNDLE_ENTRY_NAME = "auth_state_bundle"

KIND_TICKET = "ticket"
KIND_CLAWUID_META = "clawuid_meta"
KIND_DEDICATED_ROOT = "dedicated_root"
KIND_PENDING_AUTH = "pending_auth"
# validate/desc 协商命中的 scopeIds（服务端要求的取票 scope）：保存后供后续取票使用，
# 平台登记再次变化时由 103082 协商路径重查并更新（见 auth_idaas_common）。
KIND_SCOPE_IDS = "scope_ids"

CLAW_UID_SLOT_SUFFIX = "clawUid"

FILE_BACKEND_CLAW_UID_FILENAME = "clawUid.json"

STATE_FILE_NAME = "cmcloud_state.json"
STATE_BACKUP_SUFFIX = ".backup"

STORE_UNAVAILABLE_MESSAGE = "系统凭据库不可用，已拒绝任何明文降级"
# file 后端的状态文件写入失败属于"目录不可写"（磁盘满 / 只读 / 权限不足），
# 不得复用凭据库的文案，否则排障被引向系统 Keychain。
STATE_DIR_UNAVAILABLE_MESSAGE = "本机状态目录不可写，已拒绝任何明文降级"

_BUNDLE_TRANSACTION_LOCK = threading.RLock()

# 跨进程状态文件锁的等待预算：多进程/多副本同时读写同一 profile 时串行等待，
# 超预算按本机存储故障明确报错（不抛裸 OSError，也不静默丢写）。
LOCK_RETRY_INTERVAL_SEC = 0.05
LOCK_WAIT_BUDGET_SEC = 30.0
LOCK_TIMEOUT_MESSAGE = "状态文件被其他进程占用，等待超时，请稍后重试"


def entry_key(auth_profile, kind):
    """旧版分 kind 条目名；仅用于兼容迁移与清理，不再作为新写入目标。"""
    return "%s/%s/%s/%s" % (
        agent_segment(auth_profile),
        auth_profile.profile_ref,
        auth_profile.skill_id,
        kind,
    )


def bundle_entry_key(auth_profile):
    """新版单条目 bundle 名：智能体 + profile + skill 三重隔离。"""
    return "%s/%s/%s/%s" % (
        agent_segment(auth_profile),
        auth_profile.profile_ref,
        auth_profile.skill_id,
        BUNDLE_ENTRY_NAME,
    )


def agent_segment(auth_profile):
    """条目/槽位里的智能体路径段；缺失时归兜底编码，保证不会退回跨智能体共用。"""
    agent = getattr(auth_profile, "agent_code", None)
    if isinstance(agent, str) and agent.strip():
        return agent.strip()
    from cmcloud_auth import agent_scope

    return agent_scope.FALLBACK_AGENT_CODE


def sdk_claw_uid_slot(skill_id):
    """SDK 设备身份副本的槽位名；本模块只读该槽位，写入仍由 SDK 在取票成功后完成。"""
    return "%s:%s" % (skill_id, CLAW_UID_SLOT_SUFFIX)


def state_file_path(auth_profile):
    """file 后端的状态文件：与 SDK 的 auth_info_ydrz.json 同目录、不同文件名。"""
    return os.path.join(auth_profile.auth_info_path, STATE_FILE_NAME)


def _uses_keychain(auth_profile):
    return bool(getattr(auth_profile, "uses_keychain", True))


def _read_state_file(auth_profile):
    path = state_file_path(auth_profile)
    document, state = secure_file.read_secure_bundle_state(path)
    if state in (secure_file.STATE_CORRUPT, secure_file.STATE_ABSENT):
        recovered, recovery_state = secure_file.read_secure_bundle_state(path + STATE_BACKUP_SUFFIX)
        if recovery_state in secure_file.STATE_READABLE:
            ai.diagnostic("状态文件不可用，已从固定加密副本恢复")
            document, state = recovered, recovery_state
        elif state == secure_file.STATE_CORRUPT:
            backup = _backup_corrupt_state(path)
            ai.diagnostic("状态文件损坏，已留证备份后按空状态重建（backup=%s）" % (backup or "无"))
            return _new_bundle(auth_profile)
    if state == secure_file.STATE_FINGERPRINT_MISMATCH:
        ai.diagnostic("状态文件与 SDK 当前设备指纹不符（机器标识变化或旧版指纹），按空状态重建，需重新授权")
        return _new_bundle(auth_profile)
    if state == secure_file.STATE_VERSION_UNSUPPORTED:
        ai.diagnostic("状态文件信封版本不被当前 CLI 支持，按空状态重建")
        return _new_bundle(auth_profile)
    if not isinstance(document, dict):
        return _new_bundle(auth_profile)
    if (
        document.get("schemaVersion") != BUNDLE_SCHEMA_VERSION
        or document.get("agentCode") != agent_segment(auth_profile)
        or document.get("profileRef") != auth_profile.profile_ref
        or document.get("skillId") != auth_profile.skill_id
        or not isinstance(document.get("entries"), dict)
    ):
        ai.diagnostic("状态文件结构或归属不符，按空状态重建")
        return _new_bundle(auth_profile)
    return document


def credential_state(auth_profile):
    """本机凭据文件的读取状态摘要（只读，不含任何凭据值）。

    取值见 ``secure_file.STATE_*``；keychain 后端不适用（返回 ``None``）。
    供 ``auth status`` 等只读入口区分"没登录 / 文件损坏 / 换了环境 / 版本不兼容"。
    """
    if _uses_keychain(auth_profile):
        return None
    path = state_file_path(auth_profile)
    _document, state = secure_file.read_secure_bundle_state(path)
    if state in (secure_file.STATE_CORRUPT, secure_file.STATE_ABSENT):
        _previous, previous_state = secure_file.read_secure_bundle_state(path + STATE_BACKUP_SUFFIX)
        if previous_state in secure_file.STATE_READABLE:
            state = previous_state
    return {
        "state": state,
        "backupFile": _latest_backup_name(path) if state == secure_file.STATE_CORRUPT else None,
    }


def _backup_corrupt_state(path):
    """把损坏的状态文件复制为 ``<path>.corrupt[.N]``（0600）留证；失败不阻断重建。"""
    for index in range(0, 10):
        target = path + ".corrupt" if index == 0 else "%s.corrupt.%d" % (path, index)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, secure_file.FILE_MODE)
        except FileExistsError:
            continue
        except OSError as err:
            ai.diagnostic("损坏状态文件备份失败: %s" % err.__class__.__name__)
            return None
        try:
            with os.fdopen(fd, "wb") as handle, open(path, "rb") as source:
                handle.write(source.read())
        except OSError as err:
            ai.diagnostic("损坏状态文件备份写入失败: %s" % err.__class__.__name__)
            return None
        return os.path.basename(target)
    return None


def _latest_backup_name(path):
    candidates = [path + ".corrupt"] + ["%s.corrupt.%d" % (path, i) for i in range(1, 10)]
    names = [os.path.basename(item) for item in candidates if os.path.exists(item)]
    return names[-1] if names else None


def _write_state_file(auth_profile, bundle):
    try:
        path = state_file_path(auth_profile)
        # 同一份新状态先写固定副本，再写主文件；授权撤销后副本不会复活旧票据。
        # 中断于任一写入时，总有另一份完整的加密状态可读。
        secure_file.write_secure_bundle(path + STATE_BACKUP_SUFFIX, bundle, atomic=False)
        secure_file.write_secure_bundle(path, bundle, atomic=False)
    except OSError as err:
        ai.diagnostic("状态文件写入失败: %s" % err.__class__.__name__)
        raise _store_error(STATE_DIR_UNAVAILABLE_MESSAGE)


def load(auth_profile, kind, *, fresh=False):
    """从单条目 bundle 读取逻辑分区；旧版条目按需迁移。"""
    if not _uses_keychain(auth_profile):
        with _bundle_transaction(auth_profile):
            bundle = _read_state_file(auth_profile)
            document = bundle["entries"].get(kind)
            return _decode_document(document, kind) if document is not None else None
    keyring = _backend()
    with _bundle_transaction(auth_profile):
        bundle = _read_bundle(keyring, auth_profile, fresh=fresh)
        document = bundle["entries"].get(kind)
        if document is not None:
            return _decode_document(document, kind)
        if kind in bundle["legacyResolved"]:
            return None
        legacy = _read_legacy(keyring, auth_profile, kind)
        bundle["legacyResolved"].append(kind)
        if legacy is not None:
            bundle["entries"][kind] = legacy
        _write_bundle(keyring, auth_profile, bundle)
        if legacy is not None:
            _delete_legacy_best_effort(keyring, auth_profile, kind)
        return legacy


def save(auth_profile, kind, document):
    """更新单条目 bundle 中的逻辑分区。

    file 后端的整个 bundle 落在同一文件，写入采用原子替换。先比较逻辑内容，
    避免相同状态也触发一次 ``os.replace``：在 WorkBuddy/macOS 中后者会被安全层
    归类为删除旧文件并要求额外确认。
    """
    payload = dict(document)
    payload["schemaVersion"] = SCHEMA_VERSION
    if not _uses_keychain(auth_profile):
        with _bundle_transaction(auth_profile):
            bundle = _read_state_file(auth_profile)
            if bundle["entries"].get(kind) == payload:
                return
            bundle["entries"][kind] = payload
            _write_state_file(auth_profile, bundle)
        return
    keyring = _backend()
    with _bundle_transaction(auth_profile):
        bundle = _read_bundle(keyring, auth_profile, fresh=True)
        legacy_was_resolved = kind in bundle["legacyResolved"]
        bundle["entries"][kind] = payload
        _mark_legacy_resolved(bundle, kind)
        _write_bundle(keyring, auth_profile, bundle)
        if not legacy_was_resolved:
            _delete_legacy_best_effort(keyring, auth_profile, kind)


def update_many(auth_profile, documents, *, remove=()):
    """一次 bundle 写入保存授权会话并作废旧票据，避免两次写入间状态不一致。"""
    payloads = {kind: dict(document, schemaVersion=SCHEMA_VERSION)
                for kind, document in documents.items()}
    removed = tuple(dict.fromkeys(remove))
    if set(payloads).intersection(removed):
        raise ValueError("同一状态分区不能同时保存和删除")
    with _bundle_transaction(auth_profile):
        keyring = None if not _uses_keychain(auth_profile) else _backend()
        bundle = (_read_state_file(auth_profile) if keyring is None
                  else _read_bundle(keyring, auth_profile, fresh=True))
        for kind in removed:
            bundle["entries"].pop(kind, None)
        bundle["entries"].update(payloads)
        if keyring is None:
            _write_state_file(auth_profile, bundle)
        else:
            for kind in (*payloads, *removed):
                _mark_legacy_resolved(bundle, kind)
            _write_bundle(keyring, auth_profile, bundle)
            for kind in (*payloads, *removed):
                _delete_legacy_best_effort(keyring, auth_profile, kind)


def delete(auth_profile, kind):
    """清除 bundle 中的逻辑分区；保留空容器以复用 macOS 条目 ACL。"""
    return bool(delete_many(auth_profile, (kind,)))


def delete_many(auth_profile, kinds):
    """一次读改写清除多个逻辑分区，避免逐项访问系统钥匙串。"""
    normalized = tuple(dict.fromkeys(str(kind) for kind in kinds if str(kind)))
    if not normalized:
        return ()
    if not _uses_keychain(auth_profile):
        with _bundle_transaction(auth_profile):
            bundle = _read_state_file(auth_profile)
            removed = [kind for kind in normalized if bundle["entries"].pop(kind, None) is not None]
            if removed:
                _write_state_file(auth_profile, bundle)
        return tuple(removed)
    keyring = _backend()
    with _bundle_transaction(auth_profile):
        bundle = _read_bundle(keyring, auth_profile, fresh=True)
        removed = []
        for kind in normalized:
            if bundle["entries"].pop(kind, None) is not None:
                removed.append(kind)
            _mark_legacy_resolved(bundle, kind)
        _write_bundle(keyring, auth_profile, bundle)
        for kind in normalized:
            if _delete_legacy(keyring, auth_profile, kind):
                if kind not in removed:
                    removed.append(kind)
        return tuple(removed)


def sdk_claw_uid(auth_profile):
    """读取 SDK 持有的设备身份副本：keychain 槽位，或 file 后端的 clawUid.json。"""
    if _uses_keychain(auth_profile):
        keyring = _backend()
        try:
            value = keyring.get_password(SERVICE_NAME, sdk_claw_uid_slot(auth_profile.skill_id))
        except Exception as err:
            ai.diagnostic("凭据库读取失败 (clawUid): %s" % err.__class__.__name__)
            raise _store_error()
        return _text(value)
    return _file_claw_uid(auth_profile)


def _file_claw_uid(auth_profile):
    """读 SDK 写在 ``clawUid.json`` 的设备身份副本；SDK v3.1.x 起该文件是信封加密。

    历史版本写过明文 JSON（未加密、未 base64），故信封读不到时回退到明文解析。
    """
    path = os.path.join(auth_profile.auth_info_path, FILE_BACKEND_CLAW_UID_FILENAME)
    document = secure_file.read_secure_bundle(path)
    if document is None:
        document = _read_legacy_plaintext(path)
    return _text(document.get("clawUid") if isinstance(document, dict) else None)


def _read_legacy_plaintext(path):
    """兼容读取旧版 SDK 的明文 JSON 文件；只为迁移期读取，不做任何写入。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _text(value):
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _decode(raw, kind):
    if raw is None:
        return None
    try:
        document = json.loads(raw)
    except (ValueError, TypeError):
        ai.diagnostic("凭据库条目无法解析，按不存在处理 (%s)" % kind)
        return None
    return _decode_document(document, kind)


def _decode_document(document, kind):
    if not isinstance(document, dict) or document.get("schemaVersion") != SCHEMA_VERSION:
        ai.diagnostic("凭据库条目结构不符，按不存在处理 (%s)" % kind)
        return None
    return dict(document)


def _new_bundle(auth_profile):
    return {
        "schemaVersion": BUNDLE_SCHEMA_VERSION,
        "agentCode": agent_segment(auth_profile),
        "profileRef": auth_profile.profile_ref,
        "skillId": auth_profile.skill_id,
        "entries": {},
        "legacyResolved": [],
    }


def _read_bundle(keyring, auth_profile, *, fresh=False):
    key = bundle_entry_key(auth_profile)
    if fresh:
        backend = keyring.get_keyring()
        invalidate = getattr(backend, "invalidate_password", None)
        if callable(invalidate):
            invalidate(SERVICE_NAME, key)
    try:
        raw = keyring.get_password(SERVICE_NAME, key)
    except Exception as err:
        ai.diagnostic("凭据库 bundle 读取失败: %s" % err.__class__.__name__)
        raise _store_error()
    if raw is None:
        return _new_bundle(auth_profile)
    try:
        bundle = json.loads(raw)
    except (ValueError, TypeError):
        ai.diagnostic("凭据库 bundle 无法解析，按空状态重建")
        return _new_bundle(auth_profile)
    expected = _new_bundle(auth_profile)
    if (
        not isinstance(bundle, dict)
        or bundle.get("schemaVersion") != BUNDLE_SCHEMA_VERSION
        or bundle.get("agentCode") != expected["agentCode"]
        or bundle.get("profileRef") != expected["profileRef"]
        or bundle.get("skillId") != expected["skillId"]
        or not isinstance(bundle.get("entries"), dict)
        or not isinstance(bundle.get("legacyResolved"), list)
    ):
        ai.diagnostic("凭据库 bundle 结构或归属不符，按空状态重建")
        return expected
    bundle["legacyResolved"] = list(
        dict.fromkeys(str(item) for item in bundle["legacyResolved"] if str(item))
    )
    return bundle


def _write_bundle(keyring, auth_profile, bundle):
    try:
        keyring.set_password(
            SERVICE_NAME,
            bundle_entry_key(auth_profile),
            json.dumps(bundle, ensure_ascii=False, sort_keys=True),
        )
    except Exception as err:
        ai.diagnostic("凭据库 bundle 写入失败: %s" % err.__class__.__name__)
        raise _store_error()


def _read_legacy(keyring, auth_profile, kind):
    try:
        raw = keyring.get_password(SERVICE_NAME, entry_key(auth_profile, kind))
    except Exception as err:
        ai.diagnostic("旧版凭据条目读取失败 (%s): %s" % (kind, err.__class__.__name__))
        raise _store_error()
    return _decode(raw, kind)


def _mark_legacy_resolved(bundle, kind):
    if kind not in bundle["legacyResolved"]:
        bundle["legacyResolved"].append(kind)


def _delete_legacy(keyring, auth_profile, kind):
    try:
        keyring.delete_password(SERVICE_NAME, entry_key(auth_profile, kind))
        return True
    except Exception as err:
        absent = getattr(getattr(keyring, "errors", None), "PasswordDeleteError", ())
        if isinstance(err, absent):
            return False
        ai.diagnostic("旧版凭据条目删除失败 (%s): %s" % (kind, err.__class__.__name__))
        raise _store_error()


def _delete_legacy_best_effort(keyring, auth_profile, kind):
    try:
        _delete_legacy(keyring, auth_profile, kind)
    except result_module.ExecutionError:
        # 新 bundle 已安全写入；旧副本仍位于系统凭据库中，不发生明文降级。
        # 后续显式退出会再次严格清理，当前读写不因兼容副本删除失败而伪报整体失败。
        ai.diagnostic("旧版凭据条目暂未清理 (%s)，后续退出时将再次处理" % kind)


@contextlib.contextmanager
def _bundle_transaction(auth_profile):
    """跨线程、跨进程串行化单条目 bundle 的读改写。

    Windows 的 ``msvcrt.locking(LK_LOCK)`` 只重试 10 次（约 10 秒）就抛 EDEADLOCK；
    并发稍多（多进程 / 多副本同时取票）就会以裸 OSError 失败。这里改为带预算的
    非阻塞自旋重试：超预算按本机存储故障明确报错，不抛裸异常、不静默丢写。
    """
    digest = hashlib.sha256(bundle_entry_key(auth_profile).encode("utf-8")).hexdigest()
    lock_root = os.path.join(tempfile.gettempdir(), "cmcloud-keyring-locks")
    os.makedirs(lock_root, exist_ok=True)
    lock_path = os.path.join(lock_root, "%s.lock" % digest)
    with _BUNDLE_TRANSACTION_LOCK, open(lock_path, "a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        _acquire_file_lock(handle, lock_path)
        try:
            yield
        finally:
            _release_file_lock(handle)


def _acquire_file_lock(handle, lock_path):
    """获取跨进程锁；等待超预算即按本机存储故障报错。"""
    deadline = time.monotonic() + LOCK_WAIT_BUDGET_SEC
    while True:
        try:
            _try_file_lock(handle)
            return
        except OSError:
            if time.monotonic() >= deadline:
                ai.diagnostic("状态文件锁等待超时: %s" % os.path.basename(lock_path))
                raise ai.make_error(
                    result_module.SECURE_STORE_UNAVAILABLE, LOCK_TIMEOUT_MESSAGE, next_action=None
                )
            time.sleep(LOCK_RETRY_INTERVAL_SEC)


def _try_file_lock(handle):
    # 锁定区间由当前文件位置决定：每次尝试前都必须回到 0，否则锁在 byte 1 而解锁在 byte 0。
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_file_lock(handle):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _backend():
    try:
        import keyring
    except ImportError:
        ai.diagnostic("keyring 未安装，凭据库不可用")
        raise _store_error()
    # 每次读写前做一次幂等自检：后端被替换时重新注入智能体隔离包装，
    # 避免条目悄悄落回不带命名空间的旧槽位。
    from cmcloud_auth import scoped_keyring

    scoped_keyring.ensure_installed()
    return keyring


def _store_error(message=STORE_UNAVAILABLE_MESSAGE):
    return ai.make_error(
        result_module.SECURE_STORE_UNAVAILABLE, message, next_action=None
    )


__all__ = (
    "BUNDLE_ENTRY_NAME",
    "KIND_CLAWUID_META",
    "KIND_DEDICATED_ROOT",
    "KIND_PENDING_AUTH",
    "KIND_SCOPE_IDS",
    "KIND_TICKET",
    "SERVICE_NAME",
    "agent_segment",
    "bundle_entry_key",
    "delete",
    "delete_many",
    "entry_key",
    "load",
    "save",
    "sdk_claw_uid",
    "sdk_claw_uid_slot",
    "update_many",
)
