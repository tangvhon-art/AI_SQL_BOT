"""公共 SSE 事件总线。

统一事件注册 + 序列化，所有 SSE 事件通过此总线发送。
新增事件类型只需注册序列化函数，不改推送逻辑。
"""
from __future__ import annotations

import json
from typing import Any, Callable


class SseEvent:
    """SSE 事件：event 类型 + data 字典。"""

    def __init__(self, event: str, data: dict[str, Any]):
        self.event = event
        self.data = data

    def to_sse(self) -> str:
        """序列化为 SSE 格式。"""
        return f"event: {self.event}\ndata: {json.dumps(self.data, ensure_ascii=False, default=str)}\n\n"


# 内置事件类型
BUILTIN_EVENTS = {
    # 现有问数事件
    "progress", "conv_id", "spec", "sql", "result", "chart", "error", "done",
    "summary", "table_clarify",
    # 多查询事件
    "multi_spec", "sub_sql", "sub_result", "sub_error", "dashboard",
    # AI 解读事件
    "ai_interpretation_start", "ai_interpretation", "ai_interpretation_done",
    "ai_interpretation_error",
}


class SseBus:
    """SSE 事件总线：事件注册表 + 统一发送。

    用法：
        bus = SseBus()
        bus.emit("multi_spec", {"sub_queries": [...]})
        # 在 SSE 生成器中：
        for event_str in bus.consume():
            yield event_str
    """

    _handlers: dict[str, Callable[[dict], dict]] = {}

    def __init__(self):
        self._buffer: list[str] = []

    @classmethod
    def register(cls, event_type: str, handler: Callable[[dict], dict] | None = None) -> None:
        """注册事件类型及可选的序列化处理器。

        handler 接收原始 data，返回处理后的 data（用于字段校验/转换）。
        不传 handler 时仅注册事件类型（允许 emit）。
        """
        if handler:
            cls._handlers[event_type] = handler

    def emit(self, event_type: str, data: dict[str, Any]) -> None:
        """发送事件：经过注册的 handler 处理后，序列化为 SSE 字符串存入缓冲区。"""
        handler = self._handlers.get(event_type)
        if handler:
            try:
                data = handler(data)
            except Exception:
                pass
        event = SseEvent(event_type, data)
        self._buffer.append(event.to_sse())

    def consume(self) -> list[str]:
        """消费缓冲区中的所有事件字符串，并清空。"""
        events = self._buffer[:]
        self._buffer.clear()
        return events

    def has_events(self) -> bool:
        return len(self._buffer) > 0


def sse_format(event: str, data: dict[str, Any]) -> str:
    """便捷函数：直接格式化 SSE 字符串（不经过总线）。"""
    return SseEvent(event, data).to_sse()
