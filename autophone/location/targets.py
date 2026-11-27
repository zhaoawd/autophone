"""Location target seam with production and in-memory Adapters."""
from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
import time
from types import SimpleNamespace
from typing import Any, Callable, Protocol, runtime_checkable

from autophone.location.journey.coordinates import gcj02_to_wgs84, wgs84_to_gcj02
from autophone.location.contracts import (
    CoordinateSystem,
    DeliveredLocation,
    DeliveryReceipt,
    LocationCoordinate,
    LocationControlCommand,
    LocationPersistence,
    LocationSample,
    LocationSessionSpec,
    LocationSourceKind,
    LocationTargetKind,
    LocationTargetSnapshot,
    LocationTargetSpec,
    LocationTargetState,
)
from autophone.location.system import SystemLocationClient, SystemLocationError, UDSSystemLocationTransport


class LocationTargetError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LocationSequenceConflict(LocationTargetError):
    pass


@runtime_checkable
class LocationTargetAdapter(Protocol):
    """Target role used by LocationSessionCoordinator and source publishers."""

    def open(
        self,
        session: LocationSessionSpec,
        target: LocationTargetSpec,
    ) -> LocationTargetSnapshot: ...

    def deliver(self, sample: LocationSample) -> DeliveryReceipt: ...

    def control(self, command: str) -> LocationTargetSnapshot: ...

    def close(self) -> LocationTargetSnapshot: ...

    def snapshot(self) -> LocationTargetSnapshot: ...


def _converted_sample(sample: LocationSample, coordinate_system: str) -> LocationSample:
    if sample.coordinate.coordinate_system == coordinate_system:
        return sample
    if sample.coordinate.coordinate_system == CoordinateSystem.GCJ02.value:
        latitude, longitude = gcj02_to_wgs84(sample.lat, sample.lng)
    else:
        latitude, longitude = wgs84_to_gcj02(sample.lat, sample.lng)
    return LocationSample(
        producer_sequence=sample.producer_sequence,
        emitted_wall_at=sample.emitted_wall_at,
        emitted_monotonic_s=sample.emitted_monotonic_s,
        coordinate=LocationCoordinate(latitude, longitude, coordinate_system),
        horizontal_accuracy_m=sample.horizontal_accuracy_m,
        vertical_accuracy_m=sample.vertical_accuracy_m,
        altitude_m=sample.altitude_m,
        speed_mps=sample.speed_mps,
        course_deg=sample.course_deg,
    )


def _delivered(sample: LocationSample) -> DeliveredLocation:
    return DeliveredLocation(
        coordinate=sample.coordinate,
        timestamp=sample.emitted_wall_at,
        horizontal_accuracy_m=sample.horizontal_accuracy_m,
        vertical_accuracy_m=sample.vertical_accuracy_m,
        altitude_m=sample.altitude_m,
        speed_mps=sample.speed_mps,
        course_deg=sample.course_deg,
    )


def _sample_fingerprint(sample: LocationSample) -> str:
    return json.dumps(sample.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _SequencedAdapter:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._mu = RLock()
        self._session: LocationSessionSpec | None = None
        self._target: LocationTargetSpec | None = None
        self._state = LocationTargetState.CLOSED.value
        self._generation: str | None = None
        self._process_epoch: str | None = None
        self._last_sequence: int | None = None
        self._next_sequence = 0
        self._pending_sample: tuple[int, str] | None = None
        self._last_delivery_sequence: int | None = None
        self._last_receipt: DeliveryReceipt | None = None
        self._fingerprints: dict[int, str] = {}
        self._receipts: dict[int, DeliveryReceipt] = {}
        self._error_code: str | None = None
        self._error: str | None = None

    def _bind(self, session: LocationSessionSpec, target: LocationTargetSpec) -> None:
        if target.target_id not in {item.target_id for item in session.targets}:
            raise LocationTargetError(
                "location_target_not_declared",
                f"target {target.target_id!r} is not declared by session {session.session_id!r}",
            )
        if self._session is not None and self._state != LocationTargetState.STOPPED.value:
            raise LocationTargetError("location_target_open", "target Adapter is already open")
        self._session = session
        self._target = target
        self._state = LocationTargetState.OPEN.value
        self._generation = None
        self._process_epoch = None
        self._last_sequence = None
        self._next_sequence = 0
        self._pending_sample = None
        self._last_delivery_sequence = None
        self._last_receipt = None
        self._fingerprints.clear()
        self._receipts.clear()
        self._error_code = None
        self._error = None

    def _prepare(self, sample: LocationSample) -> DeliveryReceipt | None:
        if self._session is None or self._target is None:
            raise LocationTargetError("location_target_closed", "target Adapter is not open")
        if self._state == LocationTargetState.STOPPED.value:
            raise LocationTargetError("location_target_closed", "target Adapter is stopped")
        fingerprint = _sample_fingerprint(sample)
        known = self._fingerprints.get(sample.producer_sequence)
        if known is not None:
            if known != fingerprint:
                raise LocationSequenceConflict(
                    "location_sequence_conflict",
                    f"producer_sequence {sample.producer_sequence} was reused with a different payload",
                )
            return self._receipts[sample.producer_sequence]
        if (
            self._pending_sample is not None
            and sample.producer_sequence == self._pending_sample[0]
        ):
            if fingerprint != self._pending_sample[1]:
                raise LocationSequenceConflict(
                    "location_sequence_conflict",
                    f"producer_sequence {sample.producer_sequence} was reused with a different payload",
                )
            return None
        if sample.producer_sequence != self._next_sequence:
            raise LocationSequenceConflict(
                "location_sequence_gap",
                f"expected producer_sequence {self._next_sequence}, got {sample.producer_sequence}",
            )
        self._pending_sample = (sample.producer_sequence, fingerprint)
        # Optional targets observe every proposal but do not hold the source ACK barrier.
        # Keep input continuity separate from accepted delivery and App process state.
        if not self._target.required:
            self._next_sequence = sample.producer_sequence + 1
        return None

    def _commit(self, sample: LocationSample, receipt: DeliveryReceipt) -> DeliveryReceipt:
        if not receipt.accepted:
            return receipt
        fingerprint = _sample_fingerprint(sample)
        self._fingerprints[sample.producer_sequence] = fingerprint
        self._receipts[sample.producer_sequence] = receipt
        self._last_sequence = sample.producer_sequence
        self._next_sequence = sample.producer_sequence + 1
        self._last_delivery_sequence = receipt.delivery_sequence
        self._last_receipt = receipt
        self._state = LocationTargetState.APPLIED.value
        self._error_code = None
        self._error = None
        return receipt

    def snapshot(self) -> LocationTargetSnapshot:
        with self._mu:
            if self._target is None:
                raise LocationTargetError(
                    "location_target_unbound", "target Adapter has not been opened"
                )
            return LocationTargetSnapshot(
                target=self._target,
                state=self._state,
                generation=self._generation,
                process_epoch=self._process_epoch,
                last_producer_sequence=self._last_sequence,
                last_delivery_sequence=self._last_delivery_sequence,
                last_receipt=self._last_receipt,
                error_code=self._error_code,
                error=self._error,
            )


class FakeLocationTargetAdapter(_SequencedAdapter):
    """Deterministic in-memory Adapter for coordinator and contract tests."""

    def __init__(
        self,
        *,
        reject_sequences: frozenset[int] = frozenset(),
        reject_control_commands: frozenset[str] = frozenset(),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(clock=clock)
        self.reject_sequences = reject_sequences
        self.reject_control_commands = reject_control_commands
        self.control_commands: list[str] = []

    def open(
        self,
        session: LocationSessionSpec,
        target: LocationTargetSpec,
    ) -> LocationTargetSnapshot:
        with self._mu:
            self._bind(session, target)
            self._generation = f"fake:{session.session_id}:{target.target_id}"
            return self.snapshot()

    def deliver(self, sample: LocationSample) -> DeliveryReceipt:
        with self._mu:
            cached = self._prepare(sample)
            if cached is not None:
                return cached
            assert self._session is not None and self._target is not None
            started = self._clock()
            if sample.producer_sequence in self.reject_sequences:
                self._state = LocationTargetState.BACKOFF.value
                self._error_code = "fake_rejected"
                self._error = "configured fake rejection"
                return DeliveryReceipt(
                    session_id=self._session.session_id,
                    target_id=self._target.target_id,
                    target_kind=self._target.kind,
                    producer_sequence=sample.producer_sequence,
                    accepted=False,
                    applied_monotonic_s=self._clock(),
                    latency_ms=max(0.0, (self._clock() - started) * 1000),
                    authority_coordinate=sample.coordinate,
                    error_code=self._error_code,
                    error=self._error,
                )
            delivered_sample = _converted_sample(sample, self._target.coordinate_system)
            delivery_sequence = 0 if self._last_delivery_sequence is None else self._last_delivery_sequence + 1
            receipt = DeliveryReceipt(
                session_id=self._session.session_id,
                target_id=self._target.target_id,
                target_kind=self._target.kind,
                producer_sequence=sample.producer_sequence,
                delivery_sequence=delivery_sequence,
                accepted=True,
                applied_monotonic_s=self._clock(),
                latency_ms=max(0.0, (self._clock() - started) * 1000),
                authority_coordinate=sample.coordinate,
                delivered=_delivered(delivered_sample),
            )
            return self._commit(sample, receipt)

    def close(self) -> LocationTargetSnapshot:
        with self._mu:
            if self._target is None:
                raise LocationTargetError(
                    "location_target_unbound", "target Adapter has not been opened"
                )
            self._state = LocationTargetState.STOPPED.value
            return self.snapshot()

    def control(self, command: str) -> LocationTargetSnapshot:
        with self._mu:
            if self._target is None or self._state == LocationTargetState.STOPPED.value:
                raise LocationTargetError(
                    "location_target_closed", "target Adapter is not open"
                )
            try:
                normalized = LocationControlCommand(command).value
            except ValueError as exc:
                raise LocationTargetError(
                    "location_command_unsupported",
                    f"unsupported location command {command!r}",
                ) from exc
            if normalized not in {
                LocationControlCommand.PAUSE.value,
                LocationControlCommand.RESUME.value,
            }:
                raise LocationTargetError(
                    "location_command_unsupported",
                    f"target Adapter does not support {normalized!r}",
                )
            self.control_commands.append(normalized)
            if normalized in self.reject_control_commands:
                raise LocationTargetError(
                    "fake_control_rejected",
                    f"configured fake rejection for {normalized}",
                )
            self._last_delivery_sequence = (
                0
                if self._last_delivery_sequence is None
                else self._last_delivery_sequence + 1
            )
            self._state = (
                LocationTargetState.QUIESCED.value
                if normalized == LocationControlCommand.PAUSE.value
                else LocationTargetState.APPLIED.value
            )
            return self.snapshot()


class CoreLocationSpawnGateAdapter(_SequencedAdapter):
    """One-shot App CoreLocation target installed before process resume.

    This target is intentionally operation-scoped.  The probe spawn-gates the
    declared App, waits for bounded consumption evidence, and tears the process
    down before ``deliver`` returns; it is not a background policy runtime.
    """

    def __init__(
        self,
        run_dir: str | Path,
        *,
        settle_s: float = 3.0,
        inject_delay_ms: int = 100,
        inject_delegate: bool = True,
        probe_fn: Callable[[Any], dict[str, Any]] | None = None,
        summarize_fn: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(clock=clock)
        self.run_dir = Path(run_dir)
        self.settle_s = max(0.0, float(settle_s))
        self.inject_delay_ms = int(inject_delay_ms)
        self.inject_delegate = bool(inject_delegate)
        self._probe_fn = probe_fn
        self._summarize_fn = summarize_fn
        self.last_probe_result: dict[str, Any] = {}
        self.last_summary: dict[str, Any] = {}

    def open(
        self,
        session: LocationSessionSpec,
        target: LocationTargetSpec,
    ) -> LocationTargetSnapshot:
        with self._mu:
            if target.kind != LocationTargetKind.APP_CORE_LOCATION_SPAWN.value:
                raise LocationTargetError(
                    "location_target_kind_mismatch",
                    "CoreLocationSpawnGateAdapter requires app_core_location_spawn",
                )
            if session.source.kind != LocationSourceKind.FIXED.value:
                raise LocationTargetError(
                    "location_source_unsupported",
                    "CoreLocation spawn-gate supports only a fixed source",
                )
            assert target.app is not None and target.bundle_id is not None
            from autophone.assets.apps import load_app_profile

            profile = load_app_profile(target.app)
            declaration = profile.core_location_spawn
            if profile.bundle_id != target.bundle_id:
                raise LocationTargetError(
                    "location_app_bundle_mismatch",
                    f"bundle_id does not match App Profile {target.app!r}",
                )
            if declaration.get("allowed") is not True:
                raise LocationTargetError(
                    "location_app_target_not_allowed",
                    f"App Profile {target.app!r} does not allow CoreLocation spawn-gate",
                )
            if declaration.get("coordinate_system") != target.coordinate_system:
                raise LocationTargetError(
                    "location_app_coordinate_mismatch",
                    "spawn target coordinate_system does not match App Profile",
                )
            self._bind(session, target)
            self._generation = f"corelocation-spawn:{session.session_id}"
            self._state = LocationTargetState.WAITING_SAMPLE.value
            self.last_probe_result = {}
            self.last_summary = {}
            return self.snapshot()

    def deliver(self, sample: LocationSample) -> DeliveryReceipt:
        with self._mu:
            cached = self._prepare(sample)
            if cached is not None:
                return cached
            assert self._session is not None and self._target is not None
            assert self._target.bundle_id is not None
            started = self._clock()
            delivered_sample = _converted_sample(
                sample, self._target.coordinate_system
            )
            probe_fn, summarize_fn = self._probe_functions()
            args = SimpleNamespace(
                latitude=delivered_sample.lat,
                longitude=delivered_sample.lng,
                horizontal_accuracy=delivered_sample.horizontal_accuracy_m,
                vertical_accuracy=delivered_sample.vertical_accuracy_m,
                altitude=delivered_sample.altitude_m,
                speed=delivered_sample.speed_mps,
                course=delivered_sample.course_deg,
                sample_seq=delivered_sample.producer_sequence,
                timestamp_ms=0,
                settle=self.settle_s,
                inject_delay_ms=self.inject_delay_ms,
                no_delegate_inject=not self.inject_delegate,
                bundle_id=self._target.bundle_id,
                out=self.run_dir,
            )
            try:
                result = dict(probe_fn(args) or {})
                events = list(result.get("events") or [])
                summary = dict(summarize_fn(events) or {})
            except Exception as exc:  # noqa: BLE001 - target returns a receipt
                self.last_probe_result = {
                    "error": f"{type(exc).__name__}: {exc}",
                    "events": [],
                }
                self.last_summary = {}
                return self._reject_spawn(
                    sample,
                    started,
                    "location_app_delivery_failed",
                    str(exc),
                )
            self.last_probe_result = result
            self.last_summary = summary
            pid = int(result.get("pid") or 0)
            self._process_epoch = f"pid:{pid}" if pid > 0 else None
            error = result.get("error") or result.get("spawn_error")
            if error:
                return self._reject_spawn(
                    sample,
                    started,
                    "location_spawn_gate_failed",
                    str(error),
                )
            if summary.get("app_gps_control") != "frida_mock":
                return self._reject_spawn(
                    sample,
                    started,
                    "location_app_consumption_missing",
                    f"CoreLocation control status={summary.get('status')}",
                )
            delivery_sequence = (
                0
                if self._last_delivery_sequence is None
                else self._last_delivery_sequence + 1
            )
            evidence_refs = (
                f"corelocation-spawn:{self._target.bundle_id}:pid:{pid}:"
                f"delivery:{delivery_sequence}",
            )
            receipt = DeliveryReceipt(
                session_id=self._session.session_id,
                target_id=self._target.target_id,
                target_kind=self._target.kind,
                producer_sequence=sample.producer_sequence,
                delivery_sequence=delivery_sequence,
                accepted=True,
                applied_monotonic_s=self._clock(),
                latency_ms=max(0.0, (self._clock() - started) * 1000),
                authority_coordinate=sample.coordinate,
                delivered=_delivered(delivered_sample),
                evidence_refs=evidence_refs,
            )
            committed = self._commit(sample, receipt)
            self._state = LocationTargetState.EFFECTIVE.value
            return committed

    def control(self, command: str) -> LocationTargetSnapshot:
        raise LocationTargetError(
            "location_command_unsupported",
            f"CoreLocation spawn-gate does not support {command!r}",
        )

    def close(self) -> LocationTargetSnapshot:
        with self._mu:
            if self._target is None:
                raise LocationTargetError(
                    "location_target_unbound", "target Adapter has not been opened"
                )
            self._state = LocationTargetState.STOPPED.value
            self._process_epoch = None
            return self.snapshot()

    def _probe_functions(self):
        if self._probe_fn is not None and self._summarize_fn is not None:
            return self._probe_fn, self._summarize_fn
        from autophone.location.adapters.corelocation_control import (
            run_control_probe,
            summarize_control,
        )

        return self._probe_fn or run_control_probe, self._summarize_fn or summarize_control

    def _reject_spawn(
        self,
        sample: LocationSample,
        started: float,
        code: str,
        error: str,
    ) -> DeliveryReceipt:
        assert self._session is not None and self._target is not None
        self._state = LocationTargetState.ERROR.value
        self._error_code = code
        self._error = error
        return DeliveryReceipt(
            session_id=self._session.session_id,
            target_id=self._target.target_id,
            target_kind=self._target.kind,
            producer_sequence=sample.producer_sequence,
            accepted=False,
            applied_monotonic_s=self._clock(),
            latency_ms=max(0.0, (self._clock() - started) * 1000),
            authority_coordinate=sample.coordinate,
            error_code=code,
            error=error,
        )


class VPhoneSystemLocationAdapter(_SequencedAdapter):
    """Production Adapter for the vphone system CoreLocation target."""

    def __init__(
        self,
        client: SystemLocationClient | None = None,
        *,
        heartbeat_s: float = 1.0,
        replace: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(clock=clock)
        self.client = client or SystemLocationClient()
        self._default_client = client is None
        self.heartbeat_s = heartbeat_s
        self.replace = replace

    def open(
        self,
        session: LocationSessionSpec,
        target: LocationTargetSpec,
    ) -> LocationTargetSnapshot:
        with self._mu:
            if target.kind != LocationTargetKind.SYSTEM_CORE_LOCATION.value:
                raise LocationTargetError(
                    "location_target_kind_mismatch",
                    "VPhoneSystemLocationAdapter requires system_core_location",
                )
            if session.source.kind == LocationSourceKind.HOST.value:
                raise LocationTargetError(
                    "location_source_unsupported",
                    "host source remains owned by the vphone GUI",
                )
            client = self.client
            if self._default_client:
                from autophone.device.target import resolve_target

                bound = resolve_target()
                if session.device not in {bound.device, bound.vm_dir.name}:
                    raise LocationTargetError(
                        "location_device_target_mismatch",
                        f"session device {session.device!r} is not bound to target {bound.device!r}",
                    )
                client = SystemLocationClient(UDSSystemLocationTransport(str(bound.sock)))
            self._bind(session, target)
            self.client = client
            if session.source.kind == LocationSourceKind.FIXED.value:
                self._state = LocationTargetState.WAITING_SAMPLE.value
                return self.snapshot()
            try:
                response = self.client.start_stream(
                    owner=session.owner,
                    watchdog_s=session.source.watchdog_s or 3.0,
                    on_timeout=session.source.on_timeout,
                    replace=self.replace,
                )
                self._generation = self._response_generation(response)
            except SystemLocationError as exc:
                self._state = LocationTargetState.ERROR.value
                self._error_code = exc.code
                self._error = str(exc)
                raise
            return self.snapshot()

    def deliver(self, sample: LocationSample) -> DeliveryReceipt:
        with self._mu:
            cached = self._prepare(sample)
            if cached is not None:
                return cached
            assert self._session is not None and self._target is not None
            started = self._clock()
            delivered_sample = self.client.delivered_sample(sample)
            try:
                if self._session.source.kind == LocationSourceKind.FIXED.value:
                    if sample.producer_sequence != 0:
                        raise LocationSequenceConflict(
                            "location_fixed_sequence",
                            "fixed source accepts only producer_sequence 0",
                        )
                    response = self.client.set_fixed(
                        owner=self._session.owner,
                        sample=sample,
                        heartbeat_s=self.heartbeat_s,
                        replace=self.replace,
                        persist=(
                            self._session.source.persistence
                            == LocationPersistence.DURABLE.value
                        ),
                    )
                    self._generation = self._response_generation(response)
                else:
                    if not self._generation:
                        raise LocationTargetError(
                            "location_generation_missing",
                            "system stream generation is missing",
                        )
                    response = self.client.push(self._generation, sample)
            except SystemLocationError as exc:
                self._state = LocationTargetState.BACKOFF.value
                self._error_code = exc.code
                self._error = str(exc)
                return DeliveryReceipt(
                    session_id=self._session.session_id,
                    target_id=self._target.target_id,
                    target_kind=self._target.kind,
                    producer_sequence=sample.producer_sequence,
                    accepted=False,
                    applied_monotonic_s=self._clock(),
                    latency_ms=max(0.0, (self._clock() - started) * 1000),
                    authority_coordinate=sample.coordinate,
                    error_code=exc.code,
                    error=str(exc),
                )
            try:
                delivery_sequence = self._delivery_sequence(response)
            except SystemLocationError as exc:
                self._state = LocationTargetState.BACKOFF.value
                self._error_code = exc.code
                self._error = str(exc)
                return DeliveryReceipt(
                    session_id=self._session.session_id,
                    target_id=self._target.target_id,
                    target_kind=self._target.kind,
                    producer_sequence=sample.producer_sequence,
                    accepted=False,
                    applied_monotonic_s=self._clock(),
                    latency_ms=max(0.0, (self._clock() - started) * 1000),
                    authority_coordinate=sample.coordinate,
                    error_code=exc.code,
                    error=str(exc),
                )
            receipt = DeliveryReceipt(
                session_id=self._session.session_id,
                target_id=self._target.target_id,
                target_kind=self._target.kind,
                producer_sequence=sample.producer_sequence,
                delivery_sequence=delivery_sequence,
                accepted=True,
                applied_monotonic_s=self._clock(),
                latency_ms=max(0.0, (self._clock() - started) * 1000),
                authority_coordinate=sample.coordinate,
                delivered=_delivered(delivered_sample),
                evidence_refs=tuple(
                    str(item) for item in (response.get("evidence_refs") or ())
                ),
            )
            return self._commit(sample, receipt)

    def close(self) -> LocationTargetSnapshot:
        with self._mu:
            if self._target is None:
                raise LocationTargetError(
                    "location_target_unbound", "target Adapter has not been opened"
                )
            if self._state == LocationTargetState.STOPPED.value:
                return self.snapshot()
            if self._generation:
                try:
                    self.client.stop(self._generation)
                except SystemLocationError as exc:
                    if exc.code != "location_generation_conflict":
                        raise
                    # Another owner replaced this generation (replace=true). The
                    # guest no longer holds anything for this Adapter; stopping the
                    # current source would release the other owner's generation.
                    current = str(self.client.status().get("generation") or "")
                    if current == self._generation:
                        raise
                    self._error_code = exc.code
                    self._error = (
                        f"generation {self._generation} was already replaced: {exc}"
                    )
            self._state = LocationTargetState.STOPPED.value
            return self.snapshot()

    def control(self, command: str) -> LocationTargetSnapshot:
        with self._mu:
            if self._target is None or self._state == LocationTargetState.STOPPED.value:
                raise LocationTargetError(
                    "location_target_closed", "target Adapter is not open"
                )
            if not self._generation:
                raise LocationTargetError(
                    "location_generation_missing",
                    "system location generation is missing",
                )
            try:
                normalized = LocationControlCommand(command).value
            except ValueError as exc:
                raise LocationTargetError(
                    "location_command_unsupported",
                    f"unsupported location command {command!r}",
                ) from exc
            if normalized not in {
                LocationControlCommand.PAUSE.value,
                LocationControlCommand.RESUME.value,
            }:
                raise LocationTargetError(
                    "location_command_unsupported",
                    f"target Adapter does not support {normalized!r}",
                )
            try:
                response = self.client.control(
                    self._generation,
                    paused=normalized == LocationControlCommand.PAUSE.value,
                )
                delivery_sequence = self._delivery_sequence(response)
            except SystemLocationError as exc:
                self._state = LocationTargetState.ERROR.value
                self._error_code = exc.code
                self._error = str(exc)
                raise
            if delivery_sequence is not None:
                self._last_delivery_sequence = delivery_sequence
            self._state = (
                LocationTargetState.QUIESCED.value
                if normalized == LocationControlCommand.PAUSE.value
                else LocationTargetState.APPLIED.value
            )
            self._error_code = None
            self._error = None
            return self.snapshot()

    @staticmethod
    def _response_generation(response: dict) -> str:
        generation = str(response.get("generation") or "").strip()
        if not generation:
            raise SystemLocationError(
                "location_protocol_error", "system location response has no generation"
            )
        return generation

    @staticmethod
    def _delivery_sequence(response: dict) -> int | None:
        applied = response.get("applied")
        raw = applied.get("last_delivery_sequence") if isinstance(applied, dict) else None
        if raw is None:
            raw = response.get("delivery_sequence")
        if raw is None:
            return None
        if isinstance(raw, bool):
            raise SystemLocationError(
                "location_protocol_error", "delivery_sequence must be an integer"
            )
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise SystemLocationError(
                "location_protocol_error", "delivery_sequence must be an integer"
            ) from exc
        if value < 0:
            raise SystemLocationError(
                "location_protocol_error", "delivery_sequence must be non-negative"
            )
        return value
