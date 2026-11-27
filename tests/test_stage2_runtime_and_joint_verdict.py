"""Stage 2 runtime & joint-verdict unit/integration tests.

Covers:
1. Window-tagged ``api_mark`` propagation (CaseContext -> CaptureHandle host
   ``_action_meta`` -> ``net_capture.merge``) with zero ``net_tap.js`` edits and
   100% backward compatibility when metadata is absent.
2. Symbol-bound step dispatch sets/clears ``_current_symbol_id`` and
   ``_current_window_tag`` around ``tap_ax(symbol=...)``, emitting a window-tagged
   ``api_mark`` before dispatch.
3. ``runner.run_case`` emits ``body_end`` mark immediately after recipe body
   finishes and before post-body ``entity_readback`` and teardown.
4. Post-action instance-level ``entity_readback.py`` (works on ordinary cases,
   resolves literal/{from_entity}/{from_context}/{from_fixture}, rejects
   unregistered & irreversible ops fail-closed, isolates HTTP calls via
   ``entity_readback`` mark, evaluates scalar & predicate ``expect_context``,
   redacts sensitive fields, computes digests, writes ``entity-readback.json``,
   and registers artifact in ``default_phase2_artifacts``).
5. Hole 1 closed: ``login_precondition="any"`` case with API-required
   ``entity_binding`` triggers ``_run_with_capture`` (without invoking
   ``_ensure_login_state``) and passes ``_reject_uncaptured_api_required``.
6. Hole 2 closed: capture factory returning ``None``, capture startup error,
   ``session.merged()`` crash, ``finalize_api_plane`` crash, or ``api=disabled``
   all fail closed with ``api_evidence_missing`` instead of silently passing.
7. ``RunResultAssembler`` serializes ``cases[].joint_verdict`` and
   ``cases[].planes.readback`` strictly conforming to ``run-result.v1.json``
   while leaving non-``entity_binding`` cases 100% unchanged; ``PendingPhase2Collection``
   snapshot/restore preserves all Stage 2 fields cleanly.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from autophone.assets import app_symbols
from autophone.assets.apps import create_app_profile, load_app_profile
from autophone.assets.entity_binding import resolve_case_entity
from autophone.assets.screens import load_screen_asset
from autophone.contracts import validate_or_raise
from autophone.device.ax_target import AxDump, Node
from autophone.execution import entity_readback as entity_rb
from autophone.execution.engine import actions as actions_mod
from autophone.execution.engine import recipe as recipe_mod
from autophone.execution.engine import report as report_mod
from autophone.execution.engine import runner as runner_mod
from autophone.execution.run_execution import RunExecution
from autophone.execution.run_types import PendingPhase2Collection
from autophone.observation import artifacts as artifacts_mod
from autophone.observation import capture as capture_mod
from autophone.observation.frida import capture_session, net_capture
from autophone.observation.run_result import RunResultAssembler

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_ROOT = REPO_ROOT / "examples" / "food-ordering"
SHA_V1 = "14282f675363a9d74361c9b5d24dcde461648df2a13cf9aa4488d0aad4f68d58"


def _ocr_frame(*texts: str) -> dict[str, Any]:
    boxes = []
    for idx, text in enumerate(texts):
        boxes.append({
            "text": text,
            "x": 100,
            "y": 150 + idx * 120,
            "w": 220,
            "h": 50,
            "confidence": 0.99,
        })
    return {"image_w": 1290, "image_h": 2796, "boxes": boxes}


def _ax_node(
    *,
    identifier: str = "home.ai_prompt.preferences",
    role: str = "button",
    label: str = "调偏好",
    enabled: bool = True,
) -> Node:
    return Node(
        role=role,
        label=label,
        traits=["button"],
        frame=(100.0, 200.0, 120.0, 44.0),
        enabled=enabled,
        value=None,
        identifier=identifier,
        path="0/0",
        native_x=480,
        native_y=666,
        nx=0.37,
        ny=0.24,
        hittable=True,
    )


def _ax_dump(nodes: list[Node]) -> AxDump:
    return AxDump(
        app="com.autophone.foodorderingdemo.v2",
        scale=3.0,
        w=430.0,
        h=932.0,
        nodes=nodes,
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


def _patch_ctx_io(monkeypatch: pytest.MonkeyPatch, ocr_sequence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    taps: list[dict[str, Any]] = []
    png_b64 = base64.b64encode(b"\xff\xd8\xff\xe0FAKEJPEG\xff\xd9").decode("ascii")
    idx = {"n": 0}

    def fake_request(msg: dict[str, Any], **_kw: Any):
        t = msg.get("t")
        if t == "screenshot":
            return {"ok": True, "image": png_b64}, SimpleNamespace(
                image_w=1290, image_h=2796, image_fmt="jpeg", screen_w=430, screen_h=932, scale=3.0,
            )
        if t == "tap":
            taps.append(dict(msg))
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
    return taps


def _json_b64(payload: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")


def _make_req_resp_events(
    *,
    task_ptr: str,
    url: str,
    method: str = "GET",
    ts_req: int,
    ts_resp: int,
    status: int = 200,
    req_body: dict[str, Any] | None = None,
    resp_body: dict[str, Any] | None = None,
    req_headers: dict[str, str] | None = None,
    resp_headers: dict[str, str] | None = None,
    resp_error: str | None = None,
) -> list[dict[str, Any]]:
    req_ev: dict[str, Any] = {
        "ev": "req",
        "task_ptr": task_ptr,
        "ts": ts_req,
        "req": {
            "method": method,
            "url": url,
            "host": "api.food.example",
            "headers": req_headers or {},
        },
    }
    if req_body is not None:
        req_ev["body_b64"] = _json_b64(req_body)
    resp_ev: dict[str, Any] = {
        "ev": "resp",
        "task_ptr": task_ptr,
        "ts": ts_resp,
        "status": status,
        "resp_headers": resp_headers or {"Content-Type": "application/json"},
        "error": resp_error,
    }
    if resp_body is not None:
        resp_ev["body_b64"] = _json_b64(resp_body)
    return [req_ev, resp_ev]


# ── 1 & 2. Window-tagged api_mark propagation & zero net_tap.js edits ────────


def test_capture_handle_enriches_marks_on_host_without_touching_net_tap_rpc() -> None:
    """CaptureHandle stores symbol_id & window_tag in host _action_meta keyed by
    action_id, calls the unchanged 5-arg guest RPC, and net_capture.merge merges
    them onto attributed call records while staying backward compatible."""
    events: list[dict[str, Any]] = []
    rpc_calls: list[tuple[Any, ...]] = []
    clock = {"ts": 1000}

    class _FakeGuestExports:
        def mark(self, case_id, step, operation, action_id, subject_epoch_ref):
            # Exactly 5 arguments matching untouched net_tap.js rpc.exports.mark!
            rpc_calls.append((case_id, step, operation, action_id, subject_epoch_ref))
            events.append({
                "ev": "mark",
                "case_id": case_id,
                "step": step,
                "operation": operation,
                "action_id": action_id,
                "subject_epoch_ref": subject_epoch_ref,
                "ts": clock["ts"],
            })

    handle = capture_session.CaptureHandle(_FakeGuestExports(), events)

    # Mark 1: legacy mark without symbol_id/window_tag
    handle.mark("R5", "tap_text", None, action_id="act-legacy")
    events.extend(_make_req_resp_events(
        task_ptr="0x1",
        url="https://api.food.example/api/v1/banner",
        ts_req=1010,
        ts_resp=1020,
        resp_body={"ok": True},
    ))

    # Mark 2: symbol-bound mark with symbol_id & window_tag
    clock["ts"] = 2000
    handle.mark(
        "R5",
        "tap_ax",
        None,
        action_id="act-sym",
        symbol_id="ctrl.home.preferences",
        window_tag="symbol:ctrl.home.preferences",
    )
    events.extend(_make_req_resp_events(
        task_ptr="0x2",
        url="https://api.food.example/api/v1/preferences?store_id=store-flagship-a",
        ts_req=2050,
        ts_resp=2080,
        resp_body={"data": {"store_id": "store-flagship-a"}},
    ))

    # Mark 3: body_end mark cuts off the interaction window
    clock["ts"] = 3000
    handle.mark("R5", "body_end", None, action_id="act-end", window_tag="body_end")
    events.extend(_make_req_resp_events(
        task_ptr="0x3",
        url="https://api.food.example/api/v1/cleanup",
        ts_req=3050,
        ts_resp=3090,
        resp_body={"ok": True},
    ))

    assert len(rpc_calls) == 3
    assert handle.marks == 3
    assert "act-legacy" not in handle.action_meta
    assert handle.action_meta["act-sym"] == {
        "symbol_id": "ctrl.home.preferences",
        "window_tag": "symbol:ctrl.home.preferences",
    }

    merged = handle.merged()
    assert len(merged) == 3

    # Legacy record preserves exact backward-compatible shape (no symbol_id/window_tag keys)
    assert merged[0]["action_id"] == "act-legacy"
    assert "symbol_id" not in merged[0]
    assert "window_tag" not in merged[0]

    # Symbol-bound record receives host-enriched symbol_id & window_tag
    assert merged[1]["action_id"] == "act-sym"
    assert merged[1]["symbol_id"] == "ctrl.home.preferences"
    assert merged[1]["window_tag"] == "symbol:ctrl.home.preferences"

    # Post-body record receives body_end step & window_tag (and NO symbol_id leak!)
    assert merged[2]["step"] == "body_end"
    assert merged[2]["window_tag"] == "body_end"
    assert "symbol_id" not in merged[2]

    # Direct net_capture.merge without action_meta still passes native mark fields through
    native_events = [
        {"ev": "mark", "case_id": "R9", "step": "tap_ax", "symbol_id": "ctrl.x", "window_tag": "w1", "ts": 10},
        *_make_req_resp_events(task_ptr="0x9", url="https://api.food.example/x", ts_req=20, ts_resp=30),
    ]
    native_merged = net_capture.merge(native_events)
    assert native_merged[0]["symbol_id"] == "ctrl.x"
    assert native_merged[0]["window_tag"] == "w1"


def test_symbol_step_dispatch_and_runner_emit_window_marks_and_body_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tap_ax(symbol=...) emits a window-tagged api_mark with symbol_id, clears
    ctx._current_symbol_id afterwards, and runner emits body_end before readback & teardown."""
    profile = load_app_profile("food_ordering", root=EXAMPLE_ROOT, state_root=tmp_path)
    screen_doc = load_screen_asset(profile.screens_path())
    binding = app_symbols.load_symbol_binding(profile, artifact_content_digest=SHA_V1)

    mark_log: list[dict[str, Any]] = []

    class _RecordingCapture:
        subject_epoch_ref = "epoch-1"

        def mark(self, case_id, step, operation=None, *, action_id=None, subject_epoch_ref=None,
                 symbol_id=None, window_tag=None):
            mark_log.append({
                "case_id": case_id,
                "step": step,
                "operation": operation,
                "action_id": action_id,
                "symbol_id": symbol_id,
                "window_tag": window_tag,
            })

    order_of_events: list[str] = []

    def fake_readback_op(ctx: actions_mod.CaseContext, store_id: str) -> dict[str, Any]:
        order_of_events.append(f"readback:{store_id}")
        assert ctx._current_symbol_id is None
        return {
            "store_id": store_id,
            "store_name": "旗舰店-A",
            "preference_profile": "少冰少糖-VIP",
        }

    def fake_teardown_op(ctx: actions_mod.CaseContext) -> None:
        order_of_events.append("teardown")

    monkeypatch.setitem(recipe_mod.OPERATIONS, "read_preference_profile", fake_readback_op)
    monkeypatch.setitem(recipe_mod.OPERATIONS, "reset_after_case", fake_teardown_op)
    from autophone.execution import effects as effects_mod
    monkeypatch.setitem(
        effects_mod._EXECUTOR_EFFECTS,
        "read_preference_profile",
        effects_mod.EffectDecision(effects_mod.REVERSIBLE, "read_preference_profile", "test"),
    )
    monkeypatch.setitem(
        effects_mod._EXECUTOR_EFFECTS,
        "reset_after_case",
        effects_mod.EffectDecision(effects_mod.REVERSIBLE, "reset_after_case", "test"),
    )

    _patch_ctx_io(
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
    case = {
        "id": "R5",
        "module": "饮食偏好",
        "item": "窗口标记与顺序",
        "teardown": [{"op": "reset_after_case"}],
        "entity_binding": {
            "entity_type": "store",
            "entity_id": {"from_fixture": "expected_store_id"},
            "attributes": {
                "store_name": {"from_fixture": "expected_store_name"},
                "preference_profile": {"from_fixture": "expected_preference_profile"},
            },
            "readback": {
                "op": "read_preference_profile",
                "with": {"store_id": {"from_entity": "id"}},
                "expect_context": {
                    "store_id": {"from_entity": "id"},
                    "store_name": "${entity.store_name}",
                    "preference_profile": {"op": "eq", "value": "${entity.preference_profile}"},
                },
            },
        },
    }
    with recipe_mod.recipe_scope("food_ordering", root=EXAMPLE_ROOT, overrides={"R5": steps}):
        outcome = runner_mod.run_case(
            case,
            tmp_path / "shots",
            str(tmp_path / "absent.sock"),
            10.0,
            capture=_RecordingCapture(),
            ax=_FakeAxProvider(_ax_dump([_ax_node()])),
            screen_asset_doc=screen_doc,
            symbol_binding=binding,
            api_fixtures_path=profile.api_fixtures_path(),
        )

    assert outcome.status == "pass", outcome.detail
    assert outcome.entity_readback is not None
    assert outcome.entity_readback["status"] == "pass"
    assert outcome.joint_verdict is not None
    assert outcome.joint_verdict["status"] == "pass"
    assert order_of_events == ["readback:store-flagship-a", "teardown"]

    # Check exact sequence of API marks:
    # 1. tap_ax with symbol_id="ctrl.home.preferences" and window_tag="symbol:ctrl.home.preferences"
    # 2. body_end with symbol_id=None and window_tag="body_end"
    # 3. entity_readback before readback op
    # 4. operation(read_preference_profile) -> operation_end -> operation(reset_after_case)
    tap_marks = [m for m in mark_log if m["step"] == "tap_ax"]
    assert len(tap_marks) == 1
    assert tap_marks[0]["symbol_id"] == "ctrl.home.preferences"
    assert tap_marks[0]["window_tag"] == "symbol:ctrl.home.preferences"

    step_seq = [m["step"] for m in mark_log]
    idx_tap = step_seq.index("tap_ax")
    idx_body_end = step_seq.index("body_end")
    idx_rb = step_seq.index("entity_readback")
    idx_teardown = next(i for i, m in enumerate(mark_log) if m["operation"] == "reset_after_case")
    assert idx_tap < idx_body_end < idx_rb < idx_teardown
    assert mark_log[idx_body_end]["symbol_id"] is None
    assert mark_log[idx_body_end]["window_tag"] == "body_end"
    assert mark_log[idx_body_end]["action_id"] != mark_log[idx_tap]["action_id"]
    assert mark_log[idx_rb]["symbol_id"] is None
    assert mark_log[idx_rb]["window_tag"] == "entity_readback"
    rb_op_marks = [m for m in mark_log if m["operation"] == "read_preference_profile" and m["step"] == "operation"]
    assert len(rb_op_marks) == 1
    assert rb_op_marks[0]["window_tag"] == "entity_readback"
    assert rb_op_marks[0]["symbol_id"] is None
    assert mark_log[idx_teardown]["window_tag"] == "post_case_cleanup"
    assert mark_log[idx_teardown]["symbol_id"] is None


# ── 4. Post-action entity_readback.py deep unit tests ────────────────────────


def test_entity_readback_safety_predicates_redaction_and_artifact_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify readback parameter resolution, fail-closed checks on missing/irreversible
    ops, expect_context predicates (eq/ne/contains/exists/empty/regex), redaction + digests,
    entity-readback.json persistence, and Artifact manifest registration."""
    from autophone.execution import effects as effects_mod

    dispatched: list[dict[str, Any]] = []

    def ro_reader(ctx: actions_mod.CaseContext, store_id: str, order_token: str, env_tag: str) -> dict[str, Any]:
        dispatched.append({"store_id": store_id, "order_token": order_token, "env_tag": env_tag})
        ctx._record("readback_probe", True, f"read {store_id} phone=13812345678", 0.0)
        return {
            "store_id": store_id,
            "status": "saved",
            "profile_label": "少冰少糖-VIP",
            "phone": "13812345678",
            "token": "secret-jwt-999",
            "notes": "",
            "order_code": "ORD-2026-0042",
        }

    def irreversible_writer(ctx: actions_mod.CaseContext, store_id: str) -> None:
        dispatched.append({"SHOULD_NEVER_RUN": store_id})

    monkeypatch.setitem(recipe_mod.OPERATIONS, "ro_reader", ro_reader)
    monkeypatch.setitem(recipe_mod.OPERATIONS, "irreversible_writer", irreversible_writer)
    monkeypatch.setitem(
        effects_mod._EXECUTOR_EFFECTS,
        "ro_reader",
        effects_mod.EffectDecision(effects_mod.REVERSIBLE, "ro_reader", "read-only"),
    )
    monkeypatch.setitem(
        effects_mod._EXECUTOR_EFFECTS,
        "irreversible_writer",
        effects_mod.EffectDecision(effects_mod.IRREVERSIBLE, "irreversible_writer", "writes prod"),
    )

    case_ok = {
        "id": "R10",
        "entity_binding": {
            "entity_type": "store",
            "entity_id": {"from_fixture": "expected_store_id"},
            "attributes": {"profile": {"from_fixture": "expected_profile"}},
            "readback": {
                "op": "ro_reader",
                "with": {
                    "store_id": {"from_entity": "id"},
                    "order_token": {"from_context": "ctx_token"},
                    "env_tag": {"from_fixture": "env_tag"},
                },
                "expect_context": {
                    "store_id": "${entity.id}",
                    "status": {"op": "ne", "value": "deleted"},
                    "profile_label": {"op": "contains", "value": "VIP"},
                    "order_code": {"op": "regex", "value": "^ORD-\\d{4}-\\d+$"},
                    "notes": {"op": "empty", "value": True},
                    "phone": {"op": "exists", "value": True},
                },
            },
        },
    }
    fixtures = {"expected_store_id": "store-flagship-a", "expected_profile": "少冰少糖-VIP", "env_tag": "uat"}
    ctx = actions_mod.CaseContext(case_id="R10", shot_dir=tmp_path / "shots")
    ctx.fixture_order["ctx_token"] = "tok-secret-123"
    resolved = resolve_case_entity(case_ok, fixture_vars=fixtures, runtime_context=ctx.fixture_order)

    rec = entity_rb.run_entity_readback(ctx, case_ok, resolved_entity=resolved, fixture_vars=fixtures)
    assert rec["status"] == "pass" and rec["ok"] is True
    assert rec["record_sha256"].startswith("sha256:")
    assert rec["readback_ref"].startswith("result.json#action=")
    # Sensitive values MUST be redacted in persisted payload and HMAC-digested!
    assert rec["observed_context"]["token"] == "<redacted>"
    assert rec["observed_context"]["phone"] == "<redacted>"
    assert rec["observed_context_sha256"]["token"].startswith("hmac-sha256:")
    assert rec["observed_context_sha256"]["store_id"].startswith("sha256:")
    assert rec["readback_args"]["order_token"] == "<redacted>"
    assert rec["readback_args_sha256"]["order_token"].startswith("hmac-sha256:")

    # Write entity-readback.json and verify default_phase2_artifacts registers it
    run_dir = tmp_path / "run_art"
    run_dir.mkdir()
    outcome = runner_mod.CaseOutcome(
        id="R10", status="pass", module="m", item="i", row=1, entity_readback=rec,
    )
    out_path = entity_rb.write_entity_readback_evidence(run_dir, "run-10", [outcome])
    assert out_path == run_dir / "entity-readback.json"
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["schema"] == entity_rb.ENTITY_READBACK_EVIDENCE_SCHEMA
    assert written["cases"]["R10"]["status"] == "pass"
    assert "secret-jwt-999" not in out_path.read_text(encoding="utf-8")

    manifest_items = artifacts_mod.default_phase2_artifacts(run_dir)
    rb_item = next(a for a in manifest_items if a.path == "entity-readback.json")
    assert rb_item.redaction_status == "redacted"
    assert rb_item.ai_policy == "allowed"

    # Fail closed on irreversible operation — zero dispatch!
    dispatched.clear()
    case_irrev = {
        "id": "R11",
        "entity_binding": {
            "entity_type": "store",
            "entity_id": "store-flagship-a",
            "readback": {"op": "irreversible_writer", "with": {"store_id": {"from_entity": "id"}}},
        },
    }
    rec_irrev = entity_rb.run_entity_readback(
        ctx, case_irrev, resolved_entity=resolve_case_entity(case_irrev),
    )
    assert rec_irrev["status"] == "fail"
    assert rec_irrev["reason"] == entity_rb.REASON_OPERATION_IRREVERSIBLE
    assert dispatched == []

    # Fail closed on unregistered operation — zero dispatch!
    case_missing_op = {
        "id": "R12",
        "entity_binding": {
            "entity_type": "store",
            "entity_id": "store-flagship-a",
            "readback": {"op": "does_not_exist"},
        },
    }
    rec_missing = entity_rb.run_entity_readback(
        ctx, case_missing_op, resolved_entity=resolve_case_entity(case_missing_op),
    )
    assert rec_missing["status"] == "fail"
    assert rec_missing["reason"] == entity_rb.REASON_OPERATION_INVALID

    # Fail closed on unresolved {from_context} binding — zero dispatch!
    ctx_empty = actions_mod.CaseContext(case_id="R10", shot_dir=tmp_path / "shots2")
    rec_unres = entity_rb.run_entity_readback(
        ctx_empty, case_ok, resolved_entity=resolved, fixture_vars=fixtures,
    )
    assert rec_unres["status"] == "fail"
    assert rec_unres["reason"] == entity_rb.REASON_BINDING_UNRESOLVED
    assert dispatched == []


# ── 5 & 6. Orchestrator Hole 1 & Hole 2 fail-closed guarantees ───────────────


def _make_orchestrator(tmp_path: Path, *, capture_session_factory: Any) -> tuple[RunExecution, Any]:
    from autophone.device.lock import ScreenLock
    from autophone.rig.maintenance import RigMaintenance

    profile = create_app_profile("entity_demo", "com.example.entity", "Entity Demo", root=tmp_path)
    cases_doc = {
        "meta": {"title": "Entity Demo", "app": "entity_demo", "schema_version": 2, "total": 1, "next_seq": 2},
        "cases": [
            {
                "id": "R1",
                "revision": 1,
                "status": "active",
                "automation": "ui_ready",
                "case_type": "功能测试",
                "test_point": "实体校验",
                "owner": "qa",
                "module": "偏好",
                "item": "login_precondition=any 实体校验触发抓包",
                "login_precondition": "any",
                "steps": ["断言首页"],
                "expects": ["首页可见"],
                "entity_binding": {
                    "entity_type": "store",
                    "entity_id": {"from_fixture": "expected_store_id"},
                    "api_window": {
                        "expect": [
                            {
                                "name": "load_store",
                                "method": "GET",
                                "path": "/api/v1/store",
                                "assert": {
                                    "status": 200,
                                    "resp.data.store_id": "${entity.id}",
                                },
                            }
                        ]
                    },
                    "joint_verdict": {"require_ui": True, "require_api": True, "require_readback": False},
                },
            }
        ],
    }
    profile.cases_path().parent.mkdir(parents=True, exist_ok=True)
    profile.cases_path().write_text(yaml.safe_dump(cases_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    profile.api_fixtures_path().write_text(
        yaml.safe_dump({"R1": {"expected_store_id": "store-flagship-a"}}, allow_unicode=True),
        encoding="utf-8",
    )
    profile.recipes_dir().mkdir(parents=True, exist_ok=True)
    (profile.recipes_dir() / "r1.yaml").write_text(
        yaml.safe_dump({"id": "R1", "steps": [{"action": "assert_present", "queries": ["首页"], "wait_s": 0}]}),
        encoding="utf-8",
    )
    exec_obj = RunExecution(
        lock=ScreenLock(root=tmp_path / "locks"),
        rig_maintenance=RigMaintenance(None, capability_support=["api_capture"]),
        executor=None,
        run_dir=lambda rid: tmp_path / "runs" / rid,
        persist_result=lambda rdir, payload: rdir / "platform-run-result.json",
        set_active=lambda *a, **k: None,
        update_active=lambda *a, **k: None,
        clear_active=lambda *a, **k: None,
        assert_store_context=lambda *a, **k: None,
        bind_store_context=lambda *a, **k: None,
        capture_session_factory=capture_session_factory,
        recorder_factory=lambda *a, **k: None,
        crash_trap_factory=lambda *a, **k: None,
        telemetry_sleep=lambda _s: None,
        location_quiesce_fn=None,
        app_hygiene_factory=lambda *a, **k: None,
    )
    return exec_obj, profile


def test_hole1_login_any_entity_case_engages_capture_without_login_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hole 1: a case with login_precondition='any' and entity_binding.api_window
    engages _run_with_capture (skipping login probe) and passes submit preflight."""
    capture_entered = {"count": 0}
    login_probe_called = {"count": 0}

    class _FakeSession:
        spawn_gated = False

        def __init__(self) -> None:
            self.handle = type("_H", (), {"mark": lambda *a, **k: None, "subject_epoch_ref": None})()

        def __enter__(self):
            capture_entered["count"] += 1
            return self

        def __exit__(self, *a):
            return None

        def merged(self) -> list[dict[str, Any]]:
            return [{
                "case_id": "R1",
                "step": "tap_ax",
                "method": "GET",
                "url": "https://api.food.example/api/v1/store",
                "status": 200,
                "resp_body_text": json.dumps({"data": {"store_id": "store-flagship-a"}}),
                "noise": False,
            }]

    exec_obj, profile = _make_orchestrator(tmp_path, capture_session_factory=lambda *a: _FakeSession())
    monkeypatch.setattr(
        exec_obj,
        "_ensure_login_state",
        lambda *a, **k: (login_probe_called.__setitem__("count", login_probe_called["count"] + 1) or "should_not_run"),
    )
    monkeypatch.setattr(exec_obj, "_launch_app", lambda *a, **k: None)
    _patch_ctx_io(monkeypatch, ocr_sequence=[_ocr_frame("首页")])

    spec = {
        "run_id": "run-hole1",
        "type": "regression",
        "app": profile.app,
        "device": "vm-test",
        "case_selector": {"ids": ["R1"]},
        "planes": {"ui": {"mode": "required"}, "api": {"mode": "required"}},
    }
    # Submit preflight MUST accept api=required for login_precondition='any' when entity_binding requires API!
    exec_obj._reject_uncaptured_api_required(spec, profile)

    run_dir = tmp_path / "runs" / "run-hole1"
    shot_dir = run_dir / "shots"
    shot_dir.mkdir(parents=True)
    result = exec_obj._run_cases_for_state(
        spec, profile, run_dir, shot_dir, profile.cases_path(), spec["case_selector"], lambda _p: None,
    )
    assert capture_entered["count"] == 1
    assert login_probe_called["count"] == 0
    assert len(result.outcomes) == 1
    out = result.outcomes[0]
    assert out.status == "pass", out.detail
    assert out.joint_verdict is not None
    assert out.joint_verdict["status"] == "pass"
    assert out.joint_verdict["planes"]["api"] == "pass"


@pytest.mark.parametrize("failure_mode", ["factory_none", "merged_crash", "finalize_crash", "api_disabled"])
def test_hole2_capture_failure_or_absence_fails_closed_api_evidence_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_mode: str,
) -> None:
    """Hole 2: any capture failure/absence on an API-required entity_binding case
    MUST fail closed with outcome.status='fail' and fail_kind='api_evidence_missing',
    NEVER silently passing even when UI is green and api.mode='advisory'."""

    class _CrashingMergedSession:
        spawn_gated = False
        handle = type("_H", (), {"mark": lambda *a, **k: None, "subject_epoch_ref": None})()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def merged(self):
            if failure_mode == "merged_crash":
                raise RuntimeError("simulated session.merged() crash")
            return []

    def factory(*_a):
        if failure_mode == "factory_none":
            return None
        return _CrashingMergedSession()

    exec_obj, profile = _make_orchestrator(tmp_path, capture_session_factory=factory)
    monkeypatch.setattr(exec_obj, "_launch_app", lambda *a, **k: None)
    _patch_ctx_io(monkeypatch, ocr_sequence=[_ocr_frame("首页")])
    if failure_mode == "finalize_crash":
        monkeypatch.setattr(
            capture_mod,
            "finalize_api_plane",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated finalize crash")),
        )

    api_mode = "disabled" if failure_mode == "api_disabled" else "advisory"
    spec = {
        "run_id": f"run-hole2-{failure_mode}",
        "type": "regression",
        "app": profile.app,
        "device": "vm-test",
        "case_selector": {"ids": ["R1"]},
        "planes": {"ui": {"mode": "required"}, "api": {"mode": api_mode}},
    }
    run_dir = tmp_path / "runs" / spec["run_id"]
    shot_dir = run_dir / "shots"
    shot_dir.mkdir(parents=True)
    result = exec_obj._run_cases_for_state(
        spec, profile, run_dir, shot_dir, profile.cases_path(), spec["case_selector"], lambda _p: None,
    )
    assert len(result.outcomes) == 1
    out = result.outcomes[0]
    assert out.ui_status == "pass"
    assert out.status == "fail"
    assert out.fail_kind == "api_evidence_missing"
    assert out.joint_verdict is not None
    assert out.joint_verdict["status"] == "fail"
    assert out.joint_verdict["fail_kind"] == "api_evidence_missing"
    assert out.joint_verdict["planes"]["api"] == "missing"

    # Assemble platform-run-result.json and verify schema + fail-closed overall status!
    assembled = RunResultAssembler().assemble(
        spec,
        result,
        package={
            "active_slot": None,
            "active_version": None,
            "active_sha256": None,
            "device_version": None,
            "profile_build_label": None,
        },
        timestamps={"queued_at": "2026-11-27T00:00:00Z", "collect_ended_at": "2026-11-27T00:00:05Z"},
        phase_status={"provision": {"status": "ok"}, "execute": {"status": "ok"}, "collect": {"status": "ok"}},
    )
    validate_or_raise("run-result.v1.json", assembled)
    assert assembled["status"] == "failed"
    assert assembled["cases"][0]["status"] == "failed"
    assert assembled["cases"][0]["planes"]["ui"]["verdict"] == "pass"
    assert assembled["cases"][0]["joint_verdict"]["fail_kind"] == "api_evidence_missing"


# ── 7. RunResultAssembler schema compliance & PendingPhase2Collection roundtrip ──


def test_run_result_assembler_and_pending_collection_preserve_stage2_fields(tmp_path: Path) -> None:
    """cases[].joint_verdict & cases[].planes.readback validate strictly against
    run-result.v1.json, legacy cases omit them, and PendingPhase2Collection round-trips cleanly."""
    entity_outcome = runner_mod.CaseOutcome(
        id="R5",
        status="pass",
        ui_status="pass",
        module="饮食偏好",
        item="实体绑定通过",
        row=5,
        exec_via="recipe",
        net_jsonl=str(tmp_path / "planes" / "api" / "capture.jsonl"),
        api_assertions=[{"kind": "expect", "verdict": "pass", "reason": "ok"}],
        entity_readback={
            "case_id": "R5",
            "entity_type": "store",
            "entity_id": "store-flagship-a",
            "op": "read_preference_profile",
            "status": "pass",
            "ok": True,
            "reason": "",
            "detail": "entity readback read_preference_profile passed",
        },
        joint_verdict={
            "status": "pass",
            "fail_kind": "",
            "detail": "all required entity planes passed",
            "entity_type": "store",
            "entity_id": "store-flagship-a",
            "planes": {"ui": "pass", "api": "pass", "readback": "pass"},
            "mismatch_reasons": [],
        },
        resolved_entity={
            "entity_type": "store",
            "entity_id": "store-flagship-a",
            "attributes": {"store_name": "旗舰店-A"},
            "unresolved": [],
        },
    )
    legacy_outcome = runner_mod.CaseOutcome(
        id="R1",
        status="pass",
        ui_status="pass",
        module="首页",
        item="普通用例",
        row=1,
        exec_via="recipe",
    )
    (tmp_path / "planes" / "api").mkdir(parents=True)
    (tmp_path / "planes" / "api" / "capture.jsonl").write_text("", encoding="utf-8")

    phase2 = runner_mod.RunResult(aborted=False, abort_reason="", outcomes=[entity_outcome, legacy_outcome])
    pending = PendingPhase2Collection(
        phase2_result=phase2,
        cases_path=tmp_path / "cases.yaml",
        case_docs=(),
        report_meta={"run_id": "run-rt"},
        recordings=(),
    )
    snap = pending.snapshot(tmp_path)
    restored = PendingPhase2Collection.restore(snap, tmp_path)
    assert restored.phase2_result.outcomes[0].joint_verdict == entity_outcome.joint_verdict
    assert restored.phase2_result.outcomes[0].entity_readback == entity_outcome.entity_readback
    assert restored.phase2_result.outcomes[0].resolved_entity == entity_outcome.resolved_entity

    # Report JSON drops None Stage 2 fields on legacy outcome and retains them on entity outcome
    entity_row = report_mod._case_result(restored.phase2_result.outcomes[0])
    legacy_row = report_mod._case_result(restored.phase2_result.outcomes[1])
    assert "joint_verdict" in entity_row and "entity_readback" in entity_row
    assert "joint_verdict" not in legacy_row and "entity_readback" not in legacy_row

    spec = {
        "run_id": "run-rt",
        "type": "regression",
        "app": "food_ordering",
        "device": "vm-test",
        "planes": {"ui": {"mode": "required"}, "api": {"mode": "advisory"}},
    }
    assembled = RunResultAssembler().assemble(
        spec,
        restored.phase2_result,
        package={
            "active_slot": None,
            "active_version": None,
            "active_sha256": None,
            "device_version": None,
            "profile_build_label": None,
        },
        timestamps={"queued_at": "2026-11-27T00:00:00Z", "collect_ended_at": "2026-11-27T00:00:05Z"},
        phase_status={"provision": {"status": "ok"}, "execute": {"status": "ok"}, "collect": {"status": "ok"}},
    )
    validate_or_raise("run-result.v1.json", assembled)
    assert assembled["status"] == "passed"
    r5_case = assembled["cases"][0]
    r1_case = assembled["cases"][1]
    assert r5_case["joint_verdict"]["planes"] == {"ui": "pass", "api": "pass", "readback": "pass"}
    assert r5_case["planes"]["readback"] == {
        "mode": "required",
        "verdict": "pass",
        "detail": "entity readback read_preference_profile passed",
    }
    assert "joint_verdict" not in r1_case
    assert "readback" not in r1_case["planes"]
    # Verify digest helper stability
    assert len(hashlib.sha256(json.dumps(assembled, sort_keys=True).encode()).hexdigest()) == 64
