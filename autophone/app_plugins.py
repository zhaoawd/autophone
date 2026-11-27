"""Load explicitly registered App capabilities only when they are requested."""
from __future__ import annotations

import importlib
import re
from threading import RLock
from typing import Any, Mapping


class AppPluginUnavailable(RuntimeError):
    """The selected App has no installed provider for a requested capability."""


_CAPABILITIES: dict[str, dict[str, Any]] = {}
_LOCK = RLock()


def register_app_plugin(app: str, capabilities: Mapping[str, Any]) -> None:
    """Register one App's providers; conflicting registrations are refused."""
    with _LOCK:
        if app in _CAPABILITIES:
            raise ValueError(f"App plugin already registered: {app}")
        _CAPABILITIES[app] = dict(capabilities)


def app_capability(app: str, capability: str) -> Any:
    """Resolve a capability, importing only the selected App's plugin on demand."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", app):
        raise ValueError(f"Invalid App identifier: {app!r}")
    with _LOCK:
        registered = app in _CAPABILITIES
    if not registered:
        module_name = f"apps.{app}.plugin"
        # importlib serializes module initialization. Do not hold our registry
        # lock while taking its module lock: a direct plugin import may already
        # own that lock and be waiting to register its providers.
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name not in {"apps", f"apps.{app}", module_name}:
                raise
            raise AppPluginUnavailable(f"App plugin is not installed: {app}") from exc
    with _LOCK:
        try:
            return _CAPABILITIES[app][capability]
        except KeyError as exc:
            raise AppPluginUnavailable(f"App {app} did not register capability {capability}") from exc
