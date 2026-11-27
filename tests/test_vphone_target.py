from __future__ import annotations

from pathlib import Path

import pytest


def test_default_target_is_vm_when_another_vm_directory_also_exists(tmp_path: Path) -> None:
    from autophone.device.target import resolve_target

    cli = tmp_path / "vphone-cli"
    legacy = cli / "vm-legacy" / "vphone.sock"
    current = cli / "vm" / "vphone.sock"
    legacy.parent.mkdir(parents=True)
    current.parent.mkdir(parents=True)
    legacy.touch()
    current.touch()

    target = resolve_target(environ={}, cli_dir=cli)

    assert target.device == "vm"
    assert target.vm_dir == cli / "vm"
    assert target.sock == current


def test_vm_is_selected_explicitly(tmp_path: Path) -> None:
    from autophone.device.target import resolve_target

    cli = tmp_path / "vphone-cli"
    target = resolve_target(
        environ={"VM_DIR": "vm"},
        cli_dir=cli,
    )

    assert target.device == "vm"
    assert target.vm_dir == cli / "vm"
    assert target.sock == cli / "vm" / "vphone.sock"
    assert target.public_dict() == {
        "device": "vm",
        "vm": "vm",
        "vm_dir": str(cli / "vm"),
        "socket": str(cli / "vm" / "vphone.sock"),
    }


def test_vm_alt_is_selected_explicitly_with_directory_device_key(tmp_path: Path) -> None:
    from autophone.device.target import resolve_target, resolve_screen_lock_target

    cli = tmp_path / "vphone-cli"
    target = resolve_target(environ={"VM_DIR": "vm-alt"}, cli_dir=cli)

    assert target.device == "vm-alt"
    assert target.vm_dir == cli / "vm-alt"
    assert target.sock == cli / "vm-alt" / "vphone.sock"
    assert target.public_dict() == {
        "device": "vm-alt",
        "vm": "vm-alt",
        "vm_dir": str(cli / "vm-alt"),
        "socket": str(cli / "vm-alt" / "vphone.sock"),
    }

    locked = resolve_screen_lock_target(environ={"VM_DIR": "vm-alt"}, cli_dir=cli)
    assert locked.device == "vm-alt"
    assert locked.sock == (cli / "vm-alt" / "vphone.sock").resolve()


def test_other_vm_directory_has_no_vphone_0_mapping(tmp_path: Path) -> None:
    from autophone.device.target import resolve_target

    cli = tmp_path / "vphone-cli"
    target = resolve_target(environ={"VM_DIR": "vm-legacy"}, cli_dir=cli)

    # 其他 VM 目录不做特殊映射，按普通目录名取设备键。
    assert target.device == "vm-legacy"
    assert target.vm_dir == cli / "vm-legacy"
    assert target.sock == cli / "vm-legacy" / "vphone.sock"


def test_explicit_device_name_overrides_vm_derived_name(tmp_path: Path) -> None:
    from autophone.device.target import resolve_target

    target = resolve_target(
        environ={"VM_DIR": "vm", "AP_DEVICE": "new-lab"},
        cli_dir=tmp_path / "vphone-cli",
    )

    assert target.device == "new-lab"


def test_explicit_socket_is_authoritative(tmp_path: Path) -> None:
    from autophone.device.target import resolve_target

    sock = tmp_path / "custom.sock"
    target = resolve_target(
        environ={"VPHONE_SOCK": str(sock), "VM_DIR": "vm"},
        cli_dir=tmp_path / "vphone-cli",
    )

    assert target.sock == sock


def test_screen_lock_target_rejects_custom_socket_without_explicit_device(
    tmp_path: Path,
) -> None:
    from autophone.device.target import (
        VPhoneTargetBindingError,
        resolve_screen_lock_target,
    )

    with pytest.raises(VPhoneTargetBindingError, match="explicit device"):
        resolve_screen_lock_target(
            environ={"VPHONE_SOCK": str(tmp_path / "alternate.sock")},
            cli_dir=tmp_path / "vphone-cli",
        )


def test_screen_lock_target_rejects_blank_explicit_device(tmp_path: Path) -> None:
    from autophone.device.target import (
        VPhoneTargetBindingError,
        resolve_screen_lock_target,
    )

    with pytest.raises(VPhoneTargetBindingError, match="non-blank"):
        resolve_screen_lock_target(
            device="   ",
            environ={},
            cli_dir=tmp_path / "vphone-cli",
        )


def test_screen_lock_target_rejects_path_like_device(tmp_path: Path) -> None:
    from autophone.device.target import (
        VPhoneTargetBindingError,
        resolve_screen_lock_target,
    )

    with pytest.raises(VPhoneTargetBindingError, match="safe ScreenLock key"):
        resolve_screen_lock_target(
            device="../alternate",
            environ={},
            cli_dir=tmp_path / "vphone-cli",
        )


def test_screen_lock_target_rejects_blank_ap_device(tmp_path: Path) -> None:
    from autophone.device.target import (
        VPhoneTargetBindingError,
        resolve_screen_lock_target,
    )

    with pytest.raises(VPhoneTargetBindingError, match="non-blank"):
        resolve_screen_lock_target(
            environ={"AP_DEVICE": "\t"},
            cli_dir=tmp_path / "vphone-cli",
        )


def test_screen_lock_target_rejects_device_alias_for_default_socket(
    tmp_path: Path,
) -> None:
    from autophone.device.target import (
        VPhoneTargetBindingError,
        resolve_screen_lock_target,
    )

    cli = tmp_path / "vphone-cli"
    with pytest.raises(VPhoneTargetBindingError, match="must match the resolved device"):
        resolve_screen_lock_target(
            sock=cli / "vm" / "vphone.sock",
            device="same-socket-different-lock",
            environ={},
            cli_dir=cli,
        )
def test_target_scope_binds_agent_execution_without_environment(tmp_path: Path) -> None:
    from autophone.device.target import VPhoneTarget, resolve_target, target_scope

    explicit = VPhoneTarget(
        cli_dir=tmp_path / "cli",
        vm_dir=tmp_path / "cli" / "vm-a",
        sock=tmp_path / "cli" / "vm-a" / "vphone.sock",
        device="rig-a",
    )
    with target_scope(explicit):
        assert resolve_target() is explicit

    assert resolve_target(environ={}, cli_dir=tmp_path / "other") is not explicit
