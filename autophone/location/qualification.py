"""Location-only active qualification; protocol ACKs never certify App consumption.

The caller supplies an Assignment-bound session and transport and must hold both
ScreenLock and location write authority. No global device lookup or direct fallback.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from autophone.contracts import validate_or_raise
from autophone.device.runtime_probe import runtime_identity, deployed_identity_complete
from autophone.location.system import SystemLocationClient

CONTRACT = 'owned-fixed/v1'
GATES = ('status_schema', 'fixed_ack_heartbeat', 'generation_stop_reconnect',
         'map_launch_orders', 'route_consumption', 'durable_restart',
         'sdk_consumption', 'spawn_consumption')


class LocationQualificationError(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise LocationQualificationError(reason)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def _identity(raw):
    identity = runtime_identity(raw)
    _require(identity is not None and deployed_identity_complete(identity), 'runtime_identity_incomplete')
    return identity


def _snapshot(value):
    _require(isinstance(value, dict) and value.get('ok') is True, 'status_not_ok')
    _require(value.get('state') in {'off', 'applying', 'running', 'paused', 'holding'},
             'invalid_location_state')
    _require(isinstance(value.get('desired'), dict) and isinstance(value.get('applied'), dict),
             'invalid_location_snapshot')
    sequence = value['applied'].get('last_delivery_sequence')
    _require(type(sequence) is int and sequence >= -1, 'invalid_delivery_sequence')
    if value['state'] == 'off':
        _require(value.get('generation') is None and not value['desired'].get('generation')
                 and not value['desired'].get('owner'), 'off_retains_source')
    return value


def _ack(value, owner):
    _snapshot(value)
    _require(value['state'] == 'running' and value['desired'].get('owner') == owner,
             'fixed_not_running_for_owner')
    generation = value.get('generation')
    _require(isinstance(generation, str) and bool(generation)
             and value['desired'].get('generation') == generation, 'generation_missing_or_mismatch')
    applied = value['applied']
    _require(type(applied.get('last_producer_sequence')) is int
             and applied['last_producer_sequence'] == 0
             and applied['last_delivery_sequence'] >= 0, 'fixed_ack_missing')
    try:
        stamp = datetime.fromisoformat(applied['last_ack_at'].replace('Z', '+00:00'))
        _require(stamp.tzinfo is not None, 'ack_timestamp_not_aware')
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise LocationQualificationError('invalid_ack_timestamp') from exc
    fix = applied.get('last_fix')
    _require(isinstance(fix, dict) and all(type(fix.get(k)) in (int, float)
             and math.isfinite(fix[k]) for k in ('latitude', 'longitude', 'speed_mps', 'course_deg')),
             'applied_fix_missing')
    return generation


def evaluate_evidence(evidence):
    """Recompute narrow protocol qualification from raw responses, never passed flags."""
    _require(evidence.get('error') is None, 'execution_error')
    identity = _identity(evidence['runtime_before'])
    _require(_identity(evidence['runtime_after']) == identity, 'runtime_identity_drift')
    rows = {row['step']: row for row in evidence['requests']}
    _require(len(rows) == len(evidence['requests']), 'duplicate_evidence_step')
    def response(step, command):
        row = rows[step]
        _require(row['request']['t'] == command and 'error' not in row, 'invalid_request_evidence')
        return row['response']
    capabilities = response('capabilities', 'capabilities')
    _require(capabilities.get('ok') is True and capabilities.get('guest_connected') is True
             and 'location_owned' in capabilities.get('guest_capabilities', [])
             and all(capabilities.get('commands', {}).get(name) is True for name in
                     ('location_source_status', 'location_source_set', 'location_stream_push', 'location_source_stop')),
             'owned_location_capability_unavailable')
    if identity.get('binding'):
        _require(identity['binding']['rig_id'] == evidence['rig_id'], 'runtime_target_mismatch')
    before = _snapshot(response('initial', 'location_source_status'))
    _require(before['state'] == 'off', 'initial_source_not_off')
    owner = evidence['owner']
    first = response('fixed', 'location_source_set')
    _require(rows['fixed']['request'].get('owner') == owner
             and rows['fixed']['request'].get('persist') is False
             and rows['fixed']['request'].get('replace') is False, 'unsafe_fixed_request')
    generation = _ack(first, owner)
    for observed, requested in [('latitude', 'lat'), ('longitude', 'lon'), ('speed_mps', 'speed'), ('course_deg', 'course')]:
        _require(first['applied']['last_fix'][observed] == rows['fixed']['request'][requested], 'applied_fix_mismatch')
    beat = response('heartbeat', 'location_source_status')
    _require(_ack(beat, owner) == generation
             and beat['applied']['last_delivery_sequence'] > first['applied']['last_delivery_sequence'],
             'heartbeat_not_advanced')
    replacement = response('replacement', 'location_source_set')
    next_generation = _ack(replacement, owner)
    _require(next_generation != generation, 'replacement_generation_not_changed')
    _require(rows['replacement']['request'].get('replace') is True
             and rows['replacement']['request'].get('owner') == owner
             and rows['replacement']['request'].get('persist') is False, 'unsafe_replacement_request')
    for step, command in [('stale_push', 'location_stream_push'), ('stale_stop', 'location_source_stop')]:
        rejected = response(step, command)
        _require(rows[step]['request'].get('generation') == generation
                 and rejected.get('ok') is False
                 and rejected.get('code') == 'location_generation_conflict', 'stale_generation_accepted')
    unchanged = response('after_stale', 'location_source_status')
    _require(_ack(unchanged, owner) == next_generation, 'stale_request_changed_generation')
    _require(all((unchanged['applied'].get('last_fix') or {}).get(k) == (replacement['applied'].get('last_fix') or {}).get(k) for k in ('latitude', 'longitude', 'speed_mps', 'course_deg')),
             'stale_request_changed_fix')
    for step in ('stop', 'repeat_stop'):
        stopped = _snapshot(response(step, 'location_source_stop'))
        _require(rows[step]['request'].get('generation') == next_generation
                 and stopped['state'] == 'off', 'stop_not_off')
    final = _snapshot(response('final', 'location_source_status'))
    _require(final['state'] == 'off' and evidence.get('cleanup_confirmed') is True,
             'cleanup_not_confirmed')
    return identity


def assert_location_qualified(receipt, *, runtime_info, run_id, rig_id):
    """Read-only admission for this narrow contract; no release verdict implied."""
    validate_or_raise('location-qualification.v1.json', receipt)
    _require(receipt.get('status') == 'qualified', 'qualification_failed')
    _require(receipt.get('schema_version') == 'location-qualification/v1'
             and receipt.get('contract_version') == CONTRACT, 'qualification_contract_mismatch')
    evidence = receipt.get('evidence')
    _require(isinstance(evidence, dict) and receipt.get('evidence_sha256') == _digest(evidence),
             'qualification_evidence_digest_mismatch')
    try:
        identity = evaluate_evidence(evidence)
    except (KeyError, TypeError) as exc:
        raise LocationQualificationError('qualification_evidence_incomplete') from exc
    _require(identity == _identity(runtime_info), 'qualification_runtime_mismatch')
    _require(evidence['rig_id'] == rig_id, 'qualification_target_mismatch')
    # Current process observation has no connection-generation observation. Do
    # not invent one or allow cross-Run reuse based on host/guest hashes alone.
    if type(identity['guest'].get('connection_generation')) is not int:
        _require(evidence['run_id'] == run_id, 'qualification_run_mismatch')
    return identity


def qualify_system_location(session, *, transport, run_id: str, sample,
                            artifact_dir: Path, check_authority: Callable[[], None],
                            heartbeat_s=0.1, timeout_s=3.0, sleep=time.sleep,
                            monotonic=time.monotonic):
    """Execute a bounded owned-fixed qualification under caller's Assignment.

    ``check_authority`` must check ScreenLock + Location Lease + write fence.
    ``transport`` must preserve structured NACKs and use session.target.socket
    under the same StageGuard. Identity reads use the supplied session only.
    Initial non-off sources are refused; cleanup stops only our own generation.
    Output is exclusive-create: earlier evidence is never overwritten.
    """
    _require(bool(run_id) and callable(check_authority), 'qualification_context_missing')
    _require(all(isinstance(v, (float, int)) and not isinstance(v, bool)
                 and math.isfinite(v) and v > 0 for v in (heartbeat_s, timeout_s)), 'invalid_timeout')
    _require(timeout_s >= heartbeat_s * 2, 'heartbeat_timeout_too_short')
    out = Path(artifact_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / 'location-qualification.json'
    # Reserve before any device action; failure leaves no ambiguous overwrite.
    with path.open('x', encoding='utf-8') as output:
        owner = f'location-qualification:{run_id}'
        evidence: dict[str, Any] = dict(run_id=run_id, rig_id=session.target.rig_id,
                                      owner=owner, requests=[], error=None,
                                      cleanup_confirmed=False)
        generation = None
        attempted_write = False
        initial_off = False
        def request(step, payload):
            check_authority()
            row = dict(step=step, request=copy.deepcopy(payload))
            evidence['requests'].append(row)
            try:
                row['response'] = copy.deepcopy(dict(transport.request(payload)))
                return row['response']
            except Exception as exc:
                row['error'] = f'{type(exc).__name__}: {exc}'
                raise
        try:
            check_authority()
            evidence['runtime_before'] = session.runtime_info(timeout_s=timeout_s)
            before_identity = _identity(evidence['runtime_before'])
            if before_identity.get('binding'):
                _require(before_identity['binding']['rig_id'] == session.target.rig_id, 'runtime_target_mismatch')
            capabilities = request('capabilities', {'t': 'capabilities'})
            _require(capabilities.get('ok') is True and capabilities.get('guest_connected') is True
                     and 'location_owned' in capabilities.get('guest_capabilities', [])
                     and all(capabilities.get('commands', {}).get(name) is True for name in
                         ('location_source_status', 'location_source_set', 'location_stream_push', 'location_source_stop')),
                     'owned_location_capability_unavailable')
            initial = _snapshot(request('initial', {'t': 'location_source_status'}))
            _require(initial['state'] == 'off', 'initial_source_not_off')
            initial_off = True
            wire = SystemLocationClient._wire_fields(SystemLocationClient.delivered_sample(sample))
            _require(wire['producer_sequence'] == 0, 'fixed_requires_zero_sequence')
            payload = dict(t='location_source_set', mode='fixed', owner=owner,
                           coordinate_system='wgs84', heartbeat_s=heartbeat_s,
                           replace=False, persist=False, **wire)
            attempted_write = True
            first = request('fixed', payload)
            generation = _ack(first, owner)
            deadline = monotonic() + timeout_s
            while True:
                check_authority()
                sleep(heartbeat_s)
                check_authority()
                beat = request(f'heartbeat_poll_{len(evidence["requests"])}', {'t': 'location_source_status'})
                _require(_ack(beat, owner) == generation, 'heartbeat_generation_changed')
                if beat['applied']['last_delivery_sequence'] > first['applied']['last_delivery_sequence']:
                    evidence['requests'].append(dict(step='heartbeat', request={'t': 'location_source_status'}, response=beat))
                    break
                _require(monotonic() < deadline, 'heartbeat_timeout')
            replacement = request('replacement', dict(payload, replace=True))
            generation = _ack(replacement, owner)
            old = first['generation']
            request('stale_push', dict(t='location_stream_push', generation=old, **wire))
            request('stale_stop', dict(t='location_source_stop', generation=old))
            request('after_stale', {'t': 'location_source_status'})
            request('stop', dict(t='location_source_stop', generation=generation))
            request('repeat_stop', dict(t='location_source_stop', generation=generation))
        except Exception as exc:
            evidence['error'] = f'{type(exc).__name__}: {exc}'
        finally:
            try:
                if initial_off:
                    current = _snapshot(request('cleanup_status', {'t': 'location_source_status'}))
                    if current['state'] != 'off' and attempted_write:
                        _require(current['desired'].get('owner') == owner, 'cleanup_foreign_owner')
                        current_generation = current.get('generation')
                        _require(isinstance(current_generation, str) and bool(current_generation), 'cleanup_generation_missing')
                        request('cleanup_stop', dict(t='location_source_stop', generation=current_generation))
                    final = _snapshot(request('final', {'t': 'location_source_status'}))
                    evidence['cleanup_confirmed'] = final['state'] == 'off'
                check_authority()
                evidence['runtime_after'] = session.runtime_info(timeout_s=timeout_s)
            except Exception as exc:
                evidence['cleanup_error'] = f'{type(exc).__name__}: {exc}'
            try:
                identity = evaluate_evidence(evidence)
                status, reason = 'qualified', None
            except (LocationQualificationError, KeyError, TypeError) as exc:
                identity, status, reason = None, 'failed', str(exc)
            gates = {name: 'not_run' for name in GATES}
            if status == 'qualified':
                gates.update(status_schema='passed', fixed_ack_heartbeat='passed',
                             generation_stop_reconnect='partial')
            receipt = dict(schema_version='location-qualification/v1', contract_version=CONTRACT,
                           observed_at=datetime.now(timezone.utc).isoformat(), status=status, reason=reason,
                           scope='owned_fixed_protocol', runtime_identity=identity,
                           reuse_scope='identity' if identity and type(identity['guest'].get('connection_generation')) is int else 'run',
                           release_gates=gates, release_status='incomplete', evidence=evidence,
                           evidence_sha256=_digest(evidence))
            validate_or_raise('location-qualification.v1.json', receipt)
            output.write(json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    return receipt


RELEASE_PHASES = {
    'generation_stop_reconnect': ('after_reconnect', 'after_stop_reconnect'),
    'map_launch_orders': ('fixed_before_cold_launch', 'fixed_after_launch', 'kill_relaunch'),
    'route_consumption': ('route', 'pause', 'resume', 'stop'),
    'durable_restart': ('after_restart', 'stop_then_restart'),
    'sdk_consumption': ('adapter_receipt_and_sdk_callback',),
    'spawn_consumption': ('configure_before_resume_and_callback',),
}


def capture_release_observation(session, *, gate, phase, run_id, artifact_dir,
                                collect, check_authority, subject=None):
    """Capture raw evidence for one remaining gate; never accept a passed flag.

    The Assignment-owned collector performs the scenario and returns its native
    observation payload plus artifact paths. Callbacks are application-specific,
    not AI decisions. It must return ``observations`` (nonempty list of native
    callback/UI/API/protocol events) and ``artifacts`` (local raw evidence paths).
    The wrapper reads bytes, seals their hashes, and records actual runtime on
    both sides. Restart scenarios may intentionally change process identity;
    this is retained for the release evaluator instead of silently equated.

    This is a capture interface, not a release-gate evaluator. A valid envelope
    or artifact hash alone cannot promote any release gate to passed.
    """
    _require(gate in RELEASE_PHASES and phase in RELEASE_PHASES[gate], 'unknown_release_phase')
    _require(bool(run_id), 'release_run_id_missing')
    if gate in {'map_launch_orders', 'route_consumption', 'sdk_consumption', 'spawn_consumption'}:
        _require(isinstance(subject, dict) and subject.get('bundle_id')
                 and isinstance(subject.get('build_sha256'), str)
                 and len(subject['build_sha256']) == 64, 'release_subject_identity_missing')
    out = Path(artifact_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f'location-release-{gate}-{phase}.json'
    with path.open('x', encoding='utf-8') as output:
        evidence = dict(schema_version='location-release-observation/v1', gate=gate, phase=phase,
                        run_id=run_id, rig_id=session.target.rig_id, subject=subject,
                        observed_at=datetime.now(timezone.utc).isoformat(), verdict='not_evaluated')
        try:
            check_authority()
            evidence['runtime_before'] = session.runtime_info()
            _identity(evidence['runtime_before'])
            raw = collect()
            _require(isinstance(raw, dict) and set(raw) == {'observations', 'artifacts'},
                     'release_collector_contract_invalid')
            _require(isinstance(raw['observations'], list) and bool(raw['observations'])
                     and isinstance(raw['artifacts'], list) and bool(raw['artifacts']),
                     'release_native_evidence_missing')
            evidence['observations'] = copy.deepcopy(raw['observations'])
            sealed = []
            for artifact in raw['artifacts']:
                artifact = Path(artifact).resolve()
                data = artifact.read_bytes()
                _require(bool(data), 'empty_release_artifact')
                sealed.append(dict(path=str(artifact), size_bytes=len(data),
                                   sha256=hashlib.sha256(data).hexdigest()))
            evidence['artifacts'] = sealed
            check_authority()
            evidence['runtime_after'] = session.runtime_info()
            _identity(evidence['runtime_after'])
        except Exception as exc:
            evidence['error'] = f'{type(exc).__name__}: {exc}'
        evidence['capture_sha256'] = _digest(evidence)
        output.write(json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    return evidence
