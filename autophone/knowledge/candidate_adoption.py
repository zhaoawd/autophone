"""Adopt one BL-038 stage D candidate as a draft Knowledge Claim (slice D3).

A D1 / D2 candidate is a fresh derivation (``draft`` / ``static_hint`` /
``missing``) that is never persisted.  Adoption is an explicit user request
that turns one candidate into a ``draft`` Claim in the App's existing Claim
store (``apps/<app>/knowledge/claims.yaml``, the same store ``knowledge
compile --write-catalog`` writes).  The rules below are the contract
(usecase assetization §21 stage D, slice D3):

* Only Case-scoped candidates are adoptable: current Exception candidates
  (``exception-claim-``) and business validity hints (``validity-claim-``).
  Duplicate path hints (``duplicate-path-claim-``) are rejected: their
  follow-up asset (Extraction Demand / draft Operation) has no defined input.
* The caller names the candidate by ID and echoes the candidate body it
  reviewed.  The server derives the candidate again; a candidate that is no
  longer derived, or whose body differs from the echoed one, is rejected.  The
  written Claim is built from the server derivation, never from the echo.
* At least one Evidence Ref is required.  Each ref must name exactly one
  Observation in a compiled ``observations.json`` of this App whose
  ``case_id`` equals the candidate's Case.  Run references in provenance are
  not Observations and are never promoted to evidence.
* ``evidence_level`` / ``evidence_state`` / gaps come from the deterministic
  Evidence Verifier; callers cannot declare them.  A Verifier result of
  ``static_hint`` (no runtime Observation with complete correlation keys) is
  "no evidence" and the adoption is rejected.
* ``scope.subject_build_key`` is taken from the Observations only when all of
  them carry the same confirmed Build Scope; otherwise it stays ``null``.  The
  current Build is not asserted, so the Verifier runs without a current key.
* The Claim status is always ``draft``.  An ID already present in the store is
  rejected; existing Claims are never modified.  Draft → active is not part
  of adoption.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from autophone.contracts import validate_or_raise
from autophone.knowledge.evidence_verifier import verify

ADOPTABLE_PREFIXES = {
    "exception-claim-": "exception_candidates",
    "validity-claim-": "business_validity_candidates",
}
NOT_ADOPTABLE_PREFIXES = {"duplicate-path-claim-": "duplicate_path_hints"}
REQUEST_KEYS = frozenset({"candidate_id", "candidate", "evidence_refs"})
MAX_EVIDENCE_REFS = 64
MAX_TEXT = 512
OBSERVATIONS_GLOB = "*/*/observations.json"


class AdoptionError(Exception):
    """A rejected adoption; ``detail`` is the HTTP ``detail`` body."""

    def __init__(self, status_code: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = {"code": code, "message": message, **extra}


def rules() -> dict[str, Any]:
    """Rule constants echoed in every adoption response."""
    return {
        "adoptable_sources": sorted(ADOPTABLE_PREFIXES.values()),
        "not_adoptable_sources": sorted(NOT_ADOPTABLE_PREFIXES.values()),
        "min_evidence_refs": 1,
        "max_evidence_refs": MAX_EVIDENCE_REFS,
        "min_evidence_level": "observed",
        "evidence_scope": "observation app and case_id equal the candidate scope",
        "written_status": "draft",
    }


def candidate_source(candidate_id: str) -> str:
    for prefix, source in ADOPTABLE_PREFIXES.items():
        if candidate_id.startswith(prefix):
            return source
    for prefix, source in NOT_ADOPTABLE_PREFIXES.items():
        if candidate_id.startswith(prefix):
            raise AdoptionError(
                422, "candidate_kind_not_adoptable",
                f"{source} candidates have no defined adoption target", source=source,
            )
    raise AdoptionError(422, "candidate_kind_unknown", f"unknown candidate id: {candidate_id}")


def parse_request(body: Any) -> tuple[str, dict[str, Any], list[str]]:
    """Validate the request shape; returns (candidate_id, echoed candidate, refs)."""
    if not isinstance(body, dict):
        raise AdoptionError(422, "adoption_request_invalid", "request body must be an object")
    unknown = sorted(set(body) - REQUEST_KEYS)
    if unknown:
        raise AdoptionError(422, "adoption_request_invalid", f"unknown keys: {', '.join(unknown)}")
    candidate_id = body.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip() or len(candidate_id) > MAX_TEXT:
        raise AdoptionError(422, "adoption_request_invalid", "candidate_id must contain 1..512 characters")
    candidate = body.get("candidate")
    if not isinstance(candidate, dict) or candidate.get("id") != candidate_id:
        raise AdoptionError(
            422, "adoption_request_invalid", "candidate must be the reviewed candidate object with the same id",
        )
    raw_refs = body.get("evidence_refs")
    if raw_refs is None or raw_refs == []:
        raise AdoptionError(422, "evidence_refs_missing", "at least one Observation id is required")
    if not isinstance(raw_refs, list) or any(
        not isinstance(ref, str) or not ref.strip() or len(ref) > MAX_TEXT for ref in raw_refs
    ):
        raise AdoptionError(422, "adoption_request_invalid", "evidence_refs must be 1..512 character strings")
    refs = list(dict.fromkeys(ref.strip() for ref in raw_refs))
    if len(refs) > MAX_EVIDENCE_REFS:
        raise AdoptionError(422, "adoption_request_invalid", f"at most {MAX_EVIDENCE_REFS} evidence_refs")
    return candidate_id, candidate, refs


def canonical_sha256(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def current_candidate(document: dict[str, Any], candidate_id: str, echoed: dict[str, Any]) -> dict[str, Any]:
    """The server-derived candidate; rejects a vanished or changed candidate."""
    matches = [row for row in document.get("claims") or [] if row.get("id") == candidate_id]
    if not matches:
        raise AdoptionError(
            409, "candidate_not_current",
            "the candidate is no longer derived; refresh the preview", candidate_id=candidate_id,
        )
    candidate = matches[0]
    if canonical_sha256(candidate) != canonical_sha256(echoed):
        raise AdoptionError(
            409, "candidate_changed",
            "the derived candidate differs from the reviewed one; refresh the preview",
            candidate_id=candidate_id,
        )
    if candidate.get("status") != "draft" or not (candidate.get("scope") or {}).get("case_id"):
        raise AdoptionError(422, "candidate_kind_not_adoptable", "only Case-scoped draft candidates are adoptable")
    return candidate


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def resolve_observations(
    run_root: Path, app: str, case_id: str, refs: Iterable[str],
) -> list[dict[str, Any]]:
    """Resolve refs to Observations of this App and Case, fail closed.

    Returns one ``{"observation", "path", "run_id"}`` entry per ref in order.
    """
    wanted = list(refs)
    found: dict[str, list[tuple[dict[str, Any], Path, Any]]] = {ref: [] for ref in wanted}
    for path in sorted(Path(run_root).glob(OBSERVATIONS_GLOB)):
        document = _read_json(path)
        if not isinstance(document, dict):
            continue
        for row in document.get("observations") or []:
            if isinstance(row, dict) and str(row.get("id")) in found:
                found[str(row.get("id"))].append((row, path, document.get("app")))
    unresolved = [ref for ref in wanted if not found[ref]]
    if unresolved:
        raise AdoptionError(422, "evidence_ref_unresolved", "Observation not found", refs=unresolved)
    ambiguous = [ref for ref in wanted if len(found[ref]) > 1]
    if ambiguous:
        raise AdoptionError(422, "evidence_ref_ambiguous", "Observation id appears in several Runs", refs=ambiguous)
    resolved: list[dict[str, Any]] = []
    other_app: list[str] = []
    other_case: list[str] = []
    for ref in wanted:
        row, path, document_app = found[ref][0]
        spec = _read_json(path.parent / "run-spec.json")
        spec_app = spec.get("app") if isinstance(spec, dict) else None
        if str(document_app or "") != app or (spec_app and str(spec_app) != app):
            other_app.append(ref)
            continue
        if str(row.get("case_id") or "") != case_id:
            other_case.append(ref)
            continue
        resolved.append({"observation": row, "path": path, "run_id": row.get("run_id")})
    if other_app:
        raise AdoptionError(422, "evidence_ref_app_mismatch", f"Observation does not belong to App {app}", refs=other_app)
    if other_case:
        raise AdoptionError(
            422, "evidence_ref_case_mismatch", f"Observation does not belong to Case {case_id}", refs=other_case,
        )
    return resolved


def _confirmed_build_key(rows: list[dict[str, Any]]) -> str | None:
    keys = set()
    for row in rows:
        scope = row.get("build_scope") or {}
        if scope.get("state") != "confirmed" or not scope.get("subject_build_key"):
            return None
        keys.add(str(scope["subject_build_key"]))
    return keys.pop() if len(keys) == 1 else None


def build_adopted_claim(
    candidate: dict[str, Any],
    resolved: list[dict[str, Any]],
    *,
    actor: str | None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (claim, verifier decision); rejects evidence the Verifier rates static_hint."""
    rows = [entry["observation"] for entry in resolved]
    claim = copy.deepcopy(candidate)
    claim["status"] = "draft"
    claim["evidence_refs"] = [str(row["id"]) for row in rows]
    claim["scope"] = {**(claim.get("scope") or {}), "subject_build_key": _confirmed_build_key(rows)}
    decision = verify(claim, rows, current_subject_build_key=None)
    if decision["evidence_level"] == "static_hint":
        raise AdoptionError(
            422, "evidence_insufficient",
            "the Evidence Verifier found no runtime Observation with complete correlation keys",
            gaps=decision["gaps"],
        )
    claim["evidence_level"] = decision["evidence_level"]
    claim["evidence_state"] = decision["evidence_state"]
    claim["evidence_gaps"] = list(decision["gaps"])
    provenance = dict(claim.get("provenance") or {})
    provenance["adoption"] = {
        "slice": "BL-038 D3",
        "adopted_at": now().isoformat(timespec="seconds"),
        "actor": actor or None,
        "candidate_sha256": canonical_sha256(candidate),
        "evidence": [
            {"observation_id": str(entry["observation"]["id"]), "run_id": entry["run_id"],
             "type": entry["observation"].get("type")}
            for entry in resolved
        ],
    }
    claim["provenance"] = provenance
    app = str((claim.get("scope") or {}).get("app") or "unknown")
    validate_or_raise("knowledge-claims.v1.json", {"schema_version": "v1", "app": app, "claims": [claim]})
    return claim, decision


__all__ = [
    "ADOPTABLE_PREFIXES", "AdoptionError", "NOT_ADOPTABLE_PREFIXES", "build_adopted_claim",
    "candidate_source", "canonical_sha256", "current_candidate", "parse_request",
    "resolve_observations", "rules",
]
