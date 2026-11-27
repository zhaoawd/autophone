"""Read-only business validity hints for Cases (BL-038 stage D, slice D2).

A hint marks a Case whose latest regression verdict is ``failed`` while its
structured classification or declared Fixtures name a campaign, coupon or
eligibility dependency.  Such a failure may come from an expired campaign, an
ineligible account, a product that does not take part, or a product defect
(usecase assetization §4.2); this module does not tell them apart and never
states a failure cause.

Rules (all constants below are part of the documented contract, §21 stage D):

* Only Cases whose lifecycle is in ``CASE_STATUSES`` are read.  Archived and
  deprecated Cases already left the actionable set (§7.1).
* Dependency signals come only from structured fields: the Case fields in
  ``CLASSIFICATION_FIELDS`` and the ``id`` / ``kind`` of the Case's entries in
  ``fixture_requirements.yaml``.  Free text (``item``, ``precondition``,
  ``module`` …) is not read.  ASCII terms match whole words of the lower-cased
  value split on non-alphanumerics; CJK terms match as substrings.
* The run signal is the latest regression verdict from the Run catalog; only
  ``RUN_STATUSES`` (assertion mismatch) qualifies.  ``error`` and ``blocked``
  verdicts point at the Rig or the Fixture and are not hints.

A hint does not change the Case, its readiness, its Automation Level or any
debt count.  The Case's current Automation Level and, for active Cases, the key
of the matching current ``failure`` Exception are carried as context so the
existing classification is referenced instead of recomputed.  The output is a
``knowledge-claims.v1`` document (``draft`` / ``static_hint`` / ``missing``)
plus a ``derivation`` block describing rules, skipped inputs and the reason for
an empty result.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from autophone.application.case_exceptions import overlay_key
from autophone.contracts import validate_or_raise

CLAIM_KIND = "business_validity_hint"
SOURCE = "business_validity_rule"
CASE_STATUSES = ("active", "draft")
RUN_STATUSES = ("failed",)
CLASSIFICATION_FIELDS = ("business_family", "family_id")
FIXTURE_FIELDS = ("id", "kind")
ASCII_TERMS = (
    "campaign", "promotion", "promo", "coupon", "voucher", "discount",
    "eligibility", "membership",
)
CJK_TERMS = ("活动", "优惠", "券", "满减", "折扣", "权益", "资格", "会员", "营销", "促销")
EVIDENCE_GAPS = (
    "observation_missing", "subject_build_key_unconfirmed", "validity_rebaseline_missing",
)


def load_case_rows(path: Path) -> list[Any]:
    """Case Registry rows; an absent registry has no Cases.  Parse errors propagate."""
    from autophone.execution.engine.cases_loader import load_cases

    return load_cases(path) if path.is_file() else []


def rules() -> dict[str, Any]:
    """The rule constants, echoed in every response for traceability."""
    return {
        "case_statuses": list(CASE_STATUSES),
        "run_statuses": list(RUN_STATUSES),
        "classification_fields": list(CLASSIFICATION_FIELDS),
        "fixture_fields": list(FIXTURE_FIELDS),
        "ascii_terms": list(ASCII_TERMS),
        "cjk_terms": list(CJK_TERMS),
    }


def matched_terms(value: Any) -> list[str]:
    """Campaign terms found in one structured value, in constant order."""
    if not isinstance(value, str) or not value.strip():
        return []
    text = value.casefold()
    words = set(re.findall(r"[a-z0-9]+", text))
    return [term for term in ASCII_TERMS if term in words] + [
        term for term in CJK_TERMS if term in value
    ]


def dependency_signals(
    case: dict[str, Any], fixtures: Iterable[Any] | None,
) -> list[dict[str, Any]]:
    """Structured fields of one Case that name a campaign / coupon / eligibility term."""
    signals: list[dict[str, Any]] = []
    for field in CLASSIFICATION_FIELDS:
        terms = matched_terms(case.get(field))
        if terms:
            signals.append({"source": "case", "field": field, "value": case[field], "terms": terms})
    for row in fixtures or []:
        if not isinstance(row, dict):
            continue
        for field in FIXTURE_FIELDS:
            terms = matched_terms(row.get(field))
            if terms:
                signals.append({
                    "source": "fixture_requirements", "fixture_id": str(row.get("id") or ""),
                    "field": field, "value": row[field], "terms": terms,
                })
    return signals


def _empty(app: str, reason: str, *, skipped: list[dict[str, Any]] | None = None,
           sources: dict[str, str] | None = None) -> dict[str, Any]:
    return _document(app, [], reason=reason, skipped=skipped or [], sources=sources or {})


def _document(app: str, claims: list[dict[str, Any]], *, reason: str | None,
              skipped: list[dict[str, Any]], sources: dict[str, str]) -> dict[str, Any]:
    core = {"schema_version": "v1", "app": app, "claims": claims}
    validate_or_raise("knowledge-claims.v1.json", core)
    return {**core, "derivation": {
        "kind": CLAIM_KIND, "rules": rules(), "sources": sources,
        "reason": reason, "skipped": skipped,
    }}


def propose_business_validity_candidates(
    app: str,
    cases: Any,
    *,
    case_verdicts: dict[str, dict[str, Any]] | None,
    fixture_requirements: dict[str, Any] | None = None,
    build_label: str | None = None,
    case_id: str | None = None,
) -> dict[str, Any]:
    """Derive draft validity hints; never raises on missing fields.

    ``cases`` is the Case Registry list.  ``case_verdicts`` maps Case ID to the
    latest regression verdict (``RunCatalog.case_verdicts``); ``None`` means
    the Run catalog could not be read and yields an empty result with a reason.
    ``fixture_requirements`` is ``load_fixture_requirements`` output.
    """
    sources = {"case_registry": "loaded", "run_verdicts": "loaded", "fixture_requirements": "absent"}
    if not isinstance(cases, list):
        sources["case_registry"] = "invalid"
        return _empty(app, "case_registry_invalid", sources=sources)
    if not isinstance(case_verdicts, dict):
        sources["run_verdicts"] = "unavailable"
        return _empty(app, "run_verdicts_unavailable", sources=sources)
    fixture_cases: dict[str, Any] = {}
    if isinstance(fixture_requirements, dict) and fixture_requirements.get("present"):
        invalid = any(
            (issue or {}).get("code") == "fixture_requirements_invalid_document"
            for issue in fixture_requirements.get("issues") or []
        )
        sources["fixture_requirements"] = "invalid" if invalid else "loaded"
        if not invalid and isinstance(fixture_requirements.get("cases"), dict):
            fixture_cases = fixture_requirements["cases"]

    claims: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            skipped.append({"index": index, "reason": "case_row_invalid"})
            continue
        cid = str(case.get("id") or "").strip()
        if not cid:
            skipped.append({"index": index, "reason": "case_id_missing"})
            continue
        if cid in seen:
            skipped.append({"index": index, "case_id": cid, "reason": "case_id_duplicate"})
            continue
        seen.add(cid)
        status = case.get("status")
        if status not in CASE_STATUSES or (case_id is not None and cid != case_id):
            continue
        verdict = case_verdicts.get(cid)
        if not isinstance(verdict, dict) or verdict.get("status") not in RUN_STATUSES:
            continue
        signals = dependency_signals(case, fixture_cases.get(cid.upper()))
        if not signals:
            continue
        verdict_status = str(verdict["status"])
        run_id = verdict.get("run_id")
        run_refs = [run_id] if isinstance(run_id, str) and run_id.strip() else []
        identity = json.dumps([app, CLAIM_KIND, cid], ensure_ascii=False, separators=(",", ":"))
        fields = "、".join(sorted({
            signal["field"] if signal["source"] == "case" else f"Fixture {signal['fixture_id']}.{signal['field']}"
            for signal in signals
        }))
        claims.append({
            "id": "validity-claim-" + hashlib.sha256(identity.encode()).hexdigest(),
            "status": "draft",
            "kind": CLAIM_KIND,
            "statement": (
                f"Case {cid} 的 {fields} 标明依赖活动、券或资格类条件，最近一次回归裁决为 {verdict_status}。"
                "该失败可能来自活动失效、账号无资格、商品不参与或产品缺陷，本提示不区分原因；"
                "需要先做有效性重基线再归因。"
            ),
            "scope": {"app": app, "case_id": cid, "state": "unknown", "subject_build_key": None},
            "evidence_refs": [],
            "evidence_level": "static_hint",
            "evidence_state": "missing",
            "evidence_gaps": list(EVIDENCE_GAPS),
            "provenance": {
                "source": SOURCE,
                "signals": signals,
                "verdict": {
                    "status": verdict_status, "run_id": run_refs[0] if run_refs else None,
                    "ended_at": verdict.get("ended_at"),
                },
                "run_refs": run_refs,
            },
            "context": {
                "build_label": build_label,
                "case_status": status,
                "automation_level": str(case.get("automation") or "") or None,
                "exception_key": (
                    overlay_key(cid, "failure", verdict_status) if status == "active" else None
                ),
                "changes_readiness": False,
            },
        })
    reason = None if claims else "no_matching_case"
    return _document(app, claims, reason=reason, skipped=skipped, sources=sources)
