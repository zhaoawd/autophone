"""Read model for the multi-App workspace.

This module keeps profile, Case, package and Run storage details behind one
small interface.  Callers provide Run history in newest-first order so the
HTTP layer can obtain it once and avoid a per-App request/query loop.
"""
from __future__ import annotations

from typing import Any, Iterable

from autophone.packages import store as packages
from autophone.assets.apps import AppProfile
from autophone.execution.engine.cases_loader import load_cases


_CASE_LIFECYCLES = ("active", "draft", "deprecated", "archived")
_RUN_FIELDS = ("run_id", "status", "type", "ended_at", "queued_at", "stat")


def build_app_summaries(
    profiles: Iterable[AppProfile],
    *,
    runs: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Combine App Profile identity with its lightweight workspace summary.

    ``runs`` must be newest-first.  Only the latest Run per App is retained;
    sensitive profile policy fields and filesystem paths never enter this read
    model.
    """
    latest_runs: dict[str, dict[str, Any]] = {}
    for run in runs:
        app = str(run.get("app") or "")
        if app and app not in latest_runs:
            latest_runs[app] = run

    return [
        _build_one(profile, latest_run=latest_runs.get(profile.app))
        for profile in profiles
    ]


def _build_one(
    profile: AppProfile,
    *,
    latest_run: dict[str, Any] | None,
) -> dict[str, Any]:
    cases = load_cases(profile.cases_path())
    case_counts = {
        lifecycle: sum(
            1
            for case in cases
            if str(case.get("status") or "active") == lifecycle
        )
        for lifecycle in _CASE_LIFECYCLES
    }
    case_counts = {"total": len(cases), **case_counts}

    catalog = packages.catalog(
        profile.app,
        root=profile.host_state_root,
        code_root=profile.root,
    )
    installed_ref = (catalog.get("installed") or {}).get("build_ref") or {}
    active_slot = installed_ref.get("slot")
    active = next(
        (
            slot.get("candidate")
            for slot in catalog["version_slots"]
            if slot["slot"] == active_slot
        ),
        None,
    )
    package_summary = {
        # `count` stays the number of Builds so existing callers keep counting
        # artifacts; version_count is the number of App versions.
        "count": catalog["stats"]["build_count"],
        "version_count": catalog["stats"]["version_count"],
        "active_slot": active_slot,
        "active_version": installed_ref.get("short_version"),
        "active_build_id": installed_ref.get("build_id"),
        "active_label": active.get("label") if active else None,
        "default_slot": catalog.get("default_slot"),
    }

    return {
        "app": profile.app,
        "product_key": profile.product_key,
        "display_name": profile.display_name,
        "bundle_id": profile.bundle_id,
        "env": profile.env,
        "lifecycle": profile.lifecycle,
        "build_label": profile.build_label,
        "case_counts": case_counts,
        "package": package_summary,
        "latest_run": (
            {field: latest_run.get(field) for field in _RUN_FIELDS}
            if latest_run
            else None
        ),
    }
