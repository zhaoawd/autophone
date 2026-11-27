"""Static admission decisions for default active regression selection."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from autophone.assets.app_symbols import SymbolBindingError, load_symbol_binding_for_app
from autophone.assets.entity_binding import (
    case_has_entity_binding,
    validate_case_entity_binding,
)
from autophone.paths import CODE_ROOT
from autophone.execution.app_recipes import import_app_recipes
from autophone.execution.effects import (
    EffectMode,
    EffectPolicyError,
    REASON_EFFECT_COMPLETENESS_INSUFFICIENT,
    REASON_MALFORMED_SYMBOL_EFFECTS,
    REASON_MULTI_EFFECT_WILDCARD_REJECTED,
    REASON_TRIAL_DATA_UNISOLATED,
    REASON_TRIAL_DRAFT_REQUIRED,
    REASON_TRIAL_EFFECTS_NOT_CONFIRMED,
    REASON_UNRESOLVED_SYMBOL_EFFECT,
    profile_trial_data_isolation,
    strip_symbol_effect_metadata,
    validate_allowed_effects,
)
from autophone.execution.contracts import blocker
from autophone.execution.engine.recipe import (
    RecipeError,
    _validate_raw_symbol_step,
    expand_symbol_steps,
    load_recipe,
    operation_scope,
    recipe_has_unresolved_symbols,
    validate_recipe,
)
from autophone.execution.golden_path_binding import GOLDEN_PATH_LIVE_DOCUMENT_KEY

_EXPLICIT_EFFECT_BLOCKER_CODES: frozenset[str] = frozenset({
    REASON_EFFECT_COMPLETENESS_INSUFFICIENT,
    REASON_MULTI_EFFECT_WILDCARD_REJECTED,
    REASON_TRIAL_DRAFT_REQUIRED,
    REASON_TRIAL_EFFECTS_NOT_CONFIRMED,
    REASON_TRIAL_DATA_UNISOLATED,
    REASON_UNRESOLVED_SYMBOL_EFFECT,
    REASON_MALFORMED_SYMBOL_EFFECTS,
})


def _load_screen_asset_doc(profile: Any) -> dict[str, Any] | None:
    from autophone.assets.screens import load_screen_asset

    path: Path | None = None
    if callable(getattr(profile, "screens_path", None)):
        try:
            candidate = profile.screens_path()
            if candidate.exists():
                path = candidate
        except ValueError:
            path = None
    if path is None and getattr(profile, "profile_dir", None) is not None:
        fallback = Path(profile.profile_dir) / "screens.yaml"
        if fallback.exists():
            path = fallback
    if path is None:
        return None
    return load_screen_asset(path)


def _case_fixture_vars_for_admission(profile: Any, case_id: str) -> dict[str, Any] | None:
    from autophone.observation.frida import api_oracle

    fixtures_path = None
    if callable(getattr(profile, "api_fixtures_path", None)):
        candidate = profile.api_fixtures_path()
        if candidate.is_file():
            fixtures_path = candidate
    env_vars = api_oracle.fixture_context_vars_from_env()
    if fixtures_path is None and not env_vars:
        return None
    flat_vars = api_oracle.flat_fixture_vars(fixtures_path, case_id) if fixtures_path is not None else {}
    return {**env_vars, **flat_vars}


def _effect_blocker_code(exc: EffectPolicyError, *, source: str) -> str:
    if exc.code in _EXPLICIT_EFFECT_BLOCKER_CODES:
        return exc.code
    return f"invalid_{source}_recipe"


def admit_recipe_steps(
    profile: Any,
    steps: list[dict[str, Any]],
    *,
    mode: EffectMode = "active",
    trial_draft_recipes: bool = False,
    confirm_trial_effects: bool = False,
    trial_data_isolation: str | None = None,
    label: str | None = None,
    validate_shape: bool = True,
) -> dict[str, Any]:
    """Validate recipe steps against shape, allowed effects, and completeness gates.

    Shared by :func:`admit_case` and Wave 2 callers (``case lint``, ``recipe.py``,
    draft trial preflight). ``mode="active"`` always hard-blocks ``lower_bound``
    and ``unknown`` completeness with ``effect_completeness_insufficient``.
    """
    source = "draft" if mode == "draft" else "live"
    effective_label = label or f"app {getattr(profile, 'app', '<unknown>')} recipe"
    effective_isolation = (
        trial_data_isolation
        if trial_data_isolation is not None
        else profile_trial_data_isolation(profile)
    )
    effective_trial_draft = mode == "draft" and trial_draft_recipes is True
    effective_confirm_trial = mode == "draft" and confirm_trial_effects is True
    try:
        if not isinstance(steps, list) or not steps:
            raise RecipeError(f"{source} recipe has no non-empty steps")
        if validate_shape:
            validate_recipe(strip_symbol_effect_metadata(steps))
        validate_allowed_effects(
            steps,
            allowed_effects=set(getattr(profile, "allowed_effects", []) or []),
            label=effective_label,
            mode=mode,
            trial_draft_recipes=effective_trial_draft,
            confirm_trial_effects=effective_confirm_trial,
            trial_data_isolation=effective_isolation,
        )
    except EffectPolicyError as exc:
        return {
            "admitted": False,
            "blocker": blocker(
                "asset",
                _effect_blocker_code(exc, source=source),
                recovery=f"repair the {source} recipe: {exc}",
            ),
            "violations": list(exc.violations),
        }
    except (OSError, RecipeError, ValueError) as exc:
        return {
            "admitted": False,
            "blocker": blocker(
                "asset",
                f"invalid_{source}_recipe",
                recovery=f"repair the {source} recipe: {exc}",
            ),
            "violations": [],
        }
    return {"admitted": True, "blocker": None, "violations": []}


def admit_case(
    profile: Any,
    case: dict[str, Any],
    *,
    trial_draft_recipes: bool = False,
    confirm_trial_effects: bool = False,
    trial_data_isolation: str | None = None,
) -> dict[str, Any]:
    """Decide whether one case may enter active regression or a draft trial."""
    case_id = str(case.get("id") or "")
    if case.get("_trial_recipe_missing") is True:
        return {
            "case_id": case_id,
            "admitted": False,
            "blocker": blocker(
                "asset",
                "missing_draft_recipe",
                recovery="write the explicitly selected draft recipe before retrying",
            ),
        }

    trial_document = case.get("_trial_recipe_document")
    trial_identity = case.get("_trial_recipe_identity")
    # Golden path Run (BL-038): the live Recipe was read and frozen at binding;
    # admission validates those frozen steps, symmetric to the draft branch.
    frozen_live = case.get(GOLDEN_PATH_LIVE_DOCUMENT_KEY)
    is_draft_trial = trial_document is not None

    if not is_draft_trial and (
        case.get("status") != "active" or case.get("automation") != "ui_ready"
    ):
        return {"case_id": case_id, "admitted": True, "blocker": None}

    source = "draft" if is_draft_trial else "live"
    mode: EffectMode = "draft" if is_draft_trial else "active"
    recipe_path = profile.recipes_dir() / f"{case_id.lower()}.yaml"
    if source == "live" and frozen_live is None and not recipe_path.exists():
        return {
            "case_id": case_id,
            "admitted": False,
            "blocker": blocker(
                "asset",
                "missing_live_recipe",
                recovery="restore a live recipe or return the case to draft",
            ),
        }
    try:
        profile_root = getattr(profile, "root", None) or CODE_ROOT
        import_app_recipes(profile.app, root=profile_root)
        if source == "draft":
            if not isinstance(trial_document, dict) or not isinstance(trial_identity, dict):
                raise RecipeError("draft trial document and identity must both be mappings")
            steps = trial_document.get("steps")
            if not isinstance(steps, list) or not steps:
                raise RecipeError("draft trial document has no non-empty steps")
        elif frozen_live is not None:
            if not isinstance(frozen_live, dict):
                raise RecipeError("golden path frozen live document must be a mapping")
            steps = frozen_live.get("steps")
            if not isinstance(steps, list) or not steps:
                raise RecipeError("golden path frozen live document has no non-empty steps")
        else:
            steps = load_recipe(recipe_path)
            if recipe_has_unresolved_symbols(steps):
                for idx, step in enumerate(steps):
                    if isinstance(step, dict) and "symbol" in step:
                        _validate_raw_symbol_step(step, index=idx)
                symbol_binding = load_symbol_binding_for_app(
                    profile,
                    allow_default_build=True,
                )
                screen_asset_doc = _load_screen_asset_doc(profile)
                steps = expand_symbol_steps(
                    steps,
                    symbol_binding=symbol_binding,
                    screen_asset_doc=screen_asset_doc,
                )
        with operation_scope(
            profile.app,
            root=profile_root,
        ):
            step_decision = admit_recipe_steps(
                profile,
                steps,
                mode=mode,
                trial_draft_recipes=trial_draft_recipes,
                confirm_trial_effects=confirm_trial_effects,
                trial_data_isolation=trial_data_isolation,
            )
            eb_issues = (
                validate_case_entity_binding(
                    profile,
                    case,
                    fixture_vars=_case_fixture_vars_for_admission(profile, case_id),
                )
                if step_decision["admitted"] and case_has_entity_binding(case)
                else []
            )
    except (EffectPolicyError, SymbolBindingError, OSError, RecipeError, ValueError) as exc:
        if isinstance(exc, EffectPolicyError):
            code = _effect_blocker_code(exc, source=source)
        elif isinstance(exc, SymbolBindingError):
            code = exc.code
        else:
            code = f"invalid_{source}_recipe"
        return {
            "case_id": case_id,
            "admitted": False,
            "blocker": blocker(
                "asset",
                code,
                recovery=f"repair the {source} recipe: {exc}",
            ),
        }
    if not step_decision["admitted"]:
        return {
            "case_id": case_id,
            "admitted": False,
            "blocker": step_decision["blocker"],
        }
    if eb_issues:
        summary = "; ".join(
            f"{issue.code} ({issue.field}): {issue.message}" if issue.field else f"{issue.code}: {issue.message}"
            for issue in eb_issues
        )
        return {
            "case_id": case_id,
            "admitted": False,
            "blocker": blocker(
                "asset",
                eb_issues[0].code,
                recovery=f"repair entity_binding for {case_id}: {summary}",
            ),
        }
    return {"case_id": case_id, "admitted": True, "blocker": None}


def activation_blocker(
    profile: Any,
    case: dict[str, Any],
    *,
    changes: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Evaluate the admission contract for the case as it would look after activation.

    Gate entry points that *create* an (active, ui_ready) asset — promote, batch
    activate, status/automation edits — share this single check so a broken asset
    is rejected at the door instead of surfacing later as blocked_asset debt.
    """
    prospective = {**case, **(changes or {}), "status": "active"}
    prospective.pop("_trial_recipe_document", None)
    prospective.pop("_trial_recipe_identity", None)
    return admit_case(profile, prospective)["blocker"]


def audit_active_assets(profile: Any, *, cases: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return every active UI asset that cannot enter the default selector."""
    findings: list[dict[str, Any]] = []
    for case in cases:
        decision = admit_case(profile, case)
        if not decision["admitted"]:
            findings.append(decision)
    return findings


def partition_admitted_cases(
    profile: Any,
    *,
    cases: Iterable[dict[str, Any]],
    trial_draft_recipes: bool = False,
    confirm_trial_effects: bool = False,
    trial_data_isolation: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a selected sequence without allowing admission gaps to join sharing.

    The runner only receives runnable cases.  A rejected case is nevertheless a
    scheduling boundary: the first later runnable case carries a private marker
    which makes the runner forget any reusable setup established before the gap.
    The original case mappings are never mutated.
    """
    runnable: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    boundary_before_next = False
    for case in cases:
        decision = admit_case(
            profile,
            case,
            trial_draft_recipes=trial_draft_recipes,
            confirm_trial_effects=confirm_trial_effects,
            trial_data_isolation=trial_data_isolation,
        )
        if not decision["admitted"]:
            blocked.append(decision)
            boundary_before_next = True
            continue
        planned = dict(case)
        if boundary_before_next:
            planned["_break_shared_precondition_before"] = True
            boundary_before_next = False
        runnable.append(planned)
    return runnable, blocked


def normalise_case_promotion_evidence(runs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only reviewed case trials and make non-approved review interrupt a streak."""
    evidence: list[dict[str, Any]] = []
    for run in runs:
        review_status = run.get("review_status")
        if review_status not in {"approved", "needs_changes", "rejected"}:
            continue
        item = dict(run)
        if review_status != "approved" or item.get("status") != "passed":
            item["status"] = "failed"
        evidence.append(item)
    return evidence
