"""Stage 3B offline tests for guest_axruntime AxSession, foreground gates,
version/scale contracts, variant_evidence decoupling, and cross-backend isolation.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from autophone.contracts import validate_or_raise
from autophone.device import guest_ax_probe as probe_mod
from autophone.device.ax_session import AxSession, AxUnavailable
from autophone.device.ax_target import parse_dump
from autophone.device.guest_ax_probe import (
    GuestAxProbeRunner,
    LocalGuestAxProbe,
    forget_probe_deployments,
    normalize_probe_to_ax_v1,
    resolve_local_probe,
)
from autophone.execution.engine import actions as actions_mod
from autophone.execution.engine.actions import CaseContext, StepFailure
from autophone.observation import ax_checkpoint
from autophone.observation.capture import build_run_ax_session
from autophone.observation.impact import ax_comparison, variant_evidence
from autophone.observation.impact.comparison import EvidenceError

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ID = "com.autophone.foodorderingdemo.v2"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_fake_probe_binary(
    tmp_path: Path,
    *,
    content: bytes = b"fake-macho-probe-binary",
    source_sha: str | None = None,
    ent_sha: str | None = None,
    write_record: bool = True,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    binary = tmp_path / "guest_ax_probe"
    binary.write_bytes(content)
    binary.chmod(0o755)
    if write_record:
        record = {
            "schema": "autophone.guest-helper-build/v1",
            "protocol": "autophone.ax_probe/1",
            "probe_version": 1,
            "binary_sha256": _sha256_bytes(content),
            "source_sha256": source_sha or ("1" * 64),
            "entitlements_sha256": ent_sha or ("2" * 64),
            "archs": ["arm64", "arm64e"],
        }
        Path(f"{binary}.build.json").write_text(json.dumps(record), encoding="utf-8")
    return binary


def _probe_raw_payload(
    *,
    pid: int = 4242,
    bundle_id: str = BUNDLE_ID,
    request_id: str = "req-s3-1",
    captured_at_ms: int = 1732680000100,
    short_version: str = "1.0.0",
    bundle_version: str = "1",
    screen_w: float = 430.0,
    screen_h: float = 932.0,
    screen_x: float = 0.0,
    screen_y: float = 0.0,
    scale: float = 3.0,
    is_screen_locked: bool = False,
    is_control_center_visible: bool = False,
    primary_app: dict[str, Any] | None = None,
    occluded_scenes: list[str] | None = None,
    voiceover: bool = False,
    nodes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if primary_app is None:
        primary_app = {
            "pid": pid,
            "bundle_id": bundle_id,
            "process_name": "FoodAIVerification",
            "label": "Food Ordering",
            "application_is_modal": False,
        }
    if nodes is None:
        nodes = [
            {
                "identifier": "home.ai_prompt.preferences",
                "ax_uid": "1001",
                "label": "调偏好",
                "value": None,
                "role": "button",
                "traits": ["button"],
                "raw_traits": 1,
                "enabled": True,
                "is_visible": True,
                "responds_to_user_interaction": True,
                "frame": [20.0, 200.0, 120.0, 44.0],
                "visible_frame": [20.0, 200.0, 120.0, 44.0],
            },
            {
                "identifier": "home.title",
                "ax_uid": "1002",
                "label": "今晚，好好吃一顿",
                "value": None,
                "role": "header",
                "traits": ["header"],
                "raw_traits": 65536,
                "enabled": True,
                "is_visible": True,
                "responds_to_user_interaction": False,
                "frame": [20.0, 60.0, 260.0, 36.0],
                "visible_frame": [20.0, 60.0, 260.0, 36.0],
            },
        ]
    return {
        "schema": "autophone.ax_probe/1",
        "probe_version": 1,
        "ax_backend": "guest_axruntime",
        "request_id": request_id,
        "captured_at_ms": captured_at_ms,
        "complete": True,
        "truncated": False,
        "pid": pid,
        "bundle_id": bundle_id,
        "short_version": short_version,
        "bundle_version": bundle_version,
        "process_name": "FoodAIVerification",
        "elapsed_ms": 56.2,
        "first_wake_retries": 0,
        "did_enable_ax": False,
        "batch_mode": True,
        "ax_state": {
            "app_accessibility": True,
            "automation": True,
            "voiceover": voiceover,
        },
        "screen": {
            "x": screen_x,
            "y": screen_y,
            "w": screen_w,
            "h": screen_h,
            "scale": scale,
            "app_frame": [0.0, 0.0, screen_w, screen_h],
        },
        "system": {
            "is_screen_locked": is_screen_locked,
            "is_control_center_visible": is_control_center_visible,
            "focused_scene_identifier": "sceneID:com.autophone.foodorderingdemo.v2-default",
            "focused_occluded_app_scene_identifiers": occluded_scenes or [],
            "primary_app": primary_app,
            "current_applications": [primary_app],
        },
        "node_count": len(nodes),
        "nodes": nodes,
    }


def _patch_ctx_io(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ocr_texts: tuple[str, ...] = ("今晚，好好吃一顿",),
) -> list[dict[str, Any]]:
    taps: list[dict[str, Any]] = []
    b64_jpeg = base64.b64encode(b"\xff\xd8\xff\xe0FAKEJPEG").decode("ascii")

    def fake_request(msg: dict[str, Any], **_kw: Any) -> tuple[dict[str, Any], Any]:
        if msg.get("t") == "screenshot":
            return {"ok": True, "image": b64_jpeg}, SimpleNamespace(image_w=None)
        if msg.get("t") == "tap":
            taps.append(dict(msg))
            return {"ok": True}, None
        return {"ok": True}, None

    ocr_frame = {
        "image_w": 430,
        "image_h": 932,
        "boxes": [
            {"text": t, "confidence": 0.99, "x": 40, "y": 80 + i * 40, "w": 200, "h": 30}
            for i, t in enumerate(ocr_texts)
        ],
    }

    def fake_ocr(self: CaseContext, *, force: bool = False) -> dict[str, Any]:
        self._ocr = ocr_frame
        self._dirty = False
        return ocr_frame

    monkeypatch.setattr(actions_mod, "request", fake_request)
    monkeypatch.setattr(actions_mod, "GeometryGuard", lambda: SimpleNamespace(check=lambda sample: None))
    monkeypatch.setattr(CaseContext, "_settle", lambda self, **_kw: None)
    monkeypatch.setattr(CaseContext, "ocr_now", fake_ocr)
    monkeypatch.setattr(actions_mod.time, "sleep", lambda _s: None)
    return taps


# ── 1. Binary resolution, build-record staleness, deployment & VoiceOver guard ──


def test_resolve_local_probe_checks_missing_and_staleness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(probe_mod.PROBE_ENV, raising=False)
    fake_root = tmp_path / "repo"
    (fake_root / "support" / "guest-ax-probe").mkdir(parents=True)
    src = fake_root / "support" / "guest-ax-probe" / "ax_runtime_probe.m"
    ent = fake_root / "support" / "guest-ax-probe" / "entitlements.plist"
    src.write_bytes(b"// probe source v1\n")
    ent.write_bytes(b"<plist>v1</plist>\n")

    with pytest.raises(AxUnavailable, match="missing guest_ax_probe binary"):
        resolve_local_probe(code_root=fake_root)

    bin_path = fake_root / "support" / "guest_ax_probe"
    bin_bytes = b"compiled-probe-v1"
    bin_path.write_bytes(bin_bytes)
    record_path = Path(f"{bin_path}.build.json")
    record = {
        "schema": "autophone.guest-helper-build/v1",
        "protocol": "autophone.ax_probe/1",
        "probe_version": 1,
        "binary_sha256": _sha256_bytes(bin_bytes),
        "source_sha256": _sha256_bytes(src.read_bytes()),
        "entitlements_sha256": _sha256_bytes(ent.read_bytes()),
    }
    record_path.write_text(json.dumps(record), encoding="utf-8")
    resolved = resolve_local_probe(code_root=fake_root)
    assert resolved.binary == bin_path.resolve()
    assert resolved.binary_sha256 == _sha256_bytes(bin_bytes)

    # Source drift is rejected fail-closed.
    src.write_bytes(b"// probe source v2\n")
    with pytest.raises(AxUnavailable, match="stale guest_ax_probe binary"):
        resolve_local_probe(code_root=fake_root)
    src.write_bytes(b"// probe source v1\n")

    # Entitlements drift is rejected fail-closed.
    ent.write_bytes(b"<plist>v2</plist>\n")
    with pytest.raises(AxUnavailable, match="entitlements.plist changed"):
        resolve_local_probe(code_root=fake_root)
    ent.write_bytes(b"<plist>v1</plist>\n")

    # Explicit env override resolves custom binary.
    override = _make_fake_probe_binary(tmp_path / "override", content=b"custom-bin")
    monkeypatch.setenv(probe_mod.PROBE_ENV, str(override))
    overridden = resolve_local_probe(code_root=fake_root)
    assert overridden.binary == override.resolve()


def test_runner_deploy_caching_redeploy_and_voiceover_guard(tmp_path: Path) -> None:
    forget_probe_deployments()
    bin_path = _make_fake_probe_binary(tmp_path, content=b"probe-bytes-deploy")
    local = LocalGuestAxProbe(bin_path, _sha256_bytes(b"probe-bytes-deploy"), None)

    guest_fs: dict[str, str] = {}
    state = {"probe_calls": 0, "voiceover": False, "drop_on_first_run": False}

    def fake_request(msg: dict[str, Any], **_kw: Any) -> tuple[dict[str, Any], None]:
        t = msg.get("t")
        if t == "file_put":
            guest_fs[msg["path"]] = local.binary_sha256
            return {"ok": True}, None
        if t == "shell":
            cmd = str(msg.get("cmd") or "")
            if "echo ABSENT" in cmd:
                digest = guest_fs.get(local.guest_path)
                return {"ok": True, "code": 0, "stdout": f"{digest}  {local.guest_path}\n" if digest else "ABSENT\n", "stderr": ""}, None
            state["probe_calls"] += 1
            if state["drop_on_first_run"] and state["probe_calls"] == 1:
                guest_fs.pop(local.guest_path, None)
                return {"ok": True, "code": 127, "stdout": "", "stderr": "not found"}, None
            payload = _probe_raw_payload(request_id="req-deploy", voiceover=state["voiceover"])
            return {"ok": True, "code": 0, "stdout": json.dumps(payload), "stderr": ""}, None
        raise AssertionError(f"unexpected UDS command: {msg}")

    runner = GuestAxProbeRunner(
        sock_path=str(tmp_path / "vphone.sock"),
        request_fn=fake_request,
        local=local,
    )
    dep1 = runner.ensure_deployed()
    assert dep1["action"] == "pushed"
    dep2 = runner.ensure_deployed()
    assert dep2["action"] == "cached"

    # Exit 127 triggers cache invalidation + automatic redeploy + retry.
    state["drop_on_first_run"] = True
    raw = runner.run_probe(pid=4242, request_id="req-deploy")
    assert raw["ax_backend"] == "guest_axruntime"
    assert state["probe_calls"] == 2

    # VoiceOver enabled on guest MUST fail closed.
    state["drop_on_first_run"] = False
    state["voiceover"] = True
    with pytest.raises(AxUnavailable, match="VoiceOver unexpectedly enabled"):
        runner.run_probe(pid=4242, request_id="req-deploy")


# ── 2. Mandatory --pid Foreground Gate & tap_ax fail-closed occlusion ─────────


@pytest.mark.parametrize(
    ("override_kwargs", "expected_reason"),
    [
        ({"is_control_center_visible": True, "primary_app": {"pid": 50, "bundle_id": "com.apple.springboard"}}, "control_center_visible"),
        ({"is_screen_locked": True}, "screen_locked"),
        ({"primary_app": {"pid": 999, "bundle_id": "com.apple.Preferences"}}, "foreground_app_mismatch:com.apple.Preferences"),
        ({"primary_app": {"pid": 9999, "bundle_id": BUNDLE_ID}}, "foreground_pid_mismatch:9999!=4242"),
        ({"occluded_scenes": [f"sceneID:{BUNDLE_ID}-default"]}, "scene_occluded"),
    ],
)
def test_foreground_gate_strips_background_nodes_and_blocks_tap_ax(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override_kwargs: dict[str, Any],
    expected_reason: str,
) -> None:
    raw = _probe_raw_payload(**override_kwargs)
    normalized = normalize_probe_to_ax_v1(
        raw,
        app=BUNDLE_ID,
        request_id="req-fg-gate",
        pid=4242,
        capabilities_screen={"width": 1290, "height": 2796, "scale": 3},
    )
    assert normalized["foreground_active"] is False
    assert normalized["foreground_occlusion_reason"] == expected_reason
    assert normalized["nodes"] == []
    assert normalized["suppressed_background_node_count"] == 2
    assert "dumper_version" not in normalized
    assert "variant_observation" not in normalized

    dump = parse_dump(normalized, ax_backend="guest_axruntime")
    assert dump.foreground_active is False
    assert dump.foreground_occlusion_reason == expected_reason

    taps = _patch_ctx_io(monkeypatch)
    fake_ax = SimpleNamespace(ax_backend="guest_axruntime", refresh=lambda: dump)
    ctx = CaseContext(case_id="R_FG", shot_dir=tmp_path / "shots", ax=fake_ax, ax_backend="guest_axruntime")

    # Even WITHOUT screen_id and WITH fallback_xy, foreground gate must fail closed with 0 UDS taps!
    with pytest.raises(StepFailure) as exc_info:
        ctx.tap_ax(
            identifier="home.ai_prompt.preferences",
            role="button",
            fallback_xy=(215, 420),
        )
    assert exc_info.value.result.kind == "overlay_occluded"
    assert expected_reason in exc_info.value.result.detail
    assert taps == []


# ── 3. Contract §4.1, §4.3, §4.4 & AxSession end-to-end ──────────────────────


def test_ax_session_guest_axruntime_contracts_41_43_44_and_checkpoint_v2(
    tmp_path: Path,
) -> None:
    forget_probe_deployments()
    bin_path = _make_fake_probe_binary(tmp_path, content=b"probe-e2e")
    plist_bytes = plistlib.dumps(
        {
            "CFBundleIdentifier": BUNDLE_ID,
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1",
        }
    )

    state: dict[str, Any] = {
        "short_version": "",
        "bundle_version": "",
        "nodes": None,
        "probe_scale": 3.0,
        "probe_w": 430.0,
        "probe_h": 932.0,
        "control_center": False,
    }

    def fake_request(msg: dict[str, Any], **_kw: Any) -> tuple[dict[str, Any], Any]:
        t = msg.get("t")
        if t == "capabilities":
            return {
                "ok": True,
                "protocol": "upstream_v1",
                "commands": {"shell": True, "file_get": True, "app_list": True},
                "screen": {"width": 1290, "height": 2796, "scale": 3},
            }, None
        if t == "app_list":
            return {
                "ok": True,
                "apps": [
                    {
                        "bundle_id": BUNDLE_ID,
                        "pid": 4242,
                        "path": "/var/containers/Bundle/Application/UUID/Food.app",
                    }
                ],
            }, None
        if t == "file_get":
            Path(msg["save"]).write_bytes(plist_bytes)
            return {"ok": True}, None
        if t == "file_put":
            return {"ok": True}, None
        if t == "shell":
            cmd = str(msg.get("cmd") or "")
            if "echo ABSENT" in cmd:
                return {"ok": True, "code": 0, "stdout": f"{_sha256_bytes(b'probe-e2e')}  /var/jb/tmp/bin\n", "stderr": ""}, None
            req_id = cmd.split("--request-id ")[1].split()[0].strip("'")
            payload = _probe_raw_payload(
                request_id=req_id,
                short_version=state["short_version"],
                bundle_version=state["bundle_version"],
                scale=state["probe_scale"],
                screen_w=state["probe_w"],
                screen_h=state["probe_h"],
                is_control_center_visible=state["control_center"],
                primary_app=(
                    {"pid": 50, "bundle_id": "com.apple.springboard"}
                    if state["control_center"]
                    else None
                ),
                nodes=state["nodes"],
            )
            return {"ok": True, "code": 0, "stdout": json.dumps(payload), "stderr": ""}, None
        raise AssertionError(f"unexpected request: {msg}")

    out_file = tmp_path / "latest.json"
    session = AxSession(
        BUNDLE_ID,
        sock_path=str(tmp_path / "vphone.sock"),
        out=str(out_file),
        push=False,
        request_fn=fake_request,
        ax_backend="guest_axruntime",
        probe_path=bin_path,
    )
    assert session.host is None
    with session:
        # Contract §4.1: Info.plist fallback supplies missing short_version / bundle_version.
        snap = session.snapshot()
        assert snap["ax_backend"] == "guest_axruntime"
        assert snap["snapshot_kind"] == "ui_structure"
        assert snap["short_version"] == "1.0.0"
        assert snap["bundle_version"] == "1"
        assert snap["screen"] == {"scale": 3.0, "w": 430.0, "h": 932.0}
        assert out_file.is_file()

        # Validates cleanly under operation-ax-checkpoint.v2.json!
        run_dir = tmp_path / "run_cp"
        (run_dir / "shots").mkdir(parents=True)
        (run_dir / "shots" / "cp_01.jpg").write_bytes(b"\xff\xd8\xff\xd9")
        bi_v2 = {
            "schema_version": "v2",
            "run_id": "run_s3_01",
            "app": "food_ordering",
            "identity_state": "confirmed",
            "identity_source": "combined",
            "subject_build_key": "build-v1:" + SHA_A,
            "device_observation": {
                "bundle_id": BUNDLE_ID,
                "short_version": "1.0.0",
                "build_version": "1",
            },
            "match": {"state": "exact"},
            "subject_epochs": [],
            "updated_at": "2026-11-27T10:00:00Z",
            "candidate_identity": None,
        }
        (run_dir / "build-identity.json").write_text(json.dumps(bi_v2), encoding="utf-8")
        cp_res = ax_checkpoint.capture(
            session,
            run_dir=run_dir,
            identity={
                "run_id": "run_s3_01",
                "app": "food_ordering",
                "build": "1.0.0-1",
                "bundle_id": BUNDLE_ID,
                "operation": {"id": "OP1", "name": "open_prefs", "revision": 1},
            },
            checkpoint={"expected": "S01_home", "matched": "S01_home", "shot": "shots/cp_01.jpg", "attempt": 1},
            label="after_home",
            build_evidence={
                "package": {
                    "registry_build_ref": {
                        "app": "food_ordering",
                        "build_id": "bld-v1",
                        "source_revision": {"kind": "unknown", "state": "unknown"},
                    },
                    "device_build_observation": {
                        "bundle_id": BUNDLE_ID,
                        "short_version": "1.0.0",
                        "build_version": "1",
                    },
                    "build_identity_match": "match",
                    "comparison_basis": "candidate_build_ref",
                    "comparison_granularity": "info_plist",
                    "comparison_build_id": "bld-v1",
                },
                "sources": {
                    "run-submission.json": {"path": "run-submission.json", "sha256": SHA_B},
                    "world-identity.json": {"path": "world-identity.json", "sha256": SHA_C},
                },
            },
        )
        assert cp_res["status"] == "captured"
        assert cp_res["ax_backend"] == "guest_axruntime"
        saved_cp = json.loads((run_dir / cp_res["path"]).read_text(encoding="utf-8"))
        validate_or_raise("operation-ax-checkpoint.v2.json", saved_cp)

        # Contract §4.3: empty foreground tree is rejected fail-closed.
        state["nodes"] = []
        with pytest.raises(AxUnavailable, match="empty AX tree"):
            session.refresh(request_id="req-empty")
        state["nodes"] = None

        # Control Center occlusion blocks snapshot() fail-closed.
        state["control_center"] = True
        with pytest.raises(AxUnavailable, match="control_center_visible"):
            session.snapshot()
        state["control_center"] = False

        # Contract §4.4: scale mismatch against UDS capabilities fails closed.
        state["probe_scale"] = 2.0
        with pytest.raises(AxUnavailable, match="screen scale mismatch"):
            session.refresh(request_id="req-scale-mismatch")
        state["probe_scale"] = 3.0


def test_screen_geometry_normalization_and_no_hardcoded_scale(tmp_path: Path) -> None:
    # Native-pixel probe screen (1290x2796) aligned against capabilities.screen (1290x2796@3)
    raw_px = _probe_raw_payload(screen_w=1290.0, screen_h=2796.0, scale=3.0)
    raw_px["nodes"][0]["frame"] = [60.0, 600.0, 360.0, 132.0]
    norm = normalize_probe_to_ax_v1(
        raw_px,
        app=BUNDLE_ID,
        request_id="req-px",
        capabilities_screen={"width": 1290, "height": 2796, "scale": 3},
    )
    assert norm["screen"] == {"scale": 3.0, "w": 430.0, "h": 932.0}
    assert norm["nodes"][0]["frame"] == [20.0, 200.0, 120.0, 44.0]

    # Non-zero screen origin rejected fail-closed.
    with pytest.raises(AxUnavailable, match="unexpected screen origin"):
        normalize_probe_to_ax_v1(
            _probe_raw_payload(screen_x=10.0, screen_y=0.0),
            app=BUNDLE_ID,
            request_id="req-origin",
            capabilities_screen={"width": 1290, "height": 2796, "scale": 3},
        )

    # Missing scale without UDS capabilities fails closed (never hardcodes 3.0).
    with pytest.raises(AxUnavailable, match="unable to determine screen scale"):
        normalize_probe_to_ax_v1(
            _probe_raw_payload(scale=0.0),
            app=BUNDLE_ID,
            request_id="req-no-scale",
            capabilities_screen=None,
        )

    # iOS 27.0.1 (24A446) locked/sleeping SpringBoard reports a scale-divided
    # screen frame (430/3 x 932/3); when occluded/locked (nodes=[]), preserve
    # foreground_occlusion_reason using UDS capabilities.screen, while still
    # failing closed if foreground_active=True reports mismatched geometry.
    locked_dim = normalize_probe_to_ax_v1(
        _probe_raw_payload(
            screen_w=430.0 / 3.0,
            screen_h=932.0 / 3.0,
            scale=3.0,
            is_screen_locked=True,
        ),
        app=BUNDLE_ID,
        request_id="req-ios27-locked-dim",
        capabilities_screen={"width": 1290, "height": 2796, "scale": 3},
    )
    assert locked_dim["foreground_active"] is False
    assert locked_dim["foreground_occlusion_reason"] == "screen_locked"
    assert locked_dim["nodes"] == []
    assert locked_dim["screen"] == {"scale": 3.0, "w": 430.0, "h": 932.0}

    with pytest.raises(AxUnavailable, match="screen geometry mismatch"):
        normalize_probe_to_ax_v1(
            _probe_raw_payload(screen_w=430.0 / 3.0, screen_h=932.0 / 3.0, scale=3.0),
            app=BUNDLE_ID,
            request_id="req-fg-mismatch",
            capabilities_screen={"width": 1290, "height": 2796, "scale": 3},
        )

    # build_run_ax_session with controlled capture session constructs guest_axruntime without borrowed_lifetime.
    profile = SimpleNamespace(
        app="food_ordering",
        bundle_id=BUNDLE_ID,
        ax_backend="guest_axruntime",
        raw={"capabilities": {"ax": {"enabled": True, "backend": "guest_axruntime"}}},
    )
    controlled = SimpleNamespace(
        _controlled_identity={"digest": SHA_A},
        _lifetime=None,
        pid=4242,
        _owned_pid=4242,
        bundle_id=BUNDLE_ID,
    )
    run_dir = tmp_path / "run_ctrl"
    run_dir.mkdir()
    built = build_run_ax_session(profile, run_dir, capture_session=controlled)
    assert built is not None and built.ax_backend == "guest_axruntime"
    assert built._target_pid == 4242


# ── 4. Contract §4.2: variant_evidence decoupling & cross-backend isolation ──


def test_variant_evidence_decoupled_for_ui_structure_and_cross_backend_isolation(
    tmp_path: Path,
) -> None:
    screen_ax = {
        "regions": {"header": {"rect": [0.0, 0.0, 1.0, 0.25]}},
        "variant_checks": {
            "auth": {
                "source": "ax_ui_text",
                "region": "header",
                "values": {
                    "logged_in": {"all": ["今晚，好好吃一顿"], "none": ["请登录"]},
                    "logged_out": {"all": ["请登录"], "none": ["今晚，好好吃一顿"]},
                },
            }
        },
    }
    snap_ui = normalize_probe_to_ax_v1(
        _probe_raw_payload(),
        app=BUNDLE_ID,
        request_id="req-var-1",
        capabilities_screen={"width": 1290, "height": 2796, "scale": 3},
    )
    variant_evidence.classify(snap_ui, screen_ax, {"auth": "logged_in"})

    # Occluded ui_structure snapshot fails variant_app_not_foreground.
    snap_occ = normalize_probe_to_ax_v1(
        _probe_raw_payload(is_control_center_visible=True, primary_app={"pid": 50, "bundle_id": "com.apple.springboard"}),
        app=BUNDLE_ID,
        request_id="req-var-occ",
        capabilities_screen={"width": 1290, "height": 2796, "scale": 3},
    )
    with pytest.raises(EvidenceError, match="variant_app_not_foreground"):
        variant_evidence.classify(snap_occ, screen_ax, {"auth": "logged_in"})

    # In-process gray-box rules fail closed on ui_structure snapshots.
    screen_graybox = {
        "regions": {"header": {"rect": [0.0, 0.0, 1.0, 0.25]}},
        "variant_checks": {
            "location": {
                "source": "core_location_authorization",
                "values": {"granted": [3, 4], "denied": [0, 1, 2]},
            }
        },
    }
    with pytest.raises(EvidenceError, match="variant_inproc_graybox_required"):
        variant_evidence.classify(snap_ui, screen_graybox, {"location": "granted"})

    # screen-asset.v1.json accepts source="ax_ui_text".
    validate_or_raise(
        "screen-asset.v1.json",
        {
            "meta": {"schema_version": 1, "app": "food_ordering", "build": "1.0.0-1"},
            "screens": [{"id": "S01_home", "status": "active", "regions": screen_ax["regions"], "variant_checks": screen_ax["variant_checks"]}],
        },
    )

    # Cross-backend comparison is strictly rejected by ax_comparison.compare().
    run_a = tmp_path / "run_a"
    run_b = tmp_path / "run_b"
    out_dir = tmp_path / "impact"
    private = out_dir / "private"
    for r in (run_a, run_b, private):
        (r / "shots").mkdir(parents=True, exist_ok=True)
        (r / "planes" / "ax" / "private").mkdir(parents=True, exist_ok=True)
    screen_asset = tmp_path / "screens.yaml"
    screen_asset.write_text("meta:\n  schema_version: 1\n  build: '1.0.0-1'\nscreens: []\n", encoding="utf-8")

    def _write_cp(run_dir: Path, backend: str, req_id: str, ts: int) -> tuple[dict[str, Any], dict[str, Any]]:
        shot_rel = "shots/cp.jpg"
        (run_dir / shot_rel).write_bytes(b"\xff\xd8\xff\xd9")
        shot_sha = _sha256_bytes((run_dir / shot_rel).read_bytes())
        cp_doc = {
            "schema_version": "v1",
            "identity": {
                "run_id": run_dir.name,
                "app": "food_ordering",
                "build": "1.0.0-1",
                "bundle_id": BUNDLE_ID,
                "operation": {"id": "OP1", "name": "op", "revision": 1},
            },
            "checkpoint": {"expected": "S01", "matched": "S01", "shot": shot_rel, "attempt": 1},
            "shot_sha256": shot_sha,
            "snapshot": {
                "schema": "autophone.ax/1",
                "evidence_protocol": 1,
                "request_id": req_id,
                "captured_at_ms": ts,
                "app": BUNDLE_ID,
                "ax_backend": backend,
                "complete": True,
                "truncated": False,
                "short_version": "1.0.0",
                "bundle_version": "1",
                "screen": {"scale": 3.0, "w": 430.0, "h": 932.0},
                "nodes": [
                    {
                        "path": "0/0",
                        "label": "偏好",
                        "role": "button",
                        "traits": ["button"],
                        "enabled": True,
                        "value": None,
                        "identifier": "home.ai_prompt.preferences",
                        "frame": [20.0, 200.0, 120.0, 44.0],
                    }
                ],
            },
            "build_identity": {
                "schema_version": "v1",
                "run_id": run_dir.name,
                "app": "food_ordering",
                "identity_state": "confirmed",
                "identity_source": "uds_info_plist",
                "subject_build_key": f"build-v1:{_sha256_bytes(chr(0).join((BUNDLE_ID, '1.0.0', '1')).encode())}",
                "device_observation": {"bundle_id": BUNDLE_ID, "short_version": "1.0.0", "build_version": "1"},
                "match": {"state": "exact"},
                "subject_epochs": [],
                "updated_at": "2026-11-27T10:00:00Z",
            },
        }
        cp_path = run_dir / "planes" / "ax" / "private" / "cp.json"
        cp_bytes = json.dumps(cp_doc).encode("utf-8")
        cp_path.write_bytes(cp_bytes)
        doc = {"run_id": run_dir.name, "app": "food_ordering", "build": "1.0.0-1", "operation": {"id": "OP1", "name": "op", "revision": 1}}
        cp = {
            "expected": "S01",
            "matched": "S01",
            "shot": shot_rel,
            "attempt": 1,
            "ax_evidence": {"status": "captured", "path": "planes/ax/private/cp.json", "sha256": _sha256_bytes(cp_bytes)},
        }
        return doc, cp

    before_doc, before_cp = _write_cp(run_a, "frida_inproc", "r-1", 1000)
    after_doc, after_cp = _write_cp(run_b, "guest_axruntime", "r-2", 2000)
    with pytest.raises(EvidenceError, match="ax_backend_mismatch"):
        ax_comparison.compare(
            before_dir=run_a,
            after_dir=run_b,
            before_doc=before_doc,
            after_doc=after_doc,
            before_cp=before_cp,
            after_cp=after_cp,
            regions={"full": (0.0, 0.0, 1.0, 1.0)},
            excluded=[],
            private=private,
            out_dir=out_dir,
            screen_asset=screen_asset,
        )


def test_variant_evidence_compare_ui_structure_and_backend_mismatch(tmp_path: Path) -> None:
    import yaml

    screen_asset = tmp_path / "screens.yaml"
    screen_doc = {
        "meta": {"schema_version": 1, "app": "food_ordering", "build": "1.0.0-1"},
        "screens": [
            {
                "id": "S01_home",
                "status": "active",
                "regions": {"header": {"rect": [0.0, 0.0, 1.0, 0.25]}},
                "variant_checks": {
                    "auth": {
                        "source": "ax_ui_text",
                        "region": "header",
                        "values": {
                            "logged_in": {"all": ["今晚，好好吃一顿"], "none": ["请登录"]},
                            "logged_out": {"all": ["请登录"], "none": ["今晚，好好吃一顿"]},
                        },
                    }
                },
            }
        ],
    }
    screen_asset.write_text(yaml.safe_dump(screen_doc, allow_unicode=True), encoding="utf-8")

    def _build_v2_run(run_dir: Path, *, backend: str, req_id: str, ts: int) -> dict[str, Any]:
        (run_dir / "shots").mkdir(parents=True, exist_ok=True)
        (run_dir / "planes" / "ax" / "private").mkdir(parents=True, exist_ok=True)
        shot_rel = "shots/cp.jpg"
        (run_dir / shot_rel).write_bytes(b"\xff\xd8\xff\xd9")
        shot_sha = _sha256_bytes((run_dir / shot_rel).read_bytes())

        sub_bytes = json.dumps({"run_id": run_dir.name, "app": "food_ordering"}).encode("utf-8")
        world_bytes = json.dumps({"run_id": run_dir.name}).encode("utf-8")
        (run_dir / "run-submission.json").write_bytes(sub_bytes)
        (run_dir / "world-identity.json").write_bytes(world_bytes)

        if backend == "guest_axruntime":
            snap = normalize_probe_to_ax_v1(
                _probe_raw_payload(request_id=req_id, captured_at_ms=ts),
                app=BUNDLE_ID,
                request_id=req_id,
                capabilities_screen={"width": 1290, "height": 2796, "scale": 3},
            )
        else:
            snap = {
                "schema": "autophone.ax/1",
                "evidence_protocol": 1,
                "request_id": req_id,
                "captured_at_ms": ts,
                "app": BUNDLE_ID,
                "ax_backend": "frida_inproc",
                "dumper_version": 9,
                "application_state": 0,
                "key_window_scene_activation_state": 0,
                "key_window_is_app_window": True,
                "complete": True,
                "truncated": False,
                "short_version": "1.0.0",
                "bundle_version": "1",
                "screen": {"scale": 3.0, "w": 430.0, "h": 932.0},
                "nodes": [
                    {
                        "path": "0/0",
                        "label": "今晚，好好吃一顿",
                        "role": "header",
                        "traits": ["header"],
                        "enabled": True,
                        "value": None,
                        "identifier": "home.title",
                        "frame": [20.0, 60.0, 260.0, 36.0],
                    }
                ],
                "variant_observation": {
                    "protocol": 1,
                    "complete": True,
                    "location_authorization_status": 4,
                    "ui_text_nodes": [{"text": "今晚，好好吃一顿", "frame": [20.0, 60.0, 260.0, 36.0]}],
                },
            }

        cp_doc = {
            "schema_version": "v2",
            "ax_backend": backend,
            "identity": {
                "run_id": run_dir.name,
                "app": "food_ordering",
                "build": "1.0.0-1",
                "bundle_id": BUNDLE_ID,
                "operation": {"id": "OP1", "name": "op", "revision": 1},
            },
            "checkpoint": {"expected": "S01_home", "matched": "S01_home", "shot": shot_rel, "attempt": 1},
            "shot_sha256": shot_sha,
            "snapshot": snap,
            "build_identity": {
                "schema_version": "v2",
                "run_id": run_dir.name,
                "app": "food_ordering",
                "identity_state": "confirmed",
                "identity_source": "uds_info_plist",
                "subject_build_key": f"build-v1:{_sha256_bytes(chr(0).join((BUNDLE_ID, '1.0.0', '1')).encode())}",
                "device_observation": {"bundle_id": BUNDLE_ID, "short_version": "1.0.0", "build_version": "1"},
                "match": {"state": "exact"},
                "subject_epochs": [],
                "updated_at": "2026-11-27T10:00:00Z",
                "candidate_identity": None,
            },
            "build_evidence": {
                "package": {
                    "registry_build_ref": {
                        "app": "food_ordering",
                        "build_id": "bld-v1",
                        "source_revision": {"kind": "unknown", "state": "unknown"},
                    },
                    "device_build_observation": {
                        "bundle_id": BUNDLE_ID,
                        "short_version": "1.0.0",
                        "build_version": "1",
                    },
                    "build_identity_match": "match",
                    "comparison_basis": "candidate_build_ref",
                    "comparison_granularity": "info_plist",
                    "comparison_build_id": "bld-v1",
                },
                "sources": {
                    "run-submission.json": {"path": "run-submission.json", "sha256": _sha256_bytes(sub_bytes)},
                    "world-identity.json": {"path": "world-identity.json", "sha256": _sha256_bytes(world_bytes)},
                },
            },
        }
        cp_path = run_dir / "planes" / "ax" / "private" / "cp.json"
        cp_bytes = json.dumps(cp_doc).encode("utf-8")
        cp_path.write_bytes(cp_bytes)
        return {
            "run_id": run_dir.name,
            "app": "food_ordering",
            "build": "1.0.0-1",
            "operation": {"id": "OP1", "name": "op", "revision": 1},
            "steps": [
                {
                    "status": "passed",
                    "screen_checkpoint": {
                        "expected": "S01_home",
                        "matched": "S01_home",
                        "shot": shot_rel,
                        "attempt": 1,
                        "status": "passed",
                        "ax_evidence": {
                            "status": "captured",
                            "path": "planes/ax/private/cp.json",
                            "sha256": _sha256_bytes(cp_bytes),
                        },
                    },
                }
            ],
        }

    plan = {"app": "food_ordering", "build": "1.0.0-1"}
    target = {"target_id": "tgt-1", "screen_id": "S01_home", "variant_selector": {"auth": "logged_in"}}

    run_g1 = tmp_path / "run_g1"
    run_g2 = tmp_path / "run_g2"
    run_f1 = tmp_path / "run_f1"
    doc_g1 = _build_v2_run(run_g1, backend="guest_axruntime", req_id="rg-1", ts=1000)
    doc_g2 = _build_v2_run(run_g2, backend="guest_axruntime", req_id="rg-2", ts=2000)
    doc_f1 = _build_v2_run(run_f1, backend="frida_inproc", req_id="rf-1", ts=500)

    out_ok = tmp_path / "out_ok"
    out_ok.mkdir()
    receipt, _frozen = variant_evidence.compare(
        plan=plan,
        target=target,
        before_dir=run_g1,
        after_dir=run_g2,
        before_doc=doc_g1,
        after_doc=doc_g2,
        screen_asset=screen_asset,
        out_dir=out_ok,
    )
    assert receipt["status"] == "compared"
    assert receipt["scope"] == "ui_structure_checkpoint_variant"

    out_mix = tmp_path / "out_mix"
    out_mix.mkdir()
    with pytest.raises(EvidenceError, match="variant_ax_backend_mismatch"):
        variant_evidence.compare(
            plan=plan,
            target=target,
            before_dir=run_f1,
            after_dir=run_g2,
            before_doc=doc_f1,
            after_doc=doc_g2,
            screen_asset=screen_asset,
            out_dir=out_mix,
        )


def test_public_guest_ax_probe_source_entitlements_and_build_script_layout() -> None:
    probe_src = REPO_ROOT / "support" / "guest-ax-probe" / "ax_runtime_probe.m"
    entitlements = REPO_ROOT / "support" / "guest-ax-probe" / "entitlements.plist"
    build_script = REPO_ROOT / "scripts" / "build_guest_ax_probe.sh"

    assert probe_src.is_file()
    assert entitlements.is_file()
    assert build_script.is_file()
    assert bool(build_script.stat().st_mode & stat.S_IXUSR)
    assert os.access(build_script, os.X_OK)
    if (REPO_ROOT / ".git").exists():
        assert not subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), "ls-files", "tools/guest"], text=True
        ).strip()
    else:
        assert not (REPO_ROOT / "tools" / "guest").exists()

    src_text = probe_src.read_text(encoding="utf-8")
    non_comment_src = "\n".join(
        line for line in src_text.splitlines() if not line.lstrip().startswith("//")
    )
    assert "_AXSVoiceOverTouchSetEnabled" not in non_comment_src
    assert "_AXUIElementCreateAppElementWithPid" in non_comment_src
    assert "axIdentifier" in non_comment_src

    parsed_ent = plistlib.loads(entitlements.read_bytes())
    assert parsed_ent == {"com.apple.accessibility.api": True}
    assert "sysLockedNow || sysCCNow || (primPidNow > 0 && primPidNow != targetPid)" in non_comment_src


def test_voiceover_int_or_bool_and_background_suspended_pid_fail_closed(tmp_path: Path) -> None:
    # Both bool True and int 1 in ax_state.voiceover must fail closed.
    for vo_val in (True, 1):
        payload = _probe_raw_payload()
        payload["ax_state"] = {"app_accessibility": True, "automation": True, "voiceover": vo_val}
        with pytest.raises(AxUnavailable, match="VoiceOver unexpectedly enabled"):
            normalize_probe_to_ax_v1(
                payload,
                app=BUNDLE_ID,
                request_id="req-vo",
                capabilities_screen={"width": 1290, "height": 2796, "scale": 3},
            )

    # Backgrounded/suspended --pid payload (probe early-break: bundle_id="", nodes=[])
    bg_payload = _probe_raw_payload(
        bundle_id="",
        nodes=[],
        primary_app={"pid": 38, "bundle_id": "com.apple.springboard", "process_name": "SpringBoard"},
    )
    norm_bg = normalize_probe_to_ax_v1(
        bg_payload,
        app=BUNDLE_ID,
        request_id="req-bg-pid",
        capabilities_screen={"width": 1290, "height": 2796, "scale": 3},
    )
    assert norm_bg["foreground_active"] is False
    assert norm_bg["nodes"] == []
    assert "com.apple.springboard" in norm_bg["foreground_occlusion_reason"]

    # CaseContext.tap_ax fails closed with overlay_occluded and 0 UDS taps even when fallback_xy is set.
    taps: list[dict[str, Any]] = []
    stub_ax = SimpleNamespace(
        ax_backend="guest_axruntime",
        refresh=lambda: parse_dump(norm_bg),
    )
    ctx = CaseContext(
        case_id="bg_gate",
        shot_dir=tmp_path / "shots",
        sock_path=str(tmp_path / "missing.sock"),
        ax=stub_ax,
        ax_backend="guest_axruntime",
    )
    ctx._settle = lambda *a, **kw: None  # type: ignore[method-assign]
    ctx._request_dispatch = lambda msg, **kw: (taps.append(msg) or {"ok": True}, None)  # type: ignore[method-assign]
    with pytest.raises(StepFailure) as exc_info:
        ctx.tap_ax(identifier="home.ai_prompt.preferences", fallback_xy=(215, 420))
    assert exc_info.value.result.kind == "overlay_occluded"
    assert taps == []


# ── 5. Stage 3 Zero-Frida Gates (Gaps #1-#4) ─────────────────────────────────


def _make_stage3_app(
    tmp_path: Path,
    *,
    app: str = "stage3_demo",
    ax_enabled: bool = True,
    ax_backend: str | None = "guest_axruntime",
    api_capture_enabled: bool | None = None,
    profile_requirements: dict[str, str] | None = None,
    cases: list[dict[str, Any]] | None = None,
    recipes: dict[str, dict[str, Any]] | None = None,
    operations: list[dict[str, Any]] | None = None,
):
    import yaml
    from autophone.assets.apps import AppProfile

    app_dir = tmp_path / "apps" / app
    (app_dir / "cases").mkdir(parents=True, exist_ok=True)
    (app_dir / "recipes").mkdir(parents=True, exist_ok=True)
    (app_dir / "operations").mkdir(parents=True, exist_ok=True)

    ax_cfg: dict[str, Any] = {"enabled": ax_enabled}
    if ax_backend is not None:
        ax_cfg["backend"] = ax_backend
    capabilities: dict[str, Any] = {
        "ax": ax_cfg,
    }
    if api_capture_enabled is not None:
        capabilities["api_capture"] = {"enabled": api_capture_enabled}
    raw: dict[str, Any] = {
        "schema_version": "v1",
        "app": app,
        "bundle_id": BUNDLE_ID,
        "display_name": "Stage3 Demo",
        "env": "verification",
        "lifecycle": "active",
        "build_label": "1.0.0-1",
        "capabilities": capabilities,
    }
    if profile_requirements is not None:
        raw["requirements"] = dict(profile_requirements)
    (app_dir / "profile.yaml").write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    case_rows = cases if cases is not None else [
        {
            "id": "R1",
            "revision": 1,
            "status": "active",
            "automation": "ui_ready",
            "module": "home",
            "item": "guest ax home check",
            "login_precondition": "any",
        }
    ]
    (app_dir / "cases" / "cases.yaml").write_text(
        yaml.safe_dump(
            {"meta": {"schema_version": 2, "app": app}, "cases": case_rows},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    recipe_docs = recipes if recipes is not None else {
        "R1": {
            "id": "R1",
            "steps": [{"action": "assert_present", "queries": ["今晚，好好吃一顿"], "wait_s": 0}],
        }
    }
    for cid, doc in recipe_docs.items():
        (app_dir / "recipes" / f"{cid.lower()}.yaml").write_text(
            yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    if operations is not None:
        (app_dir / "operations" / "operations.yaml").write_text(
            yaml.safe_dump(
                {"meta": {"schema_version": 1, "app": app, "next_seq": len(operations) + 1}, "operations": operations},
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
    return AppProfile(raw=raw, root=tmp_path)


def _make_stage3_run_execution(
    tmp_path: Path,
    *,
    capture_session_factory: Any = None,
    crash_trap_factory: Any = None,
    app_hygiene_factory: Any = None,
):
    from autophone.device.lock import ScreenLock
    from autophone.execution.run_execution import (
        RunExecution,
        _default_crash_trap_factory,
    )
    from autophone.rig.maintenance import RigMaintenance

    return RunExecution(
        lock=ScreenLock(root=tmp_path / "locks"),
        rig_maintenance=RigMaintenance(None, capability_support=[]),
        executor=None,
        run_dir=lambda rid: tmp_path / "runs" / rid,
        persist_result=lambda rdir, payload: (
            (rdir / "platform-run-result.json").write_text(json.dumps(payload), encoding="utf-8")
            and (rdir / "platform-run-result.json")
        )
        or (rdir / "platform-run-result.json"),
        set_active=lambda *a, **k: None,
        update_active=lambda *a, **k: None,
        clear_active=lambda *a, **k: None,
        assert_store_context=lambda *a, **k: None,
        bind_store_context=lambda *a, **k: None,
        capture_session_factory=capture_session_factory or (lambda *a, **k: None),
        recorder_factory=lambda *a, **k: None,
        crash_trap_factory=(
            crash_trap_factory if crash_trap_factory is not None else _default_crash_trap_factory
        ),
        telemetry_sleep=lambda _s: None,
        location_quiesce_fn=None,
        app_hygiene_factory=app_hygiene_factory,
    )


def test_gap1_derive_requirements_api_capture_defaults_and_monotonic_upgrades(
    tmp_path: Path,
) -> None:
    from autophone.execution.capability_requirements import derive_requirements

    plain_case = {"id": "R1", "login_precondition": "any"}
    recipe_doc = {"steps": [{"action": "assert_present", "queries": ["首页"]}]}

    # Backward-compatible default when neither api_plane_mode nor profile disables api_capture.
    default_profile = _make_stage3_app(tmp_path / "p1", app="p1", api_capture_enabled=None)
    assert derive_requirements(default_profile, plain_case, recipe_doc)["api_capture"] == "advisory"

    # Explicit api_plane_mode="disabled" lowers the starting default to "disabled".
    assert (
        derive_requirements(default_profile, plain_case, recipe_doc, api_plane_mode="disabled")[
            "api_capture"
        ]
        == "disabled"
    )

    # Profile capabilities.api_capture.enabled=false or requirements.api_capture=disabled defaults to "disabled".
    cap_off_profile = _make_stage3_app(tmp_path / "p2", app="p2", api_capture_enabled=False)
    assert derive_requirements(cap_off_profile, plain_case, recipe_doc)["api_capture"] == "disabled"
    # Explicit advisory plane overrides disabled profile capability default.
    assert (
        derive_requirements(cap_off_profile, plain_case, recipe_doc, api_plane_mode="advisory")[
            "api_capture"
        ]
        == "advisory"
    )

    req_off_profile = _make_stage3_app(
        tmp_path / "p3", app="p3", profile_requirements={"api_capture": "disabled"}
    )
    assert derive_requirements(req_off_profile, plain_case, recipe_doc)["api_capture"] == "disabled"

    # Monotonic upgrades still apply when a case/recipe requires api_capture even if api_plane_mode="disabled".
    entity_case = {
        "id": "R5",
        "login_precondition": "any",
        "entity_binding": {
            "entity_type": "store",
            "entity_id": "store-1",
            "api_window": {"expect": [{"name": "get", "method": "GET", "path": "/api/v1/store"}]},
        },
    }
    assert (
        derive_requirements(default_profile, entity_case, recipe_doc, api_plane_mode="disabled")[
            "api_capture"
        ]
        == "required"
    )
    req_recipe = {"requirements": {"api_capture": "required"}, "steps": recipe_doc["steps"]}
    assert (
        derive_requirements(default_profile, plain_case, req_recipe, api_plane_mode="disabled")[
            "api_capture"
        ]
        == "required"
    )


@pytest.mark.parametrize(
    ("ax_enabled", "ax_backend", "api_mode", "recipe_reqs", "entity_api", "expected_need_frida"),
    [
        (True, None, "disabled", None, False, False),
        (True, "guest_axruntime", "disabled", None, False, False),
        (False, "frida_inproc", "disabled", None, False, False),
        (True, "frida_inproc", "disabled", None, False, True),
        (True, "guest_axruntime", "advisory", None, False, True),
        (True, "guest_axruntime", "required", None, False, True),
        (True, "guest_axruntime", "disabled", {"location_injection": "required"}, False, True),
        (True, "guest_axruntime", "disabled", {"frida": "required"}, False, True),
        (True, "guest_axruntime", "disabled", None, True, True),
    ],
)
def test_gap1_run_execution_need_frida_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ax_enabled: bool,
    ax_backend: str | None,
    api_mode: str,
    recipe_reqs: dict[str, str] | None,
    entity_api: bool,
    expected_need_frida: bool,
) -> None:
    from autophone.execution.engine import runner as runner_mod
    from autophone.rig.supervisor import RigSnapshot

    case: dict[str, Any] = {
        "id": "R1",
        "revision": 1,
        "status": "active",
        "automation": "ui_ready",
        "module": "home",
        "item": "matrix case",
        "login_precondition": "any",
    }
    if entity_api:
        case["entity_binding"] = {
            "entity_type": "store",
            "entity_id": "store-1",
            "api_window": {"expect": [{"name": "get", "method": "GET", "path": "/api/v1/store"}]},
        }
    recipe_doc: dict[str, Any] = {
        "id": "R1",
        "steps": [{"action": "assert_present", "queries": ["今晚，好好吃一顿"], "wait_s": 0}],
    }
    if recipe_reqs:
        recipe_doc["requirements"] = recipe_reqs

    profile = _make_stage3_app(
        tmp_path,
        ax_enabled=ax_enabled,
        ax_backend=ax_backend,
        cases=[case],
        recipes={"R1": recipe_doc},
    )
    run_exec = _make_stage3_run_execution(tmp_path)
    seen_need_frida: list[bool] = []

    def fake_prepare_rig(prof: Any, sdir: Path, *, need_app: bool, need_frida: bool) -> RigSnapshot:
        seen_need_frida.append(need_frida)
        return RigSnapshot(
            vm_uds={"available": True, "evidence": "ok"},
            app_ui={"available": True, "evidence": "ok"},
            frida={
                "available": True,
                "evidence": "probed" if need_frida else "not_required",
            },
            health_guard={"available": True, "evidence": "ok"},
        )

    monkeypatch.setattr(run_exec, "_prepare_rig", fake_prepare_rig)
    monkeypatch.setattr(run_exec, "_probe_build_identity", lambda *a, **k: None)
    monkeypatch.setattr(run_exec, "_ensure_unlocked", lambda *a, **k: None)
    monkeypatch.setattr(run_exec, "_launch_app", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "gate", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr("autophone.observation.capture.build_run_ax_session", lambda *a, **k: None)
    _patch_ctx_io(monkeypatch, ocr_texts=("今晚，好好吃一顿",))

    run_dir = tmp_path / "runs" / "run-gap1"
    run_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "schema_version": "v1",
        "run_id": "run-gap1",
        "type": "regression",
        "app": profile.app,
        "device": "vm-test",
        "case_selector": {"ids": ["R1"]},
        "planes": {"ui": {"mode": "required"}, "api": {"mode": api_mode}},
    }
    run_exec._execute_phase2_cases(spec, profile, run_dir)
    assert seen_need_frida == [expected_need_frida]


def test_gap1_ax_only_operation_requirement_zero_frida_and_availability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autophone.execution import effects
    from autophone.execution.engine import recipe as recipe_mod
    from autophone.execution.engine import runner as runner_mod
    from autophone.rig.supervisor import RigSnapshot

    recipe_doc = {
        "id": "R1",
        "steps": [
            {"operation": "guest_ax_nav"},
            {"action": "assert_present", "queries": ["今晚，好好吃一顿"], "wait_s": 0},
        ],
    }

    def _run_case_with_ax(subdir: str, *, ax_enabled: bool):
        root = tmp_path / subdir
        app_slug = f"ax_op_{subdir}"
        profile = _make_stage3_app(
            root,
            app=app_slug,
            ax_enabled=ax_enabled,
            ax_backend="guest_axruntime",
            recipes={"R1": recipe_doc},
        )
        marker_file = root / "nav_called.txt"
        (profile.recipes_dir() / "__init__.py").write_text(
            "from pathlib import Path\n"
            "from autophone.execution.engine.recipe import load_recipe, register_operation, register_recipe\n"
            "\n"
            "def _guest_ax_nav(ctx):\n"
            f"    Path({str(marker_file)!r}).write_text('ok', encoding='utf-8')\n"
            "\n"
            "register_operation('guest_ax_nav', _guest_ax_nav, effect='reversible', requires=('ax',))\n"
            "register_recipe('R1', load_recipe(Path(__file__).parent / 'r1.yaml'))\n",
            encoding="utf-8",
        )
        resolved_root = str(root.resolve())
        try:
            run_exec = _make_stage3_run_execution(root)
            seen_need_frida: list[bool] = []

            def fake_prepare_rig(prof: Any, sdir: Path, *, need_app: bool, need_frida: bool) -> RigSnapshot:
                seen_need_frida.append(need_frida)
                return RigSnapshot(
                    vm_uds={"available": True, "evidence": "ok"},
                    app_ui={"available": True, "evidence": "ok"},
                    frida={"available": not need_frida, "evidence": "not_required"},
                    health_guard={"available": True, "evidence": "ok"},
                )

            monkeypatch.setattr(run_exec, "_prepare_rig", fake_prepare_rig)
            monkeypatch.setattr(run_exec, "_probe_build_identity", lambda *a, **k: None)
            monkeypatch.setattr(run_exec, "_ensure_unlocked", lambda *a, **k: None)
            monkeypatch.setattr(run_exec, "_launch_app", lambda *a, **k: None)
            monkeypatch.setattr(runner_mod, "gate", lambda *a, **k: (True, "ok"))
            monkeypatch.setattr("autophone.observation.capture.build_run_ax_session", lambda *a, **k: None)
            _patch_ctx_io(monkeypatch, ocr_texts=("今晚，好好吃一顿",))

            run_dir = root / "runs" / f"run-{subdir}"
            run_dir.mkdir(parents=True, exist_ok=True)
            spec = {
                "schema_version": "v1",
                "run_id": f"run-{subdir}",
                "type": "regression",
                "app": profile.app,
                "device": "vm-test",
                "case_selector": {"ids": ["R1"]},
                "planes": {"ui": {"mode": "required"}, "api": {"mode": "disabled"}},
            }
            result, _, _ = run_exec._execute_phase2_cases(spec, profile, run_dir)
            return result, seen_need_frida, marker_file.is_file()
        finally:
            recipe_mod.APP_ROOT_OPERATIONS.pop((profile.app, resolved_root), None)
            recipe_mod.APP_ROOT_OPERATION_REQUIREMENTS.pop((profile.app, resolved_root), None)
            recipe_mod.APP_OPERATIONS.pop(profile.app, None)
            recipe_mod.APP_OPERATION_REQUIREMENTS.pop(profile.app, None)
            effects.clear_app_executor_effects(profile.app, root=root)

    res_ok, frida_ok, nav_called_ok = _run_case_with_ax("enabled", ax_enabled=True)
    assert frida_ok == [False]
    assert nav_called_ok is True
    assert res_ok.outcomes[0].status == "pass"

    res_blocked, frida_blocked, nav_called_blocked = _run_case_with_ax("disabled", ax_enabled=False)
    assert frida_blocked == [False]
    assert nav_called_blocked is False
    assert res_blocked.outcomes[0].status == "blocked"
    assert res_blocked.outcomes[0].blocker["code"] == "ax_unavailable"


def test_gap2_crash_trap_default_disabled_for_zero_frida_and_respects_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autophone.execution.run_execution import (
        _crash_trap_enabled,
        _default_crash_trap_factory,
    )

    monkeypatch.delenv("AUTOPHONE_CRASH_TRAP", raising=False)
    default_ax_profile = _make_stage3_app(
        tmp_path / "default_ax", app="default_ax_app", ax_enabled=True, ax_backend=None
    )
    guest_profile = _make_stage3_app(
        tmp_path / "guest", app="guest_app", ax_enabled=True, ax_backend="guest_axruntime"
    )
    frida_profile = _make_stage3_app(
        tmp_path / "frida", app="frida_app", ax_enabled=True, ax_backend="frida_inproc"
    )
    spec_api_disabled = {
        "run_id": "r-zero",
        "type": "regression",
        "planes": {"ui": {"mode": "required"}, "api": {"mode": "disabled"}},
    }
    spec_api_advisory = {
        "run_id": "r-adv",
        "type": "regression",
        "planes": {"ui": {"mode": "required"}, "api": {"mode": "advisory"}},
    }
    spec_api_required = {
        "run_id": "r-req",
        "type": "regression",
        "planes": {"ui": {"mode": "required"}, "api": {"mode": "required"}},
    }

    # Pure default AX (guest_axruntime) + api disabled -> default OFF.
    assert default_ax_profile.ax_backend == "guest_axruntime"
    assert _crash_trap_enabled(spec_api_disabled, default_ax_profile) is False
    assert _default_crash_trap_factory(default_ax_profile, spec_api_disabled, tmp_path) is None

    # Pure guest_axruntime + api disabled -> default OFF.
    assert _crash_trap_enabled(spec_api_disabled, guest_profile, need_frida=False) is False
    assert _crash_trap_enabled(spec_api_disabled, guest_profile) is False
    assert _default_crash_trap_factory(guest_profile, spec_api_disabled, tmp_path, need_frida=False) is None
    assert _default_crash_trap_factory(guest_profile, spec_api_disabled, tmp_path) is None

    # Existing frida_inproc / API-capture behavior remains default ON.
    assert _crash_trap_enabled(spec_api_disabled, frida_profile, need_frida=True) is True
    assert _crash_trap_enabled(spec_api_disabled, frida_profile) is True
    assert _crash_trap_enabled(spec_api_advisory, guest_profile, need_frida=True) is True
    assert _crash_trap_enabled(spec_api_required, guest_profile, need_frida=True) is True
    # Backward-compatible 1-arg call with no profile/need_frida defaults ON.
    assert _crash_trap_enabled(spec_api_disabled) is True

    # Explicit AUTOPHONE_CRASH_TRAP=1 forces crash_trap ON even on pure guest_axruntime.
    monkeypatch.setenv("AUTOPHONE_CRASH_TRAP", "1")
    assert _crash_trap_enabled(spec_api_disabled, guest_profile, need_frida=False) is True
    # Explicit spec crash_trap=False still overrides env.
    assert _crash_trap_enabled({**spec_api_disabled, "crash_trap": False}, guest_profile, need_frida=False) is False

    # Explicit AUTOPHONE_CRASH_TRAP=0 disables crash_trap even for frida_inproc.
    monkeypatch.setenv("AUTOPHONE_CRASH_TRAP", "0")
    assert _crash_trap_enabled(spec_api_required, frida_profile, need_frida=True) is False
    # Explicit spec crash_trap=True still overrides env.
    assert _crash_trap_enabled({**spec_api_disabled, "crash_trap": True}, guest_profile, need_frida=False) is True


@pytest.mark.parametrize(
    ("scenario", "expect_terminate"),
    [
        ("guest_clean", False),
        ("guest_not_running", False),
        ("guest_pre_residue", True),
        ("guest_pre_check_failed", True),
        ("guest_crash_trap_env_1", True),
        ("guest_api_advisory", True),
        ("guest_capture_only_no_crash_trap", True),
        ("frida_inproc_clean", True),
    ],
)
def test_gap3_app_frida_hygiene_post_run_terminate_gating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expect_terminate: bool,
) -> None:
    from autophone.execution import app_frida_hygiene as hygiene
    from autophone.execution.engine import runner as runner_mod
    from autophone.rig.supervisor import RigSnapshot

    monkeypatch.delenv("AUTOPHONE_CRASH_TRAP", raising=False)
    monkeypatch.delenv("AUTOPHONE_APP_FRIDA_HYGIENE", raising=False)
    if scenario == "guest_crash_trap_env_1":
        monkeypatch.setenv("AUTOPHONE_CRASH_TRAP", "1")
    elif scenario == "guest_capture_only_no_crash_trap":
        monkeypatch.setenv("AUTOPHONE_CRASH_TRAP", "0")

    ax_backend = "frida_inproc" if scenario == "frida_inproc_clean" else "guest_axruntime"
    api_mode = (
        "advisory"
        if scenario in {"guest_api_advisory", "guest_capture_only_no_crash_trap"}
        else "disabled"
    )
    case_login = "required" if scenario == "guest_capture_only_no_crash_trap" else "any"
    profile = _make_stage3_app(
        tmp_path,
        app="hygiene_demo",
        ax_enabled=True,
        ax_backend=ax_backend,
        cases=[
            {
                "id": "R1",
                "revision": 1,
                "status": "active",
                "automation": "ui_ready",
                "module": "home",
                "item": "guest ax home check",
                "login_precondition": case_login,
            }
        ],
    )

    events: dict[str, Any] = {
        "terminate_calls": 0,
        "relaunch_calls": 0,
        "crash_trap_starts": 0,
        "capture_enters": 0,
        "guest_ax_opened": 0,
        "need_frida": [],
    }

    class _FakeGuestAx:
        ax_backend = "guest_axruntime"

        def open(self) -> None:
            events["guest_ax_opened"] += 1

        def close(self) -> None:
            return None

    class _FakeCrashTrap:
        def __init__(self, **_kw: Any) -> None:
            pass

        def start(self) -> None:
            events["crash_trap_starts"] += 1

        def stop(self) -> dict[str, Any]:
            return {"crashes": [], "attaches": [{"pid": 4242}]}

    class _FakeCaptureSession:
        spawn_gated = False
        handle = SimpleNamespace(mark=lambda *a, **k: None, subject_epoch_ref=None)

        def __enter__(self):
            events["capture_enters"] += 1
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def merged(self) -> list[dict[str, Any]]:
            return []

    class _FakeHygienePorts:
        def __init__(self) -> None:
            self.pid = 0 if scenario == "guest_not_running" else 4242

        def app_pid(self) -> int:
            return self.pid

        def shell(self, cmd: str) -> tuple[int, str]:
            return 0, ""

        def inspect_residue(self, pid: int) -> dict[str, Any]:
            if scenario == "guest_pre_residue":
                return {
                    "verdict": "residue",
                    "detail": "1 region(s) tagged 'Tag: 255'",
                    "tag255_regions": [{"tag": "Tag: 255"}],
                }
            if scenario == "guest_pre_check_failed":
                return {
                    "verdict": "check_failed",
                    "detail": "procexp unavailable",
                    "tag255_regions": [],
                }
            return {"verdict": "clean", "detail": "no Tag: 255", "tag255_regions": []}

        def relaunch(self) -> None:
            events["relaunch_calls"] += 1
            self.pid = 4343

        def terminate(self) -> None:
            events["terminate_calls"] += 1
            self.pid = 0

    import autophone.observation.frida.crash_trap as crash_trap_mod

    monkeypatch.setattr(crash_trap_mod, "CrashTrapSession", _FakeCrashTrap)
    ports = _FakeHygienePorts()
    run_exec = _make_stage3_run_execution(
        tmp_path,
        capture_session_factory=lambda *a, **k: (
            _FakeCaptureSession()
            if scenario in {"guest_api_advisory", "guest_capture_only_no_crash_trap"}
            else None
        ),
        app_hygiene_factory=lambda *a, **k: ports,
    )

    def fake_prepare_rig(prof: Any, sdir: Path, *, need_app: bool, need_frida: bool) -> RigSnapshot:
        events["need_frida"].append(need_frida)
        if ports.pid == 0:
            ports.pid = 4242
        return RigSnapshot(
            vm_uds={"available": True, "evidence": "ok"},
            app_ui={"available": True, "evidence": "ok"},
            frida={"available": True, "evidence": "probed" if need_frida else "not_required"},
            health_guard={"available": True, "evidence": "ok"},
        )

    monkeypatch.setattr(run_exec, "_prepare_rig", fake_prepare_rig)
    monkeypatch.setattr(run_exec, "_probe_build_identity", lambda *a, **k: None)
    monkeypatch.setattr(run_exec, "_ensure_unlocked", lambda *a, **k: None)
    monkeypatch.setattr(run_exec, "_launch_app", lambda *a, **k: None)
    monkeypatch.setattr(run_exec, "_ensure_login_state", lambda *a, **k: None)
    monkeypatch.setattr(run_exec, "_package_snapshot", lambda *a, **k: {"profile_build_label": "1.0.0-1"})
    monkeypatch.setattr(
        "autophone.execution.candidate_lifecycle.activate_run_candidate",
        lambda *a, **k: None,
    )
    monkeypatch.setattr("autophone.execution.world_identity.capture_world", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "gate", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(
        "autophone.observation.capture.build_run_ax_session",
        lambda prof, *a, **k: _FakeGuestAx() if prof.ax_backend == "guest_axruntime" else None,
    )
    _patch_ctx_io(monkeypatch, ocr_texts=("今晚，好好吃一顿",))

    run_id = f"run-gap3-{scenario}"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "schema_version": "v1",
        "run_id": run_id,
        "type": "regression",
        "app": profile.app,
        "device": "vm-test",
        "case_selector": {"ids": ["R1"]},
        "planes": {"ui": {"mode": "required"}, "api": {"mode": api_mode}},
    }

    handle = run_exec.execute(spec, profile, run_id, run_dir, "vm-test")
    assert handle.result["status"] == "passed"

    evidence_path = run_dir / hygiene.EVIDENCE_NAME
    assert evidence_path.is_file()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["schema"] == hygiene.SCHEMA

    if expect_terminate:
        assert events["terminate_calls"] == 1
        assert evidence["post_run"]["result"] == "terminated"
        if scenario == "guest_crash_trap_env_1":
            assert events["crash_trap_starts"] == 1
        if scenario == "guest_capture_only_no_crash_trap":
            assert events["crash_trap_starts"] == 0
            assert events["capture_enters"] == 1
    else:
        assert events["need_frida"] == [False]
        assert events["guest_ax_opened"] == 1
        assert events["crash_trap_starts"] == 0
        assert events["capture_enters"] == 0
        assert events["terminate_calls"] == 0
        assert evidence["post_run"]["result"] == "skipped"
        assert evidence["post_run"]["reason"] == hygiene.NO_FRIDA_REASON
        assert evidence["post_run"]["last_detach_at"] is None
        assert evidence["post_run"]["pid"] == 4242


def test_gap4_operation_run_injects_ax_session_for_tap_ax_and_ax_requirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autophone.execution import operation_run
    from autophone.execution.engine import recipe as recipe_mod
    from autophone.execution.operation_run import (
        AX_SESSION_ACTIONS,
        AX_SESSION_REQUIREMENTS,
        OperationExecutionContext,
        OperationExecutionRequest,
        execute_operation,
        steps_need_ax_session,
    )

    assert "ax" in AX_SESSION_REQUIREMENTS
    assert "frida" in AX_SESSION_REQUIREMENTS
    assert "tap_ax" in AX_SESSION_ACTIONS

    # Direct tap_ax step triggers AX session without any frida requirement.
    tap_ax_steps = [
        {"action": "tap_ax", "identifier": "home.ai_prompt.preferences", "role": "button"}
    ]
    assert steps_need_ax_session(None, tap_ax_steps) is True
    assert steps_need_ax_session("op_demo", [{"action": "tap_text", "query": "偏好"}], root=tmp_path) is False

    # Operation registered with requires={"ax"} (no "frida") also triggers AX session.
    profile = _make_stage3_app(
        tmp_path,
        app="op_demo",
        ax_enabled=True,
        ax_backend="guest_axruntime",
        operations=[
            {
                "id": "OP1",
                "name": "open_prefs_ax",
                "status": "active",
                "revision": 1,
                "summary": "tap preferences via guest AX",
                "params": [],
                "steps": tap_ax_steps,
            }
        ],
    )
    resolved_root = str(tmp_path.resolve())
    try:
        recipe_mod.APP_ROOT_OPERATION_REQUIREMENTS[("op_demo", resolved_root)] = {
            "ax_only_op": {"ax"},
            "frida_op": {"frida"},
            "plain_op": set(),
        }
        assert steps_need_ax_session("op_demo", [{"operation": "ax_only_op"}], root=tmp_path) is True
        assert steps_need_ax_session("op_demo", [{"operation": "frida_op"}], root=tmp_path) is True
        assert steps_need_ax_session("op_demo", [{"operation": "plain_op"}], root=tmp_path) is False
    finally:
        recipe_mod.APP_ROOT_OPERATION_REQUIREMENTS.pop(("op_demo", resolved_root), None)

    # End-to-end typed-IR operation with tap_ax receives a LazyAxSession(guest_axruntime)
    # without requires={"frida"} and without screen checkpoints.
    norm_dump = normalize_probe_to_ax_v1(
        _probe_raw_payload(),
        app=BUNDLE_ID,
        request_id="req-op-ax",
        capabilities_screen={"width": 1290, "height": 2796, "scale": 3},
    )
    parsed_dump = parse_dump(norm_dump, ax_backend="guest_axruntime")
    ax_lifecycle = {"opened": 0, "refreshed": 0, "closed": 0, "built_backends": []}

    class _FakeGuestAxSession:
        ax_backend = "guest_axruntime"
        out = ""

        def open(self) -> None:
            ax_lifecycle["opened"] += 1

        def refresh(self, *a: Any, **k: Any) -> Any:
            ax_lifecycle["refreshed"] += 1
            return parsed_dump

        def close(self) -> None:
            ax_lifecycle["closed"] += 1

    def fake_build_run_ax_session(prof: Any, rdir: Path, **kw: Any) -> Any:
        ax_lifecycle["built_backends"].append(prof.ax_backend)
        return _FakeGuestAxSession() if prof.ax_enabled else None

    monkeypatch.setattr("autophone.observation.capture.build_run_ax_session", fake_build_run_ax_session)
    taps = _patch_ctx_io(monkeypatch, ocr_texts=("今晚，好好吃一顿",))
    monkeypatch.setattr(operation_run.time, "sleep", lambda _s: None)

    run_dir = tmp_path / "runs" / "run-op-ax"
    run_dir.mkdir(parents=True, exist_ok=True)
    op_doc = {
        "schema_version": "v1",
        "run_id": "run-op-ax",
        "app": "op_demo",
        "operation": {"name": "open_prefs_ax", "id": "OP1", "revision": 1},
        "summary": "tap preferences via guest AX",
        "args": {},
        "steps": [{"index": 1, "text": "action tap_ax", "status": "pending"}],
        "status": "pending_execution",
        "detail": "",
    }
    res = execute_operation(
        OperationExecutionRequest(operation=op_doc, raw_steps=tuple(tap_ax_steps)),
        OperationExecutionContext(run_dir=run_dir, app="op_demo", root=tmp_path),
    )
    assert res["status"] == "passed", res
    assert ax_lifecycle == {
        "opened": 1,
        "refreshed": 1,
        "closed": 1,
        "built_backends": ["guest_axruntime"],
    }
    assert len(taps) == 1

    # Non-AX typed-IR steps without checkpoints do not construct an AX session.
    ax_lifecycle["built_backends"].clear()
    res_plain = execute_operation(
        OperationExecutionRequest(
            operation=op_doc,
            raw_steps=({"action": "assert_present", "queries": ["今晚，好好吃一顿"], "wait_s": 0},),
        ),
        OperationExecutionContext(run_dir=run_dir, app="op_demo", root=tmp_path),
    )
    assert res_plain["status"] == "passed", res_plain
    assert ax_lifecycle["built_backends"] == []

    # When capabilities.ax.enabled is False, build_run_ax_session returns None and no session is opened.
    _make_stage3_app(
        tmp_path / "ax_off",
        app="op_demo_off",
        ax_enabled=False,
        ax_backend="guest_axruntime",
    )
    ax_lifecycle["opened"] = 0
    ax_lifecycle["built_backends"].clear()
    res_off = execute_operation(
        OperationExecutionRequest(
            operation={**op_doc, "app": "op_demo_off"},
            raw_steps=tuple(tap_ax_steps),
        ),
        OperationExecutionContext(run_dir=run_dir, app="op_demo_off", root=tmp_path / "ax_off"),
    )
    assert res_off["status"] == "failed"
    assert ax_lifecycle["opened"] == 0


# ── 6. v2 Package Store build_marker -> confirmed build-identity & claims ────


def _build_synthetic_ipa(
    path: Path,
    *,
    bundle_id: str = BUNDLE_ID,
    short_version: str = "1.0.0",
    build_version: str = "1",
    marker_payload: dict[str, Any] | None = None,
) -> tuple[Path, str, bytes | None]:
    import zipfile

    info_plist_bytes = plistlib.dumps(
        {
            "CFBundleIdentifier": bundle_id,
            "CFBundleShortVersionString": short_version,
            "CFBundleVersion": build_version,
        }
    )
    marker_bytes = (
        json.dumps(marker_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if marker_payload is not None
        else None
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Payload/Food.app/Info.plist", info_plist_bytes)
        if marker_bytes is not None:
            zf.writestr("Payload/Food.app/autophone-ui-eval-build-marker.json", marker_bytes)
    return path, _sha256_bytes(path.read_bytes()), marker_bytes


def test_build_identity_probe_v2_build_marker_confirmation_and_fail_closed(
    tmp_path: Path,
) -> None:
    from autophone.assets import app_symbols, build_identity
    from autophone.execution import run_submission

    plist_bytes = plistlib.dumps(
        {
            "CFBundleIdentifier": BUNDLE_ID,
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1",
        }
    )
    expected_marker = json.dumps(
        {
            "schema_version": "ui-eval-installed-build-marker/v1",
            "target_id": "food_ordering",
            "bundle_id": BUNDLE_ID,
            "source_tree_sha256": "1" * 64,
            "app_tree_sha256": "2" * 64,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    other_marker = json.dumps(
        {
            "schema_version": "ui-eval-installed-build-marker/v1",
            "target_id": "food_ordering",
            "bundle_id": BUNDLE_ID,
            "source_tree_sha256": "9" * 64,
            "app_tree_sha256": "8" * 64,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    build_id = "bld-" + ("a" * 64)
    installed_ref = {
        "app": "food_ordering",
        "slot": "v1.0.0",
        "build_id": build_id,
        "artifact_sha256": SHA_A,
        "short_version": "1.0.0",
        "build_version": "1",
        "source_revision": {"kind": "source_tree", "tree_sha256": "1" * 64},
    }
    submission_ref = run_submission.RegistryBuildRef(
        captured_at="2026-11-27T10:00:00Z",
        default_slot="v1.0.0",
        default_version=None,
        default_sha256=None,
        short_version=None,
        build_version=None,
        profile_build_label=f"1.0.0-{build_id[:16]}",
        capture_state="unknown",
        unknown_reason="registry_default_entry_missing",
    )

    def _make_request_fn(device_marker: bytes | None):
        def _req(msg: dict[str, Any]) -> dict[str, Any]:
            t = msg.get("t")
            if t == "app_list":
                return {
                    "ok": True,
                    "apps": [
                        {
                            "bundle_id": BUNDLE_ID,
                            "pid": 4242,
                            "path": "/var/containers/Bundle/Application/UUID/Food.app",
                        }
                    ],
                }
            if t == "file_get":
                remote = str(msg.get("path") or "")
                save = Path(msg["save"])
                if remote.endswith("/Info.plist"):
                    save.write_bytes(plist_bytes)
                    return {"ok": True}
                if remote.endswith("/autophone-ui-eval-build-marker.json"):
                    if device_marker is None:
                        return {"ok": False, "error": "ENOENT"}
                    save.write_bytes(device_marker)
                    return {"ok": True}
            raise AssertionError(f"unexpected request: {msg}")

        return _req

    # 1) Exact build marker match -> state="exact", identity_state="confirmed", matched SHA & build_id.
    run_ok = tmp_path / "run_ok"
    run_ok.mkdir()
    obs_ok, match_ok, epoch_ok = build_identity.probe(
        bundle_id=BUNDLE_ID,
        request_fn=_make_request_fn(expected_marker),
        submission_ref=submission_ref,
        registry_entries=[],
        installed_ref=installed_ref,
        registry_schema_version="v2",
        expected_build_marker=expected_marker,
    )
    assert obs_ok.build_marker_sha256 == _sha256_bytes(expected_marker)
    assert match_ok.state == "exact"
    assert match_ok.match_basis == build_identity.INSTALLED_REF_MARKER_BASIS
    assert match_ok.matched_registry_sha256 == SHA_A
    assert match_ok.matched_build_id == build_id
    assert match_ok.reason is None
    build_identity.write_probe(
        run_ok, run_id="run_ok", app="food_ordering", observation=obs_ok, match=match_ok, epoch=epoch_ok
    )
    doc_ok = build_identity.load_document(run_ok)
    assert doc_ok is not None
    assert doc_ok["identity_state"] == "confirmed"
    assert doc_ok["match"]["matched_registry_sha256"] == SHA_A
    assert doc_ok["match"]["matched_build_id"] == build_id

    profile = _make_stage3_app(tmp_path / "app_ok", app="food_ordering")
    resolved = app_symbols.resolve_artifact_identity_for_app(
        profile, build_identity_doc=doc_ok, allow_default_build=False
    )
    assert resolved.identity_state == "confirmed"
    assert resolved.artifact_sha256 == SHA_A
    assert resolved.build_id == build_id

    # 2) Swapped same-version IPA with different marker -> state="mismatch", identity_state="mismatch".
    run_mis = tmp_path / "run_mis"
    run_mis.mkdir()
    obs_mis, match_mis, epoch_mis = build_identity.probe(
        bundle_id=BUNDLE_ID,
        request_fn=_make_request_fn(other_marker),
        submission_ref=submission_ref,
        registry_entries=[],
        installed_ref=installed_ref,
        registry_schema_version="v2",
        expected_build_marker=expected_marker,
    )
    assert match_mis.state == "mismatch"
    assert match_mis.match_basis == build_identity.INSTALLED_REF_MARKER_BASIS
    assert match_mis.reason == "installed_build_marker_mismatch"
    assert match_mis.matched_registry_sha256 is None
    build_identity.write_probe(
        run_mis, run_id="run_mis", app="food_ordering", observation=obs_mis, match=match_mis, epoch=epoch_mis
    )
    doc_mis = build_identity.load_document(run_mis)
    assert doc_mis is not None and doc_mis["identity_state"] == "mismatch"
    # Even if world-identity earlier observed match, a probe marker mismatch fails closed in package comparison.
    compared = run_submission.compare_installed_build(
        installed_ref,
        {"short_version": "1.0.0", "build_version": "1"},
        world_subject={"state": "observed", "installed_build_ref": installed_ref},
        probe_match=doc_mis["match"],
    )
    assert compared["build_identity_match"] == "mismatch"
    assert compared["comparison_granularity"] == "build_marker"

    # 3) Swapped same-version IPA missing marker on device -> identity_state="ambiguous".
    run_missing = tmp_path / "run_missing"
    run_missing.mkdir()
    obs_miss, match_miss, epoch_miss = build_identity.probe(
        bundle_id=BUNDLE_ID,
        request_fn=_make_request_fn(None),
        submission_ref=submission_ref,
        registry_entries=[],
        installed_ref=installed_ref,
        registry_schema_version="v2",
        expected_build_marker=expected_marker,
    )
    assert match_miss.state == "ambiguous"
    assert match_miss.reason == "device_build_marker_unavailable"
    assert match_miss.matched_registry_sha256 is None
    build_identity.write_probe(
        run_missing,
        run_id="run_missing",
        app="food_ordering",
        observation=obs_miss,
        match=match_miss,
        epoch=epoch_miss,
    )
    doc_miss = build_identity.load_document(run_missing)
    assert doc_miss is not None and doc_miss["identity_state"] == "ambiguous"

    # 4) Version-tuple only (expected_build_marker=None) -> stays ambiguous (D2/D5).
    obs_ver, match_ver, _ = build_identity.probe(
        bundle_id=BUNDLE_ID,
        request_fn=_make_request_fn(expected_marker),
        submission_ref=submission_ref,
        registry_entries=[],
        installed_ref=installed_ref,
        registry_schema_version="v2",
        expected_build_marker=None,
    )
    assert match_ver.state == "ambiguous"
    assert match_ver.reason == "build_marker_not_compared"

    # 5) Drift against world_subject marker SHA fails closed to mismatch.
    _, match_drift, _ = build_identity.probe(
        bundle_id=BUNDLE_ID,
        request_fn=_make_request_fn(expected_marker),
        submission_ref=submission_ref,
        registry_entries=[],
        installed_ref=installed_ref,
        registry_schema_version="v2",
        expected_build_marker=expected_marker,
        world_subject={
            "state": "observed",
            "installed_build_ref": installed_ref,
            "device_observation": {"build_marker_sha256": _sha256_bytes(other_marker)},
        },
    )
    assert match_drift.state == "mismatch"
    assert match_drift.reason == "installed_build_marker_mismatch"


@pytest.mark.parametrize(
    "mode",
    ["confirmed", "missing_marker", "mismatched_marker", "unknown_source_revision"],
)
def test_run_execution_v2_package_store_build_marker_confirmed_claims_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    import yaml
    from autophone.assets import app_symbols, build_identity
    from autophone.execution import run_submission
    from autophone.execution.engine import runner as runner_mod
    from autophone.packages import store as package_store
    from autophone.packages.model import SourceRevision
    from autophone.rig.supervisor import RigSnapshot

    root = tmp_path / mode
    app_slug = "food_ordering"
    source_tree_sha = "3" * 64
    marker_payload = {
        "schema_version": "ui-eval-installed-build-marker/v1",
        "target_id": app_slug,
        "bundle_id": BUNDLE_ID,
        "source_tree_sha256": source_tree_sha,
        "app_tree_sha256": "4" * 64,
    }
    wrong_marker_bytes = json.dumps(
        {
            **marker_payload,
            "source_tree_sha256": "f" * 64,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    # 1) Create app profile, screen asset, active case, and symbol-bound live recipe.
    profile = _make_stage3_app(
        root,
        app=app_slug,
        ax_enabled=True,
        ax_backend="guest_axruntime",
        api_capture_enabled=False,
        cases=[
            {
                "id": "R3",
                "revision": 1,
                "status": "active",
                "automation": "ui_ready",
                "module": "home",
                "item": "open preferences via guest AX",
                "login_precondition": "any",
            }
        ],
        recipes={
            "R3": {
                "id": "R3",
                "steps": [
                    {"action": "tap_ax", "symbol": "ctrl.home.preferences", "label": "打开饮食偏好"},
                    {"action": "assert_present", "queries": ["今晚，好好吃一顿"], "wait_s": 0},
                ],
            }
        },
    )
    screens_path = profile.profile_dir / "screens" / "1.0.0-1.yaml"
    screens_path.parent.mkdir(parents=True, exist_ok=True)
    screens_path.write_text(
        yaml.safe_dump(
            {
                "meta": {"schema_version": 1, "app": app_slug, "build": "1.0.0-1"},
                "screens": [
                    {
                        "id": "S01_home",
                        "display_name": "首页",
                        "status": "active",
                        "must": [{"text": "今晚，好好吃一顿"}],
                        "forbid": [],
                        "any_of": {"min_hits": 0, "tokens": []},
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    # 2) Build synthetic IPA with embedded marker and register in a real v2 Package Store.
    ipa_path, ipa_sha, expected_marker_bytes = _build_synthetic_ipa(
        root / "dist" / "Food.ipa",
        marker_payload=marker_payload,
    )
    assert expected_marker_bytes is not None
    src_rev = (
        SourceRevision.unknown()
        if mode == "unknown_source_revision"
        else SourceRevision.source_tree(source_tree_sha)
    )
    package_store.add_package(
        app_slug,
        ipa_path,
        source_revision=src_rev,
        root=root,
        code_root=root,
    )
    registry = package_store.load_registry(app_slug, root=root, code_root=root)
    build_ref = registry.resolve()
    assert build_ref.artifact_sha256 == ipa_sha
    target_identity = package_store.resolve_install_target_identity()
    fp = package_store.install_target_fingerprint(target_identity)
    package_store._write_target_receipt(
        app_slug,
        build_ref,
        verified=True,
        installer="test",
        target=target_identity,
        target_fingerprint=fp,
        root=root,
    )

    # 3) Write an app-symbols.v1 artifact bound to this exact IPA SHA-256 & build_id.
    sym_dir = profile.profile_dir / "symbols" / "artifacts"
    sym_dir.mkdir(parents=True, exist_ok=True)
    sym_doc = {
        "schema": "autophone.app-symbols/1",
        "app": app_slug,
        "artifact_identity": {
            "kind": "ipa_build",
            "bundle_id": BUNDLE_ID,
            "short_version": "1.0.0",
            "build_version": "1",
            "build_id": build_ref.build_id,
            "artifact_sha256": ipa_sha,
        },
        "controls": {
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
            }
        },
    }
    sym_path = sym_dir / f"{ipa_sha[:12]}.yaml"
    sym_bytes_before = yaml.safe_dump(sym_doc, allow_unicode=True, sort_keys=False).encode("utf-8")
    sym_path.write_bytes(sym_bytes_before)

    # 4) Initialize real run-submission.json + mock guest UDS request for _probe_build_identity.
    run_id = f"run-v2-marker-{mode}"
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "schema_version": "v1",
        "run_id": run_id,
        "type": "regression",
        "app": app_slug,
        "device": "vm-test",
        "build_id": build_ref.build_id,
        "case_selector": {"ids": ["R3"]},
        "planes": {"ui": {"mode": "required"}, "api": {"mode": "disabled"}},
    }
    run_submission.initialize(run_dir, spec, profile)

    plist_bytes = plistlib.dumps(
        {
            "CFBundleIdentifier": BUNDLE_ID,
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1",
        }
    )

    def fake_uds_request(msg: dict[str, Any], **_kw: Any) -> tuple[dict[str, Any], None]:
        t = msg.get("t")
        if t == "app_list":
            return {
                "ok": True,
                "apps": [
                    {
                        "bundle_id": BUNDLE_ID,
                        "pid": 4242,
                        "path": "/var/containers/Bundle/Application/UUID/Food.app",
                    }
                ],
            }, None
        if t == "file_get":
            remote = str(msg.get("path") or "")
            save = Path(msg["save"])
            if remote.endswith("/Info.plist"):
                save.write_bytes(plist_bytes)
                return {"ok": True}, None
            if remote.endswith("/autophone-ui-eval-build-marker.json"):
                if mode == "missing_marker":
                    return {"ok": False, "error": "ENOENT"}, None
                if mode == "mismatched_marker":
                    save.write_bytes(wrong_marker_bytes)
                    return {"ok": True}, None
                save.write_bytes(expected_marker_bytes)
                return {"ok": True}, None
        raise AssertionError(f"unexpected UDS request during build_identity probe: {msg}")

    monkeypatch.setattr("autophone.device.uds.request", fake_uds_request)

    norm_dump = normalize_probe_to_ax_v1(
        _probe_raw_payload(),
        app=BUNDLE_ID,
        request_id="req-v2-claims",
        capabilities_screen={"width": 1290, "height": 2796, "scale": 3},
    )
    parsed_dump = parse_dump(norm_dump, ax_backend="guest_axruntime")

    class _FakeGuestAxSession:
        ax_backend = "guest_axruntime"

        def open(self) -> None:
            return None

        def refresh(self, *a: Any, **k: Any) -> Any:
            return parsed_dump

        def close(self) -> None:
            return None

    run_exec = _make_stage3_run_execution(root)
    seen_need_frida: list[bool] = []

    def fake_prepare_rig(prof: Any, sdir: Path, *, need_app: bool, need_frida: bool) -> RigSnapshot:
        seen_need_frida.append(need_frida)
        return RigSnapshot(
            vm_uds={"available": True, "evidence": "ok"},
            app_ui={"available": True, "evidence": "ok"},
            frida={"available": not need_frida, "evidence": "not_required"},
            health_guard={"available": True, "evidence": "ok"},
        )

    monkeypatch.setattr(run_exec, "_prepare_rig", fake_prepare_rig)
    monkeypatch.setattr(run_exec, "_ensure_unlocked", lambda *a, **k: None)
    monkeypatch.setattr(run_exec, "_launch_app", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "gate", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(
        "autophone.observation.capture.build_run_ax_session",
        lambda *a, **k: _FakeGuestAxSession(),
    )
    _patch_ctx_io(monkeypatch, ocr_texts=("今晚，好好吃一顿",))

    claims_file = app_symbols.symbol_claims_path(profile.profile_dir)

    if mode == "mismatched_marker":
        with pytest.raises(app_symbols.SymbolBindingMismatchError, match="mismatched"):
            run_exec._execute_phase2_cases(spec, profile, run_dir)
        bi_doc = build_identity.load_document(run_dir)
        assert bi_doc is not None
        assert bi_doc["identity_state"] == "mismatch"
        assert bi_doc["match"]["reason"] == "installed_build_marker_mismatch"
        assert not claims_file.exists()
        return

    result, _, _ = run_exec._execute_phase2_cases(spec, profile, run_dir)
    assert seen_need_frida == [False]
    assert result.ax_backend == "guest_axruntime"
    assert len(result.outcomes) == 1
    assert result.outcomes[0].status == "pass", result.outcomes[0].detail
    assert sym_path.read_bytes() == sym_bytes_before

    bi_doc = build_identity.load_document(run_dir)
    assert bi_doc is not None

    if mode == "confirmed":
        assert bi_doc["identity_state"] == "confirmed"
        assert bi_doc["match"]["state"] == "exact"
        assert bi_doc["match"]["match_basis"] == build_identity.INSTALLED_REF_MARKER_BASIS
        assert bi_doc["match"]["matched_registry_sha256"] == ipa_sha
        assert bi_doc["match"]["matched_build_id"] == build_ref.build_id
        assert claims_file.is_file()
        claims_doc = app_symbols.load_symbol_claims(profile)
        vis_claims = [c for c in claims_doc["claims"] if c.get("kind") == "symbol.unique_visibility"]
        assert len(vis_claims) == 1
        claim = vis_claims[0]
        assert claim["scope"]["symbol_id"] == "ctrl.home.preferences"
        assert claim["scope"]["identity_state"] == "confirmed"
        assert claim["scope"]["ax_backend"] == "guest_axruntime"
        assert claim["scope"]["artifact_sha256"] == ipa_sha
        assert claim["scope"]["artifact_content_digest"] == ipa_sha
        assert claim["scope"]["screen_id"] == "S01_home"
        assert any(ref.endswith("build-identity.json") for ref in claim["evidence_refs"])
        assert any(ref.endswith(".ax.json") for ref in claim["evidence_refs"])
        assert app_symbols.has_reusable_unique_visibility(
            claims_doc,
            symbol_id="ctrl.home.preferences",
            artifact_content_digest=ipa_sha,
            binding_sha256=claim["scope"]["binding_sha256"],
            screen_id="S01_home",
            ax_backend="guest_axruntime",
        )
    else:
        assert bi_doc["identity_state"] == "ambiguous"
        expected_reason = (
            "device_build_marker_unavailable"
            if mode == "missing_marker"
            else "build_marker_not_compared"
        )
        assert bi_doc["match"]["reason"] == expected_reason
        assert not claims_file.exists()


