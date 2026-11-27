"""Reviewable Family suggestions; generation never writes Case assets."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

from autophone.assets import business_chains as bc
from autophone.paths import resolve_state_root
from autophone.util import write_text_atomic

SCHEMA = "autophone.family-candidate/1"


def match_terms(text: str) -> set[str]:
    """Lower-cased ASCII words plus Chinese bigrams; the shared-term unit of suggestions."""
    words = set(re.findall(r"[a-z0-9_]+", text.lower()))
    for phrase in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(phrase[i:i + 2] for i in range(len(phrase) - 1))
    return words


_terms = match_terms


def case_match_terms(case: dict) -> set[str]:
    """Terms of the Case fields that Family and Operation suggestions compare."""
    return match_terms(json.dumps([case.get(key) for key in (
        "module", "item", "test_point", "precondition", "steps", "expects",
    )], ensure_ascii=False))


def suggest_families(case: dict, declaration: bc.BusinessChainsDeclaration) -> list[dict]:
    """Rank active declared Families by shared words / Chinese bigrams."""
    if not declaration.loaded:
        return []
    terms = case_match_terms(case)
    suggestions = []
    for family in declaration.families.values():
        if family.status != "active":
            continue
        shared = sorted(terms & _terms(f"{family.title} {family.rule or ''}"))
        suggestions.append({
            "family_id": family.id, "title": family.title, "source": "heuristic",
            "basis": "共享词项：" + "、".join(shared[:12]) if shared else "无共享词项；仅列出可选声明",
            "score": len(shared),
        })
    return sorted(suggestions, key=lambda row: (-row["score"], row["family_id"]))


def candidate_directory(app: str, *, state_root: Path | None = None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", app):
        raise ValueError("invalid App identifier")
    return (state_root or resolve_state_root()) / "family-candidates" / app


def save_candidates(profile: Any, cases: list[dict], *, state_root: Path | None = None) -> list[dict]:
    declaration = bc.load_business_chains(profile)
    if not declaration.loaded:
        raise ValueError("Family candidates require a valid Business Chain declaration")
    directory = candidate_directory(profile.app, state_root=state_root)
    result = []
    for case in cases:
        if case.get("status") not in {"draft", "active"}:
            continue
        for suggestion in suggest_families(case, declaration):
            if not suggestion["score"] or suggestion["family_id"] == bc.case_family_id(case):
                continue
            candidate_id = uuid.uuid4().hex
            candidate = {
                "schema": SCHEMA, "candidate_id": candidate_id, "app": profile.app,
                "case_id": case["id"], "case_revision": case["revision"],
                "case_status": case["status"], "original_family_id": bc.case_family_id(case),
                "declaration_sha256": declaration.sha256, **suggestion,
            }
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = directory / f"{candidate_id}.json"
            write_text_atomic(path, json.dumps(candidate, ensure_ascii=False, indent=2) + "\n")
            result.append({**candidate, "path": str(path)})
    return result


def load_candidate(app: str, candidate_id: str, *, state_root: Path | None = None) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", candidate_id):
        raise ValueError("invalid candidate_id")
    path = candidate_directory(app, state_root=state_root) / f"{candidate_id}.json"
    candidate = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(candidate, dict) or any(candidate.get(k) != v for k, v in {
        "schema": SCHEMA, "candidate_id": candidate_id, "app": app,
    }.items()):
        raise ValueError("invalid Family candidate identity")
    for field in ("case_id", "family_id", "declaration_sha256", "basis", "case_status"):
        if not isinstance(candidate.get(field), str) or not candidate[field]:
            raise ValueError(f"invalid candidate field: {field}")
    revision = candidate.get("case_revision")
    if type(revision) is not int or revision < 1:
        raise ValueError("invalid candidate Case revision")
    if not isinstance(candidate.get("original_family_id"), str):
        raise ValueError("invalid candidate original_family_id")
    if candidate["case_status"] not in {"draft", "active"}:
        raise ValueError("invalid candidate Case status")
    if candidate.get("source") not in {"ai", "heuristic"}:
        raise ValueError("invalid candidate source")
    return candidate


def import_snapshot(path: Path, declaration: bc.BusinessChainsDeclaration) -> dict:
    return {"workbook_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "declaration_sha256": declaration.sha256, "declaration_state": declaration.state}
