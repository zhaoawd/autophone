"""Verified baseline/child evidence. Raw values remain in their local run.

The receipt freezes source files, not a claim that two device worlds are equal.
Request and recorded Operation context equality are necessary comparison gates.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from autophone.assets.screens import load_screen_asset, screen_by_id
from autophone.observation import visual_diff
from autophone.observation.api_capture import CAPTURE_REL
from autophone.observation.impact import endpoints, manifest
from autophone.util import write_json_atomic


class EvidenceError(ValueError):
    """Evidence cannot support a comparison; messages contain no raw values."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_file(root: Path, ref: str) -> Path:
    path = Path(ref)
    if not ref or path.is_absolute() or '..' in path.parts:
        raise EvidenceError('unsafe_artifact_reference')
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise EvidenceError('missing_or_escaped_artifact')
    return resolved


def read(root: Path, ref: str, expected: str | None = None) -> tuple[bytes, dict]:
    data = safe_file(root, ref).read_bytes()
    sha = digest(data)
    if expected is not None and sha != expected:
        raise EvidenceError('artifact_digest_mismatch')
    return data, {'path': ref, 'sha256': sha}


def json_value(data: bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise EvidenceError('duplicate_json_key')
            result[key] = value
        return result

    def constant(_value):
        raise EvidenceError('nonfinite_json_value')

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError) as exc:
        raise EvidenceError('invalid_json_evidence') from exc


def operation_source(root: Path, run_id: str, app: str, build: str,
                     expected: str | None = None) -> tuple[dict, dict]:
    data, ref = read(root, 'operation-result.json', expected)
    doc = json_value(data)
    if not isinstance(doc, dict) or any(doc.get(k) != v for k, v in {
        'run_id': run_id, 'app': app, 'build': build, 'status': 'passed',
    }.items()):
        raise EvidenceError('operation_identity_mismatch')
    op = doc.get('operation')
    if not isinstance(op, dict) or not all(op.get(k) for k in ('name', 'id', 'revision')):
        raise EvidenceError('operation_identity_missing')
    return doc, {'run_id': run_id, **ref}


def checkpoint(doc: dict, screen_id: str) -> dict:
    matches = [step['screen_checkpoint'] for step in doc.get('steps') or []
               if isinstance(step, dict) and step.get('status') == 'passed'
               and isinstance(step.get('screen_checkpoint'), dict)
               and step['screen_checkpoint'].get('matched') == screen_id
               and step['screen_checkpoint'].get('status') == 'passed'
               # 只出遥测的 draft checkpoint（BL-060）不作目标页证据。
               and step['screen_checkpoint'].get('enforcement') != 'telemetry_only']
    if len(matches) != 1:
        raise EvidenceError('target_checkpoint_missing_or_ambiguous')
    return matches[0]


def freeze_baseline_source(*, run_dir: Path, app: str, build: str,
                           screen_id: str, frame_sha256: str) -> dict:
    """Bind a confirmed frame to its exact Operation and optional capture bytes."""
    data, _ = read(run_dir, 'operation-result.json')
    raw = json_value(data)
    run_id = raw.get('run_id') if isinstance(raw, dict) else None
    if not isinstance(run_id, str) or not run_id:
        raise EvidenceError('source_run_id_missing')
    doc, ref = operation_source(run_dir, run_id, app, build)
    cp = checkpoint(doc, screen_id)
    read(run_dir, str(cp.get('shot') or ''), frame_sha256)
    source = {'run_id': run_id, 'operation_result_sha256': ref['sha256']}
    if (run_dir / CAPTURE_REL).exists():
        _, capture_ref = read(run_dir, CAPTURE_REL)
        source['capture_sha256'] = capture_ref['sha256']
    from autophone.observation.impact.lifecycle_evidence import EVENTS_REL
    if (run_dir / EVENTS_REL).exists():
        _, events_ref = read(run_dir, EVENTS_REL)
        source['capture_events_sha256'] = events_ref['sha256']
    return source


def snapshot_baseline(path: Path | None, run_dir: Path) -> Path | None:
    """Freeze configured baseline inputs before child execution, including retries.

    A missing/bad frame is left absent in the snapshot and fails collection.
    Invalid sources never cause the run to fall back to a later live manifest.
    """
    if path is None:
        return None
    dest = Path(run_dir) / 'impact/private/baseline-input/manifest.json'
    if dest.is_file():
        return dest
    try:
        data = path.read_bytes()
        doc = json_value(data)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(doc, dict):
            for frame in doc.get('frames') or []:
                try:
                    ref = str(frame['file'])
                    frame_data, _ = read(path.parent, ref, str(frame['sha256']))
                    output = dest.parent / ref
                    # read() already checked the relative path; reject manifest collision.
                    if output == dest:
                        continue
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(frame_data)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        dest.write_bytes(data)
    except Exception:  # noqa: BLE001 freeze failure leaves unavailable evidence
        pass
    return dest


def _baseline(path: Path | None, plan: Mapping, target: Mapping) -> tuple[dict, dict, dict]:
    if path is None:
        raise EvidenceError('baseline_manifest_missing')
    data = path.read_bytes()
    doc = json_value(data)
    if not isinstance(doc, dict) or any(doc.get(k) != plan.get(k) or not doc.get(k)
                                       for k in ('app', 'build')):
        raise EvidenceError('baseline_identity_mismatch')
    entries = [f for f in doc.get('frames') or [] if f.get('screen_id') == target.get('screen_id')]
    if len(entries) != 1:
        raise EvidenceError('baseline_screen_missing_or_ambiguous')
    entry = entries[0]
    read(path.parent, str(entry.get('file') or ''), str(entry.get('sha256') or ''))
    source = entry.get('comparison_source')
    if not isinstance(source, dict) or not source.get('run_id') or not source.get('operation_result_sha256'):
        raise EvidenceError('baseline_comparison_source_missing')
    return entry, source, {'path': 'manifest.json', 'sha256': digest(data)}


def _endpoint(record: Mapping) -> str:
    try:
        return endpoints.endpoint_key(str(record.get('method') or ''), str(record.get('url') or ''),
                                      host=record.get('host') or None)
    except endpoints.EndpointNormalizeError:
        return ''


def _response(root: Path, run_id: str, endpoint: str, expected: str | None, *,
              source_selection: dict | None = None) -> tuple[dict, Any, dict]:
    data, ref = read(root, CAPTURE_REL, expected)
    matches, rows = [], []
    for line_number, line in enumerate(data.splitlines(), 1):
        if not line.strip():
            continue
        row = json_value(line)
        if not isinstance(row, dict):
            raise EvidenceError('invalid_capture_record')
        rows.append((line_number, row))
        if not row.get('noise') and _endpoint(row) == endpoint:
            matches.append((line_number, row))
    from autophone.observation.impact.controlled_comparison import reject_injected
    reject_injected(root, matches)
    if source_selection is not None:
        from autophone.observation.impact.lifecycle_evidence import select
        matches, proof = select(root=root, run_id=run_id, rows=rows, endpoint=endpoint,
            events_sha256=source_selection.get('events_sha256'), capture_sha256=ref['sha256'],
            child=source_selection.get('child', False),
            strategy=source_selection.get('strategy', 'terminal_cancelled_zero_bytes'))
        source_selection['proof'] = proof
    if source_selection is not None and source_selection.get('strategy') == 'consistent_complete_response_set':
        if not matches:
            raise EvidenceError('response_missing_or_ambiguous')
        bodies = [_complete_set_response(row) for _, row in matches]
        identities = {_request_identity(row) for _, row in matches}
        texts = {row['resp_body_text'].encode('utf-8') for _, row in matches}
        statuses = {row['status'] for _, row in matches}
        if len(identities) != 1 or len(texts) != 1 or len(statuses) != 1:
            raise EvidenceError('response_set_inconsistent')
        source_selection['proof']['response_body_sha256'] = digest(next(iter(texts)))
        source_selection['members'] = [(row, {'run_id': run_id, **ref, 'line': line}) for line, row in matches]
        identity = next(iter(identities))
        common = dict(zip(('method', 'url', 'req_body_text', 'req_body_b64'), identity))
        common['status'] = next(iter(statuses))
        return common, bodies[0], {'run_id': run_id, **ref}
    if len(matches) != 1:
        raise EvidenceError('response_missing_or_ambiguous')
    line_number, row = matches[0]
    body = _complete_response(row)
    return row, body, {'run_id': run_id, **ref, 'line': line_number}


def _complete_response(row: dict) -> Any:
    # Capture metadata distinguishes an absent body from an unobserved stream/body.
    if row.get('req_body_stream') or row.get('req_body_err'):
        raise EvidenceError('request_body_incomplete')
    request_length = row.get('req_body_len')
    if (isinstance(request_length, (int, float)) and request_length > 0
            and not (row.get('req_body_text') or row.get('req_body_b64'))):
        raise EvidenceError('request_body_incomplete')
    if row.get('resp_body_err'):
        raise EvidenceError('response_body_incomplete')
    if (row.get('resp_body_truncated') or row.get('req_body_truncated') or row.get('resp_error')
            or not isinstance(row.get('status'), int) or isinstance(row.get('status'), bool)
            or not 200 <= row['status'] < 300):
        raise EvidenceError('response_incomplete_or_unsuccessful')
    if not isinstance(row.get('resp_body_text'), str) or not row['resp_body_text']:
        raise EvidenceError('response_body_missing')
    return json_value(row['resp_body_text'].encode())



def _complete_body_bytes(row: dict, prefix: str) -> None:
    """Require complete bytes and the same text projection used by capture merge."""
    from autophone.observation.frida.net_capture import _maybe_text

    length = row.get(prefix + '_body_len')
    binary = row.get(prefix + '_body_b64')
    text = row.get(prefix + '_body_text')
    absent = prefix == 'req' and row.get('req_body_absent') is True
    if (type(length) is not int or (length < 0 and not (length == -1 and absent))
            or (absent and length > 0)):
        raise EvidenceError(prefix + '_body_incomplete')
    if length <= 0:
        if binary not in (None, '') or text not in (None, ''):
            raise EvidenceError(prefix + '_body_incomplete')
        return
    try:
        if not isinstance(binary, str):
            raise ValueError
        raw = base64.b64decode(binary, validate=True)
        if len(raw) != length or text != _maybe_text(raw, row.get(prefix + '_content_encoding')):
            raise ValueError
    except (ValueError, AttributeError):
        raise EvidenceError(prefix + '_body_incomplete') from None


def _complete_request_body(row: dict) -> None:
    """Strict request proof for complete sets and logical controlled delivery."""
    if row.get('req_body_stream') or row.get('req_body_err') or row.get('req_body_truncated'):
        raise EvidenceError('request_body_incomplete')
    _complete_body_bytes(row, 'req')


def _complete_set_response(row: dict) -> Any:
    try:
        url = row.get('url')
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (parsed is None or parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or (row.get('host') and str(row['host']).lower() != parsed.hostname.lower())):
            raise ValueError
        parsed.port  # Reject malformed ports rather than accepting a partial identity.
    except ValueError:
        raise EvidenceError('request_url_incomplete') from None
    _complete_request_body(row)
    body = _complete_response(row)
    _complete_body_bytes(row, 'resp')
    return body


def _request_identity(row: dict) -> tuple:
    # Exact URL includes subject IDs/query. Endpoint normalization alone is insufficient.
    return tuple(row.get(k) for k in ('method', 'url', 'req_body_text', 'req_body_b64'))


def _api(before_dir: Path, after_dir: Path, before_id: str, after_id: str,
         source: dict, target: Mapping, *, out_dir: Path | None = None,
         selection_policy: str | None = None) -> dict:
    expected = source.get('capture_sha256')
    if not expected:
        raise EvidenceError('baseline_capture_not_frozen')
    endpoint = str(target.get('endpoint_key') or '')
    if not endpoint:
        raise EvidenceError('endpoint_identity_missing')
    selection_proofs, selection_members = {}, {}
    def response(root, run_id, key, expected_sha, side):
        selection = None
        if selection_policy in ('terminal_cancelled_zero_bytes', 'consistent_complete_response_set'):
            selection = {'events_sha256': source.get('capture_events_sha256') if root == before_dir else None,
                         'child': root == after_dir, 'strategy': selection_policy}
        value = _response(root, run_id, key, expected_sha, source_selection=selection)
        if selection is not None:
            selection_proofs[side] = selection['proof']
            if 'members' in selection:
                selection_members[side] = selection['members']
        return value
    left, before, before_ref = response(before_dir, before_id, endpoint, expected, 'before')
    right, after, after_ref = response(after_dir, after_id, endpoint, None, 'after')
    if _request_identity(left) != _request_identity(right):
        raise EvidenceError('request_identity_mismatch')
    try:
        paths = sorted(manifest.changed_paths(before, after))
    except manifest.ChangeManifestError as exc:
        raise EvidenceError(str(exc)) from exc
    requested = [endpoints.normalize_response_path(p) for p in target.get('response_paths') or []]
    if requested:
        # A target absent on both sides supplies no evidence for unchanged.
        seen = set(endpoints.response_paths(before)) | set(endpoints.response_paths(after))
        if any(r != '$' and not any(endpoints.response_path_matches(p, r) for p in seen) for r in requested):
            raise EvidenceError('target_response_path_missing')
        paths = [p for p in paths if any(endpoints.response_path_matches(p, r)
                                        or endpoints.response_path_matches(r, p) for r in requested)]
    scope = ('json_paths_first_array_element_consistent_complete_response_set'
             if selection_policy == 'consistent_complete_response_set' else 'json_paths_first_array_element')
    receipt = {'status': 'compared', 'scope': scope,
               'before': before_ref, 'after': after_ref}
    if selection_proofs:
        if out_dir is None:
            raise EvidenceError('source_selection_receipt_missing')
        path = out_dir / 'impact/private' / digest(str(target['target_id']).encode())[:16] / 'source-selection.json'
        write_json_atomic(path, selection_proofs)
        receipt['source_selection'] = {'path': path.relative_to(out_dir).as_posix(), 'sha256': digest(path.read_bytes())}
    return {'api_observed': True, 'api_changed': bool(paths), 'changed_paths': paths,
            'status': right['status'], 'api_note': (
                'verified_consistent_complete_response_set_diff; all_sources_retained; arrays=first_element'
                if selection_policy == 'consistent_complete_response_set'
                else 'verified_response_diff; arrays=first_element'),
            'api_comparison': receipt}


def _ocr_projection(doc: Any, regions: dict, excluded: list[tuple]) -> dict[str, list[str]]:
    if not isinstance(doc, dict) or not isinstance(doc.get('boxes'), list):
        raise EvidenceError('invalid_ocr_evidence')
    width, height = doc.get('image_w'), doc.get('image_h')
    if not isinstance(width, (int, float)) or not isinstance(height, (int, float)) or not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
        raise EvidenceError('invalid_ocr_dimensions')
    out = {name: [] for name in regions}
    for box in doc['boxes']:
        if not isinstance(box, dict) or not isinstance(box.get('text'), str):
            raise EvidenceError('invalid_ocr_box')
        try:
            x, y, w, h = [float(box[k]) for k in ('x', 'y', 'w', 'h')]
            confidence = float(box['confidence'])
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceError('invalid_ocr_box') from exc
        if not all(math.isfinite(v) for v in (x, y, w, h, confidence)) or w < 0 or h < 0 or not 0 <= confidence <= 1:
            raise EvidenceError('invalid_ocr_box')
        rect = (x / width, y / height, (x + w) / width, (y + h) / height)
        if any(rect[0] < r[2] and rect[2] > r[0] and rect[1] < r[3] and rect[3] > r[1] for r in excluded):
            continue
        if confidence < 0.5:
            raise EvidenceError('ocr_confidence_insufficient')
        cx, cy = (x + w / 2) / width, (y + h / 2) / height
        for name, r in regions.items():
            if r[0] <= cx <= r[2] and r[1] <= cy <= r[3]:
                out[name].append(box['text'])
    if not out or not all(out.values()):
        raise EvidenceError('ocr_coverage_empty')
    return {name: sorted(texts) for name, texts in out.items()}


def _semantic(*, before_dir: Path, after_dir: Path, before_doc: dict, after_doc: dict,
              before_id: str, after_id: str, target: Mapping, entry: dict, plan: Mapping,
              screen_asset_path: Path | None, out_dir: Path | None,
              ocr_runner: Callable | None, scoped: bool = False) -> dict:
    if screen_asset_path is None or out_dir is None:
        raise EvidenceError('semantic_assets_missing')
    private = out_dir / 'impact/private' / hashlib.sha256(str(target['target_id']).encode()).hexdigest()[:16]
    private.mkdir(parents=True, exist_ok=True)
    frozen_screen = private / 'screen-asset.yaml'
    frozen_screen.write_bytes(screen_asset_path.read_bytes())
    screen_asset_path = frozen_screen
    screen_doc = load_screen_asset(screen_asset_path)
    if any(str(screen_doc.get('meta', {}).get(k) or '') != str(plan.get(k) or '') for k in ('app', 'build')):
        raise EvidenceError('screen_asset_identity_mismatch')
    screen = screen_by_id(screen_doc, target['screen_id'])
    excluded_names = visual_diff.volatile_region_names(screen, entry)
    declared = screen.get('regions') or {}
    names = target.get('regions') or ['__screen__']
    if scoped:
        if '__screen__' in names:
            raise EvidenceError('semantic_named_target_required')
        sensitive = visual_diff.sensitive_region_names(screen)
        if any(name in sensitive for name in names):
            raise EvidenceError('target_region_excluded')
        excluded_names = tuple(name for name in excluded_names if name not in names or name in sensitive)
    excluded = []
    for name in excluded_names:
        rect = visual_diff.region_rect(declared.get(name))
        if rect is None:
            raise EvidenceError('invalid_mask_region')
        excluded.append(rect)
    regions = {}
    for name in names:
        if name in excluded_names:
            raise EvidenceError('target_region_excluded')
        rect = (0., 0., 1., 1.) if name == '__screen__' else visual_diff.region_rect(declared.get(name))
        if rect is None:
            raise EvidenceError('target_region_missing')
        regions[name] = rect
    before_cp, after_cp = checkpoint(before_doc, target['screen_id']), checkpoint(after_doc, target['screen_id'])
    if 'ax_evidence' in before_cp or 'ax_evidence' in after_cp:
        from autophone.observation.impact.ax_comparison import compare
        return {**compare(before_dir=before_dir, after_dir=after_dir, before_doc=before_doc,
                          after_doc=after_doc, before_cp=before_cp, after_cp=after_cp,
                          regions=regions, excluded=excluded, private=private, out_dir=out_dir,
                          screen_asset=screen_asset_path), 'masked_regions': list(excluded_names)}
    if ocr_runner is None:
        from autophone.device.ocr_target import run_ocr
        ocr_runner = run_ocr
    # Private sidecars are not embedded in reports or default AI context bundles.
    refs, values = [], []
    for side, root, doc, run_id in [('before', before_dir, before_doc, before_id),
                                   ('after', after_dir, after_doc, after_id)]:
        cp = checkpoint(doc, target['screen_id'])
        frame_data, frame_ref = read(root, str(cp.get('shot') or ''))
        # OCR uses exactly the bytes fingerprinted in the reference, no live device access.
        image = private / f'{side}.jpg'
        image.write_bytes(frame_data)
        ocr = ocr_runner(image)
        projection = _ocr_projection(ocr, regions, excluded)
        raw_path = private / f'{side}-ocr.json'
        write_json_atomic(raw_path, ocr)
        refs.append({'run_id': run_id, **frame_ref, 'ocr': {
            'path': raw_path.relative_to(out_dir).as_posix(), 'sha256': digest(raw_path.read_bytes())}})
        values.append(projection)
    changed = [name for name in regions if values[0][name] != values[1][name]]
    return {'semantic_changed': bool(changed), 'changed_regions': changed,
            'masked_regions': list(excluded_names),
            'ui_note': 'verified_ocr_diff; exact_text_multiset_by_region; AX=unavailable',
            'semantic_comparison': {'status': 'compared', 'scope': 'ocr_text_multiset_by_region',
                                    'before': refs[0], 'after': refs[1],
                                    'screen_asset': {'path': screen_asset_path.relative_to(out_dir).as_posix(),
                                                     'sha256': digest(screen_asset_path.read_bytes())}}}


def collect(*, plan: Mapping, target: Mapping, item: dict, execution: Mapping,
            run_dir_resolver: Callable, baseline_manifest_path: Path | None,
            screen_asset_path: Path | None, impact_run_dir: Path | None,
            ocr_runner: Callable | None = None) -> None:
    """Add only verified conclusions; failure is explicit and contains no source values."""
    controlled = None
    if plan.get('api_scenario'):
        item['controlled_validation'] = {'status': 'failed', 'gold': False, 'reason': 'controlled_comparison_incomplete'}
    try:
        from autophone.observation.impact import policy
        settings, frozen_screen = policy.verified(target, impact_run_dir)
        if frozen_screen is not None:
            screen_asset_path = frozen_screen
        entry, source, manifest_ref = _baseline(baseline_manifest_path, plan, target)
        before_id = source['run_id']
        # Use the run registry, never a path supplied by the baseline manifest.
        before_dir = run_dir_resolver(before_id)
        if before_dir is None:
            raise EvidenceError('baseline_run_missing')
        before_dir = Path(before_dir)
        before_doc, before_ref = operation_source(before_dir, before_id, plan['app'], plan['build'],
                                                   source['operation_result_sha256'])
        cp = checkpoint(before_doc, target['screen_id'])
        read(before_dir, str(cp.get('shot') or ''), entry['sha256'])
        run_ids = item.get('evidence_runs') or []
        if not run_ids:
            raise EvidenceError('child_run_missing')
        after_id = run_ids[-1]
        if after_id == before_id:
            raise EvidenceError('baseline_is_child_run')
        after_dir = run_dir_resolver(after_id)
        if after_dir is None:
            raise EvidenceError('child_run_missing')
        after_dir = Path(after_dir)
        after_doc, after_ref = operation_source(after_dir, after_id, plan['app'], plan['build'])
        rows = [r for r in execution.get('operation_runs') or [] if r.get('run_id') == after_id
                and target['target_id'] in (r.get('target_ids') or []) and r.get('status') == 'passed']
        if len(rows) != 1 or rows[0].get('operation') != after_doc['operation']['name']:
            raise EvidenceError('child_execution_identity_mismatch')
        checkpoint(after_doc, target['screen_id'])
        route = (target.get('route') or {}).get('steps') or []
        if not route or any(route[-1].get(k) != after_doc['operation'].get(v)
                            for k, v in [('operation', 'name'), ('revision', 'revision')]):
            raise EvidenceError('child_plan_identity_mismatch')
        if any(before_doc.get(k) != after_doc.get(k) for k in ('operation', 'args', 'store_context')):
            raise EvidenceError('comparison_context_mismatch')
        if target.get('variant_selector'):
            from autophone.observation.impact.variant_evidence import compare as compare_variant
            receipt, screen_asset_path = compare_variant(
                plan=plan, target=target, before_dir=before_dir, after_dir=after_dir,
                before_doc=before_doc, after_doc=after_doc, screen_asset=screen_asset_path,
                out_dir=impact_run_dir)
            item['variant_comparison'] = receipt
        if impact_run_dir is not None:
            saved = Path(impact_run_dir) / 'impact/private/baseline-manifest.json'
            saved.parent.mkdir(parents=True, exist_ok=True)
            frozen = baseline_manifest_path.read_bytes()
            if digest(frozen) != manifest_ref['sha256']:
                raise EvidenceError('baseline_changed_during_collection')
            saved.write_bytes(frozen)
            manifest_ref['path'] = saved.relative_to(impact_run_dir).as_posix()
        item['comparison_sources'] = {'baseline_manifest': manifest_ref, 'before': before_ref, 'after': after_ref}
        if plan.get('api_scenario'):
            from autophone.observation.impact import controlled_input
            spec = json_value(read(after_dir, 'run-spec.json')[0])
            loaded = controlled_input.load_for_run(after_dir, spec)
            if loaded is not None:
                key = endpoints.endpoint_key('GET', loaded.compiled.rules[0]['match']['exact_url'])
                if key == target.get('endpoint_key'):
                    controlled = loaded
                    item['source_kind'] = 'controlled_api_change'
            if controlled is None:
                item.pop('controlled_validation', None)
    except Exception as exc:  # noqa: BLE001 unavailable evidence must not abort the run
        reason = str(exc) if isinstance(exc, (EvidenceError, policy.PolicyError)) else 'comparison_source_unavailable'
        if target.get('variant_selector'):
            item['variant_comparison'] = {'status': 'unavailable', 'reason': reason}
        for field in ('api_comparison', 'semantic_comparison'):
            item[field] = {'status': 'unavailable', 'reason': reason}
        return
    def api():
        if controlled is not None:
            from autophone.observation.impact.controlled_comparison import paired_api
            return paired_api(before_dir=before_dir, after_dir=after_dir, before_id=before_id,
                after_id=after_id, source=source, target=target, before_doc=before_doc,
                after_doc=after_doc, plan=plan, out_dir=Path(impact_run_dir), settings=settings)
        return _api(before_dir, after_dir, before_id, after_id, source, target, out_dir=impact_run_dir,
                    selection_policy=(settings or {}).get('source_selection'))

    def semantic():
        if controlled is not None:
            if (item.get('api_comparison') or {}).get('status') != 'compared':
                raise EvidenceError('controlled_api_proof_required')
            from autophone.observation.impact.controlled_oracle import verify_text_oracle
            proof = verify_text_oracle(oracle=controlled.document['oracle'], baseline_dir=before_dir,
                current_dir=after_dir, app=plan['app'], build=plan['build'], before_id=before_id,
                current_id=after_id, target=target, screen_asset_path=screen_asset_path,
                output_dir=impact_run_dir, ocr_runner=ocr_runner)
            return {'semantic_changed': True, 'changed_regions': [proof['region']],
                    'controlled_validation': {'status': 'pending_restoration', 'gold': False, 'text_oracle': proof},
                    'semantic_comparison': {'status': 'compared', 'scope': 'controlled_exact_text',
                        'before': before_ref, 'after': after_ref, 'source_selection': proof['proof']}}
        scoped = (settings or {}).get('semantic_scope') == 'declared_target_regions'
        if scoped and (item.get('api_comparison') or {}).get('status') != 'compared':
            raise EvidenceError('semantic_api_proof_required')
        return _semantic(before_dir=before_dir, after_dir=after_dir, before_doc=before_doc,
                         after_doc=after_doc, before_id=before_id, after_id=after_id,
                         target=target, entry=entry, plan=plan, screen_asset_path=screen_asset_path,
                         out_dir=impact_run_dir, ocr_runner=ocr_runner, scoped=scoped)

    for kind, action in [
        ('api', api),
        ('semantic', semantic),
    ]:
        try:
            item.update(action())
        except Exception as exc:  # noqa: BLE001 optional evidence cannot abort execution
            reason = str(exc) if isinstance(exc, EvidenceError) else f'{kind}_evidence_unavailable'
            item[f'{kind}_comparison'] = {'status': 'unavailable', 'reason': reason}
