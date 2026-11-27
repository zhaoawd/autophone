"""Read-only duplicate path hints over Recipe step sequences (BL-038 stage D, slice D2).

Two kinds of hint, both deterministic comparisons of normalized steps:

* ``duplicate_path_use_operation``: an Operation whose ``steps`` are all
  structured steps (at least ``MIN_SEQUENCE_STEPS``) appears as a contiguous
  subsequence of one or more Recipes; the Recipe could call that Operation.
* ``duplicate_path_extraction``: the same contiguous subsequence of at least
  ``MIN_SEQUENCE_STEPS`` steps appears in at least ``MIN_DISTINCT_RECIPES``
  distinct Recipes (usecase assetization §9.2: "两条以上 … 重复同一目标路径").
  A window qualifies only when it contains at least one Action step (a window of
  Operation references already reuses Operations) and no assertion Action
  (``ASSERTION_ACTION_PREFIX``; an Operation does not carry Case assertions,
  §9.2).  Windows equal to an existing comparable Operation are left to the
  first kind.  Only maximal windows are reported: a window is dropped when a
  longer reported window contains it and covers all of its Recipes.

Normalization: a step is comparable when it is a mapping with ``action`` or an
Operation reference (``operation`` / ``op``).  ``op`` / ``with`` are read as
``operation`` / ``args``; empty ``args`` equals no ``args``; the keys in
``IGNORED_STEP_KEYS`` (waits, retries, tolerances) are removed.  The remaining
mapping is compared by canonical JSON.  A non-comparable step (free text, an
unknown mapping) ends a window.

Recipes are read from ``recipes/*.yaml`` (live) and ``recipes/drafts/*.yaml``
(draft); a live and a draft file of the same Recipe ID count as one Recipe.
Nothing is written and no Operation is generated.  The output is a
``knowledge-claims.v1`` document plus a ``derivation`` block.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from autophone.contracts import validate_or_raise

USE_OPERATION = "duplicate_path_use_operation"
EXTRACTION = "duplicate_path_extraction"
SOURCE = "recipe_step_comparison"
MIN_SEQUENCE_STEPS = 2
MIN_DISTINCT_RECIPES = 2
MATCHED_OPERATION_STATUSES = ("active", "draft")
ASSERTION_ACTION_PREFIX = "assert"
IGNORED_STEP_KEYS = frozenset({
    "wait_s", "timeout_s", "retries", "retry_delay_s", "poll_ms", "duration_s", "min_confidence",
})
EVIDENCE_GAPS = ("observation_missing", "subject_build_key_unconfirmed")


def rules() -> dict[str, Any]:
    return {
        "min_sequence_steps": MIN_SEQUENCE_STEPS,
        "min_distinct_recipes": MIN_DISTINCT_RECIPES,
        "matched_operation_statuses": list(MATCHED_OPERATION_STATUSES),
        "assertion_action_prefix": ASSERTION_ACTION_PREFIX,
        "ignored_step_keys": sorted(IGNORED_STEP_KEYS),
    }


def normalize_step(step: Any) -> dict[str, Any] | None:
    """Comparable form of one step, or ``None`` when the step cannot be compared."""
    if not isinstance(step, dict):
        return None
    body = dict(step)
    if "op" in body and "operation" not in body:
        body["operation"] = body.pop("op")
        if "with" in body and "args" not in body:
            body["args"] = body.pop("with")
    if "action" in body and "operation" in body:
        return None
    if "action" in body:
        if not isinstance(body["action"], str) or not body["action"].strip():
            return None
    elif "operation" in body:
        if not isinstance(body["operation"], str) or not body["operation"].strip():
            return None
    else:
        return None
    if body.get("args") in ({}, None):
        body.pop("args", None)
    return {key: value for key, value in body.items() if key not in IGNORED_STEP_KEYS}


def step_signature(step: Any) -> str | None:
    normalized = normalize_step(step)
    if normalized is None:
        return None
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _is_action(signature: str) -> bool:
    return "action" in json.loads(signature)


def _is_assertion(signature: str) -> bool:
    action = json.loads(signature).get("action")
    return isinstance(action, str) and action.startswith(ASSERTION_ACTION_PREFIX)


def load_recipe_sources(recipes_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read live and draft Recipe files; unreadable files are skipped with a reason."""
    sources: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for source, folder in (("live", recipes_dir), ("draft", recipes_dir / "drafts")):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.yaml")):
            relative = f"{'drafts/' if source == 'draft' else ''}{path.name}"
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
                skipped.append({"path": relative, "reason": "recipe_unreadable", "detail": type(exc).__name__})
                continue
            if not isinstance(doc, dict) or not isinstance(doc.get("steps"), list):
                skipped.append({"path": relative, "reason": "recipe_steps_missing"})
                continue
            recipe_id = doc.get("id") if isinstance(doc.get("id"), str) and doc["id"].strip() else path.stem
            sources.append({"recipe_id": str(recipe_id), "source": source, "path": relative, "steps": doc["steps"]})
    return sources, skipped


def load_operation_rows(path: Path) -> list[dict[str, Any]] | None:
    """Raw Operation rows; ``[]`` without a library, ``None`` when it cannot be read."""
    if not path.is_file():
        return []
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    rows = doc.get("operations") if isinstance(doc, dict) else None
    if rows is None:
        return []
    return rows if isinstance(rows, list) else None


def _occurrences(sequence: tuple[str, ...], recipe: dict[str, Any]) -> list[dict[str, Any]]:
    sigs = recipe["signatures"]
    size = len(sequence)
    found = []
    for start in range(len(sigs) - size + 1):
        if tuple(sigs[start:start + size]) == sequence:
            found.append(_locator(recipe, start, size))
    return found


def _locator(recipe: dict[str, Any], start: int, size: int) -> dict[str, Any]:
    return {
        "recipe_id": recipe["recipe_id"], "recipe_source": recipe["source"], "path": recipe["path"],
        "step_range": [start + 1, start + size],
    }


def _contains(longer: tuple[str, ...], shorter: tuple[str, ...]) -> bool:
    size = len(shorter)
    return any(longer[i:i + size] == shorter for i in range(len(longer) - size + 1))


def _claim(app: str, kind: str, identity: list[Any], statement: str, scope_ids: list[str],
           provenance: dict[str, Any], operation: str | None = None) -> dict[str, Any]:
    key = json.dumps([app, kind, *identity], ensure_ascii=False, separators=(",", ":"))
    claim = {
        "id": "duplicate-path-claim-" + hashlib.sha256(key.encode()).hexdigest(),
        "status": "draft",
        "kind": kind,
        "statement": statement,
        "scope": {"app": app, "recipe_ids": scope_ids, "state": "unknown", "subject_build_key": None},
        "evidence_refs": [],
        "evidence_level": "static_hint",
        "evidence_state": "missing",
        "evidence_gaps": list(EVIDENCE_GAPS),
        "provenance": {"source": SOURCE, **provenance},
    }
    if operation:
        claim["operation"] = operation
    return claim


def _range_text(occ: dict[str, Any]) -> str:
    start, end = occ["step_range"]
    label = "草稿 " if occ["recipe_source"] == "draft" else ""
    return f"{label}Recipe {occ['recipe_id']} 第 {start}–{end} 步"


def propose_duplicate_path_hints(
    app: str,
    recipes: Any,
    operations: Any,
    *,
    recipe_id: str | None = None,
    skipped: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Derive draft duplicate path hints; never raises on missing fields.

    ``recipes`` is ``load_recipe_sources`` output; ``operations`` the raw rows
    from ``load_operation_rows`` (``None`` = library unreadable, which yields an
    empty result with a reason, because "already an Operation" cannot be told).
    """
    skipped = list(skipped or [])
    sources = {"recipes": "loaded", "operations": "loaded"}
    if not isinstance(recipes, list):
        sources["recipes"] = "invalid"
        return _document(app, [], reason="recipes_invalid", skipped=skipped, sources=sources)
    if not isinstance(operations, list):
        sources["operations"] = "unavailable"
        return _document(app, [], reason="operation_library_unavailable", skipped=skipped, sources=sources)

    prepared: list[dict[str, Any]] = []
    for recipe in recipes:
        if not isinstance(recipe, dict) or not isinstance(recipe.get("steps"), list) or not recipe.get("recipe_id"):
            skipped.append({"path": (recipe or {}).get("path") if isinstance(recipe, dict) else None,
                            "reason": "recipe_steps_missing"})
            continue
        prepared.append({**recipe, "signatures": [step_signature(step) for step in recipe["steps"]]})

    comparable_ops: list[dict[str, Any]] = []
    for index, op in enumerate(operations):
        if not isinstance(op, dict) or not op.get("id") or not op.get("name"):
            skipped.append({"operation_index": index, "reason": "operation_row_invalid"})
            continue
        if op.get("status") not in MATCHED_OPERATION_STATUSES:
            continue
        steps = op.get("steps")
        if not isinstance(steps, list) or len(steps) < MIN_SEQUENCE_STEPS:
            continue
        sigs = [step_signature(step) for step in steps]
        if any(sig is None for sig in sigs):
            continue
        comparable_ops.append({"op": op, "sequence": tuple(sigs)})

    claims: list[dict[str, Any]] = []
    op_sequences = {item["sequence"] for item in comparable_ops}
    for item in comparable_ops:
        op, sequence = item["op"], item["sequence"]
        occurrences = [occ for recipe in prepared for occ in _occurrences(sequence, recipe)]
        if not occurrences:
            continue
        ids = sorted({occ["recipe_id"] for occ in occurrences})
        claims.append(_claim(
            app, USE_OPERATION, [str(op["id"])],
            "、".join(_range_text(occ) for occ in occurrences)
            + f"与 Operation {op['id']} {op['name']}（{op.get('status')}）的步骤序列相同，可改用该 Operation。",
            ids,
            {
                "operation": {"id": str(op["id"]), "name": str(op["name"]),
                              "status": op.get("status"), "revision": op.get("revision")},
                "step_signatures": list(sequence),
                "occurrences": occurrences,
            },
            operation=str(op["name"]),
        ))

    windows: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for recipe in prepared:
        sigs = recipe["signatures"]
        for start in range(len(sigs)):
            for end in range(start + MIN_SEQUENCE_STEPS, len(sigs) + 1):
                window = tuple(sigs[start:end])
                if any(sig is None for sig in window):
                    break
                windows.setdefault(window, []).append(_locator(recipe, start, end - start))
    groups = []
    for window, occurrences in windows.items():
        ids = {occ["recipe_id"] for occ in occurrences}
        if len(ids) < MIN_DISTINCT_RECIPES or window in op_sequences:
            continue
        if not any(_is_action(sig) for sig in window) or any(_is_assertion(sig) for sig in window):
            continue
        groups.append((window, occurrences, ids))
    groups.sort(key=lambda group: (-len(group[0]), group[0]))
    kept: list[tuple[tuple[str, ...], list[dict[str, Any]], set[str]]] = []
    for window, occurrences, ids in groups:
        if any(len(other) > len(window) and _contains(other, window) and ids <= other_ids
               for other, _, other_ids in kept):
            continue
        kept.append((window, occurrences, ids))
    for window, occurrences, ids in kept:
        claims.append(_claim(
            app, EXTRACTION, list(window),
            f"{len(ids)} 条 Recipe 重复同一 {len(window)} 步子序列（"
            + "、".join(_range_text(occ) for occ in occurrences)
            + "），且没有步骤相同的 Operation，可提炼为 Operation。",
            sorted(ids),
            {"step_signatures": list(window), "occurrences": occurrences},
        ))

    if recipe_id is not None:
        claims = [claim for claim in claims if recipe_id in claim["scope"]["recipe_ids"]]
    reason = None if claims else ("no_recipes" if not prepared else "no_duplicate_path")
    return _document(app, claims, reason=reason, skipped=skipped, sources=sources)


def _document(app: str, claims: list[dict[str, Any]], *, reason: str | None,
              skipped: list[dict[str, Any]], sources: dict[str, str]) -> dict[str, Any]:
    core = {"schema_version": "v1", "app": app, "claims": claims}
    validate_or_raise("knowledge-claims.v1.json", core)
    return {**core, "derivation": {
        "kinds": [USE_OPERATION, EXTRACTION], "rules": rules(), "sources": sources,
        "reason": reason, "skipped": skipped,
    }}
