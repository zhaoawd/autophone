"""Owned system CoreLocation fixed and external-stream session helpers.

This module is the small non-HTTP facade used by probes and runners.  It keeps
owner, generation, sequence, coordinate validation, and stop semantics behind
the same production target Adapter used by ``LocationSessionCoordinator``.
"""
from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any, Callable
from uuid import uuid4

from autophone.device.target import default_device
from autophone.location.contracts import (
    CoordinateSystem,
    LocationCoordinate,
    LocationPersistence,
    LocationSample,
    LocationSessionSpec,
    LocationSourceSpec,
    LocationTargetSpec,
)
from autophone.location.publishers import LocationStreamPublisher
from autophone.location.targets import (
    LocationTargetAdapter,
    VPhoneSystemLocationAdapter,
)
from autophone.location.system import SystemLocationClient


class SystemLocationSessionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _identity(kind: str, owner: str | None) -> tuple[str, str]:
    token = uuid4().hex[:12]
    normalized_owner = str(owner or f"system-{kind}:{token}").strip()
    if not normalized_owner:
        raise ValueError("owner is required")
    return normalized_owner, f"system-{kind}:{normalized_owner}:{token}"


def _system_target(device: str) -> LocationTargetSpec:
    normalized = str(device or "").strip()
    if not normalized:
        raise ValueError("device is required")
    return LocationTargetSpec(
        target_id=f"system:{normalized}",
        kind="system_core_location",
        device=normalized,
        coordinate_system=CoordinateSystem.WGS84.value,
    )


def _adapter(
    target_adapter: LocationTargetAdapter | None,
    client: SystemLocationClient | None,
    *,
    replace: bool,
) -> LocationTargetAdapter:
    if target_adapter is not None and client is not None:
        raise ValueError("target_adapter and client are mutually exclusive")
    return target_adapter or VPhoneSystemLocationAdapter(client, replace=replace)


class SystemFixedLocationSession:
    """Own one fixed system location until ``stop`` or process handoff.

    ``persist=True`` requests vphone durable fixed restoration.  A caller that
    intentionally hands the fixed source to vphone may omit ``stop`` and retain
    the returned owner/generation receipt for later reconciliation.
    """

    def __init__(
        self,
        *,
        owner: str | None = None,
        device: str | None = None,
        persist: bool = False,
        replace: bool = False,
        client: SystemLocationClient | None = None,
        target_adapter: LocationTargetAdapter | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.owner, session_id = _identity("fixed", owner)
        self.device = default_device() if device is None else str(device).strip()
        self.persist = bool(persist)
        self.target = _system_target(self.device)
        self.spec = LocationSessionSpec(
            session_id=session_id,
            owner=self.owner,
            device=self.device,
            source=LocationSourceSpec(
                source_id=f"fixed:{session_id}",
                kind="fixed",
                coordinate_system=CoordinateSystem.WGS84.value,
                persistence=(
                    LocationPersistence.DURABLE.value
                    if self.persist
                    else LocationPersistence.EPHEMERAL.value
                ),
            ),
            targets=(self.target,),
        )
        self.adapter = _adapter(target_adapter, client, replace=replace)
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic
        self._opened = False
        self._started = False

    @property
    def generation(self) -> str | None:
        return self.adapter.snapshot().generation if self._opened else None

    def start(
        self,
        latitude: float,
        longitude: float,
        *,
        horizontal_accuracy_m: float = 5.0,
        vertical_accuracy_m: float = 5.0,
        altitude_m: float = 0.0,
        speed_mps: float = 0.0,
        course_deg: float = -1.0,
    ) -> dict[str, Any]:
        if self._started:
            raise SystemLocationSessionError(
                "location_session_open", "fixed system location session is already started"
            )
        emitted_at = self.wall_clock()
        if not isinstance(emitted_at, datetime):
            raise SystemLocationSessionError(
                "location_clock_invalid", "wall_clock must return datetime"
            )
        sample = LocationSample(
            producer_sequence=0,
            emitted_wall_at=emitted_at.isoformat(),
            emitted_monotonic_s=self.monotonic(),
            coordinate=LocationCoordinate(
                latitude, longitude, CoordinateSystem.WGS84.value
            ),
            horizontal_accuracy_m=horizontal_accuracy_m,
            vertical_accuracy_m=vertical_accuracy_m,
            altitude_m=altitude_m,
            speed_mps=speed_mps,
            course_deg=course_deg,
        )
        try:
            self.adapter.open(self.spec, self.target)
            self._opened = True
            receipt = self.adapter.deliver(sample)
            if not receipt.accepted:
                raise SystemLocationSessionError(
                    receipt.error_code or "location_delivery_rejected",
                    receipt.error or "system fixed location was rejected",
                )
            self._started = True
            return {
                "schema": "autophone.system-location-session/v1",
                "ok": True,
                "kind": "fixed",
                "session_id": self.spec.session_id,
                "owner": self.owner,
                "generation": self.generation,
                "persist": self.persist,
                "receipt": receipt.as_dict(),
                "snapshot": self.adapter.snapshot().as_dict(),
            }
        except Exception:
            if self._opened and not self._started:
                try:
                    self.adapter.close()
                finally:
                    self._opened = False
            raise

    def stop(self) -> dict[str, Any]:
        if not self._opened:
            return {
                "schema": "autophone.system-location-session/v1",
                "ok": True,
                "kind": "fixed",
                "session_id": self.spec.session_id,
                "owner": self.owner,
                "generation": None,
                "stopped": True,
            }
        generation = self.generation
        snapshot = self.adapter.close()
        self._opened = False
        self._started = False
        return {
            "schema": "autophone.system-location-session/v1",
            "ok": True,
            "kind": "fixed",
            "session_id": self.spec.session_id,
            "owner": self.owner,
            "generation": generation,
            "stopped": True,
            "snapshot": snapshot.as_dict(),
        }


class SystemLocationStreamSession:
    """Own one sequential external stream over the system target Adapter."""

    def __init__(
        self,
        *,
        owner: str | None = None,
        device: str | None = None,
        coordinate_system: str = CoordinateSystem.WGS84.value,
        watchdog_s: float = 3.0,
        on_timeout: str = "hold",
        replace: bool = False,
        client: SystemLocationClient | None = None,
        target_adapter: LocationTargetAdapter | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.owner, session_id = _identity("stream", owner)
        self.device = default_device() if device is None else str(device).strip()
        self.coordinate_system = CoordinateSystem(coordinate_system).value
        self.target = _system_target(self.device)
        self.spec = LocationSessionSpec(
            session_id=session_id,
            owner=self.owner,
            device=self.device,
            source=LocationSourceSpec(
                source_id=f"external:{session_id}",
                kind="external_stream",
                coordinate_system=self.coordinate_system,
                watchdog_s=watchdog_s,
                on_timeout=on_timeout,
            ),
            targets=(self.target,),
        )
        adapter = _adapter(target_adapter, client, replace=replace)
        publisher_kwargs: dict[str, Any] = {"monotonic": monotonic}
        if wall_clock is not None:
            publisher_kwargs["wall_clock"] = wall_clock
        self.publisher = LocationStreamPublisher(
            adapter, self.spec, self.target, **publisher_kwargs
        )
        self._opened = False

    @property
    def generation(self) -> str | None:
        return self.publisher.generation if self._opened else None

    def start(self) -> dict[str, Any]:
        if self._opened:
            raise SystemLocationSessionError(
                "location_session_open", "system location stream is already started"
            )
        snapshot = self.publisher.start()
        self._opened = True
        if not snapshot.generation:
            self.publisher.stop()
            self._opened = False
            raise SystemLocationSessionError(
                "location_generation_missing", "system location stream has no generation"
            )
        return {
            "schema": "autophone.system-location-session/v1",
            "ok": True,
            "kind": "external_stream",
            "session_id": self.spec.session_id,
            "owner": self.owner,
            "generation": snapshot.generation,
            "snapshot": snapshot.as_dict(),
        }

    def publish(
        self,
        latitude: float,
        longitude: float,
        *,
        horizontal_accuracy_m: float = 5.0,
        vertical_accuracy_m: float = 5.0,
        altitude_m: float = 0.0,
        speed_mps: float = -1.0,
        course_deg: float = -1.0,
    ) -> dict[str, Any]:
        if not self._opened:
            raise SystemLocationSessionError(
                "location_session_closed", "system location stream is not started"
            )
        receipt = self.publisher.publish(
            LocationCoordinate(latitude, longitude, self.coordinate_system),
            horizontal_accuracy_m=horizontal_accuracy_m,
            vertical_accuracy_m=vertical_accuracy_m,
            altitude_m=altitude_m,
            speed_mps=speed_mps,
            course_deg=course_deg,
        )
        return {
            "schema": "autophone.system-location-session/v1",
            "ok": receipt.accepted,
            "kind": "external_stream",
            "session_id": self.spec.session_id,
            "owner": self.owner,
            "generation": self.generation,
            "receipt": receipt.as_dict(),
            "snapshot": self.publisher.snapshot().as_dict(),
        }

    def stop(self) -> dict[str, Any]:
        generation = self.generation
        snapshot = self.publisher.stop() if self._opened else None
        self._opened = False
        return {
            "schema": "autophone.system-location-session/v1",
            "ok": True,
            "kind": "external_stream",
            "session_id": self.spec.session_id,
            "owner": self.owner,
            "generation": generation,
            "stopped": True,
            "snapshot": snapshot.as_dict() if snapshot is not None else None,
        }
