"""Minimal Phase 5 verifier result and OCR-backed state checks.

The verifier is a signal layer: it answers whether a step progressed to a
business state, with evidence and confidence. M5 starts with OCR-only checks and
the same result shape that later AX/API fusion can fill.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

VERIFIER_SCHEMA = "autophone.verifier/1"
DEFAULT_CONFIDENCE_THRESHOLD = 0.75


class VerifierError(RuntimeError):
    """Base class for verifier failures."""


class VerifierLowConfidence(VerifierError):
    """Raised when a verifier result is not strong enough to pass the step."""


@dataclass
class VerifierResult:
    case_id: str
    step: str
    progressed: bool
    state: str
    confidence: float
    signals: dict[str, list[str]] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    schema: str = VERIFIER_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VerifierResult":
        return cls(
            schema=str(data.get("schema") or VERIFIER_SCHEMA),
            case_id=str(data.get("case_id") or ""),
            step=str(data.get("step") or ""),
            progressed=bool(data.get("progressed")),
            state=str(data.get("state") or ""),
            confidence=float(data.get("confidence") or 0.0),
            signals={
                str(k): [str(x) for x in (v or [])]
                for k, v in (data.get("signals") or {}).items()
            },
            evidence_refs=[str(x) for x in data.get("evidence_refs", []) or []],
            notes=[str(x) for x in data.get("notes", []) or []],
        )


def verifier_passed(result: VerifierResult,
                    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD) -> bool:
    return result.progressed and result.confidence >= threshold


def ensure_verifier_passed(
    result: VerifierResult,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> None:
    if verifier_passed(result, threshold):
        return
    reason = "not progressed" if not result.progressed else f"confidence {result.confidence:.2f} < {threshold:.2f}"
    raise VerifierLowConfidence(
        f"verifier failed for {result.case_id}/{result.step}/{result.state}: {reason}"
    )


def normalize_verifier_result(
    value: Any,
    *,
    case_id: str,
    step: str,
    state: str,
) -> VerifierResult:
    """Convert a custom ctx.verify_state return value into VerifierResult.

    Future concrete contexts can return VerifierResult directly. Older fake or
    runner-local contexts may return an ActionResult-like object; those are
    normalized as successful but low-detail OCR signals.
    """
    if isinstance(value, VerifierResult):
        if not value.case_id or not value.step or not value.state:
            return replace(
                value,
                case_id=value.case_id or case_id,
                step=value.step or step,
                state=value.state or state,
            )
        return value
    if isinstance(value, dict):
        return VerifierResult.from_dict(value)

    detail = getattr(value, "detail", None)
    shot_path = getattr(value, "shot_path", None)
    return VerifierResult(
        case_id=case_id,
        step=step,
        progressed=True,
        state=state,
        confidence=0.80,
        signals={"custom": [str(detail or f"verify_state({state})")]},
        evidence_refs=[str(shot_path)] if shot_path else [],
        notes=["normalized legacy verify_state return"],
    )


def verify_state_ocr(
    ctx: Any,
    *,
    case_id: str,
    step: str,
    state: str,
    assertions: tuple[str, ...],
    confidence: float = 0.80,
) -> VerifierResult:
    """Run an OCR assertion and return a VerifierResult.

    `ctx.assert_present` remains fail-closed: if OCR cannot prove the assertion,
    the underlying StepFailure propagates and the lifecycle run fails.
    """
    ret = ctx.assert_present(list(assertions))
    detail = getattr(ret, "detail", None)
    shot_path = getattr(ret, "shot_path", None)
    return VerifierResult(
        case_id=case_id,
        step=step,
        progressed=True,
        state=state,
        confidence=confidence,
        signals={"ocr": list(assertions)},
        evidence_refs=[str(shot_path)] if shot_path else [],
        notes=[str(detail)] if detail else [],
    )
