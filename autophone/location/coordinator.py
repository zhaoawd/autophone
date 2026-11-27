"""Unified location session lifecycle over target Adapters."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context, copy_context
from dataclasses import dataclass, field
from threading import Event, RLock, Thread, current_thread
import time
from typing import Callable

from autophone.assets.apps import load_app_profile
from autophone.location.contracts import (
    DeliveryReceipt,
    LocationCapabilitySnapshot,
    LocationControlCommand,
    LocationSample,
    LocationSessionSnapshot,
    LocationSessionSpec,
    LocationSessionState,
    LocationSourceKind,
    LocationSystemSnapshot,
    LocationTargetKind,
    LocationTargetSpec,
)
from autophone.location.checkpoints import (
    LocationCheckpointError,
    LocationCheckpointStore,
)
from autophone.location.sources import (
    ApproachLocationSourceDriver,
    EmitLocationSample,
    LocationSourceDriver,
    LocationSourceError,
    RouteLocationSourceDriver,
)
from autophone.location.system import SystemLocationClient
from autophone.location.targets import (
    LocationTargetAdapter,
    VPhoneSystemLocationAdapter,
)


class LocationCoordinatorError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


LocationTargetAdapterFactory = Callable[
    [LocationSessionSpec, LocationTargetSpec, bool],
    LocationTargetAdapter,
]
LocationSourceDriverFactory = Callable[
    [LocationSessionSpec, EmitLocationSample],
    LocationSourceDriver | None,
]

location_checkpoint_store = LocationCheckpointStore()


def production_location_target_factory(
    _session: LocationSessionSpec,
    target: LocationTargetSpec,
    replace: bool,
    *,
    client: SystemLocationClient | None = None,
) -> LocationTargetAdapter:
    if target.kind == LocationTargetKind.SYSTEM_CORE_LOCATION.value:
        return VPhoneSystemLocationAdapter(client, replace=replace)
    raise LocationCoordinatorError(
        "location_target_unsupported",
        f"no production Adapter is registered for target kind {target.kind!r}",
    )


def production_location_source_factory(
    session: LocationSessionSpec,
    emit: EmitLocationSample,
) -> LocationSourceDriver | None:
    if session.source.kind == LocationSourceKind.ROUTE.value:
        try:
            return RouteLocationSourceDriver(
                session,
                emit,
                checkpoint_store=location_checkpoint_store,
            )
        except LocationCheckpointError as exc:
            raise LocationCoordinatorError(
                "location_checkpoint_read_failed", str(exc)
            ) from exc
        except (LocationSourceError, ValueError) as exc:
            raise LocationCoordinatorError(
                getattr(exc, "code", "location_source_invalid"), str(exc)
            ) from exc
    if session.source.kind == LocationSourceKind.APPROACH.value:
        try:
            return ApproachLocationSourceDriver(session, emit)
        except (LocationSourceError, ValueError) as exc:
            raise LocationCoordinatorError(
                getattr(exc, "code", "location_source_invalid"), str(exc)
            ) from exc
    return None


@dataclass(frozen=True)
class LocationSessionDelivery:
    sample: LocationSample
    receipts: tuple[DeliveryReceipt, ...]
    required_accepted: bool
    snapshot: LocationSessionSnapshot

    def as_dict(self) -> dict:
        return {
            "sample": self.sample.as_dict(),
            "receipts": [receipt.as_dict() for receipt in self.receipts],
            "required_accepted": self.required_accepted,
            "snapshot": self.snapshot.as_dict(),
        }


@dataclass
class _SessionRuntime:
    spec: LocationSessionSpec
    adapters: dict[str, LocationTargetAdapter]
    state: str
    source_driver: LocationSourceDriver | None = None
    last_producer_sequence: int | None = None
    last_sample: LocationSample | None = None
    last_sample_monotonic_s: float | None = None
    watchdog_fired: bool = False
    error_code: str | None = None
    error: str | None = None
    access_paused: bool = False
    delivery_executor: ThreadPoolExecutor | None = None
    context: Context = field(default_factory=copy_context)


class LocationSessionCoordinator:
    """Own session identity, target claims, fan-out, ACK barrier, and stop."""

    STOPPED_SESSION_HISTORY = 128

    def __init__(
        self,
        adapter_factory: LocationTargetAdapterFactory = production_location_target_factory,
        *,
        source_driver_factory: LocationSourceDriverFactory = production_location_source_factory,
        supported_target_kinds: tuple[str, ...] = (
            LocationTargetKind.SYSTEM_CORE_LOCATION.value,
        ),
        lock_status_fn: Callable[[str], dict] | None = None,
        lease_status_fn: Callable[[], dict | None] | None = None,
        access_poll_s: float = 0.25,
        audit_fn: Callable[[str, dict], None] | None = None,
    ) -> None:
        self._adapter_factory = adapter_factory
        self._source_driver_factory = source_driver_factory
        self._supported_target_kinds = tuple(dict.fromkeys(supported_target_kinds))
        self._lock_status_fn = lock_status_fn
        self._lease_status_fn = lease_status_fn
        self._access_poll_s = max(0.05, float(access_poll_s))
        self._audit_fn = audit_fn
        self._mu = RLock()
        self._sessions: dict[str, _SessionRuntime] = {}
        self._stopped_sessions: OrderedDict[str, LocationSessionSnapshot] = OrderedDict()
        self._claims: dict[str, str] = {}
        self._access_stop = Event()
        self._access_thread: Thread | None = None

    def start(
        self,
        spec: LocationSessionSpec,
        *,
        initial_sample: LocationSample | None = None,
        replace: bool = False,
    ) -> LocationSessionSnapshot:
        with self._mu:
            existing = self._sessions.get(spec.session_id)
            if existing is not None:
                if existing.spec == spec and existing.state != LocationSessionState.STOPPED.value:
                    return self._snapshot(existing)
                raise LocationCoordinatorError(
                    "location_session_conflict",
                    f"session_id {spec.session_id!r} is already registered",
                )
            self._validate_supported_targets(spec)
            self._require_device_access(spec)
            self._validate_sample(spec, initial_sample, required=(
                spec.source.kind == LocationSourceKind.FIXED.value
            ))
            conflicts = self._conflicting_session_ids(spec)
            if conflicts and not replace:
                raise LocationCoordinatorError(
                    "location_target_claim_conflict",
                    f"location targets are held by sessions {sorted(conflicts)!r}",
                )
            for session_id in sorted(conflicts):
                replaced = self._sessions[session_id]
                self._stop_runtime(replaced)
                self._retire_runtime(replaced)

            adapters: dict[str, LocationTargetAdapter] = {}
            opened: list[LocationTargetAdapter] = []
            registered = False
            runtime: _SessionRuntime | None = None
            try:
                for target in spec.targets:
                    adapter = self._adapter_factory(spec, target, replace)
                    adapter.open(spec, target)
                    adapters[target.target_id] = adapter
                    opened.append(adapter)
                runtime = _SessionRuntime(
                    spec=spec,
                    adapters=adapters,
                    state=(
                        LocationSessionState.WAITING_TARGET.value
                        if initial_sample is None
                        else LocationSessionState.STARTING.value
                    ),
                    delivery_executor=(
                        ThreadPoolExecutor(
                            max_workers=len(spec.targets),
                            thread_name_prefix=f"location-delivery-{spec.session_id}",
                        )
                        if len(spec.targets) > 1
                        else None
                    ),
                )
                if initial_sample is not None:
                    delivery = self._deliver(runtime, initial_sample)
                    if not delivery.required_accepted:
                        rejected = next(
                            receipt
                            for receipt in delivery.receipts
                            if not receipt.accepted
                            and self._target(spec, receipt.target_id).required
                        )
                        if rejected.error_code == "location_app_not_running":
                            runtime.state = LocationSessionState.WAITING_TARGET.value
                        else:
                            raise LocationCoordinatorError(
                                rejected.error_code or "location_delivery_rejected",
                                rejected.error or "required location target rejected sample",
                            )
                runtime.source_driver = self._source_driver_factory(
                    spec,
                    lambda sample: self._publish_from_source(
                        spec.session_id, sample
                    ).required_accepted,
                )
                self._sessions[spec.session_id] = runtime
                self._stopped_sessions.pop(spec.session_id, None)
                for target in spec.targets:
                    self._claims[self._claim_key(target)] = spec.session_id
                registered = True
                self._ensure_runtime_monitor()
                if runtime.source_driver is not None:
                    try:
                        runtime.source_driver.start()
                    except LocationSourceError as exc:
                        raise LocationCoordinatorError(exc.code, str(exc)) from exc
                return self._snapshot(runtime)
            except Exception:
                if runtime is not None and runtime.source_driver is not None:
                    try:
                        runtime.source_driver.stop()
                    except Exception:
                        pass
                if runtime is not None and runtime.delivery_executor is not None:
                    runtime.delivery_executor.shutdown(wait=True, cancel_futures=True)
                    runtime.delivery_executor = None
                for adapter in reversed(opened):
                    try:
                        adapter.close()
                    except Exception:
                        pass
                if registered:
                    self._sessions.pop(spec.session_id, None)
                    for target in spec.targets:
                        claim = self._claim_key(target)
                        if self._claims.get(claim) == spec.session_id:
                            self._claims.pop(claim, None)
                raise

    def publish(
        self,
        session_id: str,
        sample: LocationSample,
        *,
        owner: str,
    ) -> LocationSessionDelivery:
        with self._mu:
            runtime = self._active_runtime(session_id)
            self._require_owner(runtime, owner)
            self._require_device_access(runtime.spec)
            if runtime.source_driver is not None:
                raise LocationCoordinatorError(
                    "location_source_managed",
                    "samples for a managed source are published by its source Driver",
                )
            if runtime.state == LocationSessionState.PAUSED.value:
                raise LocationCoordinatorError(
                    "location_session_paused",
                    f"location session {session_id!r} is paused",
                )
            self._validate_sample(runtime.spec, sample, required=True)
            return self._deliver(runtime, sample)

    def _publish_from_source(
        self,
        session_id: str,
        sample: LocationSample,
    ) -> LocationSessionDelivery:
        with self._mu:
            runtime = self._active_runtime(session_id)
            self._require_device_access(runtime.spec)
            if runtime.access_paused:
                raise LocationCoordinatorError(
                    "location_device_locked",
                    f"device {runtime.spec.device!r} is held by another ScreenLock owner",
                )
            if runtime.source_driver is None:
                raise LocationCoordinatorError(
                    "location_source_unmanaged",
                    "location session has no managed source Driver",
                )
            self._validate_sample(runtime.spec, sample, required=True)
            return self._deliver(runtime, sample)

    def control(
        self,
        session_id: str,
        command: str,
        *,
        owner: str,
        value: object | None = None,
    ) -> LocationSessionSnapshot:
        with self._mu:
            runtime = self._active_runtime(session_id)
            self._require_owner(runtime, owner)
            if runtime.access_paused:
                raise LocationCoordinatorError(
                    "location_device_locked",
                    f"device {runtime.spec.device!r} is held by another ScreenLock owner",
                )
            try:
                normalized = LocationControlCommand(command).value
            except ValueError as exc:
                raise LocationCoordinatorError(
                    "location_command_unsupported",
                    f"unsupported location command {command!r}",
                ) from exc

            if normalized in {
                LocationControlCommand.SET_TIME_SCALE.value,
                LocationControlCommand.ARRIVE_NOW.value,
                LocationControlCommand.HOLD.value,
            }:
                if runtime.source_driver is None:
                    raise LocationCoordinatorError(
                        "location_command_unsupported",
                        f"source kind {runtime.spec.source.kind!r} does not support {normalized}",
                    )
                try:
                    source = self._control_source(
                        runtime,
                        normalized,
                        value=value,
                    )
                except LocationSourceError as exc:
                    runtime.error_code = exc.code
                    runtime.error = str(exc)
                    raise LocationCoordinatorError(exc.code, str(exc)) from exc
                runtime.state = source.state
                runtime.error_code = source.error_code
                runtime.error = source.error
                return self._snapshot(runtime)

            current_state = self._runtime_state(runtime)
            if normalized == LocationControlCommand.PAUSE.value:
                if current_state == LocationSessionState.PAUSED.value:
                    return self._snapshot(runtime)
                if current_state != LocationSessionState.RUNNING.value:
                    raise LocationCoordinatorError(
                        "location_control_state_conflict",
                        f"cannot pause session in state {current_state!r}",
                    )
                target_state = LocationSessionState.PAUSED.value
                rollback_command = LocationControlCommand.RESUME.value
            else:
                if current_state == LocationSessionState.RUNNING.value:
                    return self._snapshot(runtime)
                if current_state == LocationSessionState.HOLDING.value:
                    if runtime.source_driver is None:
                        raise LocationCoordinatorError(
                            "location_control_state_conflict",
                            "holding session has no managed source Driver",
                        )
                    try:
                        source = self._control_source(runtime, normalized)
                    except LocationSourceError as exc:
                        raise LocationCoordinatorError(exc.code, str(exc)) from exc
                    runtime.state = source.state
                    runtime.error_code = source.error_code
                    runtime.error = source.error
                    return self._snapshot(runtime)
                if (
                    current_state == LocationSessionState.ERROR_PAUSED.value
                    and runtime.source_driver is not None
                ):
                    try:
                        source = self._control_source(runtime, normalized)
                    except LocationSourceError as exc:
                        raise LocationCoordinatorError(exc.code, str(exc)) from exc
                    runtime.state = source.state
                    runtime.error_code = source.error_code
                    runtime.error = source.error
                    return self._snapshot(runtime)
                if current_state != LocationSessionState.PAUSED.value:
                    raise LocationCoordinatorError(
                        "location_control_state_conflict",
                        f"cannot resume session in state {current_state!r}",
                    )
                target_state = LocationSessionState.RUNNING.value
                rollback_command = LocationControlCommand.PAUSE.value

            previous_state = current_state
            controlled: list[LocationTargetAdapter] = []
            source_controlled = False
            try:
                if (
                    normalized == LocationControlCommand.PAUSE.value
                    and runtime.source_driver is not None
                ):
                    self._control_source(runtime, normalized)
                    source_controlled = True
                for target in runtime.spec.targets:
                    adapter = runtime.adapters[target.target_id]
                    adapter.control(normalized)
                    controlled.append(adapter)
                if (
                    normalized == LocationControlCommand.RESUME.value
                    and runtime.source_driver is not None
                ):
                    self._control_source(runtime, normalized)
                    source_controlled = True
            except Exception as exc:  # noqa: BLE001
                rollback_failures: list[str] = []
                for adapter in reversed(controlled):
                    try:
                        adapter.control(rollback_command)
                    except Exception as rollback_exc:  # noqa: BLE001
                        rollback_failures.append(str(rollback_exc))
                if source_controlled and runtime.source_driver is not None:
                    try:
                        self._control_source(runtime, rollback_command)
                    except Exception as rollback_exc:  # noqa: BLE001
                        rollback_failures.append(str(rollback_exc))
                if rollback_failures:
                    runtime.state = LocationSessionState.ERROR_PAUSED.value
                    runtime.error_code = "location_control_rollback_failed"
                    runtime.error = "; ".join(rollback_failures)
                    raise LocationCoordinatorError(
                        runtime.error_code,
                        f"location control failed and rollback was incomplete: {exc}",
                    ) from exc
                runtime.state = previous_state
                runtime.error_code = getattr(
                    exc, "code", "location_control_rejected"
                )
                runtime.error = str(exc)
                raise LocationCoordinatorError(
                    runtime.error_code,
                    runtime.error,
                ) from exc

            runtime.state = target_state
            runtime.error_code = None
            runtime.error = None
            return self._snapshot(runtime)

    def _control_source(
        self,
        runtime: _SessionRuntime,
        command: str,
        *,
        value: object | None = None,
    ):
        """调用 source control 时让出 coordinator 锁，允许正在投递的样本完成。

        调用方必须持有 ``_mu``。返回前重新持锁并确认 session runtime 未被替换。
        """
        source_driver = runtime.source_driver
        if source_driver is None:
            raise LocationCoordinatorError(
                "location_source_unmanaged",
                "location session has no managed source Driver",
            )
        result = None
        error: Exception | None = None
        self._mu.release()
        try:
            result = source_driver.control(command, value=value)
        except Exception as exc:  # noqa: BLE001 - restored after coordinator lock
            error = exc
        finally:
            self._mu.acquire()
        if self._sessions.get(runtime.spec.session_id) is not runtime:
            raise LocationCoordinatorError(
                "location_session_stopped",
                f"location session {runtime.spec.session_id!r} changed during control",
            )
        if error is not None:
            raise error
        return result

    def stop(self, session_id: str, *, owner: str) -> LocationSessionSnapshot:
        with self._mu:
            runtime = self._sessions.get(str(session_id))
            if runtime is None:
                stopped = self._stopped_sessions.get(str(session_id))
                if stopped is None:
                    raise LocationCoordinatorError(
                        "location_session_not_found",
                        f"location session {session_id!r} not found",
                    )
                self._require_owner(stopped, owner)
                self._stopped_sessions.move_to_end(str(session_id))
                return stopped
            self._require_owner(runtime, owner)
            self._stop_runtime(runtime)
            return self._retire_runtime(runtime)

    def snapshot(self, session_id: str) -> LocationSessionSnapshot:
        with self._mu:
            runtime = self._sessions.get(str(session_id))
            if runtime is not None:
                return self._snapshot(runtime)
            stopped = self._stopped_sessions.get(str(session_id))
            if stopped is not None:
                self._stopped_sessions.move_to_end(str(session_id))
                return stopped
            raise LocationCoordinatorError(
                "location_session_not_found",
                f"location session {session_id!r} not found",
            )

    def system_snapshot(self) -> LocationSystemSnapshot:
        with self._mu:
            return LocationSystemSnapshot(
                capabilities=tuple(
                    LocationCapabilitySnapshot(
                        target_kind=kind,
                        installed=True,
                        detail="Autophone target Adapter registered; remote availability is checked on open",
                    )
                    for kind in self._supported_target_kinds
                ),
                sessions=tuple(
                    self._snapshot(runtime)
                    for runtime in self._sessions.values()
                ),
            )

    def _deliver(
        self,
        runtime: _SessionRuntime,
        sample: LocationSample,
    ) -> LocationSessionDelivery:
        runtime.last_sample = sample
        runtime.last_sample_monotonic_s = time.monotonic()
        runtime.watchdog_fired = False
        if len(runtime.spec.targets) == 1:
            target = runtime.spec.targets[0]
            receipts = (runtime.adapters[target.target_id].deliver(sample),)
        else:
            executor = runtime.delivery_executor
            if executor is None:
                raise LocationCoordinatorError(
                    "location_delivery_executor_closed",
                    "location delivery executor is not available",
                )
            futures = [
                executor.submit(
                    copy_context().run,
                    runtime.adapters[target.target_id].deliver,
                    sample,
                )
                for target in runtime.spec.targets
            ]
            receipts = tuple(future.result() for future in futures)
        required_accepted = all(
            receipt.accepted
            for target, receipt in zip(runtime.spec.targets, receipts, strict=True)
            if target.required
        )
        runtime.last_producer_sequence = (
            sample.producer_sequence if required_accepted else runtime.last_producer_sequence
        )
        runtime.state = (
            LocationSessionState.RUNNING.value
            if required_accepted
            else LocationSessionState.BACKOFF.value
        )
        rejected = next((
            receipt
            for target, receipt in zip(runtime.spec.targets, receipts, strict=True)
            if target.required and not receipt.accepted
        ), None)
        runtime.error_code = rejected.error_code if rejected else None
        runtime.error = rejected.error if rejected else None
        return LocationSessionDelivery(
            sample=sample,
            receipts=receipts,
            required_accepted=required_accepted,
            snapshot=self._snapshot(runtime),
        )

    def shutdown(self, *, preserve_checkpoints: bool = True) -> None:
        """Bounded service shutdown; route progress remains recoverable by default."""
        self._access_stop.set()
        access_thread = self._access_thread
        if access_thread is not None and access_thread is not current_thread():
            access_thread.join(timeout=max(1.0, self._access_poll_s * 2))
        with self._mu:
            failures: list[str] = []
            for runtime in tuple(self._sessions.values()):
                if runtime.state == LocationSessionState.STOPPED.value:
                    continue
                try:
                    self._stop_runtime(
                        runtime,
                        preserve_checkpoint=preserve_checkpoints,
                    )
                    self._retire_runtime(runtime)
                except LocationCoordinatorError as exc:
                    failures.append(str(exc))
            if failures:
                raise LocationCoordinatorError(
                    "location_shutdown_failed", "; ".join(failures)
                )
            self._access_thread = None

    def _stop_runtime(
        self,
        runtime: _SessionRuntime,
        *,
        preserve_checkpoint: bool = False,
    ) -> None:
        if runtime.state == LocationSessionState.STOPPED.value:
            return
        runtime.state = LocationSessionState.STOPPING.value
        failures: list[str] = []
        if runtime.source_driver is not None:
            try:
                runtime.source_driver.stop(
                    preserve_checkpoint=preserve_checkpoint
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(f"source:{runtime.spec.source.source_id}: {exc}")
        for target in reversed(runtime.spec.targets):
            try:
                runtime.adapters[target.target_id].close()
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{target.target_id}: {exc}")
        if runtime.delivery_executor is not None:
            runtime.delivery_executor.shutdown(wait=True, cancel_futures=True)
            runtime.delivery_executor = None
        if failures:
            runtime.state = LocationSessionState.ERROR_PAUSED.value
            runtime.error_code = "location_target_stop_failed"
            runtime.error = "; ".join(failures)
            raise LocationCoordinatorError(runtime.error_code, runtime.error)
        runtime.state = LocationSessionState.STOPPED.value
        runtime.error_code = None
        runtime.error = None
        runtime.access_paused = False
        for target in runtime.spec.targets:
            self._claims.pop(self._claim_key(target), None)

    def _retire_runtime(self, runtime: _SessionRuntime) -> LocationSessionSnapshot:
        snapshot = self._snapshot(runtime)
        session_id = runtime.spec.session_id
        self._sessions.pop(session_id, None)
        self._stopped_sessions[session_id] = snapshot
        self._stopped_sessions.move_to_end(session_id)
        while len(self._stopped_sessions) > self.STOPPED_SESSION_HISTORY:
            self._stopped_sessions.popitem(last=False)
        return snapshot

    def _snapshot(self, runtime: _SessionRuntime) -> LocationSessionSnapshot:
        source_runtime = (
            runtime.source_driver.snapshot()
            if runtime.source_driver is not None
            else None
        )
        state = self._runtime_state(runtime, source_runtime=source_runtime)
        error_code = runtime.error_code
        error = runtime.error
        if source_runtime is not None and source_runtime.error_code:
            error_code = source_runtime.error_code
            error = source_runtime.error
        return LocationSessionSnapshot(
            spec=runtime.spec,
            state=state,
            targets=tuple(
                runtime.adapters[target.target_id].snapshot()
                for target in runtime.spec.targets
            ),
            source_runtime=source_runtime,
            paused=state == LocationSessionState.PAUSED.value,
            last_producer_sequence=runtime.last_producer_sequence,
            error_code=error_code,
            error=error,
        )

    @staticmethod
    def _runtime_state(
        runtime: _SessionRuntime,
        *,
        source_runtime=None,
    ) -> str:
        if runtime.state in {
            LocationSessionState.STOPPING.value,
            LocationSessionState.STOPPED.value,
        }:
            return runtime.state
        source = source_runtime
        if source is None and runtime.source_driver is not None:
            source = runtime.source_driver.snapshot()
        if source is not None and source.state in {
            LocationSessionState.PAUSED.value,
            LocationSessionState.HOLDING.value,
            LocationSessionState.BACKOFF.value,
            LocationSessionState.ERROR_PAUSED.value,
        }:
            return source.state
        return runtime.state

    def _runtime(self, session_id: str) -> _SessionRuntime:
        runtime = self._sessions.get(str(session_id))
        if runtime is None:
            raise LocationCoordinatorError(
                "location_session_not_found", f"location session {session_id!r} not found"
            )
        return runtime

    def _active_runtime(self, session_id: str) -> _SessionRuntime:
        if str(session_id) in self._stopped_sessions:
            raise LocationCoordinatorError(
                "location_session_stopped", f"location session {session_id!r} is stopped"
            )
        runtime = self._runtime(session_id)
        if runtime.state in {
            LocationSessionState.STOPPING.value,
            LocationSessionState.STOPPED.value,
        }:
            raise LocationCoordinatorError(
                "location_session_stopped", f"location session {session_id!r} is stopped"
            )
        return runtime

    @staticmethod
    def _require_owner(
        runtime: _SessionRuntime | LocationSessionSnapshot,
        owner: str,
    ) -> None:
        if runtime.spec.owner != str(owner):
            raise LocationCoordinatorError(
                "location_owner_conflict", "location session is owned by another owner"
            )

    def _validate_supported_targets(self, spec: LocationSessionSpec) -> None:
        unsupported = sorted({
            target.kind
            for target in spec.targets
            if target.kind not in self._supported_target_kinds
        })
        if unsupported:
            raise LocationCoordinatorError(
                "location_target_unsupported",
                f"target kinds are not registered: {unsupported!r}",
            )

    @staticmethod
    def _validate_sample(
        spec: LocationSessionSpec,
        sample: LocationSample | None,
        *,
        required: bool,
    ) -> None:
        if sample is None:
            if required:
                raise LocationCoordinatorError(
                    "location_initial_sample_required",
                    f"source kind {spec.source.kind!r} requires an initial sample",
                )
            return
        if sample.coordinate.coordinate_system != spec.source.coordinate_system:
            raise LocationCoordinatorError(
                "location_coordinate_system_mismatch",
                "sample coordinate_system must match the session source",
            )
        if sample.producer_sequence != 0 and spec.source.kind == LocationSourceKind.FIXED.value:
            raise LocationCoordinatorError(
                "location_sequence_conflict", "fixed source initial sequence must be 0"
            )

    def _conflicting_session_ids(self, spec: LocationSessionSpec) -> set[str]:
        return {
            self._claims[key]
            for target in spec.targets
            if (key := self._claim_key(target)) in self._claims
        }

    def reconcile_access(self) -> None:
        """Apply ScreenLock/lease state to active sessions immediately."""
        with self._mu:
            for runtime in tuple(self._sessions.values()):
                if runtime.state in {
                    LocationSessionState.STOPPING.value,
                    LocationSessionState.STOPPED.value,
                }:
                    continue
                self._reconcile_runtime(runtime)

    def _reconcile_runtime(self, runtime: _SessionRuntime) -> None:
        blocked = not self._device_access_allowed(runtime.spec)
        if blocked and not runtime.access_paused:
            self._pause_for_access(runtime)
        elif not blocked and runtime.access_paused:
            self._resume_after_access(runtime)
        elif not blocked:
            self._reconcile_targets(runtime)
        self._reconcile_external_watchdog(runtime)

    def await_quiesced(self, device: str, timeout_s: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while time.monotonic() <= deadline:
            self.reconcile_access()
            with self._mu:
                active = [
                    runtime
                    for runtime in self._sessions.values()
                    if runtime.spec.device == device
                    and runtime.state not in {
                        LocationSessionState.STOPPING.value,
                        LocationSessionState.STOPPED.value,
                    }
                ]
                if all(
                    runtime.access_paused
                    or self._device_access_allowed(runtime.spec)
                    for runtime in active
                ):
                    return True
            time.sleep(0.02)
        return False

    def _ensure_runtime_monitor(self) -> None:
        if self._access_thread is not None and self._access_thread.is_alive():
            return
        self._access_stop.clear()
        self._access_thread = Thread(
            target=self._access_loop,
            name="location-session-access",
            daemon=True,
        )
        self._access_thread.start()

    def _access_loop(self) -> None:
        while not self._access_stop.wait(self._access_poll_s):
            with self._mu:
                for runtime in tuple(self._sessions.values()):
                    if runtime.state in {
                        LocationSessionState.STOPPING.value,
                        LocationSessionState.STOPPED.value,
                    }:
                        continue
                    try:
                        # Each session owns its context; a shared monitor must not
                        # borrow the first session's target or stage authority.
                        runtime.context.copy().run(self._reconcile_runtime, runtime)
                    except Exception:
                        continue

    def _require_device_access(self, spec: LocationSessionSpec) -> None:
        if not self._check_device_access(spec):
            raise LocationCoordinatorError(
                "location_device_locked",
                f"device {spec.device!r} is held by another ScreenLock owner",
            )

    def _device_access_allowed(self, spec: LocationSessionSpec) -> bool:
        try:
            return self._check_device_access(spec)
        except LocationCoordinatorError:
            # Background reconciliation pauses delivery until access can be verified.
            return False

    def _check_device_access(self, spec: LocationSessionSpec) -> bool:
        if self._lock_status_fn is None:
            return True
        try:
            lock = self._lock_status_fn(spec.device)
        except Exception as exc:
            raise LocationCoordinatorError(
                "location_access_status_unavailable",
                f"cannot read ScreenLock status for device {spec.device!r}",
            ) from exc
        if not isinstance(lock, dict) or type(lock.get("locked")) is not bool:
            raise LocationCoordinatorError(
                "location_access_status_unavailable", f"invalid ScreenLock status for device {spec.device!r}",
            )
        if not lock["locked"]:
            return True
        lock_owner = str(lock.get("run_id") or "")
        if lock_owner != spec.owner or self._lease_status_fn is None:
            return False
        try:
            lease = self._lease_status_fn()
        except Exception as exc:
            raise LocationCoordinatorError(
                "location_access_status_unavailable",
                f"cannot read lease status for device {spec.device!r}",
            ) from exc
        if not isinstance(lease, dict):
            return False
        try:
            live = float(lease.get("expires_at") or 0.0) > time.time()
        except (TypeError, ValueError):
            live = False
        return bool(
            live
            and lease.get("owner") == spec.owner
            and lease.get("device") == spec.device
        )

    def _pause_for_access(self, runtime: _SessionRuntime) -> None:
        failures: list[str] = []
        if runtime.source_driver is not None:
            source_state = runtime.source_driver.snapshot().state
            if source_state in {
                LocationSessionState.RUNNING.value,
                LocationSessionState.BACKOFF.value,
            }:
                try:
                    self._control_source(runtime, LocationControlCommand.PAUSE.value)
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"source: {exc}")
        for target in runtime.spec.targets:
            try:
                runtime.adapters[target.target_id].control(
                    LocationControlCommand.PAUSE.value
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{target.target_id}: {exc}")
        runtime.access_paused = True
        runtime.state = LocationSessionState.PAUSED.value
        runtime.error_code = "location_access_quiesce_failed" if failures else None
        runtime.error = "; ".join(failures) if failures else None
        self._audit("session_quiesced", runtime, failures=failures)

    def _resume_after_access(self, runtime: _SessionRuntime) -> None:
        failures: list[str] = []
        for target in runtime.spec.targets:
            try:
                runtime.adapters[target.target_id].control(
                    LocationControlCommand.RESUME.value
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{target.target_id}: {exc}")
        if runtime.source_driver is not None:
            source_state = runtime.source_driver.snapshot().state
            if source_state == LocationSessionState.PAUSED.value:
                try:
                    self._control_source(runtime, LocationControlCommand.RESUME.value)
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"source: {exc}")
        runtime.access_paused = bool(failures)
        runtime.state = (
            LocationSessionState.ERROR_PAUSED.value
            if failures
            else LocationSessionState.RUNNING.value
        )
        runtime.error_code = "location_access_resume_failed" if failures else None
        runtime.error = "; ".join(failures) if failures else None
        self._audit("session_access_resumed", runtime, failures=failures)

    def _reconcile_targets(self, runtime: _SessionRuntime) -> None:
        if runtime.last_sample is None:
            return
        for target in runtime.spec.targets:
            adapter = runtime.adapters[target.target_id]
            reconcile = getattr(adapter, "reconcile", None)
            if callable(reconcile):
                reconcile(runtime.last_sample)
        if runtime.spec.source.kind != LocationSourceKind.FIXED.value:
            return
        required_ready = all(
            runtime.adapters[target.target_id].snapshot().last_producer_sequence
            == runtime.last_sample.producer_sequence
            for target in runtime.spec.targets
            if target.required
        )
        if required_ready:
            runtime.last_producer_sequence = runtime.last_sample.producer_sequence
            runtime.state = LocationSessionState.RUNNING.value
            runtime.error_code = None
            runtime.error = None
        elif any(
            runtime.adapters[target.target_id].snapshot().state == "waiting_app"
            for target in runtime.spec.targets
            if target.required
        ):
            runtime.state = LocationSessionState.WAITING_TARGET.value

    def _reconcile_external_watchdog(self, runtime: _SessionRuntime) -> None:
        source = runtime.spec.source
        if (
            source.kind != LocationSourceKind.EXTERNAL_STREAM.value
            or source.watchdog_s is None
            or runtime.watchdog_fired
            or runtime.last_sample_monotonic_s is None
        ):
            return
        if time.monotonic() - runtime.last_sample_monotonic_s <= source.watchdog_s:
            return
        runtime.watchdog_fired = True
        if source.on_timeout == "stop":
            self._audit("external_stream_timed_out", runtime, action="stop")
            self._stop_runtime(runtime)
            self._retire_runtime(runtime)
            return
        runtime.state = LocationSessionState.HOLDING.value
        runtime.error_code = None
        runtime.error = None
        self._audit("external_stream_timed_out", runtime, action="hold")

    def _audit(self, event: str, runtime: _SessionRuntime, **fields) -> None:
        if self._audit_fn is None:
            return
        self._audit_fn(event, {
            "session_id": runtime.spec.session_id,
            "owner": runtime.spec.owner,
            "device": runtime.spec.device,
            **fields,
        })

    @staticmethod
    def _claim_key(target: LocationTargetSpec) -> str:
        if target.kind == LocationTargetKind.SYSTEM_CORE_LOCATION.value:
            return f"{target.device}:system_core_location"
        return f"{target.device}:{target.kind}:{target.bundle_id}"

    @staticmethod
    def _target(spec: LocationSessionSpec, target_id: str) -> LocationTargetSpec:
        return next(target for target in spec.targets if target.target_id == target_id)
