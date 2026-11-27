"""冻结比较策略与其绑定、页面资产；重试只验证已冻结字节。"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping

from autophone.contracts import validate_or_raise
from autophone.observation.impact import context
from autophone.observation.impact.endpoints import endpoint_key_from_binding


class PolicyError(ValueError):
    """策略身份缺失、变化或冻结证据损坏。"""


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(value), sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def binding_digest(binding: Mapping[str, Any]) -> str:
    return _sha(_canonical(binding))


def _directory(target: Mapping[str, Any], outdir: Path) -> Path:
    identifier = str(target.get('target_id') or '')
    if not identifier:
        raise PolicyError('comparison_target_identity_missing')
    base = Path(outdir) / 'impact/private/comparison-inputs'
    # 不跟随冻结目录内的符号链接，避免读取或覆盖目录外的证据。
    if any(path.is_symlink() for path in (base.parent, base, base / _sha(identifier.encode()))):
        raise PolicyError('comparison_snapshot_symlink')
    return base / _sha(identifier.encode())


def has_snapshot(target: Mapping[str, Any], outdir: Path) -> bool:
    return _directory(target, outdir).exists()


def _read(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise PolicyError('comparison_snapshot_missing')
    return path.read_bytes()


def _validate_binding(target: Mapping[str, Any], binding: Mapping[str, Any], screen: bytes) -> None:
    validate_or_raise('field-binding.v1.json', {
        'meta': {'schema_version': 1, 'app': 'comparison-snapshot', 'next_seq': 1},
        'bindings': [dict(binding)],
    })
    if (not target.get('binding_sha256') or not target.get('screen_sha256')
            or target.get('binding_revision') != binding.get('revision')
            or target.get('binding_id') != binding.get('id')
            or target['binding_sha256'] != binding_digest(binding)
            or target['screen_sha256'] != _sha(screen)):
        raise PolicyError('comparison_asset_identity_mismatch')
    if target.get('comparison_policy') != binding.get('comparison_policy'):
        raise PolicyError('comparison_policy_mismatch')
    if (binding.get('sensitivity') == 'sensitive'
            and 'semantic_scope' in (binding.get('comparison_policy') or {})):
        raise PolicyError('comparison_sensitive_binding_scope')
    if target.get('confidence') != binding.get('confidence'):
        raise PolicyError('comparison_binding_confidence_mismatch')
    expected_endpoint = endpoint_key_from_binding(binding.get('endpoint'))
    if expected_endpoint and target.get('endpoint_key') != expected_endpoint:
        raise PolicyError('comparison_binding_endpoint_mismatch')
    place = dict(binding.get('target') or {})
    if (target.get('screen_id') != place.get('screen_id')
            or list(target.get('regions') or []) != ([place['region']] if place.get('region') else [])
            or context.normalize_variant(target.get('variant_selector'))
            != context.normalize_variant(place.get('variant_selector'))):
        raise PolicyError('comparison_binding_target_mismatch')


def verified(target: Mapping[str, Any], outdir: Path) -> tuple[dict[str, Any] | None, Path | None]:
    """返回校验通过的冻结策略与 Screen 路径；旧目标返回 ``(None, None)``。"""
    if 'comparison_policy' not in target:
        if outdir is not None and target.get('target_id') and has_snapshot(target, outdir):
            raise PolicyError('comparison_policy_removed')
        return None, None
    try:
        directory = _directory(target, outdir)
        metadata = json.loads(_read(directory / 'manifest.json'))
        binding = json.loads(_read(directory / 'binding.json'))
        screen_path = directory / 'screen-asset.yaml'
        screen = _read(screen_path)
        if (not isinstance(metadata, dict) or not isinstance(binding, dict)
                or metadata.get('schema') != 'autophone.comparison-inputs/1'
                or metadata.get('target_sha256') != _sha(_canonical(target))
                or metadata.get('binding_sha256') != _sha(_canonical(binding))
                or metadata.get('screen_sha256') != _sha(screen)):
            raise PolicyError('comparison_snapshot_identity_mismatch')
        _validate_binding(target, binding, screen)
        frozen_policy = deepcopy(binding['comparison_policy'])
        for key in ('binding_revision', 'binding_sha256', 'screen_sha256'):
            frozen_policy[key] = target[key]
        return frozen_policy, screen_path
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, PolicyError):
            raise
        raise PolicyError('comparison_snapshot_invalid') from exc


def freeze(target: Mapping[str, Any], outdir: Path, *, binding: Mapping[str, Any],
           screen_asset_path: Path) -> tuple[dict[str, Any], Path]:
    """首次核对计划身份后原子发布快照；已有快照仅校验，不读取当前资产。"""
    directory = _directory(target, outdir)
    if directory.exists():
        frozen_policy, screen = verified(target, outdir)
        if frozen_policy is None or screen is None:
            raise PolicyError('comparison_policy_missing')
        return frozen_policy, screen
    try:
        screen_bytes = Path(screen_asset_path).read_bytes()
        _validate_binding(target, binding, screen_bytes)
        if 'comparison_policy' not in target:
            raise PolicyError('comparison_policy_missing')
        metadata = {'schema': 'autophone.comparison-inputs/1',
                    'target_sha256': _sha(_canonical(target)),
                    'binding_sha256': binding_digest(binding), 'screen_sha256': _sha(screen_bytes)}
        directory.parent.mkdir(parents=True, exist_ok=True)
        staged = Path(tempfile.mkdtemp(prefix='.staging-', dir=directory.parent))
        try:
            (staged / 'binding.json').write_bytes(_canonical(binding))
            (staged / 'screen-asset.yaml').write_bytes(screen_bytes)
            (staged / 'manifest.json').write_bytes(_canonical(metadata))
            try:
                os.rename(staged, directory)
            except OSError:
                if not directory.exists():
                    raise
                # 另一执行者已发布时只校验其结果，不覆盖。
        finally:
            if staged.exists():
                shutil.rmtree(staged)
        frozen_policy, screen = verified(target, outdir)
        assert frozen_policy is not None and screen is not None
        return frozen_policy, screen
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, PolicyError):
            raise
        raise PolicyError('comparison_freeze_invalid') from exc
