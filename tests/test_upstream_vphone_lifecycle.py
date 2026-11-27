from __future__ import annotations

import json
import plistlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from autophone.rig import upstream


@pytest.fixture
def target(tmp_path):
    bundle = tmp_path / "Core Bundle" / "VPhone.bundle"
    macos = bundle / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    for name in ("vphone-cli", "vphone-vm", "vphone-escalator"):
        binary = macos / name
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
    vm = tmp_path / "isolated library" / "test phone"
    vm.mkdir(parents=True)
    (vm / "config.plist").write_bytes(plistlib.dumps({"schemaVersion": 2}))
    return bundle, vm


def test_launch_uses_explicit_bundle_and_library(target):
    bundle, vm = target
    assert upstream.lifecycle_command("launch", bundle=bundle, vm_dir=vm, headless=True) == [
        str(bundle / "Contents/MacOS/vphone-cli"), "vm", "launch", "--library-root",
        str(vm.parent), "--headless", "--", vm.name,
    ]


def test_stop_delegates_graceful_shutdown_to_upstream(target):
    bundle, vm = target
    assert upstream.lifecycle_command("stop", bundle=bundle, vm_dir=vm, timeout=45)[1:] == [
        "vm", "stop", "--library-root", str(vm.parent), "--timeout", "45", "--", vm.name,
    ]


def test_preflight_needs_no_vm(target):
    bundle, _ = target
    assert upstream.lifecycle_command("preflight", bundle=bundle)[1:] == ["host", "preflight"]


@pytest.mark.parametrize("schema", [None, 1, "2", True])
def test_legacy_or_invalid_schema_is_refused(target, schema):
    bundle, vm = target
    manifest = {} if schema is None else {"schemaVersion": schema}
    (vm / "config.plist").write_bytes(plistlib.dumps(manifest))
    with pytest.raises(upstream.UpstreamConfigurationError, match="schemaVersion=2"):
        upstream.lifecycle_command("launch", bundle=bundle, vm_dir=vm)


@pytest.mark.parametrize("action", ["launch", "stop"])
def test_vm_path_is_required(target, action):
    bundle, _ = target
    with pytest.raises(upstream.UpstreamConfigurationError, match="vm-dir"):
        upstream.lifecycle_command(action, bundle=bundle)


@pytest.mark.parametrize("name", ["vphone-cli", "vphone-vm", "vphone-escalator"])
def test_incomplete_bundle_fails_before_execution(target, name):
    bundle, _ = target
    (bundle / "Contents/MacOS" / name).chmod(0o644)
    with pytest.raises(upstream.UpstreamConfigurationError, match="executable is missing"):
        upstream.lifecycle_command("preflight", bundle=bundle)


def test_invalid_manifest_fails_closed(target):
    bundle, vm = target
    (vm / "config.plist").write_text("invalid plist")
    with pytest.raises(upstream.UpstreamConfigurationError, match="cannot read"):
        upstream.lifecycle_command("stop", bundle=bundle, vm_dir=vm)


def test_dry_run_never_executes(target, monkeypatch, capsys):
    bundle, vm = target
    monkeypatch.setattr(upstream.os, "execv", lambda *_: pytest.fail("unexpected exec"))
    assert upstream.main(["launch", "--bundle", str(bundle), "--vm-dir", str(vm), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)[-1] == vm.name


def test_launch_executes_once_without_protocol_fallback(target, monkeypatch):
    bundle, vm = target
    calls = []
    def refuse(*args):
        calls.append(args)
        raise OSError("launch refused")
    monkeypatch.setattr(upstream.os, "execv", refuse)
    with pytest.raises(SystemExit) as error:
        upstream.main(["launch", "--bundle", str(bundle), "--vm-dir", str(vm)])
    assert error.value.code == 1
    assert len(calls) == 1


@pytest.mark.parametrize("options", [{"headless": True}, {"timeout": 0}, {"timeout": -1}])
def test_invalid_options_are_refused(target, options):
    bundle, vm = target
    with pytest.raises(upstream.UpstreamConfigurationError):
        upstream.lifecycle_command("stop", bundle=bundle, vm_dir=vm, **options)


def test_managed_execute_stage_cannot_launch(target, monkeypatch):
    from autophone.device.stage_guard import StageAccessDenied, StageGuard
    bundle, vm = target
    monkeypatch.setattr(upstream.os, "execv", lambda *_: pytest.fail("unexpected exec"))
    guard = StageGuard(owner_id="test", rig_id="test", generation=1, check_lease=lambda: None)
    with guard.scope("execute"):
        with pytest.raises(StageAccessDenied):
            upstream.main(["launch", "--bundle", str(bundle), "--vm-dir", str(vm)])


def test_exec_preserves_upstream_process_arguments(target, monkeypatch):
    bundle, vm = target
    command = upstream.lifecycle_command("launch", bundle=bundle, vm_dir=vm)
    calls = []
    monkeypatch.setattr(upstream.os, "execv", lambda *args: calls.append(args))
    assert upstream.main(["launch", "--bundle", str(bundle), "--vm-dir", str(vm)]) == 0
    assert calls == [(command[0], command)]


def test_launch_lock_survives_exec_and_releases_after_exit(target, tmp_path):
    bundle, vm = target
    ready = tmp_path / "ready"
    cli = bundle / "Contents/MacOS/vphone-cli"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import os, time\n"
        "from pathlib import Path\n"
        "Path(os.environ['TEST_READY']).write_text('ready')\n"
        "time.sleep(float(os.environ.get('TEST_HOLD', '0')))\n"
    )
    command = [sys.executable, "-m", "autophone.rig.upstream", "launch",
               "--bundle", str(bundle), "--vm-dir", str(vm)]
    environment = {**os.environ, "TEST_READY": str(ready), "TEST_HOLD": "10",
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    first = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            assert first.poll() is None
            time.sleep(.01)
        assert ready.exists()
        second = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=3)
        assert second.returncode == 1
        assert "VM launch is already active" in second.stderr
    finally:
        first.terminate()
        first.communicate(timeout=5)
    third = subprocess.run(command, env={**environment, "TEST_HOLD": "0"},
                           capture_output=True, text=True, timeout=3)
    assert third.returncode == 0, third.stderr


def test_stop_does_not_wait_for_launch_lock(target, monkeypatch):
    bundle, vm = target
    calls = []
    monkeypatch.setattr(upstream.os, "execv", lambda *args: calls.append(args))
    with upstream.launch_lock(vm):
        assert upstream.main(["stop", "--bundle", str(bundle), "--vm-dir", str(vm)]) == 0
    assert calls[0][1][2] == "stop"


def test_launch_lock_refuses_symlink(target, tmp_path):
    _, vm = target
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    (vm / ".autophone-launch.lock").symlink_to(outside)
    with pytest.raises(OSError):
        with upstream.launch_lock(vm):
            pytest.fail("symlink admitted")
    assert outside.read_text() == "unchanged"
