from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from autophone.util import now_iso

OVERTURNS_NAME = "overturns.jsonl"


def overturns_path(run_root: Path) -> Path:
    return run_root / OVERTURNS_NAME


def append_overturn(
    run_root: Path,
    *,
    run_id: str,
    case_id: str,
    source: str,
    reason: str,
    evidence: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    if not str(run_id or "").strip():
        raise ValueError("run_id is required")
    if not str(case_id or "").strip():
        raise ValueError("case_id is required")
    if not str(reason or "").strip():
        raise ValueError("reason is required")
    event = {
        "schema_version": "v1",
        "event": "verdict_overturned",
        "run_id": str(run_id),
        "case_id": str(case_id),
        "source": str(source or "manual"),
        "reason": str(reason),
        "evidence": str(evidence) if evidence else None,
        "actor": str(actor) if actor else None,
        "created_at": now_iso(),
    }
    path = overturns_path(run_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, sort_keys=False) + "\n")
    return event


def load_overturns(run_root: Path) -> list[dict[str, Any]]:
    path = overturns_path(run_root)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == "verdict_overturned":
            events.append(event)
    return events
