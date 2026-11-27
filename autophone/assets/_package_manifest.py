"""Pure-kernel read-only Package Store manifest reader (v1 & v2).

Allows ``autophone-kernel`` asset modules (such as ``app_symbols`` and
``symbol_tools``) to resolve immutable Build Records and ``bundle_id`` metadata
from ``packages/<app>/manifest.json`` without importing ``autophone-domain``
(``autophone.packages.*``).

Mirrors the read-only resolution semantics of ``autophone.packages.model`` and
``autophone.packages.store`` fail-closed:
- Validates ``app`` slug and guards against path traversal.
- Normalizes legacy ``active_slot`` -> ``default_slot`` on read (never writes).
- Validates against ``package-manifest.v1.json`` or ``package-manifest.v2.json``.
- Projects v1 manifests with the exact canonical ``compute_build_id`` digest.
- Resolves explicit ``build_id``, ``default_slot``, or latest non-archived slot.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from autophone.assets.apps import load_app_profile
from autophone.contracts import ContractError, validate_or_raise
from autophone.paths import APP_ASSET_ROOT, STATE_ROOT
from autophone.util import read_json

PACKAGES_DIRNAME = "packages"
BUILD_ID_PREFIX = "bld-"
_APP_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class PackageManifestError(ValueError):
    """Fail-closed error reading or resolving a package store manifest."""


@dataclass(frozen=True)
class PackageBuildInfo:
    """Resolved read-only build identity from ``packages/<app>/manifest.json``."""

    app: str
    slot: str
    build_id: str
    artifact_sha256: str
    short_version: str | None
    build_version: str | None
    bundle_id: str | None
    resolution_source: str


@dataclass(frozen=True)
class _SlotView:
    slot: str
    short_version: str | None
    candidate_build_id: str | None
    archived: bool = False


@dataclass(frozen=True)
class _BuildView:
    build_id: str
    slot: str
    bundle_id: str | None
    build_version: str | None
    sha256: str


@dataclass(frozen=True)
class _ManifestView:
    app: str
    bundle_id: str | None
    version_slots: tuple[_SlotView, ...]
    builds: tuple[_BuildView, ...]
    legacy_slot_aliases: Mapping[str, str]
    default_slot: str | None

    def slot(self, slot: str) -> _SlotView | None:
        return next((s for s in self.version_slots if s.slot == str(slot)), None)

    def build(self, build_id: str) -> _BuildView | None:
        return next((b for b in self.builds if b.build_id == str(build_id)), None)

    def short_version_of(self, build: _BuildView) -> str | None:
        slot = self.slot(build.slot)
        return slot.short_version if slot else None

    @property
    def latest_slot(self) -> _SlotView | None:
        active = [s for s in self.version_slots if not s.archived]
        return max(active, key=lambda s: s.slot) if active else None


def _checked_app_id(app: str) -> str:
    app_str = str(app or "")
    if _APP_SLUG_RE.fullmatch(app_str) is None:
        raise PackageManifestError(
            f"invalid app id {app_str!r}: must match {_APP_SLUG_RE.pattern}"
        )
    return app_str


def _manifest_path(app: str, *, root: Path) -> Path:
    app_str = _checked_app_id(app)
    packages_root = Path(root) / PACKAGES_DIRNAME
    target = packages_root / app_str
    resolved_root = packages_root.resolve(strict=False)
    resolved_target = target.resolve(strict=False)
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise PackageManifestError(
            f"package store for app {app_str!r} escapes packages root: {resolved_target}"
        ) from exc
    return target / "manifest.json"


def _profile_bundle_id(
    app: str, *, root: Path, code_root: Path | None,
) -> str | None:
    try:
        profile_root = code_root or (APP_ASSET_ROOT if root == STATE_ROOT else root)
        return load_app_profile(app, root=profile_root).bundle_id
    except Exception:  # noqa: BLE001
        return None


def _compute_v1_build_id(
    *,
    app: str,
    slot: str,
    short_version: str | None,
    artifact_sha256: str,
) -> str:
    payload = {
        "app": str(app),
        "slot": str(slot),
        "short_version": short_version,
        "build_version": None,
        "source_revision": {"kind": "unknown", "state": "unknown"},
        "artifact_sha256": str(artifact_sha256),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return BUILD_ID_PREFIX + digest


def _load_manifest_view(
    app: str,
    *,
    root: Path = STATE_ROOT,
    code_root: Path | None = None,
) -> _ManifestView:
    path = _manifest_path(app, root=root)
    try:
        raw = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageManifestError(f"failed to read package manifest {path}: {exc}") from exc

    profile_bundle = _profile_bundle_id(app, root=root, code_root=code_root)
    if raw is None:
        return _ManifestView(
            app=app,
            bundle_id=profile_bundle,
            version_slots=(),
            builds=(),
            legacy_slot_aliases={},
            default_slot=None,
        )
    if not isinstance(raw, dict):
        raise PackageManifestError(f"package manifest {path} must be a JSON object")

    if "default_slot" not in raw and "active_slot" in raw:
        raw = {**raw, "default_slot": raw.get("active_slot")}
        raw.pop("active_slot", None)

    schema = str(raw.get("schema_version") or "v1")
    if schema not in ("v1", "v2"):
        raise PackageManifestError(f"unsupported package manifest schema_version {schema!r}")
    try:
        validate_or_raise(f"package-manifest.{schema}.json", raw)
    except ContractError as exc:
        raise PackageManifestError(f"invalid package manifest {path}: {exc}") from exc

    bundle_id = profile_bundle or (str(raw["bundle_id"]) if raw.get("bundle_id") else None)
    app_id = str(raw.get("app") or app or "")

    if schema == "v1":
        slots: list[_SlotView] = []
        builds: list[_BuildView] = []
        aliases: dict[str, str] = {}
        for item in raw.get("packages", []):
            slot = str(item["slot"])
            short_version = item.get("version")
            sha256 = str(item["sha256"])
            build_id = _compute_v1_build_id(
                app=app_id,
                slot=slot,
                short_version=short_version,
                artifact_sha256=sha256,
            )
            builds.append(_BuildView(
                build_id=build_id,
                slot=slot,
                bundle_id=item.get("bundle_id"),
                build_version=None,
                sha256=sha256,
            ))
            slots.append(_SlotView(
                slot=slot,
                short_version=short_version,
                candidate_build_id=build_id,
                archived=False,
            ))
            aliases[slot] = build_id
        return _ManifestView(
            app=app_id,
            bundle_id=bundle_id,
            version_slots=tuple(sorted(slots, key=lambda s: s.slot)),
            builds=tuple(sorted(builds, key=lambda b: b.slot)),
            legacy_slot_aliases=aliases,
            default_slot=raw.get("default_slot") or None,
        )

    v2_slots = tuple(
        _SlotView(
            slot=str(v["slot"]),
            short_version=v.get("short_version"),
            candidate_build_id=v.get("candidate_build_id"),
            archived=bool(v.get("archived", False)),
        )
        for v in raw.get("version_slots", [])
    )
    v2_builds = tuple(
        _BuildView(
            build_id=str(b["build_id"]),
            slot=str(b["slot"]),
            bundle_id=b.get("bundle_id"),
            build_version=b.get("build_version"),
            sha256=str((b.get("artifact") or {})["sha256"]),
        )
        for b in raw.get("builds", [])
    )
    return _ManifestView(
        app=app_id,
        bundle_id=bundle_id,
        version_slots=v2_slots,
        builds=v2_builds,
        legacy_slot_aliases=dict(raw.get("legacy_slot_aliases") or {}),
        default_slot=raw.get("default_slot") or None,
    )


def _resolve_slot_build(view: _ManifestView, slot_key: str) -> _BuildView:
    version_slot = view.slot(slot_key)
    if version_slot is None:
        alias = view.legacy_slot_aliases.get(slot_key)
        build = view.build(alias) if alias else None
        if build is not None:
            return build
        raise PackageManifestError(f"app {view.app!r} has no version slot {slot_key!r}")
    build = view.build(version_slot.candidate_build_id or "")
    if build is None:
        raise PackageManifestError(f"version slot {slot_key!r} has no candidate build")
    return build


def resolve_package_manifest_build(
    app: str,
    *,
    build_id: str | None = None,
    root: Path = STATE_ROOT,
    code_root: Path | None = None,
) -> PackageBuildInfo:
    """Resolve one Build from ``packages/<app>/manifest.json`` fail-closed."""
    view = _load_manifest_view(app, root=root, code_root=code_root)

    if build_id is not None:
        build = view.build(build_id)
        if build is None:
            raise PackageManifestError(f"app {view.app!r} has no build {build_id!r}")
        source = "build_id"
    elif view.default_slot:
        build = _resolve_slot_build(view, str(view.default_slot))
        source = "default_slot"
    else:
        latest = view.latest_slot
        if latest is None:
            raise PackageManifestError(f"app {view.app!r} has no packages; add one first")
        build = view.build(latest.candidate_build_id or "")
        if build is None:
            raise PackageManifestError(f"version slot {latest.slot!r} has no candidate build")
        source = "candidate"

    resolved_bundle = (
        str(build.bundle_id)
        if build.bundle_id
        else (str(view.bundle_id) if view.bundle_id else None)
    )
    return PackageBuildInfo(
        app=view.app,
        slot=build.slot,
        build_id=build.build_id,
        artifact_sha256=build.sha256,
        short_version=view.short_version_of(build),
        build_version=build.build_version,
        bundle_id=resolved_bundle,
        resolution_source=source,
    )


def lookup_package_manifest_bundle_id(
    app: str,
    build_id: str,
    *,
    root: Path = STATE_ROOT,
    code_root: Path | None = None,
) -> str | None:
    """Best-effort bundle_id lookup from a build record or manifest root."""
    try:
        view = _load_manifest_view(app, root=root, code_root=code_root)
    except PackageManifestError:
        return None
    record = view.build(build_id)
    if record is not None and record.bundle_id:
        return str(record.bundle_id)
    return str(view.bundle_id) if view.bundle_id else None


__all__ = [
    "PackageBuildInfo",
    "PackageManifestError",
    "lookup_package_manifest_bundle_id",
    "resolve_package_manifest_build",
]
