"""为 cm-cloud CLI 自动准备当前用户隔离的 Python 依赖。"""

import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


REQUIRED_IMPORTS = ("requests", "Crypto", "keyring")
MINIMUM_PYTHON = (3, 10)
READY_MARKER = ".cmcloud-requirements-ready"
MANIFEST_FILE = ".cmcloud-manifest.json"

# 完整性校验的验证戳：校验通过后写入，戳新鲜期内跳过重复全量校验
# （校验成本随缓存文件数线性增长，受限环境下更慢）。
VERIFICATION_STAMP_FILE = ".cmcloud-manifest-verified"
VERIFICATION_STAMP_MAX_AGE_SECONDS = 3600

# 废弃残目录的冷却期：无就绪标记且超过该时长的目录视为安装中断遗留，持锁清理。
ABANDONED_CACHE_AGE_SECONDS = 600

# 依赖安装与锁等待的上界：超过即受控失败，避免调用方无限挂起。
# pip 网络参数收紧为快速失败，避免慢网/弱网下长时间重试拖死 CLI。
LOCK_WAIT_SECONDS = 60
PIP_INSTALL_TIMEOUT_SECONDS = 90
PIP_NETWORK_ARGS = ("--retries", "2", "--timeout", "15")


class DependencyBootstrapError(RuntimeError):
    """依赖自动准备失败。"""


def ensure_dependencies(
    *, requirements_path=None, cache_root=None, runner=None, required_imports=None
):
    """安装并加载与当前 requirements 指纹对应的用户级隔离依赖目录。"""
    if sys.version_info < MINIMUM_PYTHON:
        raise DependencyBootstrapError(
            "cm-cloud 需要 Python 3.10 或更高版本，当前为 %s.%s"
            % (sys.version_info.major, sys.version_info.minor)
        )
    requirements = Path(requirements_path or _requirements_path()).resolve()
    try:
        content = requirements.read_bytes()
    except OSError as err:
        raise DependencyBootstrapError("无法读取依赖清单: %s" % err) from err
    fingerprint = hashlib.sha256(content).hexdigest()
    root = Path(cache_root or _default_cache_root()).expanduser().resolve()
    cache_dir = root / ("py%s.%s" % sys.version_info[:2])
    cache_key = fingerprint[:16]
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        raise DependencyBootstrapError("无法创建依赖缓存目录: %s" % err) from err
    imports = tuple(required_imports or REQUIRED_IMPORTS)

    # 快路径：缓存已就绪时无需持锁，避免并发调用在安装锁上排队
    # （锁只为安装/修复而设；等锁期间的安装进度由双重检查兜底）。
    ready = _ready_candidate(cache_dir, cache_key, fingerprint, imports)
    if ready is not None:
        return ready

    with _install_lock(cache_dir / (cache_key + ".lock")):
        # 双重检查：等锁期间可能已被其它进程装好。
        ready = _ready_candidate(cache_dir, cache_key, fingerprint, imports)
        if ready is not None:
            return ready
        # 清理安装中断的残目录（无标记 + 已过冷却期），避免残目录无限堆积。
        _prune_abandoned(cache_dir, cache_key)
        # 仍能就绪的缓存保留原样，损坏目录不做批量删除；始终在全新目录安装。
        target = Path(tempfile.mkdtemp(prefix=cache_key + "-", dir=cache_dir))
        _install_cache_finder(target)
        command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--upgrade",
            *PIP_NETWORK_ARGS,
            "--target",
            str(target),
            "-r",
            str(requirements),
        ]
        execute = runner or subprocess.run
        options = {"check": False, "capture_output": True, "text": True}
        if runner is None:
            # 注入的 runner 供测试使用，不接受 timeout；真实 pip 必须有上界。
            options["timeout"] = PIP_INSTALL_TIMEOUT_SECONDS
        try:
            completed = execute(command, **options)
        except subprocess.TimeoutExpired as err:
            _remove_cache_finder(target)
            raise DependencyBootstrapError(
                "自动安装 Python 依赖超时（超过 %s 秒），请检查网络后重试"
                % PIP_INSTALL_TIMEOUT_SECONDS
            ) from err
        except OSError as err:
            _remove_cache_finder(target)
            raise DependencyBootstrapError("无法启动 pip: %s" % err) from err
        if completed.returncode != 0:
            _remove_cache_finder(target)
            detail = _last_line(completed.stderr) or _last_line(completed.stdout)
            suffix = "：%s" % detail if detail else ""
            raise DependencyBootstrapError(
                "自动安装 Python 依赖失败（pip 退出码 %s）%s"
                % (completed.returncode, suffix)
            )
        importlib.invalidate_caches()
        missing = _missing_imports(imports)
        if missing:
            _remove_cache_finder(target)
            raise DependencyBootstrapError(
                "依赖安装完成但仍无法加载: %s" % ", ".join(missing)
            )
        # 先写完整性清单、最后写就绪标记：中途失败留下的目录"无标记"，语义单一
        # （无标记 = 未完成），交由 _prune_abandoned 在后续持锁调用时清理。
        try:
            _write_manifest(target)
        except Exception:
            _remove_cache_finder(target)
            raise
        try:
            (target / READY_MARKER).write_text(fingerprint, encoding="ascii")
        except OSError as err:
            _remove_cache_finder(target)
            raise DependencyBootstrapError("无法写入依赖就绪标记: %s" % err) from err
    return target


def _requirements_path():
    return (
        Path(__file__).resolve().parent.parent
        / "IDaaS_AUT_KMS_v3.1.1"
        / "requirements.txt"
    )


def _default_cache_root():
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "cm-cloud" / "python-deps"
        return Path.home() / "AppData" / "Local" / "cm-cloud" / "python-deps"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "cm-cloud" / "python-deps"
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "cm-cloud" / "python-deps"
    return Path.home() / ".cache" / "cm-cloud" / "python-deps"


class _CachePathFinder:
    """依赖缓存目录的兜底查找器：替代把缓存目录插入模块搜索路径的做法。

    挂在 ``sys.meta_path`` 末尾，排在标准库与站点包的 ``PathFinder`` 之后，因此同名模块
    只在系统未安装时才回落到缓存目录；缓存内容无法再遮蔽真实依赖。
    """

    def __init__(self, directory):
        self.directory = str(directory)

    def find_spec(self, name, path=None, target=None):
        return importlib.machinery.PathFinder.find_spec(name, [self.directory])


_CACHE_FINDERS = {}


def _install_cache_finder(target):
    key = str(target)
    finder = _CACHE_FINDERS.get(key)
    if finder is None:
        finder = _CachePathFinder(key)
        _CACHE_FINDERS[key] = finder
    if finder not in sys.meta_path:
        sys.meta_path.append(finder)


def _remove_cache_finder(target):
    finder = _CACHE_FINDERS.get(str(target))
    if finder in sys.meta_path:
        sys.meta_path.remove(finder)


def _cache_candidates(cache_dir, cache_key):
    """先读旧版固定目录，再读同指纹的新目录；不修改任何候选。"""
    legacy = cache_dir / cache_key
    if legacy.is_dir() and not legacy.is_symlink():
        yield legacy
    try:
        children = sorted(cache_dir.glob(cache_key + "-*"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return
    for child in children:
        if child.is_dir() and not child.is_symlink():
            yield child


def _ready_candidate(cache_dir, cache_key, fingerprint, imports):
    """无锁检查已就绪的缓存候选；命中返回目录路径，未命中返回 None。"""
    for candidate in _cache_candidates(cache_dir, cache_key):
        _install_cache_finder(candidate)
        if _is_ready(candidate, candidate / READY_MARKER, fingerprint, imports):
            return candidate
        _remove_cache_finder(candidate)
    return None


def _prune_abandoned(cache_dir, cache_key):
    """清理安装中断留下的残目录：无就绪标记且已过冷却期。

    仅在持锁路径调用：安装与建目录全程持锁，锁内看到的无标记目录不可能是正在进行的
    安装，只可能是被强杀等异常留下的残留；冷却期进一步兜底避免误删。
    """
    cutoff = time.time() - ABANDONED_CACHE_AGE_SECONDS
    try:
        children = list(cache_dir.glob(cache_key + "-*"))
    except OSError:
        return
    for child in children:
        try:
            if (child / READY_MARKER).exists():
                continue
            if not child.is_dir() or child.is_symlink():
                continue
            if child.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(child, ignore_errors=True)


def _manifest_entries(target):
    """缓存目录完整性清单：文件相对路径 -> sha256。

    排除字节码缓存（``__pycache__`` / ``*.pyc``，由解释器在导入时生成、内容不稳定）
    与 ``.cmcloud-`` 前缀的管理产物（就绪标记、清单自身、验证戳及未来同类文件）。
    """
    try:
        paths = sorted(path for path in target.rglob("*") if path.is_file())
    except OSError:
        return None
    entries = {}
    for path in paths:
        try:
            relative = path.relative_to(target)
        except ValueError:
            return None
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if relative.name.startswith(".cmcloud-"):
            continue
        try:
            entries[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None
    return entries


def _write_manifest(target):
    entries = _manifest_entries(target)
    if entries is None:
        raise DependencyBootstrapError("无法读取依赖缓存内容，无法生成完整性清单")
    try:
        (target / MANIFEST_FILE).write_text(
            json.dumps(entries, sort_keys=True), encoding="utf-8"
        )
    except OSError as err:
        raise DependencyBootstrapError("无法写入依赖完整性清单: %s" % err) from err


def _manifest_drift(target, expected):
    """对比完整性清单与实际内容：只看"文件缺失或内容变化"。

    运行期目录里可能多出解释器/工具链产物（验证戳、编辑器临时文件等），额外文件
    不构成损坏，不应触发重装。返回 (missing, modified)；目录不可读返回 None。
    """
    actual = _manifest_entries(target)
    if actual is None:
        return None
    missing = sorted(name for name in expected if name not in actual)
    modified = sorted(
        name for name in expected if name in actual and expected[name] != actual[name]
    )
    return missing, modified


def _verify_manifest(target):
    """校验缓存内容完整性；清单缺失时按当前内容重建，避免历史遗留目录反复重装。

    调用方（``_is_ready``）必须已确认：就绪标记存在且依赖可加载。此时目录确实可用，
    重建清单只是把真实内容记录为新基准。清单存在但显示文件缺失/内容变化时按损坏
    处理，不重建（真正损坏必须重装）。
    """
    try:
        raw = (target / MANIFEST_FILE).read_text(encoding="utf-8")
    except OSError:
        raw = None
    if raw is not None:
        try:
            expected = json.loads(raw)
        except ValueError:
            expected = None
        if isinstance(expected, dict) and expected:
            drift = _manifest_drift(target, expected)
            if drift is None:
                return False
            missing, modified = drift
            return not missing and not modified
    try:
        _write_manifest(target)
    except Exception:
        return False
    return True


def _verification_stamp_path(target):
    return target / VERIFICATION_STAMP_FILE


def _verification_is_fresh(target, fingerprint):
    """戳新鲜期内跳过重复全量校验；CMCLOUD_DEPS_VERIFY_ALWAYS 置位时强制全量。"""
    if os.environ.get("CMCLOUD_DEPS_VERIFY_ALWAYS"):
        return False
    try:
        stamp = json.loads(_verification_stamp_path(target).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(stamp, dict) or stamp.get("fingerprint") != fingerprint:
        return False
    try:
        verified_at = datetime.fromisoformat(str(stamp.get("verifiedAt", "")))
    except ValueError:
        return False
    if verified_at.tzinfo is None:
        return False
    return (datetime.now(timezone.utc) - verified_at) < timedelta(
        seconds=VERIFICATION_STAMP_MAX_AGE_SECONDS
    )


def _write_verification_stamp(target, fingerprint):
    stamp = {
        "fingerprint": fingerprint,
        "verifiedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    try:
        _verification_stamp_path(target).write_text(json.dumps(stamp), encoding="utf-8")
    except OSError:
        pass


def _missing_imports(imports):
    return tuple(name for name in imports if importlib.util.find_spec(name) is None)


def _is_ready(target, marker, fingerprint, imports):
    try:
        ready = marker.read_text(encoding="ascii").strip() == fingerprint
    except OSError:
        return False
    if not ready or _missing_imports(imports):
        return False
    # 指纹一致还不够：缓存内容必须先通过完整性校验，否则按未就绪重装。
    # 校验通过后写验证戳，戳新鲜期内跳过重复全量校验（成本随缓存文件数线性增长）。
    if _verification_is_fresh(target, fingerprint):
        return True
    if not _verify_manifest(target):
        return False
    _write_verification_stamp(target, fingerprint)
    return True


def _last_line(value):
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    return lines[-1][:500] if lines else ""


@contextlib.contextmanager
def _install_lock(path):
    """跨进程安装锁：等待上界 LOCK_WAIT_SECONDS，超时转受控失败。

    Windows 用 msvcrt.locking、POSIX 用 fcntl.flock，都走非阻塞轮询 + 截止时间；
    进程被强杀时由内核释放锁，不依赖锁文件是否残留，等待方不会无限挂起。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            def _lock():
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

            def _unlock():
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def _lock():
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def _unlock():
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                _lock()
                break
            except OSError as err:
                if time.monotonic() >= deadline:
                    raise DependencyBootstrapError(
                        "等待其它进程安装依赖超时（超过 %s 秒）" % LOCK_WAIT_SECONDS
                    ) from err
                time.sleep(0.2)
        try:
            yield
        finally:
            handle.seek(0)
            _unlock()


__all__ = ("DependencyBootstrapError", "ensure_dependencies")
