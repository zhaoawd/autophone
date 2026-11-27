"""In-process and WebSocket transports for Rig Agent."""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from typing import TYPE_CHECKING, Any, Mapping

from .ack_contract import agent_ack
from .target import DeviceTarget

if TYPE_CHECKING:
    from autophone.control.interface import AgentControl


class AgentTransportError(RuntimeError):
    pass


class InProcessAgentTransport:
    """进程内传输：返回值与 websocket ack 同一字段契约（`ack_contract`）。"""

    def __init__(self, control: "AgentControl") -> None:
        self.control = control
        self.target: DeviceTarget | None = None
        self._assignments: deque[dict[str, Any]] = deque()

    def connect(
        self,
        target: DeviceTarget,
        capability_support: list[str],
        source_identity: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.target = target
        return self.control.register_agent(
            host_id=target.host_id,
            rig_id=target.rig_id,
            generation=target.rig_generation,
            capability_support=capability_support,
            target=target.public_dict(),
            source_identity=dict(source_identity) if source_identity else None,
        )

    def enqueue(self, assignment: Mapping[str, Any]) -> None:
        self._assignments.append(dict(assignment))

    def receive_assignment(self) -> dict[str, Any] | None:
        return self._assignments.popleft() if self._assignments else None

    def heartbeat(self, assignment_id: str, attempt_no: int, fencing: int) -> dict[str, Any]:
        return agent_ack("heartbeat", self.control.heartbeat(assignment_id, attempt_no, fencing))

    def validate_write(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        target: DeviceTarget,
    ) -> dict[str, Any]:
        return self.control.validate_device_write(
            assignment_id,
            attempt_no,
            fencing,
            host_id=target.host_id,
            rig_id=target.rig_id,
            generation=target.rig_generation,
        )

    def record_stage(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        stage: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        return agent_ack("stage_result", self.control.record_stage_result(
            assignment_id, attempt_no, fencing, stage, result,
        ))

    def read_observed_state(self, target: DeviceTarget) -> dict[str, Any]:
        return self.control.read_observed_state(target.host_id, target.rig_id, target.rig_generation)

    def publish_assignment_observed_state(
        self, target, assignment_id, attempt_no, fencing, observed_state, capability_state,
    ) -> dict[str, Any]:
        return agent_ack("assignment_observed_state", self.control.publish_assignment_observed_state(
            assignment_id, attempt_no, fencing,
            host_id=target.host_id, rig_id=target.rig_id, generation=target.rig_generation,
            observed_state=observed_state, capability_state=capability_state,
        ))

    def update_observed_state(
        self,
        target: DeviceTarget,
        observed_state: Mapping[str, Any],
        capability_state: Mapping[str, Any],
    ) -> dict[str, Any]:
        return agent_ack("observed_state", self.control.update_observed_state(
            target.rig_id,
            target.rig_generation,
            observed_state,
            capability_state,
        ))

    def report_screen_quarantine(self, state: Mapping[str, Any]) -> dict[str, Any]:
        if self.target is None:
            raise AgentTransportError("agent transport is not connected")
        return self.control.report_screen_quarantine(
            self.target.host_id, self.target.rig_id, self.target.rig_generation, dict(state),
        )

    def complete(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        return agent_ack("complete", self.control.complete_assignment(
            assignment_id, attempt_no, fencing, result,
        ))

    def decline(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        *,
        reason: str,
        detail: str,
    ) -> dict[str, Any]:
        return agent_ack("decline", self.control.decline_assignment(
            assignment_id, attempt_no, fencing, reason=reason, detail=detail,
        ))

    def report_interrupted(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        *,
        reason: str,
        detail: str,
    ) -> dict[str, Any]:
        # BL-240：控制面不支持时（旧版本）抛错，Agent 不重放，等 Lease 过期回收。
        if self.target is None:
            raise AgentTransportError("agent transport is not connected")
        report = getattr(self.control, "report_interrupted_attempt", None)
        if not callable(report):
            raise AgentTransportError("unknown agent message: attempt_interrupted")
        return agent_ack("attempt_interrupted", report(
            assignment_id, attempt_no, fencing,
            host_id=self.target.host_id, rig_id=self.target.rig_id,
            generation=self.target.rig_generation, reason=reason, detail=detail,
        ))

    def close(self) -> None:
        return None


class WebSocketAgentTransport:
    def __init__(
        self,
        url: str,
        *,
        open_timeout_s: float = 10.0,
        request_timeout_s: float = 10.0,
    ) -> None:
        self.url = url
        self.open_timeout_s = open_timeout_s
        if request_timeout_s <= 0:
            raise ValueError("request_timeout_s must be positive")
        self.request_timeout_s = request_timeout_s
        self._socket: Any = None
        self._notifications: deque[dict[str, Any]] = deque()
        self._lock = threading.RLock()

    def connect(
        self,
        target: DeviceTarget,
        capability_support: list[str],
        source_identity: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        from websockets.sync.client import connect

        with self._lock:
            self.close()
            self._socket = connect(self.url, open_timeout=self.open_timeout_s)
            self._send({
                "type": "hello",
                "host_id": target.host_id,
                "rig_id": target.rig_id,
                "rig_generation": target.rig_generation,
                "capability_support": capability_support,
                "target": target.public_dict(),
                # BL-151：这条 agent 执行的源码身份。老 agent 不带这个键，控制面存 null。
                "source_identity": dict(source_identity) if source_identity else None,
            })
            message = self._receive(timeout=self.open_timeout_s)
            if message.get("type") != "sync":
                raise AgentTransportError(f"expected sync, got {message.get('type')}")
            return list(message.get("assignments") or [])

    def receive_assignment(self) -> dict[str, Any] | None:
        with self._lock:
            if self._notifications:
                return self._notifications.popleft()
            try:
                message = self._receive(timeout=1.0)
            except TimeoutError:
                return None
            if message.get("type") == "assignment":
                return dict(message.get("assignment") or {})
            if message.get("type") == "sync":
                self._notifications.extend(dict(item) for item in message.get("assignments") or [])
                return self._notifications.popleft() if self._notifications else None
            if message.get("type") == "error":
                raise AgentTransportError(str(message.get("error") or "agent gateway error"))
            return None

    def heartbeat(self, assignment_id: str, attempt_no: int, fencing: int) -> dict[str, Any]:
        return self._request("heartbeat", assignment_id, attempt_no, fencing)

    def validate_write(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        target: DeviceTarget,
    ) -> dict[str, Any]:
        return self._request(
            "validate_write", assignment_id, attempt_no, fencing,
            host_id=target.host_id, rig_id=target.rig_id, generation=target.rig_generation,
        )

    def record_stage(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        stage: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "stage_result",
            assignment_id,
            attempt_no,
            fencing,
            stage=stage,
            result=dict(result),
        )

    def read_observed_state(self, target: DeviceTarget) -> dict[str, Any]:
        # Gateway 使用已握手的身份，调用方不能在消息体中切换 Rig。
        return self._request("read_observed_state")

    def publish_assignment_observed_state(
        self, target, assignment_id, attempt_no, fencing, observed_state, capability_state,
    ) -> dict[str, Any]:
        return self._request(
            "assignment_observed_state", assignment_id, attempt_no, fencing,
            host_id=target.host_id, rig_id=target.rig_id, generation=target.rig_generation,
            observed_state=dict(observed_state), capability_state=dict(capability_state),
        )

    def update_observed_state(
        self,
        target: DeviceTarget,
        observed_state: Mapping[str, Any],
        capability_state: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            request_id = uuid.uuid4().hex
            self._send({
                "type": "observed_state",
                "request_id": request_id,
                "observed_state": dict(observed_state),
                "capability_state": dict(capability_state),
            })
            deadline = time.monotonic() + self.request_timeout_s
            while True:
                message = self._receive(timeout=max(0.0, deadline - time.monotonic()))
                if message.get("type") == "assignment":
                    self._notifications.append(dict(message.get("assignment") or {}))
                    continue
                if message.get("request_id") != request_id:
                    continue
                if message.get("type") == "error":
                    raise AgentTransportError(str(message.get("error") or "agent gateway error"))
                return dict(message.get("result") or {})

    def report_screen_quarantine(self, state: Mapping[str, Any]) -> dict[str, Any]:
        # Gateway 使用已握手的身份（host、rig、generation），消息体只带状态（BL-224）。
        return self._request("screen_quarantine", state=dict(state))

    def complete(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "complete",
            assignment_id,
            attempt_no,
            fencing,
            result=dict(result),
        )

    def decline(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        *,
        reason: str,
        detail: str,
    ) -> dict[str, Any]:
        # 执行器之前拒单（BL-214）；控制面按 Lease 与 Fencing 核对，只接受未开始的 Attempt。
        return self._request(
            "decline", assignment_id, attempt_no, fencing, reason=reason, detail=detail,
        )

    def report_interrupted(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        *,
        reason: str,
        detail: str,
    ) -> dict[str, Any]:
        # BL-240：重连时报告本地只有「开始」记录的 Attempt。Gateway 以握手身份核对；旧控制面
        # 回 `unknown agent message`，这里抛 AgentTransportError。
        return self._request(
            "attempt_interrupted", assignment_id, attempt_no, fencing,
            reason=reason, detail=detail,
        )

    def close(self) -> None:
        with self._lock:
            socket, self._socket = self._socket, None
            if socket is not None:
                socket.close()

    def _request(
        self,
        kind: str,
        assignment_id: str | None = None,
        attempt_no: int | None = None,
        fencing: int | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        with self._lock:
            request_id = uuid.uuid4().hex
            self._send({
                "type": kind,
                "request_id": request_id,
                **({"assignment_id": assignment_id, "attempt_no": attempt_no, "fencing": fencing}
                   if assignment_id is not None else {}),
                **extra,
            })
            deadline = time.monotonic() + self.request_timeout_s
            while True:
                message = self._receive(timeout=max(0.0, deadline - time.monotonic()))
                if message.get("type") == "assignment":
                    self._notifications.append(dict(message.get("assignment") or {}))
                    continue
                if message.get("request_id") != request_id:
                    continue
                if message.get("type") == "error":
                    raise AgentTransportError(str(message.get("error") or "agent gateway error"))
                if message.get("type") != "ack":
                    raise AgentTransportError(f"unexpected response: {message.get('type')}")
                return dict(message.get("result") or {})

    def _send(self, message: Mapping[str, Any]) -> None:
        if self._socket is None:
            raise AgentTransportError("agent transport is not connected")
        self._socket.send(json.dumps(dict(message), ensure_ascii=False))

    def _receive(self, timeout: float | None = None) -> dict[str, Any]:
        if self._socket is None:
            raise AgentTransportError("agent transport is not connected")
        raw = self._socket.recv(timeout=timeout)
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise AgentTransportError("agent gateway message must be an object")
        return message
