"""Validate sealed raw capture evidence before excluding cancelled tasks."""
from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass

from autophone.observation.impact.comparison import EvidenceError

EVENTS_REL = 'planes/api/private/events.jsonl'


def reject():
    raise EvidenceError('lifecycle_evidence_invalid')


def coverage(value):
    if not isinstance(value, list) or any(not isinstance(h, dict) or h.get('installed') is not True for h in value):
        return False
    return all(any(isinstance(h, dict) and h.get('selector') == selector
                   and h.get('installed') is True
                   and isinstance(h.get('implementation'), str) and h['implementation']
                   for h in value) for selector in ('- cancel', '- setState:'))


def cancelled_without_response(state):
    if not state['lifecycle']:
        return False
    lifecycle = state['lifecycle'][-1]
    terminal_proof = (not state['resp'] and lifecycle.get('terminal') is True
        and lifecycle.get('terminal_source') in ('state_observed', 'completion', 'delegate_done')
        and type(lifecycle.get('state')) is int and lifecycle['state'] == 3
        and type(lifecycle.get('error_code')) is int and lifecycle['error_code'] == -999
        and lifecycle.get('error_domain') == 'NSURLErrorDomain'
        and type(lifecycle.get('bytes_received')) is int and lifecycle['bytes_received'] == 0
        and lifecycle.get('native_response_present') is False
        and lifecycle.get('response_observed') is False
        and type(lifecycle.get('data_callbacks')) is int and lifecycle['data_callbacks'] == 0
        and type(lifecycle.get('observed_data_bytes')) is int and lifecycle['observed_data_bytes'] == 0
        and lifecycle.get('identity_ambiguous') is False and lifecycle.get('coverage_gap') is False
        and lifecycle.get('snapshot_errors') == [] and coverage(lifecycle.get('hook_coverage')))
    history_proof = all(e.get('native_response_present') is False
        and type(e.get('bytes_received')) is int and e['bytes_received'] == 0
        and e.get('response_observed') is False and type(e.get('data_callbacks')) is int
        and e['data_callbacks'] == 0 and type(e.get('observed_data_bytes')) is int
        and e['observed_data_bytes'] == 0 for e in state['lifecycle'])
    return terminal_proof and history_proof


@dataclass(frozen=True)
class SealedLedger:
    events: list[dict]
    groups: dict
    tasks: dict
    rebuilt: list[dict]
    events_ref: dict
    capture_ref: dict


def read_sealed_ledger(*, root: Path, run_id: str, rows: list[tuple[int, dict]],
                       events_sha256: str | None, capture_sha256: str,
                       child: bool = False) -> SealedLedger:
    """Validate persisted raw sequence, seal ACK, manifest and merged byte identity."""
    from autophone.observation.impact.comparison import json_value, read
    from autophone.observation.api_capture import CAPTURE_REL
    from autophone.observation.frida.net_capture import merge

    if not child and not events_sha256:
        raise EvidenceError('baseline_capture_events_not_frozen')
    raw, events_ref = read(root, EVENTS_REL, events_sha256)
    _, capture_ref = read(root, CAPTURE_REL, capture_sha256)
    if child:
        data, _ = read(root, 'artifact-manifest.json')
        registry = json_value(data)
        if not isinstance(registry, dict) or registry.get('run_id') != run_id:
            reject()
        for ref in (events_ref, capture_ref):
            found = [item for item in registry.get('artifacts') or []
                     if isinstance(item, dict) and item.get('path') == ref['path']]
            if len(found) != 1 or found[0].get('sha256') != ref['sha256']:
                reject()
    events = [json_value(line) for line in raw.splitlines() if line.strip()]
    if not events or any(not isinstance(event, dict) for event in events):
        reject()

    groups, requested, verified = {}, {}, {}
    for position, event in enumerate(events):
        kind = event.get('ev')
        if kind in ('capture_seal_failed', 'lifecycle_coverage_failed', 'lifecycle_err'):
            reject()
        if kind in ('capture_seal_requested', 'capture_seal_verified'):
            nonce = event.get('nonce')
            destination = requested if kind == 'capture_seal_requested' else verified
            if not isinstance(nonce, str) or not nonce or nonce in destination:
                reject()
            destination[nonce] = (position, event)
            continue
        identity, sequence = event.get('capture_id'), event.get('event_seq')
        if not isinstance(identity, str) or not identity or type(sequence) is not int:
            reject()
        group = groups.setdefault(identity, [])
        if sequence != len(group) + 1 or (group and group[-1][1].get('ev') == 'capture_sealed'):
            reject()
        group.append((position, event))
    if not groups or len(requested) != len(groups) or len(verified) != len(groups):
        reject()
    tasks = {}
    for capture_id, group in groups.items():
        position, seal = group[-1]
        nonce = seal.get('nonce')
        if (seal.get('ev') != 'capture_sealed' or seal.get('ok') is not True
                or seal.get('pending') != [] or not coverage(seal.get('hook_coverage'))
                or nonce not in requested or nonce not in verified):
            reject()
        request_position, _ = requested[nonce]
        verification_position, ack = verified[nonce]
        if (not request_position < position < verification_position
                or ack.get('ack_verified') is not True or ack.get('capture_id') != capture_id
                or type(ack.get('last_event_seq')) is not int or ack['last_event_seq'] != seal['event_seq']
                or type(ack.get('pending_count')) is not int or ack['pending_count'] != 0):
            reject()
        for _, event in group[:-1]:
            if event.get('ev') not in ('req', 'resp', 'task_lifecycle'):
                continue
            if event['ev'] == 'task_lifecycle' and event.get('kind') not in (
                    'task_observed', 'resume_observed', 'cancel_requested', 'state_observed', 'response_observed',
                    'data_observed', 'completion', 'delegate_done', 'synthetic_completion'):
                reject()
            task_id = event.get('task_instance_id')
            if not isinstance(task_id, str) or not task_id:
                reject()
            identity = (capture_id, task_id)
            state = tasks.setdefault(identity, {'req': [], 'resp': [], 'lifecycle': []})
            state['lifecycle' if event['ev'] == 'task_lifecycle' else event['ev']].append(event)
        if type(seal.get('task_count')) is not int or seal['task_count'] != sum(k[0] == capture_id for k in tasks):
            reject()
    # Rebuild from raw req/resp bytes. Caller-supplied merge annotations cannot
    # establish identity, absence of response, or response contents.
    rebuilt = merge(events)
    if len(rebuilt) != len(rows):
        reject()
    for (_, row), expected in zip(rows, rebuilt):
        keys = set(expected) | {'req_body_text', 'req_body_b64', 'resp_body_text', 'resp_body_b64'}
        if any(json.dumps(row.get(key), sort_keys=True) != json.dumps(expected.get(key), sort_keys=True) for key in keys):
            reject()
    return SealedLedger(events, groups, tasks, rebuilt, events_ref, capture_ref)


def select(*, root: Path, run_id: str, rows: list[tuple[int, dict]], endpoint: str,
           events_sha256: str | None, capture_sha256: str, child: bool = False,
           strategy: str = 'terminal_cancelled_zero_bytes') -> tuple[list[tuple[int, dict]], dict]:
    from autophone.observation.impact.comparison import _endpoint

    ledger = read_sealed_ledger(root=root, run_id=run_id, rows=rows,
        events_sha256=events_sha256, capture_sha256=capture_sha256, child=child)
    events, groups, tasks = ledger.events, ledger.groups, ledger.tasks
    rebuilt, events_ref, capture_ref = ledger.rebuilt, ledger.events_ref, ledger.capture_ref
    controlled_ids, controlled_proof = set(), {}
    if any(event.get('controlled') is True for event in events):
        from autophone.observation.impact.controlled_comparison import select_response_set
        from autophone.observation.impact.comparison import json_value, read
        from autophone.observation.impact import controlled_input
        from autophone.observation.impact.endpoints import endpoint_key
        spec = json_value(read(root, 'run-spec.json')[0])
        loaded = controlled_input.load_for_run(root, spec)
        if loaded is None:
            reject()
        controlled_endpoint = endpoint_key('GET', loaded.compiled.rules[0]['match']['exact_url'])
        if controlled_endpoint == endpoint:
            raise EvidenceError('real_source_injected')
        selected = select_response_set(root, spec, endpoint=controlled_endpoint)
        controlled_ids = {(m['capture_id'], m['task_instance_id']) for m in selected['members']}
        controlled_proof = {'controlled_delivery': selected['receipt']}
    unrequested_cancelled = 0
    for identity, state in tasks.items():
        if identity in controlled_ids:
            continue
        if len(state['req']) > 1 or len(state['resp']) > 1:
            reject()
        if state['req']:
            request = state['req'][0]
        else:
            if (strategy != 'consistent_complete_response_set' or not cancelled_without_response(state)
                    or any(e.get('request_observed') is not False
                           or e.get('identity_source') != 'creation'
                           or type(e.get('bytes_sent')) is not int or e['bytes_sent'] != 0
                           or type(e.get('state')) is not int or e['state'] not in (1, 2, 3)
                           or e.get('kind') == 'resume_observed' or e.get('snapshot_errors') != []
                           for e in state['lifecycle'])):
                reject()
            request = state['lifecycle'][0]
            unrequested_cancelled += 1
        if not isinstance(request.get('task_ptr'), str) or not request['task_ptr']:
            reject()
        if any(event.get('task_ptr') != request.get('task_ptr') for event in state['resp'] + state['lifecycle']):
            reject()
        terminals = [event for event in state['lifecycle'] if event.get('terminal') is True]
        if not terminals or any(event.get('identity_ambiguous') is not False
                                or event.get('coverage_gap') is not False for event in state['lifecycle']):
            reject()
        if state['lifecycle'][-1].get('terminal') is not True:
            reject()

    if strategy == 'consistent_complete_response_set' and any(not _endpoint(row) for row in rebuilt):
        reject()
    candidates = [(line, row) for (line, row), expected in zip(rows, rebuilt)
                  if not expected.get('noise') and _endpoint(expected) == endpoint]
    if strategy == 'consistent_complete_response_set':
        for _, row in candidates:
            state = tasks[(row['capture_id'], row['task_instance_id'])]
            lifecycle = state['lifecycle'][-1]
            if (len(state['resp']) != 1 or type(lifecycle.get('state')) is not int
                    or lifecycle['state'] != 3 or lifecycle.get('error_code') is not None
                    or lifecycle.get('native_response_present') is not True
                    or type(lifecycle.get('status')) is not int or lifecycle['status'] != row.get('status')
                    or type(lifecycle.get('bytes_received')) is not int or lifecycle['bytes_received'] < 0
                    or lifecycle.get('snapshot_errors') != [] or not coverage(lifecycle.get('hook_coverage'))):
                reject()
        return candidates, {'run_id': run_id, 'events': events_ref, 'capture': capture_ref,
            'event_count': len(events), 'capture_count': len(groups), 'candidate_count': len(candidates),
            'excluded_count': 0, 'retained_count': len(candidates),
            'unrequested_cancelled_task_count': unrequested_cancelled,
            'members': [{'run_id': run_id, **capture_ref, 'line': line} for line, _ in candidates], **controlled_proof}
    retained, excluded = [], 0
    for line, row in candidates:
        state = tasks.get((row.get('capture_id'), row.get('task_instance_id')))
        if state is None:
            reject()
        lifecycle = state['lifecycle'][-1]
        cancelled = cancelled_without_response(state)
        if cancelled:
            excluded += 1
        else:
            retained.append((line, row))
    return retained, {'run_id': run_id, 'events': events_ref, 'capture': capture_ref,
                      'event_count': len(events), 'capture_count': len(groups),
                      'candidate_count': len(candidates), 'excluded_count': excluded,
                      'retained_count': len(retained), **controlled_proof}
