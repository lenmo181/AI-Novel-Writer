# -*- coding: utf-8 -*-
"""预签名传输通道：curl 子进程优先（-4 + ALPN 放行 + 有限重试），urllib 兜底（带 ALPN）。

背景：云沙箱到移动云盘链路存在两层故障——
1) 沙箱无 IPv6 出口但解析返回 AAAA → urllib 触发 [Errno 101] Network is unreachable；
2) 出口安全设备按 TLS ClientHello 判定脚本客户端——**缺少 ALPN 扩展**的握手被重置
   （[Errno 104]）。同批实测：Python 无 ALPN 53% / +ALPN http/1.1 80% / +ALPN h2,http/1.1 93%；
   curl 默认携带 ALPN（h2）故显著更稳，改 UA 无效。

策略：
- 优先用系统 curl 子进程传输（自带 ALPN + -4 避免 v6 黑洞）；
- curl 不存在/执行失败时回落 urllib：getaddrinfo 结果做 IPv4 优先排序（消 101），
  SSL 上下文统一补齐 ALPN（见 core_tls，缓解 104）；
- 瞬时网络错误做有限重试（默认 2 次重试，共 3 次尝试；幂等 PUT/预签名 POST）。

环境开关：
- CMCLOUD_TRANSPORT=urllib → 强制走 urllib（带 ALPN）；=curl → 强制只用 curl；
- CMCLOUD_CURL_RETRIES=0..5  → 覆盖重试次数。
"""

import os
import http.client
import socket
import subprocess
import time

_CURL_CONNECT_TIMEOUT = 10
_CURL_MAX_TIME = 600
_DEFAULT_RETRIES = 2
_RETRY_BASE_DELAY = 0.5
RETRY_BASE_DELAY = _RETRY_BASE_DELAY  # 公开别名：控制面 shim 复用同一退避节奏


# 发送证据：用于判定"能否重发"。只有 PRE_SEND 才可证明本次未把业务数据交给网络。
EVIDENCE_PRE_SEND = "pre_send"
EVIDENCE_UNKNOWN = "unknown"

# curl 原始退出码 → 是否可证明未发出业务数据。
# 6=解析失败、7=连接失败、35=TLS 握手失败、60=证书校验失败：均发生在发出 HTTP 请求之前。
_PRE_SEND_EXIT_CODES = frozenset((6, 7, 35, 60))
# 18=部分传输、52=空响应、55=发送失败、56=接收失败：可能已发出，保守按不确定处理。
_POST_SEND_EXIT_CODES = frozenset((18, 52, 55, 56))


def classify_exit_code(exit_code, stderr_text=""):
    """按 **curl 原始退出码** 判定发送证据（不要用内部兜底码）。

    - ``6/7/35/60``：解析/连接/TLS 握手/证书校验失败，早于 HTTP 请求 → ``PRE_SEND``；
    - ``28``：连接超时属握手期 → ``PRE_SEND``；其余"操作超时"可能已发出 → ``UNKNOWN``；
    - ``18/52/55/56`` 及其余未识别码 → 一律 ``UNKNOWN``（保守）。
    """
    if exit_code in _PRE_SEND_EXIT_CODES:
        return EVIDENCE_PRE_SEND
    if exit_code == 28:
        low = (stderr_text or "").lower()
        if "connection timed out" in low or "couldn't connect" in low or "failed to connect" in low:
            return EVIDENCE_PRE_SEND
        return EVIDENCE_UNKNOWN
    return EVIDENCE_UNKNOWN


def classify_os_error(err):
    """OSError/urllib 异常的发送证据：只承认能证明"未发出业务数据"的确定情形。"""
    import socket
    import ssl
    probe = getattr(err, "reason", None) or err
    if isinstance(probe, socket.gaierror):         # DNS 解析失败：未发出
        return EVIDENCE_PRE_SEND
    if isinstance(probe, ConnectionRefusedError):  # 连接被拒：未发出
        return EVIDENCE_PRE_SEND
    if isinstance(probe, ssl.SSLCertVerificationError):  # 证书校验发生在握手阶段
        return EVIDENCE_PRE_SEND
    # 普通 SSLError 也可能是发送/接收阶段的 TLS 记录错误，不能推断为未发送。
    return EVIDENCE_UNKNOWN


class CurlTransportError(RuntimeError):
    def __init__(self, message, *, host=None, errno=None, attempts=0, via="curl",
                 exit_code=None, evidence=None):
        super().__init__(message)
        self.host = host
        self.errno = errno          # 粗粒度内部归类，仅供文案/统计，不用于重发判定
        self.attempts = attempts
        self.via = via
        self.exit_code = exit_code  # curl 原始退出码（可追溯）
        # 发送证据：未知时保守按"可能已发送"处理
        self.evidence = evidence if evidence is not None else EVIDENCE_UNKNOWN


def _host_suffix(url):
    """失败报错定位用：只输出预签名地址的主机名，不输出路径与签名查询串。"""
    try:
        import urllib.parse
        host = urllib.parse.urlsplit(url).hostname
    except (ValueError, AttributeError):
        host = None
    return " (host: %s)" % host if host else ""


def host_suffix(url):
    """公开别名：供控制面连接池报错拼接主机名（同样不暴露路径与查询串）。"""
    return _host_suffix(url)


def _retry_count():
    raw = os.environ.get("CMCLOUD_CURL_RETRIES", "").strip()
    if not raw:
        return _DEFAULT_RETRIES
    try:
        n = int(raw)
    except ValueError:
        return _DEFAULT_RETRIES
    return 0 if n < 0 else min(n, 5)


def _curl_available():
    from shutil import which
    return which("curl") is not None


def _errno_of(text):
    import re
    m = re.search(r"errno (\d+)", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _friendly(errno, host):
    if errno == 101:
        return "网络不可达：运行环境缺少可用 IPv6 出口（目标域名返回了 IPv6 地址）%s" % _host_suffix(host)
    if errno == 104:
        return "连接被中间设备重置：疑似出口链路对该传输栈的拦截%s" % _host_suffix(host)
    return None


def describe_error(errno, url):
    """控制面报错用：无“网络请求失败:”前缀的干净描述（含 host 后缀）。

    errno 语义：101=v6 黑洞、104=出口拦截/RST、28=超时、6/None=解析失败、7=无法建立连接。
    未识别的 errno 返回 None，由调用方按兜底文案处理。
    """
    if errno == 101:
        base = "网络不可达：运行环境缺少可用 IPv6 出口（目标域名返回了 IPv6 地址）"
    elif errno == 104:
        base = "连接被中间设备重置：疑似出口链路对该传输栈的拦截"
    elif errno == 28:
        base = "连接超时"
    elif errno == 6:
        base = "域名解析失败"
    elif errno == 7:
        base = "无法建立连接"
    else:
        return None
    return base + _host_suffix(url)


def safe_retry_budget():
    """控制面安全重试预算（仅用于“未建立连接”的失败，默认同 CMCLOUD_CURL_RETRIES）。"""
    return _retry_count()


def _run_curl(args):
    """执行 curl；返回 (returncode, stdout_bytes, stderr_text)。"""
    proc = subprocess.run(
        ["curl", "-4", "--silent", "--show-error", "--max-time", str(_CURL_MAX_TIME),
         "--connect-timeout", str(_CURL_CONNECT_TIMEOUT)] + args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", "replace")


def _curl_errno(stderr_text):
    """从 curl stderr 提取**粗粒度内部归类**（仅供文案与统计，不是 curl 原始退出码）。

    未匹配到已知文案时返回 None，因此**不得**据此判定"连接失败/未发出业务数据"；
    发送证据请使用 ``classify_exit_code`` 配合 ``proc.returncode`` 原始退出码。
    """
    if "Connection reset by peer" in stderr_text or "reset" in stderr_text.lower():
        return 104
    if "Network is unreachable" in stderr_text:
        return 101
    if "Could not resolve host" in stderr_text:
        return 6
    if "timed out" in stderr_text:
        return 28
    if "failed to connect" in stderr_text.lower() or "couldn't connect" in stderr_text.lower():
        return 7
    return None


def transfer(method, url, *, headers=None, body=None, body_file=None,
             content_length=None, expect_2xx=True, env=None, timeout=None, retries=None,
             prefer=None, replay_safe=False):
    """HTTP(S) 传输：curl -4 优先 + 有限重试，失败回落 urllib（IPv4 优先）。

    ``replay_safe``：该业务操作是否"允许重复"（对象级幂等，例如同一对象地址的
    PUT/GET 或同一表单策略的表单上传）。默认 ``False``，此时**仅当可证明未发出业务
    数据**（curl 原始退出码 6/7/35/60，或连接超时）才允许重试；否则重试可能造成重复
    副作用，直接失败交由上层判定。

    body: bytes 或提供 read() 的流对象；body_file: 同 body（兼容旧参数名）。
    流对象在 curl 通道会先落临时文件再传输（--data-binary @file）。
    timeout: 覆盖单次请求超时（秒）；None 用默认 600s（数据面大文件语义）。
    retries: 覆盖重试次数；None 用默认（CMCLOUD_CURL_RETRIES 或 2）。控制面对写请求传 0。
    prefer: 覆盖通道选择——"curl" 强制 curl、"urllib" 强制 urllib；None 取环境变量或默认。
    返回 (status:int, response_headers:list[tuple], response_bytes:bytes)。
    非预期异常抛 CurlTransportError（message 含 errno 语义与 host）。
    """
    assert method in ("GET", "HEAD", "PUT", "POST")
    stream = body_file if body_file is not None else body
    if stream is None and body is None:
        raise CurlTransportError("body/body_file 必须提供其一")
    is_bytes = isinstance(stream, (bytes, bytearray, memoryview))
    mode = (prefer or os.environ.get("CMCLOUD_TRANSPORT", "")).strip().lower()
    if mode == "urllib":
        via_curl = False
    else:
        # 默认或 "curl"：curl 优先；无 curl 时回落 urllib。
        via_curl = _curl_available()
    if via_curl:
        if is_bytes or stream is None:
            return _transfer_curl(method, url, headers=headers, body=bytes(stream) if stream else b"",
                                  body_file=None, content_length=content_length,
                                  expect_2xx=expect_2xx, env=env, timeout=timeout, retries=retries,
                                  replay_safe=replay_safe)
        return _transfer_curl(method, url, headers=headers, body=None,
                              body_file=stream, content_length=content_length,
                              expect_2xx=expect_2xx, env=env, timeout=timeout, retries=retries,
                              replay_safe=replay_safe)
    return _transfer_urllib(method, url, headers=headers, body=body, body_file=body_file,
                            content_length=content_length, expect_2xx=expect_2xx,
                            timeout=timeout, retries=retries, replay_safe=replay_safe)


def curl_available():
    """系统 curl 是否可用（控制面 shim 据此决定能否回退到 curl 通道）。"""
    return _curl_available()


# ---------------------------------------------------------------------------
# curl 通道
# ---------------------------------------------------------------------------

def _transfer_curl(method, url, *, headers, body, body_file, content_length, expect_2xx, env,
                   timeout=None, retries=None, replay_safe=False):
    retries = _retry_count() if retries is None else max(0, min(int(retries), 5))
    max_time = timeout if isinstance(timeout, (int, float)) and timeout > 0 else _CURL_MAX_TIME
    connect_timeout = min(_CURL_CONNECT_TIMEOUT, max_time)
    attempts = 0
    last_err = None
    import tempfile

    tmp_path = None
    if body_file is not None:
        # 流式 body 落临时文件再交 curl（--data-binary @file），避免 OOM 与参数长度限制
        fd = tempfile.NamedTemporaryFile(prefix="cmcloud-curl-", suffix=".bin", delete=False)
        try:
            while True:
                chunk = body_file.read(1 << 20)
                if not chunk:
                    break
                fd.write(chunk)
            fd.close()
            tmp_path = fd.name
        except Exception:
            fd.close()
            os.unlink(fd.name)
            raise

    args = ["--include", "--request", method]
    for key, value in (headers or {}).items():
        args += ["--header", "%s: %s" % (key, value)]
    if tmp_path is not None:
        args += ["--data-binary", "@" + tmp_path]
    elif body is not None and method in ("PUT", "POST"):
        args += ["--data-binary", "@-"]

    try:
        while True:
            attempts += 1
            proc_args = list(args)
            if tmp_path is None and body is not None and method in ("PUT", "POST"):
                stdin_data = bytes(body)
            else:
                stdin_data = None
            proc = subprocess.run(
                ["curl", "-4", "--silent", "--show-error", "--include",
                 "--max-time", str(max_time),
                 "--connect-timeout", str(connect_timeout)] + proc_args + [url],
                input=stdin_data,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            )
            err = proc.stderr.decode("utf-8", "replace")
            status, resp_headers, resp_body = _split_head_body(proc.stdout)
            if proc.returncode == 0 and status is not None:
                return status, resp_headers, resp_body
            # 传输层失败：保留 curl 原始退出码，并据此判定是否可证明"未发出业务数据"
            errno = _curl_errno(err)
            exit_code = proc.returncode
            if exit_code == 0:
                # 进程成功但没有完整 HTTP 响应头，仍不能作为业务成功。
                exit_code = 52
            evidence = classify_exit_code(exit_code, err)
            last_err = err or "curl exit %s" % exit_code
            # 重发门禁：业务允许重复（replay_safe）或本次可证明未发出，才允许再试一次
            if attempts > retries or not (replay_safe or evidence == EVIDENCE_PRE_SEND):
                raise CurlTransportError(
                    "网络请求失败: %s%s" % (last_err, _host_suffix(url)),
                    host=None, errno=errno, attempts=attempts,
                    exit_code=exit_code, evidence=evidence,
                )
            time.sleep(_RETRY_BASE_DELAY * attempts)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# urllib 回退通道（IPv4 优先，消 Errno 101）
# ---------------------------------------------------------------------------

_PREFER_V4_INSTALLED = False


def install_ipv4_preferred_resolver():
    """把 getaddrinfo 结果按 AF_INET 优先排序（幂等安装）。

    沙箱无 IPv6 出口但解析返回 AAAA，urllib 默认顺序会先撞 v6 → Errno 101；
    v4 优先后不再触发 v6 黑洞（v4 若被指纹拦截仍是 104，由重试/报错语义表达）。
    """
    global _PREFER_V4_INSTALLED
    if _PREFER_V4_INSTALLED:
        return
    original_getaddrinfo = socket.getaddrinfo

    def ipv4_first_getaddrinfo(*args, **kwargs):
        results = original_getaddrinfo(*args, **kwargs)
        return sorted(results, key=lambda item: 0 if item[0] == socket.AF_INET else 1)

    socket.getaddrinfo = ipv4_first_getaddrinfo
    _PREFER_V4_INSTALLED = True


_ALPN_URLLIB_OPENER = None


def _alpn_urllib_opener():
    """进程内 Python HTTPS 通道（SSL 上下文带 ALPN）；懒建并缓存。"""
    global _ALPN_URLLIB_OPENER
    if _ALPN_URLLIB_OPENER is None:
        import urllib.request

        import core_tls

        _ALPN_URLLIB_OPENER = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=core_tls.create_ssl_context())
        )
    return _ALPN_URLLIB_OPENER


def _transfer_urllib(method, url, *, headers, body, body_file, content_length, expect_2xx,
                     timeout=None, retries=None, replay_safe=False):
    import urllib.error
    import urllib.request

    install_ipv4_preferred_resolver()
    retries = _retry_count() if retries is None else max(0, min(int(retries), 5))
    attempts = 0
    if body_file is not None and body is None:
        body = body_file.read() if content_length is None else body_file.read(content_length)

    while True:
        attempts += 1
        request = urllib.request.Request(url, data=body if method in ("PUT", "POST") else None,
                                         headers=dict(headers or {}), method=method)
        try:
            response = _alpn_urllib_opener().open(
                request, timeout=timeout if timeout else _CURL_MAX_TIME
            )
            try:
                status = response.getcode()
                resp_headers = [(k.lower(), v) for k, v in response.headers.items()]
                resp_body = response.read()
            finally:
                response.close()
            return status, resp_headers, resp_body
        except urllib.error.HTTPError as err:
            status = err.code
            resp_headers = [(k.lower(), v) for k, v in err.headers.items()]
            resp_body = err.read() if hasattr(err, "read") else b""
            try:
                err.close()
            except Exception:
                pass
            return status, resp_headers, resp_body
        except (OSError, http.client.HTTPException) as err:
            errno = getattr(err, "errno", None)
            evidence = classify_os_error(err)
            if attempts > retries or not (replay_safe or evidence == EVIDENCE_PRE_SEND):
                friendly = _friendly(errno, url)
                raise CurlTransportError(
                    "网络请求失败: %s%s" % (friendly or (getattr(err, "reason", None) or err), _host_suffix(url)),
                    host=None, errno=errno, attempts=attempts, via="urllib",
                    exit_code=None, evidence=evidence,
                )
            time.sleep(_RETRY_BASE_DELAY * attempts)


# ---------------------------------------------------------------------------
# 便捷封装：供适配器替换 _post/_put/_get 使用
# ---------------------------------------------------------------------------

def request_bytes(method, url, *, headers=None, body=None, body_file=None, content_length=None,
                  replay_safe=False):
    """返回 (status, headers, body_bytes)；2xx 由调用方判定。

    ``replay_safe`` 语义见 ``transfer``：仅当业务操作允许重复时才传 True。
    """
    return transfer(method, url, headers=headers, body=body, body_file=body_file,
                    content_length=content_length, expect_2xx=True,
                    replay_safe=replay_safe)


def curl_channel_enabled():
    """是否启用 curl 通道（可用 CMCLOUD_TRANSPORT=urllib 强制关闭）。"""
    return os.environ.get("CMCLOUD_TRANSPORT", "").strip().lower() != "urllib" and _curl_available()


def download_to_file(method, url, *, headers=None):
    """GET 下载到临时文件，保持流式读语义；失败返回 (None, [], message)。

    仅用于只读下载：重发同一 GET 无副作用，故内部重试不区分发送证据。
    message 带重试计数与 host 定位后缀（errno 语义化：101=v6黑洞 / 104=指纹拦截）。
    """
    retries = _retry_count()
    attempts = 0
    import tempfile

    fd = tempfile.NamedTemporaryFile(prefix="cmcloud-dl-", suffix=".bin", delete=False)
    fd.close()
    out_path = fd.name
    header_file = tempfile.NamedTemporaryFile(prefix="cmcloud-dl-", suffix=".headers", delete=False)
    header_file.close()
    args = ["--request", method, "--dump-header", header_file.name, "--output", out_path]
    for key, value in (headers or {}).items():
        args += ["--header", "%s: %s" % (key, value)]
    try:
        while True:
            attempts += 1
            proc = subprocess.run(
                ["curl", "-4", "--silent", "--show-error",
                 "--max-time", str(_CURL_MAX_TIME),
                 "--connect-timeout", str(_CURL_CONNECT_TIMEOUT)] + args + [url],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            err = proc.stderr.decode("utf-8", "replace")
            with open(header_file.name, "rb") as fh:
                status, resp_headers, _ = _split_head_body(fh.read())
            if proc.returncode == 0 and status is not None:
                return status, resp_headers, out_path
            errno = _curl_errno(err)
            message = err or "curl exit %s" % proc.returncode
            if attempts > retries:
                _quiet_unlink(out_path)
                return None, [], "网络请求失败: %s%s" % (message, _host_suffix(url))
            time.sleep(_RETRY_BASE_DELAY * attempts)
    except Exception:
        _quiet_unlink(out_path)
        raise
    finally:
        _quiet_unlink(header_file.name)


def _split_head_body(raw):
    """取最终响应，跳过 100/103 等临时响应和代理 CONNECT 响应头。"""
    offset = 0
    while True:
        marker = raw.find(b"\r\n\r\n", offset)
        if marker < 0:
            return None, [], b""
        head = raw[offset:marker].decode("utf-8", "replace")
        status, resp_headers = _parse_status_lines(head)
        if status is None:
            return None, [], b""
        offset = marker + 4
        proxy_connect = "connection established" in head.split("\r\n", 1)[0].lower()
        if 100 <= status < 200 or proxy_connect:
            continue
        return status, resp_headers, raw[offset:]


def _parse_status_lines(head_text):
    lines = head_text.split("\r\n")
    status = None
    resp_headers = []
    for line in lines:
        if status is None and line.startswith("HTTP/"):
            try:
                status = int(line.split()[1])
            except (IndexError, ValueError):
                return None, []
        else:
            k, _, v = line.partition(":")
            if v:
                resp_headers.append((k.strip().lower(), v.strip()))
    return status, resp_headers


def _quiet_unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass
