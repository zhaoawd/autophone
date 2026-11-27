import subprocess
import sys
from pathlib import Path

import pytest

from tools.phase1 import ssh_bringup
from autophone.device.shell import ShellResult


def test_verify_login_uses_askpass_without_password_in_argv(fake_guest_ssh_password, monkeypatch):
    seen: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="uid=501(mobile)\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ssh_bringup.verify_login()

    cmd = seen["cmd"]
    assert isinstance(cmd, list)
    assert cmd[0] == "ssh"
    assert all(fake_guest_ssh_password not in arg for arg in cmd)
    assert seen["kwargs"]["env"]["AUTOPHONE_SSH_PASSWORD"] == fake_guest_ssh_password
    assert seen["kwargs"]["env"]["SSH_ASKPASS_REQUIRE"] == "force"
    assert "-n" not in cmd
    assert "-o" in cmd
    assert "PreferredAuthentications=password" in cmd
    assert "PubkeyAuthentication=no" in cmd
    assert "IdentitiesOnly=yes" in cmd
    assert "NumberOfPasswordPrompts=1" in cmd
    assert cmd[-1].startswith("export PATH=/var/jb/usr/bin:/var/jb/bin:")
    assert seen["kwargs"]["capture_output"] is True
    assert seen["kwargs"]["text"] is True
    assert seen["kwargs"]["timeout"] == 20
    assert seen["kwargs"]["start_new_session"] is True


@pytest.mark.usefixtures("fake_guest_ssh_password")
def test_main_uses_native_dropbear_before_openssh_bootstrap(monkeypatch):
    active_port = {"value": None}
    tunnel_ports: list[int] = []
    verified: list[bool] = []

    def start_iproxy(port=ssh_bringup.GUEST_PORT):
        active_port["value"] = port
        tunnel_ports.append(port)

    def login_usable():
        if active_port["value"] == ssh_bringup.DROPBEAR_GUEST_PORT:
            return True, ""
        return False, "connection refused"

    monkeypatch.setattr(sys, "argv", ["ssh_bringup"])
    monkeypatch.setattr(ssh_bringup, "start_iproxy", start_iproxy)
    monkeypatch.setattr(ssh_bringup, "login_usable", login_usable)
    monkeypatch.setattr(
        ssh_bringup,
        "ensure_host_keys",
        lambda: pytest.fail("OpenSSH bootstrap must not run when dropbear works"),
    )
    monkeypatch.setattr(ssh_bringup, "verify_login", lambda: verified.append(True))

    assert ssh_bringup.main() == 0
    assert tunnel_ports == [
        ssh_bringup.GUEST_PORT,
        ssh_bringup.DROPBEAR_GUEST_PORT,
    ]
    assert verified == [True]


def test_start_sshd_rejects_port_22_held_by_non_standalone_listener(monkeypatch):
    bg_calls: list[str] = []

    def fake_run_shell(cmd, **kwargs):
        if "launchctl list" in cmd:
            return ShellResult(
                stdout="123\t0\tcom.openssh.sshd\n",
                stderr="",
                code=0,
                timed_out=False,
                truncated=False,
            )
        if "launchctl unload" in cmd:
            return ShellResult(
                stdout="Unload failed: Input/output error\n",
                stderr="",
                code=0,
                timed_out=False,
                truncated=False,
            )
        if "ps aux" in cmd:
            return ShellResult(
                stdout="",
                stderr="",
                code=0,
                timed_out=False,
                truncated=False,
            )
        if "netstat" in cmd:
            return ShellResult(
                stdout="tcp4       0      0  *.22                   *.*                    LISTEN\n",
                stderr="",
                code=0,
                timed_out=False,
                truncated=False,
            )
        raise AssertionError(f"unexpected shell command: {cmd}")

    monkeypatch.setattr(ssh_bringup, "run_shell", fake_run_shell)
    monkeypatch.setattr(ssh_bringup, "run_shell_bg", lambda cmd: bg_calls.append(cmd))
    monkeypatch.setattr(ssh_bringup.time, "sleep", lambda _: None)

    with pytest.raises(RuntimeError, match="standalone sshd"):
        ssh_bringup.start_sshd()

    assert bg_calls == [ssh_bringup.SSHD]


@pytest.mark.usefixtures("fake_guest_ssh_password")
def test_login_usable_reports_last_error_line(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 255, stdout="",
            stderr="Warning: Permanently added '[127.0.0.1]:2222'\n"
                   "mobile@127.0.0.1: Permission denied (publickey,password).\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(ssh_bringup.time, "sleep", lambda _seconds: None)

    ok, err = ssh_bringup.login_usable()
    assert ok is False
    assert "Permission denied" in err


@pytest.mark.usefixtures("fake_guest_ssh_password")
def test_login_usable_retries_a_transient_password_denial(monkeypatch):
    """Right after boot the guest denies password auth transiently; a single
    denial must not condemn an endpoint that works on the next try."""
    outcomes = iter([
        subprocess.CompletedProcess(
            [], 255, stdout="",
            stderr="mobile@127.0.0.1: Permission denied (publickey,password).\n"),
        subprocess.CompletedProcess([], 0, stdout="AUTOPHONE_SSH_OK\n", stderr=""),
    ])
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kwargs: next(outcomes))
    monkeypatch.setattr(ssh_bringup.time, "sleep", lambda _seconds: None)

    assert ssh_bringup.login_usable() == (True, "")


@pytest.mark.usefixtures("fake_guest_ssh_password")
def test_verify_login_retries_then_reports_the_last_failure(monkeypatch):
    attempts: list[int] = []

    def fake_run(cmd, **kwargs):
        attempts.append(1)
        return subprocess.CompletedProcess(
            cmd, 255, stdout="",
            stderr="mobile@127.0.0.1: Permission denied (publickey,password).\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(ssh_bringup.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="Permission denied"):
        ssh_bringup.verify_login()
    assert len(attempts) == ssh_bringup.LOGIN_ATTEMPTS


@pytest.mark.usefixtures("fake_guest_ssh_password")
def test_verify_login_succeeds_after_a_transient_denial(monkeypatch):
    outcomes = iter([
        subprocess.CompletedProcess(
            [], 255, stdout="",
            stderr="mobile@127.0.0.1: Permission denied (publickey,password).\n"),
        subprocess.CompletedProcess([], 0, stdout="uid=501(mobile)\n", stderr=""),
    ])
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kwargs: next(outcomes))
    monkeypatch.setattr(ssh_bringup.time, "sleep", lambda _seconds: None)

    ssh_bringup.verify_login()


@pytest.mark.usefixtures("fake_guest_ssh_password")
def test_login_usable_ok_on_probe_echo(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="AUTOPHONE_SSH_OK\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert ssh_bringup.login_usable() == (True, "")


def test_fallback_port_starts_sshd_and_repoints_iproxy(monkeypatch):
    bg_calls: list[str] = []
    iproxy_ports: list[int] = []
    listening = {"alt": False}

    def fake_run_shell(cmd, **kwargs):
        if "netstat" in cmd:
            out = (f"tcp4  0 0 *.{ssh_bringup.ALT_GUEST_PORT} *.* LISTEN\n"
                   if listening["alt"] else "")
            return ShellResult(stdout=out, stderr="", code=0,
                               timed_out=False, truncated=False)
        raise AssertionError(f"unexpected shell command: {cmd}")

    def fake_bg(cmd):
        bg_calls.append(cmd)
        listening["alt"] = True

    monkeypatch.setattr(ssh_bringup, "run_shell", fake_run_shell)
    monkeypatch.setattr(ssh_bringup, "run_shell_bg", fake_bg)
    monkeypatch.setattr(ssh_bringup, "start_iproxy", iproxy_ports.append)
    monkeypatch.setattr(ssh_bringup.time, "sleep", lambda _: None)

    ssh_bringup.start_sshd_fallback_port()

    assert bg_calls == [f"{ssh_bringup.SSHD} -p {ssh_bringup.ALT_GUEST_PORT}"]
    assert iproxy_ports == [ssh_bringup.ALT_GUEST_PORT]


def test_iproxy_command_binds_the_expected_usb_udid(monkeypatch):
    monkeypatch.setattr(ssh_bringup, "HOST_PORT", 2223)
    monkeypatch.setattr(
        ssh_bringup,
        "EXPECTED_UDID",
        "00000000-0000000000000002",
    )

    assert ssh_bringup._iproxy_command(22) == [
        "iproxy",
        "-u",
        "00000000-0000000000000002",
        "2223:22",
    ]
    assert ssh_bringup._ssh_cmd("id")[-3] == "2223"


def test_start_iproxy_records_the_exact_process_pid(monkeypatch, tmp_path: Path):
    calls = []

    class Proc:
        pid = 4321

    states = iter([False, True])
    monkeypatch.setattr(ssh_bringup, "_iproxy_running", lambda _port: next(states))
    monkeypatch.setattr(ssh_bringup, "_stop_iproxy_on_host_port", lambda: ())
    monkeypatch.setattr(
        ssh_bringup.subprocess,
        "Popen",
        lambda command, **kwargs: calls.append((command, kwargs)) or Proc(),
    )
    monkeypatch.setattr(ssh_bringup.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ssh_bringup, "HOST_PORT", 2223)
    monkeypatch.setattr(ssh_bringup, "EXPECTED_UDID", "udid-b")
    pid_file = tmp_path / "iproxy.pid"
    monkeypatch.setattr(ssh_bringup, "IPROXY_PID_FILE", pid_file)

    ssh_bringup.start_iproxy(22)

    assert calls[0][0] == ["iproxy", "-u", "udid-b", "2223:22"]
    assert pid_file.read_text(encoding="utf-8") == "4321\n"


def test_stop_iproxy_matches_exact_host_port_and_migrates_legacy_shape(monkeypatch):
    monkeypatch.setattr(ssh_bringup, "HOST_PORT", 2222)
    monkeypatch.setattr(ssh_bringup, "_iproxy_processes", lambda: (
        (101, "udid-a", 12222, 22),
        (102, "", 2222, 22),
        (103, "udid-a", 2222, 22222),
    ))
    killed = []
    monkeypatch.setattr(ssh_bringup.os, "kill", lambda pid, kind: killed.append((pid, kind)))
    monkeypatch.setattr(ssh_bringup.time, "sleep", lambda _seconds: None)

    assert ssh_bringup._stop_iproxy_on_host_port() == (102, 103)
    assert [pid for pid, _kind in killed] == [102, 103]


def test_iproxy_process_parser_accepts_new_and_legacy_shapes(monkeypatch):
    output = """\
101 /opt/homebrew/bin/iproxy -u udid-a 2222:22
102 iproxy 2223 22222
103 iproxy -u udid-b 12222:22
"""
    monkeypatch.setattr(
        ssh_bringup.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, output, ""),
    )

    assert ssh_bringup._iproxy_processes() == (
        (101, "udid-a", 2222, 22),
        (102, "", 2223, 22222),
        (103, "udid-b", 12222, 22),
    )


@pytest.mark.parametrize("value", [None, ""])
def test_missing_password_refused_before_tunnel_setup(monkeypatch, capsys, value):
    if value is None:
        monkeypatch.delenv("AUTOPHONE_SSH_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("AUTOPHONE_SSH_PASSWORD", value)
    monkeypatch.setattr(sys, "argv", ["ssh_bringup", "--no-verify"])
    monkeypatch.setattr(ssh_bringup, "start_iproxy", lambda: pytest.fail("unexpected tunnel"))
    with pytest.raises(SystemExit) as exc:
        ssh_bringup.main()
    assert exc.value.code == 2
    assert "AUTOPHONE_SSH_PASSWORD" in capsys.readouterr().err


def test_probe_and_verify_use_configured_password_without_logging_it(
    monkeypatch, fake_guest_ssh_password, capsys,
):
    calls = []
    def run(cmd, *, password, **kwargs):
        assert password == fake_guest_ssh_password
        assert all(password not in arg for arg in cmd)
        calls.append(cmd[-1])
        output = "AUTOPHONE_SSH_OK" if len(calls) == 1 else "uid=501(mobile)"
        return subprocess.CompletedProcess(cmd, 0, stdout=output, stderr="")
    monkeypatch.setattr(sys, "argv", ["ssh_bringup"])
    monkeypatch.setattr(ssh_bringup, "start_iproxy", lambda: None)
    monkeypatch.setattr(ssh_bringup, "run_password_command", run)
    assert ssh_bringup.main() == 0
    assert len(calls) == 2
    logs = capsys.readouterr()
    assert fake_guest_ssh_password not in logs.out + logs.err
    assert "password:" not in logs.out
