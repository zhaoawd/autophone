from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
import threading
from typing import Any, Literal

from autophone.assets.effect_markers import (
    _IRREVERSIBLE_MARKERS,
    _TRIGGER_ACTION_TEXT_KEYS,
    ordered_irreversible_markers as _ordered_irreversible_markers,
)

REVERSIBLE = "reversible"
IRREVERSIBLE = "irreversible"

COMPLETENESS_EXHAUSTIVE: Literal["exhaustive"] = "exhaustive"
COMPLETENESS_LOWER_BOUND: Literal["lower_bound"] = "lower_bound"
COMPLETENESS_UNKNOWN: Literal["unknown"] = "unknown"
EffectCompleteness = Literal["exhaustive", "lower_bound", "unknown"]
EFFECT_COMPLETENESS_VALUES: frozenset[str] = frozenset({
    COMPLETENESS_EXHAUSTIVE,
    COMPLETENESS_LOWER_BOUND,
    COMPLETENESS_UNKNOWN,
})

EffectMode = Literal["active", "draft"]
EFFECT_MODES: frozenset[str] = frozenset({"active", "draft"})

TRIAL_DATA_ISOLATION_UNISOLATED = "unisolated"
TRIAL_DATA_ISOLATION_ISOLATED = "isolated"

REASON_EFFECT_NOT_ALLOWED = "effect_not_allowed"
REASON_MULTI_EFFECT_WILDCARD_REJECTED = "multi_effect_wildcard_rejected"
REASON_EFFECT_COMPLETENESS_INSUFFICIENT = "effect_completeness_insufficient"
REASON_TRIAL_DRAFT_REQUIRED = "trial_draft_required"
REASON_TRIAL_EFFECTS_NOT_CONFIRMED = "trial_effects_not_confirmed"
REASON_TRIAL_DATA_UNISOLATED = "trial_data_unisolated"
REASON_UNRESOLVED_SYMBOL_EFFECT = "unresolved_symbol_effect"
REASON_MALFORMED_SYMBOL_EFFECTS = "malformed_symbol_effects"

# Internal step metadata keys populated by the symbol binding loader (M2/M3).
SYMBOL_EFFECTS_STEP_KEY = "_symbol_effects"
SYMBOL_BINDING_STEP_KEY = "_symbol_binding"
_SYMBOL_METADATA_KEYS: frozenset[str] = frozenset({
    SYMBOL_EFFECTS_STEP_KEY,
    SYMBOL_BINDING_STEP_KEY,
})

_EXECUTOR_EFFECTS: dict[str, "EffectDecision"] = {}
_APP_EXECUTOR_EFFECTS: dict[str, dict[str, "EffectDecision"]] = {}
_APP_ROOT_EXECUTOR_EFFECTS: dict[
    tuple[str, str], dict[str, "EffectDecision"]
] = {}
_APP_EXECUTOR_EFFECT_ROOTS: dict[str, str] = {}
_ACTIVE_EXECUTOR_EFFECT_IDENTITY: tuple[str, str] | None = None
_EXECUTOR_EFFECTS_LOCK = threading.RLock()
_SCOPED_EXECUTOR_EFFECTS: ContextVar[dict[str, "EffectDecision"] | None] = ContextVar(
    "scoped_executor_effects",
    default=None,
)


def _format_verbs(verbs: frozenset[str], fallback: str = "") -> str:
    if not verbs:
        return fallback
    return ",".join(sorted(verbs))


def _normalize_verbs(raw_verbs: Any) -> frozenset[str]:
    if isinstance(raw_verbs, (str, bytes)) or not isinstance(raw_verbs, Iterable):
        raise ValueError(f"effect verbs must be an iterable of non-empty strings, got {raw_verbs!r}")
    normalized: set[str] = set()
    for item in raw_verbs:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"effect verb must be a non-empty string, got {item!r}")
        normalized.add(item.strip())
    return frozenset(normalized)


def _validate_completeness(value: Any) -> EffectCompleteness:
    if not isinstance(value, str) or value not in EFFECT_COMPLETENESS_VALUES:
        raise ValueError(
            f"unknown effect completeness {value!r}; "
            f"expected one of {sorted(EFFECT_COMPLETENESS_VALUES)}"
        )
    return value  # type: ignore[return-value]


@dataclass(frozen=True)
class EffectDecision:
    effect: str
    verb: str
    reason: str
    declared: bool = True
    verbs: frozenset[str] = field(default=None)  # type: ignore[assignment]
    completeness: EffectCompleteness = COMPLETENESS_EXHAUSTIVE
    symbol_bound: bool = False
    symbol_id: str | None = None

    def __post_init__(self) -> None:
        if self.effect not in {REVERSIBLE, IRREVERSIBLE}:
            raise ValueError(f"unknown effect {self.effect!r}")
        _validate_completeness(self.completeness)
        if self.verbs is None:
            derived = (
                frozenset({self.verb})
                if self.effect == IRREVERSIBLE and self.verb
                else frozenset()
            )
            object.__setattr__(self, "verbs", derived)
        else:
            normalized = _normalize_verbs(self.verbs)
            if normalized and self.effect == REVERSIBLE:
                raise ValueError("reversible EffectDecision cannot declare irreversible verbs")
            object.__setattr__(self, "verbs", normalized)

    @property
    def irreversible(self) -> bool:
        return (
            self.effect == IRREVERSIBLE
            or bool(self.verbs)
            or self.completeness != COMPLETENESS_EXHAUSTIVE
        )

    @property
    def is_multi_effect(self) -> bool:
        return len(self.verbs) >= 2

    @property
    def incomplete(self) -> bool:
        return self.completeness != COMPLETENESS_EXHAUSTIVE


@dataclass(frozen=True)
class EffectEvaluation:
    """Outcome of evaluating one EffectDecision against profile & trial gates."""

    allowed: bool
    code: str | None
    codes: tuple[str, ...]
    disallowed_verbs: tuple[str, ...]
    missing_trial_requirements: tuple[str, ...]
    detail: str
    decision: EffectDecision


class EffectPolicyError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = REASON_EFFECT_NOT_ALLOWED,
        violations: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.violations: list[dict[str, Any]] = list(violations or [])


def irreversible_markers(text: str) -> frozenset[str]:
    """Extract all irreversible effect verbs matched in ``text``."""
    return frozenset(_ordered_irreversible_markers(text))


def irreversible_marker(text: str) -> str | None:
    """Return the first matched irreversible effect verb, or ``None``.

    Retained for backward compatibility with single-string callers. Prefer
    :func:`irreversible_markers` so composite phrases like ``"下单并支付"``
    never drop secondary irreversible verbs.
    """
    ordered = _ordered_irreversible_markers(text)
    return ordered[0] if ordered else None


def symbol_effect_decision(
    effects: Mapping[str, Any] | None = None,
    *,
    verbs: Iterable[str] | None = None,
    completeness: EffectCompleteness | None = None,
    symbol_id: str | None = None,
    reason: str = "symbol_declared_effects",
) -> EffectDecision:
    """Construct a validated :class:`EffectDecision` from symbol-declared effects."""
    raw_verbs: Any = verbs
    raw_completeness: Any = completeness
    resolved_symbol_id = symbol_id
    if effects is not None:
        if not isinstance(effects, Mapping):
            raise ValueError(f"symbol effects must be a mapping, got {type(effects).__name__}")
        if raw_verbs is None:
            if "verbs" not in effects:
                raise ValueError("symbol effects missing required 'verbs'")
            raw_verbs = effects["verbs"]
        if raw_completeness is None:
            if "completeness" not in effects:
                raise ValueError("symbol effects missing required 'completeness'")
            raw_completeness = effects["completeness"]
        if resolved_symbol_id is None and isinstance(effects.get("symbol_id"), str):
            resolved_symbol_id = effects["symbol_id"].strip() or None
    if raw_verbs is None:
        raise ValueError("symbol effects missing required 'verbs'")
    if raw_completeness is None:
        raise ValueError("symbol effects missing required 'completeness'")
    normalized_verbs = _normalize_verbs(raw_verbs)
    validated_completeness = _validate_completeness(raw_completeness)
    is_read_only = not normalized_verbs and validated_completeness == COMPLETENESS_EXHAUSTIVE
    effect = REVERSIBLE if is_read_only else IRREVERSIBLE
    verb_label = _format_verbs(normalized_verbs, fallback=resolved_symbol_id or "")
    return EffectDecision(
        effect=effect,
        verb=verb_label,
        reason=reason,
        declared=True,
        verbs=normalized_verbs,
        completeness=validated_completeness,
        symbol_bound=True,
        symbol_id=resolved_symbol_id,
    )


def strip_symbol_effect_metadata(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return shallow copies of ``steps`` without internal symbol effect metadata."""
    cleaned: list[dict[str, Any]] = []
    for step in steps:
        if isinstance(step, dict) and any(key in step for key in _SYMBOL_METADATA_KEYS):
            cleaned.append({k: v for k, v in step.items() if k not in _SYMBOL_METADATA_KEYS})
        else:
            cleaned.append(step)
    return cleaned


def profile_trial_data_isolation(profile: Any) -> str:
    """Read ``trial_data_isolation`` from an AppProfile, failing closed to ``unisolated``."""
    if profile is None:
        return TRIAL_DATA_ISOLATION_UNISOLATED
    value = getattr(profile, "trial_data_isolation", None)
    if value is None:
        raw = getattr(profile, "raw", None)
        if isinstance(raw, Mapping):
            value = raw.get("trial_data_isolation")
    if value == TRIAL_DATA_ISOLATION_ISOLATED:
        return TRIAL_DATA_ISOLATION_ISOLATED
    return TRIAL_DATA_ISOLATION_UNISOLATED


def _extract_symbol_decision_from_params(params: Mapping[str, Any]) -> EffectDecision | None:
    has_binding = SYMBOL_BINDING_STEP_KEY in params
    has_effects = SYMBOL_EFFECTS_STEP_KEY in params
    raw_symbol = params.get("symbol")
    explicit_symbol_id = raw_symbol.strip() if isinstance(raw_symbol, str) and raw_symbol.strip() else None

    if not has_binding and not has_effects:
        if "symbol" in params:
            label = explicit_symbol_id or "<unresolved_symbol>"
            return EffectDecision(
                IRREVERSIBLE,
                label,
                REASON_UNRESOLVED_SYMBOL_EFFECT,
                declared=False,
                verbs=frozenset(),
                completeness=COMPLETENESS_UNKNOWN,
                symbol_bound=True,
                symbol_id=explicit_symbol_id,
            )
        return None

    decisions: list[EffectDecision] = []
    symbol_id = explicit_symbol_id
    try:
        if has_binding:
            binding = params[SYMBOL_BINDING_STEP_KEY]
            if not isinstance(binding, Mapping):
                raise ValueError("_symbol_binding must be a mapping")
            raw_id = binding.get("symbol_id")
            if isinstance(raw_id, str) and raw_id.strip():
                symbol_id = raw_id.strip()
            if "effects" not in binding:
                raise ValueError("_symbol_binding missing required 'effects'")
            decisions.append(symbol_effect_decision(binding["effects"], symbol_id=symbol_id))
        if has_effects:
            decisions.append(symbol_effect_decision(params[SYMBOL_EFFECTS_STEP_KEY], symbol_id=symbol_id))
        if len(decisions) == 2 and (
            decisions[0].verbs != decisions[1].verbs
            or decisions[0].completeness != decisions[1].completeness
        ):
            raise ValueError("conflicting _symbol_binding.effects and _symbol_effects")
    except ValueError:
        return EffectDecision(
            IRREVERSIBLE,
            symbol_id or "<malformed_symbol_effects>",
            REASON_MALFORMED_SYMBOL_EFFECTS,
            declared=False,
            verbs=frozenset(),
            completeness=COMPLETENESS_UNKNOWN,
            symbol_bound=True,
            symbol_id=symbol_id,
        )
    return decisions[0]


def classify_freeform_action(
    verb: str,
    target: str = "",
    *,
    reversible: bool | None = None,
) -> EffectDecision:
    blob = f"{verb or ''} {target or ''}"
    markers = irreversible_markers(blob)
    if reversible is False:
        verb_str = str(verb or "").strip()
        verbs = markers if markers else (frozenset({verb_str}) if verb_str else frozenset())
        verb_label = _format_verbs(verbs, fallback=str(verb or ""))
        return EffectDecision(
            IRREVERSIBLE,
            verb_label,
            "self_declared_irreversible",
            verbs=verbs,
        )
    if markers:
        return EffectDecision(
            IRREVERSIBLE,
            _format_verbs(markers),
            "irreversible_marker",
            verbs=markers,
        )
    return EffectDecision(
        REVERSIBLE,
        str(verb or ""),
        "no_irreversible_marker",
        verbs=frozenset(),
    )


def action_effect(action: str, params: dict[str, Any] | None = None) -> EffectDecision:
    params = params or {}
    symbol_decision = _extract_symbol_decision_from_params(params)
    if symbol_decision is not None:
        return symbol_decision
    action_name = str(action or "")
    name_markers = irreversible_markers(action_name)
    text_keys = _TRIGGER_ACTION_TEXT_KEYS.get(action_name, ())
    text_bits = [str(params.get(key) or "") for key in text_keys]
    target_markers = irreversible_markers(" ".join(text_bits))
    if name_markers:
        combined = name_markers | target_markers
        return EffectDecision(
            IRREVERSIBLE,
            _format_verbs(combined),
            "action_name_marker",
            verbs=combined,
        )
    if target_markers:
        return EffectDecision(
            IRREVERSIBLE,
            _format_verbs(target_markers),
            "trigger_target_marker",
            verbs=target_markers,
        )
    return EffectDecision(
        REVERSIBLE,
        action_name,
        "dispatcher_action_default",
        verbs=frozenset(),
    )


def register_executor_effect(
    name: str,
    *,
    effect: str,
    verb: str | None = None,
    verbs: Iterable[str] | None = None,
    completeness: EffectCompleteness = COMPLETENESS_EXHAUSTIVE,
) -> None:
    if effect not in {REVERSIBLE, IRREVERSIBLE}:
        raise ValueError(f"unknown effect {effect!r}")
    normalized_verbs = _normalize_verbs(verbs) if verbs is not None else None
    resolved_verb = (
        str(verb)
        if verb is not None
        else (_format_verbs(normalized_verbs, fallback=str(name)) if normalized_verbs is not None else str(name))
    )
    with _EXECUTOR_EFFECTS_LOCK:
        _EXECUTOR_EFFECTS[str(name)] = EffectDecision(
            effect=effect,
            verb=resolved_verb,
            reason="executor_effect_registered",
            declared=True,
            verbs=normalized_verbs,  # type: ignore[arg-type]
            completeness=completeness,
        )


def register_app_executor_effects(
    app: str,
    decisions: dict[str, EffectDecision],
    *,
    root: Path | None = None,
) -> None:
    """Pin executor effect declarations captured from one App recipe package."""
    with _EXECUTOR_EFFECTS_LOCK:
        key = str(app)
        copied = dict(decisions)
        _APP_EXECUTOR_EFFECTS[key] = copied
        if root is not None:
            resolved_root = str(Path(root).resolve())
            _APP_ROOT_EXECUTOR_EFFECTS[(key, resolved_root)] = copied
            _APP_EXECUTOR_EFFECT_ROOTS[key] = resolved_root


def has_app_executor_effects(app: str, *, root: Path | None = None) -> bool:
    with _EXECUTOR_EFFECTS_LOCK:
        key = str(app)
        if root is None:
            return key in _APP_EXECUTOR_EFFECTS
        return (key, str(Path(root).resolve())) in _APP_ROOT_EXECUTOR_EFFECTS


def executor_effects_for_app(
    app: str,
    *,
    root: Path | None = None,
) -> dict[str, EffectDecision]:
    with _EXECUTOR_EFFECTS_LOCK:
        key = str(app)
        if root is None:
            return dict(_APP_EXECUTOR_EFFECTS.get(key) or {})
        identity = (key, str(Path(root).resolve()))
        return dict(_APP_ROOT_EXECUTOR_EFFECTS.get(identity) or {})


def activate_app_executor_effects(
    app: str,
    *,
    root: Path | None = None,
) -> dict[str, EffectDecision]:
    """Rebuild the process-global compatibility mirror from one App cache."""
    global _ACTIVE_EXECUTOR_EFFECT_IDENTITY
    with _EXECUTOR_EFFECTS_LOCK:
        key = str(app)
        if root is None:
            decisions = dict(_APP_EXECUTOR_EFFECTS.get(key) or {})
            resolved_root = _APP_EXECUTOR_EFFECT_ROOTS.get(key)
        else:
            resolved_root = str(Path(root).resolve())
            decisions = dict(
                _APP_ROOT_EXECUTOR_EFFECTS.get((key, resolved_root)) or {}
            )
            _APP_EXECUTOR_EFFECTS[key] = dict(decisions)
            _APP_EXECUTOR_EFFECT_ROOTS[key] = resolved_root
        _EXECUTOR_EFFECTS.clear()
        _EXECUTOR_EFFECTS.update(decisions)
        _ACTIVE_EXECUTOR_EFFECT_IDENTITY = (
            (key, resolved_root) if resolved_root is not None else None
        )
        return decisions


def clear_app_executor_effects(app: str, *, root: Path | None = None) -> None:
    global _ACTIVE_EXECUTOR_EFFECT_IDENTITY
    with _EXECUTOR_EFFECTS_LOCK:
        key = str(app)
        if root is None:
            _APP_EXECUTOR_EFFECTS.pop(key, None)
            _APP_EXECUTOR_EFFECT_ROOTS.pop(key, None)
            for identity in [item for item in _APP_ROOT_EXECUTOR_EFFECTS if item[0] == key]:
                _APP_ROOT_EXECUTOR_EFFECTS.pop(identity, None)
            active_matches = bool(
                _ACTIVE_EXECUTOR_EFFECT_IDENTITY
                and _ACTIVE_EXECUTOR_EFFECT_IDENTITY[0] == key
            )
        else:
            resolved_root = str(Path(root).resolve())
            identity = (key, resolved_root)
            _APP_ROOT_EXECUTOR_EFFECTS.pop(identity, None)
            active_matches = _ACTIVE_EXECUTOR_EFFECT_IDENTITY == identity
            if _APP_EXECUTOR_EFFECT_ROOTS.get(key) == resolved_root:
                _APP_EXECUTOR_EFFECTS.pop(key, None)
                _APP_EXECUTOR_EFFECT_ROOTS.pop(key, None)
        if active_matches:
            _EXECUTOR_EFFECTS.clear()
            _ACTIVE_EXECUTOR_EFFECT_IDENTITY = None


@contextmanager
def executor_effect_scope(
    app: str,
    *,
    root: Path | None = None,
) -> Iterator[dict[str, EffectDecision]]:
    """Resolve executor effects against one App for the current execution context."""
    decisions = executor_effects_for_app(app, root=root)
    token = _SCOPED_EXECUTOR_EFFECTS.set(decisions)
    try:
        yield decisions
    finally:
        _SCOPED_EXECUTOR_EFFECTS.reset(token)


@contextmanager
def executor_effect_snapshot_scope(
    decisions: dict[str, EffectDecision],
) -> Iterator[dict[str, EffectDecision]]:
    """Install an already captured effect snapshot without holding registry locks."""
    copied = dict(decisions)
    token = _SCOPED_EXECUTOR_EFFECTS.set(copied)
    try:
        yield copied
    finally:
        _SCOPED_EXECUTOR_EFFECTS.reset(token)


def executor_effect(name: str) -> EffectDecision:
    scoped = _SCOPED_EXECUTOR_EFFECTS.get()
    with _EXECUTOR_EFFECTS_LOCK:
        registered = (scoped if scoped is not None else _EXECUTOR_EFFECTS).get(str(name))
    if registered is not None:
        return registered
    return EffectDecision(
        IRREVERSIBLE,
        str(name),
        "executor_missing_effect_declaration",
        declared=False,
    )


def step_effect(step: dict[str, Any]) -> EffectDecision:
    if not isinstance(step, Mapping):
        return EffectDecision(IRREVERSIBLE, "<malformed>", "malformed_step", declared=False)
    if "action" in step:
        return action_effect(
            str(step.get("action") or ""),
            {k: v for k, v in step.items() if k != "action"},
        )
    if "operation" in step:
        return executor_effect(str(step.get("operation") or ""))
    return EffectDecision(IRREVERSIBLE, "<malformed>", "malformed_step", declared=False)


def recipe_step_effects(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-step effect decisions of a shape-valid Recipe, for read-only display.

    Operation effects resolve against the active executor scope, so callers
    should wrap this in the App's ``operation_scope``.
    """
    rows: list[dict[str, Any]] = []
    for idx, step in enumerate(steps):
        decision = step_effect(step)
        kind = "action" if isinstance(step, Mapping) and "action" in step else (
            "operation" if isinstance(step, Mapping) and "operation" in step else ""
        )
        row: dict[str, Any] = {
            "step_index": idx,
            "kind": kind,
            "name": str(step.get(kind) or "") if kind else "",
            "effect": decision.effect,
            "verb": decision.verb,
            "verbs": sorted(decision.verbs),
            "completeness": decision.completeness,
            "reason": decision.reason,
            "declared": decision.declared,
            "symbol_bound": decision.symbol_bound,
        }
        if decision.symbol_id is not None:
            row["symbol_id"] = decision.symbol_id
        rows.append(row)
    return rows


# Case-level Operation roles are owned by the kernel asset module.
from autophone.assets.operations import CASE_OPERATION_ROLES  # noqa: E402


def case_operation_effects(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-ref effect decisions of a Case's setup / teardown / post-case cleanup.

    Each ``{"op": name, "with": {...}}`` ref is judged like the Operation step
    the Runner dispatches for it (``executor_effect``); a ref without a usable
    ``op`` (or a role that is not a list) is ``irreversible`` / ``malformed_step``,
    the same fail-closed judgement ``step_effect`` gives a malformed Recipe step.
    Read-only display; resolve inside the App's ``operation_scope``.
    """
    rows: list[dict[str, Any]] = []
    for role in CASE_OPERATION_ROLES:
        refs = case.get(role)
        if not refs:
            continue
        items = refs if isinstance(refs, list) else [refs]
        for idx, ref in enumerate(items):
            op = ref.get("op") if isinstance(ref, dict) else None
            if isinstance(op, str) and op.strip() and isinstance(refs, list):
                name = op
                decision = executor_effect(op)
            else:
                name = ""
                decision = EffectDecision(IRREVERSIBLE, "<malformed>", "malformed_step", declared=False)
            rows.append({
                "role": role,
                "step_index": idx,
                "kind": "operation",
                "name": name,
                "effect": decision.effect,
                "verb": decision.verb,
                "verbs": sorted(decision.verbs),
                "completeness": decision.completeness,
                "reason": decision.reason,
                "declared": decision.declared,
            })
    return rows


def _resolve_policy_inputs(
    *,
    allowed_effects: Iterable[str] | None,
    mode: EffectMode | None,
    trial_draft_recipes: bool,
    confirm_trial_effects: bool,
    trial_data_isolation: str | None,
    profile: Any,
) -> tuple[set[str], EffectMode, bool, bool, str]:
    if mode is not None and mode not in EFFECT_MODES:
        raise ValueError(f"unknown effect evaluation mode {mode!r}; expected 'active' or 'draft'")
    strict_trial_draft = trial_draft_recipes is True
    strict_confirm_trial = confirm_trial_effects is True
    effective_mode: EffectMode = mode if mode is not None else ("draft" if strict_trial_draft else "active")
    if allowed_effects is None and profile is not None:
        raw_allowed = getattr(profile, "allowed_effects", []) or []
    else:
        raw_allowed = allowed_effects or []
    allowed = {str(item).strip() for item in raw_allowed if str(item).strip()}
    if trial_data_isolation is not None:
        effective_isolation = (
            TRIAL_DATA_ISOLATION_ISOLATED
            if trial_data_isolation == TRIAL_DATA_ISOLATION_ISOLATED
            else TRIAL_DATA_ISOLATION_UNISOLATED
        )
    else:
        effective_isolation = profile_trial_data_isolation(profile)
    return allowed, effective_mode, strict_trial_draft, strict_confirm_trial, effective_isolation


def evaluate_effect_decision(
    decision: EffectDecision,
    *,
    allowed_effects: Iterable[str] | None = None,
    mode: EffectMode | None = None,
    trial_draft_recipes: bool = False,
    confirm_trial_effects: bool = False,
    trial_data_isolation: str | None = None,
    profile: Any = None,
) -> EffectEvaluation:
    """Evaluate one :class:`EffectDecision` against allowed verbs, wildcard rules,
    and completeness/isolation gates."""
    (
        allowed,
        effective_mode,
        strict_trial_draft,
        strict_confirm_trial,
        effective_isolation,
    ) = _resolve_policy_inputs(
        allowed_effects=allowed_effects,
        mode=mode,
        trial_draft_recipes=trial_draft_recipes,
        confirm_trial_effects=confirm_trial_effects,
        trial_data_isolation=trial_data_isolation,
        profile=profile,
    )

    if decision.symbol_bound and not decision.declared:
        code = (
            REASON_UNRESOLVED_SYMBOL_EFFECT
            if decision.reason == REASON_UNRESOLVED_SYMBOL_EFFECT
            else REASON_MALFORMED_SYMBOL_EFFECTS
        )
        detail = (
            f"symbol {decision.symbol_id or decision.verb} effect declaration is "
            f"{decision.reason}; expand and validate app-symbols before dispatch"
        )
        return EffectEvaluation(
            allowed=False,
            code=code,
            codes=(code,),
            disallowed_verbs=(),
            missing_trial_requirements=(),
            detail=detail,
            decision=decision,
        )

    if not decision.irreversible:
        return EffectEvaluation(
            allowed=True,
            code=None,
            codes=(),
            disallowed_verbs=(),
            missing_trial_requirements=(),
            detail="",
            decision=decision,
        )

    codes: list[str] = []
    details: list[str] = []
    missing_trial: list[str] = []
    disallowed_verbs: tuple[str, ...] = ()

    # Gate 1: Completeness & isolated draft trial admission gate.
    if decision.incomplete:
        if effective_mode == "active":
            codes.append(REASON_EFFECT_COMPLETENESS_INSUFFICIENT)
            details.append(
                f"completeness={decision.completeness} is blocked in active mode "
                "(requires exhaustive effects)"
            )
        else:
            if not strict_trial_draft:
                missing_trial.append("trial_draft_recipes")
            if not strict_confirm_trial:
                missing_trial.append("confirm_trial_effects")
            if effective_isolation != TRIAL_DATA_ISOLATION_ISOLATED:
                missing_trial.append("trial_data_isolation=isolated")
            if missing_trial:
                if not strict_trial_draft:
                    codes.append(REASON_TRIAL_DRAFT_REQUIRED)
                elif not strict_confirm_trial:
                    codes.append(REASON_TRIAL_EFFECTS_NOT_CONFIRMED)
                else:
                    codes.append(REASON_TRIAL_DATA_UNISOLATED)
                details.append(
                    f"completeness={decision.completeness} draft trial requires "
                    + ", ".join(missing_trial)
                )

    # Gate 2: Concrete verb authorization & multi-effect wildcard rejection.
    has_wildcard = IRREVERSIBLE in allowed
    if decision.verbs:
        missing_verbs = tuple(sorted(decision.verbs - allowed))
        if missing_verbs:
            if decision.is_multi_effect:
                disallowed_verbs = missing_verbs
                if has_wildcard:
                    codes.append(REASON_MULTI_EFFECT_WILDCARD_REJECTED)
                    details.append(
                        "multi-effect action rejects 'irreversible' wildcard; "
                        f"explicitly allow concrete verbs {list(missing_verbs)}"
                    )
                else:
                    codes.append(REASON_EFFECT_NOT_ALLOWED)
                    details.append(f"disallowed verbs {list(missing_verbs)}")
            elif not has_wildcard:
                disallowed_verbs = missing_verbs
                codes.append(REASON_EFFECT_NOT_ALLOWED)
                details.append(f"disallowed verb {missing_verbs[0]!r}")
    elif decision.effect == IRREVERSIBLE and not decision.symbol_bound and not has_wildcard:
        codes.append(REASON_EFFECT_NOT_ALLOWED)
        details.append(f"irreversible effect {decision.verb or '<unnamed>'!r} not allowed")

    if not codes:
        return EffectEvaluation(
            allowed=True,
            code=None,
            codes=(),
            disallowed_verbs=(),
            missing_trial_requirements=(),
            detail="",
            decision=decision,
        )

    return EffectEvaluation(
        allowed=False,
        code=codes[0],
        codes=tuple(codes),
        disallowed_verbs=disallowed_verbs,
        missing_trial_requirements=tuple(missing_trial),
        detail="; ".join(details),
        decision=decision,
    )


def effect_violations(
    steps: list[dict[str, Any]],
    *,
    allowed_effects: Iterable[str] | None = None,
    mode: EffectMode | None = None,
    trial_draft_recipes: bool = False,
    confirm_trial_effects: bool = False,
    trial_data_isolation: str | None = None,
    profile: Any = None,
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for idx, step in enumerate(steps):
        decision = step_effect(step)
        evaluation = evaluate_effect_decision(
            decision,
            allowed_effects=allowed_effects,
            mode=mode,
            trial_draft_recipes=trial_draft_recipes,
            confirm_trial_effects=confirm_trial_effects,
            trial_data_isolation=trial_data_isolation,
            profile=profile,
        )
        if evaluation.allowed:
            continue
        row: dict[str, Any] = {
            "step_index": idx,
            "code": evaluation.code,
            "codes": list(evaluation.codes),
            "effect": decision.effect,
            "verb": decision.verb,
            "verbs": sorted(decision.verbs),
            "disallowed_verbs": list(evaluation.disallowed_verbs),
            "completeness": decision.completeness,
            "missing_trial_requirements": list(evaluation.missing_trial_requirements),
            "reason": decision.reason,
            "detail": evaluation.detail,
            "declared": decision.declared,
            "symbol_bound": decision.symbol_bound,
            "step": step,
        }
        if decision.symbol_id is not None:
            row["symbol_id"] = decision.symbol_id
        violations.append(row)
    return violations


def validate_allowed_effects(
    steps: list[dict[str, Any]],
    *,
    allowed_effects: Iterable[str] | None = None,
    label: str = "recipe",
    mode: EffectMode | None = None,
    trial_draft_recipes: bool = False,
    confirm_trial_effects: bool = False,
    trial_data_isolation: str | None = None,
    profile: Any = None,
) -> None:
    violations = effect_violations(
        steps,
        allowed_effects=allowed_effects,
        mode=mode,
        trial_draft_recipes=trial_draft_recipes,
        confirm_trial_effects=confirm_trial_effects,
        trial_data_isolation=trial_data_isolation,
        profile=profile,
    )
    if not violations:
        return
    primary_code = str(violations[0].get("code") or REASON_EFFECT_NOT_ALLOWED)
    summary = ", ".join(
        f"step[{v['step_index']}]:{v['verb']}({v['code']}:{v['detail'] or v['reason']})"
        for v in violations
    )
    raise EffectPolicyError(
        f"{label}: effect policy blocked ({primary_code}): {summary}",
        code=primary_code,
        violations=violations,
    )


def validate_step_effect(
    step: dict[str, Any],
    *,
    allowed_effects: Iterable[str] | None = None,
    label: str = "runtime step",
    mode: EffectMode | None = None,
    trial_draft_recipes: bool = False,
    confirm_trial_effects: bool = False,
    trial_data_isolation: str | None = None,
    profile: Any = None,
) -> EffectDecision:
    """Evaluate and enforce effect & completeness policy for a single step."""
    decision = step_effect(step)
    validate_allowed_effects(
        [step],
        allowed_effects=allowed_effects,
        label=label,
        mode=mode,
        trial_draft_recipes=trial_draft_recipes,
        confirm_trial_effects=confirm_trial_effects,
        trial_data_isolation=trial_data_isolation,
        profile=profile,
    )
    return decision


def registered_executor_effects() -> dict[str, EffectDecision]:
    with _EXECUTOR_EFFECTS_LOCK:
        return dict(_EXECUTOR_EFFECTS)


def replace_registered_executor_effects(
    decisions: dict[str, EffectDecision],
) -> None:
    """Replace the process-global compatibility mirror during App capture/restore."""
    with _EXECUTOR_EFFECTS_LOCK:
        _EXECUTOR_EFFECTS.clear()
        _EXECUTOR_EFFECTS.update(decisions)


from autophone.execution._entity_binding_hooks import (  # noqa: E402
    classify_readback_step as _classify_readback_step,
)
from autophone.assets.entity_binding import (  # noqa: E402
    register_readback_runtime_provider as _register_readback_runtime_provider,
)

_register_readback_runtime_provider(classify_step=_classify_readback_step)
