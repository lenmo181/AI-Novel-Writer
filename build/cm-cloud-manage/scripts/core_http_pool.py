# -*- coding: utf-8 -*-
"""进程内 HTTPS 连接池 + 首握手并行对冲（仅标准库）。

一条 CLI 命令 = 一个进程，串行对同一 host 发 N 个请求（如 files list 实测 4 个）。默认每个
请求都新建 TCP+TLS 连接，而出口设备按握手特征放行/重置，单握手成功率不足 100%，导致命令
成功率按 ``p^N`` 坍塌。本模块：

- **L2 连接复用**：按 (scheme, host, port) 复用 keep-alive 连接，把 N 次握手降为 1 次；
- **L3 首握手并行对冲**：建连时并行开 M 条（默认 2），只竞速 TCP+TLS 握手、**不发任何业务
  数据**，取先成功者为命令连接——对读、写都安全（不复制请求）；
- **陈旧连接重连**：命中的复用连接被对端/中间设备关闭时，请求失败即丢弃并新建重连一次。

线程与安全：CLI 单线程；仍在池上加锁。只竞速握手、不复制业务请求，故不会造成重复副作用。
环境开关：``CMCLOUD_HANDSHAKE_RACE=1..5``（默认 2），``CMCLOUD_HTTP_KEEPALIVE=0`` 关闭复用。
"""

import http.client
import os
import ssl
import threading
import time
import urllib.parse

import core_tls
import core_transport_curl

_IDLE_LIMIT_SECONDS = 60.0
_IDEMPOTENT_METHODS = frozenset(("GET", "HEAD", "OPTIONS", "TRACE"))
_DEFAULT_RACE = 2
_race_override = None


class PoolNetworkError(OSError):
    """连接层网络失败。

    ``stage``：``"connect"`` 表示 TCP+TLS 握手阶段（未发业务数据，重试对所有方法安全）；
    ``"send"``/``"recv"`` 表示已可能发出业务数据（写方法重试可能重复副作用）。
    """

    def __init__(self, message, stage, errno=None):
        super().__init__(message)
        self.stage = stage
        self.errno = errno if errno is not None else getattr(message, "errno", None)
        self.reason = message

    @property
    def evidence(self):
        """发送证据：只有握手阶段（connect）失败才能证明"未发出业务数据"。"""
        if self.stage == "connect":
            return core_transport_curl.EVIDENCE_PRE_SEND
        return core_transport_curl.EVIDENCE_UNKNOWN


_lock = threading.Lock()
_pool = {}  # (scheme, host, port) -> [connection, last_used_monotonic]


def keepalive_enabled():
    return os.environ.get("CMCLOUD_HTTP_KEEPALIVE", "").strip() != "0"


def _race_count():
    if _race_override is not None:
        return max(1, int(_race_override))
    raw = os.environ.get("CMCLOUD_HANDSHAKE_RACE", "").strip()
    if not raw:
        return _DEFAULT_RACE
    try:
        n = int(raw)
    except ValueError:
        return _DEFAULT_RACE
    return 1 if n < 1 else min(n, 5)


def _split(url):
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme or "https"
    host = parsed.hostname
    port = parsed.port or (443 if scheme == "https" else 80)
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    return (scheme, host, port), target


def _new_conn(scheme, host, port, timeout):
    if scheme == "https":
        return http.client.HTTPSConnection(
            host, port, timeout=timeout, context=core_tls.create_ssl_context()
        )
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _quiet_close(conn):
    try:
        conn.close()
    except Exception:
        pass


def _discard(key):
    with _lock:
        entry = _pool.pop(key, None)
    if entry is not None:
        _quiet_close(entry[0])


def _acquire(scheme, host, port, timeout):
    """返回 (connection, reused)；命中复用连接 reused=True，否则新建（可能并行对冲）。"""
    key = (scheme, host, port)
    now = time.monotonic()
    with _lock:
        entry = _pool.get(key)
        if entry is not None:
            if now - entry[1] <= _IDLE_LIMIT_SECONDS:
                entry[0].timeout = timeout
                if entry[0].sock is not None:
                    entry[0].sock.settimeout(timeout)
                return entry[0], True
            _pool.pop(key, None)
            _quiet_close(entry[0])
    try:
        conn = _open_handshake(scheme, host, port, timeout, _race_count())
    except PoolNetworkError:
        raise
    except Exception as err:  # 兜底：把任何建连异常归一为 connect 阶段失败
        raise PoolNetworkError(err, "connect", getattr(err, "errno", None)) from err
    with _lock:
        _pool[key] = [conn, time.monotonic()]
    return conn, False


def _open_handshake(scheme, host, port, timeout, count):
    """建立一条已握手的连接；count>1 时并行对冲，取先成功者。"""
    if count <= 1:
        conn = _new_conn(scheme, host, port, timeout)
        try:
            conn.connect()
        except Exception:
            _quiet_close(conn)
            raise
        return conn
    done = threading.Event()
    result = {"conn": None, "accepting": True, "remaining": count}
    candidates = []
    errors = []
    guard = threading.Lock()

    def worker():
        local = None
        try:
            local = _new_conn(scheme, host, port, timeout)
            with guard:
                candidates.append(local)
            local.connect()
        except Exception as err:  # noqa: BLE001 - 归一为竞速失败样本
            if local is not None:
                _quiet_close(local)
            with guard:
                errors.append(err)
                result["remaining"] -= 1
                if result["remaining"] == 0:
                    done.set()
            return
        with guard:
            if result["accepting"] and result["conn"] is None:
                result["conn"] = local
                result["accepting"] = False
                done.set()
                return
        _quiet_close(local)  # 已有胜者，关闭本条

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(count)]
    for thread in threads:
        thread.start()
    deadline = timeout if isinstance(timeout, (int, float)) and timeout > 0 else 30
    done.wait(deadline)
    with guard:
        # 超时后禁止迟到线程成为新胜者，否则连接失去持有者并泄漏。
        result["accepting"] = False
        conn = result["conn"]
        losers = [item for item in candidates if item is not conn]
    for item in losers:
        _quiet_close(item)
    if conn is None:
        raise PoolNetworkError(
            str(errors[-1]) if errors else "握手并行对冲全部失败",
            "connect",
            getattr(errors[-1], "errno", None) if errors else None,
        )
    return conn


def _do(conn, method, target, headers, body, key):
    try:
        conn.request(method, target, body=body, headers=headers or {})
    except (http.client.HTTPException, OSError, ssl.SSLError) as err:
        raise PoolNetworkError(str(err), "send", getattr(err, "errno", None)) from err
    try:
        response = conn.getresponse()
        try:
            data = response.read()
        finally:
            response.close()
    except (http.client.HTTPException, OSError, ssl.SSLError) as err:
        raise PoolNetworkError(str(err), "recv", getattr(err, "errno", None)) from err
    if not response.will_close:
        with _lock:
            _pool[key] = [conn, time.monotonic()]
    else:
        with _lock:
            entry = _pool.get(key)
            if entry is not None and entry[0] is conn:
                _pool.pop(key)
        _quiet_close(conn)
    return response.status, response.headers, data


def request(method, url, headers=None, body=None, timeout=None, allow_replay=False):
    """发送一次请求并复用连接。

    返回 (status:int, headers:email.message.Message, body:bytes)。
    网络失败抛 PoolNetworkError（带 stage / evidence 供调用方判定重试安全性）。

    ``allow_replay``：调用方是否额外授权"对**可能已发出**的请求重发"，仅用于已声明的
    无副作用查询。默认 False；幂等方法与握手阶段失败不受此限制。
    """
    key, target = _split(url)
    deadline = time.monotonic() + timeout if isinstance(timeout, (int, float)) and timeout > 0 else None
    conn, reused = _acquire(*key, timeout)
    try:
        return _do(conn, method, target, headers, body, key)
    except PoolNetworkError as err:
        # 复用到的连接可能已被对端/中间设备关闭：丢弃后新建重连一次。
        # 但重连等于"重发"，必须先过同一个重发门禁，否则非幂等写会重复提交。
        _discard(key)
        if not reused:
            raise
        if not (method.upper() in _IDEMPOTENT_METHODS or allow_replay
                or err.evidence == core_transport_curl.EVIDENCE_PRE_SEND):
            raise
        remaining = deadline - time.monotonic() if deadline is not None else timeout
        if deadline is not None and remaining <= 0:
            raise
        conn2, _ = _acquire(*key, remaining)
        try:
            return _do(conn2, method, target, headers, body, key)
        except PoolNetworkError:
            _discard(key)
            raise


def reset():
    """清空连接池（测试/收尾用）。"""
    with _lock:
        items = list(_pool.values())
        _pool.clear()
    for entry in items:
        _quiet_close(entry[0])


__all__ = ("PoolNetworkError", "keepalive_enabled", "request", "reset")
