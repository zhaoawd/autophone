"""Stage 2 (Author A): Entity-binding schemas, resolver, extended API oracle,
fail-closed joint verdict arbiter, static lint/admission/capability gates, and
negative-control/promotion joint-verdict support."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from autophone.assets.apps import AppProfile
from autophone.assets.case_lint import lint_case_registry_document
from autophone.assets.entity_binding import (
    EntityBindingIssue,
    ResolvedEntity,
    case_has_entity_binding,
    case_requires_api_capture,
    case_requires_entity_readback,
    entity_binding_policy,
    resolve_case_entity,
    validate_case_entity_binding,
)
from autophone.assets.negative_control import (
    NegativeControlError,
    VERDICT_COUNTED,
    VERDICT_PASSED,
    declaration_sha256,
    judge_negative_control,
    resolve_negative_control,
)
from autophone.assets.promotion import (
    discrimination_gaps,
    evaluate_promotion_gate,
    load_discrimination_proof,
)
from autophone.contracts import ContractError, validate_or_raise
from autophone.execution.admission import admit_case, audit_active_assets
from autophone.execution.capability_requirements import derive_requirements
from autophone.execution.engine import recipe as recipe_mod
from autophone.execution import effects
from autophone.observation.frida import api_oracle
from autophone.observation.joint_verdict import arbitrate_joint_verdict


_SHA_A = "a" * 64
_SHA_B = "b" * 64
_BIND_A = "1" * 64
_BIND_B = "2" * 64
_RECIPE_SHA = "c" * 64


def _sample_entity_binding() -> dict[str, Any]:
    return {
        "entity_type": "store",
        "entity_id": {"from_fixture": "expected_store_id"},
        "attributes": {
            "store_name": {"from_fixture": "expected_store_name"},
            "preference_profile": {"from_fixture": "expected_preference_profile"},
            "tier": "vip",
        },
        "api_window": {
            "window": {
                "symbol_id": "ctrl.home.preferences",
                "window_tag": "save_pref",
            },
            "expect": [
                {
                    "name": "save_preferences",
                    "method": "POST",
                    "path": "/api/v1/stores/${entity.id}/preferences",
                    "assert": {
                        "status": 200,
                        "resp_error": {"op": "empty"},
                        "req.query.store_id": "${entity.id}",
                        "req.header.x-store-id": "${entity.id}",
                        "resp.header.x-served-store": "${entity.id}",
                        "req.preference_profile": "${entity.preference_profile}",
                        "resp.data.store_name": "${entity.store_name}",
                    },
                }
            ],
            "forbid": [
                {
                    "name": "no_fallback_store",
                    "method": "POST",
                    "url_regex": "/api/v1/stores/fallback",
                }
            ],
        },
        "readback": {
            "op": "read_store_preference",
            "with": {
                "store_id": {"from_entity": "id"},
                "tenant": "default",
            },
            "expect_context": {
                "persisted_store_id": {"from_entity": "id"},
                "persisted_profile": {"from_entity": "preference_profile"},
                "persisted_flag": {"op": "eq", "value": True},
            },
        },
        "joint_verdict": {
            "require_ui": True,
            "require_api": True,
            "require_readback": True,
        },
    }


def _sample_case(
    *,
    case_id: str = "R5",
    status: str = "active",
    automation: str = "ui_ready",
    entity_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    case: dict[str, Any] = {
        "id": case_id,
        "status": status,
        "revision": 1,
        "item": "校验目标门店偏好落盘",
        "automation": automation,
        "login_precondition": "any",
        "case_type": "功能测试",
        "test_point": "业务对象联合判定",
        "owner": "qa",
        "expects": ["页面、API 与回读均命中同一目标门店与偏好方案"],
    }
    if entity_binding is not None:
        case["entity_binding"] = entity_binding
    return case


def _make_profile(tmp_path: Path, *, app: str = "demo_s2", operations: list[dict[str, Any]] | None = None, fixtures: dict[str, Any] | None = None) -> AppProfile:
    app_dir = tmp_path / "apps" / app
    (app_dir / "recipes").mkdir(parents=True, exist_ok=True)
    (app_dir / "cases").mkdir(parents=True, exist_ok=True)
    if operations is not None:
        ops_dir = app_dir / "operations"
        ops_dir.mkdir(parents=True, exist_ok=True)
        (ops_dir / "operations.yaml").write_text(
            yaml.safe_dump(
                {
                    "meta": {"schema_version": 1, "app": app, "next_seq": len(operations) + 1},
                    "operations": operations,
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
    if fixtures is not None:
        (app_dir / "api_fixtures.yaml").write_text(
            yaml.safe_dump(fixtures, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    raw = {
        "schema_version": "v1",
        "app": app,
        "bundle_id": "com.example.demo",
        "display_name": "Demo S2",
        "env": "verification",
        "lifecycle": "active",
        "build_label": "1.0.0-bld-1",
        "trial_data_isolation": "isolated",
    }
    return AppProfile(raw=raw, root=tmp_path)


# ── 1. JSON Schema Contracts ──────────────────────────────────────────────────


def test_entity_binding_schema_valid_and_strict_rejections() -> None:
    full = _sample_entity_binding()
    validate_or_raise("entity-binding.v1.json", full)

    minimal = {"entity_type": "dish", "entity_id": "dish-42"}
    validate_or_raise("entity-binding.v1.json", minimal)

    numeric_id = {"entity_type": "order", "entity_id": 10042}
    validate_or_raise("entity-binding.v1.json", numeric_id)

    ctx_id = {"entity_type": "order", "entity_id": {"from_context": "created_order_id"}}
    validate_or_raise("entity-binding.v1.json", ctx_id)

    # Reject unknown top-level property
    bad_top = {**minimal, "unknown_key": True}
    with pytest.raises(ContractError):
        validate_or_raise("entity-binding.v1.json", bad_top)

    # Reject unknown property inside window / expect / readback / joint_verdict / ref
    for mutator in (
        lambda d: d["api_window"]["window"].__setitem__("extra", "x"),
        lambda d: d["api_window"]["expect"][0].__setitem__("extra", "x"),
        lambda d: d["api_window"]["expect"][0]["assert"].__setitem__("status", {"op": "eq", "value": 200, "bogus": 1}),
        lambda d: d["readback"].__setitem__("extra", "x"),
        lambda d: d["readback"]["expect_context"].__setitem__("k", {"op": "eq", "value": 1, "extra": 2}),
        lambda d: d["joint_verdict"].__setitem__("require_telemetry", True),
        lambda d: d.__setitem__("entity_id", {"from_fixture": "k", "extra": 1}),
        lambda d: d.__setitem__("entity_type", "   "),
        lambda d: d.__setitem__("entity_id", ""),
    ):
        candidate = copy.deepcopy(full)
        mutator(candidate)
        with pytest.raises(ContractError):
            validate_or_raise("entity-binding.v1.json", candidate)


def test_case_registry_v2_accepts_entity_binding_and_preserves_backward_compat() -> None:
    doc_without = {
        "meta": {"schema_version": 2, "total": 1, "next_seq": 2},
        "cases": [_sample_case(case_id="R1")],
    }
    validate_or_raise("case-registry.v2.json", doc_without)

    doc_with = {
        "meta": {"schema_version": 2, "total": 1, "next_seq": 6},
        "cases": [_sample_case(case_id="R5", entity_binding=_sample_entity_binding())],
    }
    validate_or_raise("case-registry.v2.json", doc_with)

    bad_doc = copy.deepcopy(doc_with)
    bad_doc["cases"][0]["entity_binding"]["bogus"] = 1
    with pytest.raises(ContractError):
        validate_or_raise("case-registry.v2.json", bad_doc)


def test_run_result_v1_joint_verdict_and_readback_plane_schema() -> None:
    base_run: dict[str, Any] = {
        "schema_version": "v1",
        "run_id": "run-s2-001",
        "type": "regression",
        "app": "food_ordering",
        "device": "rig-a",
        "status": "passed",
        "planes": {
            "ui": {"mode": "required", "verdict": "pass"},
            "api": {"mode": "required", "verdict": "pass"},
        },
        "cases": [
            {
                "id": "R1",
                "status": "passed",
                "planes": {
                    "ui": {"mode": "required", "verdict": "pass"},
                },
            }
        ],
        "timestamps": {},
    }
    # Backward compat without joint_verdict / readback
    validate_or_raise("run-result.v1.json", base_run)

    # With joint_verdict and cases[].planes.readback
    with_jv = copy.deepcopy(base_run)
    with_jv["cases"][0]["planes"]["readback"] = {
        "mode": "required",
        "verdict": "pass",
        "detail": "persisted_store_id matched",
    }
    with_jv["cases"][0]["joint_verdict"] = {
        "status": "pass",
        "fail_kind": "",
        "detail": "all required entity planes passed",
        "entity_type": "store",
        "entity_id": "store-flagship-a",
        "planes": {"ui": "pass", "api": "pass", "readback": "pass"},
        "mismatch_reasons": [],
    }
    validate_or_raise("run-result.v1.json", with_jv)

    # Strict additionalProperties: false on joint_verdict and joint_verdict.planes
    bad_jv = copy.deepcopy(with_jv)
    bad_jv["cases"][0]["joint_verdict"]["extra"] = "nope"
    with pytest.raises(ContractError):
        validate_or_raise("run-result.v1.json", bad_jv)

    bad_plane_val = copy.deepcopy(with_jv)
    bad_plane_val["cases"][0]["joint_verdict"]["planes"]["ui"] = "missing"
    with pytest.raises(ContractError):
        validate_or_raise("run-result.v1.json", bad_plane_val)


def test_negative_controls_schema_supports_joint_planes_and_optional_step() -> None:
    doc = {
        "schema": "autophone.negative-controls/1",
        "controls": [
            {
                "id": "nc-ui-legacy",
                "case_id": "R2",
                "hypothesis": "skipping nav fails UI assert",
                "perturbation": {"kind": "skip_step", "step": {"action": "tap_ax"}},
                "expect": {"step": {"action": "assert_present"}},
            },
            {
                "id": "nc-joint-no-step",
                "case_id": "R5",
                "hypothesis": "selecting wrong store fails joint verdict post-body",
                "perturbation": {
                    "kind": "replace_args",
                    "step": {"action": "tap_ax"},
                    "args": {"label": "wrong_store"},
                },
                "expect": {
                    "plane": "joint",
                    "fail_kinds": ["entity_api_mismatch", "entity_readback_mismatch"],
                },
            },
            {
                "id": "nc-api-plane",
                "case_id": "R5",
                "hypothesis": "api plane catches wrong entity",
                "perturbation": {"kind": "skip_step", "step": {"action": "tap_ax"}},
                "expect": {"plane": "api"},
            },
            {
                "id": "nc-readback-plane",
                "case_id": "R5",
                "hypothesis": "readback plane catches unpersisted write",
                "perturbation": {"kind": "skip_step", "step": {"action": "tap_ax"}},
                "expect": {"plane": "readback"},
            },
        ],
    }
    validate_or_raise("negative-controls.v1.json", doc)

    # Missing step on default/ui plane MUST fail
    bad_ui = {
        "schema": "autophone.negative-controls/1",
        "controls": [
            {
                "id": "nc-bad-ui",
                "case_id": "R2",
                "hypothesis": "missing step on ui plane",
                "perturbation": {"kind": "skip_step", "step": {"action": "tap_ax"}},
                "expect": {"plane": "ui"},
            }
        ],
    }
    with pytest.raises(ContractError):
        validate_or_raise("negative-controls.v1.json", bad_ui)

    # flip_witness MUST NOT use non-ui plane
    bad_flip = {
        "schema": "autophone.negative-controls/1",
        "controls": [
            {
                "id": "nc-bad-flip",
                "case_id": "R2",
                "hypothesis": "flip witness on joint",
                "perturbation": {"kind": "flip_witness", "step": {"operation": "op_a"}},
                "expect": {"plane": "joint", "step": {"operation": "op_a"}},
            }
        ],
    }
    with pytest.raises(ContractError):
        validate_or_raise("negative-controls.v1.json", bad_flip)


# ── 2. Entity Binding Resolution & Static Validation ───────────────────────────


def test_entity_binding_policy_and_resolution() -> None:
    plain_case = _sample_case(case_id="R1")
    assert not case_has_entity_binding(plain_case)
    assert entity_binding_policy(plain_case) == {
        "require_ui": True,
        "require_api": False,
        "require_readback": False,
    }
    assert not case_requires_api_capture(plain_case)
    assert not case_requires_entity_readback(plain_case)
    assert resolve_case_entity(plain_case) is None

    # Default policy inferred from presence of api_window / readback
    inferred_case = _sample_case(
        entity_binding={
            "entity_type": "store",
            "entity_id": {"from_fixture": "expected_store_id"},
            "attributes": {
                "store_name": {"from_fixture": "expected_store_name"},
                "session_token_id": {"from_context": "ctx_session"},
                "tier": "vip",
            },
            "api_window": {
                "expect": [{"method": "GET", "path": "/stores/${entity.id}", "assert": {"status": 200}}],
            },
        }
    )
    assert case_has_entity_binding(inferred_case)
    assert entity_binding_policy(inferred_case) == {
        "require_ui": True,
        "require_api": True,
        "require_readback": False,
    }
    assert case_requires_api_capture(inferred_case)
    assert not case_requires_entity_readback(inferred_case)

    resolved = resolve_case_entity(
        inferred_case,
        fixture_vars={
            "expected_store_id": "store-flagship-a",
            "expected_store_name": "旗舰店-A",
            "extra_var": "hello",
        },
        runtime_context={"ctx_session": "sess-99"},
    )
    assert isinstance(resolved, ResolvedEntity)
    assert resolved.entity_type == "store"
    assert resolved.entity_id == "store-flagship-a"
    assert resolved.attributes == {
        "store_name": "旗舰店-A",
        "session_token_id": "sess-99",
        "tier": "vip",
    }
    assert resolved.unresolved == ()
    assert resolved.template_vars["entity.type"] == "store"
    assert resolved.template_vars["entity.id"] == "store-flagship-a"
    assert resolved.template_vars["entity.store_name"] == "旗舰店-A"
    assert resolved.template_vars["entity.session_token_id"] == "sess-99"
    assert resolved.template_vars["entity.tier"] == "vip"
    assert resolved.template_vars["extra_var"] == "hello"

    # Missing fixture/context refs are captured in unresolved without raising
    partial = resolve_case_entity(inferred_case, fixture_vars={}, runtime_context={})
    assert partial is not None
    assert partial.entity_id is None
    assert len(partial.unresolved) == 3
    assert any("from_fixture:expected_store_id" in item for item in partial.unresolved)
    assert any("from_context:ctx_session" in item for item in partial.unresolved)


def test_validate_case_entity_binding_catches_all_static_errors(tmp_path: Path) -> None:
    readonly_op = {
        "id": "OP1",
        "status": "active",
        "revision": 1,
        "name": "read_store_preference",
        "summary": "Read persisted store preference",
        "params": [
            {"name": "store_id", "type": "string", "required": True},
            {"name": "tenant", "type": "string", "default": "default"},
        ],
        "steps": [{"action": "assert_present", "queries": ["偏好"]}],
        "navigation": {"safety": "read_only"},
        "provenance": {"source": "observed"},
    }
    mutating_op = {
        "id": "OP2",
        "status": "active",
        "revision": 1,
        "name": "submit_order_write",
        "summary": "Mutating checkout op",
        "params": [{"name": "store_id", "type": "string", "required": True}],
        "steps": [{"action": "tap_text", "query": "确认支付"}],
        "navigation": {"safety": "mutating", "effects": ["pay"]},
        "provenance": {"source": "observed"},
    }
    profile = _make_profile(tmp_path, operations=[readonly_op, mutating_op])
    fixtures = {
        "expected_store_id": "store-a",
        "expected_store_name": "旗舰店-A",
        "expected_preference_profile": "少冰少糖-VIP",
    }

    # Valid case passes with zero issues
    valid_case = _sample_case(entity_binding=_sample_entity_binding())
    assert validate_case_entity_binding(profile, valid_case, fixture_vars=fixtures) == []

    # Invalid assert key
    bad_key_case = copy.deepcopy(valid_case)
    bad_key_case["entity_binding"]["api_window"]["expect"][0]["assert"]["invalid.key"] = "x"
    issues = validate_case_entity_binding(profile, bad_key_case, fixture_vars=fixtures)
    assert any(i.code == "entity_binding_invalid_assert_key" for i in issues)

    # Housekeeping window step rejected
    bad_win_case = copy.deepcopy(valid_case)
    bad_win_case["entity_binding"]["api_window"]["window"]["step"] = "post_case_cleanup"
    issues = validate_case_entity_binding(profile, bad_win_case, fixture_vars=fixtures)
    assert any(i.code == "entity_binding_invalid_window_scope" for i in issues)

    # Unknown ${entity.*} template ref rejected
    bad_var_case = copy.deepcopy(valid_case)
    bad_var_case["entity_binding"]["api_window"]["expect"][0]["assert"]["resp.x"] = "${entity.nonexistent}"
    issues = validate_case_entity_binding(profile, bad_var_case, fixture_vars=fixtures)
    assert any(i.code == "entity_binding_unknown_entity_var" for i in issues)

    # Missing fixture ref rejected when fixture_vars provided
    issues = validate_case_entity_binding(profile, valid_case, fixture_vars={"expected_store_id": "store-a"})
    assert any(i.code == "entity_binding_unresolved_fixture" for i in issues)

    # Missing readback.op rejected
    missing_op_case = copy.deepcopy(valid_case)
    missing_op_case["entity_binding"]["readback"]["op"] = "no_such_op"
    issues = validate_case_entity_binding(profile, missing_op_case, fixture_vars=fixtures)
    assert any(i.code == "entity_binding_readback_op_missing" for i in issues)

    # Irreversible readback.op rejected (operations.yaml)
    irrev_case = copy.deepcopy(valid_case)
    irrev_case["entity_binding"]["readback"]["op"] = "submit_order_write"
    irrev_case["entity_binding"]["readback"]["with"] = {"store_id": {"from_entity": "id"}}
    issues = validate_case_entity_binding(profile, irrev_case, fixture_vars=fixtures)
    assert any(i.code == "entity_binding_readback_op_irreversible" for i in issues)

    # Policy inconsistency: require_api=True without api_window
    no_api_case = _sample_case(
        entity_binding={
            "entity_type": "store",
            "entity_id": "store-a",
            "joint_verdict": {"require_api": True},
        }
    )
    issues = validate_case_entity_binding(profile, no_api_case)
    assert any(i.code == "entity_binding_api_window_missing" for i in issues)
    assert all(isinstance(i, EntityBindingIssue) for i in issues)


# ── 3. Extended API Oracle ────────────────────────────────────────────────────


def test_api_oracle_query_headers_resp_error_and_window_isolation() -> None:
    for valid_key in (
        "status",
        "resp_error",
        "req.query.store_id",
        "req.header.X-Store-Id",
        "resp.header.X-Served-Store",
        "req.body.store.id",
        "resp.items[?(@.id=='${entity.id}')].name",
    ):
        api_oracle.static_check_assert_key(valid_key)

    for bad_key in ("req.query.", "req.header.", "resp.header.", "req.header.bad name", "other.foo", "req.items["):
        with pytest.raises(ValueError):
            api_oracle.static_check_assert_key(bad_key)

    calls = [
        # Valid interaction window call
        {
            "case_id": "R5",
            "step": "tap_ax",
            "symbol_id": "ctrl.home.preferences",
            "window_tag": "save_pref",
            "method": "POST",
            "url": "https://api.example.com/api/v1/stores/store-flagship-a/preferences?store_id=store-flagship-a&mode=sync",
            "req_headers": {"X-Store-Id": "store-flagship-a", "Content-Type": "application/json"},
            "resp_headers": {"X-Served-Store": "store-flagship-a"},
            "req_body_text": '{"preference_profile":"少冰少糖-VIP","origin":20,"discount":5}',
            "resp_body_text": '{"data":{"store_name":"旗舰店-A","final":15}}',
            "status": 200,
            "resp_error": None,
        },
        # Post-body housekeeping readback call that MUST be excluded from interaction windows
        {
            "case_id": "R5",
            "step": "entity_readback",
            "symbol_id": "ctrl.home.preferences",
            "window_tag": "save_pref",
            "method": "POST",
            "url": "https://api.example.com/api/v1/stores/fallback/preferences",
            "status": 200,
            "resp_error": None,
        },
        # Post-body cleanup call that MUST be excluded
        {
            "case_id": "R5",
            "step": "post_case_cleanup",
            "method": "POST",
            "url": "https://api.example.com/api/v1/stores/fallback/reset",
            "status": 200,
        },
        # body_end housekeeping call that MUST be excluded
        {
            "case_id": "R5",
            "step": "body_end",
            "method": "POST",
            "url": "https://api.example.com/api/v1/stores/fallback/audit",
            "status": 200,
        },
    ]

    binding = _sample_entity_binding()
    # Add a cross-element formula assertion to prove reuse of existing cross-op engine
    binding["api_window"]["expect"][0]["assert"]["resp.data.final"] = {
        "op": "computed_amount",
        "refs": {"origin": "req.origin", "discount": "req.discount"},
        "formula": "origin - discount",
    }

    ctx = {
        "entity.type": "store",
        "entity.id": "store-flagship-a",
        "entity.store_name": "旗舰店-A",
        "entity.preference_profile": "少冰少糖-VIP",
    }
    results = api_oracle.evaluate_entity_api_assertions("R5", calls, binding["api_window"], ctx=ctx)
    assert len(results) == 2
    assert all(r["verdict"] == "pass" for r in results), results
    assert api_oracle.api_status(results) == "pass"

    # If ONLY housekeeping calls exist, expect rule MUST fail as missing (no bleed!)
    housekeeping_only = [c for c in calls if c["step"] != "tap_ax"]
    missing_results = api_oracle.evaluate_entity_api_assertions(
        "R5", housekeeping_only, binding["api_window"], ctx=ctx
    )
    assert missing_results[0]["verdict"] == "fail"
    assert missing_results[0]["reason"] == "missing"

    # resp_error assertion catches transport errors
    err_calls = [
        {
            **calls[0],
            "status": None,
            "resp_error": "NSURLErrorTimedOut",
        }
    ]
    err_results = api_oracle.evaluate_entity_api_assertions(
        "R5", err_calls, binding["api_window"], ctx=ctx
    )
    assert err_results[0]["verdict"] == "fail"
    resp_err_field = next(f for f in err_results[0]["fields"] if f["key"] == "resp_error")
    assert resp_err_field["ok"] is False
    assert resp_err_field["got"] == "NSURLErrorTimedOut"

    # Unresolved ${entity.*} variable fails closed
    unres_results = api_oracle.evaluate_entity_api_assertions(
        "R5", calls, binding["api_window"], ctx={"entity.id": "store-flagship-a"}
    )
    assert unres_results[0]["verdict"] == "fail"
    assert unres_results[0]["reason"] == "unresolved_var"


# ── 4. Fail-Closed Joint Verdict Arbiter ──────────────────────────────────────


def test_joint_verdict_arbiter_all_fail_closed_scenarios() -> None:
    binding = _sample_entity_binding()
    resolved = ResolvedEntity(
        entity_type="store",
        entity_id="store-flagship-a",
        attributes={"store_name": "旗舰店-A", "preference_profile": "少冰少糖-VIP"},
        template_vars={
            "entity.type": "store",
            "entity.id": "store-flagship-a",
            "entity.store_name": "旗舰店-A",
            "entity.preference_profile": "少冰少糖-VIP",
        },
        unresolved=(),
    )

    def _assert_valid_jv_schema(jv: dict[str, Any]) -> None:
        validate_or_raise(
            "run-result.v1.json",
            {
                "schema_version": "v1",
                "run_id": "r1",
                "type": "regression",
                "app": "food_ordering",
                "device": "rig-a",
                "status": "passed" if jv["status"] == "pass" else "failed",
                "planes": {"ui": {"mode": "required", "verdict": "pass"}},
                "cases": [{"id": "R5", "status": "passed", "joint_verdict": jv}],
                "timestamps": {},
            },
        )

    # 1. All required planes pass -> pass
    passed = arbitrate_joint_verdict(
        ui_status="pass",
        api_status="pass",
        api_assertions=[{"verdict": "pass", "reason": "ok"}],
        api_captured=True,
        readback_record={"status": "verified", "ok": True, "mismatches": []},
        entity_binding=binding,
        resolved_entity=resolved,
    )
    assert passed["status"] == "pass"
    assert passed["fail_kind"] == ""
    assert passed["planes"] == {"ui": "pass", "api": "pass", "readback": "pass"}
    assert passed["mismatch_reasons"] == []
    _assert_valid_jv_schema(passed)

    # 2. Unresolved entity binding -> entity_binding_unresolved
    unres = ResolvedEntity(
        entity_type="store",
        entity_id=None,
        attributes={},
        template_vars={},
        unresolved=("entity_id:from_fixture:expected_store_id",),
    )
    jv_unres = arbitrate_joint_verdict(
        ui_status="pass",
        api_status="pass",
        api_captured=True,
        readback_record={"status": "pass"},
        entity_binding=binding,
        resolved_entity=unres,
    )
    assert jv_unres["status"] == "fail"
    assert jv_unres["fail_kind"] == "entity_binding_unresolved"
    _assert_valid_jv_schema(jv_unres)

    # 3. Missing API capture when require_api=True -> api_evidence_missing
    for api_captured, api_status in ((False, None), (True, None), (True, "pending"), (True, "missing")):
        jv_missing_api = arbitrate_joint_verdict(
            ui_status="pass",
            api_status=api_status,
            api_assertions=[],
            api_captured=api_captured,
            readback_record={"status": "pass"},
            entity_binding=binding,
            resolved_entity=resolved,
        )
        assert jv_missing_api["status"] == "fail"
        assert jv_missing_api["fail_kind"] == "api_evidence_missing"
        assert jv_missing_api["planes"]["api"] == "missing"
        _assert_valid_jv_schema(jv_missing_api)

    # 4. Wrong business entity (UI passed, API 200 but wrong store_id) -> entity_api_mismatch
    wrong_entity_assertions = [
        {
            "kind": "expect",
            "verdict": "fail",
            "reason": "assert",
            "status": 200,
            "fields": [
                {
                    "key": "req.query.store_id",
                    "op": "eq",
                    "ok": False,
                    "want": "eq 'store-flagship-a'",
                    "got": "store-other-b",
                    "entity_bound": True,
                }
            ],
            "detail": "req.query.store_id mismatch",
        }
    ]
    jv_wrong_entity = arbitrate_joint_verdict(
        ui_status="pass",
        api_status="fail",
        api_assertions=wrong_entity_assertions,
        api_captured=True,
        readback_record={"status": "pass"},
        entity_binding=binding,
        resolved_entity=resolved,
    )
    assert jv_wrong_entity["status"] == "fail"
    assert jv_wrong_entity["fail_kind"] == "entity_api_mismatch"
    assert jv_wrong_entity["planes"]["api"] == "fail"
    assert "entity_api_mismatch" in jv_wrong_entity["mismatch_reasons"]
    _assert_valid_jv_schema(jv_wrong_entity)

    # 5. Optimistic UI passed, backend API returned 500 -> optimistic_ui_api_failed
    api_500_assertions = [
        {
            "kind": "expect",
            "verdict": "fail",
            "reason": "assert",
            "status": 500,
            "fields": [
                {"key": "status", "op": "eq", "ok": False, "want": "eq 200", "got": 500},
                {"key": "resp.data.store_name", "op": "eq", "ok": False, "want": "eq '旗舰店-A'", "got": "<missing>", "entity_bound": True},
            ],
            "detail": "status want='eq 200' got=500",
        }
    ]
    jv_opt_500 = arbitrate_joint_verdict(
        ui_status="pass",
        api_status="fail",
        api_assertions=api_500_assertions,
        api_captured=True,
        readback_record={"status": "pass"},
        entity_binding=binding,
        resolved_entity=resolved,
    )
    assert jv_opt_500["status"] == "fail"
    assert jv_opt_500["fail_kind"] == "optimistic_ui_api_failed"
    assert "optimistic_ui_api_failed" in jv_opt_500["mismatch_reasons"]
    _assert_valid_jv_schema(jv_opt_500)

    # 6. Optimistic UI passed, expected API call missing -> optimistic_ui_api_failed
    jv_opt_missing = arbitrate_joint_verdict(
        ui_status="pass",
        api_status="fail",
        api_assertions=[{"kind": "expect", "verdict": "fail", "reason": "missing", "detail": "call missing"}],
        api_captured=True,
        readback_record={"status": "pass"},
        entity_binding=binding,
        resolved_entity=resolved,
    )
    assert jv_opt_missing["status"] == "fail"
    assert jv_opt_missing["fail_kind"] == "optimistic_ui_api_failed"
    _assert_valid_jv_schema(jv_opt_missing)

    # 7. Readback missing -> entity_readback_missing
    jv_rb_missing = arbitrate_joint_verdict(
        ui_status="pass",
        api_status="pass",
        api_assertions=[{"verdict": "pass"}],
        api_captured=True,
        readback_record=None,
        entity_binding=binding,
        resolved_entity=resolved,
    )
    assert jv_rb_missing["status"] == "fail"
    assert jv_rb_missing["fail_kind"] == "entity_readback_missing"
    assert jv_rb_missing["planes"]["readback"] == "missing"
    _assert_valid_jv_schema(jv_rb_missing)

    # 8. Readback diverged (UI + API 200 green, readback shows unpersisted state) -> entity_readback_mismatch
    jv_rb_mismatch = arbitrate_joint_verdict(
        ui_status="pass",
        api_status="pass",
        api_assertions=[{"verdict": "pass"}],
        api_captured=True,
        readback_record={"status": "fail", "ok": False, "mismatches": ["persisted_profile: want '少冰少糖-VIP' got '默认'"]},
        entity_binding=binding,
        resolved_entity=resolved,
    )
    assert jv_rb_mismatch["status"] == "fail"
    assert jv_rb_mismatch["fail_kind"] == "entity_readback_mismatch"
    assert jv_rb_mismatch["planes"]["readback"] == "fail"
    _assert_valid_jv_schema(jv_rb_mismatch)

    # 9. UI failed / UI error when require_ui=True
    jv_ui_fail = arbitrate_joint_verdict(
        ui_status="fail",
        api_status="pass",
        api_assertions=[{"verdict": "pass"}],
        api_captured=True,
        readback_record={"status": "pass"},
        entity_binding=binding,
        resolved_entity=resolved,
    )
    assert jv_ui_fail["status"] == "fail"
    assert jv_ui_fail["planes"]["ui"] == "fail"
    _assert_valid_jv_schema(jv_ui_fail)

    jv_ui_err = arbitrate_joint_verdict(
        ui_status="error",
        api_captured=False,
        entity_binding=binding,
        resolved_entity=resolved,
    )
    assert jv_ui_err["status"] == "error"
    assert jv_ui_err["planes"]["ui"] == "fail"
    _assert_valid_jv_schema(jv_ui_err)


# ── 5. Case Lint, Admission & Capability Requirements ─────────────────────────


def test_case_lint_validates_entity_binding_schema_and_semantics() -> None:
    good_doc = {
        "meta": {"schema_version": 2, "total": 1, "next_seq": 6},
        "cases": [_sample_case(case_id="R5", entity_binding=_sample_entity_binding())],
    }
    report = lint_case_registry_document(good_doc)
    assert report["error_count"] == 0, report["errors"]

    bad_assert_doc = copy.deepcopy(good_doc)
    bad_assert_doc["cases"][0]["entity_binding"]["api_window"]["expect"][0]["assert"]["bad_prefix.x"] = "1"
    report_bad = lint_case_registry_document(bad_assert_doc)
    assert report_bad["error_count"] >= 1
    assert any(e["code"] == "entity_binding_invalid_assert_key" for e in report_bad["errors"])


def test_admission_and_audit_enforce_entity_binding_and_readonly_readback(tmp_path: Path) -> None:
    readonly_op = {
        "id": "OP1",
        "status": "active",
        "revision": 1,
        "name": "read_store_preference",
        "summary": "Read store preference",
        "params": [
            {"name": "store_id", "type": "string", "required": True},
            {"name": "tenant", "type": "string", "default": "default"},
        ],
        "steps": [{"action": "assert_present", "queries": ["偏好"]}],
        "navigation": {"safety": "read_only"},
        "provenance": {"source": "observed"},
    }
    irrev_op = {
        "id": "OP2",
        "status": "active",
        "revision": 1,
        "name": "pay_and_submit",
        "summary": "Mutating payment op",
        "params": [{"name": "store_id", "type": "string", "required": True}],
        "steps": [{"action": "tap_text", "query": "确认支付"}],
        "navigation": {"safety": "mutating", "effects": ["pay"]},
        "provenance": {"source": "observed"},
    }
    profile = _make_profile(
        tmp_path,
        operations=[readonly_op, irrev_op],
        fixtures={
            "R5": {
                "expected_store_id": "store-a",
                "expected_store_name": "旗舰店-A",
                "expected_preference_profile": "少冰少糖-VIP",
            }
        },
    )
    (profile.recipes_dir() / "r5.yaml").write_text(
        yaml.safe_dump(
            {"id": "R5", "steps": [{"action": "assert_present", "queries": ["偏好"]}]},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    good_case = _sample_case(case_id="R5", entity_binding=_sample_entity_binding())
    assert admit_case(profile, good_case)["admitted"] is True
    assert audit_active_assets(profile, cases=[good_case]) == []

    # Irreversible readback operation MUST be blocked by admit_case & audit_active_assets
    bad_irrev = copy.deepcopy(good_case)
    bad_irrev["entity_binding"]["readback"]["op"] = "pay_and_submit"
    bad_irrev["entity_binding"]["readback"]["with"] = {"store_id": {"from_entity": "id"}}
    decision = admit_case(profile, bad_irrev)
    assert decision["admitted"] is False
    assert decision["blocker"]["code"] == "entity_binding_readback_op_irreversible"
    assert len(audit_active_assets(profile, cases=[bad_irrev])) == 1

    # Also test registered Python executor with irreversible effect (app-scoped & global)
    def _dummy_exec(ctx: Any, store_id: str) -> None:
        return None

    try:
        recipe_mod.register_app_operations(
            profile.app,
            {"py_mutating_rb": _dummy_exec},
            root=profile.root,
        )
        effects.register_app_executor_effects(
            profile.app,
            {
                "py_mutating_rb": effects.EffectDecision(
                    effects.IRREVERSIBLE,
                    "pay",
                    "test_irreversible_executor",
                )
            },
            root=profile.root,
        )
        bad_py = copy.deepcopy(good_case)
        bad_py["entity_binding"]["readback"]["op"] = "py_mutating_rb"
        bad_py["entity_binding"]["readback"]["with"] = {"store_id": {"from_entity": "id"}}
        dec_py = admit_case(profile, bad_py)
        assert dec_py["admitted"] is False
        assert dec_py["blocker"]["code"] == "entity_binding_readback_op_irreversible"
    finally:
        resolved_root = str(profile.root.resolve())
        recipe_mod.APP_OPERATIONS.pop(profile.app, None)
        recipe_mod.APP_ROOT_OPERATIONS.pop((profile.app, resolved_root), None)
        recipe_mod.APP_OPERATION_REQUIREMENTS.pop(profile.app, None)
        recipe_mod.APP_ROOT_OPERATION_REQUIREMENTS.pop((profile.app, resolved_root), None)
        effects.clear_app_executor_effects(profile.app, root=profile.root)


def test_capability_requirements_upgrades_api_capture_for_entity_binding(tmp_path: Path) -> None:
    profile = _make_profile(tmp_path)
    recipe_doc = {"steps": [{"action": "assert_present", "queries": ["首页"]}]}

    plain_case = _sample_case(case_id="R1")
    reqs_plain = derive_requirements(profile, plain_case, recipe_doc)
    assert reqs_plain["api_capture"] == "advisory"

    entity_case = _sample_case(case_id="R5", entity_binding=_sample_entity_binding())
    reqs_entity = derive_requirements(profile, entity_case, recipe_doc)
    assert reqs_entity["api_capture"] == "required"

    # Case with entity_binding but require_api=False (no api_window) stays advisory
    ui_only_entity = _sample_case(
        case_id="R6",
        entity_binding={"entity_type": "store", "entity_id": "store-a"},
    )
    reqs_ui_only = derive_requirements(profile, ui_only_entity, recipe_doc)
    assert reqs_ui_only["api_capture"] == "advisory"


# ── 6. Negative Control & Promotion (`teeth_at="joint"`) ──────────────────────


def _confirmed_run_result(
    *,
    run_id: str,
    case_row: dict[str, Any],
    build_label: str = "1.0.0-bld-aaaaaaaaaaaa",
    build_id: str = "bld-aaaaaaaaaaaa",
    artifact_sha256: str = _SHA_A,
) -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "run_id": run_id,
        "type": "regression",
        "app": "food_ordering",
        "device": "rig-a",
        "status": "passed" if case_row["status"] == "passed" else "failed",
        "package": {
            "device_version": "1.0.0",
            "profile_build_label": build_label,
            "default_slot": "default",
            "default_version": "1.0.0",
            "default_sha256": artifact_sha256,
            "registry_build_ref": {
                "build_id": build_id,
                "artifact_sha256": artifact_sha256,
                "bundle_id": "com.example.demo",
                "short_version": "1.0.0",
                "build_version": "1",
            },
            "build_identity_match": "match",
            "comparison_granularity": "build_marker",
            "comparison_build_id": build_id,
        },
        "planes": {"ui": {"mode": "required", "verdict": "pass"}},
        "cases": [case_row],
        "timestamps": {},
    }


def test_negative_control_and_promotion_joint_verdict_teeth_and_freshness(tmp_path: Path) -> None:
    steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences"},
        {"action": "assert_present", "queries": ["饮食偏好"]},
    ]
    control = {
        "id": "nc-r5-wrong-store",
        "case_id": "R5",
        "hypothesis": "switching store breaks joint entity contract even when UI stays green",
        "perturbation": {
            "kind": "replace_args",
            "step": {"action": "tap_ax"},
            "args": {"label": "wrong_store"},
        },
        "expect": {
            "plane": "joint",
            "fail_kinds": ["entity_api_mismatch", "entity_readback_mismatch"],
        },
    }
    resolved = resolve_negative_control(steps, control)
    assert resolved.expect_plane == "joint"
    assert resolved.expected_index is None
    assert resolved.expected_index_after is None
    rec_base = resolved.record(control, steps)
    assert rec_base["expect"]["plane"] == "joint"
    assert rec_base["expect"]["step_index"] is None

    # flip_witness with plane="joint" is rejected
    with pytest.raises(NegativeControlError):
        resolve_negative_control(
            [{"operation": "op_a"}],
            {
                "id": "nc-bad",
                "case_id": "R5",
                "hypothesis": "invalid",
                "perturbation": {"kind": "flip_witness", "step": {"operation": "op_a"}},
                "expect": {"plane": "joint", "step": {"operation": "op_a"}},
            },
        )

    digest = declaration_sha256(control)
    base_recipe_id = {
        "source": "draft",
        "path": "apps/food_ordering/recipes/drafts/r5.yaml",
        "recipe_sha256": _RECIPE_SHA,
        "case_revision": 1,
        "reserved_external_id": None,
        "binding_sha256": _BIND_A,
    }
    pos_row = {
        "id": "R5",
        "status": "passed",
        "revision": 1,
        "recipe_identity": base_recipe_id,
        "binding_sha256": _BIND_A,
        "planes": {
            "ui": {"mode": "required", "verdict": "pass"},
            "api": {"mode": "required", "verdict": "pass"},
            "readback": {"mode": "required", "verdict": "pass"},
        },
        "joint_verdict": {
            "status": "pass",
            "fail_kind": "",
            "detail": "ok",
            "entity_type": "store",
            "entity_id": "store-flagship-a",
            "planes": {"ui": "pass", "api": "pass", "readback": "pass"},
            "mismatch_reasons": [],
        },
    }
    neg_row = {
        "id": "R5",
        "status": "failed",
        "revision": 1,
        "binding_sha256": _BIND_A,
        "planes": {
            "ui": {"mode": "required", "verdict": "pass"},
            "api": {"mode": "required", "verdict": "fail"},
            "readback": {"mode": "required", "verdict": "pass"},
        },
        "joint_verdict": {
            "status": "fail",
            "fail_kind": "entity_api_mismatch",
            "detail": "wrong store",
            "entity_type": "store",
            "entity_id": "store-flagship-a",
            "planes": {"ui": "pass", "api": "fail", "readback": "pass"},
            "mismatch_reasons": ["entity_api_mismatch"],
        },
        "negative_control": {
            "control_id": control["id"],
            "declaration_sha256": digest,
            "applied": True,
            **rec_base,
            "base_recipe_identity": base_recipe_id,
            "base_steps_digest": "base-digest",
            "perturbed_steps_digest": "perturbed-digest",
            "failed_step_index": None,
            "fail_kind": "entity_api_mismatch",
            "binding_sha256": _BIND_A,
        },
    }
    pos_run = _confirmed_run_result(run_id="run-pos", case_row=pos_row)
    neg_run = _confirmed_run_result(run_id="run-neg", case_row=neg_row)

    probe = judge_negative_control(
        app="food_ordering",
        control=control,
        build_label="1.0.0-bld-aaaaaaaaaaaa",
        positive=pos_run,
        negative=neg_run,
        positive_rig_id="rig-a",
        negative_rig_id="rig-a",
    )
    assert probe["negative_verdict"] == VERDICT_COUNTED
    assert probe["discriminator_flipped"] is True
    assert probe["counted"] is True
    assert probe["teeth_at"] == "joint"

    # Promotion gate accepts teeth_at="joint" with fresh artifact & symbol binding digests
    draft_identity = {
        "draft_recipe_sha256": _RECIPE_SHA,
        "case_revision": 1,
        "reserved_external_id": None,
    }
    assert discrimination_gaps(
        probe,
        current_build_label="1.0.0-bld-aaaaaaaaaaaa",
        current_artifact_sha256=_SHA_A,
        current_binding_sha256=_BIND_A,
        uses_symbol_binding=True,
        current_recipe_identity=draft_identity,
    ) == []

    runs = [
        {"draft_id": "R5", "run_id": f"r{i}", "status": "passed", "review_status": "approved", "ended_at": f"2026-11-27T00:0{i}:00Z"}
        for i in range(1, 4)
    ]
    gate = evaluate_promotion_gate(
        runs,
        draft_id="R5",
        discrimination=probe,
        require_discrimination=True,
        current_build_label="1.0.0-bld-aaaaaaaaaaaa",
        current_artifact_sha256=_SHA_A,
        current_binding_sha256=_BIND_A,
        uses_symbol_binding=True,
        current_recipe_identity=draft_identity,
    )
    assert gate["ready"] is True
    assert gate["gaps"] == []

    # D6 freshness preserved on teeth_at="joint": artifact digest or binding_sha256 drift -> stale
    stale_art_gaps = discrimination_gaps(
        probe,
        current_build_label="1.0.0-bld-aaaaaaaaaaaa",
        current_artifact_sha256=_SHA_B,
        current_binding_sha256=_BIND_A,
        uses_symbol_binding=True,
        current_recipe_identity=draft_identity,
    )
    assert "discrimination_stale" in stale_art_gaps

    stale_bind_gaps = discrimination_gaps(
        probe,
        current_build_label="1.0.0-bld-aaaaaaaaaaaa",
        current_artifact_sha256=_SHA_A,
        current_binding_sha256=_BIND_B,
        uses_symbol_binding=True,
        current_recipe_identity=draft_identity,
    )
    assert "discrimination_stale" in stale_bind_gaps

    # load_discrimination_proof selects teeth_at="joint" probes cleanly
    negctl_dir = tmp_path / "negctl" / "1.0.0-bld-aaaaaaaaaaaa"
    negctl_dir.mkdir(parents=True)
    import json
    (negctl_dir / "artifact.json").write_text(
        json.dumps({"build_label": "1.0.0-bld-aaaaaaaaaaaa", "probes": [probe]}),
        encoding="utf-8",
    )
    loaded = load_discrimination_proof(
        tmp_path / "negctl",
        case_id="R5",
        app="food_ordering",
        current_artifact_sha256=_SHA_A,
        current_binding_sha256=_BIND_A,
        uses_symbol_binding=True,
    )
    assert loaded is not None
    assert loaded["teeth_at"] == "joint"

    # Toothless case: negative run stays green -> VERDICT_PASSED -> discrimination_toothless
    toothless_neg_row = copy.deepcopy(neg_row)
    toothless_neg_row["status"] = "passed"
    toothless_neg_row["joint_verdict"]["status"] = "pass"
    toothless_neg_row["joint_verdict"]["fail_kind"] = ""
    toothless_probe = judge_negative_control(
        app="food_ordering",
        control=control,
        build_label="1.0.0-bld-aaaaaaaaaaaa",
        positive=pos_run,
        negative=_confirmed_run_result(run_id="run-neg-pass", case_row=toothless_neg_row),
        positive_rig_id="rig-a",
        negative_rig_id="rig-a",
    )
    assert toothless_probe["negative_verdict"] == VERDICT_PASSED
    assert toothless_probe["teeth_at"] == "none"
    assert "discrimination_toothless" in discrimination_gaps(
        toothless_probe,
        current_build_label="1.0.0-bld-aaaaaaaaaaaa",
        current_artifact_sha256=_SHA_A,
        current_binding_sha256=_BIND_A,
        uses_symbol_binding=True,
    )

    # Readback-plane negative control (UI + API green, readback catches unpersisted write)
    rb_control = {
        "id": "nc-r5-readback-only",
        "case_id": "R5",
        "hypothesis": "readback plane flips when write does not persist",
        "perturbation": {
            "kind": "replace_args",
            "step": {"action": "tap_ax"},
            "args": {"label": "dry_run_save"},
        },
        "expect": {"plane": "readback"},
    }
    rb_resolved = resolve_negative_control(steps, rb_control)
    rb_rec_base = rb_resolved.record(rb_control, steps)
    rb_neg_row = copy.deepcopy(neg_row)
    rb_neg_row["planes"]["api"]["verdict"] = "pass"
    rb_neg_row["planes"]["readback"]["verdict"] = "fail"
    rb_neg_row["joint_verdict"]["planes"] = {"ui": "pass", "api": "pass", "readback": "fail"}
    rb_neg_row["joint_verdict"]["fail_kind"] = "entity_readback_mismatch"
    rb_neg_row["negative_control"] = {
        "control_id": rb_control["id"],
        "declaration_sha256": declaration_sha256(rb_control),
        "applied": True,
        **rb_rec_base,
        "base_recipe_identity": base_recipe_id,
        "base_steps_digest": "base-digest",
        "perturbed_steps_digest": "perturbed-digest",
        "failed_step_index": None,
        "fail_kind": "entity_readback_mismatch",
        "binding_sha256": _BIND_A,
    }
    rb_probe = judge_negative_control(
        app="food_ordering",
        control=rb_control,
        build_label="1.0.0-bld-aaaaaaaaaaaa",
        positive=pos_run,
        negative=_confirmed_run_result(run_id="run-neg-rb", case_row=rb_neg_row),
        positive_rig_id="rig-a",
        negative_rig_id="rig-a",
    )
    assert rb_probe["negative_verdict"] == VERDICT_COUNTED
    assert rb_probe["counted"] is True
    assert rb_probe["teeth_at"] == "joint"

    # Premature UI step crash (API/readback did NOT fail) under plane="joint" -> failed_elsewhere
    crash_neg_row = copy.deepcopy(neg_row)
    crash_neg_row["planes"]["ui"]["verdict"] = "fail"
    crash_neg_row["planes"]["api"]["verdict"] = "skipped"
    crash_neg_row["joint_verdict"]["planes"] = {"ui": "fail", "api": "skipped", "readback": "skipped"}
    crash_neg_row["joint_verdict"]["fail_kind"] = "ui_assertion_failed"
    crash_neg_row["negative_control"]["failed_step_index"] = 0
    crash_neg_row["negative_control"]["fail_kind"] = "locator_missing"
    crash_probe = judge_negative_control(
        app="food_ordering",
        control=control,
        build_label="1.0.0-bld-aaaaaaaaaaaa",
        positive=pos_run,
        negative=_confirmed_run_result(run_id="run-neg-crash", case_row=crash_neg_row),
        positive_rig_id="rig-a",
        negative_rig_id="rig-a",
    )
    assert crash_probe["negative_verdict"] == "failed_elsewhere"
    assert crash_probe["counted"] is False


# ── 7. Verifier Defect Regression Tests (1A, 1B, 1C, 2, 3, 4) ─────────────────


def test_verifier_1a_1b_1c_negctl_fail_closed_invariants() -> None:
    from autophone.assets.negative_control import (
        DEFAULT_API_FAIL_KINDS,
        DEFAULT_JOINT_FAIL_KINDS,
        DEFAULT_READBACK_FAIL_KINDS,
        NON_DISCRIMINATING_JOINT_FAIL_KINDS,
    )

    # 1A: Infra/missing fail kinds & ui_assertion_failed are excluded from defaults
    forbidden_defaults = NON_DISCRIMINATING_JOINT_FAIL_KINDS | {"ui_assertion_failed"}
    assert not (set(DEFAULT_API_FAIL_KINDS) & forbidden_defaults)
    assert not (set(DEFAULT_READBACK_FAIL_KINDS) & forbidden_defaults)
    assert not (set(DEFAULT_JOINT_FAIL_KINDS) & forbidden_defaults)

    steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences"},
        {"action": "assert_present", "queries": ["饮食偏好"]},
    ]
    control = {
        "id": "nc-r5-joint-default",
        "case_id": "R5",
        "hypothesis": "joint plane verifies real assertion failure only",
        "perturbation": {
            "kind": "replace_args",
            "step": {"action": "tap_ax"},
            "args": {"label": "wrong_store"},
        },
        "expect": {"plane": "joint"},
    }
    resolved = resolve_negative_control(steps, control)
    rec_base = resolved.record(control, steps)
    digest = declaration_sha256(control)
    base_recipe_id = {
        "source": "draft",
        "path": "apps/food_ordering/recipes/drafts/r5.yaml",
        "recipe_sha256": _RECIPE_SHA,
        "case_revision": 1,
        "reserved_external_id": None,
        "binding_sha256": _BIND_A,
    }
    pos_row = {
        "id": "R5",
        "status": "passed",
        "revision": 1,
        "recipe_identity": base_recipe_id,
        "binding_sha256": _BIND_A,
        "planes": {"ui": {"mode": "required", "verdict": "pass"}},
    }
    pos_run = _confirmed_run_result(run_id="run-pos", case_row=pos_row)

    # 1A: Missing API / missing readback / unresolved entity / ui_error MUST NEVER count,
    # even when explicitly requested in expect.fail_kinds!
    for infra_fk, plane_map in (
        ("api_evidence_missing", {"ui": "pass", "api": "missing", "readback": "pass"}),
        ("entity_readback_missing", {"ui": "pass", "api": "pass", "readback": "missing"}),
        ("entity_binding_unresolved", {"ui": "pass", "api": "skipped", "readback": "skipped"}),
        ("ui_error", {"ui": "fail", "api": "missing", "readback": "missing"}),
    ):
        explicit_ctrl = copy.deepcopy(control)
        explicit_ctrl["expect"]["fail_kinds"] = [infra_fk]
        explicit_rec = resolve_negative_control(steps, explicit_ctrl).record(explicit_ctrl, steps)
        neg_row = {
            "id": "R5",
            "status": "failed",
            "revision": 1,
            "binding_sha256": _BIND_A,
            "joint_verdict": {
                "status": "fail",
                "fail_kind": infra_fk,
                "detail": infra_fk,
                "entity_type": "store",
                "entity_id": "store-flagship-a",
                "planes": plane_map,
                "mismatch_reasons": [infra_fk],
            },
            "negative_control": {
                "control_id": explicit_ctrl["id"],
                "declaration_sha256": declaration_sha256(explicit_ctrl),
                "applied": True,
                **explicit_rec,
                "base_recipe_identity": base_recipe_id,
                "base_steps_digest": "base-digest",
                "perturbed_steps_digest": "perturbed-digest",
                "failed_step_index": None,
                "fail_kind": infra_fk,
                "binding_sha256": _BIND_A,
            },
        }
        probe = judge_negative_control(
            app="food_ordering",
            control=explicit_ctrl,
            build_label="1.0.0-bld-aaaaaaaaaaaa",
            positive=pos_run,
            negative=_confirmed_run_result(run_id=f"run-neg-{infra_fk}", case_row=neg_row),
            positive_rig_id="rig-a",
            negative_rig_id="rig-a",
        )
        assert probe["negative_verdict"] == "failed_unexpected_kind", (infra_fk, probe)
        assert probe["counted"] is False
        assert probe["teeth_at"] is None

    # 1B: Premature UI step crash (failed_step_index=0) with expect.step omitted MUST return
    # failed_elsewhere even if API also failed because the interaction never happened!
    early_crash_row = {
        "id": "R5",
        "status": "failed",
        "revision": 1,
        "binding_sha256": _BIND_A,
        "planes": {
            "ui": {"mode": "required", "verdict": "fail"},
            "api": {"mode": "required", "verdict": "fail"},
        },
        "joint_verdict": {
            "status": "fail",
            "fail_kind": "entity_api_mismatch",
            "detail": "expected call missing after UI crash",
            "entity_type": "store",
            "entity_id": "store-flagship-a",
            "planes": {"ui": "fail", "api": "fail", "readback": "missing"},
            "mismatch_reasons": ["entity_api_mismatch"],
        },
        "negative_control": {
            "control_id": control["id"],
            "declaration_sha256": digest,
            "applied": True,
            **rec_base,
            "base_recipe_identity": base_recipe_id,
            "base_steps_digest": "base-digest",
            "perturbed_steps_digest": "perturbed-digest",
            "failed_step_index": 0,
            "fail_kind": "locator_missing",
            "binding_sha256": _BIND_A,
        },
    }
    early_probe = judge_negative_control(
        app="food_ordering",
        control=control,
        build_label="1.0.0-bld-aaaaaaaaaaaa",
        positive=pos_run,
        negative=_confirmed_run_result(run_id="run-neg-early", case_row=early_crash_row),
        positive_rig_id="rig-a",
        negative_rig_id="rig-a",
    )
    assert early_probe["negative_verdict"] == "failed_elsewhere"
    assert early_probe["counted"] is False

    # 1C: Flat record["planes"] = {"ui": "pass", "api": "fail"} (without nested verdict dicts)
    # is accurately recognized for api/readback/joint negative controls.
    api_ctrl = {
        "id": "nc-r5-api-flat",
        "case_id": "R5",
        "hypothesis": "flat planes on negative_control record still counts API failure",
        "perturbation": {
            "kind": "replace_args",
            "step": {"action": "tap_ax"},
            "args": {"label": "wrong_store"},
        },
        "expect": {"plane": "api"},
    }
    api_rec_base = resolve_negative_control(steps, api_ctrl).record(api_ctrl, steps)
    flat_neg_row = {
        "id": "R5",
        "status": "failed",
        "revision": 1,
        "binding_sha256": _BIND_A,
        "negative_control": {
            "control_id": api_ctrl["id"],
            "declaration_sha256": declaration_sha256(api_ctrl),
            "applied": True,
            **api_rec_base,
            "base_recipe_identity": base_recipe_id,
            "base_steps_digest": "base-digest",
            "perturbed_steps_digest": "perturbed-digest",
            "failed_step_index": None,
            "fail_kind": "entity_api_mismatch",
            "planes": {"ui": "pass", "api": "fail", "readback": "pass"},
            "binding_sha256": _BIND_A,
        },
    }
    flat_probe = judge_negative_control(
        app="food_ordering",
        control=api_ctrl,
        build_label="1.0.0-bld-aaaaaaaaaaaa",
        positive=pos_run,
        negative=_confirmed_run_result(run_id="run-neg-flat", case_row=flat_neg_row),
        positive_rig_id="rig-a",
        negative_rig_id="rig-a",
    )
    assert flat_probe["negative_verdict"] == VERDICT_COUNTED
    assert flat_probe["counted"] is True
    assert flat_probe["teeth_at"] == "joint"


def test_verifier_2_joint_verdict_ui_failure_takes_primary_fail_kind_precedence() -> None:
    binding = _sample_entity_binding()
    resolved = ResolvedEntity(
        entity_type="store",
        entity_id="store-flagship-a",
        attributes={"store_name": "旗舰店-A", "preference_profile": "少冰少糖-VIP"},
        template_vars={"entity.id": "store-flagship-a"},
        unresolved=(),
    )
    jv = arbitrate_joint_verdict(
        ui_status="fail",
        api_status="fail",
        api_assertions=[{"kind": "expect", "verdict": "fail", "reason": "missing", "detail": "no call"}],
        api_captured=True,
        readback_record={"status": "not_executed", "reason": "case_body_failed"},
        entity_binding=binding,
        resolved_entity=resolved,
    )
    assert jv["status"] == "fail"
    assert jv["fail_kind"] == "ui_assertion_failed"
    assert jv["planes"] == {"ui": "fail", "api": "fail", "readback": "missing"}
    assert "ui_assertion_failed" in jv["mismatch_reasons"]
    assert "entity_readback_missing" in jv["mismatch_reasons"]


def test_verifier_3_api_oracle_excludes_housekeeping_window_tag_without_symbol_filter() -> None:
    # Even when step="operation" (nested dispatch_step inside readback/teardown) AND
    # api_window has NO window.symbol_id filter, window_tag in POST_BODY_HOUSEKEEPING_STEPS
    # MUST be excluded from interaction windows.
    api_window_no_symbol = {
        "expect": [
            {
                "name": "save_pref_any_window",
                "method": "POST",
                "path": "/api/v1/stores/${entity.id}/preferences",
                "assert": {"status": 200},
            }
        ],
    }
    leaked_calls = [
        {
            "case_id": "R5",
            "step": "operation",
            "operation": "read_store_preference",
            "window_tag": "entity_readback",
            "method": "POST",
            "url": "https://api.example.com/api/v1/stores/store-flagship-a/preferences",
            "status": 200,
        },
        {
            "case_id": "R5",
            "step": "operation",
            "operation": "cleanup_store",
            "window_tag": "post_case_cleanup",
            "method": "POST",
            "url": "https://api.example.com/api/v1/stores/store-flagship-a/preferences",
            "status": 200,
        },
        {
            "case_id": "R5",
            "step": "operation",
            "window_tag": "body_end",
            "method": "POST",
            "url": "https://api.example.com/api/v1/stores/store-flagship-a/preferences",
            "status": 200,
        },
    ]
    ctx = {"entity.id": "store-flagship-a"}
    results = api_oracle.evaluate_entity_api_assertions("R5", leaked_calls, api_window_no_symbol, ctx=ctx)
    assert len(results) == 1
    assert results[0]["verdict"] == "fail"
    assert results[0]["reason"] == "missing"

    # Also verify static validation rejects housekeeping window_tag on api_window
    bad_case = _sample_case(
        entity_binding={
            "entity_type": "store",
            "entity_id": "store-flagship-a",
            "api_window": {
                "window": {"window_tag": "entity_readback"},
                "expect": [{"method": "GET", "path": "/x", "assert": {"status": 200}}],
            },
        }
    )
    issues = validate_case_entity_binding(None, bad_case)
    assert any(i.code == "entity_binding_invalid_window_scope" for i in issues)


def test_verifier_4_malformed_operations_yaml_emits_issue_instead_of_crashing(tmp_path: Path) -> None:
    profile = _make_profile(tmp_path)
    ops_dir = profile.profile_dir / "operations"
    ops_dir.mkdir(parents=True, exist_ok=True)
    # Unquoted ': ' inside mapping value triggers yaml.scanner.ScannerError
    (ops_dir / "operations.yaml").write_text(
        "meta:\n  schema_version: 1\n  app: demo_s2\n  next_seq: 2\noperations:\n  - name: bad_op\n    desc: 目标门店: {from_entity: id} 注入\n",
        encoding="utf-8",
    )
    case = _sample_case(case_id="R5", entity_binding=_sample_entity_binding())
    issues = validate_case_entity_binding(
        profile,
        case,
        fixture_vars={
            "expected_store_id": "store-a",
            "expected_store_name": "旗舰店-A",
            "expected_preference_profile": "少冰少糖-VIP",
        },
    )
    assert any(i.code == "entity_binding_readback_op_invalid" for i in issues)

