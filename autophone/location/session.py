"""Location Adapter integration for target-bound Run execution."""
from __future__ import annotations

import time
from typing import Callable, Sequence

from autophone.device import DeviceSession
from autophone.location.journey.adapters import LocationAdapter
from autophone.location.journey.models import AdapterAck, DeliveredLocation, LocationSample


class DeviceSystemLocationAdapter:
    name = "vphone_system_location"

    def __init__(
        self,
        device: DeviceSession,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.device = device
        self.monotonic = monotonic
        self._started = False

    def start(self) -> None:
        self._started = True

    def deliver(self, sample: LocationSample) -> AdapterAck:
        if not self._started:
            raise RuntimeError("location adapter is not started")
        started = self.monotonic()
        self.device.set_location(
            sample.lat,
            sample.lng,
            speed=sample.speed_mps,
            course=sample.course_deg,
            horizontal_accuracy=sample.horizontal_accuracy_m,
        )
        ended = self.monotonic()
        return AdapterAck(
            adapter=self.name,
            sample_seq=sample.seq,
            accepted=True,
            consumed_monotonic_s=ended,
            latency_ms=max(0.0, (ended - started) * 1000),
            delivered=DeliveredLocation(
                lat=sample.lat,
                lng=sample.lng,
                datum=sample.datum,
                speed_mps=sample.speed_mps,
                course_deg=sample.course_deg,
                timestamp=sample.emitted_wall_at,
                horizontal_accuracy_m=sample.horizontal_accuracy_m,
            ),
            evidence_refs=(f"rig:{self.device.target.rig_id}:sample:{sample.seq}",),
        )

    def stop(self) -> None:
        self._started = False


class LocationExecutionSession:
    """Own one or more real Location Adapter lifecycles for a Run."""

    def __init__(
        self,
        device: DeviceSession,
        adapters: Sequence[LocationAdapter],
    ) -> None:
        self.device = device
        self.adapters = tuple(adapters)
        self._started: list[LocationAdapter] = []

    def start(self) -> None:
        try:
            for adapter in self.adapters:
                adapter.start()
                self._started.append(adapter)
        except Exception:
            self.stop()
            raise

    def deliver(self, sample: LocationSample) -> tuple[AdapterAck, ...]:
        return tuple(adapter.deliver(sample) for adapter in self._started)

    def stop(self) -> None:
        errors = []
        for adapter in reversed(self._started):
            try:
                adapter.stop()
            except Exception as exc:  # noqa: BLE001 - stop remaining adapters
                errors.append(exc)
        self._started.clear()
        if errors:
            raise RuntimeError(
                "; ".join(f"{type(exc).__name__}: {exc}" for exc in errors)
            )
