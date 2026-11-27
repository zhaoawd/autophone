"""Registry and static snapshot diff with conservative behavior semantics."""
from __future__ import annotations

from typing import Any


def _entry(manifest: dict[str, Any], slot: str | None) -> dict[str, Any] | None:
    selected = slot if slot is not None else manifest.get("default_slot")
    return next(
        (row for row in manifest.get("packages") or [] if str(row.get("slot")) == str(selected)),
        None,
    )


def registry_diff(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    before_slot: str | None = None,
    after_slot: str | None = None,
) -> dict[str, Any]:
    left = _entry(before, before_slot)
    right = _entry(after, after_slot)
    if left is None or right is None:
        return {
            "schema_version": "registry-diff.v1",
            "status": "build_not_found",
            "behavior_change": "unknown",
            "before": left,
            "after": right,
            "changes": [],
        }
    fields = (
        "slot", "sha256", "version", "short_version", "build_version",
        "bundle_id", "size", "label",
    )
    changes = [
        {"field": field, "before": left.get(field), "after": right.get(field)}
        for field in fields
        if left.get(field) != right.get(field)
    ]
    return {
        "schema_version": "registry-diff.v1",
        "status": "changed" if changes else "unchanged",
        "behavior_change": "unknown",
        "before": {"slot": left.get("slot"), "package_sha256": left.get("sha256")},
        "after": {"slot": right.get("slot"), "package_sha256": right.get("sha256")},
        "changes": changes,
    }


def static_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    signals: list[dict[str, Any]] = []
    left_detectors = {item["scope"]: item for item in before.get("detectors") or []}
    right_detectors = {item["scope"]: item for item in after.get("detectors") or []}
    for scope in sorted(set(left_detectors) | set(right_detectors)):
        left = left_detectors.get(scope)
        right = right_detectors.get(scope)
        if left is None or right is None:
            signals.append({
                "scope": scope,
                "kind": "detector_availability_changed",
                "before_status": (left or {}).get("status", "unavailable"),
                "after_status": (right or {}).get("status", "unavailable"),
            })
            continue
        if left.get("status") == "unavailable" or right.get("status") == "unavailable":
            if left.get("status") != right.get("status"):
                signals.append({
                    "scope": scope,
                    "kind": "detector_availability_changed",
                    "before_status": left.get("status"),
                    "after_status": right.get("status"),
                })
            continue
        if left.get("observations") != right.get("observations"):
            signals.append({
                "scope": scope,
                "kind": "canonical_observations_changed",
                "before_digest": left.get("digest"),
                "after_digest": right.get("digest"),
            })
    return {
        "schema_version": "ipa-static-diff.v1",
        "before_package_sha256": before.get("package_sha256"),
        "after_package_sha256": after.get("package_sha256"),
        "behavior_change": "unknown",
        "candidate_signals": signals,
    }
