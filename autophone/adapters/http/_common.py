"""HTTP 入口层各组共用的请求上下文（console-split 文档 §1.2 的 `_common.py`）。

只放「被多组 handler 共用」的东西。目前是一组请求作用域的读数：谁在调
（`current_actor`）、这次请求的幂等键、请求体声明多大（`request_content_length`）。
它们由 `autophone/adapters/http/app.py` 的中间件在每次请求进出时 set / reset，
handler 只读；ContextVar 而不是参数，因为 FastAPI 的依赖注入不穿透后台线程与
本地直调，而 actor 要跟着写进账本。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any


REQUEST_ACTOR: ContextVar[str] = ContextVar("autophone_request_actor", default="web")
REQUEST_IDEMPOTENCY_KEY: ContextVar[str | None] = ContextVar(
    "autophone_request_idempotency_key", default=None
)


def current_actor() -> str:
    """本次请求的操作者。中间件没设过时是默认值 `web`（本地直调、后台线程）。"""
    return REQUEST_ACTOR.get()


def request_content_length(request: Any) -> int | None:
    """请求头声明的字节数；缺失或不是整数时返回 None（**不**据此拒绝）。"""
    raw = request.headers.get("content-length")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
