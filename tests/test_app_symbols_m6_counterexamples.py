"""Unified M6 offline E2E integration suite: all 6 counterexamples (§4.3),
`autophone app case lint --build-id` (D5), `bind_negative_control` symbol support,
and `examples/food-ordering` experimental asset verification.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from autophone.adapters.cli import case as cli_case
from autophone.application.cases import CaseRecipeApplication
from autophone.assets import app_symbols
from autophone.assets.apps import AppProfile, create_app_profile, load_app_profile
from autophone.assets.build_identity import subject_build_key
from autophone.assets.negative_control import (
    declaration_sha256,
    judge_negative_control,
    load_negative_controls,
    resolve_negative_control,
)
from autophone.assets.promotion import evaluate_promotion_gate
from autophone.assets.screens import load_screen_asset, match_screen, screen_by_id
from autophone.contracts import ContractError
from autophone.device.ax_target import AxDump, Node
from autophone.execution import effects
from autophone.execution.engine import actions as actions_mod
from autophone.execution.engine import recipe as recipe_mod
from autophone.execution.engine import runner as runner_mod
from autophone.execution.engine.actions import CaseContext, StepFailure
from autophone.execution.negative_control import (
    NEGATIVE_CONTROL_RECORD_KEY,
    NEGATIVE_CONTROL_STEPS_KEY,
    attach_negative_control_records,
    bind_negative_control,
)
from autophone.observation.frida.api_oracle import flat_fixture_vars

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_ROOT = REPO_ROOT / "examples" / "food-ordering"

BUNDLE_ID = "com.autophone.foodorderingdemo.v2"
BUILD_LABEL = "1.0.0-bld-14282f675363"
BUILD_ID_V1 = "bld-14282f675363"
SHA_V1 = "14282f675363a9d74361c9b5d24dcde461648df2a13cf9aa4488d0aad4f68d58"
BUILD_ID_V2 = "bld-222222222222"
SHA_V2 = "222222222222a9d74361c9b5d24dcde461648df2a13cf9aa4488d0aad4f68d58"


# ── Helpers & Fakes ───────────────────────────────────────────────────────────


def _ocr_frame(*texts: str) -> dict[str, Any]:
    return {
        "image_w": 430,
        "image_h": 932,
        "boxes": [
            {"text": t, "confidence": 0.99, "x": 100, "y": 200 + i * 40, "w": 160, "h": 30}
            for i, t in enumerate(texts)
        ],
    }


def _ax_node(
    *,
    identifier: str = "home.ai_prompt.preferences",
    role: str = "button",
    label: str = "调偏好",
    enabled: bool = True,
    native_x: int = 360,
    native_y: int = 900,
    nx: float = 0.5,
    ny: float = 0.5,
) -> Node:
    return Node(
        label=label,
        role=role,
        traits=[role],
        frame=(100.0, 280.0, 44.0, 44.0),
        enabled=enabled,
        value=None,
        identifier=identifier,
        path="0/1",
        native_x=native_x,
        native_y=native_y,
        nx=nx,
        ny=ny,
    )


def _ax_dump(nodes: list[Node], *, ax_backend: str = "frida_inproc") -> AxDump:
    return AxDump(
        app=BUNDLE_ID,
        scale=3.0,
        w=430.0,
        h=932.0,
        view_controller="HomeVC",
        nodes=nodes,
        ax_backend=ax_backend,
    )


class _FakeAxProvider:
    def __init__(self, dumps: list[AxDump] | AxDump, *, ax_backend: str = "frida_inproc") -> None:
        self._dumps = list(dumps) if isinstance(dumps, list) else [dumps]
        self.ax_backend = ax_backend
        self.calls = 0

    def refresh(self) -> AxDump:
        idx = min(self.calls, len(self._dumps) - 1)
        self.calls += 1
        return self._dumps[idx]


def _patch_ctx_io(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ocr_sequence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Patch CaseContext UDS & OCR so tests run deterministically with exact tap counts."""
    taps: list[dict[str, Any]] = []
    ocr_iter = {"idx": 0}

    def fake_request(self: CaseContext, payload: dict[str, Any], **_kw: Any) -> tuple[dict[str, Any], None]:
        if payload.get("t") == "tap":
            taps.append(dict(payload))
        return {"ok": True}, None

    def fake_ocr(self: CaseContext, *, force: bool = False) -> dict[str, Any]:
        i = min(ocr_iter["idx"], len(ocr_sequence) - 1)
        ocr_iter["idx"] += 1
        return ocr_sequence[i]

    monkeypatch.setattr(CaseContext, "_request_dispatch", fake_request)
    monkeypatch.setattr(CaseContext, "_settle", lambda self, **_kw: None)
    monkeypatch.setattr(CaseContext, "ocr_now", fake_ocr)
    monkeypatch.setattr(actions_mod.time, "sleep", lambda _s: None)
    return taps


def _write_pkg_manifest(
    root: Path,
    app: str,
    builds: list[tuple[str, str, str]],
    *,
    default_slot: str = "001",
) -> None:
    """Write a valid package-manifest.v2.json with (slot, build_id, sha256) entries."""
    pkg_dir = root / "packages" / app
    pkg_dir.mkdir(parents=True, exist_ok=True)
    slots = []
    build_rows = []
    for slot, bid, sha in builds:
        slots.append({
            "slot": slot,
            "short_version": "1.0.0",
            "candidate_build_id": bid,
            "created_at": "2026-11-26T10:00:00Z",
        })
        build_rows.append({
            "build_id": bid,
            "slot": slot,
            "label": None,
            "bundle_id": BUNDLE_ID,
            "build_version": "1",
            "source_revision": {"kind": "unknown", "state": "unknown"},
            "artifact": {"file": f"{bid}.ipa", "sha256": sha, "size": 1024},
            "artifact_availability": "present",
            "release_like": True,
            "release_evidence": "embedded_js_bundle:Payload/App.app/main.jsbundle",
            "legacy_slot": None,
            "added_at": "2026-11-26T10:00:00Z",
        })
    manifest = {
        "schema_version": "v2",
        "app": app,
        "bundle_id": BUNDLE_ID,
        "default_slot": default_slot,
        "version_slots": slots,
        "builds": build_rows,
    }
    (pkg_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _write_symbol_artifact(
    profile: AppProfile,
    *,
    build_id: str,
    artifact_sha256: str,
    controls: dict[str, Any],
    filename: str | None = None,
) -> Path:
    sym_dir = profile.profile_dir / "symbols" / "artifacts"
    sym_dir.mkdir(parents=True, exist_ok=True)
    path = sym_dir / (filename or f"{artifact_sha256[:12]}.yaml")
    doc = {
        "schema": "autophone.app-symbols/1",
        "app": profile.app,
        "artifact_identity": {
            "kind": "ipa_build",
            "bundle_id": BUNDLE_ID,
            "short_version": "1.0.0",
            "build_version": "1",
            "build_id": build_id,
            "artifact_sha256": artifact_sha256,
        },
        "controls": controls,
    }
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _setup_workspace_app(
    tmp_path: Path,
    *,
    app: str = "food_ordering",
    allowed_effects: list[str] | None = None,
    trial_data_isolation: str = "isolated",
    controls_v1: dict[str, Any] | None = None,
    write_screens: bool = True,
    write_package_store: bool = True,
    extra_builds: list[tuple[str, str, str]] | None = None,
) -> tuple[AppProfile, dict[str, Any]]:
    profile = create_app_profile(app, BUNDLE_ID, "Food Ordering", root=tmp_path)
    profile_path = profile.profile_dir / "profile.yaml"
    raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    raw["build_label"] = BUILD_LABEL
    raw["trial_data_isolation"] = trial_data_isolation
    raw["allowed_effects"] = list(allowed_effects or [])
    raw["capabilities"] = {"ax": {"enabled": True, "backend": "frida_inproc"}}
    profile_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    profile = load_app_profile(app, root=tmp_path, state_root=tmp_path)

    if write_package_store:
        builds = [("001", BUILD_ID_V1, SHA_V1), *(extra_builds or [])]
        _write_pkg_manifest(tmp_path, app, builds, default_slot="001")

    screen_doc = yaml.safe_load(
        (EXAMPLE_ROOT / "apps" / "food_ordering" / "screens" / f"{BUILD_LABEL}.yaml").read_text(encoding="utf-8")
    )
    screen_doc["meta"]["app"] = app
    if write_screens:
        sp = profile.screens_path()
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(yaml.safe_dump(screen_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")

    default_controls = {
        "ctrl.home.preferences": {
            "kind": "button",
            "scope": {"screen_id": "S01_home", "region": [0.05, 0.15, 0.95, 0.90]},
            "locator": {
                "kind": "ax_identifier",
                "identifier": "home.ai_prompt.preferences",
                "role": "button",
                "enabled_only": True,
                "require_unique": True,
            },
            "effects": {"verbs": [], "completeness": "exhaustive"},
        },
        "ctrl.checkout.submit": {
            "kind": "button",
            "scope": {"screen_id": "S01_home", "region": [0.05, 0.15, 0.95, 0.90]},
            "locator": {
                "kind": "ax_identifier",
                "identifier": "checkout.submit",
                "role": "button",
                "enabled_only": True,
                "require_unique": True,
            },
            "effects": {"verbs": ["place_order", "pay"], "completeness": "exhaustive"},
        },
        "ctrl.checkout.lower": {
            "kind": "button",
            "scope": {"screen_id": "S01_home", "region": [0.05, 0.15, 0.95, 0.90]},
            "locator": {
                "kind": "ax_identifier",
                "identifier": "checkout.lower",
                "role": "button",
                "enabled_only": True,
                "require_unique": True,
            },
            "effects": {"verbs": ["pay"], "completeness": "lower_bound"},
        },
        "ctrl.l1.unknown": {
            "kind": "button",
            "scope": {"screen_id": "S01_home", "region": [0.05, 0.15, 0.95, 0.90]},
            "locator": {
                "kind": "ax_identifier",
                "identifier": "l1.unknown",
                "role": None,
                "enabled_only": True,
                "require_unique": True,
            },
            "effects": {"verbs": [], "completeness": "unknown"},
        },
    }
    _write_symbol_artifact(
        profile,
        build_id=BUILD_ID_V1,
        artifact_sha256=SHA_V1,
        controls=controls_v1 if controls_v1 is not None else default_controls,
    )
    return profile, screen_doc


def _write_cases(profile: AppProfile, cases: list[dict[str, Any]]) -> None:
    doc = {
        "meta": {
            "title": profile.display_name,
            "app": profile.app,
            "schema_version": 2,
            "total": len(cases),
            "next_seq": len(cases) + 1,
        },
        "cases": cases,
    }
    profile.cases_path().write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8",
    )


def _write_recipe(profile: AppProfile, case_id: str, steps: list[dict[str, Any]], *, draft: bool = False) -> Path:
    base = profile.recipes_dir() / "drafts" if draft else profile.recipes_dir()
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{case_id.lower()}.yaml"
    path.write_text(
        yaml.safe_dump({"id": case_id.upper(), "steps": steps}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


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


def _make_run_result_for_judge(
    *,
    run_id: str,
    case_id: str,
    outcome: runner_mod.CaseOutcome,
    recipe_identity: dict[str, Any],
    build_label: str = BUILD_LABEL,
    build_id: str = BUILD_ID_V1,
    artifact_sha256: str = SHA_V1,
) -> dict[str, Any]:
    status_map = {"pass": "passed", "fail": "failed", "error": "error", "skipped": "skipped"}
    case_status = status_map[outcome.status]
    rec_id = dict(recipe_identity)
    if outcome.binding_sha256 is not None:
        rec_id["binding_sha256"] = outcome.binding_sha256
    case_row: dict[str, Any] = {
        "id": case_id,
        "status": case_status,
        "revision": 1,
        "recipe_identity": rec_id,
    }
    if outcome.binding_sha256 is not None:
        case_row["binding_sha256"] = outcome.binding_sha256
    if getattr(outcome, "negative_control", None) is not None:
        case_row["negative_control"] = dict(outcome.negative_control)
    return {
        "run_id": run_id,
        "status": case_status,
        "package": {
            "profile_build_label": build_label,
            "build_identity_match": "match",
            "comparison_granularity": "build_marker",
            "comparison_build_id": build_id,
            "registry_build_ref": {
                "app": "food_ordering",
                "slot": "001",
                "short_version": "1.0.0",
                "build_version": "1",
                "build_id": build_id,
                "artifact_sha256": artifact_sha256,
            },
            "device_build_observation": {
                "bundle_id": BUNDLE_ID,
                "short_version": "1.0.0",
                "build_version": "1",
                "observed_at": "2026-11-26T10:00:00Z",
            },
        },
        "cases": [case_row],
    }


# ── Counterexample #1: Copy Changed Without ID Change ─────────────────────────


def test_ce1_copy_changed_without_id_change_separates_navigation_from_copy_assertion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.3 CE #1: button label changes '调偏好' -> '口味设置' while identifier stays
    'home.ai_prompt.preferences' -> symbol navigation PASSES; independent old-copy
    assertion case FAILS; handwritten copy-based recipe also FAILS."""
    profile = load_app_profile("food_ordering", root=EXAMPLE_ROOT, state_root=tmp_path)
    screen_doc = load_screen_asset(profile.screens_path())
    binding = app_symbols.load_symbol_binding(profile, artifact_content_digest=SHA_V1)

    symbol_steps = recipe_mod.load_recipe(profile.recipes_dir() / "drafts" / "r3.yaml")
    manual_steps = recipe_mod.load_recipe(profile.recipes_dir() / "drafts" / "r2.yaml")
    old_copy_steps = [{"action": "assert_present", "queries": ["调偏好"], "wait_s": 0}]

    # App v2 changed visible copy to "口味设置", keeping identifier "home.ai_prompt.preferences".
    home_ocr_v2 = _ocr_frame("今晚，好好吃一顿", "立即下单", "口味设置")
    prefs_ocr_v2 = _ocr_frame("饮食偏好", "保存偏好", "微辣")
    ax_v2 = _FakeAxProvider(_ax_dump([_ax_node(label="口味设置")]))

    # 1) Symbol-bound navigation case (R3) PASSES!
    taps = _patch_ctx_io(monkeypatch, ocr_sequence=[home_ocr_v2, prefs_ocr_v2])
    with recipe_mod.recipe_scope("food_ordering", root=EXAMPLE_ROOT, overrides={"R3": symbol_steps}):
        out_sym = runner_mod.run_case(
            {"id": "R3", "module": "首页", "item": "符号导航调偏好"},
            tmp_path / "run_sym",
            str(tmp_path / "absent.sock"),
            30.0,
            ax=ax_v2,
            ax_backend="frida_inproc",
            screen_asset_doc=screen_doc,
            symbol_binding=binding,
            allowed_effects=set(),
            effect_mode="active",
        )
    assert out_sym.status == "pass", out_sym.detail
    assert out_sym.binding_sha256 == binding.binding_sha256
    assert len(taps) == 1

    # 2) Independent old-copy assertion case (R4) FAILS accurately!
    taps_copy = _patch_ctx_io(monkeypatch, ocr_sequence=[home_ocr_v2])
    with recipe_mod.recipe_scope("food_ordering", root=EXAMPLE_ROOT, overrides={"R4": old_copy_steps}):
        out_copy = runner_mod.run_case(
            {"id": "R4", "module": "首页", "item": "旧文案断言"},
            tmp_path / "run_copy",
            str(tmp_path / "absent.sock"),
            30.0,
            ax=ax_v2,
            screen_asset_doc=screen_doc,
            allowed_effects=set(),
            effect_mode="active",
        )
    assert out_copy.status == "fail"
    assert out_copy.fail_kind == "assertion_failed"
    assert len(taps_copy) == 0

    # 3) Handwritten copy-based recipe (R2) breaks on the renamed label (locator_missing, 0 taps).
    taps_manual = _patch_ctx_io(monkeypatch, ocr_sequence=[home_ocr_v2])
    with recipe_mod.recipe_scope("food_ordering", root=EXAMPLE_ROOT, overrides={"R2": manual_steps}):
        out_manual = runner_mod.run_case(
            {"id": "R2", "module": "首页", "item": "手写文案导航"},
            tmp_path / "run_manual",
            str(tmp_path / "absent.sock"),
            30.0,
            ax=_FakeAxProvider(_ax_dump([_ax_node(label="口味设置")])),
            screen_asset_doc=screen_doc,
            allowed_effects=set(),
            effect_mode="active",
        )
    assert out_manual.status == "fail"
    assert out_manual.fail_kind == "locator_missing"
    assert len(taps_manual) == 0


# ── Counterexample #2: Duplicate ID Ambiguity Zero-Touch ──────────────────────


@pytest.mark.parametrize(
    ("node_count", "expected_status", "expected_fail_kind", "expected_taps"),
    [
        (0, "fail", "locator_missing", 0),
        (1, "pass", "", 1),
        (2, "fail", "ambiguous_match", 0),
    ],
)
def test_ce2_duplicate_identifier_role_dispatches_0_1_0_taps_no_fallback_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    node_count: int,
    expected_status: str,
    expected_fail_kind: str,
    expected_taps: int,
) -> None:
    """§4.3 CE #2: same identifier + role returns 0 / 1 / 2 nodes -> UDS taps strictly
    0 / 1 / 0 (locator_missing / pass / ambiguous_match), zero fallback_xy escape."""
    profile = load_app_profile("food_ordering", root=EXAMPLE_ROOT, state_root=tmp_path)
    screen_doc = load_screen_asset(profile.screens_path())
    binding = app_symbols.load_symbol_binding(profile, artifact_content_digest=SHA_V1)
    symbol_steps = recipe_mod.load_recipe(profile.recipes_dir() / "drafts" / "r3.yaml")

    nodes = [
        _ax_node(native_x=300 + i * 40, native_y=900, nx=0.4 + i * 0.1, ny=0.5)
        for i in range(node_count)
    ]
    taps = _patch_ctx_io(
        monkeypatch,
        ocr_sequence=[
            _ocr_frame("今晚，好好吃一顿", "立即下单", "调偏好"),
            _ocr_frame("饮食偏好", "保存偏好"),
        ],
    )
    with recipe_mod.recipe_scope("food_ordering", root=EXAMPLE_ROOT, overrides={"R3": symbol_steps}):
        outcome = runner_mod.run_case(
            {"id": "R3", "module": "首页", "item": "唯一命中测试"},
            tmp_path / f"run_{node_count}",
            str(tmp_path / "absent.sock"),
            30.0,
            ax=_FakeAxProvider(_ax_dump(nodes)),
            ax_backend="frida_inproc",
            screen_asset_doc=screen_doc,
            symbol_binding=binding,
            allowed_effects=set(),
            effect_mode="active",
        )
    assert outcome.status == expected_status, outcome.detail
    assert outcome.fail_kind == expected_fail_kind
    assert len(taps) == expected_taps

    # Direct require_unique=True with fallback_xy must NEVER fall back on 0 or 2 nodes.
    if node_count != 1:
        taps_fb = _patch_ctx_io(
            monkeypatch, ocr_sequence=[_ocr_frame("今晚，好好吃一顿", "立即下单")]
        )
        ctx = CaseContext(
            "R3",
            tmp_path / f"direct_{node_count}",
            sock_path=str(tmp_path / "absent.sock"),
            ax=_FakeAxProvider(_ax_dump(nodes)),
            ax_backend="frida_inproc",
            screen_asset_doc=screen_doc,
        )
        with pytest.raises(StepFailure) as exc_info:
            ctx.tap_ax(
                identifier="home.ai_prompt.preferences",
                role="button",
                region=(0.05, 0.15, 0.95, 0.90),
                screen_id="S01_home",
                require_unique=True,
                fallback_xy=(215, 420),
            )
        assert exc_info.value.result.kind == expected_fail_kind
        assert len(taps_fb) == 0


# ── Counterexample #3: Same Version Rebuilt IPA, Legacy Probe & Binding Drift ─


def test_ce3_same_version_rebuilt_ipa_legacy_probe_and_binding_drift_stale(
    tmp_path: Path,
) -> None:
    """§4.3 CE #3:
    (a) Same version 1.0.0 (1) rebuilt IPA (different SHA) -> old unique_visibility
        not reused, promotion gate blocks with discrimination_stale;
    (b) Legacy probe lacking artifact_sha256 -> discrimination_stale;
    (c) Modified symbol YAML (binding_sha256 changed) on same IPA -> old visibility
        not reused and promotion gate blocks with discrimination_stale."""
    profile, _ = _setup_workspace_app(
        tmp_path,
        extra_builds=[("002", BUILD_ID_V2, SHA_V2)],
    )
    binding_v1 = app_symbols.load_symbol_binding(profile, artifact_content_digest=SHA_V1)

    # Record a confirmed vm_observation unique_visibility claim for v1.
    app_symbols.record_unique_visibility_claim(
        profile,
        symbol_id="ctrl.home.preferences",
        artifact_content_digest=SHA_V1,
        artifact_sha256=SHA_V1,
        binding_sha256=binding_v1.binding_sha256,
        screen_id="S01_home",
        identity_state="confirmed",
        ax_backend="frida_inproc",
        evidence_refs=["runs/run_01/build-identity.json", "runs/run_01/shots/R3_01.ax.json"],
    )
    claims_doc = app_symbols.load_symbol_claims(profile)

    # Same artifact + binding + screen + backend -> reusable.
    assert app_symbols.has_reusable_unique_visibility(
        claims_doc,
        symbol_id="ctrl.home.preferences",
        artifact_content_digest=SHA_V1,
        binding_sha256=binding_v1.binding_sha256,
        screen_id="S01_home",
        ax_backend="frida_inproc",
    ) is True

    # Rebuilt IPA v2 with identical version 1.0.0 (1) but SHA_V2 -> NOT reusable!
    assert app_symbols.has_reusable_unique_visibility(
        claims_doc,
        symbol_id="ctrl.home.preferences",
        artifact_content_digest=SHA_V2,
        binding_sha256=binding_v1.binding_sha256,
        screen_id="S01_home",
        ax_backend="frida_inproc",
    ) is False

    # Cross-backend claim -> NOT reusable!
    assert app_symbols.has_reusable_unique_visibility(
        claims_doc,
        symbol_id="ctrl.home.preferences",
        artifact_content_digest=SHA_V1,
        binding_sha256=binding_v1.binding_sha256,
        screen_id="S01_home",
        ax_backend="guest_axruntime",
    ) is False

    # Construct a fresh counted probe on v1 (1.0.0 (1), SHA_V1, binding_v1).
    base_steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "open prefs"},
        {"action": "assert_present", "queries": ["饮食偏好", "保存偏好"], "wait_s": 0},
    ]
    control = {
        "id": "neg.prefs.skip",
        "case_id": "R3",
        "hypothesis": "skipping navigation fails target assertion",
        "perturbation": {"kind": "skip_step", "step": {"action": "tap_ax"}},
        "expect": {"step": {"action": "assert_present"}, "fail_kinds": ["assertion_failed"]},
    }
    resolved = resolve_negative_control(base_steps, control)
    recipe_id = {
        "source": "draft",
        "path": "recipes/drafts/r3.yaml",
        "recipe_sha256": "e" * 64,
        "case_revision": 1,
        "reserved_external_id": None,
    }
    pos_out = runner_mod.CaseOutcome(
        id="R3",
        row=1,
        module="首页",
        item="符号导航",
        status="pass",
        exec_via="recipe",
        binding_sha256=binding_v1.binding_sha256,
    )
    neg_out = runner_mod.CaseOutcome(
        id="R3",
        row=1,
        module="首页",
        item="符号导航",
        status="fail",
        fail_kind="assertion_failed",
        exec_via="recipe",
        binding_sha256=binding_v1.binding_sha256,
        recipe_steps_digest=recipe_mod.recipe_steps_digest(resolved.steps),
        failed_recipe_step=resolved.expected_index_after,
    )
    neg_out.negative_control = {
        "control_id": control["id"],
        "declaration_sha256": declaration_sha256(control),
        **resolved.record(control, base_steps),
        "base_recipe_identity": {**recipe_id, "binding_sha256": binding_v1.binding_sha256},
        "base_steps_digest": recipe_mod.recipe_steps_digest(base_steps),
        "perturbed_steps_digest": recipe_mod.recipe_steps_digest(resolved.steps),
        "applied": True,
        "failed_step_index": resolved.expected_index_after,
        "fail_kind": "assertion_failed",
        "binding_sha256": binding_v1.binding_sha256,
    }
    pos_res = _make_run_result_for_judge(
        run_id="run-pos-v1", case_id="R3", outcome=pos_out, recipe_identity=recipe_id,
    )
    neg_res = _make_run_result_for_judge(
        run_id="run-neg-v1", case_id="R3", outcome=neg_out, recipe_identity=recipe_id,
    )
    probe_v1 = judge_negative_control(
        app="food_ordering",
        control=control,
        build_label=BUILD_LABEL,
        positive=pos_res,
        negative=neg_res,
        positive_rig_id="rig-a",
        negative_rig_id="rig-a",
    )
    assert probe_v1["counted"] is True
    assert probe_v1["binding_sha256"] == binding_v1.binding_sha256

    sbk = subject_build_key(BUNDLE_ID, "1.0.0", "1")
    # Fresh on v1 -> ready!
    gate_v1 = evaluate_promotion_gate(
        _approved_runs("R3"),
        draft_id="R3",
        discrimination=probe_v1,
        require_discrimination=True,
        current_build_label=BUILD_LABEL,
        current_build_identity_ref={"subject_build_key": sbk, "artifact_sha256": SHA_V1},
        current_binding_sha256=binding_v1.binding_sha256,
        uses_symbol_binding=True,
    )
    assert gate_v1["ready"] is True, gate_v1

    # (a) Same version 1.0.0 (1) & same subject_build_key & same build_label, rebuilt IPA SHA_V2 -> stale!
    gate_v2 = evaluate_promotion_gate(
        _approved_runs("R3"),
        draft_id="R3",
        discrimination=probe_v1,
        require_discrimination=True,
        current_build_label=BUILD_LABEL,
        current_build_identity_ref={"subject_build_key": sbk, "artifact_sha256": SHA_V2},
        current_binding_sha256=binding_v1.binding_sha256,
        uses_symbol_binding=True,
    )
    assert gate_v2["ready"] is False
    assert "discrimination_stale" in gate_v2["gaps"]

    # (b) Legacy probe lacking artifact_sha256 / artifact_content_digest -> stale!
    legacy_probe = {
        "build_label": BUILD_LABEL,
        "case_id": "R3",
        "target_green": True,
        "discriminator_flipped": True,
        "teeth_at": "recipe",
        "build_identity_ref": {"subject_build_key": sbk},
    }
    gate_legacy = evaluate_promotion_gate(
        _approved_runs("R3"),
        draft_id="R3",
        discrimination=legacy_probe,
        require_discrimination=True,
        current_build_label=BUILD_LABEL,
        current_build_identity_ref={"subject_build_key": sbk},
    )
    assert gate_legacy["ready"] is False
    assert "discrimination_stale" in gate_legacy["gaps"]

    # (c) Modify symbol YAML on same IPA SHA_V1 -> binding_sha256 changes -> old visibility & probe stale!
    sym_file = profile.profile_dir / "symbols" / "artifacts" / f"{SHA_V1[:12]}.yaml"
    sym_file.write_text(sym_file.read_text(encoding="utf-8") + "\n# revised region note\n", encoding="utf-8")
    binding_v1_mod = app_symbols.load_symbol_binding(profile, artifact_content_digest=SHA_V1)
    assert binding_v1_mod.binding_sha256 != binding_v1.binding_sha256

    assert app_symbols.has_reusable_unique_visibility(
        claims_doc,
        symbol_id="ctrl.home.preferences",
        artifact_content_digest=SHA_V1,
        binding_sha256=binding_v1_mod.binding_sha256,
        screen_id="S01_home",
        ax_backend="frida_inproc",
    ) is False

    gate_mod = evaluate_promotion_gate(
        _approved_runs("R3"),
        draft_id="R3",
        discrimination=probe_v1,
        require_discrimination=True,
        current_build_label=BUILD_LABEL,
        current_artifact_sha256=SHA_V1,
        current_binding_sha256=binding_v1_mod.binding_sha256,
        uses_symbol_binding=True,
    )
    assert gate_mod["ready"] is False
    assert "discrimination_stale" in gate_mod["gaps"]


# ── Counterexample #4: bind_negative_control + skip_step Toothless vs Counted ─


def test_ce4_bind_negative_control_skip_step_on_symbol_recipe_toothless_vs_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.3 CE #4 + M3/M5 negative_control symbol compatibility:
    - Weak shared-header assertion passes even after skip_step removes symbol navigation ->
      judge_negative_control + evaluate_promotion_gate blocks with discrimination_toothless;
    - Discriminating assertion fails on assert_present after skip_step ->
      binding_sha256 is preserved across skip_step and promotion gate passes cleanly;
    - Multi-symbol recipe keeps remaining symbol steps expanded & validated."""
    profile, screen_doc = _setup_workspace_app(tmp_path)
    binding = app_symbols.load_symbol_binding(profile, artifact_content_digest=SHA_V1)

    _write_cases(
        profile,
        [
            {
                "id": "R1",
                "revision": 1,
                "status": "active",
                "automation": "ui_ready",
                "case_type": "功能测试",
                "test_point": "弱断言负对照",
                "owner": "qa",
                "module": "首页",
                "item": "跨页共有文案弱断言",
                "login_precondition": "any",
                "steps": ["点击调偏好", "检查跨页共有文案"],
                "expects": ["页面展示微辣"],
            },
            {
                "id": "R2",
                "revision": 1,
                "status": "active",
                "automation": "ui_ready",
                "case_type": "功能测试",
                "test_point": "强断言负对照",
                "owner": "qa",
                "module": "首页",
                "item": "偏好页目标断言",
                "login_precondition": "any",
                "steps": ["点击调偏好", "检查饮食偏好标题"],
                "expects": ["页面展示饮食偏好与保存偏好"],
            },
            {
                "id": "R3",
                "revision": 1,
                "status": "active",
                "automation": "ui_ready",
                "case_type": "功能测试",
                "test_point": "双符号步骤负对照",
                "owner": "qa",
                "module": "首页",
                "item": "跳过首个符号步骤仍展开剩余符号步骤",
                "login_precondition": "any",
                "steps": ["两次符号点击", "检查饮食偏好"],
                "expects": ["页面展示饮食偏好与保存偏好"],
            },
        ],
    )

    weak_steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "打开饮食偏好页"},
        {"action": "assert_present", "queries": ["微辣"], "wait_s": 0},
    ]
    strong_steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "打开饮食偏好页"},
        {"action": "assert_present", "queries": ["饮食偏好", "保存偏好"], "wait_s": 0},
    ]
    two_symbol_steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "第一步"},
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "第二步"},
        {"action": "assert_present", "queries": ["饮食偏好", "保存偏好"], "wait_s": 0},
    ]
    weak_path = _write_recipe(profile, "R1", weak_steps)
    strong_path = _write_recipe(profile, "R2", strong_steps)
    _write_recipe(profile, "R3", two_symbol_steps)

    neg_dir = profile.profile_dir / "discrimination"
    neg_dir.mkdir(parents=True, exist_ok=True)
    neg_controls = [
        {
            "id": "neg.weak.skip_nav",
            "case_id": "R1",
            "hypothesis": "skipping navigation reveals whether shared-copy assertion discriminates",
            "perturbation": {"kind": "skip_step", "step": {"action": "tap_ax"}},
            "expect": {"step": {"action": "assert_present"}, "fail_kinds": ["assertion_failed"]},
        },
        {
            "id": "neg.strong.skip_nav",
            "case_id": "R2",
            "hypothesis": "skipping navigation must fail target preferences assertion",
            "perturbation": {"kind": "skip_step", "step": {"action": "tap_ax"}},
            "expect": {"step": {"action": "assert_present"}, "fail_kinds": ["assertion_failed"]},
        },
        {
            "id": "neg.two_sym.skip_first",
            "case_id": "R3",
            "hypothesis": "skipping first symbol step still expands remaining symbol step",
            "perturbation": {"kind": "skip_step", "step": {"action": "tap_ax", "occurrence": 1}},
            "expect": {"step": {"action": "assert_present"}, "fail_kinds": ["assertion_failed"]},
        },
    ]
    (neg_dir / "negative_controls.yaml").write_text(
        yaml.safe_dump({"schema": "autophone.negative-controls/1", "controls": neg_controls}, allow_unicode=True),
        encoding="utf-8",
    )

    home_frame = _ocr_frame("今晚，好好吃一顿", "立即下单", "微辣", "调偏好")
    prefs_frame = _ocr_frame("饮食偏好", "保存偏好", "微辣")
    ax_home = _FakeAxProvider(_ax_dump([_ax_node()]))

    # ── Part A: Weak shared-header assertion -> discrimination_toothless ──
    weak_ctrl = neg_controls[0]
    weak_case = {"id": "R1", "module": "首页", "item": "弱断言"}
    bound_weak_cases = bind_negative_control(
        profile,
        [weak_case],
        {
            "case_id": "R1",
            "control_id": weak_ctrl["id"],
            "declaration_sha256": declaration_sha256(weak_ctrl),
        },
        symbol_binding=binding,
        screen_asset_doc=screen_doc,
    )
    bound_weak = bound_weak_cases[0]
    assert bound_weak[NEGATIVE_CONTROL_RECORD_KEY]["binding_sha256"] == binding.binding_sha256
    assert bound_weak[NEGATIVE_CONTROL_RECORD_KEY]["base_recipe_identity"]["binding_sha256"] == binding.binding_sha256
    assert bound_weak["_symbol_binding_sha256"] == binding.binding_sha256
    assert len(bound_weak[NEGATIVE_CONTROL_STEPS_KEY]) == 1

    # Positive run passes on prefs_frame.
    _patch_ctx_io(monkeypatch, ocr_sequence=[home_frame, prefs_frame])
    with recipe_mod.recipe_scope("food_ordering", root=tmp_path, overrides={"R1": weak_steps}):
        pos_weak_out = runner_mod.run_case(
            weak_case,
            tmp_path / "run_pos_weak",
            str(tmp_path / "absent.sock"),
            30.0,
            ax=ax_home,
            ax_backend="frida_inproc",
            screen_asset_doc=screen_doc,
            symbol_binding=binding,
            allowed_effects=set(),
            effect_mode="active",
        )
    assert pos_weak_out.status == "pass"
    assert pos_weak_out.binding_sha256 == binding.binding_sha256

    # Negative run stays on home_frame, yet weak shared-header assertion STILL passes!
    _patch_ctx_io(monkeypatch, ocr_sequence=[home_frame])
    with recipe_mod.recipe_scope(
        "food_ordering", root=tmp_path, overrides={"R1": bound_weak[NEGATIVE_CONTROL_STEPS_KEY]},
    ):
        neg_weak_out = runner_mod.run_case(
            bound_weak,
            tmp_path / "run_neg_weak",
            str(tmp_path / "absent.sock"),
            30.0,
            ax=ax_home,
            ax_backend="frida_inproc",
            screen_asset_doc=screen_doc,
            symbol_binding=binding,
            allowed_effects=set(),
            effect_mode="active",
        )
    attach_negative_control_records([neg_weak_out], bound_weak_cases)
    assert neg_weak_out.status == "pass"
    assert neg_weak_out.binding_sha256 == binding.binding_sha256
    assert neg_weak_out.negative_control["applied"] is True
    assert neg_weak_out.negative_control["binding_sha256"] == binding.binding_sha256

    weak_recipe_id = {
        "source": "live",
        "path": "recipes/r1.yaml",
        "recipe_sha256": hashlib.sha256(weak_path.read_bytes()).hexdigest(),
        "case_revision": 1,
        "reserved_external_id": None,
    }
    weak_probe = judge_negative_control(
        app="food_ordering",
        control=weak_ctrl,
        build_label=BUILD_LABEL,
        positive=_make_run_result_for_judge(
            run_id="pos-weak", case_id="R1", outcome=pos_weak_out, recipe_identity=weak_recipe_id,
        ),
        negative=_make_run_result_for_judge(
            run_id="neg-weak", case_id="R1", outcome=neg_weak_out, recipe_identity=weak_recipe_id,
        ),
        positive_rig_id="rig-a",
        negative_rig_id="rig-a",
    )
    assert weak_probe["negative_verdict"] == "passed"
    assert weak_probe["discriminator_flipped"] is False
    assert weak_probe["teeth_at"] == "none"
    assert weak_probe["counted"] is False
    assert weak_probe["binding_sha256"] == binding.binding_sha256

    weak_gate = evaluate_promotion_gate(
        _approved_runs("R1"),
        draft_id="R1",
        discrimination=weak_probe,
        require_discrimination=True,
        current_build_label=BUILD_LABEL,
        current_artifact_sha256=SHA_V1,
        current_binding_sha256=binding.binding_sha256,
        uses_symbol_binding=True,
    )
    assert weak_gate["ready"] is False
    assert "discrimination_toothless" in weak_gate["gaps"]

    # ── Part B: Strong target-page assertion -> counted & promotion ready ──
    strong_ctrl = neg_controls[1]
    strong_case = {"id": "R2", "module": "首页", "item": "强断言"}
    bound_strong_cases = bind_negative_control(
        profile,
        [strong_case],
        {
            "case_id": "R2",
            "control_id": strong_ctrl["id"],
            "declaration_sha256": declaration_sha256(strong_ctrl),
        },
        symbol_binding=binding,
        screen_asset_doc=screen_doc,
    )
    bound_strong = bound_strong_cases[0]
    assert bound_strong[NEGATIVE_CONTROL_RECORD_KEY]["binding_sha256"] == binding.binding_sha256

    _patch_ctx_io(monkeypatch, ocr_sequence=[home_frame, prefs_frame])
    with recipe_mod.recipe_scope("food_ordering", root=tmp_path, overrides={"R2": strong_steps}):
        pos_strong_out = runner_mod.run_case(
            strong_case,
            tmp_path / "run_pos_strong",
            str(tmp_path / "absent.sock"),
            30.0,
            ax=ax_home,
            ax_backend="frida_inproc",
            screen_asset_doc=screen_doc,
            symbol_binding=binding,
            allowed_effects=set(),
            effect_mode="active",
        )
    assert pos_strong_out.status == "pass"

    _patch_ctx_io(monkeypatch, ocr_sequence=[home_frame])
    with recipe_mod.recipe_scope(
        "food_ordering", root=tmp_path, overrides={"R2": bound_strong[NEGATIVE_CONTROL_STEPS_KEY]},
    ):
        neg_strong_out = runner_mod.run_case(
            bound_strong,
            tmp_path / "run_neg_strong",
            str(tmp_path / "absent.sock"),
            30.0,
            ax=ax_home,
            ax_backend="frida_inproc",
            screen_asset_doc=screen_doc,
            symbol_binding=binding,
            allowed_effects=set(),
            effect_mode="active",
        )
    attach_negative_control_records([neg_strong_out], bound_strong_cases)
    assert neg_strong_out.status == "fail"
    assert neg_strong_out.fail_kind == "assertion_failed"
    assert neg_strong_out.negative_control["applied"] is True
    assert neg_strong_out.negative_control["failed_step_index"] == 0
    assert neg_strong_out.negative_control["binding_sha256"] == binding.binding_sha256

    strong_recipe_id = {
        "source": "live",
        "path": "recipes/r2.yaml",
        "recipe_sha256": hashlib.sha256(strong_path.read_bytes()).hexdigest(),
        "case_revision": 1,
        "reserved_external_id": None,
    }
    strong_probe = judge_negative_control(
        app="food_ordering",
        control=strong_ctrl,
        build_label=BUILD_LABEL,
        positive=_make_run_result_for_judge(
            run_id="pos-strong", case_id="R2", outcome=pos_strong_out, recipe_identity=strong_recipe_id,
        ),
        negative=_make_run_result_for_judge(
            run_id="neg-strong", case_id="R2", outcome=neg_strong_out, recipe_identity=strong_recipe_id,
        ),
        positive_rig_id="rig-a",
        negative_rig_id="rig-a",
    )
    assert strong_probe["counted"] is True
    assert strong_probe["negative_verdict"] == "failed_on_expected_step"
    assert strong_probe["binding_sha256"] == binding.binding_sha256

    strong_gate = evaluate_promotion_gate(
        _approved_runs("R2"),
        draft_id="R2",
        discrimination=strong_probe,
        require_discrimination=True,
        current_build_label=BUILD_LABEL,
        current_artifact_sha256=SHA_V1,
        current_binding_sha256=binding.binding_sha256,
        uses_symbol_binding=True,
    )
    assert strong_gate["ready"] is True, strong_gate

    # ── Part C: Multi-symbol recipe keeps remaining symbol step expanded & validated ──
    two_ctrl = neg_controls[2]
    bound_two = bind_negative_control(
        profile,
        [{"id": "R3", "module": "首页", "item": "双符号"}],
        {
            "case_id": "R3",
            "control_id": two_ctrl["id"],
            "declaration_sha256": declaration_sha256(two_ctrl),
        },
        symbol_binding=binding,
        screen_asset_doc=screen_doc,
    )[0]
    remaining_steps = bound_two[NEGATIVE_CONTROL_STEPS_KEY]
    assert len(remaining_steps) == 2
    assert "symbol" not in remaining_steps[0]
    assert remaining_steps[0]["identifier"] == "home.ai_prompt.preferences"
    assert remaining_steps[0]["_symbol_binding"]["binding_sha256"] == binding.binding_sha256
    assert bound_two[NEGATIVE_CONTROL_RECORD_KEY]["binding_sha256"] == binding.binding_sha256

    # Missing symbol_binding for symbol-bound negative control fails closed with ContractError.
    with pytest.raises(ContractError, match="symbol binding invalid"):
        bind_negative_control(
            profile,
            [{"id": "R2", "module": "首页", "item": "强断言"}],
            {
                "case_id": "R2",
                "control_id": strong_ctrl["id"],
                "declaration_sha256": declaration_sha256(strong_ctrl),
            },
            symbol_binding=None,
            screen_asset_doc=screen_doc,
        )


# ── Counterexample #5: Multi-Effect Wildcard Rejection & Completeness Gate ────


def test_ce5_multi_effect_wildcard_rejection_and_completeness_isolation_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.3 CE #5:
    - ['place_order', 'pay'] rejected with ['pay'] or 'irreversible' wildcard;
    - lower_bound / unknown hard-blocked in active (case lint & runtime) and blocked
      in draft unless BOTH confirmed trial effects AND trial_data_isolation='isolated' hold."""
    # 1) Multi-effect ["place_order", "pay"] rejected with ["pay"] or ["irreversible"].
    for allowed, expected_code in (
        (["pay"], "invalid_live_recipe"),
        (["irreversible"], effects.REASON_MULTI_EFFECT_WILDCARD_REJECTED),
    ):
        ws = tmp_path / f"multi_{'_'.join(allowed)}"
        profile, screen_doc = _setup_workspace_app(ws, allowed_effects=allowed)
        binding = app_symbols.load_symbol_binding(profile, artifact_content_digest=SHA_V1)
        _write_cases(profile, [{
            "id": "R1", "revision": 1, "status": "active", "automation": "ui_ready",
            "case_type": "功能测试", "test_point": "下单并支付", "owner": "qa",
            "module": "结算", "item": "多副作用符号动作", "login_precondition": "any",
            "steps": ["点击提交订单并支付"], "expects": ["支付成功"],
        }])
        steps = [
            {"action": "tap_ax", "symbol": "ctrl.checkout.submit", "label": "提交并支付"},
            {"action": "assert_present", "queries": ["支付成功"], "wait_s": 0},
        ]
        _write_recipe(profile, "R1", steps)

        lint = CaseRecipeApplication(profile).lint()
        assert lint["error_count"] == 1
        assert lint["findings"][0]["blocker"]["code"] == expected_code

        taps = _patch_ctx_io(monkeypatch, ocr_sequence=[_ocr_frame("今晚，好好吃一顿", "立即下单"), _ocr_frame("支付成功")])
        with recipe_mod.recipe_scope("food_ordering", root=ws, overrides={"R1": steps}):
            out = runner_mod.run_case(
                {"id": "R1", "module": "结算", "item": "多副作用"},
                ws / "run_block",
                str(ws / "absent.sock"),
                30.0,
                ax=_FakeAxProvider(_ax_dump([_ax_node(identifier="checkout.submit")])),
                screen_asset_doc=screen_doc,
                symbol_binding=binding,
                allowed_effects=set(allowed),
                effect_mode="active",
            )
        assert out.status == "fail"
        assert len(taps) == 0

    # Explicit ["place_order", "pay"] passes case lint and runtime!
    ws_ok = tmp_path / "multi_ok"
    profile_ok, screen_ok = _setup_workspace_app(ws_ok, allowed_effects=["place_order", "pay"])
    binding_ok = app_symbols.load_symbol_binding(profile_ok, artifact_content_digest=SHA_V1)
    _write_cases(profile_ok, [{
        "id": "R1", "revision": 1, "status": "active", "automation": "ui_ready",
        "case_type": "功能测试", "test_point": "下单并支付", "owner": "qa",
        "module": "结算", "item": "多副作用全授权", "login_precondition": "any",
        "steps": ["点击提交并支付"], "expects": ["支付成功"],
    }])
    steps_ok = [
        {"action": "tap_ax", "symbol": "ctrl.checkout.submit", "label": "提交并支付"},
        {"action": "assert_present", "queries": ["支付成功"], "wait_s": 0},
    ]
    _write_recipe(profile_ok, "R1", steps_ok)
    assert CaseRecipeApplication(profile_ok).lint()["error_count"] == 0

    taps_ok = _patch_ctx_io(monkeypatch, ocr_sequence=[_ocr_frame("今晚，好好吃一顿", "立即下单"), _ocr_frame("支付成功")])
    with recipe_mod.recipe_scope("food_ordering", root=ws_ok, overrides={"R1": steps_ok}):
        out_ok = runner_mod.run_case(
            {"id": "R1", "module": "结算", "item": "多副作用全授权"},
            ws_ok / "run_ok",
            str(ws_ok / "absent.sock"),
            30.0,
            ax=_FakeAxProvider(_ax_dump([_ax_node(identifier="checkout.submit")])),
            screen_asset_doc=screen_ok,
            symbol_binding=binding_ok,
            allowed_effects={"place_order", "pay"},
            effect_mode="active",
        )
    assert out_ok.status == "pass"
    assert len(taps_ok) == 1

    # 2) lower_bound / unknown hard-blocked in active (lint & runtime), gated in draft by both flags!
    for sym_id, ax_ident in (("ctrl.checkout.lower", "checkout.lower"), ("ctrl.l1.unknown", "l1.unknown")):
        ws_c = tmp_path / f"comp_{sym_id.replace('.', '_')}"
        prof_c, scr_c = _setup_workspace_app(
            ws_c, allowed_effects=["pay"], trial_data_isolation="unisolated",
        )
        bind_c = app_symbols.load_symbol_binding(prof_c, artifact_content_digest=SHA_V1)
        _write_cases(prof_c, [{
            "id": "R1", "revision": 1, "status": "active", "automation": "ui_ready",
            "case_type": "功能测试", "test_point": "完备度检查", "owner": "qa",
            "module": "结算", "item": sym_id, "login_precondition": "any",
            "steps": ["执行符号动作"], "expects": ["完成"],
        }])
        steps_c = [
            {"action": "tap_ax", "symbol": sym_id, "label": "测试"},
            {"action": "assert_present", "queries": ["完成"], "wait_s": 0},
        ]
        _write_recipe(prof_c, "R1", steps_c)

        # Active case lint hard-blocks with effect_completeness_insufficient!
        lint_c = CaseRecipeApplication(prof_c).lint()
        assert lint_c["error_count"] == 1
        assert lint_c["findings"][0]["blocker"]["code"] == effects.REASON_EFFECT_COMPLETENESS_INSUFFICIENT

        # Runtime active hard-blocks (0 taps) even if draft trial flags are True!
        taps_act = _patch_ctx_io(monkeypatch, ocr_sequence=[_ocr_frame("今晚，好好吃一顿", "立即下单"), _ocr_frame("完成")])
        with recipe_mod.recipe_scope("food_ordering", root=ws_c, overrides={"R1": steps_c}):
            out_act = runner_mod.run_case(
                {"id": "R1", "module": "结算", "item": sym_id},
                ws_c / "run_active",
                str(ws_c / "absent.sock"),
                30.0,
                ax=_FakeAxProvider(_ax_dump([_ax_node(identifier=ax_ident)])),
                screen_asset_doc=scr_c,
                symbol_binding=bind_c,
                allowed_effects={"pay"},
                effect_mode="active",
                trial_draft_recipes=True,
                confirm_trial_effects=True,
                trial_data_isolation="isolated",
            )
        assert out_act.status == "fail"
        assert out_act.fail_kind == effects.REASON_EFFECT_COMPLETENESS_INSUFFICIENT
        assert len(taps_act) == 0

        # Draft missing confirm_trial_effects -> blocked (0 taps).
        taps_no_confirm = _patch_ctx_io(monkeypatch, ocr_sequence=[_ocr_frame("今晚，好好吃一顿", "立即下单"), _ocr_frame("完成")])
        with recipe_mod.recipe_scope("food_ordering", root=ws_c, overrides={"R1": steps_c}):
            out_nc = runner_mod.run_case(
                {"id": "R1", "module": "结算", "item": sym_id},
                ws_c / "run_no_confirm",
                str(ws_c / "absent.sock"),
                30.0,
                ax=_FakeAxProvider(_ax_dump([_ax_node(identifier=ax_ident)])),
                screen_asset_doc=scr_c,
                symbol_binding=bind_c,
                allowed_effects={"pay"},
                effect_mode="draft",
                trial_draft_recipes=True,
                confirm_trial_effects=False,
                trial_data_isolation="isolated",
            )
        assert out_nc.status == "fail"
        assert out_nc.fail_kind == effects.REASON_TRIAL_EFFECTS_NOT_CONFIRMED
        assert len(taps_no_confirm) == 0

        # Draft missing isolated data isolation -> blocked (0 taps).
        taps_uniso = _patch_ctx_io(monkeypatch, ocr_sequence=[_ocr_frame("今晚，好好吃一顿", "立即下单"), _ocr_frame("完成")])
        with recipe_mod.recipe_scope("food_ordering", root=ws_c, overrides={"R1": steps_c}):
            out_uniso = runner_mod.run_case(
                {"id": "R1", "module": "结算", "item": sym_id},
                ws_c / "run_uniso",
                str(ws_c / "absent.sock"),
                30.0,
                ax=_FakeAxProvider(_ax_dump([_ax_node(identifier=ax_ident)])),
                screen_asset_doc=scr_c,
                symbol_binding=bind_c,
                allowed_effects={"pay"},
                effect_mode="draft",
                trial_draft_recipes=True,
                confirm_trial_effects=True,
                trial_data_isolation="unisolated",
            )
        assert out_uniso.status == "fail"
        assert out_uniso.fail_kind == effects.REASON_TRIAL_DATA_UNISOLATED
        assert len(taps_uniso) == 0

        # Draft with BOTH confirmed trial effects AND isolated data -> passes (1 tap)!
        taps_pass = _patch_ctx_io(monkeypatch, ocr_sequence=[_ocr_frame("今晚，好好吃一顿", "立即下单"), _ocr_frame("完成")])
        with recipe_mod.recipe_scope("food_ordering", root=ws_c, overrides={"R1": steps_c}):
            out_pass = runner_mod.run_case(
                {"id": "R1", "module": "结算", "item": sym_id},
                ws_c / "run_draft_ok",
                str(ws_c / "absent.sock"),
                30.0,
                ax=_FakeAxProvider(_ax_dump([_ax_node(identifier=ax_ident)])),
                screen_asset_doc=scr_c,
                symbol_binding=bind_c,
                allowed_effects={"pay"},
                effect_mode="draft",
                trial_draft_recipes=True,
                confirm_trial_effects=True,
                trial_data_isolation="isolated",
            )
        assert out_pass.status == "pass", out_pass.detail
        assert len(taps_pass) == 1


# ── Counterexample #6: Overlay Fail-Closed & Wrong Business Entity ────────────


def test_ce6_overlay_skips_auto_dismiss_and_wrong_business_entity_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.3 CE #6:
    (a) Top overlay blocking button -> symbol step fails closed WITHOUT calling
        _dismiss_blocking_overlay_once (0 UDS taps);
    (b) Structurally valid target screen (S02_preferences) displaying the wrong
        business entity vs independent expected-entity fixture data -> Case fails."""
    profile = load_app_profile("food_ordering", root=EXAMPLE_ROOT, state_root=tmp_path)
    screen_doc = load_screen_asset(profile.screens_path())
    binding = app_symbols.load_symbol_binding(profile, artifact_content_digest=SHA_V1)
    symbol_steps = recipe_mod.load_recipe(profile.recipes_dir() / "drafts" / "r3.yaml")

    # (a) Top promo overlay obscures the button; symbol step must fail closed BEFORE
    #     AX refresh with overlay_occluded (0 taps, 0 dismiss calls), AND even if a
    #     symbol tap succeeds and a subsequent non-symbol assert_present fails while
    #     an overlay is present, overlay auto-dismiss stays suppressed across the whole case!
    dismiss_calls: list[str] = []
    with monkeypatch.context() as mp:
        mp.setattr(
            recipe_mod,
            "_dismiss_blocking_overlay_once",
            lambda _ctx: (dismiss_calls.append("called") or "promo_popup") if not dismiss_calls else None,
        )
        overlay_ocr = _ocr_frame("今晚，好好吃一顿", "立即下单", "活动弹窗", "关闭")
        taps = _patch_ctx_io(monkeypatch, ocr_sequence=[overlay_ocr])
        # Even if underlying AX node is enabled (e.g. transparent modal), pre-tap
        # blocking_overlays check fails closed with overlay_occluded and 0 UDS taps.
        ax_overlay = _FakeAxProvider(_ax_dump([_ax_node(enabled=True)]))
        with recipe_mod.recipe_scope("food_ordering", root=EXAMPLE_ROOT, overrides={"R3": symbol_steps}):
            out_overlay = runner_mod.run_case(
                {"id": "R3", "module": "首页", "item": "弹窗遮挡不绕过"},
                tmp_path / "run_overlay",
                str(tmp_path / "absent.sock"),
                30.0,
                ax=ax_overlay,
                ax_backend="frida_inproc",
                screen_asset_doc=screen_doc,
                symbol_binding=binding,
                allowed_effects=set(),
                effect_mode="active",
            )
        assert out_overlay.status == "fail"
        assert out_overlay.fail_kind == "overlay_occluded"
        assert len(taps) == 0
        assert ax_overlay.calls == 0
        assert dismiss_calls == []

        # Whole-case suppression: symbol step succeeds on clean S01_home, then
        # follow-up non-symbol assert_present fails while overlay is shown ->
        # _dismiss_blocking_overlay_once is STILL never called (0 dismiss taps).
        clean_home_ocr = _ocr_frame("今晚，好好吃一顿", "立即下单", "调偏好")
        post_symbol_steps = [
            {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "打开饮食偏好页"},
            {"action": "assert_present", "queries": ["饮食偏好"], "wait_s": 0},
        ]
        taps_post = _patch_ctx_io(monkeypatch, ocr_sequence=[clean_home_ocr, overlay_ocr])
        with recipe_mod.recipe_scope("food_ordering", root=EXAMPLE_ROOT, overrides={"R3": post_symbol_steps}):
            out_post = runner_mod.run_case(
                {"id": "R3", "module": "首页", "item": "后续非符号步骤不自动关闭弹窗"},
                tmp_path / "run_overlay_post",
                str(tmp_path / "absent.sock"),
                30.0,
                ax=_FakeAxProvider(_ax_dump([_ax_node(enabled=True)])),
                ax_backend="frida_inproc",
                screen_asset_doc=screen_doc,
                symbol_binding=binding,
                allowed_effects=set(),
                effect_mode="active",
            )
        assert out_post.status == "fail"
        assert out_post.fail_kind == "assertion_failed"
        assert len(taps_post) == 1
        assert dismiss_calls == []

    # (b) Valid S02_preferences structure, but wrong business entity vs independent fixture data.
    expected_entity = flat_fixture_vars(profile.api_fixtures_path(), "R5")
    assert expected_entity["expected_store_id"] == "store-flagship-a"
    assert expected_entity["expected_store_name"] == "旗舰店-A"
    assert expected_entity["expected_preference_profile"] == "少冰少糖-VIP"

    entity_steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "打开饮食偏好页"},
        {
            "action": "assert_present",
            "queries": [
                "饮食偏好",
                "保存偏好",
                str(expected_entity["expected_store_name"]),
                str(expected_entity["expected_preference_profile"]),
            ],
            "wait_s": 0,
        },
    ]

    # Wrong entity frame STILL satisfies S02_preferences screen structure!
    wrong_entity_frame = _ocr_frame("饮食偏好", "保存偏好", "分店-B", "微辣•￥20-35・无忌口")
    assert match_screen(wrong_entity_frame, screen_by_id(screen_doc, "S02_preferences")).matched is True

    _patch_ctx_io(
        monkeypatch,
        ocr_sequence=[_ocr_frame("今晚，好好吃一顿", "立即下单", "调偏好"), wrong_entity_frame],
    )
    with recipe_mod.recipe_scope("food_ordering", root=EXAMPLE_ROOT, overrides={"R5": entity_steps}):
        out_wrong = runner_mod.run_case(
            {"id": "R5", "module": "饮食偏好", "item": "错对象拦截"},
            tmp_path / "run_wrong_entity",
            str(tmp_path / "absent.sock"),
            30.0,
            ax=_FakeAxProvider(_ax_dump([_ax_node()])),
            ax_backend="frida_inproc",
            screen_asset_doc=screen_doc,
            symbol_binding=binding,
            allowed_effects=set(),
            effect_mode="active",
        )
    assert out_wrong.status == "fail"
    assert out_wrong.fail_kind == "assertion_failed"

    # Matching independent entity frame PASSES!
    right_entity_frame = _ocr_frame("饮食偏好", "保存偏好", "旗舰店-A", "少冰少糖-VIP")
    _patch_ctx_io(
        monkeypatch,
        ocr_sequence=[_ocr_frame("今晚，好好吃一顿", "立即下单", "调偏好"), right_entity_frame],
    )
    with recipe_mod.recipe_scope("food_ordering", root=EXAMPLE_ROOT, overrides={"R5": entity_steps}):
        out_right = runner_mod.run_case(
            {"id": "R5", "module": "饮食偏好", "item": "正确对象通过"},
            tmp_path / "run_right_entity",
            str(tmp_path / "absent.sock"),
            30.0,
            ax=_FakeAxProvider(_ax_dump([_ax_node()])),
            ax_backend="frida_inproc",
            screen_asset_doc=screen_doc,
            symbol_binding=binding,
            allowed_effects=set(),
            effect_mode="active",
        )
    assert out_right.status == "pass", out_right.detail


# ── M3 item 2: `autophone app case lint --build-id` & Failure Modes ──────────


def test_case_lint_build_id_routing_and_symbol_failure_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify D5 offline `case lint --build-id` and default promoted build resolution
    without editing profile.yaml, plus all fail-closed checks and non-symbol compatibility."""
    # 1) Non-symbol active app without Package Store stays 100% backward compatible.
    ws_plain = tmp_path / "plain"
    plain_prof = create_app_profile("plain_app", "com.example.plain", "Plain", root=ws_plain)
    _write_cases(plain_prof, [{
        "id": "R1", "revision": 1, "status": "active", "automation": "ui_ready",
        "case_type": "功能测试", "test_point": "普通用例", "owner": "qa",
        "module": "首页", "item": "普通断言", "login_precondition": "any",
        "steps": ["断言首页"], "expects": ["首页可见"],
    }])
    _write_recipe(plain_prof, "R1", [{"action": "assert_present", "queries": ["首页"], "wait_s": 0}])
    plain_lint = CaseRecipeApplication(plain_prof).lint()
    assert plain_lint["error_count"] == 0 and plain_lint["warning_count"] == 0

    # 2) Multi-version symbol routing via default promoted build AND explicit --build-id!
    ws = tmp_path / "sym_lint"
    profile, _ = _setup_workspace_app(ws, extra_builds=[("002", BUILD_ID_V2, SHA_V2)])
    _write_symbol_artifact(
        profile,
        build_id=BUILD_ID_V2,
        artifact_sha256=SHA_V2,
        controls={
            "ctrl.home.preferences": {
                "kind": "button",
                "scope": {"screen_id": "S01_home", "region": [0.05, 0.15, 0.95, 0.90]},
                "locator": {
                    "kind": "ax_identifier",
                    "identifier": "home.ai_prompt.preferences.v2",
                    "role": "button",
                    "enabled_only": True,
                    "require_unique": True,
                },
                "effects": {"verbs": [], "completeness": "exhaustive"},
            },
            "ctrl.v2.only": {
                "kind": "button",
                "scope": {"screen_id": "S01_home"},
                "locator": {
                    "kind": "ax_identifier",
                    "identifier": "home.v2.only",
                    "role": "button",
                    "enabled_only": True,
                    "require_unique": True,
                },
                "effects": {"verbs": [], "completeness": "exhaustive"},
            },
        },
    )
    _write_cases(profile, [{
        "id": "R1", "revision": 1, "status": "active", "automation": "ui_ready",
        "case_type": "功能测试", "test_point": "符号导航", "owner": "qa",
        "module": "首页", "item": "符号导航", "login_precondition": "any",
        "steps": ["点击调偏好"], "expects": ["饮食偏好"],
    }])
    _write_recipe(profile, "R1", [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "调偏好"},
        {"action": "assert_present", "queries": ["饮食偏好"], "wait_s": 0},
    ])

    # Default promoted build (slot 001 -> bld-14282f675363) passes without editing profile.yaml.
    assert CaseRecipeApplication(profile).lint()["error_count"] == 0

    # Recipe referencing v2-only symbol fails against default v1, passes with --build-id bld-222222222222!
    _write_recipe(profile, "R1", [
        {"action": "tap_ax", "symbol": "ctrl.v2.only", "label": "v2入口"},
        {"action": "assert_present", "queries": ["饮食偏好"], "wait_s": 0},
    ])
    lint_v1 = CaseRecipeApplication(profile).lint()
    assert lint_v1["error_count"] == 1
    assert lint_v1["findings"][0]["blocker"]["code"] == "invalid_live_recipe"

    lint_v2 = CaseRecipeApplication(profile).lint(build_id=BUILD_ID_V2)
    assert lint_v2["error_count"] == 0, lint_v2

    # Exercise CLI parser & cmd_case_lint with --build-id!
    from autophone.cli import build_parser

    parser = build_parser(include_extensions=False)
    args = parser.parse_args(["app", "case", "lint", "food_ordering", "--build-id", BUILD_ID_V2])
    assert args.build_id == BUILD_ID_V2
    monkeypatch.setattr(cli_case.common, "load_app_profile", lambda _name: profile)
    rc = cli_case.cmd_case_lint(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_count"] == 0

    # Unknown --build-id -> symbol_binding_missing (exit 1).
    args_bad = parser.parse_args(["app", "case", "lint", "food_ordering", "--build-id", "bld-000000000000"])
    rc_bad = cli_case.cmd_case_lint(args_bad)
    assert rc_bad == 1
    bad_payload = json.loads(capsys.readouterr().out)
    assert bad_payload["apps"][0]["findings"][0]["blocker"]["code"] == "symbol_binding_missing"

    # Mixed raw locator/fallback keys -> invalid_live_recipe.
    _write_recipe(profile, "R1", [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "fallback_xy": [100, 200]},
        {"action": "assert_present", "queries": ["饮食偏好"], "wait_s": 0},
    ])
    lint_mixed = CaseRecipeApplication(profile).lint()
    assert lint_mixed["error_count"] == 1
    assert lint_mixed["findings"][0]["blocker"]["code"] == "invalid_live_recipe"
    assert "mix raw locator/fallback" in lint_mixed["findings"][0]["blocker"]["recovery"]

    # Missing screen_id in screens.yaml -> invalid_live_recipe.
    _write_symbol_artifact(
        profile,
        build_id=BUILD_ID_V2,
        artifact_sha256=SHA_V2,
        controls={
            "ctrl.bad.screen": {
                "kind": "button",
                "scope": {"screen_id": "S99_nonexistent"},
                "locator": {
                    "kind": "ax_identifier",
                    "identifier": "home.bad",
                    "role": "button",
                    "enabled_only": True,
                    "require_unique": True,
                },
                "effects": {"verbs": [], "completeness": "exhaustive"},
            },
        },
    )
    _write_recipe(profile, "R1", [
        {"action": "tap_ax", "symbol": "ctrl.bad.screen"},
        {"action": "assert_present", "queries": ["饮食偏好"], "wait_s": 0},
    ])
    lint_bad_screen = CaseRecipeApplication(profile).lint(build_id=BUILD_ID_V2)
    assert lint_bad_screen["error_count"] == 1
    assert "S99_nonexistent" in lint_bad_screen["findings"][0]["blocker"]["recovery"]

    # Tampered artifact_identity inside symbols/artifacts/<digest>.yaml -> symbol_binding_mismatch.
    sym_v1_path = profile.profile_dir / "symbols" / "artifacts" / f"{SHA_V1[:12]}.yaml"
    tampered = yaml.safe_load(sym_v1_path.read_text(encoding="utf-8"))
    tampered["artifact_identity"]["artifact_sha256"] = "9" * 64
    sym_v1_path.write_text(yaml.safe_dump(tampered, allow_unicode=True, sort_keys=False), encoding="utf-8")
    _write_recipe(profile, "R1", [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences"},
        {"action": "assert_present", "queries": ["饮食偏好"], "wait_s": 0},
    ])
    lint_tampered = CaseRecipeApplication(profile).lint()
    assert lint_tampered["error_count"] == 1
    assert lint_tampered["findings"][0]["blocker"]["code"] == "symbol_binding_mismatch"


# ── M6 item 2: `examples/food-ordering` Complete Asset Validation ─────────────


def test_examples_food_ordering_assets_validate_and_lint_cleanly(tmp_path: Path) -> None:
    """Verify all experimental assets under examples/food-ordering conform to schemas,
    match computed binding_sha256, resolve negative controls, and pass `case lint`."""
    profile = load_app_profile("food_ordering", root=EXAMPLE_ROOT, state_root=tmp_path)
    assert profile.build_label == BUILD_LABEL
    assert profile.ax_enabled is True
    assert profile.ax_backend == "frida_inproc"
    assert profile.trial_data_isolation == "isolated"

    # Screens asset at AppProfile.screens_path() defines calibrated live v1 anchors.
    screen_doc = load_screen_asset(profile.screens_path())
    s01_home = screen_by_id(screen_doc, "S01_home")
    s02_prefs = screen_by_id(screen_doc, "S02_preferences")
    assert s01_home["status"] == "active"
    assert [m["text"] for m in s01_home["must"]] == ["今晚，好好吃一顿", "立即下单"]
    assert s02_prefs["status"] == "active"
    assert [m["text"] for m in s02_prefs["must"]] == ["饮食偏好", "保存偏好"]

    # Immutable symbol artifact & external claims reference exact binding_sha256.
    binding = app_symbols.load_symbol_binding(profile, artifact_content_digest=SHA_V1)
    assert binding.build_id == BUILD_ID_V1
    assert binding.require_control("ctrl.home.preferences").locator.identifier == "home.ai_prompt.preferences"

    claims_doc = app_symbols.load_symbol_claims(profile)
    assert len(claims_doc["claims"]) >= 1
    assert any(
        c["kind"] == app_symbols.CLAIM_KIND_LOCATOR_BINDING
        and c["provenance"]["source"] == "l2_build_manifest"
        and c["evidence_level"] == "static_hint"
        and c["scope"]["binding_sha256"] == binding.binding_sha256
        for c in claims_doc["claims"]
    )

    # Recipes validate cleanly (R1 untouched, draft manual/symbol recipes, import_app_recipes).
    from autophone.application.negative_control import plan_negative_control
    from autophone.execution.app_recipes import import_app_recipes

    assert import_app_recipes("food_ordering", root=EXAMPLE_ROOT) is True

    r1_steps = recipe_mod.load_recipe(profile.recipes_dir() / "r1.yaml")
    recipe_mod.validate_recipe(r1_steps)

    manual_steps = recipe_mod.load_recipe(profile.recipes_dir() / "drafts" / "r2.yaml")
    recipe_mod.validate_recipe_for_app(manual_steps, profile)

    symbol_steps = recipe_mod.load_recipe(profile.recipes_dir() / "drafts" / "r3.yaml")
    expanded = recipe_mod.expand_symbol_steps(
        symbol_steps, symbol_binding=binding, screen_asset_doc=screen_doc,
    )
    recipe_mod.validate_recipe_for_app(
        expanded, profile, symbol_binding=binding, screen_asset_doc=screen_doc, effect_mode="active",
    )

    # Negative controls validate against negative-controls.v1.json and plan cleanly for R2 & R3.
    controls = load_negative_controls(profile)
    sym_ctrl = next(c for c in controls if c["id"] == "neg.prefs.symbol.skip_nav")
    resolved_sym = resolve_negative_control(symbol_steps, sym_ctrl)
    assert resolved_sym.perturbed_index == 0
    assert resolved_sym.expected_index_after == 0

    plan_r2 = plan_negative_control(profile, "R2", "neg.prefs.manual.skip_nav")
    assert plan_r2.recipe_source == "draft"
    assert plan_r2.case_id == "R2"
    plan_r3 = plan_negative_control(profile, "R3", "neg.prefs.symbol.skip_nav")
    assert plan_r3.recipe_source == "draft"
    assert plan_r3.case_id == "R3"

    # Independent expected-entity data config for Counterexample #6.
    r5_vars = flat_fixture_vars(profile.api_fixtures_path(), "R5")
    assert r5_vars["expected_store_id"] == "store-flagship-a"

    # Case lint passes cleanly with 0 errors and 0 warnings (no Package Store required).
    lint_report = CaseRecipeApplication(profile).lint()
    assert lint_report["error_count"] == 0, lint_report
    assert lint_report["warning_count"] == 0, lint_report


def test_promotion_and_activation_admission_gates_resolve_symbols_and_enforce_completeness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify autophone.execution.promotion.validate_recipe and admission.admit_case /
    activation_blocker / CaseLifecycleApplication.activate_many resolve app-symbols.v1
    for symbol-bound recipes, enforce active completeness hard-blocks, and leave
    non-symbol recipes untouched (no Package Store needed)."""
    from autophone.application.cases import CaseLifecycleApplication
    from autophone.execution import admission
    from autophone.execution import promotion as exec_promotion

    # Non-symbol app without Package Store stays 100% untouched.
    ws_plain = tmp_path / "plain_gate"
    plain_prof = create_app_profile("plain_gate", "com.example.plain", "Plain", root=ws_plain)
    plain_steps = [{"action": "assert_present", "queries": ["首页"], "wait_s": 0}]
    _write_cases(plain_prof, [{
        "id": "R1", "revision": 1, "status": "draft", "automation": "ui_ready",
        "case_type": "功能测试", "test_point": "非符号", "owner": "qa",
        "module": "首页", "item": "非符号用例", "login_precondition": "any",
        "steps": ["断言首页"], "expects": ["首页"],
    }])
    _write_recipe(plain_prof, "R1", plain_steps)
    exec_promotion.validate_recipe(plain_prof, "R1", plain_steps)
    assert admission.activation_blocker(plain_prof, {"id": "R1", "status": "draft", "automation": "ui_ready"}) is None

    # Symbol-bound app: exhaustive allowed, lower_bound & unknown hard-blocked!
    ws = tmp_path / "sym_gate"
    profile, _ = _setup_workspace_app(ws, allowed_effects=["pay"], trial_data_isolation="isolated")
    _write_cases(profile, [
        {
            "id": "R1", "revision": 1, "status": "deprecated", "automation": "ui_ready",
            "case_type": "功能测试", "test_point": "exhaustive符号", "owner": "qa",
            "module": "首页", "item": "调偏好", "login_precondition": "any",
            "steps": ["点击调偏好"], "expects": ["饮食偏好"],
        },
        {
            "id": "R2", "revision": 1, "status": "deprecated", "automation": "ui_ready",
            "case_type": "功能测试", "test_point": "lower_bound符号", "owner": "qa",
            "module": "结算", "item": "不完备下界", "login_precondition": "any",
            "steps": ["点击支付"], "expects": ["完成"],
        },
        {
            "id": "R3", "revision": 1, "status": "deprecated", "automation": "ui_ready",
            "case_type": "功能测试", "test_point": "unknown符号", "owner": "qa",
            "module": "结算", "item": "未知副作用", "login_precondition": "any",
            "steps": ["点击L1候选"], "expects": ["完成"],
        },
    ])
    exhaustive_steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "调偏好"},
        {"action": "assert_present", "queries": ["饮食偏好"], "wait_s": 0},
    ]
    lower_steps = [
        {"action": "tap_ax", "symbol": "ctrl.checkout.lower", "label": "支付"},
        {"action": "assert_present", "queries": ["完成"], "wait_s": 0},
    ]
    unknown_steps = [
        {"action": "tap_ax", "symbol": "ctrl.l1.unknown", "label": "未知"},
        {"action": "assert_present", "queries": ["完成"], "wait_s": 0},
    ]
    _write_recipe(profile, "R1", exhaustive_steps)
    _write_recipe(profile, "R2", lower_steps)
    _write_recipe(profile, "R3", unknown_steps)

    # 1) promotion.validate_recipe: exhaustive passes; lower_bound & unknown raise ContractError.
    exec_promotion.validate_recipe(profile, "R1", exhaustive_steps)
    with pytest.raises(ContractError, match=effects.REASON_EFFECT_COMPLETENESS_INSUFFICIENT):
        exec_promotion.validate_recipe(profile, "R2", lower_steps)
    with pytest.raises(ContractError, match=effects.REASON_EFFECT_COMPLETENESS_INSUFFICIENT):
        exec_promotion.validate_recipe(profile, "R3", unknown_steps)

    # 2) admission.activation_blocker & CaseLifecycleApplication.activate_many (default activation_policy):
    assert admission.activation_blocker(profile, {"id": "R1", "status": "draft", "automation": "ui_ready"}) is None
    blk_r2 = admission.activation_blocker(profile, {"id": "R2", "status": "draft", "automation": "ui_ready"})
    assert blk_r2 is not None and blk_r2["code"] == effects.REASON_EFFECT_COMPLETENESS_INSUFFICIENT
    blk_r3 = admission.activation_blocker(profile, {"id": "R3", "status": "draft", "automation": "ui_ready"})
    assert blk_r3 is not None and blk_r3["code"] == effects.REASON_EFFECT_COMPLETENESS_INSUFFICIENT

    lifecycle = CaseLifecycleApplication(
        profile,
        negctl_root=ws / "negctl",
        discrimination_required=lambda _c: False,
    )
    monkeypatch.setattr(lifecycle, "requires_promotion", lambda *_a: False)
    res_ok = lifecycle.activate_many(["R1"])
    assert res_ok["activated_count"] == 1 and res_ok["failed_count"] == 0, res_ok

    res_blocked = lifecycle.activate_many(["R2", "R3"])
    assert res_blocked["activated_count"] == 0 and res_blocked["failed_count"] == 2
    assert all(
        effects.REASON_EFFECT_COMPLETENESS_INSUFFICIENT in item["error"]
        for item in res_blocked["failed"]
    )

    # Missing symbol binding in live admission reports symbol_binding_missing.
    sym_file = profile.profile_dir / "symbols" / "artifacts" / f"{SHA_V1[:12]}.yaml"
    sym_file.unlink()
    blk_missing = admission.activation_blocker(profile, {"id": "R1", "status": "draft", "automation": "ui_ready"})
    assert blk_missing is not None and blk_missing["code"] == "symbol_binding_missing"


def test_bind_negative_control_draft_trial_respects_effect_confirmation_and_isolation(
    tmp_path: Path,
) -> None:
    """Draft trial negative controls with a remaining lower_bound/unknown symbol step
    enforce the M4 draft isolation/confirmation gate instead of unconditionally rejecting
    as active, while live negative controls still hard-block incomplete symbols."""
    profile, screen_doc = _setup_workspace_app(
        tmp_path, allowed_effects=["pay"], trial_data_isolation="isolated",
    )
    binding = app_symbols.load_symbol_binding(profile, artifact_content_digest=SHA_V1)

    _write_cases(profile, [{
        "id": "R1", "revision": 1, "status": "draft", "automation": "ui_ready",
        "case_type": "功能测试", "test_point": "草稿负对照", "owner": "qa",
        "module": "结算", "item": "草稿试跑负对照含不完备符号", "login_precondition": "any",
        "steps": ["先导航再触发lower_bound符号", "检查完成"], "expects": ["完成"],
    }])
    raw_steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "前置导航"},
        {"action": "tap_ax", "symbol": "ctrl.checkout.lower", "label": "下界支付"},
        {"action": "assert_present", "queries": ["完成"], "wait_s": 0},
    ]
    _write_recipe(profile, "R1", raw_steps, draft=True)
    _write_recipe(profile, "R1", raw_steps, draft=False)

    neg_dir = profile.profile_dir / "discrimination"
    neg_dir.mkdir(parents=True, exist_ok=True)
    ctrl = {
        "id": "neg.draft.skip_first",
        "case_id": "R1",
        "hypothesis": "skipping first nav step leaves lower_bound symbol step to validate under draft policy",
        "perturbation": {"kind": "skip_step", "step": {"action": "tap_ax", "occurrence": 1}},
        "expect": {"step": {"action": "assert_present"}, "fail_kinds": ["assertion_failed"]},
    }
    (neg_dir / "negative_controls.yaml").write_text(
        yaml.safe_dump({"schema": "autophone.negative-controls/1", "controls": [ctrl]}, allow_unicode=True),
        encoding="utf-8",
    )
    request = {
        "case_id": "R1",
        "control_id": ctrl["id"],
        "declaration_sha256": declaration_sha256(ctrl),
    }

    expanded_steps = recipe_mod.expand_symbol_steps(
        raw_steps, symbol_binding=binding, screen_asset_doc=screen_doc,
    )
    draft_case = {
        "id": "R1",
        "module": "结算",
        "item": "草稿试跑负对照",
        "_trial_recipe_document": {"id": "R1", "steps": expanded_steps},
        "_trial_recipe_identity": {
            "case_revision": 1,
            "reserved_external_id": None,
            "draft_recipe_sha256": "a" * 64,
        },
        "_symbol_binding_sha256": binding.binding_sha256,
    }

    # Draft missing confirm_trial_effects -> blocked!
    with pytest.raises(effects.EffectPolicyError) as exc_nc:
        bind_negative_control(
            profile,
            [draft_case],
            request,
            symbol_binding=binding,
            screen_asset_doc=screen_doc,
            trial_draft_recipes=True,
            confirm_trial_effects=False,
            trial_data_isolation="isolated",
        )
    assert exc_nc.value.code == effects.REASON_TRIAL_EFFECTS_NOT_CONFIRMED

    # Draft unisolated -> blocked!
    with pytest.raises(effects.EffectPolicyError) as exc_uniso:
        bind_negative_control(
            profile,
            [draft_case],
            request,
            symbol_binding=binding,
            screen_asset_doc=screen_doc,
            trial_draft_recipes=True,
            confirm_trial_effects=True,
            trial_data_isolation="unisolated",
        )
    assert exc_uniso.value.code == effects.REASON_TRIAL_DATA_UNISOLATED

    # Draft with BOTH confirm_trial_effects & isolated -> admitted & preserves binding_sha256!
    bound_draft = bind_negative_control(
        profile,
        [draft_case],
        request,
        symbol_binding=binding,
        screen_asset_doc=screen_doc,
        trial_draft_recipes=True,
        confirm_trial_effects=True,
        trial_data_isolation="isolated",
    )[0]
    assert bound_draft[NEGATIVE_CONTROL_RECORD_KEY]["binding_sha256"] == binding.binding_sha256
    assert len(bound_draft[NEGATIVE_CONTROL_STEPS_KEY]) == 2

    # Live (non-draft) negative control with remaining lower_bound step ALWAYS hard-blocks as active!
    live_case = {"id": "R1", "module": "结算", "item": "live负对照"}
    with pytest.raises(effects.EffectPolicyError) as exc_live:
        bind_negative_control(
            profile,
            [live_case],
            request,
            symbol_binding=binding,
            screen_asset_doc=screen_doc,
            trial_draft_recipes=True,
            confirm_trial_effects=True,
            trial_data_isolation="isolated",
        )
    assert exc_live.value.code == effects.REASON_EFFECT_COMPLETENESS_INSUFFICIENT
