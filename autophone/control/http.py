"""Standalone Control Plane HTTP/WebSocket process without device imports."""
from __future__ import annotations

import asyncio
import ipaddress
import os
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

from fastapi import FastAPI, WebSocket

from autophone.logging import get_logger, level_from_env, setup_logging
from autophone.rig.source_identity import process_source_identity

from .agent_gateway import RigAgentGateway
from .device_session import mount_device_session_channel
from .postgres import PostgresControlStore
from .routes import create_control_router
from .scheduler import ControlScheduler
from .service import ControlPlane
from .host_coordination import HostCoordination


log = get_logger(__name__)


@lru_cache(maxsize=1)
def control_source_identity() -> dict[str, Any]:
    """返回包首次导入时冻结的摘要，首次 /health 或装配不能重新采集磁盘基线。"""
    return process_source_identity()


def create_control_app(
    control: ControlPlane,
    *,
    scheduler_interval_s: float | None = None,
) -> FastAPI:
    hosts: HostCoordination = control
    scheduler: ControlScheduler | None = None
    # 装配期复制进程基线；摘要已在包首次导入时冻结。
    identity = dict(control_source_identity())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task: asyncio.Task[None] | None = None
        if scheduler is not None:
            task = asyncio.create_task(scheduler.run_forever())
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.stop()
            if task is not None:
                await task

    app = FastAPI(title="autophone-control-plane", lifespan=lifespan)
    gateway = RigAgentGateway(control)
    if scheduler_interval_s is not None:
        scheduler = ControlScheduler(
            control,
            gateway.notify,
            gateway.connected_rigs,
            interval_s=scheduler_interval_s,
        )
    # 交互通道（BL-100）：与 Assignment 并列的第二条协议层，自己的 WebSocket、
    # 自己的读循环，会话状态在 rig 侧，这里只留 device → 连接的路由。
    device_sessions = mount_device_session_channel(app, host_snapshot=lambda host: hosts.get_host(host))
    app.state.control = control
    app.state.agent_gateway = gateway
    app.state.device_session_gateway = device_sessions
    app.state.scheduler = scheduler

    app.state.source_identity = identity

    @app.get("/health")
    def health() -> dict:
        # `source_identity` 供 bring-up 的复用判定与 host 侧派单准入比对（BL-151）。
        # 监听器被 `validate_control_host` 限在 loopback，这里报的版本、HEAD 与摘要
        # 不出本机。
        return {
            "ok": True,
            "service": "autophone-control-plane",
            "source_identity": dict(identity),
        }

    app.include_router(create_control_router(lambda: control, host_provider=lambda: hosts))

    @app.websocket("/api/control/agent/ws")
    async def agent_socket(websocket: WebSocket) -> None:
        await gateway.connect(websocket)

    return app


def configured_app() -> FastAPI:
    dsn = os.environ.get("AUTOPHONE_DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("AUTOPHONE_DATABASE_URL is required")
    schema = os.environ.get("AUTOPHONE_DATABASE_SCHEMA", "autophone").strip() or "autophone"
    store = PostgresControlStore(dsn, schema=schema)
    store.initialize()
    interval_s = float(os.environ.get("AUTOPHONE_SCHEDULER_INTERVAL", "0.5"))
    return create_control_app(ControlPlane(store), scheduler_interval_s=interval_s)


def validate_control_host(host: str) -> str:
    """Keep the first same-Host Control Plane on a loopback listener."""

    value = str(host or "").strip().strip("[]")
    loopback = value.lower() == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(value).is_loopback
        except ValueError:
            loopback = False
    if not loopback:
        raise RuntimeError(
            "AUTOPHONE_CONTROL_HOST must be loopback until HTTPS and Agent Token support are enabled"
        )
    return host


def main() -> None:
    import uvicorn

    # 与 CLI serve 同一套：先装我们自己的 root handler，再让 uvicorn 用
    # log_config=None 把它的三个 logger 冒泡上来，派单决策与 access log 才在
    # 同一个文件里、同一种格式。缺了它这个进程只有 uvicorn 的 access log。
    setup_logging(level_from_env())
    host = validate_control_host(
        os.environ.get("AUTOPHONE_CONTROL_HOST", "127.0.0.1")
    )
    port = int(os.environ.get("AUTOPHONE_CONTROL_PORT", "8100"))
    from autophone.rig.source_identity import describe_identity

    log.info(
        "autophone-control starting: http://%s:%s schema=%s %s",
        host,
        port,
        os.environ.get("AUTOPHONE_DATABASE_SCHEMA", "autophone"),
        describe_identity(control_source_identity()),
    )
    uvicorn.run(
        configured_app(),
        host=host,
        port=port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
