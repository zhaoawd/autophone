from __future__ import annotations

import pytest

from autophone.location.contracts import LocationCoordinate, LocationSample
from autophone.location.system import (
    CallableSystemLocationTransport,
    SystemLocationClient,
    SystemLocationError,
)


def _sample(seq: int = 0, *, coordinate_system: str = "gcj02") -> LocationSample:
    return LocationSample(
        producer_sequence=seq,
        emitted_wall_at="2026-08-09T10:00:00+08:00",
        emitted_monotonic_s=1.0,
        coordinate=LocationCoordinate(31.2304, 121.4737, coordinate_system),
        speed_mps=12.0,
        course_deg=90.0,
    )


def test_stream_client_converts_once_at_system_delivery_seam() -> None:
    seen: list[dict] = []
    client = SystemLocationClient(CallableSystemLocationTransport(
        lambda payload: seen.append(dict(payload)) or {
            "ok": True,
            "generation": "loc-1",
            "applied": {"last_delivery_sequence": 4},
        }
    ))
    start = client.start_stream(owner="route-1")
    client.push(start["generation"], _sample(7))

    assert seen[0]["t"] == "location_stream_start"
    assert seen[1]["producer_sequence"] == 7
    assert seen[1]["generation"] == "loc-1"
    assert seen[1]["lat"] != pytest.approx(31.2304)


def test_wgs84_sample_is_not_converted_again() -> None:
    seen: list[dict] = []
    client = SystemLocationClient(CallableSystemLocationTransport(
        lambda payload: seen.append(dict(payload)) or {"ok": True}
    ))
    client.push("loc-1", _sample(0, coordinate_system="wgs84"))
    assert seen[0]["lat"] == pytest.approx(31.2304)
    assert seen[0]["lon"] == pytest.approx(121.4737)


def test_client_preserves_protocol_error_code_and_wraps_transport_error() -> None:
    rejected = SystemLocationClient(CallableSystemLocationTransport(
        lambda _payload: {
            "ok": False,
            "code": "location_sequence_conflict",
            "error": "missing sequence",
        }
    ))
    with pytest.raises(SystemLocationError) as caught:
        rejected.push("loc-1", _sample())
    assert caught.value.code == "location_sequence_conflict"

    unavailable = SystemLocationClient(CallableSystemLocationTransport(
        lambda _payload: (_ for _ in ()).throw(OSError("socket down"))
    ))
    with pytest.raises(SystemLocationError) as caught:
        unavailable.status()
    assert caught.value.code == "location_guest_unavailable"


def test_same_sample_retry_sends_identical_payload() -> None:
    seen: list[dict] = []
    client = SystemLocationClient(CallableSystemLocationTransport(
        lambda payload: seen.append(dict(payload)) or {"ok": True}
    ))
    sample = _sample(3, coordinate_system="wgs84")
    client.push("loc-1", sample)
    client.push("loc-1", sample)
    assert seen[0] == seen[1]


def test_client_validates_owner_generation_and_timing_before_transport() -> None:
    client = SystemLocationClient(CallableSystemLocationTransport(
        lambda _payload: {"ok": True}
    ))
    with pytest.raises(ValueError, match="owner"):
        client.start_stream(owner="")
    with pytest.raises(ValueError, match="watchdog"):
        client.start_stream(owner="owner", watchdog_s=0)
    with pytest.raises(ValueError, match="watchdog"):
        client.start_stream(owner="owner", watchdog_s=True)
    with pytest.raises(ValueError, match="generation"):
        client.push("", _sample())
    with pytest.raises(ValueError, match="on_timeout"):
        client.start_stream(owner="owner", on_timeout="continue")


@pytest.mark.parametrize("command", ["location_source_set", "location_stream_start"])
def test_upstream_location_services_off_does_not_claim_a_source(monkeypatch, tmp_path, command):
    from autophone.device import uds
    from autophone.location.system import UDSSystemLocationTransport

    sock = str(tmp_path / "vphone.sock")
    monkeypatch.setenv("AUTOPHONE_VPHONE_PROTOCOL", "upstream_v1")
    monkeypatch.setenv("VPHONE_SOCK", sock)
    sent = []

    def send(message, **kwargs):
        assert kwargs["sock_path"] == sock
        sent.append(message["t"])
        return {"ok": False, "guest_error": {"code": "location_services_off",
                "message": "Location Services are off"}}, None

    monkeypatch.setattr(uds, "request", send)
    response = UDSSystemLocationTransport(sock).request({"t": command, "owner": "first"})
    assert sent == ["location_current"]
    assert response["code"] == "location_services_off"
