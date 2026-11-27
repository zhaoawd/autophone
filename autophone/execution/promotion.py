from __future__ import annotations
from pathlib import Path
from autophone.assets.promotion_service import PromotionExecution
from autophone.assets.apps import AppProfile
from autophone.paths import CODE_ROOT
from autophone.contracts import ContractError
from autophone.execution.app_recipes import import_app_recipes
from autophone.execution.admission import normalise_case_promotion_evidence
from autophone.execution.engine.cases_loader import load_cases
from autophone.execution.engine.recipe import set_app_recipe
from autophone.execution.operation_run import review_operation_result
from autophone.execution.verification import review_verification_result
from typing import Any


def _load_screen_asset_doc(profile: AppProfile) -> dict[str, Any] | None:
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


def validate_recipe(
    profile: AppProfile, case_id: str, steps: list[dict[str, Any]]
) -> None:
    """recipe 落 live 前先过 AssetAdmission 同款静态契约。

    与 asset_admission.admit_case 的差别只在 recipe 来源（此刻可能还在 drafts/），
    校验面相同：operation 引用可解析 + 符号绑定解析 + effect policy 放行。fail-closed → ContractError，
    坏 recipe 不落 live、不翻 status。
    """
    from autophone.assets.app_symbols import SymbolBindingError, load_symbol_binding_for_app
    from autophone.execution.effects import EffectPolicyError
    from autophone.execution.engine.recipe import (
        RecipeError,
        _validate_raw_symbol_step,
        operation_scope,
        recipe_has_unresolved_symbols,
        validate_recipe_for_app,
    )

    import_app_recipes(profile.app, root=getattr(profile, "root", None) or CODE_ROOT)
    try:
        symbol_binding = None
        screen_asset_doc = None
        if recipe_has_unresolved_symbols(steps):
            for idx, step in enumerate(steps):
                if isinstance(step, dict) and "symbol" in step:
                    _validate_raw_symbol_step(step, index=idx)
            symbol_binding = load_symbol_binding_for_app(
                profile,
                allow_default_build=True,
            )
            screen_asset_doc = _load_screen_asset_doc(profile)
        with operation_scope(
            profile.app,
            root=getattr(profile, "root", None) or CODE_ROOT,
        ):
            validate_recipe_for_app(
                steps,
                profile,
                symbol_binding=symbol_binding,
                screen_asset_doc=screen_asset_doc,
                effect_mode="active",
            )
    except (EffectPolicyError, SymbolBindingError, RecipeError, ValueError) as exc:
        raise ContractError(
            f"asset_admission: 用例 {case_id} 的 recipe 未过静态契约，不能转正：{exc}"
        ) from exc


def promotion_execution() -> PromotionExecution:
    from autophone.record.case_recipe import attribution_gaps
    return PromotionExecution(
        review_verification=review_verification_result,
        review_operation=review_operation_result,
        load_cases=load_cases,
        normalize_evidence=normalise_case_promotion_evidence,
        publish_recipe=set_app_recipe,
        validate_recipe=validate_recipe,
        recording_attribution_gaps=attribution_gaps,
    )
