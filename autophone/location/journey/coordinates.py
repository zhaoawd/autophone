"""Deterministic WGS-84/GCJ-02 conversions used only at adapter boundaries."""
from __future__ import annotations

import math

from autophone.location.route_replay import haversine_m

_A = 6378245.0
_EE = 0.00669342162296594323
_X_PI = math.pi * 3000.0 / 180.0


def _outside_china(lat: float, lng: float) -> bool:
    return lng < 72.004 or lng > 137.8347 or lat < 0.8293 or lat > 55.8271


def _transform_lat(x: float, y: float) -> float:
    value = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    value += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    value += (20 * math.sin(y * math.pi) + 40 * math.sin(y / 3 * math.pi)) * 2 / 3
    value += (160 * math.sin(y / 12 * math.pi) + 320 * math.sin(y * math.pi / 30)) * 2 / 3
    return value


def _transform_lng(x: float, y: float) -> float:
    value = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    value += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    value += (20 * math.sin(x * math.pi) + 40 * math.sin(x / 3 * math.pi)) * 2 / 3
    value += (150 * math.sin(x / 12 * math.pi) + 300 * math.sin(x / 30 * math.pi)) * 2 / 3
    return value


def _offset(lat: float, lng: float) -> tuple[float, float]:
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    rad_lat = math.radians(lat)
    magic = math.sin(rad_lat)
    magic = 1 - _EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    dlat = dlat * 180 / ((_A * (1 - _EE)) / (magic * sqrt_magic) * math.pi)
    dlng = dlng * 180 / (_A / sqrt_magic * math.cos(rad_lat) * math.pi)
    return dlat, dlng


def wgs84_to_gcj02(lat: float, lng: float) -> tuple[float, float]:
    if _outside_china(lat, lng):
        return lat, lng
    dlat, dlng = _offset(lat, lng)
    return lat + dlat, lng + dlng


def gcj02_to_wgs84(lat: float, lng: float) -> tuple[float, float]:
    """Invert GCJ-02 iteratively so adapter evidence is stable at meter scale."""
    if _outside_china(lat, lng):
        return lat, lng
    guess_lat, guess_lng = lat, lng
    for _ in range(8):
        shifted_lat, shifted_lng = wgs84_to_gcj02(guess_lat, guess_lng)
        guess_lat -= shifted_lat - lat
        guess_lng -= shifted_lng - lng
    return guess_lat, guess_lng


def gcj02_to_bd09(lat: float, lng: float) -> tuple[float, float]:
    """Convert a GCJ-02 point to Baidu's BD-09 latitude/longitude pair."""
    z = math.hypot(lng, lat) + 0.00002 * math.sin(lat * _X_PI)
    theta = math.atan2(lat, lng) + 0.000003 * math.cos(lng * _X_PI)
    return z * math.sin(theta) + 0.006, z * math.cos(theta) + 0.0065
