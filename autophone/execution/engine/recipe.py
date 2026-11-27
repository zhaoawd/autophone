"""recipe.py — case IR v1 dispatcher (docs §10.10).

A case IR is a *closed* sequence over two step kinds — never a DSL:
  - `{"action": <verb>, **params}`     a whitelisted CaseContext verb
  - `{"operation": <name>, "args": …}` a hand-written executor (stateful nav /
                                       fixtures: open_options, login, …)

Everything with runtime control flow (loops, screen-state predicates, data-driven
termination, state-reset) lives inside an operation executor or a verb — it MUST
NOT sink into the IR (§10.10). The dispatcher therefore only does two things:
① dispatch by whitelist, ② check a step's params exactly fit the target's keyword
surface — no unknown names, no missing required ones, right value types — and that
operation steps are a closed shape (`operation` + `args` only). Param names in the
recipe MUST equal the method/executor kwargs (no translation table = stays a
dispatcher, not an interpreter).

`run_recipe` replicates runner.run_case's outcome semantics exactly (§10.4):
StepFailure→fail, INFRA_ERRORS→error, completed-with-zero-asserts→error.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import copy
import hashlib
import inspect
import json
import logging
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Mapping

import yaml

from autophone.execution import effects
from autophone.device.uds import HARDWARE_KEYS, GeometryDrift, ProtocolError
from autophone.device.ocr_target import OcrError
from autophone.execution.engine.actions import CaseContext, StepFailure

logger = logging.getLogger(__name__)

# Mirror runner.INFRA_ERRORS so error-vs-fail classification is identical.
INFRA_ERRORS = (GeometryDrift, ProtocolError, OcrError, OSError, ConnectionError)


class RecipeError(ValueError):
    """A recipe step is malformed: unknown verb/operation, both/neither of
    action+operation, or a param name the target does not accept. Raised at
    validate/dispatch time — before any device call — so a hallucinated step is
    rejected, not half-executed."""


# Closed verb whitelist. An `action` not in here is rejected outright. Values are
# the unbound CaseContext methods; the dispatcher passes ctx positionally.
ACTIONS: dict[str, Callable[..., Any]] = {
    "tap_text": CaseContext.tap_text,
    "tap_ax": CaseContext.tap_ax,
    "tap_xy": CaseContext.tap_xy,
    "swipe_xy": CaseContext.swipe_xy,
    "press_key": CaseContext.press_key,
    "wait_text": CaseContext.wait_text,
    "type_text": CaseContext.type_text,
    "assert_present": CaseContext.assert_present,
    "assert_screen": CaseContext.assert_screen,
    "assert_present_enhanced": CaseContext.assert_present_enhanced,
    "assert_region_nonempty": CaseContext.assert_region_nonempty,
    "assert_absent": CaseContext.assert_absent,
    "assert_remains_absent": CaseContext.assert_remains_absent,
    "handle_dialog_if_present": CaseContext.handle_dialog_if_present,
    "manual_pause": CaseContext.manual_pause,
}

# Stateful navigation / fixtures, as hand-written executors `(ctx, **args)`. This
# is where the loop + screen-state predicate of _reset_to_home/_open_options
# belongs (§10.10) — never in the case IR. App-populated via register_operation.
OPERATIONS: dict[str, Callable[..., Any]] = {}
APP_OPERATIONS: dict[str, dict[str, Callable[..., Any]]] = {}
APP_ROOT_OPERATIONS: dict[
    tuple[str, str], dict[str, Callable[..., Any]]
] = {}
OPERATION_REQUIREMENTS: dict[str, set[str]] = {}
APP_OPERATION_REQUIREMENTS: dict[str, dict[str, set[str]]] = {}
APP_ROOT_OPERATION_REQUIREMENTS: dict[
    tuple[str, str], dict[str, set[str]]
] = {}
_SCOPED_OPERATIONS: ContextVar[dict[str, Callable[..., Any]] | None] = ContextVar(
    "recipe_scoped_operations",
    default=None,
)
_SCOPED_ASSET_ROOT: ContextVar[Path | None] = ContextVar("recipe_asset_root", default=None)


def scoped_asset_root() -> Path:
    from autophone.paths import APP_ASSET_ROOT
    return _SCOPED_ASSET_ROOT.get() or APP_ASSET_ROOT


def register_operation(
    name: str,
    fn: Callable[..., Any],
    *,
    effect: str | None = None,
    effect_verb: str | None = None,
    requires: Iterable[str] = (),
) -> None:
    """Register an operation executor under `name` (app side; see
    apps/food_ordering_v2/recipes/__init__.py). The executor's first positional param is
    the CaseContext; its remaining kwargs are the recipe step's `args` keys."""
    OPERATIONS[name] = fn
    OPERATION_REQUIREMENTS[name] = {str(capability) for capability in requires if str(capability)}
    if effect is not None:
        effects.register_executor_effect(name, effect=effect, verb=effect_verb)


def register_app_operations(
    app: str,
    operations: dict[str, Callable[..., Any]],
    *,
    root: Path | None = None,
) -> None:
    """Pin the operation registry captured while importing one app's recipes."""
    key = str(app)
    copied_operations = dict(operations)
    with _RECIPE_REGISTRY_LOCK:
        copied_requirements = {
            name: set(OPERATION_REQUIREMENTS.get(name) or ()) for name in operations
        }
        APP_OPERATIONS[key] = copied_operations
        APP_OPERATION_REQUIREMENTS[key] = copied_requirements
        if root is not None:
            resolved_root = str(Path(root).resolve())
            identity = (key, resolved_root)
            APP_ROOT_OPERATIONS[identity] = copied_operations
            APP_ROOT_OPERATION_REQUIREMENTS[identity] = copied_requirements
            APP_REGISTRY_ROOTS[key] = resolved_root


def operations_for_app(
    app: str,
    *,
    root: Path | None = None,
) -> dict[str, Callable[..., Any]]:
    """Return the registered operation executors for one app without mutating globals."""
    key = str(app)
    with _RECIPE_REGISTRY_LOCK:
        if root is None:
            return dict(APP_OPERATIONS.get(key) or {})
        identity = (key, str(Path(root).resolve()))
        return dict(APP_ROOT_OPERATIONS.get(identity) or {})


def operation_requirements_for_app(
    app: str,
    name: str,
    *,
    root: Path | None = None,
) -> set[str]:
    """Return capability requirements declared by one app's registered operation."""
    key = str(app)
    with _RECIPE_REGISTRY_LOCK:
        registry = (
            APP_OPERATION_REQUIREMENTS.get(key)
            if root is None
            else APP_ROOT_OPERATION_REQUIREMENTS.get(
                (key, str(Path(root).resolve()))
            )
        )
        return set((registry or {}).get(str(name)) or ())


def activate_app_operations(
    app: str,
    *,
    root: Path | None = None,
) -> dict[str, Callable[..., Any]]:
    """Rebuild operation compatibility mirrors from one App/root identity."""
    key = str(app)
    with _RECIPE_REGISTRY_LOCK:
        if root is None:
            operations = dict(APP_OPERATIONS.get(key) or {})
            requirements = {
                name: set(values)
                for name, values in (APP_OPERATION_REQUIREMENTS.get(key) or {}).items()
            }
            resolved_root = APP_REGISTRY_ROOTS.get(key)
        else:
            resolved_root = str(Path(root).resolve())
            identity = (key, resolved_root)
            operations = dict(APP_ROOT_OPERATIONS.get(identity) or {})
            requirements = {
                name: set(values)
                for name, values in (
                    APP_ROOT_OPERATION_REQUIREMENTS.get(identity) or {}
                ).items()
            }
            APP_OPERATIONS[key] = dict(operations)
            APP_OPERATION_REQUIREMENTS[key] = requirements
            APP_REGISTRY_ROOTS[key] = resolved_root
        OPERATIONS.clear()
        OPERATIONS.update(operations)
        OPERATION_REQUIREMENTS.clear()
        OPERATION_REQUIREMENTS.update(requirements)
        return operations


@contextmanager
def operation_scope(
    app: str,
    *,
    root: Path | None = None,
) -> Iterator[dict[str, Callable[..., Any]]]:
    """Resolve operation executors and effects against one App execution context."""
    # Import capture takes the same locks in recipe -> effects order.  Copy both
    # registries while holding that boundary, then release it before executing
    # arbitrary Operation code so one scope cannot mix two recapture generations.
    with _RECIPE_REGISTRY_LOCK:
        ops = operations_for_app(app, root=root)
        effect_snapshot = effects.executor_effects_for_app(app, root=root)
    token = _SCOPED_OPERATIONS.set(ops)
    root_token = _SCOPED_ASSET_ROOT.set(root)
    try:
        with effects.executor_effect_snapshot_scope(effect_snapshot):
            yield ops
    finally:
        _SCOPED_OPERATIONS.reset(token)
        _SCOPED_ASSET_ROOT.reset(root_token)


def _active_operations() -> dict[str, Callable[..., Any]]:
    scoped = _SCOPED_OPERATIONS.get()
    return OPERATIONS if scoped is None else scoped


# Case-id → validated recipe steps. The production runner (runner.run_case) prefers
# a registered recipe over the hand-written CASE_SCRIPTS[id] — this is the IR
# cutover (§10.10). Parallel to CASE_SCRIPTS, populated by the app's recipes package
# (apps/food_ordering_v2/recipes/__init__.py) at import time.
RECIPES: dict[str, list[dict[str, Any]]] = {}
APP_RECIPES: dict[str, dict[str, list[dict[str, Any]]]] = {}
APP_ROOT_RECIPES: dict[
    tuple[str, str], dict[str, list[dict[str, Any]]]
] = {}
APP_REGISTRY_ROOTS: dict[str, str] = {}
_RECIPE_REGISTRY_LOCK = threading.RLock()
_ACTIVE_REGISTRY_IDENTITY: tuple[str, str] | None = None
_SCOPED_RECIPES: ContextVar[
    dict[str, list[dict[str, Any]] | None] | None
] = ContextVar(
    "recipe_scoped_recipes",
    default=None,
)


def register_app_recipes(
    app: str,
    recipes: dict[str, list[dict[str, Any]]],
    *,
    root: Path | None = None,
) -> None:
    """Pin the recipe registry captured while importing one app's package."""
    with _RECIPE_REGISTRY_LOCK:
        key = str(app)
        copied = copy.deepcopy(recipes)
        APP_RECIPES[key] = copied
        if root is not None:
            resolved_root = str(Path(root).resolve())
            APP_ROOT_RECIPES[(key, resolved_root)] = copied
            APP_REGISTRY_ROOTS[key] = resolved_root


def recipes_for_app(
    app: str,
    *,
    root: Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return an isolated copy of one app's registered recipes."""
    with _RECIPE_REGISTRY_LOCK:
        key = str(app)
        if root is None:
            return copy.deepcopy(APP_RECIPES.get(key) or {})
        identity = (key, str(Path(root).resolve()))
        return copy.deepcopy(APP_ROOT_RECIPES.get(identity) or {})


def set_app_recipe(
    app: str,
    case_id: str,
    steps: list[dict[str, Any]],
    *,
    root: Path | None = None,
) -> None:
    """Update one app's live recipe cache after an atomic live-asset write."""
    global _ACTIVE_REGISTRY_IDENTITY
    key = str(case_id).upper()
    with _RECIPE_REGISTRY_LOCK:
        app_key = str(app)
        copied = copy.deepcopy(steps)
        if root is None:
            APP_RECIPES.setdefault(app_key, {})[key] = copied
            APP_OPERATIONS.setdefault(app_key, {})
            APP_OPERATION_REQUIREMENTS.setdefault(app_key, {})
            resolved_root = APP_REGISTRY_ROOTS.get(app_key)
            if resolved_root is not None:
                identity = (app_key, resolved_root)
                APP_ROOT_RECIPES.setdefault(identity, {})[key] = copy.deepcopy(copied)
                if _ACTIVE_REGISTRY_IDENTITY == identity:
                    RECIPES[key] = copy.deepcopy(copied)
            return
        resolved_root = str(Path(root).resolve())
        identity = (app_key, resolved_root)
        APP_ROOT_RECIPES.setdefault(identity, {})[key] = copied
        if _ACTIVE_REGISTRY_IDENTITY == identity:
            APP_RECIPES.setdefault(app_key, {})[key] = copy.deepcopy(copied)
            RECIPES[key] = copy.deepcopy(copied)


def activate_app_recipes(
    app: str,
    *,
    root: Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Rebuild the process-global compatibility mirror from one App cache."""
    global _ACTIVE_REGISTRY_IDENTITY
    with _RECIPE_REGISTRY_LOCK:
        key = str(app)
        if root is None:
            recipes = copy.deepcopy(APP_RECIPES.get(key) or {})
            resolved_root = APP_REGISTRY_ROOTS.get(key)
        else:
            resolved_root = str(Path(root).resolve())
            identity = (key, resolved_root)
            recipes = copy.deepcopy(APP_ROOT_RECIPES.get(identity) or {})
            APP_RECIPES[key] = copy.deepcopy(recipes)
            APP_REGISTRY_ROOTS[key] = resolved_root
        RECIPES.clear()
        RECIPES.update(copy.deepcopy(recipes))
        _ACTIVE_REGISTRY_IDENTITY = (
            (key, resolved_root) if resolved_root is not None else None
        )
        return recipes


def set_app_registry_root(app: str, root: Path) -> None:
    with _RECIPE_REGISTRY_LOCK:
        APP_REGISTRY_ROOTS[str(app)] = str(Path(root).resolve())


def app_registry_matches_root(app: str, root: Path) -> bool:
    with _RECIPE_REGISTRY_LOCK:
        return APP_REGISTRY_ROOTS.get(str(app)) == str(Path(root).resolve())


def has_app_recipe_registry(app: str, *, root: Path | None = None) -> bool:
    """Return whether a complete Recipe/Operation cache exists for an identity."""
    key = str(app)
    with _RECIPE_REGISTRY_LOCK:
        if root is None:
            return key in APP_RECIPES and key in APP_OPERATIONS
        identity = (key, str(Path(root).resolve()))
        return identity in APP_ROOT_RECIPES and identity in APP_ROOT_OPERATIONS


def clear_app_recipe_registry(app: str, *, root: Path | None = None) -> None:
    """Drop a mismatched/missing App package so stale operations cannot validate."""
    global _ACTIVE_REGISTRY_IDENTITY
    with _RECIPE_REGISTRY_LOCK:
        key = str(app)
        if root is None:
            APP_RECIPES.pop(key, None)
            APP_OPERATIONS.pop(key, None)
            APP_OPERATION_REQUIREMENTS.pop(key, None)
            APP_REGISTRY_ROOTS.pop(key, None)
            for identity in [item for item in APP_ROOT_RECIPES if item[0] == key]:
                APP_ROOT_RECIPES.pop(identity, None)
                APP_ROOT_OPERATIONS.pop(identity, None)
                APP_ROOT_OPERATION_REQUIREMENTS.pop(identity, None)
            active_matches = bool(
                _ACTIVE_REGISTRY_IDENTITY and _ACTIVE_REGISTRY_IDENTITY[0] == key
            )
        else:
            resolved_root = str(Path(root).resolve())
            identity = (key, resolved_root)
            APP_ROOT_RECIPES.pop(identity, None)
            APP_ROOT_OPERATIONS.pop(identity, None)
            APP_ROOT_OPERATION_REQUIREMENTS.pop(identity, None)
            active_matches = _ACTIVE_REGISTRY_IDENTITY == identity
            if APP_REGISTRY_ROOTS.get(key) == resolved_root:
                APP_RECIPES.pop(key, None)
                APP_OPERATIONS.pop(key, None)
                APP_OPERATION_REQUIREMENTS.pop(key, None)
                APP_REGISTRY_ROOTS.pop(key, None)
        effects.clear_app_executor_effects(key, root=root)
        if active_matches:
            RECIPES.clear()
            OPERATIONS.clear()
            OPERATION_REQUIREMENTS.clear()
            _ACTIVE_REGISTRY_IDENTITY = None


@contextmanager
def recipe_registry_lock() -> Iterator[None]:
    """Coordinate package capture, cache activation, and live hot updates."""
    with _RECIPE_REGISTRY_LOCK:
        yield


@contextmanager
def recipe_scope(
    app: str,
    *,
    root: Path | None = None,
    overrides: dict[str, list[dict[str, Any]] | None] | None = None,
) -> Iterator[dict[str, list[dict[str, Any]] | None]]:
    """Resolve case IDs against one app plus explicit per-run draft overrides."""
    recipes = recipes_for_app(app, root=root)
    if overrides:
        recipes.update(copy.deepcopy(overrides))
    token = _SCOPED_RECIPES.set(recipes)
    try:
        yield recipes
    finally:
        _SCOPED_RECIPES.reset(token)


def scoped_recipes() -> dict[str, list[dict[str, Any]] | None] | None:
    """Return the current execution registry, or ``None`` outside an app scope."""
    return _SCOPED_RECIPES.get()


def register_recipe(case_id: str, steps: list[dict[str, Any]]) -> None:
    """Register a recipe under `case_id`, validating it FIRST. Validation at
    registration (not at run) is the contract that makes the runtime cutover safe:
    a malformed recipe raises here — at app-package import, before any case runs —
    so runner.run_case's inlined dispatch can never hit a RecipeError mid-device
    (§10.10 'static check rejects first'). Operations the recipe references must
    already be registered when this is called."""
    if case_id in RECIPES:
        # Idempotent: if the same recipe is registered again (e.g., load_cases called
        # multiple times), silently skip. Only raise if the steps differ.
        if RECIPES[case_id] == steps:
            return
        raise ValueError(f"duplicate recipe for {case_id}")
    validate_recipe(steps, allow_unresolved_symbols=True)
    RECIPES[case_id] = steps


# ── value-type checks, keyed by param NAME ──────────────────────────────────
# Param names are type-consistent across the whole verb surface (`query` is
# always a str, `queries` always list[str], `region` always a 4-number box, …),
# so one name→type table covers every verb. This catches the bad-but-well-named
# recipe an LLM is most likely to emit — e.g. `queries: "首页"` (a scalar that
# assert_present would iterate char-by-char, actions.py) — which the name/required
# checks alone wave through. Names absent from the table are left unchecked.
def _is_str(v: Any) -> bool:
    return isinstance(v, str)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_positive_int(v: Any) -> bool:
    return _is_int(v) and v > 0


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_nonnegative_num(v: Any) -> bool:
    return _is_num(v) and v >= 0


def _is_positive_num(v: Any) -> bool:
    return _is_num(v) and v > 0


def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _is_str_list(v: Any) -> bool:
    return isinstance(v, list) and len(v) > 0 and all(isinstance(x, str) for x in v)


def _is_box(v: Any) -> bool:
    return isinstance(v, (list, tuple)) and len(v) == 4 and all(_is_num(x) for x in v)


def _is_xy(v: Any) -> bool:
    return isinstance(v, (list, tuple)) and len(v) == 2 and all(_is_int(x) for x in v)


def _is_region_list(v: Any) -> bool:
    if not isinstance(v, list) or not v:
        return False
    for item in v:
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            return False
        box, label = item
        if not (_is_box(box) and isinstance(label, str)):
            return False
    return True


_PARAM_TYPES: dict[str, tuple[Callable[[Any], bool], str]] = {
    "screen_id": (_is_str, "string"),
    "screen_asset": (_is_str, "string"),
    "screen_sha256": (_is_str, "string"),
    "query": (_is_str, "string"),
    "queries": (_is_str_list, "non-empty list of strings"),
    "text": (_is_str, "string"),
    "label": (_is_str, "string"),
    "tap_query": (_is_str, "string"),
    "entry": (_is_str, "string"),
    "prompt": (_is_str, "string"),
    "role": (_is_str, "string"),
    "identifier": (_is_str, "string"),
    "level": (_is_str, "string"),
    "px": (_is_int, "int"),
    "py": (_is_int, "int"),
    "px1": (_is_int, "int"),
    "py1": (_is_int, "int"),
    "px2": (_is_int, "int"),
    "py2": (_is_int, "int"),
    "ms": (_is_positive_int, "positive int"),  # 0/负时长不可回放——与采集/提升侧同口径
    "retries": (_is_int, "int"),
    "poll_ms": (_is_int, "int"),
    "wait_s": (_is_num, "number"),
    "timeout_s": (_is_num, "number"),
    "duration_s": (_is_positive_num, "positive number"),
    "retry_delay_s": (_is_nonnegative_num, "non-negative number"),
    "entry_mc": (_is_num, "number"),
    "min_confidence": (_is_num, "number"),
    "enabled_only": (_is_bool, "bool"),
    "require_unique": (_is_bool, "bool"),
    "region": (_is_box, "[nx0,ny0,nx1,ny1] of numbers"),
    "regions": (_is_region_list, "list of [[4 numbers], label-string]"),
    "fallback_xy": (_is_xy, "[px,py] of ints"),
}

# Controlled symbol binding step contract (Decision D3):
# `{"action": "tap_ax", "symbol": "<id>", ...optional "label"...}` only.
SYMBOL_ALLOWED_KEYS: frozenset[str] = frozenset({"action", "symbol", "label"})
SYMBOL_FORBIDDEN_MIXED_KEYS: frozenset[str] = frozenset({
    "identifier",
    "role",
    "region",
    "fallback_xy",
    "query",
    "enabled_only",
    "screen_id",
    "require_unique",
})
_INTERNAL_STEP_METADATA_KEYS: frozenset[str] = frozenset({
    effects.SYMBOL_BINDING_STEP_KEY,
    effects.SYMBOL_EFFECTS_STEP_KEY,
})


def recipe_has_unresolved_symbols(steps: Iterable[Any] | None) -> bool:
    """Return True if any step contains an unexpanded ``symbol`` reference."""
    if not steps:
        return False
    return any(isinstance(step, Mapping) and "symbol" in step for step in steps)


def recipe_uses_symbols(steps: Iterable[Any] | None) -> bool:
    """Return True if any step references a symbol (raw or expanded)."""
    if not steps:
        return False
    return any(
        isinstance(step, Mapping)
        and ("symbol" in step or effects.SYMBOL_BINDING_STEP_KEY in step)
        for step in steps
    )


def steps_binding_sha256(steps: Iterable[Any] | None) -> str | None:
    """Extract the unique ``binding_sha256`` from expanded symbol steps, or ``None``.

    Raises ``RecipeError`` if multiple expanded symbol steps disagree on
    ``binding_sha256``.
    """
    if not steps:
        return None
    found: set[str] = set()
    for idx, step in enumerate(steps):
        if not isinstance(step, Mapping):
            continue
        meta = step.get(effects.SYMBOL_BINDING_STEP_KEY)
        if not isinstance(meta, Mapping):
            continue
        digest = meta.get("binding_sha256")
        if not isinstance(digest, str) or not digest.strip():
            raise RecipeError(f"step[{idx}]: malformed _symbol_binding.binding_sha256")
        found.add(digest.strip())
    if not found:
        return None
    if len(found) > 1:
        raise RecipeError(f"conflicting symbol binding_sha256 values across steps: {sorted(found)}")
    return next(iter(found))


def _is_symbol_bound_step(step: Mapping[str, Any]) -> bool:
    return (
        isinstance(step, Mapping)
        and isinstance(step.get(effects.SYMBOL_BINDING_STEP_KEY), Mapping)
    )


def _validate_raw_symbol_step(
    step: Mapping[str, Any],
    *,
    index: int | None = None,
) -> tuple[str, str | None]:
    prefix = f"step[{index}]: " if index is not None else ""
    if "operation" in step:
        raise RecipeError(f"{prefix}'symbol' is only supported on action 'tap_ax', not operation steps")
    action = step.get("action")
    if action != "tap_ax":
        raise RecipeError(
            f"{prefix}'symbol' is only supported on action 'tap_ax', got {action!r}"
        )
    mixed = sorted(key for key in step if key in SYMBOL_FORBIDDEN_MIXED_KEYS)
    if mixed:
        raise RecipeError(
            f"{prefix}action tap_ax symbol step cannot mix raw locator/fallback key(s) {mixed}"
        )
    extra = sorted(key for key in step if key not in SYMBOL_ALLOWED_KEYS)
    if extra:
        raise RecipeError(
            f"{prefix}action tap_ax symbol step has disallowed key(s) {extra}; "
            f"allowed keys are {sorted(SYMBOL_ALLOWED_KEYS)}"
        )
    raw_symbol = step.get("symbol")
    if not isinstance(raw_symbol, str) or not raw_symbol.strip():
        raise RecipeError(f"{prefix}action tap_ax: symbol must be a non-empty string, got {raw_symbol!r}")
    label: str | None = None
    if "label" in step:
        raw_label = step["label"]
        if not isinstance(raw_label, str):
            raise RecipeError(f"{prefix}action tap_ax: label must be string, got {raw_label!r}")
        label = raw_label
    return raw_symbol.strip(), label


def _validate_screen_exists(
    screen_asset_doc: Mapping[str, Any] | None,
    screen_id: str,
    *,
    symbol_id: str,
    index: int,
) -> None:
    from autophone.assets.screens import screen_by_id

    if not isinstance(screen_asset_doc, Mapping):
        raise RecipeError(
            f"step[{index}]: symbol {symbol_id!r} requires screen_asset_doc (screens.yaml) "
            f"to validate scope.screen_id={screen_id!r}"
        )
    try:
        screen_by_id(dict(screen_asset_doc), screen_id)
    except KeyError as exc:
        raise RecipeError(
            f"step[{index}]: symbol {symbol_id!r} references unknown screen_id "
            f"{screen_id!r} in screens.yaml"
        ) from exc


def expand_symbol_steps(
    steps: list[dict[str, Any]],
    *,
    symbol_binding: Any | None,
    screen_asset_doc: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Unified load-time symbol binder (Decisions D3 & D4).

    Expands every ``{"action": "tap_ax", "symbol": "<id>", ...}`` step 1:1
    (preserving exact ``step_index``) into a frozen ``tap_ax`` step with
    ``require_unique=True``, ``fallback_xy=None``, ``screen_id`` validated
    against ``screen_asset_doc`` (``screens.yaml``), and internal
    ``_symbol_binding`` metadata carrying ``symbol_id``, ``binding_sha256``,
    ``artifact_content_digest``, ``screen_id``, and declared ``effects``.

    Rejects mixing ``symbol`` with raw locator/fallback keys, rejects ``symbol``
    on any non-``tap_ax`` action, rejects missing bindings/controls/screens,
    and leaves non-symbol steps (and already-expanded symbol steps) intact.
    """
    from autophone.assets.app_symbols import SymbolBindingError

    if not isinstance(steps, list):
        raise RecipeError(f"recipe steps must be a list, got {type(steps).__name__}")

    expanded: list[dict[str, Any]] = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise RecipeError(f"step[{i}]: step must be a mapping, got {type(step).__name__}: {step!r}")
        if "symbol" not in step:
            copied = copy.deepcopy(step)
            if _is_symbol_bound_step(copied) and screen_asset_doc is not None:
                meta = copied[effects.SYMBOL_BINDING_STEP_KEY]
                bound_screen_id = str(meta.get("screen_id") or copied.get("screen_id") or "")
                bound_symbol_id = str(meta.get("symbol_id") or "<symbol>")
                if bound_screen_id:
                    _validate_screen_exists(
                        screen_asset_doc,
                        bound_screen_id,
                        symbol_id=bound_symbol_id,
                        index=i,
                    )
            expanded.append(copied)
            continue

        symbol_id, label = _validate_raw_symbol_step(step, index=i)
        if symbol_binding is None:
            raise RecipeError(
                f"step[{i}]: symbol {symbol_id!r} requires a loaded app-symbols binding"
            )
        try:
            control = symbol_binding.require_control(symbol_id)
        except SymbolBindingError as exc:
            raise RecipeError(f"step[{i}]: {exc}") from exc

        screen_id = str(control.scope.screen_id)
        _validate_screen_exists(
            screen_asset_doc,
            screen_id,
            symbol_id=symbol_id,
            index=i,
        )

        frozen_step: dict[str, Any] = {
            "action": "tap_ax",
            "identifier": control.locator.identifier,
            "role": control.locator.role,
            "region": list(control.scope.region) if control.scope.region is not None else None,
            "enabled_only": bool(control.locator.enabled_only),
            "screen_id": screen_id,
            "require_unique": True,
            "fallback_xy": None,
        }
        if label is not None:
            frozen_step["label"] = label
        frozen_step[effects.SYMBOL_BINDING_STEP_KEY] = {
            "symbol_id": control.symbol_id,
            "binding_sha256": symbol_binding.binding_sha256,
            "artifact_content_digest": symbol_binding.artifact_content_digest,
            "screen_id": screen_id,
            "effects": {
                "verbs": sorted(control.effects.verbs),
                "completeness": control.effects.completeness,
            },
        }
        expanded.append(frozen_step)
    return expanded

# 少数动作的取值是封闭集，而 _PARAM_TYPES 是按 param 名全局生效的——`name` 这种通用词
# 迟早被别的目标用上（将来某个 select_store(name=…)），所以按 (verb, param) 单独收口，
# 别让硬件键的封闭集污染全局。静态就判掉，模型瞎编的键名不必等到设备上才发现。
_ACTION_VALUE_CHECKS: dict[tuple[str, str], tuple[Callable[[Any], bool], str]] = {
    ("press_key", "name"): (lambda v: v in HARDWARE_KEYS,
                            "one of " + "/".join(HARDWARE_KEYS)),
    ("assert_remains_absent", "poll_ms"): (_is_positive_int, "positive int"),
}


def _check_params(label: str, fn: Callable[..., Any], params: dict[str, Any],
                  *, verb: str | None = None,
                  allow_missing_required: bool = False) -> None:
    """Enforce that `params` exactly fits `fn`'s keyword surface — no unknown names
    (a model can't smuggle params) AND no missing *required* ones. The missing
    check matters: a legal-looking but underspecified step (e.g. tap_text with no
    query) would otherwise pass offline validation, then crash run_recipe with a
    TypeError that neither the StepFailure nor INFRA_ERRORS catch sees — breaking
    the "static check rejects first" contract (§10.10). The leading context param
    (self/ctx, passed positionally) is excluded.

    ``allow_missing_required`` skips only the missing-required check. It exists for
    the §10.10 narrow exception (an Operation whose registered steps are a bare
    self-call receives its validated ``--with`` params at run time); callers decide
    when it applies. Unknown names and value types are still checked."""
    sig_params = list(inspect.signature(fn).parameters.values())[1:]  # drop ctx/self
    kwargs = [p for p in sig_params
              if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
    by_name = {p.name: p for p in kwargs}
    allowed = set(by_name)
    required = {p.name for p in kwargs if p.default is inspect.Parameter.empty}
    unknown = set(params) - allowed
    if unknown:
        raise RecipeError(
            f"{label}: unknown param(s) {sorted(unknown)}; allowed kwargs are "
            f"{sorted(allowed)} (recipe field names must equal kwargs)")
    missing = required - set(params)
    if missing and not allow_missing_required:
        raise RecipeError(
            f"{label}: missing required param(s) {sorted(missing)}; "
            f"required are {sorted(required)}")
    bad = [f"{name} must be {spec[1]}, got {value!r}"
           for name, value in params.items()
           if not (value is None and by_name[name].default is None)
           and (spec := _PARAM_TYPES.get(name)) and not spec[0](value)]
    bad += [f"{name} must be {spec[1]}, got {value!r}"
            for name, value in params.items()
            if not (value is None and by_name[name].default is None)
            and (spec := _ACTION_VALUE_CHECKS.get((verb or "", name))) and not spec[0](value)]
    if bad:
        raise RecipeError(f"{label}: " + "; ".join(bad))


def _resolve_step(
    step: dict[str, Any],
    *,
    allow_missing_required: bool = False,
    allow_unresolved_symbols: bool = False,
) -> tuple[str, Callable[..., Any], dict[str, Any]]:
    """Validate one step's shape and resolve it to (label, fn, kwargs). Raises
    RecipeError on anything malformed — no device side effects."""
    if not isinstance(step, dict):
        raise RecipeError(f"step must be a mapping, got {type(step).__name__}: {step!r}")
    has_action = "action" in step
    has_operation = "operation" in step
    if has_action == has_operation:
        raise RecipeError(f"step must have exactly one of action/operation: {step!r}")

    if has_action:
        name = step["action"]
        fn = ACTIONS.get(name)
        if fn is None:
            raise RecipeError(f"unknown action {name!r}; whitelist={sorted(ACTIONS)}")
        if "symbol" in step:
            _validate_raw_symbol_step(step)
            if not allow_unresolved_symbols:
                raise RecipeError(
                    f"action {name}: unresolved 'symbol' step {step.get('symbol')!r}; "
                    "expand with expand_symbol_steps before validation/dispatch"
                )
            return f"action {name}", fn, {}
        params = {
            k: v for k, v in step.items()
            if k != "action" and k not in _INTERNAL_STEP_METADATA_KEYS
        }
        label = f"action {name}"
    else:
        name = step["operation"]
        operations = _active_operations()
        fn = operations.get(name)
        if fn is None:
            raise RecipeError(
                f"unknown operation {name!r}; registered={sorted(operations)} "
                f"(did you import the app's recipes package?)")
        # operation step is a closed shape: only `operation` + `args`. A stray
        # top-level key (e.g. `query`) must be rejected, not silently dropped —
        # otherwise IR review gets false safety (the swallowed param looks honored).
        extra = set(step) - {"operation", "args"}
        if extra:
            raise RecipeError(
                f"operation {name}: unexpected top-level key(s) {sorted(extra)}; "
                f"operation steps allow only 'operation' and 'args'")
        args = step.get("args", {})
        if not isinstance(args, dict):
            raise RecipeError(
                f"operation {name}: 'args' must be a mapping, got {type(args).__name__}")
        params = dict(args)
        label = f"operation {name}"

    _check_params(label, fn, params, verb=str(name) if has_action else None,
                  allow_missing_required=allow_missing_required)
    return label, fn, params


def validate_recipe(
    steps: list[dict[str, Any]],
    *,
    allow_missing_required: bool = False,
    allow_unresolved_symbols: bool = False,
) -> None:
    """Offline (no-VM) static check: every step resolves to a known action /
    operation, is a closed shape, and its params exactly fit the target's kwargs
    (no unknown, no missing-required, right value types). Raises RecipeError on the
    first problem. This is the `--validate` path — it never touches the device.

    ``allow_missing_required`` (default off) is only for the §10.10 bare self-call
    exception; see ``_check_params``.
    ``allow_unresolved_symbols`` (default off) permits static syntax checking of
    raw ``tap_ax`` + ``symbol`` steps during app-package recipe registration before
    run-time artifact routing resolves the exact ``app-symbols.v1`` file."""
    for i, step in enumerate(steps):
        try:
            _resolve_step(
                step,
                allow_missing_required=allow_missing_required,
                allow_unresolved_symbols=allow_unresolved_symbols,
            )
        except RecipeError as e:
            raise RecipeError(f"step[{i}]: {e}") from None


def validate_recipe_for_app(
    steps: list[dict[str, Any]],
    profile: Any,
    *,
    allow_missing_required: bool = False,
    symbol_binding: Any | None = None,
    screen_asset_doc: Mapping[str, Any] | None = None,
    effect_mode: effects.EffectMode | None = None,
    mode: effects.EffectMode | None = None,
    trial_draft_recipes: bool = False,
    confirm_trial_effects: bool = False,
    trial_data_isolation: str | None = None,
) -> None:
    """App-aware outer static gate.

    `validate_recipe` deliberately stays app-agnostic; profile-sensitive symbol
    expansion and effect policy (multi-verb sets, wildcard rejection, and
    completeness/isolation gates) live here and at dispatch time so direct
    execution cannot bypass them. ``allow_missing_required`` is passed through
    to `validate_recipe` and never relaxes the effect policy.
    """
    if effect_mode is not None and mode is not None and effect_mode != mode:
        raise ValueError(f"conflicting effect_mode={effect_mode!r} and mode={mode!r}")
    resolved_mode = effect_mode if effect_mode is not None else mode
    effective_steps = (
        expand_symbol_steps(
            steps,
            symbol_binding=symbol_binding,
            screen_asset_doc=screen_asset_doc,
        )
        if recipe_has_unresolved_symbols(steps)
        else steps
    )
    validate_recipe(effective_steps, allow_missing_required=allow_missing_required)
    effective_isolation = (
        trial_data_isolation
        if trial_data_isolation is not None
        else effects.profile_trial_data_isolation(profile)
    )
    effects.validate_allowed_effects(
        effective_steps,
        allowed_effects=set(getattr(profile, "allowed_effects", []) or []),
        label=f"app {getattr(profile, 'app', '<unknown>')} recipe",
        mode=resolved_mode,
        trial_draft_recipes=trial_draft_recipes,
        confirm_trial_effects=confirm_trial_effects,
        trial_data_isolation=effective_isolation,
        profile=profile,
    )


def dispatch_step(ctx: CaseContext, step: dict[str, Any]) -> Any:
    """Record local definition location without attributing nested executor failures."""
    try:
        resolved = _resolve_step(step)
    except RecipeError as exc:
        exc._failure_local = True
        raise
    try:
        return _dispatch_step(ctx, step, resolved)
    except Exception as exc:
        # A nested executor's local step is not a step owned by its caller.
        exc._failure_local = 'action' in step
        exc._failure_operation = 'operation' in step
        exc._failure_assertion = (
            'action' in step and str(step['action']).startswith('assert')
            and isinstance(exc, StepFailure) and exc.result.action.startswith('assert')
        )
        raise


def _dispatch_step(ctx: CaseContext, step: dict[str, Any], resolved: tuple) -> Any:
    """Validate + execute one step against `ctx`. Verbs/executors raise
    StepFailure / INFRA_ERRORS on their own terms — run_recipe classifies them."""
    _label, fn, params = resolved
    decision = effects.step_effect(step)
    allowed_effects = getattr(ctx, "allowed_effects", None)
    if allowed_effects is not None:
        try:
            effects.validate_step_effect(
                step,
                allowed_effects=allowed_effects,
                label="runtime step",
                mode=getattr(ctx, "effect_mode", None),
                trial_draft_recipes=getattr(ctx, "trial_draft_recipes", False),
                confirm_trial_effects=getattr(ctx, "confirm_trial_effects", False),
                trial_data_isolation=getattr(ctx, "trial_data_isolation", None),
            )
        except effects.EffectPolicyError as exc:
            result = ctx._record(
                "effect_policy",
                False,
                str(exc),
                time.perf_counter(),
                kind=exc.code or effects.REASON_EFFECT_NOT_ALLOWED,
            )
            raise StepFailure(result) from exc
    op_name = str(step["operation"]) if "operation" in step else None
    previous_operation = getattr(ctx, "_current_operation", None)
    previous_effect = getattr(ctx, "_current_effect", None)
    symbol_bound = _is_symbol_bound_step(step)
    previous_symbol_id = getattr(ctx, "_current_symbol_id", None)
    previous_window_tag = getattr(ctx, "_current_window_tag", None)
    if op_name:
        ctx._current_operation = op_name
        ctx.api_mark("operation", operation=op_name)
    ctx._current_effect = decision
    if symbol_bound and not getattr(ctx, "_housekeeping_window_tag", None):
        sym_meta = step.get(effects.SYMBOL_BINDING_STEP_KEY) or {}
        sym_id = str(sym_meta.get("symbol_id") or "").strip() or None
        ctx._current_symbol_id = sym_id
        ctx._current_window_tag = f"symbol:{sym_id}" if sym_id else previous_window_tag
    try:
        try:
            result = fn(ctx, **params)
        except StepFailure:
            # Decision D4 + Round 1.5 Item 4: symbol-bound steps and symbol-using
            # cases fail closed with zero overlay-dismiss retry.
            if (
                not symbol_bound
                and not getattr(ctx, "suppress_overlay_dismiss", False)
                and _dismiss_blocking_overlay_once(ctx)
            ):
                return dispatch_step(ctx, step)
            raise
    finally:
        ctx._current_effect = previous_effect
        if symbol_bound:
            ctx._current_symbol_id = previous_symbol_id
            ctx._current_window_tag = previous_window_tag
        if op_name:
            ctx._current_operation = previous_operation
            ctx.api_mark("operation_end", operation=previous_operation)
    if symbol_bound and step.get("action") == "tap_ax":
        record_obs = getattr(ctx, "_record_symbol_observation", None)
        if callable(record_obs):
            record_obs(
                dict(step[effects.SYMBOL_BINDING_STEP_KEY]),
                result,
                screen_id=params.get("screen_id"),
            )
    # An operation drives the screen through its own UDS calls (login/logout
    # screenshot via login_capture_session, never via ctx verbs), so ctx's
    # one-frame OCR cache is stale afterwards. Invalidate it so the next assert
    # re-shoots the post-operation screen instead of a 0ms cache hit on the old.
    if "operation" in step:
        ctx.invalidate_frame()
    return result


def _dismiss_blocking_overlay_once(ctx: CaseContext) -> str | None:
    if getattr(ctx, "suppress_overlay_dismiss", False) or getattr(ctx, "_overlay_recovery_depth", 0):
        return None
    doc = getattr(ctx, "screen_asset_doc", None)
    if not isinstance(doc, dict):
        return None
    try:
        from autophone.assets import screens as screen_assets

        ocr = ctx.ocr_now(force=True)
        matched = screen_assets.match_screens(ocr, doc.get("screens") or [], statuses=("active", "draft"))
        if matched is not None:
            candidates = [screen_assets.screen_by_id(doc, matched.screen_id)]
        else:
            candidates = [s for s in doc.get("screens") or [] if isinstance(s, dict)]
    except Exception as exc:  # noqa: BLE001 - overlay recovery is advisory
        logger.debug("blocking overlay screen match unavailable: %r", exc)
        return None

    ax_cache: dict[str, Any] = {"fetched": False, "dump": None}
    for screen in candidates:
        for overlay in screen.get("blocking_overlays") or []:
            if not isinstance(overlay, dict):
                continue
            name = str(overlay.get("name") or "blocking_overlay")
            detect = overlay.get("detect") if isinstance(overlay.get("detect"), dict) else {}
            if not _overlay_detected(ctx, ocr, detect, _ax_cache=ax_cache):
                continue
            dismiss = overlay.get("dismiss")
            if not isinstance(dismiss, list) or not dismiss:
                continue
            t0 = time.perf_counter()
            setattr(ctx, "_overlay_recovery_depth", 1)
            try:
                for dismiss_step in dismiss:
                    dispatch_step(ctx, dismiss_step)
            except Exception as exc:  # noqa: BLE001
                logger.debug("blocking overlay dismiss failed name=%s: %r", name, exc)
                return None
            finally:
                setattr(ctx, "_overlay_recovery_depth", 0)
            ctx.invalidate_frame()
            ctx._record("overlay_dismissed", True, name, t0)
            return name
    return None


def _overlay_detected(
    ctx: CaseContext,
    ocr: dict[str, Any],
    detect: dict[str, Any],
    *,
    _ax_cache: dict[str, Any] | None = None,
) -> bool:
    if not isinstance(detect, dict) or not detect:
        return False
    from autophone.assets import screens as screen_assets

    synthetic_screen = {"blocking_overlays": [{"name": "overlay", "detect": detect}]}
    if screen_assets.detect_blocking_overlay(ocr, synthetic_screen) is not None:
        return True
    if detect.get("ax_text") and getattr(ctx, "ax", None) is not None:
        if _ax_cache is not None and _ax_cache.get("fetched"):
            dump = _ax_cache.get("dump")
        else:
            try:
                dump = ctx.ax.refresh()
            except Exception:  # noqa: BLE001
                dump = None
            if _ax_cache is not None:
                _ax_cache["fetched"] = True
                _ax_cache["dump"] = dump
        if dump is not None:
            return screen_assets.detect_blocking_overlay(ocr, synthetic_screen, ax_dump=dump) is not None
    return False


def run_recipe(steps: list[dict[str, Any]], ctx: CaseContext) -> tuple[str, str, str]:
    """Execute a recipe against `ctx`, returning (status, detail, fail_kind) with the
    SAME semantics as runner.run_case (§10.4): StepFailure→fail, INFRA_ERRORS→error,
    a clean finish with zero assert_* actions→error (no vacuous pass). fail_kind is
    the taxonomy `kind` of the failing action (empty unless status=='fail').

    Kept as a standalone classifier for the shadow harness (recipe_shadow path B);
    the production runner inlines dispatch_step under its own try/except so both
    paths classify identically (one site to keep in lockstep)."""
    status, detail, fail_kind = "pass", "", ""
    prev_suppress = getattr(ctx, "suppress_overlay_dismiss", False)
    set_suppress = recipe_uses_symbols(steps) and not prev_suppress
    if set_suppress:
        ctx.suppress_overlay_dismiss = True
    try:
        for step in steps:
            dispatch_step(ctx, step)
        if not any(r.action.startswith("assert") for r in ctx.results):
            status, detail = "error", "recipe asserted nothing (vacuous)"
    except StepFailure as e:
        status, detail, fail_kind = "fail", e.result.detail, e.result.kind
    except INFRA_ERRORS as e:
        status, detail = "error", f"{type(e).__name__}: {e}"
    finally:
        if set_suppress:
            ctx.suppress_overlay_dismiss = prev_suppress
    return status, detail, fail_kind


def load_recipe_document(path: str | Path) -> dict[str, Any]:
    """Load the complete recipe document for static planners and validators."""
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return _checked_recipe_document(doc, path)


def recipe_document_from_bytes(data: bytes, path: str | Path) -> dict[str, Any]:
    """Parse Recipe bytes already read, with ``load_recipe_document``'s checks.

    Lets a caller hash exactly the bytes it parsed (BL-186 execution identity).
    """
    doc = yaml.safe_load(data.decode("utf-8")) or {}
    return _checked_recipe_document(doc, path)


def _checked_recipe_document(doc: Any, path: str | Path) -> dict[str, Any]:
    if not isinstance(doc, dict):
        raise RecipeError(f"recipe {path} must be a mapping")
    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps:
        raise RecipeError(f"recipe {path} has no non-empty `steps` list")
    for idx, step in enumerate(steps):
        if isinstance(step, dict):
            private_keys = sorted(k for k in step if isinstance(k, str) and k.startswith("_"))
            if private_keys:
                raise RecipeError(
                    f"recipe {path} step[{idx}] must not declare internal key(s) {private_keys}"
                )
    return doc


def _canonical_steps_for_digest(steps: Any) -> Any:
    """Normalize expanded symbol steps back to their authored form for digest stability.

    Ensures ``recipe_steps_digest(raw_steps) == recipe_steps_digest(expanded_steps)``
    while keeping ``binding_sha256`` tracked independently on run/case identities.
    """
    if not isinstance(steps, list):
        return steps
    normalized: list[Any] = []
    for step in steps:
        if isinstance(step, dict):
            binding = step.get(effects.SYMBOL_BINDING_STEP_KEY)
            if (
                isinstance(binding, dict)
                and isinstance(binding.get("symbol_id"), str)
                and binding["symbol_id"].strip()
            ):
                authored: dict[str, Any] = {
                    "action": step.get("action", "tap_ax"),
                    "symbol": binding["symbol_id"].strip(),
                }
                if "label" in step:
                    authored["label"] = step["label"]
                normalized.append(authored)
                continue
            if effects.SYMBOL_EFFECTS_STEP_KEY in step:
                normalized.append(
                    {k: v for k, v in step.items() if k != effects.SYMBOL_EFFECTS_STEP_KEY}
                )
                continue
        normalized.append(step)
    return normalized


def _typed_canonical(value: Any) -> Any:
    """Encode a parsed YAML value with explicit types, JSON-serialisable.

    Every scalar carries its type name, so ``1`` / ``"1"``, ``true`` /
    ``"true"`` and an unquoted date / its string differ.  Mapping entries are
    ordered by (key type name, ``repr(key)``), which is total for mixed key
    types where ``json.dumps(sort_keys=True)`` raises.
    """
    if value is None:
        return ["none"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        return ["float", repr(value)]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, dict):
        entries = sorted(
            value.items(), key=lambda item: (type(item[0]).__name__, repr(item[0]))
        )
        return ["map", [[_typed_canonical(k), _typed_canonical(v)] for k, v in entries]]
    if isinstance(value, (list, tuple)):
        return [type(value).__name__, [_typed_canonical(item) for item in value]]
    return [type(value).__name__, repr(value)]


def recipe_steps_digest(steps: Any) -> str:
    """Canonical SHA-256 of parsed Recipe steps with type-tagged encoding.

    The Runner records it for the steps it actually executed; the platform
    compares it with the steps parsed from the Recipe bytes it hashed, so a run
    records a Recipe file digest only when that file's content was executed.
    """
    payload = json.dumps(
        _typed_canonical(_canonical_steps_for_digest(steps)),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_recipe(path: str | Path) -> list[dict[str, Any]]:
    """Load a recipe YAML, returning its `steps` list for the runtime runner."""
    doc = load_recipe_document(path)
    steps = doc["steps"]
    assert isinstance(steps, list)  # guaranteed by load_recipe_document
    return steps


from autophone.execution._entity_binding_hooks import (  # noqa: E402
    install_entity_binding_runtime_hooks as _install_entity_binding_runtime_hooks,
)

_install_entity_binding_runtime_hooks()
