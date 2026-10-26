# -*- coding: utf-8 -*-
"""控制面 HTTP 通道：连接复用(Python+ALPN) 优先 + curl 兜底（L1–L4）。

控制面适配器以
    _OPENER = core_control_http.build_opener(_NoRedirectHandler,
                                             urllib.request.HTTPSHandler(context=...))
构造 `_OPENER`，随后 `_OPENER.open(request, timeout=...)`。本模块提供与
`urllib.request.build_opener(*handlers)` 同形的工厂，返回的 opener：

- **L1 握手特征**：进程内 Python 通道统一用带 **ALPN** 的 SSL 上下文（见 core_tls）；
- **L2 连接复用**：按 host 复用 keep-alive 连接（core_http_pool），把一条命令的 N 次握手
  降为 1 次，消除 ``p^N`` 坍塌；
- **L3 首握手对冲**：建连时并行开 M 条只竞速握手（core_http_pool），抬高首握手成功率；
- **L4 失败只重放失败步**：某请求网络失败时，只重发**该请求**（已成功的步骤不重做）；且
  仅当失败可证明发生在"未发出业务数据"阶段（握手阶段）才允许对写方法重试——避免重复副作用。
- 兜底：进程内通道失败后回落 curl（第二条独立握手栈）。

环境开关：
- ``CMCLOUD_TRANSPORT=curl`` 强制只用 curl；``=urllib`` 强制只用进程内 Python（不回退）；
- ``CMCLOUD_HTTP_KEEPALIVE=0`` 关闭连接复用（退回每请求新建连接）；
- ``CMCLOUD_HANDSHAKE_RACE=1..5`` 首握手并行数（默认 2）。

返回与异常与 urllib 同形，适配器现有 `_post`/`HTTPError`/`OSError` 处理无需改动：
- 2xx → 响应对象（getcode/headers/read/close）；
- 非 2xx → urllib.error.HTTPError（code/headers/read）；
- 网络失败 → urllib.error.URLError（reason 含 errno 语义、阶段与 host 后缀）。
"""

import io
import json
import http.client
import os
import time
import urllib.error
import urllib.request
from email.message import Message

import core_http_pool as _pool
import core_tls as _tls
import core_transport_curl as _transport

# 发送证据与重发判定统一由 core_transport_curl（curl 原始退出码）与 core_http_pool
# （失败阶段）提供；本模块只做"单调合并 + 单一门禁"，不再按 errno 单词推断安全性。
# 幂等方法：网络失败一律可安全重试。
_IDEMPOTENT = frozenset(("GET", "HEAD", "OPTIONS", "TRACE"))
_DEFAULT_TIMEOUT = 30


class ControlNetworkError(urllib.error.URLError):
    """穿过适配器异常包装仍可识别的网络状态，不包含请求头或签名地址。"""

    cmcloud_network_error = True

    def __init__(self, reason, *, evidence, replay_safe, trace):
        super().__init__(reason)
        self.evidence = evidence
        self.outcome_unknown = not replay_safe and evidence != _transport.EVIDENCE_PRE_SEND
        self.retryable = not self.outcome_unknown
        self.attempts = tuple(dict(item) for item in trace)


class _CurlResponse:
    """urllib 响应对象的最小同形替身（适配器只用 getcode/headers/read/close）。"""

    def __init__(self, status, headers, body):
        self._status = status
        self._headers = headers
        self._body = body
        self._pos = 0

    def getcode(self):
        return self._status

    @property
    def status(self):
        return self._status

    @property
    def headers(self):
        return self._headers

    def read(self, amt=None):
        if amt is None:
            chunk = self._body[self._pos:]
            self._pos = len(self._body)
            return chunk
        chunk = self._body[self._pos:self._pos + amt]
        self._pos += len(chunk)
        return chunk

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _to_message(headers):
    """把 curl 解析出的 (name, value) 转成 urllib 同形的 email.message.Message。"""
    message = Message()
    for key, value in headers or ():
        message[str(key)] = str(value)
    return message


def _reason(err, url):
    """curl 失败 → 干净 reason：优先 errno 语义；未知则剥离数据面前缀避免双重前缀。"""
    text = _transport.describe_error(err.errno, url)
    if text:
        return text
    return "传输失败（curl 退出码 %s）%s" % (err.exit_code, _transport.host_suffix(url))


def _urllib_reason(err, url):
    """urllib 失败 → 干净 reason（含 errno 语义与 host）。"""
    errno = getattr(err, "errno", None)
    if errno is None:
        errno = getattr(getattr(err, "reason", None), "errno", None)
    text = _transport.describe_error(errno, url)
    if text:
        return text
    return "网络请求失败%s" % _transport.host_suffix(url)


def _pool_reason(err, url):
    """连接池失败 → 干净 reason（含 errno 语义、失败阶段与 host）。"""
    text = _transport.describe_error(getattr(err, "errno", None), url)
    if text:
        return "%s（%s 阶段）" % (text, err.stage)
    return "网络请求失败（%s 阶段）%s" % (err.stage, _transport.host_suffix(url))


def _urllib_evidence(err):
    """urllib 层失败的发送证据（与数据面共用同一判定）。"""
    return _transport.classify_os_error(err)


def _merge_evidence(current, new):
    """单调合并发送证据：任一次"可能已发出"后永久保持未知，不再授权重发。"""
    if current == _transport.EVIDENCE_PRE_SEND and new == _transport.EVIDENCE_PRE_SEND:
        return _transport.EVIDENCE_PRE_SEND
    return _transport.EVIDENCE_UNKNOWN


def _unknown_note(method, declared, evidence):
    """结果未知时的补充话术（仅非幂等写、且未声明为只读查询时给出）。"""
    if evidence == _transport.EVIDENCE_PRE_SEND:
        return ""
    if method in _IDEMPOTENT or declared:
        return ""
    return "；请求可能已送达（结果未知），请先核查原操作，不要直接重发"


def _trace_attempts(method, url, declared, evidence, trace, outcome="failed"):
    """排障用：记录每次尝试的通道/阶段/原始退出码与门禁结论（默认关闭）。

    ``CMCLOUD_HTTP_TRACE=1`` 打开，输出到 stderr（只含主机名，不含路径与查询串）。
    """
    if os.environ.get("CMCLOUD_HTTP_TRACE", "").strip().lower() not in ("1", "true", "yes"):
        return
    try:
        import sys
        import urllib.parse
        host = urllib.parse.urlsplit(url).hostname
    except Exception:
        host = None
    try:
        record = dict(method=method, host=host, declared=declared, evidence=evidence,
                      outcome=outcome, attempts=trace)
        sys.stderr.write("[cmcloud-http] " + json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def query_request(url, data=None, headers=None, method="POST"):
    """构造**已声明的无副作用只读查询**请求。

    清单/详情/搜索类命令在协议上用 POST 承载，但业务上没有副作用，重放安全。显式声明后
    由重发门禁放行，从而关闭"查询失败却不用剩余预算"的缺口——而不是放宽所有 POST 的重试范围。

    **仅用于经审计确认的只读命令；写入类命令禁止使用。**
    """
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    request.cmcloud_declared_query = True
    return request


class _CurlOpener:
    """与 urllib OpenerDirector 同形的 opener：连接复用(Python+ALPN) 优先，curl 兜底。"""

    def __init__(self, handlers):
        # 丢弃适配器传入的 HTTPSHandler（其上下文不带 ALPN），统一替换为带 ALPN 的
        # 上下文；其余 handler（如禁止跟随重定向）保留，供无池的进程内通道使用。
        kept = [h for h in handlers if not isinstance(h, urllib.request.HTTPSHandler)]
        self._handlers = tuple(kept) + (
            urllib.request.HTTPSHandler(context=_tls.create_ssl_context()),
        )
        self._fallback = None

    def _urllib_opener(self):
        if self._fallback is None:
            _transport.install_ipv4_preferred_resolver()
            self._fallback = urllib.request.build_opener(*self._handlers)
        return self._fallback

    def _python_open(self, request, method, url, headers, body, timeout, declared=False):
        """进程内通道：默认走连接池；CMCLOUD_HTTP_KEEPALIVE=0 时退回单次 urllib。"""
        if _pool.keepalive_enabled():
            return _pool.request(method, url, headers=headers, body=body, timeout=timeout,
                                 allow_replay=declared)
        opener = self._urllib_opener()
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as err:
            raw = err.read()
            headers_obj = err.headers
            status = err.code
            try:
                err.close()
            except Exception:
                pass
            return status, headers_obj, raw
        try:
            status = response.getcode()
            headers_obj = response.headers
            raw = response.read()
        finally:
            response.close()
        return status, headers_obj, raw

    def _curl_open(self, request, timeout):
        """curl 通道：返回 urllib 同形的响应或抛出同形异常。"""
        method = request.get_method().upper()
        url = request.full_url
        # header_items() 的键名与 urllib 实际发出的报文一致（复用同一 Request）。
        headers = dict(request.header_items())
        body = request.data
        if isinstance(body, str):
            body = body.encode("utf-8")
        status, resp_headers, resp_body = _transport.transfer(
            method, url, headers=headers, body=body if body is not None else b"",
            timeout=timeout, prefer="curl", retries=0,
        )
        return status, _to_message(resp_headers), resp_body

    @staticmethod
    def _make_response(url, status, headers_obj, raw):
        if isinstance(status, int) and 200 <= status < 300:
            return _CurlResponse(status, headers_obj, raw)
        reason = headers_obj.get("reason", "") if hasattr(headers_obj, "get") else ""
        raise urllib.error.HTTPError(
            url, status, reason or "", headers_obj, io.BytesIO(raw)
        )

    def open(self, fullurl, data=None, timeout=None):
        request = fullurl if isinstance(fullurl, urllib.request.Request) \
            else urllib.request.Request(fullurl, data=data)
        method = request.get_method().upper()
        url = request.full_url
        headers = dict(request.header_items())
        body = request.data
        if isinstance(body, str):
            body = body.encode("utf-8")
        effective_timeout = timeout if isinstance(timeout, (int, float)) and timeout > 0 \
            else _DEFAULT_TIMEOUT

        mode = os.environ.get("CMCLOUD_TRANSPORT", "").strip().lower()
        curl_ok = _transport.curl_available()
        use_curl = mode != "urllib" and curl_ok
        use_python = mode != "curl" or not curl_ok
        idempotent = method in _IDEMPOTENT
        # 只读查询由适配器显式声明（query_request）；写入类命令不得声明
        declared = bool(getattr(request, "cmcloud_declared_query", False))
        budget = _transport.safe_retry_budget()
        deadline = time.monotonic() + effective_timeout

        # 调用级**单调**发送证据：一旦出现"可能已发出业务数据"，此后不再授权任何重发。
        evidence = _transport.EVIDENCE_PRE_SEND
        trace = []
        last_desc = "网络请求失败"

        def may_replay():
            """唯一重发门禁：连接失效重连、切换通道、下一轮重试都必须先过这里。"""
            return idempotent or declared or evidence == _transport.EVIDENCE_PRE_SEND

        for attempt in range(budget + 1):
            if attempt:
                time.sleep(min(_transport.RETRY_BASE_DELAY * attempt,
                               max(0, deadline - time.monotonic())))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last_desc += "；已达到请求恢复的总时间预算"
                break

            # 1) 进程内 Python + ALPN + 连接复用（除非显式强制 curl）
            if use_python:
                entry = dict(attempt=attempt + 1, channel="pool")
                trace.append(entry)
                try:
                    status, headers_obj, raw = self._python_open(
                        request, method, url, headers, body, remaining, declared
                    )
                except urllib.error.HTTPError:
                    # HTTPError 也是 URLError；业务拒绝必须交回适配器，不能切通道重发。
                    raise
                except _pool.PoolNetworkError as err:
                    evidence = _merge_evidence(evidence, err.evidence)
                    last_desc = _pool_reason(err, url)
                    entry.update(stage=err.stage, errno=err.errno, evidence=err.evidence)
                except (urllib.error.URLError, OSError, http.client.HTTPException) as err:
                    evidence = _merge_evidence(evidence, _urllib_evidence(err))
                    last_desc = _urllib_reason(err, url)
                    entry.update(stage="unknown", evidence=_urllib_evidence(err))
                else:
                    entry.update(status=status, action="response")
                    _trace_attempts(method, url, declared, evidence, trace, outcome="response")
                    return self._make_response(url, status, headers_obj, raw)
                entry["action"] = "recover" if may_replay() else "stop_unknown"

            # 2) curl 兜底（或强制 curl）——切换通道同样是"重发"，必须先过门禁
            remaining = deadline - time.monotonic()
            if use_curl and may_replay() and remaining > 0:
                entry = dict(attempt=attempt + 1, channel="curl")
                trace.append(entry)
                try:
                    status, headers_obj, raw = self._curl_open(request, remaining)
                except _transport.CurlTransportError as err:
                    evidence = _merge_evidence(evidence, err.evidence)
                    last_desc = _reason(err, url)
                    entry.update(curlExitCode=err.exit_code, evidence=err.evidence,
                                 action="recover" if may_replay() else "stop_unknown")
                else:
                    entry.update(status=status, action="response")
                    _trace_attempts(method, url, declared, evidence, trace, outcome="response")
                    return self._make_response(url, status, headers_obj, raw)

            if attempt < budget and may_replay():
                continue
            break

        _trace_attempts(method, url, declared, evidence, trace)
        raise ControlNetworkError(last_desc + _unknown_note(method, declared, evidence),
                                  evidence=evidence, replay_safe=idempotent or declared, trace=trace)


def build_opener(*handlers):
    """与 urllib.request.build_opener 同形；handlers 用于无池的进程内通道。"""
    return _CurlOpener(handlers)


__all__ = ("build_opener", "query_request", "ControlNetworkError")
