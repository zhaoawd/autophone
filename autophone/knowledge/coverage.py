"""Coverage Contract evaluation and evidence-cost build queue."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import yaml

from autophone.paths import APP_ASSET_ROOT
from autophone.contracts import validate_or_raise
from autophone.knowledge.evidence_levels import LEVELS


def coverage_path(app: str, *, root: Path = APP_ASSET_ROOT) -> Path:
    return root / "apps" / app / "knowledge" / "coverage.yaml"


def load_coverage(app: str, *, root: Path = APP_ASSET_ROOT) -> dict[str, Any]:
    path = coverage_path(app, root=root)
    if not path.is_file():
        return {"schema_version": "v1", "app": app, "cells": []}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain an object")
    validate_or_raise("knowledge-coverage.v1.json", raw)
    if str(raw.get("app")) != app:
        raise ValueError(f"{path} app does not match {app}")
    return raw


def _state(cell: dict[str, Any]) -> str:
    if cell.get("out_of_scope") is True:
        return "out_of_scope"
    if cell.get("blocker"):
        return "blocked"
    level = cell.get("evidence_level")
    if not level:
        return "unknown"
    required = str(cell.get("required_evidence_level") or "observed")
    current = str(level)
    complete_scope = cell.get("scope_complete", True) is True
    if (
        cell.get("evidence_state") == "current"
        and current in LEVELS
        and required in LEVELS
        and LEVELS.index(current) >= LEVELS.index(required)
        and complete_scope
    ):
        return "covered"
    return "partial"


def evaluate(cells: Iterable[dict[str, Any]]) -> dict[str, Any]:
    evaluated: list[dict[str, Any]] = []
    for raw in cells:
        row = dict(raw)
        row["current_state"] = _state(row)
        weight = float(row.get("business_weight") or 0)
        risk = float(row.get("effect_risk") or 0)
        cost = float(row.get("evidence_cost") or 0)
        row["build_priority"] = (weight * risk / cost) if cost > 0 else 0.0
        evaluated.append(row)
    in_scope = [row for row in evaluated if row["current_state"] != "out_of_scope"]
    covered = [row for row in in_scope if row["current_state"] == "covered"]
    total_weight = sum(float(row.get("business_weight") or 0) for row in in_scope)
    covered_weight = sum(float(row.get("business_weight") or 0) for row in covered)
    counts = Counter(row["current_state"] for row in evaluated)
    payload = {
        "schema_version": "v1",
        "raw_coverage": (len(covered) / len(in_scope)) if in_scope else 0.0,
        "business_coverage": (covered_weight / total_weight) if total_weight else 0.0,
        "counts": dict(sorted(counts.items())),
        "cells": evaluated,
        "build_queue": sorted(
            [row for row in in_scope if row["current_state"] != "covered"],
            key=lambda row: (-float(row["build_priority"]), str(row.get("id") or "")),
        ),
    }
    validate_or_raise("knowledge-coverage-result.v1.json", payload)
    return payload
