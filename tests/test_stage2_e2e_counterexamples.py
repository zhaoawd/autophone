"""Stage 2 End-to-End Counterexamples (专项二).

Proves all 6 Stage 2 counterexamples + the full positive E2E flow on
``examples/food-ordering`` (R5) through the real runner, host-enriched
``CaptureHandle`` + ``net_capture.merge``, ``entity_readback``,
``finalize_api_plane``, ``arbitrate_joint_verdict``, ``RunResultAssembler``,
``negative_control`` + ``promotion``, and ``case lint``:

1. CE#1: Right screen + right control ID, wrong business entity (store-flagship-a
   expected, store-branch-b in API / readback) -> caught fail-closed
   (``entity_api_mismatch`` / ``entity_readback_mismatch``).
2. CE#2: Optimistic UI refresh (UI passes!) while backend API returns 500 or wrong
   write payload -> caught fail-closed (``optimistic_ui_api_failed``).
3. CE#3: Optimistic UI + API 200, post-action ``entity_readback`` observes
   unpersisted/divergent state -> caught fail-closed (``entity_readback_mismatch``)
   with redacted, digest-bound ``entity-readback.json``.
4. CE#4: Missing API capture on API-required ``entity_binding`` case -> fail-closed
   (``api_evidence_missing``), never silent pass.
5. CE#5: Interaction window isolation (``body_end`` / ``symbol_id`` /
   ``entity_readback`` / ``post_case_cleanup`` marks stop post-body readback or
   teardown HTTP calls from falsely satisfying the interaction window).
6. CE#6: Negative control with ``expect.plane: "joint"`` proves joint-verdict teeth
   (``teeth_at="joint"``, ``counted=True``, accepted by ``promotion``) AND catches
   toothless weak joint assertions (``teeth_at="none"``, ``discrimination_toothless``).
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from autophone.application.cases import CaseRecipeApplication
from autophone.assets import app_symbols
from autophone.assets.apps import create_app_profile, load_app_profile
from autophone.assets.entity_binding import validate_case_entity_binding
from autophone.assets.negative_control import (
    VERDICT_COUNTED,
    VERDICT_PASSED,
    declaration_sha256,
    judge_negative_control,
)
from autophone.assets.operations import load_operations
from autophone.assets.promotion import discrimination_gaps
from autophone.assets.screens import load_screen_asset
from autophone.contracts import validate_or_raise
from autophone.device.ax_target import AxDump, Node
from autophone.execution import effects as effects_mod
from autophone.execution import entity_readback as entity_rb
from autophone.execution.engine import actions as actions_mod
from autophone.execution.engine import cases_loader
from autophone.execution.engine import recipe as recipe_mod
from autophone.execution.engine import runner as runner_mod
from autophone.execution.negative_control import (
    NEGATIVE_CONTROL_STEPS_KEY,
    attach_negative_control_records,
    bind_negative_control,
)
from autophone.observation.frida import api_oracle
from autophone.observation import capture as capture_mod
from autophone.observation.frida import capture_session
from autophone.observation.run_result import RunResultAssembler

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_ROOT = REPO_ROOT / "examples" / "food-ordering"
BUILD_LABEL = "1.0.0-bld-14282f675363"
BUILD_ID_V1 = "bld-14282f675363"
SHA_V1 = "14282f675363a9d74361c9b5d24dcde461648df2a13cf9aa4488d0aad4f68d58"


def _ocr_frame(*texts: str) -> dict[str, Any]:
    return {
        "image_w": 1290,
        "image_h": 2796,
        "boxes": [
            {"text": t, "x": 100, "y": 140 + i * 110, "w": 240, "h": 50, "confidence": 0.99}
            for i, t in enumerate(texts)
        ],
    }


def _ax_dump() -> AxDump:
    return AxDump(
        app="com.autophone.foodorderingdemo.v2",
        scale=3.0,
        w=430.0,
        h=932.0,
        nodes=[
            Node(
                role="button",
                label="调偏好",
                traits=["button"],
                frame=(100.0, 200.0, 120.0, 44.0),
                enabled=True,
                value=None,
                identifier="home.ai_prompt.preferences",
                path="0/0",
                native_x=480,
                native_y=666,
                nx=0.37,
                ny=0.24,
                hittable=True,
            ),
            Node(
                role="button",
                label="保存偏好",
                traits=["button"],
                frame=(100.0, 500.0, 120.0, 44.0),
                enabled=True,
                value=None,
                identifier="prefs.save.button",
                path="0/1",
                native_x=480,
                native_y=1566,
                nx=0.37,
                ny=0.56,
                hittable=True,
            ),
        ],
        ax_backend="frida_inproc",
        application_state=0,
        key_window_scene_activation_state=0,
    )


class _FakeAxProvider:
    ax_backend = "frida_inproc"

    def __init__(self, dump: AxDump) -> None:
        self._dump = dump

    def refresh(self) -> AxDump:
        return self._dump


def _json_b64(payload: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")


class _SimulatedCaptureHarness:
    """Host CaptureHandle + guest 5-arg mark simulator driving real net_capture.merge."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._ts = 1000
        self._task_seq = 0
        self.handle = capture_session.CaptureHandle(self._GuestExports(self), self.events)

    class _GuestExports:
        def __init__(self, outer: "_SimulatedCaptureHarness") -> None:
            self._outer = outer

        def mark(self, case_id, step, operation, action_id, subject_epoch_ref) -> None:
            self._outer._ts += 100
            self._outer.events.append({
                "ev": "mark",
                "case_id": case_id,
                "step": step,
                "operation": operation,
                "action_id": action_id,
                "subject_epoch_ref": subject_epoch_ref,
                "ts": self._outer._ts,
            })

    def emit_http(
        self,
        *,
        url: str,
        method: str = "GET",
        status: int = 200,
        req_body: dict[str, Any] | None = None,
        resp_body: dict[str, Any] | None = None,
        req_headers: dict[str, str] | None = None,
        resp_headers: dict[str, str] | None = None,
        resp_error: str | None = None,
    ) -> None:
        self._task_seq += 1
        tp = f"0x{self._task_seq:x}"
        self._ts += 10
        req_ev: dict[str, Any] = {
            "ev": "req",
            "task_ptr": tp,
            "ts": self._ts,
            "req": {
                "method": method,
                "url": url,
                "host": "api.food.example",
                "headers": req_headers or {},
            },
        }
        if req_body is not None:
            req_ev["body_b64"] = _json_b64(req_body)
        self._ts += 10
        resp_ev: dict[str, Any] = {
            "ev": "resp",
            "task_ptr": tp,
            "ts": self._ts,
            "status": status,
            "resp_headers": resp_headers or {"Content-Type": "application/json"},
            "error": resp_error,
        }
        if resp_body is not None:
            resp_ev["body_b64"] = _json_b64(resp_body)
        self.events.extend([req_ev, resp_ev])

    def merged(self) -> list[dict[str, Any]]:
        return self.handle.merged()


def _patch_device(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ocr_sequence: list[dict[str, Any]],
    on_tap: Any = None,
) -> None:
    png_b64 = base64.b64encode(b"\xff\xd8\xff\xe0FAKEJPEG\xff\xd9").decode("ascii")
    idx = {"n": 0}

    def fake_request(msg: dict[str, Any], **_kw: Any):
        if msg.get("t") == "screenshot":
            return {"ok": True, "image": png_b64}, SimpleNamespace(
                image_w=1290, image_h=2796, image_fmt="jpeg", screen_w=430, screen_h=932, scale=3.0,
            )
        if msg.get("t") == "tap":
            if callable(on_tap):
                on_tap(msg)
            return {"ok": True}, None
        return {"ok": True}, None

    def fake_ocr(_path: Any):
        cur = min(idx["n"], len(ocr_sequence) - 1)
        idx["n"] += 1
        return ocr_sequence[cur]

    monkeypatch.setattr(actions_mod, "request", fake_request)
    monkeypatch.setattr(actions_mod, "run_ocr", fake_ocr)
    monkeypatch.setattr(actions_mod.CaseContext, "_settle", lambda self: None)
    monkeypatch.setattr(runner_mod, "gate", lambda sock_path, timeout=8.0: (True, "ok"))


def _load_food_ordering_r5(tmp_path: Path):
    profile = load_app_profile("food_ordering", root=EXAMPLE_ROOT, state_root=tmp_path)
    screen_doc = load_screen_asset(profile.screens_path())
    binding = app_symbols.load_symbol_binding(profile, artifact_content_digest=SHA_V1)
    cases = cases_loader.load_cases(profile.cases_path())
    r5 = next(c for c in cases if c["id"] == "R5")
    return profile, screen_doc, binding, r5


def _register_readback_op(
    monkeypatch: pytest.MonkeyPatch,
    fn: Any,
    *,
    name: str = "read_preference_profile",
) -> None:
    monkeypatch.setitem(recipe_mod.OPERATIONS, name, fn)
    monkeypatch.setitem(
        effects_mod._EXECUTOR_EFFECTS,
        name,
        effects_mod.EffectDecision(effects_mod.REVERSIBLE, name, "read-only entity readback"),
    )


def _assemble_and_validate(run_id: str, app: str, phase2: runner_mod.RunResult, *, api_mode: str = "advisory") -> dict[str, Any]:
    spec = {
        "run_id": run_id,
        "type": "regression",
        "app": app,
        "device": "vm-test",
        "planes": {"ui": {"mode": "required"}, "api": {"mode": api_mode}},
    }
    assembled = RunResultAssembler().assemble(
        spec,
        phase2,
        package={
            "active_slot": None,
            "active_version": None,
            "active_sha256": None,
            "device_version": None,
            "profile_build_label": None,
        },
        timestamps={"queued_at": "2026-11-27T00:00:00Z", "collect_ended_at": "2026-11-27T00:00:10Z"},
        phase_status={"provision": {"status": "ok"}, "execute": {"status": "ok"}, "collect": {"status": "ok"}},
    )
    validate_or_raise("run-result.v1.json", assembled)
    return assembled


# ── Positive E2E + examples/food-ordering lint ───────────────────────────────


def test_stage2_positive_e2e_on_food_ordering_r5_and_case_lint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full positive Stage 2 E2E on examples/food-ordering R5: UI + window-scoped API
    + post-action entity_readback all agree on store-flagship-a, and case lint has 0 findings."""
    profile, screen_doc, binding, r5 = _load_food_ordering_r5(tmp_path)

    # `case lint`, `load_operations`, and `validate_case_entity_binding` on examples/food-ordering
    # all pass with 0 errors & 0 warnings!
    ops_by_name = load_operations(profile)
    assert "read_preference_profile" in ops_by_name, ops_by_name
    assert (ops_by_name["read_preference_profile"].get("navigation") or {}).get("safety") == "read_only"
    r5_fixtures = api_oracle.flat_fixture_vars(profile.api_fixtures_path(), "R5")
    binding_issues = validate_case_entity_binding(profile, r5, fixture_vars=r5_fixtures)
    assert binding_issues == [], binding_issues

    lint_report = CaseRecipeApplication(profile).lint()
    assert lint_report["error_count"] == 0, lint_report
    assert lint_report["warning_count"] == 0, lint_report

    harness = _SimulatedCaptureHarness()
    _register_readback_op(
        monkeypatch,
        lambda ctx, store_id: {
            "store_id": store_id,
            "store_name": "旗舰店-A",
            "preference_profile": "少冰少糖-VIP",
        },
    )
    _patch_device(
        monkeypatch,
        ocr_sequence=[
            _ocr_frame("今晚，好好吃一顿", "立即下单", "调偏好"),
            _ocr_frame("饮食偏好", "保存偏好", "旗舰店-A", "少冰少糖-VIP"),
        ],
        on_tap=lambda _msg: harness.emit_http(
            url="https://api.food.example/api/v1/preferences?store_id=store-flagship-a",
            method="GET",
            status=200,
            resp_body={
                "data": {
                    "store_id": "store-flagship-a",
                    "store_name": "旗舰店-A",
                    "preference_profile": "少冰少糖-VIP",
                }
            },
        ),
    )

    steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "打开饮食偏好页"},
        {"action": "assert_present", "queries": ["饮食偏好", "保存偏好"], "wait_s": 0},
    ]
    run_dir = tmp_path / "run_pos"
    with recipe_mod.recipe_scope("food_ordering", root=EXAMPLE_ROOT, overrides={"R5": steps}):
        outcome = runner_mod.run_case(
            r5,
            run_dir / "shots",
            str(tmp_path / "absent.sock"),
            10.0,
            capture=harness.handle,
            ax=_FakeAxProvider(_ax_dump()),
            screen_asset_doc=screen_doc,
            symbol_binding=binding,
            api_fixtures_path=profile.api_fixtures_path(),
        )
    phase2 = runner_mod.RunResult(aborted=False, abort_reason="", outcomes=[outcome])
    finalized = capture_mod.finalize_api_plane(
        run_dir,
        harness.merged(),
        phase2,
        app="food_ordering",
        cases=[r5],
        profile=profile,
        api_captured=True,
    )
    out = finalized.run_result.outcomes[0]
    assert out.status == "pass", out.detail
    assert out.joint_verdict["status"] == "pass"
    assert out.joint_verdict["planes"] == {"ui": "pass", "api": "pass", "readback": "pass"}
    rb_path = entity_rb.write_entity_readback_evidence(run_dir, "run-pos", finalized.run_result.outcomes)
    assert rb_path is not None and rb_path.is_file()

    assembled = _assemble_and_validate("run-pos", "food_ordering", finalized.run_result)
    assert assembled["status"] == "passed"
    assert assembled["cases"][0]["joint_verdict"]["entity_id"] == "store-flagship-a"


# ── CE #1: Right screen + right control ID, wrong business entity ────────────


def test_ce1_right_screen_and_control_wrong_business_entity_caught_by_joint_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CE#1: Screen S02_preferences and symbol ctrl.home.preferences both match,
    UI asserts only screen structure and stays green, but API & readback return
    store-branch-b instead of store-flagship-a -> caught by joint verdict."""
    profile, screen_doc, binding, r5 = _load_food_ordering_r5(tmp_path)
    harness = _SimulatedCaptureHarness()
    _register_readback_op(
        monkeypatch,
        lambda ctx, store_id: {
            "store_id": "store-branch-b",
            "store_name": "分店-B",
            "preference_profile": "微辣-普通",
        },
    )
    _patch_device(
        monkeypatch,
        ocr_sequence=[
            _ocr_frame("今晚，好好吃一顿", "立即下单", "调偏好"),
            _ocr_frame("饮食偏好", "保存偏好", "分店-B"),
        ],
        on_tap=lambda _msg: harness.emit_http(
            url="https://api.food.example/api/v1/preferences?store_id=store-branch-b",
            method="GET",
            status=200,
            resp_body={
                "data": {
                    "store_id": "store-branch-b",
                    "store_name": "分店-B",
                    "preference_profile": "微辣-普通",
                }
            },
        ),
    )

    steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "打开饮食偏好页"},
        {"action": "assert_present", "queries": ["饮食偏好", "保存偏好"], "wait_s": 0},
    ]
    run_dir = tmp_path / "run_ce1"
    with recipe_mod.recipe_scope("food_ordering", root=EXAMPLE_ROOT, overrides={"R5": steps}):
        outcome = runner_mod.run_case(
            r5,
            run_dir / "shots",
            str(tmp_path / "absent.sock"),
            10.0,
            capture=harness.handle,
            ax=_FakeAxProvider(_ax_dump()),
            screen_asset_doc=screen_doc,
            symbol_binding=binding,
            api_fixtures_path=profile.api_fixtures_path(),
        )
    # UI alone passed!
    assert outcome.status == "pass"

    finalized = capture_mod.finalize_api_plane(
        run_dir,
        harness.merged(),
        runner_mod.RunResult(aborted=False, abort_reason="", outcomes=[outcome]),
        app="food_ordering",
        cases=[r5],
        profile=profile,
        api_captured=True,
    )
    out = finalized.run_result.outcomes[0]
    assert out.ui_status == "pass"
    assert out.status == "fail"
    assert out.fail_kind == "entity_api_mismatch"
    assert out.joint_verdict["planes"] == {"ui": "pass", "api": "fail", "readback": "fail"}
    assert "entity_api_mismatch" in out.joint_verdict["mismatch_reasons"]
    assert "entity_readback_mismatch" in out.joint_verdict["mismatch_reasons"]

    assembled = _assemble_and_validate("run-ce1", "food_ordering", finalized.run_result)
    assert assembled["status"] == "failed"
    assert assembled["cases"][0]["status"] == "failed"
    assert assembled["cases"][0]["planes"]["ui"]["verdict"] == "pass"
    assert assembled["cases"][0]["planes"]["api"]["verdict"] == "fail"
    assert assembled["cases"][0]["planes"]["readback"]["verdict"] == "fail"


# ── CE #2: Optimistic UI refresh + backend API 500 or wrong write payload ────


@pytest.mark.parametrize("backend_defect", ["http_500", "wrong_write_payload"])
def test_ce2_optimistic_ui_green_while_backend_api_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend_defect: str,
) -> None:
    """CE#2: UI optimistically renders '保存成功' and passes, while the backend
    write API returns HTTP 500 or persists the wrong write payload -> caught."""
    profile, screen_doc, binding, _r5 = _load_food_ordering_r5(tmp_path)
    harness = _SimulatedCaptureHarness()

    case_save = {
        "id": "R5",
        "module": "饮食偏好",
        "item": "乐观 UI 假绿拦截",
        "entity_binding": {
            "entity_type": "store",
            "entity_id": {"from_fixture": "expected_store_id"},
            "attributes": {"preference_profile": {"from_fixture": "expected_preference_profile"}},
            "api_window": {
                "window": {"symbol_id": "ctrl.home.preferences"},
                "expect": [
                    {
                        "name": "save_preference",
                        "method": "POST",
                        "path": "/api/v1/preferences/save",
                        "assert": {
                            "status": 200,
                            "resp_error": {"op": "empty"},
                            "req.store_id": "${entity.id}",
                            "req.saved_flag": True,
                            "resp.code": 0,
                        },
                    }
                ],
            },
            "joint_verdict": {"require_ui": True, "require_api": True, "require_readback": False},
        },
    }

    def on_tap(_msg: dict[str, Any]) -> None:
        if backend_defect == "http_500":
            harness.emit_http(
                url="https://api.food.example/api/v1/preferences/save",
                method="POST",
                status=500,
                req_body={"store_id": "store-flagship-a", "saved_flag": True},
                resp_body={"code": 500, "msg": "internal error"},
            )
        else:
            harness.emit_http(
                url="https://api.food.example/api/v1/preferences/save",
                method="POST",
                status=200,
                req_body={"store_id": "store-flagship-a", "saved_flag": False},
                resp_body={"code": 0},
            )

    _patch_device(
        monkeypatch,
        ocr_sequence=[
            _ocr_frame("今晚，好好吃一顿", "立即下单", "调偏好"),
            _ocr_frame("饮食偏好", "保存成功"),
        ],
        on_tap=on_tap,
    )
    steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "触发保存"},
        {"action": "assert_present", "queries": ["保存成功"], "wait_s": 0},
    ]
    run_dir = tmp_path / f"run_ce2_{backend_defect}"
    with recipe_mod.recipe_scope("food_ordering", root=EXAMPLE_ROOT, overrides={"R5": steps}):
        outcome = runner_mod.run_case(
            case_save,
            run_dir / "shots",
            str(tmp_path / "absent.sock"),
            10.0,
            capture=harness.handle,
            ax=_FakeAxProvider(_ax_dump()),
            screen_asset_doc=screen_doc,
            symbol_binding=binding,
            api_fixtures_path=profile.api_fixtures_path(),
        )
    assert outcome.status == "pass"

    finalized = capture_mod.finalize_api_plane(
        run_dir,
        harness.merged(),
        runner_mod.RunResult(aborted=False, abort_reason="", outcomes=[outcome]),
        app="food_ordering",
        cases=[case_save],
        profile=profile,
        api_captured=True,
    )
    out = finalized.run_result.outcomes[0]
    assert out.ui_status == "pass"
    assert out.status == "fail"
    assert out.fail_kind == "optimistic_ui_api_failed"
    assert "optimistic_ui_api_failed" in out.joint_verdict["mismatch_reasons"]

    assembled = _assemble_and_validate(f"run-ce2-{backend_defect}", "food_ordering", finalized.run_result)
    assert assembled["status"] == "failed"
    assert assembled["cases"][0]["planes"]["ui"]["verdict"] == "pass"
    assert assembled["cases"][0]["planes"]["api"]["verdict"] == "fail"


# ── CE #3: Optimistic UI + API 200, post-action entity_readback diverges ─────


def test_ce3_ui_and_api_200_but_post_action_readback_unpersisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CE#3: UI passes and write API returns 200 with matching payload, yet
    post-action entity_readback sees stale/unpersisted backend state -> caught."""
    profile, screen_doc, binding, r5 = _load_food_ordering_r5(tmp_path)
    harness = _SimulatedCaptureHarness()
    _register_readback_op(
        monkeypatch,
        lambda ctx, store_id: {
            "store_id": store_id,
            "store_name": "旗舰店-A",
            # Write was dropped / not persisted!
            "preference_profile": "默认-未持久化",
        },
    )
    _patch_device(
        monkeypatch,
        ocr_sequence=[
            _ocr_frame("今晚，好好吃一顿", "立即下单", "调偏好"),
            _ocr_frame("饮食偏好", "保存偏好", "旗舰店-A", "少冰少糖-VIP"),
        ],
        on_tap=lambda _msg: harness.emit_http(
            url="https://api.food.example/api/v1/preferences?store_id=store-flagship-a",
            method="GET",
            status=200,
            resp_body={
                "data": {
                    "store_id": "store-flagship-a",
                    "store_name": "旗舰店-A",
                    "preference_profile": "少冰少糖-VIP",
                }
            },
        ),
    )
    steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "打开饮食偏好页"},
        {"action": "assert_present", "queries": ["饮食偏好", "保存偏好"], "wait_s": 0},
    ]
    run_dir = tmp_path / "run_ce3"
    with recipe_mod.recipe_scope("food_ordering", root=EXAMPLE_ROOT, overrides={"R5": steps}):
        outcome = runner_mod.run_case(
            r5,
            run_dir / "shots",
            str(tmp_path / "absent.sock"),
            10.0,
            capture=harness.handle,
            ax=_FakeAxProvider(_ax_dump()),
            screen_asset_doc=screen_doc,
            symbol_binding=binding,
            api_fixtures_path=profile.api_fixtures_path(),
        )
    assert outcome.entity_readback is not None
    assert outcome.entity_readback["status"] == "fail"
    assert outcome.entity_readback["reason"] == entity_rb.REASON_EXPECT_MISMATCH

    finalized = capture_mod.finalize_api_plane(
        run_dir,
        harness.merged(),
        runner_mod.RunResult(aborted=False, abort_reason="", outcomes=[outcome]),
        app="food_ordering",
        cases=[r5],
        profile=profile,
        api_captured=True,
    )
    out = finalized.run_result.outcomes[0]
    assert out.ui_status == "pass"
    assert out.status == "fail"
    assert out.fail_kind == "entity_readback_mismatch"
    assert out.joint_verdict["planes"] == {"ui": "pass", "api": "pass", "readback": "fail"}

    rb_file = entity_rb.write_entity_readback_evidence(run_dir, "run-ce3", finalized.run_result.outcomes)
    assert rb_file is not None and rb_file.is_file()
    rb_doc = json.loads(rb_file.read_text(encoding="utf-8"))
    failed_checks = [c for c in rb_doc["cases"]["R5"]["checks"] if not c["ok"]]
    assert len(failed_checks) == 1 and failed_checks[0]["key"] == "preference_profile"

    assembled = _assemble_and_validate("run-ce3", "food_ordering", finalized.run_result)
    assert assembled["status"] == "failed"
    assert assembled["cases"][0]["planes"]["ui"]["verdict"] == "pass"
    assert assembled["cases"][0]["planes"]["api"]["verdict"] == "pass"
    assert assembled["cases"][0]["planes"]["readback"]["verdict"] == "fail"


# ── CE #4: Missing API capture on API-required entity_binding case ───────────


def test_ce4_missing_api_capture_fails_closed_never_silent_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CE#4: UI and readback both pass, but API capture did not happen ->
    finalize_api_plane(api_captured=False) fails closed with api_evidence_missing."""
    profile, screen_doc, binding, r5 = _load_food_ordering_r5(tmp_path)
    _register_readback_op(
        monkeypatch,
        lambda ctx, store_id: {
            "store_id": store_id,
            "store_name": "旗舰店-A",
            "preference_profile": "少冰少糖-VIP",
        },
    )
    _patch_device(
        monkeypatch,
        ocr_sequence=[
            _ocr_frame("今晚，好好吃一顿", "立即下单", "调偏好"),
            _ocr_frame("饮食偏好", "保存偏好", "旗舰店-A", "少冰少糖-VIP"),
        ],
    )
    steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "打开饮食偏好页"},
        {"action": "assert_present", "queries": ["饮食偏好", "保存偏好"], "wait_s": 0},
    ]
    run_dir = tmp_path / "run_ce4"
    with recipe_mod.recipe_scope("food_ordering", root=EXAMPLE_ROOT, overrides={"R5": steps}):
        outcome = runner_mod.run_case(
            r5,
            run_dir / "shots",
            str(tmp_path / "absent.sock"),
            10.0,
            capture=None,
            ax=_FakeAxProvider(_ax_dump()),
            screen_asset_doc=screen_doc,
            symbol_binding=binding,
            api_fixtures_path=profile.api_fixtures_path(),
        )
    finalized = capture_mod.finalize_api_plane(
        run_dir,
        [],
        runner_mod.RunResult(aborted=False, abort_reason="", outcomes=[outcome]),
        app="food_ordering",
        cases=[r5],
        profile=profile,
        api_captured=False,
    )
    out = finalized.run_result.outcomes[0]
    assert out.ui_status == "pass"
    assert out.status == "fail"
    assert out.fail_kind == "api_evidence_missing"
    assert out.joint_verdict["planes"] == {"ui": "pass", "api": "missing", "readback": "pass"}
    assert not (run_dir / "planes" / "api" / "capture.jsonl").exists()

    assembled = _assemble_and_validate("run-ce4", "food_ordering", finalized.run_result, api_mode="required")
    assert assembled["status"] == "failed"
    assert assembled["cases"][0]["status"] == "failed"
    assert assembled["cases"][0]["joint_verdict"]["fail_kind"] == "api_evidence_missing"


# ── CE #5: Interaction window isolation (body_end / entity_readback / teardown) ─


def test_ce5_interaction_window_isolation_blocks_readback_and_teardown_http_bleed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CE#5: If the UI tap emitted NO matching HTTP call during the body, a
    matching GET /api/v1/preferences made later by entity_readback or teardown
    MUST NOT leak across body_end/entity_readback/symbol_id to satisfy api_window."""
    profile, screen_doc, binding, r5 = _load_food_ordering_r5(tmp_path)
    harness = _SimulatedCaptureHarness()

    def readback_that_makes_matching_http(ctx: actions_mod.CaseContext, store_id: str) -> dict[str, Any]:
        # This HTTP call matches the URL & JSON body of R5's api_window.expect,
        # and even attempts to emit a symbol-scoped tap_ax mark inside readback;
        # the post-body housekeeping window lock MUST force window_tag="entity_readback"
        # and strip symbol_id so it cannot spoof the body window or leak into windowless api_window!
        ctx.api_mark(
            "tap_ax",
            symbol_id="ctrl.home.preferences",
            window_tag="symbol:ctrl.home.preferences",
        )
        harness.emit_http(
            url="https://api.food.example/api/v1/preferences?store_id=store-flagship-a",
            method="GET",
            status=200,
            resp_body={
                "data": {
                    "store_id": "store-flagship-a",
                    "store_name": "旗舰店-A",
                    "preference_profile": "少冰少糖-VIP",
                }
            },
        )
        return {
            "store_id": store_id,
            "store_name": "旗舰店-A",
            "preference_profile": "少冰少糖-VIP",
        }

    def teardown_that_makes_matching_http(ctx: actions_mod.CaseContext) -> None:
        harness.emit_http(
            url="https://api.food.example/api/v1/preferences?store_id=store-flagship-a",
            method="GET",
            status=200,
            resp_body={
                "data": {
                    "store_id": "store-flagship-a",
                    "store_name": "旗舰店-A",
                    "preference_profile": "少冰少糖-VIP",
                }
            },
        )

    _register_readback_op(monkeypatch, readback_that_makes_matching_http)
    monkeypatch.setitem(recipe_mod.OPERATIONS, "cleanup_prefs", teardown_that_makes_matching_http)
    monkeypatch.setitem(
        effects_mod._EXECUTOR_EFFECTS,
        "cleanup_prefs",
        effects_mod.EffectDecision(effects_mod.REVERSIBLE, "cleanup_prefs", "cleanup"),
    )

    # Notice on_tap emits ZERO HTTP calls during the body!
    _patch_device(
        monkeypatch,
        ocr_sequence=[
            _ocr_frame("今晚，好好吃一顿", "立即下单", "调偏好"),
            _ocr_frame("饮食偏好", "保存偏好", "旗舰店-A", "少冰少糖-VIP"),
        ],
    )
    case_iso = {**r5, "teardown": [{"op": "cleanup_prefs"}]}
    steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "打开饮食偏好页"},
        {"action": "assert_present", "queries": ["饮食偏好", "保存偏好"], "wait_s": 0},
    ]
    run_dir = tmp_path / "run_ce5"
    with recipe_mod.recipe_scope("food_ordering", root=EXAMPLE_ROOT, overrides={"R5": steps}):
        outcome = runner_mod.run_case(
            case_iso,
            run_dir / "shots",
            str(tmp_path / "absent.sock"),
            10.0,
            capture=harness.handle,
            ax=_FakeAxProvider(_ax_dump()),
            screen_asset_doc=screen_doc,
            symbol_binding=binding,
            api_fixtures_path=profile.api_fixtures_path(),
        )
    merged = harness.merged()
    assert len(merged) == 2
    assert merged[0]["operation"] == "read_preference_profile"
    assert merged[0]["window_tag"] == "entity_readback"
    assert "symbol_id" not in merged[0]
    assert merged[1]["operation"] == "cleanup_prefs"
    assert merged[1]["window_tag"] == "post_case_cleanup"
    assert "symbol_id" not in merged[1]

    # 1) With explicit window.symbol_id filter: readback & teardown calls cannot spoof the symbol window.
    finalized = capture_mod.finalize_api_plane(
        run_dir,
        merged,
        runner_mod.RunResult(aborted=False, abort_reason="", outcomes=[copy.deepcopy(outcome)]),
        app="food_ordering",
        cases=[case_iso],
        profile=profile,
        api_captured=True,
    )
    out = finalized.run_result.outcomes[0]
    assert out.status == "fail"
    assert out.fail_kind == "optimistic_ui_api_failed"
    assert "api_expected_call_missing" in out.joint_verdict["mismatch_reasons"]
    assert out.joint_verdict["planes"] == {"ui": "pass", "api": "fail", "readback": "pass"}

    # 2) Hole 3 regression: EVEN WITH NO window.symbol_id filter on api_window,
    #    post-body readback/teardown HTTP calls (window_tag in POST_BODY_HOUSEKEEPING_STEPS)
    #    are strictly excluded and CANNOT falsely satisfy the case's api_window!
    case_windowless = copy.deepcopy(case_iso)
    case_windowless["entity_binding"]["api_window"].pop("window", None)
    finalized_windowless = capture_mod.finalize_api_plane(
        tmp_path / "run_ce5_windowless",
        merged,
        runner_mod.RunResult(aborted=False, abort_reason="", outcomes=[copy.deepcopy(outcome)]),
        app="food_ordering",
        cases=[case_windowless],
        profile=profile,
        api_captured=True,
    )
    out_wl = finalized_windowless.run_result.outcomes[0]
    assert out_wl.status == "fail"
    assert out_wl.fail_kind == "optimistic_ui_api_failed"
    assert "api_expected_call_missing" in out_wl.joint_verdict["mismatch_reasons"]
    assert out_wl.joint_verdict["planes"] == {"ui": "pass", "api": "fail", "readback": "pass"}


# ── CE #6: Negative control with expect.plane="joint" (teeth & toothless) ────


def _confirmed_package_payload() -> dict[str, Any]:
    return {
        "active_slot": "001",
        "active_version": "1.0.0",
        "active_sha256": SHA_V1,
        "device_version": "1.0.0",
        "profile_build_label": BUILD_LABEL,
        "build_identity_match": "match",
        "comparison_basis": "installed_build_ref",
        "comparison_build_id": BUILD_ID_V1,
        "comparison_granularity": "build_marker",
        "registry_build_ref": {
            "app": "joint_negctl_app",
            "slot": "001",
            "build_id": BUILD_ID_V1,
            "short_version": "1.0.0",
            "build_version": "1",
            "artifact_sha256": SHA_V1,
            "source_revision": {"kind": "unknown", "state": "unknown"},
        },
        "device_build_observation": {
            "bundle_id": "com.example.joint",
            "short_version": "1.0.0",
            "build_version": "1",
            "observed_at": "2026-11-27T00:00:00Z",
        },
    }


def test_ce6_negative_control_joint_plane_teeth_and_toothless_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CE#6: End-to-end negative control with expect.plane='joint' where UI stays
    green under perturbation:
    - Strong joint entity assertion catches skipped interaction via API/readback
      -> discriminator_flipped=True, teeth_at='joint', counted=True, passes promotion!
    - Toothless weak joint assertion still passes under perturbation ->
      negative_verdict='passed_unexpectedly', teeth_at='none', counted=False,
      blocked by promotion with discrimination_toothless!"""
    profile = create_app_profile(
        "joint_negctl_app",
        "com.example.joint",
        "Joint NegCtl",
        root=tmp_path,
    )
    profile_yaml = profile.profile_dir / "profile.yaml"
    prof_raw = yaml.safe_load(profile_yaml.read_text(encoding="utf-8"))
    prof_raw["build_label"] = BUILD_LABEL
    profile_yaml.write_text(yaml.safe_dump(prof_raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    profile = load_app_profile("joint_negctl_app", root=tmp_path)
    # Write screens.yaml + symbols artifact matching SHA_V1
    screen_doc = load_screen_asset(
        EXAMPLE_ROOT / "apps" / "food_ordering" / "screens" / f"{BUILD_LABEL}.yaml"
    )
    profile.screens_path().parent.mkdir(parents=True, exist_ok=True)
    profile.screens_path().write_text(
        yaml.safe_dump(screen_doc, allow_unicode=True, sort_keys=False), encoding="utf-8",
    )
    sym_doc = yaml.safe_load(
        (EXAMPLE_ROOT / "apps" / "food_ordering" / "symbols" / "artifacts" / f"{SHA_V1[:12]}.yaml").read_text(encoding="utf-8")
    )
    sym_doc["app"] = "joint_negctl_app"
    sym_path = profile.profile_dir / "symbols" / "artifacts" / f"{SHA_V1[:12]}.yaml"
    sym_path.parent.mkdir(parents=True, exist_ok=True)
    sym_path.write_text(yaml.safe_dump(sym_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    binding = app_symbols.load_symbol_binding(profile, artifact_content_digest=SHA_V1)

    base_steps = [
        {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "进入偏好"},
        {"action": "assert_present", "queries": ["今晚，好好吃一顿"], "wait_s": 0},
    ]
    recipe_bytes = yaml.safe_dump({"id": "R1", "steps": base_steps}, allow_unicode=True, sort_keys=False).encode("utf-8")
    profile.recipes_dir().mkdir(parents=True, exist_ok=True)
    (profile.recipes_dir() / "r1.yaml").write_text(recipe_bytes.decode("utf-8"), encoding="utf-8")
    recipe_sha = hashlib.sha256(recipe_bytes).hexdigest()

    strong_case = {
        "id": "R1",
        "revision": 1,
        "status": "active",
        "automation": "ui_ready",
        "case_type": "功能测试",
        "test_point": "强联合裁决负对照",
        "owner": "qa",
        "module": "偏好",
        "item": "跳过符号点击后 UI 仍绿但联合裁决翻转",
        "login_precondition": "any",
        "steps": ["点击调偏好", "检查页面"],
        "expects": ["交互窗口 API 必须发生且命中目标门店"],
        "entity_binding": {
            "entity_type": "store",
            "entity_id": "store-flagship-a",
            "api_window": {
                "window": {"symbol_id": "ctrl.home.preferences"},
                "expect": [
                    {
                        "name": "load_prefs",
                        "method": "GET",
                        "path": "/api/v1/preferences",
                        "assert": {"status": 200, "resp.data.store_id": "${entity.id}"},
                    }
                ],
            },
            "joint_verdict": {"require_ui": True, "require_api": True, "require_readback": False},
        },
    }
    cases_doc = {
        "meta": {"title": "Joint NegCtl", "app": "joint_negctl_app", "schema_version": 2, "total": 1, "next_seq": 2},
        "cases": [strong_case],
    }
    profile.cases_path().parent.mkdir(parents=True, exist_ok=True)
    profile.cases_path().write_text(yaml.safe_dump(cases_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")

    ctrl = {
        "id": "neg.r1.skip_tap_joint",
        "case_id": "R1",
        "hypothesis": "跳过符号点击后 UI 锚点仍通过，但强联合裁决应在 API 平面捕获缺失调用并判失败",
        "perturbation": {"kind": "skip_step", "step": {"action": "tap_ax"}},
        "expect": {
            "plane": "joint",
            "fail_kinds": ["optimistic_ui_api_failed", "entity_api_mismatch"],
        },
    }
    neg_dir = profile.profile_dir / "discrimination"
    neg_dir.mkdir(parents=True, exist_ok=True)
    (neg_dir / "negative_controls.yaml").write_text(
        yaml.safe_dump({"schema": "autophone.negative-controls/1", "controls": [ctrl]}, allow_unicode=True),
        encoding="utf-8",
    )

    def _run_pair(
        *,
        case_def: dict[str, Any],
        weak_toothless: bool,
        neg_api_captured: bool = True,
        neg_fail_ui_step: bool = False,
        tag: str = "",
    ) -> dict[str, Any]:
        request = {
            "case_id": "R1",
            "control_id": ctrl["id"],
            "declaration_sha256": declaration_sha256(ctrl),
        }
        bound_neg = bind_negative_control(
            profile,
            [case_def],
            request,
            symbol_binding=binding,
            screen_asset_doc=screen_doc,
        )[0]

        def _exec_one(
            run_id: str,
            steps_override: list[dict[str, Any]],
            bound_cases: list[dict[str, Any]] | None,
            *,
            api_captured: bool = True,
            ocr_frames: list[dict[str, Any]] | None = None,
        ) -> dict[str, Any]:
            harness = _SimulatedCaptureHarness()
            _patch_device(
                monkeypatch,
                ocr_sequence=ocr_frames or [
                    _ocr_frame("今晚，好好吃一顿", "立即下单", "调偏好"),
                    _ocr_frame("今晚，好好吃一顿", "饮食偏好"),
                ],
                on_tap=lambda _msg: harness.emit_http(
                    url="https://api.food.example/api/v1/preferences",
                    method="GET",
                    status=200,
                    resp_body={"data": {"store_id": "store-flagship-a"}},
                ),
            )
            rdir = tmp_path / run_id
            with recipe_mod.recipe_scope("joint_negctl_app", root=tmp_path, overrides={"R1": steps_override}):
                outcome = runner_mod.run_case(
                    case_def,
                    rdir / "shots",
                    str(tmp_path / "absent.sock"),
                    10.0,
                    capture=harness.handle if api_captured else None,
                    ax=_FakeAxProvider(_ax_dump()),
                    screen_asset_doc=screen_doc,
                    symbol_binding=binding,
                )
            outcome.recipe_identity = {
                "source": "live",
                "path": "recipes/r1.yaml",
                "recipe_sha256": recipe_sha,
                "case_revision": 1,
                "reserved_external_id": None,
                "binding_sha256": binding.binding_sha256,
            }
            outcome.binding_sha256 = binding.binding_sha256
            merged = harness.merged() if api_captured else []
            if weak_toothless and bound_cases is not None:
                # Weak/toothless case has require_api=False so skipping tap leaves joint verdict GREEN!
                merged = []
            finalized = capture_mod.finalize_api_plane(
                rdir,
                merged,
                runner_mod.RunResult(aborted=False, abort_reason="", outcomes=[outcome]),
                app="joint_negctl_app",
                cases=[case_def],
                profile=profile,
                api_captured=api_captured,
            )
            if bound_cases is not None:
                attach_negative_control_records(finalized.run_result.outcomes, bound_cases)
            spec = {
                "run_id": run_id,
                "type": "regression",
                "app": "joint_negctl_app",
                "device": "vm-test",
                "planes": {"ui": {"mode": "required"}, "api": {"mode": "advisory"}},
            }
            assembled = RunResultAssembler().assemble(
                spec,
                finalized.run_result,
                package=_confirmed_package_payload(),
                timestamps={"queued_at": "2026-11-27T00:00:00Z", "collect_ended_at": "2026-11-27T00:00:05Z"},
                phase_status={"provision": {"status": "ok"}, "execute": {"status": "ok"}, "collect": {"status": "ok"}},
                case_meta={"R1": {"revision": 1, "status": "active"}},
            )
            validate_or_raise("run-result.v1.json", assembled)
            return assembled

        label = tag or ("weak" if weak_toothless else "strong")
        pos_res = _exec_one(f"pos-{label}", base_steps, None)
        neg_res = _exec_one(
            f"neg-{label}",
            bound_neg[NEGATIVE_CONTROL_STEPS_KEY],
            [bound_neg],
            api_captured=neg_api_captured,
            ocr_frames=[_ocr_frame("完全不相关的空白页")] if neg_fail_ui_step else None,
        )
        return judge_negative_control(
            app="joint_negctl_app",
            control=ctrl,
            build_label=BUILD_LABEL,
            positive=pos_res,
            negative=neg_res,
            positive_rig_id="rig-a",
            negative_rig_id="rig-a",
            target_rig_id="rig-a",
        )

    # 1) Strong joint verdict flips on perturbation while UI stayed green -> teeth_at="joint", counted=True!
    probe_strong = _run_pair(case_def=strong_case, weak_toothless=False)
    assert probe_strong["target_green"] is True
    assert probe_strong["negative_verdict"] == VERDICT_COUNTED, probe_strong
    assert probe_strong["discriminator_flipped"] is True
    assert probe_strong["teeth_at"] == "joint"
    assert probe_strong["counted"] is True

    # Promotion gate accepts teeth_at="joint" with matching artifact & binding_sha256!
    issues_ok = discrimination_gaps(
        probe_strong,
        current_build_label=BUILD_LABEL,
        current_artifact_content_digest=SHA_V1,
        current_binding_sha256=binding.binding_sha256,
        uses_symbol_binding=True,
    )
    assert issues_ok == [], issues_ok

    # 2) Toothless weak joint policy (require_api=False) still passes under perturbation -> caught!
    weak_case = {
        **strong_case,
        "entity_binding": {
            "entity_type": "store",
            "entity_id": "store-flagship-a",
            "joint_verdict": {"require_ui": True, "require_api": False, "require_readback": False},
        },
    }
    probe_weak = _run_pair(case_def=weak_case, weak_toothless=True)
    assert probe_weak["target_green"] is True
    assert probe_weak["negative_verdict"] == VERDICT_PASSED, probe_weak
    assert probe_weak["discriminator_flipped"] is False
    assert probe_weak["teeth_at"] == "none"
    assert probe_weak["counted"] is False

    issues_weak = discrimination_gaps(
        probe_weak,
        current_build_label=BUILD_LABEL,
        current_artifact_content_digest=SHA_V1,
        current_binding_sha256=binding.binding_sha256,
        uses_symbol_binding=True,
    )
    assert "discrimination_toothless" in issues_weak, issues_weak

    # 3) Hole 4A regression: missing API capture (api_evidence_missing) during a plane="joint"
    #    negative control is an infra failure -> inconclusive (counted=False, teeth_at=None),
    #    NEVER falsely awarded VERDICT_COUNTED / teeth_at="joint"!
    probe_missing_cap = _run_pair(
        case_def=strong_case,
        weak_toothless=False,
        neg_api_captured=False,
        tag="missing-cap",
    )
    assert probe_missing_cap["target_green"] is True
    assert probe_missing_cap["counted"] is False, probe_missing_cap
    assert probe_missing_cap["teeth_at"] is None, probe_missing_cap
    assert probe_missing_cap["negative_verdict"] != VERDICT_COUNTED, probe_missing_cap

    # 4) Hole 4B regression: premature UI step failure (failed_step_index=0) during a
    #    plane="joint" negative control -> failed_elsewhere (counted=False, teeth_at=None),
    #    NEVER falsely awarded VERDICT_COUNTED!
    probe_ui_crash = _run_pair(
        case_def=strong_case,
        weak_toothless=False,
        neg_fail_ui_step=True,
        tag="early-ui-fail",
    )
    assert probe_ui_crash["target_green"] is True
    assert probe_ui_crash["counted"] is False, probe_ui_crash
    assert probe_ui_crash["teeth_at"] is None, probe_ui_crash
    assert probe_ui_crash["negative_verdict"] == "failed_elsewhere", probe_ui_crash
