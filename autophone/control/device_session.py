"""交互通道的控制面侧：会话路由与事件转发（BL-100）。

**与 Assignment 并列，不是它的一种。** Assignment 是一次性工作单元，状态在 PostgreSQL 里，
一次往返要经「提交 → Scheduler tick → Agent WebSocket → 执行 → 回报 → 提交侧轮询」。
屏幕预览按 ~800ms 轮询、device console 一次 tap 就是一次往返，这两种量级差两个数量级；
而且 device console 的不变量是「一条会话从 start 到 stop 持有同一条 ScreenLock lease」，
一动作一 Assignment 会在两次动作之间放开设备。所以交互通道是另一条协议层。

**用另一条 WebSocket，不复用 Agent WebSocket。** `autophone/rig/transport.py` 的
`WebSocketAgentTransport` 是一条加锁的同步 socket：agent 发请求、控制面回 ack，
非本次 request_id 的消息在 `_request()` 的循环里被丢弃。把控制面主动发起的会话请求
塞进那条 socket，会在 agent 执行 Assignment 期间被直接丢掉。交互通道因此自带一条
连接与一个读循环，Assignment 协议一行不改。

**会话状态在 rig，不在这里，也不在 PostgreSQL。** 会话的实体是一条 ScreenLock lease
和一个 UDS 驱动器，两者都是持设备那台主机上的进程内资源；进程一没，lease 也就没了。
把会话写进 PostgreSQL 只会得到一张 rig 重启后还在说「会话存活」的表。控制面这边只留
**路由**：device → 哪条连接。连接一断，路由删除，同时 rig 侧读循环退出并还掉全部 lease
（`DeviceSessionOps.close_all`），两侧各有一条终止路径，lease 不泄漏。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from autophone.application.device_session import (
    DEVICE_SESSION_OPS,
    DEVICE_SESSION_WS_PATH,
    DeviceSessionRefused,
)


logger = logging.getLogger(__name__)

# 控制面等 rig 回一次操作的上限。比提交侧客户端的 30s 短一档，好让「rig 不回话」
# 以通道自己的 code 收场，而不是提交侧的连接超时。
RIG_RESPONSE_TIMEOUT_S = 25.0


class _Connection:
    def __init__(
        self,
        websocket: WebSocket,
        *,
        host_id: str,
        rig_id: str,
        generation: int,
        devices: list[str],
    ) -> None:
        self.websocket = websocket
        self.host_id = host_id
        self.rig_id = rig_id
        self.generation = generation
        self.devices = devices
        self.pending: dict[str, asyncio.Future] = {}
        self.send_lock = asyncio.Lock()

    def fail_pending(self, error: DeviceSessionRefused) -> None:
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(error)
        self.pending.clear()


class DeviceSessionGateway:
    """把一个设备操作转发给正在服务该设备的 rig，并等它回话。"""

    def __init__(
        self,
        *,
        host_snapshot: Callable[[str], Mapping[str, Any]],
        response_timeout_s: float = RIG_RESPONSE_TIMEOUT_S,
    ) -> None:
        self._host_snapshot = host_snapshot
        self.response_timeout_s = response_timeout_s
        self._by_device: dict[str, _Connection] = {}
        self._lock = asyncio.Lock()

    # ── rig 侧连接 ────────────────────────────────────────────────────────────
    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        connection: _Connection | None = None
        displaced: set[_Connection] = set()
        try:
            hello = await websocket.receive_json()
            connection = _validate_hello(websocket, hello)
            await self._validate_identity(connection)
            async with self._lock:
                # 查询在工作线程中执行，旧快照可能晚于新连接返回；绑定时不得降低已知代数。
                if any(
                    current.rig_id == connection.rig_id
                    and current.generation > connection.generation
                    for current in self._by_device.values()
                ):
                    raise DeviceSessionRefused(
                        "device session identity is older than the connected Rig generation",
                        status=409, code="stale_device_session",
                    )
                for device in connection.devices:
                    previous = self._by_device.get(device)
                    if previous is not None:
                        displaced.add(previous)
                    self._by_device[device] = connection
        except (DeviceSessionRefused, ValueError, TypeError, KeyError, WebSocketDisconnect) as exc:
            logger.warning("device session handshake rejected: %s: %s", type(exc).__name__, exc)
            try:
                await websocket.send_json({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
                await websocket.close(code=1008)
            except (RuntimeError, WebSocketDisconnect):
                pass
            return

        for previous in displaced:
            await self._retire(previous, DeviceSessionRefused(
                "rig reconnected; in-flight device session request abandoned",
                status=503,
                code="device_session_unavailable",
            ))
        logger.info(
            "device session rig connected: host=%s rig=%s generation=%s devices=%s",
            connection.host_id, connection.rig_id, connection.generation, connection.devices,
        )
        try:
            await websocket.send_json({"type": "ready", "devices": connection.devices})
            while True:
                message = await websocket.receive_json()
                self._resolve(connection, message)
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            async with self._lock:
                for device in connection.devices:
                    if self._by_device.get(device) is connection:
                        self._by_device.pop(device, None)
            connection.fail_pending(DeviceSessionRefused(
                "rig agent disconnected while the request was in flight",
                status=503,
                code="device_session_unavailable",
            ))
            logger.info(
                "device session rig disconnected: host=%s rig=%s",
                connection.host_id, connection.rig_id,
            )

    @staticmethod
    def _resolve(connection: _Connection, message: Mapping[str, Any]) -> None:
        request_id = str(message.get("request_id") or "")
        future = connection.pending.pop(request_id, None)
        if future is None or future.done():
            return
        if message.get("ok") is True:
            result = message.get("result")
            future.set_result(dict(result) if isinstance(result, Mapping) else {})
            return
        error = message.get("error")
        future.set_exception(DeviceSessionRefused.from_dict(
            dict(error) if isinstance(error, Mapping) else {},
        ))

    # ── 提交侧入口 ────────────────────────────────────────────────────────────
    async def _validate_identity(self, connection: _Connection) -> None:
        # 每次派发都读取控制面的登记事实。仅比较在线连接无法阻止控制面重启后旧代接入。
        try:
            snapshot = await asyncio.to_thread(self._host_snapshot, connection.host_id)
        except Exception as exc:
            raise DeviceSessionRefused(
                "device session identity authority unavailable",
                status=503, code="device_session_unavailable",
            ) from exc
        rig = next((row for row in snapshot.get("rigs", [])
                    if row.get("rig_id") == connection.rig_id), None)
        target = next((row for row in snapshot.get("targets", [])
                       if row.get("rig_id") == connection.rig_id), None)
        identity_matches = (
            rig is not None and target is not None
            and rig.get("host_id") == target.get("host_id") == connection.host_id
            and rig.get("generation") == target.get("rig_generation") == connection.generation
        )
        if not identity_matches:
            raise DeviceSessionRefused(
                "device session identity is not the registered Rig generation",
                status=409, code="stale_device_session",
            )
        vm_name = PurePosixPath(str(target.get("vm_dir") or "")).name
        allowed = {connection.rig_id}
        if vm_name:
            allowed.add(vm_name)
        if not set(connection.devices).issubset(allowed):
            raise DeviceSessionRefused(
                "device session identity does not own the requested device keys",
                status=409, code="stale_device_session",
            )

    async def _retire(self, connection: _Connection, error: DeviceSessionRefused) -> None:
        async with self._lock:
            for device in connection.devices:
                if self._by_device.get(device) is connection:
                    self._by_device.pop(device, None)
        connection.fail_pending(error)
        try:
            # rig 读循环退出后释放该连接持有的 console lease。
            await connection.websocket.close(code=1008)
        except (RuntimeError, WebSocketDisconnect):
            pass

    async def call(self, device: str, op: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if op not in DEVICE_SESSION_OPS:
            raise DeviceSessionRefused(
                f"unknown device session op: {op}", status=422, code="unknown_op",
            )
        async with self._lock:
            connection = self._by_device.get(str(device or "").strip())
        if connection is None:
            raise DeviceSessionRefused(
                f"no rig agent is serving device {device!r} on the interactive channel",
                status=503,
                code="device_session_unavailable",
            )
        try:
            await self._validate_identity(connection)
        except DeviceSessionRefused as exc:
            await self._retire(connection, exc)
            raise
        request_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        connection.pending[request_id] = future
        try:
            async with connection.send_lock:
                await connection.websocket.send_json({
                    "type": "request",
                    "request_id": request_id,
                    # 别名只用于寻址；执行侧统一使用 Rig ID，保证 console 与 run 共用 ScreenLock。
                    "device": connection.rig_id,
                    "op": op,
                    "payload": dict(payload or {}),
                })
        except (RuntimeError, WebSocketDisconnect) as exc:
            connection.pending.pop(request_id, None)
            raise DeviceSessionRefused(
                f"device session channel send failed: {exc}",
                status=503,
                code="device_session_unavailable",
            ) from exc
        try:
            return await asyncio.wait_for(future, timeout=self.response_timeout_s)
        except asyncio.TimeoutError as exc:
            connection.pending.pop(request_id, None)
            raise DeviceSessionRefused(
                f"rig agent did not answer op {op!r} within {self.response_timeout_s}s",
                status=503,
                code="device_session_timeout",
            ) from exc

    async def connected_devices(self) -> list[dict[str, Any]]:
        async with self._lock:
            return sorted(
                (
                    {
                        "device": device,
                        "host_id": connection.host_id,
                        "rig_id": connection.rig_id,
                        "rig_generation": connection.generation,
                    }
                    for device, connection in self._by_device.items()
                ),
                key=lambda row: row["device"],
            )


def _validate_hello(websocket: WebSocket, hello: Any) -> _Connection:
    if not isinstance(hello, Mapping) or hello.get("type") != "hello":
        raise ValueError("device session hello is required")
    host_id = str(hello.get("host_id") or "").strip()
    rig_id = str(hello.get("rig_id") or "").strip()
    generation = int(hello.get("rig_generation") or 0)
    devices = [str(item).strip() for item in (hello.get("devices") or []) if str(item).strip()]
    if not host_id or not rig_id or generation <= 0:
        raise ValueError("hello requires host_id, rig_id and a positive rig_generation")
    if not devices:
        raise ValueError("hello requires at least one device key")
    return _Connection(
        websocket,
        host_id=host_id,
        rig_id=rig_id,
        generation=generation,
        devices=sorted(set(devices)),
    )


def create_device_session_router(gateway: DeviceSessionGateway) -> APIRouter:
    """交互通道的 HTTP 面：提交侧（web 进程）经它把操作交给控制面。"""

    router = APIRouter()

    @router.get("/api/control/device-sessions")
    async def list_device_sessions() -> dict[str, Any]:
        return {"devices": await gateway.connected_devices()}

    @router.post("/api/control/device-sessions/{device}/ops")
    async def call_device_session(device: str, payload: dict | None = None) -> dict[str, Any]:
        body = payload or {}
        try:
            result = await gateway.call(
                device,
                str(body.get("op") or ""),
                body.get("payload") if isinstance(body.get("payload"), dict) else {},
            )
        except DeviceSessionRefused as exc:
            raise HTTPException(
                status_code=exc.status,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        return {"ok": True, "result": result}

    return router


def mount_device_session_channel(
    app: Any, *, host_snapshot: Callable[[str], Mapping[str, Any]],
) -> DeviceSessionGateway:
    """把交互通道的 HTTP 面与 rig WebSocket 挂到一个 FastAPI app 上。

    生产的 `autophone-control` 与测试 / 基准脚本用同一个函数装配，路径与形状不会分叉。
    """
    gateway = DeviceSessionGateway(host_snapshot=host_snapshot)
    app.include_router(create_device_session_router(gateway))

    @app.websocket(DEVICE_SESSION_WS_PATH)
    async def device_session_socket(websocket: WebSocket) -> None:
        await gateway.connect(websocket)

    return gateway


__all__ = [
    "DEVICE_SESSION_WS_PATH",
    "DeviceSessionGateway",
    "create_device_session_router",
    "mount_device_session_channel",
]
