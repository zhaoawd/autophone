from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from autophone.paths import APP_ASSET_ROOT
from autophone.execution import effects

ROOT = APP_ASSET_ROOT
from autophone.assets.apps import AppProfile, list_app_profiles, load_app_profile  # noqa: E402
from autophone.execution.app_recipes import import_app_recipes  # noqa: E402
from autophone.execution.engine.recipe import load_recipe  # noqa: E402


def _typed_steps(raw_steps: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_steps, list):
        return []
    return [step for step in raw_steps if isinstance(step, dict)]


def _operation_assets(profile: AppProfile) -> list[tuple[str, str, str, list[dict[str, Any]]]]:
    path = profile.operations_path()
    if not path.exists():
        return []
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assets = []
    for op in doc.get("operations") or []:
        if not isinstance(op, dict):
            continue
        label = f"operation:{op.get('name') or op.get('id')}"
        assets.append((label, str(path), str(op.get("status") or ""), _typed_steps(op.get("steps"))))
    return assets


def _recipe_assets(profile: AppProfile) -> list[tuple[str, str, str, list[dict[str, Any]]]]:
    recipe_dir = profile.recipes_dir()
    if not recipe_dir.exists():
        return []
    assets = []
    for path in sorted(recipe_dir.glob("*.yaml")):
        try:
            steps = load_recipe(path)
        except Exception:  # noqa: BLE001 - audit reports readable assets only
            continue
        assets.append((f"recipe:{path.stem.upper()}", str(path), "recipe", _typed_steps(steps)))
    return assets


def audit_app(profile: AppProfile) -> dict[str, Any]:
    try:
        import_app_recipes(profile.app, root=profile.root)
    except Exception:
        pass
    allowed = set(profile.allowed_effects)
    trial_data_isolation = effects.profile_trial_data_isolation(profile)
    assets: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    draft_violations: list[dict[str, Any]] = []
    irreversible_count = 0
    incomplete_effect_steps = 0
    missing_executor_declarations = 0

    for label, path, status, steps in [*_operation_assets(profile), *_recipe_assets(profile)]:
        mode: effects.EffectMode = "draft" if status == "draft" else "active"
        hits = []
        for idx, step in enumerate(steps):
            decision = effects.step_effect(step)
            if not decision.irreversible:
                continue
            irreversible_count += 1
            if decision.incomplete:
                incomplete_effect_steps += 1
            evaluation = effects.evaluate_effect_decision(
                decision,
                allowed_effects=allowed,
                mode=mode,
                trial_draft_recipes=False,
                confirm_trial_effects=False,
                trial_data_isolation=trial_data_isolation,
            )
            allowed_here = evaluation.allowed
            hit: dict[str, Any] = {
                "step_index": idx,
                "effect": decision.effect,
                "verb": decision.verb,
                "verbs": sorted(decision.verbs),
                "disallowed_verbs": list(evaluation.disallowed_verbs),
                "completeness": decision.completeness,
                "reason": decision.reason,
                "declared": decision.declared,
                "symbol_bound": decision.symbol_bound,
                "allowed": allowed_here,
                "policy_code": evaluation.code,
            }
            if decision.symbol_id is not None:
                hit["symbol_id"] = decision.symbol_id
            hits.append(hit)
            if not decision.declared:
                missing_executor_declarations += 1
            if not allowed_here:
                row = {"asset": label, "path": path, "status": status, **hit}
                if status == "draft":
                    draft_violations.append(row)
                else:
                    violations.append(row)
        if hits:
            assets.append({"asset": label, "path": path, "status": status, "hits": hits})

    return {
        "app": profile.app,
        "mode": "report-only",
        "allowed_effects": sorted(allowed),
        "trial_data_isolation": trial_data_isolation,
        "assets_with_irreversible_effects": assets,
        "summary": {
            "assets": len(assets),
            "irreversible_steps": irreversible_count,
            "incomplete_effect_steps": incomplete_effect_steps,
            "violations_if_enforced": len(violations),
            "draft_violations": len(draft_violations),
            "missing_executor_effect_declarations": missing_executor_declarations,
        },
        "violations_if_enforced": violations,
        "draft_violations": draft_violations,
    }


def audit_apps(apps: list[str] | None = None, *, root: Path = ROOT) -> dict[str, Any]:
    if apps:
        profiles = [load_app_profile(app, root=root) for app in apps]
    else:
        profiles = list_app_profiles(root=root)
    reports = [audit_app(profile) for profile in profiles]
    return {
        "ok": True,
        "mode": "report-only",
        "apps": reports,
        "summary": {
            "apps": len(reports),
            "incomplete_effect_steps": sum(
                r["summary"]["incomplete_effect_steps"] for r in reports
            ),
            "violations_if_enforced": sum(r["summary"]["violations_if_enforced"] for r in reports),
            "draft_violations": sum(r["summary"]["draft_violations"] for r in reports),
            "missing_executor_effect_declarations": sum(
                r["summary"]["missing_executor_effect_declarations"] for r in reports
            ),
        },
    }
