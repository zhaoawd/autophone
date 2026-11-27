"""Compare captured AX leaf structure; raw identifiers and values stay local."""
from __future__ import annotations

import math
from pathlib import Path

from autophone.assets.build_identity import subject_build_key
from autophone.contracts import validate_or_raise
from autophone.observation.impact.comparison import EvidenceError, digest, json_value, read


def source(root: Path, doc: dict, cp: dict) -> tuple[dict, dict]:
    receipt = cp.get('ax_evidence') or {}
    if receipt.get('status') != 'captured' or not receipt.get('sha256'):
        raise EvidenceError('ax_checkpoint_missing')
    data, ref = read(root, str(receipt.get('path') or ''), receipt['sha256'])
    envelope = json_value(data)
    try:
        version = envelope.get('schema_version')
        if version not in {'v1', 'v2'}:
            raise ValueError('unsupported AX checkpoint')
        validate_or_raise(f'operation-ax-checkpoint.{version}.json', envelope)
    except Exception as exc:
        raise EvidenceError('ax_snapshot_invalid_or_incomplete') from exc
    identity = envelope['identity']
    if any(identity[k] != doc.get(k) for k in ('run_id', 'app', 'build', 'operation')):
        raise EvidenceError('ax_operation_identity_mismatch')
    if any(envelope['checkpoint'][k] != cp.get(k) for k in ('expected', 'matched', 'shot', 'attempt')):
        raise EvidenceError('ax_checkpoint_identity_mismatch')
    read(root, cp['shot'], envelope['shot_sha256'])
    snapshot = envelope['snapshot']
    if snapshot['app'] != identity['bundle_id']:
        raise EvidenceError('ax_bundle_identity_mismatch')
    build = envelope['build_identity']
    key = subject_build_key(snapshot['app'], snapshot['short_version'], snapshot['bundle_version'])
    if (build.get('run_id') != doc['run_id'] or build.get('app') != doc['app']
            or build.get('subject_build_key') != key):
        raise EvidenceError('ax_build_identity_unconfirmed')
    if version == 'v1':
        if build.get('identity_state') != 'confirmed':
            raise EvidenceError('ax_build_identity_unconfirmed')
    else:
        from autophone.observation.ax_checkpoint import validate_build_evidence
        proof = envelope['build_evidence']
        try:
            validate_build_evidence(proof['package'], snapshot, identity)
        except (ValueError, KeyError, TypeError) as exc:
            raise EvidenceError('ax_build_identity_unconfirmed') from exc
        for name, source_ref in proof['sources'].items():
            if source_ref['path'] != name:
                raise EvidenceError('ax_build_source_mismatch')
            source_data, _ = read(root, source_ref['path'], source_ref['sha256'])
            source_doc = json_value(source_data)
            if source_doc.get('run_id') != doc['run_id']:
                raise EvidenceError('ax_build_source_identity_mismatch')
            if name == 'run-submission.json' and source_doc.get('app') != doc['app']:
                raise EvidenceError('ax_build_source_identity_mismatch')
    return envelope, {'run_id': doc['run_id'], **ref}


def project(snapshot: dict, regions: dict, excluded: list) -> dict:
    screen = snapshot['screen']
    if any(not math.isfinite(screen[k]) or screen[k] <= 0 for k in ('w', 'h', 'scale')):
        raise EvidenceError('ax_dimensions_invalid')
    out = {name: {} for name in regions}
    seen_paths, seen_ids = set(), set()
    for node in snapshot['nodes']:
        path, ident = node['path'], node['identifier']
        if path in seen_paths or (ident and ident in seen_ids):
            raise EvidenceError('ax_node_identity_ambiguous')
        seen_paths.add(path)
        if ident:
            seen_ids.add(ident)
        x, y, w, h = node['frame']
        if not all(math.isfinite(v) for v in (x, y, w, h)) or w < 0 or h < 0:
            raise EvidenceError('ax_frame_invalid')
        rect = (x / screen['w'], y / screen['h'], (x + w) / screen['w'], (y + h) / screen['h'])
        if w == 0 or h == 0:
            continue
        if any(rect[0] < r[2] and rect[2] > r[0] and rect[1] < r[3] and rect[3] > r[1] for r in excluded):
            continue
        cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
        for name, region in regions.items():
            if region[0] <= cx <= region[2] and region[1] <= cy <= region[3]:
                # Identifier is preferred; path fallback explicitly describes tree position.
                key = ('identifier', ident) if ident else ('path', path)
                out[name][key] = {k: (sorted(node[k]) if k == 'traits' else node[k])
                                  for k in ('path', 'label', 'role', 'traits', 'enabled', 'value', 'identifier')}
    if not out or not all(out.values()):
        raise EvidenceError('ax_coverage_empty')
    return out


def compare(*, before_dir: Path, after_dir: Path, before_doc: dict, after_doc: dict,
            before_cp: dict, after_cp: dict, regions: dict, excluded: list,
            private: Path, out_dir: Path, screen_asset: Path) -> dict:
    refs, values, envelopes = [], [], []
    for side, root, doc, cp in [('before', before_dir, before_doc, before_cp),
                               ('after', after_dir, after_doc, after_cp)]:
        envelope, ref = source(root, doc, cp)
        values.append(project(envelope['snapshot'], regions, excluded))
        envelopes.append(envelope)
        data, _ = read(root, ref['path'], ref['sha256'])
        (private / f'{side}-ax.json').write_bytes(data)
        refs.append(ref)
    before, after = (e['snapshot'] for e in envelopes)
    if (before.get('ax_backend') or 'frida_inproc') != (after.get('ax_backend') or 'frida_inproc'):
        raise EvidenceError('ax_backend_mismatch')
    packages = [e.get('build_evidence', {}).get('package', {}) for e in envelopes]
    if all(packages) and packages[0]['comparison_build_id'] != packages[1]['comparison_build_id']:
        raise EvidenceError('ax_package_build_mismatch')
    if (before['app'], before['short_version'], before['bundle_version'], before['screen']) != (
            after['app'], after['short_version'], after['bundle_version'], after['screen']):
        raise EvidenceError('ax_device_context_mismatch')
    if before['request_id'] == after['request_id'] or before['captured_at_ms'] >= after['captured_at_ms']:
        raise EvidenceError('ax_capture_order_invalid')
    changes = []
    for region in regions:
        left, right = values[0][region], values[1][region]
        for key in sorted(left.keys() | right.keys()):
            fields = [] if key not in left or key not in right else [k for k in left[key] if left[key][k] != right[key][k]]
            kind = 'added' if key not in left else 'removed' if key not in right else 'modified'
            if kind == 'modified' and not fields:
                continue
            # No identifier/path hash: even a hash can reveal low-entropy page text.
            changes.append({'region': region, 'kind': kind, 'fields': fields})
    changed_regions = sorted({c['region'] for c in changes})
    return {'semantic_changed': bool(changes), 'ax_changed': bool(changes),
            'changed_regions': changed_regions, 'ax_changes': changes,
            'ui_note': 'verified_ax_diff; ax_leaf_structure_by_region; sequential_checkpoint_capture',
            'semantic_comparison': {
                'status': 'compared', 'scope': 'ax_leaf_structure_by_region',
                'build_granularity': {side: package.get('comparison_granularity', 'legacy_version_tuple')
                                      for side, package in zip(('before', 'after'), packages)},
                'before': refs[0], 'after': refs[1],
                'screen_asset': {'path': screen_asset.relative_to(out_dir).as_posix(),
                                 'sha256': digest(screen_asset.read_bytes())}}}
