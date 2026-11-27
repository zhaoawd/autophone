"""Execution-side binding of a golden path Run (BL-038).

A Run Spec with ``golden_path`` (``{id, case_id, declaration_sha256}``) was
submitted for one declared golden path.  Before the Runner starts,
``bind_golden_path`` reloads the declaration, checks the digest and that the
Run selected exactly the path's Case, and marks that Case's in-memory plan.
Only a marked Case gets a Fixture Readback plan when it runs its live Recipe
(``fixture_readback.bind_fixture_readback_plans``); ordinary regression Runs
carry no marker and execute unchanged.

For a live Recipe the bytes are read once here and frozen onto the plan: the
parsed steps run as the Case's Recipe for this Run (the same override path a
Draft Trial uses), and the digests of those bytes back the BL-186 execution
identity (``recipe_identity.RECIPE_READ_KEY``).  The Readback plan, the Body
and the recorded identity therefore come from one read; a later edit of the
live file cannot change what this Run executes.  A Draft Trial Case keeps its
frozen draft document and is only marked.
"""
from __future__ import annotations

import copy
import hashlib
from typing import Any, Mapping

from autophone.assets.apps import AppProfile
from autophone.assets.golden_paths import (
    GoldenPathError,
    declaration_sha256,
    find_golden_path,
)
from autophone.assets.promotion_identity import (
    RECIPE_SOURCE_LIVE,
    current_recipe_identity,
    recipe_path,
)
from autophone.contracts import ContractError
from autophone.execution.negative_control import NEGATIVE_CONTROL_RECORD_KEY
from autophone.execution.recipe_identity import RECIPE_READ_KEY, recipe_read_record

#: Private Case plan keys (never persisted).
GOLDEN_PATH_KEY = "_golden_path"
GOLDEN_PATH_LIVE_DOCUMENT_KEY = "_golden_path_live_recipe_document"


def bind_golden_path(
    profile: AppProfile,
    cases: list[dict[str, Any]],
    request: Mapping[str, Any] | None,
    *,
    symbol_binding: Any | None = None,
    screen_asset_doc: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Mark the golden path's Case and freeze its live Recipe, fail-closed."""
    from autophone.execution.engine.recipe import RecipeError, steps_binding_sha256

    if not request:
        return cases
    path_id = str(request.get("id") or "")
    try:
        declared = find_golden_path(profile, path_id)
    except GoldenPathError as exc:
        raise ContractError(f"golden path: {exc}") from exc
    digest = declaration_sha256(declared)
    if digest != request.get("declaration_sha256"):
        raise ContractError(
            f"golden path {path_id}: declaration changed after submission "
            f"(submitted {request.get('declaration_sha256')}, current {digest})"
        )
    key = str(declared["case_id"]).upper()
    if str(request.get("case_id") or "").upper() != key:
        raise ContractError(
            f"golden path {path_id}: Case {request.get('case_id')} does not belong to this path"
        )
    if [str(case.get("id") or "").upper() for case in cases] != [key]:
        raise ContractError(
            f"golden path Run must select exactly Case {key}; "
            f"selected {[case.get('id') for case in cases]}"
        )
    case = dict(cases[0])
    if case.get(NEGATIVE_CONTROL_RECORD_KEY) is not None:
        raise ContractError("golden path Run must not carry a negative control")
    record = {"id": declared["id"], "case_id": declared["case_id"], "declaration_sha256": digest}
    if isinstance(case.get("_trial_recipe_identity"), dict):
        # Draft Trial: the draft document is already frozen and Readback-bound.
        draft_record: dict[str, Any] = {**record, "recipe_source": "draft"}
        draft_binding = case.get("_symbol_binding_sha256")
        if draft_binding is None and isinstance(case.get("_trial_recipe_document"), dict):
            try:
                draft_binding = steps_binding_sha256(case["_trial_recipe_document"].get("steps"))
            except RecipeError as exc:
                raise ContractError(f"golden path: draft Recipe of {key} has invalid symbol binding: {exc}") from exc
        if draft_binding is not None:
            draft_record["binding_sha256"] = str(draft_binding)
        case[GOLDEN_PATH_KEY] = draft_record
        return [case]
    if case.get("_trial_recipe_missing") is True:
        # The Runner skips the Case; nothing to freeze or bind.
        return [case]
    live_record: dict[str, Any] = {**record, "recipe_source": RECIPE_SOURCE_LIVE}
    document, read = _freeze_live_recipe(
        profile,
        key,
        symbol_binding=symbol_binding,
        screen_asset_doc=screen_asset_doc,
    )
    if read.get("binding_sha256") is not None:
        live_record["binding_sha256"] = str(read["binding_sha256"])
        case["_symbol_binding_sha256"] = str(read["binding_sha256"])
    case[GOLDEN_PATH_KEY] = live_record
    case[GOLDEN_PATH_LIVE_DOCUMENT_KEY] = document
    case[RECIPE_READ_KEY] = read
    return [case]


def _freeze_live_recipe(
    profile: AppProfile,
    key: str,
    *,
    symbol_binding: Any | None = None,
    screen_asset_doc: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read, validate and freeze the live Recipe once (document, read record)."""
    from autophone.assets.app_symbols import SymbolBindingError
    from autophone.execution.app_recipes import import_app_recipes
    from autophone.execution.effects import EffectPolicyError
    from autophone.execution.engine.recipe import (
        RecipeError,
        expand_symbol_steps,
        operation_scope,
        recipe_document_from_bytes,
        recipe_has_unresolved_symbols,
        steps_binding_sha256,
        validate_recipe_for_app,
    )
    from autophone.paths import CODE_ROOT
    import yaml

    path = recipe_path(profile, key, source=RECIPE_SOURCE_LIVE)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ContractError(f"golden path: live Recipe of {key} is unreadable: {exc}") from exc
    try:
        document = recipe_document_from_bytes(data, path)
    except (RecipeError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ContractError(f"golden path: live Recipe of {key} is invalid: {exc}") from exc
    if str(document.get("id") or "").upper() != key:
        raise ContractError(f"golden path: live Recipe {path.name} id must equal {key}")
    # The Case binding and file must still be the current identity at freeze time.
    current = current_recipe_identity(profile, key, source=RECIPE_SOURCE_LIVE)
    if current is None or current.get("recipe_sha256") != hashlib.sha256(data).hexdigest():
        raise ContractError(f"golden path: live Recipe of {key} changed while it was frozen")
    root = getattr(profile, "root", None) or CODE_ROOT
    import_app_recipes(profile.app, root=root)
    frozen_doc = copy.deepcopy(document)
    binding_sha256: str | None = None
    try:
        steps = frozen_doc["steps"]
        if recipe_has_unresolved_symbols(steps):
            steps = expand_symbol_steps(
                steps,
                symbol_binding=symbol_binding,
                screen_asset_doc=screen_asset_doc,
            )
            frozen_doc["steps"] = steps
        with operation_scope(profile.app, root=root):
            validate_recipe_for_app(
                steps,
                profile,
                symbol_binding=symbol_binding,
                screen_asset_doc=screen_asset_doc,
                effect_mode="active",
            )
        binding_sha256 = steps_binding_sha256(steps)
    except (RecipeError, EffectPolicyError, SymbolBindingError) as exc:
        raise ContractError(f"golden path: live Recipe of {key} is invalid: {exc}") from exc
    read = recipe_read_record(profile, key.lower(), path, data, frozen_doc)
    if binding_sha256 is not None:
        read["binding_sha256"] = binding_sha256
    return frozen_doc, read


def golden_path_live_steps(case: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """Frozen live Recipe steps of a golden path Case, or None."""
    document = case.get(GOLDEN_PATH_LIVE_DOCUMENT_KEY)
    if not isinstance(document, dict):
        return None
    steps = document.get("steps")
    return copy.deepcopy(steps) if isinstance(steps, list) and steps else None


__all__ = [
    "GOLDEN_PATH_KEY",
    "GOLDEN_PATH_LIVE_DOCUMENT_KEY",
    "bind_golden_path",
    "golden_path_live_steps",
]
