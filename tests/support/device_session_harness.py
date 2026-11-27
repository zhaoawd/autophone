"""Minimal real uvicorn + WebSocket + HTTP harness for device-session channel tests."""
from __future__ import annotations

import socket
import threading
import time
from typing import Any

from autophone.application.device_session import DeviceSessionOps, RemoteDeviceSessions
from autophone.control.device_session import mount_device_session_channel
from autophone.rig.device_session import DeviceSessionAgent, device_session_ws_url

PREVIEW_FRAME = b"\xff\xd8" + b"f" * 90_000


class _BenchConsole:
    """In-memory console stub preserving channel payload shapes without touching a device."""

    def __init__(self) -> None:
        self.sessions: dict[str, str] = {}
        self.actions = 0

    def device_status(self, device: str) -> dict[str, Any]:
        return {"device": device, "active": bool(self.sessions)}

    def start(self, device: str, *, request_id: str | None = None) -> dict[str, Any]:
        self.sessions["ctl-bench"] = device
        return {
            "session_id": "ctl-bench", "device": device, "state": "live",
            "lifecycle": "active", "request_id": request_id,
        }

    def view(self, session_id: str, *, device: str | None = None) -> dict[str, Any]:
        return {"session_id": session_id, "device": device, "seq": self.actions}

    def frame(self, session_id: str, *, device: str | None = None) -> tuple[bytes, int, float]:
        return PREVIEW_FRAME, self.actions, time.time()

    def event(self, session_id, payload, token, *, device=None) -> dict[str, Any]:
        self.actions += 1
        return {"event_id": payload.get("event_id"), "ok": True, "index": self.actions}

    def heartbeat(self, session_id, token, *, device=None) -> dict[str, Any]:
        return {"ok": True}

    def stop(self, session_id, token, *, device=None, reason="stopped") -> dict[str, Any]:
        self.sessions.pop(session_id, None)
        return {"session_id": session_id, "reason": reason, "closed_reason": reason}

    def close_all(self, reason: str) -> int:
        closed = len(self.sessions)
        self.sessions.clear()
        return closed


def build_bench_ops() -> DeviceSessionOps:
    return DeviceSessionOps(
        console=_BenchConsole(),
        screenshot=lambda: PREVIEW_FRAME,
        status=lambda device: {"device": device, "online": True, "lock": {}, "queue_depth": 0},
        target=lambda: {"device": "bench", "sock": "/dev/null"},
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_until(predicate, message: str, *, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise RuntimeError(message)


class ChannelHarness:
    """Real uvicorn + real WebSocket + real HTTP device-session channel test harness."""

    def __init__(self, *, device: str = "bench-rig", host: str = "127.0.0.1") -> None:
        from fastapi import FastAPI

        self.device = device
        self.host = host
        self.port = _free_port()
        self.app = FastAPI(title="device-session-bench")
        self.gateway = mount_device_session_channel(self.app, host_snapshot=lambda host_id: {
            "rigs": [{"host_id": "bench-host", "rig_id": self.device, "generation": 1}],
            "targets": [{"host_id": "bench-host", "rig_id": self.device,
                         "rig_generation": 1, "vm_dir": "/bench/" + self.device}],
        })
        self.ops = build_bench_ops()
        self.base_url = f"http://{host}:{self.port}"
        self.agent: DeviceSessionAgent | None = None
        self._server: Any = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "ChannelHarness":
        import uvicorn

        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        _wait_until(lambda: bool(getattr(self._server, "started", False)), "uvicorn did not start")
        self.agent = DeviceSessionAgent(
            device_session_ws_url(f"ws://{self.host}:{self.port}/api/control/agent/ws"),
            host_id="bench-host",
            rig_id=self.device,
            rig_generation=1,
            devices=[self.device],
            ops=self.ops,
        )
        self.agent.start()
        _wait_until(self.agent.connected.is_set, "device session agent did not connect")
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self.agent is not None:
            self.agent.stop()
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)

    def client(self) -> RemoteDeviceSessions:
        return RemoteDeviceSessions(self.base_url)
