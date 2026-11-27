"""Policy gate for release-managed asset mutations.

Installed release trees are immutable when ``AUTOPHONE_READONLY_ASSETS`` is
enabled.  Mutable host state (runs, packages, dispositions and local config)
does not use this gate.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


ENV_READONLY_ASSETS = "AUTOPHONE_READONLY_ASSETS"
READONLY_ASSETS_CODE = "readonly_assets"

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})


class ReadonlyAssetsError(RuntimeError):
    """A release-managed asset mutation was rejected by policy."""

    code = READONLY_ASSETS_CODE

    def __init__(self, operation: str, *, path: str | Path | None = None) -> None:
        self.operation = str(operation or "write")
        self.path = Path(path) if path is not None else None
        target = f": {self.path}" if self.path is not None else ""
        super().__init__(
            f"{self.code}: {self.operation} is disabled by {ENV_READONLY_ASSETS}{target}"
        )

    def as_detail(self) -> dict[str, str]:
        detail = {
            "code": self.code,
            "message": str(self),
            "operation": self.operation,
        }
        if self.path is not None:
            detail["path"] = str(self.path)
        return detail


def readonly_assets_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether release-managed assets are read-only.

    Empty/unset and the conventional false spellings are disabled.  Unknown
    non-empty values are treated as enabled so a malformed deployment setting
    cannot silently reopen asset writes.
    """

    env = os.environ if environ is None else environ
    raw = str(env.get(ENV_READONLY_ASSETS) or "").strip().lower()
    if raw in _FALSE_VALUES:
        return False
    if raw in _TRUE_VALUES:
        return True
    return True


def assert_assets_writable(
    operation: str = "write release asset",
    *,
    path: str | Path | None = None,
) -> None:
    """Reject one A-class asset mutation when the deployment is read-only."""

    if readonly_assets_enabled():
        raise ReadonlyAssetsError(operation, path=path)


__all__ = [
    "ENV_READONLY_ASSETS",
    "READONLY_ASSETS_CODE",
    "ReadonlyAssetsError",
    "assert_assets_writable",
    "readonly_assets_enabled",
]
