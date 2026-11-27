"""Location source publishers built on the shared target Adapter seam."""
from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Callable

from autophone.location.contracts import (
    DeliveryReceipt,
    LocationCoordinate,
    LocationSample,
    LocationSessionSpec,
    LocationSourceKind,
    LocationTargetSnapshot,
    LocationTargetSpec,
)
from autophone.location.targets import LocationTargetAdapter


class LocationPublisherError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LocationStreamPublisher:
    """Own producer sequencing and retry identity for one target Adapter.

    A rejected sample remains pending. The next attempt must contain the same
    location payload and reuses both its producer sequence and timestamps.
    """

    def __init__(
        self,
        adapter: LocationTargetAdapter,
        session: LocationSessionSpec,
        target: LocationTargetSpec,
        *,
        wall_clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if target.target_id not in {item.target_id for item in session.targets}:
            raise LocationPublisherError(
                "location_target_not_declared",
                f"target {target.target_id!r} is not declared by the session",
            )
        if session.source.kind in {
            LocationSourceKind.FIXED.value,
            LocationSourceKind.HOST.value,
        }:
            raise LocationPublisherError(
                "location_source_not_streaming",
                f"source kind {session.source.kind!r} does not publish sequential samples",
            )
        self.adapter = adapter
        self.session = session
        self.target = target
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic
        self.next_producer_sequence = 0
        self.last_attempt_sequence: int | None = None
        self.last_receipt: DeliveryReceipt | None = None
        self._pending_sample: LocationSample | None = None
        self._opened = False

    @property
    def pending_sample(self) -> LocationSample | None:
        return self._pending_sample

    @property
    def generation(self) -> str | None:
        return self.adapter.snapshot().generation if self._opened else None

    @property
    def last_delivery_sequence(self) -> int | None:
        return self.last_receipt.delivery_sequence if self.last_receipt else None

    def start(self) -> LocationTargetSnapshot:
        if self._opened:
            raise LocationPublisherError(
                "location_publisher_open", "location publisher is already open"
            )
        snapshot = self.adapter.open(self.session, self.target)
        self._opened = True
        return snapshot

    def publish(
        self,
        coordinate: LocationCoordinate,
        *,
        speed_mps: float = -1.0,
        course_deg: float = -1.0,
        horizontal_accuracy_m: float = 5.0,
        vertical_accuracy_m: float = 5.0,
        altitude_m: float = 0.0,
    ) -> DeliveryReceipt:
        if not self._opened:
            raise LocationPublisherError(
                "location_publisher_closed", "location publisher is not open"
            )
        if not isinstance(coordinate, LocationCoordinate):
            raise LocationPublisherError(
                "location_coordinate_invalid",
                "coordinate must be a LocationCoordinate",
            )
        requested = (
            coordinate,
            float(horizontal_accuracy_m),
            float(vertical_accuracy_m),
            float(altitude_m),
            float(speed_mps),
            float(course_deg),
        )
        sample = self._pending_sample
        if sample is None:
            emitted_at = self.wall_clock()
            if not isinstance(emitted_at, datetime):
                raise LocationPublisherError(
                    "location_clock_invalid", "wall_clock must return datetime"
                )
            sample = LocationSample(
                producer_sequence=self.next_producer_sequence,
                emitted_wall_at=emitted_at.isoformat(),
                emitted_monotonic_s=self.monotonic(),
                coordinate=coordinate,
                horizontal_accuracy_m=horizontal_accuracy_m,
                vertical_accuracy_m=vertical_accuracy_m,
                altitude_m=altitude_m,
                speed_mps=speed_mps,
                course_deg=course_deg,
            )
            self._pending_sample = sample
        else:
            pending = (
                sample.coordinate,
                sample.horizontal_accuracy_m,
                sample.vertical_accuracy_m,
                sample.altitude_m,
                sample.speed_mps,
                sample.course_deg,
            )
            if pending != requested:
                raise LocationPublisherError(
                    "location_pending_sample_conflict",
                    "a rejected sample must be retried with the same payload",
                )

        self.last_attempt_sequence = sample.producer_sequence
        receipt = self.adapter.deliver(sample)
        self.last_receipt = receipt
        if receipt.accepted:
            self.next_producer_sequence += 1
            self._pending_sample = None
        return receipt

    def stop(self) -> LocationTargetSnapshot | None:
        if not self._opened:
            return None
        snapshot = self.adapter.close()
        self._opened = False
        return snapshot

    def snapshot(self) -> LocationTargetSnapshot:
        if not self._opened:
            raise LocationPublisherError(
                "location_publisher_closed", "location publisher is not open"
            )
        return self.adapter.snapshot()
