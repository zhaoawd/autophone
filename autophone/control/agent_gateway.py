"""WebSocket gateway for Rig Agent notification and request handling."""
from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Mapping

from fastapi.encoders import jsonable_encoder
from fastapi import WebSocket, WebSocketDisconnect

from autophone.logging import get_logger
from autophone.rig.ack_contract import agent_ack

from ._psycopg import psycopg_error_types, psycopg_errors_by_name
from .postgres import ControlStateError
from .interface import AgentControl

if TYPE_CHECKING:  # pragma: no cover
    import psycopg


logger = get_logger(__name__)
AGENT_CONNECTION_REFRESH_SECONDS = 15.0
TRANSIENT_RETRY_DELAYS_S = (0.2, 0.5)
_RETRYABLE_DATABASE_ERROR_NAMES = (
    "DeadlockDetected",
    "SerializationFailure",
    "LockNotAvailable",
)


class RigAgentGateway:
    def __init__(self, control: AgentControl) -> None:
        self.control = control
        self._connections: dict[tuple[str, str], WebSocket] = {}
        self._send_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        identity: tuple[str, str] | None = None
        connection_id = uuid.uuid4().hex
        liveness: asyncio.Task | None = None
        try:
            hello = await websocket.receive_json()
            if not isinstance(hello, Mapping) or hello.get("type") != "hello":
                raise ControlStateError("hello is required")
            host_id = str(hello.get("host_id") or "")
            rig_id = str(hello.get("rig_id") or "")
            generation = int(hello.get("rig_generation") or 0)
            if not host_id or not rig_id or generation <= 0:
                raise ControlStateError(
                    "Agent hello requires host_id, rig_id and a positive rig_generation"
                )
            target = dict(hello.get("target") or {})
            if target and (
                str(target.get("host_id") or "") != host_id
                or str(target.get("rig_id") or "") != rig_id
                or int(target.get("rig_generation") or 0) != generation
            ):
                raise ControlStateError("Agent target identity does not match hello identity")
            # BL-151：源码身份是 agent 自报的诊断/准入字段，形态不对就当没报（存 null），
            # 不因此拒绝握手——握手被拒会让整台 rig 不可用，比「身份未知」更严重。
            raw_identity = hello.get("source_identity")
            source_identity = dict(raw_identity) if isinstance(raw_identity, Mapping) else None
            assignments = self.control.register_agent(
                host_id=host_id,
                rig_id=rig_id,
                generation=generation,
                capability_support=list(hello.get("capability_support") or []),
                target=target or None,
                source_identity=source_identity,
            )
            self.control.begin_agent_connection(host_id, rig_id, generation, connection_id)
        except (ControlStateError, KeyError, TypeError, ValueError) as exc:
            logger.warning("agent handshake rejected: %s: %s", type(exc).__name__, exc)
            try:
                await websocket.send_json({
                    "type": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                })
                await websocket.close(code=1008)
            except (RuntimeError, WebSocketDisconnect):
                pass
            return

        try:
            identity = (host_id, rig_id)
            logger.info(
                "agent connected: host=%s rig=%s generation=%s capabilities=%s resync=%d",
                host_id,
                rig_id,
                generation,
                list(hello.get("capability_support") or []),
                len(assignments),
            )
            async with self._lock:
                previous = self._connections.get(identity)
                self._connections[identity] = websocket
                self._send_locks[identity] = asyncio.Lock()
            if previous is not None and previous is not websocket:
                await previous.close(code=1012)
            await self._send(
                identity,
                websocket,
                {"type": "sync", "assignments": assignments},
            )
            liveness = asyncio.create_task(
                self._maintain_connection(websocket, rig_id, connection_id),
            )
            while True:
                message = await websocket.receive_json()
                await self._handle(websocket, identity, generation, message)
        except WebSocketDisconnect:
            pass
        finally:
            if liveness is not None:
                liveness.cancel()
                with suppress(asyncio.CancelledError):
                    await liveness
            if identity is not None:
                logger.info("agent disconnected: host=%s rig=%s", identity[0], identity[1])
                async with self._lock:
                    if self._connections.get(identity) is websocket:
                        self._connections.pop(identity, None)
                        self._send_locks.pop(identity, None)
            try:
                self.control.end_agent_connection(rig_id, connection_id)
            except Exception:
                # The persisted deadline still expires if PostgreSQL is unavailable.
                logger.exception("agent disconnect persistence failed: rig=%s", rig_id)

    async def _maintain_connection(
        self, websocket: WebSocket, rig_id: str, connection_id: str,
    ) -> None:
        try:
            while True:
                await asyncio.sleep(AGENT_CONNECTION_REFRESH_SECONDS)
                if not self.control.refresh_agent_connection(rig_id, connection_id):
                    break
        except Exception:
            logger.exception("agent connection liveness update failed: rig=%s", rig_id)
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close(code=1012)

    async def notify(self, assignment: Mapping[str, Any]) -> bool:
        host_id = str(assignment.get("bound_host_id") or "")
        rig_id = str(assignment.get("bound_rig_id") or "")
        assignment_id = str(assignment.get("assignment_id") or "")
        attempt = assignment.get("current_attempt_no")
        fencing = assignment.get("current_fencing")
        if not host_id or not rig_id:
            self._log_notification_failure(
                assignment_id, attempt, fencing, host_id, rig_id, reason="missing_target",
            )
            return False
        async with self._lock:
            websocket = self._connections.get((host_id, rig_id))
        if websocket is None:
            self._log_notification_failure(
                assignment_id, attempt, fencing, host_id, rig_id, reason="agent_disconnected",
            )
            return False
        try:
            await self._send(
                (host_id, rig_id),
                websocket,
                {
                    "type": "assignment",
                    # PostgreSQL projections are already normalized by
                    # PostgresControlStore._jsonable. Keep the WebSocket boundary
                    # safe for in-process controls and future projection sources too.
                    "assignment": jsonable_encoder(dict(assignment)),
                },
            )
        except Exception as exc:  # noqa: BLE001 - transport failures are reported, not fatal
            self._log_notification_failure(
                assignment_id,
                attempt,
                fencing,
                host_id,
                rig_id,
                reason=f"send_error:{type(exc).__name__}",
                exc_info=True,
            )
            return False
        return True

    @staticmethod
    def _log_notification_failure(
        assignment_id: str,
        attempt: Any,
        fencing: Any,
        host_id: str,
        rig_id: str,
        *,
        reason: str,
        exc_info: bool = False,
    ) -> None:
        logger.error(
            "event=assignment_notification_failed assignment_id=%s attempt=%s fencing=%s "
            "host_id=%s rig_id=%s reason=%s",
            assignment_id or "unknown",
            attempt,
            fencing,
            host_id or "unknown",
            rig_id or "unknown",
            reason,
            exc_info=exc_info,
        )

    async def connected_rigs(self) -> set[tuple[str, str]]:
        async with self._lock:
            return set(self._connections)

    async def _handle(
        self,
        websocket: WebSocket,
        identity: tuple[str, str],
        generation: int,
        message: Mapping[str, Any],
    ) -> None:
        """Answer one message; a database error must not take the connection down with it.

        断开连接会让运行中的 Agent 退出、Rig 被隔离（2026-11-04 实测）。死锁这类错误的事务
        已回滚，重试是安全的。其他数据库错误不确定是否已提交，回 error 交给 Agent 处理。
        """
        for delay in (*TRANSIENT_RETRY_DELAYS_S, None):
            try:
                await self._handle_once(websocket, identity, generation, message)
                return
            except psycopg_errors_by_name(*_RETRYABLE_DATABASE_ERROR_NAMES) as exc:
                if delay is None:
                    await self._reply_database_error(websocket, identity, message, exc)
                    return
                logger.warning("agent message retried: type=%s %s", message.get("type"), type(exc).__name__)
                await asyncio.sleep(delay)
            except psycopg_error_types() as exc:
                await self._reply_database_error(websocket, identity, message, exc)
                return

    async def _reply_database_error(
        self,
        websocket: WebSocket,
        identity: tuple[str, str],
        message: Mapping[str, Any],
        exc: psycopg.Error,
    ) -> None:
        logger.error("agent message failed: type=%s rig=%s %s: %s",
                     message.get("type"), identity[1], type(exc).__name__, exc)
        await self._send(identity, websocket, {
            "type": "error",
            "request_id": str(message.get("request_id") or ""),
            "error": f"{type(exc).__name__}: {exc}",
        })

    async def _handle_once(
        self,
        websocket: WebSocket,
        identity: tuple[str, str],
        generation: int,
        message: Mapping[str, Any],
    ) -> None:
        request_id = str(message.get("request_id") or "")
        kind = str(message.get("type") or "")
        try:
            if kind == "sync":
                result: Any = self.control.sync_rig(*identity)
            elif kind == "heartbeat":
                result = self.control.heartbeat(
                    str(message["assignment_id"]),
                    int(message["attempt_no"]),
                    int(message["fencing"]),
                )
            elif kind == "validate_write":
                if any(key in message for key in ("host_id", "rig_id", "generation")) and (
                    message.get("host_id"), message.get("rig_id"), message.get("generation")
                ) != (identity[0], identity[1], generation):
                    raise ControlStateError("device write connection identity mismatch")
                result = self.control.validate_device_write(
                    str(message["assignment_id"]),
                    int(message["attempt_no"]),
                    int(message["fencing"]),
                    host_id=identity[0],
                    rig_id=identity[1],
                    generation=generation,
                )
            elif kind == "stage_result":
                result = self.control.record_stage_result(
                    str(message["assignment_id"]),
                    int(message["attempt_no"]),
                    int(message["fencing"]),
                    str(message["stage"]),
                    dict(message.get("result") or {}),
                )
            elif kind == "read_observed_state":
                result = self.control.read_observed_state(identity[0], identity[1], generation)
            elif kind == "assignment_observed_state":
                if (message.get("host_id"), message.get("rig_id"), message.get("generation")) != (
                    identity[0], identity[1], generation,
                ):
                    raise ControlStateError("installation observation connection identity mismatch")
                result = self.control.publish_assignment_observed_state(
                    str(message["assignment_id"]), int(message["attempt_no"]), int(message["fencing"]),
                    host_id=identity[0], rig_id=identity[1], generation=generation,
                    observed_state=dict(message.get("observed_state") or {}),
                    capability_state=dict(message.get("capability_state") or {}),
                )
            elif kind == "observed_state":
                result = self.control.update_observed_state(
                    identity[1],
                    generation,
                    dict(message.get("observed_state") or {}),
                    dict(message.get("capability_state") or {}),
                )
            elif kind == "screen_quarantine":
                # 已握手的身份决定上报哪台 Rig，消息体不能切换（BL-224）。
                result = self.control.report_screen_quarantine(
                    identity[0], identity[1], generation, dict(message.get("state") or {}),
                )
            elif kind == "complete":
                result = self.control.complete_assignment(
                    str(message["assignment_id"]),
                    int(message["attempt_no"]),
                    int(message["fencing"]),
                    dict(message.get("result") or {}),
                )
            elif kind == "decline":
                result = self.control.decline_assignment(
                    str(message["assignment_id"]),
                    int(message["attempt_no"]),
                    int(message["fencing"]),
                    reason=str(message["reason"]),
                    detail=str(message.get("detail") or ""),
                )
            elif kind == "attempt_interrupted":
                # BL-240：新 Agent 进程重连时报告本地只有「开始」记录的 Attempt。已握手的身份决定
                # Rig，消息体只能带同一身份。旧控制面不认识该消息，回 error，Agent 等 Lease 过期。
                if any(key in message for key in ("host_id", "rig_id", "generation")) and (
                    message.get("host_id"), message.get("rig_id"), message.get("generation")
                ) != (identity[0], identity[1], generation):
                    raise ControlStateError("interrupted attempt connection identity mismatch")
                result = self.control.report_interrupted_attempt(
                    str(message["assignment_id"]),
                    int(message["attempt_no"]),
                    int(message["fencing"]),
                    host_id=identity[0],
                    rig_id=identity[1],
                    generation=generation,
                    reason=str(message["reason"]),
                    detail=str(message.get("detail") or ""),
                )
            else:
                raise ControlStateError(f"unknown agent message: {kind}")
            await self._send(
                identity,
                websocket,
                # ack 只带 Agent 读取的字段（BL-083 第三轮）：整条 Assignment 投影随 Run Result
                # 增长，超过 Agent websocket 客户端 1 MiB 接收上限时 Agent 判定完成未确认后退出。
                {"type": "ack", "request_id": request_id, "result": agent_ack(kind, result)},
            )
        except (ControlStateError, KeyError, TypeError, ValueError) as exc:
            await self._send(
                identity,
                websocket,
                {
                    "type": "error",
                    "request_id": request_id,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )

    async def _send(
        self,
        identity: tuple[str, str],
        websocket: WebSocket,
        message: Mapping[str, Any],
    ) -> None:
        async with self._lock:
            lock = self._send_locks.get(identity)
        if lock is None:
            raise WebSocketDisconnect(code=1012)
        async with lock:
            await websocket.send_json(dict(message))
