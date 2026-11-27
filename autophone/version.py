"""Runtime build identity for source checkouts and packaged releases."""
from __future__ import annotations

import importlib.metadata
import json
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any


UNKNOWN_VERSION = "0.0.0+unknown"
_PACKAGE_METADATA_DISTS: tuple[str, ...] = (
    "autophone",
    "autophone-kernel",
    "autophone-console",
    "autophone-rig",
    "autophone-domain",
)
_SEMVER_DESCRIBE = re.compile(
    r"^(?P<tag>v\d+\.\d+\.\d+)(?:-(?P<distance>\d+)-g(?P<commit>[0-9a-f]+))?(?P<dirty>-dirty)?$"
)
_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")


def _code_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _is_git_checkout() -> bool:
    return (_code_root() / ".git").exists()


def _manifest_candidates() -> tuple[Path, ...]:
    # Source/tar releases carry the manifest at their top level.  The package
    # candidate keeps the reader usable if a future wheel includes it as data.
    return (_code_root() / "version.json", Path(__file__).with_name("version.json"))


def read_version_manifest(path: Path | None = None) -> dict[str, Any] | None:
    candidates = (Path(path),) if path is not None else _manifest_candidates()
    for candidate in candidates:
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        # UnicodeDecodeError is a ValueError, not an OSError: a non-UTF-8
        # version.json must fall through to git/fallback, not crash identity.
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        version = str(raw.get("version") or "")
        commit = str(raw.get("commit") or "")
        if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
            continue
        if commit and not _COMMIT.fullmatch(commit):
            continue
        return raw
    return None


def _git(*args: str) -> str | None:
    if not _is_git_checkout():
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(_code_root()), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _package_metadata_version() -> str | None:
    for dist_name in _PACKAGE_METADATA_DISTS:
        try:
            value = importlib.metadata.version(dist_name)
        except importlib.metadata.PackageNotFoundError:
            continue
        if value:
            return value
    return None


def _version_from_describe(value: str | None) -> str:
    if not value:
        return UNKNOWN_VERSION
    match = _SEMVER_DESCRIBE.fullmatch(value)
    if match:
        tag = match.group("tag")
        distance = match.group("distance")
        commit = match.group("commit")
        dirty = match.group("dirty")
        if distance and commit:
            suffix = f"+{distance}.g{commit}"
            if dirty:
                suffix += ".dirty"
            return f"{tag}{suffix}"
        return f"{tag}{'+dirty' if dirty else ''}"
    if _COMMIT.fullmatch(value.removesuffix("-dirty")):
        commit = value.removesuffix("-dirty")
        return f"0.0.0+g{commit}{'.dirty' if value.endswith('-dirty') else ''}"
    return UNKNOWN_VERSION


@lru_cache(maxsize=1)
def _build_identity_cached() -> dict[str, Any]:
    manifest = read_version_manifest()
    if manifest is not None:
        return {
            "version": str(manifest["version"]),
            "commit": str(manifest.get("commit") or "unknown"),
            "built_at": manifest.get("built_at"),
            "state_schema": manifest.get("state_schema"),
            "source": "version.json",
        }
    if _is_git_checkout():
        describe = _git(
            "describe", "--tags", "--match", "v[0-9]*.[0-9]*.[0-9]*",
            "--always", "--dirty",
        )
        if describe:
            return {
                "version": _version_from_describe(describe),
                "commit": _git("rev-parse", "--short=12", "HEAD") or "unknown",
                "built_at": None,
                "state_schema": None,
                "source": "git",
            }
    pkg_version = _package_metadata_version()
    if pkg_version is not None:
        return {
            "version": pkg_version,
            "commit": "unknown",
            "built_at": None,
            "state_schema": None,
            "source": "package_metadata",
        }
    return {
        "version": UNKNOWN_VERSION,
        "commit": "unknown",
        "built_at": None,
        "state_schema": None,
        "source": "fallback",
    }


def build_identity() -> dict[str, Any]:
    """Return a copy of the immutable build identity for this process."""
    return dict(_build_identity_cached())


def stamp_run_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Copy a RunSpec and freeze the server's build identity into it."""
    stamped = dict(spec)
    identity = build_identity()
    # These fields are provenance, not caller-controlled input.
    stamped["autophone_version"] = str(identity["version"])
    stamped["commit"] = str(identity["commit"])
    return stamped


def inherit_run_identity(
    result: dict[str, Any], spec: dict[str, Any] | None,
) -> dict[str, Any]:
    """Copy frozen provenance from a RunSpec without guessing for legacy runs."""
    stamped = dict(result)
    frozen = spec or {}
    for field in ("autophone_version", "commit"):
        stamped.pop(field, None)
        if frozen.get(field):
            stamped[field] = str(frozen[field])
    return stamped


def get_version() -> str:
    return str(build_identity()["version"])


def get_commit() -> str:
    return str(build_identity()["commit"])


def _status_porcelain() -> str | None:
    """Return the working tree status, or None when the checkout cannot answer.

    不能复用 `_git()`：它把「空输出（工作区干净）」和「执行失败」都返回 None，
    这里必须区分两者。
    """
    if not _is_git_checkout():
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(_code_root()), "status", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def source_identity() -> dict[str, Any]:
    """Describe the checkout this process runs from, including uncommitted work.

    `build_identity()` 带 lru_cache 且以 `version.json` 优先：同一个版本号在两个
    checkout 上完全相同，未提交改动也只体现为一个 `dirty` 后缀。要判定「另一个进程
    跑的是不是我这份源码」，还需要 `code_root`、当前 HEAD 和工作区状态，并且每次实读
    ——工作区脏不脏会在两次调用之间变化。

    仓库读不出时 `head` 与 `dirty` 为 None，调用方按「无法判定」处理，不得按
    「干净」处理。
    """
    status = _status_porcelain()
    return {
        "code_root": str(_code_root()),
        "head": _git("rev-parse", "HEAD"),
        "dirty": None if status is None else bool(status.strip()),
    }


__all__ = [
    "UNKNOWN_VERSION",
    "build_identity",
    "get_commit",
    "get_version",
    "inherit_run_identity",
    "read_version_manifest",
    "source_identity",
    "stamp_run_spec",
]
