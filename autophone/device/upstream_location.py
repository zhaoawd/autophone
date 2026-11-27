"""Native owned-location protocol validation; receipt fields always come from the peer."""
from __future__ import annotations

import math

LOCATION = frozenset({'location_source_set', 'location_stream_start', 'location_stream_push',
                      'location_source_control', 'location_source_status', 'location_source_stop'})


def _text(value, name, maximum=128):
    if not isinstance(value, str) or not value.strip() or '\0' in value or len(value.encode()) > maximum:
        raise ValueError(f'{name} must be a nonempty bounded string')
    return value


def _number(value, name):
    if type(value) not in (int, float):
        raise ValueError(f'{name} must be a finite number')
    try:
        if math.isfinite(value):
            return value
    except OverflowError:
        pass
    raise ValueError(f'{name} must be a finite number')


def _integer(value, name, minimum=0):
    if type(value) is not int or not minimum <= value <= 9_007_199_254_740_991:
        raise ValueError(f'{name} must be a safe integer')
    return value


def _boolean(value, name):
    if type(value) is not bool:
        raise ValueError(f'{name} must be boolean')
    return value


def prepare(message):
    wire = {**message, 'screen': False}
    kind = wire['t']
    if kind in {'location_source_set', 'location_stream_start'}:
        _text(wire.get('owner'), 'location owner', 1024)
        if wire.get('coordinate_system') != 'wgs84':
            raise ValueError('system location delivery requires wgs84')
        _boolean(wire.get('replace', False), 'location replace')
        timer = 'heartbeat_s' if kind == 'location_source_set' else 'watchdog_s'
        if not 0.01 <= _number(wire.get(timer, 1 if timer == 'heartbeat_s' else 3), timer) <= 86400:
            raise ValueError('location timer outside native bounds')
        if kind == 'location_stream_start' and wire.get('on_timeout', 'hold') not in {'hold', 'stop'}:
            raise ValueError('invalid location timeout policy')
    if kind in {'location_stream_push', 'location_source_control', 'location_source_stop'}:
        _text(wire.get('generation'), 'location generation')
    if kind == 'location_source_control':
        _boolean(wire.get('paused'), 'location paused')
    if kind in {'location_source_set', 'location_stream_push'}:
        _integer(wire.get('producer_sequence'), 'location producer_sequence')
        lat, lon = _number(wire.get('lat'), 'location latitude'), _number(wire.get('lon'), 'location longitude')
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError('location coordinate outside native bounds')
        if abs(_number(wire.get('alt', 0), 'location altitude')) > 100000:
            raise ValueError('location altitude outside native bounds')
        if _number(wire.get('hacc', 5), 'location accuracy') <= 0:
            raise ValueError('location horizontal accuracy must be positive')
        vacc = _number(wire.get('vacc', 5), 'location accuracy')
        if vacc != -1 and vacc <= 0:
            raise ValueError('location vertical accuracy must be positive or unknown')
        speed, course = _number(wire.get('speed', -1), 'location speed'), _number(wire.get('course', -1), 'location course')
        if (speed != -1 and speed < 0) or (course != -1 and not 0 <= course < 360):
            raise ValueError('location movement outside native bounds')
        if kind == 'location_source_set':
            if wire.get('mode') != 'fixed' or wire['producer_sequence'] != 0:
                raise ValueError('fixed location source requires mode fixed and initial sequence zero')
            _boolean(wire.get('persist', False), 'location persist')
    return wire


def validate(kind, value, request):
    if value.get('ok') is not True:
        return value
    if kind in {'location_source_set', 'location_stream_start'}:
        generation = _text(value.get('generation'), 'location generation')
        desired = value.get('desired')
        if not isinstance(desired, dict) or desired.get('owner') != request['owner']:
            raise ValueError('location source owner confirmation mismatch')
        if desired.get('mode') != ('fixed' if kind == 'location_source_set' else 'stream'):
            raise ValueError('location source mode confirmation mismatch')
        if desired.get('generation', generation) != generation:
            raise ValueError('location desired generation mismatch')
    if kind in {'location_stream_push', 'location_source_control'}:
        if value.get('generation') != request['generation']:
            raise ValueError('location response generation mismatch')
    if kind in {'location_source_set', 'location_stream_push'}:
        applied = value.get('applied')
        if not isinstance(applied, dict) or applied.get('last_producer_sequence') != request['producer_sequence']:
            raise ValueError('location producer sequence confirmation mismatch')
        _integer(applied.get('last_producer_sequence'), 'location producer acknowledgement')
        _integer(applied.get('last_delivery_sequence'), 'location delivery sequence')
    if kind == 'location_source_control':
        if value.get('desired', {}).get('paused') is not request['paused']:
            raise ValueError('location pause confirmation mismatch')
    if kind == 'location_source_stop' and (value.get('generation') is not None or value.get('state') != 'off'):
        raise ValueError('location source stop not confirmed')
    return value
