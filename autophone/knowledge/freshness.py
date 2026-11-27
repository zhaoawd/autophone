"""Generic Build Scope freshness projection; lifecycle status is unchanged."""
from __future__ import annotations

from typing import Any


def evidence_state(
    asset: dict[str, Any],
    *,
    current_subject_build_key: str | None,
) -> dict[str, Any]:
    scope = asset.get("scope") or asset.get("build_scope") or {}
    key = scope.get("subject_build_key")
    if not key or not current_subject_build_key:
        return {
            "evidence_state": "missing",
            "reason": "build_identity_unconfirmed",
            "asset_status": asset.get("status"),
        }
    if key != current_subject_build_key:
        return {
            "evidence_state": "stale",
            "reason": "subject_build_key_changed",
            "asset_status": asset.get("status"),
        }
    return {
        "evidence_state": "current",
        "reason": None,
        "asset_status": asset.get("status"),
    }
