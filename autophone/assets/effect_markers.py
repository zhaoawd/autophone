"""Pure-kernel irreversible step effect markers and static step classifier.

Shared single source of truth used by both:
- ``autophone.assets.entity_binding`` (kernel static readback safety validation)
- ``autophone.execution.effects`` (domain effect policy engine)

Contains zero execution/domain imports so ``autophone-kernel`` can evaluate
static action/symbol irreversibility standalone.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
from typing import Any

REVERSIBLE = "reversible"
IRREVERSIBLE = "irreversible"

COMPLETENESS_EXHAUSTIVE = "exhaustive"
COMPLETENESS_LOWER_BOUND = "lower_bound"
COMPLETENESS_UNKNOWN = "unknown"
EFFECT_COMPLETENESS_VALUES: frozenset[str] = frozenset({
    COMPLETENESS_EXHAUSTIVE,
    COMPLETENESS_LOWER_BOUND,
    COMPLETENESS_UNKNOWN,
})

SYMBOL_EFFECTS_STEP_KEY = "_symbol_effects"
SYMBOL_BINDING_STEP_KEY = "_symbol_binding"

_IRREVERSIBLE_MARKERS: tuple[tuple[str, str], ...] = (
    ("send", "发送"),
    ("submit", "提交"),
    ("pay", "支付"),
    ("pay", "付款"),
    ("place_order", "下单"),
    ("place_order", "确认下单"),
    ("purchase", "购买"),
    ("checkout", "结算"),
    ("delete", "删除"),
    ("transfer", "转账"),
    ("withdraw", "提现"),
    ("recharge", "充值"),
    ("publish", "发布"),
    ("confirm_receipt", "确认收货"),
    ("accept_order", "接单"),
    ("accept_order", "抢单"),
    ("refund", "退款"),
    ("close_account", "注销"),
    ("unbind", "解绑"),
    ("pay", "确认支付"),
    ("purchase", "立即购买"),
    ("send", "send"),
    ("submit", "submit"),
    ("pay", "pay"),
    ("pay", "payment"),
    ("purchase", "purchase"),
    ("checkout", "checkout"),
    ("delete", "delete"),
    ("delete", "remove"),
    ("transfer", "transfer"),
    ("withdraw", "withdraw"),
    ("publish", "publish"),
    ("publish", "post"),
    ("place_order", "confirm order"),
    ("place_order", "place order"),
    ("place_order", "place_order"),
    ("purchase", "buy"),
)

_TRIGGER_ACTION_TEXT_KEYS: dict[str, tuple[str, ...]] = {
    "tap_text": ("query",),
    "tap_ax": ("query", "label", "text"),
    "tap_xy": ("label", "target"),
    "handle_dialog_if_present": ("tap_query",),
}


def ordered_irreversible_markers(text: str) -> tuple[str, ...]:
    blob = str(text or "")
    low = blob.lower()
    matched: list[str] = []
    seen: set[str] = set()
    for verb, marker in _IRREVERSIBLE_MARKERS:
        if verb in seen:
            continue
        if marker.isascii():
            hit = bool(re.search(rf"(?<![a-z0-9]){re.escape(marker.lower())}(?![a-z0-9])", low))
        else:
            hit = marker in blob
        if hit:
            seen.add(verb)
            matched.append(verb)
    return tuple(matched)


def irreversible_markers(text: str) -> frozenset[str]:
    """Extract all irreversible effect verbs matched in ``text``."""
    return frozenset(ordered_irreversible_markers(text))


def _normalize_symbol_verbs(raw_verbs: Any) -> frozenset[str]:
    if isinstance(raw_verbs, (str, bytes)) or not isinstance(raw_verbs, Iterable):
        raise ValueError("symbol effects verbs must be an iterable of non-empty strings")
    normalized: set[str] = set()
    for item in raw_verbs:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("symbol effect verb must be a non-empty string")
        normalized.add(item.strip())
    return frozenset(normalized)


def static_step_effect(step: Mapping[str, Any]) -> tuple[bool, str, str]:
    """Return ``(irreversible, effect, reason)`` for one operation step without domain registries."""
    if not isinstance(step, Mapping):
        return True, IRREVERSIBLE, "malformed_step"
    if "action" in step:
        params = {k: v for k, v in step.items() if k != "action"}
        has_binding = SYMBOL_BINDING_STEP_KEY in params
        has_effects = SYMBOL_EFFECTS_STEP_KEY in params
        if not has_binding and not has_effects:
            if "symbol" in params:
                return True, IRREVERSIBLE, "unresolved_symbol_effect"
        else:
            try:
                parsed: list[tuple[frozenset[str], str]] = []
                if has_binding:
                    binding = params[SYMBOL_BINDING_STEP_KEY]
                    if not isinstance(binding, Mapping) or "effects" not in binding:
                        raise ValueError("malformed _symbol_binding")
                    eff = binding["effects"]
                    if not isinstance(eff, Mapping) or "verbs" not in eff or "completeness" not in eff:
                        raise ValueError("malformed _symbol_binding.effects")
                    verbs = _normalize_symbol_verbs(eff["verbs"])
                    comp = eff["completeness"]
                    if not isinstance(comp, str) or comp not in EFFECT_COMPLETENESS_VALUES:
                        raise ValueError("invalid completeness")
                    parsed.append((verbs, comp))
                if has_effects:
                    eff = params[SYMBOL_EFFECTS_STEP_KEY]
                    if not isinstance(eff, Mapping) or "verbs" not in eff or "completeness" not in eff:
                        raise ValueError("malformed _symbol_effects")
                    verbs = _normalize_symbol_verbs(eff["verbs"])
                    comp = eff["completeness"]
                    if not isinstance(comp, str) or comp not in EFFECT_COMPLETENESS_VALUES:
                        raise ValueError("invalid completeness")
                    parsed.append((verbs, comp))
                if len(parsed) == 2 and parsed[0] != parsed[1]:
                    raise ValueError("conflicting symbol effects")
                verbs, comp = parsed[0]
                is_read_only = not verbs and comp == COMPLETENESS_EXHAUSTIVE
                if is_read_only:
                    return False, REVERSIBLE, "symbol_declared_effects"
                return True, IRREVERSIBLE, "symbol_declared_effects"
            except ValueError:
                return True, IRREVERSIBLE, "malformed_symbol_effects"

        action_name = str(step.get("action") or "")
        name_markers = irreversible_markers(action_name)
        text_keys = _TRIGGER_ACTION_TEXT_KEYS.get(action_name, ())
        text_bits = [str(params.get(key) or "") for key in text_keys]
        target_markers = irreversible_markers(" ".join(text_bits))
        if name_markers:
            return True, IRREVERSIBLE, "action_name_marker"
        if target_markers:
            return True, IRREVERSIBLE, "trigger_target_marker"
        return False, REVERSIBLE, "dispatcher_action_default"
    if "operation" in step:
        return True, IRREVERSIBLE, "executor_missing_effect_declaration"
    return True, IRREVERSIBLE, "malformed_step"


__all__ = [
    "COMPLETENESS_EXHAUSTIVE",
    "COMPLETENESS_LOWER_BOUND",
    "COMPLETENESS_UNKNOWN",
    "EFFECT_COMPLETENESS_VALUES",
    "IRREVERSIBLE",
    "REVERSIBLE",
    "SYMBOL_BINDING_STEP_KEY",
    "SYMBOL_EFFECTS_STEP_KEY",
    "_IRREVERSIBLE_MARKERS",
    "_TRIGGER_ACTION_TEXT_KEYS",
    "irreversible_markers",
    "ordered_irreversible_markers",
    "static_step_effect",
]
