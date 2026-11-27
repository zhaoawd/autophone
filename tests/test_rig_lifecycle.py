from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path

import pytest

from autophone.rig.lifecycle import ProcessInfo, RigLifecycle, RigLifecycleError
from autophone.rig.target import DeviceTarget


def _target(tmp_path: Path) -> DeviceTarget:
    vm_dir = tmp_path / "vphone-cli" / "vm-a"
    vm_dir.mkdir(parents=True)
    (vm_dir / "config.plist").write_text("plist", encoding="utf-8")
    (vm_dir / "udid-prediction.txt").write_text(
        "UDID: udid-a\nCPID: 1\n",
        encoding="utf-8",
    )
    return DeviceTarget(
        host_id="host-a",
        rig_id="rig-a",
        rig_generation=1,
        cli_dir=tmp_path / "vphone-cli",
        vm_dir=vm_dir,
        socket=vm_dir / "vphone.sock",
        expected_usb_udid="udid-a",
        ssh_host="127.0.0.1",
        ssh_port=2222,
        frida_host="127.0.0.1",
        frida_port=27042,
        frida_guest_port=27042,
        log_dir=tmp_path / "logs",
        temp_dir=tmp_path / "tmp",
        package_cache=tmp_path / "packages",
    )


def test_start_passes_one_complete_target_environment(tmp_path: Path) -> None:
    target = _target(tmp_path)
    repository = tmp_path / "repo"
    (repository / "scripts").mkdir(parents=True)
    (repository / "scripts" / "start_vphone.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="healthy\n", stderr="")

    receipt = RigLifecycle(target, repository=repository, run_command=run).start(
        allow_multi_vm=True
    )

    assert receipt.status == "running"
    environment = calls[0][1]["env"]
    assert environment["VPHONE_SOCK"] == str(target.socket)
    assert environment["AUTOPHONE_EXPECTED_USB_UDID"] == "udid-a"
    assert environment["AUTOPHONE_SSH_PORT"] == "2222"
    assert environment["AUTOPHONE_FRIDA_GUEST_PORT"] == "27042"
    assert environment["AUTOPHONE_EXPERIMENTAL_MULTI_VM"] == "1"
    assert environment["VPHONE_LOG_DIR"] == str(target.log_dir)
    assert environment["AUTOPHONE_RIG_PACKAGE_CACHE"] == str(target.package_cache)


def test_start_rejects_a_target_whose_usb_identity_drifted(tmp_path: Path) -> None:
    target = _target(tmp_path)
    (target.vm_dir / "udid-prediction.txt").write_text("UDID: other\n", encoding="utf-8")

    with pytest.raises(RigLifecycleError, match="does not match"):
        RigLifecycle(target, repository=tmp_path).start(allow_multi_vm=True)


def test_start_wraps_subprocess_timeout_in_lifecycle_error(tmp_path: Path) -> None:
    target = _target(tmp_path)
    repository = tmp_path / "repo"
    (repository / "scripts").mkdir(parents=True)
    script = repository / "scripts" / "start_vphone.sh"
    script.write_text("#!/bin/bash\n", encoding="utf-8")

    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    with pytest.raises(RigLifecycleError, match="timed out"):
        RigLifecycle(target, repository=repository, run_command=run).start(
            allow_multi_vm=True,
            timeout_s=1,
        )


def test_start_failure_preserves_stdout_stage_and_stderr_warning(tmp_path: Path) -> None:
    target = _target(tmp_path)
    repository = tmp_path / "repo"
    (repository / "scripts").mkdir(parents=True)
    (repository / "scripts" / "start_vphone.sh").write_text(
        "#!/bin/bash\n",
        encoding="utf-8",
    )

    def run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="[start_vphone] FATAL: health did not reach healthy\n",
            stderr="Warning: SSH host key was added\n",
        )

    with pytest.raises(RigLifecycleError) as caught:
        RigLifecycle(target, repository=repository, run_command=run).start(
            allow_multi_vm=True
        )

    detail = str(caught.value)
    assert "[stdout]" in detail
    assert "FATAL: health did not reach healthy" in detail
    assert "[stderr]" in detail
    assert "Warning: SSH host key was added" in detail


def test_lifecycle_module_help_has_no_runpy_reimport_warning() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::RuntimeWarning",
            "-m",
            "autophone.rig.lifecycle",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "RuntimeWarning" not in completed.stderr


def test_stop_signals_only_processes_owned_by_the_target(tmp_path: Path) -> None:
    target = _target(tmp_path)
    repository = tmp_path / "repo"
    repository.mkdir()
    processes = (
        ProcessInfo(101, f"/opt/vphone-cli --config {target.config_path}"),
        ProcessInfo(102, "/opt/vphone-cli --config /other/config.plist"),
        ProcessInfo(201, "iproxy -u udid-a 2222:22"),
        ProcessInfo(202, "iproxy -u udid-b 2223:22"),
        ProcessInfo(301, "ssh -N -L 27042:127.0.0.1:27042 -p 2222"),
    )
    signals = []

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout='{"status":"stopped"}', stderr="")

    lifecycle = RigLifecycle(
        target,
        repository=repository,
        run_command=run,
        list_processes=lambda: processes,
        send_signal=lambda pid, kind: signals.append((pid, kind)),
        process_alive=lambda _pid: False,
    )

    receipt = lifecycle.stop()

    assert receipt.affected_pids == (101, 201, 301)
    assert signals == [
        (201, signal.SIGTERM),
        (301, signal.SIGTERM),
        (101, signal.SIGTERM),
    ]


def test_stop_also_reclaims_the_legacy_two_port_iproxy_form(tmp_path: Path) -> None:
    # libimobiledevice's older `iproxy [options] HOST GUEST` shape is what
    # tools.phase1.ssh_bringup already accepts; stop() has to see the same
    # helpers, or a forwarder survives the Rig it belonged to.
    target = _target(tmp_path)
    processes = (
        ProcessInfo(201, "iproxy -u udid-a 2222 22"),
        ProcessInfo(202, "iproxy -u udid-a 2223 22"),
    )
    signals = []

    receipt = RigLifecycle(
        target,
        repository=tmp_path,
        run_command=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
        list_processes=lambda: processes,
        send_signal=lambda pid, kind: signals.append((pid, kind)),
        process_alive=lambda _pid: False,
    ).stop()

    assert receipt.affected_pids == (201,)
    assert signals == [(201, signal.SIGTERM)]


def test_stop_uses_exact_frida_forward_token_and_ignores_exited_pid(
    tmp_path: Path,
) -> None:
    target = DeviceTarget.from_mapping({
        **_target(tmp_path).public_dict(),
        "frida_port": 7042,
    })
    processes = (
        ProcessInfo(301, "ssh -N -L 7042:127.0.0.1:27042 -p 2222 mobile@127.0.0.1"),
        ProcessInfo(302, "ssh -N -L 27042:127.0.0.1:27042 -p 2223 mobile@127.0.0.1"),
    )
    signals = []

    def send_signal(pid: int, kind: int) -> None:
        if pid == 301:
            raise ProcessLookupError(pid)
        signals.append((pid, kind))

    receipt = RigLifecycle(
        target,
        repository=tmp_path,
        run_command=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
        list_processes=lambda: processes,
        send_signal=send_signal,
        process_alive=lambda _pid: False,
    ).stop()

    assert receipt.affected_pids == ()
    assert signals == []
