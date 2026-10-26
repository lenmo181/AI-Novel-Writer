"""设备风控参数采集模块 + 智能体识别。

提供:
  - collect_device_info(): 设备硬件风控参数
  - collect_agent_info(): 智能体平台路径与实例ID（用于 preAuth payload）

智能体识别规则依据《主流智能体环境变量识别.xlsx》，按优先级匹配 SDK 部署路径。
"""
import ctypes
import json
import logging
import os
import platform
import socket
import sys
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_log_enabled = False


def set_log_enabled(enabled: bool) -> None:
    """全局开关：是否在采集/识别失败时打印 warning。默认 False。"""
    global _log_enabled
    _log_enabled = bool(enabled)


def _log_warning(message: str) -> None:
    if _log_enabled:
        logger.warning(message)


# ============================================================
# 设备风控参数（向后兼容）
# ============================================================

def _get_os_type() -> str:
    try:
        sys = platform.system().lower()
        return {"windows": "windows", "darwin": "macos", "linux": "linux"}.get(sys, "other")
    except Exception as e:
        _log_warning(f"get_os_type failed: {e}")
        return ""


def _get_os_version() -> str:
    try:
        return platform.release() or ""
    except Exception as e:
        _log_warning(f"get_os_version failed: {e}")
        return ""


def _get_hostname() -> str:
    try:
        return socket.gethostname().upper()
    except Exception as e:
        _log_warning(f"get_hostname failed: {e}")
        return ""


def _get_cpu_id() -> str:
    try:
        return platform.processor().strip()
    except Exception as e:
        _log_warning(f"get_cpu_id failed: {e}")
        return ""


def collect_device_info() -> dict:
    """采集本机设备风控参数。"""
    return {
        "osType": _get_os_type(),
        "osVersion": _get_os_version(),
        "hostName": _get_hostname(),
        "cpuId": _get_cpu_id(),
    }


# ============================================================
# 智能体识别（preAuth payload 用）
# ============================================================

# 关键词大小写不敏感匹配时使用的常量
_KEYWORD_LEN_OPENCLAW = len("openclaw")


def _script_dir() -> str:
    """当前脚本所在目录的绝对路径。"""
    return os.path.dirname(os.path.abspath(__file__))


def _expand(path: str) -> str:
    """展开 ~ 为用户主目录。"""
    return os.path.expanduser(path)


def _read_text_file(path: str) -> Optional[str]:
    """读取文本文件内容并去除首尾空白；失败返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _read_json_machine_id(path: str) -> Optional[str]:
    """读取 JSON 文件并取 machineID 字段；失败返回 None。

    ClaudeCode 的 ~/.claude.json 用 machineID 作为设备标识（64 字符 hex），
    不是 userId（ClaudeCode 实际文件里没有 userId 字段）。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        mid = data.get("machineID")
        if mid is None:
            return None
        return str(mid).strip() or None
    except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError):
        return None


def _read_json_device_id(path: str) -> Optional[str]:
    """读取 JSON 文件并取 deviceId 字段；失败返回 None。

    用于 auto_claw / open_claw / easy_claw / q_claw / claw_x 等智能体的 *device.json。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        did = data.get("deviceId")
        if did is None:
            return None
        return str(did).strip() or None
    except (FileNotFoundError, PermissionError, OSError, json.JSONDecodeError):
        return None


def _replace_openclaw_with_clawx(path: str) -> str:
    """
    把 path 中第一个 openclaw（大小写不敏感、严格边界匹配）替换为 clawx。

    边界：前面是路径分隔符或字符串开头，后面是路径分隔符或字符串结尾。
    避免子串误替换（如 test_clawx_replaces_openclaw_w0 里的 _openclaw_w0
    部分会被识别为独立 openclaw 但不是合法目录段）。

    - 不动 openclaw 之后的子串（避免 openclaw-autoclaw → clawx-autoclaw）
    - 大小写不敏感匹配，但替换目标固定为小写 clawx
    """
    seps = ("/", "\\")
    lower = path.lower()
    pos = 0
    while True:
        pos = lower.find("openclaw", pos)
        if pos < 0:
            return path
        before_ok = (pos == 0) or (path[pos - 1] in seps)
        after_pos = pos + _KEYWORD_LEN_OPENCLAW
        after_ok = (after_pos >= len(path)) or (path[after_pos] in seps)
        if before_ok and after_ok:
            return path[:pos] + "clawx" + path[after_pos:]
        pos = after_pos


def _check_clawx(sdk_path: str) -> bool:
    """
    ClawX 额外检查：sdk_path 中含 openclaw 的目录，下级有 .openclaw/clawx。

    严格匹配路径分隔符边界，避免子串误识别（如 test_clawx_replaces_openclaw_w0
    这种测试目录名本身含 openclaw 子串的情况）。

    例如 sdk_path = '/home/user/openclaw/skills/foo'：
      - openclaw 所在目录 = '/home/user/openclaw'
      - 候选 = '/home/user/openclaw/.openclaw/clawx'
    """
    seps = ("/", "\\")
    lower = sdk_path.lower()
    pos = 0
    while True:
        pos = lower.find("openclaw", pos)
        if pos < 0:
            return False
        # 边界检查：前面是分隔符或字符串开头，后面是分隔符或字符串结尾
        before_ok = (pos == 0) or (sdk_path[pos - 1] in seps)
        after_pos = pos + _KEYWORD_LEN_OPENCLAW
        after_ok = (after_pos >= len(sdk_path)) or (sdk_path[after_pos] in seps)
        if before_ok and after_ok:
            openclaw_dir = sdk_path[:after_pos]
            candidate = os.path.join(openclaw_dir, ".openclaw", "clawx")
            return os.path.exists(candidate)
        pos = after_pos  # 继续往后找


# 智能体规则（按优先级排序）：
#   1. ClawX 优先（与 OpenClaw 同样含 openclaw，靠额外检查区分）
#   2. AutoClaw（autoclaw）优先于 OpenClaw（openclaw），靠关键字特异性区分
#   3. 其他按表格顺序
_AGENT_RULES: List[Dict[str, object]] = [
    # ClawX：路径含 openclaw 且 openclaw 目录下有 .openclaw/clawx
    {
        "code": "claw_x",
        "keyword": "openclaw",
        "instance_source": "~/.openclaw/identity/device-auth.json",
        "instance_reader": _read_json_device_id,  # JSON 的 deviceId 字段
        "extra_check": _check_clawx,
        "replace_in_path": True,  # 命中后把路径里的 openclaw 替换为 clawx
    },
    # AutoClaw：路径含 autoclaw
    # 关键字 'autoclaw' 比 'openclaw' 更具体，先匹配；
    # instance_source 仍指向 ~/.openclaw-autoclaw/，兼容旧部署布局。
    {
        "code": "auto_claw",
        "keyword": "autoclaw",
        "instance_source": "~/.openclaw-autoclaw/identity/device.json",
        "instance_reader": _read_json_device_id,  # JSON 的 deviceId 字段
        "extra_check": None,
        "replace_in_path": False,
    },
    # OpenClaw：路径含 openclaw
    {
        "code": "open_claw",
        "keyword": "openclaw",
        "instance_source": "~/.openclaw/identity/device.json",
        "instance_reader": _read_json_device_id,  # JSON 的 deviceId 字段
        "extra_check": None,
        "replace_in_path": False,
    },
    # 其他按表格顺序
    {
        "code": "work_buddy",
        "keyword": "workbuddy",
        "instance_source": "~/.workbuddy/device-id",
        "instance_reader": _read_text_file,
        "extra_check": None,
        "replace_in_path": False,
        # ★ WorkBuddy 真实特征变量白名单(基于 2026-09-22 dump,跨平台修正 v2)
        # 4 个 key 全部跨平台(Windows + Mac 双验证),3 个运行时 + 1 个安装即有的,
        # 满足"安装即有的最多 1 个"偏好。
        # 5/5 跨平台命中 → 4/4 跨平台命中,跨平台兼容性不退化。
        "env_signature_keys": [
            "WORKBUDDY_APP_PATH",          # 跨平台,安装即有的唯一保留(路径含 WorkBuddy)
            "WORKBUDDY_STARTUP_PID",       # 跨平台,运行时 + PID 可验证
            "WORKBUDDY_PAC_RPC_SOCKET",    # 跨平台,运行时 + socket 可验证(Windows: named pipe / Mac: unix sock)
            "CLIENT_INFO_PRODUCT_NAME",    # 跨平台,运行时,强唯一 key 名
        ],
    },
    {
        "code": "baidu_buddy",
        "keyword": "qianfan",
        "instance_source": None,  # 云端 AI
        "instance_reader": None,
        "extra_check": None,
        "replace_in_path": False,
    },
    {
        "code": "doubao_work",
        "keyword": "doubao",
        "instance_source": None,  # 云端 AI
        "instance_reader": None,
        "extra_check": None,
        "replace_in_path": False,
    },
    {
        "code": "qianwen_work",
        "keyword": "qwenwork",
        "instance_source": "~/.qwenworkcn/machine-id",
        "instance_reader": _read_text_file,
        "extra_check": None,
        "replace_in_path": False,
    },
    {
        "code": "hermes",
        "keyword": "hermes",
        "instance_source": None,  # 无
        "instance_reader": None,
        "extra_check": None,
        "replace_in_path": False,
        # ★ Hermes 特征变量白名单(基于 2026-09-22 dump,采用方案 A:3 运行时 + 1 安装)
        # 优先运行时必有变量(SDK 跑时一定设),只留 1 个安装即有的作为补强,
        # 把误报风险降到最低。过半命中即识别。
        # ★ v2(2026-09-22 跨平台修正):基于 Windows + Mac 两份 dump 交叉验证
        # 旧版 4 个 key 中有 3 个只在 Windows 有,Mac 完全不命中(1/4)。
        # 新版 4 个 key 全部跨平台,且 100% 运行时变量(0 个安装即有的),
        # 满足"安装即有的最多选 1 个"偏好。
        "env_signature_keys": [
            "HERMES_AGENT",             # 跨平台,运行时必有 ★★★
            "HERMES_INTERACTIVE",       # 跨平台,运行时 ★★
            "HERMES_REAL_HOME",         # 跨平台,运行时 + 路径可验证 ★★
            "_HERMES_GATEWAY",          # 跨平台,运行时 ★★
        ],
    },
    {
        "code": "easy_claw",
        "keyword": "easyclaw",
        "instance_source": "~/.easyclaw/identity/device.json",
        "instance_reader": _read_json_device_id,  # JSON 的 deviceId 字段
        "extra_check": None,
        "replace_in_path": False,
    },
    {
        "code": "q_claw",
        "keyword": "qclaw",
        "instance_source": "~/.qclaw/identity/device.json",
        "instance_reader": _read_json_device_id,  # JSON 的 deviceId 字段
        "extra_check": None,
        "replace_in_path": False,
    },
    {
        "code": "kimi_claw",
        "keyword": "kimi",
        "instance_source": None,  # 云端 AI
        "instance_reader": None,
        "extra_check": None,
        "replace_in_path": False,
    },
    {
        "code": "claude_code",
        "keyword": "claude",
        "instance_source": "~/.claude.json",
        "instance_reader": _read_json_machine_id,  # 取 JSON 的 machineID（ClaudeCode 实例标识）
        "extra_check": None,
        "replace_in_path": False,
        # ★ Claude Code 真实特征变量白名单(基于 2026-09-22 dump 数据)
        # 只配 env_signature_keys 的 agent 走"过半制"投票,杜绝 PATH/用户名/URL
        # 等字符串里碰巧含 keyword 导致的误识别。
        # 阈值:命中数 / 总数 >= 0.5 且 命中数 >= 2(防止白名单太小的 agent 被误判)
        "env_signature_keys": [
            "CLAUDECODE",                       # 简明标志
            "CLAUDE_CODE_EXECPATH",             # 值可验证(exe 完整路径)
            "CLAUDE_PID",                       # 值可验证(PID 存活)
            "CLAUDE_CODE_ENTRYPOINT",           # 启动入口(cli/ide/sdk/vscode)
            "CLAUDE_CODE_MESSAGING_SOCKET",     # 值可验证(named pipe)
        ],
    },
    {
        "code": "codex",
        "keyword": "codex",
        "instance_source": None,  # 无
        "instance_reader": None,
        "extra_check": None,
        "replace_in_path": False,
        # ★ Codex 特征变量白名单(基于 2026-09-22 dump,Windows + Mac 双验证)
        # 7 个 key 全部跨平台 + 全部运行时变量(0 个安装即有的),符合"安装即有的最多 1 个"偏好。
        # UUID 类(SESSION_ID/THREAD_ID)每次启动值会变,但投票只看"key 存在且非空",
        # 变与不变不影响命中识别 — 只要 SDK 在 codex 里跑,这两个 key 就一定存在。
        # 进程链第 L2/L3 层已能命中 codex.exe / ChatGPT.app/Resources/codex,
        # env 投票只是兜底层,主要防"客户端污染导致误识别"。
        "env_signature_keys": [
            "CODEX_APP_TOOLS_PIPE_PATH",           # socket,值可验证(Windows=pipe / Mac=sock),跨平台
            "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",  # 字符串 "Codex Desktop",跨平台,值稳定
            "CODEX_MCP_NODE_PATH",                 # 路径,值可验证,跨平台
            "CODEX_SESSION_ID",                    # UUID,跨平台,运行时必有
            "CODEX_THREAD_ID",                     # UUID,跨平台,运行时必有
            "CODEX_VERSION",                       # 版本号,跨平台,运行时必有
            "CODEX_CI",                            # 简单值 "1",跨平台
        ],
    },
]


# ============================================================
# env 白名单聚合(最小权限读取)
# ============================================================
def _collect_env_signature_keys() -> List[str]:
    """
    聚合 _AGENT_RULES 里所有配了 env_signature_keys 的 agent 的白名单 key（去重）。

    ★ 用于 _detect_agent_from_env 的最小权限读取:只读这些 key,
      不再全量快照 os.environ(避免把用户敏感环境变量
      如 AWS_SECRET_ACCESS_KEY / 数据库连接串 载入内存)。
    投票逻辑仍由 _match_agent_by_env_voting 负责;此处只聚合全集。
    """
    seen: set = set()
    result: List[str] = []
    for rule in _AGENT_RULES:
        for k in rule.get("env_signature_keys", []) or []:
            ku = str(k).upper()
            if ku not in seen:
                seen.add(ku)
                result.append(str(k))
    return result


_ENV_SIGNATURE_KEYS: List[str] = _collect_env_signature_keys()


# ============================================================
# 进程链查询（路径未命中时，回退识别的中间优先级）
# ============================================================
# 进程链比环境变量更可靠：环境变量可被任意注入，但"当前进程被哪个智能体进程拉起"
# 是 OS 决定的，没法伪造。
# 跨平台策略：psutil 优先（依赖少、代码简洁），纯标准库兜底（无第三方依赖）。

_ANCESTOR_DEPTH = 10  # 向上查几层


def _get_ancestor_processes_psutil(depth: int):
    """psutil 路径：跨平台统一，3 行核心逻辑。"""
    import psutil  # type: ignore

    chain = []
    pid = os.getpid()
    seen = set()
    for _ in range(depth + 1):
        if pid in seen or pid <= 0:
            break
        seen.add(pid)
        try:
            p = psutil.Process(pid)
            chain.append({
                "pid": pid,
                "name": p.name() or "",
                "exe": p.exe() if hasattr(p, "exe") else None,
            })
            pid = p.ppid()
        except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
            break
        except Exception:
            break
    return chain


def _get_ancestor_processes_stdlib(depth: int):
    """标准库兜底：Win 用 toolhelp32，Linux 用 /proc，macOS 用 libproc。"""
    system = platform.system()
    if system == "Windows":
        return _get_ancestor_processes_windows(depth)
    if system == "Linux":
        return _get_ancestor_processes_linux(depth)
    if system == "Darwin":
        return _get_ancestor_processes_macos(depth)
    raise RuntimeError(f"不支持的平台: {system}")


def _get_ancestor_processes_windows(depth: int):
    """Windows: toolhelp32 + psapi。"""
    try:
        import ctypes.wintypes as wt  # type: ignore
    except ImportError:
        return []

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD),
            ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(wt.ULONG)),
            ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wt.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi

    def snapshot():
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == wt.HANDLE(-1).value:
            return {}
        try:
            pe = PROCESSENTRY32()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
            out = {}
            ok = kernel32.Process32FirstW(snap, ctypes.byref(pe))
            while ok:
                out[pe.th32ProcessID] = (pe.th32ParentProcessID, pe.szExeFile)
                ok = kernel32.Process32NextW(snap, ctypes.byref(pe))
            return out
        finally:
            kernel32.CloseHandle(snap)

    def get_module_exe(pid):
        PROCESS_QUERY_INFORMATION = 0x0400
        PROCESS_VM_READ = 0x0010
        h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(260)
            n = psapi.GetModuleFileNameExW(h, None, buf, 260)
            return buf.value if n > 0 else None
        finally:
            kernel32.CloseHandle(h)

    snap = snapshot()
    chain = []
    pid = os.getpid()
    seen = set()
    for _ in range(depth + 1):
        if pid in seen or pid == 0:
            break
        seen.add(pid)
        info = snap.get(pid)
        if info is None:
            break
        ppid, exe_name = info
        chain.append({
            "pid": pid,
            "name": exe_name or "",
            "exe": get_module_exe(pid),
        })
        if ppid == 0 or ppid == pid:
            break
        pid = ppid
    return chain


def _get_ancestor_processes_linux(depth: int):
    """Linux: /proc/<pid>/{comm,exe,status}。"""
    chain = []
    pid = os.getpid()
    seen = set()
    for _ in range(depth + 1):
        if pid in seen or pid == 0:
            break
        seen.add(pid)
        try:
            with open(f"/proc/{pid}/comm", "r", encoding="utf-8") as f:
                name = f.read().strip()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            break
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            exe = None
        chain.append({"pid": pid, "name": name, "exe": exe})
        try:
            with open(f"/proc/{pid}/status", "r", encoding="utf-8") as f:
                ppid = 0
                for line in f:
                    if line.startswith("PPid:"):
                        ppid = int(line.split()[1])
                        break
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            break
        if ppid == 0 or ppid == pid:
            break
        pid = ppid
    return chain


def _get_ancestor_processes_macos(depth: int):
    """macOS: libproc 原生（沙箱里 ps 命令可能被禁，但 libproc 可用）。"""
    import ctypes.util

    libproc_path = ctypes.util.find_library("proc")
    if libproc_path and not libproc_path.startswith("/"):
        for cand in ("/usr/lib/libproc.dylib",
                     "/System/Library/PrivateFrameworks/libproc.dylib"):
            if os.path.exists(cand):
                libproc_path = cand
                break
    if not libproc_path or not os.path.exists(libproc_path):
        return _get_ancestor_processes_macos_ps(depth)

    try:
        libproc = ctypes.CDLL(libproc_path)
    except OSError:
        return _get_ancestor_processes_macos_ps(depth)

    libproc.proc_name.restype = ctypes.c_int
    libproc.proc_name.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]

    PROC_PIDPATHINFO_MAXSIZE = 4096
    libproc.proc_pidpath.restype = ctypes.c_int
    libproc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]

    PROC_PIDT_SHORTBSDINFO = 14
    libproc.proc_pidinfo.restype = ctypes.c_int
    libproc.proc_pidinfo.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
        ctypes.c_void_p, ctypes.c_int,
    ]

    class proc_bsdshortinfo(ctypes.Structure):
        _fields_ = [
            ("pbsi_pid", ctypes.c_uint32),
            ("pbsi_ppid", ctypes.c_uint32),
            ("pbsi_pgid", ctypes.c_uint32),
            ("pbsi_status", ctypes.c_uint32),
            ("pbsi_comm", ctypes.c_char * 16),
        ]

    PROC_BSDSHORTINFO_SIZE = ctypes.sizeof(proc_bsdshortinfo)

    def get_name(pid):
        buf = ctypes.create_string_buffer(256)
        n = libproc.proc_name(pid, buf, 256)
        if n <= 0:
            return None
        try:
            return buf.value.decode("utf-8")
        except UnicodeDecodeError:
            return buf.value.decode("utf-8", errors="replace")

    def get_exe(pid):
        buf = ctypes.create_string_buffer(PROC_PIDPATHINFO_MAXSIZE)
        n = libproc.proc_pidpath(pid, buf, PROC_PIDPATHINFO_MAXSIZE)
        if n <= 0:
            return None
        try:
            return buf.value.decode("utf-8")
        except UnicodeDecodeError:
            return buf.value.decode("utf-8", errors="replace")

    def get_ppid(pid):
        info = proc_bsdshortinfo()
        n = libproc.proc_pidinfo(
            pid, PROC_PIDT_SHORTBSDINFO, 0,
            ctypes.byref(info), PROC_BSDSHORTINFO_SIZE,
        )
        if n != PROC_BSDSHORTINFO_SIZE:
            return 0
        return info.pbsi_ppid

    chain = []
    pid = os.getpid()
    seen = set()
    for _ in range(depth + 1):
        if pid in seen or pid <= 0:
            break
        seen.add(pid)
        name = get_name(pid)
        if name is None:
            break
        chain.append({"pid": pid, "name": name, "exe": get_exe(pid)})
        try:
            ppid = get_ppid(pid)
        except Exception:
            break
        if ppid <= 0 or ppid == pid:
            break
        pid = ppid
    return chain


def _get_ancestor_processes_macos_ps(depth: int):
    """macOS 兜底：用 ps 命令（沙箱里可能被禁）。"""
    import subprocess

    def lookup(pid):
        try:
            out = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "pid=,comm="],
                stderr=subprocess.DEVNULL, text=True,
            ).strip()
            parts = out.split(None, 1)
            if len(parts) == 2:
                return parts[1]
        except (subprocess.CalledProcessError, FileNotFoundError, PermissionError):
            pass
        return None

    def get_ppid(pid):
        try:
            out = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "ppid="],
                stderr=subprocess.DEVNULL, text=True,
            ).strip()
            return int(out)
        except (subprocess.CalledProcessError, FileNotFoundError, PermissionError, ValueError):
            return 0

    chain = []
    pid = os.getpid()
    seen = set()
    for _ in range(depth + 1):
        if pid in seen or pid == 0:
            break
        seen.add(pid)
        name = lookup(pid)
        if name is None:
            break
        chain.append({"pid": pid, "name": name, "exe": None})
        ppid = get_ppid(pid)
        if ppid == 0 or ppid == pid:
            break
        pid = ppid
    return chain


def _get_ancestor_processes(depth: int = _ANCESTOR_DEPTH):
    """拿当前进程向上 N 层。psutil 优先，纯标准库兜底。"""
    try:
        import psutil  # type: ignore  # noqa
        return _get_ancestor_processes_psutil(depth)
    except ImportError:
        return _get_ancestor_processes_stdlib(depth)


def _detect_agent_from_process(
    chain: Optional[List[Dict]] = None,
) -> Optional[Tuple[str, Optional[Callable], Optional[str]]]:
    """
    在路径未命中时，回退到进程链识别（比环境变量更可靠）。

    检查每个祖先进程的 name/exe 是否包含 _AGENT_RULES.keyword。
    按 _AGENT_RULES 顺序匹配第一个命中的规则。

    ★ ClawX 特殊处理：路径场景下靠 keyword='openclaw' + extra_check 区分；
      进程链场景下没有文件系统可校验 extra_check，因此改用 'clawx' 关键字，
      避免与 open_claw 冲突。

    Args:
        chain: 进程链 list；None 时自动获取

    Returns:
        命中时返回 (code, reader, source) 三元组；未命中返回 None。
    """
    if chain is None:
        try:
            chain = _get_ancestor_processes()
        except Exception as e:
            _log_warning(f"获取进程链失败: {e}")
            return None

    if not chain:
        return None

    # 把每个进程的 name + exe 拼一起(归一化)用于关键字匹配
    candidates = []
    for entry in chain:
        name = (entry.get("name") or "").lower()
        exe = (entry.get("exe") or "").lower()
        candidates.append(name)
        candidates.append(exe)

    all_text = " ".join(candidates)

    for rule in _AGENT_RULES:
        keyword = rule.get("keyword")
        if not keyword:
            continue
        # ★ claw_x 在进程链场景下用 'clawx' 关键字(避免与 open_claw 冲突)
        if str(rule.get("code")) == "claw_x":
            kw_lower = "clawx"
        else:
            kw_lower = str(keyword).lower()

        if kw_lower in all_text:
            return (
                str(rule["code"]),
                rule.get("instance_reader"),
                rule.get("instance_source"),
            )

    return None


# ============================================================
# 环境变量回退识别（路径未命中时使用）
# ============================================================

# 通用系统环境变量关键字（大小写不敏感）。这些是 OS / Shell 自身注入的，
# 与智能体无关，扫描时跳过避免误匹配。
_SYSTEM_ENV_KEYWORDS = frozenset({
    # 跨平台通用
    "path", "home", "user", "username", "logname", "shell", "lang",
    "term", "pwd", "oldpwd", "hostname", "shlvl", "term_program",
    # Windows
    "systemroot", "windir", "programdata", "programfiles",
    "programfiles(x86)", "programw6432", "commonprogramfiles",
    "commonprogramw6432", "comspec", "pathext", "os", "processor_*",
    "userprofile", "homedrive", "homepath", "appdata", "localappdata",
    "temp", "tmp", "psmodulepath", "userdomain", "logonserver",
    "sessionname", "public", "allusersprofile", "commonprogramfiles(x86)",
    "driverdata", "onedrive", "platform", "computername", "number_of_processors",
    "processor_architecture", "processor_identifier", "processor_level",
    "processor_revision", "systemdrive", "systemroot",
    # Unix / macOS
    "xdg_session_*", "xdg_runtime_dir", "xdg_config_dirs", "xdg_data_dirs",
    "xdg_cache_home", "xdg_config_home", "xdg_data_home", "xdg_state_home",
    "xdg_current_desktop", "xdg_session_type", "xdg_session_id",
    "xdg_seat", "xdg_vtnr", "xdg_desktop_session",
    "mail", "editor", "visual", "browser", "colorterm", "ls_colors",
    "lc_*", "tmux", "tmux_pane", "wgetrc", "curl_ca_bundle", "manpath",
    "infopath", "ld_library_path", "ld_preload", "ld_config",
    "library_path", "c_include_path", "cplus_include_path",
    "display", "wayland_display", "dbus_session_bus_address",
    "ssh_auth_sock", "ssh_agent_pid", "ssh_connection",
    # Node.js / Python 运行时（无关智能体）
    "node_*", "npm_*", "python*", "pip*", "virtual_env", "conda_*",
    "nvm_*", "pyenv_*", "rbenv_*", "rust_*", "cargo_*", "go_*",
})


def _is_system_env_key(key: str) -> bool:
    """
    判断一个环境变量 key 是否属于通用系统变量（需要跳过）。

    - 大小写不敏感
    - 支持通配符后缀（以 _* 结尾的为前缀匹配，如 PROCESSOR_*）
    - 同时检查 key 是否本身就是关键字
    """
    if not key:
        return True
    k_lower = key.lower()
    if k_lower in _SYSTEM_ENV_KEYWORDS:
        return True
    for kw in _SYSTEM_ENV_KEYWORDS:
        if kw.endswith("_*"):
            prefix = kw[:-2]
            if k_lower.startswith(prefix + "_") or k_lower == prefix:
                return True
    return False


def _filter_system_env(env: Dict[str, str]) -> Dict[str, str]:
    """
    从环境变量字典中过滤掉通用系统变量。
    """
    return {k: v for k, v in env.items() if not _is_system_env_key(k)}


def _is_value_verifiable(key: str, value: str) -> bool:
    """
    判断 env 值是否可二次验证(路径存在 / PID 存活 / named pipe 存在)。

    用于"过半制"投票的可验证加权——值能验证的命中算 1.5 票。
    key 名含 EXECPATH / HOME / DIR / SOCKET 等关键字视为可路径验证,
    含 PID 视为可进程存活验证。
    """
    if not value:
        return False
    key_upper = key.upper()
    try:
        if "EXECPATH" in key_upper or "HOME" in key_upper or "DIR" in key_upper:
            return os.path.exists(value)
        if "PID" in key_upper and str(value).isdigit():
            # ★ Windows 上 os.kill(pid, 0) 自身及其底层实现偶发抛 SystemError
            #   (CPython issue 10080 / 系统调用回归)。用 subprocess + psutil 不行 —
            #   这里要 0 依赖。改用 NT 上更稳的 OpenProcess 关掉 handle:
            #   OpenProcess 失败(PID 不存在 / 权限不足)返回 0/None,→ 视为不可验证。
            if sys.platform == "win32":
                import ctypes
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                PROCESS_QUERY_INFORMATION = 0x0400
                # 先试 LIMITED(普通权限),失败再退到 INFORMATION(需要权限)。
                # 两边都返回 0 => 进程不存在 / 无权访问。
                h1 = ctypes.windll.kernel32.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION, False, int(value))
                if h1:
                    ctypes.windll.kernel32.CloseHandle(h1)
                    return True
                h2 = ctypes.windll.kernel32.OpenProcess(
                    PROCESS_QUERY_INFORMATION, False, int(value))
                if h2:
                    ctypes.windll.kernel32.CloseHandle(h2)
                    return True
                return False
            else:
                os.kill(int(value), 0)
                return True
        if "SOCKET" in key_upper:
            return os.path.exists(value)
    except (OSError, ValueError):
        return False
    return False


def _match_agent_by_env_voting(filtered_env: Dict[str, str]) -> Optional[Dict]:
    """
    群体投票:每个配了 env_signature_keys 的 agent,至少过半命中才算。

    阈值:
      - 命中数 / 总特征数 >= 0.5
      - 命中数 >= 2(防白名单只有 1-2 个的 agent 靠 50% 误判)
    加权:值可验证的命中算 1.5 票(_is_value_verifiable 判定)
    冲突:加权分高者优先 → 命中数多者优先 → _AGENT_RULES 顺序

    Args:
        filtered_env: 已过滤系统变量的环境变量 dict

    Returns:
        命中时返回 {"rule", "hit_count", "total", "weighted", "verifiable_count", "hits"}
        未命中返回 None
    """
    candidates = []
    for rule in _AGENT_RULES:
        sig_keys = rule.get("env_signature_keys")
        if not sig_keys:
            continue  # 没配白名单 → 走旧逻辑,不在投票范围内

        sig_set = {k.upper() for k in sig_keys}
        hits = []
        verifiable_count = 0
        for k, v in filtered_env.items():
            if k.upper() in sig_set and v:
                hits.append(k)
                if _is_value_verifiable(k, str(v)):
                    verifiable_count += 1

        hit_count = len(hits)
        total = len(sig_keys)
        if hit_count == 0:
            continue

        # 加权:可验证命中算 1.5 票
        weighted = hit_count + 0.5 * verifiable_count
        ratio = weighted / total

        if ratio >= 0.5 and hit_count >= 2:
            candidates.append({
                "rule": rule,
                "hit_count": hit_count,
                "verifiable_count": verifiable_count,
                "weighted": weighted,
                "ratio": ratio,
                "hits": hits,
            })

    if not candidates:
        return None

    # 冲突仲裁:加权分 > 命中数 > _AGENT_RULES 顺序
    candidates.sort(key=lambda c: (
        -c["weighted"],
        -c["hit_count"],
        -_AGENT_RULES.index(c["rule"]),
    ))
    return candidates[0]


def _detect_agent_from_env(env: Optional[Dict[str, str]] = None) -> Optional[Tuple[str, Optional[Callable], Optional[str]]]:
    """
    在路径 + 进程链都未命中时,最后回退到环境变量识别。

    ★ 仅配了 env_signature_keys 白名单的 agent 会参与识别。
      未配白名单的 agent 不再做 env 兜底(2026-09-22 用户决策):
      用户选择让"路径 + 进程链"成为唯二可靠信号,
      env 子串匹配太容易被 PATH/用户名/URL 污染,即使 keyword 独特也存在漏报风险。
      经典案例:客户的 `HERMES_HOME` / `HERMES.GIT BASH PATH` 被误识别为 hermes
      — 旧逻辑即使没配白名单,也会走 keyword 子串匹配,产生误报。

    所以本函数当前只做一件事:对配了白名单的 agent 跑"过半制"投票。
    命中则返回 (code, reader, source),未命中返回 None。

    ★ 返回 reader/source:让 _detect_agent 后续能按规则定义的 instance_source
      继续读取 instance_id(env 命中 ≠ SDK 部署在该智能体目录,但 deviceId
      文件通常仍然存在 ~/.xxx/identity/ 下,尝试读取没有副作用)。

    ★ 白名单是唯一过滤层:env 字典要么来自最小权限读取(只含白名单 key,
      没有任何系统变量),要么由调用方手工注入(白名单外 key 进 voting
      也只会撞不到 sig_set 被忽略)。因此本函数不再调用 _filter_system_env,
      与"最小必要权限"语义对齐到底。

    Args:
        env: 待扫描的环境变量字典;None 时使用 os.environ

    Returns:
        命中时返回 (code, reader, source);未命中返回 None。
        reader / source 可能为 None(云端 AI / 无设备文件的智能体)。
    """
    if env is None:
        try:
            # ★ 最小权限读取:只读 _AGENT_RULES env_signature_keys 并集,不再全量快照 os.environ
            env = {k: os.environ[k] for k in _ENV_SIGNATURE_KEYS if k in os.environ}
        except Exception:
            return None

    if not env:
        return None

    # ★ 仅对配了 env_signature_keys 白名单的 agent 走"过半制"群体投票
    vote = _match_agent_by_env_voting(env)
    if vote:
        rule = vote["rule"]
        _log_warning(
            f"环境变量过半命中: code={rule['code']} "
            f"命中 {vote['hit_count']}/{len(rule['env_signature_keys'])} "
            f"(可验证 {vote['verifiable_count']} 个, 加权 {vote['weighted']:.1f})"
        )
        return (
            str(rule["code"]),
            rule.get("instance_reader"),
            rule.get("instance_source"),
        )

    return None


def _detect_agent() -> Tuple[str, str, str]:
    """
    识别当前 SDK 部署的智能体。

    识别顺序：
      1) 按 SDK 部署路径匹配 _AGENT_RULES(L1)
      2) 路径未命中时,回退到进程链识别(L2/L3,name/exe 是否包含 keyword)
      3) 路径 + 进程链都未命中时,最后回退到环境变量识别
         ★ 仅配了 env_signature_keys 白名单的 agent 才参与 env 兜底;
           未配白名单的 agent 不再做 env 子串匹配(2026-09-22 用户决策,
           避免 PATH/用户名/URL 字符串碰巧含 keyword 导致的误识别)

    Returns:
        (agent_code, agent_platform_path, instance_id)

    未识别任何智能体时：返回 ("unknown", 当前脚本目录, "")
    """
    sdk_path = _script_dir()
    sdk_lower = sdk_path.lower()

    for rule in _AGENT_RULES:
        keyword = rule["keyword"]
        if not keyword or str(keyword).lower() not in sdk_lower:
            continue
        extra_check = rule["extra_check"]
        if extra_check is not None and not extra_check(sdk_path):
            continue
        # 命中
        if rule.get("replace_in_path"):
            path = _replace_openclaw_with_clawx(sdk_path)
        else:
            path = sdk_path
        # instance_id
        reader = rule["instance_reader"]
        source = rule["instance_source"]
        if reader is None or source is None:
            instance_id = ""
        else:
            instance_id = reader(_expand(source)) or ""
        return str(rule["code"]), path, instance_id

    # ★ 路径未命中:优先回退到进程链识别(比环境变量更可靠)
    proc_hit = _detect_agent_from_process()
    if proc_hit:
        proc_code, proc_reader, proc_source = proc_hit
        _log_warning(
            f"路径未识别智能体，回退到进程链识别: code={proc_code} (sdk_path={sdk_path})"
        )
        # 进程链命中后按规则定义的 instance_source 尝试读 instance_id
        if proc_reader is None or proc_source is None:
            instance_id = ""
        else:
            instance_id = proc_reader(_expand(proc_source)) or ""
        return proc_code, sdk_path, instance_id

    # ★ 进程链未命中:再回退到环境变量识别
    env_hit = _detect_agent_from_env()
    if env_hit:
        env_code, env_reader, env_source = env_hit
        _log_warning(
            f"路径和进程链都未识别智能体，回退到环境变量识别: code={env_code} (sdk_path={sdk_path})"
        )
        if env_reader is None or env_source is None:
            instance_id = ""
        else:
            instance_id = env_reader(_expand(env_source)) or ""
        return env_code, sdk_path, instance_id

    # 未识别：返回脚本路径 + 空 instanceId
    return "unknown", sdk_path, ""


def collect_agent_info() -> Dict[str, str]:
    """
    采集智能体平台信息。

    Returns:
        {
            "agentCode":       "open_claw",        # 内部用，便于日志/调试
            "agentPlatformPath": "/path/...",       # 发给服务端
            "instanceId":      "uuid",             # 发给服务端；无则 ""
            "agentPlatform":   "open_claw",        # 发给服务端：_AGENT_RULES 的 code，未识别则 ""
        }
    """
    code, path, instance_id = _detect_agent()
    return {
        "agentCode": code,
        "agentPlatformPath": path,
        "instanceId": instance_id,
        "agentPlatform": code if code and code != "unknown" else "",
    }


def detect_claw_uid() -> Optional[str]:
    """
    从当前智能体对应的实例 ID 文件读取 clawUid。

    ★ 与 _detect_agent() 共用同一套三级回退（路径 → 进程链 → 环境变量），
      保证 clawUid 与 agentPlatform 始终来自同一条 _AGENT_RULES：
      - 路径命中关键词 + extra_check 通过 → 用规则的 instance_reader 读 instance_source
      - 路径未命中、进程链命中 → 用进程链命中的 agent 的 instance_reader 读 instance_source
      - 进程链未命中、环境变量命中 → 用 env 命中的 agent 的 instance_reader 读 instance_source
      - 全部未命中 / instance_source 为 None / 文件不存在 → 返回 None
        （由 getToken 链路降级到 config.clawUid_path / 兜底 UUID 生成）
    """
    code, _path, instance_id = _detect_agent()
    if not instance_id:
        return None
    return instance_id
