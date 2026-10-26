"""
Keychain 凭据存储封装(per-skill 命名空间)。

每个 skill 的凭据(clawUid / accessToken / uuid)打包成单一 JSON 条目,
进程内缓存避免重复读取 keychain。

存储布局:
  service = "skill_auth_code"

  macOS / Linux (单条 JSON):
    username = "<skill_id>"     -> JSON {"clawUid":"...","accessToken":"...","uuid":"..."}
    username = "_skills"        -> JSON ["skill_a", "skill_b", ...]

  Windows (分片,绕开 Credential Manager 1280 字符单条上限):
    username = "<skill_id>"     -> JSON manifest: {"v":2,"chunks":N,"chunk_size":1024,"data":"<chunk_0>"}
    username = "<skill_id>#1"   -> 第二片 (仅 chunks>=2 时存在)
    username = "<skill_id>#2"   -> 第三片 ...
    分片大小 = 1024 字符(留 25% 安全边际,应付 keyring 驱动差异)

keyring 自动路由:
- macOS:   Keychain Access
- Windows: Credential Manager (Generic Credentials)
- Linux:   Secret Service API

性能:
- 进程内缓存 _BUNDLE_CACHE / _SKILLS_CACHE 避免重复 keychain 访问
- 同进程内单次 getToken 在稳态触发 1 次 keyring get (取 clawUid) +
  可能的 1 次 keyring set (写 bundle)
- 环境变量 KMS_KEYCHAIN_DEBUG=1 启用 IO 计数器,通过 get_io_stats() 查看
"""

import json
import os
import sys
from typing import Dict, List, Optional, Tuple

try:
    import keyring
    from keyring.errors import KeyringError, PasswordDeleteError
    _KEYRING_AVAILABLE = True
except ImportError:  # pragma: no cover
    keyring = None  # type: ignore
    KeyringError = Exception  # type: ignore
    PasswordDeleteError = Exception  # type: ignore
    _KEYRING_AVAILABLE = False


SERVICE_NAME = "skill_auth_code"

# 已注册 skill_id 注册表(两平台统一为 JSON 数组)
KEY_SKILLS_REGISTRY = "_skills"

# Windows 分片大小(字符),留 25% 安全边际应对 1280 上限
_CHUNK_SIZE = 1024

# 平台分支:模块加载时确定一次
_WIN_CHUNKED: bool = sys.platform == "win32"

# 进程内缓存
_BUNDLE_CACHE: Dict[str, Optional[Dict[str, Optional[str]]]] = {}
#   skill_id -> 已解析 bundle 字典;None 表示"已确认 keychain 中不存在"
#   key 不存在表示"未读取过",下次访问会触发 keychain 读

_SKILLS_CACHE: Optional[List[str]] = None
#   None = 未初始化;list = 已读(含空列表)

# IO 计数器(仅在 KMS_KEYCHAIN_DEBUG=1 时累计)
_IO_DEBUG: bool = os.environ.get("KMS_KEYCHAIN_DEBUG") == "1"
_IO_COUNTER: Dict[str, int] = {"get": 0, "set": 0, "delete": 0}


# ---------------------------------------------------------------------------
# 后端可用性
# ---------------------------------------------------------------------------

def _require_backend() -> None:
    """keyring 不可用或后端初始化失败时抛统一异常。"""
    if not _KEYRING_AVAILABLE:
        raise RuntimeError("keyring 库未安装,请运行: pip install keyring")
    try:
        keyring.get_keyring()
    except Exception as e:
        raise RuntimeError(
            f"keychain 后端不可用({type(e).__name__}: {e});"
            "Linux 上请确认已安装并运行 GNOME Keyring 或 KWallet"
        ) from e


def _io_record(op: str) -> None:
    """记录一次底层 IO (仅 debug 模式)。"""
    if _IO_DEBUG:
        _IO_COUNTER[op] = _IO_COUNTER.get(op, 0) + 1


# ---------------------------------------------------------------------------
# 底层 CRUD (keyring 包装层)
# ---------------------------------------------------------------------------

def get(key: str) -> Optional[str]:
    """读取单个 key 的值;不存在返回 None。"""
    _require_backend()
    try:
        _io_record("get")
        return keyring.get_password(SERVICE_NAME, key)
    except KeyringError as e:
        raise RuntimeError(f"keychain 读取失败: {e}") from e


def set(key: str, value: str) -> None:
    """写入单个 key。"""
    _require_backend()
    try:
        _io_record("set")
        keyring.set_password(SERVICE_NAME, key, value)
    except KeyringError as e:
        raise RuntimeError(f"keychain 写入失败: {e}") from e


def delete(key: str) -> None:
    """删除单个 key;不存在不报错。"""
    _require_backend()
    try:
        _io_record("delete")
        keyring.delete_password(SERVICE_NAME, key)
    except PasswordDeleteError:
        pass
    except KeyringError as e:
        raise RuntimeError(f"keychain 删除失败: {e}") from e


# ---------------------------------------------------------------------------
# 进程内缓存工具(供测试/调试使用)
# ---------------------------------------------------------------------------

def reset_cache() -> None:
    """清空进程内缓存(测试用)。下一次读会重新打 keychain。"""
    _BUNDLE_CACHE.clear()
    global _SKILLS_CACHE
    _SKILLS_CACHE = None


def get_io_stats() -> Dict[str, int]:
    """获取 IO 计数器快照。"""
    return dict(_IO_COUNTER)


def reset_io_stats() -> None:
    """清零 IO 计数器。"""
    _IO_COUNTER["get"] = 0
    _IO_COUNTER["set"] = 0
    _IO_COUNTER["delete"] = 0


# ---------------------------------------------------------------------------
# Bundle 归一化
# ---------------------------------------------------------------------------

_BUNDLE_FIELDS = ("clawUid", "accessToken", "uuid")


def _normalize_bundle(data: object) -> Dict[str, Optional[str]]:
    """从反序列化后的 JSON 中提取三个字段;非 dict 输入返回空 bundle。"""
    if not isinstance(data, dict):
        return {f: None for f in _BUNDLE_FIELDS}
    return {
        "clawUid": data.get("clawUid") if isinstance(data.get("clawUid"), str) else None,
        "accessToken": data.get("accessToken") if isinstance(data.get("accessToken"), str) else None,
        "uuid": data.get("uuid") if isinstance(data.get("uuid"), str) else None,
    }


def _bundle_has_value(bundle: Dict[str, Optional[str]]) -> bool:
    return any(v is not None for v in bundle.values())


# ---------------------------------------------------------------------------
# Bundle 读取:macOS / Linux 单条路径
# ---------------------------------------------------------------------------

def _load_bundle_single(skill_id: str) -> Optional[Dict[str, Optional[str]]]:
    """非 Windows:单条 JSON。"""
    raw = get(skill_id)
    if not raw:
        return None
    try:
        return _normalize_bundle(json.loads(raw))
    except (json.JSONDecodeError, ValueError):
        return None


def _write_bundle_single(skill_id: str, bundle: Dict[str, Optional[str]]) -> None:
    payload = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
    set(skill_id, payload)


# ---------------------------------------------------------------------------
# Bundle 读取:Windows 分片路径
# ---------------------------------------------------------------------------

_MANIFEST_VERSION = 2
# 单条 manifest 在 keychain 中的硬上限;超过则 _write_bundle_win 抛 ValueError
_MANIFEST_MAX_LEN = 1280


def _load_bundle_win(skill_id: str) -> Optional[Dict[str, Optional[str]]]:
    """Windows:读 manifest(首片)+ 后续 #1, #2, ... chunks。"""
    manifest_raw = get(skill_id)
    if not manifest_raw:
        return None
    try:
        manifest = json.loads(manifest_raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(manifest, dict) or manifest.get("v") != _MANIFEST_VERSION:
        return None

    try:
        chunks_count = int(manifest.get("chunks", 1))
        chunk_size = int(manifest.get("chunk_size", _CHUNK_SIZE))
    except (TypeError, ValueError):
        return None
    if chunks_count < 1 or chunk_size < 1:
        return None

    first_chunk = manifest.get("data", "")
    if not isinstance(first_chunk, str):
        return None

    if chunks_count == 1:
        # 只有首片:首片就是完整 bundle JSON
        try:
            return _normalize_bundle(json.loads(first_chunk))
        except (json.JSONDecodeError, ValueError):
            return None

    # 多片:拼接后反序列化
    full = first_chunk
    for i in range(1, chunks_count):
        chunk = get(f"{skill_id}#{i}")
        if chunk is None:
            # 分片不完整(可能中途写入崩了),视为不存在
            return None
        full += chunk
    try:
        return _normalize_bundle(json.loads(full))
    except (json.JSONDecodeError, ValueError):
        return None


def _write_bundle_win(skill_id: str, bundle: Dict[str, Optional[str]]) -> None:
    """Windows:分片写。"""
    payload = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
    n = len(payload)

    if n == 0:
        _delete_bundle_win(skill_id)
        return

    chunks_count = max(1, (n + _CHUNK_SIZE - 1) // _CHUNK_SIZE)
    first_chunk = payload[:_CHUNK_SIZE]
    manifest = {
        "v": _MANIFEST_VERSION,
        "chunks": chunks_count,
        "chunk_size": _CHUNK_SIZE,
        "data": first_chunk,
    }
    manifest_raw = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    if len(manifest_raw) > _MANIFEST_MAX_LEN:
        raise ValueError(
            f"Windows keychain manifest 超长 ({len(manifest_raw)} 字符, 上限 {_MANIFEST_MAX_LEN});"
            "通常说明 JSON 序列化异常或 chunk_size 配置错误"
        )
    set(skill_id, manifest_raw)

    for i in range(1, chunks_count):
        chunk = payload[i * _CHUNK_SIZE : (i + 1) * _CHUNK_SIZE]
        set(f"{skill_id}#{i}", chunk)


def _delete_bundle_win(skill_id: str, max_chunks_to_check: int = 32) -> None:
    """Windows:删除该 skill 的 manifest + 最多 max_chunks_to_check 个 chunks。

    不知道实际有几个 chunks,从 #1 开始顺序删直至不存在。
    max_chunks_to_check 默认 32 片 = 32KB 凭据,足够覆盖所有场景。
    """
    delete(skill_id)
    for i in range(1, max_chunks_to_check + 1):
        chunk_key = f"{skill_id}#{i}"
        # 用一次 get 判断是否存在(避免无意义的 delete 调用记入 IO 计数器)
        try:
            if get(chunk_key) is None:
                break
        except RuntimeError:
            break
        try:
            delete(chunk_key)
        except RuntimeError:
            # 删除失败说明键已不存在或不可访问,停
            break


# ---------------------------------------------------------------------------
# Bundle 顶层分发
# ---------------------------------------------------------------------------

def _load_bundle(skill_id: str) -> Optional[Dict[str, Optional[str]]]:
    """从缓存或 keychain 读取 bundle。返回 None 表示已确认不存在。"""
    if not skill_id:
        return None
    if skill_id in _BUNDLE_CACHE:
        return _BUNDLE_CACHE[skill_id]
    try:
        if _WIN_CHUNKED:
            bundle = _load_bundle_win(skill_id)
        else:
            bundle = _load_bundle_single(skill_id)
    except RuntimeError:
        # 读失败不缓存;下次会重试
        return None
    _BUNDLE_CACHE[skill_id] = bundle
    return bundle


def _write_bundle(skill_id: str, bundle: Dict[str, Optional[str]]) -> None:
    """写 bundle 到 keychain + 同步缓存。空 bundle 走删除路径。"""
    if _bundle_has_value(bundle):
        _BUNDLE_CACHE[skill_id] = bundle
        if _WIN_CHUNKED:
            _write_bundle_win(skill_id, bundle)
        else:
            _write_bundle_single(skill_id, bundle)
    else:
        _delete_bundle(skill_id)


def _delete_bundle(skill_id: str) -> None:
    """删除该 skill 的所有 keychain 条目 + 清缓存。"""
    _BUNDLE_CACHE[skill_id] = None
    if _WIN_CHUNKED:
        try:
            _delete_bundle_win(skill_id)
        except RuntimeError:
            pass
    else:
        try:
            delete(skill_id)
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# Skill 注册表 (JSON 数组)
# ---------------------------------------------------------------------------

def _load_skills_registry() -> List[str]:
    """读 _skills 注册表(走缓存)。返回新 list,调用方修改不影响 cache。"""
    global _SKILLS_CACHE
    if _SKILLS_CACHE is not None:
        return list(_SKILLS_CACHE)
    raw = get(KEY_SKILLS_REGISTRY)
    skills: List[str] = []
    if raw:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            data = None
        if isinstance(data, list):
            skills = [s for s in data if isinstance(s, str) and s]
    _SKILLS_CACHE = skills
    return list(skills)


def _save_skills_registry(skills: List[str]) -> None:
    """写 _skills 注册表(走缓存 + 一次 set)。"""
    global _SKILLS_CACHE
    payload = json.dumps(skills, ensure_ascii=False, separators=(",", ":"))
    set(KEY_SKILLS_REGISTRY, payload)
    _SKILLS_CACHE = list(skills)


def _register_skill(skill_id: str) -> None:
    """将 skill_id 加入注册表(已存在跳过)。"""
    if not skill_id:
        return
    skills = _load_skills_registry()
    if skill_id in skills:
        return
    skills.append(skill_id)
    _save_skills_registry(skills)


def _unregister_skill(skill_id: str) -> None:
    """从注册表移除 skill_id;若列表空则一并删除键。"""
    if not skill_id:
        return
    skills = _load_skills_registry()
    if skill_id not in skills:
        return
    skills.remove(skill_id)
    if skills:
        _save_skills_registry(skills)
    else:
        try:
            delete(KEY_SKILLS_REGISTRY)
        except RuntimeError:
            pass
        global _SKILLS_CACHE
        _SKILLS_CACHE = []


def list_skills() -> List[str]:
    """返回所有已注册 skill_id。"""
    return _load_skills_registry()


# ---------------------------------------------------------------------------
# 公开高层 API
# ---------------------------------------------------------------------------

def get_auth_bundle(claw_uid: Optional[str], skill_id: str) -> Tuple[Optional[str], Optional[str]]:
    """读取指定 skill 的 (uuid, accessToken)。

    claw_uid 不匹配该 skill 槽内存储值时返回 (None, None),用于检测设备切换。
    """
    if not skill_id:
        return None, None

    bundle = _load_bundle(skill_id)
    if bundle is None:
        return None, None

    stored_claw = bundle.get("clawUid")
    if stored_claw is None:
        return None, None
    if claw_uid and stored_claw != claw_uid:
        return None, None
    return bundle.get("uuid"), bundle.get("accessToken")


def save_auth_bundle(
    *,
    claw_uid: Optional[str] = None,
    skill_id: str,
    access_token: Optional[str] = None,
    auth_uuid: Optional[str] = None,
    clear_uuid: bool = False,
) -> bool:
    """保存指定 skill 的凭据。

    - claw_uid:     非 None 时覆盖到该 skill 槽
    - access_token: None = 不动;非 None (含空串) = 覆盖
    - auth_uuid:    None = 不动;非 None = 覆盖;clear_uuid=True = 从 bundle 删除
    """
    if not skill_id:
        print("保存auth信息失败：缺少 skill_id")
        return False

    try:
        # 取现有 bundle(走缓存,稳态 0 keyring get)
        bundle = _load_bundle(skill_id) or {f: None for f in _BUNDLE_FIELDS}

        # claw_uid 用真值判断(对齐旧版 if claw_uid: 语义;None/空串均跳过)
        if claw_uid:
            bundle["clawUid"] = claw_uid
        if access_token is not None:
            bundle["accessToken"] = access_token
        if clear_uuid:
            bundle.pop("uuid", None)
        elif auth_uuid is not None:
            bundle["uuid"] = auth_uuid

        _write_bundle(skill_id, bundle)

        # 只要有任何该 skill 的数据存在,就注册到 skill 表
        if _bundle_has_value(bundle):
            _register_skill(skill_id)
        return True
    except RuntimeError as e:
        print(f"保存auth信息失败：{e}")
        return False


def revoke_access_token(skill_id: str) -> bool:
    """从该 skill 的 bundle 中删除 accessToken 字段(显式语义)。

    与 save_auth_bundle(access_token="") hack 不同:本函数写回时 accessToken
    字段真正从 bundle 移除,后续 get_auth_bundle 直接返回 (uuid, None)。
    """
    if not skill_id:
        print("撤销accessToken失败：缺少 skill_id")
        return False

    try:
        bundle = _load_bundle(skill_id)
        if bundle is None:
            return True  # 没有凭据也算成功(幂等)
        bundle.pop("accessToken", None)
        _write_bundle(skill_id, bundle)
        return True
    except RuntimeError as e:
        print(f"撤销accessToken失败：{e}")
        return False


def clear_all(skill_id: Optional[str] = None) -> int:
    """清空凭据。

    - skill_id 指定时:仅清除该 skill 的 bundle + 从注册表移除
    - skill_id=None:按注册表逐个清所有 skill,再清注册表本身
    返回实际删除的条目数。
    """
    deleted = 0

    if skill_id:
        try:
            _delete_bundle(skill_id)
            deleted += 1
        except RuntimeError:
            pass
        try:
            _unregister_skill(skill_id)
        except RuntimeError:
            pass
        return deleted

    # 清全部:遍历注册表
    for sid in list_skills():
        try:
            _delete_bundle(sid)
            deleted += 1
        except RuntimeError:
            continue
        try:
            _unregister_skill(sid)
        except RuntimeError:
            continue

    # 清注册表本身
    try:
        if get(KEY_SKILLS_REGISTRY) is not None:
            delete(KEY_SKILLS_REGISTRY)
            deleted += 1
    except RuntimeError:
        pass

    # 清缓存
    _BUNDLE_CACHE.clear()
    global _SKILLS_CACHE
    _SKILLS_CACHE = None
    return deleted


def status(skill_id: Optional[str] = None) -> Dict[str, object]:
    """返回凭据状态(用于 status 命令)。

    返回:
      {
        "skills": {
          "<skill_id>": {"claw_uid": bool, "access_token": bool, "uuid": bool},
          ...
        }
      }
    """
    target_skills = [skill_id] if skill_id else list_skills()
    skills_dict: Dict[str, Dict[str, bool]] = {}
    for sid in target_skills:
        if not sid:
            continue
        bundle = _load_bundle(sid)
        skills_dict[sid] = {
            "claw_uid": bool(bundle and bundle.get("clawUid") is not None),
            "access_token": bool(bundle and bundle.get("accessToken") is not None),
            "uuid": bool(bundle and bundle.get("uuid") is not None),
        }
    return {"skills": skills_dict}


def get_claw_uid_masked(skill_id: str, prefix_len: int = 8) -> Optional[str]:
    """返回指定 skill 的 clawUid 前缀掩码。"""
    if not skill_id:
        return None
    bundle = _load_bundle(skill_id)
    if not bundle:
        return None
    value = bundle.get("clawUid")
    if not value:
        return None
    if len(value) <= prefix_len:
        return value
    return value[:prefix_len] + "..."