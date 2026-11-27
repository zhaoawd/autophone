"""Round 1.5 tests for AX v9 host parsing, session version gating, and schema/variant compat."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from autophone.contracts import validate_or_raise
from autophone.device.ax_session import (
    DUMPER_MIN_VERSION,
    GUEST_DYLIB,
    AxSession,
    AxUnavailable,
)
from autophone.device.ax_target import AxDump, Node, find_node, parse_dump
from autophone.observation.impact import ax_comparison, variant_evidence
from autophone.observation.impact.comparison import EvidenceError

ROOT = Path(__file__).resolve().parents[1]
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _v9_raw_dump(
    *,
    request_id: str = "req-v9-001",
    captured_at_ms: int = 1732622400100,
    dumper_version: Any = 9,
    application_state: Any = 0,
    scene_state: Any = 0,
    is_app_window: Any = True,
    nodes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": "autophone.ax/1",
        "evidence_protocol": 1,
        "request_id": request_id,
        "captured_at_ms": captured_at_ms,
        "app": "com.example.demo",
        "complete": True,
        "truncated": False,
        "short_version": "1.0.0",
        "bundle_version": "1",
        "screen": {"scale": 3.0, "w": 430.0, "h": 932.0},
        "window_class": "UIWindow",
        "view_controller": "RCTRootViewController",
        "nodes": nodes
        if nodes is not None
        else [
            {
                "path": "0/1/0",
                "label": "Preferences",
                "role": "button",
                "traits": ["button"],
                "frame": [20.0, 200.0, 120.0, 44.0],
                "enabled": True,
                "value": None,
                "identifier": "home.preferences",
                "class": "RCTViewComponentView",
                "hittable": True,
            },
            {
                "path": "0/0/2",
                "label": "Start Order",
                "role": "button",
                "traits": ["button"],
                "frame": [20.0, 300.0, 200.0, 48.0],
                "enabled": True,
                "value": None,
                "identifier": "home.start_order",
                "class": "RCTViewComponentView",
                "hittable": False,
                "occluded_reason": "hit_test_occluded",
            },
        ],
        "variant_observation": {
            "protocol": 1,
            "complete": True,
            "location_authorization_status": 4,
            "ui_text_nodes": [
                {"text": "Logged in as Demo", "frame": [20.0, 80.0, 200.0, 30.0]},
            ],
        },
    }
    if dumper_version is not ...:
        payload["dumper_version"] = dumper_version
    if application_state is not ...:
        payload["application_state"] = application_state
    if scene_state is not ...:
        payload["key_window_scene_activation_state"] = scene_state
    if is_app_window is not ...:
        payload["key_window_is_app_window"] = is_app_window
    return payload


def test_parse_dump_v9_fields_and_all_occlusion_reasons() -> None:
    reasons = [
        "offscreen_or_empty",
        "hidden_or_transparent",
        "clipped_by_ancestor",
        "modal_sibling",
        "higher_window",
        "hit_test_occluded",
    ]
    nodes: list[dict[str, Any]] = [
        {
            "path": "0/0",
            "label": "Visible",
            "role": "button",
            "traits": ["button"],
            "frame": [10.0, 100.0, 100.0, 40.0],
            "enabled": True,
            "value": None,
            "identifier": "btn.visible",
            "hittable": True,
        }
    ]
    for idx, reason in enumerate(reasons, start=1):
        nodes.append(
            {
                "path": f"0/{idx}",
                "label": f"Occluded {reason}",
                "role": "button",
                "traits": ["button"],
                "frame": [10.0, 160.0 + idx * 50.0, 100.0, 40.0],
                "enabled": True,
                "value": None,
                "identifier": f"btn.{reason}",
                "hittable": False,
                "occluded_reason": reason,
            }
        )

    dump = parse_dump(_v9_raw_dump(nodes=nodes))
    assert dump.dumper_version == 9
    assert dump.application_state == 0
    assert dump.key_window_scene_activation_state == 0
    assert dump.key_window_is_app_window is True

    assert dump.nodes[0].hittable is True
    assert dump.nodes[0].occluded_reason == ""
    for idx, reason in enumerate(reasons, start=1):
        assert dump.nodes[idx].hittable is False
        assert dump.nodes[idx].occluded_reason == reason

    # Non-zero inactive states and False boolean must be preserved distinctly from None.
    inactive = parse_dump(
        _v9_raw_dump(application_state=1, scene_state=1, is_app_window=False)
    )
    assert inactive.application_state == 1
    assert inactive.key_window_scene_activation_state == 1
    assert inactive.key_window_is_app_window is False


def test_parse_dump_backward_compat_and_strict_type_guards() -> None:
    # Legacy v8 / synthetic dump without v9 or activation fields.
    legacy = {
        "schema": "autophone.ax/1",
        "app": "com.example.legacy",
        "screen": {"scale": 3.0, "w": 430.0, "h": 932.0},
        "nodes": [
            {
                "label": "Send",
                "role": "button",
                "traits": ["button"],
                "frame": [300.0, 800.0, 80.0, 40.0],
                "enabled": True,
                "path": "0/1",
            }
        ],
    }
    dump = parse_dump(legacy)
    assert dump.dumper_version is None
    assert dump.application_state is None
    assert dump.key_window_scene_activation_state is None
    assert dump.key_window_is_app_window is None
    assert len(dump.nodes) == 1
    assert dump.nodes[0].hittable is None
    assert dump.nodes[0].occluded_reason == ""

    # Strict guards: bool must not be coerced to int, int must not be coerced to bool.
    malformed_types = _v9_raw_dump(
        dumper_version=True,
        application_state=False,
        scene_state="active",
        is_app_window=1,
        nodes=[
            {
                "path": "0/1",
                "label": "Weird",
                "role": "button",
                "traits": ["button"],
                "frame": [10.0, 100.0, 80.0, 40.0],
                "enabled": True,
                "value": None,
                "identifier": "btn.weird",
                "hittable": 1,
                "occluded_reason": 123,
            }
        ],
    )
    guarded = parse_dump(malformed_types)
    assert guarded.dumper_version is None
    assert guarded.application_state is None
    assert guarded.key_window_scene_activation_state is None
    assert guarded.key_window_is_app_window is None
    assert guarded.nodes[0].hittable is None
    assert guarded.nodes[0].occluded_reason == ""


def test_find_node_hittable_only_default_vs_filtered() -> None:
    dump = AxDump(
        app="com.example.demo",
        scale=3.0,
        w=430.0,
        h=932.0,
        nodes=[
            Node(
                label="Confirm",
                role="button",
                traits=["button"],
                frame=(20.0, 200.0, 100.0, 40.0),
                enabled=True,
                value=None,
                identifier="btn.occluded",
                path="0/0/1",
                hittable=False,
                occluded_reason="modal_sibling",
            ),
            Node(
                label="Confirm",
                role="button",
                traits=["button"],
                frame=(20.0, 400.0, 100.0, 40.0),
                enabled=True,
                value=None,
                identifier="btn.hittable",
                path="0/1/1",
                hittable=True,
            ),
            Node(
                label="Confirm",
                role="button",
                traits=["button"],
                frame=(20.0, 600.0, 100.0, 40.0),
                enabled=True,
                value=None,
                identifier="btn.legacy",
                path="0/2/1",
                hittable=None,
            ),
        ],
    )

    # Default hittable_only=False returns all matches including occluded nodes.
    all_matches = find_node(dump, "Confirm", role="button")
    assert [n.identifier for n in all_matches] == [
        "btn.occluded",
        "btn.hittable",
        "btn.legacy",
    ]

    # hittable_only=True excludes hittable=False while keeping hittable=True and legacy None.
    hittable_matches = find_node(dump, "Confirm", role="button", hittable_only=True)
    assert [n.identifier for n in hittable_matches] == ["btn.hittable", "btn.legacy"]

    # When only an occluded match exists, default sees it and hittable_only excludes it.
    occluded_only = find_node(dump, "", identifier="btn.occluded", hittable_only=False)
    assert len(occluded_only) == 1
    assert occluded_only[0].hittable is False
    assert occluded_only[0].occluded_reason == "modal_sibling"
    assert find_node(dump, "", identifier="btn.occluded", hittable_only=True) == []


def test_v9_dump_compatible_with_checkpoint_v2_ax_comparison_and_variant_evidence() -> None:
    snapshot = _v9_raw_dump()
    snapshot["ax_backend"] = "frida_inproc"

    envelope = {
        "schema_version": "v2",
        "ax_backend": "frida_inproc",
        "identity": {
            "run_id": "run_20261126_v9",
            "app": "demo_app",
            "build": "1.0.0-1",
            "bundle_id": "com.example.demo",
            "operation": {"id": "OP1", "name": "open_preferences", "revision": 1},
        },
        "checkpoint": {
            "expected": "S01_home",
            "matched": "S01_home",
            "shot": "shots/R1_01.jpg",
            "attempt": 1,
        },
        "shot_sha256": SHA_A,
        "snapshot": snapshot,
        "build_identity": {
            "schema_version": "v2",
            "run_id": "run_20261126_v9",
            "app": "demo_app",
            "identity_state": "confirmed",
            "identity_source": "combined",
            "subject_build_key": "com.example.demo:1.0.0:1",
            "device_observation": {
                "bundle_id": "com.example.demo",
                "short_version": "1.0.0",
                "build_version": "1",
            },
            "match": {"status": "match"},
            "subject_epochs": [],
            "updated_at": "2026-11-26T12:00:00Z",
            "candidate_identity": None,
        },
        "build_evidence": {
            "package": {
                "registry_build_ref": {"app": "demo_app", "build_id": "bld-v9"},
                "device_build_observation": {
                    "bundle_id": "com.example.demo",
                    "short_version": "1.0.0",
                    "build_version": "1",
                },
                "build_identity_match": "match",
                "comparison_granularity": "info_plist",
                "comparison_build_id": "bld-v9",
            },
            "sources": {
                "run-submission.json": {"path": "run-submission.json", "sha256": SHA_B},
                "world-identity.json": {"path": "world-identity.json", "sha256": SHA_C},
            },
        },
    }
    validate_or_raise("operation-ax-checkpoint.v2.json", envelope)

    # ax_comparison projects only structural fields, unaffected by v9 hittable/occluded_reason.
    projected = ax_comparison.project(snapshot, {"full": [0.0, 0.0, 1.0, 1.0]}, [])
    assert ("identifier", "home.preferences") in projected["full"]
    assert ("identifier", "home.start_order") in projected["full"]
    for item in projected["full"].values():
        assert set(item.keys()) == {
            "path",
            "label",
            "role",
            "traits",
            "enabled",
            "value",
            "identifier",
        }

    # variant_evidence accepts dumper_version=9 and active foreground state.
    screen = {
        "regions": {"header": {"rect": [0.0, 0.0, 1.0, 0.25]}},
        "variant_checks": {
            "auth": {
                "source": "native_ui_text",
                "region": "header",
                "values": {
                    "logged_in": {"all": ["Logged in"], "none": ["Sign in"]},
                    "logged_out": {"all": ["Sign in"], "none": ["Logged in"]},
                },
            },
            "location": {
                "source": "core_location_authorization",
                "values": {
                    "granted": [3, 4],
                    "denied": [0, 1, 2],
                },
            },
        },
    }
    variant_evidence.classify(snapshot, screen, {"auth": "logged_in", "location": "granted"})

    inactive_snapshot = _v9_raw_dump(application_state=1)
    with pytest.raises(EvidenceError, match="variant_app_not_foreground"):
        variant_evidence.classify(
            inactive_snapshot, screen, {"auth": "logged_in", "location": "granted"}
        )


def test_ax_session_v9_constants_and_dumper_version_enforcement(tmp_path: Path) -> None:
    assert GUEST_DYLIB == "/var/jb/tmp/ax_dump-v9.dylib"
    assert GUEST_DYLIB.endswith("ax_dump-v9.dylib")
    assert DUMPER_MIN_VERSION == 9

    out_file = tmp_path / "pulled.json"
    state: dict[str, Any] = {"req_id": "", "payload": {}}

    class _FakeExports:
        def load(self, path: str) -> dict[str, Any]:
            state["loaded_dylib"] = path
            return {"ok": True}

        def prepare(self, req_id: str) -> int:
            state["req_id"] = req_id
            return 0

        def post(self, _notif: str) -> int:
            return 0

    def _fake_request(cmd: dict[str, Any], **_kw: Any) -> tuple[dict[str, Any], None]:
        assert cmd["t"] == "file_get"
        payload = dict(state["payload"])
        payload["request_id"] = state["req_id"]
        Path(cmd["save"]).write_text(json.dumps(payload), encoding="utf-8")
        return {"ok": True}, None

    session = AxSession(
        "com.example.demo",
        out=str(out_file),
        push=False,
        settle_s=0.0,
        request_fn=_fake_request,
        ax_backend="frida_inproc",
    )
    session._script = SimpleNamespace(exports_sync=_FakeExports())

    # 1) v9 dump passes and populates v9 fields on AxDump + Node.
    state["payload"] = _v9_raw_dump(captured_at_ms=1732622400100, dumper_version=9)
    dump = session.refresh(request_id="req-v9")
    assert dump.dumper_version == 9
    assert dump.application_state == 0
    assert dump.key_window_scene_activation_state == 0
    assert dump.key_window_is_app_window is True
    assert dump.nodes[0].hittable is True
    assert dump.nodes[1].hittable is False
    assert dump.nodes[1].occluded_reason == "hit_test_occluded"
    assert session._last_captured_at_ms == 1732622400100

    # 2) Older v8 dylib is rejected fail-closed without advancing _last_captured_at_ms.
    state["payload"] = _v9_raw_dump(captured_at_ms=1732622400200, dumper_version=8)
    with pytest.raises(AxUnavailable, match="stale AX dumper_version=8 < 9"):
        session.refresh(request_id="req-v8")
    assert session._last_captured_at_ms == 1732622400100

    # 3) Non-int or bool dumper_version is rejected fail-closed.
    for bad_ver in (True, "9", 8.5):
        state["payload"] = _v9_raw_dump(captured_at_ms=1732622400300, dumper_version=bad_ver)
        with pytest.raises(AxUnavailable, match="stale AX dumper_version"):
            session.refresh(request_id=f"req-bad-{bad_ver}")
    assert session._last_captured_at_ms == 1732622400100

    # 4) Synthetic mock (not opened via open()) omitting dumper_version remains compatible.
    state["payload"] = _v9_raw_dump(captured_at_ms=1732622400400, dumper_version=...)
    legacy_dump = session.refresh(request_id="req-legacy-mock")
    assert legacy_dump.dumper_version is None
    assert session._last_captured_at_ms == 1732622400400

    # 5) Live session opened via open() rejects missing dumper_version fail-closed.
    class _FakeScript:
        def __init__(self) -> None:
            self.exports_sync = _FakeExports()

        def load(self) -> None:
            pass

    class _FakeBorrowedLifetime:
        host = "127.0.0.1:27042"
        closed = False
        closing = False

        def __init__(self) -> None:
            self.session = SimpleNamespace(create_script=lambda _js: _FakeScript())

        def add_script(self, _script: Any) -> None:
            pass

        def exports(self, exp: Any) -> Any:
            return exp

    live_session = AxSession(
        "com.example.demo",
        out=str(out_file),
        push=False,
        settle_s=0.0,
        request_fn=_fake_request,
        borrowed_lifetime=_FakeBorrowedLifetime(),
        ax_backend="frida_inproc",
    )
    assert live_session.ax_backend == "frida_inproc"
    live_session.open()
    assert state["loaded_dylib"] == "/var/jb/tmp/ax_dump-v9.dylib"
    state["payload"] = _v9_raw_dump(captured_at_ms=1732622400500, dumper_version=...)
    with pytest.raises(AxUnavailable, match="missing AX dumper_version"):
        live_session.refresh(request_id="req-live-missing")
    state["payload"] = _v9_raw_dump(captured_at_ms=1732622400600, dumper_version=9)
    assert live_session.refresh(request_id="req-live-v9").dumper_version == 9
    live_session.close()


def test_public_core_ax_dump_source_and_build_script_layout() -> None:
    src = ROOT / "support" / "ax-dump" / "ax_dump.m"
    script = ROOT / "scripts" / "build_ax_dump.sh"
    assert src.is_file()
    assert script.is_file()
    assert bool(script.stat().st_mode & stat.S_IXUSR)
    assert "kDumperVersion = 9;" in src.read_text(encoding="utf-8")
    if (ROOT / ".git").exists():
        assert not subprocess.check_output(
            ["git", "-C", str(ROOT), "ls-files", "tools/guest"], text=True
        ).strip()
    else:
        assert not (ROOT / "tools" / "guest").exists()
    assert os.access(script, os.X_OK)
