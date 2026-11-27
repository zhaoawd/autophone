"""Stable, side-effect-free contracts for location samples delivered to app adapters."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class JourneyPhase(str, Enum):
    PRE_HANDOFF = "pre_handoff"
    NAVIGATING = "navigating"
    ARRIVED_HOLD = "arrived_hold"


def _finite(name: str, value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _coordinate(lat: float, lng: float) -> None:
    if not -90 <= _finite("lat", lat) <= 90:
        raise ValueError("lat out of range")
    if not -180 <= _finite("lng", lng) <= 180:
        raise ValueError("lng out of range")


def _required(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class LocationSample:
    seq: int
    emitted_wall_at: str
    emitted_monotonic_s: float
    lat: float
    lng: float
    speed_mps: float
    course_deg: float
    phase: JourneyPhase
    datum: str = "gcj02"
    horizontal_accuracy_m: float = 5.0

    def __post_init__(self) -> None:
        if not isinstance(self.seq, int) or self.seq < 0:
            raise ValueError("seq must be a non-negative integer")
        _required("emitted_wall_at", self.emitted_wall_at)
        if _finite("emitted_monotonic_s", self.emitted_monotonic_s) < 0:
            raise ValueError("emitted_monotonic_s must be >= 0")
        _coordinate(self.lat, self.lng)
        if self.datum != "gcj02":
            raise ValueError("authoritative LocationSample datum must be gcj02")
        if _finite("speed_mps", self.speed_mps) < 0:
            raise ValueError("speed_mps must be >= 0")
        course = _finite("course_deg", self.course_deg)
        if not 0 <= course < 360:
            raise ValueError("course_deg must be in [0, 360)")
        if _finite("horizontal_accuracy_m", self.horizontal_accuracy_m) <= 0:
            raise ValueError("horizontal_accuracy_m must be > 0")
        if not isinstance(self.phase, JourneyPhase):
            try:
                object.__setattr__(self, "phase", JourneyPhase(self.phase))
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid journey phase") from exc

    def as_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class DeliveredLocation:
    lat: float
    lng: float
    datum: str
    speed_mps: float
    course_deg: float
    timestamp: str
    altitude_m: float = 0.0
    horizontal_accuracy_m: float = 5.0
    vertical_accuracy_m: float = 5.0

    def __post_init__(self) -> None:
        _coordinate(self.lat, self.lng)
        if self.datum not in {"gcj02", "wgs84"}:
            raise ValueError("delivered datum must be gcj02 or wgs84")
        if _finite("speed_mps", self.speed_mps) < 0:
            raise ValueError("speed_mps must be >= 0")
        if not 0 <= _finite("course_deg", self.course_deg) < 360:
            raise ValueError("course_deg must be in [0, 360)")
        _required("timestamp", self.timestamp)
        _finite("altitude_m", self.altitude_m)
        if _finite("horizontal_accuracy_m", self.horizontal_accuracy_m) <= 0:
            raise ValueError("horizontal_accuracy_m must be > 0")
        if _finite("vertical_accuracy_m", self.vertical_accuracy_m) <= 0:
            raise ValueError("vertical_accuracy_m must be > 0")

    def as_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class AdapterAck:
    adapter: str
    sample_seq: int
    accepted: bool
    consumed_monotonic_s: float
    latency_ms: float
    delivered: DeliveredLocation | None = None
    evidence_refs: tuple[str, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        _required("adapter", self.adapter)
        if not isinstance(self.sample_seq, int) or self.sample_seq < 0:
            raise ValueError("sample_seq must be a non-negative integer")
        if _finite("consumed_monotonic_s", self.consumed_monotonic_s) < 0:
            raise ValueError("consumed_monotonic_s must be >= 0")
        if _finite("latency_ms", self.latency_ms) < 0:
            raise ValueError("latency_ms must be >= 0")
        if self.accepted and self.delivered is None:
            raise ValueError("accepted AdapterAck requires delivered location")
        object.__setattr__(
            self, "evidence_refs",
            tuple(str(value) for value in self.evidence_refs if str(value)),
        )

    def as_dict(self) -> dict[str, Any]:
        payload = _jsonable(asdict(self))
        payload["delivered"] = self.delivered.as_dict() if self.delivered else None
        return payload
