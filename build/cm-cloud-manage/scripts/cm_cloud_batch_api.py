"""cmcloud 批量执行入口：一个进程内连续执行多条 CLI 命令。

调用形态：
    python3 <技能根>/scripts/cm_cloud_batch_api.py --commands-file <清单> [--output-file <结果>] [--stop-on-error]

清单（JSON 数组，元素为对象）：
    [{"id": "case-001", "argv": ["files", "list", "--format", "json"]}, ...]
    argv 与单条 CLI 的参数一致（域命令不带脚本路径）；缺 --format 时自动补 json。

结果（JSONL，每行一条；--output-file 缺省时写 stdout）：
    {"id": "case-001", "exitCode": 0, "elapsedMs": 812, "envelope": {...}}

设计约束：
- 固定开销（进程启动、依赖校验与加载、模块导入、首次取票）整批只付一次；
  适合测试执行、批量探测等命令集可预知的场景，单条交互式命令仍走单条入口。
- 单条命令失败不中断整批（--stop-on-error 可改为立即停止）；逐条隔离异常。
- 台账/游标行为与单条入口一致：同一批次共享同一台账目录与身份。
"""

import argparse
import contextlib
import io
import json
import sys
import time

from dependency_bootstrap import DependencyBootstrapError, ensure_dependencies


def _configure_streams():
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


def _load_commands(path):
    # utf-8-sig：容忍 Windows 工具（PowerShell 等）写入的 BOM 头。
    with open(path, "r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, list) or not data:
        raise ValueError("清单必须是至少含一条命令的 JSON 数组")
    commands = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError("第 %d 条命令必须是对象" % (index + 1))
        argv = item.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(part, str) for part in argv):
            raise ValueError("第 %d 条命令的 argv 必须是非空字符串数组" % (index + 1))
        if not any(part == "--format" or part.startswith("--format=") for part in argv):
            argv = list(argv) + ["--format", "json"]
        commands.append({"id": item.get("id") or ("cmd-%d" % (index + 1)), "argv": argv})
    return commands


def _call_with_debug_log(argv, cli_main):
    """与单条入口一致地包裹调试日志；调试日志自身异常不影响命令执行。"""
    try:
        from cmcloud_core import debug_log
    except Exception:
        return cli_main(argv)
    try:
        debug_log.install(argv)
    except Exception:
        return cli_main(argv)
    code = 1
    try:
        code = cli_main(argv)
    finally:
        try:
            debug_log.finish(code)
        except Exception:
            pass
    return code


def _run_one(cli_main, command):
    stdout = io.StringIO()
    stderr = io.StringIO()
    started = time.perf_counter()
    code = 1
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code = _call_with_debug_log(command["argv"], cli_main)
        except SystemExit as err:
            code = err.code if isinstance(err.code, int) else 1
        except Exception as err:  # 单条隔离：任何异常不拖垮整批
            code = 1
            stderr.write("批量执行异常: %r" % (err,))
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    result = {"id": command["id"], "exitCode": code, "elapsedMs": elapsed_ms}
    text = stdout.getvalue()
    try:
        result["envelope"] = json.loads(text)
    except ValueError:
        result["envelope"] = None
        result["stdout"] = text[:4000]
    error_text = stderr.getvalue().strip()
    if error_text:
        result["stderr"] = error_text[:2000]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cm_cloud_batch_api.py",
        description="一个进程内连续执行多条 cmcloud CLI 命令",
    )
    parser.add_argument("--commands-file", required=True, help="命令清单 JSON 文件路径")
    parser.add_argument("--output-file", default=None, help="结果 JSONL 输出路径（缺省写 stdout）")
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="任一条失败即停止后续命令（缺省继续执行全部）",
    )
    args = parser.parse_args(argv)

    _configure_streams()
    try:
        commands = _load_commands(args.commands_file)
    except (OSError, ValueError) as err:
        sys.stderr.write("批量清单不可用: %s\n" % err)
        return 2

    try:
        ensure_dependencies()
        from core_main import main as cli_main
    except DependencyBootstrapError as err:
        sys.stderr.write("cm-cloud 环境准备失败：%s\n" % err)
        return 1
    except Exception as err:  # 导入失败按环境错误处理，保持单条入口的文案口径
        sys.stderr.write("cm-cloud 环境准备失败：%r\n" % (err,))
        return 1

    lines = []
    failures = 0
    for command in commands:
        result = _run_one(cli_main, command)
        lines.append(json.dumps(result, ensure_ascii=False))
        if result["exitCode"] != 0:
            failures += 1
            if args.stop_on_error:
                break

    payload = "\n".join(lines) + "\n"
    if args.output_file:
        try:
            with open(args.output_file, "w", encoding="utf-8") as handle:
                handle.write(payload)
        except OSError as err:
            sys.stderr.write("结果文件写入失败: %s\n" % err)
            return 1
        summary = {
            "status": "SUCCESS" if failures == 0 else "PARTIAL",
            "executed": len(lines),
            "failures": failures,
            "outputFile": args.output_file,
        }
        sys.stdout.write(json.dumps(summary, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(payload)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
