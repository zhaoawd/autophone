"""Deterministic build and asset inventory for one registered App."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from autophone.paths import APP_ASSET_ROOT
from autophone.packages import store as packages
from autophone.packages.model import PackageManifest, sorted_builds
from autophone.assets.apps import load_app_profile
from autophone.execution.engine.cases_loader import load_cases


def inventory(
    app: str,
    *,
    root: Path = APP_ASSET_ROOT,
    state_root: Path | None = None,
) -> dict[str, Any]:
    profile = load_app_profile(app, root=root, state_root=state_root)
    manifest = packages.load_manifest(
        app,
        root=profile.host_state_root,
        code_root=profile.root,
    )
    default_slot = manifest.get("default_slot")
    builds = _build_rows(manifest, app=app, default_slot=default_slot)
    app_dir = root / "apps" / app
    cases = load_cases(profile.cases_path())
    return {
        "schema_version": "knowledge-inventory.v1",
        "app": app,
        "bundle_id": profile.bundle_id,
        "profile_build_label": profile.build_label,
        "default_slot": default_slot,
        "builds": builds,
        "assets": {
            "cases": len(cases),
            "screens": _count(app_dir / "screens", "*.yaml"),
            "operations": _count(app_dir / "operations", "*.yaml"),
            "recipes": _count(app_dir / "recipes", "*.yaml"),
            "api_expectations": int((app_dir / "api_expectations.yaml").is_file()),
            "claims": _count(app_dir / "knowledge", "claims*.yaml"),
            "coverage_cells": _coverage_cells(app_dir / "knowledge" / "coverage.yaml"),
        },
    }


def _build_rows(
    manifest: dict[str, Any], *, app: str, default_slot: Any,
) -> list[dict[str, Any]]:
    """One row per registered Build, for either manifest schema.

    ``candidate`` marks the Build a bare slot resolves to; ``default`` marks the
    default selection (``default_slot``'s Candidate).  Neither says what any
    device has installed.
    """
    if str(manifest.get("schema_version") or "v1") == "v2":
        registry = PackageManifest.from_dict(manifest, app=app)
        rows = []
        for build in sorted_builds(registry.builds):
            slot = registry.slot(build.slot)
            short_version = slot.short_version if slot else None
            candidate = bool(slot) and slot.candidate_build_id == build.build_id
            rows.append({
                "slot": build.slot,
                "build_id": build.build_id,
                "label": build.label,
                "bundle_id": build.bundle_id or manifest.get("bundle_id"),
                "version": short_version,
                "short_version": short_version,
                "build_version": build.build_version,
                "sha256": build.sha256,
                "size": build.size,
                "candidate": candidate,
                "default": candidate and build.slot == default_slot,
            })
        return rows
    # A v1 slot holds exactly one Build, which is that slot's Candidate.
    return [
        {
            "slot": row.get("slot"),
            "build_id": None,
            "label": row.get("label"),
            "bundle_id": row.get("bundle_id") or manifest.get("bundle_id"),
            "version": row.get("version"),
            "short_version": row.get("short_version"),
            "build_version": row.get("build_version"),
            "sha256": row.get("sha256"),
            "size": row.get("size"),
            "candidate": True,
            "default": row.get("slot") == default_slot,
        }
        for row in manifest.get("packages") or []
    ]


def _count(path: Path, pattern: str) -> int:
    return sum(1 for item in path.glob(pattern) if item.is_file()) if path.exists() else 0


def _coverage_cells(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return 0
    return len(document.get("cells") or []) if isinstance(document, dict) else 0
