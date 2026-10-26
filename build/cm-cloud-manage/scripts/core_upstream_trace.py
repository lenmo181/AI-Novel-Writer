"""下游网关链路标识的读取与提升。

2026-09-21 实测：IDaaS V3 网关的响应体顶层只有 `success/code/message/data`，
链路标识只出现在 HTTP 响应头 `trace_id` 与 `x-yun-tid`（两者同值）。

`executor.RuntimeContext` 只从 adapter 返回值读取顶层 `traceId`（多接口场景按调用顺序
用英文逗号拼接），因此链路标识必须在 adapter 层逐调用提升，不能依赖跨调用的全局状态：
同一次命令里可能有多个 adapter 串行调用，用全局变量会把上一个接口的标识带到下一个。
"""

# 网关下发的两个响应头同值；按归一化后的键名匹配，兼容大小写与连字符/下划线两种写法。
TRACE_ID_HEADERS = ("trace_id", "x-yun-tid")

TRACE_ID_FIELD = "traceId"

_ACCEPTED_HEADER_KEYS = frozenset(name.replace("-", "_") for name in TRACE_ID_HEADERS)


def read(headers):
    """从响应头读取链路标识；响应头缺失或无有效值时返回 None。"""
    items = getattr(headers, "items", None)
    if not callable(items):
        return None
    for name, value in items():
        if not isinstance(value, str) or not value.strip():
            continue
        if str(name).lower().replace("-", "_") in _ACCEPTED_HEADER_KEYS:
            return value.strip()
    return None


def attach(payload, trace_id):
    """把链路标识写到响应体顶层 `traceId`；无有效标识、载荷非映射或已有有效值时不改动。"""
    if not isinstance(payload, dict) or not isinstance(trace_id, str):
        return payload
    trace_id = trace_id.strip()
    if not trace_id:
        return payload
    existing = payload.get(TRACE_ID_FIELD)
    if isinstance(existing, str) and existing.strip():
        return payload
    payload[TRACE_ID_FIELD] = trace_id
    return payload
