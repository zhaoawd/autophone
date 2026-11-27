"""Golden path declarations and the golden path Run marker (BL-038).

A golden path (usecase assetization §21 stage C) is declared in
``apps/<app>/golden_paths.yaml`` (schema ``autophone.golden-path/1``) and binds
one Case to the full closed loop.  This module owns the declaration and the
marker a Run carries when it is submitted for one golden path:

* ``load_golden_paths`` / ``find_golden_path`` parse the declaration, fail-closed.
* ``run_marker`` builds the Run Spec field ``golden_path``
  (``{id, case_id, declaration_sha256}``).  The digest covers the normalized
  declaration (every field ``load_golden_paths`` returns, defaults filled in,
  ``forbidden_effects`` sorted) except the display-only ``title``.  Adding a
  new declared field with a default therefore changes the digest of every
  existing declaration and invalidates markers recorded before the change:
  such Runs verify as ``declaration_changed``.
* ``validate_golden_path_request`` rejects, at submission, a marker whose path
  is not declared, whose Case does not belong to the path or is not ``draft``
  / ``active``, whose digest differs from the current declaration, or whose Run
  selects anything other than the selector the plan gives for the Case's
  current lifecycle.  An App Profile that cannot be loaded is a
  ``ContractError`` too.
* ``run_binding`` compares a recorded Run Spec with one declared path; the
  golden path verification and the preparation card read it.

Only a Run with a marker gets Fixture Readback bound for a live Recipe Case
(``autophone.execution.golden_path_binding``); ordinary regression Runs carry
no marker and execute unchanged.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import yaml

from autophone.contracts import ContractError

GOLDEN_PATH_SCHEMA = "autophone.golden-path/1"
#: Run Spec field that marks a Run as submitted for one golden path.
RUN_MARKER_FIELD = "golden_path"
_PATH_FIELDS = frozenset({"id", "title", "case_id", "trial_draft_recipes", "forbidden_effects", "stop_before"})
#: Declared fields that do not enter ``declaration_sha256`` (display only).
_UNDIGESTED_FIELDS = frozenset({"title"})
_BROAD_SELECTOR_KEYS = ("tag", "module", "suite", "flow")
#: Case lifecycles a golden path Run may be submitted for.
_SUBMITTABLE_LIFECYCLES = frozenset({"draft", "active"})

#: ``run_binding`` states.
BINDING_MATCHED = "matched"
BINDING_ABSENT = "absent"
BINDING_OTHER_PATH = "other_path"
BINDING_DECLARATION_CHANGED = "declaration_changed"
BINDING_INVALID = "invalid"


class GoldenPathError(ValueError):
    """Raised when a golden path declaration cannot be trusted."""


def golden_paths_path(profile: Any) -> Path:
    return profile.profile_dir / "golden_paths.yaml"


def load_golden_paths(profile: Any) -> list[dict[str, Any]]:
    path = golden_paths_path(profile)
    if not path.is_file():
        return []
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise GoldenPathError(f"{path}: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != GOLDEN_PATH_SCHEMA:
        raise GoldenPathError(f"{path}: schema must be {GOLDEN_PATH_SCHEMA}")
    rows = doc.get("paths")
    if not isinstance(rows, list):
        raise GoldenPathError(f"{path}: paths must be a list")
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise GoldenPathError(f"paths[{index}] must be a mapping")
        unknown = sorted(set(row) - _PATH_FIELDS)
        if unknown:
            raise GoldenPathError(f"paths[{index}] unknown fields: {unknown}")
        path_id = str(row.get("id") or "").strip()
        case_id = str(row.get("case_id") or "").strip()
        if not path_id or not case_id:
            raise GoldenPathError(f"paths[{index}] requires id and case_id")
        if path_id in seen:
            raise GoldenPathError(f"duplicate golden path id: {path_id}")
        seen.add(path_id)
        trial = row.get("trial_draft_recipes", False)
        forbidden = row.get("forbidden_effects") or []
        if not isinstance(trial, bool):
            raise GoldenPathError(f"{path_id}: trial_draft_recipes must be a boolean")
        if not isinstance(forbidden, list) or not all(isinstance(x, str) for x in forbidden):
            raise GoldenPathError(f"{path_id}: forbidden_effects must be a list of strings")
        out.append({
            "id": path_id,
            "title": str(row.get("title") or path_id),
            "case_id": case_id,
            "trial_draft_recipes": trial,
            "forbidden_effects": list(forbidden),
            "stop_before": str(row.get("stop_before") or ""),
        })
    return out


def find_golden_path(profile: Any, path_id: str) -> dict[str, Any]:
    for row in load_golden_paths(profile):
        if row["id"] == path_id:
            return row
    raise GoldenPathError(f"golden path not declared: {path_id}")


def declaration_sha256(declared: Mapping[str, Any]) -> str:
    """SHA-256 (hex) of one normalized declaration, without display-only fields."""
    body = {key: value for key, value in declared.items() if key not in _UNDIGESTED_FIELDS}
    # Order of forbidden_effects carries no meaning; reordering must not change the digest.
    body["forbidden_effects"] = sorted(str(item) for item in body.get("forbidden_effects") or [])
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_marker(declared: Mapping[str, Any]) -> dict[str, str]:
    """Run Spec ``golden_path`` value for one declared path."""
    return {
        "id": str(declared["id"]),
        "case_id": str(declared["case_id"]),
        "declaration_sha256": declaration_sha256(declared),
    }


def golden_path_selector(declared: Mapping[str, Any], lifecycle: str) -> dict[str, Any]:
    """Explicit selector for the path's Case under its current lifecycle."""
    case_id = str(declared["case_id"])
    if lifecycle == "draft":
        return {"ids": [case_id], "statuses": ["draft"]}
    if declared.get("trial_draft_recipes"):
        return {"ids": [case_id], "trial_draft_recipes": True}
    return {"ids": [case_id]}


def case_lifecycle(profile: Any, case_id: str) -> str:
    """Current lifecycle of one Case in the Registry, fail-closed."""
    from autophone.assets.promotion_identity import load_case_registry

    registry = load_case_registry(profile)
    key = str(case_id).upper()
    for row in registry.get("cases") or []:
        if isinstance(row, dict) and str(row.get("id") or "").upper() == key:
            return str(row.get("status") or "")
    raise ContractError(f"golden path Case {case_id} is not in the Case Registry")


def validate_golden_path_request(spec: Mapping[str, Any], profile: Any | None = None) -> None:
    """Submission checks for a Run Spec that carries ``golden_path``.

    The marker must name a declared path, the path's own Case and the current
    declaration digest; the Run must be a regression Run that selects exactly
    that Case with the selector the plan gives for the Case's lifecycle.
    """
    marker = spec.get(RUN_MARKER_FIELD)
    if marker is None:
        return
    if not isinstance(marker, Mapping):
        raise ContractError("golden_path must be an object")
    if str(spec.get("type") or "") != "regression":
        raise ContractError("golden_path is only valid on a regression Run")
    if spec.get("execution_plan"):
        raise ContractError("golden_path Run must not use an execution_plan")
    if spec.get("negative_control"):
        raise ContractError("golden_path Run must not carry a negative_control")
    if profile is None:
        from autophone.assets.apps import load_app_profile

        app = str(spec.get("app") or "")
        try:
            profile = load_app_profile(app)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            # Unknown App or unreadable Profile: a request error (HTTP 422), not a 500.
            raise ContractError(f"golden_path: App profile {app!r} cannot be loaded: {exc}") from exc
    path_id = str(marker.get("id") or "")
    try:
        declared = find_golden_path(profile, path_id)
    except GoldenPathError as exc:
        raise ContractError(f"golden_path: {exc}") from exc
    if str(marker.get("case_id") or "") != declared["case_id"]:
        raise ContractError(
            f"golden_path {path_id}: Case {marker.get('case_id')} does not belong to this path "
            f"(declared Case {declared['case_id']})"
        )
    digest = declaration_sha256(declared)
    if marker.get("declaration_sha256") != digest:
        raise ContractError(
            f"golden_path {path_id}: declaration changed "
            f"(submitted {marker.get('declaration_sha256')}, current {digest})"
        )
    selector = spec.get("case_selector") or {}
    broad = [key for key in _BROAD_SELECTOR_KEYS if selector.get(key)]
    if broad:
        raise ContractError(f"golden_path Run must not select by {broad}")
    lifecycle = case_lifecycle(profile, declared["case_id"])
    if lifecycle not in _SUBMITTABLE_LIFECYCLES:
        raise ContractError(
            f"golden_path {path_id}: Case {declared['case_id']} lifecycle is {lifecycle!r}; "
            f"only {sorted(_SUBMITTABLE_LIFECYCLES)} Cases can be submitted"
        )
    expected = golden_path_selector(declared, lifecycle)
    if _normalized_selector(selector) != _normalized_selector(expected):
        raise ContractError(
            f"golden_path {path_id}: case_selector must be {expected} for the Case's current "
            f"lifecycle, got {dict(selector)}"
        )


def _normalized_selector(selector: Mapping[str, Any]) -> dict[str, Any]:
    ids = selector.get("ids") or []
    if isinstance(ids, str):
        ids = [ids]
    statuses = selector.get("statuses") or []
    if isinstance(statuses, str):
        statuses = [statuses]
    return {
        "ids": [str(item) for item in ids],
        "statuses": sorted(str(item) for item in statuses),
        "trial_draft_recipes": bool(selector.get("trial_draft_recipes")),
    }


def run_binding(spec: Mapping[str, Any] | None, declared: Mapping[str, Any]) -> dict[str, Any]:
    """Whether a recorded Run Spec was submitted for this declared path.

    Returns ``{"state", "marker", "detail"}``.  ``matched`` requires the
    marker's id, Case and declaration digest to equal the current declaration.
    """
    marker = (spec or {}).get(RUN_MARKER_FIELD)
    if marker is None:
        return {"state": BINDING_ABSENT, "marker": None, "detail": "run carries no golden_path marker"}
    if not isinstance(marker, Mapping):
        return {"state": BINDING_INVALID, "marker": None, "detail": "golden_path marker is not an object"}
    recorded = dict(marker)
    if recorded.get("id") != declared["id"] or recorded.get("case_id") != declared["case_id"]:
        return {
            "state": BINDING_OTHER_PATH, "marker": recorded,
            "detail": f"run was submitted for golden path {recorded.get('id')} "
                      f"(Case {recorded.get('case_id')})",
        }
    if recorded.get("declaration_sha256") != declaration_sha256(declared):
        return {
            "state": BINDING_DECLARATION_CHANGED, "marker": recorded,
            "detail": "golden path declaration changed after the run was submitted",
        }
    return {"state": BINDING_MATCHED, "marker": recorded, "detail": f"golden_path={declared['id']}"}


def marked_for_case(profile: Any, spec: Mapping[str, Any] | None, case_id: str) -> bool:
    """Whether a Run Spec carries a marker matching a declared path of this Case."""
    marker = (spec or {}).get(RUN_MARKER_FIELD)
    if not isinstance(marker, Mapping) or str(marker.get("case_id") or "") != str(case_id):
        return False
    try:
        declared = find_golden_path(profile, str(marker.get("id") or ""))
    except GoldenPathError:
        return False
    return run_binding(spec, declared)["state"] == BINDING_MATCHED


__all__ = [
    "BINDING_ABSENT",
    "BINDING_DECLARATION_CHANGED",
    "BINDING_INVALID",
    "BINDING_MATCHED",
    "BINDING_OTHER_PATH",
    "GOLDEN_PATH_SCHEMA",
    "GoldenPathError",
    "RUN_MARKER_FIELD",
    "case_lifecycle",
    "declaration_sha256",
    "find_golden_path",
    "golden_path_selector",
    "golden_paths_path",
    "load_golden_paths",
    "marked_for_case",
    "run_binding",
    "run_marker",
    "validate_golden_path_request",
]
