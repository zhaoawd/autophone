"""Audit release runtime dependency licenses, archive checksums, and git history.

Designed to run inside the isolated wheel verification virtualenv (`.venv-wheel`)
using only the Python standard library, and emit `RELEASE-COMPLIANCE.json` for
archival alongside GitHub Release artifacts.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata as importlib_metadata
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable

try:
    from scripts.check_public_content import check_git_history
except ModuleNotFoundError:
    _scripts_dir = str(Path(__file__).resolve().parent)
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
    from check_public_content import check_git_history


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "1.0"

FIRST_PARTY_PACKAGES: frozenset[str] = frozenset({
    "autophone",
    "autophone-kernel",
    "autophone-rig",
    "autophone-domain",
    "autophone-console",
})

ALLOWED_PERMISSIVE_LICENSES: frozenset[str] = frozenset({
    "Apache-2.0",
    "MIT",
    "MIT-0",
    "MIT-CMU",
    "BSD",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "PSF-2.0",
    "ISC",
})

# Reviewed permissive SPDX `<license> WITH <exception>` combinations.
# Any unreviewed license+exception pair fails closed and requires manual review.
ALLOWED_PERMISSIVE_LICENSE_EXCEPTIONS: frozenset[str] = frozenset({
    "Apache-2.0 WITH LLVM-exception",
})

# Weak-copyleft runtime dependencies explicitly disclosed in docs/public-core.md.
# Scoped strictly by normalized package name so new non-permissive dependencies fail closed.
DISCLOSED_PACKAGE_LICENSES: dict[str, frozenset[str]] = {
    "certifi": frozenset({"MPL-2.0"}),
    "psycopg": frozenset({"LGPL-3.0-only"}),
    "psycopg-binary": frozenset({"LGPL-3.0-only"}),
}

_LEGACY_PLACEHOLDER_LICENSES: frozenset[str] = frozenset({
    "",
    "unknown",
    "none",
    "dual license",
    "dual-license",
    "see license",
    "see license.txt",
    "see license file",
})

_LEGACY_LICENSE_MAP: dict[str, str] = {
    "apache-2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache 2": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "mit": "MIT",
    "mit license": "MIT",
    "the mit license (mit)": "MIT",
    "mit-0": "MIT-0",
    "mit-cmu": "MIT-CMU",
    "bsd": "BSD",
    "bsd license": "BSD",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd 2-clause": "BSD-2-Clause",
    "bsd 2-clause license": "BSD-2-Clause",
    "bsd-2-clause license": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "bsd 3-clause license": "BSD-3-Clause",
    "bsd-3-clause license": "BSD-3-Clause",
    "new bsd": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "3-clause bsd": "BSD-3-Clause",
    "3-clause bsd license": "BSD-3-Clause",
    "modified bsd": "BSD-3-Clause",
    "modified bsd license": "BSD-3-Clause",
    "isc": "ISC",
    "isc license": "ISC",
    "isc license (iscl)": "ISC",
    "psf": "PSF-2.0",
    "psf-2.0": "PSF-2.0",
    "python software foundation license": "PSF-2.0",
    "python software foundation license version 2": "PSF-2.0",
    "mpl-2.0": "MPL-2.0",
    "mpl 2.0": "MPL-2.0",
    "mozilla public license 2.0": "MPL-2.0",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "lgpl-3.0-only": "LGPL-3.0-only",
    "lgpl-3.0": "LGPL-3.0-only",
    "lgplv3": "LGPL-3.0-only",
    "gnu lesser general public license v3 (lgplv3)": "LGPL-3.0-only",
    "lgpl-3.0-or-later": "LGPL-3.0-or-later",
    "lgplv3+": "LGPL-3.0-or-later",
    "gpl": "GPL",
    "gplv2": "GPL-2.0-only",
    "gplv3": "GPL-3.0-only",
    "gpl-2.0": "GPL-2.0-only",
    "gpl-2.0-only": "GPL-2.0-only",
    "gpl-2.0-or-later": "GPL-2.0-or-later",
    "gpl-3.0": "GPL-3.0-only",
    "gpl-3.0-only": "GPL-3.0-only",
    "gpl-3.0-or-later": "GPL-3.0-or-later",
    "agpl": "AGPL",
    "agplv3": "AGPL-3.0-only",
    "agpl-3.0": "AGPL-3.0-only",
    "agpl-3.0-only": "AGPL-3.0-only",
    "agpl-3.0-or-later": "AGPL-3.0-or-later",
}

_CLASSIFIER_LICENSE_MAP: dict[str, str] = {
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: MIT No Attribution License (MIT-0)": "MIT-0",
    "License :: OSI Approved :: BSD License": "BSD",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0-only",
    "License :: OSI Approved :: GNU Lesser General Public License v3 or later (LGPLv3+)": "LGPL-3.0-or-later",
}

_SPDX_TOKEN_RE = re.compile(r"\(|\)|[A-Za-z0-9.+_-]+|\S")
_CHECKSUM_LINE_RE = re.compile(r"^([0-9a-fA-F]{64}) [ *](.+)$")


def normalize_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def _map_classifier(classifier: str) -> str:
    exact = _CLASSIFIER_LICENSE_MAP.get(classifier)
    if exact is not None:
        return exact
    lower = classifier.lower()
    if "gnu affero general public license" in lower or "agpl" in lower:
        return "AGPL"
    if "gnu general public license" in lower:
        return "GPL"
    if "gnu lesser general public license" in lower or "lgpl" in lower:
        return "LGPL"
    if "proprietary" in lower:
        return "PROPRIETARY"
    return f"UNKNOWN({classifier})"


def _normalize_spdx_atom(token: str) -> str:
    return _LEGACY_LICENSE_MAP.get(token.lower(), token)


def _evaluate_spdx_expression_detailed(
    expression: str,
    allowed_ids: frozenset[str],
) -> tuple[bool, list[str]]:
    """Parse and evaluate an SPDX license expression against allowed license IDs.

    Precedence follows the SPDX specification: WITH > AND > OR.
    `WITH` expressions are matched as exact `<license> WITH <exception>` strings
    against `allowed_ids` so unreviewed license+exception combinations fail closed.
    Returns `(allowed, unreviewed_exceptions)`.
    """
    tokens = _SPDX_TOKEN_RE.findall(expression.strip())
    if not tokens:
        return False, []

    pos = 0
    unreviewed_exceptions: list[str] = []

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def consume() -> str:
        nonlocal pos
        tok = tokens[pos]
        pos += 1
        return tok

    def parse_or() -> bool:
        result = parse_and()
        while (tok := peek()) is not None and tok.upper() == "OR":
            consume()
            rhs = parse_and()
            result = result or rhs
        return result

    def parse_and() -> bool:
        result = parse_primary()
        while (tok := peek()) is not None and tok.upper() == "AND":
            consume()
            rhs = parse_primary()
            result = result and rhs
        return result

    def parse_primary() -> bool:
        tok = peek()
        if tok is None:
            raise ValueError("unexpected end of license expression")
        if tok == "(":
            consume()
            val = parse_or()
            if peek() != ")":
                raise ValueError("unclosed '(' in license expression")
            consume()
            return val
        if tok in {")"} or tok.upper() in {"AND", "OR", "WITH"}:
            raise ValueError(f"unexpected token {tok!r} in license expression")
        lic_id = _normalize_spdx_atom(consume())
        if (nxt := peek()) is not None and nxt.upper() == "WITH":
            consume()
            exc = peek()
            if exc is None or exc in {"(", ")"} or exc.upper() in {"AND", "OR", "WITH"}:
                raise ValueError("missing exception after WITH")
            exc_id = consume()
            compound = f"{lic_id} WITH {exc_id}"
            if compound in allowed_ids:
                return True
            if compound not in unreviewed_exceptions:
                unreviewed_exceptions.append(compound)
            return False
        return lic_id in allowed_ids

    try:
        outcome = parse_or()
    except ValueError:
        return False, []
    if pos != len(tokens):
        return False, []
    if outcome:
        return True, []
    return False, unreviewed_exceptions


def evaluate_spdx_expression(expression: str, allowed_ids: frozenset[str]) -> bool:
    """Parse and evaluate an SPDX license expression against allowed license IDs.

    Precedence follows the SPDX specification: WITH > AND > OR.
    `WITH` expressions require an exact `<license> WITH <exception>` match in `allowed_ids`.
    Returns False on any syntax error, disallowed license atom, or unreviewed exception.
    """
    outcome, _ = _evaluate_spdx_expression_detailed(expression, allowed_ids)
    return outcome


def extract_license_info(metadata: Any) -> tuple[str, str]:
    """Return `(license_expression_or_label, source)` from distribution metadata."""
    lic_expr = (metadata.get("License-Expression") or "").strip()
    if lic_expr:
        return lic_expr, "license-expression"

    raw_license = (metadata.get("License") or "").strip()
    classifiers = [
        c.strip()
        for c in (metadata.get_all("Classifier") or [])
        if isinstance(c, str) and c.strip().startswith("License ::")
    ]
    mapped_classifiers = [_map_classifier(c) for c in classifiers]

    if raw_license and "\n" not in raw_license and len(raw_license) <= 80:
        lower_raw = raw_license.lower()
        if lower_raw not in _LEGACY_PLACEHOLDER_LICENSES:
            mapped = _LEGACY_LICENSE_MAP.get(lower_raw)
            resolved = mapped if mapped is not None else raw_license
            # Fail closed if a strong-copyleft classifier contradicts a legacy header.
            if any(c in {"GPL", "AGPL"} for c in mapped_classifiers):
                wrapped = f"({resolved})" if " " in resolved else resolved
                return f"{wrapped} AND " + " AND ".join(
                    sorted(set(c for c in mapped_classifiers if c in {"GPL", "AGPL"}))
                ), "license-header+classifier-conflict"
            return resolved, "license-header"

    if mapped_classifiers:
        unique_mapped = sorted(set(mapped_classifiers))
        if raw_license.lower() in {"dual license", "dual-license"} and len(unique_mapped) > 1:
            return " OR ".join(unique_mapped), "classifiers-dual"
        if len(unique_mapped) == 1:
            return unique_mapped[0], "classifiers"
        return " AND ".join(unique_mapped), "classifiers"

    return "UNKNOWN", "missing"


def evaluate_package_license_details(name: str, license_expr: str) -> tuple[bool, str, list[str]]:
    """Return `(allowed, policy_category, unreviewed_exceptions)` for a normalized package name."""
    pkg = normalize_package_name(name)
    if pkg in FIRST_PARTY_PACKAGES:
        allowed, unreviewed = _evaluate_spdx_expression_detailed(license_expr, frozenset({"Apache-2.0"}))
        if allowed:
            return True, "first-party", []
        return False, "review-required" if unreviewed else "rejected", unreviewed

    permissive_allowed = ALLOWED_PERMISSIVE_LICENSES | ALLOWED_PERMISSIVE_LICENSE_EXCEPTIONS
    allowed, unreviewed = _evaluate_spdx_expression_detailed(license_expr, permissive_allowed)
    if allowed:
        return True, "permissive", []

    disclosed = DISCLOSED_PACKAGE_LICENSES.get(pkg)
    if disclosed:
        allowed, unreviewed = _evaluate_spdx_expression_detailed(
            license_expr,
            permissive_allowed | disclosed,
        )
        if allowed:
            return True, "disclosed-exception", []

    return False, "review-required" if unreviewed else "rejected", unreviewed


def evaluate_package_license(name: str, license_expr: str) -> tuple[bool, str]:
    """Return `(allowed, policy_category)` for a normalized package name and license."""
    allowed, policy, _ = evaluate_package_license_details(name, license_expr)
    return allowed, policy


def audit_distributions(
    distributions: Iterable[Any],
    *,
    require_first_party: bool = True,
) -> dict[str, Any]:
    """Audit all distributions and return structured inventory + violations."""
    seen: dict[str, dict[str, Any]] = {}
    for dist in distributions:
        meta = dist.metadata
        raw_name = meta.get("Name") or getattr(dist, "name", "")
        if not raw_name:
            continue
        norm_name = normalize_package_name(raw_name)
        version = str(dist.version)
        license_expr, source = extract_license_info(meta)
        allowed, policy, unreviewed_exceptions = evaluate_package_license_details(norm_name, license_expr)
        entry: dict[str, Any] = {
            "name": norm_name,
            "version": version,
            "license": license_expr,
            "source": source,
            "policy": policy,
            "allowed": allowed,
        }
        if unreviewed_exceptions:
            entry["unreviewed_exceptions"] = unreviewed_exceptions
        # Prefer first occurrence unless duplicate entries conflict.
        if norm_name not in seen:
            seen[norm_name] = entry

    packages = [seen[name] for name in sorted(seen)]
    violations: list[str] = []
    for entry in packages:
        if not entry["allowed"]:
            unreviewed = entry.get("unreviewed_exceptions") or []
            if unreviewed:
                exprs = ", ".join(repr(item) for item in unreviewed)
                if len(unreviewed) == 1 and unreviewed[0] == entry["license"]:
                    violations.append(
                        f"{entry['name']}=={entry['version']}: unreviewed license exception "
                        f"{exprs} requires manual review (source={entry['source']})"
                    )
                else:
                    violations.append(
                        f"{entry['name']}=={entry['version']}: unreviewed license exception(s) "
                        f"{exprs} in {entry['license']!r} require manual review (source={entry['source']})"
                    )
            else:
                violations.append(
                    f"{entry['name']}=={entry['version']}: disallowed or unknown license "
                    f"{entry['license']!r} (source={entry['source']})"
                )

    first_party = [p for p in packages if p["name"] in FIRST_PARTY_PACKAGES]
    if require_first_party:
        present_fp = {p["name"] for p in first_party}
        missing_fp = sorted(FIRST_PARTY_PACKAGES - present_fp)
        if missing_fp:
            violations.append(f"missing first-party packages in environment: {', '.join(missing_fp)}")
        fp_versions = {p["version"] for p in first_party}
        if len(fp_versions) > 1:
            violations.append(f"inconsistent first-party package versions: {sorted(fp_versions)}")

    disclosed_used = [
        {"name": p["name"], "version": p["version"], "license": p["license"]}
        for p in packages
        if p["policy"] == "disclosed-exception"
    ]
    return {
        "total": len(packages),
        "first_party": len(first_party),
        "third_party": len(packages) - len(first_party),
        "disclosed_exceptions": disclosed_used,
        "violations": violations,
        "packages": packages,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_checksums_file(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        match = _CHECKSUM_LINE_RE.match(line)
        if not match:
            raise ValueError(f"{path.name}:{number}: malformed sha256sum line {raw_line!r}")
        digest, filename = match.group(1).lower(), match.group(2).strip()
        if filename in entries:
            raise ValueError(f"{path.name}:{number}: duplicate entry for {filename!r}")
        entries[filename] = digest
    return entries


def collect_archives(dist_dir: Path, checksums_path: Path | None = None) -> list[dict[str, Any]]:
    dist_dir = dist_dir.resolve()
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 5 or len(sdists) != 5:
        raise ValueError(
            f"expected 5 wheels and 5 sdists in {dist_dir}, got {len(wheels)} wheels and {len(sdists)} sdists"
        )
    expected_prefixes = {pkg.replace("-", "_") + "-" for pkg in FIRST_PARTY_PACKAGES}
    for kind_name, files in (("wheel", wheels), ("sdist", sdists)):
        found_prefixes = {p.name.split("-", 1)[0] + "-" for p in files}
        if found_prefixes != expected_prefixes:
            raise ValueError(
                f"{kind_name} package set mismatch: expected {sorted(expected_prefixes)}, "
                f"got {sorted(found_prefixes)}"
            )

    archives: list[dict[str, Any]] = []
    computed: dict[str, str] = {}
    for path in sorted((*wheels, *sdists), key=lambda p: p.name):
        digest = _sha256_file(path)
        computed[path.name] = digest
        archives.append({
            "filename": path.name,
            "kind": "wheel" if path.name.endswith(".whl") else "sdist",
            "size_bytes": path.stat().st_size,
            "sha256": digest,
        })

    if checksums_path is not None:
        recorded = parse_checksums_file(checksums_path)
        if set(recorded) != set(computed):
            extra = sorted(set(recorded) - set(computed))
            missing = sorted(set(computed) - set(recorded))
            raise ValueError(
                f"checksum manifest mismatch in {checksums_path.name}: missing={missing}, extra={extra}"
            )
        mismatches = [
            f"{name}: expected {recorded[name]}, got {computed[name]}"
            for name in sorted(computed)
            if recorded[name] != computed[name]
        ]
        if mismatches:
            raise ValueError("archive SHA-256 mismatch:\n" + "\n".join(mismatches))

    return archives


def _resolve_commit(repo: Path, explicit_commit: str | None) -> str:
    if explicit_commit and explicit_commit.strip():
        return explicit_commit.strip()
    if (repo / ".git").exists():
        completed = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()
    return "unknown"


def build_compliance_report(
    *,
    dist_dir: Path,
    checksums_path: Path | None = None,
    repo: Path = ROOT,
    check_history: bool = False,
    rev: str = "HEAD",
    commit: str | None = None,
    tag: str | None = None,
    distributions: Iterable[Any] | None = None,
    require_first_party: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    """Run all compliance checks and return `(report_dict, errors)`."""
    errors: list[str] = []
    repo_resolved = repo.resolve()
    resolved_commit = _resolve_commit(repo_resolved, commit)
    resolved_tag = tag.strip() if tag and tag.strip() else None

    if check_history:
        history_errors = check_git_history(repo_resolved, rev=rev)
        if history_errors:
            errors.extend(history_errors)
        git_history_section: dict[str, Any] = {
            "checked": True,
            "status": "passed" if not history_errors else "failed",
            "rev": rev,
            "error_count": len(history_errors),
        }
    else:
        git_history_section = {
            "checked": False,
            "status": "skipped",
            "rev": None,
            "error_count": None,
        }

    archives: list[dict[str, Any]] = []
    try:
        archives = collect_archives(dist_dir, checksums_path=checksums_path)
    except (OSError, ValueError) as exc:
        errors.append(str(exc))

    dists = list(distributions) if distributions is not None else list(importlib_metadata.distributions())
    dep_audit = audit_distributions(dists, require_first_party=require_first_party)
    errors.extend(dep_audit["violations"])

    release_version = next(
        (p["version"] for p in dep_audit["packages"] if p["name"] == "autophone"),
        None,
    )

    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "release": {
            "version": release_version,
            "tag": resolved_tag,
            "commit": resolved_commit,
        },
        "git_history_check": git_history_section,
        "archives": archives,
        "license_policy": {
            "allowed_permissive": sorted(ALLOWED_PERMISSIVE_LICENSES),
            "allowed_permissive_exceptions": sorted(ALLOWED_PERMISSIVE_LICENSE_EXCEPTIONS),
            "disclosed_exceptions": {
                pkg: sorted(lics) for pkg, lics in sorted(DISCLOSED_PACKAGE_LICENSES.items())
            },
        },
        "dependencies": dep_audit,
        "summary": {
            "status": "passed" if not errors else "failed",
            "archive_count": len(archives),
            "dependency_count": dep_audit["total"],
            "violation_count": len(errors),
        },
    }
    return report, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit runtime dependency licenses, archive checksums, and git history for a release.",
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        required=True,
        help="Directory containing the 5 built wheels and 5 built sdists.",
    )
    parser.add_argument(
        "--checksums",
        type=Path,
        default=None,
        help="Optional SHA256SUMS.txt file to cross-check against built archives.",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=ROOT,
        help="Git repository root for commit metadata and --check-git-history.",
    )
    parser.add_argument(
        "--check-git-history",
        action="store_true",
        help="Run full git history hygiene check and require 0 findings.",
    )
    parser.add_argument(
        "--rev",
        default="HEAD",
        help="Git revision range for --check-git-history (default: HEAD).",
    )
    parser.add_argument(
        "--commit",
        default=None,
        help="Release commit SHA (defaults to `git rev-parse HEAD` in --repo).",
    )
    parser.add_argument(
        "--tag",
        default=None,
        help="Optional release tag (e.g. v0.1.0).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path to write RELEASE-COMPLIANCE.json (written only when all checks pass).",
    )
    args = parser.parse_args(argv)

    report, errors = build_compliance_report(
        dist_dir=args.dist_dir,
        checksums_path=args.checksums,
        repo=args.repo,
        check_history=args.check_git_history,
        rev=args.rev,
        commit=args.commit,
        tag=args.tag,
    )
    if errors:
        print("Release compliance audit FAILED:", file=sys.stderr)
        for item in errors:
            print(f"  - {item}", file=sys.stderr)
        return 1

    payload = json.dumps(report, indent=2, sort_keys=False) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(
            f"Release compliance audit passed ({report['summary']['archive_count']} archives, "
            f"{report['summary']['dependency_count']} packages) -> {args.output}"
        )
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
