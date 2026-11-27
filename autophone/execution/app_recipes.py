"""把一个被测 App 的 Recipe 包导入执行期注册表。

原先住在 `autophone.assets.apps`，那里要 import `autophone.execution.{engine.recipe,effects}`，
构成 `assets → execution` 的反向依赖（BL-003 判据 A）。注册表填充本来就是执行层的动作，
资产层只负责 App Profile 的读写，因此整块搬到执行层。

`ROOT` 与 App Profile 的目录布局仍由资产层给出（`execution → assets` 是正向）。
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import re
import sys
from pathlib import Path

from autophone.assets.apps import ROOT
from autophone.execution import effects
from autophone.paths import CODE_ROOT, refuse_code_root_app_assets


def import_app_recipes(app: str, *, root: Path = ROOT) -> bool:
    """Serialize registry activation because its compatibility mirrors are global."""
    from autophone.execution.engine.recipe import recipe_registry_lock

    refuse_code_root_app_assets(root, "import app recipes", path=Path(root) / "apps" / app / "recipes")
    with recipe_registry_lock():
        return _import_app_recipes_locked(app, root=root)


def _import_app_recipes_locked(app: str, *, root: Path = ROOT) -> bool:
    """Import an app's recipe package so recipe.OPERATIONS is populated.

    Tries ``apps.<app>.recipes`` first (works even when ``app`` contains ``-``:
    ``apps/<app>/`` has no ``__init__.py``, so it resolves as a PEP 420
    namespace package under the real ``apps`` package regardless of what
    characters are in the directory name). That normal import only ever sees
    the real ``ROOT/apps`` tree, though, because ``apps`` is typically already
    in ``sys.modules`` with its ``__path__`` fixed to that tree. So when a
    caller passes a different ``root`` (tests loading a throwaway app fixture),
    the normal import can't see it and we fall back to loading
    ``root/apps/<app>/recipes/__init__.py`` directly by file path.
    """
    from autophone.execution.engine import recipe as recipe_mod

    def _activate_cached() -> bool:
        if (
            not recipe_mod.has_app_recipe_registry(app, root=root)
            or not effects.has_app_executor_effects(app, root=root)
        ):
            return False
        recipe_mod.activate_app_operations(app, root=root)
        effects.activate_app_executor_effects(app, root=root)
        # Compatibility surface for callers outside recipe_scope. Runtime
        # execution uses the App-scoped registry and therefore does not race on
        # this process-global mirror.
        recipe_mod.activate_app_recipes(app, root=root)
        return True

    if _activate_cached():
        return True

    def _capture(load) -> bool:
        previous = dict(recipe_mod.OPERATIONS)
        previous_requirements = {
            name: set(values)
            for name, values in recipe_mod.OPERATION_REQUIREMENTS.items()
        }
        previous_recipes = dict(recipe_mod.RECIPES)
        previous_effects = effects.registered_executor_effects()
        recipe_mod.OPERATIONS.clear()
        recipe_mod.OPERATION_REQUIREMENTS.clear()
        recipe_mod.RECIPES.clear()
        effects.replace_registered_executor_effects({})
        try:
            load()
            captured_operations = dict(recipe_mod.OPERATIONS)
            captured_recipes = dict(recipe_mod.RECIPES)
            captured_effects = effects.registered_executor_effects()
        except Exception:
            recipe_mod.OPERATIONS.clear()
            recipe_mod.OPERATIONS.update(previous)
            recipe_mod.OPERATION_REQUIREMENTS.clear()
            recipe_mod.OPERATION_REQUIREMENTS.update(previous_requirements)
            recipe_mod.RECIPES.clear()
            recipe_mod.RECIPES.update(previous_recipes)
            effects.replace_registered_executor_effects(previous_effects)
            raise
        recipe_mod.register_app_operations(app, captured_operations, root=root)
        recipe_mod.register_app_recipes(app, captured_recipes, root=root)
        effects.register_app_executor_effects(app, captured_effects, root=root)
        recipe_mod.activate_app_operations(app, root=root)
        effects.activate_app_executor_effects(app, root=root)
        recipe_mod.activate_app_recipes(app, root=root)
        return True

    recipes_dir = root / "apps" / app / "recipes"
    init_path = recipes_dir / "__init__.py"
    if not init_path.exists():
        live_recipe_paths = sorted(
            path for path in recipes_dir.glob("*.yaml") if path.is_file()
        )
        if not live_recipe_paths:
            recipe_mod.clear_app_recipe_registry(app, root=root)
            return False

        def _load_generic_recipes() -> None:
            # Package-free Apps have no Operation executors to register.  Capture
            # starts from an empty Operation registry, so action-only Recipes
            # validate normally while any Operation reference fails closed.
            for path in live_recipe_paths:
                document = recipe_mod.load_recipe_document(path)
                case_id = document.get("id")
                if not isinstance(case_id, str) or not case_id.strip():
                    raise recipe_mod.RecipeError(
                        f"recipe {path} must declare a non-empty string `id`"
                    )
                if case_id.upper() != path.stem.upper():
                    raise recipe_mod.RecipeError(
                        f"recipe id {case_id!r} does not match filename {path.name}"
                    )
                recipe_mod.register_recipe(
                    path.stem.upper(),
                    document["steps"],
                )

        return _capture(_load_generic_recipes)

    module_name = f"apps.{app}.recipes"
    # 包名导入只看得到 sys.path 上那棵 `apps/`，也就是 CODE_ROOT 下的源码树。
    # App 资产根被 AUTOPHONE_APP_ASSET_DIR 移走时，按文件路径从新根加载，
    # 否则包内 `Path(__file__)` 会读到仓库里的 Recipe YAML。
    if root.resolve() == CODE_ROOT.resolve():
        try:
            def _load_module() -> None:
                already_loaded = module_name in sys.modules
                module = importlib.import_module(module_name)
                if already_loaded:
                    importlib.reload(module)
            _capture(_load_module)
            return True
        except ImportError:
            pass

    safe = re.sub(r"[^A-Za-z0-9_]+", "_", app)
    digest = hashlib.sha1(str(init_path.resolve()).encode("utf-8")).hexdigest()[:12]
    dynamic_name = f"autophone_dynamic_app_recipes.{safe}_{digest}"
    if dynamic_name in sys.modules and _activate_cached():
        return True
    spec = importlib.util.spec_from_file_location(
        dynamic_name,
        init_path,
        submodule_search_locations=[str(init_path.parent)],
    )
    if spec is None or spec.loader is None:
        return False
    def _load_dynamic() -> None:
        module = sys.modules.get(dynamic_name)
        if module is None:
            module = importlib.util.module_from_spec(spec)
            sys.modules[dynamic_name] = module
        spec.loader.exec_module(module)
    return _capture(_load_dynamic)
