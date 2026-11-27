"""Verify displayed variant categories from identity-bound native checkpoints.

Native text stays private. Login rules describe the displayed state, not server
session validity. Location rules read OS authorization without requesting it.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from autophone.assets.screens import load_screen_asset, screen_by_id
from autophone.observation.impact.comparison import EvidenceError, checkpoint, digest


def snapshot_kind(snapshot: dict) -> str:
    """Classify an AX checkpoint snapshot as ``ui_structure`` or ``inproc_graybox``."""
    raw_kind = snapshot.get('snapshot_kind')
    if raw_kind is not None and raw_kind not in {'ui_structure', 'inproc_graybox'}:
        raise EvidenceError('variant_snapshot_kind_mismatch')
    if snapshot.get('ax_backend') == 'guest_axruntime' and raw_kind == 'inproc_graybox':
        raise EvidenceError('variant_snapshot_kind_mismatch')
    if raw_kind == 'ui_structure' or snapshot.get('ax_backend') == 'guest_axruntime':
        return 'ui_structure'
    return 'inproc_graybox'


def _region_rect(screen: dict, rule: dict) -> list[float]:
    region = (screen.get('regions') or {}).get(rule['region'])
    rect = region.get('rect') if isinstance(region, dict) else region
    if not isinstance(rect, list) or len(rect) != 4:
        raise EvidenceError('variant_region_missing')
    return rect


def _screen_dims(snapshot: dict) -> tuple[float, float]:
    screen_box = snapshot.get('screen') or {}
    width, height = screen_box.get('w', 0), screen_box.get('h', 0)
    if (type(width) not in (int, float) or type(height) not in (int, float)
            or not math.isfinite(width) or not math.isfinite(height)
            or width <= 0 or height <= 0):
        raise EvidenceError('variant_dimensions_invalid')
    return float(width), float(height)


def _extract_region_texts(
    nodes: Any,
    *,
    rect: list[float],
    width: float,
    height: float,
    from_ax_nodes: bool,
) -> list[str]:
    if not isinstance(nodes, list):
        raise EvidenceError('variant_text_missing')
    texts: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            raise EvidenceError('variant_text_invalid')
        if not from_ax_nodes and not isinstance(node.get('text'), str):
            raise EvidenceError('variant_text_invalid')
        frame = node.get('frame')
        if (not isinstance(frame, list) or len(frame) != 4
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in frame)):
            raise EvidenceError('variant_frame_invalid')
        x, y, w, h = frame
        if w <= 0 or h <= 0:
            continue
        cx, cy = (x + w / 2) / width, (y + h / 2) / height
        if not (rect[0] <= cx <= rect[2] and rect[1] <= cy <= rect[3]):
            continue
        if from_ax_nodes:
            for key in ('label', 'value'):
                val = node.get(key)
                if isinstance(val, str) and val:
                    texts.append(val)
        else:
            texts.append(node['text'])
    return texts


def _match_text_candidates(texts: list[str], rule: dict) -> list[str]:
    candidates: list[str] = []
    for value, tokens in rule['values'].items():
        if (all(any(t in text for text in texts) for t in tokens['all'])
                and not any(t in text for t in tokens['none'] for text in texts)):
            candidates.append(value)
    return candidates


def _classify_inproc_graybox(snapshot: dict, screen: dict, selector: dict) -> None:
    native = snapshot.get('variant_observation') or {}
    if (type(snapshot.get('dumper_version')) is not int or snapshot['dumper_version'] < 8
            or type(native.get('protocol')) is not int or native['protocol'] != 1
            or native.get('complete') is not True):
        raise EvidenceError('variant_native_observation_missing')
    if (any(type(snapshot.get(key)) is not int or snapshot[key] != 0
            for key in ('application_state', 'key_window_scene_activation_state'))
            or snapshot.get('key_window_is_app_window') is not True):
        raise EvidenceError('variant_app_not_foreground')
    rules = screen.get('variant_checks') or {}
    for axis, expected in selector.items():
        rule = rules.get(axis) or {}
        source_kind = rule.get('source')
        if source_kind == 'core_location_authorization':
            status = native.get('location_authorization_status')
            if type(status) is not int or status not in range(5):
                raise EvidenceError('variant_location_authorization_missing')
            candidates = [value for value, statuses in rule['values'].items() if status in statuses]
        elif source_kind == 'native_ui_text':
            rect = _region_rect(screen, rule)
            width, height = _screen_dims(snapshot)
            texts = _extract_region_texts(
                native.get('ui_text_nodes'),
                rect=rect,
                width=width,
                height=height,
                from_ax_nodes=False,
            )
            candidates = _match_text_candidates(texts, rule)
        elif source_kind == 'ax_ui_text':
            rect = _region_rect(screen, rule)
            width, height = _screen_dims(snapshot)
            texts = _extract_region_texts(
                snapshot.get('nodes'),
                rect=rect,
                width=width,
                height=height,
                from_ax_nodes=True,
            )
            candidates = _match_text_candidates(texts, rule)
        else:
            raise EvidenceError('variant_rule_missing')
        if candidates != [expected]:
            raise EvidenceError('variant_missing_mismatched_or_ambiguous')


def _classify_ui_structure(snapshot: dict, screen: dict, selector: dict) -> None:
    if (snapshot.get('schema') != 'autophone.ax/1'
            or snapshot.get('complete') is not True
            or snapshot.get('truncated') is not False
            or not isinstance(snapshot.get('nodes'), list)):
        raise EvidenceError('variant_ui_structure_missing')
    width, height = _screen_dims(snapshot)
    system = snapshot.get('system')
    if snapshot.get('foreground_active') is not True or (
        isinstance(system, dict)
        and (system.get('is_screen_locked') is True or system.get('is_control_center_visible') is True)
    ):
        raise EvidenceError('variant_app_not_foreground')
    rules = screen.get('variant_checks') or {}
    for axis, expected in selector.items():
        rule = rules.get(axis) or {}
        source_kind = rule.get('source')
        if source_kind == 'ax_ui_text':
            rect = _region_rect(screen, rule)
            texts = _extract_region_texts(
                snapshot.get('nodes'),
                rect=rect,
                width=width,
                height=height,
                from_ax_nodes=True,
            )
            candidates = _match_text_candidates(texts, rule)
        elif source_kind in {'core_location_authorization', 'native_ui_text'}:
            raise EvidenceError('variant_inproc_graybox_required')
        else:
            raise EvidenceError('variant_rule_missing')
        if candidates != [expected]:
            raise EvidenceError('variant_missing_mismatched_or_ambiguous')


def classify(snapshot: dict, screen: dict, selector: dict) -> None:
    if snapshot_kind(snapshot) == 'ui_structure':
        _classify_ui_structure(snapshot, screen, selector)
    else:
        _classify_inproc_graybox(snapshot, screen, selector)


def compare(*, plan: dict, target: dict, before_dir: Path, after_dir: Path,
            before_doc: dict, after_doc: dict, screen_asset: Path, out_dir: Path) -> tuple[dict, Path]:
    from autophone.observation.impact.ax_comparison import source

    if screen_asset is None or out_dir is None:
        raise EvidenceError('variant_assets_missing')
    private = out_dir / 'impact/private' / digest(str(target['target_id']).encode())[:16]
    private.mkdir(parents=True, exist_ok=True)
    frozen = private / 'variant-screen-asset.yaml'
    frozen.write_bytes(screen_asset.read_bytes())
    asset = load_screen_asset(frozen)
    if any(str(asset.get('meta', {}).get(k) or '') != str(plan.get(k) or '') for k in ('app', 'build')):
        raise EvidenceError('variant_asset_identity_mismatch')
    screen = screen_by_id(asset, target['screen_id'])
    refs, snapshots, builds, kinds, backends = [], [], [], [], []
    for side, root, doc in [('before', before_dir, before_doc), ('after', after_dir, after_doc)]:
        envelope, ref = source(root, doc, checkpoint(doc, target['screen_id']))
        if envelope['schema_version'] != 'v2':
            raise EvidenceError('variant_confirmed_build_required')
        snap = envelope['snapshot']
        kinds.append(snapshot_kind(snap))
        backends.append(str(snap.get('ax_backend') or 'frida_inproc'))
        classify(snap, screen, target['variant_selector'])
        snapshots.append(snap)
        builds.append(envelope['build_evidence']['package']['comparison_build_id'])
        (private / f'{side}-variant.json').write_bytes((root / ref['path']).read_bytes())
        refs.append(ref)
    if backends[0] != backends[1] or kinds[0] != kinds[1]:
        raise EvidenceError('variant_ax_backend_mismatch')
    if (builds[0] != builds[1] or snapshots[0]['request_id'] == snapshots[1]['request_id']
            or snapshots[0]['captured_at_ms'] >= snapshots[1]['captured_at_ms']):
        raise EvidenceError('variant_source_context_mismatch')
    scope = 'ui_structure_checkpoint_variant' if kinds[0] == 'ui_structure' else 'native_checkpoint_variant'
    return {'status': 'compared', 'scope': scope, 'before': refs[0], 'after': refs[1],
            'screen_asset': {'path': frozen.relative_to(out_dir).as_posix(),
                             'sha256': digest(frozen.read_bytes())}}, frozen
