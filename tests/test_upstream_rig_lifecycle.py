from __future__ import annotations

from dataclasses import replace
import plistlib
import signal
import subprocess
from types import SimpleNamespace

import pytest

from autophone.rig.lifecycle import ProcessInfo, RigLifecycle, RigLifecycleError
from autophone.rig.target import TargetConfigurationError
from tests.test_rig_lifecycle import _target


def upstream_target(tmp_path):
    target = _target(tmp_path)
    bundle = tmp_path / "VPhone.bundle"
    macos = bundle / "Contents/MacOS"
    macos.mkdir(parents=True)
    for name in ("vphone-cli", "vphone-vm", "vphone-escalator"):
        path = macos / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    target.config_path.write_bytes(plistlib.dumps({"schemaVersion": 2}))
    return replace(target, protocol="upstream_v1", core_bundle=bundle)


def test_new_rig_uses_bundle_and_health_daemon_without_legacy_script(tmp_path):
    target = upstream_target(tmp_path)
    calls, launches = [], []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    def start(command, **kwargs):
        launches.append((command, kwargs))
        return SimpleNamespace(pid=12345, poll=lambda: None, returncode=None)
    vm = ProcessInfo(55, f"{target.core_bundle}/Contents/MacOS/vphone-vm --config {target.config_path}")
    receipt = RigLifecycle(target, repository=tmp_path, run_command=run,
                           list_processes=lambda: (vm,) if launches else (), start_process=start,
                           probe_uds=lambda *a, **k: {"state": "guest_ready"}).start()
    assert receipt.status == "running"
    assert calls[0][0] == [str(target.core_bundle / "Contents/MacOS/vphone-cli"), "host", "preflight"]
    assert "health_daemon" in calls[1][0][2]
    assert launches[0][0][2] == "autophone.rig.upstream"
    environment = launches[0][1]["env"]
    assert environment["AUTOPHONE_VPHONE_PROTOCOL"] == "upstream_v1"
    assert environment["VPHONE_SOCK"] == str(target.socket)
    assert environment["AUTOPHONE_RIG_TARGET"] == str(target.runtime_target_file)
    assert target.runtime_target_file.is_file()
    assert not any("start_vphone.sh" in str(command) for command, _ in calls)


def test_stop_delegates_sigint_to_selected_upstream_vm(tmp_path):
    target = upstream_target(tmp_path)
    calls, signals = [], []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="stopped", stderr="")
    process = ProcessInfo(55, f"{target.core_bundle}/Contents/MacOS/vphone-vm --config {target.config_path}")
    receipt = RigLifecycle(target, repository=tmp_path, run_command=run,
                           list_processes=lambda: [process, ProcessInfo(201, "iproxy -u udid-a 2222:22"),
                                                   ProcessInfo(202, "iproxy -u udid-b 2223:22")],
                           send_signal=lambda *args: signals.append(args)).stop()
    assert receipt.status == "stopped" and receipt.affected_pids == (201, 55)
    assert calls[-1] == [str(target.core_bundle / "Contents/MacOS/vphone-cli"), "vm", "stop",
                         "--library-root", str(target.vm_dir.parent), "--timeout", "20", "--", target.vm_dir.name]
    assert signals == [(201, signal.SIGTERM)]


def test_readiness_timeout_stops_only_new_launch(tmp_path):
    target = upstream_target(tmp_path)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    terminated = []
    launcher = SimpleNamespace(pid=12345, poll=lambda: None,
                               terminate=lambda: terminated.append(12345), wait=lambda **kwargs: 0)
    with pytest.raises(RigLifecycleError, match="readiness timed out"):
        RigLifecycle(target, repository=tmp_path, run_command=run, list_processes=lambda: (),
                     start_process=lambda *a, **k: launcher,
                     probe_uds=lambda *a, **k: {"state": "host_only"}, sleep=lambda _: None).start(timeout_s=.001)
    assert terminated == [12345]
    assert all(command[1:3] != ["vm", "stop"] for command in calls)


def test_existing_unhealthy_vm_is_not_stopped_by_failed_start(tmp_path):
    target = upstream_target(tmp_path)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    process = ProcessInfo(55, f"{target.core_bundle}/Contents/MacOS/vphone-vm --config {target.config_path}")
    with pytest.raises(RigLifecycleError, match="readiness timed out"):
        RigLifecycle(target, repository=tmp_path, run_command=run, list_processes=lambda: [process],
                     start_process=lambda *a, **k: pytest.fail("duplicate launch"),
                     probe_uds=lambda *a, **k: {"state": "host_only"}, sleep=lambda _: None).start(timeout_s=.001)
    assert len(calls) == 1 and calls[0][1:3] == ["host", "preflight"]


def test_target_generation_configuration_is_not_overwritten(tmp_path):
    target = upstream_target(tmp_path)
    target.ensure_local_directories()
    previous = target.runtime_target_file.read_bytes()
    with pytest.raises(TargetConfigurationError, match="generation configuration changed"):
        replace(target, core_bundle=tmp_path / "different.bundle").ensure_local_directories()
    assert target.runtime_target_file.read_bytes() == previous


def test_target_round_trip_keeps_protocol_and_bundle(tmp_path):
    from autophone.rig.target import DeviceTarget
    target = upstream_target(tmp_path)
    target.ensure_local_directories()
    assert DeviceTarget.load(target.runtime_target_file) == target


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["status", "exception"])
def test_health_start_failure_cleans_up_only_owned_launch(tmp_path, existing, failure):
    target = upstream_target(tmp_path)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if "tools.phase1.health_daemon" in command:
            if failure == "exception":
                raise subprocess.TimeoutExpired(command, 20)
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="health unavailable")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    process = ProcessInfo(55, f"{target.core_bundle}/Contents/MacOS/vphone-vm --config {target.config_path}")
    terminated = []
    launcher = SimpleNamespace(pid=12345, poll=lambda: None,
                               terminate=lambda: terminated.append(12345), wait=lambda **kwargs: 0)
    with pytest.raises(RigLifecycleError, match="health|timed out"):
        RigLifecycle(target, repository=tmp_path, run_command=run,
                     list_processes=lambda: [process] if existing else [],
                     start_process=lambda *a, **k: launcher,
                     probe_uds=lambda *a, **k: {"state": "guest_ready"}).start()
    stops = [command for command in calls if command[1:3] == ["vm", "stop"]]
    assert not stops
    assert terminated == ([] if existing else [12345])


def test_failed_start_reports_cleanup_failure(tmp_path):
    target = upstream_target(tmp_path)
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    def refuse():
        raise OSError("stop refused")
    with pytest.raises(RigLifecycleError, match="readiness timed out.*cleanup failed.*stop refused"):
        RigLifecycle(target, repository=tmp_path, run_command=run, list_processes=lambda: (),
                     start_process=lambda *a, **k: SimpleNamespace(pid=12345, poll=lambda: None, terminate=refuse),
                     probe_uds=lambda *a, **k: {"state": "host_only"}, sleep=lambda _: None).start(timeout_s=.001)


def test_refused_launcher_never_stops_concurrent_vm(tmp_path):
    target = upstream_target(tmp_path)
    calls, signals = [], []
    snapshots = iter([(), (ProcessInfo(555, f"{target.core_bundle}/Contents/MacOS/vphone-vm --config {target.config_path}"),)])
    lifecycle = RigLifecycle(
        target, repository=tmp_path,
        run_command=lambda command, **kwargs: (
            calls.append(command) or subprocess.CompletedProcess(command, 0, "", "")
        ),
        list_processes=lambda: next(snapshots),
        start_process=lambda *args, **kwargs: SimpleNamespace(pid=12345, poll=lambda: 1, returncode=1),
        send_signal=lambda *args: signals.append(args),
        probe_uds=lambda *args, **kwargs: pytest.fail("refused launcher must fail before probing"),
    )
    with pytest.raises(RigLifecycleError, match="launcher exited: 1"):
        lifecycle.start()
    assert not signals
    assert all(command[1:3] != ["vm", "stop"] for command in calls)


def test_launcher_exiting_during_readiness_never_stops_replacement(tmp_path):
    target = upstream_target(tmp_path)
    calls = []
    exited = False
    def probe(*args, **kwargs):
        nonlocal exited
        exited = True
        return {"state": "host_only"}
    lifecycle = RigLifecycle(
        target, repository=tmp_path,
        run_command=lambda command, **kwargs: (
            calls.append(command) or subprocess.CompletedProcess(command, 0, "", "")
        ), list_processes=lambda: (),
        start_process=lambda *args, **kwargs: SimpleNamespace(
            pid=12345, poll=lambda: 1 if exited else None, returncode=1),
        probe_uds=probe, sleep=lambda _: None,
    )
    with pytest.raises(RigLifecycleError, match="launcher exited: 1"):
        lifecycle.start()
    assert all(command[1:3] != ["vm", "stop"] for command in calls)


def test_cleanup_signals_only_vm_child_of_this_launcher(tmp_path):
    target = upstream_target(tmp_path)
    child = ProcessInfo(555, f"{target.core_bundle}/Contents/MacOS/vphone-vm --config {target.config_path}", parent_pid=12345)
    competitor = ProcessInfo(556, child.command, parent_pid=98765)
    processes, signals, waits = [child, competitor], [], []
    def send(pid, sig):
        signals.append((pid, sig))
        processes[:] = [process for process in processes if process.pid != pid]
    launcher = SimpleNamespace(pid=12345, poll=lambda: None,
                               terminate=lambda: pytest.fail("VM child must shut down first"),
                               wait=lambda **kwargs: waits.append(kwargs))
    lifecycle = RigLifecycle(target, list_processes=lambda: tuple(processes), send_signal=send)
    lifecycle._cleanup_upstream_launch(launcher)
    assert signals == [(555, signal.SIGINT)]
    assert processes == [competitor]
    assert waits == [{"timeout": 5}]


def test_cleanup_does_not_signal_unowned_target_with_live_launcher(tmp_path):
    target = upstream_target(tmp_path)
    competitor = ProcessInfo(556, f"{target.core_bundle}/Contents/MacOS/vphone-vm --config {target.config_path}", parent_pid=98765)
    terminated = []
    launcher = SimpleNamespace(pid=12345, poll=lambda: None,
                               terminate=lambda: terminated.append(12345), wait=lambda **kwargs: 0)
    lifecycle = RigLifecycle(target, list_processes=lambda: (competitor,),
                             send_signal=lambda *args: pytest.fail("unowned VM signalled"))
    lifecycle._cleanup_upstream_launch(launcher)
    assert terminated == [12345]


def test_local_process_snapshot_preserves_parent_identity(monkeypatch):
    from autophone.rig import lifecycle
    binary = "/Library/Application Support/VPhone.bundle/Contents/MacOS/vphone-vm"
    config = "/VM Folder/config.plist"
    monkeypatch.setattr(lifecycle.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, f"  555 12345 {binary} --config {config}\n", ""))
    monkeypatch.setattr(lifecycle, "process_arguments", lambda pid: [binary, "--config", config])
    (process,) = lifecycle._local_processes()
    assert process.pid == 555
    assert process.parent_pid == 12345
    assert lifecycle._command_arguments(process.command)[0] == binary
    assert str(lifecycle._command_config(process.command)) == config


def _idle_sleep_lifecycle(target, processes, start):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    return RigLifecycle(target, repository=target.vm_dir.parent, run_command=run,
                        list_processes=lambda: processes, start_process=start,
                        probe_uds=lambda *a, **k: {"state": "guest_ready"})


def test_running_vm_holds_host_idle_sleep_until_it_exits(tmp_path):
    target = upstream_target(tmp_path)
    vm = ProcessInfo(55, f"{target.core_bundle}/Contents/MacOS/vphone-vm --config {target.config_path}")
    started = []
    def start(command, **kwargs):
        started.append((command, kwargs))
        return SimpleNamespace(pid=66, poll=lambda: None, returncode=None)
    assert _idle_sleep_lifecycle(target, (vm,), start).start().status == "running"
    assert [command for command, _ in started] == [["/usr/bin/caffeinate", "-i", "-w", "55"]]
    assert started[0][1]["start_new_session"] is True


def test_start_does_not_stack_idle_sleep_holds(tmp_path):
    target = upstream_target(tmp_path)
    vm = ProcessInfo(55, f"{target.core_bundle}/Contents/MacOS/vphone-vm --config {target.config_path}")
    hold = ProcessInfo(66, "/usr/bin/caffeinate -i -w 55")
    receipt = _idle_sleep_lifecycle(target, (vm, hold), lambda *a, **k: pytest.fail("second hold")).start()
    assert receipt.status == "running"


def test_start_refuses_a_vm_it_cannot_keep_awake(tmp_path):
    target = upstream_target(tmp_path)
    vm = ProcessInfo(55, f"{target.core_bundle}/Contents/MacOS/vphone-vm --config {target.config_path}")
    def start(command, **kwargs):
        raise OSError("no caffeinate")
    with pytest.raises(RigLifecycleError, match="idle sleep"):
        _idle_sleep_lifecycle(target, (vm,), start).start()


def test_guest_ready_without_a_vm_process_is_refused(tmp_path):
    target = upstream_target(tmp_path)
    launcher = SimpleNamespace(pid=12345, poll=lambda: None, terminate=lambda: None, wait=lambda **kwargs: 0)
    with pytest.raises(RigLifecycleError, match="idle sleep"):
        _idle_sleep_lifecycle(target, (), lambda *a, **k: launcher).start()
