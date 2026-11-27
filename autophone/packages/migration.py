"""package_migration.py — v1 → v2 package store migration, preview first.

Design source: ``docs/package-store-version-slot-build-trace.md`` §13.

The migration regroups historical per-IPA slots into per-version Version Slots
and backfills Source Revisions from evidence that already carries an artifact
sha256.  It never改写 existing Run / Requirement / Ledger files, never invents a
Source Revision from a label, filename, timestamp or the current Git HEAD, and
refuses to write at all when one artifact carries conflicting authoritative
revisions.

``plan()`` is read-only.  ``apply()`` writes the v2 manifest (and Target-level
install receipts) atomically, and only from a clean plan.

A conflict can also be taken off the table by an explicit human decision rather
than by choosing a winner: ``plan()``/``apply()`` accept ``exclusions``, a list
of :class:`ArtifactExclusion` records naming an artifact sha256 that is no
longer adjudicated.  An excluded artifact keeps ``unknown`` as its Source
Revision — the decision withdraws the claims, it never picks one — and the
suppressed conflict, with its full claim identities and per-file evidence
hashes, is copied into the migration receipt written under
``packages/<app>/migrations/``.  Historical iteration files stay untouched.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from autophone.paths import STATE_ROOT
from autophone.contracts import validate_or_raise, write_json
from autophone.packages.model import (
    BuildRecord,
    PackageManifest,
    SourceRevision,
    VersionSlot,
    compute_build_id,
    _version_key,
)
from autophone.util import now_iso, read_json

ROOT = STATE_ROOT

# Historical ``identity.source_commit`` spellings found in requirement
# iterations.  Anything else is treated as non-authoritative rather than parsed
# optimistically.
_BARE_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SOURCE_TREE = re.compile(
    r"^source-tree-sha256:(?P<tree>[0-9a-f]{64})(?::(?P<state>dirty|generated))?$"
)
_WORKFLOW_TREE = re.compile(r"^workflow:[^:]+:sha256:(?P<tree>[0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

MIGRATION_RECEIPT_SCHEMA = "package-migration-receipt.v1.json"


class MigrationError(RuntimeError):
    """The plan is not clean enough to write."""


class DecisionError(ValueError):
    """An exclusion decision is unusable as written."""


@dataclass(frozen=True)
class ArtifactExclusion:
    """A human decision to stop adjudicating one artifact's Source Revision.

    This is a withdrawal, not a verdict: the artifact keeps ``unknown``, and the
    claims that conflicted are preserved verbatim in the migration receipt.
    """

    artifact_sha256: str
    reason: str
    decided_at: str
    decided_by: str

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(str(self.artifact_sha256 or "").lower()):
            raise DecisionError(
                f"exclusion artifact_sha256 must be 64 hex chars, got {self.artifact_sha256!r}"
            )
        object.__setattr__(self, "artifact_sha256", str(self.artifact_sha256).lower())
        for field_name in ("reason", "decided_at", "decided_by"):
            if not str(getattr(self, field_name) or "").strip():
                raise DecisionError(
                    f"exclusion for {self.artifact_sha256[:12]}… is missing {field_name}"
                )
            object.__setattr__(self, field_name, str(getattr(self, field_name)).strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_sha256": self.artifact_sha256,
            "reason": self.reason,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
        }


def parse_decision_document(raw: Mapping[str, Any]) -> list[ArtifactExclusion]:
    """Read a decision file into exclusions. Top-level who/when are defaults."""
    if not isinstance(raw, Mapping):
        raise DecisionError("decision document must be a JSON object")
    entries = raw.get("exclusions")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise DecisionError("decision document needs an 'exclusions' array")
    default_by = str(raw.get("decided_by") or "").strip()
    default_at = str(raw.get("decided_at") or "").strip()
    default_reason = str(raw.get("reason") or "").strip()
    exclusions: list[ArtifactExclusion] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise DecisionError("each exclusion must be a JSON object")
        exclusions.append(ArtifactExclusion(
            artifact_sha256=str(entry.get("artifact_sha256") or ""),
            reason=str(entry.get("reason") or default_reason),
            decided_at=str(entry.get("decided_at") or default_at),
            decided_by=str(entry.get("decided_by") or default_by),
        ))
    return _checked_exclusions(exclusions)


def load_decision_file(path: str | Path) -> list[ArtifactExclusion]:
    document = read_json(Path(path))
    if document is None:
        raise DecisionError(f"decision file not found or not JSON: {path}")
    return parse_decision_document(document)


def _checked_exclusions(
    exclusions: Sequence[ArtifactExclusion],
) -> list[ArtifactExclusion]:
    seen: dict[str, ArtifactExclusion] = {}
    for exclusion in exclusions:
        existing = seen.get(exclusion.artifact_sha256)
        if existing is not None and existing != exclusion:
            raise DecisionError(
                f"artifact {exclusion.artifact_sha256[:12]}… has two different decisions"
            )
        seen[exclusion.artifact_sha256] = exclusion
    return sorted(seen.values(), key=lambda item: item.artifact_sha256)


@dataclass(frozen=True)
class RevisionEvidence:
    """One authoritative (artifact sha256 → source revision) observation."""

    artifact_sha256: str
    revision: SourceRevision
    source: str
    source_sha256: str | None = None


@dataclass
class MigrationPlan:
    app: str
    source_schema_version: str
    manifest: PackageManifest
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    receipts: list[dict[str, Any]] = field(default_factory=list)
    exclusions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.conflicts

    def to_dict(self) -> dict[str, Any]:
        by_slot: dict[str, list[BuildRecord]] = {}
        for build in self.manifest.builds:
            by_slot.setdefault(build.slot, []).append(build)
        return {
            "app": self.app,
            "generated_at": now_iso(),
            "source_schema_version": self.source_schema_version,
            "clean": self.clean,
            "version_slots": [
                {
                    "slot": slot.slot,
                    "short_version": slot.short_version,
                    "candidate_build_id": slot.candidate_build_id,
                    "build_count": len(by_slot.get(slot.slot, [])),
                    "legacy_slots": sorted(
                        b.legacy_slot for b in by_slot.get(slot.slot, []) if b.legacy_slot
                    ),
                }
                for slot in self.manifest.version_slots
            ],
            "source_revisions": {
                "resolved": sum(
                    1 for b in self.manifest.builds if b.source_revision.is_managed
                ),
                "unknown": sum(
                    1 for b in self.manifest.builds if not b.source_revision.is_managed
                ),
            },
            "conflicts": list(self.conflicts),
            "exclusions": list(self.exclusions),
            "excluded_artifacts": len(self.exclusions),
            "notes": list(self.notes),
            "receipts": list(self.receipts),
            "manifest": self.manifest.to_dict(),
        }


# --------------------------------------------------------------------------- #
# Source revision evidence
# --------------------------------------------------------------------------- #

def parse_source_commit(value: str | None) -> SourceRevision | None:
    """Map a historical ``source_commit`` string to a Source Revision.

    Returns None for spellings that are not authoritative source identities, so
    the caller records ``unknown`` instead of half-parsing.
    """
    text = str(value or "").strip().lower()
    if _BARE_COMMIT.fullmatch(text):
        return SourceRevision.git_commit(text)
    for pattern in (_SOURCE_TREE, _WORKFLOW_TREE):
        matched = pattern.fullmatch(text)
        if matched is not None:
            return SourceRevision.source_tree(
                matched.group("tree"),
                state=matched.groupdict().get("state") or "generated",
            )
    return None


def collect_revision_evidence(
    app: str, *, root: Path = ROOT,
) -> list[RevisionEvidence]:
    """Scan requirement iterations for (artifact sha256 → source revision) pairs.

    Only evidence that carries the artifact sha256 counts, so a revision can
    never be attached to the wrong bytes.
    """
    evidence: list[RevisionEvidence] = []
    requirements_root = Path(root) / "requirements"
    if not requirements_root.is_dir():
        return evidence
    for path in sorted(requirements_root.glob("*/iterations/*/iteration.json")):
        content = path.read_bytes()
        document = json.loads(content)
        if not isinstance(document, Mapping):
            continue
        identity = document.get("identity") or {}
        build = identity.get("build") or {}
        if str(build.get("app") or "") != app:
            continue
        sha256 = str(build.get("sha256") or "")
        revision = parse_source_commit(identity.get("source_commit"))
        if not sha256 or revision is None:
            continue
        evidence.append(RevisionEvidence(
            artifact_sha256=sha256,
            revision=revision,
            source=str(path.relative_to(Path(root))),
            source_sha256=hashlib.sha256(content).hexdigest(),
        ))
    return evidence


def _claims(
    items: Sequence[RevisionEvidence], revisions: Sequence[SourceRevision],
) -> list[dict[str, Any]]:
    """Full identities with their exact supporting files.

    Display prefixes are never sufficient for adjudication or for auditing a
    decision after the fact, so every claim carries the whole Source Revision
    plus each evidence file's own sha256.
    """
    return [
        {
            "source_revision": revision.to_dict(),
            "evidence": [
                {"path": item.source, "sha256": item.source_sha256}
                for item in sorted(items, key=lambda item: item.source)
                if item.revision == revision
            ],
        }
        for revision in sorted(
            revisions, key=lambda revision: json.dumps(revision.to_dict(), sort_keys=True),
        )
    ]


def _conflict_record(
    sha256: str, items: Sequence[RevisionEvidence], distinct: Sequence[SourceRevision],
) -> dict[str, Any]:
    return {
        "code": "source_revision_conflict",
        "artifact_sha256": sha256,
        "detail": (
            f"{len(distinct)} conflicting authoritative source revisions for one artifact"
        ),
        "values": sorted(rev.identity for rev in distinct),
        "evidence": sorted({item.source for item in items}),
        "claims": _claims(items, distinct),
    }


def _revision_index(
    evidence: Sequence[RevisionEvidence],
    exclusions: Sequence[ArtifactExclusion] = (),
) -> tuple[dict[str, SourceRevision], list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve artifact → Source Revision, and report what blocks or was withdrawn.

    An excluded artifact never reaches the conflict gate and never gets a
    revision: a decision withdraws the competing claims, it does not elect one.
    """
    decided = {item.artifact_sha256: item for item in exclusions}
    grouped: dict[str, list[RevisionEvidence]] = {}
    for item in evidence:
        grouped.setdefault(item.artifact_sha256, []).append(item)
    resolved: dict[str, SourceRevision] = {}
    conflicts: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for sha256, items in grouped.items():
        distinct = {item.revision for item in items}
        decision = decided.get(sha256)
        if decision is not None:
            record = dict(decision.to_dict())
            record.update({
                "code": "excluded_by_decision",
                "withdrawn_claims": _claims(items, distinct),
                "resulting_source_revision": SourceRevision.unknown().to_dict(),
            })
            if len(distinct) > 1:
                record["suppressed_conflict"] = _conflict_record(sha256, items, distinct)
            excluded.append(record)
            continue
        if len(distinct) == 1:
            resolved[sha256] = items[0].revision
            continue
        conflicts.append(_conflict_record(sha256, items, distinct))
    for sha256, decision in decided.items():
        if sha256 in grouped:
            continue
        record = dict(decision.to_dict())
        record.update({
            "code": "excluded_by_decision",
            "withdrawn_claims": [],
            "resulting_source_revision": SourceRevision.unknown().to_dict(),
            "matched_no_evidence": True,
        })
        excluded.append(record)
    excluded.sort(key=lambda item: item["artifact_sha256"])
    return resolved, conflicts, excluded


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #

def plan(
    app: str,
    *,
    root: Path = ROOT,
    code_root: Path | None = None,
    exclusions: Sequence[ArtifactExclusion] = (),
) -> MigrationPlan:
    """Compute the v2 manifest this store would migrate to. Reads only."""
    from autophone.packages import store as packages

    decisions = _checked_exclusions(exclusions)
    raw = read_json(packages.manifest_path(app, root=root))
    if raw is None:
        raise MigrationError(f"app {app!r} has no package manifest to migrate")
    schema_version = str(raw.get("schema_version") or "")
    if schema_version == "v2":
        notes = [{"code": "already_v2", "detail": "store is already at schema v2"}]
        if decisions:
            notes.append({
                "code": "exclusions_not_applicable",
                "detail": "store is already v2; no conflict gate to withdraw claims from",
                "artifacts": [item.artifact_sha256 for item in decisions],
            })
        return MigrationPlan(
            app=app,
            source_schema_version="v2",
            manifest=PackageManifest.from_dict(raw, app=app),
            notes=notes,
        )

    projected = PackageManifest.from_v1(raw, app=app)
    revisions, conflicts, excluded = _revision_index(
        collect_revision_evidence(app, root=root), decisions,
    )
    for record in (*conflicts, *excluded):
        slots = sorted(
            build.slot for build in projected.builds
            if build.sha256 == record["artifact_sha256"]
        )
        record["registered_legacy_slots"] = slots
        # Keep the suppressed conflict shaped exactly like the one the gate
        # would have raised, so the receipt is comparable with a plan preview.
        if isinstance(record.get("suppressed_conflict"), dict):
            record["suppressed_conflict"]["registered_legacy_slots"] = slots
    notes: list[dict[str, Any]] = []

    receipt = packages.load_install_receipt(app, root=root)
    installed_slot = str(receipt.get("slot")) if receipt else None
    # Explicit null means no default. Only pre-split manifests fall back to
    # active_slot; an installation receipt must not override asset selection.
    default_value = raw.get("default_slot") if "default_slot" in raw else raw.get("active_slot")
    default_slot = str(default_value) if default_value else None
    if default_slot and projected.slot(default_slot) is None:
        conflicts.append({
            "code": "default_slot_unresolvable",
            "slot": default_slot,
            "detail": "default slot matches no historical package",
        })

    grouped: dict[str | None, list[BuildRecord]] = {}
    for build in projected.builds:
        grouped.setdefault(
            _version_key(projected.short_version_of(build)), [],
        ).append(build)

    version_slots: list[VersionSlot] = []
    builds: list[BuildRecord] = []
    aliases: dict[str, str] = {}
    ordered_versions = sorted(
        grouped.items(),
        key=lambda item: min(b.slot for b in item[1]),
    )
    for short_version, group in ordered_versions:
        legacy_slots = sorted(b.slot for b in group)
        canonical = _canonical_slot(legacy_slots, installed_slot, default_slot)
        if len(group) > 1:
            notes.append({
                "code": "version_slots_merged",
                "short_version": short_version,
                "canonical_slot": canonical,
                "legacy_slots": legacy_slots,
            })
        migrated: list[BuildRecord] = []
        for build in sorted(group, key=lambda b: b.slot):
            revision = revisions.get(build.sha256, SourceRevision.unknown())
            build_id = compute_build_id(
                app=app,
                slot=canonical,
                short_version=short_version,
                build_version=build.build_version,
                source_revision=revision,
                artifact_sha256=build.sha256,
            )
            migrated.append(BuildRecord(
                build_id=build_id,
                slot=canonical,
                label=build.label,
                bundle_id=build.bundle_id,
                build_version=build.build_version,
                source_revision=revision,
                file=build.file,
                sha256=build.sha256,
                size=build.size,
                release_like=build.release_like,
                release_evidence=build.release_evidence,
                added_at=build.added_at,
                legacy_slot=build.slot,
            ))
        for build in migrated:
            if build.legacy_slot in aliases:
                conflicts.append({
                    "code": "legacy_slot_collision",
                    "detail": f"legacy slot {build.legacy_slot!r} maps to more than one build",
                })
            aliases[str(build.legacy_slot)] = build.build_id
        candidate = _candidate_build(migrated, installed_slot, default_slot)
        version_slots.append(VersionSlot(
            slot=canonical,
            short_version=short_version,
            candidate_build_id=candidate.build_id if candidate else None,
            created_at=min(b.added_at for b in migrated),
        ))
        builds.extend(migrated)

    duplicated = [
        slot.slot for slot in version_slots
        if sum(1 for other in version_slots if other.slot == slot.slot) > 1
    ]
    if duplicated:
        conflicts.append({
            "code": "canonical_slot_collision",
            "detail": f"versions would share canonical slots {sorted(set(duplicated))}",
        })

    manifest = PackageManifest(
        app=app,
        bundle_id=raw.get("bundle_id"),
        version_slots=tuple(sorted(version_slots, key=lambda s: s.slot)),
        builds=tuple(sorted(builds, key=lambda b: (b.slot, b.added_at))),
        legacy_slot_aliases=aliases,
        source_schema_version="v1",
        default_slot=next(
            (build.slot for build in builds if build.legacy_slot == default_slot), None,
        ),
    )

    receipts: list[dict[str, Any]] = []
    if receipt is not None:
        migrated_receipt = _migrate_receipt(receipt, manifest, app)
        if migrated_receipt is None:
            conflicts.append({
                "code": "install_receipt_unresolvable",
                "detail": (
                    f"install receipt slot={receipt.get('slot')!r} "
                    f"sha256={str(receipt.get('sha256'))[:12]}… matches no migrated build"
                ),
            })
        else:
            receipts.append(migrated_receipt)

    return MigrationPlan(
        app=app,
        source_schema_version=schema_version or "v1",
        manifest=manifest,
        conflicts=conflicts,
        notes=notes,
        receipts=receipts,
        exclusions=excluded,
    )


def _canonical_slot(
    legacy_slots: Sequence[str], installed_slot: str | None, default_slot: str | None,
) -> str:
    """Preserve explicit asset selection, then use the historical fallback."""
    if default_slot and default_slot in legacy_slots:
        return default_slot
    if installed_slot and installed_slot in legacy_slots:
        return installed_slot
    return max(legacy_slots)


def _candidate_build(
    builds: Sequence[BuildRecord], installed_slot: str | None, default_slot: str | None,
) -> BuildRecord | None:
    for marker in (default_slot, installed_slot):
        if marker:
            match = next((b for b in builds if b.legacy_slot == marker), None)
            if match is not None:
                return match
    return builds[-1] if builds else None


def _migrate_receipt(
    receipt: Mapping[str, Any], manifest: PackageManifest, app: str,
) -> dict[str, Any] | None:
    sha256 = str(receipt.get("sha256") or "")
    slot = str(receipt.get("slot") or "")
    build = next(
        (b for b in manifest.builds if b.sha256 == sha256 and b.legacy_slot == slot),
        None,
    )
    if build is None:
        return None
    from autophone.packages.model import RegistryBuildRef

    ref = RegistryBuildRef.from_build(
        app, build, manifest.short_version_of(build), resolution_source="slot_sha256",
    )
    return {
        "schema_version": "v2",
        "app": app,
        "build_ref": ref.to_dict(),
        "verified": bool(receipt.get("verified")),
        "installer": str(receipt.get("installer") or "vphone_native"),
        "target": dict(receipt.get("target") or {}),
        "target_fingerprint": str(receipt.get("target_fingerprint") or ""),
        "installed_at": str(receipt.get("installed_at") or now_iso()),
    }


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #

def _migration_receipt(
    migration: MigrationPlan,
    *,
    source_manifest_sha256: str | None,
    manifest_document: Mapping[str, Any],
) -> dict[str, Any]:
    """The immutable record of one applied migration and the decisions it used."""
    result_sha256 = hashlib.sha256(
        json.dumps(manifest_document, sort_keys=True, ensure_ascii=False).encode("utf-8"),
    ).hexdigest()
    identity = json.dumps(
        {
            "app": migration.app,
            "source_manifest_sha256": source_manifest_sha256,
            "result_manifest_sha256": result_sha256,
            "exclusions": migration.exclusions,
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return {
        "schema_version": "v1",
        "migration_id": "mig-" + hashlib.sha256(identity).hexdigest()[:24],
        "app": migration.app,
        "migrated_at": now_iso(),
        "source_schema_version": migration.source_schema_version,
        "source_manifest_sha256": source_manifest_sha256,
        "result_manifest_sha256": result_sha256,
        "version_slots": len(migration.manifest.version_slots),
        "builds": len(migration.manifest.builds),
        "exclusions": list(migration.exclusions),
        "notes": list(migration.notes),
    }


def apply(
    app: str,
    *,
    root: Path = ROOT,
    code_root: Path | None = None,
    backup: bool = True,
    exclusions: Sequence[ArtifactExclusion] = (),
) -> dict[str, Any]:
    """Write the migrated v2 manifest and Target receipts. Refuses a dirty plan.

    ``exclusions`` withdraw named artifacts from the conflict gate; every one of
    them is written back out as an ``excluded_by_decision`` entry in the
    migration receipt, together with the conflict it suppressed.
    """
    from autophone.packages import store as packages

    with packages._package_mutation_lock(app, root=Path(root)):
        migration = plan(app, root=root, code_root=code_root, exclusions=exclusions)
        if migration.source_schema_version == "v2":
            return {"app": app, "migrated": False, "reason": "already_v2"}
        if not migration.clean:
            raise MigrationError(
                f"refusing to migrate {app!r}: "
                + "; ".join(str(c.get("code")) for c in migration.conflicts)
            )
        document = migration.manifest.to_dict()
        validate_or_raise("package-manifest.v2.json", document)
        for receipt in migration.receipts:
            validate_or_raise("package-install-receipt.v2.json", receipt)

        path = packages.manifest_path(app, root=root)
        source_manifest_sha256 = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        )
        migration_receipt = _migration_receipt(
            migration,
            source_manifest_sha256=source_manifest_sha256,
            manifest_document=document,
        )
        validate_or_raise(MIGRATION_RECEIPT_SCHEMA, migration_receipt)
        receipt_path = packages.migration_receipt_path(
            app, migration_receipt["migration_id"], root=root,
        )

        if backup and path.is_file():
            shutil.copy2(path, path.with_suffix(".v1.bak.json"))
        write_json(path, document)
        written: list[str] = []
        for receipt in migration.receipts:
            target = packages.target_receipt_path(
                app, receipt["target_fingerprint"], root=root,
            )
            write_json(target, receipt)
            written.append(str(target))
        write_json(receipt_path, migration_receipt)
        return {
            "app": app,
            "migrated": True,
            "manifest": str(path),
            "receipts": written,
            "migration_receipt": str(receipt_path),
            "migration_id": migration_receipt["migration_id"],
            "version_slots": len(migration.manifest.version_slots),
            "builds": len(migration.manifest.builds),
            "exclusions": list(migration.exclusions),
        }
