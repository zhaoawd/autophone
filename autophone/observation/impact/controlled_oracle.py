"""Freeze AX-first/OCR-second text evidence and verify replacement or restoration."""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from autophone.assets.screens import load_screen_asset, screen_by_id
from autophone.observation import visual_diff
from autophone.observation.impact import ax_comparison, controlled_input as inputs
from autophone.observation.impact.comparison import EvidenceError, checkpoint, operation_source, read, _ocr_projection
from autophone.observation.impact.controlled_comparison import require


def _geometry(screen_asset_path, target, app, build):
    asset = load_screen_asset(screen_asset_path)
    require(all(str(asset.get('meta', {}).get(k)) == str(v) for k, v in {'app': app, 'build': build}.items()),
            'controlled_screen_identity_mismatch')
    screen = screen_by_id(asset, target['screen_id'])
    names = target.get('regions', [])
    require(len(names) == 1 and names[0] != '__screen__', 'controlled_named_region_required')
    name = names[0]
    sensitive = visual_diff.sensitive_region_names(screen)
    require(name not in sensitive, 'target_region_excluded')
    declared = screen.get('regions', {})
    rect = visual_diff.region_rect(declared.get(name))
    require(rect is not None, 'target_region_missing')
    excluded = [visual_diff.region_rect(declared[n]) for n in sensitive]
    require(all(r is not None for r in excluded), 'invalid_mask_region')
    require(not any(rect[0] < r[2] and rect[2] > r[0] and rect[1] < r[3] and rect[3] > r[1]
                    for r in excluded), 'target_region_excluded')
    return name, rect


def checkpoint_source(root, run_id, app, build, target):
    doc, ref = operation_source(root, run_id, app, build)
    cp = checkpoint(doc, target['screen_id'])
    # Even OCR uses the immutable checkpoint envelope for build, frame and time identity.
    envelope, ax_ref = ax_comparison.source(root, doc, cp)
    return doc, cp, envelope, ref, ax_ref


def _ax_texts(envelope, name, rect):
    snap = envelope['snapshot']
    projection = ax_comparison.project(snap, {name: rect}, [])
    # Only whole nodes inside the target can establish complete visible text.
    visible = set()
    width, height = snap['screen']['w'], snap['screen']['h']
    for node in snap['nodes']:
        x, y, w, h = node['frame']
        if w > 0 and h > 0 and x / width >= rect[0] and y / height >= rect[1] and (x+w) / width <= rect[2] and (y+h) / height <= rect[3]:
            visible.add(('identifier', node['identifier']) if node['identifier'] else ('path', node['path']))
    return [v for key, node in projection[name].items() if key in visible
            for field in ('label', 'value') if isinstance(v := node.get(field), str) and v]


def _projection(*, root, run_id, app, build, target, screen_asset_path, output_dir, channel, label, ocr_runner=None):
    name, rect = _geometry(screen_asset_path, target, app, build)
    doc, cp, envelope, operation_ref, ax_ref = checkpoint_source(root, run_id, app, build, target)
    frame, frame_ref = read(root, cp['shot'], envelope['shot_sha256'])
    proof = {'run_id': run_id, 'operation': operation_ref, 'checkpoint': ax_ref,
             'frame': frame_ref, 'channel': channel, 'region': name,
             'captured_at_ms': envelope['snapshot']['captured_at_ms']}
    if channel == 'ax':
        texts = _ax_texts(envelope, name, rect)
    else:
        require(channel == 'ocr', 'controlled_oracle_channel_invalid')
        if ocr_runner is None:
            from autophone.device.ocr_target import run_ocr
            ocr_runner = run_ocr
        with Image.open(io.BytesIO(frame)) as image:
            width, height = image.size
            bounds = (int(rect[0]*width), int(rect[1]*height), int(rect[2]*width), int(rect[3]*height))
            require(bounds[2] > bounds[0] and bounds[3] > bounds[1], 'controlled_crop_empty')
            crop = image.crop(bounds)
            data = io.BytesIO()
            crop.save(data, format='PNG')
        rel = f'impact/private/controlled-oracle/{label}-crop.png'
        path = inputs._safe_path(output_dir, rel, exists=False)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists():
            require(path.read_bytes() == data.getvalue(), 'controlled_crop_exists')
        else:
            with path.open('xb') as handle:
                path.chmod(0o600)
                handle.write(data.getvalue())
        from autophone.observation.artifacts import Artifact, append_artifacts, write_artifact_manifest
        artifact = Artifact(rel, 'other', sensitivity='sensitive', redaction_status='raw',
                            retention='local_only', ai_policy='forbidden', share_policy='forbidden')
        if (output_dir / 'artifact-manifest.json').exists():
            append_artifacts(output_dir, [artifact])
        else:
            write_artifact_manifest(output_dir, output_dir.name, [artifact])
        ocr = ocr_runner(path)
        require(ocr.get('image_w') == crop.width and ocr.get('image_h') == crop.height,
                'controlled_ocr_crop_dimensions')
        require(not ocr.get('error') and not ocr.get('truncated'), 'controlled_ocr_incomplete')
        texts = _ocr_projection(ocr, {name: (0, 0, 1, 1)}, [])[name]
        # Crop boundary crossings cannot prove complete recognition.
        require(all(b['x'] >= 0 and b['y'] >= 0 and b['x'] + b['w'] <= crop.width and
                    b['y'] + b['h'] <= crop.height for b in ocr['boxes']), 'controlled_ocr_clipped')
        proof['crop'] = {'path': rel, 'sha256': inputs._sha(data.getvalue()), 'rect': list(rect), 'pixels': list(bounds)}
        proof['ocr'] = ocr
    require(bool(texts), 'controlled_text_coverage_empty')
    return {**proof, 'texts': texts}


def _baseline_projection(*, before_text, marker, **kwargs):
    require(isinstance(before_text, str) and bool(before_text.strip()) and isinstance(marker, str)
            and bool(marker.strip()) and marker != before_text, 'controlled_oracle_text_invalid')
    try:
        proof = _projection(channel='ax', **kwargs)
        if before_text in proof['texts'] and not any(marker in text for text in proof['texts']):
            return proof
    except EvidenceError:
        pass
    proof = _projection(channel='ocr', **kwargs)
    require(before_text in proof['texts'] and not any(marker in text for text in proof['texts']),
            'controlled_original_not_visible')
    return proof


def freeze_text_oracle(*, run_id, run_dir_resolver, app, build, target, screen_asset_path,
                       before_text, marker, output_dir, ocr_runner=None):
    """Call after A and before B; put returned ``oracle`` into the staged scenario."""
    output_dir = Path(output_dir)
    root = run_dir_resolver(run_id)
    require(root is not None, 'controlled_baseline_missing')
    proof = _baseline_projection(root=Path(root), run_id=run_id, app=app, build=build, target=target,
        screen_asset_path=screen_asset_path, output_dir=output_dir, label='freeze-A',
        before_text=before_text, marker=marker, ocr_runner=ocr_runner)
    oracle = {'channel': proof['channel'], 'region': proof['region'], 'before_text': before_text, 'marker': marker}
    ref = inputs._write_snapshot(output_dir, 'impact/private/controlled-oracle/freeze.json',
                                 {'run_id': output_dir.name, 'oracle': oracle, 'baseline': proof})
    return {'oracle': oracle, 'proof': ref}


def verify_text_oracle(*, oracle, baseline_dir, current_dir, app, build, before_id, current_id,
                       target, screen_asset_path, output_dir, restoration=False, ocr_runner=None):
    """Revalidate A's preferred channel and use only that channel for B or C."""
    output_dir = Path(output_dir)
    common = dict(app=app, build=build, target=target, screen_asset_path=screen_asset_path,
                  output_dir=output_dir, ocr_runner=ocr_runner)
    side = 'C' if restoration else 'B'
    before = _baseline_projection(root=Path(baseline_dir), run_id=before_id, label=f'{side}-A',
                                  before_text=oracle['before_text'], marker=oracle['marker'], **common)
    require((before['channel'], before['region']) == (oracle['channel'], oracle['region']),
            'controlled_oracle_frozen_channel_mismatch')
    after = _projection(root=Path(current_dir), run_id=current_id, channel=oracle['channel'], label=side, **common)
    require(before['captured_at_ms'] < after['captured_at_ms'], 'controlled_checkpoint_order_invalid')
    wanted, absent = (oracle['before_text'], oracle['marker']) if restoration else (oracle['marker'], oracle['before_text'])
    require(wanted in after['texts'] and not any(absent in text for text in after['texts']),
            'controlled_restoration_text_failed' if restoration else 'controlled_replacement_text_failed')
    ref = inputs._write_snapshot(output_dir, f'impact/private/controlled-oracle/{side}-proof.json',
        {'run_id': output_dir.name, 'oracle': dict(oracle), 'baseline': before, 'current': after})
    return {'status': 'verified', 'channel': oracle['channel'], 'region': oracle['region'],
            'coverage_count': len(after['texts']), 'proof': ref}
