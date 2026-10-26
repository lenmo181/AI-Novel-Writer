"""测试环境排障日志：CLI 链路事件 + curl 落盘。

仅当配置**显式开启**（`debug.enabled=true`）且**请求目标**命中固定测试域（常量
`test.yun.139.com`，**不可通过任何配置扩权**）时启用。正式/现网地址不在白名单，
因此永不启用，且不依赖任何人的操作。

纪律（必须保持）：
- 只写 stderr 与日志文件，绝不触碰 stdout：stdout 永远只有一个 JSON 信封；
- 任何内部异常都不外抛，日志绝不改变命令结果与退出码；
- 关闭态不装钩子、不建目录与文件、不做文件 IO。
- **落盘一律脱敏**：请求头按敏感词表掩码（只留可比对指纹），请求体与响应体
  都不落明文，命令行里的 `--confirm-token` 一类取值同样只留指纹；日志用于排障定位，
  不携带可用凭据。
- CLI 调用不自动删除旧日志；测试环境的日志由部署方按留存策略清理。

开关只有一个来源：控制面配置 `cmcloud.json` 里的 `debug` 段（与地址键同级）：

```json
{
  "base_url": "https://test.yun.139.com/ecloud/yun-test/open-ai-space",
  "personal_saas_base_url": "https://test.yun.139.com/ecloud/yun-test/open-ai-space",
  "debug": {
    "enabled": true,
    "quiet": false,
    "dir": "<专用日志目录>"
  }
}
```

- `enabled`  总开关，**必须显式 `true`** 才启用（省略或 `false` 一律关闭）；启用后仍受固定测试域约束，现网包永不开启；
- `quiet`    只落盘，不写 stderr；
- `dir`      日志目录，缺省为系统临时目录下的 cmcloud-debug。

旧配置中的 `bodies` 为兼容保留但不再生效；`hosts` 不再扩权（可记录范围只能是固定测试域
常量），出现时按未知项忽略并提示；请求体和响应体均不会写入旁路文件。

文件缺失或非法只影响本次开关判定，不影响命令结果。

落盘格式：每行一条 JSON 事件，固定头为 `ts`（UTC 毫秒）、`seq`（进程内递增）、
`pid`、`sid`、`event`、`elapsed_ms`（相对进程起点）。凡已建立的排障上下文
（`command_id`/`scenario_id`/`adapter`，随 `dispatch`、`route`、`adapter.*` 事件推进）
都会随**每条**事件一起写入，因此 `http.request`/`http.response` 这类在 HTTP 钩子里
发出的事件也自带命令与场景归属，不必靠 `seq` 反推；stderr 前缀取自同一份上下文。
另有 `line` 字段，取 stderr 摘要的**第一行**（如 `-> POST /path`、`<- 200 125ms
traceId=.. ok=True`），内容与终端输出逐字一致，`jq -r '.line'` 即可复现终端观感；
多行内容的其余部分不重复写入（curl 在 `curl` 字段）；HTTP 正文只记录长度和摘要，
不记录明文。

环境变量：
- `CMCLOUD_DEBUG_SESSION`  会话标识，用于串联同一轮提问的多次 CLI 调用（不是开关）。
"""

import hashlib
import http.client
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import weakref
from datetime import datetime, timezone


ENV_SESSION = "CMCLOUD_DEBUG_SESSION"

_SUBPROCESS_ERRORS = (OSError, subprocess.SubprocessError)

CONFIG_KEYS = ("enabled", "quiet", "dir", "bodies")
# 控制面配置 cmcloud.json 里承载调试开关的段名（与该文件的地址键同级）
PLATFORM_DEBUG_KEY = "debug"

# 每条落盘事件都随附的排障上下文字段，取值与 stderr 前缀同源
CONTEXT_KEYS = ("command_id", "scenario_id", "adapter")

DEFAULT_HOSTS = ("test.yun.139.com",)
ENV_HOST_SUFFIX_PREFIX = "."

LOG_FILE_PREFIX = "cmcloud-debug-"
LOG_FILE_SUFFIX = ".jsonl"
LOG_DIR_NAME = "cmcloud-debug"

BASE_KEYS = ("base_url", "personal_saas_base_url", "oauth_base_url", "qr_oauth_base_url")

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_PREVIEW_BYTES = 4 * 1024
MAX_HASH_BYTES = 1024 * 1024

_TRUTHY = ("1", "true")
_FALSY = ("0", "false")

_state = {
    "checked": False,
    "enabled": False,
    "quiet": False,
    "handle": None,
    "path": None,
    "dir": None,
    "seq": 0,
    "sid": None,
    "t0": 0.0,
    "broken": False,
    "installed": False,
    "context": {},
    "settings": None,
    "notes": [],
}

# 安装钩子时保存的原始方法引用；finish/reset 据此还原，避免同进程批处理重复包装。
_hook_originals = {}
_pending_requests = weakref.WeakKeyDictionary()
_responses = weakref.WeakKeyDictionary()


# ---------------------------------------------------------------------------
# 门禁
# ---------------------------------------------------------------------------


def reset():
    """清空进程内状态（供测试与同进程重复判定使用）。"""
    handle = _state["handle"]
    if handle is not None:
        try:
            handle.close()
        except Exception:
            pass
    _uninstall_hooks()
    _pending_requests.clear()
    _responses.clear()
    _state.update(
        checked=False,
        enabled=False,
        quiet=False,
        handle=None,
        path=None,
        dir=None,
        seq=0,
        sid=None,
        t0=0.0,
        broken=False,
        installed=False,
        context={},
        settings=None,
        notes=[],
    )


def is_enabled():
    """测试域命中**且配置显式开启**时为真；判定结果进程内缓存一次。

    必须显式 `debug.enabled=true`：仅凭"地址指向测试域"不再自动开日志——否则仓库里
    一份面向测试域的配置就会默认把请求头与链路写进临时目录，等于默认泄露凭据。
    """
    if _state["checked"]:
        return _state["enabled"]
    _state["checked"] = True
    _state["enabled"] = False
    switch = _switch()
    if switch != "1":
        return False
    try:
        config = _load_config()
    except Exception:
        return False
    hosts = _allowed_hosts()
    checked = 0
    for name in BASE_KEYS:
        value = getattr(config, name, None)
        if not isinstance(value, str) or not value:
            continue
        checked += 1
        host = _host_of(value)
        if host and any(_host_matches(host, entry) for entry in hosts):
            continue
        _warn(
            "[cmcloud-debug] 已忽略：当前 %s 非测试域，debug 仅在测试环境可用" % name
        )
        return False
    if checked == 0:
        return False
    _state["enabled"] = True
    _state["quiet"] = _as_on(_setting("quiet"))
    _flush_notes()
    return True


def _switch():
    """开关归一：只读 cmcloud.json 的 debug 段；`"1"` 才算开启，其余（含未声明）一律关闭。"""
    return _as_flag(_setting("enabled"))


def _as_flag(value):
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text in _FALSY:
        return "0"
    if text in _TRUTHY:
        return "1"
    return None


def _as_on(value):
    return _as_flag(value) == "1"


def _setting(name):
    """取值只有一个来源：cmcloud.json 的 debug 段（见 `_platform_settings()`）。"""
    return _load_settings().get(name)


def _platform_settings():
    """控制面配置 `cmcloud.json` 里的 `debug` 段（与地址同文件；缺失/非法即视为空）。"""
    try:
        config = _load_config()
        path = config.config_path
    except Exception:
        return {}
    if not isinstance(path, str) or not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        _note("cmcloud.json 不可读取或不是合法 JSON，其 debug 段已忽略：%s" % path)
        return {}
    if not isinstance(document, dict):
        return {}
    section = document.get(PLATFORM_DEBUG_KEY)
    if section is None:
        return {}
    if not isinstance(section, dict):
        _note("cmcloud.json 的 debug 段必须是对象，已忽略")
        return {}
    resolved = {}
    for key, value in section.items():
        if key in CONFIG_KEYS:
            resolved[key] = value
        else:
            _note("cmcloud.json 的 debug 段存在未知项 %s，已忽略" % key)
    return resolved


def _load_settings():
    """读取 cmcloud.json 的 debug 段，进程内缓存一次。"""
    if _state["settings"] is not None:
        return _state["settings"]
    _state["settings"] = _platform_settings()
    return _state["settings"]


def _note(text):
    """把配置提示挂起，直到确认在测试域启用后才输出，避免现网刷屏。"""
    _state["notes"].append("[cmcloud-debug] %s" % text)


def _flush_notes():
    for text in _state["notes"]:
        _warn(text)
    _state["notes"] = []


def _env(name):
    try:
        return os.environ.get(name)
    except Exception:
        return None


def _load_config():
    from cmcloud_core import platform_config

    return platform_config.load_platform_config()


def _allowed_hosts():
    """可记录目标域：仅固定测试域常量，不接受任何配置扩权。"""
    return tuple(DEFAULT_HOSTS)


def _is_allowed_url(url):
    """请求目标是否属于可记录测试域：按解析后的 hostname 精确/子域匹配。

    路径或查询里出现测试域不算命中；`test.yun.139.com.evil.example` 这类伪造后缀、
    以及解析失败/空主机都判为不可记录。
    """
    host = _host_of(url)
    if not host:
        return False
    return any(_host_matches(host, entry) for entry in _allowed_hosts())


def _host_of(url):
    try:
        return (urllib.parse.urlsplit(url).hostname or "").strip().lower()
    except (AttributeError, ValueError):
        return ""


def _path_of(url):
    try:
        parsed = urllib.parse.urlsplit(url or "")
    except (AttributeError, ValueError):
        return url
    path = parsed.path or "/"
    return "%s?%s" % (path, parsed.query) if parsed.query else path


def _absolute_url(connection, url):
    """补全 urllib 只给 path 的请求地址：`https://<host>[:<port>]<path>`。"""
    text = str(url or "")
    if text.startswith(("http://", "https://")):
        return text
    scheme = "https" if isinstance(connection, http.client.HTTPSConnection) else "http"
    host = getattr(connection, "host", None)
    if not host:
        return text
    port = getattr(connection, "port", None)
    default_port = 443 if scheme == "https" else 80
    netloc = host if port in (None, default_port) else "%s:%s" % (host, port)
    path = text if text.startswith("/") else "/" + text
    return "%s://%s%s" % (scheme, netloc, path)


def _host_matches(host, entry):
    host = (host or "").lower()
    entry = (entry or "").lower()
    if not host or not entry:
        return False
    if entry.startswith(ENV_HOST_SUFFIX_PREFIX):
        return host.endswith(entry)
    return host == entry


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


def _uninstall_hooks():
    """还原本模块安装的 HTTP 包装（若已安装）；重复调用安全。"""
    originals = dict(_hook_originals)
    _hook_originals.clear()
    if not originals:
        return
    try:
        http.client.HTTPSConnection.request = originals["http_request"]
        http.client.HTTPSConnection.getresponse = originals["http_getresponse"]
        http.client.HTTPResponse.read = originals["http_read"]
    except Exception:
        pass
    try:
        session_class = originals.get("requests_session")
        send = originals.get("requests_send")
        if session_class is not None and send is not None:
            session_class.send = send
    except Exception:
        pass


def install(argv):
    """每个 CLI 进程的最外层调用：装钩子、开日志文件、写 cli.start。"""
    if _state["installed"]:
        return
    if not is_enabled():
        return
    _state["installed"] = True
    _state["t0"] = time.monotonic()
    _state["sid"] = _env(ENV_SESSION) or _new_sid()
    configured_dir = _setting("dir")
    _state["dir"] = (
        configured_dir
        if isinstance(configured_dir, str) and configured_dir.strip()
        else _default_log_dir()
    )
    try:
        os.environ[ENV_SESSION] = _state["sid"]
    except Exception:
        pass
    try:
        _open_log_file()
    except OSError:
        _disable("日志路径不可安全使用，已停止记录")
    if _state["broken"]:
        return
    _install_http_client_hook()
    _install_requests_hook()
    try:
        config = _load_config()
        base_host = _host_of(getattr(config, "base_url", "") or "")
    except Exception:
        base_host = None
    emit(
        "cli.start",
        argv=_redact_argv(argv),
        cwd=_safe_cwd(),
        python=platform.python_version(),
        profile=_profile_of(argv),
        base_host=base_host,
        debug_dir=_state["dir"],
        skill_root=_skill_root(),
    )
    if not _state["quiet"]:
        _warn("[cmcloud-debug] on sid=%s log=%s" % (_state["sid"], _state["path"]))


def finish(exit_code):
    """写 cli.end 并关闭文件；响应体未被读取时补一条 http.response。"""
    if _state["enabled"] and not _state["broken"]:
        try:
            _flush_unread_response()
            emit("cli.end", exit_code=exit_code, total_ms=_ms(_state["t0"]))
        except Exception:
            pass
    # session 标识由 ENV_SESSION 保留；故障状态、配置和句柄不能跨命令残留。
    reset()


def emit(event, **fields):
    """写一条事件并按需打印 stderr 摘要；任何内部异常都不外抛。"""
    if not _state["enabled"] or _state["broken"]:
        return
    try:
        _state["seq"] += 1
        record = {
            "ts": _now(),
            "seq": _state["seq"],
            "pid": os.getpid(),
            "sid": _state["sid"],
            "event": event,
            "elapsed_ms": _ms(_state["t0"]),
        }
        for key, value in fields.items():
            record[key] = _jsonable(value)
        _remember_context(event, record)
        for key, value in _context_fields().items():
            record.setdefault(key, value)
        try:
            lines = _stderr_lines(event, record)
        except Exception:
            lines = ()
        if lines:
            record["line"] = lines[0]
        handle = _state["handle"]
        if handle is not None:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
    except Exception:
        _disable("日志写入失败，已停止记录")
        return
    if _state["quiet"]:
        return
    try:
        for line in lines:
            _warn(line)
    except Exception:
        pass


def _remember_context(event, record):
    context = _state["context"]
    if event == "route":
        context["command_id"] = record.get("command_id")
        context["scenario_id"] = record.get("scenario_id")
    elif event == "dispatch":
        context["command_id"] = record.get("command_id")
    elif event in ("adapter.start", "adapter.end", "adapter.error"):
        context["adapter"] = record.get("action")


def _context_fields():
    """当前排障上下文：与 stderr 前缀同源，落盘时随每条事件一起写入。

    这样 `http.request`/`http.response` 这类在 HTTP 钩子里发出的事件也自带
    命令、场景与 adapter 归属，不必靠 `seq` 夹在哪个 `adapter.start` 之间反推。
    """
    context = _state["context"]
    fields = {}
    for key in CONTEXT_KEYS:
        value = context.get(key)
        if value:
            fields[key] = value
    return fields


def _context_prefix():
    fields = _context_fields()
    parts = [fields.get("command_id") or "cmcloud"]
    for key in ("scenario_id", "adapter"):
        value = fields.get(key)
        if value:
            parts.append(value)
    return " ".join(str(item) for item in parts if item)


# ---------------------------------------------------------------------------
# stderr 呈现
# ---------------------------------------------------------------------------


def _stderr_lines(event, record):
    if event == "cli.start":
        return ()
    prefix = _context_prefix()
    if event == "http.request":
        lines = [
            "[cmcloud-debug] %s -> %s %s" % (prefix, record.get("method"), _path_of(record.get("url")))
        ]
        curl = record.get("curl")
        if curl:
            lines.append(curl)
        return tuple(lines)
    if event == "http.response":
        ok = record.get("ok")
        code = record.get("code")
        line = "[cmcloud-debug] %s <- %s %sms traceId=%s ok=%s%s" % (
            prefix,
            record.get("status"),
            record.get("duration_ms"),
            record.get("trace_id") or "-",
            ok,
            "" if code is None else " code=%s" % code,
        )
        return (line,)
    if event == "adapter.error":
        return (
            "[cmcloud-debug] %s adapter 失败 code=%s %sms" % (prefix, record.get("code"), record.get("duration_ms")),
        )
    if event == "run.error":
        return (
            "[cmcloud-debug] %s 终止于 %s：%s" % (prefix, record.get("code"), record.get("message")),
        )
    if event == "envelope":
        return (
            "[cmcloud-debug] %s 结果 status=%s exit=%s nextAction=%s%s"
            % (
                prefix,
                record.get("status"),
                record.get("exit_code"),
                record.get("nextAction"),
                "" if not record.get("error") else " error=%s" % (record["error"] or {}).get("code"),
            ),
        )
    return ()


def _warn(text):
    try:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _disable(reason):
    _state["broken"] = True
    handle = _state["handle"]
    if handle is not None:
        try:
            handle.close()
        except Exception:
            pass
        _state["handle"] = None
    _uninstall_hooks()
    _pending_requests.clear()
    _responses.clear()
    _warn("[cmcloud-debug] %s" % reason)


# ---------------------------------------------------------------------------
# 日志文件
# ---------------------------------------------------------------------------


def _open_log_file():
    base = "%s%s" % (LOG_FILE_PREFIX, _file_sid())
    try:
        os.makedirs(_state["dir"], exist_ok=True)
    except OSError:
        _disable("日志写入失败，已停止记录")
        return
    # 先收紧目录并在写入任何事件前确认成功，避免"先写后 chmod"的假性安全。
    if not _restrict_directory(_state["dir"]):
        _disable("日志目录权限收紧失败，已停止记录")
        return
    path = os.path.join(_state["dir"], base + LOG_FILE_SUFFIX)
    index = 0
    try:
        while os.path.exists(path) and os.path.getsize(path) >= MAX_FILE_BYTES:
            index += 1
            path = os.path.join(_state["dir"], "%s-%d%s" % (base, index, LOG_FILE_SUFFIX))
        handle = open(path, "a", encoding="utf-8")
    except OSError:
        _disable("日志写入失败，已停止记录")
        return
    if not _restrict(path, 0o600):
        try:
            handle.close()
        except OSError:
            pass
        _disable("日志文件权限收紧失败，已停止记录")
        return
    _state["handle"] = handle
    _state["path"] = path


def _restrict(path, mode=0o600):
    """把单一路径收窄到当前用户，返回是否确认成功。

    POSIX 用 chmod；Windows 的 `os.chmod` 只影响只读位、不改 ACL，必须用 `icacls`
    断开继承（口径与 `core_operation_ledger._restrict_directory` 一致）。授权主体优先用
    SID，避免名字解析不到时写成空主体、把本机自己挡在日志之外。取不到主体或命令返回
    非 0 时返回 False，调用方据此停止落盘，不把"尽力收紧"当成安全保证。
    """
    if os.name == "posix":
        try:
            os.chmod(path, mode)
        except OSError:
            return False
        return True
    if os.name != "nt":
        return True
    principal = _current_user_sid()
    if not principal:
        return False
    return _apply_acl(path, principal)


def _restrict_directory(path):
    """沿用原目录权限处理范围；返回结果供日志权限失败时停止落盘。"""
    if not _restrict(path, 0o700):
        return False
    if os.name != "nt":
        return True
    try:
        names = os.listdir(path)
    except OSError:
        return False
    for name in names:
        if not _restrict(os.path.join(path, name)):
            return False
    return True


def _apply_acl(target, principal):
    try:
        completed = subprocess.run(
            ["icacls", target, "/inheritance:r", "/grant:r", _grant_spec(target, principal)],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except _SUBPROCESS_ERRORS:
        return False
    return getattr(completed, "returncode", 1) == 0


def _grant_spec(target, principal):
    """目录授权串带 (OI)(CI) 供子项继承；文件只能用 `F`。

    对文件套 `(OI)(CI)F` 时 icacls 返回 0 但会**清空该文件的 DACL**（实测：之后连读取
    都被拒），因此文件必须与目录分开处理。
    """
    return "%s:%s" % (principal, "(OI)(CI)F" if os.path.isdir(target) else "F")


def _default_log_dir():
    """缺省日志目录：系统临时目录下的技能子目录。"""
    return os.path.join(tempfile.gettempdir(), LOG_DIR_NAME)


def _current_user_sid():
    """受限授权主体：优先 SID（`*S-1-5-...`），退回 `DOMAIN\\账号` / 账号名。"""
    sid = _whoami_sid()
    if sid:
        return "*" + sid
    user = _current_user()
    if not user:
        return None
    domain = _env("USERDOMAIN")
    if isinstance(domain, str) and domain.strip():
        return "%s\\%s" % (domain.strip(), user)
    return user


def _whoami_sid():
    """从 `whoami /user` 解析当前账号 SID；取不到返回 None。"""
    try:
        completed = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except _SUBPROCESS_ERRORS:
        return None
    if getattr(completed, "returncode", None) != 0:
        return None
    match = re.search(r"S-1-[0-9-]+", getattr(completed, "stdout", None) or "")
    return match.group(0) if match else None


def _current_user():
    """当前 OS 账号名；取不到返回 None（此时不假装已收紧 ACL）。"""
    for name in ("USERNAME", "USER"):
        value = _env(name)
        if value and str(value).strip():
            return str(value).strip()
    return None


# ---------------------------------------------------------------------------
# HTTP 钩子：urllib（全部 adapter）
# ---------------------------------------------------------------------------


def _install_http_client_hook():
    if "http_request" in _hook_originals:
        return
    original_request = http.client.HTTPSConnection.request
    original_getresponse = http.client.HTTPSConnection.getresponse
    original_read = http.client.HTTPResponse.read
    _hook_originals.update(http_request=original_request, http_getresponse=original_getresponse,
                           http_read=original_read)

    def patched_request(self, method, url, body=None, headers=None, *, encode_chunked=False):
        # Each connection owns its pending request; a blocked connection never clears
        # another connection's response, nor borrows its context.
        _pending_requests.pop(self, None)
        try:
            absolute = _absolute_url(self, url)
            if _is_allowed_url(absolute):
                safe_url = _redact_url(absolute)
                header_map = {str(k): str(v) for k, v in (headers or {}).items()}
                _pending_requests[self] = {
                    "method": method, "url": safe_url, "started": time.monotonic(),
                }
                emit("http.request", method=method, url=safe_url,
                     headers=_redact_headers(header_map), body_bytes=_body_length(body),
                     body_sha256=_sha256(body), curl=build_curl(method, safe_url, header_map, body))
        except Exception:
            _pending_requests.pop(self, None)
        try:
            return original_request(self, method, url, body, headers, encode_chunked=encode_chunked)
        except Exception:
            _pending_requests.pop(self, None)
            raise

    def patched_getresponse(self):
        pending = _pending_requests.pop(self, None)
        response = original_getresponse(self)
        try:
            if pending and not _state["broken"]:
                _responses[response] = {
                    **pending, "status": response.status,
                    "trace_id": _header(response, "trace_id") or _header(response, "x-yun-tid"),
                    "reads": 0, "total": 0, "hash": hashlib.sha256(),
                    "hash_skipped": False, "preview": bytearray(), "reported": False,
                }
        except Exception:
            pass
        return response

    def patched_read(self, *args, **kwargs):
        data = original_read(self, *args, **kwargs)
        try:
            context = _responses.get(self)
            if context is not None:
                _consume_response(context, data)
        except Exception:
            pass
        return data

    http.client.HTTPSConnection.request = patched_request
    http.client.HTTPSConnection.getresponse = patched_getresponse
    http.client.HTTPResponse.read = patched_read


def _consume_response(context, data):
    raw = b"" if data is None else (data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8", "replace"))
    context["reads"] += 1
    context["total"] += len(raw)
    if context["total"] <= MAX_HASH_BYTES:
        context["hash"].update(raw)
    else:
        context["hash_skipped"] = True
    if len(context["preview"]) < MAX_PREVIEW_BYTES:
        room = MAX_PREVIEW_BYTES - len(context["preview"])
        context["preview"].extend(raw[:room])
    if context["reads"] > 1:
        return
    _report_response(context, first_chunk=raw)


def _report_response(context, first_chunk):
    context["reported"] = True
    status = context["status"]
    body = bytes(context["preview"])
    fields = {
        "method": context.get("method"),
        "url": context.get("url"),
        "status": status,
        "duration_ms": _ms(context["started"]) if context["started"] else None,
        "trace_id": context["trace_id"],
        "bytes": context["total"],
        "ok": 200 <= status < 300,
        "body_sha256": None if context.get("hash_skipped") else context["hash"].hexdigest(),
        "body_sha256_skipped": True if context.get("hash_skipped") else None,
        "body_logged": False,
    }
    fields.update(_safe_response_fields(body))
    emit("http.response", **fields)


def _flush_unread_response():
    for context in list(_responses.values()):
        if context["reported"]:
            continue
        context["reported"] = True
        status = context["status"]
        emit("http.response", method=context.get("method"), url=context.get("url"),
             status=status, duration_ms=_ms(context["started"]) if context["started"] else None,
             trace_id=context["trace_id"], bytes=context["total"],
             ok=200 <= status < 300, body_logged=False)
    _responses.clear()


# ---------------------------------------------------------------------------
# HTTP 钩子：requests（IDaaS 授权域，该目录只读，不改其源码）
# ---------------------------------------------------------------------------


def _install_requests_hook():
    if "requests_send" in _hook_originals:
        return
    try:
        import requests
    except Exception:
        try:
            emit("hook.skipped", hook="requests", reason="requests 不可导入")
        except Exception:
            pass
        return
    try:
        original_send = requests.sessions.Session.send
        _hook_originals["requests_session"] = requests.sessions.Session
        _hook_originals["requests_send"] = original_send

        def patched_send(self, request, **kwargs):
            started = time.monotonic()
            try:
                allowed = _is_allowed_url(request.url)
            except Exception:
                allowed = False
            if allowed:
                try:
                    header_map = {str(k): str(v) for k, v in (request.headers or {}).items()}
                    body = request.body
                    safe_url = _redact_url(request.url)
                    emit(
                        "http.request",
                        method=request.method,
                        url=safe_url,
                        headers=_redact_headers(header_map),
                        body_bytes=_body_length(body),
                        body_sha256=_sha256(body),
                        curl=build_curl(request.method, safe_url, header_map, body),
                    )
                except Exception:
                    pass
            try:
                response = original_send(self, request, **kwargs)
            except Exception as err:
                if allowed:
                    emit("http.response", ok=False, error=err.__class__.__name__, duration_ms=_ms(started))
                raise
            if not allowed:
                return response
            # Session.send may follow redirects; never inspect a final response
            # from a disallowed host merely because the initial URL was allowed.
            if not _is_allowed_url(getattr(response, "url", request.url)):
                return response
            try:
                content = response.content or b""
            except Exception:
                content = b""
            try:
                fields = {
                    "status": response.status_code,
                    "duration_ms": _ms(started),
                    "trace_id": response.headers.get("trace_id") or response.headers.get("x-yun-tid"),
                    "bytes": len(content),
                    "ok": 200 <= response.status_code < 300,
                    "body_sha256": _sha256(content),
                    "body_logged": False,
                }
                fields.update(_safe_response_fields(content))
                emit("http.response", **fields)
            except Exception:
                pass
            return response

        requests.sessions.Session.send = patched_send
    except Exception:
        try:
            emit("hook.skipped", hook="requests", reason="钩子安装失败")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 敏感信息脱敏
# ---------------------------------------------------------------------------

# 命中即视为凭据：与 auth_idaas_common 的脱敏词表同源，另补 `auth`（Authorization /
# Proxy-Authorization 这类只带方案名、值却是裸票据的头）与 `cookie`、设备身份 clawUid。
_SENSITIVE_KEY_PATTERN = re.compile(
    r"token|secret|password|passwd|apikey|api_key|cookie|sign|ticket|credit|clawuid|auth|credential",
    re.IGNORECASE,
)
_MASK_MARK = "***#"


def _is_sensitive_key(name):
    """键名命中敏感词表即为真；判定自身异常时按敏感处理（fail-closed）。"""
    try:
        return bool(_SENSITIVE_KEY_PATTERN.search(str(name)))
    except Exception:
        return True


def _mask_secret(value):
    """凭据掩码：只留稳定指纹，可跨条日志比对，但无法还原原值。"""
    text = "" if value is None else str(value)
    if not text:
        return text
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:8]
    return "%s%s" % (_MASK_MARK, digest)


_URL_SECRET_PARAMS = frozenset(
    (
        "token", "access_token", "refresh_token", "id_token", "accesstoken", "refreshtoken",
        "ticket", "st", "sign", "signature", "password", "passwd", "pwd", "secret",
        "client_secret", "clientsecret", "credential", "credentials", "auth", "authorization",
        "code", "authcode", "auth_code", "key", "apikey", "api_key", "accesskey", "access_key",
        "secretkey", "secret_key", "session", "sessionid", "session_id", "nonce",
        "clawuid", "claw_uid", "credittoken", "uuid", "uuid_param", "deviceid",
    )
)
_PATH_SECRET_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]{20,}$")


def _is_secret_param(name):
    """Normalize separator/case variants; header rules also cover camel-case tokens."""
    text = str(name or "").strip().lower()
    compact = re.sub(r"[^a-z0-9]", "", text)
    return (bool(text) and (
        _is_sensitive_key(compact)
        or compact in {re.sub(r"[^a-z0-9]", "", key) for key in _URL_SECRET_PARAMS}
        or any(token in _URL_SECRET_PARAMS for token in re.split(r"[^a-z0-9]+", text))
    ))


def _url_mask(value):
    """URL 用掩码：只留稳定指纹，且不含 `#`（避免被误当 fragment 而截断）。"""
    text = "" if value is None else str(value)
    if not text:
        return text
    if re.fullmatch(r"\*\*\*[0-9a-f]{8}", text):
        return text
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:8]
    return "***%s" % digest


def _redact_path(path):
    """Mask ticket positions by route semantics, including encoded/short values.

    Repeatedly encoded segments are masked conservatively rather than decoded into
    a different URL path. Long opaque IDs remain masked as a secondary safeguard.
    """
    segments = (path or "").split("/")
    sensitive_tail = False
    for index, segment in enumerate(segments):
        decoded = urllib.parse.unquote(segment)
        if sensitive_tail or decoded != segment or _PATH_SECRET_SEGMENT.fullmatch(decoded):
            segments[index] = _url_mask(segment)
        # Singular/plural route names: /token/<value>, /tickets/<value>, etc.
        if _is_secret_param(decoded) or _is_secret_param(decoded.rstrip("s")):
            sensitive_tail = True
    return "/".join(segments)


def _redact_query(query):
    """清洗查询串：敏感参数名（忽略大小写）取值只留指纹，别的一并保留。"""
    text = query or ""
    if not text:
        return text
    try:
        pairs = urllib.parse.parse_qsl(text, keep_blank_values=True)
    except ValueError:
        return _url_mask(text)
    cleaned = [
        (name, _url_mask(value) if "%" in name or _is_secret_param(name) else value)
        for name, value in pairs
    ]
    return urllib.parse.urlencode(cleaned)


def _redact_url(url):
    """统一 URL 清洗：去掉 userinfo 与 fragment，掩码敏感查询参数与疑似票据路径段。

    只用于日志落点；实际网络请求仍使用原 URL，日志清洗不改变业务请求。
    """
    text = str(url or "")
    if not text:
        return text
    try:
        parts = urllib.parse.urlsplit(text)
        host = parts.hostname or ""
        netloc = "[%s]" % host if ":" in host else host
        if parts.port is not None:
            netloc = "%s:%s" % (netloc, parts.port)
        return urllib.parse.urlunsplit(
            (parts.scheme, netloc, _redact_path(parts.path), _redact_query(parts.query), "")
        )
    except ValueError:
        return "<redacted-url>"


def _redact_headers(headers):
    """按词表掩码请求头；非映射视为空，任何异常都不外抛。"""
    if not isinstance(headers, dict):
        return {}
    redacted = {}
    for name, value in headers.items():
        key = str(name)
        redacted[key] = _mask_secret(value) if _is_sensitive_key(key) else value
    return redacted


def _redact_argv(argv):
    """命令行脱敏：`--confirm-token` / `--password` 一类开关的取值只留指纹。

    既覆盖 `--x-token value` 这种分离写法，也覆盖 `--x-token=value` 这种内联写法。
    """
    try:
        items = [str(item) for item in argv]
    except TypeError:
        return []
    redacted = []
    mask_next = False
    for item in items:
        if mask_next:
            redacted.append(_mask_secret(item))
            mask_next = False
            continue
        if item.startswith("-") and _is_sensitive_key(item.split("=", 1)[0]):
            name, separator, value = item.partition("=")
            if separator:
                redacted.append("%s=%s" % (name, _mask_secret(value)))
                continue
            mask_next = True
        redacted.append(item)
    return redacted


# ---------------------------------------------------------------------------
# curl 与载荷
# ---------------------------------------------------------------------------


def build_curl(method, url, headers, body):
    """curl 复现骨架：请求头按敏感词表掩码，请求体不落明文（只留字节数与摘要）。

    保留命令行形状是为了排障时看清"打了哪个地址、带了哪些头"；凭据与请求体只以指纹
    占位，因此日志里的这条命令既不能被直接复用，也不构成可用凭证。
    """
    parts = ["curl -sS -X %s '%s'" % (method, _quote(_redact_url(url)))]
    for name, value in _redact_headers(headers).items():
        parts.append("  -H '%s: %s'" % (_quote(name), _quote(value)))
    if body is not None:
        parts.append(
            "  --data-raw '<NOT_LOGGED %s 字节 sha256=%s>'"
            % (_body_length(body), _sha256(body))
        )
    return " \\\n".join(parts)


def _quote(text):
    return str(text).replace("'", "'\\''")


def _body_length(body):
    if body is None:
        return 0
    if isinstance(body, str):
        return len(body.encode("utf-8"))
    if isinstance(body, (bytes, bytearray)):
        return len(body)
    return None


def _sha256(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, (bytes, bytearray)):
        return None
    return hashlib.sha256(bytes(data)).hexdigest()[:16]


def _safe_response_fields(data):
    """只从 JSON 响应提取固定白名单元数据，绝不返回正文、描述或 data 内容。"""
    if not data:
        return {}
    try:
        payload = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    fields = {}
    for source, target in (("code", "code"), ("resultCode", "result_code")):
        value = payload.get(source)
        if isinstance(value, (str, int)):
            code = str(value).strip()
            if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", code):
                fields[target] = code
    raw_code = payload.get("code")
    success = payload.get("success")
    result = _business_ok(success, raw_code) if success is not None or raw_code is not None else None
    if result is not None:
        fields["business_ok"] = result
    return fields


def _business_ok(success, code):
    if success is None and code is None:
        return None
    if success is not None and str(success).strip().lower() not in _TRUTHY:
        return False
    if code is not None and str(code).strip().lower() not in ("0000", "0"):
        return False
    return True


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _header(response, name):
    getter = getattr(response, "getheader", None)
    if callable(getter):
        try:
            return getter(name)
        except Exception:
            return None
    return None


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return repr(value)


def _now():
    moment = datetime.now(timezone.utc)
    return "%s.%03dZ" % (moment.strftime("%Y-%m-%dT%H:%M:%S"), moment.microsecond // 1000)


def _today():
    return datetime.now().strftime("%Y%m%d")


def _file_sid():
    """文件名用的会话标识：自定义 sid 会自动补上当天日期，避免同名冲突。"""
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", str(_state["sid"] or "session"))[:128]
    if len(sid) >= 8 and sid[:8].isdigit():
        return sid
    return "%s-%s" % (_today(), sid)


def _new_sid():
    return "%s-%04d" % (datetime.now().strftime("%Y%m%d-%H%M%S"), int.from_bytes(os.urandom(2), "big") % 10000)


def _ms(started):
    if not started:
        return 0
    return int((time.monotonic() - started) * 1000)


def _safe_cwd():
    try:
        return os.getcwd()
    except OSError:
        return None


def _profile_of(argv):
    try:
        items = list(argv)
    except TypeError:
        return "default"
    if "--profile" in items:
        index = items.index("--profile")
        if index + 1 < len(items):
            return items[index + 1]
    return "default"


def _skill_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
