"""Milestone M4 unit tests: multi-effect sets, wildcard rejection & isolated trial admission gate."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from autophone.assets.apps import create_app_profile, load_app_profile
from autophone.execution import admission, effect_audit, effects
from autophone.execution.engine import recipe as recipe_mod
from autophone.execution.golden_path_binding import GOLDEN_PATH_LIVE_DOCUMENT_KEY


def _symbol_tap_step(
    *,
    symbol_id: str = "ctrl.checkout.submit",
    identifier: str = "checkout.submit",
    verbs: list[str] | tuple[str, ...],
    completeness: effects.EffectCompleteness,
    label: str = "",
    use_binding_envelope: bool = True,
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "action": "tap_ax",
        "identifier": identifier,
        "role": "button",
    }
    if label:
        step["label"] = label
    effects_decl = {"verbs": list(verbs), "completeness": completeness}
    if use_binding_envelope:
        step[effects.SYMBOL_BINDING_STEP_KEY] = {
            "symbol_id": symbol_id,
            "binding_sha256": "a" * 64,
            "effects": effects_decl,
        }
    else:
        step[effects.SYMBOL_EFFECTS_STEP_KEY] = {
            **effects_decl,
            "symbol_id": symbol_id,
        }
    return step


def _assert_step() -> dict[str, Any]:
    return {"action": "assert_present", "queries": ["完成"]}


def _make_profile(
    tmp_path: Path,
    *,
    app: str = "demo_effects",
    allowed_effects: list[str] | None = None,
    trial_data_isolation: str | None = None,
) -> Any:
    profile = create_app_profile(app, f"com.example.{app}", "Demo Effects", root=tmp_path)
    profile_path = profile.profile_dir / "profile.yaml"
    raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    if allowed_effects is not None:
        raw["allowed_effects"] = list(allowed_effects)
    if trial_data_isolation is not None:
        raw["trial_data_isolation"] = trial_data_isolation
    profile_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return load_app_profile(app, root=tmp_path, state_root=tmp_path)


def _draft_case(
    case_id: str,
    steps: list[dict[str, Any]],
    *,
    status: str = "active",
    automation: str = "ui_ready",
) -> dict[str, Any]:
    return {
        "id": case_id,
        "status": status,
        "automation": automation,
        "_trial_recipe_document": {"id": case_id, "steps": steps},
        "_trial_recipe_identity": {
            "case_revision": 1,
            "reserved_external_id": None,
            "draft_recipe_sha256": "b" * 64,
        },
    }


def _frozen_live_case(
    case_id: str,
    steps: list[dict[str, Any]],
    *,
    status: str = "active",
    automation: str = "ui_ready",
) -> dict[str, Any]:
    return {
        "id": case_id,
        "status": status,
        "automation": automation,
        GOLDEN_PATH_LIVE_DOCUMENT_KEY: {"id": case_id, "steps": steps},
    }


# ── 1. Multi-verb extraction & backward compatibility ────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("下单并支付", frozenset({"place_order", "pay"})),
        ("确认下单并付款", frozenset({"place_order", "pay"})),
        ("提交并删除", frozenset({"submit", "delete"})),
        ("place order and pay now", frozenset({"place_order", "pay"})),
        ("place_order + checkout + pay", frozenset({"place_order", "checkout", "pay"})),
        ("打开饮食偏好设置", frozenset()),
        ("", frozenset()),
    ],
)
def test_irreversible_markers_extracts_all_verbs(text: str, expected: frozenset[str]) -> None:
    assert effects.irreversible_markers(text) == expected


def test_irreversible_marker_preserves_single_string_compat() -> None:
    assert effects.irreversible_marker("下单并支付") == "pay"
    assert effects.irreversible_marker("确认下单") == "place_order"
    assert effects.irreversible_marker("打开偏好设置") is None


def test_classify_freeform_action_extracts_multiple_verbs() -> None:
    decision = effects.classify_freeform_action("tap", "下单并支付")
    assert decision.effect == effects.IRREVERSIBLE
    assert decision.irreversible is True
    assert decision.verbs == frozenset({"place_order", "pay"})
    assert decision.verb == "pay,place_order"
    assert decision.is_multi_effect is True
    assert decision.completeness == effects.COMPLETENESS_EXHAUSTIVE

    self_declared = effects.classify_freeform_action("custom_write", "settings", reversible=False)
    assert self_declared.effect == effects.IRREVERSIBLE
    assert self_declared.verbs == frozenset({"custom_write"})

    safe = effects.classify_freeform_action("tap", "饮食偏好")
    assert safe.effect == effects.REVERSIBLE
    assert safe.irreversible is False
    assert safe.verbs == frozenset()


def test_action_effect_extracts_all_verbs_from_composite_text() -> None:
    tap_text = effects.action_effect("tap_text", {"query": "下单并支付"})
    assert tap_text.effect == effects.IRREVERSIBLE
    assert tap_text.verbs == frozenset({"place_order", "pay"})
    assert tap_text.verb == "pay,place_order"
    assert tap_text.is_multi_effect is True
    assert tap_text.completeness == effects.COMPLETENESS_EXHAUSTIVE

    tap_ax = effects.action_effect("tap_ax", {"label": "提交并支付"})
    assert tap_ax.verbs == frozenset({"submit", "pay"})

    combined_name = effects.action_effect("pay", {"label": "下单"})
    assert combined_name.verbs == frozenset({"pay"})  # "pay" not in _TRIGGER_ACTION_TEXT_KEYS

    read_only = effects.action_effect("tap_text", {"query": "饮食偏好"})
    assert read_only.effect == effects.REVERSIBLE
    assert read_only.verbs == frozenset()
    assert read_only.irreversible is False


# ── 2. EffectDecision & symbol-declared effects ──────────────────────────────


def test_effect_decision_backward_compat_and_validation() -> None:
    legacy_irr = effects.EffectDecision(effects.IRREVERSIBLE, "pay", "legacy_reason")
    assert legacy_irr.verbs == frozenset({"pay"})
    assert legacy_irr.completeness == effects.COMPLETENESS_EXHAUSTIVE
    assert legacy_irr.irreversible is True
    assert legacy_irr.is_multi_effect is False
    assert legacy_irr.incomplete is False

    legacy_rev = effects.EffectDecision(effects.REVERSIBLE, "tap_text", "dispatcher_action_default")
    assert legacy_rev.verbs == frozenset()
    assert legacy_rev.completeness == effects.COMPLETENESS_EXHAUSTIVE
    assert legacy_rev.irreversible is False

    with pytest.raises(ValueError, match="unknown effect"):
        effects.EffectDecision("mutating", "pay", "bad")
    with pytest.raises(ValueError, match="unknown effect completeness"):
        effects.EffectDecision(effects.IRREVERSIBLE, "pay", "bad", completeness="partial")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="reversible EffectDecision cannot declare irreversible verbs"):
        effects.EffectDecision(effects.REVERSIBLE, "tap", "bad", verbs=frozenset({"pay"}))


def test_symbol_effect_decision_variants() -> None:
    read_only = effects.symbol_effect_decision(
        {"verbs": [], "completeness": "exhaustive"},
        symbol_id="ctrl.home.preferences",
    )
    assert read_only.effect == effects.REVERSIBLE
    assert read_only.irreversible is False
    assert read_only.verbs == frozenset()
    assert read_only.completeness == "exhaustive"
    assert read_only.symbol_bound is True
    assert read_only.symbol_id == "ctrl.home.preferences"

    multi = effects.symbol_effect_decision(
        {"verbs": ["place_order", "pay"], "completeness": "exhaustive"},
        symbol_id="ctrl.checkout.submit",
    )
    assert multi.effect == effects.IRREVERSIBLE
    assert multi.irreversible is True
    assert multi.verbs == frozenset({"place_order", "pay"})
    assert multi.verb == "pay,place_order"
    assert multi.is_multi_effect is True

    l1_unknown = effects.symbol_effect_decision(
        {"verbs": [], "completeness": "unknown"},
        symbol_id="ctrl.home.l1_candidate",
    )
    assert l1_unknown.effect == effects.IRREVERSIBLE
    assert l1_unknown.irreversible is True
    assert l1_unknown.incomplete is True
    assert l1_unknown.verbs == frozenset()
    assert l1_unknown.completeness == "unknown"

    lower_bound = effects.symbol_effect_decision(
        verbs=["pay"],
        completeness="lower_bound",
        symbol_id="ctrl.order.pay",
    )
    assert lower_bound.effect == effects.IRREVERSIBLE
    assert lower_bound.verbs == frozenset({"pay"})
    assert lower_bound.completeness == "lower_bound"
    assert lower_bound.incomplete is True


@pytest.mark.parametrize(
    "bad_effects",
    [
        {"verbs": "pay", "completeness": "exhaustive"},
        {"verbs": [""], "completeness": "exhaustive"},
        {"verbs": [123], "completeness": "exhaustive"},
        {"verbs": ["pay"], "completeness": "partial"},
        {"verbs": ["pay"]},
        {"completeness": "exhaustive"},
        "not-a-mapping",
    ],
)
def test_malformed_symbol_effects_fail_closed(bad_effects: Any) -> None:
    with pytest.raises(ValueError):
        effects.symbol_effect_decision(bad_effects)

    step = {
        "action": "tap_ax",
        "identifier": "btn.bad",
        "role": "button",
        effects.SYMBOL_EFFECTS_STEP_KEY: bad_effects,
    }
    decision = effects.step_effect(step)
    assert decision.irreversible is True
    assert decision.declared is False
    assert decision.reason == effects.REASON_MALFORMED_SYMBOL_EFFECTS
    assert decision.completeness == effects.COMPLETENESS_UNKNOWN

    # Even in isolated confirmed draft trial with wildcard, malformed symbol effects MUST block.
    with pytest.raises(effects.EffectPolicyError) as exc_info:
        effects.validate_allowed_effects(
            [step],
            allowed_effects=["irreversible", "pay"],
            mode="draft",
            trial_draft_recipes=True,
            confirm_trial_effects=True,
            trial_data_isolation="isolated",
        )
    assert exc_info.value.code == effects.REASON_MALFORMED_SYMBOL_EFFECTS


def test_unresolved_symbol_step_fails_closed_everywhere() -> None:
    raw_symbol_step = {"action": "tap_ax", "symbol": "ctrl.home.preferences"}
    decision = effects.step_effect(raw_symbol_step)
    assert decision.irreversible is True
    assert decision.declared is False
    assert decision.symbol_bound is True
    assert decision.symbol_id == "ctrl.home.preferences"
    assert decision.reason == effects.REASON_UNRESOLVED_SYMBOL_EFFECT
    assert decision.completeness == effects.COMPLETENESS_UNKNOWN

    with pytest.raises(effects.EffectPolicyError) as exc_info:
        effects.validate_allowed_effects(
            [raw_symbol_step],
            allowed_effects=["irreversible", "ctrl.home.preferences"],
            mode="draft",
            trial_draft_recipes=True,
            confirm_trial_effects=True,
            trial_data_isolation="isolated",
        )
    assert exc_info.value.code == effects.REASON_UNRESOLVED_SYMBOL_EFFECT


def test_conflicting_symbol_binding_and_symbol_effects_fails_closed() -> None:
    step = _symbol_tap_step(verbs=["pay"], completeness="exhaustive", use_binding_envelope=True)
    step[effects.SYMBOL_EFFECTS_STEP_KEY] = {"verbs": ["place_order", "pay"], "completeness": "exhaustive"}
    decision = effects.step_effect(step)
    assert decision.declared is False
    assert decision.reason == effects.REASON_MALFORMED_SYMBOL_EFFECTS
    with pytest.raises(effects.EffectPolicyError) as exc_info:
        effects.validate_allowed_effects([step], allowed_effects=["place_order", "pay"])
    assert exc_info.value.code == effects.REASON_MALFORMED_SYMBOL_EFFECTS


def test_exhaustive_read_only_symbol_ignores_label_keyword_noise() -> None:
    step = _symbol_tap_step(
        symbol_id="ctrl.help.payment_faq",
        identifier="help.payment_faq",
        verbs=[],
        completeness="exhaustive",
        label="查看支付说明",
    )
    decision = effects.step_effect(step)
    assert decision.effect == effects.REVERSIBLE
    assert decision.irreversible is False
    assert decision.verbs == frozenset()
    effects.validate_allowed_effects([step], allowed_effects=[])


# ── 3. Counterexample #5: multi-effect wildcard rejection & completeness gate ─


@pytest.mark.parametrize("use_binding_envelope", [True, False])
def test_counterexample_5_multi_effect_symbol_blocks_partial_and_wildcard(
    use_binding_envelope: bool,
) -> None:
    step = _symbol_tap_step(
        verbs=["place_order", "pay"],
        completeness="exhaustive",
        label="下单并支付",
        use_binding_envelope=use_binding_envelope,
    )

    # Partial allowlist (only "pay") MUST block before dispatch.
    with pytest.raises(effects.EffectPolicyError) as partial_exc:
        effects.validate_allowed_effects([step], allowed_effects=["pay"])
    assert partial_exc.value.code == effects.REASON_EFFECT_NOT_ALLOWED
    assert partial_exc.value.violations[0]["disallowed_verbs"] == ["place_order"]
    assert partial_exc.value.violations[0]["verbs"] == ["pay", "place_order"]

    # Broad "irreversible" wildcard MUST be rejected for multi-effect symbol action.
    with pytest.raises(effects.EffectPolicyError) as wildcard_exc:
        effects.validate_allowed_effects([step], allowed_effects=["irreversible"])
    assert wildcard_exc.value.code == effects.REASON_MULTI_EFFECT_WILDCARD_REJECTED
    assert wildcard_exc.value.violations[0]["disallowed_verbs"] == ["pay", "place_order"]

    # Partial concrete allowlist + "irreversible" wildcard MUST still reject missing verb.
    with pytest.raises(effects.EffectPolicyError) as partial_wildcard_exc:
        effects.validate_allowed_effects([step], allowed_effects=["pay", "irreversible"])
    assert partial_wildcard_exc.value.code == effects.REASON_MULTI_EFFECT_WILDCARD_REJECTED
    assert partial_wildcard_exc.value.violations[0]["disallowed_verbs"] == ["place_order"]

    # Explicit listing of EVERY concrete verb passes.
    effects.validate_allowed_effects([step], allowed_effects=["place_order", "pay"])
    effects.validate_allowed_effects([step], allowed_effects=["place_order", "pay", "irreversible"])


def test_counterexample_5_composite_text_step_blocks_partial_and_wildcard() -> None:
    step = {"action": "tap_text", "query": "下单并支付"}

    with pytest.raises(effects.EffectPolicyError) as partial_exc:
        effects.validate_allowed_effects([step], allowed_effects=["pay"])
    assert partial_exc.value.code == effects.REASON_EFFECT_NOT_ALLOWED
    assert partial_exc.value.violations[0]["disallowed_verbs"] == ["place_order"]

    with pytest.raises(effects.EffectPolicyError) as wildcard_exc:
        effects.validate_allowed_effects([step], allowed_effects=["irreversible"])
    assert wildcard_exc.value.code == effects.REASON_MULTI_EFFECT_WILDCARD_REJECTED
    assert wildcard_exc.value.violations[0]["disallowed_verbs"] == ["pay", "place_order"]

    effects.validate_allowed_effects([step], allowed_effects=["place_order", "pay"])


def test_single_verb_legacy_wildcard_still_preserved() -> None:
    step = {"action": "tap_text", "query": "支付"}
    effects.validate_allowed_effects([step], allowed_effects=["pay"])
    effects.validate_allowed_effects([step], allowed_effects=["irreversible"])
    with pytest.raises(effects.EffectPolicyError) as exc_info:
        effects.validate_allowed_effects([step], allowed_effects=[])
    assert exc_info.value.code == effects.REASON_EFFECT_NOT_ALLOWED
    assert exc_info.value.violations[0]["disallowed_verbs"] == ["pay"]


@pytest.mark.parametrize("completeness", ["lower_bound", "unknown"])
def test_counterexample_5_completeness_gate_active_vs_draft_matrix(
    completeness: effects.EffectCompleteness,
) -> None:
    step = _symbol_tap_step(
        symbol_id="ctrl.order.submit",
        verbs=["pay"],
        completeness=completeness,
    )

    # Active mode (default) ALWAYS hard-blocks with effect_completeness_insufficient.
    with pytest.raises(effects.EffectPolicyError) as active_default:
        effects.validate_allowed_effects([step], allowed_effects=["pay"])
    assert active_default.value.code == effects.REASON_EFFECT_COMPLETENESS_INSUFFICIENT

    # Explicit mode="active" STILL hard-blocks even when all draft trial flags are True!
    with pytest.raises(effects.EffectPolicyError) as active_forced:
        effects.validate_allowed_effects(
            [step],
            allowed_effects=["pay"],
            mode="active",
            trial_draft_recipes=True,
            confirm_trial_effects=True,
            trial_data_isolation="isolated",
        )
    assert active_forced.value.code == effects.REASON_EFFECT_COMPLETENESS_INSUFFICIENT

    # Draft mode missing (a) trial_draft_recipes=True -> blocked.
    with pytest.raises(effects.EffectPolicyError) as missing_a:
        effects.validate_allowed_effects(
            [step],
            allowed_effects=["pay"],
            mode="draft",
            trial_draft_recipes=False,
            confirm_trial_effects=True,
            trial_data_isolation="isolated",
        )
    assert missing_a.value.code == effects.REASON_TRIAL_DRAFT_REQUIRED
    assert "trial_draft_recipes" in missing_a.value.violations[0]["missing_trial_requirements"]

    # Draft mode missing (b) confirm_trial_effects=True -> blocked.
    with pytest.raises(effects.EffectPolicyError) as missing_b:
        effects.validate_allowed_effects(
            [step],
            allowed_effects=["pay"],
            mode="draft",
            trial_draft_recipes=True,
            confirm_trial_effects=False,
            trial_data_isolation="isolated",
        )
    assert missing_b.value.code == effects.REASON_TRIAL_EFFECTS_NOT_CONFIRMED
    assert "confirm_trial_effects" in missing_b.value.violations[0]["missing_trial_requirements"]

    # Draft mode missing (c) trial_data_isolation="isolated" -> blocked.
    with pytest.raises(effects.EffectPolicyError) as missing_c:
        effects.validate_allowed_effects(
            [step],
            allowed_effects=["pay"],
            mode="draft",
            trial_draft_recipes=True,
            confirm_trial_effects=True,
            trial_data_isolation="unisolated",
        )
    assert missing_c.value.code == effects.REASON_TRIAL_DATA_UNISOLATED
    assert "trial_data_isolation=isolated" in missing_c.value.violations[0]["missing_trial_requirements"]

    # Draft mode omitting trial_data_isolation (default unisolated) -> blocked.
    with pytest.raises(effects.EffectPolicyError) as omitted_c:
        effects.validate_allowed_effects(
            [step],
            allowed_effects=["pay"],
            mode="draft",
            trial_draft_recipes=True,
            confirm_trial_effects=True,
        )
    assert omitted_c.value.code == effects.REASON_TRIAL_DATA_UNISOLATED

    # Draft mode missing (d) allowed_effects -> blocked.
    with pytest.raises(effects.EffectPolicyError) as missing_d:
        effects.validate_allowed_effects(
            [step],
            allowed_effects=[],
            mode="draft",
            trial_draft_recipes=True,
            confirm_trial_effects=True,
            trial_data_isolation="isolated",
        )
    assert missing_d.value.code == effects.REASON_EFFECT_NOT_ALLOWED
    assert missing_d.value.violations[0]["disallowed_verbs"] == ["pay"]

    # Non-bool truthy spoofing of trial flags MUST NOT bypass the gate.
    with pytest.raises(effects.EffectPolicyError) as spoof_exc:
        effects.validate_allowed_effects(
            [step],
            allowed_effects=["pay"],
            mode="draft",
            trial_draft_recipes="true",  # type: ignore[arg-type]
            confirm_trial_effects="true",  # type: ignore[arg-type]
            trial_data_isolation="isolated",
        )
    assert spoof_exc.value.code == effects.REASON_TRIAL_DRAFT_REQUIRED

    # All 4 conditions satisfied -> ALLOWED!
    decision = effects.validate_step_effect(
        step,
        allowed_effects=["pay"],
        mode="draft",
        trial_draft_recipes=True,
        confirm_trial_effects=True,
        trial_data_isolation="isolated",
    )
    assert decision.verbs == frozenset({"pay"})
    assert decision.completeness == completeness


def test_l1_unknown_empty_verbs_draft_trial_requires_isolation_and_confirmation() -> None:
    l1_step = _symbol_tap_step(
        symbol_id="ctrl.home.preferences",
        identifier="home.ai_prompt.preferences",
        verbs=[],
        completeness="unknown",
    )

    with pytest.raises(effects.EffectPolicyError) as active_exc:
        effects.validate_allowed_effects([l1_step], allowed_effects=[])
    assert active_exc.value.code == effects.REASON_EFFECT_COMPLETENESS_INSUFFICIENT

    with pytest.raises(effects.EffectPolicyError) as unconfirmed_exc:
        effects.validate_allowed_effects(
            [l1_step],
            allowed_effects=[],
            trial_draft_recipes=True,
            confirm_trial_effects=False,
            trial_data_isolation="isolated",
        )
    assert unconfirmed_exc.value.code == effects.REASON_TRIAL_EFFECTS_NOT_CONFIRMED

    # With all 3 draft trial gates satisfied and no disallowed known verbs, allowed even with empty allowed_effects.
    effects.validate_allowed_effects(
        [l1_step],
        allowed_effects=[],
        trial_draft_recipes=True,
        confirm_trial_effects=True,
        trial_data_isolation="isolated",
    )


def test_multi_effect_lower_bound_still_rejects_wildcard_in_isolated_draft_trial() -> None:
    step = _symbol_tap_step(
        verbs=["place_order", "pay"],
        completeness="lower_bound",
    )
    with pytest.raises(effects.EffectPolicyError) as exc_info:
        effects.validate_allowed_effects(
            [step],
            allowed_effects=["irreversible"],
            mode="draft",
            trial_draft_recipes=True,
            confirm_trial_effects=True,
            trial_data_isolation="isolated",
        )
    assert exc_info.value.code == effects.REASON_MULTI_EFFECT_WILDCARD_REJECTED
    assert exc_info.value.violations[0]["disallowed_verbs"] == ["pay", "place_order"]

    # Passes only when every concrete verb is explicitly listed AND draft trial gates hold.
    effects.validate_allowed_effects(
        [step],
        allowed_effects=["place_order", "pay"],
        mode="draft",
        trial_draft_recipes=True,
        confirm_trial_effects=True,
        trial_data_isolation="isolated",
    )


def test_profile_trial_data_isolation_fallback_and_app_profile_integration(tmp_path: Path) -> None:
    default_profile = _make_profile(tmp_path, app="app_default", allowed_effects=["pay"])
    isolated_profile = _make_profile(
        tmp_path,
        app="app_isolated",
        allowed_effects=["pay"],
        trial_data_isolation="isolated",
    )
    assert effects.profile_trial_data_isolation(default_profile) == "unisolated"
    assert effects.profile_trial_data_isolation(isolated_profile) == "isolated"
    assert effects.profile_trial_data_isolation(SimpleNamespace()) == "unisolated"
    assert effects.profile_trial_data_isolation(SimpleNamespace(trial_data_isolation="isolated")) == "isolated"
    assert effects.profile_trial_data_isolation(SimpleNamespace(raw={"trial_data_isolation": "isolated"})) == "isolated"
    assert effects.profile_trial_data_isolation(SimpleNamespace(trial_data_isolation="other")) == "unisolated"
    assert effects.profile_trial_data_isolation(None) == "unisolated"

    step = _symbol_tap_step(verbs=["pay"], completeness="lower_bound")
    with pytest.raises(effects.EffectPolicyError) as exc_info:
        effects.validate_allowed_effects(
            [step],
            profile=default_profile,
            trial_draft_recipes=True,
            confirm_trial_effects=True,
        )
    assert exc_info.value.code == effects.REASON_TRIAL_DATA_UNISOLATED

    effects.validate_allowed_effects(
        [step],
        profile=isolated_profile,
        trial_draft_recipes=True,
        confirm_trial_effects=True,
    )


# ── 4. Admission gate (admit_case, activation_blocker, audit, partition) ────


@pytest.mark.parametrize("completeness", ["lower_bound", "unknown"])
def test_admission_active_hard_blocks_incomplete_effects(
    tmp_path: Path,
    completeness: effects.EffectCompleteness,
) -> None:
    profile = _make_profile(
        tmp_path,
        app="active_gate",
        allowed_effects=["pay"],
        trial_data_isolation="isolated",
    )
    steps = [_symbol_tap_step(verbs=["pay"], completeness=completeness), _assert_step()]
    live_case = _frozen_live_case("R1", steps)

    # Even if trial flags + isolated profile are supplied, an active live case MUST hard-block.
    decision = admission.admit_case(
        profile,
        live_case,
        trial_draft_recipes=True,
        confirm_trial_effects=True,
    )
    assert decision["admitted"] is False
    assert decision["blocker"]["scope"] == "asset"
    assert decision["blocker"]["code"] == effects.REASON_EFFECT_COMPLETENESS_INSUFFICIENT

    # audit_active_assets (used by case lint) reports the exact blocker code.
    findings = admission.audit_active_assets(profile, cases=[live_case])
    assert len(findings) == 1
    assert findings[0]["case_id"] == "R1"
    assert findings[0]["blocker"]["code"] == effects.REASON_EFFECT_COMPLETENESS_INSUFFICIENT

    # activation_blocker refuses promoting a draft case with incomplete live recipe.
    blocker_payload = admission.activation_blocker(profile, {**live_case, "status": "draft"})
    assert blocker_payload is not None
    assert blocker_payload["code"] == effects.REASON_EFFECT_COMPLETENESS_INSUFFICIENT


@pytest.mark.parametrize("case_status", ["active", "draft"])
def test_admission_draft_trial_requires_all_four_conditions(
    tmp_path: Path,
    case_status: str,
) -> None:
    unisolated = _make_profile(tmp_path, app="trial_unisolated", allowed_effects=["pay"])
    isolated = _make_profile(
        tmp_path,
        app="trial_isolated",
        allowed_effects=["pay"],
        trial_data_isolation="isolated",
    )
    steps = [_symbol_tap_step(verbs=["pay"], completeness="lower_bound"), _assert_step()]
    case = _draft_case("R2", steps, status=case_status)

    # Missing (a) trial_draft_recipes -> blocked.
    r_no_trial = admission.admit_case(
        isolated,
        case,
        trial_draft_recipes=False,
        confirm_trial_effects=True,
    )
    assert r_no_trial["admitted"] is False
    assert r_no_trial["blocker"]["code"] == effects.REASON_TRIAL_DRAFT_REQUIRED

    # Missing (b) confirm_trial_effects -> blocked.
    r_no_confirm = admission.admit_case(
        isolated,
        case,
        trial_draft_recipes=True,
        confirm_trial_effects=False,
    )
    assert r_no_confirm["admitted"] is False
    assert r_no_confirm["blocker"]["code"] == effects.REASON_TRIAL_EFFECTS_NOT_CONFIRMED

    # Missing (c) trial_data_isolation="isolated" -> blocked.
    r_unisolated = admission.admit_case(
        unisolated,
        case,
        trial_draft_recipes=True,
        confirm_trial_effects=True,
    )
    assert r_unisolated["admitted"] is False
    assert r_unisolated["blocker"]["code"] == effects.REASON_TRIAL_DATA_UNISOLATED

    # Missing (d) verb not allowed / wildcard on multi-effect -> blocked.
    multi_case = _draft_case(
        "R3",
        [_symbol_tap_step(verbs=["place_order", "pay"], completeness="lower_bound"), _assert_step()],
        status=case_status,
    )
    wildcard_profile = _make_profile(
        tmp_path,
        app="trial_wildcard",
        allowed_effects=["irreversible"],
        trial_data_isolation="isolated",
    )
    r_wildcard = admission.admit_case(
        wildcard_profile,
        multi_case,
        trial_draft_recipes=True,
        confirm_trial_effects=True,
    )
    assert r_wildcard["admitted"] is False
    assert r_wildcard["blocker"]["code"] == effects.REASON_MULTI_EFFECT_WILDCARD_REJECTED

    # All four conditions satisfied -> admitted!
    r_ok = admission.admit_case(
        isolated,
        case,
        trial_draft_recipes=True,
        confirm_trial_effects=True,
    )
    assert r_ok == {"case_id": "R2", "admitted": True, "blocker": None}


def test_admission_admit_recipe_steps_and_partition_boundary(tmp_path: Path) -> None:
    profile = _make_profile(
        tmp_path,
        app="partition_app",
        allowed_effects=["place_order", "pay"],
        trial_data_isolation="isolated",
    )
    good_steps = [
        _symbol_tap_step(verbs=["place_order", "pay"], completeness="exhaustive"),
        _assert_step(),
    ]
    bad_steps = [
        _symbol_tap_step(verbs=["pay"], completeness="lower_bound"),
        _assert_step(),
    ]
    assert admission.admit_recipe_steps(profile, good_steps, mode="active")["admitted"] is True
    bad_res = admission.admit_recipe_steps(profile, bad_steps, mode="active")
    assert bad_res["admitted"] is False
    assert bad_res["blocker"]["code"] == effects.REASON_EFFECT_COMPLETENESS_INSUFFICIENT
    assert len(bad_res["violations"]) == 1

    cases = [
        _frozen_live_case("R1", good_steps),
        _frozen_live_case("R2", bad_steps),
        _frozen_live_case("R3", good_steps),
    ]
    runnable, blocked = admission.partition_admitted_cases(profile, cases=cases)
    assert [c["id"] for c in runnable] == ["R1", "R3"]
    assert runnable[0].get("_break_shared_precondition_before") is None
    assert runnable[1].get("_break_shared_precondition_before") is True
    assert len(blocked) == 1
    assert blocked[0]["case_id"] == "R2"
    assert blocked[0]["blocker"]["code"] == effects.REASON_EFFECT_COMPLETENESS_INSUFFICIENT


def test_admission_existing_regression_contracts(tmp_path: Path) -> None:
    profile = _make_profile(tmp_path, app="legacy_admit", allowed_effects=[])

    # Missing draft marker.
    missing_draft = admission.admit_case(profile, {"id": "R1", "status": "active", "automation": "ui_ready", "_trial_recipe_missing": True})
    assert missing_draft["admitted"] is False
    assert missing_draft["blocker"]["code"] == "missing_draft_recipe"

    # Non-active case without draft trial document is skipped (admitted=True).
    draft_unselected = admission.admit_case(profile, {"id": "R2", "status": "draft", "automation": "ui_ready"})
    assert draft_unselected == {"case_id": "R2", "admitted": True, "blocker": None}

    # Missing live recipe.
    missing_live = admission.admit_case(profile, {"id": "R3", "status": "active", "automation": "ui_ready"})
    assert missing_live["admitted"] is False
    assert missing_live["blocker"]["code"] == "missing_live_recipe"

    # Valid live recipe on disk.
    live_dir = profile.recipes_dir()
    live_dir.mkdir(parents=True, exist_ok=True)
    (live_dir / "r4.yaml").write_text(
        yaml.safe_dump({"id": "R4", "steps": [{"action": "tap_text", "query": "偏好"}, _assert_step()]}, allow_unicode=True),
        encoding="utf-8",
    )
    ok_live = admission.admit_case(profile, {"id": "R4", "status": "active", "automation": "ui_ready"})
    assert ok_live == {"case_id": "R4", "admitted": True, "blocker": None}

    # normalise_case_promotion_evidence keeps reviewed trials and fails non-approved.
    normalized = admission.normalise_case_promotion_evidence([
        {"id": "1", "review_status": "pending", "status": "passed"},
        {"id": "2", "review_status": "approved", "status": "passed"},
        {"id": "3", "review_status": "needs_changes", "status": "passed"},
        {"id": "4", "review_status": "rejected", "status": "passed"},
    ])
    assert [(r["id"], r["status"]) for r in normalized] == [
        ("2", "passed"),
        ("3", "failed"),
        ("4", "failed"),
    ]


# ── 5. Read-only helpers, executor effects & effect_audit ───────────────────


def test_recipe_step_effects_and_case_operation_effects_include_verbs_and_completeness() -> None:
    steps = [
        _symbol_tap_step(verbs=["place_order", "pay"], completeness="lower_bound"),
        {"action": "tap_text", "query": "下单并支付"},
        {"action": "assert_present", "queries": ["成功"]},
    ]
    rows = effects.recipe_step_effects(steps)
    assert rows[0]["verbs"] == ["pay", "place_order"]
    assert rows[0]["completeness"] == "lower_bound"
    assert rows[0]["symbol_bound"] is True
    assert rows[0]["symbol_id"] == "ctrl.checkout.submit"
    assert rows[1]["verbs"] == ["pay", "place_order"]
    assert rows[1]["completeness"] == "exhaustive"
    assert rows[1]["symbol_bound"] is False
    assert rows[2]["effect"] == effects.REVERSIBLE
    assert rows[2]["verbs"] == []

    snapshot = {
        "op_checkout": effects.EffectDecision(
            effects.IRREVERSIBLE,
            "pay,place_order",
            "executor_effect_registered",
            declared=True,
            verbs=frozenset({"place_order", "pay"}),
            completeness="exhaustive",
        ),
    }
    with effects.executor_effect_snapshot_scope(snapshot):
        case_rows = effects.case_operation_effects({"setup": [{"op": "op_checkout"}]})
    assert case_rows[0]["verbs"] == ["pay", "place_order"]
    assert case_rows[0]["completeness"] == "exhaustive"


def test_effect_audit_enforces_multi_verb_and_rejects_wildcard(tmp_path: Path) -> None:
    partial_profile = _make_profile(tmp_path, app="audit_partial", allowed_effects=["pay"])
    wildcard_profile = _make_profile(tmp_path, app="audit_wildcard", allowed_effects=["irreversible"])
    allowed_profile = _make_profile(tmp_path, app="audit_allowed", allowed_effects=["place_order", "pay"])

    for prof in (partial_profile, wildcard_profile, allowed_profile):
        rec_dir = prof.recipes_dir()
        rec_dir.mkdir(parents=True, exist_ok=True)
        (rec_dir / "r1.yaml").write_text(
            yaml.safe_dump(
                {"id": "R1", "steps": [{"action": "tap_text", "query": "下单并支付"}, _assert_step()]},
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

    partial_report = effect_audit.audit_app(partial_profile)
    assert partial_report["summary"]["violations_if_enforced"] == 1
    hit = partial_report["violations_if_enforced"][0]
    assert hit["verbs"] == ["pay", "place_order"]
    assert hit["disallowed_verbs"] == ["place_order"]
    assert hit["policy_code"] == effects.REASON_EFFECT_NOT_ALLOWED

    wildcard_report = effect_audit.audit_app(wildcard_profile)
    assert wildcard_report["summary"]["violations_if_enforced"] == 1
    assert wildcard_report["violations_if_enforced"][0]["policy_code"] == (
        effects.REASON_MULTI_EFFECT_WILDCARD_REJECTED
    )

    allowed_report = effect_audit.audit_app(allowed_profile)
    assert allowed_report["summary"]["violations_if_enforced"] == 0
    assert allowed_report["trial_data_isolation"] == "unisolated"


def test_register_executor_effect_supports_multi_verb_and_completeness() -> None:
    prev = effects.registered_executor_effects()
    try:
        effects.register_executor_effect(
            "m4_test_multi_op",
            effect=effects.IRREVERSIBLE,
            verbs=["place_order", "pay"],
            completeness="exhaustive",
        )
        decision = effects.executor_effect("m4_test_multi_op")
        assert decision.verbs == frozenset({"place_order", "pay"})
        assert decision.verb == "pay,place_order"
        assert decision.completeness == "exhaustive"
        with pytest.raises(effects.EffectPolicyError) as exc_info:
            effects.validate_allowed_effects(
                [{"operation": "m4_test_multi_op"}],
                allowed_effects=["irreversible"],
            )
        assert exc_info.value.code == effects.REASON_MULTI_EFFECT_WILDCARD_REJECTED
    finally:
        effects.replace_registered_executor_effects(prev)


def test_register_operation_still_registers_single_verb_effect() -> None:
    prev_effects = effects.registered_executor_effects()
    prev_ops = dict(recipe_mod.OPERATIONS)
    prev_reqs = {k: set(v) for k, v in recipe_mod.OPERATION_REQUIREMENTS.items()}
    try:
        recipe_mod.register_operation("m4_test_single_op", lambda ctx: None, effect=effects.REVERSIBLE)
        decision = effects.executor_effect("m4_test_single_op")
        assert decision.effect == effects.REVERSIBLE
        assert decision.verbs == frozenset()
        assert decision.completeness == effects.COMPLETENESS_EXHAUSTIVE
    finally:
        effects.replace_registered_executor_effects(prev_effects)
        recipe_mod.OPERATIONS.clear()
        recipe_mod.OPERATIONS.update(prev_ops)
        recipe_mod.OPERATION_REQUIREMENTS.clear()
        recipe_mod.OPERATION_REQUIREMENTS.update(prev_reqs)


def test_split_text_keys_extract_all_irreversible_verbs() -> None:
    tap_ax = effects.action_effect("tap_ax", {"query": "确认下单", "label": "立即支付"})
    assert tap_ax.verbs == frozenset({"place_order", "pay"})
    assert tap_ax.is_multi_effect is True

    tap_xy = effects.action_effect("tap_xy", {"label": "提交", "target": "删除账号"})
    assert tap_xy.verbs == frozenset({"submit", "delete"})
    assert tap_xy.is_multi_effect is True


def test_exhaustive_read_only_draft_step_admitted_without_trial_flags(tmp_path: Path) -> None:
    profile = _make_profile(tmp_path, app="ro_draft", allowed_effects=[])
    steps = [
        _symbol_tap_step(
            symbol_id="ctrl.home.preferences",
            identifier="home.ai_prompt.preferences",
            verbs=[],
            completeness="exhaustive",
        ),
        _assert_step(),
    ]
    case = _draft_case("R1", steps, status="draft")
    res = admission.admit_case(profile, case)
    assert res == {"case_id": "R1", "admitted": True, "blocker": None}


def test_case_lint_and_live_disk_recipe_block_multi_effect_wildcard(tmp_path: Path) -> None:
    from autophone.application.cases import CaseRecipeApplication

    profile = _make_profile(
        tmp_path,
        app="lint_multi",
        allowed_effects=["irreversible"],
    )
    (profile.cases_path()).write_text(
        yaml.safe_dump(
            {
                "meta": {"title": "lint_multi", "app": "lint_multi", "schema_version": 2, "total": 1, "next_seq": 2},
                "cases": [
                    {
                        "id": "R1",
                        "revision": 1,
                        "status": "active",
                        "automation": "ui_ready",
                        "case_type": "功能测试",
                        "test_point": "下单并支付",
                        "owner": "qa",
                        "module": "结算",
                        "item": "下单并支付",
                        "login_precondition": "any",
                        "steps": ["点击下单并支付"],
                        "expects": ["订单完成"],
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    rec_dir = profile.recipes_dir()
    rec_dir.mkdir(parents=True, exist_ok=True)
    (rec_dir / "r1.yaml").write_text(
        yaml.safe_dump(
            {"id": "R1", "steps": [{"action": "tap_text", "query": "下单并支付"}, _assert_step()]},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    lint_report = CaseRecipeApplication(profile).lint()
    assert lint_report["error_count"] >= 1
    assert len(lint_report["findings"]) == 1
    assert lint_report["findings"][0]["case_id"] == "R1"
    assert lint_report["findings"][0]["blocker"]["code"] == effects.REASON_MULTI_EFFECT_WILDCARD_REJECTED


def test_external_callers_backward_compatibility() -> None:
    from autophone.knowledge.trial_confirmation import _recipe_effect_level
    from autophone.observation.impact import admission as impact_admission

    # impact.admission consumes irreversible_marker (str | None) and EffectDecision.
    findings = impact_admission.scan_steps([{"action": "tap_text", "query": "删除记录"}])
    assert findings and findings[0]["marker"] == "delete"

    safety, _, op_findings = impact_admission.operation_safety(
        {"name": "reset_to_home", "navigation": {"safety": "read_only", "effects": []}, "steps": []},
    )
    assert safety == impact_admission.READ_ONLY
    assert op_findings == []

    # trial_confirmation treats unknown-completeness symbols as requiring confirmation.
    l1_rows = effects.recipe_step_effects([
        _symbol_tap_step(verbs=[], completeness="unknown"),
        _assert_step(),
    ])
    assert _recipe_effect_level(l1_rows) == effects.IRREVERSIBLE

