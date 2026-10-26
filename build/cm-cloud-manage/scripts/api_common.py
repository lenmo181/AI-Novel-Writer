"""cmcloud 域包装脚本的公共入口逻辑。

调用形态：`python3 <技能根>/scripts/cm_cloud_<domain>_api.py <动词> [参数 ...]`

规则：
- 动词 = 命令 action（如 `search`），也接受 `<action>_<domain>` 别名（如 `search_album`）；
- 动词可声明一个「位置参数」字段：`search "<值>"` 等价于 `search --keyword "<值>"`；
- 其余参数原样转发给 CLI（`--旗标 值`），缺 `--format json` 时自动补上；
- 信封、退出码、stdin 透传与直接调用 CLI 完全一致：包装层不产出自己的错误格式，
  未知动词与非法参数一律由 CLI 返回标准信封。
"""

import os
import socket
import sys

from dependency_bootstrap import DependencyBootstrapError, ensure_dependencies

# 兜底网络超时：未显式传 timeout 的 socket 调用统一受此上界约束，避免弱网或下游
# 无响应时无限挂起；显式 timeout（业务 15s、上传下载 600s）优先级更高，不受影响。
SOCKET_TIMEOUT_ENV = "CMCLOUD_SOCKET_TIMEOUT"
DEFAULT_SOCKET_TIMEOUT_SECONDS = 45


def _configure_socket_timeout():
    raw = os.environ.get(SOCKET_TIMEOUT_ENV)
    if raw is None:
        socket.setdefaulttimeout(DEFAULT_SOCKET_TIMEOUT_SECONDS)
        return
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return
    if seconds > 0:
        socket.setdefaulttimeout(seconds)


def _configure_streams():
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


def _with_format(args):
    if "--format" in args:
        return args
    return args + ["--format", "json"]


def _normalize(action, domain):
    suffix = "_" + domain
    if domain and action.endswith(suffix) and len(action) > len(suffix):
        return action[: -len(suffix)]
    return action


def _cli_main():
    _configure_socket_timeout()
    ensure_dependencies()
    from core_main import main

    return main


def _call_cli(args):
    from cmcloud_core import debug_log

    debug_log.install(args)
    code = 1
    try:
        code = _cli_main()(args)
    except DependencyBootstrapError as err:
        sys.stderr.write("cm-cloud 环境准备失败：%s\n" % err)
        code = 1
    finally:
        debug_log.finish(code)
    return code


def _usage(script, verbs):
    sys.stderr.write(
        "用法: python3 %s <动词> [参数 ...]\n动词: %s\n"
        % (script, ", ".join(sorted(verbs)))
    )


def run(domain, verbs, argv=None, help_topic=True):
    """执行一条业务域命令；`verbs` 形如 {"search": "keyword", "list": None}。

    `help_topic=False` 用于未登记 help 领域的域（如 auth），改取全局 help。
    """
    _configure_streams()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        _usage(sys.argv[0], verbs)
        return 2
    head = args[0]
    if head in ("-h", "--help", "help"):
        help_args = ["help", "--topic", domain] if help_topic else ["help"]
        return _call_cli(_with_format(help_args))
    action = _normalize(head, domain)
    field = verbs.get(action)
    rest = args[1:]
    if field and rest and not rest[0].startswith("-"):
        rest = ["--" + field, rest[0]] + rest[1:]
    return _call_cli(_with_format([domain, action] + rest))


def run_control(commands, argv=None):
    """执行控制面命令；`commands` 形如 {"capabilities": None, "schema": "command-id"}。"""
    _configure_streams()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in commands:
        _usage(sys.argv[0], commands)
        return 2
    verb = args[0]
    field = commands[verb]
    rest = args[1:]
    if field and rest and not rest[0].startswith("-"):
        rest = ["--" + field, rest[0]] + rest[1:]
    return _call_cli(_with_format([verb] + rest))
