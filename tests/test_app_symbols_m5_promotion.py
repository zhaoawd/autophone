"""M5 contract tests: negative-control exact artifact digest + binding_sha256 and D6 promotion freshness."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from autophone.application.ai_draft import _draft_gate
from autophone.application.case_exceptions import ExceptionView
from autophone.application.cases import CaseLifecycleApplication
from autophone.application.health import app_health
from autophone.application.negative_control import NegativeControlRequest, NegativeControlRunner
from autophone.assets.apps import AppProfile, create_app_profile, load_app_profile
from autophone.assets.build_identity import subject_build_key
from autophone.assets.negative_control import (
    PROBE_SCHEMA,
    binding_sha256_of,
    build_identity_of,
    canonical_artifact_content_digest,
    declaration_sha256,
    judge_negative_control,
    merge_probes,
    probe_artifact_content_digest,
    probe_binding_sha256,
    resolve_negative_control,
    rn_candidate_content_digest,
    write_probe,
)
from autophone.assets.operation_witness import operation_identity
from autophone.assets.promotion import (
    discrimination_gaps,
    discrimination_status,
    evaluate_promotion_gate,
    load_discrimination_proof,
)
from autophone.assets.promotion_identity import (
    current_artifact_content_digest,
    current_discrimination_context,
    current_draft_recipe_identity,
    current_symbol_binding_sha256,
    steps_use_symbol_binding,
)
from autophone.assets.promotion_service import PromotionExecution, PromotionService
from autophone.contracts import ContractError
from autophone.execution.engine.recipe import recipe_steps_digest
from autophone.execution.negative_control import (
    NEGATIVE_CONTROL_RECORD_KEY,
    attach_negative_control_records,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_BASE = "1" * 64
SHA_JS_1 = "2" * 64
SHA_JS_2 = "3" * 64
SHA_ASSETS = "4" * 64
BINDING_1 = "c" * 64
BINDING_2 = "d" * 64
RECIPE_SHA = "e" * 64
BUNDLE_ID = "com.autophone.foodorderingdemo.v2"
BUILD_LABEL = "1.0.0"
SBK_1_0_0_1 = subject_build_key(BUNDLE_ID, "1.0.0", "1")


def _rn_expected_digest(base: str, bundle: str, assets: str) -> str:
    payload = json.dumps(
        {
            "kind": "rn_candidate",
            "base_ipa_sha256": base.lower(),
            "main_jsbundle_sha256": bundle.lower(),
            "assets_tree_sha256": assets.lower(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _approved_runs(draft_id: str, count: int = 3) -> list[dict[str, Any]]:
    return [
        {
            "draft_id": draft_id,
            "run_id": f"run-{idx}",
            "status": "passed",
            "review_status": "approved" if idx == count - 1 else "pending",
            "ended_at": f"2026-11-26T10:0{idx}:00Z",
        }
        for idx in range(count)
    ]


def _make_run_result(
    *,
    run_id: str,
    case_id: str = "R1",
    case_status: str = "passed",
    build_label: str = BUILD_LABEL,
    build_id: str = "bld-aaaaaaaaaaaa",
    artifact_sha256: str | None = SHA_A,
    recipe_identity: dict[str, Any] | None = None,
    binding_sha256: str | None = None,
    binding_location: str = "case",
    negative_control: dict[str, Any] | None = None,
    candidate_identity: dict[str, Any] | None = None,
    revision: int = 1,
) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "app": "food_ordering",
        "slot": "001",
        "short_version": "1.0.0",
        "build_version": "1",
        "build_id": build_id,
    }
    if artifact_sha256 is not None:
        ref["artifact_sha256"] = artifact_sha256
    package: dict[str, Any] = {
        "profile_build_label": build_label,
        "build_identity_match": "match",
        "comparison_granularity": "build_marker",
        "comparison_build_id": build_id,
        "registry_build_ref": ref,
        "device_build_observation": {
            "bundle_id": BUNDLE_ID,
            "short_version": "1.0.0",
            "build_version": "1",
            "observed_at": "2026-11-26T10:00:00Z",
        },
    }
    if candidate_identity is not None:
        package["candidate_identity"] = dict(candidate_identity)
    rec_id = dict(
        recipe_identity
        or {
            "source": "draft",
            "path": f"recipes/drafts/{case_id.lower()}.yaml",
            "recipe_sha256": RECIPE_SHA,
            "case_revision": revision,
            "reserved_external_id": None,
        }
    )
    case_row: dict[str, Any] = {
        "id": case_id,
        "status": case_status,
        "revision": revision,
        "recipe_identity": rec_id,
    }
    if negative_control is not None:
        case_row["negative_control"] = dict(negative_control)
    if binding_sha256 is not None:
        if binding_location == "case":
            case_row["binding_sha256"] = binding_sha256
        elif binding_location == "recipe_identity":
            case_row["recipe_identity"]["binding_sha256"] = binding_sha256
        elif binding_location == "negative_control":
            case_row.setdefault("negative_control", {})["binding_sha256"] = binding_sha256
        else:
            raise AssertionError(binding_location)
    return {
        "run_id": run_id,
        "status": case_status,
        "package": package,
        "cases": [case_row],
    }


def _make_counted_probe(
    *,
    case_id: str = "R1",
    control_id: str = "neg-nav",
    build_label: str = BUILD_LABEL,
    artifact_sha256: str | None = SHA_A,
    binding_sha256: str | None = None,
    binding_location: str = "case",
    candidate_identity: dict[str, Any] | None = None,
    recipe_identity: dict[str, Any] | None = None,
    base_steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    steps = base_steps or [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "open prefs"},
        {"action": "assert_present", "queries": ["饮食偏好"]},
    ]
    control = {
        "id": control_id,
        "case_id": case_id,
        "hypothesis": "skipping navigation must fail the target-page assertion",
        "perturbation": {"kind": "skip_step", "step": {"action": "tap_ax"}},
        "expect": {"step": {"action": "assert_present"}, "fail_kinds": ["assertion_failed"]},
    }
    resolved = resolve_negative_control(steps, control)
    rec_id = dict(
        recipe_identity
        or {
            "source": "draft",
            "path": f"recipes/drafts/{case_id.lower()}.yaml",
            "recipe_sha256": RECIPE_SHA,
            "case_revision": 1,
            "reserved_external_id": None,
        }
    )
    neg_record = {
        "control_id": control_id,
        "declaration_sha256": declaration_sha256(control),
        **resolved.record(control, steps),
        "base_recipe_identity": dict(rec_id),
        "base_steps_digest": recipe_steps_digest(steps),
        "perturbed_steps_digest": recipe_steps_digest(resolved.steps),
        "applied": True,
        "failed_step_index": resolved.expected_index_after,
        "fail_kind": "assertion_failed",
    }
    pos = _make_run_result(
        run_id="run-pos",
        case_id=case_id,
        case_status="passed",
        build_label=build_label,
        artifact_sha256=artifact_sha256,
        recipe_identity=rec_id,
        binding_sha256=binding_sha256,
        binding_location="recipe_identity" if binding_location == "negative_control" else binding_location,
        candidate_identity=candidate_identity,
    )
    neg = _make_run_result(
        run_id="run-neg",
        case_id=case_id,
        case_status="failed",
        build_label=build_label,
        artifact_sha256=artifact_sha256,
        recipe_identity=rec_id,
        binding_sha256=binding_sha256,
        binding_location=binding_location,
        negative_control=neg_record,
        candidate_identity=candidate_identity,
    )
    return judge_negative_control(
        app="food_ordering",
        control=control,
        build_label=build_label,
        positive=pos,
        negative=neg,
        positive_rig_id="rig-a",
        negative_rig_id="rig-a",
        target_rig_id="rig-a",
        generated_at="2026-11-26T10:10:00Z",
    )


def _write_manifest_v2(
    state_root: Path,
    app: str,
    *,
    build_id: str,
    artifact_sha256: str,
    short_version: str = "1.0.0",
    build_version: str = "1",
    slot: str = "001",
) -> Path:
    path = state_root / "packages" / app / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema_version": "v2",
        "app": app,
        "bundle_id": BUNDLE_ID,
        "default_slot": slot,
        "version_slots": [
            {
                "slot": slot,
                "short_version": short_version,
                "candidate_build_id": build_id,
                "created_at": "2026-11-26T10:00:00Z",
            }
        ],
        "builds": [
            {
                "build_id": build_id,
                "slot": slot,
                "bundle_id": BUNDLE_ID,
                "build_version": build_version,
                "artifact": {
                    "file": f"blobs/{artifact_sha256}.ipa",
                    "sha256": artifact_sha256,
                    "size": 1024,
                },
                "added_at": "2026-11-26T10:00:00Z",
            }
        ],
    }
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def _write_symbol_file(
    profile: AppProfile,
    *,
    artifact_sha256: str,
    build_id: str = "bld-aaaaaaaaaaaa",
    identifier: str = "home.ai_prompt.preferences",
) -> tuple[Path, str]:
    doc = {
        "schema": "autophone.app-symbols/1",
        "app": profile.app,
        "artifact_identity": {
            "kind": "ipa_build",
            "bundle_id": profile.bundle_id,
            "short_version": "1.0.0",
            "build_version": "1",
            "build_id": build_id,
            "artifact_sha256": artifact_sha256,
        },
        "controls": {
            "ctrl.home.preferences": {
                "kind": "button",
                "scope": {"screen_id": "S01_home", "region": [0.05, 0.15, 0.95, 0.90]},
                "locator": {
                    "kind": "ax_identifier",
                    "identifier": identifier,
                    "role": "button",
                    "enabled_only": True,
                    "require_unique": True,
                },
                "effects": {"verbs": [], "completeness": "exhaustive"},
            }
        },
    }
    path = profile.profile_dir / "symbols" / "artifacts" / f"{artifact_sha256[:12]}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = yaml.safe_dump(doc, sort_keys=False).encode("utf-8")
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def _create_app_with_build_label(tmp_path: Path, app: str = "food_ordering", build_label: str = BUILD_LABEL) -> AppProfile:
    create_app_profile(app, BUNDLE_ID, "Food Ordering", root=tmp_path)
    profile_path = tmp_path / "apps" / app / "profile.yaml"
    raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    raw["build_label"] = build_label
    profile_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return load_app_profile(app, root=tmp_path, state_root=tmp_path)


# ── 1. Canonical pure-content artifact_content_digest contract ────────────────


def test_canonical_artifact_content_digest_ipa_and_rn_candidate_ignore_target_fingerprint():
    assert canonical_artifact_content_digest({"kind": "ipa_build", "artifact_sha256": SHA_A.upper()}) == SHA_A

    expected_rn = _rn_expected_digest(SHA_BASE, SHA_JS_1, SHA_ASSETS)
    assert rn_candidate_content_digest(SHA_BASE.upper(), SHA_JS_1.upper(), SHA_ASSETS.upper()) == expected_rn

    cand_rig_a = {
        "target_fingerprint": "sha256:" + "9" * 64,
        "bundle_id": BUNDLE_ID,
        "base_build_id": "bld-111111111111",
        "base_ipa_sha256": SHA_BASE,
        "main_jsbundle_sha256": SHA_JS_1,
        "assets_tree_sha256": SHA_ASSETS,
    }
    cand_rig_b = {**cand_rig_a, "target_fingerprint": "sha256:" + "8" * 64}
    assert canonical_artifact_content_digest(cand_rig_a) == expected_rn
    assert canonical_artifact_content_digest(cand_rig_b) == expected_rn

    # Changing main_jsbundle_sha256 MUST change the canonical digest.
    cand_js2 = {**cand_rig_a, "main_jsbundle_sha256": SHA_JS_2}
    assert canonical_artifact_content_digest(cand_js2) == _rn_expected_digest(SHA_BASE, SHA_JS_2, SHA_ASSETS)
    assert canonical_artifact_content_digest(cand_js2) != expected_rn

    # target_fingerprint prefixed strings & incomplete triplets fail closed.
    assert canonical_artifact_content_digest({"artifact_sha256": "sha256:" + SHA_A}) is None
    assert canonical_artifact_content_digest({"kind": "rn_candidate", "base_ipa_sha256": SHA_BASE}) is None
    # Contradictory explicit artifact_content_digest vs artifact_sha256 fails closed.
    assert canonical_artifact_content_digest({"artifact_sha256": SHA_A, "artifact_content_digest": SHA_B}) is None

    # Direct build_identity_of verification (IPA + RN candidate + malformed RN fail-closed).
    bi_rn = build_identity_of(
        _make_run_result(run_id="r-rn", artifact_sha256=SHA_BASE, candidate_identity=cand_rig_a)
    )
    assert bi_rn["artifact_content_digest"] == expected_rn
    assert "target_fingerprint" not in bi_rn
    assert "target_fingerprint" not in bi_rn["rn_candidate_digests"]
    bi_rn_partial = build_identity_of(
        _make_run_result(
            run_id="r-rn-bad",
            artifact_sha256=SHA_BASE,
            candidate_identity={"base_ipa_sha256": SHA_BASE, "main_jsbundle_sha256": SHA_JS_1},
        )
    )
    assert bi_rn_partial["artifact_content_digest"] is None
    assert "rn_candidate_digests" not in bi_rn_partial


# ── 2. negative_control.py extraction, consistency, superseding, execution ───


@pytest.mark.parametrize("binding_location", ["case", "recipe_identity", "negative_control"])
def test_judge_negative_control_persists_exact_digest_and_binding_sha256(binding_location):
    probe = _make_counted_probe(artifact_sha256=SHA_A, binding_sha256=BINDING_1, binding_location=binding_location)
    assert probe["schema"] == PROBE_SCHEMA
    assert probe["counted"] is True
    assert probe["negative_verdict"] == "failed_on_expected_step"
    assert probe["binding_sha256"] == BINDING_1
    bi = probe["build_identity"]
    assert bi["artifact_sha256"] == SHA_A
    assert bi["artifact_content_digest"] == SHA_A
    assert bi["subject_build_key"] == SBK_1_0_0_1
    assert "target_fingerprint" not in bi
    assert probe_artifact_content_digest(probe) == SHA_A
    assert probe_binding_sha256(probe) == BINDING_1


def test_judge_negative_control_rn_candidate_records_triplet_and_strips_target_fingerprint():
    cand = {
        "target_fingerprint": "sha256:" + "7" * 64,
        "bundle_id": BUNDLE_ID,
        "base_build_id": "bld-111111111111",
        "base_ipa_sha256": SHA_BASE,
        "main_jsbundle_sha256": SHA_JS_1,
        "assets_tree_sha256": SHA_ASSETS,
    }
    probe = _make_counted_probe(artifact_sha256=SHA_BASE, binding_sha256=BINDING_1, candidate_identity=cand)
    expected_rn = _rn_expected_digest(SHA_BASE, SHA_JS_1, SHA_ASSETS)
    assert probe["counted"] is True
    bi = probe["build_identity"]
    assert bi["artifact_content_digest"] == expected_rn
    assert bi["rn_candidate_digests"] == {
        "base_ipa_sha256": SHA_BASE,
        "main_jsbundle_sha256": SHA_JS_1,
        "assets_tree_sha256": SHA_ASSETS,
    }
    assert "target_fingerprint" not in bi
    assert "target_fingerprint" not in bi["rn_candidate_digests"]


def test_judge_negative_control_fails_closed_on_mismatched_digest_or_binding():
    steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences"},
        {"action": "assert_present", "queries": ["饮食偏好"]},
    ]
    control = {
        "id": "neg-nav",
        "case_id": "R1",
        "hypothesis": "nav skip must fail",
        "perturbation": {"kind": "skip_step", "step": {"action": "tap_ax"}},
        "expect": {"step": {"action": "assert_present"}},
    }
    resolved = resolve_negative_control(steps, control)
    rec_id = {
        "source": "draft",
        "path": "recipes/drafts/r1.yaml",
        "recipe_sha256": RECIPE_SHA,
        "case_revision": 1,
        "reserved_external_id": None,
    }
    neg_record = {
        "control_id": "neg-nav",
        "declaration_sha256": declaration_sha256(control),
        **resolved.record(control, steps),
        "base_recipe_identity": dict(rec_id),
        "base_steps_digest": recipe_steps_digest(steps),
        "perturbed_steps_digest": recipe_steps_digest(resolved.steps),
        "applied": True,
        "failed_step_index": resolved.expected_index_after,
        "fail_kind": "assertion_failed",
    }

    # (a) Mismatched artifact_content_digest between positive and negative (same version 1.0.0 (1))
    pos_a = _make_run_result(run_id="pos-a", artifact_sha256=SHA_A, binding_sha256=BINDING_1, recipe_identity=rec_id)
    neg_b = _make_run_result(
        run_id="neg-b",
        case_status="failed",
        artifact_sha256=SHA_B,
        binding_sha256=BINDING_1,
        recipe_identity=rec_id,
        negative_control=neg_record,
    )
    probe_digest_mismatch = judge_negative_control(
        app="food_ordering",
        control=control,
        build_label=BUILD_LABEL,
        positive=pos_a,
        negative=neg_b,
        positive_rig_id="rig-a",
        negative_rig_id="rig-a",
    )
    assert probe_digest_mismatch["counted"] is False
    assert probe_digest_mismatch["negative_verdict"] == "identity_unconfirmed"
    assert "negative_artifact_content_digest_mismatch" in probe_digest_mismatch["identity_reasons"]

    # (b) Mismatched binding_sha256 between positive and negative
    neg_bind2 = _make_run_result(
        run_id="neg-bind2",
        case_status="failed",
        artifact_sha256=SHA_A,
        binding_sha256=BINDING_2,
        recipe_identity=rec_id,
        negative_control=neg_record,
    )
    probe_bind_mismatch = judge_negative_control(
        app="food_ordering",
        control=control,
        build_label=BUILD_LABEL,
        positive=pos_a,
        negative=neg_bind2,
        positive_rig_id="rig-a",
        negative_rig_id="rig-a",
    )
    assert probe_bind_mismatch["counted"] is False
    assert probe_bind_mismatch["negative_verdict"] == "identity_unconfirmed"
    assert "negative_binding_sha256_mismatch" in probe_bind_mismatch["identity_reasons"]

    # (c) Positive missing artifact_sha256 -> fail-closed
    pos_no_sha = _make_run_result(run_id="pos-nosha", artifact_sha256=None, recipe_identity=rec_id)
    neg_no_sha = _make_run_result(
        run_id="neg-nosha", case_status="failed", artifact_sha256=None, recipe_identity=rec_id, negative_control=neg_record,
    )
    probe_missing_digest = judge_negative_control(
        app="food_ordering",
        control=control,
        build_label=BUILD_LABEL,
        positive=pos_no_sha,
        negative=neg_no_sha,
        positive_rig_id="rig-a",
        negative_rig_id="rig-a",
    )
    assert probe_missing_digest["counted"] is False
    assert probe_missing_digest["negative_verdict"] == "identity_unconfirmed"
    assert "positive_artifact_content_digest_missing" in probe_missing_digest["identity_reasons"]

    # (d) Contradictory binding_sha256 inside one case row -> fail-closed
    pos_conflict = _make_run_result(run_id="pos-conflict", artifact_sha256=SHA_A, binding_sha256=BINDING_1, recipe_identity=rec_id)
    pos_conflict["cases"][0]["recipe_identity"]["binding_sha256"] = BINDING_2
    assert binding_sha256_of(pos_conflict, "R1") is None
    probe_conflict = judge_negative_control(
        app="food_ordering",
        control=control,
        build_label=BUILD_LABEL,
        positive=pos_conflict,
        negative=neg_bind2,
        positive_rig_id="rig-a",
        negative_rig_id="rig-a",
    )
    assert probe_conflict["counted"] is False
    assert "positive_binding_sha256_conflict" in probe_conflict["identity_reasons"]


def test_merge_probes_supersedes_counted_probe_on_new_artifact_or_binding():
    old_counted = _make_counted_probe(artifact_sha256=SHA_A, binding_sha256=BINDING_1)
    assert old_counted["counted"] is True

    # Same artifact + same binding + inconclusive rerun -> keeps counted probe with latest_attempt
    same_inconclusive = {
        **old_counted,
        "counted": False,
        "discriminator_flipped": False,
        "negative_verdict": "error",
        "generated_at": "2026-11-26T10:15:00Z",
    }
    merged_same = merge_probes([old_counted], same_inconclusive)
    assert len(merged_same) == 1
    assert merged_same[0]["counted"] is True
    assert merged_same[0]["latest_attempt"]["negative_verdict"] == "error"

    # New artifact_content_digest -> supersedes old counted probe even if inconclusive
    new_artifact_probe = {
        **same_inconclusive,
        "build_identity": {**old_counted["build_identity"], "artifact_sha256": SHA_B, "artifact_content_digest": SHA_B},
    }
    merged_new_art = merge_probes([old_counted], new_artifact_probe)
    assert len(merged_new_art) == 1
    assert merged_new_art[0]["counted"] is False
    assert probe_artifact_content_digest(merged_new_art[0]) == SHA_B

    # New binding_sha256 -> supersedes old counted probe even if inconclusive
    new_binding_probe = {**same_inconclusive, "binding_sha256": BINDING_2}
    merged_new_bind = merge_probes([old_counted], new_binding_probe)
    assert len(merged_new_bind) == 1
    assert merged_new_bind[0]["counted"] is False
    assert merged_new_bind[0]["binding_sha256"] == BINDING_2


def test_attach_negative_control_records_preserves_binding_sha256():
    base_record = {
        "control_id": "neg-nav",
        "declaration_sha256": "f" * 64,
        "perturbation": {"kind": "skip_step", "step_index": 0},
        "expect": {"step_index": 0, "fail_kinds": ["assertion_failed"]},
        "base_recipe_identity": {"source": "draft", "recipe_sha256": RECIPE_SHA},
        "perturbed_steps_digest": "digest-1",
    }
    cases = [
        {
            "id": "R1",
            NEGATIVE_CONTROL_RECORD_KEY: dict(base_record),
            "_symbol_binding_sha256": BINDING_1,
        },
        {
            "id": "R2",
            NEGATIVE_CONTROL_RECORD_KEY: dict(base_record),
        },
    ]
    outcomes = [
        SimpleNamespace(id="R1", exec_via="recipe", recipe_steps_digest="digest-1", failed_recipe_step=0, status="fail", fail_kind="assertion_failed"),
        SimpleNamespace(id="R2", exec_via="recipe", recipe_steps_digest="digest-1", failed_recipe_step=0, status="fail", fail_kind="assertion_failed"),
    ]
    attach_negative_control_records(outcomes, cases)
    assert outcomes[0].negative_control["binding_sha256"] == BINDING_1
    assert outcomes[0].negative_control["applied"] is True
    assert "binding_sha256" not in outcomes[1].negative_control


# ── 3. Counterexample #3: Global exact artifact freshness & binding_sha256 ───


def test_counterexample_3_same_version_triplet_different_artifact_sha256_is_stale():
    probe = _make_counted_probe(artifact_sha256=SHA_A, binding_sha256=None)
    assert probe["build_identity"]["subject_build_key"] == SBK_1_0_0_1

    # Same build_label ("1.0.0") AND same subject_build_key ("1.0.0 (1)"), DIFFERENT artifact_sha256!
    gaps = discrimination_gaps(
        probe,
        current_build_label=BUILD_LABEL,
        current_build_identity_ref={"subject_build_key": SBK_1_0_0_1},
        current_artifact_content_digest=SHA_B,
    )
    assert "discrimination_stale" in gaps

    status = discrimination_status(
        required=True,
        proof=probe,
        current_build_label=BUILD_LABEL,
        current_build_identity_ref={"subject_build_key": SBK_1_0_0_1},
        current_artifact_content_digest=SHA_B,
    )
    assert status["state"] == "stale"

    gate = evaluate_promotion_gate(
        _approved_runs("R1"),
        draft_id="R1",
        discrimination=probe,
        require_discrimination=True,
        current_build_label=BUILD_LABEL,
        current_artifact_content_digest=SHA_B,
    )
    assert gate["ready"] is False
    assert "discrimination_stale" in gate["gaps"]


def test_counterexample_3_rn_candidate_different_main_jsbundle_is_stale():
    cand_v1 = {
        "target_fingerprint": "sha256:" + "1" * 64,
        "bundle_id": BUNDLE_ID,
        "base_build_id": "bld-111111111111",
        "base_ipa_sha256": SHA_BASE,
        "main_jsbundle_sha256": SHA_JS_1,
        "assets_tree_sha256": SHA_ASSETS,
    }
    probe = _make_counted_probe(artifact_sha256=SHA_BASE, binding_sha256=BINDING_1, candidate_identity=cand_v1)

    # Different target_fingerprint ONLY -> still fresh & passes!
    cand_v1_other_rig = {**cand_v1, "target_fingerprint": "sha256:" + "2" * 64}
    assert (
        discrimination_gaps(
            probe,
            current_build_label=BUILD_LABEL,
            current_build_identity=cand_v1_other_rig,
            current_binding_sha256=BINDING_1,
            uses_symbol_binding=True,
        )
        == []
    )

    # Same base IPA + same version 1.0.0 (1), DIFFERENT main_jsbundle_sha256 -> discrimination_stale!
    cand_v2 = {**cand_v1, "main_jsbundle_sha256": SHA_JS_2}
    gaps = discrimination_gaps(
        probe,
        current_build_label=BUILD_LABEL,
        current_build_identity=cand_v2,
        current_binding_sha256=BINDING_1,
        uses_symbol_binding=True,
    )
    assert "discrimination_stale" in gaps
    gate = evaluate_promotion_gate(
        _approved_runs("R1"),
        draft_id="R1",
        discrimination=probe,
        require_discrimination=True,
        current_build_label=BUILD_LABEL,
        current_build_identity=cand_v2,
        current_binding_sha256=BINDING_1,
        uses_symbol_binding=True,
    )
    assert gate["ready"] is False
    assert "discrimination_stale" in gate["gaps"]


def test_counterexample_3_legacy_probe_lacking_exact_artifact_digest_is_globally_stale():
    # Legacy probe has matching build_label AND matching subject_build_key, NO artifact_sha256/artifact_content_digest.
    legacy_probe = {
        "build_label": BUILD_LABEL,
        "case_id": "R1",
        "target_green": True,
        "discriminator_flipped": True,
        "teeth_at": "recipe",
        "build_identity_ref": {"subject_build_key": SBK_1_0_0_1},
    }
    # Even for a NON-SYMBOL asset and even without passing current_artifact_content_digest!
    gaps = discrimination_gaps(
        legacy_probe,
        current_build_label=BUILD_LABEL,
        current_build_identity_ref={"subject_build_key": SBK_1_0_0_1},
        uses_symbol_binding=False,
    )
    assert gaps == ["discrimination_stale"]

    gate = evaluate_promotion_gate(
        _approved_runs("R1"),
        draft_id="R1",
        discrimination=legacy_probe,
        require_discrimination=True,
        current_build_label=BUILD_LABEL,
        uses_symbol_binding=False,
    )
    assert gate["ready"] is False
    assert "discrimination_stale" in gate["gaps"]

    # Also test generic probe whose build_identity only has build_id (no artifact_sha256/artifact_content_digest).
    legacy_generic = {
        "schema": PROBE_SCHEMA,
        "build_label": BUILD_LABEL,
        "case_id": "R1",
        "target_green": True,
        "discriminator_flipped": True,
        "teeth_at": "recipe",
        "negative_verdict": "failed_on_expected_step",
        "counted": True,
        "build_identity": {
            "profile_build_label": BUILD_LABEL,
            "build_identity_match": "match",
            "build_id": "bld-aaaaaaaaaaaa",
            "artifact_sha256": None,
        },
    }
    assert "discrimination_stale" in discrimination_gaps(legacy_generic, current_build_label=BUILD_LABEL)


def test_counterexample_3_symbol_binding_change_or_missing_is_stale():
    # (a) Probe recorded BINDING_1, symbol file updated to BINDING_2 -> stale
    probe_b1 = _make_counted_probe(artifact_sha256=SHA_A, binding_sha256=BINDING_1)
    gaps_changed = discrimination_gaps(
        probe_b1,
        current_build_label=BUILD_LABEL,
        current_artifact_content_digest=SHA_A,
        current_binding_sha256=BINDING_2,
        uses_symbol_binding=True,
    )
    assert gaps_changed == ["discrimination_stale"]

    # (b) Probe lacks binding_sha256 (None), asset uses symbol binding -> stale
    probe_unbound = _make_counted_probe(artifact_sha256=SHA_A, binding_sha256=None)
    gaps_missing = discrimination_gaps(
        probe_unbound,
        current_build_label=BUILD_LABEL,
        current_artifact_content_digest=SHA_A,
        current_binding_sha256=BINDING_1,
        uses_symbol_binding=True,
    )
    assert gaps_missing == ["discrimination_stale"]

    # (c) Asset uses symbol binding but current_binding_sha256 is unavailable (None) -> fail-closed stale
    gaps_unresolved = discrimination_gaps(
        probe_b1,
        current_build_label=BUILD_LABEL,
        current_artifact_content_digest=SHA_A,
        current_binding_sha256=None,
        uses_symbol_binding=True,
    )
    assert gaps_unresolved == ["discrimination_stale"]

    # (d) Exact matching artifact_content_digest + exact matching binding_sha256 -> passes!
    gate_ok = evaluate_promotion_gate(
        _approved_runs("R1"),
        draft_id="R1",
        discrimination=probe_b1,
        require_discrimination=True,
        current_build_label=BUILD_LABEL,
        current_artifact_content_digest=SHA_A,
        current_binding_sha256=BINDING_1,
        uses_symbol_binding=True,
    )
    assert gate_ok["ready"] is True
    assert gate_ok["gaps"] == []


def test_load_discrimination_proof_preserves_binding_and_build_identity_fields(tmp_path):
    negctl_root = tmp_path / "runs" / "negctl"
    stale_probe = _make_counted_probe(
        case_id="R1", control_id="neg-old", artifact_sha256=SHA_A, binding_sha256=BINDING_1,
    )
    fresh_probe = _make_counted_probe(
        case_id="R1", control_id="neg-fresh", artifact_sha256=SHA_B, binding_sha256=BINDING_2,
    )
    write_probe(negctl_root, stale_probe, generated_at="2026-11-26T10:00:00Z")
    write_probe(negctl_root, fresh_probe, generated_at="2026-11-26T10:05:00Z")

    loaded = load_discrimination_proof(
        negctl_root,
        case_id="R1",
        app="food_ordering",
        preferred_build_label=BUILD_LABEL,
        current_artifact_content_digest=SHA_B,
        current_binding_sha256=BINDING_2,
        uses_symbol_binding=True,
    )
    assert loaded is not None
    assert loaded["control_id"] == "neg-fresh"
    assert loaded["binding_sha256"] == BINDING_2
    assert loaded["build_identity"]["artifact_content_digest"] == SHA_B
    assert loaded["build_identity"]["artifact_sha256"] == SHA_B
    assert loaded["build_identity"]["build_id"] == "bld-aaaaaaaaaaaa"


# ── 4. Counterexample #4: skip_step + weak shared header assertion ───────────


def test_counterexample_4_skip_step_weak_shared_header_is_toothless_and_blocks_promotion(tmp_path):
    steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "navigate to preferences"},
        {"action": "assert_present", "queries": ["Food Ordering"]},  # weak shared top header on home & target
    ]
    control = {
        "id": "neg-skip-nav-weak-header",
        "case_id": "R1",
        "hypothesis": "skipping navigation must fail the postcondition assertion",
        "perturbation": {"kind": "skip_step", "step": {"action": "tap_ax"}},
        "expect": {"step": {"action": "assert_present"}, "fail_kinds": ["assertion_failed"]},
    }
    resolved = resolve_negative_control(steps, control)
    assert resolved.perturbed_index == 0
    assert resolved.expected_index == 1
    assert resolved.expected_index_after == 0
    assert resolved.steps == [{"action": "assert_present", "queries": ["Food Ordering"]}]

    rec_id = {
        "source": "draft",
        "path": "recipes/drafts/r1.yaml",
        "recipe_sha256": RECIPE_SHA,
        "case_revision": 1,
        "reserved_external_id": None,
    }
    neg_record = {
        "control_id": control["id"],
        "declaration_sha256": declaration_sha256(control),
        **resolved.record(control, steps),
        "base_recipe_identity": dict(rec_id),
        "base_steps_digest": recipe_steps_digest(steps),
        "perturbed_steps_digest": recipe_steps_digest(resolved.steps),
        "applied": True,
        "failed_step_index": None,
        "fail_kind": None,
    }
    positive = _make_run_result(
        run_id="run-pos-weak",
        case_id="R1",
        case_status="passed",
        artifact_sha256=SHA_A,
        binding_sha256=BINDING_1,
        recipe_identity=rec_id,
    )
    # Weak header assertion STILL PASSED on home screen after skipping tap_ax!
    negative = _make_run_result(
        run_id="run-neg-weak",
        case_id="R1",
        case_status="passed",
        artifact_sha256=SHA_A,
        binding_sha256=BINDING_1,
        recipe_identity=rec_id,
        negative_control=neg_record,
    )

    probe = judge_negative_control(
        app="food_ordering",
        control=control,
        build_label=BUILD_LABEL,
        positive=positive,
        negative=negative,
        positive_rig_id="rig-a",
        negative_rig_id="rig-a",
        target_rig_id="rig-a",
        generated_at="2026-11-26T10:20:00Z",
    )
    assert probe["target_green"] is True
    assert probe["negative_verdict"] == "passed"
    assert probe["discriminator_flipped"] is False
    assert probe["teeth_at"] == "none"
    assert probe["counted"] is False
    assert probe["identity_reasons"] == []
    assert probe["build_identity"]["artifact_content_digest"] == SHA_A
    assert probe["binding_sha256"] == BINDING_1

    negctl_root = tmp_path / "runs" / "negctl"
    write_probe(negctl_root, probe, generated_at="2026-11-26T10:20:00Z")
    loaded = load_discrimination_proof(
        negctl_root,
        case_id="R1",
        app="food_ordering",
        preferred_build_label=BUILD_LABEL,
        current_artifact_content_digest=SHA_A,
        current_binding_sha256=BINDING_1,
        uses_symbol_binding=True,
    )
    assert loaded is not None

    gate = evaluate_promotion_gate(
        _approved_runs("R1"),
        draft_id="R1",
        discrimination=loaded,
        require_discrimination=True,
        current_build_label=BUILD_LABEL,
        current_artifact_content_digest=SHA_A,
        current_binding_sha256=BINDING_1,
        uses_symbol_binding=True,
    )
    assert gate["ready"] is False
    assert "discrimination_toothless" in gate["gaps"]
    assert "discrimination_stale" not in gate["gaps"]
    assert "discrimination_inconclusive" not in gate["gaps"]

    status = discrimination_status(
        required=True,
        proof=loaded,
        current_build_label=BUILD_LABEL,
        current_artifact_content_digest=SHA_A,
        current_binding_sha256=BINDING_1,
        uses_symbol_binding=True,
    )
    assert status["state"] == "toothless"


# ── 5. End-to-end Caller Integration (Service, Cases, Exceptions, Health, HTTP, AI Draft, Runner) ──


def test_promotion_service_case_and_operation_enforce_exact_digest_and_binding_freshness(tmp_path):
    profile = _create_app_with_build_label(tmp_path)
    _write_manifest_v2(tmp_path, profile.app, build_id="bld-aaaaaaaaaaaa", artifact_sha256=SHA_A)
    sym_path, binding_v1 = _write_symbol_file(profile, artifact_sha256=SHA_A)

    # Seed Case R1 (draft) + draft symbol-bound recipe
    cases_doc = {
        "meta": {"title": "Food Ordering", "app": profile.app, "schema_version": 2, "total": 1, "next_seq": 2},
        "cases": [
            {
                "id": "R1",
                "status": "draft",
                "revision": 1,
                "module": "home",
                "item": "Open preferences",
                "automation": "ui_ready",
                "steps": ["Tap preferences"],
                "expects": ["Preferences visible"],
            }
        ],
    }
    profile.cases_path().write_text(yaml.safe_dump(cases_doc, sort_keys=False), encoding="utf-8")
    draft_steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "open prefs"},
        {"action": "assert_present", "queries": ["饮食偏好"]},
    ]
    draft_path = profile.recipes_dir() / "drafts" / "r1.yaml"
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(yaml.safe_dump({"id": "R1", "steps": draft_steps}, sort_keys=False), encoding="utf-8")

    assert current_artifact_content_digest(profile) == SHA_A
    assert current_symbol_binding_sha256(profile, artifact_content_digest=SHA_A) == binding_v1
    assert steps_use_symbol_binding(draft_steps) is True
    assert current_discrimination_context(profile, case_id="R1", recipe_source="draft") == {
        "current_artifact_content_digest": SHA_A,
        "current_binding_sha256": binding_v1,
        "uses_symbol_binding": True,
    }

    draft_id = current_draft_recipe_identity(profile, "R1")
    assert draft_id is not None
    rec_id = {
        "source": "draft",
        "path": "recipes/drafts/r1.yaml",
        "recipe_sha256": draft_id["draft_recipe_sha256"],
        "case_revision": 1,
        "reserved_external_id": None,
    }
    negctl_root = tmp_path / "runs" / "negctl"
    probe = _make_counted_probe(
        case_id="R1",
        artifact_sha256=SHA_A,
        binding_sha256=binding_v1,
        recipe_identity=rec_id,
        base_steps=draft_steps,
    )
    write_probe(negctl_root, probe, generated_at="2026-11-26T10:00:00Z")

    trial_runs = [
        {
            "draft_id": "R1",
            "case_id": "R1",
            "run_id": f"trial-{idx}",
            "status": "passed",
            "review_status": "approved" if idx == 2 else "pending",
            "ended_at": f"2026-11-26T10:0{idx}:00Z",
            "exec_via": "recipe",
            "revision": 1,
            "draft_recipe_identity": dict(draft_id),
        }
        for idx in range(3)
    ]

    service = PromotionService(
        negctl_root=negctl_root,
        promotion_runs=lambda **kw: list(trial_runs) if kw.get("kind") == "case" else [],
        find_run_dir=lambda run_id: tmp_path / run_id,
        detail=lambda run_id: {},
        verification_manifest=lambda run_dir, run_id: run_dir / "m.json",
        operation_manifest=lambda run_dir, run_id: run_dir / "m.json",
        execution=PromotionExecution(
            review_verification=lambda *a, **k: {},
            review_operation=lambda *a, **k: {},
            load_cases=lambda p: (yaml.safe_load(Path(p).read_text(encoding="utf-8")) or {}).get("cases") or [],
            normalize_evidence=lambda runs: list(runs),
            publish_recipe=lambda *a, **k: None,
            validate_recipe=lambda *a, **k: None,
        ),
    )

    # 1. Exact artifact + exact binding match -> gate READY
    decision_ok = service.case_promotion_decision(case_id="R1", profile=profile)
    assert decision_ok["ready"] is True, decision_ok
    assert decision_ok["gaps"] == []

    # 2. Mutate symbol file (changes binding_sha256) -> stale & promote_case_draft raises!
    sym_path.write_bytes(sym_path.read_bytes() + b"\n# binding updated\n")
    decision_binding_stale = service.case_promotion_decision(case_id="R1", profile=profile)
    assert decision_binding_stale["ready"] is False
    assert "discrimination_stale" in decision_binding_stale["gaps"]
    with pytest.raises(ContractError, match="discrimination_stale"):
        service.promote_case_draft(profile=profile, case_id="R1")

    # 3. Restore symbol file, then swap manifest to new IPA with SAME version 1.0.0 (1) -> stale!
    _write_symbol_file(profile, artifact_sha256=SHA_A)
    _write_manifest_v2(tmp_path, profile.app, build_id="bld-bbbbbbbbbbbb", artifact_sha256=SHA_B)
    decision_build_stale = service.case_promotion_decision(case_id="R1", profile=profile)
    assert decision_build_stale["ready"] is False
    assert "discrimination_stale" in decision_build_stale["gaps"]
    with pytest.raises(ContractError, match="discrimination_stale"):
        service.promote_case_draft(profile=profile, case_id="R1")

    # 4. Restore manifest to SHA_A -> promote_case_draft succeeds cleanly!
    _write_manifest_v2(tmp_path, profile.app, build_id="bld-aaaaaaaaaaaa", artifact_sha256=SHA_A)
    promoted = service.promote_case_draft(profile=profile, case_id="R1")
    assert promoted["case"]["status"] == "active"


def test_operation_promotion_and_ai_draft_gate_enforce_symbol_and_artifact_freshness(tmp_path):
    profile = _create_app_with_build_label(tmp_path)
    _write_manifest_v2(tmp_path, profile.app, build_id="bld-aaaaaaaaaaaa", artifact_sha256=SHA_A)
    sym_path, binding_v1 = _write_symbol_file(profile, artifact_sha256=SHA_A)

    op_def = {
        "id": "OP1",
        "name": "open_prefs_negative",
        "status": "draft",
        "revision": 1,
        "summary": "Open prefs with witness",
        "params": [{"name": "should_open", "type": "bool", "default": True}],
        "steps": [{"action": "tap_ax", "symbol": "ctrl.home.preferences"}],
        "witness": {
            "kind": "ocr_markers",
            "expect_stage": "prefs_open",
            "markers": ["饮食偏好"],
            "negative_control": {
                "flip_arg": "should_open",
                "flip_to": False,
                "expect_stage": "home_still_visible",
            },
        },
    }
    op_id = operation_identity(profile.app, op_def)
    negctl_root = tmp_path / "runs" / "negctl"
    op_probe = {
        "schema": PROBE_SCHEMA,
        "app": profile.app,
        "case_id": "R9",
        "control_id": "neg-op-1",
        "build_label": BUILD_LABEL,
        "target_green": True,
        "discriminator_flipped": True,
        "teeth_at": "operation",
        "witness_as_predicted": True,
        "negative_verdict": "failed_on_expected_step",
        "counted": True,
        "perturbation": {"kind": "flip_witness", "target": "open_prefs_negative"},
        "operation_identity": op_id,
        "build_identity": {
            "profile_build_label": BUILD_LABEL,
            "build_identity_match": "match",
            "build_id": "bld-aaaaaaaaaaaa",
            "artifact_sha256": SHA_A,
            "artifact_content_digest": SHA_A,
        },
        "binding_sha256": binding_v1,
    }
    write_probe(negctl_root, op_probe, generated_at="2026-11-26T10:00:00Z")

    op_runs = _approved_runs("OP1", count=3)
    service = PromotionService(
        negctl_root=negctl_root,
        promotion_runs=lambda **kw: list(op_runs),
        find_run_dir=lambda run_id: tmp_path / run_id,
        detail=lambda run_id: {},
        verification_manifest=lambda run_dir, run_id: run_dir / "m.json",
        operation_manifest=lambda run_dir, run_id: run_dir / "m.json",
        execution=PromotionExecution(
            review_verification=lambda *a, **k: {},
            review_operation=lambda *a, **k: {},
            load_cases=lambda p: [],
            normalize_evidence=lambda runs: list(runs),
            publish_recipe=lambda *a, **k: None,
            validate_recipe=lambda *a, **k: None,
        ),
    )

    # Matching digest + binding -> operation promotion ready AND ai_draft._draft_gate has no stale gap
    dec_ok = service.operation_promotion_decision(draft_id="OP1", profile=profile, op_def=op_def)
    assert dec_ok["ready"] is True, dec_ok
    draft_gate_ok = _draft_gate(profile.app, op_def, root=tmp_path, required_passes=3)
    assert "discrimination_stale" not in draft_gate_ok["gaps"]

    # Edit symbol file -> both operation_promotion_decision and _draft_gate report discrimination_stale
    sym_path.write_bytes(sym_path.read_bytes() + b"\n# modified\n")
    dec_stale = service.operation_promotion_decision(draft_id="OP1", profile=profile, op_def=op_def)
    assert dec_stale["ready"] is False
    assert "discrimination_stale" in dec_stale["gaps"]
    draft_gate_stale = _draft_gate(profile.app, op_def, root=tmp_path, required_passes=3)
    assert "discrimination_stale" in draft_gate_stale["gaps"]


def test_callers_cases_exceptions_health_and_http_enforce_d6_freshness(tmp_path, monkeypatch):
    from autophone.device import lock as lock_module

    monkeypatch.setattr(lock_module, "default_screen_lock_root", lambda: tmp_path / "locks")
    profile = _create_app_with_build_label(tmp_path)
    _write_manifest_v2(tmp_path, profile.app, build_id="bld-aaaaaaaaaaaa", artifact_sha256=SHA_A)
    sym_path, binding_v1 = _write_symbol_file(profile, artifact_sha256=SHA_A)

    live_steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences"},
        {"action": "assert_present", "queries": ["饮食偏好"]},
    ]
    live_path = profile.recipes_dir() / "r1.yaml"
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_text(yaml.safe_dump({"id": "R1", "steps": live_steps}, sort_keys=False), encoding="utf-8")

    cases_doc = {
        "meta": {"title": "Food Ordering", "app": profile.app, "schema_version": 2, "total": 1, "next_seq": 2},
        "cases": [
            {
                "id": "R1",
                "status": "active",
                "revision": 1,
                "module": "home",
                "item": "Open preferences",
                "automation": "ui_ready",
                "steps": ["Tap preferences"],
                "expects": ["Preferences visible"],
            }
        ],
    }
    profile.cases_path().write_text(yaml.safe_dump(cases_doc, sort_keys=False), encoding="utf-8")

    negctl_root = tmp_path / "runs" / "negctl"
    probe = _make_counted_probe(
        case_id="R1",
        artifact_sha256=SHA_A,
        binding_sha256=binding_v1,
        base_steps=live_steps,
    )
    write_probe(negctl_root, probe, generated_at="2026-11-26T10:00:00Z")

    # (a) ExceptionView: fresh -> no drift exception; mutated binding -> drift stale exception!
    fake_catalog = SimpleNamespace(case_verdicts=lambda app: {})
    view = ExceptionView(profile, catalog=fake_catalog, negctl_root=negctl_root)
    assert [e for e in view.current()["exceptions"] if e["kind"] == "drift"] == []

    # (b) Health: fresh -> negctl_proven_cases == 1
    run_root = tmp_path / "runs" / "platform"
    run_root.mkdir(parents=True, exist_ok=True)
    health_ok = app_health(
        run_root,
        profile.app,
        cases_path=profile.cases_path(),
        operations_path=profile.operations_path(),
        negctl_root=negctl_root,
        build_label=BUILD_LABEL,
        recipes_dir=profile.recipes_dir(),
    )
    assert health_ok["result_quality"]["discriminator_coverage"]["negctl_proven_cases"] == 1

    # (c) Mutate symbol file -> ExceptionView surfaces drift:stale AND health drops proven count to 0!
    sym_path.write_bytes(sym_path.read_bytes() + b"\n# changed\n")
    drift_items = [e for e in view.current()["exceptions"] if e["kind"] == "drift"]
    assert len(drift_items) == 1
    assert drift_items[0]["signature"] == "stale"

    health_stale = app_health(
        run_root,
        profile.app,
        cases_path=profile.cases_path(),
        operations_path=profile.operations_path(),
        negctl_root=negctl_root,
        build_label=BUILD_LABEL,
        recipes_dir=profile.recipes_dir(),
    )
    assert health_stale["result_quality"]["discriminator_coverage"]["negctl_proven_cases"] == 0

    # (d) CaseLifecycleApplication.activate_many blocks on discrimination_stale for legacy/stale proof
    draft_cases = {
        "meta": {"title": "Food Ordering", "app": profile.app, "schema_version": 2, "total": 1, "next_seq": 2},
        "cases": [
            {
                "id": "R1",
                "status": "draft",
                "revision": 1,
                "module": "home",
                "item": "Open preferences",
                "automation": "needs_fixture",
                "steps": ["Tap preferences"],
                "expects": ["Preferences visible"],
            }
        ],
    }
    profile.cases_path().write_text(yaml.safe_dump(draft_cases, sort_keys=False), encoding="utf-8")
    lifecycle = CaseLifecycleApplication(
        profile,
        negctl_root=negctl_root,
        activation_policy=lambda *a, **k: None,
        discrimination_required=lambda case: True,
    )
    res_blocked = lifecycle.activate_many(["R1"])
    assert res_blocked["activated_count"] == 0
    assert any("discrimination_stale" in item["error"] for item in res_blocked["failed"])

    # Restore symbol file & manifest matching probe -> activate_many succeeds
    _write_symbol_file(profile, artifact_sha256=SHA_A)
    res_ok = lifecycle.activate_many(["R1"])
    assert res_ok["activated_count"] == 1

    # (e) HTTP GET /api/apps/{app}/cases uses composition wiring and reflects stale after build swap
    from autophone.adapters.http.app import create_app

    http_app = create_app()
    deps = http_app.state.dependencies
    deps.STATE_ROOT = tmp_path
    deps.load_app_profile = lambda app_name: load_app_profile(app_name, root=tmp_path, state_root=tmp_path)
    # Mark R1 active + ui_ready so HTTP cases() computes discrimination_status
    active_doc = yaml.safe_load(profile.cases_path().read_text(encoding="utf-8"))
    active_doc["cases"][0]["status"] = "active"
    active_doc["cases"][0]["automation"] = "ui_ready"
    profile.cases_path().write_text(yaml.safe_dump(active_doc, sort_keys=False), encoding="utf-8")

    resp_fresh = deps.cases(profile.app)
    assert resp_fresh["cases"][0]["discrimination"]["state"] == "proven"

    # Rebuild same version 1.0.0 (1) with new SHA_B -> HTTP endpoint immediately flips to stale!
    _write_manifest_v2(tmp_path, profile.app, build_id="bld-bbbbbbbbbbbb", artifact_sha256=SHA_B)
    resp_stale = deps.cases(profile.app)
    assert resp_stale["cases"][0]["discrimination"]["state"] == "stale"


def test_negative_control_runner_preserves_rn_candidate_and_binding_sha256(tmp_path):
    profile = _create_app_with_build_label(tmp_path)
    cases_doc = {
        "meta": {"title": "Food Ordering", "app": profile.app, "schema_version": 2, "total": 1, "next_seq": 2},
        "cases": [
            {
                "id": "R1",
                "status": "active",
                "revision": 1,
                "module": "home",
                "item": "Open preferences",
                "automation": "ui_ready",
                "steps": ["Tap preferences"],
                "expects": ["Preferences visible"],
            }
        ],
    }
    profile.cases_path().write_text(yaml.safe_dump(cases_doc, sort_keys=False), encoding="utf-8")
    steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences"},
        {"action": "assert_present", "queries": ["饮食偏好"]},
    ]
    live_path = profile.recipes_dir() / "r1.yaml"
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_text(yaml.safe_dump({"id": "R1", "steps": steps}, sort_keys=False), encoding="utf-8")
    control = {
        "id": "neg-nav",
        "case_id": "R1",
        "hypothesis": "skipping navigation fails target assertion",
        "perturbation": {"kind": "skip_step", "step": {"action": "tap_ax"}},
        "expect": {"step": {"action": "assert_present"}, "fail_kinds": ["assertion_failed"]},
    }
    neg_decl_path = profile.profile_dir / "discrimination" / "negative_controls.yaml"
    neg_decl_path.parent.mkdir(parents=True, exist_ok=True)
    neg_decl_path.write_text(
        yaml.safe_dump({"schema": "autophone.negative-controls/1", "controls": [control]}, sort_keys=False),
        encoding="utf-8",
    )

    resolved = resolve_negative_control(steps, control)
    rec_id = {
        "source": "live",
        "path": "recipes/r1.yaml",
        "recipe_sha256": hashlib.sha256(live_path.read_bytes()).hexdigest(),
        "case_revision": 1,
        "reserved_external_id": None,
    }
    cand = {
        "target_fingerprint": "sha256:" + "5" * 64,
        "bundle_id": BUNDLE_ID,
        "base_build_id": "bld-111111111111",
        "base_ipa_sha256": SHA_BASE,
        "main_jsbundle_sha256": SHA_JS_1,
        "assets_tree_sha256": SHA_ASSETS,
    }
    neg_record = {
        "control_id": control["id"],
        "declaration_sha256": declaration_sha256(control),
        **resolved.record(control, steps),
        "base_recipe_identity": dict(rec_id),
        "base_steps_digest": recipe_steps_digest(steps),
        "perturbed_steps_digest": recipe_steps_digest(resolved.steps),
        "applied": True,
        "failed_step_index": resolved.expected_index_after,
        "fail_kind": "assertion_failed",
    }

    class FakeControl:
        def resolve_device_rig(self, device: str) -> str:
            return "rig-a"

        def submit_assignment(self, plan: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
            spec = plan["run_spec"]
            is_neg = "negative_control" in spec
            run_result = _make_run_result(
                run_id=spec["run_id"],
                case_id="R1",
                case_status="failed" if is_neg else "passed",
                artifact_sha256=SHA_BASE,
                recipe_identity=rec_id,
                binding_sha256=BINDING_1,
                negative_control=neg_record if is_neg else None,
            )
            return {
                "assignment_id": f"asgn-{spec['run_id']}",
                "status": "failed" if is_neg else "passed",
                "bound_rig_id": "rig-a",
                "attempt": {"rig_id": "rig-a"},
                "result": {"run_result": run_result, "candidate_identity": cand},
            }

    run_counter = iter(["run-pos-1", "run-neg-1"])
    runner = NegativeControlRunner(
        control=FakeControl(),
        negctl_root=tmp_path / "runs" / "negctl",
        profile_loader=lambda app: profile,
        run_id_factory=lambda app: next(run_counter),
        now=lambda: "2026-11-26T10:30:00Z",
    )
    out = runner.run(NegativeControlRequest(app=profile.app, case_id="R1", control_id="neg-nav", device="rig-a"))
    probe = out["probe"]
    assert probe["counted"] is True
    assert probe["binding_sha256"] == BINDING_1
    assert probe["build_identity"]["artifact_content_digest"] == _rn_expected_digest(SHA_BASE, SHA_JS_1, SHA_ASSETS)
    assert "target_fingerprint" not in probe["build_identity"]
    assert "target_fingerprint" not in probe["build_identity"]["rn_candidate_digests"]


def test_m2_m5_canonical_digest_parity_and_run_result_schema_compat():
    from autophone.assets.app_symbols import (
        canonical_artifact_content_digest as m2_canonical_digest,
        canonical_rn_candidate_content_digest as m2_rn_digest,
    )

    ipa_identity = {
        "kind": "ipa_build",
        "bundle_id": BUNDLE_ID,
        "short_version": "1.0.0",
        "build_version": "1",
        "build_id": "bld-aaaaaaaaaaaa",
        "artifact_sha256": SHA_A,
    }
    rn_identity = {
        "kind": "rn_candidate",
        "bundle_id": BUNDLE_ID,
        "short_version": "1.0.0",
        "build_version": "1",
        "base_ipa_sha256": SHA_BASE,
        "main_jsbundle_sha256": SHA_JS_1,
        "assets_tree_sha256": SHA_ASSETS,
    }
    assert canonical_artifact_content_digest(ipa_identity) == m2_canonical_digest(ipa_identity)
    assert canonical_artifact_content_digest(rn_identity) == m2_canonical_digest(rn_identity)
    assert rn_candidate_content_digest(SHA_BASE, SHA_JS_1, SHA_ASSETS) == m2_rn_digest(
        base_ipa_sha256=SHA_BASE,
        main_jsbundle_sha256=SHA_JS_1,
        assets_tree_sha256=SHA_ASSETS,
    )


def test_promotion_decision_and_transitive_operation_symbol_binding(tmp_path):
    profile = _create_app_with_build_label(tmp_path)
    _write_manifest_v2(tmp_path, profile.app, build_id="bld-aaaaaaaaaaaa", artifact_sha256=SHA_A)
    sym_path, binding_v1 = _write_symbol_file(profile, artifact_sha256=SHA_A)

    # Operation uses a symbol step; Case R1 invokes that Operation (transitive symbol dependency).
    ops_doc = {
        "meta": {"title": "Ops", "app": profile.app, "schema_version": 1, "total": 1, "next_seq": 2},
        "operations": [
            {
                "id": "OP1",
                "name": "open_prefs",
                "status": "active",
                "revision": 1,
                "summary": "Navigate via symbol",
                "steps": [{"action": "tap_ax", "symbol": "ctrl.home.preferences"}],
            }
        ],
    }
    profile.operations_path().write_text(yaml.safe_dump(ops_doc, sort_keys=False), encoding="utf-8")
    case_steps = [
        {"operation": "open_prefs", "args": {}},
        {"action": "assert_present", "queries": ["饮食偏好"]},
    ]
    draft_path = profile.recipes_dir() / "drafts" / "r1.yaml"
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(yaml.safe_dump({"id": "R1", "steps": case_steps}, sort_keys=False), encoding="utf-8")

    disc_ctx = current_discrimination_context(profile, case_id="R1", recipe_source="draft")
    assert disc_ctx == {
        "current_artifact_content_digest": SHA_A,
        "current_binding_sha256": binding_v1,
        "uses_symbol_binding": True,
    }

    # Verification draft promotion_decision enforces exact artifact digest + optional binding_sha256
    negctl_root = tmp_path / "runs" / "negctl"
    verif_probe = {
        "schema": PROBE_SCHEMA,
        "app": profile.app,
        "case_id": "V1",
        "draft_id": "verif-1",
        "control_id": "neg-verif",
        "build_label": BUILD_LABEL,
        "target_green": True,
        "discriminator_flipped": True,
        "teeth_at": "recipe",
        "negative_verdict": "failed_on_expected_step",
        "counted": True,
        "build_identity": {
            "profile_build_label": BUILD_LABEL,
            "build_identity_match": "match",
            "build_id": "bld-aaaaaaaaaaaa",
            "artifact_sha256": SHA_A,
            "artifact_content_digest": SHA_A,
        },
        "binding_sha256": binding_v1,
    }
    write_probe(negctl_root, verif_probe, generated_at="2026-11-26T10:00:00Z")
    service = PromotionService(
        negctl_root=negctl_root,
        promotion_runs=lambda **kw: _approved_runs("verif-1"),
        find_run_dir=lambda run_id: tmp_path / run_id,
        detail=lambda run_id: {},
        verification_manifest=lambda run_dir, run_id: run_dir / "m.json",
        operation_manifest=lambda run_dir, run_id: run_dir / "m.json",
        execution=PromotionExecution(
            review_verification=lambda *a, **k: {},
            review_operation=lambda *a, **k: {},
            load_cases=lambda p: [],
            normalize_evidence=lambda runs: list(runs),
            publish_recipe=lambda *a, **k: None,
            validate_recipe=lambda *a, **k: None,
        ),
    )

    ok_dec = service.promotion_decision(
        draft_id="verif-1", profile=profile, uses_symbol_binding=True,
    )
    assert ok_dec["ready"] is True, ok_dec

    # Mutate symbol file -> promotion_decision flips to discrimination_stale
    sym_path.write_bytes(sym_path.read_bytes() + b"\n# changed\n")
    stale_dec = service.promotion_decision(
        draft_id="verif-1", profile=profile, uses_symbol_binding=True,
    )
    assert stale_dec["ready"] is False
    assert "discrimination_stale" in stale_dec["gaps"]

    # Explicit override digest mismatch -> stale even for non-symbol verification draft
    digest_stale = service.promotion_decision(
        draft_id="verif-1",
        profile=profile,
        current_artifact_content_digest=SHA_B,
        uses_symbol_binding=False,
    )
    assert digest_stale["ready"] is False
    assert "discrimination_stale" in digest_stale["gaps"]


def test_case_lifecycle_activate_many_rejects_legacy_probe_and_supports_custom_stubs(tmp_path):
    profile = _create_app_with_build_label(tmp_path)
    cases_doc = {
        "meta": {"title": "Food Ordering", "app": profile.app, "schema_version": 2, "total": 1, "next_seq": 2},
        "cases": [
            {
                "id": "R1",
                "status": "draft",
                "revision": 1,
                "module": "home",
                "item": "Legacy probe check",
                "automation": "needs_fixture",
                "steps": ["Step 1"],
                "expects": ["Expect 1"],
            }
        ],
    }
    profile.cases_path().write_text(yaml.safe_dump(cases_doc, sort_keys=False), encoding="utf-8")

    negctl_root = tmp_path / "runs" / "negctl"
    legacy_dir = negctl_root / BUILD_LABEL
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "artifact.json").write_text(
        json.dumps(
            {
                "build_label": BUILD_LABEL,
                "probes": [
                    {
                        "case_id": "R1",
                        "target_green": True,
                        "discriminator_flipped": True,
                        "teeth_at": "recipe",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    # Real default proof_loader + gap_evaluator reject legacy probe lacking exact artifact digest
    lifecycle = CaseLifecycleApplication(
        profile,
        negctl_root=negctl_root,
        activation_policy=lambda *a, **k: None,
        discrimination_required=lambda case: True,
    )
    res_legacy = lifecycle.activate_many(["R1"])
    assert res_legacy["activated_count"] == 0
    assert any("discrimination_stale" in item["error"] for item in res_legacy["failed"])

    # Narrow custom stub callbacks still work via _call_with_supported_kwargs
    seen: list[str] = []
    stub_lifecycle = CaseLifecycleApplication(
        profile,
        negctl_root=negctl_root,
        activation_policy=lambda *a, **k: None,
        discrimination_required=lambda case: True,
        proof_loader=lambda root, case_id=None: seen.append(str(case_id)) or {"ok": True},
        gap_evaluator=lambda proof, current_build_label=None: [],
    )
    res_stub = stub_lifecycle.activate_many(["R1"])
    assert res_stub["activated_count"] == 1
    assert seen == ["R1"]

