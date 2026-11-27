"""Bind explicit Draft Trial acknowledgement to the current static asset snapshot.

This is informed acknowledgement, not an authorization credential. Device access,
asset admission and effect policy retain their existing independent checks.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

import yaml

from autophone.assets.apps import load_app_profile
from autophone.assets.fixture_requirements import declared_fixtures, fixture_requirements_path, load_fixture_requirements
from autophone.assets.promotion_identity import recipe_path, selector_is_draft_trial
from autophone.contracts import ContractError, validate_or_raise


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _asset_snapshot(profile, case_id: str, source: str) -> dict[str, Any]:
    paths = {
        "profile": profile.profile_dir / "profile.yaml",
        "cases": profile.cases_path(),
        "recipe": recipe_path(profile, case_id, source=source),
        "operations": profile.operations_path(),
        "fixtures": fixture_requirements_path(profile),
    }
    return {key: hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            for key, path in paths.items()}


def build_trial_context(engine, case_id: str, *, trial_draft_recipes: bool = False) -> dict[str, Any]:
    """Read static context only; never query device or historical Run evidence."""
    profile = engine.catalog.profile
    case = engine.catalog.case(case_id)
    if case is None:
        raise ContractError(f"trial_confirmation_case_not_found: {case_id}")
    source = "draft" if trial_draft_recipes or case.get("status") == "draft" else "live"
    before = _asset_snapshot(profile, case_id, source)
    static = engine.query_2(case_id=case_id, trial_draft_recipes=trial_draft_recipes)["data"]
    document = load_fixture_requirements(profile)
    requirements = declared_fixtures(document["cases"].get(case_id.upper(), []))
    irreversible = [{"fixture_id": req["id"], "effect": req["effect"]}
                    for req in requirements if req["reversibility"] == "irreversible"]
    effects = _effects(profile, static, irreversible)
    if before != _asset_snapshot(profile, case_id, source):
        raise ContractError("trial_confirmation_context_changed: reload preparation card")
    marker = {
        "case_id": case_id,
        "run_effect": effects["run_effect"],
        "context_sha256": _digest({
            "schema": "trial-confirmation/1", "app": profile.app,
            "case": case, "recipe_source": source, "assets": before, "effects": effects,
        }),
    }
    return {"case": case, "static": static, "document": document, "requirements": requirements,
            "effects": effects, "trial_confirmation": marker}


def validate_trial_confirmation(spec: Mapping[str, Any], profile=None) -> None:
    """Reject missing risky acknowledgement and stale acknowledgements before dispatch."""
    selector = spec.get("case_selector") or {}
    markers = spec.get("trial_confirmation")
    trial = spec.get("type") == "regression" and selector_is_draft_trial(selector)
    if not trial:
        if markers is not None:
            raise ContractError("trial_confirmation_not_applicable: requires explicit Draft Trial")
        return
    validate_or_raise("run-spec.v1.json", dict(spec))
    ids = selector.get("ids") or []
    if not ids or len(set(ids)) != len(ids):
        raise ContractError("trial_confirmation_selector_invalid: pin unique case ids")
    by_id = {row["case_id"]: row for row in markers or []}
    if len(by_id) != len(markers or []) or set(by_id) - set(ids):
        raise ContractError("trial_confirmation_case_mismatch")
    from autophone.knowledge.query import KnowledgeQueryEngine

    try:
        profile = profile or load_app_profile(str(spec["app"]))
        engine = KnowledgeQueryEngine(profile.app, root=profile.root, state_root=profile.host_state_root)
        for case_id in ids:
            current = build_trial_context(engine, case_id, trial_draft_recipes=True)["trial_confirmation"]
            marker = by_id.get(case_id)
            if marker is None:
                if current["run_effect"] != "reversible":
                    raise ContractError(f"trial_confirmation_required: {case_id} ({current['run_effect']}); reload preparation card")
            elif marker != {**current, "acknowledged": True}:
                raise ContractError(f"trial_confirmation_stale: {case_id}; reload preparation card and confirm again")
    except ContractError:
        raise
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        raise ContractError(f"trial_confirmation_unavailable: {exc}") from exc


def _effects(
    profile: Any, static: dict[str, Any], irreversible: list[dict[str, Any]],
) -> dict[str, Any]:
    """Shared effect summary for preparation cards and submission acknowledgement.

    Effect policy is independently enforced by query 2 and at dispatch time.

    ``requires_confirmation`` keeps its Fixture-only meaning (golden path plans
    and the ``effect`` checklist row consume it).  ``run_effect`` and
    ``run_requires_confirmation`` cover everything a Trial dispatches: Case
    setup, the Recipe, teardown and post-case cleanup, plus declared
    irreversible Fixtures.
    """
    recipe_steps = static.get("recipe_effects")
    case_op_steps = static.get("case_operation_effects")
    recipe_effect = _recipe_effect_level(recipe_steps)
    run_effect = _run_effect_level(
        recipe_effect=recipe_effect, case_op_steps=case_op_steps, irreversible=irreversible)
    return {
        "allowed_effects": list(profile.allowed_effects or []),
        "irreversible_fixtures": irreversible,
        "requires_confirmation": bool(irreversible),
        "recipe_effect": recipe_effect,
        "recipe_steps": list(recipe_steps or []),
        "case_op_steps": list(case_op_steps or []),
        "run_effect": run_effect,
        "run_requires_confirmation": run_effect == "irreversible",
    }


def _recipe_effect_level(steps: list[dict[str, Any]] | None) -> str:
    """``irreversible`` if any step is, ``reversible`` if all are, else ``unknown``."""
    if not steps:
        return "unknown"
    if any(step.get("effect") == "irreversible" for step in steps):
        return "irreversible"
    return "reversible"


def _run_effect_level(
    *,
    recipe_effect: str,
    case_op_steps: list[dict[str, Any]] | None,
    irreversible: list[dict[str, Any]],
) -> str:
    """Whole-Trial level: ``irreversible`` if the Recipe, any Case Operation or
    a declared Fixture is; ``unknown`` if the Recipe or the Case Operation
    effects could not be resolved; otherwise ``reversible``."""
    if (
        recipe_effect == "irreversible"
        or irreversible
        or any(step.get("effect") == "irreversible" for step in case_op_steps or [])
    ):
        return "irreversible"
    if recipe_effect == "unknown" or case_op_steps is None:
        return "unknown"
    return "reversible"


