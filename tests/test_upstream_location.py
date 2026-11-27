"""Point location mapping preserves native evidence and refuses stream claims."""
import json

import pytest

from autophone.device import uds
from autophone.device.upstream_wire import admitted, prepare
from autophone.device.target import VPhoneTarget, target_scope
from pathlib import Path
from tests.test_upstream_wire import endpoint

CAPS = {"ok": True, "protocol": "upstream_v1", "guest_connected": True,
        "commands": {"rpc": True}, "guest_capabilities": ["location", "camera"]}


def reading(**overrides):
    return {"latitude": 0.0, "longitude": 0.0, "fresh": True,
            "simulated": True, "timestamp": "2026-11-01T00:00:00.000Z",
            "age_seconds": 0.01, **overrides}


def reply(value):
    return [(json.dumps({"ok": True, "result": value}) + "\n").encode()]


def test_point_location_maps_fields_and_unknown_motion():
    with prepare({"t": "location", "lat": 0, "lon": 0.001, "alt": 12,
                  "hacc": 3, "vacc": -1, "speed": -1, "course": -1}) as (wire, _):
        assert wire == {"t": "rpc", "method": "location.set", "screen": False,
                        "params": {"latitude": 0, "longitude": 0.001, "altitude": 12,
                                   "horizontal_accuracy": 3, "vertical_accuracy": -1}}


@pytest.mark.parametrize("kind", ["location", "location_current", "location_stop"])
def test_location_requires_connected_rpc_and_explicit_guest_capability(kind):
    assert admitted(CAPS, kind)
    for caps in [{**CAPS, "guest_connected": False}, {**CAPS, "commands": {"rpc": False}},
                 {**CAPS, "guest_capabilities": []}, {**CAPS, "guest_capabilities": "location"},
                 {**CAPS, "commands": {"rpc": True, kind: False}}]:
        assert not admitted(caps, kind)


@pytest.mark.parametrize("field,value", [
    ("lat", 91), ("lon", -181), ("lat", True), ("lat", float("nan")),
    ("lon", float("inf")), ("alt", 100001), ("hacc", -1), ("vacc", -2),
    ("speed", -2), ("course", 360), ("course", -2), ("generation", "old"),
])
def test_invalid_point_values_refused_before_connect(field, value):
    with pytest.raises(uds.ProtocolError, match="location"):
        uds.request({"t": "location", "lat": 0, "lon": 0, field: value},
                    sock_path="/must-not-connect", protocol="upstream_v1")


def test_set_readback_and_stop_use_native_rpcs_once():
    with endpoint(reply({"simulating": True, "location": reading()}), capabilities=CAPS) as (path, sent):
        result, _ = uds.request({"t": "location", "lat": 0, "lon": 0}, sock_path=path, protocol="upstream_v1")
    assert result["location"]["fresh"] and result["location"]["simulated"]
    assert [x.get("method") for x in sent if x["t"] == "rpc"] == ["location.set"]
    with endpoint(reply(reading(fresh=False)), capabilities=CAPS) as (path, sent):
        result, _ = uds.request({"t": "location_current", "timeout": 2}, sock_path=path, protocol="upstream_v1")
    assert result["fresh"] is False
    assert sent[-1]["method"] == "location.current" and sent[-1]["params"] == {"timeout": 2}
    with endpoint(reply({"simulating": False}), capabilities=CAPS) as (path, sent):
        with target_scope(VPhoneTarget(Path(path).parent, Path(path).parent, Path(path), "location-test", "upstream_v1")):
            result, _ = uds.stop_location(sock_path=path)
    assert result["simulating"] is False and sent[-1]["method"] == "location.clear"


@pytest.mark.parametrize("value", [
    {"simulating": False}, {"simulating": True, "location": reading(fresh=False)},
    {"simulating": True, "location": reading(simulated=False)},
    {"simulating": True, "location": reading(latitude=1)},
    {"simulating": True, "location": reading(fresh=1)},
    {"simulating": True, "location": reading(timestamp="")},
])
def test_bad_set_ack_is_unknown_completion_and_never_replayed(value):
    with endpoint(reply(value), capabilities=CAPS) as (path, sent):
        with pytest.raises(uds.ProtocolError) as error:
            uds.request({"t": "location", "lat": 0, "lon": 0}, sock_path=path, protocol="upstream_v1")
    assert error.value.operation_may_continue is True
    assert [x["t"] for x in sent] == ["capabilities", "rpc"]


def test_native_location_services_off_is_preserved():
    body=b'{"ok":false,"error":"Location Services is off","code":"location_services_off"}\n'
    with endpoint([body], capabilities=CAPS) as (path, sent):
        result, _ = uds.request({"t": "location", "lat": 0, "lon": 0}, sock_path=path, protocol="upstream_v1")
    assert result["ok"] is False and result["code"] == "location_services_off"
    assert len(sent) == 2


@pytest.mark.parametrize("kind", ["location_status"])
def test_generation_protocols_remain_unsupported(kind):
    with pytest.raises(uds.ProtocolError, match="unsupported upstream"):
        uds.request({"t": kind}, sock_path="/must-not-connect", protocol="upstream_v1")


@pytest.mark.parametrize('value', [
    {"simulating": True}, {},
    {"simulating": False, "location": reading()},
])
def test_clear_requires_confirmation_and_never_replays(value):
    with endpoint(reply(value), capabilities=CAPS) as (path, sent):
        with pytest.raises(uds.ProtocolError) as error:
            uds.request({"t": "location_stop"}, sock_path=path, protocol="upstream_v1")
    assert error.value.operation_may_continue is True
    assert len(sent) == 2


@pytest.mark.parametrize('value', [reading(fresh=1), reading(simulated=None),
                                   reading(latitude=float('nan')), reading(age_seconds=-1)])
def test_current_rejects_malformed_readings(value):
    with endpoint(reply(value), capabilities=CAPS) as (path, sent):
        with pytest.raises(uds.ProtocolError):
            uds.request({"t": "location_current"}, sock_path=path, protocol="upstream_v1")
    assert len(sent) == 2


def test_nested_native_error_body_is_not_rewritten():
    body = {"ok": False, "error": "Location Services are off",
            "guest_error": {"error": {"code": "location_services_off", "message": "Location Services are off"}}}
    with endpoint([(json.dumps(body)+'\n').encode()], capabilities=CAPS) as (path, sent):
        result, _ = uds.request({"t": "location_current"}, sock_path=path, protocol="upstream_v1")
    assert result == body
    assert len(sent) == 2


def test_flat_native_guest_error_body_is_not_rewritten():
    body = {"ok": False, "error": "Location Services are turned off in the guest.",
            "guest_error": {"code": "location_services_off", "message": "Location Services are turned off in the guest."}}
    with endpoint([(json.dumps(body)+'\n').encode()], capabilities=CAPS) as (path, sent):
        result, _ = uds.request({"t": "location_current"}, sock_path=path, protocol="upstream_v1")
    assert result == body
    assert len(sent) == 2


def test_actual_native_origin_mismatch_is_refused_without_replay():
    event = json.loads((Path(__file__).parent / 'fixtures/upstream-location-origin-mismatch.json').read_text())
    assert event['response']['ok'] is True
    assert event['response']['result']['location']['longitude'] != event['request']['params']['longitude']
    with endpoint([(json.dumps(event['response'])+'\n').encode()], capabilities=CAPS) as (path, sent):
        with pytest.raises(uds.ProtocolError, match='readback differs from requested coordinates') as error:
            uds.request({'t': 'location', 'lat': 0, 'lon': 0, 'alt': 12, 'hacc': 3,
                         'vacc': 4, 'speed': 2, 'course': 90}, sock_path=path, protocol='upstream_v1')
    assert error.value.operation_may_continue is True
    assert len(sent) == 2
    assert sent[-1]['params'] == event['request']['params']


@pytest.mark.parametrize('api', ['uds', 'session'])
def test_actual_native_unconfirmed_response_rejected_without_replay(api):
    from types import SimpleNamespace
    from autophone.device.session import VPhoneDeviceSession, DeviceProtocolError

    body = json.loads((Path(__file__).parent / 'fixtures/upstream-location-unconfirmed.json').read_text())
    assert body['guest_error']['code'] == 'location_readback_unconfirmed'
    with endpoint([(json.dumps(body)+'\n').encode()], capabilities=CAPS) as (path, sent):
        if api == 'uds':
            result, _ = uds.request({'t': 'location', 'lat': 0, 'lon': 0}, sock_path=path, protocol='upstream_v1')
            assert result == body
        else:
            target = SimpleNamespace(socket=Path(path), protocol='upstream_v1')
            session = VPhoneDeviceSession(target, SimpleNamespace(check=lambda: None))
            with pytest.raises(DeviceProtocolError, match='did not confirm'):
                session.set_location(0, 0, speed=0, course=-1)
    assert [message['t'] for message in sent] == ['capabilities', 'rpc']
    assert sent[-1]['method'] == 'location.set'
