"""Explicit lifecycle entry point for schema-v2 VMs and an upstream Core Bundle.

This stage-1 entry point runs the upstream CLI in the foreground. It does not
start SSH, Frida, health daemons or the Control Plane.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import plistlib
import stat
from typing import Sequence

from autophone.device.stage_guard import OperationClass, guarded_device_operation


class UpstreamConfigurationError(ValueError):
    pass


def lifecycle_command(
    action: str, *, bundle: Path, vm_dir: Path | None = None,
    headless: bool = False, timeout: int = 20,
) -> list[str]:
    """Resolve one command without probing, launching or retrying a VM."""
    if action not in {"preflight", "launch", "stop"}:
        raise UpstreamConfigurationError(f"unsupported action: {action}")
    if headless and action != "launch":
        raise UpstreamConfigurationError("--headless requires launch")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise UpstreamConfigurationError("--timeout must be a positive integer")
    bundle = bundle.expanduser().resolve()
    macos = bundle / "Contents" / "MacOS"
    for name in ("vphone-cli", "vphone-vm", "vphone-escalator"):
        binary = macos / name
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise UpstreamConfigurationError(f"executable is missing: {binary}")
    cli = str(macos / "vphone-cli")
    if action == "preflight":
        return [cli, "host", "preflight"]
    if vm_dir is None:
        raise UpstreamConfigurationError("--vm-dir is required for launch/stop")
    vm_dir = vm_dir.expanduser().resolve()
    config = vm_dir / "config.plist"
    try:
        with config.open("rb") as stream:
            manifest = plistlib.load(stream)
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        raise UpstreamConfigurationError(f"cannot read {config}: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schemaVersion")) is not int
        or manifest["schemaVersion"] != 2
    ):
        raise UpstreamConfigurationError(f"schemaVersion=2 is required: {config}")
    command = [cli, "vm", action, "--library-root", str(vm_dir.parent)]
    if headless:
        command.append("--headless")
    if action == "stop":
        command.extend(["--timeout", str(timeout)])
    return [*command, "--", vm_dir.name]


@contextmanager
def launch_lock(vm_dir: Path):
    """Coordinate launches through this entry point; raw upstream CLI bypasses it."""
    path = vm_dir.expanduser().resolve() / ".autophone-launch.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1):
            raise UpstreamConfigurationError(f"unsafe launch lock: {path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpstreamConfigurationError(f"VM launch is already active: {vm_dir}") from exc
        # vphone-cli waits for its companion, retaining this fd until it exits.
        os.set_inheritable(descriptor, True)
        yield
    finally:
        os.close(descriptor)


@guarded_device_operation(OperationClass.VM, identity=lambda a: {
    "socket_path": a["vm_dir"].expanduser().resolve() / "vphone.sock",
})
def _execute_vm(command: list[str], vm_dir: Path) -> None:
    # Replace the wrapper so SIGINT reaches the upstream lifecycle process.
    if command[1:3] == ["vm", "launch"]:
        with launch_lock(vm_dir):
            os.execv(command[0], command)
    else:
        os.execv(command[0], command)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "launch", "stop"))
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--vm-dir", type=Path)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        command = lifecycle_command(
            args.action, bundle=args.bundle, vm_dir=args.vm_dir,
            headless=args.headless, timeout=args.timeout,
        )
        if args.dry_run:
            print(json.dumps(command, ensure_ascii=False))
        elif args.action == "preflight":
            os.execv(command[0], command)
        else:
            _execute_vm(command, args.vm_dir)
    except (UpstreamConfigurationError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
