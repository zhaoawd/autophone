"""Case import batch receipts and intake progress (usecase assetization §21 stage B, slice B1).

A committed Web import writes one append-only receipt under
``<state root>/case-import-batches/<app>/<batch_id>.json``.  The receipt only
records what the commit did (Case IDs, counts, workbook digest); it is not a
second Case store.  The Case Registry stays the source of truth.

Batch progress is a read model: for every Case ID in the receipt it projects
the current Case state through ``build_app_knowledge_view`` (same Family and
preparation card rows as the App knowledge view), so readiness is never judged
twice.  A Case deprecated or removed after the import still yields a row.
Nothing here executes a Trial, writes a Case or calls AI.
"""
from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autophone.logging import get_logger
from autophone.paths import APP_ASSET_ROOT, resolve_state_root
from autophone.util import write_text_atomic

log = get_logger(__name__)

CASE_IMPORT_BATCH_SCHEMA = "autophone.case-import-batch/1"
CASE_IMPORT_PROGRESS_SCHEMA = "autophone.case-import-progress/1"
BATCH_DIRECTORY = "case-import-batches"
DEFAULT_LIST_LIMIT = 10
MAX_LIST_LIMIT = 50

#: ``<UTC timestamp with microseconds>-<12 hex>``; lexical order is commit order.
_BATCH_ID = re.compile(r"\d{8}T\d{12}Z-[0-9a-f]{12}")
_APP = re.compile(r"[A-Za-z0-9_-]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
#: Batch rows for Cases that no longer exist in the Case Registry.
MISSING = "missing"
#: Batch rows whose knowledge view row carries an error.
UNAVAILABLE = "unavailable"


class CaseImportBatchError(ValueError):
    """A receipt exists but does not match schema ``autophone.case-import-batch/1``."""


def new_batch_id(now: datetime | None = None) -> str:
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"{moment.strftime('%Y%m%dT%H%M%S%f')}Z-{uuid.uuid4().hex[:12]}"


def batch_directory(app: str, *, state_root: Path | None = None) -> Path:
    if not isinstance(app, str) or not _APP.fullmatch(app):
        raise ValueError("invalid App identifier")
    return (state_root or resolve_state_root()) / BATCH_DIRECTORY / app


def is_batch_id(value: Any) -> bool:
    return isinstance(value, str) and _BATCH_ID.fullmatch(value) is not None


def write_batch_receipt(
    app: str,
    *,
    added: list[str],
    parsed_count: int,
    mode: str,
    actor: str | None = None,
    workbook_sha256: str | None = None,
    source_filename: str | None = None,
    family_selection_count: int = 0,
    state_root: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write one receipt atomically and return it with its ``path``.

    Callers skip this when ``added`` is empty; an empty batch has nothing to
    follow up.  A fresh ``batch_id`` is generated per call, so an existing
    receipt is never overwritten.
    """
    if not added:
        raise ValueError("a case import batch receipt needs at least one added Case")
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    batch_id = new_batch_id(moment)
    added_ids = [str(case_id) for case_id in added]
    receipt: dict[str, Any] = {
        "schema": CASE_IMPORT_BATCH_SCHEMA,
        "batch_id": batch_id,
        "app": app,
        "committed_at": moment.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "actor": actor or None,
        "source_filename": source_filename or None,
        "workbook_sha256": workbook_sha256 or None,
        "mode": mode,
        "added": added_ids,
        "counts": {
            "parsed": int(parsed_count),
            "added": len(added_ids),
            # Rows dropped by external_id deduplication (existing or earlier in the batch).
            "duplicates": max(int(parsed_count) - len(added_ids), 0),
            "family_selections": int(family_selection_count),
        },
    }
    directory = batch_directory(app, state_root=state_root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{batch_id}.json"
    write_text_atomic(path, json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    return {**receipt, "path": str(path)}


def load_batch_receipt(app: str, batch_id: str, *, state_root: Path | None = None) -> dict[str, Any]:
    """Read and validate one receipt.

    Raises ``FileNotFoundError`` for an unknown batch (including a malformed
    ``batch_id``, which cannot name a receipt) and ``CaseImportBatchError`` for
    a receipt that exists but is unreadable or does not match the schema.
    """
    if not is_batch_id(batch_id):
        raise FileNotFoundError(f"case import batch not found: {batch_id}")
    path = batch_directory(app, state_root=state_root) / f"{batch_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"case import batch not found: {batch_id}")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaseImportBatchError(f"receipt unreadable: {type(exc).__name__}: {exc}") from exc
    _validate_receipt(receipt, app=app, batch_id=batch_id)
    return receipt


def _validate_receipt(receipt: Any, *, app: str, batch_id: str) -> None:
    if not isinstance(receipt, dict):
        raise CaseImportBatchError("receipt must be a JSON object")
    for key, expected in (("schema", CASE_IMPORT_BATCH_SCHEMA), ("app", app), ("batch_id", batch_id)):
        if receipt.get(key) != expected:
            raise CaseImportBatchError(f"receipt {key} must be {expected!r}, got {receipt.get(key)!r}")
    if not isinstance(receipt.get("committed_at"), str) or not receipt["committed_at"]:
        raise CaseImportBatchError("receipt committed_at must be a non-empty string")
    added = receipt.get("added")
    if (
        not isinstance(added, list) or not added
        or not all(isinstance(item, str) and item for item in added)
    ):
        raise CaseImportBatchError("receipt added must be a non-empty list of Case IDs")
    counts = receipt.get("counts")
    if not isinstance(counts, dict) or not all(
        type(counts.get(key)) is int and counts[key] >= 0
        for key in ("parsed", "added", "duplicates")
    ):
        raise CaseImportBatchError("receipt counts must carry non-negative parsed/added/duplicates")
    if counts["added"] != len(added):
        raise CaseImportBatchError("receipt counts.added does not match added")
    digest = receipt.get("workbook_sha256")
    if digest is not None and not (isinstance(digest, str) and _SHA256.fullmatch(digest)):
        raise CaseImportBatchError("receipt workbook_sha256 must be null or 64 hex characters")
    for key in ("actor", "source_filename"):
        if receipt.get(key) is not None and not isinstance(receipt[key], str):
            raise CaseImportBatchError(f"receipt {key} must be null or a string")


def list_batches(
    app: str, *, limit: int = DEFAULT_LIST_LIMIT, state_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Newest first, at most ``limit`` receipts.

    A receipt that cannot be validated is still listed, as ``{batch_id,
    error: {code, message}}``, so a corrupt file is visible instead of silently
    dropped.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValueError(f"limit must be 1..{MAX_LIST_LIMIT}")
    directory = batch_directory(app, state_root=state_root)
    if not directory.is_dir():
        return []
    batch_ids = sorted(
        (path.stem for path in directory.glob("*.json") if is_batch_id(path.stem)),
        reverse=True,
    )[:limit]
    items: list[dict[str, Any]] = []
    for batch_id in batch_ids:
        try:
            receipt = load_batch_receipt(app, batch_id, state_root=state_root)
        except (FileNotFoundError, CaseImportBatchError) as exc:
            log.warning("case import batch receipt invalid app=%s batch_id=%s: %s", app, batch_id, exc)
            items.append({"batch_id": batch_id, "error": {
                "code": "case_import_batch_invalid", "message": str(exc),
            }})
            continue
        items.append(batch_summary(receipt))
    return items


def batch_summary(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "batch_id": receipt["batch_id"],
        "committed_at": receipt["committed_at"],
        "actor": receipt.get("actor"),
        "source_filename": receipt.get("source_filename"),
        "workbook_sha256": receipt.get("workbook_sha256"),
        "mode": receipt.get("mode"),
        "counts": dict(receipt["counts"]),
    }


def build_batch_progress(
    app: str,
    batch_id: str,
    *,
    state_root: Path | None = None,
    root: Path = APP_ASSET_ROOT,
    run_root: Path | None = None,
) -> dict[str, Any]:
    """One progress row per receipt Case, in receipt order.

    ``state_root`` locates receipts only; the view is built with the same
    arguments as ``GET /api/apps/{app}/knowledge/view``.  ``family`` and
    ``preparation`` are the App knowledge view row fields for the Case,
    unchanged.  A Case missing from the Case Registry yields a row
    with ``present: false``; a row the view could not build carries its
    ``error``.  Raises ``FileNotFoundError`` for an unknown batch or App and
    ``CaseImportBatchError`` for a corrupt receipt.
    """
    from autophone.knowledge.app_view import build_app_knowledge_view

    receipt = load_batch_receipt(app, batch_id, state_root=state_root)
    case_ids = list(dict.fromkeys(receipt["added"]))
    # Same arguments as the knowledge view endpoint; ``state_root`` only locates receipts.
    view = build_app_knowledge_view(app, case_ids=case_ids, root=root, run_root=run_root)
    by_id = {row["case_id"]: row for row in view["cases"]}
    rows = [_progress_row(case_id, by_id.get(case_id)) for case_id in case_ids]
    progress: dict[str, Any] = {
        "schema": CASE_IMPORT_PROGRESS_SCHEMA,
        "app": app,
        "executes": False,
        "batch": batch_summary(receipt),
        "target": view["target"],
        "summary": _progress_summary(rows),
        "cases": rows,
    }
    if "error" in view:
        progress["error"] = view["error"]
    return progress


def _progress_row(case_id: str, view_row: dict[str, Any] | None) -> dict[str, Any]:
    if view_row is None:
        return {
            "case_id": case_id, "present": False, "title": None, "status": None,
            "automation": None, "family": None, "preparation": None,
        }
    row = {
        "case_id": case_id,
        "present": True,
        "title": view_row.get("title") or "",
        "status": view_row.get("status"),
        "automation": view_row.get("automation") or "",
        "family": view_row["family"],
        "preparation": view_row.get("preparation"),
    }
    if "error" in view_row:
        row["error"] = view_row["error"]
    return row


def _progress_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_status = Counter(
        str(row["status"]) if row["present"] else MISSING for row in rows
    )
    by_preparation: Counter[str] = Counter()
    for row in rows:
        if not row["present"]:
            by_preparation[MISSING] += 1
        elif row["preparation"] is None:
            by_preparation[UNAVAILABLE] += 1
        else:
            by_preparation[str(row["preparation"]["status"])] += 1
    return {
        "cases_total": len(rows),
        "by_status": dict(sorted(by_status.items())),
        "by_preparation": dict(sorted(by_preparation.items())),
        "ready_for_trial": sum(
            1 for row in rows if (row["preparation"] or {}).get("ready_for_trial") is True),
        "runtime_verified": sum(
            1 for row in rows if (row["preparation"] or {}).get("runtime_verified") is True),
        "blocked": sum(1 for row in rows if (row["preparation"] or {}).get("blockers")),
    }
