"""DeviceTarget 生成工具：字段映射与 fail-closed 行为。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from autophone.rig import emit_target
from autophone.rig.target import DeviceTarget, TargetConfigurationError


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "AUTOPHONE_HOST_ID": "mac-mini",
        "AUTOPHONE_RIG_ID": "vm",
        "VPHONE_CLI_DIR": str(tmp_path / "vphone-cli"),
        "VM_DIR": str(tmp_path / "vphone-cli" / "vm"),
        "VPHONE_SOCK": str(tmp_path / "vphone-cli" / "vm" / "vphone.sock"),
        "AUTOPHONE_EXPECTED_USB_UDID": "0000FE01-6FFDD7F66148F3F9",
        "AUTOPHONE_SSH_PORT": "2222",
        "AUTOPHONE_FRIDA_HOST_PORT": "27042",
        "AUTOPHONE_FRIDA_GUEST_PORT": "27042",
        "VPHONE_LOG_DIR": str(tmp_path / "vphone-cli" / "vm" / "logs"),
        "AUTOPHONE_RIG_RUNTIME_DIR": str(tmp_path / "rigs" / "vm"),
    }


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path))


def test_environment_maps_onto_every_device_target_field(tmp_path: Path) -> None:
    target = emit_target.target_from_environment(_env(tmp_path))
    assert target.identity == ("mac-mini", "vm", 1)
    assert target.cli_dir == _real(tmp_path / "vphone-cli")
    assert target.vm_dir == _real(tmp_path / "vphone-cli" / "vm")
    assert target.socket == _real(tmp_path / "vphone-cli" / "vm" / "vphone.sock")
    assert target.expected_usb_udid == "0000FE01-6FFDD7F66148F3F9"
    assert (target.ssh_host, target.ssh_port) == ("127.0.0.1", 2222)
    assert (target.frida_host, target.frida_port, target.frida_guest_port) == (
        "127.0.0.1",
        27042,
        27042,
    )
    assert target.log_dir == _real(tmp_path / "vphone-cli" / "vm" / "logs")
    assert target.temp_dir == _real(tmp_path / "rigs" / "vm")
    # package_cache 没有独立 env 时派生成 rig 私有目录，不与别的 Rig 共享。
    assert target.package_cache == _real(tmp_path / "rigs" / "vm" / "package-cache")
    # runtime_dir 跟随 AUTOPHONE_RIG_RUNTIME_DIR，而不是 temp_dir/"runtime"：
    # 健康守护把 health.json 写在前者，agent 子进程必须读同一个文件。
    assert target.runtime_dir == _real(tmp_path / "rigs" / "vm")
    assert target.health_state_file == _real(tmp_path / "rigs" / "vm") / "health.json"


def test_rig_id_falls_back_to_ap_device(tmp_path: Path) -> None:
    env = _env(tmp_path)
    del env["AUTOPHONE_RIG_ID"]
    env["AP_DEVICE"] = "vm"
    assert emit_target.target_from_environment(env).rig_id == "vm"


def test_explicit_overrides_beat_environment(tmp_path: Path) -> None:
    target = emit_target.target_from_environment(
        _env(tmp_path),
        {"host_id": "other-host", "rig_generation": "3", "ssh_host": "127.0.0.2"},
    )
    assert target.identity == ("other-host", "vm", 3)
    assert target.ssh_host == "127.0.0.2"


def test_explicit_package_cache_is_kept(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env["AUTOPHONE_RIG_PACKAGE_CACHE"] = str(tmp_path / "shared-cache")
    assert emit_target.target_from_environment(env).package_cache == _real(
        tmp_path / "shared-cache"
    )


def test_paths_are_normalized_so_the_same_vm_yields_one_document(tmp_path: Path) -> None:
    """带 `..` 的写法与规范写法必须产出同一份文档。

    rig_targets 上有 UNIQUE(host_id, vm_dir) 和 uq_rig_targets_host_usb_udid：
    同一台 VM 拿到两种 vm_dir 字符串时，前者认为是新 target、后者认为 UDID 重复，
    注册直接失败。脚本默认得到的 cli_dir 正是 `<repo>/../vphone-cli` 这种带 `..` 的形式。
    """
    (tmp_path / "vphone-cli" / "vm").mkdir(parents=True)
    (tmp_path / "sibling").mkdir()
    canonical = emit_target.target_from_environment(_env(tmp_path))

    dotted = _env(tmp_path)
    dotted["VPHONE_CLI_DIR"] = str(tmp_path / "sibling" / ".." / "vphone-cli")
    dotted["VM_DIR"] = str(tmp_path / "sibling" / ".." / "vphone-cli" / "vm")
    assert emit_target.target_from_environment(dotted).public_dict() == canonical.public_dict()


def test_runtime_dir_defaults_to_the_health_daemon_directory(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env["AUTOPHONE_RIG_TEMP_DIR"] = str(tmp_path / "scratch")
    target = emit_target.target_from_environment(env)
    # temp_dir 与 runtime_dir 是两个独立字段：前者可以另设，后者仍指健康文件所在处。
    assert target.temp_dir == _real(tmp_path / "scratch")
    assert target.runtime_dir == _real(tmp_path / "rigs" / "vm")


def test_device_target_without_runtime_dir_keeps_the_old_derivation() -> None:
    # 旧文档（无 runtime_dir 键）必须与本字段引入之前完全等价。
    target = DeviceTarget.from_mapping({
        "host_id": "h", "rig_id": "r", "rig_generation": 1,
        "cli_dir": "/a/cli", "vm_dir": "/a/vm", "socket": "/a/vm/vphone.sock",
        "expected_usb_udid": "udid", "ssh_host": "127.0.0.1", "ssh_port": 2222,
        "frida_host": "127.0.0.1", "frida_port": 27042, "frida_guest_port": 27042,
        "log_dir": "/a/logs", "temp_dir": "/a/tmp", "package_cache": "/a/pkg",
    })
    assert target.runtime_dir == Path("/a/tmp/runtime")
    assert target.health_state_file == Path("/a/tmp/runtime/health.json")


@pytest.mark.parametrize(
    "missing",
    [
        "AUTOPHONE_HOST_ID",
        "AUTOPHONE_EXPECTED_USB_UDID",
        "AUTOPHONE_SSH_PORT",
        "AUTOPHONE_FRIDA_HOST_PORT",
        "VPHONE_LOG_DIR",
        "AUTOPHONE_RIG_RUNTIME_DIR",
    ],
)
def test_missing_required_field_fails_closed(tmp_path: Path, missing: str) -> None:
    env = _env(tmp_path)
    del env[missing]
    with pytest.raises(TargetConfigurationError):
        emit_target.target_from_environment(env)


def test_empty_expected_usb_udid_is_not_fabricated(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env["AUTOPHONE_EXPECTED_USB_UDID"] = ""
    with pytest.raises(TargetConfigurationError):
        emit_target.target_from_environment(env)


def test_relative_paths_fail_closed(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env["VM_DIR"] = "vm"
    with pytest.raises(TargetConfigurationError):
        emit_target.target_from_environment(env)


def test_ssh_and_frida_must_not_share_one_port(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env["AUTOPHONE_FRIDA_HOST_PORT"] = env["AUTOPHONE_SSH_PORT"]
    with pytest.raises(TargetConfigurationError):
        emit_target.target_from_environment(env)


def test_no_runtime_dir_drift_in_the_default_bring_up(tmp_path: Path) -> None:
    env = _env(tmp_path)
    target = emit_target.target_from_environment(env)
    assert emit_target.runtime_dir_drift(target, env) is None


def test_runtime_dir_drift_fails_closed(tmp_path: Path) -> None:
    env = _env(tmp_path)
    drifted = emit_target.target_from_environment(
        env, {"runtime_dir": str(tmp_path / "elsewhere")}
    )
    drift = emit_target.runtime_dir_drift(drifted, env)
    assert drift is not None and "VPHONE_HEALTH_STATE_FILE" in drift


def test_cli_writes_a_document_that_device_target_can_load_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in _env(tmp_path).items():
        monkeypatch.setenv(key, value)
    out = tmp_path / "nested" / "device-target.json"
    assert emit_target.main(["--out", str(out)]) == 0
    reloaded = DeviceTarget.load(out)
    assert reloaded.identity == ("mac-mini", "vm", 1)
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["expected_usb_udid"]
    assert document["runtime_dir"] == str(_real(tmp_path / "rigs" / "vm"))
    assert not list(out.parent.glob("*.tmp"))
    assert capsys.readouterr().err == ""


def test_cli_refuses_to_write_a_target_whose_health_paths_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in _env(tmp_path).items():
        monkeypatch.setenv(key, value)
    out = tmp_path / "device-target.json"
    assert emit_target.main(
        ["--out", str(out), "--runtime-dir", str(tmp_path / "elsewhere")]
    ) == 2
    assert not out.exists()


def test_cli_writes_nothing_when_a_field_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    env["AUTOPHONE_EXPECTED_USB_UDID"] = ""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    out = tmp_path / "device-target.json"
    assert emit_target.main(["--out", str(out)]) == 2
    assert not out.exists()
