"""
Token获取服务模块
提供getToken方法用于获取业务Token

依赖：
    pip install requests pycryptodome keyring

存储后端(storage_backend):
    - "file"    (默认): JSON 文件存储 <auth_info_path>/auth_info_ydrz.json
      (单文件布局，不按 skill 拆分)
    - "keychain":       使用操作系统原生 keychain(macOS Keychain /
      Windows Credential Manager / Linux Secret Service)，凭据按 skillId 隔离

      keychain 后端内部:
      - macOS / Linux: 单条 JSON 存整个 skill 的 bundle (clawUid/accessToken/uuid)
      - Windows:       Credential Manager 单条上限约 1280 字符,SDK 会自动分片
        (1024 字符/片) 绕过该限制,JWT 等长凭据可正常存取
      - 进程内缓存避免重复读取 keychain,稳态单次 getToken 触发 1 次 keyring get

文件后端加密 (file_encryption_enabled, v3.1.x 起默认 True):
    加密算法: AES-256-GCM(随机 IV)
    密钥派生: PBKDF2-HMAC-SHA256(600k 迭代) 从设备指纹派生
    设备指纹: OS machine-id + sys.platform [+ username fallback], sha256 后 32 字节
        - Linux  /etc/machine-id (dbus-machine-id 同文件)
        - macOS  IOPlatformUUID (硬件 UUID)
        - Windows HKLM\\..\\Cryptography\\MachineGuid (注册表机器 GUID)
    设计动机:WorkBuddy 等 Docker 沙箱客户端每次重建会重写 hostname/MAC,
      旧方案(hostname+MAC+username+platform)在沙箱重建后 fingerprint 漂移 → 解密失败 →
      触发 103001 重授权。改用 OS 自己管理的 machine-id 作为主锚点,容器继承宿主稳定。
    machine-id 不可用 → 降级 username + sys.platform(WARN 提示 fingerprint 强度不足)
    安全等级: anti-casual-inspection,非 anti-targeted-attack
        - 挡:grep 明文 / 同用户其他进程误读 / 简单合规扫描
        - 不挡:同 WorkBuddy 宿主的不同 user 同 sensor 容器指纹相同(由 FS 沙箱隔离兜底)
        - 熵:~80-128 bit (machine-id 32 hex + platform + username) 够挡 offline 爆破
    升级 anti-targeted-attack: 未来加用户口令派生 Argon2id(envelope v=2 路径预留)
    pycryptodome 不可用且加密启用 → 启动期 RuntimeError 硬失败(不静默回退到明文)
    pycryptodome 不可用且加密关闭 → 正常启动,明文 IO + WARN(合规不通过但 SDK 不崩)
    旧 plaintext 文件 → lazy migration: 第一次写入时自动升级为 envelope
    旧 envelope(hostname+MAC+username+platform 派生) → 不再兼容,触发 103001 一次性重授权

迁移行为:
    storage_backend="keychain" 时，若 keychain 中该 skill 槽为空，首次调用
    get_auth_info / save_auth_info 会自动把文件中的凭据迁到 keychain，
    来源为单文件 auth_info_ydrz.json。原文件保留不删，作只读兜底。
"""

import json
import hashlib
import time
import uuid
import os
import base64
import binascii
import requests
import subprocess
import sys
from typing import Dict, Any, List, Optional, Tuple

from device_info import collect_device_info, collect_agent_info
import device_info
import keychain_store

# 修复 Windows 默认 GBK 控制台乱码：让 stdout 按 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')  # type: ignore[attr-defined]
except (AttributeError, ValueError):
    pass

try:
    from Crypto.PublicKey import RSA
    from Crypto.Cipher import AES, PKCS1_OAEP
    from Crypto.Hash import SHA256
    _CRYPTO_AVAILABLE = True
    _CRYPTO_IMPORT_ERROR: Optional[str] = None
except ImportError as e:
    _CRYPTO_AVAILABLE = False
    _CRYPTO_IMPORT_ERROR = str(e)


# Token服务基础URL，需要根据实际部署环境配置
TOKEN_SERVICE_BASE_URL = "https://idaas.cmpassport.com"
# 接口版本号
INTERFACE_VERSION = "3.1"
# SDK版本号
SDK_VERSION = "IDaaS_AUT_KMS_v3.1.1"
# 配置下发接口路径
CONFIG_DELIVERY_PATH = "/keyManage/custody/configDelivery"
# preAuth接口路径
PRE_AUTH_PATH = "/keyManage/custody/preAuth"
# sdk解绑接口路径
UNBINDING_PATH = "/keyManage/custody/sdkUnbinding"
# 旧版单文件 auth_info 文件名（不区分 skill），仅用于一次性迁移读取
LEGACY_AUTH_INFO_FILENAME = "auth_info_ydrz.json"

# ======================== 文件后端 PBKDF2 加密参数（替代原 scrypt） ========================
# PBKDF2-HMAC-SHA256 600k 迭代是 OWASP 2023 推荐值,与原 scrypt N=2^17 同 CPU 量级 (~100-250ms)。
# 进程内 _KEY_CACHE 缓存后只在 cold-start 跑一次 (auth_info + clawUid ≈ 200-500ms)
PBKDF2_ITERATIONS = 600_000  # OWASP 2023 PBKDF2-HMAC-SHA256 推荐
PBKDF2_HASH = "sha256"
SCRYPT_N = 2 ** 17        # 旧 scrypt 工作因子(仅用于 envelope v=1 老文件校验,新写不再用)
SCRYPT_R = 8              # 旧 scrypt 块大小(同上)
SCRYPT_P = 1              # 旧 scrypt 并行度(同上)
SCRYPT_SALT_LEN = 16      # per-file 随机盐
SCRYPT_KEY_LEN = 32       # AES-256
SCRYPT_IV_LEN = 12        # GCM 推荐 IV 长度
SCRYPT_FILE_VERSION = 1   # 旧 envelope 版本号(v=1 scrypt, 新写见下 ENVELOPE_VERSION)
ENVELOPE_VERSION = 2      # 新 envelope 版本号: v=2 + kdf=pbkdf2-sha256
PBKDF2_IV_LEN = 12        # GCM IV(新路径)
PBKDF2_TAG_LEN = 16       # GCM tag 长度 128 bit(新路径)

# ★ 内嵌的业务公钥（preAuth 数字信封加密使用）
#   - 发布前由 configDelivery 接口抓取并固化
#   - fetch_config 不再读取远端下发的 publicKey，杜绝"远端替换公钥 → 伪造加密"攻击链
#   - 服务端轮换 RSA 密钥对时，需要发新版本 SDK 并更新此常量
EMBEDDED_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCJp+9qWjK4Z9luY/0musMaRV4x
Jyfsn3EQ1OQqgI+2ZaNsqbl5PZWlrp57WfirfZ4Y/9+xVmC2H4rE08Jo4hXjubCI
h3iJdZuVNtrlgYVZ7tyA8yELsbcaFg31fNRfWlp7tZRE5YKnO7Oe3ag2Tt3lHI9z
BC98i9bSXBD1u0TQrwIDAQAB
-----END PUBLIC KEY-----"""

# 启动期自检：内嵌公钥必须可解析，否则 SDK 带病运行没有意义
try:
    RSA.import_key(EMBEDDED_PUBLIC_KEY_PEM.encode("utf-8"))
except Exception as _e:
    raise RuntimeError(
        f"[token_service] 内嵌公钥解析失败，请检查 EMBEDDED_PUBLIC_KEY_PEM: {_e}"
    )


class Config:
    """配置类，用于管理token服务的可配置项"""

    def __init__(self):
        self.clawUid_path = None  # 由 configDelivery 注入
        self.fallback_clawUid_path = None  # 由 configDelivery 注入
        # ★ 公钥不再从 configDelivery 拉取，直接使用内嵌常量
        self.public_key = EMBEDDED_PUBLIC_KEY_PEM
        self.auth_info_path = None  # 调用方可覆盖，改变整个凭据目录
        self.log_enabled = False
        # 存储后端: "file"(默认) 或 "keychain"
        self.storage_backend = "file"
        # ★ 文件后端加密开关(v3.1.x 起默认 True,行为变更):
        #   True  → auth_info_ydrz.json / clawUid.json 走 scrypt+设备指纹+AES-GCM 加密
        #   False → 退回明文(仅当 pycryptodome 不可用且调用方显式 opt-out)
        self.file_encryption_enabled = True
        # 启动期硬检查:若加密启用但 pycryptodome 不可用 → RuntimeError
        # 不在 __init__ 抛(会被 import 期捕获),改成第一次访问时检查
        self._enc_checked = False

    def set(self, clawUid_path: str = None, auth_info_path: str = None,
            log_enabled: bool = None, fallback_clawUid_path: str = None,
            storage_backend: str = None,
            file_encryption_enabled: bool = None):
        """
        设置配置项（调用方优先级高于服务端下发；public_key 不开放给调用方）。

        Args:
            clawUid_path: clawUid本地存储的绝对路径（若已设置，优先于 configDelivery 下发值）
            auth_info_path: auth_info 存储目录（SDK 会在该目录下按 <skillId>.json 分文件存放；
                            未设置则使用代码文件所在目录）
            log_enabled: 日志开关
            fallback_clawUid_path: 兜底 clawUid 文件路径（若已设置，优先于 configDelivery 下发值）
            storage_backend: 存储后端，"file"(默认) 或 "keychain"
            file_encryption_enabled: 文件后端加密开关,默认 True;若 pycryptodome 不可用
                且保持 True 会在第一次访问凭据 IO 时 RuntimeError 硬失败
        """
        if clawUid_path is not None:
            self.clawUid_path = clawUid_path
        if auth_info_path is not None:
            self.auth_info_path = auth_info_path
        if log_enabled is not None:
            self.log_enabled = log_enabled
        if fallback_clawUid_path is not None:
            self.fallback_clawUid_path = fallback_clawUid_path
        if storage_backend is not None:
            if storage_backend not in ("keychain", "file"):
                raise ValueError(
                    f"storage_backend 必须是 'keychain' 或 'file'，收到: {storage_backend!r}"
                )
            self.storage_backend = storage_backend
        if file_encryption_enabled is not None:
            self.file_encryption_enabled = file_encryption_enabled
            # 重新触发检查(若之前已检查且通过,现在 opt-out 了需要撤销标记)
            self._enc_checked = False

    def get(self) -> Dict[str, Any]:
        """获取当前配置（public_key 仅作为只读 inspection，不可由调用方设置）"""
        return {
            "clawUid_path": self.clawUid_path,
            "fallback_clawUid_path": self.fallback_clawUid_path,
            "public_key": self.public_key,
            "auth_info_path": self.auth_info_path,
            "log_enabled": self.log_enabled,
            "storage_backend": self.storage_backend,
            "file_encryption_enabled": self.file_encryption_enabled
        }

    def validate_file_encryption(self) -> None:
        """
        启动期 / 配置变更后调用一次:验证 file_encryption_enabled=True 时 pycryptodome 可用。
        不通过 → RuntimeError 硬失败(不静默回退到明文)。
        """
        if self._enc_checked:
            return
        if self.file_encryption_enabled and not _CRYPTO_AVAILABLE:
            raise RuntimeError(
                f"file_encryption_enabled=True 但 pycryptodome 不可用: {_CRYPTO_IMPORT_ERROR}。"
                f"要么安装 pycryptodome,要么显式 setConfig(file_encryption_enabled=False) "
                f"(不推荐,审计不通过)"
            )
        self._enc_checked = True


# 全局配置实例
config = Config()


def _log(message: str):
    """内部日志函数"""
    if config.log_enabled:
        print(f"[token_service] {message}")


def _validate_file_encryption_or_raise() -> None:
    """
    文件后端加密启动期硬检查:file_encryption_enabled=True 时 pycryptodome 必须可用。
    不通过 → RuntimeError(不静默回退到明文)。在第一次凭据 IO 之前调用。
    """
    config.validate_file_encryption()


def setConfig(clawUid_path: str = None, auth_info_path: str = None,
              log_enabled: bool = None, fallback_clawUid_path: str = None,
              storage_backend: str = None,
              file_encryption_enabled: bool = None):
    """
    设置配置项（调用方优先级高于服务端下发；public_key 不允许由调用方设置）。

    Args:
        clawUid_path: clawUid本地存储的绝对路径
        auth_info_path: auth_info 存储目录（按 <skillId>.json 分文件存放）
        log_enabled: 日志开关
        fallback_clawUid_path: 兜底 clawUid 文件路径
        storage_backend: 存储后端，"file"(默认) 或 "keychain"
        file_encryption_enabled: 文件后端加密开关,默认 True
    """
    config.set(
        clawUid_path=clawUid_path,
        auth_info_path=auth_info_path,
        log_enabled=log_enabled,
        fallback_clawUid_path=fallback_clawUid_path,
        storage_backend=storage_backend,
        file_encryption_enabled=file_encryption_enabled,
    )


# HTTP状态码描述映射
HTTP_STATUS_DESC = {
    400: "请求错误",
    401: "未授权",
    403: "禁止访问",
    404: "资源未找到",
    500: "服务器内部错误",
    502: "网关错误",
    503: "服务不可用",
    504: "网关超时"
}


# ======================== 时间戳 / traceId ========================

def _make_timestamp() -> str:
    """生成 17 位时间戳：YYYYMMDDHHMMSS + 毫秒后 3 位"""
    return time.strftime("%Y%m%d%H%M%S") + str(int(time.time() * 1000))[-3:]


def _make_trace_id() -> str:
    """生成 traceId（UUID4）"""
    return str(uuid.uuid4())


# ======================== 配置下发接口 ========================

def fetch_config(client_id: str, trace_id: Optional[str] = None) -> Dict[str, Any]:
    """
    拉取路径配置（openClawPath / fallbackClawUidPath）。
    ★ 与旧版不同：不再从 data.publicKey 读取公钥，统一使用内嵌的 EMBEDDED_PUBLIC_KEY_PEM。
       这样彻底关闭"远端下发-替换公钥-伪造加密"的攻击链路。

    Args:
        client_id: 调用方传入的 clientId,放进 headers 与 preAuth 对齐。
        trace_id: 可选；传入则复用已有 traceId，与 preAuth 串联整个流程。

    Returns:
        成功: {"ok": True, "openClawPath": ..., "fallbackClawUidPath": ...}
        失败: {"ok": False, "resultCode": ..., "resultDesc": ...}
    """
    timestamp = _make_timestamp()
    if trace_id is None:
        trace_id = _make_trace_id()
    interface_version = "2.0"

    # 签名 sha256(traceId + timestamp + interfaceVersion)，输出 base64（不是 hex）
    sign = base64.b64encode(
        hashlib.sha256(
            (trace_id + timestamp + interface_version).encode("utf-8")
        ).digest()
    ).decode("utf-8")

    headers = {
        "clientId": client_id,
        "sdkVersion": SDK_VERSION,
        "interfaceVersion": interface_version,
        "timestamp": timestamp,
        "traceId": trace_id,
        "Content-Type": "application/json"
    }
    body = {"sign": sign}

    url = TOKEN_SERVICE_BASE_URL + CONFIG_DELIVERY_PATH

    _log(f"配置下发请求: POST {url}")

    try:
        response = requests.post(url, json=body, headers=headers, timeout=10)
    except requests.Timeout:
        return {"ok": False, "resultCode": "200006", "resultDesc": "配置下发接口网络超时"}
    except requests.RequestException as e:
        return {"ok": False, "resultCode": "200005", "resultDesc": f"配置下发接口网络异常: {e}"}

    _log(f"配置下发响应状态码: {response.status_code}")

    if response.status_code != 200:
        http_code = str(response.status_code)
        http_desc = HTTP_STATUS_DESC.get(int(http_code), "配置下发请求失败")
        return {"ok": False, "resultCode": http_code, "resultDesc": http_desc}

    try:
        result = response.json()
    except json.JSONDecodeError:
        return {"ok": False, "resultCode": "200007", "resultDesc": "配置下发响应数据解析异常"}

    result_code = str(result.get("resultCode"))
    data = result.get("data")

    if result_code != "103000" or not isinstance(data, dict):
        return {
            "ok": False,
            "resultCode": result_code,
            "resultDesc": result.get("desc", "") or "配置下发失败"
        }

    # ★ 修改：不再读取 data.publicKey，只取路径配置
    open_claw_path = data.get("openClawPath")
    fallback_path = data.get("fallbackClawUidPath")

    if not open_claw_path or not fallback_path:
        return {
            "ok": False,
            "resultCode": "200012",
            "resultDesc": "配置下发数据无效，缺少 openClawPath / fallbackClawUidPath"
        }

    # ★ 修改：不再写 config.public_key，公钥永远是内嵌的
    # clawUid_path / fallback_clawUid_path: 调用方 set 过就优先，没 set 才用服务端下发
    if not config.clawUid_path:
        config.clawUid_path = _normalize_storage_path(open_claw_path)
    if not config.fallback_clawUid_path:
        config.fallback_clawUid_path = _normalize_storage_path(fallback_path)

    _log(f"配置下发成功: clawUid_path={open_claw_path}, fallback_path={fallback_path}")

    return {
        "ok": True,
        "openClawPath": open_claw_path,
        "fallbackClawUidPath": fallback_path
    }


# ======================== 设备 ID 读取（含兜底） ========================

def get_openclaw_device_id() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    读取 clawUid（不带兜底）：
      1) 智能体识别源（device_info.detect_claw_uid）—— 适配多 agent
         （每次重新读，反映当前部署的智能体身份；不写本地文件）
      2) 主路径 config.clawUid_path —— 兼容旧逻辑（默认 ~/.openclaw/identity/device.json）

    函数名保留向后兼容，实际读取的是"任何智能体的 clawUid"。
    """
    # 1. 智能体识别源（每次重新读，适配多 agent 部署）
    agent_claw_uid = device_info.detect_claw_uid()
    if agent_claw_uid:
        _log(f"智能体识别源读取到 clawUid: {agent_claw_uid}")
        return agent_claw_uid, None, None

    # 2. 主路径（兼容旧逻辑）
    device_json_path = config.clawUid_path
    _log(f"clawUid路径: {device_json_path}")

    if not device_json_path or not os.path.exists(device_json_path):
        return None, "200009", "设备文件不存在"

    try:
        with open(device_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        device_id = data.get("deviceId")
        return device_id, None, None

    except Exception as e:
        return None, "200010", f"读取设备文件失败: {e}"


def _write_device_id_to_fallback(device_id: str) -> bool:
    """把 deviceId 写入兜底文件，成功返回 True"""
    fallback_path = config.fallback_clawUid_path
    if not fallback_path:
        return False
    try:
        dir_path = os.path.dirname(fallback_path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        with open(fallback_path, "w", encoding="utf-8") as f:
            json.dump({"deviceId": device_id}, f, ensure_ascii=False, indent=2)
        _log(f"已写入兜底设备文件: {fallback_path}")
        return True
    except Exception as e:
        _log(f"写入兜底设备文件失败: {e}")
        return False


def get_openclaw_device_id_with_fallback() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    读取 OpenCLAW 设备 ID，**优先读兜底文件**（**只首次生成 UUID**）：
      1) 兜底文件存在且含 deviceId → 直接返回（优先，不再访问主路径）
      2) 兜底文件不存在 或 读取失败 → 尝试主路径 get_openclaw_device_id
         （get_openclaw_device_id 内部：智能体识别源 → config.clawUid_path）
      3) 主路径读取成功 → 直接返回
      4) 主路径失败 且 兜底路径已配置 → 生成新 UUID 写入兜底文件并返回
      5) 主路径失败 且 兜底路径未配置 → 生成新 UUID 仅在内存中使用并返回
      6) 兜底写入失败 → 仍返回内存中的新 UUID（保证可用性）
    """
    # 1. 兜底文件已存在则优先读取（一旦生成就把兜底作为唯一来源，避免主路径覆盖/漂移）
    if config.fallback_clawUid_path and os.path.exists(config.fallback_clawUid_path):
        try:
            with open(config.fallback_clawUid_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            existing_id = existing.get("deviceId")
            if existing_id:
                _log(f"兜底文件已存在，优先读取兜底中的 deviceId: {existing_id}")
                return existing_id, None, None
        except Exception as e:
            _log(f"兜底文件存在但读取失败: {e}，回退到主路径")

    # 2. 兜底文件不存在/不可读，尝试主路径
    device_id, error_code, error_desc = get_openclaw_device_id()
    if device_id:
        return device_id, None, None

    _log(f"主路径读取设备ID失败({error_code}: {error_desc})")

    # 3. 主路径也失败，生成新 ID
    new_id = str(uuid.uuid4())
    _log(f"生成新 deviceId: {new_id}")

    if not config.fallback_clawUid_path:
        _log("兜底路径未配置，新生成的设备 ID 仅在内存中")
        return new_id, None, None

    if _write_device_id_to_fallback(new_id):
        return new_id, None, None

    # 写入失败，仍返回内存中的新 ID
    _log("兜底文件写入失败，新生成的设备 ID 仅在内存中")
    return new_id, None, None


# ======================== auth_info 存储：文件后端 ========================

# ======================== 文件后端加密: 设备指纹 + scrypt + AES-GCM ========================
# 安全等级: anti-casual-inspection,非 anti-targeted-attack
#  - 挡:grep 明文 / 同用户其他进程误读 / 简单合规扫描
#  - 不挡:拿到磁盘镜像 + 离线 GPU 爆破设备指纹(熵 ~30-40 bit, scrypt N=2^17 下数月)
# 设备指纹漂移(hostname / username / MAC 变化) → 解密失败 → 触发 103001 重授权(预期)
# pycryptodome 不可用且加密启用 → 启动期 RuntimeError 硬失败(不静默回退到明文)


def _get_machine_id() -> Optional[str]:
    """
    跨平台 OS machine-id(防沙箱/容器 hostname/MAC 漂移的稳定锚点)。

      - Linux  : /etc/machine-id (→ /var/lib/dbus/machine-id, dbus 同文件)
                 32 hex 字符串,OS 启动期生成,跨重启稳定,
                 容器/Docker 镜像 base image 里写入 → 容器继承宿主稳定。
      - macOS  : IOPlatformUUID via `ioreg -rd1 -c IOPlatformExpertDevice`
                 硬件 UUID,跨 OS 重装稳定。
      - Windows: HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid (注册表)
                 sysprep 才变,跨重启 / 跨升级稳定。

    返回归一化后的字符串(去首尾空白);失败 / 不支持 → 返回 None。
    """
    # Linux
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path, "r", encoding="utf-8") as fp:
                val = fp.read().strip()
            if val:
                return val
        except (OSError, IOError):
            continue

    # macOS: 解析 IOPlatformUUID
    if sys.platform == "darwin":
        try:
            out = subprocess.check_output(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                stderr=subprocess.DEVNULL,
                timeout=5,
                text=True,
            )
        except Exception:
            out = ""
        for line in out.splitlines():
            if "IOPlatformUUID" in line:
                parts = line.split('"')
                if len(parts) >= 2 and parts[1]:
                    return parts[1].strip()

    # Windows: 读注册表 MachineGuid
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
            )
            try:
                val, _ = winreg.QueryValueEx(key, "MachineGuid")
            finally:
                winreg.CloseKey(key)
            if val:
                return str(val).strip()
        except Exception:
            pass

    return None


def _get_username_for_fingerprint() -> str:
    """跨平台 username fallback (参考 device_info 风格)。"""
    try:
        u = os.getlogin()
        if u:
            return u
    except Exception:
        pass
    for var in ("USER", "USERNAME", "LOGNAME"):
        v = os.environ.get(var)
        if v:
            return v
    return ""


def _build_device_fingerprint() -> bytes:
    """
    拼接 machine_id + sys.platform → sha256 → 32 字节 fingerprint。

    设计动机:WorkBuddy 等 Docker 沙箱客户端每次重建会重写 hostname / MAC
    (实测 run1 vs run2:hostname `06ecd0489c42` → `70a77c1130c0`,MAC `06FC51C14910`
    → `06BE5A98370B`,DMI UUID 末段也跟 hostname 同源漂移)。
    旧方案(hostname+MAC+username+platform)漂移 → 解密失败 → 触发 103001 重授权。

    改用 OS 自己管理的 machine-id 作为主锚点:
      - Linux  : /etc/machine-id (容器继承宿主,稳定)
      - macOS  : IOPlatformUUID (硬件 UUID)
      - Windows: MachineGuid 注册表

    machine-id 不可用时降级到 username + sys.platform(熵弱,跨进程误读防护能力下降),
    降级路径 _log WARN 提示。
    """
    parts: List[bytes] = []
    machine_id = _get_machine_id()
    if machine_id:
        parts.append(machine_id.encode("utf-8"))
    else:
        # 降级:fingerprint 仅由 username + platform 构成
        username = _get_username_for_fingerprint()
        if username:
            parts.append(username.encode("utf-8"))
        _log(
            "fingerprint 主锚点 machine_id 不可用,"
            "降级到 username + sys.platform"
            "(熵弱,跨进程误读防护能力下降)"
        )
    parts.append(sys.platform.encode("utf-8"))
    return hashlib.sha256(b"|".join(parts)).digest()


# 进程内 (fingerprint, salt) → 派生 key 缓存
# 限制最大 16 条防止长跑进程泄漏;LRU 简化版(超限直接清空,下次按需重建)
_KEY_CACHE: Dict[Tuple[bytes, bytes], bytes] = {}
_KEY_CACHE_MAX = 16


def _derive_aes_key(fingerprint: bytes, salt: bytes) -> bytes:
    """
    PBKDF2-HMAC-SHA256(fingerprint, salt, iterations=600000, dklen=32) → 32 字节 AES-256 key。
    OWASP 2023 推荐 600k 迭代, 与原 scrypt N=2^17 同 CPU 量级 (~100-250ms)。
    进程内缓存避免每个 envelope 都重新派生。
    派生失败 → 抛 RuntimeError(不让静默回退到明文)。
    """
    cache_key = (fingerprint, salt)
    if cache_key in _KEY_CACHE:
        return _KEY_CACHE[cache_key]
    if not _CRYPTO_AVAILABLE:
        raise RuntimeError(
            f"无法派生 AES key: pycryptodome 不可用 ({_CRYPTO_IMPORT_ERROR})"
        )
    try:
        key = hashlib.pbkdf2_hmac(
            "sha256", fingerprint, salt,
            iterations=PBKDF2_ITERATIONS, dklen=SCRYPT_KEY_LEN,
        )
    except (MemoryError, ValueError, OSError) as e:
        raise RuntimeError(f"PBKDF2 派生失败: {type(e).__name__}: {e}") from e
    # 缓存上限保护
    if len(_KEY_CACHE) >= _KEY_CACHE_MAX:
        _KEY_CACHE.clear()
    _KEY_CACHE[cache_key] = key
    return key


def _aes_gcm_encrypt_with_iv(plaintext: bytes, key: bytes) -> Tuple[bytes, bytes, bytes]:
    """
    AES-256-GCM 加密(随机 IV,不复用 preAuth 的 iv = key[:12] 派生模式)。
    Returns: (iv, ciphertext, tag)
    """
    if not _CRYPTO_AVAILABLE:
        raise RuntimeError(
            f"无法 AES-GCM 加密: pycryptodome 不可用 ({_CRYPTO_IMPORT_ERROR})"
        )
    iv = os.urandom(PBKDF2_IV_LEN)
    cipher = AES.new(key, AES.MODE_GCM, nonce=iv, mac_len=PBKDF2_TAG_LEN)
    ct, tag = cipher.encrypt_and_digest(plaintext)
    return iv, ct, tag


def _aes_gcm_decrypt(ct: bytes, tag: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-256-GCM 解密。tag 校验失败抛 ValueError(由本函数映射为 RuntimeError 以兼容原语义)。"""
    if not _CRYPTO_AVAILABLE:
        raise RuntimeError(
            f"无法 AES-GCM 解密: pycryptodome 不可用 ({_CRYPTO_IMPORT_ERROR})"
        )
    cipher = AES.new(key, AES.MODE_GCM, nonce=iv, mac_len=PBKDF2_TAG_LEN)
    try:
        return cipher.decrypt_and_verify(ct, tag)
    except ValueError as e:
        # 映射为 RuntimeError 兼容原 cryptography InvalidTag 语义(调用方 catch Exception)
        raise RuntimeError(f"GCM tag 校验失败: {e}") from e


def _encrypt_envelope(bundle: Dict[str, Any]) -> Dict[str, Any]:
    """
    把 bundle(dict) 序列化为 JSON → AES-256-GCM(PBKDF2 派生 key) → envelope dict。
    envelope 包含 v / kdf / iterations / salt / fp_hint / iv / ct / tag 字段。
    """
    salt = os.urandom(SCRYPT_SALT_LEN)
    fp = _build_device_fingerprint()
    key = _derive_aes_key(fp, salt)
    plaintext = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    iv, ct, tag = _aes_gcm_encrypt_with_iv(plaintext, key)
    return {
        "v": ENVELOPE_VERSION,
        "kdf": "pbkdf2-sha256",
        "iterations": PBKDF2_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "fp_hint": hashlib.sha256(fp).hexdigest()[:8],
        "iv": base64.b64encode(iv).decode("ascii"),
        "ct": base64.b64encode(ct).decode("ascii"),
        "tag": base64.b64encode(tag).decode("ascii"),
    }


def _decrypt_envelope(envelope: Dict[str, Any], file_path: str) -> Optional[Dict[str, Any]]:
    """
    解密 envelope → 返回明文 bundle dict。
    失败(字段缺失 / base64 损坏 / GCM tag 校验失败 / JSON 解析失败)统一返回 None + 上层 WARN。
    fp_hint 不匹配但解密成功 → 仅日志提示,仍返回数据(避免误杀升级/迁移)。

    旧 envelope v=1 (kdf=scrypt) 不被识别 → 直接 None → 触发 103001 重授权
    """
    try:
        v = envelope.get("v")
        kdf = envelope.get("kdf")
        if v != ENVELOPE_VERSION or kdf != "pbkdf2-sha256":
            _log(f"envelope 版本/算法不匹配: v={v}, kdf={kdf} "
                 "(期望 v=2 + kdf=pbkdf2-sha256; 旧 v=1+scrypt 文件触发 103001 重授权)")
            return None
        iterations = int(envelope.get("iterations", 0))
        if iterations != PBKDF2_ITERATIONS:
            _log(f"envelope PBKDF2 iterations 变更: envelope={iterations}, "
                 f"current={PBKDF2_ITERATIONS}; 加密参数硬编码一致才能跨版本解密")
            return None
        salt = base64.b64decode(envelope["salt"])
        iv = base64.b64decode(envelope["iv"])
        ct = base64.b64decode(envelope["ct"])
        tag = base64.b64decode(envelope["tag"])
        fp_hint = envelope.get("fp_hint")
    except (KeyError, ValueError, TypeError, base64.binascii.Error) as e:
        _log(f"envelope 字段缺失/损坏: {e}")
        return None

    fp = _build_device_fingerprint()
    # fp_hint 不匹配仅 WARN,不阻断(防误杀:升级/迁移期间 fingerprint 可能暂时漂移)
    if fp_hint and hashlib.sha256(fp).hexdigest()[:8] != fp_hint:
        _log(f"envelope fp_hint 不匹配(可能设备指纹漂移): "
             f"file={file_path}, hint={fp_hint}")

    try:
        key = _derive_aes_key(fp, salt)
    except RuntimeError as e:
        _log(f"派生 AES key 失败: {e}")
        return None
    try:
        plaintext = _aes_gcm_decrypt(ct, tag, key, iv)
    except Exception as e:
        _log(f"envelope 解密失败(GCM tag 校验失败 / 设备指纹可能漂移): "
             f"{type(e).__name__}: {e}")
        return None
    try:
        return json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        _log(f"envelope 解密后 JSON 解析失败: {e}")
        return None


def _read_secure_bundle(file_path: str) -> Optional[Dict[str, Any]]:
    """
    统一读入口。文件格式固定为 base64(JSON dict),单行,无前缀。
    加密启用 → 解密 envelope;否则 plaintext。
    失败(文件不存在 / base64 损坏 / JSON 损坏 / 解密失败)统一返回 None + 上层 WARN。
    """
    if not os.path.exists(file_path):
        return None
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            raw_text = f.read().strip()
    except OSError as e:
        _log(f"读本地凭据失败: {file_path}, {e}")
        return None

    if not raw_text:
        return None

    try:
        json_text = base64.b64decode(raw_text).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as e:
        _log(f"base64 解码失败: {file_path}, {e}")
        return None
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        _log(f"base64 解码后 JSON 解析失败: {file_path}, {e}")
        return None

    if not isinstance(data, dict):
        _log(f"本地凭据格式异常(非 dict): {file_path}")
        return None

    # 检测 envelope(v=2 + kdf=pbkdf2-sha256)
    if data.get("v") == ENVELOPE_VERSION and data.get("kdf") == "pbkdf2-sha256":
        return _decrypt_envelope(data, file_path)
    # 否则视为 plaintext dict(加密关闭模式)
    return data


def _write_secure_bundle(file_path: str, bundle: Dict[str, Any]) -> None:
    """
    统一写入口。任何 bundle → JSON → base64 → 单行落盘(无前缀)。
    加密启用 → envelope dict 走 AES-GCM 加密后再编码;否则直接编码 plaintext。
    复用现有 _write_secure 做直接覆盖 + 0o700/0o600。

    base64 仅是编码(非加密),目的是让文件内容不显示 JSON 结构;真实加密由
    AES-GCM envelope 提供(仅在 file_encryption_enabled=True 时)。

    ★ 优化:在 _encrypt_envelope 之前算明文 hash,相同则跳过整个流程。
       加密模式下密文每次不同(随机 IV),但明文相同代表"用户感知一致" →
       根本不需要走加密 + 落盘 → 0 syscall、0 弹窗(解决 WorkBuddy 等 sandbox
         客户端每次保存触发的重复文件权限/删除弹窗)。
    """
    # 1. 在加密 / base64 之前算明文序列化(对齐 _encrypt_envelope 的输入格式)
    plain_json = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
    plain_hash = hashlib.sha256(plain_json.encode("utf-8")).hexdigest()

    if _LAST_PLAIN_HASH.get(file_path) == plain_hash:
        # 明文未变 → 不管密文怎么变都不重要,跳过整个写盘流程
        return

    # 2. 正常路径:加密 → base64 → 落盘
    if config.file_encryption_enabled:
        env = _encrypt_envelope(bundle)
    else:
        env = bundle
    json_str = json.dumps(env, ensure_ascii=False, separators=(",", ":"))
    payload = base64.b64encode(json_str.encode("utf-8")).decode("ascii")

    try:
        _write_secure(file_path, payload)
        _LAST_PLAIN_HASH[file_path] = plain_hash
    except Exception:
        # 写失败:清掉 hash entry,下次重试不要被旧 hash 短路掉
        _LAST_PLAIN_HASH.pop(file_path, None)
        raise


def _get_auth_info_base_dir() -> str:
    """auth_info 的存放目录：配置优先，否则用代码文件所在目录。"""
    if config.auth_info_path:
        return config.auth_info_path
    return os.path.dirname(os.path.abspath(__file__))


def get_auth_info_path(skill_id: str) -> str:
    """
    获取 auth_info 文件路径：固定为 <auth_info 目录>/auth_info_ydrz.json，
    沿用旧版单文件布局，不按 skill 拆分。
    """
    path = os.path.join(_get_auth_info_base_dir(), LEGACY_AUTH_INFO_FILENAME)
    _log(f"auth_info路径 (skill={skill_id}): {path}")
    return path


def _get_legacy_auth_info_path() -> str:
    """单文件 auth_info 路径（与 get_auth_info_path 一致）。"""
    return os.path.join(_get_auth_info_base_dir(), LEGACY_AUTH_INFO_FILENAME)


def _write_secure(file_path: str, data: Any) -> None:
    """
    直接覆盖写入 + 强制 0600 权限，避免敏感文件被同用户其他进程读取。

    凭据是敏感数据：目录 0o700、文件 0o600（POSIX 强制，Windows 由父目录 ACL 兜底）。

    ★ 改动(去掉 .tmp + os.replace):
      - 旧实现先写 <file>.tmp 再 os.replace,在 macOS TCC/sandbox 视角下
        会触发"创建文件"+"删除文件"两个独立权限弹窗(WorkBuddy 上每保存一次
        auth info 弹 2 次窗)。
      - 新实现直接 O_TRUNC 覆盖,只触发 1 个"创建/覆盖"弹窗。
      - 配合 _write_secure_bundle 的明文 hash 短路,相同 payload 直接 0 IO、0 弹窗。

    代价:写入中途进程崩溃可能留半截文件。auth_info < 4KB,半截概率极低;
    失败由 _write_secure_bundle 外层 try 捕获,调用方重试即可。

    Args:
        data: dict → JSON 序列化(indent=2);
              str  → 原样写(B64 envelope 单行格式);
              bytes → 原样写(二进制)。
    """
    dir_path = os.path.dirname(file_path)
    if dir_path:
        try:
            os.makedirs(dir_path, exist_ok=True, mode=0o700)
            os.chmod(dir_path, 0o700)   # 旧目录（可能是 0o755）也收紧
        except Exception:
            pass

    # 把 data 规范成 bytes,统一走 os.write 直写 fd
    if isinstance(data, bytes):
        payload_bytes = data
    elif isinstance(data, str):
        payload_bytes = data.encode("utf-8")
    elif isinstance(data, dict):
        payload_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    else:
        raise TypeError(f"_write_secure: unsupported type {type(data).__name__}")

    # 直接覆盖原文件:O_TRUNC 截断 → write → close。整个过程
    # 只触发 1 次 macOS TCC 文件操作(覆盖),不再产生"删除原文件"弹窗。
    fd = os.open(file_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload_bytes)
    finally:
        os.close(fd)

    try:
        os.chmod(file_path, 0o600)   # 旧文件（可能是 0o644）也收紧
    except Exception:
        pass


def _file_get_auth_info(clawUid: Optional[str], skill_id: str) -> Tuple[Optional[str], Optional[str]]:
    """
    文件后端读取 (uuid, accessToken)（单文件 auth_info_ydrz.json）。

    ★ 与旧版不同：不再做 clawUid 匹配校验。clawUid 已独立存到 clawUid.json，
      设备切换检测统一由 getToken 第 4-5 步处理（persisted_clawUid 比对）。

    ★ 走 _read_secure_bundle 自动处理 plaintext / envelope 两种格式。
    """
    _validate_file_encryption_or_raise()
    if not skill_id:
        return None, None

    data = _read_secure_bundle(get_auth_info_path(skill_id))
    if data is None:
        return None, None
    return data.get("uuid"), data.get("accessToken")


def _file_save_auth_info(access_token: Optional[str], auth_uuid: Optional[str],
                         clear_uuid: bool, clawUid: Optional[str],
                         skill_id: str) -> bool:
    """
    文件后端写入单文件 auth_info_ydrz.json（不按 skill 拆分）。

    ★ 与旧版不同：不再写入 clawUid 字段。clawUid 由 save_claw_uid() 写入
      独立的 clawUid.json，避免"会话/设备绑定凭据载体"模式下的串味风险。

    ★ 走 _write_secure_bundle 自动处理 plaintext / envelope 两种格式。
      加密启用时 lazy migration:旧 plaintext 文件第一次写入时升级为 envelope。
    """
    _validate_file_encryption_or_raise()
    if not skill_id:
        print("保存auth信息失败：缺少 skillId")
        return False

    auth_info_path = get_auth_info_path(skill_id)

    try:
        # 读取现有数据（文件损坏时当空处理，不因此丢掉本次写入）
        existing_data: Dict[str, Any] = _read_secure_bundle(auth_info_path) or {}

        if access_token is not None:
            existing_data["accessToken"] = access_token
        if auth_uuid is not None:
            existing_data["uuid"] = auth_uuid
        if clear_uuid or (auth_uuid == ""):
            existing_data.pop("uuid", None)

        _write_secure_bundle(auth_info_path, existing_data)
        return True
    except Exception as e:
        print(f"保存auth信息失败：{e}")
        return False


# ======================== clawUid 独立存储（文件后端） ========================

def get_clawUid_path() -> str:
    """获取 clawUid 文件路径。与 auth_info_ydrz.json 共享同一目录。"""
    return os.path.join(_get_auth_info_base_dir(), "clawUid.json")


def _file_get_claw_uid() -> Optional[str]:
    """从 clawUid.json 读取 clawUid（独立文件，不含任何 OAuth 凭据）。"""
    _validate_file_encryption_or_raise()
    data = _read_secure_bundle(get_clawUid_path())
    if data is None:
        return None
    return data.get("clawUid")


def _file_save_claw_uid(claw_uid: str, skip_if_same_as: Optional[str] = None) -> bool:
    """
    保存 clawUid 到 clawUid.json。

    Args:
        claw_uid: 要保存的值
        skip_if_same_as: 如果此值与 claw_uid 相同则跳过写入（避免重复磁盘 IO）
    """
    _validate_file_encryption_or_raise()
    if not claw_uid:
        return False
    if skip_if_same_as is not None and skip_if_same_as == claw_uid:
        return True  # 值未变，跳过
    clawUid_file = get_clawUid_path()
    try:
        _write_secure_bundle(clawUid_file, {"clawUid": claw_uid})
        _log(f"已保存 clawUid 到独立文件: {clawUid_file}")
        return True
    except Exception as e:
        print(f"保存clawUid失败：{e}")
        return False


# ======================== auth_info 存储：文件 -> keychain 迁移 ========================

# 按 skill_id 细粒度记录"已尝试过文件→keychain 迁移",避免每次 getToken 都跑迁移探测
_FILE_TO_KEYCHAIN_MIGRATED: set[str] = set()


# ======================== 文件写盘去重缓存 ========================
# 进程内缓存,记录每个文件路径上次成功写入的明文 bundle 序列化 sha256。
# _write_secure_bundle 在加密/base64 之前比对,命中则跳过整个写盘流程,
# 避免 WorkBuddy 等 macOS sandbox 客户端每次保存触发的重复文件权限/删除弹窗。
#
# 加密模式下密文每次都不同(随机 IV),所以这个缓存必须是**明文**层面 —
# 加密前比对"用户感知一致"的状态,而不是加密后的字节。
#
# 写入失败时清掉对应 entry(_write_secure_bundle 内处理),保证重试正确。
_LAST_PLAIN_HASH: Dict[str, str] = {}


def reset_write_cache() -> None:
    """清掉明文 hash 缓存（测试 / 外部强制重写时使用）。

    调用后,下一次 _write_secure_bundle 即使传入与上次完全相同的明文,
    也会重新走完整加密 + 落盘流程。

    参照 keychain_store.reset_cache 的命名风格(keychain_store.py:135)。
    """
    _LAST_PLAIN_HASH.clear()


def _read_migration_source(skill_id: str) -> Optional[Dict[str, Any]]:
    """
    读取单文件 auth_info_ydrz.json 作为迁移来源。

    文件后端统一只读写该文件，不再区分新/旧布局；只有当
    clawUid/accessToken/uuid 至少有一项非空时，才认为有值得迁移的数据。

    ★ 走 _read_secure_bundle 自动处理 plaintext / envelope 两种格式。
    """
    path = get_auth_info_path(skill_id)
    data = _read_secure_bundle(path)
    if data is None:
        return None
    if any(data.get(k) is not None for k in ("clawUid", "accessToken", "uuid")):
        _log(f"迁移来源: {path}")
        return data
    return None


def _maybe_migrate_file_to_keychain(skill_id: str) -> None:
    """
    keychain 中该 skill 槽为空且本地有文件凭据时，把文件内容搬进 keychain。

    只在 storage_backend="keychain" 时触发；per-skill 守护(避免重复探测)。
    走 keychain_store._load_bundle 单次探针替代旧版 3 次 get,稳态命中 cache 0 IO。
    原文件保留不删，作只读兜底。
    """
    if not skill_id or skill_id in _FILE_TO_KEYCHAIN_MIGRATED:
        return
    _FILE_TO_KEYCHAIN_MIGRATED.add(skill_id)

    # keychain 该 skill 已有 bundle -> 无需迁移
    try:
        bundle = keychain_store._load_bundle(skill_id)
        if bundle and any(v is not None for v in bundle.values()):
            return
    except RuntimeError:
        return

    data = _read_migration_source(skill_id)
    if data is None:
        _log(f"迁移跳过：本地无 skill {skill_id} 的文件凭据")
        return

    claw_uid = data.get("clawUid")
    access_token = data.get("accessToken")
    uuid_val = data.get("uuid")

    try:
        keychain_store.save_auth_bundle(
            claw_uid=claw_uid,
            skill_id=skill_id,
            access_token=access_token,
            auth_uuid=uuid_val,
            clear_uuid=uuid_val is None,
        )
        claw_log = claw_uid[:8] + "..." if claw_uid else "(无)"
        _log(f"已将文件凭据迁移到 keychain (skill: {skill_id}, clawUid 前缀: {claw_log})")
    except RuntimeError as e:
        _log(f"迁移失败：{e}")


# ======================== auth_info 存储：顶层分发 ========================

def get_auth_info(clawUid: Optional[str] = None, skillId: str = "") -> Tuple[Optional[str], Optional[str]]:
    """
    从当前存储后端读取指定 skill 的 (uuid, accessToken)。

    keychain 后端不可用时自动降级到文件后端，不让取 token 直接失败。
    """
    if not skillId:
        return None, None

    if config.storage_backend == "keychain":
        try:
            _maybe_migrate_file_to_keychain(skillId)
            return keychain_store.get_auth_bundle(clawUid, skillId)
        except RuntimeError as e:
            print(f"读取 keychain 失败，降级到文件：{e}")
            return _file_get_auth_info(clawUid, skillId)

    return _file_get_auth_info(clawUid, skillId)


def save_auth_info(access_token: str = None, auth_uuid: str = None,
                   clear_uuid: bool = False, clawUid: str = None,
                   skillId: str = "") -> bool:
    """保存指定 skill 的 auth 信息到当前存储后端。

    ★ 与旧版不同：clawUid 参数保留仅为兼容旧调用方；文件后端不再写入 clawUid 字段
      （由 _save_persisted_claw_uid 写入独立 clawUid.json）。keychain 后端保持现状。
    """
    if not skillId:
        print("保存auth信息失败：缺少 skillId")
        return False

    if config.storage_backend == "keychain":
        try:
            _maybe_migrate_file_to_keychain(skillId)
            return keychain_store.save_auth_bundle(
                claw_uid=clawUid,
                skill_id=skillId,
                access_token=access_token,
                auth_uuid=auth_uuid,
                clear_uuid=clear_uuid,
            )
        except RuntimeError as e:
            print(f"写入 keychain 失败，降级到文件：{e}")
            return _file_save_auth_info(access_token, auth_uuid, clear_uuid, clawUid, skillId)

    return _file_save_auth_info(access_token, auth_uuid, clear_uuid, clawUid, skillId)


# ======================== clawUid 持久化：顶层分发 ========================

def _get_persisted_claw_uid(skill_id: str) -> Optional[str]:
    """
    从当前存储后端读取持久化的 clawUid。

    - keychain: 走 keychain_store._load_bundle 拿整个 bundle 的 clawUid 字段;
      命中进程内缓存后 0 次 keychain get
    - file:     独立文件 clawUid.json（preAuth 成功后由 _save_persisted_claw_uid 写入）

    返回 None 表示未持久化或不可读（不抛异常给上层）。
    """
    if not skill_id:
        return None
    if config.storage_backend == "keychain":
        try:
            bundle = keychain_store._load_bundle(skill_id)
            if bundle:
                return bundle.get("clawUid")
        except RuntimeError:
            return None
        return None
    return _file_get_claw_uid()


def _save_persisted_claw_uid(claw_uid: str, skill_id: str,
                              skip_if_same_as: Optional[str] = None) -> bool:
    """
    保存 clawUid 到当前存储后端。

    skip_if_same_as: 若与已持久化值相同则跳过写入（避免重复磁盘 IO）。
    keychain 后端走 keychain_store.save_auth_bundle 复用 bundle 写入,cache 命中后 0 IO;
    且当 skip_if_same_as == claw_uid 时直接跳过(save_auth_info 已写过 clawUid 字段,无需重写)。
    """
    if not claw_uid or not skill_id:
        return False
    if config.storage_backend == "keychain":
        # 值未变:复用 save_auth_info 已写入的 clawUid 字段,跳过本次写
        if skip_if_same_as is not None and skip_if_same_as == claw_uid:
            return True
        try:
            keychain_store.save_auth_bundle(
                claw_uid=claw_uid,
                skill_id=skill_id,
                access_token=None,
                auth_uuid=None,
                clear_uuid=False,
            )
            return True
        except RuntimeError as e:
            print(f"写入 keychain 失败，降级到文件：{e}")
            return _file_save_claw_uid(claw_uid, skip_if_same_as)
    return _file_save_claw_uid(claw_uid, skip_if_same_as)


# ======================== 凭证清理入口 ========================

def revoke_access_token(skillId: str = "") -> bool:
    """
    主动撤销当前 accessToken：从 auth_info 中删除 accessToken 字段，但保留 uuid 供后续流程使用。
    调用方在确认 creditToken 用完、或者检测到异常时调用。

    keychain 后端走 keychain_store.revoke_access_token 显式 pop 语义,不再依赖
    save_auth_bundle(access_token="") 的空串 hack。
    """
    if not skillId:
        print("撤销accessToken失败：缺少 skillId")
        return False

    if config.storage_backend == "keychain":
        try:
            ok = keychain_store.revoke_access_token(skillId)
            if ok:
                _log(f"已撤销 accessToken (keychain, skill: {skillId})")
            return ok
        except RuntimeError as e:
            print(f"撤销 keychain accessToken 失败：{e}")
            return False

    # 文件后端：走 _read/_write_secure_bundle 自动处理 plaintext / envelope
    try:
        auth_info_path = get_auth_info_path(skillId)
        existing: Dict[str, Any] = _read_secure_bundle(auth_info_path) or {}
        existing.pop("accessToken", None)
        _write_secure_bundle(auth_info_path, existing)
        _log("已撤销 accessToken (file)")
        return True
    except Exception as e:
        print(f"revoke_access_token 失败: {e}")
        return False


def _clear_auth_info(skill_id: str) -> bool:
    """删除 auth_info_ydrz.json（用于 clawUid 变更后清掉旧设备的凭据）。"""
    auth_path = get_auth_info_path(skill_id)
    try:
        os.remove(auth_path)
        _log("已清理过期的 auth_info_ydrz.json")
        return True
    except FileNotFoundError:
        return False
    except Exception as e:
        _log(f"清理 auth_info 失败: {e}")
        return False


def clear_credentials(skillId: str = "") -> bool:
    """
    一键清理 SDK 自己创建的本地凭据。

    - keychain 后端：清理该 skill 的整个 bundle（含 Windows 分片）+ 从注册表移除
    - 文件后端：删除 auth_info_ydrz.json + clawUid.json

    不触碰 config.clawUid_path / config.fallback_clawUid_path（那是系统 OpenCLAW 路径，只读）。
    调用方在用户退出 / 设备解绑 / 卸载场景调用。
    """
    if config.storage_backend == "keychain":
        if not skillId:
            print("清理本地凭据失败：缺少 skillId")
            return False
        try:
            keychain_store.clear_all(skill_id=skillId)
            _log(f"已清理 keychain 本地凭据 (skill: {skillId})")
            return True
        except RuntimeError as e:
            print(f"清理 keychain 本地凭据失败：{e}")
            return False

    # 文件后端：两个独立文件
    targets = [
        get_auth_info_path(skillId),
        get_clawUid_path(),
    ]
    for p in targets:
        if not p:
            continue
        try:
            os.remove(p)
            _log(f"已清理本地凭据: {p}")
        except FileNotFoundError:
            pass
        except Exception as e:
            _log(f"清理失败 {p}: {e}")
    return True


# ======================== 数字信封加密 ========================

def _ensure_crypto() -> Optional[str]:
    """检查 pycryptodome 库可用,若不可用返回错误描述，否则返回 None"""
    if _CRYPTO_AVAILABLE:
        return None
    return f"加密库(pycryptodome)未安装，请执行 pip install pycryptodome。原始错误: {_CRYPTO_IMPORT_ERROR}"


def _normalize_storage_path(path: str) -> str:
    """
    跨平台路径归一化：所有路径都强制挂在用户主目录下。

    支持任意输入格式：
      - Unix 风格 /foo/bar          → ~/foo/bar
      - Windows 风格 C:\\foo\\bar   → ~/foo/bar
      - 相对路径 foo/bar            → ~/foo/bar
      - 已以 ~ 开头                  → expanduser 后原样

    这样无论服务端下发的是什么格式，SDK 在 Linux / macOS / Windows 上
    都会把设备文件落到当前用户的主目录下，避免跨平台路径不一致。
    """
    if not path:
        return path

    # 已经是 ~ 开头，做一次 expanduser 然后 normpath 统一分隔符
    if path.startswith("~"):
        return os.path.normpath(os.path.expanduser(path))

    # 去掉"绝对路径"前缀
    stripped = path
    if stripped.startswith("/"):
        # Unix 绝对路径
        stripped = stripped.lstrip("/")
    elif len(stripped) >= 2 and stripped[1] == ":":
        # Windows 盘符路径，如 C:\foo\bar 或 D:/foo/bar
        stripped = stripped[2:].lstrip("/\\")

    # 统一分隔符，并去掉两端多余的分隔符
    stripped = stripped.replace("\\", "/").strip("/")

    if not stripped:
        return os.path.expanduser("~")

    normalized = os.path.normpath(
        os.path.join(
            os.path.expanduser("~"),
            stripped.replace("/", os.sep),
        )
    )
    _log(f"路径归一化: {path} → {normalized}")
    return normalized


def _generate_aes_key() -> bytes:
    """生成 16 字节（128 位）随机 AES 密钥（对齐服务端 AES key 长度）"""
    return os.urandom(16)


def _aes_gcm_encrypt(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    """
    AES-GCM 加密。
    IV 必须由调用方提供（不再从 key 派生 → 服务端无法单向从前 12 字节推 IV）。
    Tag 长度 = 128 位 (16 字节)。
    返回 ciphertext || tag（**不含 IV**——IV 由 _envelope_encrypt 单独 RSA 加密后作为
    encryptedIV 透传给服务端,与 Java Cipher.doFinal 输出顺序一致）。
    """
    cipher = AES.new(key, AES.MODE_GCM, nonce=iv, mac_len=PBKDF2_TAG_LEN)
    ct, tag = cipher.encrypt_and_digest(plaintext)
    return ct + tag


def _rsa_oaep_encrypt(key: bytes, public_key_pem: str) -> bytes:
    """
    RSA-OAEP 加密（既用于加密 AES 密钥,也用于加密 IV）。
    Java 端填充: RSA/ECB/OAEPWithSHA-256AndMGF1Padding
    PyCryptodome PKCS1_OAEP 默认 hashAlgo=SHA256, MGF1=SHA256, label=b'' —— 与 Java 端
    (BouncyCastle / Conscrypt / 显式 OAEPParameterSpec("SHA-256","MGF1",SHA256)) 等价。
    ⚠️ 若后端用 Sun JCE 默认配置(MGF1=SHA1),此处需改 mgfunc=pss.MGF1(x,y,SHA1)。
    """
    pub = RSA.import_key(public_key_pem.encode("utf-8"))
    return PKCS1_OAEP.new(pub, hashAlgo=SHA256).encrypt(key)


def _envelope_encrypt(payload: Dict[str, Any], public_key_pem: str) -> Dict[str, str]:
    """
    数字信封加密:
      1) 生成 16/32 字节随机 AES 密钥(由 _generate_aes_key 决定)
      2) 生成 12 字节随机 IV（不再从 key 派生）
      3) AES-GCM 加密 payload 的 JSON 序列化结果
      4) RSA-OAEP-SHA256 加密 AES 密钥  → encrypted
      5) RSA-OAEP-SHA256 加密 IV         → encryptedIV
      6) 返回 {"reqdata": base64(密文+tag), "encrypted": base64(加密后的密钥), "encryptedIV": base64(加密后的IV)}
    """
    aes_key = _generate_aes_key()
    iv = os.urandom(12)  # 96 位,GCM 标准 nonce 长度
    plaintext = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    encrypted_data = _aes_gcm_encrypt(plaintext, aes_key, iv)
    encrypted_key = _rsa_oaep_encrypt(aes_key, public_key_pem)
    encrypted_iv = _rsa_oaep_encrypt(iv, public_key_pem)
    return {
        "reqdata": base64.b64encode(encrypted_data).decode("utf-8"),
        "encrypted": base64.b64encode(encrypted_key).decode("utf-8"),
        "encryptedIV": base64.b64encode(encrypted_iv).decode("utf-8"),
    }


# ======================== 主入口 ========================

def getToken(devId: str, clientId: str, skillId: str, skillName: str,
             strategyId: str, clawUid: str = None, templateId: str = None,
             uuid_param: str = None, scopeIds=None,
             forcedAuth: bool = False) -> Dict[str, Any]:
    """
    获取业务Token

    Args:
        devId: 开发者Id
        clientId: 业务方平台ID，用于标识调用方来源
        skillId: 技能ID，用于标识具体的技能服务
        skillName: 技能名称
        strategyId: 授权策略ID，必填
        clawUid: claw的唯一标识，不能为空
        templateId: 授权模板ID，可选，用于指定授权流程模板
        uuid_param: 授权流程标识，可选，若传入则优先使用传入的值
        scopeIds: 授权范围标识，可选，可以是字符串或字符串列表，多个值将用逗号拼接
        forcedAuth: 是否强制走授权流程，默认 False;True 表示强制重新授权,
                    即使本地有有效 token 缓存也绕过(透传给 preAuth.reqdata.forcedAuth)

    Returns:
        包含resultCode、resultDesc和data的字典
        - resultCode: 返回码，103000表示成功，103001表示需要用户授权，其他表示失败
        - resultDesc: 返回描述，对返回码的详细说明
        - data: token数据（成功时包含token信息，失败时为None）
    """
    # 1. 加密库可用性检查（最优先，避免后续走到一半才发现缺依赖）
    crypto_err = _ensure_crypto()
    if crypto_err:
        return {
            "resultCode": "200013",
            "resultDesc": crypto_err,
            "data": None
        }

    # 2. 参数校验
    if not devId or not isinstance(devId, str):
        return {"resultCode": "200001", "resultDesc": "devId不能为空", "data": None}
    if not clientId or not isinstance(clientId, str):
        return {"resultCode": "200002", "resultDesc": "clientId不能为空", "data": None}
    if not skillId or not isinstance(skillId, str):
        return {"resultCode": "200003", "resultDesc": "skillId不能为空", "data": None}
    if not skillName or not isinstance(skillName, str):
        return {"resultCode": "200004", "resultDesc": "skillName不能为空", "data": None}
    if not strategyId or not isinstance(strategyId, str):
        return {"resultCode": "200014", "resultDesc": "strategyId不能为空", "data": None}

    # 顶层生成 traceId，贯穿 configDelivery / preAuth 整个流程，便于服务端串联日志
    traceId = _make_trace_id()

    try:
        # 3. 配置下发（每次都重新拉取，不缓存）
        cfg = fetch_config(client_id=clientId, trace_id=traceId)
        if not cfg.get("ok"):
            return {
                "resultCode": cfg.get("resultCode", "200011"),
                "resultDesc": cfg.get("resultDesc", "配置下发失败"),
                "data": None
            }

        # 4. 解析 clawUid：调用方传入 / 持久化值 / fallback 三路收敛
        # 始终读 persisted 和 fallback 两个来源，对比检测智能体切换
        persisted_clawUid = _get_persisted_claw_uid(skillId)
        if not clawUid:
            # 调用方没传：走 fallback（智能体识别源 / 兜底文件 / 主路径）
            clawUid, error_code, error_desc = get_openclaw_device_id_with_fallback()
            if not clawUid:
                return {"resultCode": error_code, "resultDesc": error_desc, "data": None}

        # ★ clawUid 变更检测：实际值与持久化值不一致 → 跳过 auth_info 缓存（不删除文件）
        # 三路对比：调用方传入 / fallback 读出的真实值 vs 持久化的历史值
        # - 不一致：可能是 agent 切换，或调用方传了新值 → 跳过 auth_info 缓存
        claw_uid_changed = bool(persisted_clawUid) and persisted_clawUid != clawUid
        if claw_uid_changed:
            _log(f"检测到 clawUid 变更 ({persisted_clawUid} → {clawUid})，跳过 auth_info 缓存（不删除文件）")
        # ★ 不在这里写 clawUid：等 preAuth 成功（103000/103001）再写，
        #   保证持久化的 clawUid 一定是服务端验证过的

        # 5. 解析 uuid / accessToken：clawUid 变了就跳过缓存（不读 auth_info）
        if uuid_param:
            auth_uuid = uuid_param
            access_token = None
        elif claw_uid_changed:
            auth_uuid = None
            access_token = None
        else:
            auth_uuid, access_token = get_auth_info(clawUid, skillId)

        # 6. 生成请求头参数（traceId 已在函数顶部生成，与 configDelivery 共用）
        timestamp = _make_timestamp()

        # 7. 生成 SHA-256 签名 sign = Sha256(clientId + interfaceVersion + timestamp + traceId + clawUid + skillId + skillName + devId)，输出 base64
        sign_str = clientId + INTERFACE_VERSION + timestamp + traceId + clawUid + skillId + skillName + devId
        sign = base64.b64encode(hashlib.sha256(sign_str.encode('utf-8')).digest()).decode('utf-8')

        # 8. 明文 payload（仍包含 sign 作冗余校验）
        payload = {
            "clawUid": clawUid,
            "skillId": skillId,
            "skillName": skillName,
            "devId": devId,
            "strategyId": strategyId,
            "sign": sign
        }
        if templateId:
            payload["templateId"] = templateId
        # scopeIds：列表转逗号分隔字符串，其余按字符串处理
        if scopeIds is not None:
            if isinstance(scopeIds, list):
                scope_id_str = ",".join(str(s) for s in scopeIds)
            else:
                scope_id_str = str(scopeIds)
            if scope_id_str:
                payload["scopeIds"] = scope_id_str
        if auth_uuid:
            payload["uuid"] = auth_uuid
        if access_token:
            payload["accessToken"] = access_token
        # 设备风控参数（macAddress 已移除）
        payload.update(collect_device_info())
        # 智能体平台路径（SDK 部署路径；ClawX 时替换 openclaw→clawx）
        # 注意：智能体实例 ID 已作为 clawUid 通过 getToken 第 4 步走，
        #       不再单独作为 instanceId 上传（避免与服务端认证体系冲突）
        agent_info = collect_agent_info()
        payload["agentPlatformPath"] = agent_info["agentPlatformPath"]
        # 智能体平台 code（agentPlatform）：来自 _AGENT_RULES 的 code 字段。
        # 路径未命中时回退到环境变量识别（key/value 是否包含 keyword）；
        # 仍未命中则传空串，让服务端知道"未识别到具体智能体"。
        payload["agentPlatform"] = agent_info.get("agentPlatform", "")

        # 强制授权开关：调用方未传时默认 False；调用方显式传 True/False 都以传入值为准。
        # 始终写入 payload，保证服务端拿到的是「本次调用的真实意图」而不是「字段缺失」。
        payload["forcedAuth"] = bool(forcedAuth)

        # 9. 数字信封加密
        try:
            envelope = _envelope_encrypt(payload, config.public_key)
        except Exception as e:
            return {
                "resultCode": "200013",
                "resultDesc": f"数字信封加密失败: {e}",
                "data": None
            }

        # 10. 请求头
        headers = {
            "clientId": clientId,
            "timestamp": timestamp,
            "interfaceVersion": INTERFACE_VERSION,
            "sdkVersion": SDK_VERSION,
            "traceId": traceId,
            "encryptType": "SHA-256",
            "Content-Type": "application/json"
        }

        # 11. 调用 preAuth
        url = TOKEN_SERVICE_BASE_URL + PRE_AUTH_PATH

        _log(f"HTTP请求: POST {url}")
        _log(f"HTTP请求头: {json.dumps(headers, ensure_ascii=False)}")
        _log(f"HTTP请求体(reqdata): {envelope['reqdata']}")
        _log(f"HTTP请求体(encrypted): {envelope['encrypted']}")
        _log(f"HTTP请求体(encryptedIV): {envelope['encryptedIV']}")

        response = requests.post(url, json=envelope, headers=headers, timeout=10)

        _log(f"HTTP响应状态码: {response.status_code}")
        _log(f"HTTP响应体: {response.text[:500]}")

        if response.status_code == 200:
            try:
                result = response.json()
            except json.JSONDecodeError:
                return {
                    "resultCode": "200007",
                    "resultDesc": "数据解析异常",
                    "data": None
                }

            result_code = str(result.get("resultCode"))
            result_desc = result.get("desc", "")
            data_json = result.get("data")

            # 103000: 成功获取token
            if result_code == "103000":
                server_access_token = data_json.get("accessToken") if data_json else None
                if server_access_token:
                    if access_token != server_access_token:
                        save_auth_info(access_token=server_access_token, clear_uuid=True,
                                       clawUid=clawUid, skillId=skillId)
                # ★ 服务端验证通过后才写 clawUid；值未变就跳过
                _save_persisted_claw_uid(clawUid, skillId, skip_if_same_as=persisted_clawUid)
                return {
                    "resultCode": "103000",
                    "resultDesc": result_desc if result_desc else "成功",
                    "data": {
                        "creditToken": data_json.get("creditToken") if data_json else None,
                        "expireTime": data_json.get("expireTime") if data_json else None
                    }
                }
            # 103001: 需要用户授权
            elif result_code == "103001":
                auth_uuid_resp = data_json.get("uuid") if data_json else None
                if auth_uuid_resp:
                    save_auth_info(auth_uuid=auth_uuid_resp, access_token="",
                                   clawUid=clawUid, skillId=skillId)
                # ★ 服务端认可后才写 clawUid；值未变就跳过
                _save_persisted_claw_uid(clawUid, skillId, skip_if_same_as=persisted_clawUid)
                return {
                    "resultCode": "103001",
                    "resultDesc": result_desc if result_desc else "需要用户授权",
                    "data": {
                        "authPageUrl": data_json.get("authPageUrl") if data_json else None,
                        "uuid": auth_uuid_resp
                    }
                }
            # 其他错误码：直接透传服务端返回
            else:
                return {
                    "resultCode": result_code,
                    "resultDesc": result_desc,
                    "data": None
                }
        else:
            http_code = str(response.status_code)
            http_desc = HTTP_STATUS_DESC.get(int(http_code), "请求失败")
            return {
                "resultCode": http_code,
                "resultDesc": http_desc,
                "data": None
            }

    except requests.Timeout:
        return {"resultCode": "200006", "resultDesc": "网络超时", "data": None}
    except requests.RequestException:
        return {"resultCode": "200005", "resultDesc": "网络异常", "data": None}
    except json.JSONDecodeError:
        return {"resultCode": "200007", "resultDesc": "数据解析异常", "data": None}
    except Exception:
        return {"resultCode": "200008", "resultDesc": "系统异常", "data": None}


def unbinding(devId: str, clientId: str, skillId: str, strategyId: str,
              clawUid: str = None, scopeIds=None) -> Dict[str, Any]:
    """
    解绑登录的用户。

    调用 /keyManage/custody/sdk/unbinding 通知服务端解除当前 skill 下
    clawUid 对应的用户授权。accessToken 不由调用方传入，由 SDK 从本地
    auth_info 自动加载。该方法只解绑服务端授权关系，不动本地凭据。

    Args:
        devId: 开发者Id
        clientId: 业务方平台ID
        skillId: 技能ID
        strategyId: 策略id，必填
        clawUid: 平台唯一标识，可选；不传时从本地设备文件读取（含兜底）
        scopeIds: 授权范围标识，可选，可以是字符串或字符串列表

    Returns:
        包含 resultCode / resultDesc 的字典。
        - resultCode: 返回码（103000 表示成功；其余透传服务端码或本地错误码）
        - resultDesc: 返回描述
    """
    # 1. 加密库可用性检查
    crypto_err = _ensure_crypto()
    if crypto_err:
        return {"resultCode": "200013", "resultDesc": crypto_err}

    # 2. 参数校验
    if not devId or not isinstance(devId, str):
        return {"resultCode": "200001", "resultDesc": "devId不能为空"}
    if not clientId or not isinstance(clientId, str):
        return {"resultCode": "200002", "resultDesc": "clientId不能为空"}
    if not skillId or not isinstance(skillId, str):
        return {"resultCode": "200003", "resultDesc": "skillId不能为空"}
    if not strategyId or not isinstance(strategyId, str):
        return {"resultCode": "200014", "resultDesc": "strategyId不能为空"}

    # 顶层生成 traceId，贯穿 configDelivery / unbinding 整个流程
    traceId = _make_trace_id()

    try:
        # 3. 预检本地 accessToken：本地压根没有就直接返回，省掉一次 configDelivery 往返。
        #    调用方没传 clawUid 时这里以 None 读取（放宽设备匹配），比第 6 步带
        #    clawUid 的严格读取更宽松——此处读不到，严格读取同样读不到，不会误判。
        _, pre_access_token = get_auth_info(clawUid, skillId)
        if not pre_access_token:
            return {
                "resultCode": "200016",
                "resultDesc": "本地未找到可用 accessToken，请先调用 getToken 完成授权"
            }

        # 4. 配置下发（每次都重新拉取，不缓存）
        cfg = fetch_config(client_id=clientId, trace_id=traceId)
        if not cfg.get("ok"):
            return {
                "resultCode": cfg.get("resultCode", "200011"),
                "resultDesc": cfg.get("resultDesc", "配置下发失败")
            }

        # 5. 解析 clawUid
        if not clawUid:
            clawUid, error_code, error_desc = get_openclaw_device_id_with_fallback()
            if not clawUid:
                return {"resultCode": error_code, "resultDesc": error_desc}

        # 6. 带 clawUid 重新读取 accessToken，做设备切换校验（不向调用方暴露）
        _, accessToken = get_auth_info(clawUid, skillId)
        if not accessToken:
            return {
                "resultCode": "200016",
                "resultDesc": "本地未找到可用 accessToken，请先调用 getToken 完成授权"
            }

        # 7. 生成 timestamp
        timestamp = _make_timestamp()

        # 8. 签名 sign = Sha256(clientId + interfaceVersion + timestamp + traceId
        # + clawUid + skillId + devId + accessToken)，输出 base64
        sign_str = (
            clientId + INTERFACE_VERSION + timestamp + traceId
            + clawUid + skillId + devId + accessToken
        )
        sign = base64.b64encode(hashlib.sha256(sign_str.encode('utf-8')).digest()).decode('utf-8')

        # 9. 明文 payload（沿用 preAuth 同样的数字信封加密流程）
        payload = {
            "clawUid": clawUid,
            "skillId": skillId,
            "strategyId": strategyId,
            "devId": devId,
            "accessToken": accessToken,
            "sign": sign
        }
        # scopeIds: 列表转逗号分隔字符串
        if scopeIds is not None:
            if isinstance(scopeIds, list):
                scope_id_str = ",".join(str(s) for s in scopeIds)
            else:
                scope_id_str = str(scopeIds)
            if scope_id_str:
                payload["scopeIds"] = scope_id_str

        # 10. 数字信封加密（与 preAuth 完全一致）
        try:
            envelope = _envelope_encrypt(payload, config.public_key)
        except Exception as e:
            return {
                "resultCode": "200013",
                "resultDesc": f"数字信封加密失败: {e}"
            }

        # 11. 请求头
        headers = {
            "clientId": clientId,
            "timestamp": timestamp,
            "interfaceVersion": INTERFACE_VERSION,
            "sdkVersion": SDK_VERSION,
            "traceId": traceId,
            "encryptType": "SHA-256",
            "Content-Type": "application/json"
        }

        # 12. 数字信封加密后的请求体（与 preAuth 一致：顶层 reqdata + encrypted + encryptedIV）
        body = {
            "reqdata": envelope["reqdata"],
            "encrypted": envelope["encrypted"],
            "encryptedIV": envelope["encryptedIV"],
        }

        # 13. 调用 unbinding 接口
        url = TOKEN_SERVICE_BASE_URL + UNBINDING_PATH

        _log(f"解绑请求: POST {url}")
        _log(f"解绑请求头: {json.dumps(headers, ensure_ascii=False)}")
        _log(f"解绑请求体: {json.dumps(body, ensure_ascii=False)}")

        response = requests.post(url, json=body, headers=headers, timeout=10)

        _log(f"解绑响应状态码: {response.status_code}")
        _log(f"解绑响应体: {response.text[:500]}")

        if response.status_code == 200:
            try:
                result = response.json()
            except json.JSONDecodeError:
                return {
                    "resultCode": "200007",
                    "resultDesc": "数据解析异常"
                }

            result_code = str(result.get("resultCode"))
            result_desc = result.get("desc", "")

            # 解绑只通知服务端，不动本地凭据；返回码 / 描述透传，
            # 服务端响应不含 data 字段，SDK 不再读取也不返回 data。
            return {
                "resultCode": result_code,
                "resultDesc": result_desc
            }
        else:
            http_code = str(response.status_code)
            http_desc = HTTP_STATUS_DESC.get(int(http_code), "请求失败")
            return {
                "resultCode": http_code,
                "resultDesc": http_desc
            }

    except requests.Timeout:
        return {"resultCode": "200006", "resultDesc": "网络超时"}
    except requests.RequestException:
        return {"resultCode": "200005", "resultDesc": "网络异常"}
    except json.JSONDecodeError:
        return {"resultCode": "200007", "resultDesc": "数据解析异常"}
    except Exception:
        return {"resultCode": "200008", "resultDesc": "系统异常"}


if __name__ == "__main__":
    # demo 已移除：调用方应在 getToken(...) 中显式传入 devId/clientId/skillId/skillName。
    print("[token_service] demo 已移除，请在 getToken(...) 调用时显式传入 devId/clientId/skillId/skillName。")