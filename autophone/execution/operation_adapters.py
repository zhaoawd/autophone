"""App-owned operation bindings; no default App or cross-App fallback."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from autophone.app_plugins import AppPluginUnavailable, app_capability

operation_runtime: ContextVar[Mapping[str, Any] | None] = ContextVar("autophone_operation_runtime", default=None)


@dataclass(frozen=True)
class DevicePreparation:
    unlock: bool
    launch: bool

    def to_dict(self) -> dict[str, bool]:
        return {"unlock": self.unlock, "launch": self.launch}


@dataclass(frozen=True)
class OperationBinding:
    executor: Callable[[Path, dict[str, Any]], dict[str, Any]] | None = None
    device: DevicePreparation | None = None
    sensitive_args: frozenset[str] = frozenset()


def operation_binding(app: str | None, name: str) -> OperationBinding | None:
    if not app:
        return None
    try:
        provider = app_capability(app, "operation_bindings")
    except AppPluginUnavailable:
        return None
    binding = provider().get(name)
    if binding is not None and not isinstance(binding, OperationBinding):
        raise TypeError(f"invalid operation binding: {app}/{name}")
    return binding
