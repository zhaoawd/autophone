"""Pure Plane Evaluation over frozen API evidence.

The module does not open capture sessions, write files, or mutate runner
objects.  Capture adapters freeze evidence first and apply the returned values
to their compatibility models afterwards.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class CasePlaneInput:
    case_id: str
    ui_status: str
    context: Mapping[str, Any]
    entity_binding: Mapping[str, Any] | None = None
    readback_record: Mapping[str, Any] | None = None
    resolved_entity: Any | None = None
    ui_fail_kind: str = ""


@dataclass(frozen=True)
class ApiAssertionResult:
    status: str | None
    assertions: tuple[Mapping[str, Any], ...]


class ApiAssertionEngine(Protocol):
    """Adapter seam for one assertion implementation."""

    def evaluate(
        self,
        case_id: str,
        calls: Sequence[Mapping[str, Any]],
        context: Mapping[str, Any],
    ) -> ApiAssertionResult: ...

    def summarize_call(self, call: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class CasePlaneResult:
    case_id: str
    ui_status: str
    status: str
    api_status: str | None
    assertions: tuple[Mapping[str, Any], ...]
    calls: tuple[Mapping[str, Any], ...]
    joint_verdict: Mapping[str, Any] | None = None
    fail_kind: str = ""
    detail: str = ""


@dataclass(frozen=True)
class PlaneEvaluationResult:
    cases: tuple[CasePlaneResult, ...]
    calls: int
    attributed: int
    asserted_cases: int
    assertions: int
    failed_assertions: int


def synthesize_status(ui_status: str, api_status: str | None) -> str:
    """Combine UI and API verdicts without converting non-execution into failure."""
    if api_status is None:
        return ui_status
    if ui_status in ("error", "skipped", "blocked"):
        return ui_status
    if ui_status == "fail" or api_status == "fail":
        return "fail"
    return "pass" if api_status == "pass" else ui_status


def evaluate_api_plane(
    cases: Sequence[CasePlaneInput],
    calls: Sequence[Mapping[str, Any]],
    engine: ApiAssertionEngine,
    *,
    api_captured: bool = True,
) -> PlaneEvaluationResult:
    """Evaluate immutable calls and return immutable per-case results."""
    case_results: list[CasePlaneResult] = []
    asserted_cases = 0
    assertion_count = 0
    failed_assertions = 0

    for case in cases:
        attributed = tuple(
            call
            for call in calls
            if str(call.get("case_id") or "") == case.case_id and not call.get("noise")
        )
        evaluated = (
            engine.evaluate(case.case_id, calls, case.context)
            if api_captured
            else ApiAssertionResult(status=None, assertions=())
        )
        assertions = tuple(dict(item) for item in evaluated.assertions)
        if evaluated.status is not None:
            asserted_cases += 1
            assertion_count += len(assertions)
            failed_assertions += sum(
                1 for assertion in assertions if assertion.get("verdict") == "fail"
            )
        joint_verdict: Mapping[str, Any] | None = None
        fail_kind = ""
        detail = ""
        if case.entity_binding is not None:
            from autophone.observation.joint_verdict import arbitrate_joint_verdict

            joint_verdict = arbitrate_joint_verdict(
                ui_status=case.ui_status,
                api_status=evaluated.status,
                api_assertions=[dict(item) for item in assertions],
                api_captured=api_captured,
                readback_record=case.readback_record,
                entity_binding=case.entity_binding,
                resolved_entity=case.resolved_entity,
            )
            if case.ui_status in ("error", "blocked", "skipped"):
                final_status = case.ui_status
            else:
                final_status = str(joint_verdict.get("status") or "fail")
                if final_status != "pass":
                    if case.ui_status == "fail" and case.ui_fail_kind:
                        fail_kind = case.ui_fail_kind
                    else:
                        fail_kind = str(joint_verdict.get("fail_kind") or "")
                        detail = str(joint_verdict.get("detail") or "")
        else:
            final_status = synthesize_status(case.ui_status, evaluated.status)
        case_results.append(
            CasePlaneResult(
                case_id=case.case_id,
                ui_status=case.ui_status,
                status=final_status,
                api_status=evaluated.status,
                assertions=assertions,
                calls=tuple(dict(engine.summarize_call(call)) for call in attributed),
                joint_verdict=joint_verdict,
                fail_kind=fail_kind,
                detail=detail,
            )
        )

    return PlaneEvaluationResult(
        cases=tuple(case_results),
        calls=len(calls),
        attributed=sum(1 for call in calls if call.get("case_id")),
        asserted_cases=asserted_cases,
        assertions=assertion_count,
        failed_assertions=failed_assertions,
    )
