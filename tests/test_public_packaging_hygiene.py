"""Offline tests for P0 public packaging hygiene, version resolution, and snapshots."""
from __future__ import annotations

import fnmatch
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import re
import runpy
import stat
import subprocess
import sys
import tarfile
from types import ModuleType, SimpleNamespace
import zipfile

import pytest
import yaml

from autophone import version as version_mod
from scripts import (
    audit_release_compliance,
    check_public_content,
    prepare_public_snapshot,
    verify_core_archives,
    verify_core_install,
)


PACKAGES = (
    "autophone",
    "autophone-kernel",
    "autophone-rig",
    "autophone-domain",
    "autophone-console",
)


def _run_git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env=merged_env,
    )
    return completed.stdout.strip()


def _init_repo(
    root: Path,
    *,
    name: str = "Test Maintainer",
    email: str = "maintainer@example.com",
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _run_git(root, "init", "-b", "main")
    _run_git(root, "config", "user.name", name)
    _run_git(root, "config", "user.email", email)


def _commit_all(
    root: Path,
    message: str,
    *,
    author_name: str = "Test Maintainer",
    author_email: str = "maintainer@example.com",
    committer_name: str | None = None,
    committer_email: str | None = None,
) -> str:
    _run_git(root, "add", "-A")
    env = {
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": committer_name or author_name,
        "GIT_COMMITTER_EMAIL": committer_email or author_email,
    }
    _run_git(root, "commit", "--no-gpg-sign", "-m", message, env=env)
    return _run_git(root, "rev-parse", "HEAD")


def _write(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def _populate_public_repo_skeleton(root: Path, version: str = "0.1.0") -> None:
    license_text = "Apache License 2.0\n"
    notice_text = "Autophone Notice\n"
    _write(root / "LICENSE", license_text)
    _write(root / "NOTICE", notice_text)
    _write(root / "README.md", "# Autophone\n\nSee [LICENSE](LICENSE).\n")
    _write(
        root / "pyproject.toml",
        f'[project]\nname = "autophone"\nversion = "{version}"\n'
        f'dependencies = [\n'
        f'    "autophone-kernel=={version}",\n'
        f'    "autophone-rig=={version}",\n'
        f'    "autophone-domain=={version}",\n'
        f'    "autophone-console=={version}",\n'
        f']\n',
    )
    component_deps = {
        "autophone-kernel": [],
        "autophone-rig": [f"autophone-kernel=={version}"],
        "autophone-domain": [f"autophone-kernel=={version}", f"autophone-rig=={version}"],
        "autophone-console": [
            f"autophone-kernel=={version}",
            f"autophone-rig=={version}",
            f"autophone-domain=={version}",
        ],
    }
    for pkg_name, deps in component_deps.items():
        pkg_dir = root / "pkgs" / pkg_name
        _write(pkg_dir / "LICENSE", license_text)
        _write(pkg_dir / "NOTICE", notice_text)
        deps_literal = ", ".join(f'"{d}"' for d in deps)
        _write(
            pkg_dir / "pyproject.toml",
            f'[project]\nname = "{pkg_name}"\nversion = "{version}"\n'
            f"dependencies = [{deps_literal}]\n",
        )
    mcp_dir = root / "vphone_mcp"
    _write(mcp_dir / "LICENSE", license_text)
    _write(mcp_dir / "NOTICE", notice_text)
    _write(
        mcp_dir / "pyproject.toml",
        f'[project]\nname = "vphone-mcp"\nversion = "{version}"\n'
        'dependencies = ["mcp[cli]>=1.2.0"]\n',
    )


class _MiniGitIgnoreSpec:
    def __init__(self, lines: list[str]) -> None:
        self._patterns = [
            line.strip()
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        ]

    @classmethod
    def from_lines(cls, lines: list[str]) -> "_MiniGitIgnoreSpec":
        return cls(list(lines))

    def match_file(self, rel_posix: str) -> bool:
        for pat in self._patterns:
            anchored = pat.startswith("/")
            clean = pat.lstrip("/")
            if clean.endswith("/"):
                if rel_posix.startswith(clean) or (not anchored and f"/{clean}" in f"/{rel_posix}"):
                    return True
            elif fnmatch.fnmatch(rel_posix, clean):
                return True
            elif not anchored and fnmatch.fnmatch(rel_posix.rsplit("/", 1)[-1], clean):
                return True
        return False


def _load_packaging_hook_cls(monkeypatch: pytest.MonkeyPatch):
    if "hatchling.builders.hooks.plugin.interface" not in sys.modules:
        class _StubBuildHookInterface:
            def __init__(self, root: str, pkg_name: str, target_name: str = "wheel") -> None:
                self.root = root
                self.metadata = SimpleNamespace(name=pkg_name)
                self.target_name = target_name
                self.build_config = SimpleNamespace(target_config={})

        for mod_name in (
            "hatchling",
            "hatchling.builders",
            "hatchling.builders.hooks",
            "hatchling.builders.hooks.plugin",
            "hatchling.builders.hooks.plugin.interface",
        ):
            mod = ModuleType(mod_name)
            if mod_name.endswith(".interface"):
                mod.BuildHookInterface = _StubBuildHookInterface  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, mod_name, mod)
    if "pathspec" not in sys.modules:
        pathspec_mod = ModuleType("pathspec")
        pathspec_mod.GitIgnoreSpec = _MiniGitIgnoreSpec  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pathspec", pathspec_mod)

    ns = runpy.run_path(str(check_public_content.ROOT / "packaging_support.py"))
    return ns["CustomBuildHook"]


def _make_hook(monkeypatch: pytest.MonkeyPatch, root: Path, pkg_name: str, target_name: str = "wheel"):
    hook_cls = _load_packaging_hook_cls(monkeypatch)
    hook = object.__new__(hook_cls)
    hook.root = str(root)
    hook.metadata = SimpleNamespace(name=pkg_name)
    hook.target_name = target_name
    hook.build_config = SimpleNamespace(target_config={})
    return hook


# ── 1. packaging_support.py ────────────────────────────────────────────────────


def test_packaging_hook_excludes_untracked_files_in_git_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    _populate_public_repo_skeleton(tmp_path)
    _write(tmp_path / "autophone" / "cli.py", "def main():\n    return 0\n")
    _write(tmp_path / "config" / "schedules.yaml", "schedules: []\n")
    _write(tmp_path / "tools" / "phase5" / "keep.py", "VALUE = 1\n")
    _commit_all(tmp_path, "initial tracked sources")

    # Create untracked private/local files NOT listed in .gitignore inside this temp repo.
    _write(tmp_path / "config" / "kfc-merchant-emulator.local.yaml", "secret: true\n")
    _write(tmp_path / "tools" / "guest" / "ax_dump.dylib", b"\x00\x01binary")
    _write(tmp_path / "tools" / "phase5" / "runs" / "shot.jpg", b"\xff\xd8\xff\xd9")
    _write(tmp_path / "config" / "untracked-extra.yaml", "extra: 1\n")

    hook = _make_hook(monkeypatch, tmp_path / "pkgs" / "autophone-console", "autophone-console")
    build_data = {"force_include": {}}
    hook.initialize("standard", build_data)

    included = set(build_data["force_include"].values())
    assert "autophone/cli.py" in included
    assert "config/schedules.yaml" in included
    assert "tools/phase5/keep.py" in included
    assert "config/kfc-merchant-emulator.local.yaml" not in included
    assert "tools/guest/ax_dump.dylib" not in included
    assert "tools/phase5/runs/shot.jpg" not in included
    assert "config/untracked-extra.yaml" not in included


def test_packaging_hook_non_git_snapshot_and_local_sdist_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Non-git snapshot keeps filesystem + .gitignore behavior.
    snapshot = tmp_path / "snapshot"
    _populate_public_repo_skeleton(snapshot)
    _write(snapshot / ".gitignore", "config/*.local.yaml\n/tools/guest/\n/tools/phase5/runs/\n")
    _write(snapshot / "autophone" / "cli.py", "def main():\n    return 0\n")
    _write(snapshot / "config" / "schedules.yaml", "schedules: []\n")
    _write(snapshot / "config" / "private.local.yaml", "ignored: true\n")
    _write(snapshot / "tools" / "guest" / "ax_dump.dylib", b"\x00\x01")
    _write(snapshot / "tools" / "phase5" / "runs" / "shot.jpg", b"\xff\xd8")

    hook = _make_hook(monkeypatch, snapshot / "pkgs" / "autophone-console", "autophone-console")
    build_data = {"force_include": {}}
    hook.initialize("standard", build_data)
    included = set(build_data["force_include"].values())
    assert "autophone/cli.py" in included
    assert "config/schedules.yaml" in included
    assert "config/private.local.yaml" not in included
    assert "tools/guest/ax_dump.dylib" not in included
    assert "tools/phase5/runs/shot.jpg" not in included

    # Local unpacked component sdist build (`local = True`).
    sdist_dir = tmp_path / "sdist-console"
    _write(sdist_dir / "autophone" / "cli.py", "def main():\n    return 0\n")
    _write(sdist_dir / "config" / "schedules.yaml", "schedules: []\n")
    local_hook = _make_hook(monkeypatch, sdist_dir, "autophone-console")
    local_data = {"force_include": {}}
    local_hook.initialize("standard", local_data)
    assert set(local_data["force_include"].values()) == {"autophone/cli.py", "config/schedules.yaml"}


# ── 2. scripts/verify_core_archives.py ─────────────────────────────────────────


def _create_synthetic_archive_set(
    out_dir: Path,
    schema_root: Path,
    *,
    extra_wheel_files: dict[str, dict[str, bytes]] | None = None,
    extra_sdist_files: dict[str, dict[str, bytes]] | None = None,
    omit_wheel_legal: tuple[str, str] | None = None,
    omit_sdist_legal: tuple[str, str] | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    schema_rel = "autophone/_data/schemas/sample.v1.json"
    _write(schema_root / schema_rel, '{"type": "object"}\n')
    extra_wheel_files = extra_wheel_files or {}
    extra_sdist_files = extra_sdist_files or {}

    for pkg in PACKAGES:
        dist = pkg.replace("-", "_")
        whl_path = out_dir / f"{dist}-0.1.0-py3-none-any.whl"
        with zipfile.ZipFile(whl_path, "w") as zf:
            if omit_wheel_legal != (pkg, "LICENSE"):
                if pkg == "autophone":
                    zf.writestr("LICENSE", b"Apache-2.0\n")
                else:
                    zf.writestr(f"{dist}-0.1.0.dist-info/licenses/LICENSE", b"Apache-2.0\n")
            if omit_wheel_legal != (pkg, "NOTICE"):
                if pkg == "autophone":
                    zf.writestr("NOTICE", b"Notice\n")
                else:
                    zf.writestr(f"{dist}-0.1.0.dist-info/licenses/NOTICE", b"Notice\n")
            metadata_path = f"{dist}-0.1.0.dist-info/METADATA"
            pkg_extras = extra_wheel_files.get(pkg, {})
            if metadata_path not in pkg_extras:
                metadata_lines = [f"Name: {pkg}", "Version: 0.1.0"]
                if pkg in {"autophone", "autophone-rig"}:
                    metadata_lines.extend([
                        "Provides-Extra: postgres",
                        'Requires-Dist: psycopg[binary]>=3.2; extra == "postgres"',
                    ])
                zf.writestr(metadata_path, "\n".join(metadata_lines) + "\n")
            if pkg == "autophone-kernel":
                zf.writestr(schema_rel, b'{"type": "object"}\n')
            for rel, payload in pkg_extras.items():
                zf.writestr(rel, payload)

        tar_path = out_dir / f"{dist}-0.1.0.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tf:
            pyproject_body = f'[project]\nname = "{pkg}"\nversion = "0.1.0"\n'
            if pkg in {"autophone", "autophone-rig"}:
                pyproject_body += '\n[project.optional-dependencies]\npostgres = ["psycopg[binary]>=3.2"]\n'
            members: dict[str, bytes] = {
                "pyproject.toml": pyproject_body.encode("utf-8"),
            }
            if omit_sdist_legal != (pkg, "LICENSE"):
                members["LICENSE"] = b"Apache-2.0\n"
            if omit_sdist_legal != (pkg, "NOTICE"):
                members["NOTICE"] = b"Notice\n"
            if pkg == "autophone-kernel":
                members[schema_rel] = b'{"type": "object"}\n'
            members.update(extra_sdist_files.get(pkg, {}))
            for rel, payload in members.items():
                info = tarfile.TarInfo(name=f"{dist}-0.1.0/{rel}")
                info.size = len(payload)
                tf.addfile(info, io.BytesIO(payload))


def test_verify_core_archives_accepts_clean_archives(tmp_path: Path) -> None:
    schema_root = tmp_path / "repo"
    dist_dir = tmp_path / "dist"
    _create_synthetic_archive_set(
        dist_dir,
        schema_root,
        extra_wheel_files={
            "autophone-console": {
                "autophone/cli.py": b"print('ok')\n",
                "autophone/blob.bin": b"\x80\x81\xff\xfe",
                "scripts/check_public_content.py": ("/Us" + "ers/skipped/ok\n").encode("utf-8"),
            },
        },
    )
    verify_core_archives.verify(dist_dir, root=schema_root)


@pytest.mark.parametrize("missing_file", ["LICENSE", "NOTICE"])
@pytest.mark.parametrize("archive_kind", ["whl", "sdist"])
def test_verify_core_archives_rejects_missing_legal_files(
    tmp_path: Path, missing_file: str, archive_kind: str,
) -> None:
    schema_root = tmp_path / "repo"
    dist_dir = tmp_path / "dist"
    kwargs = (
        {"omit_wheel_legal": ("autophone-rig", missing_file)}
        if archive_kind == "whl"
        else {"omit_sdist_legal": ("autophone-rig", missing_file)}
    )
    _create_synthetic_archive_set(dist_dir, schema_root, **kwargs)
    with pytest.raises(ValueError, match=f"missing required legal file {missing_file}"):
        verify_core_archives.verify(dist_dir, root=schema_root)


@pytest.mark.parametrize(
    "forbidden_path",
    [
        "autophone/workflow/runner.py",
        "autophone/adapters/http/stores.py",
        "autophone/device/app_uninstall_helper.py",
        "tools/guest/ax_dump.dylib",
        "tools/phase5/runs/shot.jpg",
        "review/public-release/review.md",
        "config/kfc-merchant-emulator.local.yaml",
    ],
)
@pytest.mark.parametrize("archive_kind", ["whl", "sdist"])
def test_verify_core_archives_rejects_forbidden_paths(
    tmp_path: Path, forbidden_path: str, archive_kind: str,
) -> None:
    schema_root = tmp_path / "repo"
    dist_dir = tmp_path / "dist"
    extra = {"autophone-console": {forbidden_path: b"ok\n"}}
    kwargs = {"extra_wheel_files": extra} if archive_kind == "whl" else {"extra_sdist_files": extra}
    _create_synthetic_archive_set(dist_dir, schema_root, **kwargs)
    with pytest.raises(ValueError, match="forbidden"):
        verify_core_archives.verify(dist_dir, root=schema_root)


@pytest.mark.parametrize(
    ("secret_line", "expected_label"),
    [
        ("path = '/Us" + "ers/alice/secret'", "hardcoded macOS user path"),
        ("-----BEGIN " + "PRIVATE KEY-----", "private key block"),
        ("host = 'api.yum" + "china.com'", "internal corporate domain or host"),
        ("host = 'SGH" + "QCZ9999'", "internal corporate domain or host"),
        ("<" * 7 + " HEAD", "merge conflict marker"),
    ],
)
@pytest.mark.parametrize("archive_kind", ["whl", "sdist"])
def test_verify_core_archives_rejects_forbidden_content(
    tmp_path: Path, secret_line: str, expected_label: str, archive_kind: str,
) -> None:
    schema_root = tmp_path / "repo"
    dist_dir = tmp_path / "dist"
    payload = f"line1 = 'clean'\n{secret_line}\n".encode("utf-8")
    extra = {"autophone-console": {"autophone/cli.py": payload}}
    kwargs = {"extra_wheel_files": extra} if archive_kind == "whl" else {"extra_sdist_files": extra}
    _create_synthetic_archive_set(dist_dir, schema_root, **kwargs)
    with pytest.raises(ValueError) as exc_info:
        verify_core_archives.verify(dist_dir, root=schema_root)
    msg = str(exc_info.value)
    assert "autophone_console-0.1.0" in msg
    assert "autophone/cli.py:2" in msg
    assert expected_label in msg


# ── 3. autophone/version.py ────────────────────────────────────────────────────


def test_version_precedence_and_nested_venv_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Build a real git repo with a semver tag, and a nested site-packages inside it.
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo / "README.md", "# Repo\n")
    _commit_all(repo, "initial")
    _run_git(repo, "tag", "v1.2.3")

    nested_site_packages = repo / ".venv" / "lib" / "python3.13" / "site-packages"
    nested_site_packages.mkdir(parents=True)

    # 1. version.json takes top precedence over local .git and package metadata.
    _write(
        repo / "version.json",
        json.dumps({"version": "v9.8.7", "commit": "abcdef123456", "built_at": "2026-01-01T00:00:00Z"}),
    )
    monkeypatch.setattr(version_mod, "_code_root", lambda: repo)
    monkeypatch.setattr(version_mod, "_manifest_candidates", lambda: (repo / "version.json",))
    version_mod._build_identity_cached.cache_clear()
    try:
        ident = version_mod.build_identity()
        assert ident["source"] == "version.json"
        assert ident["version"] == "v9.8.7"
        assert ident["commit"] == "abcdef123456"

        # 2. Removing version.json falls back to local .git checkout.
        (repo / "version.json").unlink()
        version_mod._build_identity_cached.cache_clear()
        ident = version_mod.build_identity()
        assert ident["source"] == "git"
        assert ident["version"] == "v1.2.3"
        assert ident["commit"] != "unknown"

        # 3. Nested .venv/.../site-packages inside the git repo MUST NOT walk up to enclosing .git!
        monkeypatch.setattr(version_mod, "_code_root", lambda: nested_site_packages)
        monkeypatch.setattr(version_mod, "_manifest_candidates", lambda: (nested_site_packages / "version.json",))
        assert version_mod._is_git_checkout() is False
        assert version_mod._git("rev-parse", "HEAD") is None
        src_ident = version_mod.source_identity()
        assert src_ident["head"] is None
        assert src_ident["dirty"] is None

        # Verify distribution lookup order: falls through to autophone-kernel if autophone missing.
        def fake_dist_version(dist_name: str) -> str:
            if dist_name == "autophone-kernel":
                return "0.1.0"
            raise importlib.metadata.PackageNotFoundError(dist_name)

        monkeypatch.setattr(importlib.metadata, "version", fake_dist_version)
        version_mod._build_identity_cached.cache_clear()
        ident = version_mod.build_identity()
        assert ident == {
            "version": "0.1.0",
            "commit": "unknown",
            "built_at": None,
            "state_schema": None,
            "source": "package_metadata",
        }

        # 4. If no distributions are installed, fall back to UNKNOWN_VERSION.
        def missing_all(dist_name: str) -> str:
            raise importlib.metadata.PackageNotFoundError(dist_name)

        monkeypatch.setattr(importlib.metadata, "version", missing_all)
        version_mod._build_identity_cached.cache_clear()
        ident = version_mod.build_identity()
        assert ident == {
            "version": version_mod.UNKNOWN_VERSION,
            "commit": "unknown",
            "built_at": None,
            "state_schema": None,
            "source": "fallback",
        }
    finally:
        version_mod._build_identity_cached.cache_clear()


def test_verify_core_install_runtime_version_handles_source_and_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autophone

    # Source checkout branch accepts git and version.json sources.
    monkeypatch.setattr(version_mod, "_is_git_checkout", lambda: True)
    monkeypatch.setattr(autophone, "__version__", "0.0.0+gabcdef123456")
    monkeypatch.setattr(version_mod, "build_identity", lambda: {"version": "0.0.0+gabcdef123456", "source": "git"})
    assert verify_core_install.verify_runtime_version()["source"] == "git"

    monkeypatch.setattr(autophone, "__version__", "v0.1.0")
    monkeypatch.setattr(version_mod, "build_identity", lambda: {"version": "v0.1.0", "source": "version.json"})
    assert verify_core_install.verify_runtime_version()["source"] == "version.json"

    monkeypatch.setattr(autophone, "__version__", "0.0.0+unknown")
    with pytest.raises(AssertionError):
        verify_core_install.verify_runtime_version()

    # Installed wheel/sdist branch strictly requires package_metadata and kernel version parity.
    monkeypatch.setattr(version_mod, "_is_git_checkout", lambda: False)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.0" if name == "autophone-kernel" else "0.0.0")
    monkeypatch.setattr(autophone, "__version__", "0.1.0")
    monkeypatch.setattr(version_mod, "build_identity", lambda: {"version": "0.1.0", "source": "package_metadata"})
    assert verify_core_install.verify_runtime_version()["source"] == "package_metadata"

    monkeypatch.setattr(version_mod, "build_identity", lambda: {"version": "0.1.0", "source": "git"})
    with pytest.raises(AssertionError):
        verify_core_install.verify_runtime_version()

    monkeypatch.setattr(autophone, "__version__", "0.2.0")
    monkeypatch.setattr(version_mod, "build_identity", lambda: {"version": "0.2.0", "source": "package_metadata"})
    with pytest.raises(AssertionError):
        verify_core_install.verify_runtime_version()


# ── 4. scripts/check_public_content.py ─────────────────────────────────────────


def test_check_public_content_legal_and_version_parity(tmp_path: Path) -> None:
    _populate_public_repo_skeleton(tmp_path, version="0.1.0")
    assert check_public_content.check(tmp_path) == []

    # Legal file drift in a component or vphone_mcp is caught.
    _write(tmp_path / "vphone_mcp" / "NOTICE", "drifted notice\n")
    legal_errors = check_public_content.check(tmp_path)
    assert any("vphone_mcp/NOTICE" in err for err in legal_errors)
    _write(tmp_path / "vphone_mcp" / "NOTICE", (tmp_path / "NOTICE").read_text(encoding="utf-8"))

    # Subpackage project.version mismatch is caught.
    _write(
        tmp_path / "pkgs" / "autophone-rig" / "pyproject.toml",
        '[project]\nname = "autophone-rig"\nversion = "0.2.0"\ndependencies = ["autophone-kernel==0.1.0"]\n',
    )
    ver_errors = check_public_content.check(tmp_path)
    assert any("pkgs/autophone-rig/pyproject.toml" in err and "0.2.0" in err for err in ver_errors)

    # Internal dependency pin drift is caught.
    _write(
        tmp_path / "pkgs" / "autophone-rig" / "pyproject.toml",
        '[project]\nname = "autophone-rig"\nversion = "0.1.0"\ndependencies = ["autophone-kernel>=0.1.0"]\n',
    )
    pin_errors = check_public_content.check(tmp_path)
    assert any("autophone-kernel>=0.1.0" in err and "must pin ==0.1.0" in err for err in pin_errors)


def test_check_git_history_catches_dirty_emails_and_patches(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _populate_public_repo_skeleton(tmp_path)
    _commit_all(tmp_path, "clean base")
    assert check_public_content.check_git_history(tmp_path) == []

    # Add a commit with a dirty author email and a leaked path, then remove the file in a follow-up commit.
    leaked_line = "root = '/Us" + "ers/dev/private'\n"
    dirty_email = "dev@SGH" + "QCZ0001.local"
    _write(tmp_path / "leaked.py", leaked_line)
    _commit_all(tmp_path, "accidental leak", author_email=dirty_email)

    (tmp_path / "leaked.py").unlink()
    _commit_all(tmp_path, "remove leak later", committer_email="ci@localhost")

    # Working tree check is clean, but git history check catches BOTH emails and the historical added line!
    assert check_public_content.check(tmp_path) == []
    history_errors = check_public_content.check_git_history(tmp_path)
    assert any("author" in err and "forbidden email marker" in err for err in history_errors)
    assert any("committer" in err and "@localhost" in err for err in history_errors)
    assert any("leaked.py:1: forbidden hardcoded macOS user path" in err for err in history_errors)


# ── 5. scripts/prepare_public_snapshot.py ──────────────────────────────────────


def test_prepare_public_snapshot_exports_clean_git_repo_without_touching_source(tmp_path: Path) -> None:
    source = tmp_path / "source-repo"
    _init_repo(source)
    _populate_public_repo_skeleton(source)
    script_file = _write(source / "scripts" / "tool.sh", "#!/usr/bin/env bash\necho ok\n")
    os.chmod(script_file, 0o755)
    removed_file = _write(source / "obsolete.txt", "will be removed in index\n")
    # Seed source repo with a dirty commit in history so we prove snapshot scrubs history.
    dirty_email = "dev@yum" + "china.com"
    _commit_all(source, "dirty source commit", author_email=dirty_email)
    source_head_before = _run_git(source, "rev-parse", "HEAD")

    # Stage a deletion in index and place untracked private artifacts in working tree.
    _run_git(source, "rm", "obsolete.txt")
    assert not removed_file.exists()
    private_cfg = _write(source / "config" / "kfc-merchant-emulator.local.yaml", "secret: 1\n")
    guest_bin = _write(source / "tools" / "guest" / "ax_dump.dylib", b"\x00\x01")
    phase5_shot = _write(source / "tools" / "phase5" / "runs" / "shot.jpg", b"\xff\xd8")

    # Reject forbidden author email before creating destination.
    bad_out = tmp_path / "bad-snapshot"
    with pytest.raises(ValueError, match="Rejected --author-email"):
        prepare_public_snapshot.prepare_snapshot(
            bad_out,
            repo=source,
            init_git=True,
            author_name="Bot",
            author_email="bot@mac.local",
        )
    assert not bad_out.exists()

    # Reject --tag without --init-git.
    with pytest.raises(ValueError, match="--tag requires --init-git"):
        prepare_public_snapshot.prepare_snapshot(
            bad_out,
            repo=source,
            init_git=False,
            tag="v0.1.0",
        )

    # Export clean snapshot with --init-git and --tag.
    clean_out = tmp_path / "clean-snapshot"
    result = prepare_public_snapshot.prepare_snapshot(
        clean_out,
        repo=source,
        init_git=True,
        branch="main",
        author_name="Release Bot",
        author_email="release@example.com",
        message="Public release v0.1.0",
        tag="v0.1.0",
    )
    assert result["git_initialized"] is True
    assert result["branch"] == "main"
    assert result["tag"] == "v0.1.0"
    assert result["commit"]

    # Source repo was never modified and untracked user files remain intact.
    assert _run_git(source, "rev-parse", "HEAD") == source_head_before
    assert private_cfg.is_file()
    assert guest_bin.is_file()
    assert phase5_shot.is_file()

    # Exported snapshot contains tracked files with executable bits, no untracked/deleted files.
    exported_script = clean_out / "scripts" / "tool.sh"
    assert exported_script.is_file()
    assert bool(exported_script.stat().st_mode & stat.S_IXUSR)
    assert not (clean_out / "obsolete.txt").exists()
    assert not (clean_out / "config" / "kfc-merchant-emulator.local.yaml").exists()
    assert not (clean_out / "tools" / "guest" / "ax_dump.dylib").exists()
    assert not (clean_out / "tools" / "phase5" / "runs" / "shot.jpg").exists()

    # Snapshot passes BOTH working-tree and full git history checks, has 1 commit & tag v0.1.0.
    assert check_public_content.check(clean_out) == []
    assert check_public_content.check_git_history(clean_out) == []
    assert _run_git(clean_out, "rev-list", "--count", "HEAD") == "1"
    assert _run_git(clean_out, "tag", "--list") == "v0.1.0"

    # Existing destination is refused.
    with pytest.raises(FileExistsError):
        prepare_public_snapshot.prepare_snapshot(clean_out, repo=source)

    # Non-git snapshot export also succeeds and passes check().
    plain_out = tmp_path / "plain-snapshot"
    plain = prepare_public_snapshot.prepare_snapshot(plain_out, repo=source, init_git=False)
    assert plain["git_initialized"] is False
    assert not (plain_out / ".git").exists()
    assert check_public_content.check(plain_out) == []


def test_prepare_public_snapshot_head_preserves_hidden_dirs_and_matches_commit(tmp_path: Path) -> None:
    source = tmp_path / "source-repo"
    _init_repo(source)
    _populate_public_repo_skeleton(source)
    _write(source / ".gitignore", "*.pyc\n")
    _write(source / ".github" / "workflows" / "ci.yml", "name: CI\n")
    _write(source / ".github" / "workflows" / "release.yml", "name: Release\n")
    _write(source / ".claude" / "skills" / "vm-bringup" / "SKILL.md", "# VM Bring-Up\n")
    _write(source / ".claude" / "skills" / "package-store" / "SKILL.md", "# Package Store\n")
    _write(
        source / "AGENTS.md",
        "# Agents\n\n"
        "- [vm-bringup](.claude/skills/vm-bringup/SKILL.md)\n"
        "- [package-store](.claude/skills/package-store/SKILL.md)\n",
    )
    script_file = _write(source / "scripts" / "tool.sh", "#!/usr/bin/env bash\necho ok\n")
    os.chmod(script_file, 0o755)
    _commit_all(source, "release candidate commit")

    expected_head_paths = set(_run_git(source, "ls-tree", "-r", "--name-only", "HEAD").splitlines())

    # Create unstaged, staged-only, and untracked changes after HEAD to prove --source head reads HEAD only.
    _write(source / "README.md", "# Unstaged Working Tree Edit\n")
    _write(source / "staged_only.txt", "staged but not committed\n")
    _run_git(source, "add", "staged_only.txt")
    _write(source / "untracked.txt", "untracked file\n")

    head_out = tmp_path / "head-snapshot"
    result = prepare_public_snapshot.prepare_snapshot(
        head_out,
        repo=source,
        source="head",
        init_git=True,
        branch="main",
        author_name="Release Bot",
        author_email="release@example.com",
        message="Public release v0.1.0",
        tag="v0.1.0",
    )
    assert result["source"] == "head"
    assert result["file_count"] == len(expected_head_paths)

    exported_paths = {
        p.relative_to(head_out).as_posix()
        for p in head_out.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(head_out).parts
    }
    assert exported_paths == expected_head_paths
    assert (head_out / ".github" / "workflows" / "ci.yml").is_file()
    assert (head_out / ".github" / "workflows" / "release.yml").is_file()
    assert (head_out / ".claude" / "skills" / "vm-bringup" / "SKILL.md").is_file()
    assert (head_out / ".claude" / "skills" / "package-store" / "SKILL.md").is_file()
    assert (head_out / ".gitignore").is_file()
    assert not (head_out / "github").exists()
    assert not (head_out / "claude").exists()
    assert not (head_out / "gitignore").exists()
    assert not (head_out / "staged_only.txt").exists()
    assert not (head_out / "untracked.txt").exists()
    assert (head_out / "README.md").read_text(encoding="utf-8") == "# Autophone\n\nSee [LICENSE](LICENSE).\n"
    assert bool((head_out / "scripts" / "tool.sh").stat().st_mode & stat.S_IXUSR)

    assert check_public_content.check(head_out) == []
    assert check_public_content.check_git_history(head_out) == []


def test_prepare_public_snapshot_normalize_archive_member_path() -> None:
    norm = prepare_public_snapshot._normalize_archive_member_path
    assert norm(".github/workflows/release.yml") == Path(".github/workflows/release.yml")
    assert norm("./.claude/skills/vm-bringup/SKILL.md") == Path(".claude/skills/vm-bringup/SKILL.md")
    assert norm("./.gitignore") == Path(".gitignore")
    assert norm("././README.md") == Path("README.md")
    for bad in ("", ".", "./", "/etc/passwd", "../escape", "a/../escape", "a\x00b"):
        with pytest.raises(ValueError, match="invalid archive member path"):
            norm(bad)


# ── 6. scripts/audit_release_compliance.py & workflow supply-chain gates ───────


class _FakeMetadata:
    def __init__(
        self,
        name: str,
        *,
        license_expr: str | None = None,
        license_header: str | None = None,
        classifiers: list[str] | None = None,
    ) -> None:
        self._single: dict[str, str] = {"Name": name}
        if license_expr is not None:
            self._single["License-Expression"] = license_expr
        if license_header is not None:
            self._single["License"] = license_header
        self._classifiers = list(classifiers or [])

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._single.get(key, default)

    def get_all(self, key: str) -> list[str] | None:
        if key == "Classifier":
            return list(self._classifiers)
        val = self._single.get(key)
        return [val] if val is not None else None


def _fake_dist(
    name: str,
    version: str = "1.0.0",
    *,
    license_expr: str | None = None,
    license_header: str | None = None,
    classifiers: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        version=version,
        metadata=_FakeMetadata(
            name,
            license_expr=license_expr,
            license_header=license_header,
            classifiers=classifiers,
        ),
    )


def _baseline_first_party_dists(version: str = "0.1.0") -> list[SimpleNamespace]:
    return [
        _fake_dist(pkg, version, license_expr="Apache-2.0")
        for pkg in PACKAGES
    ]


def test_audit_release_compliance_accepts_permissive_and_disclosed_exceptions() -> None:
    dists = [
        *_baseline_first_party_dists("0.1.0"),
        _fake_dist("fastapi", "0.136.3", license_expr="MIT"),
        _fake_dist("pillow", "13.0.0", license_expr="MIT-CMU"),
        _fake_dist("packaging", "26.2", license_expr="Apache-2.0 OR BSD-2-Clause"),
        _fake_dist("typing_extensions", "4.16.0", license_expr="PSF-2.0"),
        _fake_dist("isc-helper", "1.0.0", license_expr="ISC"),
        _fake_dist("annotated-types", "0.7.0", classifiers=["License :: OSI Approved :: MIT License"]),
        _fake_dist(
            "python-dateutil",
            "2.9.0.post0",
            license_header="Dual License",
            classifiers=[
                "License :: OSI Approved :: BSD License",
                "License :: OSI Approved :: Apache Software License",
            ],
        ),
        _fake_dist(
            "qrcode",
            "8.2",
            license_header="BSD",
            classifiers=[
                "License :: OSI Approved :: BSD License",
                "License :: Other/Proprietary License",
            ],
        ),
        _fake_dist(
            "certifi",
            "2026.11.20",
            license_header="MPL-2.0",
            classifiers=["License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)"],
        ),
        _fake_dist("psycopg", "3.3.4", license_expr="LGPL-3.0-only"),
        _fake_dist("psycopg_binary", "3.3.4", license_expr="LGPL-3.0-only"),
    ]

    result = audit_release_compliance.audit_distributions(dists)
    assert result["violations"] == []
    assert result["first_party"] == 5
    assert result["third_party"] == 11
    by_name = {entry["name"]: entry for entry in result["packages"]}
    assert by_name["autophone"]["policy"] == "first-party"
    assert by_name["pillow"]["policy"] == "permissive"
    assert by_name["packaging"]["policy"] == "permissive"
    assert by_name["python-dateutil"]["policy"] == "permissive"
    assert by_name["qrcode"]["policy"] == "permissive"
    assert by_name["certifi"]["policy"] == "disclosed-exception"
    assert by_name["psycopg"]["policy"] == "disclosed-exception"
    assert by_name["psycopg-binary"]["policy"] == "disclosed-exception"
    assert {item["name"] for item in result["disclosed_exceptions"]} == {
        "certifi",
        "psycopg",
        "psycopg-binary",
    }


@pytest.mark.parametrize(
    "bad_dist",
    [
        _fake_dist("sneaky-gpl", "1.0.0", license_expr="GPL-3.0-only"),
        _fake_dist("sneaky-agpl", "1.0.0", license_expr="AGPL-3.0-or-later"),
        _fake_dist("mixed-gpl", "1.0.0", license_expr="MIT AND GPL-2.0-only"),
        _fake_dist("undisclosed-lgpl", "1.0.0", license_expr="LGPL-3.0-only"),
        _fake_dist("undisclosed-mpl", "1.0.0", license_expr="MPL-2.0"),
        _fake_dist("unknown-pkg", "1.0.0"),
        _fake_dist("proprietary-pkg", "1.0.0", classifiers=["License :: Other/Proprietary License"]),
        _fake_dist(
            "gpl-classifier",
            "1.0.0",
            classifiers=["License :: OSI Approved :: GNU General Public License v3 (GPLv3)"],
        ),
        _fake_dist(
            "conflicting-header",
            "1.0.0",
            license_header="MIT",
            classifiers=["License :: OSI Approved :: GNU General Public License v3 (GPLv3)"],
        ),
    ],
)
def test_audit_release_compliance_rejects_disallowed_or_unknown_licenses(bad_dist: SimpleNamespace) -> None:
    dists = [*_baseline_first_party_dists("0.1.0"), bad_dist]
    result = audit_release_compliance.audit_distributions(dists)
    assert len(result["violations"]) == 1
    assert bad_dist.name.replace("_", "-") in result["violations"][0]


def test_audit_release_compliance_spdx_with_exceptions() -> None:
    permissive = audit_release_compliance.ALLOWED_PERMISSIVE_LICENSES
    permissive_with_exc = permissive | audit_release_compliance.ALLOWED_PERMISSIVE_LICENSE_EXCEPTIONS

    # Recognized permissive license+exception combinations pass when enabled, fail without exception allowlist.
    assert audit_release_compliance.evaluate_spdx_expression("Apache-2.0 WITH LLVM-exception", permissive_with_exc)
    assert audit_release_compliance.evaluate_spdx_expression(
        "Apache-2.0 WITH LLVM-exception OR MIT",
        permissive_with_exc,
    )
    assert not audit_release_compliance.evaluate_spdx_expression("Apache-2.0 WITH LLVM-exception", permissive)

    # Unknown or unreviewed license+exception combinations always fail closed.
    for expr in (
        "MIT WITH Unrecognized-exception",
        "MIT WITH LLVM-exception",
        "Apache-2.0 WITH Unrecognized-exception",
        "GPL-3.0-only WITH GCC-exception-3.1",
        "MIT AND (Apache-2.0 WITH Custom-Exception)",
        "Apache-2.0 WITH LLVM-exception WITH Extra",
    ):
        assert not audit_release_compliance.evaluate_spdx_expression(expr, permissive)
        assert not audit_release_compliance.evaluate_spdx_expression(expr, permissive_with_exc)

    # Audit accepts recognized Apache-2.0 WITH LLVM-exception and dual-licensed fallback to MIT.
    allowed_dists = [
        *_baseline_first_party_dists("0.1.0"),
        _fake_dist("llvm-helper", "1.0.0", license_expr="Apache-2.0 WITH LLVM-exception"),
        _fake_dist("dual-llvm", "1.0.0", license_expr="Apache-2.0 WITH LLVM-exception OR MIT"),
        _fake_dist("dual-mit-fallback", "1.0.0", license_expr="MIT OR (Apache-2.0 WITH Unrecognized-exception)"),
    ]
    ok_result = audit_release_compliance.audit_distributions(allowed_dists)
    assert ok_result["violations"] == []
    by_name = {entry["name"]: entry for entry in ok_result["packages"]}
    assert by_name["llvm-helper"]["policy"] == "permissive"
    assert by_name["dual-llvm"]["policy"] == "permissive"
    assert by_name["dual-mit-fallback"]["policy"] == "permissive"

    # Unreviewed license+exception pairs fail closed, report the exact expression, and require manual review.
    for pkg_name, expr, expected_combo in (
        ("unknown-exc-pkg", "MIT WITH Unrecognized-exception", "MIT WITH Unrecognized-exception"),
        ("mismatched-exc-pkg", "MIT WITH LLVM-exception", "MIT WITH LLVM-exception"),
        ("compound-exc-pkg", "MIT AND (Apache-2.0 WITH Custom-Exception)", "Apache-2.0 WITH Custom-Exception"),
    ):
        res = audit_release_compliance.audit_distributions([
            *_baseline_first_party_dists("0.1.0"),
            _fake_dist(pkg_name, "1.0.0", license_expr=expr),
        ])
        assert len(res["violations"]) == 1
        violation = res["violations"][0]
        assert pkg_name in violation
        assert expected_combo in violation
        assert "manual review" in violation
        entry = next(p for p in res["packages"] if p["name"] == pkg_name)
        assert entry["allowed"] is False
        assert entry["policy"] == "review-required"
        assert entry["unreviewed_exceptions"] == [expected_combo]

    # First-party packages still require plain Apache-2.0 without exceptions.
    fp_dists = [
        _fake_dist("autophone", "0.1.0", license_expr="Apache-2.0 WITH LLVM-exception"),
        *(_fake_dist(pkg, "0.1.0", license_expr="Apache-2.0") for pkg in PACKAGES[1:]),
    ]
    fp_res = audit_release_compliance.audit_distributions(fp_dists)
    assert len(fp_res["violations"]) == 1
    assert "Apache-2.0 WITH LLVM-exception" in fp_res["violations"][0]
    assert "manual review" in fp_res["violations"][0]


def test_audit_release_compliance_report_archives_checksums_and_git_history(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _populate_public_repo_skeleton(repo, version="0.1.0")
    commit_sha = _commit_all(repo, "clean release commit")

    dist_dir = tmp_path / "dist"
    _create_synthetic_archive_set(dist_dir, tmp_path / "schema-root")
    checksums_path = dist_dir / "SHA256SUMS.txt"
    lines = []
    for artifact in sorted([*dist_dir.glob("*.whl"), *dist_dir.glob("*.tar.gz")], key=lambda p: p.name):
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        lines.append(f"{digest}  {artifact.name}")
    checksums_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    dists = [
        *_baseline_first_party_dists("0.1.0"),
        _fake_dist("requests", "2.34.2", license_header="Apache-2.0"),
        _fake_dist("certifi", "2026.11.20", license_header="MPL-2.0"),
    ]
    report, errors = audit_release_compliance.build_compliance_report(
        dist_dir=dist_dir,
        checksums_path=checksums_path,
        repo=repo,
        check_history=True,
        tag="v0.1.0",
        distributions=dists,
    )
    assert errors == []
    assert report["summary"]["status"] == "passed"
    assert report["summary"]["archive_count"] == 10
    assert report["release"] == {"version": "0.1.0", "tag": "v0.1.0", "commit": commit_sha}
    assert report["git_history_check"] == {
        "checked": True,
        "status": "passed",
        "rev": "HEAD",
        "error_count": 0,
    }
    assert report["license_policy"]["allowed_permissive_exceptions"] == ["Apache-2.0 WITH LLVM-exception"]
    assert len(report["archives"]) == 10

    # Tampering with SHA256SUMS.txt must fail compliance verification.
    checksums_path.write_text(
        lines[0].replace(lines[0][:8], "00000000", 1) + "\n" + "\n".join(lines[1:]) + "\n",
        encoding="utf-8",
    )
    _, checksum_errors = audit_release_compliance.build_compliance_report(
        dist_dir=dist_dir,
        checksums_path=checksums_path,
        repo=repo,
        check_history=True,
        distributions=dists,
    )
    assert any("archive SHA-256 mismatch" in err for err in checksum_errors)

    # Restore checksums, introduce a dirty commit, and verify git history fails compliance.
    checksums_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _write(repo / "bad.py", "p = '/Us" + "ers/dev/secret'\n")
    _commit_all(repo, "bad commit")
    dirty_report, dirty_errors = audit_release_compliance.build_compliance_report(
        dist_dir=dist_dir,
        checksums_path=checksums_path,
        repo=repo,
        check_history=True,
        distributions=dists,
    )
    assert dirty_report["git_history_check"]["status"] == "failed"
    assert any("bad.py:1" in err for err in dirty_errors)


def test_github_workflows_pin_all_actions_to_full_commit_shas_and_enforce_release_gates() -> None:
    workflows_dir = check_public_content.ROOT / ".github" / "workflows"
    workflow_files = sorted(workflows_dir.glob("*.yml"))
    assert {p.name for p in workflow_files} == {"ci.yml", "docs.yml", "release.yml"}

    sha_pin_re = re.compile(r"^[^/\s]+/[^@\s]+@[0-9a-f]{40}$")
    loaded: dict[str, dict] = {}
    for wf_path in workflow_files:
        data = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        loaded[wf_path.name] = data
        for job_name, job in data["jobs"].items():
            for step in job.get("steps", []):
                uses = step.get("uses")
                if uses is None:
                    continue
                assert sha_pin_re.fullmatch(uses), (
                    f"{wf_path.name}:{job_name}: action {uses!r} must be pinned to a full 40-char commit SHA"
                )

    release = loaded["release.yml"]
    triggers = release.get("on", release.get(True))
    assert triggers["push"]["tags"] == ["v*.*.*"]
    assert "workflow_dispatch" in triggers
    assert triggers["workflow_dispatch"] is None or "publish_pypi" not in (
        triggers["workflow_dispatch"].get("inputs") or {}
    )
    assert release["permissions"] == {"contents": "read"}
    jobs = release["jobs"]

    build_job = jobs["build-and-verify"]
    assert "permissions" not in build_job
    assert build_job.get("outputs") == {"tag_verified": "${{ steps.verify-tag.outputs.verified }}"}
    checkout_step = build_job["steps"][0]
    assert checkout_step["uses"].startswith("actions/checkout@")
    assert checkout_step.get("with", {}).get("fetch-depth") == 0
    verify_tag_step = next(s for s in build_job["steps"] if s.get("id") == "verify-tag")
    assert verify_tag_step["if"] == "startsWith(github.ref, 'refs/tags/v')"
    assert 'echo "verified=true" >> "$GITHUB_OUTPUT"' in verify_tag_step["run"]
    build_runs = "\n".join(step.get("run", "") for step in build_job["steps"])
    assert "check_public_content.py --check-git-history" in build_runs
    assert "audit_release_compliance.py" in build_runs
    assert "RELEASE-COMPLIANCE.json" in build_runs
    upload_step = next(s for s in build_job["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@"))
    assert "dist/core/RELEASE-COMPLIANCE.json" in upload_step["with"]["path"]

    expected_release_gate = "startsWith(github.ref, 'refs/tags/v') && needs.build-and-verify.outputs.tag_verified == 'true'"

    attest_job = jobs["attest"]
    assert attest_job["needs"] == "build-and-verify"
    assert attest_job["if"] == expected_release_gate
    assert attest_job["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
    }
    attest_runs = "\n".join(step.get("run", "") for step in attest_job["steps"])
    assert "sha256sum -c SHA256SUMS.txt" in attest_runs
    attest_step = next(
        s for s in attest_job["steps"] if str(s.get("uses", "")).startswith("actions/attest-build-provenance@")
    )
    assert attest_step["with"] == {"subject-checksums": "dist/core/SHA256SUMS.txt"}

    gh_release_job = jobs["github-release"]
    assert gh_release_job["needs"] == ["build-and-verify", "attest"]
    assert gh_release_job["if"] == expected_release_gate
    assert gh_release_job["permissions"] == {"contents": "write"}
    gh_release_step = next(
        s for s in gh_release_job["steps"] if str(s.get("uses", "")).startswith("softprops/action-gh-release@")
    )
    assert "dist/core/RELEASE-COMPLIANCE.json" in gh_release_step["with"]["files"]
    assert "dist/core/SHA256SUMS.txt" in gh_release_step["with"]["files"]

    pypi_job = jobs["publish-pypi"]
    assert pypi_job["needs"] == ["build-and-verify", "attest"]
    assert pypi_job["if"] == expected_release_gate
    assert pypi_job["permissions"] == {"id-token": "write"}
    pypi_runs = "\n".join(step.get("run", "") for step in pypi_job["steps"])
    assert "sha256sum -c SHA256SUMS.txt" in pypi_runs
    assert "rm -f dist/core/SHA256SUMS.txt dist/core/RELEASE-COMPLIANCE.json" in pypi_runs
