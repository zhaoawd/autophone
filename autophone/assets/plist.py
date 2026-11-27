"""Public plist parsing adapter shared by package and device identity probes."""
from __future__ import annotations

import plistlib
from typing import Any, Mapping


class PlistAdapterError(ValueError):
    """The supplied plist bytes do not contain a dictionary root."""


def parse_plist(raw: bytes) -> dict[str, Any]:
    try:
        value = plistlib.loads(raw)
    except Exception as exc:  # noqa: BLE001 - normalize parser failures at the seam
        raise PlistAdapterError(f"invalid plist: {exc}") from exc
    if not isinstance(value, dict):
        raise PlistAdapterError("plist root must be a dictionary")
    return value


def bundle_identity(plist: Mapping[str, Any]) -> dict[str, str | None]:
    """Return the complete version tuple without inventing missing fields."""

    def text(key: str) -> str | None:
        value = plist.get(key)
        return str(value) if value not in (None, "") else None

    return {
        "bundle_id": text("CFBundleIdentifier"),
        "short_version": text("CFBundleShortVersionString"),
        "build_version": text("CFBundleVersion"),
        "executable": text("CFBundleExecutable"),
    }
