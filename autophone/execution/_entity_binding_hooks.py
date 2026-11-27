"""Domain execution hooks registered into ``autophone.assets.entity_binding``.

Bridges ``autophone-domain`` Python operation registries (``recipe.OPERATIONS``,
app-scoped operations, ``import_app_recipes``) and ``effects`` policy lookups into
``autophone.assets.entity_binding`` without creating a reverse ``kernel -> domain``
import edge.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from autophone.assets.entity_binding import (
    ReadbackExecutorInfo,
    ReadbackStepEffect,
    register_readback_runtime_provider,
)
from autophone.paths import CODE_ROOT


def inspect_readback_executor(profile: Any, op_name: str) -> ReadbackExecutorInfo:
    """Resolve a Python-registered readback executor and its effect verdict."""
    from autophone.execution import effects
    from autophone.execution.app_recipes import import_app_recipes
    from autophone.execution.engine import recipe as recipe_mod

    profile_app = getattr(profile, "app", None)
    profile_root = getattr(profile, "root", None) or CODE_ROOT

    scoped_ops = recipe_mod._SCOPED_OPERATIONS.get()
    executors: dict[str, Any] = {}
    if scoped_ops is not None:
        executors.update(scoped_ops)
    else:
        executors.update(recipe_mod.OPERATIONS)

    app_executors: dict[str, Any] = {}
    if isinstance(profile_app, str) and profile_app:
        app_executors = recipe_mod.operations_for_app(profile_app, root=profile_root)
        if not app_executors and op_name not in executors:
            try:
                import_app_recipes(profile_app, root=profile_root)
                app_executors = recipe_mod.operations_for_app(profile_app, root=profile_root)
            except Exception:  # noqa: BLE001
                app_executors = {}
        executors.update(app_executors)

    executor_fn = executors.get(op_name)
    if executor_fn is None:
        return ReadbackExecutorInfo()

    if op_name in app_executors and scoped_ops is None and op_name not in recipe_mod.OPERATIONS:
        app_effects = effects.executor_effects_for_app(profile_app, root=profile_root)
        decision = app_effects.get(op_name) or effects.EffectDecision(
            effects.IRREVERSIBLE,
            op_name,
            "executor_missing_effect_declaration",
            declared=False,
        )
    else:
        decision = effects.executor_effect(op_name)

    return ReadbackExecutorInfo(
        executor_fn=executor_fn,
        effect=ReadbackStepEffect(
            irreversible=decision.irreversible,
            effect=decision.effect,
            reason=decision.reason,
        ),
    )


def classify_readback_step(step: Mapping[str, Any]) -> ReadbackStepEffect:
    """Classify one YAML operation step using ``autophone.execution.effects``."""
    from autophone.execution import effects

    decision = effects.step_effect(dict(step))
    return ReadbackStepEffect(
        irreversible=decision.irreversible,
        effect=decision.effect,
        reason=decision.reason,
    )


def install_entity_binding_runtime_hooks() -> None:
    """Register domain executor/effect hooks with ``autophone.assets.entity_binding``."""
    register_readback_runtime_provider(
        inspect_executor=inspect_readback_executor,
        classify_step=classify_readback_step,
    )
