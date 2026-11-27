"""Location source Drivers used behind the unified session interface."""
from __future__ import annotations

from contextvars import copy_context

from dataclasses import replace
from datetime import datetime, timezone
import math
from threading import Event, RLock, Thread
import time
from typing import Callable, Protocol, runtime_checkable

from autophone.location.checkpoints import (
    LocationCheckpointError,
    LocationCheckpointStore,
)
from autophone.location.contracts import (
    LocationControlCommand,
    LocationCoordinate,
    LocationSample,
    LocationSessionSpec,
    LocationSessionState,
    LocationSourceKind,
    LocationSourceRuntimeSnapshot,
)
from autophone.location.route_replay import (
    RoutePoint,
    RouteProposal,
    RouteReplayEngine,
    RouteSample,
    bearing_deg,
    haversine_m,
    load_route_fixture,
)


class LocationSourceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


EmitLocationSample = Callable[[LocationSample], bool]


@runtime_checkable
class LocationSourceDriver(Protocol):
    """Source role owned by ``LocationSessionCoordinator``."""

    def start(self) -> LocationSourceRuntimeSnapshot: ...

    def control(
        self,
        command: str,
        *,
        value: object | None = None,
    ) -> LocationSourceRuntimeSnapshot: ...

    def stop(
        self, *, preserve_checkpoint: bool = False
    ) -> LocationSourceRuntimeSnapshot: ...

    def snapshot(self) -> LocationSourceRuntimeSnapshot: ...


class _ApproachReplayEngine:
    """Transactional straight-line motion with the route engine's small interface."""

    def __init__(
        self,
        session: LocationSessionSpec,
    ) -> None:
        source = session.source
        if source.origin is None or source.destination is None or source.speed_mps is None:
            raise ValueError("approach source requires origin, destination, and speed_mps")
        total = haversine_m(
            source.origin.latitude,
            source.origin.longitude,
            source.destination.latitude,
            source.destination.longitude,
        )
        if source.min_distance_m > total:
            raise ValueError("min_distance_m must not exceed approach distance")
        self.origin = source.origin
        self.destination = source.destination
        self.total_m = total
        self.travel_limit_m = max(0.0, total - source.min_distance_m)
        self.speed_mps = float(source.speed_mps)
        self.course_deg = bearing_deg(
            source.origin.latitude,
            source.origin.longitude,
            source.destination.latitude,
            source.destination.longitude,
        )
        self._time_scale = float(source.time_scale)
        self._distance_m = 0.0
        self._paused = False
        self._generation = 0

    @property
    def time_scale(self) -> float:
        return self._time_scale

    def _sample(self, distance_m: float) -> RouteSample:
        distance = min(self.travel_limit_m, max(0.0, float(distance_m)))
        fraction = 1.0 if self.total_m <= 0 else distance / self.total_m
        latitude = self.origin.latitude + (
            self.destination.latitude - self.origin.latitude
        ) * fraction
        longitude = self.origin.longitude + (
            self.destination.longitude - self.origin.longitude
        ) * fraction
        done = distance >= self.travel_limit_m - 1e-6
        emitted_speed = (
            0.0 if self._paused or done else self.speed_mps * self._time_scale
        )
        return RouteSample(
            lat=latitude,
            lng=longitude,
            route_distance_m=distance,
            remaining_m=max(0.0, self.total_m - distance),
            total_m=self.total_m,
            model_speed_mps=0.0 if done else self.speed_mps,
            emitted_speed_mps=emitted_speed,
            bearing_deg=self.course_deg,
            time_scale=self._time_scale,
            done=done,
        )

    def current_sample(self) -> RouteSample:
        return self._sample(self._distance_m)

    def propose(self, elapsed_wall_s: float) -> RouteProposal:
        elapsed = float(elapsed_wall_s)
        if elapsed < 0:
            raise ValueError("elapsed_wall_s must be >= 0")
        distance = self._distance_m
        if not self._paused:
            distance += self.speed_mps * elapsed * self._time_scale
        sample = self._sample(distance)
        return RouteProposal(
            generation=self._generation,
            sample=sample,
            next_distance_m=sample.route_distance_m,
            next_speed_mps=sample.model_speed_mps,
        )

    def propose_arrival(self) -> RouteProposal:
        sample = self._sample(self.travel_limit_m)
        return RouteProposal(
            generation=self._generation,
            sample=sample,
            next_distance_m=sample.route_distance_m,
            next_speed_mps=0.0,
        )

    def commit(self, proposal: RouteProposal) -> RouteSample:
        if proposal.generation != self._generation:
            raise RuntimeError("stale approach proposal")
        self._distance_m = proposal.next_distance_m
        self._generation += 1
        return self.current_sample()

    def pause(self) -> None:
        if not self._paused:
            self._paused = True
            self._generation += 1

    def resume(self) -> None:
        if self._paused:
            self._paused = False
            self._generation += 1

    def set_time_scale(self, value: float) -> None:
        scale = float(value)
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError("time_scale must be > 0")
        if scale != self._time_scale:
            self._time_scale = scale
            self._generation += 1


class _TransactionalMotionSourceDriver:
    """Drive one motion source through the coordinator's ACK barrier.

    The Driver owns producer sequence, retry identity, route progress, and the
    background clock. It never holds its state lock while calling ``emit`` so
    coordinator control and the worker cannot deadlock each other.
    """

    def __init__(
        self,
        session: LocationSessionSpec,
        emit: EmitLocationSample,
        *,
        engine,
        expected_kind: str,
        checkpoint_store: LocationCheckpointStore | None = None,
        restored_checkpoint: bool = False,
        tick_s: float = 1.0,
        max_consecutive_failures: int = 5,
        wall_clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        run_async: bool = True,
    ) -> None:
        if session.source.kind != expected_kind:
            raise LocationSourceError(
                "location_source_kind_mismatch",
                f"source Driver requires kind {expected_kind!r}",
            )
        if tick_s <= 0:
            raise ValueError("tick_s must be > 0")
        if max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be >= 1")
        self.session = session
        self.emit = emit
        self.tick_s = float(tick_s)
        self.max_consecutive_failures = int(max_consecutive_failures)
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic
        self.run_async = run_async
        self.engine = engine
        self._checkpoint_store = checkpoint_store
        self._restored_checkpoint = restored_checkpoint
        self._mu = RLock()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._state = LocationSessionState.STARTING.value
        self._next_sequence = 0
        self._last_sequence: int | None = None
        self._pending_sample: LocationSample | None = None
        self._pending_proposal: RouteProposal | None = None
        self._emitting = False
        self._consecutive_failures = 0
        self._error_code: str | None = None
        self._error: str | None = None

    def start(self) -> LocationSourceRuntimeSnapshot:
        with self._mu:
            if self._thread is not None or self._last_sequence is not None:
                raise LocationSourceError(
                    "location_source_open", "motion source is already started"
                )
            current = self.engine.current_sample()
        if not self._attempt(None, current):
            raise LocationSourceError(
                self._error_code or "location_delivery_rejected",
                self._error or "route origin was rejected",
            )
        with self._mu:
            if self._restored_checkpoint and self._state != LocationSessionState.HOLDING.value:
                self.engine.pause()
                self._refresh_pending_proposal_locked()
                self._state = LocationSessionState.PAUSED.value
                self._persist_checkpoint_locked()
        if self.run_async and self._state != LocationSessionState.HOLDING.value:
            context = copy_context()
            thread = Thread(
                target=lambda: context.run(self._run),
                name=f"location-{self.session.source.kind}-{self.session.session_id}",
                daemon=True,
            )
            with self._mu:
                self._thread = thread
            thread.start()
        return self.snapshot()

    def control(
        self,
        command: str,
        *,
        value: object | None = None,
    ) -> LocationSourceRuntimeSnapshot:
        try:
            normalized = LocationControlCommand(command).value
        except ValueError as exc:
            raise LocationSourceError(
                "location_command_unsupported",
                f"unsupported source command {command!r}",
            ) from exc

        with self._mu:
            self._require_not_emitting()
            if self._state == LocationSessionState.STOPPED.value:
                raise LocationSourceError(
                    "location_source_stopped", "motion source is stopped"
                )
            if normalized == LocationControlCommand.PAUSE.value:
                if self._state == LocationSessionState.PAUSED.value:
                    return self.snapshot()
                if self._state not in {
                    LocationSessionState.RUNNING.value,
                    LocationSessionState.BACKOFF.value,
                }:
                    raise self._state_conflict(normalized)
                self.engine.pause()
                self._refresh_pending_proposal_locked()
                self._state = LocationSessionState.PAUSED.value
                self._persist_checkpoint_locked()
                return self.snapshot()
            if normalized == LocationControlCommand.RESUME.value:
                if self._state == LocationSessionState.RUNNING.value:
                    return self.snapshot()
                if self._state not in {
                    LocationSessionState.PAUSED.value,
                    LocationSessionState.HOLDING.value,
                    LocationSessionState.ERROR_PAUSED.value,
                }:
                    raise self._state_conflict(normalized)
                self.engine.resume()
                self._refresh_pending_proposal_locked()
                self._state = LocationSessionState.RUNNING.value
                self._consecutive_failures = 0
                self._error_code = None
                self._error = None
                self._persist_checkpoint_locked()
                return self.snapshot()
            if normalized == LocationControlCommand.SET_TIME_SCALE.value:
                if value is None:
                    raise LocationSourceError(
                        "location_control_value_required",
                        "set_time_scale requires value",
                    )
                try:
                    self.engine.set_time_scale(float(value))
                    self._refresh_pending_proposal_locked()
                except (TypeError, ValueError) as exc:
                    raise LocationSourceError(
                        "location_control_value_invalid", str(exc)
                    ) from exc
                self._persist_checkpoint_locked()
                return self.snapshot()
            if normalized == LocationControlCommand.HOLD.value:
                if self._state == LocationSessionState.HOLDING.value:
                    return self.snapshot()
                if self._state not in {
                    LocationSessionState.RUNNING.value,
                    LocationSessionState.BACKOFF.value,
                }:
                    raise self._state_conflict(normalized)
                self.engine.pause()
                self._refresh_pending_proposal_locked()
                self._state = LocationSessionState.HOLDING.value
                self._persist_checkpoint_locked()
                return self.snapshot()
            if normalized == LocationControlCommand.ARRIVE_NOW.value:
                if self._state not in {
                    LocationSessionState.RUNNING.value,
                    LocationSessionState.BACKOFF.value,
                }:
                    raise self._state_conflict(normalized)
                if self._pending_sample is not None:
                    raise LocationSourceError(
                        "location_source_pending_sample",
                        "retry the pending route sample before arrive_now",
                    )
                proposal = self.engine.propose_arrival()
                sample = proposal.sample
            else:
                raise LocationSourceError(
                    "location_command_unsupported",
                    f"unsupported source command {command!r}",
                )

        if not self._attempt(proposal, sample):
            raise LocationSourceError(
                self._error_code or "location_delivery_rejected",
                self._error or "arrival sample was rejected",
            )
        return self.snapshot()

    def stop(
        self, *, preserve_checkpoint: bool = False
    ) -> LocationSourceRuntimeSnapshot:
        with self._mu:
            self._stop_event.set()
            if preserve_checkpoint:
                self._persist_checkpoint_locked()
            elif self._checkpoint_store is not None:
                try:
                    self._checkpoint_store.remove(self.session.session_id)
                except LocationCheckpointError as exc:
                    raise LocationSourceError(
                        "location_checkpoint_write_failed", str(exc)
                    ) from exc
            self._state = LocationSessionState.STOPPED.value
            return self.snapshot()

    def snapshot(self) -> LocationSourceRuntimeSnapshot:
        with self._mu:
            current = self.engine.current_sample()
            return LocationSourceRuntimeSnapshot(
                state=self._state,
                source_kind=self.session.source.kind,
                route_id=self.session.source.route_id,
                time_scale=self.engine.time_scale,
                route_distance_m=current.route_distance_m,
                traveled_m=current.route_distance_m + current.tail_distance_m,
                phase=(
                    ("hold" if self._state == LocationSessionState.HOLDING.value else current.phase)
                    if self.session.source.kind == LocationSourceKind.ROUTE.value else None
                ),
                tail_distance_m=current.tail_distance_m,
                tail_total_m=current.tail_total_m,
                remaining_m=current.remaining_m,
                total_m=current.total_m,
                current_coordinate=LocationCoordinate(
                    latitude=current.lat,
                    longitude=current.lng,
                    coordinate_system=self.session.source.coordinate_system,
                ),
                model_speed_mps=current.model_speed_mps,
                emitted_speed_mps=current.emitted_speed_mps,
                bearing_deg=current.bearing_deg,
                done=current.done,
                last_producer_sequence=self._last_sequence,
                error_code=self._error_code,
                error=self._error,
            )

    def _run(self) -> None:
        previous = self.monotonic()
        while not self._stop_event.wait(self.tick_s):
            now = self.monotonic()
            elapsed = max(0.0, now - previous)
            previous = now
            with self._mu:
                if self._state == LocationSessionState.STOPPED.value:
                    return
                if self._state in {
                    LocationSessionState.PAUSED.value,
                    LocationSessionState.HOLDING.value,
                    LocationSessionState.ERROR_PAUSED.value,
                }:
                    continue
                proposal = self._pending_proposal
                sample = self._pending_sample
                if sample is None:
                    proposal = self.engine.propose(elapsed)
                    route_sample = proposal.sample
                else:
                    route_sample = None
            accepted = (
                self._attempt(proposal, route_sample)
                if sample is None
                else self._retry_pending()
            )
            if not accepted:
                with self._mu:
                    if self._consecutive_failures >= self.max_consecutive_failures:
                        self._state = LocationSessionState.ERROR_PAUSED.value
                        self._error_code = "location_source_retry_exhausted"
                        self._error = (
                            "route delivery retry limit reached at producer_sequence "
                            f"{self._pending_sample.producer_sequence if self._pending_sample else 'unknown'}"
                        )

    def _retry_pending(self) -> bool:
        with self._mu:
            sample = self._pending_sample
            proposal = self._pending_proposal
            self._require_not_emitting()
            if sample is None:
                return True
            self._emitting = True
        return self._finish_attempt(sample, proposal)

    def _attempt(
        self,
        proposal: RouteProposal | None,
        route_sample: RouteSample,
    ) -> bool:
        with self._mu:
            self._require_not_emitting()
            if self._pending_sample is not None:
                raise LocationSourceError(
                    "location_source_pending_sample",
                    "a route sample is already pending",
                )
            sample = self._location_sample(route_sample)
            self._pending_sample = sample
            self._pending_proposal = proposal
            self._emitting = True
        return self._finish_attempt(sample, proposal)

    def _finish_attempt(
        self,
        sample: LocationSample,
        proposal: RouteProposal | None,
    ) -> bool:
        error: Exception | None = None
        try:
            accepted = bool(self.emit(sample))
        except Exception as exc:  # noqa: BLE001
            accepted = False
            error = exc
        with self._mu:
            self._emitting = False
            if self._state == LocationSessionState.STOPPED.value:
                if accepted:
                    self._pending_sample = None
                    self._pending_proposal = None
                return accepted
            if accepted:
                if proposal is not None:
                    self.engine.commit(proposal)
                self._last_sequence = sample.producer_sequence
                self._next_sequence += 1
                self._pending_sample = None
                self._pending_proposal = None
                self._consecutive_failures = 0
                self._error_code = None
                self._error = None
                self._state = (
                    LocationSessionState.HOLDING.value
                    if self.engine.current_sample().done
                    else LocationSessionState.RUNNING.value
                )
                try:
                    self._persist_checkpoint_locked()
                except LocationSourceError as exc:
                    self._state = LocationSessionState.ERROR_PAUSED.value
                    self._error_code = exc.code
                    self._error = str(exc)
                return True
            self._consecutive_failures += 1
            self._state = LocationSessionState.BACKOFF.value
            self._error_code = getattr(
                error, "code", "location_delivery_rejected"
            )
            self._error = str(error or "required location target rejected sample")
            return False

    def _location_sample(self, sample: RouteSample) -> LocationSample:
        emitted_at = self.wall_clock()
        if not isinstance(emitted_at, datetime):
            raise LocationSourceError(
                "location_clock_invalid", "wall_clock must return datetime"
            )
        return LocationSample(
            producer_sequence=self._next_sequence,
            emitted_wall_at=emitted_at.isoformat(),
            emitted_monotonic_s=self.monotonic(),
            coordinate=LocationCoordinate(
                latitude=sample.lat,
                longitude=sample.lng,
                coordinate_system=self.session.source.coordinate_system,
            ),
            speed_mps=sample.emitted_speed_mps,
            course_deg=sample.bearing_deg,
        )

    def _refresh_pending_proposal_locked(self) -> None:
        # Controls invalidate engine proposals, but an unacknowledged sample must
        # retain its original geometry, sequence, and timestamps on retry.
        if self._pending_proposal is not None:
            self._pending_proposal = replace(
                self._pending_proposal, generation=self.engine.propose(0).generation
            )

    def _require_not_emitting(self) -> None:
        if self._emitting:
            raise LocationSourceError(
                "location_source_busy", "motion source is publishing a sample"
            )

    def _state_conflict(self, command: str) -> LocationSourceError:
        return LocationSourceError(
            "location_control_state_conflict",
            f"cannot {command} motion source in state {self._state!r}",
        )

    def _persist_checkpoint_locked(self) -> None:
        if self._checkpoint_store is None or not hasattr(self.engine, "checkpoint"):
            return
        checkpoint = dict(self.engine.checkpoint())
        checkpoint.update({
            "source_state": self._state,
            "last_producer_sequence": self._last_sequence,
        })
        try:
            self._checkpoint_store.save(self.session, checkpoint)
        except LocationCheckpointError as exc:
            raise LocationSourceError(
                "location_checkpoint_write_failed", str(exc)
            ) from exc


class RouteLocationSourceDriver(_TransactionalMotionSourceDriver):
    """Managed source Driver for one tracked route fixture."""

    def __init__(
        self,
        session: LocationSessionSpec,
        emit: EmitLocationSample,
        *,
        checkpoint_store: LocationCheckpointStore | None = None,
        **kwargs,
    ) -> None:
        if session.source.kind != LocationSourceKind.ROUTE.value:
            raise LocationSourceError(
                "location_source_kind_mismatch",
                "RouteLocationSourceDriver requires a route source",
            )
        fixture = load_route_fixture(session.source.route_id or "")
        engine = RouteReplayEngine(
            fixture,
            time_scale=session.source.time_scale,
            tail_to=(
                RoutePoint(session.source.tail_to.latitude, session.source.tail_to.longitude)
                if session.source.tail_to is not None else None
            ),
        )
        restored = False
        if checkpoint_store is not None:
            checkpoint = checkpoint_store.load(session)
            if checkpoint is not None:
                engine.restore_checkpoint(checkpoint)
                restored = True
        super().__init__(
            session,
            emit,
            engine=engine,
            expected_kind=LocationSourceKind.ROUTE.value,
            checkpoint_store=checkpoint_store,
            restored_checkpoint=restored,
            **kwargs,
        )


class ApproachLocationSourceDriver(_TransactionalMotionSourceDriver):
    """Managed source Driver for straight-line origin-to-destination motion."""

    def __init__(
        self,
        session: LocationSessionSpec,
        emit: EmitLocationSample,
        **kwargs,
    ) -> None:
        if session.source.kind != LocationSourceKind.APPROACH.value:
            raise LocationSourceError(
                "location_source_kind_mismatch",
                "ApproachLocationSourceDriver requires an approach source",
            )
        super().__init__(
            session,
            emit,
            engine=_ApproachReplayEngine(session),
            expected_kind=LocationSourceKind.APPROACH.value,
            **kwargs,
        )
