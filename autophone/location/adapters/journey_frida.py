"""Persistent Frida workers and location adapters for the dual-app journey."""
from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from functools import wraps
from typing import Any, Callable, Mapping

from autophone.location.journey.coordinates import gcj02_to_wgs84
from autophone.location.journey.models import AdapterAck, DeliveredLocation, LocationSample
from autophone.location.contracts import (
    LocationCoordinate,
    LocationSample as SharedLocationSample,
    LocationSessionSpec,
    LocationSourceSpec,
    LocationTargetSpec,
)
from autophone.location.targets import (
    LocationTargetAdapter,
    VPhoneSystemLocationAdapter,
)
from autophone.observation.frida.objc_bundle_probe import _exports
from autophone.device.frida_endpoint import resolve_frida_host
from autophone.device.stage_guard import StageGuard, OperationClass
from autophone.device.frida_lifetime import FridaLifetime
from autophone.device.stage_threads import start_device_thread

FRIDA_HOST = resolve_frida_host()

def _stage_worker(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        if method.__name__ == "stop" and self._lifetime is not None and self._lifetime.closed:
            return method(self, *args, **kwargs)
        guard = self._stage_guard or StageGuard.for_device_call()
        if guard is None:
            return method(self, *args, **kwargs)
        with guard.operation(OperationClass.FRIDA, frida_endpoint=self.frida_host):
            self._stage_guard = guard
            return method(self, *args, **kwargs)
    return call

def bounded_call(fn: Callable[[], Any], timeout_s: float = 2.0) -> None:
    def guarded() -> None:
        try:
            fn()
        except Exception:
            # Cleanup is best-effort and bounded. A script may already be destroyed
            # after an app exit or session detach; never leak that through
            # threading.excepthook after the durable result has been written.
            return

    thread = start_device_thread(target=guarded, name="journey-frida-cleanup")
    thread.join(timeout_s)


def rpc_call(exports: Any, snake: str, camel: str, *args: Any) -> Any:
    method = getattr(exports, snake, None) or getattr(exports, camel, None)
    if method is None:
        raise RuntimeError(f"agent RPC missing: {snake}/{camel}")
    return method(*args)


class FridaWorker:
    """One app PID, one Frida session, one or more scripts."""

    def __init__(
        self,
        bundle_id: str,
        scripts: Mapping[str, Path],
        *,
        device: Any | None = None,
        frida_host: str | None = None,
        ensure_bundle: Callable[[Path, str], str | Path | None] | None = None,
        attach_attempts: int = 4,
        spawn: bool = False,
        spawn_preload_labels: tuple[str, ...] = (),
        optional_script_labels: tuple[str, ...] = (),
        warmup_s: float = 0.0,
        event_sinks: Mapping[str, Callable[[dict[str, Any]], None]] | None = None,
        binary_sinks: Mapping[
            str, Callable[[Mapping[str, Any], bytes], None]
        ] | None = None,
        capture_filter: Mapping[str, Any] | None = None,
        delegate_class_names: tuple[str, ...] = (),
    ) -> None:
        self.bundle_id = bundle_id
        self._stage_guard = StageGuard.current()
        self._lifetime = None
        self.scripts = dict(scripts)
        self.device = device
        self.frida_host = resolve_frida_host(frida_host)
        self._ensure_bundle = ensure_bundle
        self.attach_attempts = attach_attempts
        self.spawn = bool(spawn)
        self.spawn_preload_labels = tuple(spawn_preload_labels)
        unknown_preloads = set(self.spawn_preload_labels) - set(self.scripts)
        if unknown_preloads:
            raise ValueError(f"unknown spawn preload labels: {sorted(unknown_preloads)}")
        self.optional_script_labels = tuple(optional_script_labels)
        unknown_optional = set(self.optional_script_labels) - set(self.scripts)
        if unknown_optional:
            raise ValueError(
                f"unknown optional script labels: {sorted(unknown_optional)}"
            )
        if set(self.optional_script_labels).intersection(self.spawn_preload_labels):
            raise ValueError("spawn preload scripts cannot be optional")
        self.warmup_s = max(0.0, float(warmup_s))
        self._event_sinks = dict(event_sinks or {})
        self._binary_sinks = dict(binary_sinks or {})
        self.capture_filter = dict(capture_filter or {})
        self.delegate_class_names = tuple(
            str(value)
            for value in delegate_class_names
            if str(value).strip()
        )
        self._sink_errors: dict[str, Exception] = {}
        self.pid = 0
        self._session: Any | None = None
        self._loaded: dict[str, Any] = {}
        self._exports: dict[str, Any] = {}
        self._events: list[dict[str, Any]] = []
        self._condition = threading.Condition()
        self._stopped = False

    @_stage_worker
    def _resolve_device(self) -> Any:
        if self.device is None:
            import frida
            self.device = frida.get_device_manager().add_remote_device(self.frida_host)
        return self.device

    @_stage_worker
    def _find_pid(self) -> int:
        return next(
            (
                int(app.pid)
                for app in self._resolve_device().enumerate_applications()
                if getattr(app, "identifier", None) == self.bundle_id
                and getattr(app, "pid", 0)
            ),
            0,
        )

    def _on_message(self, label: str, message: dict[str, Any], data: Any) -> None:
        if self._lifetime is not None:
            self._lifetime.accept_event(lambda: self._append_message(label, message, data))
            return
        self._append_message(label, message, data)

    def _append_message(self, label: str, message: dict[str, Any], data: Any) -> None:
        if message.get("type") == "send":
            payload = dict(message.get("payload") or {})
        elif message.get("type") == "error":
            payload = {
                "ev": "js_error",
                "description": message.get("description"),
                "stack": message.get("stack"),
            }
        else:
            return
        binary = bytes(data) if data is not None else None
        with self._condition:
            payload["_script"] = label
            payload["_pid"] = self.pid
            payload["_wall"] = time.time()
            payload["_event_id"] = len(self._events) + 1
            if binary is not None:
                payload["_binary_length"] = len(binary)
            self._events.append(payload)
            self._condition.notify_all()
        sink = self._event_sinks.get(label)
        if sink is not None:
            try:
                sink(dict(payload))
            except Exception as exc:  # noqa: BLE001 - surfaced through WorkerCapture.
                with self._condition:
                    self._sink_errors[label] = exc
        binary_sink = self._binary_sinks.get(label)
        if binary_sink is not None and binary is not None:
            try:
                binary_sink(dict(payload), binary)
            except Exception as exc:  # noqa: BLE001 - surfaced through sink_error.
                with self._condition:
                    self._sink_errors[label] = exc

    @_stage_worker
    def start(self) -> None:
        if self._session is not None:
            return
        device = self._resolve_device()
        self.pid = int(device.spawn([self.bundle_id])) if self.spawn else self._find_pid()
        release_suspended = lambda: None
        if self.spawn and self._stage_guard is not None:
            owned_pid = self.pid
            def kill_suspended():
                try:
                    device.kill(owned_pid)
                except Exception:
                    if any(p.pid == owned_pid for p in device.enumerate_processes()):
                        raise
                release_suspended()
            release_suspended = self._stage_guard.register_resource(kill_suspended)
        if not self.pid:
            raise RuntimeError(f"{self.bundle_id} is not running on {self.frida_host}")
        last_error: Exception | None = None
        for attempt in range(self.attach_attempts):
            try:
                self._session = device.attach(self.pid)
                break
            except Exception as exc:  # noqa: BLE001 - Frida transport retries are intentional.
                last_error = exc
                time.sleep(0.25 * (attempt + 1))
        if self._session is None:
            raise RuntimeError(f"attach {self.bundle_id} pid={self.pid} failed: {last_error}")
        if self._stage_guard is not None:
            self._lifetime = FridaLifetime(self._session, host=self.frida_host)
        resumed = False

        def load_script(label: str, path: Path) -> None:
            script = None
            try:
                if self._ensure_bundle is None:
                    from autophone.observation.frida.agent_bundle import ensure_agent_bundle
                    resolved = ensure_agent_bundle(path)
                else:
                    resolved = self._ensure_bundle(path, label)
                if resolved is not None:
                    path = Path(resolved)
                script = self._session.create_script(
                    path.read_text(encoding="utf-8"),
                )
                if self._lifetime is not None:
                    self._lifetime.add_script(script)
                script.on(
                    "message",
                    lambda message, data, script_label=label:
                    self._on_message(script_label, message, data),
                )
                script.load()
                exports = _exports(script)
                if self._lifetime is not None:
                    exports = self._lifetime.exports(exports)
            except Exception:
                if script is not None:
                    bounded_call(script.unload)
                raise
            self._loaded[label] = script
            self._exports[label] = exports

        def load_configured_script(label: str, path: Path) -> None:
            try:
                load_script(label, path)
            except Exception as exc:
                if label not in self.optional_script_labels:
                    raise
                self._on_message(
                    label,
                    {
                        "type": "send",
                        "payload": {
                            "ev": "optional_script_error",
                            "error_type": type(exc).__name__,
                            "detail": str(exc),
                        },
                    },
                    None,
                )

        try:
            preload = set(self.spawn_preload_labels) if self.spawn else set()
            for label, path in self.scripts.items():
                if label in preload:
                    load_configured_script(label, path)
            if self.spawn:
                device.resume(self.pid)
                resumed = True
                release_suspended()
                if self.warmup_s:
                    time.sleep(self.warmup_s)
            for label, path in self.scripts.items():
                if label not in preload:
                    load_configured_script(label, path)
            capture_exports = self._exports.get("capture")
            if capture_exports is not None:
                if self.capture_filter:
                    rpc_call(
                        capture_exports,
                        "configure_capture_filter",
                        "configureCaptureFilter",
                        dict(self.capture_filter),
                    )
                install = (
                    getattr(capture_exports, "install_capture_hooks", None)
                    or getattr(capture_exports, "installCaptureHooks", None)
                )
                if callable(install):
                    if self.delegate_class_names:
                        install({
                            "delegate_class_names": list(
                                self.delegate_class_names,
                            ),
                        })
                    else:
                        install()
        except Exception:
            self.stop()
            if self.spawn and not resumed:
                try:
                    device.kill(self.pid)
                except Exception:  # noqa: BLE001
                    pass
            raise

    @property
    def last_event_id(self) -> int:
        with self._condition:
            return len(self._events)

    def events(self) -> list[dict[str, Any]]:
        with self._condition:
            return [dict(event) for event in self._events]

    def events_for(self, label: str) -> list[dict[str, Any]]:
        with self._condition:
            return [
                dict(event) for event in self._events
                if event.get("_script") == label
            ]

    def events_after(
        self,
        after_event_id: int,
    ) -> tuple[int, list[dict[str, Any]]]:
        """Atomically return only events newer than the caller's cursor."""
        with self._condition:
            cursor = len(self._events)
            return (
                cursor,
                [
                    dict(event)
                    for event in self._events[max(0, int(after_event_id)):cursor]
                ],
            )

    def sink_error(self, label: str) -> Exception | None:
        with self._condition:
            return self._sink_errors.get(label)

    def exports(self, label: str) -> Any:
        if label not in self._exports:
            raise RuntimeError(f"script not loaded: {label}")
        return self._exports[label]

    @_stage_worker
    def ensure_pid(self) -> None:
        current = self._find_pid()
        if current != self.pid:
            raise RuntimeError(
                f"{self.bundle_id} pid changed during journey: {self.pid} -> {current}"
            )

    def wait_for_event(
        self,
        predicate: Callable[[Mapping[str, Any]], bool],
        *,
        after_event_id: int,
        timeout_s: float,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while True:
                for event in self._events[after_event_id:]:
                    if predicate(event):
                        return dict(event)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    @_stage_worker
    def stop(self) -> None:
        if self._stopped:
            return
        if self._lifetime is not None:
            self._lifetime.dispose()
            self._stopped = True
            self._loaded.clear()
            self._exports.clear()
            self._session = None
            return
        self._stopped = True
        for script in reversed(list(self._loaded.values())):
            bounded_call(script.unload)
        if self._session is not None:
            bounded_call(self._session.detach)
        self._loaded.clear()
        self._exports.clear()
        self._session = None


class AppProcessGuard:
    """PID-only lifecycle guard for channels that do not require Frida.

    Keeping native system-location delivery independent from a read-only
    navigation observer prevents a transient script-load failure from stopping
    vehicle movement.
    """

    def __init__(
        self,
        bundle_id: str,
        *,
        pid_lookup: Callable[[], int] | None = None,
    ) -> None:
        self.bundle_id = bundle_id
        self._pid_lookup = pid_lookup
        self.pid = 0

    def _find_pid(self) -> int:
        if self._pid_lookup is not None:
            return int(self._pid_lookup() or 0)
        from autophone.device.uds import request

        response, _sample = request({"t": "app_list"}, parse_image=False)
        apps = response.get("apps") or response.get("data") or response
        if not isinstance(apps, list):
            return 0
        return next(
            (
                int(app.get("pid") or 0)
                for app in apps
                if isinstance(app, Mapping)
                and app.get("bundle_id") == self.bundle_id
            ),
            0,
        )

    def start(self) -> None:
        if self.pid:
            return
        self.pid = self._find_pid()
        if not self.pid:
            raise RuntimeError(f"{self.bundle_id} is not running")

    def ensure_pid(self) -> None:
        current = self._find_pid()
        if current != self.pid:
            raise RuntimeError(
                f"{self.bundle_id} pid changed during journey: {self.pid} -> {current}"
            )

    def stop(self) -> None:
        return


class WorkerCapture:
    """CaptureThread-compatible view over a script in an existing worker."""

    def __init__(self, worker: FridaWorker, *, label: str = "capture") -> None:
        self.worker = worker
        self.label = label

    @property
    def pid(self) -> int:
        return self.worker.pid

    @property
    def sink_error(self) -> Exception | None:
        return self.worker.sink_error(self.label)

    def start(self) -> int:
        self.worker.start()
        self.worker.exports(self.label)
        return self.worker.pid

    def snapshot(self) -> list[dict[str, Any]]:
        return self.worker.events_for(self.label)

    def stop(self) -> None:
        # The location adapter owns the shared worker/session lifecycle.
        return


class LocationAdapter:
    passive_consumption_events: frozenset[str] = frozenset()
    consumption_failure_events: frozenset[str] = frozenset()
    name = ""
    datum = ""

    def __init__(
        self,
        worker: Any,
        *,
        consumption_timeout_s: float = 2.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.worker = worker
        self.consumption_timeout_s = consumption_timeout_s
        self.monotonic = monotonic
        self.samples: list[LocationSample] = []
        self.last_report: dict[str, Any] | None = None
        self.last_consumption_event: dict[str, Any] | None = None
        self.last_consumption_path: str | None = None
        self._samples_mu = threading.Lock()

    def _remember_sample(self, sample: LocationSample) -> None:
        with self._samples_mu:
            self.samples.append(sample)

    def start(self) -> None:
        self.worker.start()

    def stop(self) -> None:
        self.worker.stop()

    @staticmethod
    def _timestamp_ms(sample: LocationSample) -> int:
        return int(datetime.fromisoformat(sample.emitted_wall_at).timestamp() * 1000)

    def _delivered(
        self, sample: LocationSample, lat: float, lng: float, datum: str,
    ) -> DeliveredLocation:
        return DeliveredLocation(
            lat=lat,
            lng=lng,
            datum=datum,
            speed_mps=sample.speed_mps,
            course_deg=sample.course_deg,
            timestamp=sample.emitted_wall_at,
            horizontal_accuracy_m=sample.horizontal_accuracy_m,
        )

    def _ack(
        self,
        sample: LocationSample,
        delivered: DeliveredLocation,
        report: Mapping[str, Any] | None,
        event: Mapping[str, Any] | None,
        *,
        push_attempted: bool = True,
        consumption_path: str = "active",
    ) -> AdapterAck:
        now = self.monotonic()
        self.last_report = dict(report) if isinstance(report, Mapping) else None
        self.last_consumption_event = dict(event) if event is not None else None
        self.last_consumption_path = consumption_path
        # Without an active push there is no RPC report to corroborate; the
        # consumption event is then the whole evidence, and it is the stronger
        # of the two anyway (the app read our value, rather than us claiming to
        # have handed it over).
        passive_consumed = (
            event is not None
            and event.get("ev") in self.passive_consumption_events
        )
        report_ok = passive_consumed or (not push_attempted) or (
            isinstance(report, Mapping)
            and int(report.get("sampleSeq", -1)) == sample.seq
            and (
                int(report.get("delivered") or 0) > 0
                or int(report.get("scheduled") or 0) > 0
            )
        )
        event_ok = (
            event is not None
            and event.get("ev") not in self.consumption_failure_events
        )
        accepted = report_ok and event_ok
        refs = (
            (
                f"event:{self.name}:{event['_event_id']}",
                f"path:{consumption_path}",
            )
            if event is not None else ()
        )
        return AdapterAck(
            adapter=self.name,
            sample_seq=sample.seq,
            accepted=accepted,
            consumed_monotonic_s=max(now, sample.emitted_monotonic_s),
            latency_ms=max(0.0, (now - sample.emitted_monotonic_s) * 1000),
            delivered=delivered,
            evidence_refs=refs,
            error=(
                None
                if accepted
                else (
                    f"rpc_report={self.last_report!r};"
                    f"consumption_event={self.last_consumption_event!r}"
                )
            ),
        )
