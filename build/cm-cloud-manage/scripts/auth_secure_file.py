"""凭据文件信封加密读写：与 IDaaS SDK v3.1.x 的 file 后端格式一致。

格式（与 SDK 外部构建逐位对齐）：
- 文件内容 = base64(JSON) 单行；
- JSON 为信封 {"v":2,"kdf":"pbkdf2-sha256","iterations":600000,"salt","fp_hint","iv","ct","tag"}；
- 密钥 = PBKDF2-HMAC-SHA256(设备指纹, salt, 600000) → 32 字节；
- 设备指纹直接复用随包 SDK 3.1.1 的实现，不在外层复制或修正其规则；
- SDK 使用 machine-id|sys.platform；machine-id 不可用时回退 username|sys.platform；
- AES-256-GCM（16 字节 tag），任一位被改都解密失败。

安全等级：anti-casual-inspection，不是 anti-targeted-attack。
machine-id 或回退输入变化仍会解密失败；3.1.0 的旧指纹密文需要重新授权。
读侧把失败**分类**输出（见 STATE_*）：
``fingerprint_mismatch`` 由信封 ``fp_hint`` 与当前设备指纹比对得出，``corrupt`` 表示文件本身损坏，
``version_unsupported`` 表示信封版本/KDF 不认识；上层据此区分"没登录 / 凭据损坏 / 换了环境"，
不得一律当作"无凭据"。
"""

import base64
import binascii
import hashlib
import json
import os
from typing import Any, Dict, Optional

from Crypto.Cipher import AES as _AES

ENVELOPE_VERSION = 2
KDF_NAME = "pbkdf2-sha256"
PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16
IV_BYTES = 12
KEY_BYTES = 32
TAG_BYTES = 16
FP_HINT_HEX_CHARS = 8   # SDK 用 hexdigest()[:8]，是 8 个十六进制字符，不是 8 字节
DIR_MODE = 0o700
FILE_MODE = 0o600

# 读侧状态：区分"没文件 / 文件坏了 / 换了环境 / 版本不认 / 历史明文"，供上层分流话术。
STATE_OK = "ok"
STATE_ABSENT = "absent"
STATE_CORRUPT = "corrupt"
STATE_FINGERPRINT_MISMATCH = "fingerprint_mismatch"
STATE_VERSION_UNSUPPORTED = "version_unsupported"
STATE_LEGACY_PLAINTEXT = "legacy_plaintext"

# 只有这两种状态代表"读到了可用文档"。
STATE_READABLE = (STATE_OK, STATE_LEGACY_PLAINTEXT)

_KEY_CACHE: Dict[tuple, bytes] = {}
_KEY_CACHE_MAX = 16


def build_device_fingerprint() -> bytes:
    """只读调用随包 SDK 的指纹函数，保证 SDK 文件与外层状态使用同一密钥输入。

    SDK 没有公开指纹接口，因此将私有接口依赖集中在此适配点；不修改 SDK 的
    源码、全局配置或函数。延迟加载沿用既有导入隔离，避免 stdout 编码副作用。
    """
    from cmcloud_auth import idaas_common

    return idaas_common.load_token_service()._build_device_fingerprint()


def derive_key(fingerprint: bytes, salt: bytes) -> bytes:
    """PBKDF2-HMAC-SHA256 → 32 字节 AES-256 密钥；进程内缓存，避免每条命令重算。"""
    cache_key = (fingerprint, salt)
    cached = _KEY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    key = hashlib.pbkdf2_hmac(
        "sha256", fingerprint, salt, PBKDF2_ITERATIONS, dklen=KEY_BYTES
    )
    if len(_KEY_CACHE) >= _KEY_CACHE_MAX:
        _KEY_CACHE.clear()
    _KEY_CACHE[cache_key] = key
    return key


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _fingerprint_hint(fingerprint: bytes) -> str:
    return hashlib.sha256(fingerprint).hexdigest()[:FP_HINT_HEX_CHARS]


def encrypt_envelope(bundle: Dict[str, Any]) -> Dict[str, Any]:
    """明文 bundle → 信封 dict（每文件独立随机 salt / IV）。"""
    salt = os.urandom(SALT_BYTES)
    fingerprint = build_device_fingerprint()
    iv = os.urandom(IV_BYTES)
    plaintext = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    cipher = _AES.new(derive_key(fingerprint, salt), _AES.MODE_GCM, nonce=iv, mac_len=TAG_BYTES)
    ct, tag = cipher.encrypt_and_digest(plaintext)
    return {
        "v": ENVELOPE_VERSION,
        "kdf": KDF_NAME,
        "iterations": PBKDF2_ITERATIONS,
        "salt": _encode(salt),
        "fp_hint": _fingerprint_hint(fingerprint),
        "iv": _encode(iv),
        "ct": _encode(ct),
        "tag": _encode(tag),
    }


def decrypt_envelope_state(
    envelope: Dict[str, Any], *, fingerprint: Optional[bytes] = None
) -> tuple:
    """信封 → ``(明文 dict, 状态)``；状态见 ``STATE_*``。

    分类依据：版本/KDF 不符 → ``version_unsupported``；信封 ``fp_hint`` 与当前设备指纹
    不一致 → ``fingerprint_mismatch``（机器标识变化或旧指纹密文）；字段缺失、编码非法、GCM 校验失败、
    明文不是 JSON 对象 → ``corrupt``（文件被改坏）。
    """
    if not isinstance(envelope, dict):
        return None, STATE_CORRUPT
    if envelope.get("v") != ENVELOPE_VERSION or envelope.get("kdf") != KDF_NAME:
        return None, STATE_VERSION_UNSUPPORTED
    try:
        iterations = int(envelope["iterations"])
        salt = base64.b64decode(envelope["salt"], validate=True)
        iv = base64.b64decode(envelope["iv"], validate=True)
        ct = base64.b64decode(envelope["ct"], validate=True)
        tag = base64.b64decode(envelope["tag"], validate=True)
    except (KeyError, TypeError, ValueError, binascii.Error):
        return None, STATE_CORRUPT
    if iterations != PBKDF2_ITERATIONS or len(tag) != TAG_BYTES:
        return None, STATE_CORRUPT
    current = fingerprint or build_device_fingerprint()
    hint = envelope.get("fp_hint")
    if isinstance(hint, str) and hint and hint != _fingerprint_hint(current):
        return None, STATE_FINGERPRINT_MISMATCH
    try:
        cipher = _AES.new(derive_key(current, salt), _AES.MODE_GCM, nonce=iv, mac_len=TAG_BYTES)
        plaintext = cipher.decrypt_and_verify(ct, tag)
    except Exception:
        return None, STATE_CORRUPT
    try:
        document = json.loads(plaintext.decode("utf-8"))
    except Exception:
        return None, STATE_CORRUPT
    if not isinstance(document, dict):
        return None, STATE_CORRUPT
    return document, STATE_OK


def decrypt_envelope(envelope: Dict[str, Any], *, fingerprint: Optional[bytes] = None) -> Optional[Dict[str, Any]]:
    """兼容入口：只返回明文，非 ``ok`` 一律 ``None``（旧调用方语义不变）。"""
    document, _state = decrypt_envelope_state(envelope, fingerprint=fingerprint)
    return document


def write_secure_bundle(path: str, bundle: Dict[str, Any], *, atomic: bool = True) -> None:
    """加密写入；默认原子替换，受保护的双文件状态可选择原位写入。"""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True, mode=DIR_MODE)
        try:
            os.chmod(directory, DIR_MODE)
        except OSError:
            pass
    envelope = encrypt_envelope(bundle)
    payload = _encode(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if not atomic:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(path, FILE_MODE)
        except OSError:
            pass
        return
    tmp_path = path + ".tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, FILE_MODE)
    except OSError:
        pass


def read_secure_bundle_state(path: str) -> tuple:
    """读信封 → ``(文档, 状态)``；状态见 ``STATE_*``。

    ``absent``＝文件不存在或不可读；``corrupt``＝文件存在但为空、编码/明文损坏、GCM 校验失败；
    ``fingerprint_mismatch``＝信封 ``fp_hint`` 与 SDK 当前设备指纹不符；
    ``version_unsupported``＝信封版本或 KDF 不认识；``legacy_plaintext``＝非信封的历史明文 JSON。
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read().strip()
    except OSError:
        return None, STATE_ABSENT
    if not raw:
        return None, STATE_CORRUPT
    try:
        document = json.loads(base64.b64decode(raw, validate=True).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None, STATE_CORRUPT
    if not isinstance(document, dict):
        return None, STATE_CORRUPT
    if document.get("v") == ENVELOPE_VERSION and document.get("kdf") == KDF_NAME:
        return decrypt_envelope_state(document)
    return document, STATE_LEGACY_PLAINTEXT


def read_secure_bundle(path: str) -> Optional[Dict[str, Any]]:
    """兼容入口：只返回文档，读不到一律 ``None``（旧调用方语义不变）。

    需要区分"缺失/损坏/指纹漂移"的调用方请改用 :func:`read_secure_bundle_state`。
    """
    document, state = read_secure_bundle_state(path)
    return document if state in STATE_READABLE else None


__all__ = (
    "ENVELOPE_VERSION",
    "KDF_NAME",
    "PBKDF2_ITERATIONS",
    "STATE_OK",
    "STATE_ABSENT",
    "STATE_CORRUPT",
    "STATE_FINGERPRINT_MISMATCH",
    "STATE_VERSION_UNSUPPORTED",
    "STATE_LEGACY_PLAINTEXT",
    "build_device_fingerprint",
    "decrypt_envelope",
    "decrypt_envelope_state",
    "derive_key",
    "encrypt_envelope",
    "read_secure_bundle",
    "write_secure_bundle",
)
