"""Controlled source selection and exact leaf comparison, separate from physical HTTP."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Mapping

from autophone.observation.impact import controlled_input as inputs
from autophone.observation.impact import lifecycle_evidence as lifecycle
from autophone.observation.impact.comparison import EvidenceError, _endpoint, read, json_value


def require(condition, code):
    if not condition:
        raise EvidenceError(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def private_document(root: Path, rel: str) -> tuple[dict, dict]:
    data = inputs._safe_path(root, rel).read_bytes()
    sha = inputs._sha(data)
    registry = json_value(inputs._safe_path(root, 'artifact-manifest.json').read_bytes())
    matches = [a for a in registry.get('artifacts', []) if a.get('path') == rel]
    require(len(matches) == 1 and all(matches[0].get(k) == v for k, v in {
        'sha256': sha, 'retention': 'local_only', 'ai_policy': 'forbidden',
        'share_policy': 'forbidden', 'redaction_status': 'raw',
    }.items()), 'controlled_private_registry_mismatch')
    doc = json_value(data)
    require(registry.get('run_id') == doc.get('run_id'), 'controlled_private_identity_mismatch')
    return doc, {'path': rel, 'sha256': sha}


def reject_injected(root: Path, matches: list) -> None:
    require(not any(row.get('injected') or row.get('scenario') or
                    row.get('source_kind') in ('controlled', 'controlled_api_change')
                    for _, row in matches), 'real_source_injected')
    if not (root / lifecycle.EVENTS_REL).exists():
        return
    events = [json_value(line) for line in read(root, lifecycle.EVENTS_REL)[0].splitlines() if line.strip()]
    identities = {(row.get('capture_id'), row.get('task_instance_id')) for _, row in matches}
    pointers = {row.get('task_ptr') for _, row in matches}
    for event in events:
        if not (str(event.get('ev', '')).startswith('scenario') or event.get('injected')):
            continue
        relevant = ((event.get('capture_id'), event.get('task_instance_id')) in identities
                    if event.get('task_instance_id') else event.get('task_ptr') in pointers)
        require(not relevant, 'real_source_injected')


def tokens(path: str, *, wildcard=False) -> list:
    atom = r'(?:[0-9]+|\*)' if wildcard else r'[0-9]+'
    require(isinstance(path, str) and bool(re.fullmatch(r'\$(?:\.[A-Za-z_][A-Za-z0-9_]*|\[' + atom + r'\])*', path)),
            'controlled_path_invalid')
    return [field or ('*' if index == '*' else int(index)) for field, index in
            re.findall(r'\.([A-Za-z_][A-Za-z0-9_]*)|\[([0-9]+|\*)\]', path)]


def leaf(body, path):
    try:
        for token in tokens(path):
            require(isinstance(body, dict) if isinstance(token, str) else isinstance(body, list),
                    'controlled_path_missing')
            body = body[token]
        return body
    except (KeyError, IndexError, TypeError):
        raise EvidenceError('controlled_path_missing') from None


def validate_mutation_target(document: Mapping, target: Mapping) -> str:
    path = document['mutation']['path']
    exact = tokens(path)
    allowed = [tokens(p, wildcard=True) for p in target.get('response_paths', [])]
    require(bool(exact) and any(len(prefix) <= len(exact) and all(
        a == b or (a == '*' and type(b) is int) for a, b in zip(prefix, exact)) for prefix in allowed),
        'controlled_path_outside_binding')
    require(document['oracle']['region'] in target.get('regions', []), 'controlled_region_outside_binding')
    return path


def select_response_set(run_dir: Path, spec: Mapping, *, endpoint: str) -> dict:
    from autophone.observation.impact.controlled_evidence import _delivery_receipt, RECEIPT_REL
    root = Path(run_dir)
    try:
        loaded = inputs.load_for_run(root, spec)
        require(loaded is not None, 'controlled_input_required')
        receipt, ref = private_document(root, RECEIPT_REL)
        events = [json_value(line) for line in inputs._safe_path(root, lifecycle.EVENTS_REL).read_bytes().splitlines() if line.strip()]
        rebuilt = _delivery_receipt(root, spec, events, receipt.get('cleanup'))
        require(canonical(rebuilt) == canonical(receipt), 'controlled_receipt_mismatch')
        data, capture_ref = read(root, 'planes/api/capture.jsonl', receipt['capture']['sha256'])
        rows = [(i, json_value(line)) for i, line in enumerate(data.splitlines(), 1) if line.strip()]
        endpoint_ids = {(row.get('capture_id'), row.get('task_instance_id'))
                        for _, row in rows if _endpoint(row) == endpoint}
        controlled_ids = {(member['capture_id'], member['task_instance_id']) for member in receipt['members']}
        require(endpoint_ids == controlled_ids, 'controlled_response_set_incomplete')
        members = []
        for member in receipt['members']:
            selected = [(line, row) for line, row in rows if
                        (row.get('capture_id'), row.get('task_instance_id')) ==
                        (member['capture_id'], member['task_instance_id'])]
            require(len(selected) == 1 and _endpoint(selected[0][1]) == endpoint, 'controlled_endpoint_mismatch')
            line, row = selected[0]
            members.append({**member, 'physical': row, 'capture': {'run_id': spec['run_id'], **capture_ref, 'line': line}})
        return {'body': json_value(members[0]['effective_body'].encode()), 'members': members,
                'receipt': {'run_id': spec['run_id'], **ref}, 'document': loaded.document,
                'rule_snapshot_hash': loaded.compiled.snapshot_hash}
    except EvidenceError:
        raise
    except (ValueError, KeyError, TypeError, OSError, AttributeError):
        raise EvidenceError('controlled_source_invalid') from None


def paired_api(*, before_dir, after_dir, before_id, after_id, source, target,
               before_doc, after_doc, plan, out_dir, settings):
    from autophone.observation.impact.comparison import _response, _request_identity, checkpoint
    from autophone.observation.impact import ax_comparison
    spec = json_value(read(after_dir, 'run-spec.json')[0])
    selected = select_response_set(after_dir, spec, endpoint=target['endpoint_key'])
    document = selected['document']
    declared = plan.get('api_scenario', {})
    require(declared.get('rule_snapshot_hash') == selected['rule_snapshot_hash'], 'controlled_plan_rule_mismatch')
    parent, _ = inputs._read_ref(out_dir, declared.get('scenario'), inputs.IMPACT_REL)
    require(canonical({k: v for k, v in parent.items() if k != 'run_id'}) ==
            canonical({k: v for k, v in document.items() if k != 'run_id'}), 'controlled_parent_scenario_mismatch')
    if settings is not None:
        from autophone.observation.impact import policy
        directory = policy._directory(target, out_dir)
        binding = json_value((directory / 'binding.json').read_bytes())
        validate_mutation_target(document, {**target, 'response_paths': [binding.get('response_path')]})
    require(document['source']['run_id'] == before_id and spec.get('run_id') == after_id,
            'controlled_baseline_identity_mismatch')
    for key in ('capture_sha256', 'capture_events_sha256'):
        require(document['source'][key] == source.get(key), 'controlled_baseline_digest_mismatch')
    require(read(before_dir, 'run-spec.json')[0].decode() == document['_frozen_source']['run_spec'],
            'controlled_baseline_spec_mismatch')
    path = validate_mutation_target(document, target)
    selection = {'strategy': 'consistent_complete_response_set', 'events_sha256': source['capture_events_sha256']}
    left, before, before_ref = _response(before_dir, before_id, target['endpoint_key'], source['capture_sha256'], source_selection=selection)
    require({ref['line'] for _, ref in selection['members']} == set(document['source']['lines']),
            'controlled_baseline_members_mismatch')
    require(canonical(before) == canonical(json_value(document['source']['body'].encode())),
            'controlled_baseline_body_mismatch')
    require(all(_request_identity(row) == _request_identity(member['physical']) for row, _ in selection['members']
                for member in selected['members']), 'controlled_request_identity_mismatch')
    # load_for_run already validates the strict whole-body single-leaf mutation.
    require(leaf(before, path) == document['oracle']['before_text'] and leaf(selected['body'], path) == document['oracle']['marker'],
            'controlled_selected_leaf_mismatch')
    envelopes = []
    for root, doc in [(before_dir, before_doc), (after_dir, after_doc)]:
        envelopes.append(ax_comparison.source(root, doc, checkpoint(doc, target['screen_id']))[0])
    snapshots = [e['snapshot'] for e in envelopes]
    for row, _ in selection['members']:
        response_before_checkpoint(row, snapshots[0]['captured_at_ms'])
    for key in ('app', 'short_version', 'bundle_version', 'screen'):
        require(snapshots[0][key] == snapshots[1][key], 'controlled_build_context_mismatch')
    packages = [e.get('build_evidence', {}).get('package') for e in envelopes]
    if any(packages):
        require(all(packages) and packages[0]['comparison_build_id'] == packages[1]['comparison_build_id'],
                'controlled_package_build_mismatch')
    require(snapshots[0]['captured_at_ms'] < snapshots[1]['captured_at_ms'] and
            snapshots[0]['request_id'] != snapshots[1]['request_id'], 'controlled_checkpoint_order_invalid')
    events = [json_value(line) for line in read(after_dir, lifecycle.EVENTS_REL)[0].splitlines() if line.strip()]
    logical = [logical_resource(m, events) for m in selected['members']]
    require(all(row['t_resp'] <= snapshots[1]['captured_at_ms'] for row in logical),
            'controlled_delivery_checkpoint_order_invalid')
    proof = {'run_id': out_dir.name, 'source_kind': 'controlled_api_change',
             'baseline': selection['proof'], 'delivery': selected['receipt'],
             'members': [{k: m[k] for k in ('capture_id', 'task_instance_id', 'rule_id', 'ordinal',
                         'request_event_seq', 'completed_event_seq', 'body_sha256', 'capture')} for m in selected['members']]}
    ref = inputs._write_snapshot(out_dir, 'impact/private/controlled-source-comparison.json', proof)
    return {'source_kind': 'controlled_api_change', 'api_observed': True, 'api_changed': True,
            'changed_paths': [path], 'api_note': 'controlled_api_change; logical_delivery_verified',
            'api_comparison': {'status': 'compared', 'scope': 'controlled_exact_json_leaf',
                'before': before_ref, 'after': selected['receipt'], 'source_selection': ref}}


def logical_resource(member, events):
    """Ephemeral logical view for delivery timing only; never persist it as physical capture."""
    import math
    matches = [e for e in events if e.get('capture_id') == member['capture_id'] and
               e.get('task_instance_id') == member['task_instance_id'] and
               e.get('event_seq') == member['completed_event_seq'] and
               e.get('ev') == 'scenario_delivery' and e.get('phase') == 'completed']
    require(len(matches) == 1, 'controlled_completion_time_missing')
    times = [member['physical'].get('t_req'), matches[0].get('ts')]
    require(all(type(t) in (int, float) and math.isfinite(t) and t > 0 for t in times) and times[0] <= times[1],
            'controlled_completion_time_invalid')
    return {**member['physical'], 'status': 200, 't_resp': times[1],
            'resp_body_text': member['effective_body'], 'resp_body_truncated': False}


def response_before_checkpoint(row, checkpoint_ms):
    import math
    times = [row.get('t_req'), row.get('t_resp')]
    require(all(type(t) in (int, float) and math.isfinite(t) and t > 0 for t in times)
            and times[0] <= times[1] <= checkpoint_ms, 'controlled_response_checkpoint_order_invalid')
