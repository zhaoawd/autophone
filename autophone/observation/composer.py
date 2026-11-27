"""Create a new Run Result from frozen Evidence and Plane Evaluation."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from .plane_evaluation import ApiAssertionEngine, CasePlaneInput, evaluate_api_plane
from .session import FrozenEvidence


@dataclass(frozen=True)
class ApiPlaneFinalization:
    """Result of evaluating API Evidence without changing the Runner value."""

    run_result: Any
    summary: Mapping[str, Any]


def compose_phase2_api_plane(
    run_result: Any,
    evaluation: Any,
    *,
    jsonl: str | None,
) -> Any:
    """Return a copied Phase 2 result carrying the evaluated API Plane."""

    composed = deepcopy(run_result)
    outcomes_by_id: dict[str, Any] = {}
    for outcome in getattr(composed, "outcomes", []) or []:
        if not getattr(outcome, "ui_status", ""):
            outcome.ui_status = outcome.status
        case_id = str(getattr(outcome, "id", "") or "")
        if case_id:
            outcomes_by_id[case_id] = outcome
    for case in evaluation.cases:
        outcome = outcomes_by_id.get(case.case_id)
        if outcome is None:
            continue
        outcome.api_assertions = list(case.assertions)
        outcome.api_calls = list(case.calls)
        if jsonl is not None:
            outcome.net_jsonl = jsonl
        outcome.status = case.status
        jv = getattr(case, "joint_verdict", None)
        if isinstance(jv, Mapping):
            outcome.joint_verdict = deepcopy(dict(jv))
            if case.status != "pass":
                case_fail_kind = str(getattr(case, "fail_kind", "") or "")
                case_detail = str(getattr(case, "detail", "") or "")
                if case_fail_kind and (
                    not getattr(outcome, "fail_kind", "")
                    or getattr(outcome, "ui_status", "") == "pass"
                ):
                    outcome.fail_kind = case_fail_kind
                if case_detail and (
                    not getattr(outcome, "detail", "")
                    or getattr(outcome, "ui_status", "") == "pass"
                ):
                    outcome.detail = case_detail
    return composed


class RunResultComposer:
    def __init__(self, api_engine: ApiAssertionEngine | None = None) -> None:
        self.api_engine = api_engine

    def compose(
        self,
        run_result: Mapping[str, Any],
        evidence: FrozenEvidence,
    ) -> dict[str, Any]:
        result = deepcopy(dict(run_result))
        result["evidence"] = evidence.to_dict()
        if self.api_engine is None:
            return result
        api = evidence.plane("api")
        calls = list(api.get("calls") or [])
        cases = [
            CasePlaneInput(
                case_id=str(case.get("id") or ""),
                ui_status=str(case.get("status") or "pending"),
                context=deepcopy(dict(case)),
            )
            for case in result.get("cases") or []
            if str(case.get("id") or "")
        ]
        evaluated = evaluate_api_plane(cases, calls, self.api_engine)
        by_id = {case.case_id: case for case in evaluated.cases}
        composed_cases = []
        for raw in result.get("cases") or []:
            case = deepcopy(dict(raw))
            plane = by_id.get(str(case.get("id") or ""))
            if plane is not None:
                case["status"] = plane.status
                case.setdefault("planes", {})["api"] = {
                    "verdict": plane.api_status or "not_asserted",
                    "assertions": [dict(item) for item in plane.assertions],
                    "calls": [dict(item) for item in plane.calls],
                }
                jv = getattr(plane, "joint_verdict", None)
                if isinstance(jv, Mapping):
                    case["joint_verdict"] = deepcopy(dict(jv))
            composed_cases.append(case)
        result["cases"] = composed_cases
        result.setdefault("planes", {})["api"] = {
            "verdict": "failed" if evaluated.failed_assertions else "evaluated",
            "calls": evaluated.calls,
            "attributed": evaluated.attributed,
            "asserted_cases": evaluated.asserted_cases,
            "assertions": evaluated.assertions,
            "failed_assertions": evaluated.failed_assertions,
        }
        if any(case.status == "fail" for case in evaluated.cases):
            result["status"] = "failed"
        return result
