"""package_model.py — Version Slot / Build Record read model for the package store.

Design source: ``docs/package-store-version-slot-build-trace.md``.

``packages.py`` owns bytes, locks and the device install pipeline.  This module
owns the *identity* model layered on top of it:

  Version Slot   one App version (``CFBundleShortVersionString``), stable order key
  Build Record   one immutable (source revision, IPA artifact) relationship
  IPA Blob       content-addressed bytes, shared between Build Records
  RegistryBuildRef  the frozen selection that installs, runs and gates must carry

A v1 manifest is *projected*, never rewritten: each historical slot becomes a
single-build Version Slot so existing ``slot + sha256`` references still resolve
to exactly one Build.  Grouping several builds of one version under one Version
Slot is the separate, explicit migration in ``package_migration.py``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

BUILD_ID_PREFIX = "bld-"
_BUILD_ID = re.compile(r"^bld-[0-9a-f]{12,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHORT_ID_CHARS = 12

# Selector forms, in the order resolve() tries them.  Exact forms are the only
# ones automation may use; ``candidate`` and ``legacy_alias`` exist for humans
# and for compatibility call sites, and are reported so they can be counted.
RESOLUTION_SOURCES = (
    "build_id",
    "slot_build_id",
    "slot_sha256",
    "candidate",
    "installed",
    "legacy_alias",
)
EXACT_RESOLUTION_SOURCES = ("build_id", "slot_build_id", "slot_sha256")


class PackageModelError(ValueError):
    """Malformed manifest, unusable selector, or an ambiguous resolution."""


# --------------------------------------------------------------------------- #
# Source revision
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SourceRevision:
    """Product source state a Build was produced from.

    ``unknown`` is a first-class value, not a hole to be filled in later by
    guesswork: managed delivery refuses to consume it (see ``is_managed``).
    """

    kind: str
    state: str
    commit_sha: str | None = None
    base_commit_sha: str | None = None
    tree_sha256: str | None = None

    @classmethod
    def git_commit(cls, commit_sha: str) -> "SourceRevision":
        return cls(kind="git_commit", state="clean", commit_sha=_checked_commit(commit_sha))

    @classmethod
    def source_tree(
        cls,
        tree_sha256: str,
        *,
        base_commit_sha: str | None = None,
        state: str = "dirty",
    ) -> "SourceRevision":
        if state not in ("dirty", "generated"):
            raise PackageModelError(f"source_tree state must be dirty|generated, got {state!r}")
        if not _SHA256.fullmatch(str(tree_sha256 or "")):
            raise PackageModelError(f"tree_sha256 must be 64 hex chars, got {tree_sha256!r}")
        return cls(
            kind="source_tree",
            state=state,
            tree_sha256=str(tree_sha256),
            base_commit_sha=_checked_commit(base_commit_sha) if base_commit_sha else None,
        )

    @classmethod
    def unknown(cls) -> "SourceRevision":
        return cls(kind="unknown", state="unknown")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "SourceRevision":
        if not raw:
            return cls.unknown()
        kind = str(raw.get("kind") or "unknown")
        if kind == "git_commit":
            return cls.git_commit(str(raw.get("commit_sha") or ""))
        if kind == "source_tree":
            return cls.source_tree(
                str(raw.get("tree_sha256") or ""),
                base_commit_sha=raw.get("base_commit_sha") or None,
                state=str(raw.get("state") or "dirty"),
            )
        if kind == "unknown":
            return cls.unknown()
        raise PackageModelError(f"unknown source revision kind {kind!r}")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind, "state": self.state}
        if self.kind == "git_commit":
            payload["commit_sha"] = self.commit_sha
        elif self.kind == "source_tree":
            payload["tree_sha256"] = self.tree_sha256
            if self.base_commit_sha:
                payload["base_commit_sha"] = self.base_commit_sha
        return payload

    @property
    def is_managed(self) -> bool:
        """Whether managed delivery may consume builds from this revision."""
        return self.kind in ("git_commit", "source_tree")

    @property
    def identity(self) -> str:
        """Short human-facing source label; never invented when unknown."""
        if self.kind == "git_commit":
            return str(self.commit_sha or "")[:12]
        if self.kind == "source_tree":
            return f"tree:{str(self.tree_sha256 or '')[:12]}"
        return "unknown"


def _checked_commit(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", text):
        raise PackageModelError(
            f"commit sha must be a full 40-hex git commit, got {value!r}"
        )
    return text


# --------------------------------------------------------------------------- #
# Build id
# --------------------------------------------------------------------------- #

def compute_build_id(
    *,
    app: str,
    slot: str,
    short_version: str | None,
    build_version: str | None,
    source_revision: SourceRevision,
    artifact_sha256: str,
) -> str:
    """Opaque, stable Build identity.

    Display fields (label, ``added_at``) and storage location are deliberately
    excluded so renaming a file or relabelling a build never mints a new
    identity.
    """
    payload = {
        "app": str(app),
        "slot": str(slot),
        "short_version": short_version,
        "build_version": build_version,
        "source_revision": source_revision.to_dict(),
        "artifact_sha256": str(artifact_sha256),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return BUILD_ID_PREFIX + digest


def short_build_id(build_id: str) -> str:
    body = str(build_id or "")[len(BUILD_ID_PREFIX):]
    return BUILD_ID_PREFIX + body[:_SHORT_ID_CHARS]


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class BuildRecord:
    build_id: str
    slot: str
    label: str | None
    bundle_id: str | None
    build_version: str | None
    source_revision: SourceRevision
    file: str
    sha256: str
    size: int
    release_like: bool
    release_evidence: str | None
    added_at: str
    legacy_slot: str | None = None
    artifact_availability: str = "present"
    artifact_store_id: str = "central"
    retention_class: str = "unspecified"

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BuildRecord":
        artifact = raw.get("artifact") or {}
        return cls(
            build_id=str(raw["build_id"]),
            slot=str(raw["slot"]),
            label=raw.get("label"),
            bundle_id=raw.get("bundle_id"),
            build_version=raw.get("build_version"),
            source_revision=SourceRevision.from_dict(raw.get("source_revision")),
            file=str(artifact["file"]),
            sha256=str(artifact["sha256"]),
            size=int(artifact["size"]),
            release_like=bool(raw.get("release_like", False)),
            release_evidence=raw.get("release_evidence"),
            added_at=str(raw["added_at"]),
            legacy_slot=raw.get("legacy_slot"),
            artifact_availability=str(raw.get("artifact_availability") or "present"),
            artifact_store_id=str(artifact.get("store_id") or "central"),
            retention_class=str(raw.get("retention_class") or "unspecified"),
        )

    def to_dict(self) -> dict[str, Any]:
        artifact = {"file": self.file, "sha256": self.sha256, "size": self.size}
        if self.artifact_store_id != "central":
            artifact["store_id"] = self.artifact_store_id
        payload = {
            "build_id": self.build_id,
            "slot": self.slot,
            "label": self.label,
            "bundle_id": self.bundle_id,
            "build_version": self.build_version,
            "source_revision": self.source_revision.to_dict(),
            "artifact": artifact,
            "artifact_availability": self.artifact_availability,
            "release_like": self.release_like,
            "release_evidence": self.release_evidence,
            "legacy_slot": self.legacy_slot,
            "added_at": self.added_at,
        }
        if self.retention_class != "unspecified":
            payload["retention_class"] = self.retention_class
        return payload


@dataclass(frozen=True)
class VersionSlot:
    slot: str
    short_version: str | None
    candidate_build_id: str | None
    created_at: str
    archived: bool = False

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "VersionSlot":
        return cls(
            slot=str(raw["slot"]),
            short_version=raw.get("short_version"),
            candidate_build_id=raw.get("candidate_build_id"),
            created_at=str(raw["created_at"]),
            archived=bool(raw.get("archived", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "slot": self.slot,
            "short_version": self.short_version,
            "candidate_build_id": self.candidate_build_id,
            "created_at": self.created_at,
        }
        if self.archived:
            payload["archived"] = True
        return payload


@dataclass(frozen=True)
class RegistryBuildRef:
    """The frozen Host selection carried by installs, runs, iterations and gates."""

    app: str
    slot: str
    short_version: str | None
    build_id: str
    build_version: str | None
    source_revision: SourceRevision
    artifact_sha256: str
    resolution_source: str | None = None

    @classmethod
    def from_build(
        cls, app: str, build: BuildRecord, short_version: str | None,
        *, resolution_source: str | None = None,
    ) -> "RegistryBuildRef":
        return cls(
            app=str(app),
            slot=build.slot,
            short_version=short_version,
            build_id=build.build_id,
            build_version=build.build_version,
            source_revision=build.source_revision,
            artifact_sha256=build.sha256,
            resolution_source=resolution_source,
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RegistryBuildRef":
        return cls(
            app=str(raw["app"]),
            slot=str(raw["slot"]),
            short_version=raw.get("short_version"),
            build_id=str(raw["build_id"]),
            build_version=raw.get("build_version"),
            source_revision=SourceRevision.from_dict(raw.get("source_revision")),
            artifact_sha256=str(raw["artifact_sha256"]),
            resolution_source=raw.get("resolution_source"),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "app": self.app,
            "slot": self.slot,
            "short_version": self.short_version,
            "build_id": self.build_id,
            "build_version": self.build_version,
            "source_revision": self.source_revision.to_dict(),
            "artifact_sha256": self.artifact_sha256,
        }
        if self.resolution_source:
            payload["resolution_source"] = self.resolution_source
        return payload

    @property
    def is_exact(self) -> bool:
        return self.resolution_source in EXACT_RESOLUTION_SOURCES

    @property
    def summary(self) -> str:
        """``v<short_version> · <source identity>`` — the page's main line."""
        version = self.short_version or "unknown"
        return f"v{version} · {self.source_revision.identity}"


# --------------------------------------------------------------------------- #
# Selector
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class BuildSelector:
    """How a caller names a Build.

    ``legacy`` marks a v1-era reference: ``slot`` then means "the historical
    slot", resolved through the alias table rather than as a Version Slot.
    """

    build_id: str | None = None
    slot: str | None = None
    artifact_sha256: str | None = None
    legacy: bool = False

    @property
    def is_empty(self) -> bool:
        return not (self.build_id or self.slot or self.artifact_sha256)


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PackageManifest:
    """In-memory v2 view; the on-disk document may still be v1."""

    app: str
    bundle_id: str | None
    version_slots: tuple[VersionSlot, ...] = ()
    builds: tuple[BuildRecord, ...] = ()
    legacy_slot_aliases: Mapping[str, str] = field(default_factory=dict)
    source_schema_version: str = "v2"
    # Default *selection* when a caller names no Build.  Carried on the model
    # so a registry round-trip cannot silently drop it; it says nothing about
    # what is installed on any device.
    default_slot: str | None = None

    # -- construction ------------------------------------------------------ #

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], *, app: str | None = None) -> "PackageManifest":
        version = str(raw.get("schema_version") or "")
        if version == "v1":
            return cls.from_v1(raw, app=app)
        if version != "v2":
            raise PackageModelError(f"unsupported package manifest schema_version {version!r}")
        return cls(
            app=str(raw.get("app") or app or ""),
            bundle_id=raw.get("bundle_id"),
            version_slots=tuple(VersionSlot.from_dict(v) for v in raw.get("version_slots", [])),
            builds=tuple(BuildRecord.from_dict(b) for b in raw.get("builds", [])),
            legacy_slot_aliases=dict(raw.get("legacy_slot_aliases") or {}),
            source_schema_version="v2",
            default_slot=raw.get("default_slot") or None,
        )

    @classmethod
    def from_v1(cls, raw: Mapping[str, Any], *, app: str | None = None) -> "PackageManifest":
        """Project a v1 manifest without regrouping slots.

        One historical slot stays one Version Slot holding one Build, so every
        ``legacy slot + sha256`` reference keeps resolving to exactly one Build
        and no existing evidence changes meaning.
        """
        app_id = str(raw.get("app") or app or "")
        slots: list[VersionSlot] = []
        builds: list[BuildRecord] = []
        aliases: dict[str, str] = {}
        for item in raw.get("packages", []):
            slot = str(item["slot"])
            short_version = item.get("version")
            revision = SourceRevision.unknown()
            build_id = compute_build_id(
                app=app_id,
                slot=slot,
                short_version=short_version,
                build_version=None,
                source_revision=revision,
                artifact_sha256=str(item["sha256"]),
            )
            builds.append(BuildRecord(
                build_id=build_id,
                slot=slot,
                label=item.get("label"),
                bundle_id=item.get("bundle_id"),
                build_version=None,
                source_revision=revision,
                file=str(item["file"]),
                sha256=str(item["sha256"]),
                size=int(item["size"]),
                release_like=bool(item.get("release_like", False)),
                release_evidence=item.get("release_evidence"),
                added_at=str(item["added_at"]),
                legacy_slot=slot,
            ))
            slots.append(VersionSlot(
                slot=slot,
                short_version=short_version,
                candidate_build_id=build_id,
                created_at=str(item["added_at"]),
            ))
            aliases[slot] = build_id
        return cls(
            app=app_id,
            bundle_id=raw.get("bundle_id"),
            version_slots=tuple(sorted(slots, key=lambda s: s.slot)),
            # Historical slots are the insertion order of a v1 store.
            builds=tuple(sorted(builds, key=lambda b: b.slot)),
            legacy_slot_aliases=aliases,
            source_schema_version="v1",
            # v1 spells the default selection the same way; the legacy
            # ``active_slot`` reading is normalized before this point.
            default_slot=raw.get("default_slot") or None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "v2",
            "app": self.app,
            "bundle_id": self.bundle_id,
            "version_slots": [slot.to_dict() for slot in self.version_slots],
            "builds": [build.to_dict() for build in self.builds],
            "legacy_slot_aliases": dict(self.legacy_slot_aliases),
            "default_slot": self.default_slot,
        }

    # -- lookups ----------------------------------------------------------- #

    def slot(self, slot: str) -> VersionSlot | None:
        return next((s for s in self.version_slots if s.slot == str(slot)), None)

    def slot_for_version(self, short_version: str | None) -> VersionSlot | None:
        key = _version_key(short_version)
        return next(
            (s for s in self.version_slots if _version_key(s.short_version) == key),
            None,
        )

    def build(self, build_id: str) -> BuildRecord | None:
        return next((b for b in self.builds if b.build_id == str(build_id)), None)

    def builds_for_slot(self, slot: str) -> list[BuildRecord]:
        """Builds of one Version Slot, oldest first.

        Order comes from the manifest array, not ``added_at``: the timestamp is
        second-granular, so two builds registered in the same second would sort
        arbitrarily — and "the previous build" would then be a coin flip.
        """
        return [b for b in self.builds if b.slot == str(slot)]

    def short_version_of(self, build: BuildRecord) -> str | None:
        slot = self.slot(build.slot)
        return slot.short_version if slot else None

    def next_slot(self, width: int = 3) -> str:
        used = [int(s.slot) for s in self.version_slots]
        return str((max(used) + 1) if used else 1).zfill(width)

    @property
    def latest_slot(self) -> VersionSlot | None:
        active = [s for s in self.version_slots if not s.archived]
        return max(active, key=lambda s: s.slot) if active else None

    def blob_bytes(self) -> int:
        """Distinct IPA bytes held by the store (Blobs, not Build Records)."""
        seen: dict[str, int] = {}
        for build in self.builds:
            if build.artifact_availability == "present":
                seen[build.sha256] = build.size
        return sum(seen.values())

    # -- resolution -------------------------------------------------------- #

    def resolve(
        self,
        selector: BuildSelector | None = None,
    ) -> RegistryBuildRef:
        """Resolve a caller selection to exactly one RegistryBuildRef.

        Fail-closed: an under-specified selector that matches several Builds is
        an error, never a "most recent wins" guess.  ``resolution_source`` on
        the result records which form actually decided it.

        Build Catalog 只按显式选择器或 ``default_slot`` 解析，不读取任何 Rig 状态
        （目标架构 §5.4；BL-011）：省略选择器 → ``default_slot`` 的 Candidate；没有
        默认选择 → 最新 Version Slot 的 Candidate。「设备上装的是哪个 Build」是
        Provisioning 发布的 Observed State，不参与解析。
        """
        selector = selector or BuildSelector()
        build, source = self._resolve_build(selector)
        if source == "legacy_alias":
            # The only runtime collection point for historical-alias traffic:
            # persisted refs record `resolution_source`, but a caller that just
            # reads a build and never writes a ref leaves no trace otherwise.
            # Retiring the compatibility branch needs counts, so say it out loud.
            logger.warning(
                "package build resolved through a legacy slot alias: "
                "app=%s legacy_slot=%s build_id=%s slot=%s",
                self.app, selector.slot, build.build_id, build.slot,
            )
        return RegistryBuildRef.from_build(
            self.app, build, self.short_version_of(build), resolution_source=source,
        )

    def _resolve_build(self, selector: BuildSelector) -> tuple[BuildRecord, str]:
        if selector.build_id:
            build = self.build(selector.build_id)
            if build is None:
                raise PackageModelError(
                    f"app {self.app!r} has no build {selector.build_id!r}"
                )
            if selector.slot and build.slot != str(selector.slot):
                raise PackageModelError(
                    f"build {selector.build_id!r} is in slot {build.slot!r}, "
                    f"not {selector.slot!r}"
                )
            return build, "slot_build_id" if selector.slot else "build_id"

        if selector.artifact_sha256:
            candidates = [
                b for b in self.builds
                if b.sha256 == str(selector.artifact_sha256)
                and (not selector.slot or str(selector.slot) in (b.slot, b.legacy_slot))
            ]
            if not candidates:
                raise PackageModelError(
                    f"app {self.app!r} has no build with artifact sha256 "
                    f"{str(selector.artifact_sha256)[:12]}…"
                    + (f" in slot {selector.slot!r}" if selector.slot else "")
                )
            if len(candidates) > 1:
                raise PackageModelError(
                    "ambiguous historical reference: "
                    f"{len(candidates)} builds share slot {selector.slot!r} and this artifact"
                )
            return candidates[0], "slot_sha256"

        if selector.slot:
            slot_key = str(selector.slot)
            if selector.legacy:
                alias = self.legacy_slot_aliases.get(slot_key)
                if alias is None:
                    raise PackageModelError(
                        f"app {self.app!r} has no legacy slot alias {slot_key!r}"
                    )
                build = self.build(alias)
                if build is None:
                    raise PackageModelError(
                        f"legacy alias {slot_key!r} points at missing build {alias!r}"
                    )
                return build, "legacy_alias"
            version_slot = self.slot(slot_key)
            if version_slot is None:
                alias = self.legacy_slot_aliases.get(slot_key)
                build = self.build(alias) if alias else None
                if build is not None:
                    return build, "legacy_alias"
                raise PackageModelError(f"app {self.app!r} has no version slot {slot_key!r}")
            build = self.build(version_slot.candidate_build_id or "")
            if build is None:
                raise PackageModelError(
                    f"version slot {slot_key!r} has no candidate build"
                )
            return build, "candidate"

        if self.default_slot:
            # 省略选择器时的默认选择（§12.1 / M8「Package 默认选择」）。默认槽指向不存在
            # 的 slot 是资产损坏，fail-closed 而不是悄悄退回最新。
            build, _source = self._resolve_build(BuildSelector(slot=str(self.default_slot)))
            return build, "default_slot"
        latest = self.latest_slot
        if latest is None:
            raise PackageModelError(f"app {self.app!r} has no packages; add one first")
        build = self.build(latest.candidate_build_id or "")
        if build is None:
            raise PackageModelError(
                f"version slot {latest.slot!r} has no candidate build"
            )
        return build, "candidate"

    # -- mutation (returns a new manifest; callers persist) ----------------- #

    def with_build(
        self, build: BuildRecord, *, slot: VersionSlot | None = None,
        make_candidate: bool = True,
    ) -> "PackageManifest":
        """Append a Build and optionally make it the Version Slot Candidate."""
        slots = list(self.version_slots)
        if slot is not None and self.slot(slot.slot) is None:
            slots.append(slot)
        if make_candidate:
            slots = [
                replace(s, candidate_build_id=build.build_id) if s.slot == build.slot else s
                for s in slots
            ]
        aliases = dict(self.legacy_slot_aliases)
        if build.legacy_slot:
            aliases.setdefault(build.legacy_slot, build.build_id)
        existing_builds = list(self.builds)
        if make_candidate:
            existing_builds = [
                replace(item, retention_class="iteration")
                if item.slot == build.slot and item.retention_class == "candidate"
                else item
                for item in existing_builds
            ]
        return replace(
            self,
            version_slots=tuple(sorted(slots, key=lambda s: s.slot)),
            builds=(*existing_builds, build),
            legacy_slot_aliases=aliases,
        )

    def with_candidate(self, build_id: str) -> "PackageManifest":
        build = self.build(build_id)
        if build is None:
            raise PackageModelError(f"app {self.app!r} has no build {build_id!r}")
        slots = tuple(
            replace(slot, candidate_build_id=build_id) if slot.slot == build.slot else slot
            for slot in self.version_slots
        )
        builds: list[BuildRecord] = []
        for item in self.builds:
            retention = item.retention_class
            if item.slot == build.slot and retention == "candidate":
                retention = "iteration"
            if item.build_id == build_id and retention not in ("accepted", "baseline"):
                retention = "candidate"
            builds.append(replace(item, retention_class=retention))
        return replace(self, version_slots=slots, builds=tuple(builds))

    def with_artifact(
        self,
        build_id: str,
        *,
        store_id: str | None = None,
        file: str | None = None,
        availability: str | None = None,
    ) -> "PackageManifest":
        build = self.build(build_id)
        if build is None:
            raise PackageModelError(f"app {self.app!r} has no build {build_id!r}")
        updated = replace(
            build,
            artifact_store_id=store_id or build.artifact_store_id,
            file=file or build.file,
            artifact_availability=availability or build.artifact_availability,
        )
        return replace(
            self,
            builds=tuple(updated if item.build_id == build_id else item for item in self.builds),
        )

    def without_build(self, build_id: str) -> "PackageManifest":
        build = self.build(build_id)
        if build is None:
            raise PackageModelError(f"app {self.app!r} has no build {build_id!r}")
        builds = tuple(b for b in self.builds if b.build_id != build_id)
        slots: list[VersionSlot] = []
        for version_slot in self.version_slots:
            if version_slot.candidate_build_id != build_id:
                slots.append(version_slot)
                continue
            remaining = [b for b in builds if b.slot == version_slot.slot]
            slots.append(replace(
                version_slot,
                candidate_build_id=remaining[-1].build_id if remaining else None,
            ))
        aliases = {k: v for k, v in self.legacy_slot_aliases.items() if v != build_id}
        return replace(
            self, version_slots=tuple(slots), builds=builds, legacy_slot_aliases=aliases,
        )


def _version_key(short_version: str | None) -> str | None:
    """Version grouping key: the trimmed literal, never SemVer-normalized."""
    if short_version is None:
        return None
    text = str(short_version).strip()
    return text or None


def group_by_version(
    builds: Iterable[BuildRecord], manifest: PackageManifest,
) -> dict[str | None, list[BuildRecord]]:
    grouped: dict[str | None, list[BuildRecord]] = {}
    for build in builds:
        grouped.setdefault(_version_key(manifest.short_version_of(build)), []).append(build)
    return grouped


def validate_build_id(build_id: str) -> str:
    if not _BUILD_ID.fullmatch(str(build_id or "")):
        raise PackageModelError(f"malformed build id {build_id!r}")
    return str(build_id)


def sorted_builds(builds: Sequence[BuildRecord]) -> list[BuildRecord]:
    """Group by Version Slot while keeping each slot's registration order."""
    return sorted(builds, key=lambda b: b.slot)
