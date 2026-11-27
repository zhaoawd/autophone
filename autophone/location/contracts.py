"""Shared, side-effect-free contracts for the platform location system."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import math
from typing import Any, Mapping


LOCATION_SYSTEM_SCHEMA_VERSION = "autophone.location-system/v1"


class CoordinateSystem(str, Enum):
    GCJ02 = "gcj02"
    WGS84 = "wgs84"


class LocationSourceKind(str, Enum):
    FIXED = "fixed"
    APPROACH = "approach"
    ROUTE = "route"
    EXTERNAL_STREAM = "external_stream"
    HOST = "host"


class LocationTargetKind(str, Enum):
    SYSTEM_CORE_LOCATION = "system_core_location"
    APP_CORE_LOCATION_SPAWN = "app_core_location_spawn"


class LocationPersistence(str, Enum):
    EPHEMERAL = "ephemeral"
    DURABLE = "durable"


class LocationSessionState(str, Enum):
    STARTING = "starting"
    WAITING_TARGET = "waiting_target"
    RUNNING = "running"
    PAUSED = "paused"
    HOLDING = "holding"
    BACKOFF = "backoff"
    ERROR_PAUSED = "error_paused"
    STOPPING = "stopping"
    STOPPED = "stopped"


class LocationControlCommand(str, Enum):
    PAUSE = "pause"
    RESUME = "resume"
    SET_TIME_SCALE = "set_time_scale"
    ARRIVE_NOW = "arrive_now"
    HOLD = "hold"


class LocationTargetState(str, Enum):
    CLOSED = "closed"
    WAITING_SAMPLE = "waiting_sample"
    WAITING_APP = "waiting_app"
    OPEN = "open"
    ATTACHED_AWAITING_DEMAND = "attached_awaiting_demand"
    APPLIED = "applied"
    EFFECTIVE = "effective"
    QUIESCED = "quiesced"
    BACKOFF = "backoff"
    ERROR = "error"
    STOPPED = "stopped"


def _required(name: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _non_negative_int(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if number < 0 or str(value).strip() not in {str(number), f"{number}.0"}:
        raise ValueError(f"{name} must be a non-negative integer")
    return number


def _enum_value(name: str, value: Any, enum_type: type[Enum]) -> str:
    raw = value.value if isinstance(value, enum_type) else str(value or "")
    try:
        return str(enum_type(raw).value)
    except ValueError as exc:
        allowed = ", ".join(str(item.value) for item in enum_type)
        raise ValueError(f"{name} must be one of: {allowed}") from exc


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


@dataclass(frozen=True)
class LocationCoordinate:
    latitude: float
    longitude: float
    coordinate_system: str = CoordinateSystem.GCJ02.value

    def __post_init__(self) -> None:
        latitude = _finite("latitude", self.latitude)
        longitude = _finite("longitude", self.longitude)
        if not -90 <= latitude <= 90:
            raise ValueError("latitude must be in [-90, 90]")
        if not -180 <= longitude <= 180:
            raise ValueError("longitude must be in [-180, 180]")
        object.__setattr__(self, "latitude", latitude)
        object.__setattr__(self, "longitude", longitude)
        object.__setattr__(
            self,
            "coordinate_system",
            _enum_value("coordinate_system", self.coordinate_system, CoordinateSystem),
        )

    @property
    def lat(self) -> float:
        return self.latitude

    @property
    def lng(self) -> float:
        return self.longitude

    def as_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "LocationCoordinate":
        return cls(
            latitude=payload.get("latitude", payload.get("lat")),
            longitude=payload.get("longitude", payload.get("lng", payload.get("lon"))),
            coordinate_system=payload.get(
                "coordinate_system", payload.get("datum", CoordinateSystem.GCJ02.value)
            ),
        )


@dataclass(frozen=True)
class LocationSample:
    """One authoritative producer sample.

    ``-1`` is the only unknown sentinel accepted for speed and course. Route,
    Journey, or business phase fields belong in their own progress envelope.
    """

    producer_sequence: int
    emitted_wall_at: str
    emitted_monotonic_s: float
    coordinate: LocationCoordinate
    horizontal_accuracy_m: float = 5.0
    vertical_accuracy_m: float = 5.0
    altitude_m: float = 0.0
    speed_mps: float = -1.0
    course_deg: float = -1.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "producer_sequence",
            _non_negative_int("producer_sequence", self.producer_sequence),
        )
        object.__setattr__(self, "emitted_wall_at", _required(
            "emitted_wall_at", self.emitted_wall_at
        ))
        monotonic = _finite("emitted_monotonic_s", self.emitted_monotonic_s)
        if monotonic < 0:
            raise ValueError("emitted_monotonic_s must be >= 0")
        object.__setattr__(self, "emitted_monotonic_s", monotonic)
        if not isinstance(self.coordinate, LocationCoordinate):
            if not isinstance(self.coordinate, Mapping):
                raise ValueError("coordinate must be a LocationCoordinate")
            object.__setattr__(
                self, "coordinate", LocationCoordinate.from_mapping(self.coordinate)
            )
        horizontal = _finite("horizontal_accuracy_m", self.horizontal_accuracy_m)
        vertical = _finite("vertical_accuracy_m", self.vertical_accuracy_m)
        if horizontal <= 0:
            raise ValueError("horizontal_accuracy_m must be > 0")
        if vertical <= 0:
            raise ValueError("vertical_accuracy_m must be > 0")
        object.__setattr__(self, "horizontal_accuracy_m", horizontal)
        object.__setattr__(self, "vertical_accuracy_m", vertical)
        object.__setattr__(self, "altitude_m", _finite("altitude_m", self.altitude_m))
        speed = _finite("speed_mps", self.speed_mps)
        if speed != -1 and speed < 0:
            raise ValueError("speed_mps must be -1 or >= 0")
        course = _finite("course_deg", self.course_deg)
        if course != -1 and not 0 <= course < 360:
            raise ValueError("course_deg must be -1 or in [0, 360)")
        object.__setattr__(self, "speed_mps", speed)
        object.__setattr__(self, "course_deg", course)

    @property
    def seq(self) -> int:
        return self.producer_sequence

    @property
    def lat(self) -> float:
        return self.coordinate.latitude

    @property
    def lng(self) -> float:
        return self.coordinate.longitude

    @property
    def datum(self) -> str:
        return self.coordinate.coordinate_system

    def as_dict(self) -> dict[str, Any]:
        payload = _jsonable(asdict(self))
        payload["coordinate"] = self.coordinate.as_dict()
        return payload

    def transport_dict(self) -> dict[str, Any]:
        return {
            "producer_sequence": self.producer_sequence,
            "timestamp": self.emitted_wall_at,
            "emitted_monotonic_s": self.emitted_monotonic_s,
            "latitude": self.coordinate.latitude,
            "longitude": self.coordinate.longitude,
            "coordinate_system": self.coordinate.coordinate_system,
            "horizontal_accuracy_m": self.horizontal_accuracy_m,
            "vertical_accuracy_m": self.vertical_accuracy_m,
            "altitude_m": self.altitude_m,
            "speed_mps": self.speed_mps,
            "course_deg": self.course_deg,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "LocationSample":
        nested = payload.get("coordinate")
        coordinate_payload = nested if isinstance(nested, Mapping) else payload
        raw_sequence = payload.get("producer_sequence", payload.get("seq", 0))
        return cls(
            producer_sequence=_non_negative_int("producer_sequence", raw_sequence),
            emitted_wall_at=payload.get("emitted_wall_at", payload.get("timestamp")),
            emitted_monotonic_s=payload.get("emitted_monotonic_s", 0.0),
            coordinate=LocationCoordinate.from_mapping(coordinate_payload),
            horizontal_accuracy_m=payload.get(
                "horizontal_accuracy_m", payload.get("horizontal_accuracy", 5.0)
            ),
            vertical_accuracy_m=payload.get(
                "vertical_accuracy_m", payload.get("vertical_accuracy", 5.0)
            ),
            altitude_m=payload.get("altitude_m", payload.get("altitude", 0.0)),
            speed_mps=payload.get("speed_mps", payload.get("speed", -1.0)),
            course_deg=payload.get("course_deg", payload.get("course", -1.0)),
        )


@dataclass(frozen=True)
class DeliveredLocation:
    coordinate: LocationCoordinate
    timestamp: str
    horizontal_accuracy_m: float = 5.0
    vertical_accuracy_m: float = 5.0
    altitude_m: float = 0.0
    speed_mps: float = -1.0
    course_deg: float = -1.0

    def __post_init__(self) -> None:
        if not isinstance(self.coordinate, LocationCoordinate):
            if not isinstance(self.coordinate, Mapping):
                raise ValueError("coordinate must be a LocationCoordinate")
            object.__setattr__(
                self, "coordinate", LocationCoordinate.from_mapping(self.coordinate)
            )
        object.__setattr__(self, "timestamp", _required("timestamp", self.timestamp))
        horizontal = _finite("horizontal_accuracy_m", self.horizontal_accuracy_m)
        vertical = _finite("vertical_accuracy_m", self.vertical_accuracy_m)
        if horizontal <= 0:
            raise ValueError("horizontal_accuracy_m must be > 0")
        if vertical <= 0:
            raise ValueError("vertical_accuracy_m must be > 0")
        object.__setattr__(self, "horizontal_accuracy_m", horizontal)
        object.__setattr__(self, "vertical_accuracy_m", vertical)
        object.__setattr__(self, "altitude_m", _finite("altitude_m", self.altitude_m))
        speed = _finite("speed_mps", self.speed_mps)
        if speed != -1 and speed < 0:
            raise ValueError("speed_mps must be -1 or >= 0")
        course = _finite("course_deg", self.course_deg)
        if course != -1 and not 0 <= course < 360:
            raise ValueError("course_deg must be -1 or in [0, 360)")
        object.__setattr__(self, "speed_mps", speed)
        object.__setattr__(self, "course_deg", course)

    def as_dict(self) -> dict[str, Any]:
        payload = _jsonable(asdict(self))
        payload["coordinate"] = self.coordinate.as_dict()
        return payload


@dataclass(frozen=True)
class LocationSourceSpec:
    source_id: str
    kind: str
    coordinate_system: str
    persistence: str = LocationPersistence.EPHEMERAL.value
    route_id: str | None = None
    origin: LocationCoordinate | None = None
    destination: LocationCoordinate | None = None
    speed_mps: float | None = None
    min_distance_m: float = 0.0
    time_scale: float = 1.0
    watchdog_s: float | None = None
    on_timeout: str = "hold"
    tail_to: LocationCoordinate | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _required("source_id", self.source_id))
        kind = _enum_value("source kind", self.kind, LocationSourceKind)
        coordinate_system = _enum_value(
            "source coordinate_system", self.coordinate_system, CoordinateSystem
        )
        persistence = _enum_value("persistence", self.persistence, LocationPersistence)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "coordinate_system", coordinate_system)
        object.__setattr__(self, "persistence", persistence)
        scale = _finite("time_scale", self.time_scale)
        if scale <= 0:
            raise ValueError("time_scale must be > 0")
        object.__setattr__(self, "time_scale", scale)
        if kind == LocationSourceKind.ROUTE.value:
            from .route_replay import validate_route_id

            object.__setattr__(self, "route_id", validate_route_id(_required("route_id", self.route_id)))
        elif self.route_id is not None:
            raise ValueError("route_id is only valid for route sources")
        if self.tail_to is not None and kind != LocationSourceKind.ROUTE.value:
            raise ValueError("tail_to is only valid for route sources")
        if kind == LocationSourceKind.ROUTE.value and coordinate_system != "gcj02":
            raise ValueError("route source coordinate_system must be gcj02")
        for name in ("origin", "destination", "tail_to"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, LocationCoordinate):
                if not isinstance(value, Mapping):
                    raise ValueError(f"{name} must be a LocationCoordinate")
                value = LocationCoordinate.from_mapping(value)
                object.__setattr__(self, name, value)
            if value is not None and value.coordinate_system != coordinate_system:
                raise ValueError(
                    f"{name} coordinate_system must match source coordinate_system"
                )
        if kind == LocationSourceKind.APPROACH.value:
            if self.origin is None or self.destination is None:
                raise ValueError("approach source requires origin and destination")
            speed = _finite("speed_mps", self.speed_mps)
            if speed <= 0:
                raise ValueError("speed_mps must be > 0")
            object.__setattr__(self, "speed_mps", speed)
            minimum = _finite("min_distance_m", self.min_distance_m)
            if minimum < 0:
                raise ValueError("min_distance_m must be >= 0")
            object.__setattr__(self, "min_distance_m", minimum)
        elif any(value is not None for value in (
            self.origin, self.destination, self.speed_mps
        )) or self.min_distance_m != 0:
            raise ValueError(
                "origin, destination, speed_mps, and min_distance_m are only valid for approach sources"
            )
        if persistence == LocationPersistence.DURABLE.value and kind != LocationSourceKind.FIXED.value:
            raise ValueError("only fixed sources may be durable")
        if kind == LocationSourceKind.HOST.value and coordinate_system != CoordinateSystem.WGS84.value:
            raise ValueError("host source coordinate_system must be wgs84")
        if self.watchdog_s is not None:
            watchdog = _finite("watchdog_s", self.watchdog_s)
            if watchdog <= 0:
                raise ValueError("watchdog_s must be > 0")
            object.__setattr__(self, "watchdog_s", watchdog)
        if self.on_timeout not in {"hold", "stop"}:
            raise ValueError("on_timeout must be hold or stop")

    def as_dict(self) -> dict[str, Any]:
        payload = _jsonable(asdict(self))
        # Preserve the exact spec identity stored by pre-tail checkpoints.
        if self.tail_to is None:
            payload.pop("tail_to")
        return payload

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "LocationSourceSpec":
        if not isinstance(payload, Mapping):
            raise ValueError("source must be an object")
        return cls(
            source_id=payload.get("source_id"),
            kind=payload.get("kind"),
            coordinate_system=payload.get("coordinate_system"),
            persistence=payload.get(
                "persistence", LocationPersistence.EPHEMERAL.value
            ),
            route_id=payload.get("route_id"),
            tail_to=payload.get("tail_to"),
            origin=payload.get("origin"),
            destination=payload.get("destination"),
            speed_mps=payload.get("speed_mps"),
            min_distance_m=payload.get("min_distance_m", 0.0),
            time_scale=payload.get("time_scale", 1.0),
            watchdog_s=payload.get("watchdog_s"),
            on_timeout=payload.get("on_timeout", "hold"),
        )


@dataclass(frozen=True)
class LocationTargetSpec:
    target_id: str
    kind: str
    device: str
    coordinate_system: str
    app: str | None = None
    bundle_id: str | None = None
    required: bool = True
    options: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_id", _required("target_id", self.target_id))
        object.__setattr__(self, "device", _required("device", self.device))
        kind = _enum_value("target kind", self.kind, LocationTargetKind)
        coordinate_system = _enum_value(
            "target coordinate_system", self.coordinate_system, CoordinateSystem
        )
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "coordinate_system", coordinate_system)
        if not isinstance(self.required, bool):
            raise ValueError("required must be boolean")
        if kind == LocationTargetKind.SYSTEM_CORE_LOCATION.value:
            if self.app is not None or self.bundle_id is not None:
                raise ValueError("system target cannot declare app or bundle_id")
            if coordinate_system != CoordinateSystem.WGS84.value:
                raise ValueError("system target coordinate_system must be wgs84")
        else:
            object.__setattr__(self, "app", _required("app", self.app))
            object.__setattr__(self, "bundle_id", _required("bundle_id", self.bundle_id))
        options = dict(self.options or {})
        if options:
            raise ValueError("target options are not supported by any target kind")
        object.__setattr__(self, "options", options)

    def as_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "LocationTargetSpec":
        if not isinstance(payload, Mapping):
            raise ValueError("target must be an object")
        return cls(
            target_id=payload.get("target_id"),
            kind=payload.get("kind"),
            device=payload.get("device"),
            coordinate_system=payload.get("coordinate_system"),
            app=payload.get("app"),
            bundle_id=payload.get("bundle_id"),
            required=payload.get("required", True),
            options=payload.get("options"),
        )


@dataclass(frozen=True)
class LocationSessionSpec:
    session_id: str
    owner: str
    device: str
    source: LocationSourceSpec
    targets: tuple[LocationTargetSpec, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", _required("session_id", self.session_id))
        object.__setattr__(self, "owner", _required("owner", self.owner))
        object.__setattr__(self, "device", _required("device", self.device))
        if not isinstance(self.source, LocationSourceSpec):
            raise ValueError("source must be a LocationSourceSpec")
        targets = tuple(self.targets)
        if not targets:
            raise ValueError("targets must not be empty")
        if any(not isinstance(target, LocationTargetSpec) for target in targets):
            raise ValueError("targets must contain LocationTargetSpec values")
        if any(target.device != self.device for target in targets):
            raise ValueError("all targets must use the session device")
        target_ids = [target.target_id for target in targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("target_id values must be unique")
        system_targets = [
            target for target in targets
            if target.kind == LocationTargetKind.SYSTEM_CORE_LOCATION.value
        ]
        if len(system_targets) > 1:
            raise ValueError("a session may contain at most one system target")
        object.__setattr__(self, "targets", targets)

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "owner": self.owner,
            "device": self.device,
            "source": self.source.as_dict(),
            "targets": [target.as_dict() for target in self.targets],
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "LocationSessionSpec":
        if not isinstance(payload, Mapping):
            raise ValueError("session spec must be an object")
        device = payload.get("device")
        source = payload.get("source")
        raw_targets = payload.get("targets")
        if not isinstance(source, Mapping):
            raise ValueError("source must be an object")
        if (
            not isinstance(raw_targets, (list, tuple))
            or isinstance(raw_targets, (str, bytes))
        ):
            raise ValueError("targets must be an array")
        targets: list[LocationTargetSpec] = []
        for raw_target in raw_targets:
            if not isinstance(raw_target, Mapping):
                raise ValueError("each target must be an object")
            normalized = dict(raw_target)
            normalized.setdefault("device", device)
            targets.append(LocationTargetSpec.from_mapping(normalized))
        return cls(
            session_id=payload.get("session_id"),
            owner=payload.get("owner"),
            device=device,
            source=LocationSourceSpec.from_mapping(source),
            targets=tuple(targets),
        )


@dataclass(frozen=True)
class DeliveryReceipt:
    session_id: str
    target_id: str
    target_kind: str
    producer_sequence: int
    accepted: bool
    applied_monotonic_s: float
    latency_ms: float
    authority_coordinate: LocationCoordinate
    delivery_sequence: int | None = None
    delivered: DeliveredLocation | None = None
    evidence_refs: tuple[str, ...] = ()
    error_code: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", _required("session_id", self.session_id))
        object.__setattr__(self, "target_id", _required("target_id", self.target_id))
        object.__setattr__(
            self,
            "target_kind",
            _enum_value("target_kind", self.target_kind, LocationTargetKind),
        )
        object.__setattr__(
            self,
            "producer_sequence",
            _non_negative_int("producer_sequence", self.producer_sequence),
        )
        if self.delivery_sequence is not None:
            object.__setattr__(
                self,
                "delivery_sequence",
                _non_negative_int("delivery_sequence", self.delivery_sequence),
            )
        if not isinstance(self.accepted, bool):
            raise ValueError("accepted must be boolean")
        applied = _finite("applied_monotonic_s", self.applied_monotonic_s)
        latency = _finite("latency_ms", self.latency_ms)
        if applied < 0:
            raise ValueError("applied_monotonic_s must be >= 0")
        if latency < 0:
            raise ValueError("latency_ms must be >= 0")
        object.__setattr__(self, "applied_monotonic_s", applied)
        object.__setattr__(self, "latency_ms", latency)
        if not isinstance(self.authority_coordinate, LocationCoordinate):
            raise ValueError("authority_coordinate must be a LocationCoordinate")
        if self.delivered is not None and not isinstance(self.delivered, DeliveredLocation):
            raise ValueError("delivered must be a DeliveredLocation")
        if self.accepted and self.delivered is None:
            raise ValueError("accepted receipt requires delivered location")
        if not self.accepted and not (self.error_code or self.error):
            raise ValueError("rejected receipt requires an error")
        if isinstance(self.evidence_refs, (str, bytes)):
            raise ValueError("evidence_refs must be a sequence of references")
        object.__setattr__(
            self,
            "evidence_refs",
            tuple(str(value) for value in self.evidence_refs if str(value)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "target_id": self.target_id,
            "target_kind": self.target_kind,
            "producer_sequence": self.producer_sequence,
            "delivery_sequence": self.delivery_sequence,
            "accepted": self.accepted,
            "applied_monotonic_s": self.applied_monotonic_s,
            "latency_ms": self.latency_ms,
            "authority_coordinate": self.authority_coordinate.as_dict(),
            "delivered": self.delivered.as_dict() if self.delivered else None,
            "evidence_refs": list(self.evidence_refs),
            "error_code": self.error_code,
            "error": self.error,
        }


@dataclass(frozen=True)
class LocationTargetSnapshot:
    target: LocationTargetSpec
    state: str
    generation: str | None = None
    process_epoch: str | None = None
    last_producer_sequence: int | None = None
    last_delivery_sequence: int | None = None
    last_receipt: DeliveryReceipt | None = None
    error_code: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target, LocationTargetSpec):
            raise ValueError("target must be a LocationTargetSpec")
        object.__setattr__(
            self, "state", _enum_value("target state", self.state, LocationTargetState)
        )
        if self.last_producer_sequence is not None:
            object.__setattr__(
                self,
                "last_producer_sequence",
                _non_negative_int("last_producer_sequence", self.last_producer_sequence),
            )
        if self.last_delivery_sequence is not None:
            object.__setattr__(
                self,
                "last_delivery_sequence",
                _non_negative_int("last_delivery_sequence", self.last_delivery_sequence),
            )
        if self.last_receipt is not None:
            if not isinstance(self.last_receipt, DeliveryReceipt):
                raise ValueError("last_receipt must be a DeliveryReceipt")
            if self.last_receipt.target_id != self.target.target_id:
                raise ValueError("last_receipt target_id must match target")
            if self.last_receipt.target_kind != self.target.kind:
                raise ValueError("last_receipt target_kind must match target")

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target.as_dict(),
            "state": self.state,
            "generation": self.generation,
            "process_epoch": self.process_epoch,
            "last_producer_sequence": self.last_producer_sequence,
            "last_delivery_sequence": self.last_delivery_sequence,
            "last_receipt": self.last_receipt.as_dict() if self.last_receipt else None,
            "error_code": self.error_code,
            "error": self.error,
        }


@dataclass(frozen=True)
class LocationSourceRuntimeSnapshot:
    state: str
    source_kind: str | None = None
    route_id: str | None = None
    time_scale: float = 1.0
    route_distance_m: float | None = None
    traveled_m: float | None = None
    phase: str | None = None
    tail_distance_m: float | None = None
    tail_total_m: float | None = None
    remaining_m: float | None = None
    total_m: float | None = None
    current_coordinate: LocationCoordinate | None = None
    model_speed_mps: float | None = None
    emitted_speed_mps: float | None = None
    bearing_deg: float | None = None
    done: bool | None = None
    last_producer_sequence: int | None = None
    error_code: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "state", _enum_value("source state", self.state, LocationSessionState)
        )
        if self.route_id is not None:
            object.__setattr__(self, "route_id", _required("route_id", self.route_id))
        if self.source_kind is not None:
            object.__setattr__(
                self,
                "source_kind",
                _enum_value("source kind", self.source_kind, LocationSourceKind),
            )
        scale = _finite("time_scale", self.time_scale)
        if scale <= 0:
            raise ValueError("time_scale must be > 0")
        object.__setattr__(self, "time_scale", scale)
        if self.phase is not None and self.phase not in {"route", "tail", "hold"}:
            raise ValueError("phase must be route, tail, or hold")
        for name in ("route_distance_m", "traveled_m", "remaining_m", "total_m",
                     "tail_distance_m", "tail_total_m"):
            value = getattr(self, name)
            if value is None:
                continue
            number = _finite(name, value)
            if number < 0:
                raise ValueError(f"{name} must be >= 0")
            object.__setattr__(self, name, number)
        if self.current_coordinate is not None:
            coordinate = self.current_coordinate
            if not isinstance(coordinate, LocationCoordinate):
                if not isinstance(coordinate, Mapping):
                    raise ValueError(
                        "current_coordinate must be a LocationCoordinate"
                    )
                coordinate = LocationCoordinate.from_mapping(coordinate)
            object.__setattr__(self, "current_coordinate", coordinate)
        for name in ("model_speed_mps", "emitted_speed_mps"):
            value = getattr(self, name)
            if value is None:
                continue
            number = _finite(name, value)
            if number < 0:
                raise ValueError(f"{name} must be >= 0")
            object.__setattr__(self, name, number)
        if self.bearing_deg is not None:
            bearing = _finite("bearing_deg", self.bearing_deg)
            if not 0 <= bearing < 360:
                raise ValueError("bearing_deg must be in [0, 360)")
            object.__setattr__(self, "bearing_deg", bearing)
        if self.done is not None and not isinstance(self.done, bool):
            raise ValueError("done must be boolean")
        if self.last_producer_sequence is not None:
            object.__setattr__(
                self,
                "last_producer_sequence",
                _non_negative_int(
                    "last_producer_sequence", self.last_producer_sequence
                ),
            )

    def as_dict(self) -> dict[str, Any]:
        payload = _jsonable(asdict(self))
        if self.current_coordinate is not None:
            payload["current_coordinate"] = self.current_coordinate.as_dict()
        return payload


@dataclass(frozen=True)
class LocationSessionSnapshot:
    spec: LocationSessionSpec
    state: str
    targets: tuple[LocationTargetSnapshot, ...]
    source_runtime: LocationSourceRuntimeSnapshot | None = None
    paused: bool = False
    last_producer_sequence: int | None = None
    error_code: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.spec, LocationSessionSpec):
            raise ValueError("spec must be a LocationSessionSpec")
        object.__setattr__(
            self, "state", _enum_value("session state", self.state, LocationSessionState)
        )
        snapshots = tuple(self.targets)
        if any(not isinstance(snapshot, LocationTargetSnapshot) for snapshot in snapshots):
            raise ValueError("targets must contain LocationTargetSnapshot values")
        declared = {target.target_id: target for target in self.spec.targets}
        actual_ids = [snapshot.target.target_id for snapshot in snapshots]
        if len(actual_ids) != len(set(actual_ids)):
            raise ValueError("snapshot target_id values must be unique")
        if any(
            declared.get(snapshot.target.target_id) != snapshot.target
            for snapshot in snapshots
        ):
            raise ValueError("snapshot targets must match targets declared by the session")
        object.__setattr__(self, "targets", snapshots)
        if self.source_runtime is not None and not isinstance(
            self.source_runtime, LocationSourceRuntimeSnapshot
        ):
            raise ValueError(
                "source_runtime must be a LocationSourceRuntimeSnapshot"
            )
        if not isinstance(self.paused, bool):
            raise ValueError("paused must be boolean")
        if self.last_producer_sequence is not None:
            object.__setattr__(
                self,
                "last_producer_sequence",
                _non_negative_int("last_producer_sequence", self.last_producer_sequence),
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec.as_dict(),
            "state": self.state,
            "targets": [target.as_dict() for target in self.targets],
            "source_runtime": (
                self.source_runtime.as_dict() if self.source_runtime else None
            ),
            "paused": self.paused,
            "last_producer_sequence": self.last_producer_sequence,
            "error_code": self.error_code,
            "error": self.error,
        }


@dataclass(frozen=True)
class LocationCapabilitySnapshot:
    target_kind: str
    installed: bool
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_kind",
            _enum_value("target_kind", self.target_kind, LocationTargetKind),
        )
        if not isinstance(self.installed, bool):
            raise ValueError("installed must be boolean")

    def as_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class LocationSystemSnapshot:
    capabilities: tuple[LocationCapabilitySnapshot, ...]
    sessions: tuple[LocationSessionSnapshot, ...] = ()
    schema_version: str = LOCATION_SYSTEM_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != LOCATION_SYSTEM_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {LOCATION_SYSTEM_SCHEMA_VERSION}")
        capabilities = tuple(self.capabilities)
        sessions = tuple(self.sessions)
        if any(
            not isinstance(capability, LocationCapabilitySnapshot)
            for capability in capabilities
        ):
            raise ValueError("capabilities must contain LocationCapabilitySnapshot values")
        if any(not isinstance(session, LocationSessionSnapshot) for session in sessions):
            raise ValueError("sessions must contain LocationSessionSnapshot values")
        kinds = [capability.target_kind for capability in capabilities]
        if len(kinds) != len(set(kinds)):
            raise ValueError("capability target_kind values must be unique")
        session_ids = [session.spec.session_id for session in sessions]
        if len(session_ids) != len(set(session_ids)):
            raise ValueError("session_id values must be unique")
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "sessions", sessions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "capabilities": [capability.as_dict() for capability in self.capabilities],
            "sessions": [session.as_dict() for session in self.sessions],
        }
