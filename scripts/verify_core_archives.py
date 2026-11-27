"""Check schema inventory, legal files, and content hygiene of public core archives."""
from __future__ import annotations

import ast
from email.parser import Parser
from pathlib import Path
import re
import sys
import tarfile
import tomllib
import zipfile

try:
    from scripts.check_public_content import FORBIDDEN_PATTERNS
except ModuleNotFoundError:
    _scripts_dir = str(Path(__file__).resolve().parent)
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
    from check_public_content import FORBIDDEN_PATTERNS

try:
    from pkgs.ownership import owner as _ownership_owner
except ModuleNotFoundError:
    _repo_root = str(Path(__file__).resolve().parents[1])
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    try:
        from pkgs.ownership import owner as _ownership_owner
    except ModuleNotFoundError:
        _ownership_owner = None


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = "autophone/_data/schemas/"
REQUIRED_LEGAL_FILES: tuple[str, ...] = ("LICENSE", "NOTICE")
FORBIDDEN_PATH_PREFIXES: tuple[str, ...] = (
    "autophone/workflow/",
    "tools/guest/",
    "tools/phase5/runs/",
    "review/public-release/",
)
FORBIDDEN_EXACT_PATHS: frozenset[str] = frozenset({
    "autophone/adapters/http/stores.py",
    "autophone/device/app_uninstall_helper.py",
})
SKIP_CONTENT_SCAN_MEMBERS: frozenset[str] = frozenset({
    "scripts/check_public_content.py",
})
OPTIONAL_POSTGRES_PACKAGES: frozenset[str] = frozenset({
    "psycopg",
    "psycopg-binary",
    "psycopg-c",
    "psycopg-pool",
})
POSTGRES_EXTRA_OWNERS: frozenset[str] = frozenset({
    "autophone",
    "autophone-rig",
})
_EXTRA_MARKER_RE = re.compile(r"\bextra\s*==")
_POSTGRES_EXTRA_MARKER_RE = re.compile(r"""\bextra\s*==\s*['"]postgres['"]""")


def _normalize_dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def _requirement_name(requirement: str) -> str:
    head = requirement.split(";", 1)[0].strip()
    raw = re.split(r"[\s\[(<>=!~@]", head, maxsplit=1)[0]
    return _normalize_dist_name(raw)


def _verify_archive_dependencies(
    archive_name: str,
    suffix: str,
    members: list[tuple[str, bytes]],
) -> None:
    if suffix == ".whl":
        metadata_payload: bytes | None = None
        for name, payload in members:
            parts = name.split("/")
            if len(parts) == 2 and parts[0].endswith(".dist-info") and parts[1] == "METADATA":
                metadata_payload = payload
                break
        if metadata_payload is None:
            raise ValueError(f"{archive_name}: missing .dist-info/METADATA")
        meta = Parser().parsestr(metadata_payload.decode("utf-8"))
        pkg_name = _normalize_dist_name(meta.get("Name") or "")
        requires = meta.get_all("Requires-Dist") or []
        extras = {_normalize_dist_name(item) for item in (meta.get_all("Provides-Extra") or [])}
        has_postgres_dep = False
        for req in requires:
            _, _, marker = req.partition(";")
            dep_name = _requirement_name(req)
            is_extra = bool(_EXTRA_MARKER_RE.search(marker))
            if not is_extra and dep_name in OPTIONAL_POSTGRES_PACKAGES:
                raise ValueError(
                    f"{archive_name}: base dependency {req!r} must be optional (move to [postgres] extra)"
                )
            if dep_name in OPTIONAL_POSTGRES_PACKAGES and _POSTGRES_EXTRA_MARKER_RE.search(marker):
                has_postgres_dep = True
        if pkg_name in POSTGRES_EXTRA_OWNERS:
            if "postgres" not in extras or not has_postgres_dep:
                raise ValueError(
                    f"{archive_name}: missing required 'postgres' optional extra for {pkg_name}"
                )
        return

    pyproject_payload: bytes | None = None
    for name, payload in members:
        if name.lstrip("./") == "pyproject.toml":
            pyproject_payload = payload
            break
    if pyproject_payload is None:
        raise ValueError(f"{archive_name}: missing pyproject.toml")
    project = tomllib.loads(pyproject_payload.decode("utf-8")).get("project") or {}
    pkg_name = _normalize_dist_name(str(project.get("name") or ""))
    for req in project.get("dependencies") or []:
        if not isinstance(req, str):
            continue
        if _requirement_name(req) in OPTIONAL_POSTGRES_PACKAGES:
            raise ValueError(
                f"{archive_name}: base dependency {req!r} must be optional (move to [postgres] extra)"
            )
    if pkg_name in POSTGRES_EXTRA_OWNERS:
        optional = project.get("optional-dependencies") or {}
        pg_reqs = optional.get("postgres") if isinstance(optional, dict) else None
        if not isinstance(pg_reqs, list) or not any(
            isinstance(item, str) and _requirement_name(item) in OPTIONAL_POSTGRES_PACKAGES
            for item in pg_reqs
        ):
            raise ValueError(
                f"{archive_name}: missing required 'postgres' optional extra for {pkg_name}"
            )


def _tar_member_relpath(name: str) -> str:
    cleaned = name.lstrip("./")
    head, sep, tail = cleaned.partition("/")
    return tail if sep else head


def _iter_archive_members(path: Path, suffix: str) -> list[tuple[str, bytes]]:
    members: list[tuple[str, bytes]] = []
    if suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                members.append((info.filename, archive.read(info)))
    else:
        with tarfile.open(path) as archive:
            for member in archive:
                if member.issym() or member.islnk():
                    rel = _tar_member_relpath(member.name)
                    raise ValueError(f"{path.name}: {rel}: unexpected symbolic/hard link")
                if not member.isfile():
                    continue
                rel = _tar_member_relpath(member.name)
                extracted = archive.extractfile(member)
                members.append((rel, extracted.read() if extracted is not None else b""))
    return members


def _has_legal_file(file_set: set[str], legal_name: str, suffix: str) -> bool:
    if legal_name in file_set:
        return True
    if suffix == ".whl":
        for name in file_set:
            parts = name.split("/")
            if len(parts) == 3 and parts[0].endswith(".dist-info") and parts[1] == "licenses" and parts[2] == legal_name:
                return True
    return False


def _forbidden_member_reason(member: str) -> str | None:
    normalized = member.lstrip("./")
    for exact in FORBIDDEN_EXACT_PATHS:
        if normalized == exact or normalized.endswith(f"/{exact}"):
            return f"forbidden path {exact}"
    for prefix in FORBIDDEN_PATH_PREFIXES:
        stem = prefix.rstrip("/")
        if (
            normalized == stem
            or normalized.startswith(prefix)
            or f"/{prefix}" in normalized
            or normalized.endswith(f"/{stem}")
        ):
            return f"forbidden path prefix {prefix}"
    parts = normalized.split("/")
    if "config" in parts[:-1] and parts[-1].endswith(".local.yaml"):
        return "forbidden local config path (config/*.local.yaml)"
    return None


def _scan_member_content(archive_name: str, member: str, payload: bytes) -> None:
    normalized = member.lstrip("./")
    if normalized in SKIP_CONTENT_SCAN_MEMBERS:
        return
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return
    for number, line in enumerate(text.splitlines(), 1):
        for label, pattern in FORBIDDEN_PATTERNS:
            if pattern.search(line):
                raise ValueError(f"{archive_name}: {member}:{number}: forbidden {label}")


def _module_name_for_path(member: str) -> str | None:
    normalized = member.lstrip("./")
    if not normalized.endswith(".py"):
        return None
    parts = normalized[:-3].split("/")
    if not parts or parts[0] not in {"autophone", "tools"}:
        return None
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


def _is_non_kernel_target(
    module_name: str,
    non_kernel_modules: set[str],
    root: Path,
) -> str | None:
    cur = module_name
    while cur:
        if cur in non_kernel_modules:
            return cur
        if _ownership_owner is not None and (cur.startswith("autophone.") or cur.startswith("tools.")):
            rel_base = cur.replace(".", "/")
            for candidate in (f"{rel_base}.py", f"{rel_base}/__init__.py"):
                if (root / candidate).is_file():
                    comp = _ownership_owner(candidate)
                    if comp is not None and comp != "kernel":
                        return f"{cur} ({comp})"
        if "." not in cur:
            break
        cur = cur.rsplit(".", 1)[0]
    return None


def _verify_kernel_import_boundary(
    archive_name: str,
    members: list[tuple[str, bytes]],
    non_kernel_modules: set[str],
    root: Path,
) -> None:
    for member, payload in members:
        normalized = member.lstrip("./")
        if not normalized.endswith(".py"):
            continue
        if not normalized.startswith(("autophone/", "tools/")):
            continue
        try:
            source = payload.decode("utf-8")
            tree = ast.parse(source, filename=normalized)
        except (UnicodeDecodeError, SyntaxError) as exc:
            raise ValueError(f"{archive_name}: {normalized}: failed to parse python source: {exc}") from exc

        pkg_parts = normalized[:-3].split("/")[:-1]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    hit = _is_non_kernel_target(alias.name, non_kernel_modules, root)
                    if hit is not None:
                        raise ValueError(
                            f"{archive_name}: {normalized}:{node.lineno}: "
                            f"kernel layering violation importing {alias.name!r} -> {hit}"
                        )
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    if node.level > len(pkg_parts) + 1:
                        continue
                    prefix_parts = pkg_parts[: len(pkg_parts) - node.level + 1]
                    if base:
                        prefix_parts.append(base)
                    full_base = ".".join(prefix_parts)
                else:
                    full_base = base
                hit = _is_non_kernel_target(full_base, non_kernel_modules, root)
                if hit is not None:
                    raise ValueError(
                        f"{archive_name}: {normalized}:{node.lineno}: "
                        f"kernel layering violation importing from {full_base!r} -> {hit}"
                    )
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    sub = f"{full_base}.{alias.name}" if full_base else alias.name
                    sub_hit = _is_non_kernel_target(sub, non_kernel_modules, root)
                    if sub_hit is not None:
                        raise ValueError(
                            f"{archive_name}: {normalized}:{node.lineno}: "
                            f"kernel layering violation importing {sub!r} -> {sub_hit}"
                        )


def verify(directory: Path, root: Path = ROOT) -> None:
    expected = {p.relative_to(root).as_posix() for p in (root / SCHEMAS).rglob("*") if p.is_file()}
    for suffix in (".whl", ".tar.gz"):
        packages = sorted(directory.glob(f"*{suffix}"))
        if len(packages) != 5:
            raise ValueError(f"expected five {suffix} packages, got {len(packages)}")
        archive_entries: list[tuple[Path, list[tuple[str, bytes]]]] = [
            (path, _iter_archive_members(path, suffix)) for path in packages
        ]
        non_kernel_modules: set[str] = set()
        for path, members in archive_entries:
            if path.name.startswith("autophone_kernel-"):
                continue
            for name, _ in members:
                mod = _module_name_for_path(name)
                if mod is not None:
                    non_kernel_modules.add(mod)

        kernel_count = 0
        for path, members in archive_entries:
            files = [name for name, _ in members]
            file_set = set(files)

            kernel = path.name.startswith("autophone_kernel-")
            kernel_count += kernel
            wanted = expected if kernel else set()
            actual = {name for name in files if name.startswith(SCHEMAS)}
            if actual != wanted:
                raise ValueError(f"{path.name}: schema mismatch: {sorted(actual ^ wanted)}")

            for legal_name in REQUIRED_LEGAL_FILES:
                if not _has_legal_file(file_set, legal_name, suffix):
                    raise ValueError(f"{path.name}: missing required legal file {legal_name}")

            for name, payload in members:
                reason = _forbidden_member_reason(name)
                if reason is not None:
                    raise ValueError(f"{path.name}: {name}: {reason}")
                _scan_member_content(path.name, name, payload)

            _verify_archive_dependencies(path.name, suffix, members)
            if kernel:
                _verify_kernel_import_boundary(path.name, members, non_kernel_modules, root)
        if kernel_count != 1:
            raise ValueError(f"expected one kernel {suffix} package")


if __name__ == "__main__":
    verify(Path(sys.argv[1]))
