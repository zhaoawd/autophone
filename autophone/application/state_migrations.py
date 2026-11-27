"""Transactional host-state migrations for managed autophone installs."""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import stat
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator

from autophone.paths import CODE_ROOT, STATE_ROOT
from autophone.util import write_json_atomic


CURRENT_STATE_SCHEMA = 1
STATE_FILE = ".autophone-state.json"
_B_STATE_ROOTS = (
    "runs",
    "packages",
    "requirements",
    "logs",
    "config",
    "apps",
    ".control.log",
)
_TRANSACTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class StateMigrationError(RuntimeError):
    pass


class StateSchemaTooNew(StateMigrationError):
    pass


class IrreversibleMigrationRequired(StateMigrationError):
    pass


@dataclass(frozen=True)
class Migration:
    schema: int
    migration_id: str
    reversible: bool
    module: ModuleType


@dataclass
class MigrationContext:
    code_root: Path
    state_root: Path
    legacy_root: Path | None = None

    def import_legacy_state(self) -> list[str]:
        source = (self.legacy_root or self.code_root).resolve()
        target = self.state_root.resolve()
        if source == target:
            return []
        imported: list[str] = []
        for name in ("runs", "packages", "requirements", "logs"):
            src = source / name
            dst = target / name
            if src.is_dir():
                _copy_tree_missing(src, dst)
                imported.append(name)
        for phase in ("phase2", "phase4", "phase5"):
            src = source / "tools" / phase / "runs"
            dst = target / "runs" / phase
            if src.is_dir():
                _copy_tree_missing(src, dst)
                imported.append(f"runs/{phase}")
        src_config = source / "config"
        for src in src_config.glob("*.local.*") if src_config.is_dir() else ():
            dst = target / "config" / src.name
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                imported.append(dst.relative_to(target).as_posix())
        src_apps = source / "apps"
        for src in src_apps.glob("*/dispositions.json") if src_apps.is_dir() else ():
            dst = target / "apps" / src.parent.name / src.name
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                imported.append(dst.relative_to(target).as_posix())
        root_control = source / ".control.log"
        if root_control.is_file() and not (target / root_control.name).exists():
            shutil.copy2(root_control, target / root_control.name)
            imported.append(root_control.name)
        return imported


def _copy_tree_missing(source: Path, target: Path) -> None:
    for src in source.rglob("*"):
        rel = src.relative_to(source)
        dst = target / rel
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        elif src.is_file() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def state_file(state_root: Path = STATE_ROOT) -> Path:
    return Path(state_root) / STATE_FILE


def load_state_metadata(state_root: Path = STATE_ROOT) -> dict[str, Any]:
    path = state_file(state_root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"state_schema": 0, "applied_migrations": [], "last_version": None}
    except (OSError, json.JSONDecodeError) as exc:
        raise StateMigrationError(f"invalid state metadata {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise StateMigrationError(f"invalid state metadata {path}: expected object")
    schema = raw.get("state_schema", 0)
    applied = raw.get("applied_migrations", [])
    if not isinstance(schema, int) or schema < 0 or not isinstance(applied, list):
        raise StateMigrationError(f"invalid state metadata {path}: invalid schema/applied_migrations")
    return {
        "state_schema": schema,
        "applied_migrations": [str(item) for item in applied],
        "last_version": raw.get("last_version"),
        **{key: value for key, value in raw.items() if key not in {
            "state_schema", "applied_migrations", "last_version",
        }},
    }


@contextmanager
def migration_lock(state_root: Path = STATE_ROOT) -> Iterator[None]:
    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".migration.lock"
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StateMigrationError(f"another state migration is active: {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def discover_migrations(migrations_dir: Path | None = None) -> list[Migration]:
    root = Path(migrations_dir or (CODE_ROOT / "migrations"))
    found: list[Migration] = []
    if not root.is_dir():
        return found
    for path in sorted(root.glob("[0-9][0-9][0-9][0-9]_*.py")):
        spec = importlib.util.spec_from_file_location(f"autophone_state_{path.stem}", path)
        if spec is None or spec.loader is None:
            raise StateMigrationError(f"cannot load migration: {path}")
        module = importlib.util.module_from_spec(spec)
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), module.__dict__)
        schema = int(getattr(module, "STATE_SCHEMA", int(path.name[:4])))
        migration_id = str(getattr(module, "MIGRATION_ID", path.stem))
        reversible = bool(getattr(module, "REVERSIBLE", hasattr(module, "backward")))
        if not callable(getattr(module, "forward", None)):
            raise StateMigrationError(f"migration has no forward(): {path}")
        found.append(Migration(schema, migration_id, reversible, module))
    schemas = [item.schema for item in found]
    if schemas != sorted(set(schemas)):
        raise StateMigrationError(f"duplicate or unordered migration schemas: {schemas}")
    return found


def snapshot_metadata(
    state_root: Path,
    *,
    transaction_id: str,
    extra_paths: list[Path] | None = None,
) -> Path:
    root = Path(state_root).resolve()
    if not isinstance(transaction_id, str) or _TRANSACTION_ID.fullmatch(transaction_id) is None:
        raise StateMigrationError(f"invalid snapshot transaction_id: {transaction_id!r}")
    backup_root = root / ".backup"
    if backup_root.is_symlink():
        raise StateMigrationError(f"snapshot backup root must not be a symlink: {backup_root}")
    backup = backup_root / transaction_id
    backup.mkdir(parents=True, exist_ok=False)
    paths = [state_file(root), root / "runs" / "platform" / "index.sqlite"]
    paths.extend(sorted((root / "packages").glob("*/manifest.json")))
    requirement_root = root / "requirements"
    paths.append(requirement_root / "index.sqlite")
    for pattern in (
        "*/requirement.json",
        "*/ledger.jsonl",
        "*/findings/*.json",
        "*/iterations/*/iteration.json",
        "*/iterations/*/install-receipt.json",
        "*/authorizations/source-sharing*.json",
        "*/workflows/*/workflow-spec.json",
        "*/workflows/*/workflow-state.json",
        "*/workflows/*/workflow-events.jsonl",
        "*/workflows/*/authorizations/*.json",
        "*/workflows/*/attempts/*/stage-receipt.json",
        "*/workflows/*/attempts/*/approval-receipt.json",
        "*/workflows/*/attempts/*/source-baseline.json",
        "*/workflows/*/attempts/*/source-recovery-receipt.json",
    ):
        paths.extend(sorted(requirement_root.glob(pattern)))
    paths.extend(extra_paths or [])
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in paths:
        try:
            rel = source.resolve(strict=False).relative_to(root).as_posix()
        except ValueError as exc:
            raise StateMigrationError(f"snapshot path escapes state root: {source}") from exc
        if rel in seen:
            continue
        seen.add(rel)
        exists = source.is_file()
        entries.append({"path": rel, "existed": exists})
        if not exists:
            continue
        destination = backup / "files" / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.name == "index.sqlite":
            try:
                # ``sqlite3`` connections are context managers over the
                # transaction, not the handle: they must be closed explicitly or
                # every snapshot leaks two descriptors.
                with closing(sqlite3.connect(source)) as src_db, \
                        closing(sqlite3.connect(destination)) as dst_db:
                    src_db.backup(dst_db)
            except sqlite3.DatabaseError:
                shutil.copy2(source, destination)
        else:
            shutil.copy2(source, destination)
    write_json_atomic(
        backup / "snapshot.json",
        {
            "entries": entries,
            "b_state_inventory": _b_state_inventory(root),
        },
    )
    return backup


def restore_snapshot(state_root: Path, backup: Path) -> None:
    root = Path(state_root).resolve()
    backup_root = root / ".backup"
    requested_backup = Path(backup)
    if backup_root.is_symlink() or requested_backup.is_symlink():
        raise StateMigrationError(f"snapshot backup path must not be a symlink: {backup}")
    resolved_backup = requested_backup.resolve(strict=False)
    if resolved_backup.parent != backup_root.resolve(strict=False):
        raise StateMigrationError(f"snapshot backup escapes backup root: {backup}")
    try:
        receipt = json.loads((resolved_backup / "snapshot.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateMigrationError(f"invalid snapshot {backup}: {exc}") from exc
    entries = receipt.get("entries") or []
    if not isinstance(entries, list):
        raise StateMigrationError(f"invalid snapshot {backup}: entries must be an array")
    inventory = receipt.get("b_state_inventory")
    if inventory is not None:
        _validate_b_state_inventory(inventory)
    for entry in entries:
        if not isinstance(entry, dict):
            raise StateMigrationError(f"invalid snapshot {backup}: entry must be an object")
        rel = str(entry.get("path") or "")
        destination = (root / rel).resolve(strict=False)
        try:
            destination.relative_to(root)
        except ValueError as exc:
            raise StateMigrationError(f"snapshot entry escapes state root: {rel}") from exc
        source = resolved_backup / "files" / rel
        if entry.get("existed"):
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        elif destination.is_file() or destination.is_symlink():
            destination.unlink()
    if inventory is not None:
        _restore_b_state_inventory(root, inventory)


def _path_kind(path: Path) -> str:
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _b_state_inventory(root: Path) -> list[dict[str, str]]:
    inventory: list[dict[str, str]] = []

    def visit(path: Path) -> None:
        kind = _path_kind(path)
        inventory.append({"path": path.relative_to(root).as_posix(), "kind": kind})
        if kind == "directory":
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                visit(child)

    try:
        for name in _B_STATE_ROOTS:
            path = root / name
            if os.path.lexists(path):
                visit(path)
    except OSError as exc:
        raise StateMigrationError(f"cannot inventory B-class state under {root}: {exc}") from exc
    return inventory


def _validate_b_state_inventory(inventory: Any) -> dict[str, str]:
    if not isinstance(inventory, list):
        raise StateMigrationError("invalid snapshot: b_state_inventory must be an array")
    validated: dict[str, str] = {}
    for entry in inventory:
        if not isinstance(entry, dict):
            raise StateMigrationError("invalid snapshot: B-class inventory entry must be an object")
        rel = entry.get("path")
        kind = entry.get("kind")
        if not isinstance(rel, str) or not rel or rel.startswith("/"):
            raise StateMigrationError(f"invalid snapshot B-class path: {rel!r}")
        parts = Path(rel).parts
        if not parts or parts[0] not in _B_STATE_ROOTS or any(part in {"", ".", ".."} for part in parts):
            raise StateMigrationError(f"invalid snapshot B-class path: {rel!r}")
        if kind not in {"directory", "file", "symlink", "other"}:
            raise StateMigrationError(f"invalid snapshot B-class kind for {rel!r}: {kind!r}")
        if rel in validated:
            raise StateMigrationError(f"duplicate snapshot B-class path: {rel!r}")
        validated[rel] = kind
    return validated


def _restore_b_state_inventory(root: Path, inventory: list[dict[str, str]]) -> None:
    expected = _validate_b_state_inventory(inventory)
    current = {entry["path"]: entry["kind"] for entry in _b_state_inventory(root)}
    added = sorted(
        set(current) - set(expected),
        key=lambda rel: (len(Path(rel).parts), rel),
        reverse=True,
    )
    for rel in added:
        path = root.joinpath(*Path(rel).parts)
        try:
            if current[rel] == "directory":
                path.rmdir()
            else:
                path.unlink()
        except OSError as exc:
            raise StateMigrationError(f"cannot remove transaction-created B-class path {rel}: {exc}") from exc

    for rel, kind in sorted(expected.items(), key=lambda item: len(Path(item[0]).parts)):
        path = root.joinpath(*Path(rel).parts)
        if kind == "directory" and not os.path.lexists(path):
            path.mkdir()


def apply_migrations(
    *,
    target_schema: int = CURRENT_STATE_SCHEMA,
    version: str,
    code_root: Path = CODE_ROOT,
    state_root: Path = STATE_ROOT,
    migrations_dir: Path | None = None,
    legacy_root: Path | None = None,
    allow_irreversible: bool = False,
    transaction_id: str | None = None,
) -> dict[str, Any]:
    target_schema = int(target_schema)
    transaction_id = transaction_id or uuid.uuid4().hex
    with migration_lock(state_root):
        metadata = load_state_metadata(state_root)
        current = int(metadata["state_schema"])
        if current > target_schema:
            raise StateSchemaTooNew(
                f"state schema {current} is newer than release schema {target_schema}"
            )
        migrations = [
            item for item in discover_migrations(migrations_dir)
            if current < item.schema <= target_schema
        ]
        missing = list(range(current + 1, target_schema + 1))
        if [item.schema for item in migrations] != missing:
            raise StateMigrationError(
                f"missing migrations for schemas {missing}; found {[item.schema for item in migrations]}"
            )
        irreversible = [item.migration_id for item in migrations if not item.reversible]
        if irreversible and not allow_irreversible:
            raise IrreversibleMigrationRequired(
                "irreversible migrations require --allow-irreversible: " + ", ".join(irreversible)
            )
        if irreversible and metadata.get("last_version"):
            previous_major = _semver_major(str(metadata["last_version"]))
            target_major = _semver_major(version)
            if target_major <= previous_major:
                raise IrreversibleMigrationRequired(
                    "irreversible migrations require a MAJOR version increase: "
                    f"{metadata['last_version']} -> {version}"
                )
        if not migrations:
            return {"from_schema": current, "to_schema": current, "applied": [], "backup": None}
        backup = snapshot_metadata(Path(state_root), transaction_id=transaction_id)
        context = MigrationContext(Path(code_root), Path(state_root), legacy_root)
        applied: list[str] = []
        try:
            for migration in migrations:
                migration.module.forward(context)
                applied.append(migration.migration_id)
                metadata["state_schema"] = migration.schema
                metadata.setdefault("applied_migrations", []).append(migration.migration_id)
            write_json_atomic(state_file(state_root), metadata)
        except Exception as exc:
            restore_snapshot(Path(state_root), backup)
            raise StateMigrationError(
                f"state migration failed after {applied}: {type(exc).__name__}: {exc}"
            ) from exc
        return {
            "from_schema": current,
            "to_schema": target_schema,
            "applied": applied,
            "backup": str(backup),
        }


def _semver_major(value: str) -> int:
    match = re.fullmatch(r"v(0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", value)
    if match is None:
        raise StateMigrationError(f"invalid release version in state metadata: {value!r}")
    return int(match.group(1))


__all__ = [
    "CURRENT_STATE_SCHEMA",
    "IrreversibleMigrationRequired",
    "MigrationContext",
    "StateMigrationError",
    "StateSchemaTooNew",
    "apply_migrations",
    "discover_migrations",
    "load_state_metadata",
    "migration_lock",
    "restore_snapshot",
    "snapshot_metadata",
    "state_file",
]
