"""Execution-side binding of a declared negative control (discrimination evidence).

A Run Spec with ``negative_control`` names one declaration in
``apps/<app>/discrimination/negative_controls.yaml`` and its digest.  Before the
Runner starts, ``bind_negative_control`` reloads the declaration from the App
assets, checks the digest, applies the declared perturbation to the Recipe the
Case would otherwise run (the frozen Draft Trial document, or the live Recipe
file), validates the perturbed steps against the App's static contract and
attaches them to the in-memory Case plan.  Nothing is written to Recipe files.

After execution ``attach_negative_control_records`` stores what actually ran:
the declaration identity, the perturbation, the base Recipe identity, whether
the executed steps equal the perturbed steps, and the Recipe step index that
failed.  The judgement lives in ``autophone.assets.negative_control``.
"""
from __future__ import annotations

import copy
import hashlib
from typing import Any, Iterable, Mapping

from autophone.assets.apps import AppProfile
from autophone.assets.negative_control import (
    NegativeControlError,
    declaration_sha256,
    find_negative_control,
    resolve_negative_control,
)
from autophone.assets.promotion_identity import (
    RECIPE_SOURCE_DRAFT,
    RECIPE_SOURCE_LIVE,
    current_case_binding,
    executed_recipe_identity,
    recipe_path,
)
from autophone.contracts import ContractError
from autophone.execution.engine.recipe import recipe_steps_digest

#: Private Case plan keys (never persisted).
NEGATIVE_CONTROL_RECORD_KEY = "_negative_control"
NEGATIVE_CONTROL_STEPS_KEY = "_negative_control_steps"


def _base_recipe(profile: AppProfile, case: Mapping[str, Any], key: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Steps and executed-Recipe identity of what the Case would run unperturbed."""
    from autophone.execution.engine.recipe import recipe_document_from_bytes

    trial_document = case.get("_trial_recipe_document")
    trial_identity = case.get("_trial_recipe_identity")
    if isinstance(trial_document, dict) and isinstance(trial_identity, dict):
        identity = executed_recipe_identity(
            profile,
            key,
            source=RECIPE_SOURCE_DRAFT,
            recipe_sha256=trial_identity.get("draft_recipe_sha256"),
            case_revision=trial_identity.get("case_revision"),
            reserved_external_id=trial_identity.get("reserved_external_id"),
        )
        return copy.deepcopy(trial_document["steps"]), identity
    if case.get("_trial_recipe_missing") is True:
        raise ContractError(f"negative control: Case {key} has no draft Recipe for this Trial")
    path = recipe_path(profile, key, source=RECIPE_SOURCE_LIVE)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ContractError(f"negative control: live Recipe of {key} is unreadable: {exc}") from exc
    document = recipe_document_from_bytes(data, path)
    revision, reserved = current_case_binding(profile, key)
    identity = executed_recipe_identity(
        profile,
        key,
        source=RECIPE_SOURCE_LIVE,
        recipe_sha256=hashlib.sha256(data).hexdigest(),
        case_revision=revision,
        reserved_external_id=reserved,
    )
    return copy.deepcopy(document["steps"]), identity


def _steps_binding_sha256(steps: Any) -> str | None:
    if not isinstance(steps, list):
        return None
    found: set[str] = set()
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        meta = step.get("_symbol_binding")
        if isinstance(meta, Mapping) and meta.get("binding_sha256") is not None:
            found.add(str(meta["binding_sha256"]))
    return next(iter(found)) if len(found) == 1 else None


def _binding_sha256_from_metadata(
    case: Mapping[str, Any] | None,
    *,
    record: Mapping[str, Any] | None = None,
    outcome: Any = None,
) -> str | None:
    """Resolve ``binding_sha256`` from executed outcome or bound Case/Recipe metadata."""
    if outcome is not None:
        outcome_val = getattr(outcome, "binding_sha256", None)
        if outcome_val is not None:
            return str(outcome_val)
        outcome_identity = getattr(outcome, "recipe_identity", None)
        if isinstance(outcome_identity, Mapping) and outcome_identity.get("binding_sha256") is not None:
            return str(outcome_identity["binding_sha256"])
    if isinstance(record, Mapping):
        if record.get("binding_sha256") is not None:
            return str(record["binding_sha256"])
        base_identity = record.get("base_recipe_identity")
        if isinstance(base_identity, Mapping) and base_identity.get("binding_sha256") is not None:
            return str(base_identity["binding_sha256"])
    if isinstance(case, Mapping):
        for key in ("binding_sha256", "_symbol_binding_sha256", "_binding_sha256"):
            if case.get(key) is not None:
                return str(case[key])
        for id_key in ("_trial_recipe_identity", "_recipe_identity", "recipe_identity"):
            identity = case.get(id_key)
            if isinstance(identity, Mapping) and identity.get("binding_sha256") is not None:
                return str(identity["binding_sha256"])
        for steps_key in (NEGATIVE_CONTROL_STEPS_KEY,):
            from_steps = _steps_binding_sha256(case.get(steps_key))
            if from_steps is not None:
                return from_steps
        trial_doc = case.get("_trial_recipe_document")
        if isinstance(trial_doc, Mapping):
            from_trial = _steps_binding_sha256(trial_doc.get("steps"))
            if from_trial is not None:
                return from_trial
    return None


def bind_negative_control(
    profile: AppProfile,
    cases: list[dict[str, Any]],
    request: Mapping[str, Any] | None,
    *,
    symbol_binding: Any | None = None,
    screen_asset_doc: Mapping[str, Any] | None = None,
    trial_draft_recipes: bool = False,
    confirm_trial_effects: bool = False,
    trial_data_isolation: str | None = None,
) -> list[dict[str, Any]]:
    """Attach the declared perturbation to the single selected Case, fail-closed."""
    if not request:
        return cases
    from autophone.assets.app_symbols import SymbolBindingError
    from autophone.execution.app_recipes import import_app_recipes
    from autophone.execution.engine.recipe import (
        RecipeError,
        expand_symbol_steps,
        operation_scope,
        recipe_has_unresolved_symbols,
        steps_binding_sha256,
        validate_recipe_for_app,
    )
    from autophone.paths import CODE_ROOT

    key = str(request.get("case_id") or "").upper()
    if [str(case.get("id") or "").upper() for case in cases] != [key]:
        raise ContractError(
            f"negative control Run must select exactly Case {key}; "
            f"selected {[case.get('id') for case in cases]}"
        )
    try:
        control = find_negative_control(profile, key, str(request.get("control_id") or ""))
    except NegativeControlError as exc:
        raise ContractError(f"negative control: {exc}") from exc
    digest = declaration_sha256(control)
    if digest != request.get("declaration_sha256"):
        raise ContractError(
            f"negative control {control['id']}: declaration changed after submission "
            f"(submitted {request.get('declaration_sha256')}, current {digest})"
        )
    case = dict(cases[0])
    base_steps, base_identity = _base_recipe(profile, case, key)
    try:
        if recipe_has_unresolved_symbols(base_steps):
            expanded_base = expand_symbol_steps(
                base_steps,
                symbol_binding=symbol_binding,
                screen_asset_doc=screen_asset_doc,
            )
            base_binding_sha256 = steps_binding_sha256(expanded_base)
        else:
            base_binding_sha256 = steps_binding_sha256(base_steps)
    except (RecipeError, SymbolBindingError) as exc:
        raise ContractError(
            f"negative control {control['id']}: symbol binding invalid: {exc}"
        ) from exc
    try:
        from autophone.assets.operations import load_operations

        resolved = resolve_negative_control(
            base_steps, control,
            operations=load_operations(profile) if control["perturbation"]["kind"] == "flip_witness" else None,
            app=profile.app,
        )
    except NegativeControlError as exc:
        raise ContractError(f"negative control {control['id']}: {exc}") from exc
    perturbed_steps = copy.deepcopy(resolved.steps)
    try:
        if recipe_has_unresolved_symbols(perturbed_steps):
            perturbed_steps = expand_symbol_steps(
                perturbed_steps,
                symbol_binding=symbol_binding,
                screen_asset_doc=screen_asset_doc,
            )
        perturbed_binding_sha256 = steps_binding_sha256(perturbed_steps)
    except (RecipeError, SymbolBindingError) as exc:
        raise ContractError(
            f"negative control {control['id']}: symbol binding invalid: {exc}"
        ) from exc
    if (
        base_binding_sha256 is not None
        and perturbed_binding_sha256 is not None
        and base_binding_sha256 != perturbed_binding_sha256
    ):
        raise ContractError(
            f"negative control {control['id']}: conflicting symbol binding_sha256 "
            f"between base ({base_binding_sha256}) and perturbed ({perturbed_binding_sha256})"
        )
    is_draft_trial = isinstance(case.get("_trial_recipe_document"), dict)
    effect_mode = "draft" if is_draft_trial else "active"
    effective_trial_draft = bool(trial_draft_recipes) if is_draft_trial else False
    effective_confirm_trial = bool(confirm_trial_effects) if is_draft_trial else False
    root = getattr(profile, "root", None) or CODE_ROOT
    import_app_recipes(profile.app, root=root)
    with operation_scope(profile.app, root=root):
        validate_recipe_for_app(
            perturbed_steps,
            profile,
            symbol_binding=symbol_binding,
            screen_asset_doc=screen_asset_doc,
            effect_mode=effect_mode,
            trial_draft_recipes=effective_trial_draft,
            confirm_trial_effects=effective_confirm_trial,
            trial_data_isolation=trial_data_isolation,
        )
    record = {
        "control_id": control["id"],
        "declaration_sha256": digest,
        **resolved.record(control, base_steps),
        "base_recipe_identity": base_identity,
        "base_steps_digest": recipe_steps_digest(base_steps),
        "perturbed_steps_digest": recipe_steps_digest(perturbed_steps),
    }
    bound_binding = (
        perturbed_binding_sha256
        or base_binding_sha256
        or _binding_sha256_from_metadata(case, record=record)
    )
    if bound_binding is not None:
        record["binding_sha256"] = bound_binding
        if isinstance(base_identity, dict):
            base_identity["binding_sha256"] = bound_binding
        case["_symbol_binding_sha256"] = bound_binding
    case[NEGATIVE_CONTROL_RECORD_KEY] = record
    case[NEGATIVE_CONTROL_STEPS_KEY] = copy.deepcopy(perturbed_steps)
    return [case]


def bind_operation_witnesses(profile: AppProfile, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Freeze witness contracts for the Recipe steps the Runner will execute."""
    from autophone.assets.operations import load_operations
    from autophone.assets.operation_witness import operation_identity

    if not cases:
        return cases
    operations = load_operations(profile)
    if not any(op.get("witness") for op in operations.values()):
        return cases
    bound = []
    for case in cases:
        key = str(case.get("id") or "").upper()
        if not isinstance(case.get("_trial_recipe_document"), dict) and not recipe_path(
            profile, key, source=RECIPE_SOURCE_LIVE,
        ).is_file():
            bound.append(case)
            continue
        steps = case.get(NEGATIVE_CONTROL_STEPS_KEY)
        if steps is None:
            steps, _identity = _base_recipe(profile, case, key)
        plans = {}
        for index, step in enumerate(steps):
            op = operations.get(step.get("operation"))
            if op and op.get("witness"):
                plans[index] = {"operation_identity": operation_identity(profile.app, op)}
        bound.append({**case, "_operation_witness_plans": plans,
                      "_operation_witness_steps_digest": recipe_steps_digest(steps)})
    return bound


def negative_control_case_ids(cases: Iterable[Mapping[str, Any]]) -> set[str]:
    return {
        str(case.get("id") or "").upper()
        for case in cases
        if isinstance(case.get(NEGATIVE_CONTROL_RECORD_KEY), dict)
    }


def attach_negative_control_records(
    outcomes: Iterable[Any],
    cases: Iterable[Mapping[str, Any]],
) -> None:
    """Record the executed perturbation and failure position on each bound outcome."""
    cases_list = list(cases)
    cases_by_id = {
        str(case.get("id") or "").upper(): case
        for case in cases_list
        if isinstance(case, Mapping) and case.get("id")
    }
    records = {
        str(case.get("id") or "").upper(): case[NEGATIVE_CONTROL_RECORD_KEY]
        for case in cases_list
        if isinstance(case.get(NEGATIVE_CONTROL_RECORD_KEY), dict)
    }
    for outcome in outcomes:
        case_key = str(getattr(outcome, "id", "")).upper()
        record = records.get(case_key)
        if record is None:
            continue
        executed = getattr(outcome, "recipe_steps_digest", None)
        failed = getattr(outcome, "failed_recipe_step", None)
        status = getattr(outcome, "status", "")
        fail_kind: str | None = (getattr(outcome, "fail_kind", "") or None) if status == "fail" else None
        if status == "fail" and not fail_kind:
            jv = getattr(outcome, "joint_verdict", None)
            if isinstance(jv, Mapping) and jv.get("fail_kind"):
                fail_kind = str(jv["fail_kind"])
            elif any(
                isinstance(a, Mapping) and a.get("verdict") == "fail"
                for a in (getattr(outcome, "api_assertions", None) or [])
            ):
                fail_kind = "api_assertion_failed"
            else:
                rb = getattr(outcome, "entity_readback", None)
                if isinstance(rb, Mapping) and rb.get("status") == "fail":
                    fail_kind = "entity_readback_mismatch"
        attached: dict[str, Any] = {
            **copy.deepcopy(record),
            "applied": bool(
                getattr(outcome, "exec_via", "") == "recipe"
                and executed is not None
                and executed == record["perturbed_steps_digest"]
            ),
            "failed_step_index": failed if status == "fail" else None,
            "fail_kind": fail_kind,
        }
        binding_sha256 = _binding_sha256_from_metadata(
            cases_by_id.get(case_key), record=record, outcome=outcome,
        )
        if binding_sha256 is not None:
            attached["binding_sha256"] = binding_sha256
        jv = getattr(outcome, "joint_verdict", None)
        if isinstance(jv, Mapping):
            attached["joint_verdict"] = copy.deepcopy(dict(jv))
            jv_planes = jv.get("planes")
            if isinstance(jv_planes, Mapping):
                attached["planes"] = copy.deepcopy(dict(jv_planes))
        else:
            api_asserts = getattr(outcome, "api_assertions", None) or []
            rb = getattr(outcome, "entity_readback", None)
            if api_asserts or isinstance(rb, Mapping):
                ui_st = str(getattr(outcome, "ui_status", "") or status)
                planes_summary: dict[str, str] = {
                    "ui": "pass" if ui_st == "pass" else ("skipped" if ui_st in ("blocked", "skipped") else "fail"),
                }
                if api_asserts:
                    planes_summary["api"] = (
                        "fail"
                        if any(isinstance(a, Mapping) and a.get("verdict") == "fail" for a in api_asserts)
                        else "pass"
                    )
                if isinstance(rb, Mapping):
                    planes_summary["readback"] = str(rb.get("status") or "missing")
                attached["planes"] = planes_summary
        outcome.negative_control = attached


__all__ = [
    "NEGATIVE_CONTROL_RECORD_KEY",
    "NEGATIVE_CONTROL_STEPS_KEY",
    "attach_negative_control_records",
    "bind_negative_control",
    "negative_control_case_ids",
]
