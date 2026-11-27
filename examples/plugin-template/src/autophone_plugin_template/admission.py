"""Minimal `autophone.run_admission` provider example.

Loaded by `autophone.rig.plugins.validate_run_submission(spec)` inside
`ControlPlane.submit_assignment()` whenever a Run Spec carries
`trial_confirmation` or selects draft cases/recipes.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from autophone.contracts import ContractError


def _is_draft_trial(spec: Mapping[str, Any]) -> bool:
    selector = spec.get("case_selector") or {}
    if not isinstance(selector, Mapping):
        raise ContractError("trial_confirmation_selector_invalid: case_selector must be an object")
    if spec.get("type") != "regression":
        return False
    statuses = selector.get("statuses") or []
    return "draft" in statuses or bool(selector.get("trial_draft_recipes"))


def validate_run_admission(spec: Mapping[str, Any]) -> None:
    """Validate draft-trial confirmation fields fail-closed before enqueueing.

    Contract:
    - Called with a single positional mapping `spec` (the `run_spec` payload).
    - Return `None` on admission.
    - Raise `autophone.contracts.ContractError` to reject the submission before
      the Control Plane creates an Assignment.
    """
    markers = spec.get("trial_confirmation")
    trial = _is_draft_trial(spec)
    if not trial:
        if markers is not None:
            raise ContractError("trial_confirmation_not_applicable: requires explicit Draft Trial")
        return

    selector = spec.get("case_selector") or {}
    ids = selector.get("ids") or []
    if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
        raise ContractError("trial_confirmation_selector_invalid: pin unique case ids")

    if not isinstance(markers, list) or not markers:
        raise ContractError(
            f"trial_confirmation_required: {', '.join(str(item) for item in ids)}"
        )

    by_id: dict[str, Mapping[str, Any]] = {}
    for row in markers:
        if not isinstance(row, Mapping) or not row.get("case_id"):
            raise ContractError("trial_confirmation_case_mismatch: each marker requires case_id")
        case_id = str(row["case_id"])
        if case_id in by_id:
            raise ContractError("trial_confirmation_case_mismatch: duplicate case_id marker")
        by_id[case_id] = row

    expected_ids = {str(item) for item in ids}
    if set(by_id) != expected_ids:
        raise ContractError("trial_confirmation_case_mismatch: markers must match pinned case ids")

    for case_id in ids:
        marker = by_id[str(case_id)]
        if marker.get("acknowledged") is not True:
            raise ContractError(f"trial_confirmation_required: {case_id} must set acknowledged=true")
