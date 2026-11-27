"""Read-only draft Knowledge Claims derived from the current Exception view."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from autophone.contracts import validate_or_raise

CORE_KINDS = frozenset({"triage", "failure", "drift"})


def propose_exception_candidates(
    snapshot: dict[str, Any], *, key: str | None = None,
) -> dict[str, Any]:
    """Project current facts without inventing Observations or persisting Claims.

    A disposition is context, not evidence that an Exception was resolved. IDs
    use App plus the Exception key; a subsequent Run with the same failure kind
    updates the references without creating a different candidate identity.
    """
    app = snapshot["app"]
    claims = []
    for item in snapshot.get("exceptions", []):
        kind = item["kind"]
        if kind not in CORE_KINDS or (key is not None and item["key"] != key):
            continue
        case_id = item["case_id"]
        signature = item["signature"]
        if kind == "triage":
            statement = f"Case {case_id} 声明 ui_ready，但没有 Recipe。"
        elif kind == "failure":
            statement = f"Case {case_id} 最近一次回归裁决为 {signature}。"
        else:
            statement = f"Case {case_id} 当前区分力状态为 {signature}。"
        identity = json.dumps([app, item["key"]], ensure_ascii=False, separators=(",", ":"))
        run_id = (item.get("detail") or {}).get("run_id")
        claims.append({
            "id": "exception-claim-" + hashlib.sha256(identity.encode()).hexdigest(),
            "status": "draft",
            "kind": f"exception_{kind}",
            "statement": statement,
            "scope": {
                "app": app, "case_id": case_id,
                "state": "unknown", "subject_build_key": None,
            },
            "evidence_refs": [],
            "evidence_level": "static_hint",
            "evidence_state": "missing",
            "evidence_gaps": ["observation_missing", "subject_build_key_unconfirmed"],
            "provenance": {
                "source": "current_exception",
                "exception": {"key": item["key"], "kind": kind, "signature": signature},
                "run_refs": [run_id] if isinstance(run_id, str) and run_id.strip() else [],
            },
            "context": {
                "build_label": snapshot.get("build_label"),
                "muted": bool(item.get("muted")),
                "disposition": item.get("disposition"),
            },
        })
    result = {"schema_version": "v1", "app": app, "claims": claims}
    validate_or_raise("knowledge-claims.v1.json", result)
    return result
