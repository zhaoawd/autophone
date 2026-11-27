"""Milestone M3 execution tests + M2/M4 runtime wiring (Decisions D3, D4, D7, D8).

Covers:
- Counterexample #2: 0 / 1 / 2 matching AX nodes for identical identifier + role ->
  strictly 0 / 1 / 0 UDS tap dispatches (locator_missing / ok / ambiguous_match),
  fallback_xy ignored when require_unique=True, and identifier-only non-symbol
  tap_ax multi-match / zero-match fail-closed.
- Load-time symbol expansion (`expand_symbol_steps`, `validate_recipe_for_app`,
  `recipe_steps_digest`): rejects mixing symbol + raw locator/fallback keys,
  rejects non-tap_ax actions, validates screen_id against screens.yaml, preserves
  1:1 step_index and authored-vs-expanded digest parity.
- Runtime single-frame `screen_id` pre-check (`screen_mismatch`, 0 UDS taps).
- Runtime symbol step `StepFailure` skips `_dismiss_blocking_overlay_once` (D4),
  while non-symbol steps still invoke overlay recovery.
- Runtime M4 effect gate through `recipe.dispatch_step`, `runner.run_case`,
  `runner.run`, and `run_execution` (multi-effect wildcard rejection, active
  completeness block, draft 4-condition isolation/confirmation gate).
- Golden path binding symbol expansion & `binding_sha256` freeze.
- Runtime M2 wiring: `ax_snapshot` stamps `ax_backend` into `.ax.json`,
  `guest_axruntime` fails closed at `run_execution` and `operation_run` preflight,
  `binding_sha256` propagates into `outcome` & `recipe_identity`, and
  `symbol.unique_visibility` claims record ONLY on `confirmed` build-identity
  (never on `ambiguous`/`version_only`, never mutating symbol YAML).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from autophone.assets import app_symbols
from autophone.assets.apps import create_app_profile, load_app_profile
from autophone.assets.golden_paths import find_golden_path, run_marker
from autophone.contracts import ContractError, validate_or_raise
from autophone.device.ax_session import UnsupportedAxBackendError
from autophone.device.ax_target import AxDump, Node
from autophone.execution import effects
from autophone.execution.engine import actions as actions_mod
from autophone.execution.engine import recipe as recipe_mod
from autophone.execution.engine import runner as runner_mod
from autophone.execution.engine.actions import CaseContext, StepFailure
from autophone.execution.golden_path_binding import (
    GOLDEN_PATH_KEY,
    GOLDEN_PATH_LIVE_DOCUMENT_KEY,
    bind_golden_path,
)
from autophone.execution.operation_run import (
    OperationExecutionContext,
    OperationExecutionRequest,
    execute_operation,
    load_operation_spec,
)
from autophone.execution.recipe_identity import RECIPE_READ_KEY
from autophone.execution.run_execution import RunExecution

ARTIFACT_SHA = "14282f675363a9d74361c9b5d24dcde461648df2a13cf9aa4488d0aad4f68d58"
BUILD_ID = "bld-14282f675363"


# ── Shared Fixtures & Fakes ───────────────────────────────────────────────────


def _screen_doc(*, screen_id: str = "S01_home") -> dict[str, Any]:
    return {
        "meta": {"schema_version": 1, "app": "food_ordering", "build": "1.0.0-bld-14282f675363"},
        "screens": [
            {
                "id": screen_id,
                "display_name": "首页",
                "status": "active",
                "must": [{"text": "今晚，好好吃一顿"}],
                "forbid": [{"text": "登录"}],
                "any_of": {"min_hits": 0, "tokens": []},
                "blocking_overlays": [
                    {
                        "name": "promo_popup",
                        "detect": {"ocr_text": "活动弹窗"},
                        "dismiss": [{"action": "tap_text", "query": "关闭"}],
                    }
                ],
            },
            {
                "id": "S02_preferences",
                "display_name": "饮食偏好",
                "status": "active",
                "must": [{"text": "饮食偏好"}],
                "forbid": [],
                "any_of": {"min_hits": 0, "tokens": []},
            },
        ],
    }


def _ocr_frame(*texts: str) -> dict[str, Any]:
    return {
        "image_w": 430,
        "image_h": 932,
        "boxes": [
            {"text": t, "confidence": 0.99, "x": 100, "y": 200 + i * 40, "w": 120, "h": 30}
            for i, t in enumerate(texts)
        ],
    }


def _node(
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
        frame=(100.0, 280.0, 40.0, 40.0),
        enabled=enabled,
        value=None,
        identifier=identifier,
        path="0/1",
        native_x=native_x,
        native_y=native_y,
        nx=nx,
        ny=ny,
    )


def _dump(nodes: list[Node], *, ax_backend: str | None = "frida_inproc") -> AxDump:
    return AxDump(
        app="com.autophone.foodorderingdemo.v2",
        scale=3.0,
        w=430.0,
        h=932.0,
        view_controller="HomeVC",
        nodes=nodes,
        ax_backend=ax_backend,
    )


class _FakeAxProvider:
    def __init__(self, dumps: list[AxDump] | AxDump, *, ax_backend: str | None = "frida_inproc") -> None:
        self._dumps = list(dumps) if isinstance(dumps, list) else [dumps]
        self.ax_backend = ax_backend
        self.refresh_calls = 0

    def refresh(self) -> AxDump:
        idx = min(self.refresh_calls, len(self._dumps) - 1)
        self.refresh_calls += 1
        return self._dumps[idx]


def _make_app_with_symbols(
    tmp_path: Path,
    *,
    app: str = "food_ordering",
    allowed_effects: list[str] | None = None,
    trial_data_isolation: str = "unisolated",
    ax_backend: str = "frida_inproc",
    ax_enabled: bool = True,
    controls: dict[str, Any] | None = None,
    write_screens: bool = True,
) -> tuple[Any, app_symbols.LoadedSymbolBinding, dict[str, Any]]:
    profile = create_app_profile(
        app,
        "com.autophone.foodorderingdemo.v2",
        "Food Ordering",
        root=tmp_path,
    )
    profile_path = profile.profile_dir / "profile.yaml"
    raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    raw["build_label"] = "1.0.0-bld-14282f675363"
    raw["allowed_effects"] = list(allowed_effects or [])
    raw["trial_data_isolation"] = trial_data_isolation
    raw["capabilities"] = {
        "ax": {"enabled": ax_enabled, "backend": ax_backend},
    }
    profile_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")

    # Package store manifest (package-manifest.v2.json compliant).
    pkg_dir = tmp_path / "packages" / app
    pkg_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "v2",
        "app": app,
        "bundle_id": "com.autophone.foodorderingdemo.v2",
        "default_slot": "001",
        "version_slots": [
            {
                "slot": "001",
                "short_version": "1.0.0",
                "candidate_build_id": BUILD_ID,
                "created_at": "2026-11-26T10:00:00Z",
            }
        ],
        "builds": [
            {
                "build_id": BUILD_ID,
                "slot": "001",
                "label": None,
                "bundle_id": "com.autophone.foodorderingdemo.v2",
                "build_version": "1",
                "source_revision": {"kind": "unknown", "state": "unknown"},
                "artifact": {
                    "file": f"{BUILD_ID}.ipa",
                    "sha256": ARTIFACT_SHA,
                    "size": 1024,
                },
                "artifact_availability": "present",
                "release_like": True,
                "release_evidence": "embedded_js_bundle:Payload/App.app/main.jsbundle",
                "legacy_slot": None,
                "added_at": "2026-11-26T10:00:00Z",
            }
        ],
    }
    (pkg_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    screen_doc = _screen_doc()
    if write_screens:
        screens_path = profile.profile_dir / "screens" / "1.0.0-bld-14282f675363.yaml"
        screens_path.parent.mkdir(parents=True, exist_ok=True)
        screens_path.write_text(yaml.safe_dump(screen_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")

    default_controls: dict[str, Any] = {
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
    if controls is not None:
        default_controls = controls

    sym_dir = profile.profile_dir / "symbols" / "artifacts"
    sym_dir.mkdir(parents=True, exist_ok=True)
    sym_path = sym_dir / f"{ARTIFACT_SHA[:12]}.yaml"
    sym_doc = {
        "schema": "autophone.app-symbols/1",
        "app": app,
        "artifact_identity": {
            "kind": "ipa_build",
            "bundle_id": "com.autophone.foodorderingdemo.v2",
            "short_version": "1.0.0",
            "build_version": "1",
            "build_id": BUILD_ID,
            "artifact_sha256": ARTIFACT_SHA,
        },
        "controls": default_controls,
    }
    sym_path.write_text(yaml.safe_dump(sym_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")

    loaded_profile = load_app_profile(app, root=tmp_path, state_root=tmp_path)
    binding = app_symbols.load_symbol_binding_file(sym_path, expected_app=app)
    return loaded_profile, binding, screen_doc


def _patch_uds_tap_counter(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ocr_frame: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Intercept UDS requests in actions.py; return recorded tap payloads."""
    tap_dispatches: list[dict[str, Any]] = []
    fake_jpeg = b"\xff\xd8\xff\xe0FAKEJPEG"
    import base64

    b64_jpeg = base64.b64encode(fake_jpeg).decode("ascii")

    def fake_request(msg: dict[str, Any], **kwargs: Any) -> tuple[dict[str, Any], Any]:
        t = msg.get("t")
        if t == "screenshot":
            return {"ok": True, "image": b64_jpeg}, SimpleNamespace(image_w=None)
        if t == "tap":
            tap_dispatches.append(dict(msg))
            return {"ok": True}, None
        return {"ok": True}, None

    monkeypatch.setattr(actions_mod, "request", fake_request)
    monkeypatch.setattr(actions_mod, "GeometryGuard", lambda: SimpleNamespace(check=lambda sample: None))
    monkeypatch.setattr(actions_mod.CaseContext, "_settle", lambda self: None)
    if ocr_frame is not None:
        monkeypatch.setattr(actions_mod, "run_ocr", lambda img_path: ocr_frame)
    return tap_dispatches


# ── 1. Counterexample #2: 0 / 1 / 2 AX Nodes -> 0 / 1 / 0 UDS Taps ───────────


@pytest.mark.parametrize("node_count", [0, 1, 2])
def test_counterexample_2_require_unique_tap_ax_0_1_2_nodes_strict_tap_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    node_count: int,
) -> None:
    """Counterexample #2: identical identifier + role with 0 / 1 / 2 nodes must
    dispatch strictly 0 / 1 / 0 UDS taps and ignore fallback_xy when require_unique=True."""
    tap_dispatches = _patch_uds_tap_counter(monkeypatch, ocr_frame=_ocr_frame("今晚，好好吃一顿"))
    nodes = [
        _node(
            identifier="home.ai_prompt.preferences",
            role="button",
            label=f"偏好{i}",
            native_x=300 + i * 100,
            native_y=900,
        )
        for i in range(node_count)
    ]
    ctx = CaseContext(
        case_id="R_CE2",
        shot_dir=tmp_path / "shots",
        ax=_FakeAxProvider(_dump(nodes)),
        screen_asset_doc=_screen_doc(),
    )

    if node_count == 1:
        res = ctx.tap_ax(
            identifier="home.ai_prompt.preferences",
            role="button",
            screen_id="S01_home",
            require_unique=True,
            fallback_xy=(50, 50),
            label="打开饮食偏好设置",
        )
        assert res.ok is True
        assert res.kind == ""
        assert len(tap_dispatches) == 1
        assert tap_dispatches[0] == {"t": "tap", "x": 300, "y": 900}
    else:
        with pytest.raises(StepFailure) as exc_info:
            ctx.tap_ax(
                identifier="home.ai_prompt.preferences",
                role="button",
                screen_id="S01_home",
                require_unique=True,
                fallback_xy=(50, 50),
                label="打开饮食偏好设置",
            )
        expected_kind = "locator_missing" if node_count == 0 else "ambiguous_match"
        assert exc_info.value.result.kind == expected_kind
        assert len(tap_dispatches) == 0


def test_non_symbol_identifier_only_tap_ax_blocks_multi_and_zero_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identifier-only blind spot fix: even with require_unique=False,
    tap_ax(identifier=...) without query blocks both 0-match and >=2-match with 0 taps."""
    tap_dispatches = _patch_uds_tap_counter(monkeypatch)
    two_nodes = [
        _node(identifier="dup.btn", role="button", label="A"),
        _node(identifier="dup.btn", role="button", label="B"),
    ]
    ctx_dup = CaseContext(
        case_id="R_ID_DUP",
        shot_dir=tmp_path / "shots",
        ax=_FakeAxProvider(_dump(two_nodes)),
    )
    with pytest.raises(StepFailure) as dup_exc:
        ctx_dup.tap_ax(identifier="dup.btn", role="button", fallback_xy=(10, 20))
    assert dup_exc.value.result.kind == "ambiguous_match"
    assert len(tap_dispatches) == 0

    ctx_zero = CaseContext(
        case_id="R_ID_ZERO",
        shot_dir=tmp_path / "shots",
        ax=_FakeAxProvider(_dump([])),
    )
    with pytest.raises(StepFailure) as zero_exc:
        ctx_zero.tap_ax(identifier="dup.btn", role="button", fallback_xy=(10, 20))
    assert zero_exc.value.result.kind == "locator_missing"
    assert len(tap_dispatches) == 0


def test_require_unique_ignores_fallback_xy_when_ax_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tap_dispatches = _patch_uds_tap_counter(monkeypatch)
    ctx = CaseContext(case_id="R_NOAX", shot_dir=tmp_path / "shots", ax=None)
    with pytest.raises(StepFailure) as exc_info:
        ctx.tap_ax(identifier="home.ai_prompt.preferences", require_unique=True, fallback_xy=(100, 200))
    assert exc_info.value.result.kind == "ax_unavailable"
    assert len(tap_dispatches) == 0

    # Legacy query-based tap_ax with require_unique=False still degrades to fallback_xy.
    res = ctx.tap_ax(query="关闭", role="button", fallback_xy=(100, 200))
    assert res.ok is True
    assert len(tap_dispatches) == 1


# ── 2. Load-Time Symbol Expansion & Static Validation ─────────────────────────


@pytest.mark.parametrize(
    "forbidden_key,value",
    [
        ("fallback_xy", [100, 200]),
        ("identifier", "home.ai_prompt.preferences"),
        ("role", "button"),
        ("region", [0.0, 0.0, 1.0, 1.0]),
        ("query", "偏好"),
        ("enabled_only", True),
        ("screen_id", "S01_home"),
        ("require_unique", True),
    ],
)
def test_static_validation_rejects_mixing_symbol_with_raw_keys(
    tmp_path: Path,
    forbidden_key: str,
    value: Any,
) -> None:
    profile, binding, screen_doc = _make_app_with_symbols(tmp_path)
    bad_steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", forbidden_key: value},
        {"action": "assert_present", "queries": ["饮食偏好"]},
    ]
    with pytest.raises(recipe_mod.RecipeError, match="cannot mix raw locator/fallback"):
        recipe_mod.expand_symbol_steps(bad_steps, symbol_binding=binding, screen_asset_doc=screen_doc)
    with pytest.raises(recipe_mod.RecipeError, match="cannot mix raw locator/fallback"):
        recipe_mod.validate_recipe_for_app(
            bad_steps, profile, symbol_binding=binding, screen_asset_doc=screen_doc
        )


def test_static_validation_rejects_symbol_on_non_tap_ax_and_missing_screen_or_symbol(
    tmp_path: Path,
) -> None:
    profile, binding, screen_doc = _make_app_with_symbols(tmp_path)

    # Non-tap_ax action with symbol rejected.
    with pytest.raises(recipe_mod.RecipeError, match="only supported on action 'tap_ax'"):
        recipe_mod.expand_symbol_steps(
            [{"action": "tap_text", "symbol": "ctrl.home.preferences"}],
            symbol_binding=binding,
            screen_asset_doc=screen_doc,
        )

    # Unknown symbol rejected.
    with pytest.raises(recipe_mod.RecipeError, match="not found"):
        recipe_mod.expand_symbol_steps(
            [{"action": "tap_ax", "symbol": "ctrl.does.not.exist"}],
            symbol_binding=binding,
            screen_asset_doc=screen_doc,
        )

    # Missing symbol_binding rejected.
    with pytest.raises(recipe_mod.RecipeError, match="requires a loaded app-symbols binding"):
        recipe_mod.expand_symbol_steps(
            [{"action": "tap_ax", "symbol": "ctrl.home.preferences"}],
            symbol_binding=None,
            screen_asset_doc=screen_doc,
        )

    # Unknown screen_id in screens.yaml rejected.
    bad_screen_doc = {
        "meta": {"schema_version": 1, "app": "food_ordering", "build": "1.0.0"},
        "screens": [
            {
                "id": "S99_other",
                "display_name": "Other",
                "status": "active",
                "must": [{"text": "Other"}],
                "forbid": [],
                "any_of": {"min_hits": 0, "tokens": []},
            }
        ],
    }
    with pytest.raises(recipe_mod.RecipeError, match="unknown screen_id 'S01_home'"):
        recipe_mod.expand_symbol_steps(
            [{"action": "tap_ax", "symbol": "ctrl.home.preferences"}],
            symbol_binding=binding,
            screen_asset_doc=bad_screen_doc,
        )

    # Missing screen_asset_doc rejected.
    with pytest.raises(recipe_mod.RecipeError, match="requires screen_asset_doc"):
        recipe_mod.expand_symbol_steps(
            [{"action": "tap_ax", "symbol": "ctrl.home.preferences"}],
            symbol_binding=binding,
            screen_asset_doc=None,
        )

    # Raw YAML smuggling internal `_symbol_binding` key rejected at load time.
    smuggled = tmp_path / "smuggled.yaml"
    smuggled.write_text(
        yaml.safe_dump(
            {
                "id": "R1",
                "steps": [
                    {
                        "action": "tap_ax",
                        "identifier": "home.ai_prompt.preferences",
                        "_symbol_binding": {
                            "symbol_id": "ctrl.home.preferences",
                            "binding_sha256": "a" * 64,
                            "effects": {"verbs": [], "completeness": "exhaustive"},
                        },
                    }
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    with pytest.raises(recipe_mod.RecipeError, match="internal key"):
        recipe_mod.load_recipe_document(smuggled)

    # Raw unresolved symbol step cannot be dispatched directly.
    ctx = CaseContext(case_id="R_RAW", shot_dir=tmp_path / "shots")
    with pytest.raises(recipe_mod.RecipeError, match="unresolved 'symbol'"):
        recipe_mod.dispatch_step(ctx, {"action": "tap_ax", "symbol": "ctrl.home.preferences"})


def test_expand_symbol_steps_preserves_1_to_1_step_index_and_digest_parity(
    tmp_path: Path,
) -> None:
    profile, binding, screen_doc = _make_app_with_symbols(tmp_path)
    raw_steps = [
        {"action": "wait_text", "query": "今晚，好好吃一顿"},
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "打开饮食偏好设置"},
        {"action": "assert_present", "queries": ["饮食偏好"]},
    ]
    assert recipe_mod.recipe_uses_symbols(raw_steps) is True
    assert recipe_mod.recipe_has_unresolved_symbols(raw_steps) is True

    expanded = recipe_mod.expand_symbol_steps(
        raw_steps,
        symbol_binding=binding,
        screen_asset_doc=screen_doc,
    )
    assert len(expanded) == len(raw_steps) == 3
    assert expanded[0] == raw_steps[0]
    assert expanded[2] == raw_steps[2]

    sym_step = expanded[1]
    assert sym_step["action"] == "tap_ax"
    assert "symbol" not in sym_step
    assert sym_step["identifier"] == "home.ai_prompt.preferences"
    assert sym_step["role"] == "button"
    assert sym_step["region"] == [0.05, 0.15, 0.95, 0.90]
    assert sym_step["enabled_only"] is True
    assert sym_step["screen_id"] == "S01_home"
    assert sym_step["require_unique"] is True
    assert sym_step["fallback_xy"] is None
    assert sym_step["label"] == "打开饮食偏好设置"
    meta = sym_step[effects.SYMBOL_BINDING_STEP_KEY]
    assert meta == {
        "symbol_id": "ctrl.home.preferences",
        "binding_sha256": binding.binding_sha256,
        "artifact_content_digest": binding.artifact_content_digest,
        "screen_id": "S01_home",
        "effects": {"verbs": [], "completeness": "exhaustive"},
    }
    assert recipe_mod.steps_binding_sha256(expanded) == binding.binding_sha256
    assert recipe_mod.recipe_has_unresolved_symbols(expanded) is False
    assert recipe_mod.recipe_uses_symbols(expanded) is True

    # Expanded steps pass validate_recipe & validate_recipe_for_app and match raw digest!
    recipe_mod.validate_recipe(expanded)
    recipe_mod.validate_recipe_for_app(expanded, profile)
    assert recipe_mod.recipe_steps_digest(raw_steps) == recipe_mod.recipe_steps_digest(expanded)


# ── 3. Runtime screen_id Single-Frame Pre-Check (D4) ─────────────────────────


def test_runtime_screen_id_mismatch_blocks_with_zero_uds_taps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-frame screen_id mismatch fails closed with kind='screen_mismatch' and 0 taps."""
    # Wrong screen: missing "今晚，好好吃一顿" or hitting forbidden "登录"
    tap_dispatches = _patch_uds_tap_counter(monkeypatch, ocr_frame=_ocr_frame("登录", "其他页面"))
    ax = _FakeAxProvider(_dump([_node()]))
    ctx = CaseContext(
        case_id="R_WRONG_SCR",
        shot_dir=tmp_path / "shots",
        ax=ax,
        screen_asset_doc=_screen_doc(),
    )
    with pytest.raises(StepFailure) as exc_info:
        ctx.tap_ax(
            identifier="home.ai_prompt.preferences",
            role="button",
            screen_id="S01_home",
            require_unique=True,
            fallback_xy=(100, 200),
        )
    assert exc_info.value.result.kind == "screen_mismatch"
    assert len(tap_dispatches) == 0
    assert ax.refresh_calls == 0

    # Missing screen_asset_doc also fails closed with screen_mismatch and 0 taps.
    ctx_no_doc = CaseContext(
        case_id="R_NODOC",
        shot_dir=tmp_path / "shots",
        ax=ax,
        screen_asset_doc=None,
    )
    with pytest.raises(StepFailure) as nodoc_exc:
        ctx_no_doc.tap_ax(
            identifier="home.ai_prompt.preferences",
            screen_id="S01_home",
            require_unique=True,
        )
    assert nodoc_exc.value.result.kind == "screen_mismatch"
    assert len(tap_dispatches) == 0


# ── 4. Runtime Symbol Step Skips _dismiss_blocking_overlay_once (D4) ──────────


def test_symbol_step_failure_skips_overlay_dismiss_while_non_symbol_uses_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision D4: symbol-bound step StepFailure MUST NEVER call _dismiss_blocking_overlay_once,
    whereas an ordinary non-symbol step still calls it and retries."""
    _profile, binding, screen_doc = _make_app_with_symbols(tmp_path)
    tap_dispatches = _patch_uds_tap_counter(monkeypatch, ocr_frame=_ocr_frame("活动弹窗"))
    dismiss_calls: list[str] = []

    def fake_dismiss(ctx: CaseContext) -> str | None:
        dismiss_calls.append(ctx.case_id)
        return "promo_popup"

    monkeypatch.setattr(recipe_mod, "_dismiss_blocking_overlay_once", fake_dismiss)

    # 1) Symbol-bound step fails (screen_mismatch) -> overlay dismiss NEVER called!
    expanded = recipe_mod.expand_symbol_steps(
        [{"action": "tap_ax", "symbol": "ctrl.home.preferences"}],
        symbol_binding=binding,
        screen_asset_doc=screen_doc,
    )
    ctx_sym = CaseContext(
        case_id="R_SYM_FAIL",
        shot_dir=tmp_path / "shots",
        ax=_FakeAxProvider(_dump([_node()])),
        screen_asset_doc=screen_doc,
        allowed_effects=set(),
    )
    with pytest.raises(StepFailure) as sym_exc:
        recipe_mod.dispatch_step(ctx_sym, expanded[0])
    assert sym_exc.value.result.kind == "screen_mismatch"
    assert dismiss_calls == []
    assert len(tap_dispatches) == 0

    # 2) Non-symbol step fails once -> overlay dismiss IS called and retries once!
    ctx_plain = CaseContext(
        case_id="R_PLAIN",
        shot_dir=tmp_path / "shots",
        ax=_FakeAxProvider([_dump([]), _dump([_node(label="确定")])]),
        screen_asset_doc=screen_doc,
        allowed_effects=set(),
    )
    res = recipe_mod.dispatch_step(
        ctx_plain,
        {"action": "tap_ax", "query": "确定", "role": "button"},
    )
    assert res.ok is True
    assert dismiss_calls == ["R_PLAIN"]
    assert len(tap_dispatches) == 1


# ── 5. Runtime M4 Wiring Through dispatch_step & runner ───────────────────────


def test_runtime_m4_effect_gates_in_dispatch_step_and_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counterexample #5 wired through recipe.dispatch_step & runner.run_case."""
    _profile, binding, screen_doc = _make_app_with_symbols(tmp_path)
    tap_dispatches = _patch_uds_tap_counter(monkeypatch, ocr_frame=_ocr_frame("今晚，好好吃一顿"))

    multi_step = recipe_mod.expand_symbol_steps(
        [{"action": "tap_ax", "symbol": "ctrl.checkout.submit"}],
        symbol_binding=binding,
        screen_asset_doc=screen_doc,
    )[0]
    lower_step = recipe_mod.expand_symbol_steps(
        [{"action": "tap_ax", "symbol": "ctrl.checkout.lower"}],
        symbol_binding=binding,
        screen_asset_doc=screen_doc,
    )[0]
    unknown_step = recipe_mod.expand_symbol_steps(
        [{"action": "tap_ax", "symbol": "ctrl.l1.unknown"}],
        symbol_binding=binding,
        screen_asset_doc=screen_doc,
    )[0]

    # (a) Multi-effect rejects "irreversible" wildcard at dispatch time.
    ctx_wild = CaseContext(
        case_id="R_WILD",
        shot_dir=tmp_path / "shots",
        ax=_FakeAxProvider(_dump([_node(identifier="checkout.submit")])),
        screen_asset_doc=screen_doc,
        allowed_effects={"irreversible"},
        effect_mode="active",
    )
    with pytest.raises(StepFailure) as exc_wild:
        recipe_mod.dispatch_step(ctx_wild, multi_step)
    assert exc_wild.value.result.kind == effects.REASON_MULTI_EFFECT_WILDCARD_REJECTED
    assert len(tap_dispatches) == 0

    # (b) Multi-effect with partial verbs {"pay"} -> blocked.
    ctx_partial = CaseContext(
        case_id="R_PARTIAL",
        shot_dir=tmp_path / "shots",
        ax=_FakeAxProvider(_dump([_node(identifier="checkout.submit")])),
        screen_asset_doc=screen_doc,
        allowed_effects={"pay"},
        effect_mode="active",
    )
    with pytest.raises(StepFailure) as exc_partial:
        recipe_mod.dispatch_step(ctx_partial, multi_step)
    assert exc_partial.value.result.kind == effects.REASON_EFFECT_NOT_ALLOWED
    assert len(tap_dispatches) == 0

    # (c) lower_bound & unknown blocked in active mode.
    for step_item, ident in ((lower_step, "checkout.lower"), (unknown_step, "l1.unknown")):
        ctx_act = CaseContext(
            case_id="R_ACT",
            shot_dir=tmp_path / "shots",
            ax=_FakeAxProvider(_dump([_node(identifier=ident)])),
            screen_asset_doc=screen_doc,
            allowed_effects={"pay"},
            effect_mode="active",
            trial_draft_recipes=True,
            confirm_trial_effects=True,
            trial_data_isolation="isolated",
        )
        with pytest.raises(StepFailure) as exc_act:
            recipe_mod.dispatch_step(ctx_act, step_item)
        assert exc_act.value.result.kind == effects.REASON_EFFECT_COMPLETENESS_INSUFFICIENT
    assert len(tap_dispatches) == 0

    # (d) draft mode missing confirm_trial_effects -> blocked.
    ctx_no_confirm = CaseContext(
        case_id="R_NOCONF",
        shot_dir=tmp_path / "shots",
        ax=_FakeAxProvider(_dump([_node(identifier="checkout.lower")])),
        screen_asset_doc=screen_doc,
        allowed_effects={"pay"},
        effect_mode="draft",
        trial_draft_recipes=True,
        confirm_trial_effects=False,
        trial_data_isolation="isolated",
    )
    with pytest.raises(StepFailure) as exc_noconf:
        recipe_mod.dispatch_step(ctx_no_confirm, lower_step)
    assert exc_noconf.value.result.kind == effects.REASON_TRIAL_EFFECTS_NOT_CONFIRMED
    assert len(tap_dispatches) == 0

    # (e) draft mode with unisolated profile -> blocked.
    ctx_uniso = CaseContext(
        case_id="R_UNISO",
        shot_dir=tmp_path / "shots",
        ax=_FakeAxProvider(_dump([_node(identifier="checkout.lower")])),
        screen_asset_doc=screen_doc,
        allowed_effects={"pay"},
        effect_mode="draft",
        trial_draft_recipes=True,
        confirm_trial_effects=True,
        trial_data_isolation="unisolated",
    )
    with pytest.raises(StepFailure) as exc_uniso:
        recipe_mod.dispatch_step(ctx_uniso, lower_step)
    assert exc_uniso.value.result.kind == effects.REASON_TRIAL_DATA_UNISOLATED
    assert len(tap_dispatches) == 0

    # (f) draft mode with ALL 4 conditions satisfied -> ALLOWED and dispatches 1 tap!
    ctx_ok = CaseContext(
        case_id="R_OK",
        shot_dir=tmp_path / "shots",
        ax=_FakeAxProvider(_dump([_node(identifier="checkout.lower")])),
        screen_asset_doc=screen_doc,
        allowed_effects={"pay"},
        effect_mode="draft",
        trial_draft_recipes=True,
        confirm_trial_effects=True,
        trial_data_isolation="isolated",
        ax_backend="frida_inproc",
    )
    res = recipe_mod.dispatch_step(ctx_ok, lower_step)
    assert res.ok is True
    assert len(tap_dispatches) == 1
    assert len(ctx_ok._symbol_observations) == 1
    assert ctx_ok._symbol_observations[0]["symbol_id"] == "ctrl.checkout.lower"
    assert ctx_ok._symbol_observations[0]["binding_sha256"] == binding.binding_sha256

    # (g) runner.run_case threads M4 + symbol_binding + ax_backend & records binding_sha256!
    raw_recipe = [
        {"action": "tap_ax", "symbol": "ctrl.checkout.lower"},
        {"action": "assert_present", "queries": ["今晚，好好吃一顿"]},
    ]
    with recipe_mod.recipe_scope("food_ordering", root=tmp_path, overrides={"R_RUN": raw_recipe}):
        outcome_blocked = runner_mod.run_case(
            {"id": "R_RUN", "module": "checkout", "item": "lower bound"},
            tmp_path / "shots_blocked",
            "dummy.sock",
            5.0,
            ax=_FakeAxProvider(_dump([_node(identifier="checkout.lower")])),
            allowed_effects={"pay"},
            screen_asset_doc=screen_doc,
            ax_backend="frida_inproc",
            effect_mode="draft",
            trial_draft_recipes=True,
            confirm_trial_effects=False,
            trial_data_isolation="isolated",
            symbol_binding=binding,
        )
        assert outcome_blocked.status == "fail"
        assert outcome_blocked.fail_kind == effects.REASON_TRIAL_EFFECTS_NOT_CONFIRMED
        assert outcome_blocked.binding_sha256 == binding.binding_sha256
        assert outcome_blocked.ax_backend == "frida_inproc"
        assert len(tap_dispatches) == 1  # still 1; blocked case dispatched 0 taps

        outcome_ok = runner_mod.run_case(
            {"id": "R_RUN", "module": "checkout", "item": "lower bound"},
            tmp_path / "shots_ok",
            "dummy.sock",
            5.0,
            ax=_FakeAxProvider(_dump([_node(identifier="checkout.lower")])),
            allowed_effects={"pay"},
            screen_asset_doc=screen_doc,
            ax_backend="frida_inproc",
            effect_mode="draft",
            trial_draft_recipes=True,
            confirm_trial_effects=True,
            trial_data_isolation="isolated",
            symbol_binding=binding,
        )
        assert outcome_ok.status == "pass"
        assert outcome_ok.binding_sha256 == binding.binding_sha256
        assert outcome_ok.ax_backend == "frida_inproc"
        assert outcome_ok.symbol_observations is not None
        assert outcome_ok.symbol_observations[0]["symbol_id"] == "ctrl.checkout.lower"
        assert len(tap_dispatches) == 2

        # (h) Fail-closed regression: live active case (no _trial_recipe_document)
        # with effect_mode=None & trial_draft_recipes=True MUST resolve as "active"
        # and hard-block lower_bound with 0 taps!
        outcome_live_active = runner_mod.run_case(
            {"id": "R_RUN", "module": "checkout", "item": "live active lower bound"},
            tmp_path / "shots_live_active",
            "dummy.sock",
            5.0,
            ax=_FakeAxProvider(_dump([_node(identifier="checkout.lower")])),
            allowed_effects={"pay"},
            screen_asset_doc=screen_doc,
            ax_backend="frida_inproc",
            effect_mode=None,
            trial_draft_recipes=True,
            confirm_trial_effects=True,
            trial_data_isolation="isolated",
            symbol_binding=binding,
        )
        assert outcome_live_active.status == "fail"
        assert outcome_live_active.fail_kind == effects.REASON_EFFECT_COMPLETENESS_INSUFFICIENT
        assert len(tap_dispatches) == 2  # still 2; live active case dispatched 0 taps


# ── 6. Golden Path Binding Symbol Expansion ───────────────────────────────────


def test_bind_golden_path_expands_symbol_steps_and_records_binding_sha256(
    tmp_path: Path,
) -> None:
    profile, binding, screen_doc = _make_app_with_symbols(tmp_path)
    cases_doc = {
        "schema_version": "v1",
        "app": "food_ordering",
        "cases": [
            {
                "id": "R2",
                "module": "home",
                "item": "open preferences",
                "status": "active",
                "automation": "ui_ready",
                "revision": 1,
            }
        ],
    }
    profile.cases_path().parent.mkdir(parents=True, exist_ok=True)
    profile.cases_path().write_text(yaml.safe_dump(cases_doc, allow_unicode=True), encoding="utf-8")

    live_recipe_path = profile.recipes_dir() / "r2.yaml"
    live_recipe_path.parent.mkdir(parents=True, exist_ok=True)
    raw_steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "调偏好"},
        {"action": "assert_present", "queries": ["饮食偏好"]},
    ]
    live_recipe_path.write_text(
        yaml.safe_dump({"id": "R2", "steps": raw_steps}, allow_unicode=True),
        encoding="utf-8",
    )

    gp_path = profile.profile_dir / "golden_paths.yaml"
    gp_path.write_text(
        yaml.safe_dump(
            {
                "schema": "autophone.golden-path/1",
                "paths": [{"id": "gp-home-pref", "case_id": "R2", "title": "Home to Preferences"}],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    req = run_marker(find_golden_path(profile, "gp-home-pref"))

    # Missing symbol_binding fails closed.
    with pytest.raises(ContractError, match="requires a loaded app-symbols binding"):
        bind_golden_path(profile, [{"id": "R2"}], req, symbol_binding=None, screen_asset_doc=screen_doc)

    # With symbol_binding + screen_asset_doc -> freezes expanded steps & records binding_sha256.
    bound_cases = bind_golden_path(
        profile,
        [{"id": "R2"}],
        req,
        symbol_binding=binding,
        screen_asset_doc=screen_doc,
    )
    bound = bound_cases[0]
    assert bound[GOLDEN_PATH_KEY]["binding_sha256"] == binding.binding_sha256
    assert bound[RECIPE_READ_KEY]["binding_sha256"] == binding.binding_sha256
    assert bound[RECIPE_READ_KEY]["steps_digest"] == recipe_mod.recipe_steps_digest(raw_steps)
    frozen_steps = bound[GOLDEN_PATH_LIVE_DOCUMENT_KEY]["steps"]
    assert frozen_steps[0]["require_unique"] is True
    assert frozen_steps[0]["screen_id"] == "S01_home"
    assert frozen_steps[0]["fallback_xy"] is None
    assert frozen_steps[0][effects.SYMBOL_BINDING_STEP_KEY]["binding_sha256"] == binding.binding_sha256


# ── 7. Runtime M2 Wiring: ax_snapshot Stamp & guest_axruntime Preflight ───────


def test_ax_snapshot_stamps_ax_backend_and_reraises_unsupported_backend(
    tmp_path: Path,
) -> None:
    # (a) Dump without ax_backend inherits from ax_provider.ax_backend / ctx.ax_backend.
    dump_no_backend = _dump([_node()], ax_backend=None)
    ctx = CaseContext(
        case_id="R_SNAP",
        shot_dir=tmp_path / "shots",
        ax=_FakeAxProvider(dump_no_backend, ax_backend=None),
        ax_backend="frida_inproc",
    )
    dump, path = ctx.ax_snapshot(label="check")
    assert dump is not None and path is not None and path.is_file()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["ax_backend"] == "frida_inproc"

    # (b) UnsupportedAxBackendError MUST NOT be swallowed fail-open by ax_snapshot.
    class _BrokenBackendAx:
        def refresh(self) -> AxDump:
            raise UnsupportedAxBackendError("guest_axruntime", "not implemented")

    ctx_bad = CaseContext(case_id="R_BAD", shot_dir=tmp_path / "shots", ax=_BrokenBackendAx())
    with pytest.raises(UnsupportedAxBackendError) as exc_info:
        ctx_bad.ax_snapshot(label="bad")
    assert exc_info.value.code == "ax_backend_unsupported"


def test_unsupported_ax_backend_fails_closed_at_run_execution_and_operation_run_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "autophone.device.ax_session.SUPPORTED_AX_BACKENDS",
        frozenset({"frida_inproc"}),
    )
    profile, _binding, _screen_doc = _make_app_with_symbols(
        tmp_path,
        app="guest_ax_app",
        ax_backend="guest_axruntime",
    )
    cases_path = profile.cases_path()
    cases_path.parent.mkdir(parents=True, exist_ok=True)
    cases_path.write_text(
        yaml.safe_dump({"schema_version": "v1", "app": "guest_ax_app", "cases": []}, allow_unicode=True),
        encoding="utf-8",
    )
    ops_path = profile.operations_path()
    ops_path.parent.mkdir(parents=True, exist_ok=True)
    ops_path.write_text(
        yaml.safe_dump(
            {
                "meta": {"schema_version": 1, "app": "guest_ax_app"},
                "operations": [
                    {
                        "id": "OP1",
                        "name": "open_prefs",
                        "status": "active",
                        "revision": 1,
                        "summary": "open prefs",
                        "params": [],
                        "steps": [{"action": "tap_text", "query": "偏好"}],
                    }
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    ax_scope_called = False

    def _forbid_ax_scope(*args: Any, **kwargs: Any) -> Any:
        nonlocal ax_scope_called
        ax_scope_called = True
        raise AssertionError("ax_scope must not be reached when ax_backend is unsupported")

    monkeypatch.setattr("autophone.execution.run_execution.ax_scope", _forbid_ax_scope)

    run_exec = RunExecution(
        lock=SimpleNamespace(acquire=lambda **k: contextlib.nullcontext()),
        rig_maintenance=SimpleNamespace(),
        executor=None,
        run_dir=lambda rid: tmp_path / "runs" / rid,
        persist_result=lambda d, r: d / "platform-run-result.json",
        set_active=lambda *a, **k: None,
        update_active=lambda *a, **k: None,
        clear_active=lambda *a, **k: None,
        assert_store_context=lambda *a, **k: None,
        bind_store_context=lambda *a, **k: None,
        capture_session_factory=lambda *a, **k: None,
        recorder_factory=lambda *a, **k: None,
        crash_trap_factory=lambda *a, **k: None,
        telemetry_sleep=lambda s: None,
        location_quiesce_fn=None,
    )

    spec = {
        "schema_version": "v1",
        "run_id": "run-guest-ax-1",
        "type": "regression",
        "app": "guest_ax_app",
        "device": "vm-01",
        "case_selector": {"ids": ["R1"]},
        "planes": {"ui": {"mode": "required"}, "api": {"mode": "disabled"}},
    }
    run_dir = tmp_path / "runs" / "run-guest-ax-1"
    run_dir.mkdir(parents=True, exist_ok=True)

    # 1) _execute_scoped fails closed before regression/operation/verification starts.
    with pytest.raises(UnsupportedAxBackendError) as exc_scoped:
        run_exec._execute_scoped(spec, profile, "run-guest-ax-1", run_dir, "vm-01")
    assert exc_scoped.value.code == "ax_backend_unsupported"
    assert ax_scope_called is False

    # 2) _screen_execution fails closed before entering execution.
    with pytest.raises(UnsupportedAxBackendError) as exc_screen:
        with run_exec._screen_execution(
            device="vm-01", run_id="run-guest-ax-1", run_dir=run_dir, spec=spec, profile=profile
        ):
            pass
    assert exc_screen.value.code == "ax_backend_unsupported"

    # 3) _execute_phase2_cases fails closed before rig/ax_scope.
    with pytest.raises(UnsupportedAxBackendError) as exc_phase2:
        run_exec._execute_phase2_cases(spec, profile, run_dir)
    assert exc_phase2.value.code == "ax_backend_unsupported"
    assert ax_scope_called is False

    # 4) operation_run startup (`load_operation_spec` & `execute_operation`) fails closed!
    op_spec = {
        "schema_version": "v1",
        "run_id": "run-op-guest-ax",
        "app": "guest_ax_app",
        "operation": {"name": "open_prefs", "with": {}},
    }
    with pytest.raises(UnsupportedAxBackendError) as exc_op_load:
        load_operation_spec(op_spec, profile)
    assert exc_op_load.value.code == "ax_backend_unsupported"

    op_stub = {
        "schema_version": "v1",
        "run_id": "run-op-guest-ax",
        "app": "guest_ax_app",
        "operation": {"name": "open_prefs", "id": "OP1", "revision": 1},
        "summary": "open prefs",
        "args": {},
        "steps": [{"index": 1, "text": "action tap_text query=偏好", "status": "pending"}],
        "status": "pending_execution",
        "detail": "",
    }
    with pytest.raises(UnsupportedAxBackendError) as exc_op_exec:
        execute_operation(
            OperationExecutionRequest(
                operation=op_stub,
                raw_steps=({"action": "tap_text", "query": "偏好"},),
            ),
            OperationExecutionContext(run_dir=run_dir, app="guest_ax_app", root=tmp_path),
        )
    assert exc_op_exec.value.code == "ax_backend_unsupported"


# ── 8. End-to-End RunExecution: Symbol Binding, binding_sha256, Confirmed Claims ──


def _write_build_identity(
    run_dir: Path,
    *,
    app: str = "food_ordering",
    identity_state: str = "confirmed",
    artifact_sha256: str = ARTIFACT_SHA,
) -> None:
    match_state = {
        "confirmed": "exact",
        "ambiguous": "ambiguous",
        "version_only": "version_only",
        "mismatch": "mismatch",
        "unavailable": "unavailable",
    }[identity_state]
    doc = {
        "schema_version": "v2",
        "run_id": run_dir.name,
        "app": app,
        "identity_state": identity_state,
        "identity_source": "combined",
        "subject_build_key": f"build-v1:{artifact_sha256}" if identity_state == "confirmed" else None,
        "device_observation": {
            "bundle_id": "com.autophone.foodorderingdemo.v2",
            "short_version": "1.0.0",
            "build_version": "1",
        },
        "match": {
            "state": match_state,
            "matched_registry_sha256": artifact_sha256 if identity_state == "confirmed" else None,
        },
        "subject_epochs": [
            {
                "epoch_ref": "epoch-1",
                "pid": 4242,
                "bundle_id": "com.autophone.foodorderingdemo.v2",
                "short_version": "1.0.0",
                "build_version": "1",
                "started_at": "2026-11-26T12:00:00Z",
                "source": "uds_info_plist",
                "superseded_at": None,
            }
        ],
        "updated_at": "2026-11-26T12:00:00Z",
        "candidate_identity": None,
    }
    validate_or_raise("build-identity.v2.json", doc)
    (run_dir / "build-identity.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _make_stub_run_execution(tmp_path: Path) -> RunExecution:
    return RunExecution(
        lock=SimpleNamespace(acquire=lambda **k: contextlib.nullcontext()),
        rig_maintenance=SimpleNamespace(),
        executor=None,
        run_dir=lambda rid: tmp_path / "runs" / rid,
        persist_result=lambda d, r: d / "platform-run-result.json",
        set_active=lambda *a, **k: None,
        update_active=lambda *a, **k: None,
        clear_active=lambda *a, **k: None,
        assert_store_context=lambda *a, **k: None,
        bind_store_context=lambda *a, **k: None,
        capture_session_factory=lambda *a, **k: None,
        recorder_factory=lambda *a, **k: None,
        crash_trap_factory=lambda *a, **k: None,
        telemetry_sleep=lambda s: None,
        location_quiesce_fn=None,
    )


@pytest.mark.parametrize("identity_state", ["confirmed", "ambiguous", "version_only"])
def test_run_execution_symbol_binding_propagation_and_confirmed_only_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_state: str,
) -> None:
    """RunExecution resolves symbol binding, propagates binding_sha256 & ax_backend
    into case outcomes/recipe_identity/assembled RunResult, and appends
    symbol.unique_visibility claims ONLY when build-identity is confirmed
    (never on ambiguous/version_only, and never mutating the symbol YAML)."""
    profile, binding, _screen_doc = _make_app_with_symbols(tmp_path, app="food_ordering")
    sym_yaml_bytes_before = binding.path.read_bytes()

    # Author active case R2 with symbol-bound live recipe.
    cases_doc = {
        "schema_version": "v1",
        "app": "food_ordering",
        "cases": [
            {
                "id": "R2",
                "module": "home",
                "item": "open preferences",
                "status": "active",
                "automation": "ui_ready",
                "login_precondition": "any",
                "revision": 1,
            }
        ],
    }
    profile.cases_path().parent.mkdir(parents=True, exist_ok=True)
    profile.cases_path().write_text(yaml.safe_dump(cases_doc, allow_unicode=True), encoding="utf-8")
    live_recipe_path = profile.recipes_dir() / "r2.yaml"
    live_recipe_path.parent.mkdir(parents=True, exist_ok=True)
    live_recipe_bytes = yaml.safe_dump(
        {
            "id": "R2",
            "steps": [
                {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "打开饮食偏好设置"},
                {"action": "assert_present", "queries": ["今晚，好好吃一顿"]},
            ],
        },
        allow_unicode=True,
    ).encode("utf-8")
    live_recipe_path.write_bytes(live_recipe_bytes)
    expected_recipe_sha = hashlib.sha256(live_recipe_bytes).hexdigest()

    run_id = f"run-sym-{identity_state}"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "schema_version": "v1",
        "run_id": run_id,
        "type": "regression",
        "app": "food_ordering",
        "device": "vm-01",
        "build_id": BUILD_ID,
        "case_selector": {"ids": ["R2"]},
        "planes": {"ui": {"mode": "required"}, "api": {"mode": "disabled"}},
    }

    tap_dispatches = _patch_uds_tap_counter(monkeypatch, ocr_frame=_ocr_frame("今晚，好好吃一顿"))
    monkeypatch.setattr(runner_mod, "gate", lambda sock_path, timeout=8.0: (True, "ok"))

    fake_ax = _FakeAxProvider(_dump([_node(identifier="home.ai_prompt.preferences", role="button")]))
    monkeypatch.setattr(
        "autophone.observation.capture.build_run_ax_session",
        lambda prof, rdir, **kw: fake_ax,
    )
    from contextlib import contextmanager

    @contextmanager
    def _fake_ax_scope(provider: Any):
        yield provider

    monkeypatch.setattr("autophone.execution.run_execution.ax_scope", _fake_ax_scope)

    run_exec = _make_stub_run_execution(tmp_path)
    monkeypatch.setattr(
        run_exec,
        "_prepare_rig",
        lambda prof, sdir, **kw: SimpleNamespace(
            availability=lambda: {"vm_uds": True, "app_ui": True, "frida": True},
            to_dict=lambda: {
                "vm_uds": {"available": True},
                "app_ui": {"available": True},
                "frida": {"available": True},
                "health_guard": {"available": True},
            },
            recovery_advice=lambda: "",
        ),
    )
    monkeypatch.setattr(
        run_exec,
        "_probe_build_identity",
        lambda prof, rdir, **kw: _write_build_identity(
            rdir, app="food_ordering", identity_state=identity_state
        ),
    )
    monkeypatch.setattr(run_exec, "_ensure_unlocked", lambda sdir: None)
    monkeypatch.setattr(run_exec, "_launch_app", lambda prof, sdir: None)
    monkeypatch.setattr(
        run_exec,
        "_package_snapshot",
        lambda rdir, sp: {
            "active_slot": "001",
            "active_version": "1.0.0",
            "active_sha256": ARTIFACT_SHA,
            "device_version": "1.0.0",
            "profile_build_label": profile.build_label,
            "registry_build_ref": {
                "app": "food_ordering",
                "slot": "001",
                "build_id": BUILD_ID,
                "artifact_sha256": ARTIFACT_SHA,
                "short_version": "1.0.0",
                "build_version": "1",
            },
            "device_build_observation": None,
            "build_identity_match": "match" if identity_state == "confirmed" else "not_observed",
        },
    )

    result, _cases_path, _case_docs = run_exec._execute_phase2_cases(spec, profile, run_dir)
    assert result.aborted is False
    assert result.ax_backend == "frida_inproc"
    assert len(result.outcomes) == 1
    outcome = result.outcomes[0]
    assert outcome.status == "pass"
    assert outcome.exec_via == "recipe"
    assert outcome.binding_sha256 == binding.binding_sha256
    assert outcome.ax_backend == "frida_inproc"
    assert outcome.recipe_identity is not None
    assert outcome.recipe_identity["recipe_sha256"] == expected_recipe_sha
    assert "unverified_reason" not in outcome.recipe_identity
    assert outcome.recipe_identity["binding_sha256"] == binding.binding_sha256
    assert len(tap_dispatches) == 1

    # Assemble platform RunResult & validate strictly against run-result.v1.json!
    assembled = run_exec.assemble_result(
        spec,
        profile,
        result,
        timestamps={"queued_at": "2026-11-26T00:00:00Z", "collect_ended_at": "2026-11-26T00:00:05Z"},
        phase_status={"provision": {"status": "ok"}, "execute": {"status": "ok"}, "collect": {"status": "ok"}},
    )
    validate_or_raise("run-result.v1.json", assembled)
    assert assembled["ax_backend"] == "frida_inproc"
    assert assembled["planes"]["ui"]["ax_backend"] == "frida_inproc"
    assert assembled["cases"][0]["binding_sha256"] == binding.binding_sha256
    assert assembled["cases"][0]["recipe_identity"]["binding_sha256"] == binding.binding_sha256

    # Immutable symbol YAML bytes MUST NEVER be modified!
    assert binding.path.read_bytes() == sym_yaml_bytes_before

    # External unique_visibility claim recorded ONLY on confirmed build-identity!
    claims_doc = app_symbols.load_symbol_claims(profile)
    reusable = app_symbols.has_reusable_unique_visibility(
        claims_doc,
        symbol_id="ctrl.home.preferences",
        artifact_content_digest=binding.artifact_content_digest,
        binding_sha256=binding.binding_sha256,
        screen_id="S01_home",
        ax_backend="frida_inproc",
    )
    if identity_state == "confirmed":
        assert reusable is True
        assert len(claims_doc["claims"]) == 1
        claim = claims_doc["claims"][0]
        assert claim["scope"]["identity_state"] == "confirmed"
        assert claim["scope"]["ax_backend"] == "frida_inproc"
        assert any(ref.endswith("build-identity.json") for ref in claim["evidence_refs"])
        assert any(ref.endswith(".ax.json") for ref in claim["evidence_refs"])
    else:
        assert reusable is False
        assert claims_doc["claims"] == []


def test_run_execution_draft_trial_m4_confirmation_and_isolation_gate(
    tmp_path: Path,
) -> None:
    """RunExecution._draft_recipe_scope enforces M4 confirmation & isolation using
    existing spec['trial_confirmation'] markers + profile.trial_data_isolation."""
    # Unisolated profile -> blocked even with acknowledged trial_confirmation.
    profile_uniso, binding_uniso, screen_doc = _make_app_with_symbols(
        tmp_path / "uniso",
        app="food_ordering",
        allowed_effects=["pay"],
        trial_data_isolation="unisolated",
    )
    _write_draft_case_and_recipe(profile_uniso, "R3", symbol_id="ctrl.checkout.lower")
    run_exec_uniso = _make_stub_run_execution(tmp_path / "uniso")
    spec_acked = {
        "type": "regression",
        "app": "food_ordering",
        "case_selector": {"ids": ["R3"], "trial_draft_recipes": True},
        "trial_confirmation": [
            {"case_id": "R3", "run_effect": "irreversible", "context_sha256": "0" * 64, "acknowledged": True}
        ],
    }
    with pytest.raises(effects.EffectPolicyError) as exc_uniso:
        with run_exec_uniso._draft_recipe_scope(
            profile_uniso,
            spec_acked["case_selector"],
            spec=spec_acked,
            symbol_binding=binding_uniso,
            screen_asset_doc=screen_doc,
        ):
            pass
    assert exc_uniso.value.code == effects.REASON_TRIAL_DATA_UNISOLATED

    # Isolated profile WITHOUT acknowledged trial_confirmation -> blocked.
    profile_iso, binding_iso, _ = _make_app_with_symbols(
        tmp_path / "iso",
        app="food_ordering",
        allowed_effects=["pay"],
        trial_data_isolation="isolated",
    )
    _write_draft_case_and_recipe(profile_iso, "R3", symbol_id="ctrl.checkout.lower")
    run_exec_iso = _make_stub_run_execution(tmp_path / "iso")
    spec_unacked = {
        "type": "regression",
        "app": "food_ordering",
        "case_selector": {"ids": ["R3"], "trial_draft_recipes": True},
    }
    with pytest.raises(effects.EffectPolicyError) as exc_unacked:
        with run_exec_iso._draft_recipe_scope(
            profile_iso,
            spec_unacked["case_selector"],
            spec=spec_unacked,
            symbol_binding=binding_iso,
            screen_asset_doc=screen_doc,
        ):
            pass
    assert exc_unacked.value.code == effects.REASON_TRIAL_EFFECTS_NOT_CONFIRMED

    # Isolated profile WITH acknowledged trial_confirmation -> admitted & frozen!
    with run_exec_iso._draft_recipe_scope(
        profile_iso,
        spec_acked["case_selector"],
        spec=spec_acked,
        symbol_binding=binding_iso,
        screen_asset_doc=screen_doc,
    ) as identities:
        assert "R3" in identities
        bound = run_exec_iso._bind_draft_recipe_trials(
            profile_iso,
            [{"id": "R3", "status": "active", "automation": "ui_ready", "revision": 1}],
            identities,
            selector=spec_acked["case_selector"],
            symbol_binding=binding_iso,
            screen_asset_doc=screen_doc,
        )
        assert bound[0]["_symbol_binding_sha256"] == binding_iso.binding_sha256
        assert bound[0]["_trial_recipe_document"]["steps"][0]["require_unique"] is True


def test_resolve_run_symbol_binding_reads_candidate_ref_build_id(
    tmp_path: Path,
) -> None:
    """run-spec.v1.json carries candidate build_id under spec['candidate_ref']['build_id']."""
    sha_v2 = "25393f786474b0e85472d0c6e35edef572759ef3b24d0abb5599e1bbe5079e69"
    build_v2 = "bld-25393f786474"
    profile, _binding_v1, _screen_doc = _make_app_with_symbols(tmp_path, app="food_ordering")

    # Add a second build to package manifest & a distinct symbol binding file for sha_v2.
    manifest_path = tmp_path / "packages" / "food_ordering" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version_slots"].append(
        {
            "slot": "002",
            "short_version": "1.1.0",
            "candidate_build_id": build_v2,
            "created_at": "2026-11-26T11:00:00Z",
        }
    )
    manifest["builds"].append(
        {
            "build_id": build_v2,
            "slot": "002",
            "label": None,
            "bundle_id": "com.autophone.foodorderingdemo.v2",
            "build_version": "2",
            "source_revision": {"kind": "unknown", "state": "unknown"},
            "artifact": {"file": f"{build_v2}.ipa", "sha256": sha_v2, "size": 2048},
            "artifact_availability": "present",
            "release_like": True,
            "release_evidence": "embedded_js_bundle:Payload/App.app/main.jsbundle",
            "legacy_slot": None,
            "added_at": "2026-11-26T11:00:00Z",
        }
    )
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    sym_v2_path = profile.profile_dir / "symbols" / "artifacts" / f"{sha_v2[:12]}.yaml"
    sym_v2_doc = {
        "schema": "autophone.app-symbols/1",
        "app": "food_ordering",
        "artifact_identity": {
            "kind": "ipa_build",
            "bundle_id": "com.autophone.foodorderingdemo.v2",
            "short_version": "1.1.0",
            "build_version": "2",
            "build_id": build_v2,
            "artifact_sha256": sha_v2,
        },
        "controls": {
            "ctrl.home.preferences": {
                "kind": "button",
                "scope": {"screen_id": "S01_home"},
                "locator": {
                    "kind": "ax_identifier",
                    "identifier": "home.ai_prompt.preferences.v2",
                    "role": "button",
                    "enabled_only": True,
                    "require_unique": True,
                },
                "effects": {"verbs": [], "completeness": "exhaustive"},
            }
        },
    }
    sym_v2_path.write_text(yaml.safe_dump(sym_v2_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")

    run_dir = tmp_path / "runs" / "run-candidate-ref"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_exec = _make_stub_run_execution(tmp_path)
    spec = {
        "schema_version": "v1",
        "run_id": "run-candidate-ref",
        "type": "regression",
        "app": "food_ordering",
        "device": "vm-01",
        "candidate_ref": {"build_id": build_v2},
    }
    resolved = run_exec._resolve_run_symbol_binding(spec, profile, run_dir)
    assert resolved.build_id == build_v2
    assert resolved.artifact_content_digest == sha_v2
    assert resolved.require_control("ctrl.home.preferences").locator.identifier == "home.ai_prompt.preferences.v2"


def _write_draft_case_and_recipe(profile: Any, case_id: str, *, symbol_id: str) -> None:
    cases_doc = {
        "schema_version": "v1",
        "app": profile.app,
        "cases": [
            {
                "id": case_id,
                "module": "checkout",
                "item": "draft trial symbol",
                "status": "active",
                "automation": "ui_ready",
                "revision": 1,
            }
        ],
    }
    profile.cases_path().parent.mkdir(parents=True, exist_ok=True)
    profile.cases_path().write_text(yaml.safe_dump(cases_doc, allow_unicode=True), encoding="utf-8")
    draft_path = profile.recipes_dir() / "drafts" / f"{case_id.lower()}.yaml"
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(
        yaml.safe_dump(
            {
                "id": case_id,
                "steps": [
                    {"action": "tap_ax", "symbol": symbol_id},
                    {"action": "assert_present", "queries": ["完成"]},
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
