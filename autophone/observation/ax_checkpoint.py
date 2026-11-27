"""Persist genuine AX snapshots at Operation checkpoints, without report text."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from autophone.assets.apps import VALID_AX_BACKENDS
from autophone.assets.build_identity import build_identity_unconfirmed
from autophone.contracts import validate_or_raise
from autophone.device.ax_session import UnsupportedAxBackendError
from autophone.util import write_json_atomic


def validate_build_evidence(package: dict, snapshot: dict, identity: dict) -> None:
    if (build_identity_unconfirmed(package)
            or package.get('comparison_basis') not in {'installed_build_ref', 'candidate_build_ref'}
            or package.get('comparison_granularity') not in {'build_marker', 'info_plist'}):
        raise ValueError('ax_package_identity_unconfirmed')
    ref = package.get('registry_build_ref') or {}
    observation = package.get('device_build_observation') or {}
    if (ref.get('app') != identity['app'] or not ref.get('build_id')
            or ref['build_id'] != package.get('comparison_build_id')):
        raise ValueError('ax_package_build_mismatch')
    if any(observation.get(k) != snapshot.get(v) for k, v in (
            ('bundle_id', 'app'), ('short_version', 'short_version'), ('build_version', 'bundle_version'))):
        raise ValueError('ax_package_version_mismatch')


def _resolve_checkpoint_ax_backend(
    provider: Any,
    snapshot: dict[str, Any],
    ax_backend: str | None,
) -> str | None:
    provider_backend = getattr(provider, 'ax_backend', None)
    snapshot_backend = snapshot.get('ax_backend')
    observed: list[str] = []
    for raw in (ax_backend, provider_backend, snapshot_backend):
        if raw is None:
            continue
        if not isinstance(raw, str) or raw not in VALID_AX_BACKENDS:
            raise ValueError(f'ax_backend_invalid:{raw!r}')
        observed.append(raw)
    if not observed:
        return None
    if len(set(observed)) > 1:
        raise ValueError(f'ax_backend_mismatch:{sorted(set(observed))}')
    return observed[0]


def capture(provider: Any, *, run_dir: Path, identity: dict, checkpoint: dict,
            label: str, build_evidence: dict | None = None,
            ax_backend: str | None = None) -> dict:
    if provider is None:
        return {'status': 'unavailable', 'reason': 'ax_provider_unavailable'}
    try:
        snapshot = dict(provider.snapshot())
        resolved_backend = _resolve_checkpoint_ax_backend(provider, snapshot, ax_backend)
        if resolved_backend is not None:
            snapshot['ax_backend'] = resolved_backend
        shot = str(checkpoint['shot'])
        shot_path = (run_dir / shot).resolve()
        if not shot_path.is_relative_to(run_dir.resolve()):
            raise ValueError('unsafe checkpoint')
        doc: dict[str, Any] = {
            'schema_version': 'v1',
            'identity': {k: identity[k] for k in ('run_id', 'app', 'build', 'operation', 'bundle_id')},
            'checkpoint': {k: checkpoint[k] for k in ('expected', 'matched', 'shot', 'attempt')},
            'shot_sha256': hashlib.sha256(shot_path.read_bytes()).hexdigest(),
            'snapshot': snapshot,
            'build_identity': json.loads((run_dir / 'build-identity.json').read_bytes()),
        }
        if build_evidence is not None:
            validate_build_evidence(build_evidence['package'], snapshot, identity)
            doc.update(schema_version='v2', build_evidence=build_evidence)
            if resolved_backend is not None:
                doc['ax_backend'] = resolved_backend
        validate_or_raise(f"operation-ax-checkpoint.{doc['schema_version']}.json", doc)
        # Filename derives only from the checkpoint label, never from page text.
        token = hashlib.sha256(f"{label}:{checkpoint['attempt']}".encode()).hexdigest()[:24]
        dest = run_dir / 'planes/ax/private' / f'{token}.json'
        if not dest.resolve().is_relative_to(run_dir.resolve()):
            raise ValueError('escaped AX artifact')
        write_json_atomic(dest, doc)
        result = {
            'status': 'captured',
            'path': dest.relative_to(run_dir).as_posix(),
            'sha256': hashlib.sha256(dest.read_bytes()).hexdigest(),
        }
        if resolved_backend is not None:
            result['ax_backend'] = resolved_backend
        return result
    except UnsupportedAxBackendError:
        raise
    except Exception:  # Optional AX must not alter the Operation action result.
        return {'status': 'unavailable', 'reason': 'ax_capture_unavailable'}
