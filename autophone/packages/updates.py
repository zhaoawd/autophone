"""Independent RN Update metadata registry (R-07).

Artifact locators are opaque product references. Registration records declared
identity, and makes no claim about signatures, loader execution or activation.
All mutations share the Build store's per-app lock so base references cannot
race Build removal or pruning.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from autophone.contracts import validate_or_raise, write_json
from autophone.packages import store
from autophone.util import read_json

RETENTION_CLASSES = ("iteration", "candidate", "accepted", "baseline")


def manifest_path(app: str, *, root: Path = store.ROOT) -> Path:
    app_dir = store.store_dir(app, root=root)
    path = app_dir / "updates" / "manifest.json"
    if not path.resolve(strict=False).is_relative_to(app_dir.resolve(strict=False)):
        raise store.PackageError("update manifest escapes package store")
    return path


def _load(app: str, *, root: Path) -> dict[str, Any]:
    path = manifest_path(app, root=root)
    if not path.exists():
        return {"schema_version": "rn-update-store/v1", "app": app, "updates": []}
    raw = read_json(path)
    validate_or_raise("rn-update-store.v1.json", raw)
    if raw["app"] != app:
        raise store.PackageError("update registry app mismatch")
    ids = [record["update_id"] for record in raw["updates"]]
    if len(ids) != len(set(ids)):
        raise store.PackageError("duplicate update_id in update registry")
    return raw


def list_updates(
    app: str, *, required_native_runtime_id: str | None = None,
    base_build_id: str | None = None, root: Path = store.ROOT,
) -> list[dict[str, Any]]:
    """Select records by runtime ID and/or base Build; both filters are exact."""
    return [record for record in _load(app, root=root)["updates"]
            if (required_native_runtime_id is None
                or record["required_native_runtime_id"] == required_native_runtime_id)
            and (base_build_id is None or record["base_build_id"] == base_build_id)]


def base_retention_holds(app: str, build_id: str, *, root: Path) -> list[str]:
    """Every retained record holds its base, including iteration Updates."""
    return [f"retained_update:{record['update_id']}"
            for record in list_updates(app, base_build_id=build_id, root=root)]


@store._locked_package_mutation
def register_update(
    app: str, record: Mapping[str, Any], *, root: Path = store.ROOT,
    code_root: Path | None = None,
) -> dict[str, Any]:
    """Persist immutable declared identity after checking the base is retained."""
    item = deepcopy(dict(record))
    validate_or_raise("rn-update-record.v1.json", item)
    registry = _load(app, root=root)
    for existing in registry["updates"]:
        if existing["update_id"] == item["update_id"]:
            if existing != item:
                raise store.PackageError("immutable update record cannot be overwritten")
            return existing
    build = store.load_registry(app, root=root, code_root=code_root).build(item["base_build_id"])
    if build is None or build.artifact_availability != "present":
        raise store.PackageError("update base build is missing or pruned")
    registry["updates"].append(item)
    write_json(manifest_path(app, root=root), registry)
    return item


def _find(registry: dict[str, Any], update_id: str) -> dict[str, Any]:
    for item in registry["updates"]:
        if item["update_id"] == update_id:
            return item
    raise store.PackageError(f"unknown update {update_id!r}")


@store._locked_package_mutation
def set_retention_class(
    app: str, update_id: str, retention_class: str, *, root: Path = store.ROOT,
) -> dict[str, Any]:
    """Change retention metadata while preserving all declared identity fields."""
    if retention_class not in RETENTION_CLASSES:
        raise store.PackageError(f"unknown retention class {retention_class!r}")
    registry = _load(app, root=root)
    item = _find(registry, update_id)
    item["retention_class"] = retention_class
    write_json(manifest_path(app, root=root), registry)
    return item


@store._locked_package_mutation
def remove_update(app: str, update_id: str, *, root: Path = store.ROOT) -> dict[str, Any]:
    """Remove unprotected metadata only; product-owned bytes remain untouched."""
    registry = _load(app, root=root)
    item = _find(registry, update_id)
    if item["retention_class"] in ("accepted", "baseline"):
        raise store.PackageError(f"update retention hold: {item['retention_class']}")
    registry["updates"].remove(item)
    write_json(manifest_path(app, root=root), registry)
    return item
