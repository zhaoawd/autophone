"""Resolve a running app pid through Frida when guest app_list is stale."""

from __future__ import annotations

import logging
from typing import Any, Iterable
from autophone.device.frida_endpoint import resolve_frida_host
from autophone.device.stage_guard import StageGuard, OperationClass

logger = logging.getLogger(__name__)


def default_frida_host() -> str:
    return resolve_frida_host()


def _bundle_aliases(bundle_id: str) -> tuple[str, ...]:
    """Process-name aliases for bundles whose Frida process name differs from the id.

    No bundle needs an alias in the public build; callers pass explicit
    ``process_aliases`` when a target App's process name is known.
    """
    del bundle_id
    return ()


def _int_pid(value: Any) -> int:
    try:
        pid = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return pid if pid > 0 else 0


def pid_via_frida(
    bundle_id: str,
    *,
    frida_host: str | None = None,
    process_aliases: Iterable[str] | None = None,
) -> int:
    """Return the current pid for ``bundle_id`` using Frida's process view.

    ``enumerate_applications`` gives the precise bundle-id mapping when the
    frida-server side supports it. Process-name aliases are a narrow fallback
    for Apps whose process name differs from the bundle id.
    Failure to query Frida is non-fatal: callers can keep their existing
    fail-closed behavior when this returns 0.
    """
    host = frida_host or default_frida_host()
    with StageGuard.operation_if_bound(OperationClass.FRIDA, frida_endpoint=host):
        return _pid_via_frida(bundle_id, host, process_aliases)


def _pid_via_frida(bundle_id: str, host: str, process_aliases: Iterable[str] | None) -> int:
    try:
        import frida  # type: ignore
    except ImportError as exc:
        logger.debug("frida pid fallback unavailable: %s", exc)
        return 0

    try:
        device = frida.get_device_manager().add_remote_device(host)
    except Exception as exc:  # noqa: BLE001
        logger.debug("frida pid fallback cannot connect to %s: %s", host, exc)
        return 0

    try:
        for app in device.enumerate_applications():
            if getattr(app, "identifier", None) == bundle_id:
                pid = _int_pid(getattr(app, "pid", None))
                if pid:
                    return pid
    except Exception as exc:  # noqa: BLE001
        logger.debug("frida enumerate_applications failed: %s", exc)

    aliases = tuple(process_aliases or _bundle_aliases(bundle_id))
    if not aliases:
        return 0
    try:
        for proc in device.enumerate_processes():
            name = str(getattr(proc, "name", "") or "")
            if any(alias == name or alias in name for alias in aliases):
                pid = _int_pid(getattr(proc, "pid", None))
                if pid:
                    return pid
    except Exception as exc:  # noqa: BLE001
        logger.debug("frida enumerate_processes failed: %s", exc)
    return 0
