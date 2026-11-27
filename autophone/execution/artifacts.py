"""Seal Run artifacts into the Rig-local persistent spool before Lease release."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ArtifactSealingError(RuntimeError):
    pass


SPOOL_MANIFEST = "spool-manifest.json"


@dataclass(frozen=True)
class SealedArtifacts:
    spool_dir: Path
    manifest: Path
    refs: tuple[dict[str, Any], ...]


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _load_existing(spool_dir: Path) -> SealedArtifacts | None:
    manifest = spool_dir / SPOOL_MANIFEST
    if not manifest.is_file():
        return None
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        refs = tuple(dict(item) for item in raw["artifact_refs"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ArtifactSealingError(f"invalid existing Artifact spool: {manifest}: {exc}") from exc
    for ref in refs:
        relative = str(ref.get("logical_name") or "")
        path = (spool_dir / relative).resolve()
        try:
            path.relative_to(spool_dir.resolve())
        except ValueError as exc:
            raise ArtifactSealingError(f"Artifact path escapes spool: {relative}") from exc
        if not path.is_file():
            raise ArtifactSealingError(f"sealed Artifact is missing: {relative}")
        sha256, size = _hash_file(path)
        if sha256 != ref.get("sha256") or size != ref.get("size"):
            raise ArtifactSealingError(f"sealed Artifact does not match manifest: {relative}")
    return SealedArtifacts(spool_dir, manifest, refs)


def seal_artifacts(
    source_dir: Path | None,
    spool_dir: Path,
    *,
    owner_id: str,
) -> SealedArtifacts:
    """Copy a completed Run tree, hash every file, and publish it atomically."""

    spool_dir = spool_dir.resolve()
    existing = _load_existing(spool_dir)
    if existing is not None:
        return existing
    source = source_dir.resolve() if source_dir is not None else None
    if source is not None and not source.is_dir():
        raise ArtifactSealingError(f"Run artifact source does not exist: {source}")
    if source is not None:
        if (source / SPOOL_MANIFEST).exists():
            raise ArtifactSealingError(
                f"Run artifact source uses reserved name: {SPOOL_MANIFEST}"
            )
        symlinks = [path for path in source.rglob("*") if path.is_symlink()]
        if symlinks:
            raise ArtifactSealingError(
                f"Run artifact source contains a symbolic link: {symlinks[0]}"
            )
        try:
            spool_dir.relative_to(source)
        except ValueError:
            pass
        else:
            raise ArtifactSealingError("Artifact spool must not be inside the source tree")

    spool_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix=f".{spool_dir.name}-", dir=spool_dir.parent))
    try:
        if source is not None:
            for item in source.iterdir():
                destination = temp_root / item.name
                if item.is_dir():
                    shutil.copytree(item, destination, symlinks=False)
                elif item.is_file():
                    shutil.copy2(item, destination, follow_symlinks=True)
        refs: list[dict[str, Any]] = []
        for path in sorted(item for item in temp_root.rglob("*") if item.is_file()):
            if path == temp_root / SPOOL_MANIFEST:
                continue
            relative = path.relative_to(temp_root).as_posix()
            sha256, size = _hash_file(path)
            refs.append({
                "owner_kind": "assignment_attempt",
                "owner_id": owner_id,
                "logical_name": relative,
                "uri": (spool_dir / relative).as_uri(),
                "sha256": sha256,
                "size": size,
                "governance": "local_only",
                "seal_status": "sealed",
            })
        manifest = temp_root / SPOOL_MANIFEST
        manifest.write_text(
            json.dumps(
                {"schema_version": "artifact-spool.v1", "artifact_refs": refs},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )
        with manifest.open("rb") as stream:
            os.fsync(stream.fileno())
        try:
            temp_root.replace(spool_dir)
        except FileExistsError:
            existing = _load_existing(spool_dir)
            if existing is None:
                raise ArtifactSealingError(f"Artifact spool already exists: {spool_dir}")
            return existing
        return SealedArtifacts(
            spool_dir,
            spool_dir / SPOOL_MANIFEST,
            tuple(refs),
        )
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise
