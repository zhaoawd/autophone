"""Package source digest, captured before application modules are imported."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


CODE_ROOT = Path(__file__).resolve().parents[1]
# Include executable package inputs, independently of Git and distribution metadata.
SOURCE_SUFFIXES = frozenset({".py", ".js", ".mjs", ".sql", ".sh", ".swift", ".ts", ".tsx"})
# App packages under `apps/` are imported by package name from the checkout (Operation
# executors, state drivers, recipe registration), so their Python is resident code too.
# Cases, recipes and other YAML assets are re-read at use and stay out of the digest.
APP_SOURCE_SUFFIXES = frozenset({".py"})


def _walk(root: Path, tree: str, suffixes: frozenset[str]) -> list[str]:
    paths: list[str] = []
    for directory, subdirs, filenames in os.walk(root / tree):
        subdirs[:] = [name for name in subdirs
                      if not name.startswith(".") and name not in {"__pycache__", "node_modules"}]
        for name in filenames:
            if not name.startswith(".") and Path(name).suffix in suffixes:
                path = Path(directory) / name
                paths.append(path.relative_to(root).as_posix())
    return paths


def source_paths(root: Path) -> tuple[str, ...]:
    return tuple(sorted(
        _walk(root, "autophone", SOURCE_SUFFIXES) + _walk(root, "apps", APP_SOURCE_SUFFIXES)
    ))


def package_digest(root: Path) -> str:
    hasher = hashlib.sha256(b"autophone-package-source-v3\0")
    for relative in source_paths(root):
        hasher.update(relative.encode("utf-8") + b"\0")
        try:
            payload = (root / relative).read_bytes()
        except OSError:
            hasher.update(b"missing")
        else:
            hasher.update(hashlib.sha256(payload).digest())
        hasher.update(b"\n")
    return hasher.hexdigest()


# Captured by autophone.__init__ before version, paths, or a lazy caller is loaded.
PROCESS_SOURCE_DIGEST = package_digest(CODE_ROOT)
