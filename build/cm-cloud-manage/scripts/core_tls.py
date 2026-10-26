# -*- coding: utf-8 -*-
"""统一的 TLS 上下文：默认补齐 ALPN 扩展。

背景：历史链路实验观察到不同 ALPN 配置下成功率不同；以下小样本不能单独证明
连接重置来自 TLS 指纹拦截，具体断点仍须结合网关日志定位：

| ClientHello 变体 | 握手成功率 |
| --- | --- |
| Python 默认（无 ALPN） | 8/15 = 53% |
| Python + ALPN http/1.1 | 12/15 = 80% |
| Python + ALPN h2,http/1.1 | 14/15 = 93% |

Python 的 ``ssl.create_default_context()`` 默认**不携带** ALPN，因此本模块统一提供
带 ALPN 的上下文，供技能内所有 Python TLS 调用使用。

协议能力：当前 urllib/http.client 通道实现 HTTP/1.1，因此只宣告 http/1.1。
单次服务端协商结果不能保证后续仍不选择 h2，不能宣告客户端未实现的协议。
"""

import ssl
import threading

# ALPN 必须与实际 HTTP 客户端能力一致；curl 使用自己的协议协商配置。
ALPN_PROTOCOLS = ("http/1.1",)

# 进程内共享上下文：``ssl.create_default_context()`` 每次都要加载全量 CA（本机实测
# ~53ms，且持 GIL），逐连接重建会明显拉长建连耗时。SSLContext 在 ``wrap_socket`` 场景
# 下可安全跨线程共享；调用方只读使用，不做二次配置。
_context = None
_context_lock = threading.Lock()


def create_ssl_context():
    """返回进程内共享的、带 ALPN 扩展的 SSL 上下文（首次构建后缓存复用）。"""
    global _context
    if _context is not None:
        return _context
    with _context_lock:
        if _context is None:
            context = ssl.create_default_context()
            try:
                context.set_alpn_protocols(list(ALPN_PROTOCOLS))
            except (NotImplementedError, ValueError, OSError):
                # 个别平台/后端不支持 ALPN：退回默认上下文，不阻断请求。
                pass
            _context = context
    return _context


__all__ = ("ALPN_PROTOCOLS", "create_ssl_context")
