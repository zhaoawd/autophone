"""Optional static implementation bindings; evidence remains static_hint."""
from __future__ import annotations

from typing import Any, Iterable


def bind(
    claims: Iterable[dict[str, Any]],
    static_snapshot: dict[str, Any],
) -> dict[str, Any]:
    detectors = {row.get("scope"): row for row in static_snapshot.get("detectors") or []}
    endpoints = list((detectors.get("endpoints") or {}).get("observations") or [])
    resources = list((detectors.get("resources") or {}).get("observations") or [])
    bindings: list[dict[str, Any]] = []
    for claim in claims:
        statement = str(claim.get("statement") or "")
        for endpoint in endpoints:
            if str(endpoint) in statement:
                bindings.append({
                    "claim_id": claim.get("id"),
                    "kind": "endpoint_string",
                    "symbol": endpoint,
                    "evidence_level": "static_hint",
                    "package_sha256": static_snapshot.get("package_sha256"),
                })
        for resource in resources:
            path = str((resource or {}).get("path") or "")
            if path and path in statement:
                bindings.append({
                    "claim_id": claim.get("id"),
                    "kind": "resource",
                    "symbol": path,
                    "evidence_level": "static_hint",
                    "package_sha256": static_snapshot.get("package_sha256"),
                })
    return {
        "schema_version": "implementation-bindings.v1",
        "package_sha256": static_snapshot.get("package_sha256"),
        "bindings": bindings,
    }
