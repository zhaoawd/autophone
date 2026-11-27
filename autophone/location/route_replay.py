"""Validated fixed-route fixtures and deterministic cumulative-distance replay.

The module is intentionally pure: it does not know about Frida, the VM, HTTP,
or any specific App.  A caller proposes the next sample and commits it only after the sample
was successfully delivered to the app.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


EARTH_RADIUS_M = 6_371_008.8
SCHEMA = "autophone.route-fixture/v1"
COORDINATE_SYSTEM = "GCJ-02"
TRAVEL_MODE = "driving"
BUILTIN_ROUTE_DIR = Path(__file__).resolve().parents[1] / "_data" / "routes"
_ROUTE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,127}$")


class RouteFixtureError(ValueError):
    """A route fixture is missing, malformed, or unsafe to replay."""


def validate_route_id(value: str) -> str:
    if not _ROUTE_ID_RE.fullmatch(value):
        raise RouteFixtureError("invalid route id")
    return value


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in meters."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(lng2 - lng1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2) ** 2
    )
    return EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_deg(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Initial bearing in [0, 360)."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_lam = math.radians(lng2 - lng1)
    y = math.sin(d_lam) * math.cos(phi2)
    x = (
        math.cos(phi1) * math.sin(phi2)
        - math.sin(phi1) * math.cos(phi2) * math.cos(d_lam)
    )
    return math.degrees(math.atan2(y, x)) % 360


def angular_difference_deg(start: float, end: float) -> float:
    """Smallest signed rotation from ``start`` to ``end``."""
    return (float(end) - float(start) + 180) % 360 - 180


@dataclass(frozen=True)
class RoutePoint:
    lat: float
    lng: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.lat) or not -90 <= self.lat <= 90:
            raise ValueError("route latitude out of range")
        if not math.isfinite(self.lng) or not -180 <= self.lng <= 180:
            raise ValueError("route longitude out of range")


@dataclass(frozen=True)
class RouteFixture:
    route_id: str
    origin: RoutePoint
    destination: RoutePoint
    destination_name: str
    points: tuple[RoutePoint, ...]
    cumulative_m: tuple[float, ...]
    total_m: float
    maneuvers: tuple[Mapping[str, Any], ...]
    source: Mapping[str, Any]

    def sample_at_distance(self, distance_m: float) -> RoutePoint:
        """Interpolate a point on the route, clamped to both endpoints."""
        distance = min(self.total_m, max(0.0, float(distance_m)))
        if distance <= 0:
            return self.points[0]
        if distance >= self.total_m:
            return self.points[-1]
        right = bisect_right(self.cumulative_m, distance)
        left = right - 1
        start_m = self.cumulative_m[left]
        end_m = self.cumulative_m[right]
        fraction = (distance - start_m) / (end_m - start_m)
        start = self.points[left]
        end = self.points[right]
        return RoutePoint(
            lat=start.lat + (end.lat - start.lat) * fraction,
            lng=start.lng + (end.lng - start.lng) * fraction,
        )

    def bearing_at_distance(self, distance_m: float, window_m: float = 5.0) -> float:
        before = self.sample_at_distance(float(distance_m) - window_m)
        after = self.sample_at_distance(float(distance_m) + window_m)
        if before == after:
            return 0.0
        return bearing_deg(before.lat, before.lng, after.lat, after.lng)


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise RouteFixtureError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RouteFixtureError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise RouteFixtureError(f"{label} must be a finite number")
    return number


def _point_from_mapping(value: Any, label: str) -> RoutePoint:
    if not isinstance(value, Mapping):
        raise RouteFixtureError(f"{label} must be an object")
    lat = _finite_number(value.get("lat"), f"{label}.lat")
    lng = _finite_number(value.get("lng"), f"{label}.lng")
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        raise RouteFixtureError(f"{label} is outside valid latitude/longitude bounds")
    return RoutePoint(lat=lat, lng=lng)


def _geometry_points(value: Any) -> list[RoutePoint]:
    if not isinstance(value, Mapping) or value.get("type") != "LineString":
        raise RouteFixtureError("geometry.type must be LineString")
    coordinates = value.get("coordinates")
    if not isinstance(coordinates, Sequence) or isinstance(coordinates, (str, bytes)):
        raise RouteFixtureError("geometry.coordinates must be an array")
    if len(coordinates) < 2:
        raise RouteFixtureError("geometry.coordinates requires at least two points")
    points: list[RoutePoint] = []
    for index, coordinate in enumerate(coordinates):
        if (
            not isinstance(coordinate, Sequence)
            or isinstance(coordinate, (str, bytes))
            or len(coordinate) != 2
        ):
            raise RouteFixtureError(
                f"geometry.coordinates[{index}] must be [lng, lat]"
            )
        lng = _finite_number(coordinate[0], f"geometry.coordinates[{index}].lng")
        lat = _finite_number(coordinate[1], f"geometry.coordinates[{index}].lat")
        if not -90 <= lat <= 90 or not -180 <= lng <= 180:
            raise RouteFixtureError(
                f"geometry.coordinates[{index}] is outside valid bounds"
            )
        points.append(RoutePoint(lat=lat, lng=lng))
    return points


def validate_route_fixture(raw: Mapping[str, Any]) -> RouteFixture:
    """Validate and canonicalize a route fixture."""
    if not isinstance(raw, Mapping):
        raise RouteFixtureError("route fixture root must be an object")
    if raw.get("schema") != SCHEMA:
        raise RouteFixtureError(f"schema must be {SCHEMA!r}")
    route_id = str(raw.get("route_id") or "")
    if not _ROUTE_ID_RE.fullmatch(route_id):
        raise RouteFixtureError("route_id must be lowercase letters, digits, and hyphens")
    if raw.get("coordinate_system") != COORDINATE_SYSTEM:
        raise RouteFixtureError(f"coordinate_system must be {COORDINATE_SYSTEM}")
    if raw.get("travel_mode") != TRAVEL_MODE:
        raise RouteFixtureError(f"travel_mode must be {TRAVEL_MODE}")

    origin = _point_from_mapping(raw.get("origin"), "origin")
    destination_value = raw.get("destination")
    destination = _point_from_mapping(destination_value, "destination")
    destination_name = str(
        destination_value.get("name") if isinstance(destination_value, Mapping) else ""
    ).strip()
    points_raw = _geometry_points(raw.get("geometry"))

    segment_distances = [
        haversine_m(left.lat, left.lng, right.lat, right.lng)
        for left, right in zip(points_raw, points_raw[1:])
    ]
    duplicate_count = sum(distance <= 0.05 for distance in segment_distances)
    if duplicate_count / len(segment_distances) > 0.20:
        raise RouteFixtureError("geometry has an abnormal duplicate point ratio")

    points: list[RoutePoint] = [points_raw[0]]
    cumulative = [0.0]
    for point in points_raw[1:]:
        distance = haversine_m(
            points[-1].lat, points[-1].lng, point.lat, point.lng
        )
        if distance <= 0.05:
            continue
        if distance > 250:
            raise RouteFixtureError(
                f"geometry jump {distance:.1f}m exceeds the 250m limit"
            )
        points.append(point)
        cumulative.append(cumulative[-1] + distance)
    if len(points) < 2 or cumulative[-1] <= 0:
        raise RouteFixtureError("geometry total route length must be greater than zero")
    if haversine_m(origin.lat, origin.lng, points[0].lat, points[0].lng) > 30:
        raise RouteFixtureError("geometry origin differs from declared origin by >30m")
    if (
        haversine_m(
            destination.lat,
            destination.lng,
            points[-1].lat,
            points[-1].lng,
        )
        > 30
    ):
        raise RouteFixtureError(
            "geometry destination differs from declared destination by >30m"
        )

    maneuvers_value = raw.get("maneuvers") or []
    if not isinstance(maneuvers_value, Sequence) or isinstance(
        maneuvers_value, (str, bytes)
    ):
        raise RouteFixtureError("maneuvers must be an array")
    source = raw.get("source") or {}
    if not isinstance(source, Mapping):
        raise RouteFixtureError("source must be an object")
    return RouteFixture(
        route_id=route_id,
        origin=origin,
        destination=destination,
        destination_name=destination_name,
        points=tuple(points),
        cumulative_m=tuple(cumulative),
        total_m=cumulative[-1],
        maneuvers=tuple(dict(item) for item in maneuvers_value),
        source=dict(source),
    )


def load_route_fixture(route_id_or_path: str | Path) -> RouteFixture:
    """Load a built-in route id or a trusted JSON Path object."""
    if isinstance(route_id_or_path, Path):
        path = route_id_or_path
    else:
        value = validate_route_id(route_id_or_path)
        path = BUILTIN_ROUTE_DIR / f"{value}.route.json"
        if path.is_symlink() or not path.is_file():
            raise RouteFixtureError(f"unknown route fixture: {value!r}")
    if not path.is_file():
        raise RouteFixtureError(f"route fixture not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RouteFixtureError(f"cannot read route fixture {path}: {exc}") from exc
    route = validate_route_fixture(raw)
    if (
        not isinstance(route_id_or_path, Path)
        and route.route_id != str(route_id_or_path)
    ):
        raise RouteFixtureError("built-in fixture route_id does not match its filename")
    return route


def builtin_routes_for_store(store_code: str) -> tuple[RouteFixture, ...]:
    """Return tracked routes bound to one exact provider store identifier."""
    normalized = str(store_code or "").strip()
    if not normalized:
        raise RouteFixtureError("store_code is required for route cache lookup")
    matches: list[RouteFixture] = []
    for route in list_builtin_routes():
        if str(route.source.get("store_code") or "").strip() == normalized:
            matches.append(route)
    return tuple(matches)


def list_builtin_routes() -> tuple[RouteFixture, ...]:
    """Load every tracked route; one invalid fixture fails the whole catalog."""
    routes: list[RouteFixture] = []
    for path in sorted(BUILTIN_ROUTE_DIR.glob("*.route.json")):
        try:
            route = load_route_fixture(path.name.removesuffix(".route.json"))
        except RouteFixtureError as exc:
            raise RouteFixtureError(
                f"tracked route cache contains invalid fixture {path.name}: {exc}"
            ) from exc
        routes.append(route)
    return tuple(routes)


def select_builtin_route_for_store(store_code: str) -> RouteFixture:
    """Select exactly one cached route; never guess by name or nearby coordinates."""
    matches = builtin_routes_for_store(store_code)
    if not matches:
        raise RouteFixtureError(
            f"no tracked route fixture for store_code {store_code!r}; "
            "run baidu_route_builder with --store-code and --install first"
        )
    if len(matches) > 1:
        ids = ", ".join(route.route_id for route in matches)
        raise RouteFixtureError(
            f"multiple tracked routes for store_code {store_code!r}: {ids}; "
            "pass --route explicitly"
        )
    return matches[0]


@dataclass(frozen=True)
class RouteSample:
    lat: float
    lng: float
    route_distance_m: float
    remaining_m: float
    total_m: float
    model_speed_mps: float
    emitted_speed_mps: float
    bearing_deg: float
    time_scale: float
    done: bool
    phase: str = "route"
    tail_distance_m: float = 0.0
    tail_total_m: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "lat": self.lat,
            "lng": self.lng,
            "route_distance_m": self.route_distance_m,
            "remaining_m": self.remaining_m,
            "total_m": self.total_m,
            "model_speed_mps": self.model_speed_mps,
            "emitted_speed_mps": self.emitted_speed_mps,
            "bearing_deg": self.bearing_deg,
            "time_scale": self.time_scale,
            "done": self.done,
            "phase": self.phase,
            "tail_distance_m": self.tail_distance_m,
            "tail_total_m": self.tail_total_m,
        }


@dataclass(frozen=True)
class RouteProposal:
    generation: int
    sample: RouteSample
    next_distance_m: float
    next_speed_mps: float
    next_tail_distance_m: float = 0.0


class RouteReplayEngine:
    """Deterministic urban-driving replay with transactional progress."""

    CRUISE_SPEED_MPS = 12.0
    MAX_ACCEL_MPS2 = 1.5
    COMFORT_BRAKE_MPS2 = 2.5
    MAX_SUBSTEP_S = 0.2

    def __init__(
        self,
        fixture: RouteFixture,
        *,
        profile: str = "urban_drive_v1",
        time_scale: float = 10.0,
        tail_to: RoutePoint | None = None,
    ) -> None:
        if profile != "urban_drive_v1":
            raise ValueError(f"unknown route replay profile: {profile!r}")
        self.fixture = fixture
        if tail_to is not None and not isinstance(tail_to, RoutePoint):
            raise ValueError("tail_to must be a RoutePoint")
        self.tail_to = tail_to
        end = fixture.points[-1]
        self.tail_total_m = (
            haversine_m(end.lat, end.lng, tail_to.lat, tail_to.lng)
            if tail_to is not None else 0.0
        )
        self._tail_distance_m = 0.0
        self.profile = profile
        self._time_scale = self._validate_time_scale(time_scale)
        self._route_distance_m = 0.0
        self._model_speed_mps = 0.0
        self._paused = False
        self._generation = 0

    @staticmethod
    def _validate_time_scale(value: float) -> float:
        try:
            scale = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("time_scale must be a number") from exc
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError("time_scale must be > 0")
        return scale

    @property
    def time_scale(self) -> float:
        return self._time_scale

    @property
    def paused(self) -> bool:
        return self._paused

    def _sample(
        self, distance_m: float, speed_mps: float, tail_distance_m: float = 0.0
    ) -> RouteSample:
        distance = min(self.fixture.total_m, max(0.0, float(distance_m)))
        remaining = max(0.0, self.fixture.total_m - distance)
        tail_distance = min(self.tail_total_m, max(0.0, tail_distance_m))
        done = remaining <= 1e-6 and self.tail_total_m - tail_distance <= 1e-6
        phase = "tail" if tail_distance > 0 else "route"
        speed = 0.0 if done else max(0.0, float(speed_mps))
        point = self.fixture.sample_at_distance(distance)
        course = self.fixture.bearing_at_distance(distance)
        if phase == "tail" and self.tail_to is not None:
            start = self.fixture.points[-1]
            fraction = tail_distance / self.tail_total_m
            point = RoutePoint(
                start.lat + (self.tail_to.lat - start.lat) * fraction,
                start.lng + (self.tail_to.lng - start.lng) * fraction,
            )
            course = bearing_deg(start.lat, start.lng, self.tail_to.lat, self.tail_to.lng)
        remaining += self.tail_total_m - tail_distance
        emitted = 0.0 if self._paused else speed * self._time_scale
        return RouteSample(
            lat=point.lat,
            lng=point.lng,
            route_distance_m=distance,
            remaining_m=remaining,
            total_m=self.fixture.total_m,
            model_speed_mps=speed,
            emitted_speed_mps=emitted,
            bearing_deg=course,
            time_scale=self._time_scale,
            done=done,
            phase=phase,
            tail_distance_m=tail_distance,
            tail_total_m=self.tail_total_m,
        )

    def current_sample(self) -> RouteSample:
        return self._sample(
            self._route_distance_m, self._model_speed_mps, self._tail_distance_m
        )

    def curve_speed_limit_mps(self, distance_m: float) -> float:
        """A deterministic local curvature cap derived from a 25m window."""
        distance = min(self.fixture.total_m, max(0.0, float(distance_m)))
        before = self.fixture.bearing_at_distance(max(0.0, distance - 25.0), 5.0)
        after = self.fixture.bearing_at_distance(
            min(self.fixture.total_m, distance + 25.0), 5.0
        )
        turn = abs(angular_difference_deg(before, after))
        if turn < 8:
            return self.CRUISE_SPEED_MPS
        fraction = min(1.0, turn / 120.0)
        return max(4.0, self.CRUISE_SPEED_MPS * (1.0 - 0.65 * fraction))

    def propose(self, elapsed_wall_s: float) -> RouteProposal:
        try:
            wall_s = float(elapsed_wall_s)
        except (TypeError, ValueError) as exc:
            raise ValueError("elapsed_wall_s must be a number") from exc
        if not math.isfinite(wall_s) or wall_s < 0:
            raise ValueError("elapsed_wall_s must be >= 0")
        distance = self._route_distance_m
        speed = self._model_speed_mps
        simulated_s = 0.0 if self._paused else wall_s * self._time_scale
        # Publish and ACK the fixture endpoint before starting the synthetic tail.
        if distance >= self.fixture.total_m:
            tail_distance = min(
                self.tail_total_m,
                self._tail_distance_m + self.CRUISE_SPEED_MPS * simulated_s,
            )
            speed = self.CRUISE_SPEED_MPS if tail_distance > self._tail_distance_m else speed
            sample = self._sample(distance, speed, tail_distance)
            return RouteProposal(
                self._generation, sample, distance, sample.model_speed_mps, tail_distance
            )
        while simulated_s > 1e-12 and distance < self.fixture.total_m:
            dt = min(self.MAX_SUBSTEP_S, simulated_s)
            remaining = max(0.0, self.fixture.total_m - distance)
            brake_cap = math.sqrt(2 * self.COMFORT_BRAKE_MPS2 * remaining)
            target = min(
                self.CRUISE_SPEED_MPS,
                self.curve_speed_limit_mps(distance),
                brake_cap,
            )
            if speed < target:
                next_speed = min(target, speed + self.MAX_ACCEL_MPS2 * dt)
            else:
                next_speed = max(target, speed - self.COMFORT_BRAKE_MPS2 * dt)
            step_m = (speed + next_speed) * 0.5 * dt
            if step_m >= remaining:
                distance = self.fixture.total_m
                speed = 0.0
                simulated_s = 0.0
                break
            distance += step_m
            speed = next_speed
            simulated_s -= dt
        sample = self._sample(distance, speed)
        return RouteProposal(
            generation=self._generation,
            sample=sample,
            next_distance_m=sample.route_distance_m,
            next_speed_mps=sample.model_speed_mps,
        )

    def propose_arrival(self) -> RouteProposal:
        sample = self._sample(self.fixture.total_m, 0.0, self.tail_total_m)
        return RouteProposal(
            generation=self._generation,
            sample=sample,
            next_distance_m=self.fixture.total_m,
            next_speed_mps=0.0,
            next_tail_distance_m=self.tail_total_m,
        )

    def commit(self, proposal: RouteProposal) -> RouteSample:
        if proposal.generation != self._generation:
            raise RuntimeError("stale route proposal")
        self._route_distance_m = proposal.next_distance_m
        self._model_speed_mps = proposal.next_speed_mps
        self._tail_distance_m = proposal.next_tail_distance_m
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
        scale = self._validate_time_scale(value)
        if scale != self._time_scale:
            self._time_scale = scale
            self._generation += 1

    def arrive_now(self) -> RouteSample:
        proposal = self.propose_arrival()
        return self.commit(proposal)

    def checkpoint(self) -> dict[str, Any]:
        """Return the minimal ACK-committed state needed for paused recovery."""
        return {
            "route_id": self.fixture.route_id,
            "profile": self.profile,
            "route_distance_m": self._route_distance_m,
            "tail_to": (
                {"lat": self.tail_to.lat, "lng": self.tail_to.lng}
                if self.tail_to is not None else None
            ),
            "tail_distance_m": self._tail_distance_m,
            "model_speed_mps": self._model_speed_mps,
            "time_scale": self._time_scale,
        }

    def restore_checkpoint(self, payload: Mapping[str, Any]) -> RouteSample:
        """Restore committed progress; callers decide when explicit resume is allowed."""
        if not isinstance(payload, Mapping):
            raise ValueError("route checkpoint must be an object")
        if str(payload.get("route_id") or "") != self.fixture.route_id:
            raise ValueError("route checkpoint route_id mismatch")
        if str(payload.get("profile") or "") != self.profile:
            raise ValueError("route checkpoint profile mismatch")
        distance = _finite_number(
            payload.get("route_distance_m"), "route_distance_m"
        )
        speed = _finite_number(payload.get("model_speed_mps"), "model_speed_mps")
        scale = self._validate_time_scale(payload.get("time_scale"))
        if not 0 <= distance <= self.fixture.total_m:
            raise ValueError("route_distance_m is outside route bounds")
        if speed < 0:
            raise ValueError("model_speed_mps must be >= 0")
        expected_tail = (
            {"lat": self.tail_to.lat, "lng": self.tail_to.lng}
            if self.tail_to is not None else None
        )
        if payload.get("tail_to") != expected_tail:
            raise ValueError("route checkpoint tail_to mismatch")
        tail_distance = _finite_number(payload.get("tail_distance_m", 0), "tail_distance_m")
        if not 0 <= tail_distance <= self.tail_total_m:
            raise ValueError("tail_distance_m is outside tail bounds")
        if tail_distance > 0 and distance != self.fixture.total_m:
            raise ValueError("tail progress requires completed route")
        self._route_distance_m = distance
        self._tail_distance_m = tail_distance
        self._model_speed_mps = speed
        self._time_scale = scale
        self._paused = True
        self._generation += 1
        return self.current_sample()
