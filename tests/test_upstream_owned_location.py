"""Native media commands require real capabilities and bound confirmation fields."""
import json

import pytest

from autophone.device import uds
from autophone.device.upstream_wire import admitted
from tests.test_upstream_wire import endpoint

LOCATION = ['location_source_set', 'location_stream_start', 'location_stream_push',
            'location_source_control', 'location_source_status', 'location_source_stop']
CAPS = {'ok': True, 'protocol': 'upstream_v1', 'guest_connected': True,
        'guest_capabilities': ['location', 'location_owned'],
        'commands': {kind: True for kind in LOCATION}}


def sample():
    return {'producer_sequence': 0, 'lat': 0, 'lon': 0.001, 'alt': 12, 'hacc': 3,
            'vacc': 4, 'speed': 2, 'course': 90, 'timestamp': '2026-11-01T00:00:00Z'}


def snapshot(sequence=0):
    return {'generation': 'loc-A', 'state': 'active', 'desired': {'mode': 'stream', 'owner': 'test', 'paused': False},
            'applied': {'last_producer_sequence': sequence, 'last_delivery_sequence': sequence}}


def reply(value):
    return [(json.dumps({'ok': True, 'result': value}) + '\n').encode()]


@pytest.mark.parametrize('kind', LOCATION)
def test_media_requires_explicit_host_command_and_guest_capability(kind):
    assert admitted(CAPS, kind)
    missing = 'location_owned'
    for caps in [{**CAPS, 'guest_connected': False}, {**CAPS, 'guest_capabilities': []},
                 {**CAPS, 'guest_capabilities': [c for c in CAPS['guest_capabilities'] if c != missing]},
                 {**CAPS, 'commands': {kind: False}}]:
        assert not admitted(caps, kind)


def test_stream_start_push_control_and_stop_mapping():
    messages = [
        ({'t': 'location_stream_start', 'owner': 'test', 'coordinate_system': 'wgs84', 'watchdog_s': 3, 'on_timeout': 'hold', 'replace': False}, snapshot()),
        ({'t': 'location_stream_push', 'generation': 'loc-A', **sample()}, snapshot()),
        ({'t': 'location_source_control', 'generation': 'loc-A', 'paused': True}, {**snapshot(), 'desired': {'paused': True}}),
        ({'t': 'location_source_stop', 'generation': 'loc-A'}, {'state': 'off', 'generation': None}),
    ]
    for message, value in messages:
        with endpoint(reply(value), capabilities=CAPS) as (path, sent):
            result, _ = uds.request(message, sock_path=path, protocol='upstream_v1')
        assert result['ok'] is True
        assert sent[-1] == {**message, 'screen': False}
        assert len(sent) == 2


@pytest.mark.parametrize('field,value', [('producer_sequence', True), ('producer_sequence', -1),
                                        ('lat', float('nan')), ('hacc', 0), ('generation', '')])
def test_stream_bad_payload_refused_before_connect(field, value):
    with pytest.raises(uds.ProtocolError):
        uds.request({'t': 'location_stream_push', 'generation': 'loc-A', **sample(), field: value},
                    sock_path='/must-not-connect', protocol='upstream_v1')


def test_wrong_stream_generation_or_sequence_ack_is_unknown_result_and_not_replayed():
    for value in [{**snapshot(), 'generation': 'other'}, {**snapshot(), 'applied': {'last_producer_sequence': 1, 'last_delivery_sequence': 1}},
                  {**snapshot(), 'applied': {'last_producer_sequence': False, 'last_delivery_sequence': 1}}]:
        with endpoint(reply(value), capabilities=CAPS) as (path, sent):
            with pytest.raises(uds.ProtocolError) as error:
                uds.request({'t': 'location_stream_push', 'generation': 'loc-A', **sample()}, sock_path=path, protocol='upstream_v1')
        assert error.value.operation_may_continue is True
        assert len(sent) == 2


def test_native_source_errors_preserve_codes():
    body = {'ok': False, 'code': 'location_generation_conflict', 'error': 'stale generation'}
    with endpoint([(json.dumps(body)+'\n').encode()], capabilities=CAPS) as (path, sent):
        result, _ = uds.request({'t': 'location_source_stop', 'generation': 'old'}, sock_path=path, protocol='upstream_v1')
    assert result == body
    assert len(sent) == 2
