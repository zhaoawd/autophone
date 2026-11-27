"""录制会话操作：在设备持有进程执行，HTTP 只提交意图。

录制目录继续使用部署的共享 STATE_ROOT；不提供跨主机产物复制。
管理器按需创建，控制面模式的 Web 启动不会恢复或扫描 rig 会话。
"""
from __future__ import annotations

import base64
import json
import re
import threading
from pathlib import Path
from typing import Any, Callable

from autophone.paths import APP_ASSET_ROOT, STATE_ROOT
from autophone.record import recordings
from autophone.record.web import WebRecordError, WebRecordManager

from autophone.rig.session_protocol import RECORDING_SESSION_OPS


def recording_device(app: str, recording_id: str, *, root: Path = APP_ASSET_ROOT) -> str:
    try:
        directory = recordings.resolve_recording_id(recording_id, root=root)
    except recordings.RecordingError as exc:
        raise WebRecordError(str(exc), status=422, code="validation") from exc
    try:
        meta = json.loads((directory / recordings.META_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WebRecordError("recording not found", status=404, code="not_found") from exc
    if not isinstance(meta, dict) or meta.get("app") != app or not meta.get("device"):
        raise WebRecordError("recording not found", status=404, code="not_found")
    return str(meta["device"])


class RecordingSessionOps:
    def __init__(
        self, *, lock: Any, root: Path = APP_ASSET_ROOT, device: str | None = None,
        manager_factory: Callable[..., Any] = WebRecordManager,
        device_aliases: tuple[str, ...] = (),
    ) -> None:
        self.lock, self.root, self.device = lock, root, device
        self.device_aliases = device_aliases
        self._factory = manager_factory
        self._manager: Any = None
        self._guard = threading.RLock()
        self._desktop: dict[str, tuple[str, str]] = {}

    def _web(self) -> Any:
        with self._guard:
            if self._manager is None:
                self._manager = self._factory(root=self.root, lock=self.lock, device=self.device, device_aliases=self.device_aliases)
                self._manager.start_scanner()
            return self._manager

    def call(self, device: str, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        if op not in RECORDING_SESSION_OPS:
            raise WebRecordError("unknown recording operation", status=422, code="unknown_op")
        if self.device is not None and device != self.device:
            raise WebRecordError("recording device mismatch", status=409, code="device_mismatch")
        app = str(payload.get("app") or "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", app):
            raise WebRecordError("invalid app", status=422, code="validation")
        rid = str(payload.get("recording_id") or "")
        if op != "record_start":
            owner = recording_device(app, rid, root=self.root)
            if owner != device and owner not in self.device_aliases:
                raise WebRecordError("recording device mismatch", status=409, code="device_mismatch")
        token = payload.get("controller_token")
        try:
            if op == "record_start":
                state_root = STATE_ROOT if self.root == APP_ASSET_ROOT else self.root
                if payload.get("state_root") != str(state_root.resolve()):
                    raise WebRecordError("recording state root mismatch", status=409, code="recording_store_mismatch")
                surface = str(payload.get("surface") or "desktop")
                result = recordings.start_recording(
                    app, build=str(payload.get("build") or ""),
                    starts_with=payload.get("starts_with"), device=device,
                    script=payload.get("script"), root=self.root, surface=surface,
                    web_manager=self._web() if surface in {"web", "native"} else None, lock=self.lock,
                )
                if surface == "desktop":
                    self._desktop[result["recording_id"]] = (app, device)
                    self.lock.mark_recording(device=device, recording_id=result["recording_id"], pid=int(result.get("pid") or 0))
                return result
            if op == "record_desktop_cancel":
                result = recordings.cancel_recording(app, rid, root=self.root)
                self._desktop.pop(rid, None)
                self.lock.clear_recording(device=device, recording_id=rid)
                return result
            manager = self._web()
            if op == "record_frame":
                data, seq, captured_at = manager.frame(app, rid)
                return {"jpeg_b64": base64.b64encode(data).decode("ascii"), "seq": seq, "captured_at": captured_at}
            if op == "record_view":
                return manager.view(app, rid)
            method = getattr(manager, op.removeprefix("record_"))
            if op in {"record_event", "record_label"}:
                return method(app, rid, payload.get("body") or {}, token)
            return method(app, rid, token)
        except FileExistsError as exc:
            raise WebRecordError(str(exc), status=409, code="conflict") from exc
        except FileNotFoundError as exc:
            raise WebRecordError(str(exc), status=404, code="not_found") from exc
        except recordings.DeviceBusy as exc:
            raise WebRecordError(str(exc), status=409, code="device_busy") from exc
        except recordings.RecordingError as exc:
            raise WebRecordError(str(exc), status=422, code="validation") from exc
        except OSError as exc:
            raise WebRecordError(str(exc), status=502, code="recording_io_error") from exc

    def close_all(self, reason: str) -> dict[str, Any]:
        with self._guard:
            manager, self._manager = self._manager, None
            try:
                if manager is not None:
                    manager.shutdown(reason=reason)
            finally:
                errors = []
                for rid, (app, device) in list(self._desktop.items()):
                    try:
                        directory = recordings.resolve_recording_id(rid, root=self.root)
                        if directory.exists() and not (directory / "trace.json").exists():
                            recordings.cancel_recording(app, rid, root=self.root)
                        self.lock.clear_recording(device=device, recording_id=rid)
                        self._desktop.pop(rid, None)
                    except Exception as exc:
                        errors.append(exc)
                if errors:
                    raise errors[0]
        return {"reason": reason}
