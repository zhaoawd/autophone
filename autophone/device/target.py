"""Resolve the single vphone VM target used by host-side automation.

Legacy checkouts default to ``vm`` under the CLI directory. With upstream_v1
or an explicit library root, the default is ``vm-1`` in the VM library: ``VPHONE_LIBRARY_ROOT``, else
``~/.vphone/machines``.  Another image is selected with ``VM_DIR=<name>``, which
resolves in the same library, or ``VM_DIR=<absolute path>``; socket and logical
device selection then follow that same target.  Every VM directory, including ``rig-b``, uses its
directory name as the logical device key.  Low-level callers may still resolve
``VPHONE_SOCK`` and ``AP_DEVICE`` independently.  Workflows that acquire a
ScreenLock must use ``resolve_screen_lock_target`` to bind both values.
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping


CURRENT_VM_DIR = "vm"
CURRENT_DEVICE = "vm"
_SCREEN_LOCK_DEVICE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


@dataclass(frozen=True)
class VPhoneTarget:
    cli_dir: Path
    vm_dir: Path
    sock: Path
    device: str
    protocol: str = "legacy"

    def __post_init__(self) -> None:
        if self.protocol not in {"legacy", "upstream_v1"}:
            raise VPhoneTargetBindingError(f"unsupported vphone protocol: {self.protocol}")

    def public_dict(self) -> dict[str, str]:
        """Dashboard-safe identity for the selected VM target.

        ``device`` is the logical ScreenLock/queue key and is intentionally
        allowed to differ from the VM directory name (for example via
        ``AP_DEVICE``).  Keeping both visible prevents a device key from being
        mistaken for a VM image.
        """
        return {
            "device": self.device,
            "vm": self.vm_dir.name,
            "vm_dir": str(self.vm_dir),
            "socket": str(self.sock),
        }


class VPhoneTargetBindingError(ValueError):
    """Socket 与 ScreenLock 设备键无法形成明确绑定。"""


_BOUND_TARGET: ContextVar[VPhoneTarget | None] = ContextVar(
    "autophone_bound_vphone_target",
    default=None,
)
_DEVICE_WRITE_GUARD: ContextVar[Callable[[], None] | None] = ContextVar(
    "autophone_device_write_guard",
    default=None,
)


@contextmanager
def target_scope(target: VPhoneTarget) -> Iterator[VPhoneTarget]:
    """Bind one explicit target for legacy calls inside an Agent execution."""
    token = _BOUND_TARGET.set(target)
    try:
        yield target
    finally:
        _BOUND_TARGET.reset(token)


@contextmanager
def device_write_scope(guard: Callable[[], None]) -> Iterator[None]:
    """Bind the Assignment fence to legacy low-level device calls."""
    token = _DEVICE_WRITE_GUARD.set(guard)
    try:
        yield
    finally:
        _DEVICE_WRITE_GUARD.reset(token)


def check_device_write(operation: str) -> None:
    """Check the active Lease before a legacy device-mutating operation."""
    guard = _DEVICE_WRITE_GUARD.get()
    if guard is not None:
        guard()


def _safe_is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _default_cli_dir() -> Path:
    sibling = Path(__file__).resolve().parents[3] / "vphone-cli"
    if _safe_is_dir(sibling):
        return sibling
    home = Path.home()
    for candidate in (home / "github/vphone-cli", home / "Documents/github/vphone-cli"):
        if _safe_is_dir(candidate):
            return candidate
    return sibling


def _vm_library_root(env: Mapping[str, str]) -> Path:
    configured = env.get("VPHONE_LIBRARY_ROOT", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".vphone" / "machines"


def resolve_target(
    *,
    environ: Mapping[str, str] | None = None,
    cli_dir: Path | str | None = None,
) -> VPhoneTarget:
    bound = _BOUND_TARGET.get()
    if bound is not None and environ is None and cli_dir is None:
        return bound
    env = os.environ if environ is None else environ
    root = Path(cli_dir or env.get("VPHONE_CLI_DIR") or _default_cli_dir()).expanduser()

    library_layout = bool(env.get("VPHONE_LIBRARY_ROOT")) or env.get("AUTOPHONE_VPHONE_PROTOCOL") == "upstream_v1"
    vm_root = _vm_library_root(env) if library_layout else root
    configured_vm = env.get("VM_DIR", "").strip()
    if configured_vm:
        vm_dir = Path(configured_vm).expanduser()
        if not vm_dir.is_absolute():
            vm_dir = vm_root / vm_dir
    else:
        vm_dir = vm_root / ("vm-1" if library_layout else CURRENT_VM_DIR)

    configured_sock = env.get("VPHONE_SOCK", "").strip()
    sock = Path(configured_sock).expanduser() if configured_sock else vm_dir / "vphone.sock"

    configured_device = env.get("AP_DEVICE", "").strip()
    if configured_device:
        device = configured_device
    else:
        device = vm_dir.name or CURRENT_DEVICE

    return VPhoneTarget(cli_dir=root, vm_dir=vm_dir, sock=sock, device=device,
                        protocol=env.get("AUTOPHONE_VPHONE_PROTOCOL", "legacy"))


def resolve_screen_lock_target(
    *,
    sock: Path | str | None = None,
    device: str | None = None,
    environ: Mapping[str, str] | None = None,
    cli_dir: Path | str | None = None,
) -> VPhoneTarget:
    """解析碰屏工作流的 socket 与 ScreenLock 设备键。

    自定义 socket 必须同时提供 ``AP_DEVICE`` 或 ``device``。设备键只能是安全的
    单个文件名片段；返回的 socket 是规范化绝对路径。
    """
    env = os.environ if environ is None else environ
    baseline_env = dict(env)
    baseline_env.pop("VPHONE_SOCK", None)
    baseline = resolve_target(environ=baseline_env, cli_dir=cli_dir)

    configured_sock = sock
    if configured_sock is None:
        configured_sock = str(env.get("VPHONE_SOCK") or "").strip() or baseline.sock
    resolved_sock = Path(configured_sock).expanduser().resolve()
    baseline_sock = baseline.sock.expanduser().resolve()

    if device is not None and not str(device).strip():
        raise VPhoneTargetBindingError("device must be non-blank")
    configured_device = device
    if configured_device is None and "AP_DEVICE" in env:
        configured_device = str(env.get("AP_DEVICE") or "")
        if not configured_device.strip():
            raise VPhoneTargetBindingError("AP_DEVICE must be non-blank")
    if resolved_sock != baseline_sock and configured_device is None:
        raise VPhoneTargetBindingError(
            "a custom VPHONE_SOCK requires an explicit device via AP_DEVICE or --device",
        )
    device_key = str(configured_device or baseline.device)
    if _SCREEN_LOCK_DEVICE_RE.fullmatch(device_key) is None:
        raise VPhoneTargetBindingError(
            "device must be a safe ScreenLock key using only ASCII letters, digits, '.', '_', or '-'",
        )
    if (
        resolved_sock == baseline_sock
        and device is not None
        and device_key != baseline.device
    ):
        raise VPhoneTargetBindingError(
            "--device must match the resolved device for the default socket",
        )

    return VPhoneTarget(
        cli_dir=baseline.cli_dir,
        vm_dir=baseline.vm_dir,
        sock=resolved_sock,
        device=device_key,
        protocol=baseline.protocol,
    )


def default_device() -> str:
    return resolve_target().device


def resolve_protocol(sock: str | Path, explicit: str | None = None) -> str:
    target = _BOUND_TARGET.get()
    if target is not None:
        if Path(sock).expanduser().resolve() != target.sock.expanduser().resolve():
            raise VPhoneTargetBindingError("protocol target socket mismatch")
        if explicit is not None and explicit != target.protocol:
            raise VPhoneTargetBindingError("protocol conflicts with bound target")
        mode = target.protocol
    elif explicit is not None:
        mode = explicit
    else:
        mode = os.environ.get("AUTOPHONE_VPHONE_PROTOCOL", "legacy")
        if mode != "legacy" and Path(sock).expanduser().resolve() != resolve_target().sock.expanduser().resolve():
            raise VPhoneTargetBindingError("upstream protocol requires an explicit socket binding")
    if mode not in {"legacy", "upstream_v1"}:
        raise VPhoneTargetBindingError(f"unsupported vphone protocol: {mode}")
    return mode
