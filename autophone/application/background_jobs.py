"""Persistent, bounded and cancellable jobs for dashboard background workflows.

The service deliberately knows nothing about individual handlers.  Callers bind
their scene handler in ``submit``; the service owns lifecycle, persistence,
bounded execution, cancellation and event retention.  Snapshots are written
atomically to ``runs/.background/jobs/<job_id>.json`` so a restart can preserve audit and
mark unfinished work as interrupted instead of replaying side effects.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import tempfile
import threading
import time
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from secrets import token_hex
from typing import Any, Callable

from autophone.paths import STATE_ROOT
from autophone.util import now_iso as _now, write_json_atomic

EmitFn = Callable[[dict[str, Any]], None]
JobThunk = Callable[..., Any]
ACTIVE_STATUSES = frozenset({"queued", "running"})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled", "interrupted"})


class JobQueueFull(RuntimeError):
    pass


class JobConflict(RuntimeError):
    pass


class JobNotRegistered(RuntimeError):
    pass


class CancelToken:
    """Cooperative cancellation token passed to handlers that accept it."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def canceled(self) -> bool:
        return self._event.is_set()

    def raise_if_canceled(self) -> None:
        if self.canceled:
            raise JobCanceled("Background job canceled")

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)


class JobCanceled(RuntimeError):
    pass


_CURRENT_CANCEL_TOKEN: ContextVar[CancelToken | None] = ContextVar(
    "autophone_job_cancel_token", default=None
)


def current_cancel_token() -> CancelToken | None:
    """Cancellation token for the handler running in the current worker context."""
    return _CURRENT_CANCEL_TOKEN.get()


@dataclass(frozen=True)
class SceneContext:
    job_id: str
    scene: str
    request: dict[str, Any]
    actor: str
    emit: EmitFn
    cancel_token: CancelToken


SceneHandler = Callable[[SceneContext], Any]


@dataclass
class Job:
    job_id: str
    scene: str
    actor: str = "web"
    lane: str = "inference"
    status: str = "queued"
    phase: str = "queued"
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    ended_at: str | None = None
    parent_job_id: str | None = None
    plan_digest: str | None = None
    approval_expires_at: str | None = None
    consent_receipt: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    provider: str | None = None
    model: str | None = None
    cost_usd: float | None = None
    duration_s: float | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    events_truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    request_digest: str | None = None
    idempotency_key: str | None = None

    def to_dict(self, *, include_events: bool = True) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "scene": self.scene,
            "actor": self.actor,
            "lane": self.lane,
            "status": self.status,
            "phase": self.phase,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "parent_job_id": self.parent_job_id,
            "plan_digest": self.plan_digest,
            "approval_expires_at": self.approval_expires_at,
            "consent_receipt": self.consent_receipt,
            "result": self.result,
            "error": self.error,
            "provider": self.provider,
            "model": self.model,
            "cost_usd": self.cost_usd,
            "duration_s": self.duration_s,
            "events": list(self.events) if include_events else [],
            "events_truncated": self.events_truncated,
            "metadata": dict(self.metadata),
            "request_digest": self.request_digest,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Job":
        fields = cls.__dataclass_fields__
        return cls(**{key: value[key] for key in fields if key in value})


def _positional_arity(fn: Callable[..., Any]) -> int:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return 0
    if any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params):
        return 2
    return sum(
        p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        for p in params
    )


def _normalize(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_contract"):
        contract = value.to_contract()
    elif isinstance(value, dict):
        contract = value
    else:
        raise TypeError(f"job handler returned unsupported type: {type(value)!r}")
    return {
        "ok": bool(contract.get("ok")),
        "contract": contract,
        "provider": contract.get("provider"),
        "model": contract.get("model"),
        "cost_usd": contract.get("cost_usd"),
        "duration_s": contract.get("duration_s"),
        "error": contract.get("error"),
    }


def request_digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class BackgroundJobService:
    """Persistent registry backed by bounded inference/device worker pools."""

    result_failure_code = "job_result_failed"

    def __init__(
        self,
        *,
        root: Path | None = None,
        inference_workers: int = 2,
        ask_workers: int = 1,
        inference_capacity: int = 32,
        device_capacity: int = 8,
        max_events: int = 200,
        max_event_bytes: int = 256 * 1024,
        retention_days: int | None = None,
        max_terminal_jobs: int | None = None,
    ) -> None:
        self.root = root or (STATE_ROOT / "runs" / ".background" / "jobs")
        self.root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._tokens: dict[str, CancelToken] = {}
        self._futures: dict[str, Any] = {}
        self._slots: dict[str, threading.Semaphore] = {}
        self._event_bytes: dict[str, int] = {}
        self._idempotency: dict[tuple[str, str, str], str] = {}
        self._handlers: dict[str, SceneHandler] = {}
        self._lock = threading.RLock()
        self._max_events = max_events
        self._max_event_bytes = max_event_bytes
        self._retention_days = max(
            1,
            int(retention_days if retention_days is not None else os.environ.get("AUTOPHONE_BACKGROUND_JOB_RETENTION_DAYS", "30")),
        )
        self._max_terminal_jobs = max(
            1,
            int(max_terminal_jobs if max_terminal_jobs is not None else os.environ.get("AUTOPHONE_BACKGROUND_JOB_MAX_TERMINAL", "1000")),
        )
        self._inference_slots = threading.BoundedSemaphore(inference_capacity)
        self._device_slots = threading.BoundedSemaphore(device_capacity)
        self._inference_pool = ThreadPoolExecutor(max_workers=inference_workers, thread_name_prefix="background-worker")
        # Short ask jobs must not queue behind minute-long gate/lift/land workflows.
        # They retain the public inference lane contract but have a reserved worker.
        self._ask_pool = ThreadPoolExecutor(max_workers=ask_workers, thread_name_prefix="background-short")
        self._device_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="background-device")
        self._closed = False
        self._load_and_interrupt()
        self.cleanup()

    @staticmethod
    def make_job_id(scene: str) -> str:
        return f"{scene}-{int(time.time())}-{token_hex(3)}"

    def _path(self, job_id: str) -> Path:
        return self.root / f"{job_id}.json"

    def _events_path(self, job_id: str) -> Path:
        return self.root / f"{job_id}.events.jsonl"

    def _persist_locked(self, job: Job) -> None:
        write_json_atomic(self._path(job.job_id), job.to_dict())

    def _persist_event_state_locked(self, job: Job) -> None:
        """Persist event metadata without rewriting the growing event history."""
        write_json_atomic(self._path(job.job_id), job.to_dict(include_events=False))

    def _append_event_locked(self, job_id: str, encoded: bytes) -> None:
        """Append one event with one O_APPEND write while the service lock is held."""
        path = self._events_path(job_id)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, encoded + b"\n")
        finally:
            os.close(fd)

    def _load_event_sidecar(self, job_id: str) -> list[dict[str, Any]]:
        path = self._events_path(job_id)
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    def _load_and_interrupt(self) -> None:
        for path in sorted(self.root.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                job = Job.from_dict(raw)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
            sidecar_events = self._load_event_sidecar(job.job_id)
            if sidecar_events:
                by_seq = {
                    event.get("seq"): event
                    for event in [*job.events, *sidecar_events]
                    if isinstance(event, dict)
                }
                job.events = list(by_seq.values())
            if job.status in ACTIVE_STATUSES or job.status in {"running", "done"}:
                # ``done`` was terminal in the legacy service.  Preserve it as
                # succeeded; only genuinely unfinished snapshots are interrupted.
                if job.status == "done":
                    job.status = "succeeded"
                    job.phase = "completed"
                else:
                    job.status = "interrupted"
                    job.phase = "interrupted"
                    job.ended_at = _now()
                    job.error = {
                        "code": "service_restarted",
                        "message": "service restarted before this job reached a terminal state",
                        "retryable": True,
                    }
                self._persist_locked(job)
                self._events_path(job.job_id).unlink(missing_ok=True)
            self._jobs[job.job_id] = job
            self._event_bytes[job.job_id] = sum(
                len(json.dumps(event, ensure_ascii=False, default=str).encode("utf-8"))
                for event in job.events
            )
            if job.idempotency_key:
                self._idempotency[(job.scene, job.actor, job.idempotency_key)] = job.job_id

    def register(self, scene: str, handler: SceneHandler) -> None:
        """Register one fixed scene handler. Duplicate registration fails closed."""
        scene = str(scene or "").strip()
        if not scene:
            raise ValueError("scene is required")
        with self._lock:
            if scene in self._handlers:
                raise ValueError(f"Background scene already registered: {scene}")
            self._handlers[scene] = handler

    @property
    def registered_scenes(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._handlers))

    def submit_registered(
        self,
        scene: str,
        request: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Submit serializable scene input; HTTP callers never provide executable thunks."""
        with self._lock:
            handler = self._handlers.get(scene)
        if handler is None:
            raise JobNotRegistered(f"Background scene is not registered: {scene}")
        accepted = json.loads(json.dumps(request, ensure_ascii=False, default=str))
        digest_request = kwargs.pop("idempotency_request", accepted)
        digest_request = json.loads(json.dumps(digest_request, ensure_ascii=False, default=str))

        def _dispatch(emit: EmitFn, cancel_token: CancelToken) -> Any:
            return handler(SceneContext(
                job_id=str(kwargs.get("job_id") or ""),
                scene=scene,
                request=accepted,
                actor=str(kwargs.get("actor") or "web"),
                emit=emit,
                cancel_token=cancel_token,
            ))

        # Reserve the id before binding it into SceneContext.
        if not kwargs.get("job_id"):
            kwargs["job_id"] = self.make_job_id(scene)
        # Handler input may contain server-generated paths/job ids. Idempotency
        # fingerprints the stable HTTP request instead of those derived fields.
        kwargs["request"] = digest_request
        return self.submit(scene, _dispatch, **kwargs)

    def submit(
        self,
        scene: str,
        thunk: JobThunk,
        *,
        job_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        actor: str = "web",
        lane: str = "inference",
        provider: str | None = None,
        parent_job_id: str | None = None,
        plan_digest: str | None = None,
        approval_expires_at: str | None = None,
        consent_receipt: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        request: Any = None,
    ) -> dict[str, Any]:
        if lane not in {"inference", "device"}:
            raise ValueError("lane must be inference or device")
        if self._closed:
            raise RuntimeError("Background job service is shut down")
        digest = request_digest(request) if request is not None else None
        scope = (scene, actor, idempotency_key) if idempotency_key else None
        slots = self._device_slots if lane == "device" else self._inference_slots
        with self._lock:
            if scope and scope in self._idempotency:
                existing = self._jobs[self._idempotency[scope]]
                if existing.request_digest != digest:
                    raise JobConflict("idempotency key was already used with a different request")
                return existing.to_dict()
            # Admission, capacity reservation and idempotency publication are
            # one critical section so simultaneous HTTP retries cannot create
            # two jobs for the same scoped key.
            if not slots.acquire(blocking=False):
                raise JobQueueFull(f"Background {lane} queue is full")
            job_id = job_id or self.make_job_id(scene)
            token = CancelToken()
            job = Job(
                job_id=job_id,
                scene=scene,
                actor=actor,
                lane=lane,
                provider=provider,
                parent_job_id=parent_job_id,
                plan_digest=plan_digest,
                approval_expires_at=approval_expires_at,
                consent_receipt=consent_receipt,
                metadata=dict(metadata or {}),
                request_digest=digest,
                idempotency_key=idempotency_key,
            )
            if job_id in self._jobs:
                slots.release()
                raise ValueError(f"duplicate job id: {job_id}")
            self._jobs[job_id] = job
            self._event_bytes[job_id] = 0
            self._tokens[job_id] = token
            if scope:
                self._idempotency[scope] = job_id
            self._persist_locked(job)
            handle = job.to_dict()

        pool = (
            self._device_pool if lane == "device"
            else self._ask_pool if scene == "ask"
            else self._inference_pool
        )
        try:
            future = pool.submit(self._run, job_id, thunk, token, slots)
            with self._lock:
                if job_id in self._tokens:
                    self._futures[job_id] = future
                    self._slots[job_id] = slots
        except Exception:
            slots.release()
            with self._lock:
                self._jobs.pop(job_id, None)
                self._event_bytes.pop(job_id, None)
                self._tokens.pop(job_id, None)
                if scope:
                    self._idempotency.pop(scope, None)
                self._path(job_id).unlink(missing_ok=True)
            raise
        return handle

    def _run(self, job_id: str, thunk: JobThunk, token: CancelToken, slots: threading.Semaphore) -> None:
        started = time.monotonic()
        context_token = _CURRENT_CANCEL_TOKEN.set(token)
        try:
            with self._lock:
                job = self._jobs[job_id]
                if job.status == "canceled":
                    return
                job.status = "running"
                job.phase = "invoking"
                job.started_at = _now()
                self._persist_locked(job)

            def emit(event: dict[str, Any]) -> None:
                self.emit(job_id, event)

            arity = _positional_arity(thunk)
            token.raise_if_canceled()
            if arity >= 2:
                value = thunk(emit, token)
            elif arity == 1:
                value = thunk(emit)
            else:
                value = thunk()
            token.raise_if_canceled()
            norm = _normalize(value)
        except JobCanceled:
            self._finish(job_id, status="canceled", phase="canceled", error={
                "code": "canceled", "message": "job canceled", "retryable": False,
            })
        except Exception as exc:  # fail closed; details stay concise in the snapshot
            self._finish(job_id, status="failed", phase="failed", error={
                "code": "handler_error",
                "message": f"{type(exc).__name__}: {exc}",
                "retryable": False,
            })
        else:
            elapsed = round(time.monotonic() - started, 3)
            raw_error = norm["error"]
            error = None if norm["ok"] else {
                "code": self.result_failure_code,
                "message": str(raw_error or "Background job returned ok=false"),
                "retryable": False,
            }
            self._finish(
                job_id,
                status="succeeded" if norm["ok"] else "failed",
                phase="completed" if norm["ok"] else "failed",
                result=norm["contract"],
                error=error,
                provider=norm["provider"],
                model=norm["model"],
                cost_usd=norm["cost_usd"],
                duration_s=norm["duration_s"] if norm["duration_s"] is not None else elapsed,
            )
        finally:
            _CURRENT_CANCEL_TOKEN.reset(context_token)
            with self._lock:
                self._tokens.pop(job_id, None)
                self._futures.pop(job_id, None)
                self._slots.pop(job_id, None)
            slots.release()

    def emit(self, job_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in TERMINAL_STATUSES:
                return
            ev = dict(event)
            if "phase" in ev:
                next_phase = str(ev.pop("phase"))
                phase_changed = next_phase != job.phase
                job.phase = next_phase
            else:
                phase_changed = False
            ev.setdefault("seq", len(job.events) + 1)
            candidate = json.dumps(ev, ensure_ascii=False, default=str).encode("utf-8")
            current_size = self._event_bytes.get(job_id, 0)
            if len(job.events) >= self._max_events or current_size + len(candidate) > self._max_event_bytes:
                if job.events_truncated and not phase_changed:
                    return
                job.events_truncated = True
            else:
                job.events.append(ev)
                self._event_bytes[job_id] = current_size + len(candidate)
                self._append_event_locked(job_id, candidate)
            if phase_changed or job.events_truncated:
                self._persist_event_state_locked(job)

    def update(self, job_id: str, **updates: Any) -> dict[str, Any] | None:
        allowed = set(Job.__dataclass_fields__) - {"job_id", "scene", "actor", "created_at"}
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"unsupported job fields: {sorted(unknown)}")
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for key, value in updates.items():
                setattr(job, key, value)
            self._persist_locked(job)
            return job.to_dict()

    def consume_approval(self, job_id: str) -> dict[str, Any] | None:
        """原子占用一次 pending_confirmation 审批（outcome 翻到 confirmed）。

        返回占用前的 result；不可占用（不存在/非 succeeded/已消费）返回 None。
        双击/重试并发下只有一次拿到非 None，同一冻结计划不会派发两个执行 job。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            result = job.result or {}
            if job.status != "succeeded" or result.get("outcome") != "pending_confirmation":
                return None
            job.result = {**result, "outcome": "confirmed"}
            self._persist_locked(job)
            return result

    def _finish(self, job_id: str, **updates: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            # A cancellation accepted while a non-cooperative handler was
            # unwinding must remain terminal canceled.
            if job.status == "canceled" and updates.get("status") != "canceled":
                return
            for key, value in updates.items():
                setattr(job, key, value)
            job.ended_at = job.ended_at or _now()
            self._persist_locked(job)
            self._events_path(job_id).unlink(missing_ok=True)
            self._cleanup_locked()

    @staticmethod
    def _parse_time(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo is not None else parsed.astimezone()
        except ValueError:
            return None

    def _pending_confirmation_protected(self, job: Job, now: datetime) -> bool:
        outcome = (job.result or {}).get("outcome")
        expiry = self._parse_time(job.approval_expires_at)
        return outcome == "pending_confirmation" and expiry is not None and expiry > now

    def _cleanup_locked(self) -> list[str]:
        now = datetime.now().astimezone()
        cutoff = now - timedelta(days=self._retention_days)
        eligible = [
            job for job in self._jobs.values()
            if job.status in TERMINAL_STATUSES and not self._pending_confirmation_protected(job, now)
        ]
        expired = {
            job.job_id for job in eligible
            if (self._parse_time(job.ended_at) or self._parse_time(job.created_at) or now) < cutoff
        }
        survivors = [job for job in eligible if job.job_id not in expired]
        survivors.sort(
            key=lambda job: self._parse_time(job.ended_at) or self._parse_time(job.created_at) or now,
            reverse=True,
        )
        overflow = {job.job_id for job in survivors[self._max_terminal_jobs:]}
        removed = sorted(expired | overflow)
        for job_id in removed:
            job = self._jobs.pop(job_id, None)
            if job and job.idempotency_key:
                self._idempotency.pop((job.scene, job.actor, job.idempotency_key), None)
            self._tokens.pop(job_id, None)
            self._futures.pop(job_id, None)
            self._slots.pop(job_id, None)
            self._event_bytes.pop(job_id, None)
            self._path(job_id).unlink(missing_ok=True)
            self._events_path(job_id).unlink(missing_ok=True)
            artifact_dir = self.root / job_id
            if artifact_dir.is_dir():
                shutil.rmtree(artifact_dir)
        return removed

    def cleanup(self) -> list[str]:
        """Apply age/count retention; active approvals are protected until expiry."""
        with self._lock:
            return self._cleanup_locked()

    def get(self, job_id: str, *, actor: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or (actor is not None and job.actor != actor):
                return None
            return job.to_dict()

    def list(self, *, actor: str, active_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.actor == actor]
            if active_only:
                jobs = [j for j in jobs if j.status in ACTIVE_STATUSES]
            jobs.sort(key=lambda j: j.created_at, reverse=True)
            return [j.to_dict() for j in jobs[:max(1, min(limit, 200))]]

    def cancel(self, job_id: str, *, actor: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.actor != actor:
                return None
            if job.status in TERMINAL_STATUSES:
                return job.to_dict()
            token = self._tokens.get(job_id)
            if token:
                token.cancel()
            future = self._futures.get(job_id)
            if job.status == "queued" and future is not None and future.cancel():
                slots = self._slots.pop(job_id, None)
                self._futures.pop(job_id, None)
                self._tokens.pop(job_id, None)
                if slots is not None:
                    slots.release()
            job.status = "canceled"
            job.phase = "canceled"
            job.ended_at = _now()
            job.error = {"code": "canceled", "message": "job canceled", "retryable": False}
            self._persist_locked(job)
            self._events_path(job_id).unlink(missing_ok=True)
            snapshot = job.to_dict()
            self._cleanup_locked()
            return snapshot

    def shutdown(self, *, wait: bool = False) -> None:
        self._closed = True
        with self._lock:
            for token in self._tokens.values():
                token.cancel()
        self._inference_pool.shutdown(wait=wait, cancel_futures=True)
        self._ask_pool.shutdown(wait=wait, cancel_futures=True)
        self._device_pool.shutdown(wait=wait, cancel_futures=True)


