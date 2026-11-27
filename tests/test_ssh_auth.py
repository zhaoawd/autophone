"""Password input and stdin isolation; real OpenSSH tests use local keys only."""
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

from autophone.device import ssh_auth


def test_helper_is_private_has_no_secret_and_preserves_stdin(monkeypatch):
    password = "test '$` password"
    saved = {}
    original_env = dict(os.environ)
    stream = object()

    def run(command, **kwargs):
        helper = Path(kwargs["env"]["SSH_ASKPASS"])
        saved["helper"] = helper
        assert helper.is_file()
        assert stat.S_IMODE(helper.stat().st_mode) == 0o700
        assert stat.S_IMODE(helper.parent.stat().st_mode) == 0o700
        assert password not in helper.read_text()
        assert password not in command
        assert kwargs["env"]["AUTOPHONE_SSH_PASSWORD"] == password
        assert kwargs["env"]["SSH_ASKPASS_REQUIRE"] == "force"
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] is stream
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    ssh_auth.run_password_command(["ssh", "unused-host"], password=password, stdin=stream)
    assert not saved["helper"].parent.exists()
    assert dict(os.environ) == original_env


@pytest.mark.parametrize("error", [OSError("unavailable"), subprocess.TimeoutExpired(["ssh"], 1)])
def test_helper_removed_after_launch_error_or_timeout(monkeypatch, error):
    saved = {}

    def run(command, **kwargs):
        saved["helper"] = Path(kwargs["env"]["SSH_ASKPASS"])
        raise error

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(type(error)):
        ssh_auth.run_password_command(["ssh"], password="test-only")
    assert not saved["helper"].parent.exists()


@pytest.fixture
def encrypted_key(tmp_path):
    keygen = shutil.which("ssh-keygen")
    if not keygen:
        pytest.skip("OpenSSH keygen is unavailable")
    password = "test-only ' $ ` passphrase"
    key = tmp_path / "key"
    subprocess.run(
        [keygen, "-q", "-t", "ed25519", "-a", "1", "-N", password, "-f", str(key)],
        capture_output=True, text=True, timeout=10, check=True,
    )
    return keygen, key, password


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["api", "cli"])
def test_real_openssh_password_input_without_a_tty(encrypted_key, mode, monkeypatch):
    keygen, key, password = encrypted_key
    monkeypatch.delenv("DISPLAY", raising=False)
    command = [keygen, "-y", "-f", str(key)]
    if mode == "api":
        result = ssh_auth.run_password_command(
            command, password=password, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=10,
        )
    else:
        result = subprocess.run(
            [sys.executable, str(Path(ssh_auth.__file__)), *command],
            env={**os.environ, "AUTOPHONE_SSH_PASSWORD": password},
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=10, start_new_session=True,
        )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split()[:2] == key.with_suffix(".pub").read_text().split()[:2]
    assert password not in result.stderr


@pytest.mark.integration
def test_real_openssh_rejects_incorrect_password(encrypted_key):
    keygen, key, _ = encrypted_key
    result = ssh_auth.run_password_command(
        [keygen, "-y", "-f", str(key)], password="incorrect-test-password",
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert not result.stdout
    assert "incorrect-test-password" not in result.stderr


def test_transfer_cleanup_uses_the_same_password_channel(monkeypatch, fake_guest_ssh_password):
    from types import SimpleNamespace
    from autophone.device import session
    from autophone.device.transfer import GuestTransfer

    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["env"]["SSH_ASKPASS_REQUIRE"] == "force"
        assert command[0] == "ssh"
        assert kwargs["env"]["AUTOPHONE_SSH_PASSWORD"] == fake_guest_ssh_password
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    target = SimpleNamespace(ssh_port=2222, ssh_host="127.0.0.1")
    session._cleanup_transfer(GuestTransfer("/unused/target", 1), target, fake_guest_ssh_password)
    assert len(calls) == 1


@pytest.mark.parametrize("relative", [
    "autophone/device/ssh_auth.py", "autophone/device/session.py", "autophone/packages/store.py",
])
def test_password_channel_changes_invalidate_runtime_identity(tmp_path, relative):
    from autophone.paths import CODE_ROOT
    from autophone.rig.source_identity import KEY_MODULE_PATHS, module_digest

    for name in {*KEY_MODULE_PATHS, relative}:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((CODE_ROOT / name).read_bytes())
    before = module_digest(tmp_path)
    changed = tmp_path / relative
    changed.write_bytes(changed.read_bytes() + b"\n# password channel changed\n")
    assert module_digest(tmp_path) != before


def test_password_configuration_preserves_explicit_override(fake_guest_ssh_password):
    assert ssh_auth.guest_ssh_password() == fake_guest_ssh_password
    assert ssh_auth.guest_ssh_password("explicit") == "explicit"
    with pytest.raises(ValueError, match="AUTOPHONE_SSH_PASSWORD"):
        ssh_auth.guest_ssh_password("")
