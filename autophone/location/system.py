"""Typed client for the vphone device-scoped system location protocol."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Callable, Mapping, Protocol

from autophone.device.location_transport import UDSSystemLocationTransport
from autophone.location.journey.coordinates import gcj02_to_wgs84
from autophone.location.contracts import (
    CoordinateSystem,
    LocationCoordinate,
    LocationSample,
)


class SystemLocationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


class SystemLocationTransport(Protocol):
    def request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


class CallableSystemLocationTransport:
    """In-process transport Adapter used by contract tests."""

    def __init__(self, fn: Callable[[Mapping[str, Any]], Mapping[str, Any]]) -> None:
        self._fn = fn

    def request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._fn(payload)


def _required(name: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _positive(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be > 0")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be > 0") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be > 0")
    return number


class SystemLocationClient:
    """Small Interface over the vphone system location source controller."""

    def __init__(self, transport: SystemLocationTransport | None = None) -> None:
        self.transport = transport or UDSSystemLocationTransport()

    def _request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = dict(self.transport.request(payload))
        except SystemLocationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SystemLocationError("location_guest_unavailable", str(exc)) from exc
        if not response.get("ok"):
            code = str(response.get("code") or "location_delivery_rejected")
            message = str(response.get("error") or code)
            raise SystemLocationError(code, message)
        return response

    @staticmethod
    def delivered_sample(sample: LocationSample | Mapping[str, Any]) -> LocationSample:
        value = sample if isinstance(sample, LocationSample) else LocationSample.from_mapping(sample)
        if value.coordinate.coordinate_system == CoordinateSystem.WGS84.value:
            return value
        latitude, longitude = gcj02_to_wgs84(value.lat, value.lng)
        return LocationSample(
            producer_sequence=value.producer_sequence,
            emitted_wall_at=value.emitted_wall_at,
            emitted_monotonic_s=value.emitted_monotonic_s,
            coordinate=LocationCoordinate(
                latitude=latitude,
                longitude=longitude,
                coordinate_system=CoordinateSystem.WGS84.value,
            ),
            horizontal_accuracy_m=value.horizontal_accuracy_m,
            vertical_accuracy_m=value.vertical_accuracy_m,
            altitude_m=value.altitude_m,
            speed_mps=value.speed_mps,
            course_deg=value.course_deg,
        )

    def set_fixed(
        self,
        *,
        owner: str,
        sample: LocationSample | Mapping[str, Any],
        heartbeat_s: float = 1.0,
        replace: bool = False,
        persist: bool = False,
    ) -> dict[str, Any]:
        delivered = self.delivered_sample(sample)
        return self._request({
            "t": "location_source_set",
            "mode": "fixed",
            "owner": _required("owner", owner),
            "coordinate_system": CoordinateSystem.WGS84.value,
            "heartbeat_s": _positive("heartbeat_s", heartbeat_s),
            "replace": bool(replace),
            "persist": bool(persist),
            **self._wire_fields(delivered),
        })

    def start_stream(
        self,
        *,
        owner: str,
        watchdog_s: float = 3.0,
        on_timeout: str = "hold",
        replace: bool = False,
    ) -> dict[str, Any]:
        if on_timeout not in {"hold", "stop"}:
            raise ValueError("on_timeout must be hold or stop")
        return self._request({
            "t": "location_stream_start",
            "owner": _required("owner", owner),
            "coordinate_system": CoordinateSystem.WGS84.value,
            "watchdog_s": _positive("watchdog_s", watchdog_s),
            "on_timeout": on_timeout,
            "replace": bool(replace),
        })

    def push(
        self,
        generation: str,
        sample: LocationSample | Mapping[str, Any],
    ) -> dict[str, Any]:
        delivered = self.delivered_sample(sample)
        return self._request({
            "t": "location_stream_push",
            "generation": _required("generation", generation),
            **self._wire_fields(delivered),
        })

    def control(self, generation: str, **command: Any) -> dict[str, Any]:
        if not command:
            raise ValueError("location control command is required")
        return self._request({
            "t": "location_source_control",
            "generation": _required("generation", generation),
            **command,
        })

    def status(self) -> dict[str, Any]:
        return self._request({"t": "location_source_status"})

    def stop(self, generation: str) -> dict[str, Any]:
        return self._request({
            "t": "location_source_stop",
            "generation": _required("generation", generation),
        })

    @staticmethod
    def _wire_fields(sample: LocationSample) -> dict[str, Any]:
        return {
            "producer_sequence": sample.producer_sequence,
            "lat": sample.lat,
            "lon": sample.lng,
            "alt": sample.altitude_m,
            "hacc": sample.horizontal_accuracy_m,
            "vacc": sample.vertical_accuracy_m,
            "speed": sample.speed_mps,
            "course": sample.course_deg,
            "timestamp": sample.emitted_wall_at,
        }
