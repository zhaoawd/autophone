"""Export a clean, non-destructive public snapshot from tracked git files."""
from __future__ import annotations

import argparse
import io
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Any

try:
    from scripts.check_public_content import check, check_email, check_git_history
except ModuleNotFoundError:
    _scripts_dir = str(Path(__file__).resolve().parent)
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
    from check_public_content import check, check_email, check_git_history


ROOT = Path(__file__).resolve().parents[1]
_GIT_LOCATION_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_REFLOG_ACTION",
    "GIT_CEILING_DIRECTORIES",
)


def _clean_git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_ENV}
    if extra:
        env.update(extra)
    return env


def _git_config_value(repo: Path, key: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(repo), "config", "--get", key],
        check=False,
        capture_output=True,
        text=True,
        env=_clean_git_env(),
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _export_from_index(repo: Path, dest: Path) -> int:
    output = subprocess.check_output(
        ["git", "-C", str(repo), "ls-files", "-z", "--cached"],
        env=_clean_git_env(),
    )
    rel_paths = [item for item in output.decode("utf-8", errors="replace").split("\0") if item]
    count = 0
    for rel in rel_paths:
        src = repo / rel
        if src.is_symlink():
            raise ValueError(f"{rel}: unexpected symbolic link")
        if not src.is_file():
            continue
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        shutil.copymode(src, dst)
        count += 1
    return count


def _normalize_archive_member_path(name: str) -> Path:
    raw = name
    while raw.startswith("./"):
        raw = raw[2:]
    if not raw or raw == "." or raw.startswith("/") or "\x00" in raw:
        raise ValueError(f"{name}: invalid archive member path")
    rel = PurePosixPath(raw)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError(f"{name}: invalid archive member path")
    return Path(*rel.parts)


def _export_from_head(repo: Path, dest: Path) -> int:
    archive_bytes = subprocess.check_output(
        ["git", "-C", str(repo), "archive", "--format=tar", "HEAD"],
        env=_clean_git_env(),
    )
    count = 0
    dest_resolved = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
        for member in archive:
            if member.isdir():
                continue
            if member.issym() or member.islnk():
                raise ValueError(f"{member.name}: unexpected symbolic/hard link")
            if not member.isfile():
                continue
            rel_path = _normalize_archive_member_path(member.name)
            target = (dest_resolved / rel_path).resolve()
            if not target.is_relative_to(dest_resolved):
                raise ValueError(f"{member.name}: path escapes destination directory")
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = archive.extractfile(member)
            assert extracted is not None
            with target.open("wb") as handle:
                shutil.copyfileobj(extracted, handle)
            os.chmod(target, member.mode & 0o777)
            count += 1
    return count


def _init_clean_git_repo(
    dest: Path,
    *,
    branch: str,
    author_name: str,
    author_email: str,
    message: str,
    tag: str | None,
) -> str:
    git_env = _clean_git_env({
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": author_email,
    })

    def run_git(*args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(dest), *args],
            check=True,
            capture_output=True,
            text=True,
            env=git_env,
        )
        return completed.stdout.strip()

    run_git("init", "-b", branch)
    run_git("config", "user.name", author_name)
    run_git("config", "user.email", author_email)
    run_git("add", "-A")
    run_git("commit", "--no-gpg-sign", "-m", message)
    if tag:
        run_git("tag", "--no-sign", "-a", tag, "-m", f"Release {tag}")
    return run_git("rev-parse", "HEAD")


def prepare_snapshot(
    output: Path,
    *,
    repo: Path = ROOT,
    source: str = "index",
    init_git: bool = False,
    branch: str = "main",
    author_name: str | None = None,
    author_email: str | None = None,
    message: str = "Initial public release",
    tag: str | None = None,
) -> dict[str, Any]:
    repo = repo.resolve()
    output = output.resolve()

    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    if not (repo / ".git").exists():
        raise ValueError(f"Not a git repository: {repo}")
    if source not in {"index", "head"}:
        raise ValueError(f"Unsupported --source value: {source!r} (expected 'index' or 'head')")
    if tag and not init_git:
        raise ValueError("--tag requires --init-git")

    resolved_name = author_name
    resolved_email = author_email
    if init_git:
        resolved_name = (resolved_name or _git_config_value(repo, "user.name") or "").strip()
        if not resolved_name:
            raise ValueError("--author-name is required when git user.name is unset")
        resolved_email = (resolved_email or _git_config_value(repo, "user.email") or "").strip()
        email_error = check_email(resolved_email)
        if email_error is not None:
            raise ValueError(f"Rejected --author-email: {email_error}")

    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".public-snapshot-", dir=output.parent))
    commit_sha: str | None = None
    try:
        if source == "index":
            file_count = _export_from_index(repo, stage)
        else:
            file_count = _export_from_head(repo, stage)

        if init_git:
            assert resolved_name is not None and resolved_email is not None
            commit_sha = _init_clean_git_repo(
                stage,
                branch=branch,
                author_name=resolved_name,
                author_email=resolved_email,
                message=message,
                tag=tag,
            )

        errors = check(stage)
        if init_git:
            errors.extend(check_git_history(stage, rev="HEAD"))
        if errors:
            raise ValueError("Exported snapshot failed verification:\n" + "\n".join(errors))

        stage.rename(output)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    return {
        "output": str(output),
        "source": source,
        "file_count": file_count,
        "git_initialized": init_git,
        "branch": branch if init_git else None,
        "commit": commit_sha,
        "tag": tag if init_git else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export a clean, verified public snapshot from tracked git files.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Destination directory for the exported snapshot (must not already exist).",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=ROOT,
        help="Source git repository root (default: repository root; never modified).",
    )
    parser.add_argument(
        "--source",
        choices=("index", "head"),
        default="index",
        help="Export tracked files from current git index/working tree (index) or HEAD archive (head).",
    )
    parser.add_argument(
        "--init-git",
        action="store_true",
        help="Initialize a new single-commit git repository in the exported snapshot.",
    )
    parser.add_argument(
        "--branch",
        default="main",
        help="Initial branch name when --init-git is set (default: main).",
    )
    parser.add_argument(
        "--author-name",
        default=None,
        help="Author and committer name for --init-git (defaults to git user.name).",
    )
    parser.add_argument(
        "--author-email",
        default=None,
        help="Author and committer email for --init-git (must pass public email hygiene checks).",
    )
    parser.add_argument(
        "--message",
        default="Initial public release",
        help="Root commit message for --init-git.",
    )
    parser.add_argument(
        "--tag",
        default=None,
        help="Optional annotated tag to create on the root commit (e.g. v0.1.0).",
    )
    args = parser.parse_args(argv)
    try:
        result = prepare_snapshot(
            args.output,
            repo=args.repo,
            source=args.source,
            init_git=args.init_git,
            branch=args.branch,
            author_name=args.author_name,
            author_email=args.author_email,
            message=args.message,
            tag=args.tag,
        )
    except (FileExistsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    summary = f"Exported clean public snapshot ({result['file_count']} files) to {result['output']}"
    if result["git_initialized"]:
        summary += f" [branch={result['branch']} commit={result['commit'][:12]}"
        if result["tag"]:
            summary += f" tag={result['tag']}"
        summary += "]"
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
