"""Browser recording surface: durable sessions around ``RecordSession``.

The browser is deliberately a dumb display and intent source.  Device actions,
shots, geometry and trace production remain owned by ``RecordSession`` here on
the host.  ``trace.json`` is created only by an explicit successful finish.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import weakref
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from autophone.paths import APP_ASSET_ROOT, CODE_ROOT, STATE_ROOT  # noqa: F401 -- CODE_ROOT 保留给既有调用方
from autophone.device.lock import ScreenLease, ScreenLock, default_screen_lock_root
from autophone.util import now_iso
from autophone.device.uds import EXPECTED_PREVIEW, HARDWARE_KEYS
from autophone.observation.record_capture import CaptureError, RecordSession

logger = logging.getLogger(__name__)

ROOT = APP_ASSET_ROOT

META_NAME = "recording.meta.json"
SESSION_NAME = "session.json"
PARTIAL_NAME = "trace.partial.json"
TRACE_NAME = "trace.json"
FRAME_MAX_AGE_S = 10.0
FRAME_MIN_INTERVAL_S = 0.12
HEARTBEAT_TTL_S = 90.0
HEARTBEAT_SCAN_S = 15.0
_ACTIVE_MANAGERS: weakref.WeakValueDictionary[tuple[str, str | None], Any] = weakref.WeakValueDictionary()
_ACTIVE_MANAGERS_GUARD = threading.Lock()


class WebRecordError(RuntimeError):
    def __init__(self, message: str, *, status: int = 409, code: str = "conflict") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _pid_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip(".-")
    return cleaned[:48] or "recording"


def _intent_hash(intent: dict[str, Any]) -> str:
    raw = json.dumps(intent, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _event_intent(event: dict[str, Any]) -> dict[str, Any] | None:
    kind = event.get("kind")
    if kind == "tap" and isinstance(event.get("preview"), list) and len(event["preview"]) == 2:
        return {"kind": "tap", "px": int(event["preview"][0]), "py": int(event["preview"][1])}
    if kind == "type" and isinstance(event.get("text"), str):
        return {"kind": "type", "text": event["text"]}
    if kind == "key" and event.get("name") in HARDWARE_KEYS:
        return {"kind": "key", "name": str(event["name"])}
    if kind == "swipe" and all(
        isinstance(event.get(key), list) and len(event[key]) == 2
        for key in ("from_preview", "to_preview")
    ):
        return {
            "kind": "swipe",
            "px1": int(event["from_preview"][0]), "py1": int(event["from_preview"][1]),
            "px2": int(event["to_preview"][0]), "py2": int(event["to_preview"][1]),
            "ms": int(event.get("ms") or 0),
        }
    return None


def normalize_preview_intent(payload: dict[str, Any]) -> dict[str, Any]:
    """Browser intent → canonical preview-space intent (tap / type / swipe / key).

    Shared by every browser surface that forwards intents to the device (web
    recording, device console): one vocabulary, one range check, so the two
    surfaces can never disagree about what a payload means.  Raises
    ``ValueError`` — callers map it to their own 422.
    """
    kind = str(payload.get("kind") or "")
    if kind == "key":
        name = payload.get("name")
        # 硬件键封闭集：写错的键名在 guest 只换回一句 unknown key，看起来像设备
        # 没反应——在浏览器这一侧就拒掉。
        if name not in HARDWARE_KEYS:
            raise ValueError(f"key name must be one of {'/'.join(HARDWARE_KEYS)}")
        intent = {"kind": kind, "name": str(name)}
    elif kind == "tap":
        intent = {"kind": kind, "px": int(payload["px"]), "py": int(payload["py"])}
    elif kind == "type":
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("type text must be non-empty")
        intent = {"kind": kind, "text": text}
    elif kind == "swipe":
        intent = {"kind": kind, **{k: int(payload[k]) for k in ("px1", "py1", "px2", "py2", "ms")}}
        if intent["ms"] <= 0:
            raise ValueError("swipe ms must be > 0")
    else:
        raise ValueError("kind must be tap, type, swipe or key")
    for key in ("px", "px1", "px2"):
        if key in intent and not 0 <= intent[key] < EXPECTED_PREVIEW[0]:
            raise ValueError(f"{key} outside preview")
    for key in ("py", "py1", "py2"):
        if key in intent and not 0 <= intent[key] < EXPECTED_PREVIEW[1]:
            raise ValueError(f"{key} outside preview")
    return intent


@dataclass
class _Runtime:
    session: RecordSession
    lease: ScreenLease | None
    native: Any = None
    frame_seq: int = 0
    frame_data: bytes | None = None
    frame_at: float = 0.0
    frame_times: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=256))
    # Set when a cancel lands while the background ``starts_with`` reset is still
    # driving the device.  The initializer — not the cancel call — performs the
    # teardown, so the lease is never returned mid-action (§五 的同一条不变量：
    # 半空中还锁，那一下动作会打在抢到设备的下一个 run 身上)。
    cancel: threading.Event = field(default_factory=threading.Event)


SessionFactory = Callable[..., RecordSession]


class WebRecordManager:
    """Deep module owning the complete durable Web recording lifecycle."""

    def __init__(
        self,
        *,
        root: Path = ROOT,
        device: str | None = None,
        device_aliases: tuple[str, ...] = (),
        lock: ScreenLock | None = None,
        session_factory: SessionFactory = RecordSession,
        clock: Callable[[], float] = time.time,
        heartbeat_ttl_s: float = HEARTBEAT_TTL_S,
        frame_max_age_s: float = FRAME_MAX_AGE_S,
        background_start: bool = True,
    ) -> None:
        self.device = device
        self.devices = {device, *device_aliases}
        self.root = STATE_ROOT if root == APP_ASSET_ROOT else root
        self.lock = lock or ScreenLock(
            root=(
                default_screen_lock_root()
                if root == APP_ASSET_ROOT
                else self.root / "runs" / ".locks"
            ),
        )
        self.session_factory = session_factory
        self.clock = clock
        self.heartbeat_ttl_s = heartbeat_ttl_s
        self.frame_max_age_s = frame_max_age_s
        self.background_start = background_start
        self._runtime: dict[str, _Runtime] = {}
        self._initializers: dict[str, threading.Thread] = {}
        self._gates: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._scanner: threading.Thread | None = None
        self._manager_key = (str(self.lock.root.resolve()), device)
        with _ACTIVE_MANAGERS_GUARD:
            previous = _ACTIVE_MANAGERS.get(self._manager_key)
            _ACTIVE_MANAGERS[self._manager_key] = self
        if previous is not None and previous is not self:
            previous.shutdown()
        self.recover()

    def start_scanner(self) -> None:
        if self._scanner and self._scanner.is_alive():
            return
        self._stop.clear()
        self._scanner = threading.Thread(target=self._scan_loop, name="web-record-heartbeat", daemon=True)
        self._scanner.start()

    def shutdown(self, reason: str = "server_shutdown") -> None:
        self._stop.set()
        if self._scanner:
            self._scanner.join(timeout=1.0)
        # 初始化完成前不得归还 lease；停止标志使晚到的初始化结果进入 interrupted。
        for worker in list(self._initializers.values()):
            worker.join()
        for rid in list(self._runtime):
            with self._gate(rid):
                directory = self._directory(rid)
                meta = _read_json(directory / META_NAME) or {}
                state = _read_json(directory / SESSION_NAME) or {}
                if state.get("state") in {"starting", "live"}:
                    state["state"] = "interrupted"
                    state["last_error"] = reason
                    _atomic_json(directory / SESSION_NAME, state)
                self._release(rid, meta)
        with _ACTIVE_MANAGERS_GUARD:
            if _ACTIVE_MANAGERS.get(self._manager_key) is self:
                _ACTIVE_MANAGERS.pop(self._manager_key, None)

    def _initialize(
        self,
        rid: str,
        directory: Path,
        meta: dict[str, Any],
        state: dict[str, Any],
        script: dict[str, Any] | None,
    ) -> None:
        try:
            runtime = self._runtime.get(rid)
            if runtime is None:
                # Torn down before this thread got going.  Write nothing: every
                # persist here does mkdir(parents=True), so a late write would
                # resurrect a deleted recording directory as an orphan row.
                return
            if runtime.cancel.is_set():
                # Cancelled before the reset touched the device: nothing landed,
                # tear down without driving anything.
                self._finish_cancel(rid, directory, meta)
                return
            geometry = runtime.session.start()
            # The device work is done; the state transition that follows races the
            # cancel path (which persists `cancelling` from its own re-read of the
            # file).  Take the same gate cancel takes — blocking, since during
            # `starting` nobody holds it across device I/O — so exactly one of
            # "went live" / "was cancelled" wins, and the loser doesn't overwrite it.
            with self._gate(rid):
                if runtime.cancel.is_set():
                    self._finish_cancel(rid, directory, meta)
                    return
                if self._stop.is_set():
                    state["state"] = "interrupted"
                    state["last_error"] = "server_shutdown"
                    _atomic_json(directory / SESSION_NAME, state)
                    self._release(rid, meta)
                    return
                trace = runtime.session.trace()
                if script:
                    trace["script"] = script
                _atomic_json(directory / PARTIAL_NAME, trace)
                state["geometry"] = geometry
                if meta.get("surface") == "native":
                    self._start_native(rid, runtime)
                    state["native_window"] = runtime.native.snapshot
                state["state"] = "live"
                _atomic_json(directory / SESSION_NAME, state)
        except Exception as exc:
            with self._gate(rid):
                current = self._runtime.get(rid)
                if current is None:
                    return  # torn down mid-start; same no-resurrect rule as above
                if current.cancel.is_set():
                    # The reset blew up on a session the user already cancelled: the
                    # directory goes away either way, and the failure has no reader.
                    self._finish_cancel(rid, directory, meta)
                    return
                state["state"] = "failed"
                state["last_error"] = f"{type(exc).__name__}: {exc}"
                try:
                    _atomic_json(directory / SESSION_NAME, state)
                except OSError:
                    pass
                self._release(rid, meta)
            if not self.background_start:
                raise WebRecordError(str(exc), status=503, code="device_error") from exc
        finally:
            self._initializers.pop(rid, None)

    def _start_native(self, rid: str, runtime: _Runtime) -> None:
        from autophone.record.native import NativePointerRecorder
        from autophone.device.target import resolve_target

        target = resolve_target()
        if target.device not in self.devices:
            raise WebRecordError("native recording target mismatch", code="device_mismatch")
        runtime.native = NativePointerRecorder(runtime.session, target.vm_dir / "config.plist",
            lambda row, before: self._native_commit(rid, runtime, row, before),
            lambda error: self._native_fault(rid, runtime, error))
        runtime.native.start()

    @contextmanager
    def _native_gate(self, rid: str, runtime: _Runtime) -> Iterator[bool]:
        gate = self._gate(rid)
        while not runtime.native.stopping:
            if gate.acquire(timeout=.05):
                try:
                    yield self._runtime.get(rid) is runtime and not runtime.native.stopping
                finally:
                    gate.release()
                return
        yield False

    def _native_commit(self, rid: str, runtime: _Runtime, row: dict[str, Any], before: Any) -> None:
        with self._native_gate(rid, runtime) as active:
            if not active:
                return
            directory, meta, state = self._load(rid)
            if state.get("state") != "live":
                raise CaptureError("native input arrived outside live session")
            index = len(runtime.session.trace()["events"])
            event_id = f"native-{index}"
            state["inflight"] = {"event_id": event_id, "base_event_count": index}
            _atomic_json(directory / SESSION_NAME, state)
            event = runtime.session.record_observed_pointer(row, before)
            trace = runtime.session.trace()
            if isinstance(meta.get("script"), dict):
                trace["script"] = meta["script"]
            _atomic_json(directory / PARTIAL_NAME, trace)
            state["committed"][event_id] = {"event_id": event_id, "index": index,
                "kind": event.kind, "t": event.t, "source": "cgeventtap_listen_only", "delivery": "unverified"}
            state["inflight"] = None
            _atomic_json(directory / SESSION_NAME, state)

    def _native_fault(self, rid: str, runtime: _Runtime, error: str) -> None:
        with self._native_gate(rid, runtime) as active:
            if active:
                directory, _, state = self._load(rid)
                state["state"] = "faulted"
                state["last_error"] = error
                _atomic_json(directory / SESSION_NAME, state)

    def _finish_cancel(self, rid: str, directory: Path, meta: dict[str, Any]) -> None:
        """Complete a cancel that had to wait for the in-flight reset to land.

        跑在后台线程里、没有调用方可以报错，所以删除失败只记日志：设备已经还回去了，
        剩下的目录顶多是一条 `cancelling` 残留，下次启动 `recover()` 会补删。
        """
        self._release(rid, meta)
        self._runtime.pop(rid, None)
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            logger.warning("取消录制后删除目录失败（留给下次启动补删）：%s: %s", directory, exc)

    def _scan_loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_SCAN_S):
            self.expire_heartbeats()

    def _directory(self, recording_id: str) -> Path:
        rid = str(recording_id or "").strip()
        path = (self.root / rid).resolve()
        base = (self.root / "runs" / "rec").resolve()
        try:
            path.relative_to(base)
        except ValueError as exc:
            raise WebRecordError("invalid recording_id", status=404, code="not_found") from exc
        return path

    def _gate(self, recording_id: str) -> threading.Lock:
        with self._guard:
            return self._gates.setdefault(recording_id, threading.Lock())

    @contextmanager
    def _execution(self, recording_id: str) -> Iterator[None]:
        gate = self._gate(recording_id)
        if not gate.acquire(blocking=False):
            raise WebRecordError("session event is in progress", code="busy")
        try:
            yield
        finally:
            gate.release()

    def _load(self, recording_id: str, *, app: str | None = None) -> tuple[Path, dict[str, Any], dict[str, Any]]:
        directory = self._directory(recording_id)
        meta = _read_json(directory / META_NAME)
        state = _read_json(directory / SESSION_NAME)
        if not directory.exists() or meta is None or state is None or meta.get("surface") not in {"web", "native"}:
            raise WebRecordError("web recording session not found", status=404, code="not_found")
        if self.device is not None and meta.get("device") not in self.devices:
            raise WebRecordError("recording device mismatch", code="device_mismatch")
        if app is not None and meta.get("app") != app:
            raise WebRecordError("web recording session not found", status=404, code="not_found")
        return directory, meta, state

    @staticmethod
    def _check_token(state: dict[str, Any], token: str | None) -> None:
        if not token or not secrets.compare_digest(str(state.get("controller_token_hash") or ""), _token_hash(token)):
            raise WebRecordError("controller token mismatch", code="token_mismatch")

    def _release(self, recording_id: str, meta: dict[str, Any]) -> None:
        """Terminal teardown: hand the device back and drop the in-memory session.

        Every caller is already past its last use of the ``RecordSession`` (finish
        promotes the trace *before* releasing), so the runtime is dropped here
        rather than kept for the life of the process — otherwise a long-lived
        dashboard accumulates one session object plus its last JPEG per recording.
        """
        runtime = self._runtime.pop(recording_id, None)
        if runtime:
            if runtime.native is not None:
                runtime.native.stop()
            runtime.session.close_semantics()
            if runtime.lease is not None:
                runtime.lease.release()
                runtime.lease = None
        device = str(meta.get("device") or "")
        if device:
            self.lock.clear_recording(device=device, recording_id=recording_id)

    def start(
        self,
        app: str,
        *,
        build: str,
        starts_with: str | None,
        device: str,
        script: dict[str, Any] | None = None,
        surface: str = "web",
    ) -> dict[str, Any]:
        if surface not in {"web", "native"}:
            raise WebRecordError("unsupported recording surface", status=422, code="validation")
        if self.device is not None and device != self.device:
            raise WebRecordError("recording device mismatch", code="device_mismatch")
        token = secrets.token_urlsafe(32)
        stamp = now_iso().replace(":", "").replace("-", "").replace("+", "-").split(".")[0]
        rid = f"runs/rec/{app}/{stamp}-{secrets.token_hex(3)}-{_slug(build)}"
        directory = self._directory(rid)
        lease = self.lock.try_lease(device=device, owner_kind="web_recording", owner_id=rid)
        if lease is None:
            owner = self.lock.status(device=device)
            raise WebRecordError(
                f"device is busy: {owner.get('owner_kind') or 'workflow'} {owner.get('owner_id') or owner.get('run_id')}",
                code="device_busy",
            )
        try:
            directory.mkdir(parents=True, exist_ok=False)
        except Exception:
            lease.release()
            raise
        meta: dict[str, Any] = {
            "app": app, "build": build, "starts_with": starts_with,
            "started_at": now_iso(), "pid": os.getpid(), "device": device,
            "surface": surface,
        }
        if script:
            meta["script"] = script
        state: dict[str, Any] = {
            "state": "starting", "controller_token_hash": _token_hash(token),
            "heartbeat_at": self.clock(), "last_error": None, "inflight": None,
            "committed": {}, "geometry": None,
        }
        try:
            _atomic_json(directory / META_NAME, meta)
            _atomic_json(directory / SESSION_NAME, state)
            self.lock.mark_recording(
                device=device, recording_id=rid, pid=os.getpid(), surface=surface,
            )
            options = {"starts_with": starts_with}
            if surface == "native":
                # Native input is already delivered when observed; no before_event
                # hook can claim a pre-input Frida snapshot.
                options["semantic_factory"] = lambda *_: None
                from autophone.device.target import resolve_target
                options["sock_path"] = str(resolve_target().sock)
            sess = self.session_factory(directory, app, build, **options)
            self._runtime[rid] = _Runtime(session=sess, lease=lease)
            if self.background_start:
                worker = threading.Thread(
                    target=self._initialize,
                    args=(rid, directory, meta, state, script),
                    name=f"web-record-start-{directory.name}",
                    daemon=True,
                )
                self._initializers[rid] = worker
                worker.start()
                result_state = "starting"
            else:
                self._initialize(rid, directory, meta, state, script)
                result_state = "live"
        except Exception as exc:
            state["state"] = "failed"
            state["last_error"] = f"{type(exc).__name__}: {exc}"
            try:
                _atomic_json(directory / SESSION_NAME, state)
            except OSError:
                pass
            if rid in self._runtime:
                self._release(rid, meta)
            else:
                lease.release()
                self.lock.clear_recording(device=device, recording_id=rid)
            if isinstance(exc, WebRecordError):
                raise
            raise WebRecordError(str(exc), status=503, code="device_error") from exc
        return {"recording_id": rid, "dir": str(directory), "pid": os.getpid(), "controller_token": token, "state": result_state}

    def view(self, app: str, recording_id: str) -> dict[str, Any]:
        _, meta, state = self._load(recording_id, app=app)
        committed = state.get("committed") if isinstance(state.get("committed"), dict) else {}
        runtime = self._runtime.get(recording_id)
        committed_events = [
            {k: v for k, v in ack.items() if k != "intent_hash"}
            for ack in committed.values()
            if isinstance(ack, dict)
        ]
        committed_events.sort(key=lambda item: int(item.get("index") or 0))
        return {
            "recording_id": recording_id,
            "state": state.get("state"),
            "started_at": meta.get("started_at"),
            "seq": runtime.frame_seq if runtime else 0,
            "committed": list(committed),
            "committed_events": committed_events,
            "committed_count": len(committed),
            "inflight": {"event_id": state["inflight"].get("event_id")} if isinstance(state.get("inflight"), dict) else None,
            "last_error": state.get("last_error"),
            "geometry": state.get("geometry"),
        }

    def frame(self, app: str, recording_id: str) -> tuple[bytes, int, float]:
        _, _, state = self._load(recording_id, app=app)
        if state.get("state") not in {"live", "faulted"}:
            raise WebRecordError("session has no live frame", status=409, code="state_conflict")
        runtime = self._runtime.get(recording_id)
        if runtime is None:
            raise WebRecordError("recording process was interrupted", status=503, code="device_error")
        now = self.clock()
        if runtime.frame_data is None or now - runtime.frame_at >= FRAME_MIN_INTERVAL_S:
            try:
                preview = runtime.session.capture_preview()
                data = base64.b64decode(preview.image_b64)
            except Exception as exc:
                raise WebRecordError(str(exc), status=503, code="device_error") from exc
            runtime.frame_seq += 1
            runtime.frame_data = data
            runtime.frame_at = now
            runtime.frame_times.append((runtime.frame_seq, now))
        return runtime.frame_data, runtime.frame_seq, runtime.frame_at

    _normalize_intent = staticmethod(normalize_preview_intent)

    def event(self, app: str, recording_id: str, payload: dict[str, Any], token: str | None) -> dict[str, Any]:
        event_id = str(payload.get("event_id") or "").strip()
        if not event_id or len(event_id) > 128:
            raise WebRecordError("event_id is required", status=422, code="validation")
        try:
            intent = self._normalize_intent(payload)
            frame_seq = int(payload["frame_seq"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WebRecordError(str(exc), status=422, code="validation") from exc
        digest = _intent_hash(intent)
        with self._execution(recording_id):
            directory, meta, state = self._load(recording_id, app=app)
            self._check_token(state, token)
            if meta.get("surface") == "native":
                raise WebRecordError("native recording accepts observed pointer input only", code="passive_input_only")
            committed = state.get("committed") if isinstance(state.get("committed"), dict) else {}
            previous = committed.get(event_id)
            if isinstance(previous, dict):
                if previous.get("intent_hash") != digest:
                    raise WebRecordError("event_id reused with different intent", code="idempotency_conflict")
                return {k: v for k, v in previous.items() if k != "intent_hash"}
            inflight = state.get("inflight")
            if isinstance(inflight, dict) and inflight.get("event_id") == event_id:
                if inflight.get("intent_hash") != digest:
                    raise WebRecordError("event_id reused with different intent", code="idempotency_conflict")
                raise WebRecordError("event is still in progress", code="in_progress")
            if state.get("state") != "live":
                raise WebRecordError(f"session is {state.get('state')}", code="state_conflict")
            runtime = self._runtime.get(recording_id)
            if runtime is None:
                raise WebRecordError("recording process was interrupted", status=503, code="device_error")
            frame_at = next((at for seq, at in runtime.frame_times if seq == frame_seq), None)
            if frame_at is None or self.clock() - frame_at > self.frame_max_age_s:
                raise WebRecordError("frame is stale", code="stale_frame")
            base_count = len(runtime.session.trace().get("events") or [])
            state["inflight"] = {"event_id": event_id, "intent_hash": digest, "base_event_count": base_count}
            _atomic_json(directory / SESSION_NAME, state)
            try:
                action_at = now_iso()
                if intent["kind"] == "tap":
                    event = runtime.session.record_tap(intent["px"], intent["py"])
                elif intent["kind"] == "type":
                    event = runtime.session.record_type(intent["text"])
                elif intent["kind"] == "key":
                    event = runtime.session.record_key(intent["name"])
                else:
                    event = runtime.session.record_swipe(
                        intent["px1"], intent["py1"], intent["px2"], intent["py2"], ms=intent["ms"],
                    )
                trace = runtime.session.trace()
                if isinstance(meta.get("script"), dict):
                    trace["script"] = meta["script"]
                _atomic_json(directory / PARTIAL_NAME, trace)
                committed_at = now_iso()
                ack = {
                    "event_id": event_id,
                    "index": base_count,
                    "kind": intent["kind"],
                    "ok": True,
                    "t": event.t,
                    "action_at": action_at,
                    "committed_at": committed_at,
                }
                committed[event_id] = {**ack, "intent_hash": digest}
                state["committed"] = committed
                state["inflight"] = None
                _atomic_json(directory / SESSION_NAME, state)
                return ack
            except Exception as exc:
                state["state"] = "faulted"
                state["last_error"] = f"{type(exc).__name__}: {exc}"
                _atomic_json(directory / SESSION_NAME, state)
                self._release(recording_id, meta)
                raise WebRecordError(str(exc), status=503, code="device_error") from exc

    def heartbeat(self, app: str, recording_id: str, token: str | None) -> dict[str, Any]:
        with self._execution(recording_id):
            directory, _, state = self._load(recording_id, app=app)
            self._check_token(state, token)
            if state.get("state") != "live":
                raise WebRecordError(f"session is {state.get('state')}", code="state_conflict")
            state["heartbeat_at"] = self.clock()
            _atomic_json(directory / SESSION_NAME, state)
            return {"ok": True, "heartbeat_at": state["heartbeat_at"]}

    def finish(self, app: str, recording_id: str, token: str | None) -> dict[str, Any]:
        # Drain outside the gate: the producer needs it to commit the final up.
        # Token validation precedes any change to the observation producer.
        _, meta, state = self._load(recording_id, app=app)
        self._check_token(state, token)
        runtime = self._runtime.get(recording_id)
        if meta.get("surface") == "native" and runtime is not None and runtime.native is not None:
            runtime.native.freeze()
        with self._execution(recording_id):
            directory, meta, state = self._load(recording_id, app=app)
            self._check_token(state, token)
            if state.get("state") == "captured":
                return {"recording_id": recording_id, "state": "captured", "event_count": len(state.get("committed") or {})}
            if state.get("state") not in {"live", "faulted", "interrupted", "finishing"}:
                raise WebRecordError(f"session is {state.get('state')}", code="state_conflict")
            count = len(state.get("committed") or {})
            if count == 0:
                if meta.get("surface") == "native":
                    state["state"] = "interrupted"
                    state["last_error"] = "empty_recording"
                    _atomic_json(directory / SESSION_NAME, state)
                    self._release(recording_id, meta)
                raise WebRecordError("cannot finish an empty recording", code="empty_recording")
            if state.get("inflight"):
                # A faulted session may explicitly keep the already committed
                # prefix.  The uncertain action is deliberately excluded: only
                # a partial whose event count still equals base is safe to use.
                inflight = state["inflight"]
                partial_doc = _read_json(directory / PARTIAL_NAME)
                events = partial_doc.get("events") if isinstance(partial_doc, dict) else None
                base = inflight.get("base_event_count") if isinstance(inflight, dict) else None
                if (
                    state.get("state") != "faulted"
                    or not isinstance(events, list)
                    or len(events) != base
                    or count != base
                ):
                    raise WebRecordError("event outcome is uncertain", code="in_progress")
                state["inflight"] = None
            state["state"] = "finishing"
            _atomic_json(directory / SESSION_NAME, state)
            partial = directory / PARTIAL_NAME
            trace = directory / TRACE_NAME
            if not trace.exists():
                runtime = self._runtime.get(recording_id)
                if runtime is not None:
                    runtime.session.finish_semantics()
                    trace_doc = runtime.session.trace()
                    if runtime.native is not None:
                        trace_doc["native_capture"] = {"source": "cgeventtap_listen_only",
                            "delivery": "unverified", "stopped": True, "error": runtime.native.error}
                    if isinstance(meta.get("script"), dict):
                        trace_doc["script"] = meta["script"]
                    _atomic_json(partial, trace_doc)
                if not partial.exists():
                    raise WebRecordError("partial trace is missing", status=503, code="storage_error")
                partial.replace(trace)
            state["state"] = "captured"
            state["last_error"] = None
            _atomic_json(directory / SESSION_NAME, state)
            partial.unlink(missing_ok=True)
            self._release(recording_id, meta)
            return {"recording_id": recording_id, "state": "captured", "event_count": count}

    def cancel(self, app: str, recording_id: str, token: str | None) -> dict[str, Any]:
        with self._execution(recording_id):
            directory, meta, state = self._load(recording_id, app=app)
            self._check_token(state, token)
            if state.get("state") == "captured":
                raise WebRecordError("captured recording must be deleted", code="state_conflict")
            runtime = self._runtime.get(recording_id)
            initializer = self._initializers.get(recording_id)
            if (
                state.get("state") in {"starting", "cancelling"}
                and runtime is not None
                and initializer is not None
                and initializer.is_alive()
            ):
                # The `starts_with` reset is still driving the device.  Releasing
                # the lease (or deleting the directory) now would leave that reset
                # tapping a device the next run already owns — so record the intent
                # and let the initializer tear down once its action has landed.
                runtime.cancel.set()
                state["state"] = "cancelling"
                _atomic_json(directory / SESSION_NAME, state)
                return {
                    "cancelled": recording_id, "device": meta.get("device"), "pending": True,
                }
            self._release(recording_id, meta)
            self._runtime.pop(recording_id, None)
            # 同步路径不吞删除失败：目录还在却回「已取消」，列表里那条会继续像活的。
            shutil.rmtree(directory)
            return {"cancelled": recording_id, "device": meta.get("device"), "pending": False}

    def label(self, app: str, recording_id: str, payload: dict[str, Any], token: str | None) -> dict[str, Any]:
        """给一个**已提交**的事件命名（人手里的语义，别让它在入口就丢了）。

        与 event 的关键区别：命名不碰设备。所以它没有 event_id 幂等那套——重发
        同一个名字得到同一个结果，本来就是幂等的；掉包了也只是名字没写上，绝不
        产生第二次设备动作。仍然过同一把 execution gate：partial 的写入不能与
        事件/finish 晋升交错。
        """
        try:
            index = int(payload.get("index"))
        except (TypeError, ValueError) as exc:
            raise WebRecordError("index is required", status=422, code="validation") from exc
        label = payload.get("label")
        if not isinstance(label, str):
            raise WebRecordError("label must be a string", status=422, code="validation")
        with self._execution(recording_id):
            directory, meta, state = self._load(recording_id, app=app)
            self._check_token(state, token)
            if state.get("state") != "live":
                raise WebRecordError(f"session is {state.get('state')}", code="state_conflict")
            if state.get("inflight"):
                raise WebRecordError("event is still in progress", code="in_progress")
            runtime = self._runtime.get(recording_id)
            if runtime is None:
                raise WebRecordError("recording process was interrupted", status=503, code="device_error")
            try:
                event = runtime.session.label_event(index, label)
            except ValueError as exc:
                raise WebRecordError(str(exc), status=422, code="validation") from exc
            trace = runtime.session.trace()
            if isinstance(meta.get("script"), dict):
                trace["script"] = meta["script"]
            _atomic_json(directory / PARTIAL_NAME, trace)
            # ACK 台账同步带上名字，刷新页面后 UI 还能显示它——名字是事件状态的
            # 一部分，不是只活在某个标签页内存里的东西。
            resolved = index if index >= 0 else len(trace.get("events") or []) + index
            committed = state.get("committed") if isinstance(state.get("committed"), dict) else {}
            for ack in committed.values():
                if isinstance(ack, dict) and ack.get("index") == resolved:
                    ack["label"] = event.label
            state["committed"] = committed
            _atomic_json(directory / SESSION_NAME, state)
            return {"index": resolved, "kind": event.kind, "label": event.label}

    def claim(self, app: str, recording_id: str, token: str | None) -> dict[str, Any]:
        with self._execution(recording_id):
            directory, _, state = self._load(recording_id, app=app)
            current = state.get("state")
            if current == "live":
                self._check_token(state, token)
            elif current not in {"faulted", "interrupted"}:
                raise WebRecordError(f"session is {current}", code="state_conflict")
            new_token = secrets.token_urlsafe(32)
            state["controller_token_hash"] = _token_hash(new_token)
            _atomic_json(directory / SESSION_NAME, state)
            return {"recording_id": recording_id, "controller_token": new_token, "state": current}

    def expire_heartbeats(self) -> None:
        now = self.clock()
        for path in list((self.root / "runs" / "rec").glob(f"**/{SESSION_NAME}")):
            directory = path.parent
            try:
                rid = directory.resolve().relative_to(self.root.resolve()).as_posix()
            except ValueError:
                continue
            # Screen candidates *before* taking a gate: every historical web
            # recording keeps a session.json forever, and minting a lock per row
            # on every 15s scan would grow the gate table with the archive.
            if not self._heartbeat_expired(directory, path, now):
                continue
            gate = self._gate(rid)
            if not gate.acquire(blocking=False):
                continue
            try:
                # Re-read under the gate: an event may have landed since the
                # unlocked pre-screen above.
                if not self._heartbeat_expired(directory, path, self.clock()):
                    continue
                meta = _read_json(directory / META_NAME) or {}
                state = _read_json(path) or {}
                state["state"] = "interrupted"
                state["last_error"] = "heartbeat_timeout"
                _atomic_json(path, state)
                self._release(rid, meta)
            finally:
                gate.release()

    def _heartbeat_expired(self, directory: Path, path: Path, now: float) -> bool:
        meta = _read_json(directory / META_NAME) or {}
        state = _read_json(path) or {}
        if self.device is not None and meta.get("device") not in self.devices:
            return False
        if meta.get("surface") not in {"web", "native"} or state.get("state") not in ({"live", "faulted"} if meta.get("surface") == "native" else {"live"}):
            return False
        return now - float(state.get("heartbeat_at") or 0) > self.heartbeat_ttl_s

    def recover(self) -> None:
        """Recover persisted sessions after a single-worker dashboard restart."""
        rec_root = self.root / "runs" / "rec"
        if not rec_root.exists():
            return
        for path in list(rec_root.glob(f"**/{SESSION_NAME}")):
            directory = path.parent
            meta = _read_json(directory / META_NAME) or {}
            state = _read_json(path) or {}
            if meta.get("surface") not in {"web", "native"} or (self.device is not None and meta.get("device") not in self.devices):
                continue
            rid = directory.resolve().relative_to(self.root.resolve()).as_posix()
            current = state.get("state")
            inflight = state.get("inflight")
            try:
                owner_pid = int(meta.get("pid") or 0)
            except (TypeError, ValueError):
                owner_pid = 0
            if (
                current in {"starting", "live", "finishing", "cancelling"}
                and owner_pid != os.getpid()
                and _pid_alive(owner_pid)
            ):
                continue
            if current == "cancelling":
                # The process that owed this cancel is gone, so its reset is over
                # too: finish the deletion the initializer never got to run.
                device = str(meta.get("device") or "")
                shutil.rmtree(directory, ignore_errors=True)
                if device:
                    self.lock.clear_recording(device=device, recording_id=rid)
                continue
            if isinstance(inflight, dict):
                partial = _read_json(directory / PARTIAL_NAME)
                events = partial.get("events") if isinstance(partial, dict) else None
                base = inflight.get("base_event_count")
                recovered = False
                if isinstance(events, list) and isinstance(base, int) and len(events) == base + 1:
                    intent = _event_intent(events[-1]) if isinstance(events[-1], dict) else None
                    if intent is not None and _intent_hash(intent) == inflight.get("intent_hash"):
                        committed = state.get("committed") if isinstance(state.get("committed"), dict) else {}
                        event_id = str(inflight.get("event_id") or "")
                        committed[event_id] = {
                            "event_id": event_id, "index": base, "kind": intent["kind"], "ok": True,
                            "t": events[-1].get("t"), "intent_hash": inflight.get("intent_hash"),
                        }
                        state["committed"] = committed
                        state["inflight"] = None
                        recovered = True
                if not recovered:
                    state["state"] = "faulted"
                    state["last_error"] = "uncertain_event"
            if current == "finishing":
                if (directory / TRACE_NAME).exists():
                    state["state"] = "captured"
                elif state.get("inflight") is None and (directory / PARTIAL_NAME).exists():
                    (directory / PARTIAL_NAME).replace(directory / TRACE_NAME)
                    state["state"] = "captured"
                else:
                    state["state"] = "faulted"
            elif current in {"starting", "live"} and state.get("state") != "faulted":
                state["state"] = "interrupted"
                state["last_error"] = "server_restarted"
            _atomic_json(path, state)
            device = str(meta.get("device") or "")
            if device:
                self.lock.clear_recording(device=device, recording_id=rid)
