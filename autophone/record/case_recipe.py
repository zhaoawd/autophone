"""Compile explicit recording-to-Case coverage into a frozen Draft Recipe (BL-061)."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from autophone.assets import store
from autophone.assets.apps import load_app_profile
from autophone.assets.cases import registry_write_lock
from autophone.assets.operations import load_operations
from autophone.assets.operations_store import operation_registry_write_lock
from autophone.assets.policy import assert_assets_writable
from autophone.assets.screens import load_screen_asset, validate_screen_asset
from autophone.contracts import ContractError, validate_or_raise
from autophone.execution.engine.cases_loader import load_cases
from autophone.execution.engine.recipe import operation_scope, validate_recipe_for_app
from autophone.execution.app_recipes import import_app_recipes
from autophone.paths import APP_ASSET_ROOT


class CaseRecipeError(ValueError):
    """An explicit coverage claim could not be verified."""


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def snapshot(app: str, case: dict) -> dict:
    """Keep the entire two lists; there is no positional steps/expects assumption."""
    result = {"app": app, "case_id": case["id"], "case_revision": case.get("revision"),
              "steps": copy.deepcopy(case.get("steps") or []),
              "expects": copy.deepcopy(case.get("expects") or []),
              "precondition": copy.deepcopy(case.get("precondition") or [])}
    return {**result, "sha256": digest(result)}


def _source_identity(op: dict) -> dict:
    return {"id": op["id"], "revision": op["revision"], "sha256": digest({
        key: value for key, value in op.items() if key not in {"status", "deprecate_reason"}
    })}


def _case(profile: Any, case_id: str) -> dict:
    case = next((c for c in load_cases(profile.cases_path()) if c.get("id") == case_id), None)
    if case is None:
        raise CaseRecipeError("case_not_found")
    return case


def _assets(profile: Any, recording_id: str, *, promoted_revision: int | None = None) -> tuple[dict, dict, dict]:
    from autophone.record.recordings import RecordingError, resolve_recording_id

    root = Path(getattr(profile, "root", None) or APP_ASSET_ROOT)
    try:
        directory = resolve_recording_id(recording_id, root=root)
        trace = json.loads((directory / "trace.json").read_text(encoding="utf-8"))
        if not isinstance(trace, dict) or not isinstance(trace.get("script"), dict):
            raise CaseRecipeError("recording_script_invalid")
        script = trace.get("script") or {}
        frozen = script.get("case_snapshot")
        if trace.get("app") != profile.app:
            raise CaseRecipeError("recording_app_mismatch")
        if not isinstance(frozen, dict) or script.get("source") != "case":
            raise CaseRecipeError("case_snapshot_missing: start a new Case-bound recording")
        case = _case(profile, frozen.get("case_id"))
        current = snapshot(profile.app, case)
        if frozen != current:
            rebound = {k: v for k, v in frozen.items() if k != "sha256"}
            if (promoted_revision != case.get("revision") or case.get("status") != "active"
                    or frozen.get("sha256") != digest(rebound)):
                raise CaseRecipeError("case_snapshot_mismatch")
            rebound["case_revision"] = promoted_revision
            if {**rebound, "sha256": digest(rebound)} != current:
                raise CaseRecipeError("case_snapshot_mismatch")
        if script.get("edited"):
            raise CaseRecipeError("edited_script_not_covered")
        registry = yaml.safe_load(profile.operations_path().read_text(encoding="utf-8")) or {}
        candidates = [op for op in registry.get("operations", [])
                      if (op.get("provenance") or {}).get("from") == recording_id
                      and op.get("status") in {"draft", "active"}]
        if len(candidates) != 1:
            raise CaseRecipeError("recording_operation_not_unique")
        op = candidates[0]
        if not profile.build_label or trace.get("build") != profile.build_label:
            raise CaseRecipeError("recording_build_mismatch")
        return case, op, trace
    except (OSError, KeyError, TypeError, RecordingError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise CaseRecipeError(f"recording_assets_invalid: {exc}") from exc


def context(app: str, recording_id: str, *, root: Path = APP_ASSET_ROOT) -> dict:
    profile = load_app_profile(app, root=root)
    case, op, _ = _assets(profile, recording_id)
    path = profile.screens_path(profile.build_label)
    screens = load_screen_asset(path) if path.exists() else {"screens": []}
    return {"case": snapshot(app, case), "operation": _source_identity(op),
            "steps": op["steps"], "screens": [s["id"] for s in screens["screens"]
                                              if s.get("status") == "active"],
            "assertion_operations": [o["name"] for o in load_operations(profile).values()
                                     if o.get("case_assertion") is True and o.get("status") == "active"]}


ASSERTION_ACTIONS = frozenset({"assert_screen", "assert_present", "assert_present_enhanced",
                               "assert_absent", "assert_remains_absent", "assert_region_nonempty"})


def compile_recipe(profile: Any, recording_id: str, payload: dict, *, promoted_revision: int | None = None) -> dict:
    """Pure construction after authoritative reads; caller owns publication and locks."""
    try:
        validate_or_raise("record-case-binding.v1.json", payload)
    except ContractError as exc:
        raise CaseRecipeError(f"binding_contract_invalid: {exc}") from exc
    case, op, trace = _assets(profile, recording_id, promoted_revision=promoted_revision)
    source = op.get("steps")
    if not isinstance(source, list) or not source or not all(isinstance(s, dict) for s in source):
        raise CaseRecipeError("source_steps_not_typed_ir")
    if op.get("params"):
        raise CaseRecipeError("parameterized_source_not_supported")
    frozen = copy.deepcopy(trace["script"]["case_snapshot"])
    if (not isinstance(frozen["case_revision"], int) or isinstance(frozen["case_revision"], bool)
            or frozen["case_revision"] < 1):
        raise CaseRecipeError("case_revision_invalid")
    if not frozen["steps"] or not all(isinstance(s, str) and s.strip() for s in frozen["steps"]):
        raise CaseRecipeError("case_steps_empty")
    if not frozen["expects"] or not all(isinstance(s, str) and s.strip() for s in frozen["expects"]):
        raise CaseRecipeError("case_expects_empty")
    # Each original Case step must have an ordered, non-overlapping explicit action group.
    groups = payload["step_bindings"]
    if [g["case_step_index"] for g in groups] != list(range(len(frozen["steps"]))):
        raise CaseRecipeError("case_steps_not_covered")
    last = -1
    for group in groups:
        indices = group["step_indices"]
        if indices != sorted(set(indices)) or min(indices) <= last or max(indices) >= len(source):
            raise CaseRecipeError("step_binding_invalid")
        last = max(indices)
    bindings = payload["bindings"]
    if sorted(b["expect_index"] for b in bindings) != list(range(len(frozen["expects"]))):
        raise CaseRecipeError("case_expects_not_covered")
    operations = load_operations(profile)
    assertions_by_step: dict[int, list[dict]] = {}
    screen_hashes: dict[str, str] = {}
    for binding in bindings:
        position = binding["after_step"]
        if position >= len(source):
            raise CaseRecipeError("assertion_position_out_of_range")
        assertion = copy.deepcopy(binding["assertion"])
        if assertion.get("action") not in ASSERTION_ACTIONS:
            candidate = operations.get(assertion.get("operation"))
            if not candidate or candidate.get("case_assertion") is not True or candidate.get("status") != "active":
                raise CaseRecipeError("not_a_case_assertion")
        if assertion.get("action") == "assert_screen":
            # Host supplies asset identity, never a client-provided path or digest.
            if set(assertion) != {"action", "screen_id"}:
                raise CaseRecipeError("screen_assertion_fields_invalid")
            screen_path = profile.screens_path(profile.build_label)
            raw = screen_path.read_bytes()
            screen_doc = yaml.safe_load(raw)
            validate_screen_asset(screen_doc, path=screen_path)
            if str(screen_doc["meta"]["build"]) != profile.build_label:
                raise CaseRecipeError("screen_build_mismatch")
            sid = assertion["screen_id"]
            if not any(s.get("id") == sid and s.get("status") == "active" for s in screen_doc["screens"]):
                raise CaseRecipeError("screen_not_active")
            sha = hashlib.sha256(raw).hexdigest()
            assertion.update(screen_asset=str(screen_path.relative_to(profile.root)), screen_sha256=sha)
            screen_hashes[sid] = sha
        assertions_by_step.setdefault(position, []).append({"expect_index": binding["expect_index"],
                                                            "assertion": assertion})
    steps: list[dict] = []
    action_map: list[int] = []
    checkpoints: list[dict] = []
    for index, action in enumerate(source):
        action_map.append(len(steps))
        steps.append(copy.deepcopy(action))
        for assertion in sorted(assertions_by_step.get(index, []), key=lambda a: a["expect_index"]):
            checkpoints.append({"expect_index": assertion["expect_index"], "recipe_step": len(steps)})
            steps.append(assertion["assertion"])
    with operation_scope(profile.app, root=profile.root):
        validate_recipe_for_app(steps, profile)
    refs: dict[str, dict] = {}

    def bind_references(items: list) -> None:
        for step in items:
            name = step.get("operation") if isinstance(step, dict) else None
            if name and name not in refs:
                definition = operations.get(name)
                if definition is None:
                    raise CaseRecipeError(f"operation_definition_missing:{name}")
                refs[name] = _source_identity(definition)
                bind_references(definition.get("steps") or [])

    bind_references(steps)
    declaration = {"version": 1, "app": profile.app, "recording_id": recording_id,
                   "build_label": profile.build_label, "case": frozen, "operation": _source_identity(op),
                   "binding": copy.deepcopy(payload), "action_map": action_map,
                   "checkpoints": checkpoints, "screen_sha256": screen_hashes,
                   "recording_sha256": digest(trace), "operation_refs": refs}
    if promoted_revision is not None:
        declaration["promoted_case_revision"] = promoted_revision
    return {"id": case["id"], "status": "draft",
            "provenance": {"source": "recording", "from": recording_id},
            "automates_case": declaration, "steps": steps}


def attribution_gaps(profile: Any, document: dict) -> list[str]:
    """Recompile the claim; declarations and client-reported passes grant no authority."""
    if "automates_case" not in document:
        if (document.get("provenance") or {}).get("source") == "recording":
            return ["automates_case_missing"]
        return []
    try:
        claim = document["automates_case"]
        if not isinstance(claim, dict) or claim.get("app") != profile.app:
            return ["automates_case_identity_mismatch"]
        promoted_revision = claim.get("promoted_case_revision")
        if promoted_revision is not None and (document.get("status") == "draft"
                or not isinstance(promoted_revision, int) or isinstance(promoted_revision, bool)):
            return ["automates_case_invalid_promotion_binding"]
        # Promotion can be evaluated in a fresh CLI/HTTP process before any Run
        # has populated this App's scoped executor registry.
        import_app_recipes(profile.app, root=profile.root)
        expected = compile_recipe(profile, claim["recording_id"], claim["binding"],
                                  promoted_revision=promoted_revision)
        if any(document.get(k) != expected[k] for k in ("id", "automates_case", "steps")):
            return ["automates_case_identity_mismatch"]
    except (ValueError, KeyError, TypeError, OSError, ImportError, RuntimeError, ContractError) as exc:
        return [f"automates_case_invalid:{exc}"]
    return []


def generate(app: str, recording_id: str, payload: dict, *, save: bool = False,
             root: Path = APP_ASSET_ROOT, source: str = "cli") -> dict:
    """Shared CLI/HTTP entry point. Save only a new Draft Recipe, never overwrite assets."""
    profile = load_app_profile(app, root=root)
    import_app_recipes(app, root=root)
    with operation_registry_write_lock(profile), registry_write_lock(profile.cases_path()):
        document = compile_recipe(profile, recording_id, payload)
        from autophone.util import now_iso
        document["provenance"].update(binding_source=source, bound_at=now_iso())
        path = profile.recipes_dir() / "drafts" / f"{document['id'].lower()}.yaml"
        if save:
            assert_assets_writable("generate recording case recipe", path=path)
            with store.path_write_lock(path):
                if path.exists():
                    raise CaseRecipeError("draft_recipe_exists: retain the existing version")
                store.atomic_write_text(path, yaml.safe_dump(document, allow_unicode=True, sort_keys=False))
    return {"ok": True, "saved": save, "case_id": document["id"], "path": str(path),
            "document": document, "coverage": {"expects": len(document["automates_case"]["checkpoints"]),
                                               "steps": len(payload["step_bindings"])}}
