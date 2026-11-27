"""Regression tests for RigMaintenance home-reset fallback and root-only ready markers."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from autophone.assets.apps import DEFAULT_AX_BACKEND, AppProfile, load_app_profile
from autophone.contracts import validate_or_raise
from autophone.execution.engine.actions import ActionResult, CaseContext, StepFailure
from autophone.observation.capture import _ready_markers
from autophone.rig.maintenance import RigMaintenance

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_app_profile(
    root: Path,
    *,
    app: str,
    bundle_id: str = "com.example.demo",
    reset: str | None = "relaunch",
    ready_markers: list[str] | None = None,
    ax_enabled: bool = False,
    ax_backend: str = "guest_axruntime",
    recipes_init_source: str | None = None,
) -> AppProfile:
    app_dir = root / "apps" / app
    (app_dir / "recipes").mkdir(parents=True, exist_ok=True)
    raw: dict[str, Any] = {
        "schema_version": "v1",
        "app": app,
        "product_key": app,
        "bundle_id": bundle_id,
        "display_name": f"App {app}",
        "lifecycle": "active",
        "capabilities": {
            "api_capture": {
                "enabled": False,
                "ready_markers": list(ready_markers if ready_markers is not None else ["HomeAnchor"]),
            },
        },
    }
    if reset is not None:
        raw["reset"] = reset
    if ax_enabled:
        raw["capabilities"]["ax"] = {"enabled": True, "backend": ax_backend}
    validate_or_raise("app-profile.v1.json", raw)
    (app_dir / "profile.yaml").write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    if recipes_init_source is not None:
        (app_dir / "recipes" / "__init__.py").write_text(recipes_init_source, encoding="utf-8")
    return load_app_profile(app, root=root, state_root=root)


def _patch_rig_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: dict[str, Any],
    *,
    ax_marker_fn: Any | None = None,
    terminate_error: Exception | None = None,
) -> list[str]:
    events: list[str] = []
    monkeypatch.setenv("VPHONE_SOCK", str(tmp_path / "fake.sock"))
    monkeypatch.delenv("AUTOPHONE_CAPTURE_READY_TEXTS", raising=False)
    monkeypatch.setattr(
        RigMaintenance,
        "_probe_vm_uds",
        staticmethod(lambda: {"available": True, "state": "online", "evidence": "mock uds"}),
    )
    monkeypatch.setattr(
        RigMaintenance,
        "_probe_health_guard",
        staticmethod(lambda: {"state": "healthy", "daemon": False, "on_down": False}),
    )

    def fake_await_pid(self: CaseContext, bundle_id: str, *, deadline_s: float = 0.0, **kwargs: Any) -> int:
        assert bundle_id == state["bundle_id"]
        return int(state["pid"])

    def fake_await_ready(self: CaseContext, markers: list[str], *, deadline_s: float = 0.0) -> str:
        screen_texts = set(state.get("ocr_texts") or ())
        for marker in markers:
            if marker in screen_texts:
                return marker
        return ""

    def fake_terminate(self: CaseContext, bundle_id: str) -> ActionResult:
        events.append(f"terminate:{bundle_id}")
        if terminate_error is not None:
            raise terminate_error
        state["pid"] = 0
        state["screen"] = "springboard"
        state["ocr_texts"] = []
        state["ax_texts"] = []
        return ActionResult("terminate_app", True, f"terminated {bundle_id}", 1)

    monkeypatch.setattr(CaseContext, "_await_app_pid", fake_await_pid)
    monkeypatch.setattr(CaseContext, "_await_app_ready_marker", fake_await_ready)
    monkeypatch.setattr(CaseContext, "terminate_app", fake_terminate)

    if ax_marker_fn is None:
        def default_ax_probe(profile: AppProfile, markers: list[str], run_dir: Path) -> str:
            ax_texts = set(state.get("ax_texts") or ())
            for marker in markers:
                if marker in ax_texts:
                    return marker
            return ""
        ax_marker_fn = default_ax_probe

    monkeypatch.setattr(RigMaintenance, "_probe_ax_ready_marker", staticmethod(ax_marker_fn))
    return events


def test_app_profile_reset_property_and_default_ax_backend(tmp_path: Path) -> None:
    assert DEFAULT_AX_BACKEND == "guest_axruntime"

    for policy in ("relaunch", "ui_home", "none", None):
        app_id = f"app_reset_{policy or 'unset'}"
        profile = _make_app_profile(tmp_path, app=app_id, reset=policy)
        assert profile.reset == policy
        assert profile.ax_backend == "guest_axruntime"

    frida_opt_in = _make_app_profile(
        tmp_path, app="app_frida_opt_in", ax_enabled=True, ax_backend="frida_inproc"
    )
    assert frida_opt_in.ax_backend == "frida_inproc"


def test_zero_frida_subpage_with_relaunch_and_no_reset_op_triggers_relaunch_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero-Frida persistent app stuck on subpage + reset: relaunch + no reset_to_home op -> relaunch to Home."""
    bundle_id = "com.example.zerofrida"
    profile = _make_app_profile(
        tmp_path,
        app="zero_frida_relaunch",
        bundle_id=bundle_id,
        reset="relaunch",
        ready_markers=["HomeAnchor"],
        ax_enabled=True,
        ax_backend="guest_axruntime",
    )
    # Previous zero-Frida run skipped post_run terminate (no_frida_attached), leaving pid=1234 on a subpage.
    state: dict[str, Any] = {
        "bundle_id": bundle_id,
        "pid": 1234,
        "screen": "S02_preferences",
        "ocr_texts": ["PreferencesSubpage"],
        "ax_texts": ["PreferencesSubpage"],
    }
    events = _patch_rig_boundary(monkeypatch, tmp_path, state)

    def fake_launch(prof: AppProfile, shot_dir: Path) -> str | None:
        events.append(f"launch:{prof.bundle_id}:pid={state['pid']}")
        if state["pid"] == 0:
            # Cold launch after terminate lands cleanly on Home.
            state["pid"] = 5678
            state["screen"] = "S01_home"
            state["ocr_texts"] = ["HomeAnchor"]
            state["ax_texts"] = ["HomeAnchor"]
        # Otherwise uiopen/app_launch only foregrounds the live subpage process.
        return None

    maintenance = RigMaintenance(
        None,
        capability_support=[],
        run_launch_app=fake_launch,
        lock_state_probe=lambda: (False, "unlocked"),
    )
    shot_dir = tmp_path / "run-1" / "shots"
    shot_dir.mkdir(parents=True)

    snapshot = maintenance.prepare_run(profile, shot_dir, need_app=True, need_frida=False)

    assert snapshot.app_ui["available"] is True
    assert "pid=5678" in snapshot.app_ui["evidence"]
    assert "ready_marker='HomeAnchor'" in snapshot.app_ui["evidence"]
    assert "home_reset=relaunch_app ok" in snapshot.app_ui["evidence"]
    home_reset = snapshot.app_ui["home_reset"]
    assert home_reset["operation"] == "relaunch_app"
    assert home_reset["strategy"] == "profile_reset_relaunch"
    assert home_reset["fallback_from"] == "reset_to_home"
    assert home_reset["fallback_reason"] == "reset_to_home_unregistered"
    assert home_reset["ok"] is True
    assert home_reset["attempted"] is True
    assert "unknown operation 'reset_to_home'" in home_reset["reset_to_home_error"]
    assert events == [
        f"launch:{bundle_id}:pid=1234",
        f"terminate:{bundle_id}",
        f"launch:{bundle_id}:pid=0",
    ]


def test_zero_frida_relaunch_fallback_confirms_home_via_ax_ready_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After relaunch fallback, AX ready-marker fallback still confirms Home when OCR misses compact text."""
    bundle_id = "com.example.zerofrida.ax"
    profile = _make_app_profile(
        tmp_path,
        app="zero_frida_ax_only",
        bundle_id=bundle_id,
        reset="relaunch",
        ready_markers=["立即下单"],
        ax_enabled=True,
        ax_backend="guest_axruntime",
    )
    state: dict[str, Any] = {
        "bundle_id": bundle_id,
        "pid": 2001,
        "screen": "S02_preferences",
        "ocr_texts": [],
        "ax_texts": ["保存偏好"],
    }
    events = _patch_rig_boundary(monkeypatch, tmp_path, state)

    def fake_launch(prof: AppProfile, shot_dir: Path) -> str | None:
        events.append(f"launch:{state['pid']}")
        if state["pid"] == 0:
            state["pid"] = 2002
            state["screen"] = "S01_home"
            state["ocr_texts"] = []
            state["ax_texts"] = ["立即下单"]
        return None

    maintenance = RigMaintenance(
        None,
        capability_support=[],
        run_launch_app=fake_launch,
        lock_state_probe=lambda: (False, "unlocked"),
    )
    shot_dir = tmp_path / "run-ax" / "shots"
    shot_dir.mkdir(parents=True)

    snapshot = maintenance.prepare_run(profile, shot_dir, need_app=True, need_frida=False)

    assert snapshot.app_ui["available"] is True
    assert "pid=2002" in snapshot.app_ui["evidence"]
    assert "source=ax" in snapshot.app_ui["evidence"]
    assert snapshot.app_ui["home_reset"]["operation"] == "relaunch_app"
    assert snapshot.app_ui["home_reset"]["ok"] is True
    assert events == ["launch:2001", f"terminate:{bundle_id}", "launch:0"]


def test_registered_reset_to_home_operation_is_used_without_relaunch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apps that register reset_to_home continue using it even when profile declares reset: relaunch."""
    bundle_id = "com.example.withop"
    marker_file = tmp_path / "reset_op_called.txt"
    recipes_init = (
        "from pathlib import Path\n"
        "from autophone.execution.engine.recipe import register_operation\n"
        "def _reset_to_home(ctx):\n"
        f"    Path({str(marker_file)!r}).write_text('called', encoding='utf-8')\n"
        "register_operation('reset_to_home', _reset_to_home, effect='reversible')\n"
    )
    profile = _make_app_profile(
        tmp_path,
        app="app_with_reset_op",
        bundle_id=bundle_id,
        reset="relaunch",
        ready_markers=["HomeAnchor"],
        recipes_init_source=recipes_init,
    )
    state: dict[str, Any] = {
        "bundle_id": bundle_id,
        "pid": 3001,
        "ocr_texts": ["Subpage"],
        "ax_texts": ["Subpage"],
    }
    events = _patch_rig_boundary(monkeypatch, tmp_path, state)

    orig_await_ready = CaseContext._await_app_ready_marker

    def fake_await_ready_reflecting_op(self: CaseContext, markers: list[str], *, deadline_s: float = 0.0) -> str:
        if marker_file.exists():
            state["ocr_texts"] = ["HomeAnchor"]
        return orig_await_ready(self, markers, deadline_s=deadline_s)

    monkeypatch.setattr(CaseContext, "_await_app_ready_marker", fake_await_ready_reflecting_op)

    def fake_launch(prof: AppProfile, shot_dir: Path) -> str | None:
        events.append("launch")
        return None

    maintenance = RigMaintenance(
        None,
        capability_support=[],
        run_launch_app=fake_launch,
        lock_state_probe=lambda: (False, "unlocked"),
    )
    shot_dir = tmp_path / "run-op" / "shots"
    shot_dir.mkdir(parents=True)

    snapshot = maintenance.prepare_run(profile, shot_dir, need_app=True, need_frida=False)

    assert marker_file.exists()
    assert snapshot.app_ui["available"] is True
    assert snapshot.app_ui["home_reset"]["operation"] == "reset_to_home"
    assert snapshot.app_ui["home_reset"]["registered"] is True
    assert snapshot.app_ui["home_reset"]["ok"] is True
    assert "home_reset=reset_to_home ok" in snapshot.app_ui["evidence"]
    assert events == ["launch"]
    assert not any(ev.startswith("terminate:") for ev in events)


def test_registered_reset_to_home_failure_does_not_silently_relaunch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registered reset_to_home that fails must NOT trigger a surprise cold kill."""
    bundle_id = "com.example.failingop"
    recipes_init = (
        "from autophone.execution.engine.actions import ActionResult, StepFailure\n"
        "from autophone.execution.engine.recipe import register_operation\n"
        "def _failing_reset(ctx):\n"
        "    raise StepFailure(ActionResult('reset_to_home', False, 'back button missing', 5, kind='locator_missing'))\n"
        "register_operation('reset_to_home', _failing_reset, effect='reversible')\n"
    )
    profile = _make_app_profile(
        tmp_path,
        app="app_with_failing_reset_op",
        bundle_id=bundle_id,
        reset="relaunch",
        ready_markers=["HomeAnchor"],
        recipes_init_source=recipes_init,
    )
    state: dict[str, Any] = {
        "bundle_id": bundle_id,
        "pid": 3100,
        "ocr_texts": ["Subpage"],
        "ax_texts": ["Subpage"],
    }
    events = _patch_rig_boundary(monkeypatch, tmp_path, state)

    maintenance = RigMaintenance(
        None,
        capability_support=[],
        run_launch_app=lambda prof, shot_dir: events.append("launch") or None,
        lock_state_probe=lambda: (False, "unlocked"),
    )
    shot_dir = tmp_path / "run-op-fail" / "shots"
    shot_dir.mkdir(parents=True)

    snapshot = maintenance.prepare_run(profile, shot_dir, need_app=True, need_frida=False)

    assert snapshot.app_ui["available"] is False
    assert snapshot.app_ui["home_reset"]["operation"] == "reset_to_home"
    assert snapshot.app_ui["home_reset"]["registered"] is True
    assert snapshot.app_ui["home_reset"]["ok"] is False
    assert "back button missing" in snapshot.app_ui["home_reset"]["error"]
    assert events == ["launch"]
    assert not any(ev.startswith("terminate:") for ev in events)


@pytest.mark.parametrize("reset_policy", ["none", "ui_home", None])
def test_non_relaunch_profile_preserves_existing_behavior_without_relaunch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_policy: str | None,
) -> None:
    """Profiles without reset: relaunch never trigger an unexpected cold relaunch."""
    bundle_id = f"com.example.policy.{reset_policy or 'unset'}"
    profile = _make_app_profile(
        tmp_path,
        app=f"app_policy_{reset_policy or 'unset'}",
        bundle_id=bundle_id,
        reset=reset_policy,
        ready_markers=["HomeAnchor"],
    )
    state: dict[str, Any] = {
        "bundle_id": bundle_id,
        "pid": 4001,
        "ocr_texts": ["Subpage"],
        "ax_texts": ["Subpage"],
    }
    events = _patch_rig_boundary(monkeypatch, tmp_path, state)

    maintenance = RigMaintenance(
        None,
        capability_support=[],
        run_launch_app=lambda prof, shot_dir: events.append("launch") or None,
        lock_state_probe=lambda: (False, "unlocked"),
    )
    shot_dir = tmp_path / f"run-{reset_policy or 'unset'}" / "shots"
    shot_dir.mkdir(parents=True)

    snapshot = maintenance.prepare_run(profile, shot_dir, need_app=True, need_frida=False)

    assert snapshot.app_ui["available"] is False
    assert snapshot.app_ui["home_reset"]["operation"] == "reset_to_home"
    assert snapshot.app_ui["home_reset"]["registered"] is False
    assert snapshot.app_ui["home_reset"]["ok"] is False
    assert snapshot.app_ui["home_reset"]["attempted"] is True
    assert events == ["launch"]
    assert not any(ev.startswith("terminate:") for ev in events)


def test_relaunch_fallback_respects_locked_screen_and_stateful_protection_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skip guards (screen_locked_or_stale / stateful_app_protected) block relaunch fallback too."""
    bundle_id = "com.example.protected"
    profile = _make_app_profile(
        tmp_path,
        app="app_protected",
        bundle_id=bundle_id,
        reset="relaunch",
        ready_markers=["HomeAnchor"],
    )
    state: dict[str, Any] = {
        "bundle_id": bundle_id,
        "pid": 5001,
        "ocr_texts": ["Subpage"],
        "ax_texts": ["Subpage"],
    }

    # Case 1: locked screen
    events_locked = _patch_rig_boundary(monkeypatch, tmp_path, state)
    maint_locked = RigMaintenance(
        None,
        capability_support=[],
        run_launch_app=lambda prof, shot_dir: events_locked.append("launch") or None,
        lock_state_probe=lambda: (True, "lockstate=1"),
    )
    shot_locked = tmp_path / "run-locked" / "shots"
    shot_locked.mkdir(parents=True)
    snap_locked = maint_locked.prepare_run(profile, shot_locked, need_app=True, need_frida=False)
    assert snap_locked.app_ui["available"] is False
    assert snap_locked.app_ui["home_reset"]["attempted"] is False
    assert snap_locked.app_ui["home_reset"]["skipped"] == "screen_locked_or_stale"
    assert events_locked == ["launch"]

    # Case 2: stateful protected bundle without AUTOPHONE_NO_ORDER_MAINTENANCE=1
    events_protected = _patch_rig_boundary(monkeypatch, tmp_path, state)
    monkeypatch.delenv("AUTOPHONE_NO_ORDER_MAINTENANCE", raising=False)
    monkeypatch.setattr(
        RigMaintenance,
        "_STATEFUL_RECOVERY_PROTECTED_BUNDLES",
        frozenset({bundle_id}),
    )
    maint_protected = RigMaintenance(
        None,
        capability_support=[],
        run_launch_app=lambda prof, shot_dir: events_protected.append("launch") or None,
        lock_state_probe=lambda: (False, "unlocked"),
    )
    shot_protected = tmp_path / "run-protected" / "shots"
    shot_protected.mkdir(parents=True)
    snap_protected = maint_protected.prepare_run(profile, shot_protected, need_app=True, need_frida=False)
    assert snap_protected.app_ui["available"] is False
    assert snap_protected.app_ui["home_reset"]["attempted"] is False
    assert snap_protected.app_ui["home_reset"]["skipped"] == "stateful_app_protected"
    assert events_protected == ["launch"]


def test_relaunch_fallback_fails_closed_on_terminate_error_or_missing_home_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminate failure or post-relaunch non-home screen must fail closed cleanly."""
    bundle_id = "com.example.failclosed"
    profile = _make_app_profile(
        tmp_path,
        app="app_fail_closed",
        bundle_id=bundle_id,
        reset="relaunch",
        ready_markers=["HomeAnchor"],
    )

    # Subcase A: terminate_app raises StepFailure -> no second launch, available=False.
    state_a: dict[str, Any] = {
        "bundle_id": bundle_id,
        "pid": 6001,
        "ocr_texts": ["Subpage"],
        "ax_texts": [],
    }
    events_a = _patch_rig_boundary(
        monkeypatch,
        tmp_path,
        state_a,
        terminate_error=StepFailure(ActionResult("terminate_app", False, "kill denied", 5, kind="launch_failed")),
    )
    maint_a = RigMaintenance(
        None,
        capability_support=[],
        run_launch_app=lambda prof, shot_dir: events_a.append("launch") or None,
        lock_state_probe=lambda: (False, "unlocked"),
    )
    shot_a = tmp_path / "run-a" / "shots"
    shot_a.mkdir(parents=True)
    snap_a = maint_a.prepare_run(profile, shot_a, need_app=True, need_frida=False)
    assert snap_a.app_ui["available"] is False
    assert snap_a.app_ui["home_reset"]["operation"] == "relaunch_app"
    assert snap_a.app_ui["home_reset"]["ok"] is False
    assert "kill denied" in snap_a.app_ui["home_reset"]["error"]
    assert events_a == ["launch", f"terminate:{bundle_id}"]

    # Subcase B: terminate + launch succeed, yet screen still lacks ready marker -> available=False.
    state_b: dict[str, Any] = {
        "bundle_id": bundle_id,
        "pid": 6002,
        "ocr_texts": ["Subpage"],
        "ax_texts": [],
    }
    events_b = _patch_rig_boundary(monkeypatch, tmp_path, state_b)

    def launch_to_login_wall(prof: AppProfile, shot_dir: Path) -> str | None:
        events_b.append(f"launch:{state_b['pid']}")
        if state_b["pid"] == 0:
            state_b["pid"] = 6003
            state_b["ocr_texts"] = ["LoginWall"]
        return None

    maint_b = RigMaintenance(
        None,
        capability_support=[],
        run_launch_app=launch_to_login_wall,
        lock_state_probe=lambda: (False, "unlocked"),
    )
    shot_b = tmp_path / "run-b" / "shots"
    shot_b.mkdir(parents=True)
    snap_b = maint_b.prepare_run(profile, shot_b, need_app=True, need_frida=False)
    assert snap_b.app_ui["available"] is False
    assert snap_b.app_ui["home_reset"]["operation"] == "relaunch_app"
    assert snap_b.app_ui["home_reset"]["ok"] is True
    assert snap_b.app_ui["home_reset"]["attempted"] is True
    assert events_b == ["launch:6002", f"terminate:{bundle_id}", "launch:0"]


def test_already_on_home_skips_launch_reset_and_relaunch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the initial probe already sees pid + root ready marker, neither launch nor relaunch runs."""
    bundle_id = "com.example.alreadyhome"
    profile = _make_app_profile(
        tmp_path,
        app="app_already_home",
        bundle_id=bundle_id,
        reset="relaunch",
        ready_markers=["HomeAnchor"],
    )
    state: dict[str, Any] = {
        "bundle_id": bundle_id,
        "pid": 7001,
        "ocr_texts": ["HomeAnchor"],
        "ax_texts": ["HomeAnchor"],
    }
    events = _patch_rig_boundary(monkeypatch, tmp_path, state)

    maintenance = RigMaintenance(
        None,
        capability_support=[],
        run_launch_app=lambda prof, shot_dir: events.append("launch") or None,
        lock_state_probe=lambda: (False, "unlocked"),
    )
    shot_dir = tmp_path / "run-home" / "shots"
    shot_dir.mkdir(parents=True)

    snapshot = maintenance.prepare_run(profile, shot_dir, need_app=True, need_frida=False)

    assert snapshot.app_ui["available"] is True
    assert "home_reset" not in snapshot.app_ui
    assert events == []


def test_ios_settings_smoke_profile_ready_markers_exclude_subpage_tokens_and_validate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ios-settings-smoke profile.yaml validates against app-profile.v1.json and uses root-only ready_markers."""
    monkeypatch.delenv("AUTOPHONE_CAPTURE_READY_TEXTS", raising=False)
    example_root = REPO_ROOT / "examples" / "ios-settings-smoke"
    profile_path = example_root / "apps" / "ios_settings_smoke" / "profile.yaml"

    raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    validate_or_raise("app-profile.v1.json", raw)

    profile = load_app_profile("ios_settings_smoke", root=example_root, state_root=tmp_path)
    assert profile.reset == "relaunch"

    markers = _ready_markers(profile)
    assert markers, "ios_settings_smoke profile must declare root-only ready_markers"

    # Subpage-shared tokens (General nav bar title / Settings back-button label) MUST NOT appear.
    for forbidden in ("General", "通用", "Settings", "设置"):
        assert forbidden not in markers, f"subpage-shared token {forbidden!r} must not be in ready_markers"

    # Root-page-exclusive EN & zh-CN connectivity markers are present.
    assert "Airplane Mode" in markers
    assert "飞行模式" in markers
    assert "Wi-Fi" in markers
    assert "无线局域网" in markers
    assert "Bluetooth" in markers
    assert "蓝牙" in markers

    # General subpage texts must NOT match any ready marker.
    general_subpage_texts = {"Settings", "设置", "General", "通用", "About", "关于本机", "Software Update", "软件更新"}
    assert not any(marker in general_subpage_texts for marker in markers)
