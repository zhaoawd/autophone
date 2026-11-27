"""Minimal `autophone.device_session` provider example.

Loaded by `autophone.rig.plugins.create_device_session_ops(target=..., run_root=...)`
or directly via `--device-session-factory` / `AUTOPHONE_AGENT_DEVICE_SESSION_FACTORY`.
"""
from __future__ import annotations

import secrets
import threading
from pathlib import Path
from typing import Any

from autophone.application.device_session import (
    DEVICE_SESSION_OPS,
    DeviceSessionRefused,
)
from autophone.rig.target import DeviceTarget, served_device_keys


class TemplateDeviceSessionOps:
    """Minimal `DeviceSessionExecutor` implementation for interactive Rig channels."""

    def __init__(self, *, target: DeviceTarget, run_root: Path | None = None) -> None:
        self.target = target
        self.run_root = run_root
        self._served_keys = set(served_device_keys(target))
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def call(self, device: str, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Dispatch one interactive channel operation or fail closed with DeviceSessionRefused."""
        normalized_device = str(device or "").strip()
        if not normalized_device:
            raise DeviceSessionRefused("device is required", status=422, code="validation")
        if normalized_device not in self._served_keys:
            raise DeviceSessionRefused(
                "device is not served by this Rig",
                status=409,
                code="device_mismatch",
            )
        if op not in DEVICE_SESSION_OPS:
            raise DeviceSessionRefused(
                f"unknown device session op: {op}",
                status=422,
                code="unknown_op",
            )

        if op == "device_status":
            with self._lock:
                active = len(self._sessions)
            return {
                "device": self.target.rig_id,
                "online": True,
                "active_sessions": active,
                "target": self.target.public_dict(),
            }
        if op == "screen_holder":
            if payload:
                raise DeviceSessionRefused(
                    "screen_holder accepts no parameters",
                    status=422,
                    code="validation",
                )
            with self._lock:
                holder = next(iter(self._sessions.values()), None)
            return {"device": self.target.rig_id, "holder": dict(holder) if holder else None}
        if op == "console_device_status":
            with self._lock:
                session = next(iter(self._sessions.values()), None)
            return {"device": self.target.rig_id, "busy": session is not None, "session": session}
        if op == "console_start":
            request_id = str(payload.get("request_id") or "")
            with self._lock:
                if request_id:
                    for existing in self._sessions.values():
                        if existing.get("request_id") == request_id:
                            return dict(existing)
                if self._sessions:
                    raise DeviceSessionRefused("device console is busy", status=409, code="conflict")
                session_id = f"tpl-{secrets.token_hex(6)}"
                record = {
                    "session_id": session_id,
                    "device": self.target.rig_id,
                    "request_id": request_id or None,
                }
                self._sessions[session_id] = record
                return dict(record)
        if op == "console_heartbeat":
            session_id = str(payload.get("session_id") or "").strip()
            with self._lock:
                if session_id not in self._sessions:
                    raise DeviceSessionRefused("session not found", status=404, code="not_found")
                return {"session_id": session_id, "device": self.target.rig_id, "alive": True}
        if op == "console_stop":
            session_id = str(payload.get("session_id") or "").strip()
            with self._lock:
                removed = self._sessions.pop(session_id, None)
            if removed is None:
                raise DeviceSessionRefused("session not found", status=404, code="not_found")
            return {"session_id": session_id, "closed": True}

        raise DeviceSessionRefused(
            f"operation {op!r} is not implemented by template provider",
            status=501,
            code="not_implemented",
        )

    def close_all(self, reason: str) -> dict[str, Any]:
        """Release all active sessions immediately when the channel disconnects."""
        with self._lock:
            closed = sorted(self._sessions.keys())
            self._sessions.clear()
        return {
            "closed_sessions": closed,
            "closed_previews": 0,
            "reason": reason,
        }


def create_device_session_ops(
    *,
    target: DeviceTarget,
    run_root: Path | None = None,
) -> TemplateDeviceSessionOps:
    """Entry-point factory matching `autophone.rig.main` keyword arguments."""
    return TemplateDeviceSessionOps(target=target, run_root=run_root)
