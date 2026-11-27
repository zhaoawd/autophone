"""Managed install, upgrade and rollback transactions for autophone releases."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, TextIO

from autophone.contracts import ContractError, validate_or_raise
from autophone.execution.release_lock import (
    RELEASE_LOCK_FD_ENV,
    ReleaseLockBusy,
    inherited_lock_pass_fds,
    release_lock,
    release_lock_path,
)
from autophone.application.state_migrations import (
    apply_migrations,
    load_state_metadata,
    restore_snapshot,
    snapshot_metadata,
    state_file,
)
from autophone.util import now_iso, write_json_atomic


_SEMVER = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_TRANSACTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_JOURNAL_SCHEMA = "release-transaction.v1"
_JOURNAL_STATES = {
    "created",
    "preflight_passed",
    "migrated",
    "state_restored",
    "current_switched",
    "smoke_passed",
    "committed",
    "rolled_back",
    "rollback_failed",
    "recovered",
    "recovery_failed",
}
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024


class ReleaseError(RuntimeError):
    pass


class ReleaseIntegrityError(ReleaseError):
    pass


class ReleaseCompatibilityError(ReleaseError):
    pass


@dataclass(frozen=True)
class ResolvedRelease:
    manifest: dict[str, Any]
    manifest_path: Path
    artifact_path: Path
    sha256: str


@dataclass(frozen=True)
class StagedRelease:
    version: str
    release_dir: Path
    manifest: dict[str, Any]
    reused: bool = False
    compatibility: dict[str, Any] | None = None


PrepareRunner = Callable[[Path, dict[str, str]], Any]
SmokeRunner = Callable[[Path, bool, dict[str, str]], Any]
CompatRunner = Callable[..., dict[str, Any]]


def _semver_key(value: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(value)
    if not match:
        raise ReleaseError(f"invalid release version: {value!r}")
    return tuple(int(part) for part in match.groups())


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepared_tree_receipt(release_dir: Path) -> dict[str, Any]:
    """Return a content fingerprint for the prepared release-local environment."""

    root = Path(release_dir) / ".venv"
    if root.is_symlink() or not root.is_dir():
        raise ReleaseIntegrityError(f"prepared environment must be a real directory: {root}")
    root_resolved = root.resolve()
    digest = hashlib.sha256()
    entries = 0
    total_bytes = 0

    def add(record: dict[str, Any]) -> None:
        nonlocal entries
        payload = json.dumps(
            record,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        entries += 1

    add({"path": ".", "kind": "directory", "mode": stat.S_IMODE(root.lstat().st_mode)})

    def visit(directory: Path) -> None:
        nonlocal total_bytes
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise ReleaseIntegrityError(f"cannot read prepared environment {directory}: {exc}") from exc
        for path in children:
            relative = path.relative_to(root).as_posix()
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise ReleaseIntegrityError(f"cannot stat prepared environment path {relative}: {exc}") from exc
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISDIR(metadata.st_mode):
                add({"path": relative, "kind": "directory", "mode": mode})
                visit(path)
                continue
            if stat.S_ISREG(metadata.st_mode):
                file_digest = _sha256(path)
                add({
                    "path": relative,
                    "kind": "file",
                    "mode": mode,
                    "size": metadata.st_size,
                    "sha256": file_digest,
                })
                total_bytes += metadata.st_size
                continue
            if stat.S_ISLNK(metadata.st_mode):
                try:
                    link = os.readlink(path)
                    resolved = path.resolve(strict=True)
                except OSError as exc:
                    raise ReleaseIntegrityError(
                        f"invalid prepared environment symlink {relative}: {exc}"
                    ) from exc
                record: dict[str, Any] = {
                    "path": relative,
                    "kind": "symlink",
                    "mode": mode,
                    "target": link,
                }
                if resolved.is_file():
                    target_metadata = resolved.stat()
                    record.update(
                        resolved_kind="file",
                        resolved_mode=stat.S_IMODE(target_metadata.st_mode),
                        resolved_size=target_metadata.st_size,
                        resolved_sha256=_sha256(resolved),
                    )
                    total_bytes += target_metadata.st_size
                elif resolved.is_dir():
                    try:
                        resolved.relative_to(root_resolved)
                    except ValueError as exc:
                        raise ReleaseIntegrityError(
                            f"prepared environment directory symlink escapes .venv: {relative}"
                        ) from exc
                    record["resolved_kind"] = "directory"
                else:
                    raise ReleaseIntegrityError(
                        f"prepared environment symlink has unsupported target: {relative}"
                    )
                add(record)
                continue
            raise ReleaseIntegrityError(
                f"prepared environment contains unsupported file type: {relative}"
            )

    visit(root)
    return {
        "algorithm": "sha256-tree-v1",
        "sha256": digest.hexdigest(),
        "entries": entries,
        "bytes": total_bytes,
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ReleaseError(f"invalid JSON {path}: expected object")
    return raw


def _validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    try:
        validate_or_raise("release-manifest.v1.json", manifest)
    except (ContractError, FileNotFoundError) as exc:
        raise ReleaseError(str(exc)) from exc
    version = str(manifest.get("version") or "")
    _semver_key(version)
    commit = str(manifest.get("commit") or "")
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit):
        raise ReleaseError("release manifest commit must be a 7-40 character hex SHA")
    schema = manifest.get("state_schema")
    if not isinstance(schema, int) or schema < 0:
        raise ReleaseError("release manifest state_schema must be a non-negative integer")
    if not isinstance(manifest.get("compat"), dict):
        raise ReleaseError("release manifest compat must be an object")
    return manifest


def _read_checksum(path: Path) -> str | None:
    try:
        token = path.read_text(encoding="utf-8").strip().split()[0].lower()
    except (FileNotFoundError, OSError, IndexError):
        return None
    return token if re.fullmatch(r"[0-9a-f]{64}", token) else None


def _is_url(value: str) -> bool:
    return urllib.parse.urlparse(value).scheme in {"http", "https", "file"}


def _fetch(ref: str, *, base: str | None, download_dir: Path) -> Path:
    joined = urllib.parse.urljoin(base, ref) if base and _is_url(base) else ref
    parsed = urllib.parse.urlparse(joined)
    if parsed.scheme == "file":
        return Path(urllib.request.url2pathname(parsed.path)).resolve()
    if parsed.scheme in {"http", "https"}:
        download_dir.mkdir(parents=True, exist_ok=True)
        name = Path(parsed.path).name or "download"
        destination = download_dir / f"{uuid.uuid4().hex}-{name}"
        try:
            with urllib.request.urlopen(joined, timeout=30) as response, destination.open("wb") as out:
                shutil.copyfileobj(response, out)
        except Exception as exc:
            destination.unlink(missing_ok=True)
            raise ReleaseError(f"download failed: {joined}: {exc}") from exc
        return destination
    path = Path(ref).expanduser()
    if not path.is_absolute() and base and not _is_url(base):
        path = Path(base).parent / path
    return path.resolve()


def resolve_release(
    source: str | Path,
    *,
    version: str | None = None,
    download_dir: Path | None = None,
) -> ResolvedRelease:
    downloads = Path(download_dir or tempfile.mkdtemp(prefix="autophone-release-"))
    source_text = str(source)
    parsed_input = urllib.parse.urlparse(source_text)
    if parsed_input.scheme in {"http", "https"} and parsed_input.path.endswith("/"):
        source_text = urllib.parse.urljoin(source_text, "version.json")
    source_path = _fetch(source_text, base=None, download_dir=downloads)
    source_base = source_text if _is_url(source_text) else str(source_path)
    source_name = Path(urllib.parse.urlparse(source_text).path).name
    direct_checksum_path: Path | None = None
    if source_path.is_dir():
        index_candidate = source_path / "index.json"
        source_path = index_candidate if index_candidate.is_file() else source_path / "version.json"
        source_base = str(source_path)
        source_name = source_path.name
    json_candidate: dict[str, Any] | None = None
    if source_name == "index.json" or source_path.suffix == ".json":
        json_candidate = _read_json(source_path)
    is_index = source_name == "index.json" or (
        json_candidate is not None
        and json_candidate.get("schema_version") == "release-index.v1"
    )
    if is_index:
        index = json_candidate or _read_json(source_path)
        try:
            validate_or_raise("release-index.v1.json", index)
        except (ContractError, FileNotFoundError) as exc:
            raise ReleaseError(str(exc)) from exc
        rows = index.get("releases") or []
        if not isinstance(rows, list) or not rows:
            raise ReleaseError(f"release index has no releases: {source_path}")
        selected: dict[str, Any] | None = None
        if version:
            selected = next((row for row in rows if isinstance(row, dict) and row.get("version") == version), None)
        else:
            valid = [row for row in rows if isinstance(row, dict) and _SEMVER.fullmatch(str(row.get("version") or ""))]
            selected = max(valid, key=lambda row: _semver_key(str(row["version"]))) if valid else None
        if selected is None:
            raise ReleaseError(f"release {version or '<latest>'} not found in {source_path}")
        manifest_ref = str(selected.get("manifest") or selected.get("manifest_url") or "")
        if not manifest_ref:
            raise ReleaseError("release index entry is missing manifest")
        base = source_text if _is_url(source_text) else str(source_path)
        manifest_path = _fetch(manifest_ref, base=base, download_dir=downloads)
        manifest = _validate_manifest(_read_json(manifest_path))
        artifact_ref = str(
            selected.get("archive")
            or selected.get("artifact")
            or (manifest.get("artifact") or {}).get("file")
            or f"autophone-{manifest['version']}.tar.gz"
        )
        artifact_path = _fetch(artifact_ref, base=base, download_dir=downloads)
        checksum = str(selected.get("sha256") or (manifest.get("artifact") or {}).get("sha256") or "")
        if selected.get("version") != manifest["version"]:
            raise ReleaseIntegrityError("release index and manifest versions disagree")
        if selected.get("commit") != manifest["commit"]:
            raise ReleaseIntegrityError("release index and manifest commits disagree")
    else:
        if source_path.suffix in {".gz", ".tar"} or source_path.name.endswith(".tar.gz"):
            artifact_path = source_path
            parsed_source = urllib.parse.urlparse(source_text)
            if parsed_source.scheme in {"http", "https"}:
                manifest_path = _fetch("version.json", base=source_text, download_dir=downloads)
                direct_checksum_path = _fetch(
                    f"{source_name}.sha256",
                    base=source_text,
                    download_dir=downloads,
                )
            else:
                manifest_path = source_path.parent / "version.json"
        else:
            manifest_path = source_path
            manifest = _validate_manifest(_read_json(manifest_path))
            artifact_ref = str(
                (manifest.get("artifact") or {}).get("file")
                or f"autophone-{manifest['version']}.tar.gz"
            )
            artifact_path = _fetch(artifact_ref, base=source_base, download_dir=downloads)
            parsed_source = urllib.parse.urlparse(source_text)
            if parsed_source.scheme in {"http", "https"}:
                direct_checksum_path = _fetch(
                    f"{artifact_ref}.sha256",
                    base=source_base,
                    download_dir=downloads,
                )
        manifest = _validate_manifest(_read_json(manifest_path))
        checksum = str((manifest.get("artifact") or {}).get("sha256") or "")
    if version and manifest["version"] != version:
        raise ReleaseIntegrityError(
            f"resolved manifest version {manifest['version']} does not match requested {version}"
        )
    if not artifact_path.is_file():
        raise ReleaseError(f"release artifact not found: {artifact_path}")
    if not re.fullmatch(r"[0-9a-f]{64}", checksum.lower()):
        checksum = (
            _read_checksum(direct_checksum_path)
            if direct_checksum_path is not None
            else _read_checksum(Path(str(artifact_path) + ".sha256"))
        ) or ""
    if not checksum:
        sibling = manifest_path.parent / f"{artifact_path.name}.sha256"
        checksum = _read_checksum(sibling) or ""
    if not checksum:
        raise ReleaseIntegrityError("release has no valid sha256 receipt")
    return ResolvedRelease(manifest, manifest_path, artifact_path, checksum.lower())


def safe_extract_release(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    top_levels: set[str] = set()
    try:
        with tarfile.open(archive, "r:*") as bundle:
            members = bundle.getmembers()
            if not members:
                raise ReleaseIntegrityError("release archive is empty")
            if len(members) > _MAX_ARCHIVE_MEMBERS:
                raise ReleaseIntegrityError("release archive has too many members")
            total_size = 0
            normalized_names: set[str] = set()
            for member in members:
                pure = PurePosixPath(member.name)
                if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
                    raise ReleaseIntegrityError(f"unsafe archive path: {member.name!r}")
                normalized = pure.as_posix().rstrip("/")
                if normalized in normalized_names:
                    raise ReleaseIntegrityError(f"duplicate archive path: {member.name!r}")
                normalized_names.add(normalized)
                if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                    raise ReleaseIntegrityError(f"unsupported archive member: {member.name!r}")
                if member.size < 0:
                    raise ReleaseIntegrityError(f"invalid archive member size: {member.name!r}")
                total_size += member.size
                if total_size > _MAX_ARCHIVE_BYTES:
                    raise ReleaseIntegrityError("release archive expands beyond the size limit")
                top_levels.add(pure.parts[0])
                target = (destination / Path(*pure.parts)).resolve(strict=False)
                try:
                    target.relative_to(destination.resolve())
                except ValueError as exc:
                    raise ReleaseIntegrityError(f"archive path escapes destination: {member.name!r}") from exc
            if len(top_levels) != 1:
                raise ReleaseIntegrityError(f"release archive must have one top-level directory: {sorted(top_levels)}")
            for member in members:
                target = destination / Path(*PurePosixPath(member.name).parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise ReleaseIntegrityError(f"cannot read archive member: {member.name}")
                with source, target.open("wb") as out:
                    shutil.copyfileobj(source, out)
                os.chmod(target, member.mode & 0o777)
    except ReleaseIntegrityError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseIntegrityError(f"cannot read release archive {archive}: {exc}") from exc
    return destination / next(iter(top_levels))


def _verify_archive_manifest(resolved: ResolvedRelease) -> None:
    with tempfile.TemporaryDirectory(prefix="autophone-release-check-") as temp:
        top = safe_extract_release(resolved.artifact_path, Path(temp) / "extract")
        internal = _validate_manifest(_read_json(top / "version.json"))
        if not _manifest_equivalent(resolved.manifest, internal):
            raise ReleaseIntegrityError("outer and archived version.json disagree")


def _archive_inventory(archive: Path) -> dict[str, dict[str, Any]]:
    inventory: dict[str, dict[str, Any]] = {}
    try:
        with tarfile.open(archive, "r:*") as bundle:
            members = bundle.getmembers()
            tops = {PurePosixPath(member.name).parts[0] for member in members if member.name}
            if len(tops) != 1:
                raise ReleaseIntegrityError("release archive must have one top-level directory")
            top = next(iter(tops))
            for member in members:
                pure = PurePosixPath(member.name)
                if not member.isfile():
                    continue
                relative = PurePosixPath(*pure.parts[1:]).as_posix()
                if not relative or relative in inventory:
                    raise ReleaseIntegrityError(f"duplicate archive file: {relative!r}")
                source = bundle.extractfile(member)
                if source is None:
                    raise ReleaseIntegrityError(f"cannot read archive member: {member.name}")
                digest = hashlib.sha256()
                with source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                inventory[relative] = {
                    "sha256": digest.hexdigest(),
                    "mode": member.mode & 0o777,
                    "size": member.size,
                    "top": top,
                }
    except ReleaseIntegrityError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseIntegrityError(f"cannot inventory release archive {archive}: {exc}") from exc
    return inventory


def _manifest_equivalent(external: dict[str, Any], internal: dict[str, Any]) -> bool:
    # Artifact hash/size may live only in the outer index because embedding a
    # tar hash into that same tar is circular.  Build identity and compat must
    # remain byte-equivalent in meaning.
    fields = ("version", "commit", "built_at", "state_schema", "compat")
    return all(external.get(field) == internal.get(field) for field in fields)


def _default_prepare(release_dir: Path, env: dict[str, str]) -> None:
    prepare_env = dict(env)
    prepare_env["UV_PROJECT_ENVIRONMENT"] = str(release_dir / ".venv")
    completed = subprocess.run(
        # Dependencies only: the release tree itself is the code, never a copy
        # inside .venv.  An editable install would bake in the pre-rename staging
        # path, and a real install would put a second, *partial* autophone on
        # sys.path -- the wheel carries only the autophone package, without
        # tools/, apps/ or migrations/ -- which silently resolves CODE_ROOT to
        # site-packages.  Callers run from the release tree instead.
        [
            "uv", "sync", "--frozen", "--no-install-project",
            "--group", "platform", "--group", "frida",
        ],
        cwd=release_dir,
        env=prepare_env,
        pass_fds=inherited_lock_pass_fds(prepare_env),
        check=False,
    )
    if completed.returncode != 0:
        raise ReleaseError(f"dependency preparation failed with exit {completed.returncode}")


def _default_smoke(release_dir: Path, full: bool, env: dict[str, str]) -> list[dict[str, Any]]:
    python = release_dir / ".venv" / "bin" / "python"
    commands = [
        [str(python), "-m", "autophone.cli", "doctor"],
        [str(python), "-m", "tools.phase1.shell_smoke"],
    ]
    if full:
        configured = str(env.get("AUTOPHONE_FULL_SMOKE_CMD") or "").strip()
        if not configured:
            raise ReleaseError("--full requires AUTOPHONE_FULL_SMOKE_CMD")
        commands.append(shlex.split(configured))
    receipts: list[dict[str, Any]] = []
    for command in commands:
        completed = subprocess.run(
            command,
            cwd=release_dir,
            env=env,
            pass_fds=inherited_lock_pass_fds(env),
            check=False,
        )
        receipts.append({"command": command, "returncode": completed.returncode})
        if completed.returncode != 0:
            raise ReleaseError(f"post-install smoke failed: {command[0]} exit={completed.returncode}")
    return receipts


class ReleaseManager:
    def __init__(
        self,
        install_root: str | Path,
        *,
        prepare_runner: PrepareRunner | None = None,
        smoke_runner: SmokeRunner | None = None,
        compat_runner: CompatRunner | None = None,
        keep_releases: int = 3,
    ) -> None:
        requested_root = Path(install_root).expanduser()
        if requested_root.is_symlink():
            raise ReleaseError(f"install root must not be a symlink: {requested_root}")
        self.install_root = requested_root.resolve()
        if self.install_root in {Path("/"), Path.home().resolve()}:
            raise ReleaseError(f"refusing broad install root: {self.install_root}")
        self.releases_root = self.install_root / "releases"
        self.current = self.install_root / "current"
        self.state_root = self.install_root / "state"
        self._uses_default_prepare = prepare_runner is None
        self.prepare_runner = prepare_runner or _default_prepare
        self.smoke_runner = smoke_runner or _default_smoke
        self.compat_runner = compat_runner
        self.keep_releases = max(2, int(keep_releases))
        self._release_lock_fd: int | None = None

    @property
    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        for name in ("UV_PROJECT", "UV_PROJECT_ENVIRONMENT", "UV_WORKING_DIR", "VIRTUAL_ENV"):
            env.pop(name, None)
        env.pop("AUTOPHONE_RELEASE_LOCK_HELD", None)
        env.pop(RELEASE_LOCK_FD_ENV, None)
        env.update({
            "AUTOPHONE_STATE_DIR": str(self.state_root),
            "AUTOPHONE_READONLY_ASSETS": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        if self._release_lock_fd is not None:
            env[RELEASE_LOCK_FD_ENV] = str(self._release_lock_fd)
        return env

    @contextmanager
    def _exclusive_release_lock(self) -> Iterator[TextIO]:
        with release_lock(self.state_root, exclusive=True, blocking=False) as handle:
            previous = self._release_lock_fd
            self._release_lock_fd = handle.fileno()
            try:
                yield handle
            finally:
                self._release_lock_fd = previous

    def status(self) -> dict[str, Any]:
        try:
            self._ensure_managed_layout()
            lock_path = release_lock_path(self.state_root)
            if not lock_path.exists():
                snapshot = self._status_snapshot()
                if not lock_path.exists():
                    return snapshot
            with release_lock(self.state_root, exclusive=False, blocking=False):
                return self._status_snapshot()
        except ReleaseLockBusy as exc:
            raise ReleaseError(str(exc)) from exc
        except ReleaseError:
            raise
        except Exception as exc:
            raise ReleaseError(f"release status failed: {type(exc).__name__}: {exc}") from exc

    def _status_snapshot(self) -> dict[str, Any]:
        current = self.current_version()
        releases = self._installed_versions()
        metadata = load_state_metadata(self.state_root)
        return {
            "install_root": str(self.install_root),
            "current": current,
            "previous": metadata.get("previous_version"),
            "releases": releases,
            "state": metadata,
            "needs_recovery": self._incomplete_transactions(),
        }

    def current_version(self) -> str | None:
        if not self.current.is_symlink():
            if self.current.exists():
                raise ReleaseError(f"current must be a symlink: {self.current}")
            return None
        target = self.current.resolve(strict=False)
        releases = self.releases_root.resolve()
        if (
            target.parent != releases
            or not target.is_dir()
            or _SEMVER.fullmatch(target.name) is None
        ):
            raise ReleaseError(
                f"current symlink must target a releases/<SemVer> directory: {target}"
            )
        return target.name

    def check(self, source: str | Path, *, version: str | None = None, level: str = "stage") -> dict[str, Any]:
        try:
            self._ensure_managed_layout()
            with release_lock(self.state_root, exclusive=False, blocking=False):
                with tempfile.TemporaryDirectory(prefix="autophone-release-check-") as temporary:
                    resolved = resolve_release(
                        source,
                        version=version,
                        download_dir=Path(temporary),
                    )
                    actual = _sha256(resolved.artifact_path)
                    if actual != resolved.sha256:
                        raise ReleaseIntegrityError(
                            f"sha256 mismatch: expected {resolved.sha256}, got {actual}"
                        )
                    _verify_archive_manifest(resolved)
                    compat = self._compat(resolved.manifest, level=level)
                    return {
                        "version": resolved.manifest["version"],
                        "artifact": str(source),
                        "sha256": actual,
                        "compatibility": compat,
                    }
        except ReleaseLockBusy as exc:
            raise ReleaseError(str(exc)) from exc
        except ReleaseError:
            raise
        except Exception as exc:
            raise ReleaseError(f"release check failed: {type(exc).__name__}: {exc}") from exc

    def stage(self, source: str | Path, *, version: str | None = None) -> StagedRelease:
        self._ensure_managed_layout()
        self.install_root.mkdir(parents=True, exist_ok=True)
        self.state_root.mkdir(parents=True, exist_ok=True)
        try:
            with self._exclusive_release_lock():
                self._assert_no_incomplete_transaction()
                return self._stage_locked(source, version=version)
        except ReleaseLockBusy as exc:
            raise ReleaseError(str(exc)) from exc
        except ReleaseError:
            raise
        except Exception as exc:
            raise ReleaseError(f"release stage failed: {type(exc).__name__}: {exc}") from exc

    def _stage_locked(self, source: str | Path, *, version: str | None) -> StagedRelease:
        download_root = self.state_root / ".release-downloads"
        if download_root.is_symlink():
            raise ReleaseIntegrityError(
                f"release download directory must not be a symlink: {download_root}"
            )
        download_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="stage-", dir=download_root) as temporary:
            resolved = resolve_release(
                source,
                version=version,
                download_dir=Path(temporary),
            )
            return self._stage_resolved_release(resolved)

    def _stage_resolved_release(self, resolved: ResolvedRelease) -> StagedRelease:
        actual = _sha256(resolved.artifact_path)
        if actual != resolved.sha256:
            raise ReleaseIntegrityError(f"sha256 mismatch: expected {resolved.sha256}, got {actual}")
        manifest = resolved.manifest
        version_name = str(manifest["version"])
        final = self.releases_root / version_name
        if final.is_symlink():
            raise ReleaseIntegrityError(f"release directory must not be a symlink: {final}")
        if final.is_dir():
            existing = _validate_manifest(_read_json(final / "version.json"))
            if not _manifest_equivalent(manifest, existing):
                raise ReleaseIntegrityError(f"installed release {version_name} has a different manifest")
            self._verify_staged_release(
                final,
                existing,
                expected_artifact_sha256=resolved.sha256,
            )
            compatibility = self._compat(existing, level="stage", release_dir=final)
            return StagedRelease(
                version_name,
                final,
                existing,
                reused=True,
                compatibility=compatibility,
            )
        self.releases_root.mkdir(parents=True, exist_ok=True)
        transaction_id = uuid.uuid4().hex
        extraction = self.releases_root / f".extract-{version_name}-{transaction_id}"
        staging = self.releases_root / f".staging-{version_name}-{transaction_id}"
        try:
            top = safe_extract_release(resolved.artifact_path, extraction)
            internal_path = top / "version.json"
            internal = _validate_manifest(_read_json(internal_path))
            if not _manifest_equivalent(manifest, internal):
                raise ReleaseIntegrityError("outer and archived version.json disagree")
            os.replace(top, staging)
            shutil.rmtree(extraction, ignore_errors=True)
            self.prepare_runner(staging, self.env)
            self._store_artifact_receipt(resolved, release_dir=staging)
            self._verify_staged_release(staging, manifest, require_final=False)
            compatibility = self._compat(manifest, level="stage", release_dir=staging)
            os.replace(staging, final)
        except Exception:
            shutil.rmtree(extraction, ignore_errors=True)
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return StagedRelease(
            version_name,
            final,
            manifest,
            compatibility=compatibility,
        )

    def apply(
        self,
        *,
        source: str | Path | None = None,
        version: str | None = None,
        allow_irreversible: bool = False,
        full: bool = False,
        legacy_root: Path | None = None,
    ) -> dict[str, Any]:
        self._ensure_managed_layout()
        self.install_root.mkdir(parents=True, exist_ok=True)
        self.state_root.mkdir(parents=True, exist_ok=True)
        try:
            with self._exclusive_release_lock():
                self._assert_no_incomplete_transaction()
                staged = self._stage_locked(source, version=version) if source is not None else self._existing(version)
                return self._apply_locked(
                    staged,
                    allow_irreversible=allow_irreversible,
                    full=full,
                    legacy_root=legacy_root,
                )
        except ReleaseLockBusy as exc:
            raise ReleaseError(str(exc)) from exc
        except ReleaseError:
            raise
        except Exception as exc:
            raise ReleaseError(f"release apply failed: {type(exc).__name__}: {exc}") from exc

    def _apply_locked(
        self,
        staged: StagedRelease,
        *,
        allow_irreversible: bool,
        full: bool,
        legacy_root: Path | None,
    ) -> dict[str, Any]:
        manifest = staged.manifest
        compat = self._compat(manifest, level="apply", release_dir=staged.release_dir)
        if compat.get("ok") is not True:
            raise ReleaseCompatibilityError(json.dumps(compat, ensure_ascii=False))
        old_version = self.current_version()
        if legacy_root is not None:
            self._validate_legacy_adoption(Path(legacy_root), current_version=old_version)
        if old_version == staged.version:
            return {"status": "already_current", "version": staged.version, "compatibility": compat}
        if old_version is not None and _semver_key(staged.version) < _semver_key(old_version):
            raise ReleaseError(
                f"self-update apply does not downgrade {old_version}->{staged.version}; use rollback"
            )
        transaction_id = f"{old_version or 'none'}--{staged.version}--{uuid.uuid4().hex[:12]}"
        guard = snapshot_metadata(
            self.state_root,
            transaction_id=f"{transaction_id}-guard",
        )
        journal = {
            "schema_version": _JOURNAL_SCHEMA,
            "transaction_id": transaction_id,
            "kind": "apply",
            "from_version": old_version,
            "to_version": staged.version,
            "created_at": now_iso(),
            "state": "created",
            "guard_backup": self._relative_state_path(guard),
        }
        previous_target = self.current.resolve(strict=False) if self.current.is_symlink() else None
        wrapper_before = self._capture_wrapper()
        migration: dict[str, Any] | None = None
        switched = False
        try:
            self._write_journal(journal)
            journal["state"] = "preflight_passed"
            self._write_journal(journal)
            migration = apply_migrations(
                target_schema=int(manifest["state_schema"]),
                version=staged.version,
                code_root=staged.release_dir,
                state_root=self.state_root,
                migrations_dir=staged.release_dir / "migrations",
                legacy_root=legacy_root,
                allow_irreversible=allow_irreversible,
                transaction_id=transaction_id,
            )
            journal.update(state="migrated", migration=self._migration_for_journal(migration))
            self._write_journal(journal)
            self._switch_current(staged.release_dir)
            switched = True
            journal["state"] = "current_switched"
            self._write_journal(journal)
            smoke = self.smoke_runner(staged.release_dir, full, self.env)
            self._write_wrapper()
            journal.update(state="smoke_passed", smoke=smoke)
            self._write_journal(journal)
            self._record_success(staged.version, old_version, transaction_id)
            journal.update(state="committed")
            self._write_journal(journal)
            removed: list[str] = []
            cleanup_warning = None
            try:
                removed = self._prune_releases()
                journal["removed_releases"] = removed
                self._write_journal(journal)
            except Exception as cleanup_exc:
                cleanup_warning = f"{type(cleanup_exc).__name__}: {cleanup_exc}"
            return {
                "status": "updated",
                "from_version": old_version,
                "version": staged.version,
                "migration": migration,
                "compatibility": compat,
                "smoke": smoke,
                "removed_releases": removed,
                "cleanup_warning": cleanup_warning,
                "transaction_id": transaction_id,
            }
        except Exception as exc:
            rollback_errors: list[str] = []
            if switched:
                try:
                    self._restore_current(previous_target)
                except Exception as rollback_exc:
                    rollback_errors.append(f"current: {type(rollback_exc).__name__}: {rollback_exc}")
            try:
                restore_snapshot(self.state_root, guard)
            except Exception as rollback_exc:
                rollback_errors.append(f"state: {type(rollback_exc).__name__}: {rollback_exc}")
            try:
                self._restore_wrapper(wrapper_before)
            except Exception as rollback_exc:
                rollback_errors.append(f"wrapper: {type(rollback_exc).__name__}: {rollback_exc}")
            journal.update(
                state="rollback_failed" if rollback_errors else "rolled_back",
                error=f"{type(exc).__name__}: {exc}",
                rollback_errors=rollback_errors,
            )
            try:
                self._write_journal(journal)
            except Exception as rollback_exc:
                rollback_errors.append(f"journal: {type(rollback_exc).__name__}: {rollback_exc}")
            if rollback_errors:
                raise ReleaseError(
                    f"release apply failed and rollback was incomplete: {rollback_errors}"
                ) from exc
            raise

    def rollback(
        self,
        *,
        to_version: str | None = None,
        restore_state_snapshot: bool = False,
        full: bool = False,
    ) -> dict[str, Any]:
        self._ensure_managed_layout()
        try:
            with self._exclusive_release_lock():
                self._assert_no_incomplete_transaction()
                current = self.current_version()
                if current is None:
                    raise ReleaseError("no current release")
                metadata = load_state_metadata(self.state_root)
                target_version = to_version or metadata.get("previous_version")
                if not target_version:
                    older = [value for value in self._installed_versions() if _semver_key(value) < _semver_key(current)]
                    target_version = max(older, key=_semver_key) if older else None
                if not target_version:
                    raise ReleaseError("no previous release is available")
                if _semver_key(str(target_version)) >= _semver_key(current):
                    raise ReleaseError(
                        f"rollback target must be older than current: {current}->{target_version}"
                    )
                target = self._existing(str(target_version))
                target_schema = int(target.manifest["state_schema"])
                current_schema = int(metadata.get("state_schema") or 0)
                if target_schema != current_schema and not restore_state_snapshot:
                    raise ReleaseError(
                        f"rollback state schema mismatch {current_schema}->{target_schema}; "
                        "use --restore-snapshot explicitly"
                    )
                compat = self._compat(
                    target.manifest,
                    level="apply",
                    release_dir=target.release_dir,
                    state_schema=target_schema if restore_state_snapshot else current_schema,
                )
                if compat.get("ok") is not True:
                    raise ReleaseCompatibilityError(json.dumps(compat, ensure_ascii=False))
                old_target = self.current.resolve()
                restored = None
                transaction_id = f"rollback-{current}--{target_version}--{uuid.uuid4().hex[:12]}"
                rollback_guard = snapshot_metadata(
                    self.state_root,
                    transaction_id=f"{transaction_id}-guard",
                )
                wrapper_before = self._capture_wrapper()
                if restore_state_snapshot:
                    restored = self._snapshot_for_transition(str(target_version), current)
                    if restored is None:
                        raise ReleaseError(f"no state snapshot for rollback {target_version}->{current}")
                journal = {
                    "schema_version": _JOURNAL_SCHEMA,
                    "transaction_id": transaction_id,
                    "kind": "rollback",
                    "from_version": current,
                    "to_version": str(target_version),
                    "created_at": now_iso(),
                    "state": "created",
                    "guard_backup": self._relative_state_path(rollback_guard),
                    "state_snapshot": self._relative_state_path(restored) if restored else None,
                }
                switched = False
                try:
                    self._write_journal(journal)
                    if restored is not None:
                        restore_snapshot(self.state_root, restored)
                        journal["state"] = "state_restored"
                        self._write_journal(journal)
                    self._switch_current(target.release_dir)
                    switched = True
                    journal["state"] = "current_switched"
                    self._write_journal(journal)
                    smoke = self.smoke_runner(target.release_dir, full, self.env)
                    self._write_wrapper()
                    journal.update(state="smoke_passed", smoke=smoke)
                    self._write_journal(journal)
                    self._record_success(str(target_version), current, transaction_id)
                    journal["state"] = "committed"
                    self._write_journal(journal)
                    return {
                        "status": "rolled_back",
                        "from_version": current,
                        "version": target_version,
                        "state_snapshot": str(restored) if restored else None,
                        "compatibility": compat,
                        "smoke": smoke,
                        "transaction_id": transaction_id,
                    }
                except Exception as exc:
                    rollback_errors: list[str] = []
                    if switched:
                        try:
                            self._switch_current(old_target)
                        except Exception as rollback_exc:
                            rollback_errors.append(
                                f"current: {type(rollback_exc).__name__}: {rollback_exc}"
                            )
                    try:
                        restore_snapshot(self.state_root, rollback_guard)
                    except Exception as rollback_exc:
                        rollback_errors.append(
                            f"state: {type(rollback_exc).__name__}: {rollback_exc}"
                        )
                    try:
                        self._restore_wrapper(wrapper_before)
                    except Exception as rollback_exc:
                        rollback_errors.append(
                            f"wrapper: {type(rollback_exc).__name__}: {rollback_exc}"
                        )
                    journal.update(
                        state="rollback_failed" if rollback_errors else "rolled_back",
                        error=f"{type(exc).__name__}: {exc}",
                        rollback_errors=rollback_errors,
                    )
                    try:
                        self._write_journal(journal)
                    except Exception as rollback_exc:
                        rollback_errors.append(
                            f"journal: {type(rollback_exc).__name__}: {rollback_exc}"
                        )
                    if rollback_errors:
                        raise ReleaseError(
                            f"release rollback failed and recovery was incomplete: {rollback_errors}"
                        ) from exc
                    raise
        except ReleaseLockBusy as exc:
            raise ReleaseError(str(exc)) from exc
        except ReleaseError:
            raise
        except Exception as exc:
            raise ReleaseError(f"release rollback failed: {type(exc).__name__}: {exc}") from exc

    def recover(self) -> dict[str, Any]:
        """Restore the pre-transaction current/state after an interrupted update."""
        self._ensure_managed_layout()
        self.install_root.mkdir(parents=True, exist_ok=True)
        self.state_root.mkdir(parents=True, exist_ok=True)
        try:
            with self._exclusive_release_lock():
                pending = self._incomplete_transaction_documents()
                if not pending:
                    return {"status": "clean", "recovered": []}
                if len(pending) != 1:
                    raise ReleaseError(
                        "multiple unfinished release transactions require manual inspection: "
                        + json.dumps([doc.get("transaction_id") for _, doc in pending])
                    )
                path, journal = pending[0]
                guard_ref = str(journal.get("guard_backup") or "")
                if not guard_ref:
                    raise ReleaseError(f"transaction has no guard snapshot: {path}")
                guard = self._journal_backup_path(
                    guard_ref,
                    field="guard_backup",
                    expected_name=f"{journal['transaction_id']}-guard",
                )
                from_version = journal.get("from_version")
                if from_version is None:
                    previous_target = None
                else:
                    previous_target = self._existing(str(from_version)).release_dir
                errors: list[str] = []
                try:
                    self._restore_current(previous_target)
                except Exception as exc:
                    errors.append(f"current: {type(exc).__name__}: {exc}")
                try:
                    restore_snapshot(self.state_root, guard)
                except Exception as exc:
                    errors.append(f"state: {type(exc).__name__}: {exc}")
                try:
                    if previous_target is None:
                        self._restore_wrapper(None)
                    else:
                        self._write_wrapper()
                except Exception as exc:
                    errors.append(f"wrapper: {type(exc).__name__}: {exc}")
                journal.update(
                    state="recovery_failed" if errors else "recovered",
                    recovered_at=now_iso(),
                    recovery_errors=errors,
                )
                self._write_journal(journal)
                if errors:
                    raise ReleaseError(f"release recovery was incomplete: {errors}")
                return {
                    "status": "recovered",
                    "recovered": [journal["transaction_id"]],
                    "current": from_version,
                }
        except ReleaseLockBusy as exc:
            raise ReleaseError(str(exc)) from exc
        except ReleaseError:
            raise
        except Exception as exc:
            raise ReleaseError(f"release recovery failed: {type(exc).__name__}: {exc}") from exc

    def _compat(
        self,
        manifest: dict[str, Any],
        *,
        level: str,
        release_dir: Path | None = None,
        state_schema: int | None = None,
    ) -> dict[str, Any]:
        runner = self.compat_runner
        if runner is None:
            from autophone.application.release_compat import check_release_compatibility

            runner = check_release_compatibility
            actual_state_schema = (
                int(load_state_metadata(self.state_root)["state_schema"])
                if state_schema is None and self.state_root.exists()
                else int(state_schema or 0)
            )
            kwargs: dict[str, Any] = {
                "level": level,
                "state_schema": actual_state_schema,
                "python_source": "manager",
            }
            if release_dir is not None:
                kwargs["python_version_probe"] = lambda: self._staged_python_version(
                    release_dir
                )
                kwargs["python_source"] = "staged"
                kwargs["host_frida_probe"] = lambda: self._staged_frida_version(release_dir)
            return runner(manifest, **kwargs)
        actual_state_schema = (
            int(load_state_metadata(self.state_root)["state_schema"])
            if state_schema is None and self.state_root.exists()
            else int(state_schema or 0)
        )
        return runner(manifest, level=level, state_schema=actual_state_schema)

    def _run_staged_python_probe(
        self,
        release_dir: Path,
        code: str,
        *,
        description: str,
    ) -> str:
        python = Path(release_dir) / ".venv" / "bin" / "python"
        env = self.env
        try:
            completed = subprocess.run(
                [str(python), "-c", code],
                cwd=release_dir,
                env=env,
                pass_fds=inherited_lock_pass_fds(env),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseError(f"cannot execute staged Python {python}: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise ReleaseError(
                f"cannot read staged {description}: exit={completed.returncode} {detail}"
            )
        return completed.stdout.strip()

    def _staged_python_version(self, release_dir: Path) -> str:
        return self._run_staged_python_probe(
            release_dir,
            "import sys; print('.'.join(str(part) for part in sys.version_info[:3]))",
            description="Python version",
        )

    def _staged_frida_version(self, release_dir: Path) -> str:
        return self._run_staged_python_probe(
            release_dir,
            "import importlib.metadata; print(importlib.metadata.version('frida'))",
            description="host Frida version",
        )

    def _store_artifact_receipt(
        self,
        resolved: ResolvedRelease,
        *,
        release_dir: Path,
    ) -> None:
        version = str(resolved.manifest["version"])
        artifact_dir = self.state_root / ".release-artifacts" / version
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact = artifact_dir / f"autophone-{version}.tar.gz"
        if artifact.exists():
            if not artifact.is_file() or artifact.is_symlink() or _sha256(artifact) != resolved.sha256:
                raise ReleaseIntegrityError(f"stored release artifact differs for {version}")
        else:
            temp = artifact.with_name(f".{artifact.name}.{uuid.uuid4().hex}.tmp")
            shutil.copy2(resolved.artifact_path, temp)
            if _sha256(temp) != resolved.sha256:
                temp.unlink(missing_ok=True)
                raise ReleaseIntegrityError(f"stored release artifact copy failed for {version}")
            os.replace(temp, artifact)
        receipt = {
            "schema_version": "release-receipt.v1",
            "version": version,
            "commit": resolved.manifest["commit"],
            "artifact": self._relative_state_path(artifact),
            "sha256": resolved.sha256,
            "prepared_tree": (
                _prepared_tree_receipt(release_dir)
                if os.path.lexists(Path(release_dir) / ".venv")
                else None
            ),
        }
        write_json_atomic(self._receipt_path(version), receipt)

    def _verify_staged_release(
        self,
        release_dir: Path,
        manifest: dict[str, Any],
        *,
        require_final: bool = True,
        expected_artifact_sha256: str | None = None,
    ) -> None:
        version = str(manifest["version"])
        if require_final:
            self._assert_release_directory(release_dir, version=version)
        elif (
            release_dir.is_symlink()
            or not release_dir.is_dir()
            or release_dir.parent.resolve() != self.releases_root.resolve()
        ):
            raise ReleaseIntegrityError(f"invalid staging directory: {release_dir}")
        receipt_path = self._receipt_path(version)
        if not receipt_path.is_file() or receipt_path.is_symlink():
            raise ReleaseIntegrityError(f"staged release has no trusted receipt: {version}")
        receipt = _read_json(receipt_path)
        if (
            receipt.get("schema_version") != "release-receipt.v1"
            or receipt.get("version") != version
            or receipt.get("commit") != manifest.get("commit")
            or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("sha256") or ""))
            or (
                expected_artifact_sha256 is not None
                and receipt.get("sha256") != expected_artifact_sha256
            )
        ):
            raise ReleaseIntegrityError(f"invalid staged release receipt: {version}")
        artifact = self._state_relative_path(str(receipt.get("artifact") or ""))
        expected_artifact_root = (self.state_root / ".release-artifacts" / version).resolve()
        try:
            artifact.resolve().relative_to(expected_artifact_root)
        except ValueError as exc:
            raise ReleaseIntegrityError(f"release receipt artifact escapes its version directory: {artifact}") from exc
        if not artifact.is_file() or artifact.is_symlink():
            raise ReleaseIntegrityError(f"staged release artifact is missing: {artifact}")
        if _sha256(artifact) != receipt["sha256"]:
            raise ReleaseIntegrityError(f"staged release artifact hash changed: {version}")
        inventory = _archive_inventory(artifact)
        for relative, expected in inventory.items():
            path = release_dir / relative
            cursor = path.parent
            while cursor != release_dir:
                if cursor.is_symlink():
                    raise ReleaseIntegrityError(f"staged release contains a directory symlink: {cursor}")
                cursor = cursor.parent
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise ReleaseIntegrityError(f"staged release file is missing: {relative}") from exc
            if not stat.S_ISREG(metadata.st_mode):
                raise ReleaseIntegrityError(f"staged release file is not regular: {relative}")
            if metadata.st_size != expected["size"] or _sha256(path) != expected["sha256"]:
                raise ReleaseIntegrityError(f"staged release file changed: {relative}")
            if stat.S_IMODE(metadata.st_mode) != expected["mode"]:
                raise ReleaseIntegrityError(f"staged release mode changed: {relative}")
        archived = set(inventory)
        for path in release_dir.rglob("*"):
            relative_path = path.relative_to(release_dir)
            if relative_path.parts and relative_path.parts[0] == ".venv":
                continue
            if path.is_dir() and not path.is_symlink():
                continue
            relative = relative_path.as_posix()
            if relative not in archived:
                raise ReleaseIntegrityError(f"staged release has an unverified file: {relative}")
        prepared = receipt.get("prepared_tree")
        venv = Path(release_dir) / ".venv"
        if os.path.lexists(venv):
            if not isinstance(prepared, dict):
                raise ReleaseIntegrityError(
                    f"staged release has no prepared environment receipt: {version}"
                )
            actual_prepared = _prepared_tree_receipt(release_dir)
            if prepared != actual_prepared:
                raise ReleaseIntegrityError(
                    f"staged release prepared environment changed: {version}"
                )
        elif prepared is not None:
            raise ReleaseIntegrityError(
                f"staged release prepared environment is missing: {version}"
            )
        if self._uses_default_prepare:
            python = venv / "bin" / "python"
            if not python.is_file() or not os.access(python, os.X_OK):
                raise ReleaseIntegrityError(f"staged release Python is missing or not executable: {python}")

    def _receipt_path(self, version: str) -> Path:
        return self.state_root / ".release-receipts" / f"{version}.json"

    def _assert_release_directory(self, path: Path, *, version: str) -> None:
        if path.is_symlink() or not path.is_dir():
            raise ReleaseIntegrityError(f"release directory must be a real directory: {path}")
        if path.parent.resolve() != self.releases_root.resolve() or path.name != version:
            raise ReleaseIntegrityError(f"release directory is outside releases/: {path}")

    def _relative_state_path(self, path: Path | None) -> str | None:
        if path is None:
            return None
        try:
            return Path(path).resolve().relative_to(self.state_root.resolve()).as_posix()
        except ValueError as exc:
            raise ReleaseError(f"state path escapes state root: {path}") from exc

    def _state_relative_path(self, relative: str) -> Path:
        pure = PurePosixPath(relative)
        if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
            raise ReleaseIntegrityError(f"invalid state-relative path: {relative!r}")
        path = (self.state_root / Path(*pure.parts)).resolve(strict=False)
        try:
            path.relative_to(self.state_root.resolve())
        except ValueError as exc:
            raise ReleaseIntegrityError(f"state-relative path escapes root: {relative!r}") from exc
        return path

    def _journal_backup_path(
        self,
        relative: Any,
        *,
        field: str,
        expected_name: str | None = None,
    ) -> Path:
        if not isinstance(relative, str):
            raise ReleaseIntegrityError(f"journal {field} must be a state-relative path")
        pure = PurePosixPath(relative)
        if (
            len(pure.parts) != 2
            or pure.parts[0] != ".backup"
            or not _TRANSACTION_ID.fullmatch(pure.parts[1])
        ):
            raise ReleaseIntegrityError(
                f"journal {field} must be a state-relative path under .backup/: {relative!r}"
            )
        if expected_name is not None and pure.parts[1] != expected_name:
            raise ReleaseIntegrityError(
                f"journal {field} does not match transaction_id: {relative!r}"
            )
        backup_root = self.state_root / ".backup"
        if backup_root.is_symlink():
            raise ReleaseIntegrityError(f"state backup directory must not be a symlink: {backup_root}")
        lexical = backup_root / pure.parts[1]
        if lexical.is_symlink() or not lexical.is_dir():
            raise ReleaseIntegrityError(f"journal {field} must reference a real backup directory")
        path = self._state_relative_path(relative)
        if path.parent != backup_root.resolve(strict=False):
            raise ReleaseIntegrityError(f"journal {field} escapes .backup/: {relative!r}")
        return path

    def _migration_for_journal(self, migration: dict[str, Any]) -> dict[str, Any]:
        stored = dict(migration)
        backup = stored.get("backup")
        if backup:
            stored["backup"] = self._relative_state_path(Path(str(backup)))
        return stored

    def _journal_directory(self, *, create: bool = False) -> Path:
        journals = self.state_root / ".release-transactions"
        if journals.is_symlink():
            raise ReleaseIntegrityError(f"release journal directory must not be a symlink: {journals}")
        if journals.exists() and not journals.is_dir():
            raise ReleaseIntegrityError(f"release journal path must be a directory: {journals}")
        if create:
            journals.mkdir(parents=True, exist_ok=True)
        return journals

    def _validate_journal(
        self,
        document: Any,
        *,
        path: Path | None = None,
    ) -> dict[str, Any]:
        if not isinstance(document, dict):
            raise ReleaseIntegrityError("release transaction journal must be an object")
        allowed = {
            "schema_version",
            "transaction_id",
            "kind",
            "from_version",
            "to_version",
            "created_at",
            "state",
            "guard_backup",
            "state_snapshot",
            "migration",
            "smoke",
            "error",
            "rollback_errors",
            "removed_releases",
            "recovered_at",
            "recovery_errors",
        }
        unknown = sorted(set(document) - allowed)
        if unknown:
            raise ReleaseIntegrityError(f"release transaction journal has unknown fields: {unknown}")
        if document.get("schema_version") != _JOURNAL_SCHEMA:
            raise ReleaseIntegrityError(
                f"invalid release transaction schema_version: {document.get('schema_version')!r}"
            )
        transaction_id = document.get("transaction_id")
        if not isinstance(transaction_id, str) or not _TRANSACTION_ID.fullmatch(transaction_id):
            raise ReleaseIntegrityError(f"invalid journal transaction_id: {transaction_id!r}")
        if path is not None:
            journals = self._journal_directory()
            if path.parent != journals or path.name != f"{transaction_id}.json":
                raise ReleaseIntegrityError(
                    f"journal transaction_id does not match filename: {path.name!r}"
                )
            if path.is_symlink() or not path.is_file():
                raise ReleaseIntegrityError(f"release journal must be a regular file: {path}")
        kind = document.get("kind")
        if kind not in {"apply", "rollback"}:
            raise ReleaseIntegrityError(f"invalid journal kind: {kind!r}")
        from_version = document.get("from_version")
        if from_version is not None and (
            not isinstance(from_version, str) or _SEMVER.fullmatch(from_version) is None
        ):
            raise ReleaseIntegrityError(f"invalid journal from_version: {from_version!r}")
        if kind == "rollback" and from_version is None:
            raise ReleaseIntegrityError("rollback journal requires from_version")
        to_version = document.get("to_version")
        if not isinstance(to_version, str) or _SEMVER.fullmatch(to_version) is None:
            raise ReleaseIntegrityError(f"invalid journal to_version: {to_version!r}")
        created_at = document.get("created_at")
        if not isinstance(created_at, str) or not created_at.strip():
            raise ReleaseIntegrityError(f"invalid journal created_at: {created_at!r}")
        try:
            parsed_created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ReleaseIntegrityError(f"invalid journal created_at: {created_at!r}") from exc
        if parsed_created_at.tzinfo is None:
            raise ReleaseIntegrityError(f"journal created_at must include a timezone: {created_at!r}")
        state = document.get("state")
        if state not in _JOURNAL_STATES:
            raise ReleaseIntegrityError(f"invalid journal state: {state!r}")
        self._journal_backup_path(
            document.get("guard_backup"),
            field="guard_backup",
            expected_name=f"{transaction_id}-guard",
        )
        migration = document.get("migration")
        if migration is not None:
            if not isinstance(migration, dict):
                raise ReleaseIntegrityError("journal migration must be an object or null")
            migration_unknown = sorted(
                set(migration) - {"from_schema", "to_schema", "applied", "backup"}
            )
            if migration_unknown:
                raise ReleaseIntegrityError(
                    f"journal migration has unknown fields: {migration_unknown}"
                )
            for name in ("from_schema", "to_schema"):
                if type(migration.get(name)) is not int or int(migration[name]) < 0:
                    raise ReleaseIntegrityError(f"invalid journal migration {name}")
            if not isinstance(migration.get("applied"), list) or not all(
                isinstance(item, str) for item in migration["applied"]
            ):
                raise ReleaseIntegrityError("invalid journal migration applied")
            migration_backup = migration.get("backup")
            if migration_backup is not None:
                self._journal_backup_path(
                    migration_backup,
                    field="migration.backup",
                    expected_name=transaction_id,
                )
        state_snapshot = document.get("state_snapshot")
        if state_snapshot is not None:
            self._journal_backup_path(state_snapshot, field="state_snapshot")
        for name in ("rollback_errors", "removed_releases", "recovery_errors"):
            value = document.get(name)
            if value is not None and (
                not isinstance(value, list) or not all(isinstance(item, str) for item in value)
            ):
                raise ReleaseIntegrityError(f"journal {name} must be an array of strings")
        for name in ("error", "recovered_at"):
            value = document.get(name)
            if value is not None and not isinstance(value, str):
                raise ReleaseIntegrityError(f"journal {name} must be a string")
        recovered_at = document.get("recovered_at")
        if recovered_at is not None:
            try:
                parsed_recovered_at = datetime.fromisoformat(recovered_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ReleaseIntegrityError(f"invalid journal recovered_at: {recovered_at!r}") from exc
            if parsed_recovered_at.tzinfo is None:
                raise ReleaseIntegrityError(
                    f"journal recovered_at must include a timezone: {recovered_at!r}"
                )
        return document

    def _validate_legacy_adoption(self, legacy_root: Path, *, current_version: str | None) -> None:
        if current_version is not None:
            raise ReleaseError("--adopt is allowed only during the first managed install")
        legacy = legacy_root.expanduser().resolve()
        if legacy_root.is_symlink() or not legacy.is_dir():
            raise ReleaseError(f"legacy adopt root must be a real directory: {legacy_root}")
        install = self.install_root.resolve()
        state = self.state_root.resolve()
        releases = self.releases_root.resolve(strict=False)
        for managed in (install, state, releases):
            if _paths_overlap(legacy, managed):
                raise ReleaseError(f"legacy adopt root overlaps managed install paths: {legacy}")
        metadata = load_state_metadata(self.state_root)
        if int(metadata.get("state_schema") or 0) != 0 or metadata.get("last_version"):
            raise ReleaseError("--adopt requires an uninitialized managed state")
        managed_state_names = ("packages", "config", "apps", "logs", ".control.log")
        has_state = any((self.state_root / name).exists() for name in managed_state_names)
        runs = self.state_root / "runs"
        if runs.is_dir():
            has_state = has_state or any(
                path.is_file() and path.name != ".release-runtime.lock"
                for path in runs.rglob("*")
            )
        if has_state:
            raise ReleaseError("--adopt requires empty managed B-class state")

    def _ensure_managed_layout(self) -> None:
        if self.install_root.exists() and not self.install_root.is_dir():
            raise ReleaseError(f"install root must be a directory: {self.install_root}")
        for path in (self.releases_root, self.state_root, self.install_root / "bin"):
            if path.is_symlink():
                raise ReleaseError(f"managed install directory must not be a symlink: {path}")

    def _existing(self, version: str | None) -> StagedRelease:
        if not version:
            raise ReleaseError("version is required when source is omitted")
        _semver_key(version)
        path = self.releases_root / version
        if not path.is_dir() or path.is_symlink():
            raise ReleaseError(f"release is not staged: {version}")
        manifest = _validate_manifest(_read_json(path / "version.json"))
        if manifest["version"] != version:
            raise ReleaseIntegrityError(f"release directory/manifest mismatch: {version}")
        self._verify_staged_release(path, manifest)
        return StagedRelease(version, path, manifest, reused=True)

    def _switch_current(self, target: Path) -> None:
        target = target.resolve()
        try:
            target.relative_to(self.releases_root.resolve())
        except ValueError as exc:
            raise ReleaseError(f"release target escapes releases/: {target}") from exc
        temp = self.install_root / f".current-{uuid.uuid4().hex}"
        relative = os.path.relpath(target, self.install_root)
        os.symlink(relative, temp)
        os.replace(temp, self.current)

    def _restore_current(self, previous: Path | None) -> None:
        if previous is None:
            if self.current.is_symlink():
                self.current.unlink()
            return
        self._switch_current(previous)

    def _capture_wrapper(self) -> tuple[bytes, int] | None:
        wrapper = self.install_root / "bin" / "autophone"
        if not wrapper.exists() and not wrapper.is_symlink():
            return None
        if wrapper.is_symlink() or not wrapper.is_file():
            raise ReleaseError(f"managed wrapper must be a regular file: {wrapper}")
        return wrapper.read_bytes(), stat.S_IMODE(wrapper.stat().st_mode)

    def _restore_wrapper(self, previous: tuple[bytes, int] | None) -> None:
        wrapper = self.install_root / "bin" / "autophone"
        if previous is None:
            if wrapper.exists() or wrapper.is_symlink():
                wrapper.unlink()
            bin_dir = wrapper.parent
            if bin_dir.is_dir() and not any(bin_dir.iterdir()):
                bin_dir.rmdir()
            return
        content, mode = previous
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        temp = wrapper.with_name(f".{wrapper.name}.{uuid.uuid4().hex}.tmp")
        temp.write_bytes(content)
        os.chmod(temp, mode)
        os.replace(temp, wrapper)

    def _write_wrapper(self) -> None:
        bin_dir = self.install_root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        wrapper = bin_dir / "autophone"
        text = """#!/bin/bash
set -euo pipefail
INSTALL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CURRENT="$INSTALL_ROOT/current"
export AUTOPHONE_STATE_DIR="$INSTALL_ROOT/state"
export AUTOPHONE_INSTALL_ROOT="$INSTALL_ROOT"
export AUTOPHONE_READONLY_ASSETS=1
export PYTHONDONTWRITEBYTECODE=1
# Run from the release tree: autophone resolves CODE_ROOT, version.json and its
# packaged assets (apps/, autophone/_data/, migrations/) relative to the source
# tree it is imported from.  Without this the caller's cwd would decide which
# code and which assets a managed install actually runs.
cd "$CURRENT"
exec "$CURRENT/.venv/bin/python" -m autophone.cli "$@"
"""
        temp = wrapper.with_suffix(".tmp")
        temp.write_text(text, encoding="utf-8")
        os.chmod(temp, 0o755)
        os.replace(temp, wrapper)

    def _write_journal(self, journal: dict[str, Any]) -> None:
        self._validate_journal(journal)
        journals = self._journal_directory(create=True)
        path = journals / f"{journal['transaction_id']}.json"
        write_json_atomic(path, journal)

    def _record_success(self, version: str, previous: str | None, transaction_id: str) -> None:
        metadata = load_state_metadata(self.state_root)
        history = [str(item) for item in metadata.get("release_history") or [] if _SEMVER.fullmatch(str(item))]
        history = [item for item in history if item != version] + [version]
        metadata.update(
            last_version=version,
            previous_version=previous,
            release_history=history,
            last_release_transaction=transaction_id,
        )
        write_json_atomic(state_file(self.state_root), metadata)

    def _installed_versions(self) -> list[str]:
        if not self.releases_root.is_dir():
            return []
        return sorted(
            [
                path.name
                for path in self.releases_root.iterdir()
                if path.is_dir() and not path.is_symlink() and _SEMVER.fullmatch(path.name)
            ],
            key=_semver_key,
        )

    def _prune_releases(self) -> list[str]:
        versions = self._installed_versions()
        metadata = load_state_metadata(self.state_root)
        protected = {self.current_version(), metadata.get("previous_version")}
        candidates = [value for value in versions if value not in protected]
        keep_extra = max(0, self.keep_releases - len({value for value in protected if value}))
        remove = candidates[:-keep_extra] if keep_extra else candidates
        removed: list[str] = []
        for version in remove:
            path = (self.releases_root / version).resolve()
            if path.parent != self.releases_root.resolve() or not _SEMVER.fullmatch(path.name):
                continue
            shutil.rmtree(path)
            shutil.rmtree(self.state_root / ".release-artifacts" / version, ignore_errors=True)
            self._receipt_path(version).unlink(missing_ok=True)
            removed.append(version)
        return removed

    def _incomplete_transactions(self) -> list[dict[str, Any]]:
        return [
            {
                "transaction_id": document.get("transaction_id") or path.stem,
                "kind": document.get("kind") or "apply",
                "from_version": document.get("from_version"),
                "to_version": document.get("to_version"),
                "state": document.get("state"),
                "created_at": document.get("created_at"),
            }
            for path, document in self._incomplete_transaction_documents()
        ]

    def _incomplete_transaction_documents(self) -> list[tuple[Path, dict[str, Any]]]:
        journals = self._journal_directory()
        if not journals.is_dir():
            return []
        pending: list[tuple[Path, dict[str, Any]]] = []
        terminal = {"committed", "rolled_back", "recovered"}
        for path in sorted(journals.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                raise ReleaseIntegrityError(f"release journal must be a regular file: {path}")
            document = self._validate_journal(_read_json(path), path=path)
            if document.get("state") not in terminal:
                pending.append((path, document))
        return pending

    def _assert_no_incomplete_transaction(self) -> None:
        pending = self._incomplete_transactions()
        if pending:
            raise ReleaseError(
                "unfinished release transaction requires recovery: "
                + json.dumps(pending, ensure_ascii=False)
            )

    def _snapshot_for_transition(self, from_version: str, to_version: str) -> Path | None:
        journals = self._journal_directory()
        if not journals.is_dir():
            return None
        candidates: list[tuple[str, float, Path, dict[str, Any]]] = []
        for path in journals.glob("*.json"):
            if path.is_symlink() or not path.is_file():
                raise ReleaseIntegrityError(f"release journal must be a regular file: {path}")
            doc = self._validate_journal(_read_json(path), path=path)
            if (
                doc.get("kind", "apply") == "apply"
                and doc.get("state") == "committed"
                and doc.get("from_version") == from_version
                and doc.get("to_version") == to_version
            ):
                candidates.append((str(doc.get("created_at") or ""), path.stat().st_mtime, path, doc))
        for _created, _mtime, _path, doc in sorted(candidates, reverse=True):
            backup = ((doc.get("migration") or {}).get("backup"))
            if not backup:
                continue
            candidate = self._journal_backup_path(
                backup,
                field="migration.backup",
                expected_name=str(doc["transaction_id"]),
            )
            if candidate.is_dir():
                return candidate
        return None


__all__ = [
    "ReleaseCompatibilityError",
    "ReleaseError",
    "ReleaseIntegrityError",
    "ReleaseManager",
    "ResolvedRelease",
    "StagedRelease",
    "resolve_release",
    "safe_extract_release",
]
