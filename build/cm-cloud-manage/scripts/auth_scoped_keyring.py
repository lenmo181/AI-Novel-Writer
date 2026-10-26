"""SDK keychain 槽位的智能体命名空间包装后端（不修改 SDK）。

SDK 的 service 名（``skill_auth_code``）与槽位名（``<skillId>:clawUid`` 等）都写死在
SDK 内部，且 ``skillId`` 参与签名不能改；因此隔离落在唯一可影响的维度——username：
本模块用 keyring 的进程级后端注入点包一层，写入/读取/删除时把 username 改写为
``<agent>::<profile>::<原槽位>``，使同一台机器上的不同智能体不再共用同一批凭据。

约定：
- 命名空间未绑定时**透传**（引导期探测、SDK 自行读取设备身份等仍按旧行为工作）；
- ``ensure_installed()`` 幂等自检：后端被替换或被重初始化时重新注入并记脱敏诊断；
- 本模块不迁移旧槽位：把旧槽位归给某个智能体会让其它智能体读到同一份身份，
  与隔离目标冲突，旧槽位保留不用，各智能体首次使用各自重新授权。
"""

import base64
import binascii
import hashlib
import json
import os
import re
import threading
import uuid

import keyring

from cmcloud_auth import agent_scope

SEPARATOR = "::"
TEST_MEMORY_KEYRING_ENV = "CMCLOUD_TEST_MEMORY_KEYRING"

# Windows Credential Manager 的 CredentialBlob 按 UTF-16 保存，单条上限约 2560
# 字节。保留足够余量后，Windows 上超过 1800 字节的逻辑值走透明分片。
# macOS Keychain 与 Linux Secret Service 继续使用单条存储，以免增加钥匙串授权提示；
# 三个平台均能读取已存在的分片格式，便于凭据目录迁移或切换运行环境。
WINDOWS_CHUNKING_ENABLED = os.name == "nt"
DIRECT_VALUE_MAX_UTF16_BYTES = 1800
CHUNK_VALUE_MAX_CHARS = 800
CHUNK_MANIFEST_PREFIX = "cmcloud-keyring-chunks:v1:"
# 新 SDK keychain_store 在 Windows 上自带分片，清单形如 {"v":2,"chunks":N,"chunk_size":1024,"data":...}；
# 这类值不得再被本模块二次分片。
SDK_CHUNK_MANIFEST_PREFIX = '{"v":2,"chunks":'
CHUNK_USERNAME_PREFIX = "cmcloud-keyring-chunk"
MAX_CHUNK_COUNT = 4096

_GENERATION_PATTERN = re.compile(r"^[0-9a-f]{16}$")

_NAMESPACE = None
_installed = False
# 已注入的包装器实例（未注入时为 None）；bind() 清缓存时直接用它，不解析系统后端。
_INSTALLED_WRAPPER = None


def _diagnostic(message):
    from cmcloud_auth import idaas_common

    idaas_common.diagnostic(message)


def _backend_class():
    from keyring.backend import KeyringBackend

    return KeyringBackend


class MemoryKeyring(_backend_class()):
    """仅供测试的进程内后端；不会读写任何系统凭据库或明文文件。"""

    priority = 1.0

    def __init__(self):
        self._values = {}
        self._lock = threading.RLock()

    def get_password(self, service, username):
        with self._lock:
            return self._values.get((service, username))

    def set_password(self, service, username, password):
        with self._lock:
            self._values[(service, username)] = password

    def delete_password(self, service, username):
        from keyring.errors import PasswordDeleteError

        with self._lock:
            key = (service, username)
            if key not in self._values:
                raise PasswordDeleteError("No such password")
            del self._values[key]


class _LogicalCredential:
    """兼容 keyring credential 协议的最小对象。"""

    def __init__(self, username, password):
        self.username = username
        self.password = password


def _looks_like_sdk_manifest(password):
    return isinstance(password, str) and password.startswith(SDK_CHUNK_MANIFEST_PREFIX)


def _chunk_required(password):
    return (
        WINDOWS_CHUNKING_ENABLED
        and
        isinstance(password, str)
        and not _looks_like_sdk_manifest(password)
        and len(password.encode("utf-16-le")) > DIRECT_VALUE_MAX_UTF16_BYTES
    )


def _chunk_username(resolved_username, generation, index):
    identity = hashlib.sha256(resolved_username.encode("utf-8")).hexdigest()[:32]
    return "%s%s%s%s%s%s%04d" % (
        CHUNK_USERNAME_PREFIX,
        SEPARATOR,
        identity,
        SEPARATOR,
        generation,
        SEPARATOR,
        index,
    )


def _manifest_text(generation, count, digest, byte_length):
    document = {
        "count": count,
        "generation": generation,
        "sha256": digest,
        "utf8Bytes": byte_length,
    }
    return CHUNK_MANIFEST_PREFIX + json.dumps(
        document, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )


def _parse_manifest(value):
    """返回受控分片清单；普通历史值或伪清单按普通值处理。"""
    if not isinstance(value, str) or not value.startswith(CHUNK_MANIFEST_PREFIX):
        return None
    try:
        document = json.loads(value[len(CHUNK_MANIFEST_PREFIX) :])
    except (TypeError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    generation = document.get("generation")
    count = document.get("count")
    digest = document.get("sha256")
    byte_length = document.get("utf8Bytes")
    if not isinstance(generation, str) or _GENERATION_PATTERN.fullmatch(generation) is None:
        return None
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_CHUNK_COUNT:
        return None
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return None
    if (
        isinstance(byte_length, bool)
        or not isinstance(byte_length, int)
        or byte_length < 0
    ):
        return None
    return {
        "generation": generation,
        "count": count,
        "sha256": digest,
        "utf8Bytes": byte_length,
    }


def _keyring_error(message):
    try:
        from keyring.errors import KeyringError
    except (ImportError, AttributeError):
        return RuntimeError(message)
    return KeyringError(message)


def _delete_chunk_best_effort(inner, service, username):
    try:
        inner.delete_password(service, username)
    except Exception:
        # 清单已切换或删除后，旧分片不再可寻址。底层清理失败只做诊断，不能把
        # 已成功的逻辑写入/删除回滚成失败；后续同键更新仍会再次清理已知旧代。
        _diagnostic("系统凭据库旧分片清理失败，逻辑凭据已完成切换")


def _build_class():
    base = _backend_class()

    class ScopedKeyring(base):
        """命名空间隔离并透明分片超长值，再委托给系统原生凭据后端。"""

        priority = 0.0  # 不参与 keyring 的后端优先级解析，只经 set_keyring 注入

        def __init__(self, inner):
            self._inner = inner
            # keyring 的 macOS 后端按钥匙串条目做访问控制。SDK 在一次取票链路里会
            # 重复读取相同槽位；进程内缓存可避免同一 CLI 调用反复触发系统授权框。
            # None 也要缓存，否则“不存在”的 legacy 槽位仍会被重复探测。
            self._password_cache = {}
            self._cache_lock = threading.RLock()

        def _namespaced(self, username):
            namespace = _NAMESPACE
            if not namespace or not isinstance(username, str) or not username:
                return username
            if username.startswith(namespace + SEPARATOR):
                return username
            return "%s%s%s" % (namespace, SEPARATOR, username)

        def get_password(self, service, username):
            resolved = self._namespaced(username)
            cache_key = (service, resolved)
            with self._cache_lock:
                if cache_key in self._password_cache:
                    return self._password_cache[cache_key]
                # 持锁访问底层，保证同一进程的并发首次读取只触发一次系统调用。
                raw_value = self._inner.get_password(service, resolved)
                value = self._restore_value(service, resolved, raw_value)
                self._password_cache[cache_key] = value
                return value

        def set_password(self, service, username, password):
            resolved = self._namespaced(username)
            with self._cache_lock:
                # 只有 Windows 会写分片，也只有这里需要在更新前读取旧清单。
                # 避免 macOS/Linux 每次写入前多访问一次钥匙串并增加授权提示。
                previous_manifest = None
                if WINDOWS_CHUNKING_ENABLED:
                    previous = self._inner.get_password(service, resolved)
                    previous_manifest = _parse_manifest(previous)
                if _chunk_required(password):
                    result = self._set_chunked(service, resolved, password)
                else:
                    result = self._inner.set_password(service, resolved, password)
                if previous_manifest is not None:
                    self._delete_chunks(service, resolved, previous_manifest)
                self._password_cache[(service, resolved)] = password
                return result

        def delete_password(self, service, username):
            resolved = self._namespaced(username)
            with self._cache_lock:
                raw_value = self._inner.get_password(service, resolved)
                manifest = _parse_manifest(raw_value)
                try:
                    result = self._inner.delete_password(service, resolved)
                finally:
                    self._password_cache.pop((service, resolved), None)
                if manifest is not None:
                    self._delete_chunks(service, resolved, manifest)
                return result

        def _set_chunked(self, service, resolved, password):
            payload = password.encode("utf-8")
            encoded = base64.b64encode(payload).decode("ascii")
            chunks = [
                encoded[index : index + CHUNK_VALUE_MAX_CHARS]
                for index in range(0, len(encoded), CHUNK_VALUE_MAX_CHARS)
            ]
            generation = uuid.uuid4().hex[:16]
            written = []
            try:
                for index, chunk in enumerate(chunks):
                    chunk_username = _chunk_username(resolved, generation, index)
                    self._inner.set_password(service, chunk_username, chunk)
                    written.append(chunk_username)
                manifest = _manifest_text(
                    generation,
                    len(chunks),
                    hashlib.sha256(payload).hexdigest(),
                    len(payload),
                )
                return self._inner.set_password(service, resolved, manifest)
            except Exception:
                for chunk_username in written:
                    _delete_chunk_best_effort(self._inner, service, chunk_username)
                raise

        def _restore_value(self, service, resolved, raw_value):
            manifest = _parse_manifest(raw_value)
            if manifest is None:
                if isinstance(raw_value, str) and raw_value.startswith(CHUNK_MANIFEST_PREFIX):
                    raise _keyring_error("系统凭据库分片清单损坏，无法恢复逻辑凭据")
                return raw_value
            chunks = []
            for index in range(manifest["count"]):
                chunk_username = _chunk_username(
                    resolved, manifest["generation"], index
                )
                chunk = self._inner.get_password(service, chunk_username)
                if not isinstance(chunk, str):
                    raise _keyring_error("系统凭据库分片缺失，无法恢复逻辑凭据")
                chunks.append(chunk)
            try:
                payload = base64.b64decode("".join(chunks), validate=True)
                value = payload.decode("utf-8")
            except (binascii.Error, ValueError, UnicodeDecodeError) as err:
                raise _keyring_error("系统凭据库分片编码损坏，无法恢复逻辑凭据") from err
            if len(payload) != manifest["utf8Bytes"]:
                raise _keyring_error("系统凭据库分片长度不符，无法恢复逻辑凭据")
            if hashlib.sha256(payload).hexdigest() != manifest["sha256"]:
                raise _keyring_error("系统凭据库分片校验失败，无法恢复逻辑凭据")
            return value

        def _delete_chunks(self, service, resolved, manifest):
            for index in range(manifest["count"]):
                _delete_chunk_best_effort(
                    self._inner,
                    service,
                    _chunk_username(resolved, manifest["generation"], index),
                )

        def clear_cache(self):
            """清空本进程读缓存；测试或外部凭据变更后可显式调用。"""
            with self._cache_lock:
                self._password_cache.clear()

        def invalidate_password(self, service, username):
            """仅失效一个逻辑条目的缓存，供跨进程读改写前刷新。"""
            resolved = self._namespaced(username)
            with self._cache_lock:
                self._password_cache.pop((service, resolved), None)

        def get_credential(self, service, username):
            inner = getattr(self._inner, "get_credential", None)
            if inner is None:
                return super().get_credential(service, self._namespaced(username))
            resolved = self._namespaced(username)
            credential = inner(service, resolved)
            if credential is None:
                return None
            actual_username = getattr(credential, "username", resolved)
            raw_password = getattr(credential, "password", None)
            if not (
                isinstance(raw_password, str)
                and raw_password.startswith(CHUNK_MANIFEST_PREFIX)
            ):
                return credential
            password = self._restore_value(service, actual_username, raw_password)
            return _LogicalCredential(actual_username, password)

        def __getattr__(self, name):
            if name.startswith("_"):
                raise AttributeError(name)
            return getattr(self._inner, name)

        def __repr__(self):
            return "ScopedKeyring(%r)" % (self._inner,)

    return ScopedKeyring


ScopedKeyring = _build_class()


def bind(namespace):
    """绑定命名空间；None/空串表示透传。命名空间只在进程内生效。

    不解析系统后端：命名空间切换时只需清掉**已注入包装器**的缓存，直接持有实例即可，
    因此 file 后端（从不注入包装器）调用它不会触碰系统凭据库。
    """
    global _NAMESPACE
    resolved = namespace if isinstance(namespace, str) and namespace.strip() else None
    if resolved != _NAMESPACE and _INSTALLED_WRAPPER is not None:
        _INSTALLED_WRAPPER.clear_cache()
    _NAMESPACE = resolved
    return _NAMESPACE


def current_namespace():
    return _NAMESPACE


def is_installed():
    """当前进程的 keyring 后端是否已是本模块的包装后端。"""
    current = keyring.get_keyring()
    if not isinstance(current, ScopedKeyring):
        return False
    if _test_memory_requested():
        return isinstance(current._inner, MemoryKeyring)
    return True


def install(*, force=False):
    """注入包装后端；已在位时不重复包装（避免多层前缀）。"""
    global _installed, _INSTALLED_WRAPPER
    current = keyring.get_keyring()
    if _test_memory_requested() and not (
        isinstance(current, ScopedKeyring) and isinstance(current._inner, MemoryKeyring)
    ):
        current = MemoryKeyring()
        keyring.set_keyring(current)
        _diagnostic("测试隔离已启用：使用进程内凭据库，不访问系统 Keychain")
    if isinstance(current, ScopedKeyring):
        if force:
            keyring.set_keyring(current)
        _installed = True
        _INSTALLED_WRAPPER = current
        return current
    wrapper = ScopedKeyring(current)
    keyring.set_keyring(wrapper)
    _installed = True
    _INSTALLED_WRAPPER = wrapper
    _diagnostic("已注入按智能体隔离的凭据库后端（底层 %s）" % current.__class__.__name__)
    return keyring.get_keyring()


def ensure_installed():
    """幂等自检：后端被替换时重新注入，并把替换事件记入脱敏诊断。"""
    current = keyring.get_keyring()
    if isinstance(current, ScopedKeyring) and (
        not _test_memory_requested() or isinstance(current._inner, MemoryKeyring)
    ):
        return True
    _diagnostic("凭据库后端已被替换为 %s，重新注入智能体隔离后端" % current.__class__.__name__)
    install(force=True)
    return True


def _test_memory_requested():
    return os.environ.get(TEST_MEMORY_KEYRING_ENV) == "1"


__all__ = (
    "CHUNK_MANIFEST_PREFIX",
    "CHUNK_VALUE_MAX_CHARS",
    "DIRECT_VALUE_MAX_UTF16_BYTES",
    "MemoryKeyring",
    "SEPARATOR",
    "ScopedKeyring",
    "TEST_MEMORY_KEYRING_ENV",
    "bind",
    "current_namespace",
    "ensure_installed",
    "install",
    "is_installed",
)
