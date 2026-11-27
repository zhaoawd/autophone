"""Check candidate files for symlinks, broken Markdown links, and private leaks."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import tomllib
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\]\(\s*(?:<([^>]+)>|([^()\s]+))(?:\s+[\"'][^\"']*[\"'])?\s*\)")
SKIP_CONTENT_SCAN = {"uv.lock", "package-lock.json", "vphone_mcp/uv.lock", "scripts/check_public_content.py"}
FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("hardcoded macOS user path", re.compile(r"/Users/[A-Za-z0-9._-]+/")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("internal corporate domain or host", re.compile(r"(?:yumchina\.com|SGHQCZ[0-9A-Za-z_-]*)", re.IGNORECASE)),
    ("merge conflict marker", re.compile(r"^(?:<<<<<<< |>>>>>>> )")),
)
FORBIDDEN_EMAIL_PATTERN = re.compile(
    r"(?:@localhost\b|\.local(?:\b|$)|SGHQCZ|yumchina)",
    re.IGNORECASE,
)
COMPONENT_DIRS: tuple[str, ...] = (
    "pkgs/autophone-kernel",
    "pkgs/autophone-rig",
    "pkgs/autophone-domain",
    "pkgs/autophone-console",
)
ALL_PACKAGE_DIRS: tuple[str, ...] = (
    *COMPONENT_DIRS,
    "vphone_mcp",
)
LEGAL_FILES: tuple[str, ...] = ("LICENSE", "NOTICE")
PYPROJECT_PATHS: tuple[str, ...] = (
    "pyproject.toml",
    *(f"{pkg_dir}/pyproject.toml" for pkg_dir in ALL_PACKAGE_DIRS),
)
_INTERNAL_DEP_RE = re.compile(r"^(autophone-[a-z0-9_-]+)(?:\[[^\]]*\])?\s*(.*)$", re.IGNORECASE)
_EXACT_PIN_RE = re.compile(r"^==\s*([^\s;,]+)(?:\s*;.*)?$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _git_files(root: Path, *extra_args: str) -> list[str]:
    out = subprocess.check_output(["git", "ls-files", "-z", *extra_args], cwd=root)
    return [item for item in out.decode("utf-8", errors="replace").split("\0") if item]


def _walk_files(root: Path) -> list[str]:
    results: list[str] = []
    for path in sorted(root.rglob("*")):
        rel_path = path.relative_to(root)
        if ".git" in rel_path.parts:
            continue
        if path.is_symlink() or path.is_file():
            results.append(rel_path.as_posix())
    return results


def check_email(email: str) -> str | None:
    """Return an error description if an author/committer email fails public hygiene."""
    value = (email or "").strip()
    if not value or "@" not in value:
        return f"invalid commit email {email!r}"
    match = FORBIDDEN_EMAIL_PATTERN.search(value)
    if match:
        return f"forbidden email marker {match.group(0)!r} in {value!r}"
    return None


def check_legal_consistency(root: Path = ROOT) -> list[str]:
    root = root.resolve()
    problems: list[str] = []
    for filename in LEGAL_FILES:
        root_file = root / filename
        if root_file.is_symlink() or not root_file.is_file():
            problems.append(f"{filename}: missing required legal file")
            continue
        expected = root_file.read_bytes()
        for pkg_dir in ALL_PACKAGE_DIRS:
            rel = f"{pkg_dir}/{filename}"
            target = root / rel
            if target.is_symlink() or not target.is_file():
                problems.append(f"{rel}: missing required legal file")
                continue
            if target.read_bytes() != expected:
                problems.append(f"{rel}: does not match root {filename}")
    return problems


def check_version_consistency(root: Path = ROOT) -> list[str]:
    root = root.resolve()
    problems: list[str] = []
    parsed: dict[str, dict] = {}
    for rel in PYPROJECT_PATHS:
        path = root / rel
        if not path.is_file():
            problems.append(f"{rel}: missing pyproject.toml")
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            problems.append(f"{rel}: failed to parse TOML: {exc}")
            continue
        parsed[rel] = data

    root_project = parsed.get("pyproject.toml", {}).get("project")
    root_version = root_project.get("version") if isinstance(root_project, dict) else None
    if not isinstance(root_version, str) or not root_version.strip():
        problems.append("pyproject.toml: missing project.version")
        return problems

    for rel, data in parsed.items():
        project = data.get("project")
        if not isinstance(project, dict):
            problems.append(f"{rel}: missing [project] table")
            continue
        version = project.get("version")
        if version != root_version:
            problems.append(f"{rel}: project.version {version!r} != root version {root_version!r}")
        dependencies = project.get("dependencies") or []
        if isinstance(dependencies, list):
            for dep in dependencies:
                if not isinstance(dep, str):
                    continue
                match = _INTERNAL_DEP_RE.match(dep.strip())
                if not match:
                    continue
                spec = match.group(2).strip()
                pin_match = _EXACT_PIN_RE.fullmatch(spec)
                if not pin_match or pin_match.group(1) != root_version:
                    problems.append(
                        f"{rel}: internal dependency {dep!r} must pin =={root_version}"
                    )
    return problems


def check_git_history(root: Path = ROOT, rev: str = "HEAD") -> list[str]:
    root = root.resolve()
    problems: list[str] = []

    meta_out = subprocess.check_output(
        ["git", "-C", str(root), "log", "--format=%H%x1f%ae%x1f%ce", rev],
        text=True,
        errors="replace",
    )
    for row in meta_out.splitlines():
        if not row:
            continue
        parts = row.split("\x1f")
        if len(parts) != 3:
            continue
        commit_hash, author_email, committer_email = parts
        short_hash = commit_hash[:12]
        for role, email in (("author", author_email), ("committer", committer_email)):
            err = check_email(email)
            if err:
                problems.append(f"git:{short_hash}: {role} {err}")

    proc = subprocess.Popen(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "diff.noprefix=false",
            "-c",
            "core.quotepath=false",
            "log",
            "-p",
            "--no-color",
            rev,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    assert proc.stdout is not None
    assert proc.stderr is not None
    current_commit = "unknown"
    current_file: str | None = None
    in_hunk = False
    old_rem = 0
    new_rem = 0
    new_line = 0
    try:
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\r\n")
            if not in_hunk:
                if line.startswith("commit "):
                    current_commit = line.split()[1][:12]
                    current_file = None
                elif line.startswith("diff --git "):
                    current_file = None
                elif line.startswith("+++ b/"):
                    current_file = line[6:]
                elif line.startswith("+++ /dev/null"):
                    current_file = None
                elif line.startswith("@@ "):
                    match = _HUNK_RE.match(line)
                    if match:
                        old_rem = int(match.group(2)) if match.group(2) is not None else 1
                        new_line = int(match.group(3))
                        new_rem = int(match.group(4)) if match.group(4) is not None else 1
                        in_hunk = old_rem > 0 or new_rem > 0
                continue

            if line.startswith("\\"):
                continue
            if line.startswith("+"):
                content = line[1:]
                if current_file and current_file not in SKIP_CONTENT_SCAN:
                    for label, pattern in FORBIDDEN_PATTERNS:
                        if pattern.search(content):
                            problems.append(
                                f"git:{current_commit}:{current_file}:{new_line}: forbidden {label}"
                            )
                new_line += 1
                new_rem -= 1
            elif line.startswith("-"):
                old_rem -= 1
            elif line.startswith(" "):
                new_line += 1
                old_rem -= 1
                new_rem -= 1
            else:
                in_hunk = False
                continue

            if old_rem <= 0 and new_rem <= 0:
                in_hunk = False
    finally:
        proc.stdout.close()
        stderr_text = proc.stderr.read()
        proc.stderr.close()
        returncode = proc.wait()
    if returncode != 0:
        raise subprocess.CalledProcessError(returncode, proc.args, stderr=stderr_text)
    return problems


def check(root: Path = ROOT, paths: list[str] | None = None) -> list[str]:
    root = root.resolve()
    explicit_paths = paths is not None
    if paths is None:
        if (root / ".git").exists():
            paths = _git_files(root, "--cached", "--others", "--exclude-standard")
            tracked_for_hygiene = set(_git_files(root, "--cached"))
        else:
            paths = _walk_files(root)
            tracked_for_hygiene = set(paths)
    else:
        tracked_for_hygiene = set(filter(None, paths))
    files = {(root / name).resolve() for name in paths if name and (root / name).is_file()}
    directories = {parent for file in files for parent in file.parents if parent.is_relative_to(root)}
    tracked = files | directories
    problems = []
    for name in filter(None, paths):
        path = root / name
        if path.is_symlink():
            problems.append(f"{name}: unexpected symbolic link")
            continue
        if not path.is_file():
            problems.append(f"{name}: missing regular file")
            continue
        if (explicit_paths or name in tracked_for_hygiene) and name not in SKIP_CONTENT_SCAN:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None:
                for number, line in enumerate(text.splitlines(), 1):
                    for label, pattern in FORBIDDEN_PATTERNS:
                        if pattern.search(line):
                            problems.append(f"{name}:{number}: forbidden {label}")
        if path.suffix.lower() != ".md":
            continue
        fence = None
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            marker = re.match(r"^\s*(`{3,}|~{3,})", line)
            if marker and (fence is None or marker[1].startswith(fence)):
                fence = marker[1] if fence is None else None
                continue
            if fence:
                continue
            for match in LINK.finditer(line):
                dest = urlsplit(match[1] or match[2])
                if dest.scheme and dest.scheme != "file" or not dest.path:
                    continue
                target = Path(unquote(dest.path))
                if dest.scheme == "file" or target.is_absolute() or (path.parent / target).resolve() not in tracked:
                    problems.append(f"{name}:{number}: {target}")
    if not explicit_paths:
        problems.extend(check_legal_consistency(root))
        problems.extend(check_version_consistency(root))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check public repository content, legal files, and versions.")
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Repository or snapshot root directory to inspect (default: repository root).",
    )
    parser.add_argument(
        "--check-git-history",
        action="store_true",
        help="Scan git commit author/committer emails and patch history for forbidden markers.",
    )
    parser.add_argument(
        "--rev",
        default="HEAD",
        help="Git revision range for --check-git-history (default: HEAD).",
    )
    args = parser.parse_args(argv)
    errors = check(args.root)
    if args.check_git_history:
        errors.extend(check_git_history(args.root, rev=args.rev))
    if errors:
        raise SystemExit("\n".join(errors))
    print("Public content checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
