"""Deterministic failure location; identity does not establish a definition defect."""
from __future__ import annotations

from typing import Any

FAILURE_STAGES = ('not_recorded', 'rig_readiness', 'operation_execution', 'case_execution', 'assertion')
FAILURE_STATUSES = ('fail', 'failed', 'error', 'blocked')


def definition_identity(asset_type: str, asset_id: Any, revision: Any) -> dict:
    if not isinstance(asset_id, str) or not asset_id.strip() or type(revision) is not int or revision < 1:
        return {'status': 'unknown'}
    return {'status': 'known', 'asset_type': asset_type, 'asset_id': asset_id,
            'revision': revision, 'shared': None}


def failure_fields(stage: str, definition: dict | None = None) -> dict:
    if stage not in FAILURE_STAGES:
        raise ValueError('invalid failure stage')
    return {'failure_stage': stage, 'failed_definition': dict(definition or {'status': 'unknown'})}


def exception_stage(exc: Exception, fallback: str) -> str:
    if getattr(exc, '_failure_assertion', False):
        return 'assertion'
    return 'operation_execution' if getattr(exc, '_failure_operation', False) else fallback
