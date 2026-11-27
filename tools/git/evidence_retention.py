"""Read-only Git snapshot audit; policy findings never rewrite evidence.

Run with ``python3 -m tools.git.evidence_retention --ref HEAD``. Without --ref,
read stage-0 index blobs, not worktree bytes. Output contains paths, hashes and
field names, never artifact payloads. Being admitted into Git is not approval to
send an artifact externally or to AI: those permissions stay on their own fields.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
from typing import Callable

MANIFEST = "artifact-manifest.json"
ARCHIVE_INDEX = "evidence/archive-index.json"
GOVERNANCE = ("kind", "sensitivity", "redaction_status", "retention", "share_policy")
# 与仓库留存守卫（tests/test_evidence_retention_guard.py）一致：没有入库例外类别。
# 被跟踪且声明为 `local_only` 的 Artifact 一律记为 `local_only_conflict`。


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class GitSnapshot:
    def __init__(self, root: Path, ref: str | None = None):
        self.root = root
        self.objects: dict[str, tuple[str, str]] = {}
        if ref is not None:
            commit = self.git("rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
            self.source = {"kind": "commit", "commit": commit}
            entries = self.git("ls-tree", "-r", "-z", commit).split(b"\0")
            for entry in filter(None, entries):
                header, path = entry.split(b"\t", 1)
                mode, kind, oid = header.decode().split()
                if kind == "blob":
                    self.objects[path.decode()] = (mode, oid)
        else:
            entries = self.git("ls-files", "--stage", "-z").split(b"\0")
            for entry in filter(None, entries):
                header, path = entry.split(b"\t", 1)
                mode, oid, stage = header.decode().split()
                if stage != "0":
                    raise ValueError("unmerged index: " + path.decode())
                self.objects[path.decode()] = (mode, oid)
            self.source = {"kind": "index"}
        identity = json.dumps(self.objects, sort_keys=True, separators=(",", ":")).encode()
        self.source["tracked_tree_sha256"] = digest(identity)
        self.cache: dict[str, bytes] = {}

    def git(self, *args: str) -> bytes:
        return subprocess.check_output(["git", *args], cwd=self.root)

    def preload(self, paths: set[str]) -> None:
        oids = sorted({self.objects[path][1] for path in paths
                       if self.objects[path][0] in {"100644", "100755"}}
                      - self.cache.keys())
        if not oids:
            return
        payload = subprocess.check_output(
            ["git", "cat-file", "--batch"], cwd=self.root,
            input=("\n".join(oids) + "\n").encode(),
        )
        offset = 0
        for oid in oids:
            end = payload.index(b"\n", offset)
            actual, kind, size = payload[offset:end].decode().split()
            if actual != oid or kind != "blob":
                raise ValueError("invalid Git batch response")
            offset = end + 1
            self.cache[oid] = payload[offset:offset + int(size)]
            offset += int(size) + 1

    def read(self, path: str) -> bytes:
        mode, oid = self.objects[path]
        if mode not in {"100644", "100755"}:
            raise ValueError("not a regular Git blob: " + path)
        if oid not in self.cache:
            self.cache[oid] = self.git("cat-file", "blob", oid)
        return self.cache[oid]


def declared_path(manifest_path: str, path: object) -> str:
    if not isinstance(path, str) or not path or "\\" in path:
        raise ValueError("invalid_artifact_path")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("noncanonical_artifact_path")
    return str(PurePosixPath(manifest_path).parent / path)


def audit(snapshot: GitSnapshot) -> dict:
    tracked = set(snapshot.objects)
    metadata = {p for p in tracked if p.endswith("/" + MANIFEST)}
    snapshot.preload(metadata | ({ARCHIVE_INDEX} & tracked))
    artifact_json = set()
    for path in metadata:
        if not path.endswith("/" + MANIFEST):
            continue
        try:
            for item in json.loads(snapshot.read(path)).get("artifacts", []):
                target = declared_path(path, item.get("path"))
                if target in tracked and target.endswith(".json"):
                    artifact_json.add(target)
        except (ValueError, AttributeError, TypeError):
            pass  # The diagnostic pass below reports malformed manifests.
    snapshot.preload(artifact_json)
    errors: list[dict] = []
    records: dict[str, dict] = {}
    # The archive index is optional: a snapshot without archived evidence has none.
    index = json.loads(snapshot.read(ARCHIVE_INDEX)) if ARCHIVE_INDEX in tracked else {"records": []}
    for record in index["records"]:
        path = record["archive_path"]
        if path in records:
            errors.append({"path": path, "code": "duplicate_archive_record"})
        records[path] = record
    rows = []
    absent = Counter()
    governed = set()
    for manifest_path in sorted(p for p in tracked if p.endswith("/" + MANIFEST)):
        try:
            payload = snapshot.read(manifest_path)
            manifest = json.loads(payload)
            if not isinstance(manifest, dict) or not isinstance(manifest.get("artifacts"), list):
                raise ValueError("invalid manifest structure")
        except (ValueError, KeyError):
            errors.append({"path": manifest_path, "code": "manifest_unreadable",
                           "git_blob": snapshot.objects[manifest_path][1]})
            continue
        seen = set()
        for item in manifest["artifacts"]:
            try:
                if not isinstance(item, dict):
                    raise ValueError("invalid_artifact")
                path = declared_path(manifest_path, item.get("path"))
                if path in seen:
                    raise ValueError("duplicate_artifact")
                seen.add(path)
            except ValueError as exc:
                errors.append({"path": manifest_path, "code": str(exc)})
                continue
            governance = {key: item.get(key) for key in GOVERNANCE}
            if path not in tracked:
                absent["/".join(str(governance[k]) for k in GOVERNANCE)] += 1
                continue
            governed.add(path)
            try:
                data = snapshot.read(path)
            except ValueError:
                errors.append({"path": path, "code": "artifact_not_regular"})
                continue
            row = {"path": path, "manifest": manifest_path,
                   "manifest_sha256": digest(payload), "sha256": digest(data),
                   "bytes": len(data), **governance,
                   "manifest_identity_matches": item.get("sha256") == digest(data) and item.get("bytes") == len(data)}
            if path.startswith("runs/"):
                rule = "runtime_tree_forbidden"
            elif item.get("redaction_status") == "raw" and (
                item.get("retention") != "local_only" or item.get("share_policy") != "forbidden"
            ):
                rule = "raw_policy_conflict"
            elif item.get("retention") == "local_only":
                rule = "local_only_conflict"
            elif item.get("retention") == "shareable":
                rule = "shareable_" + str(item.get("redaction_status"))
            else:
                rule = "invalid_retention"
            row["rule"] = rule
            if path.endswith("/run-spec.json"):
                try:
                    spec = json.loads(data)
                    if not isinstance(spec, dict):
                        raise ValueError("invalid run spec structure")
                except (ValueError, UnicodeDecodeError):
                    spec = {}
                    errors.append({"path": path, "code": "run_spec_unreadable"})
                # Only fixed schema field names are emitted, never keys from app-specific payloads.
                row["policy_review_fields"] = sorted(
                    key for key in ("fixture", "operation", "verification", "store_context") if key in spec
                )
                row["archive_source_path"] = records.get(path, {}).get("source_path")
            if rule in {"runtime_tree_forbidden", "raw_policy_conflict", "local_only_conflict", "invalid_retention"}:
                errors.append({"path": path, "manifest": manifest_path, "code": rule})
            rows.append(row)
    runtime = sorted(p for p in tracked if p.startswith("runs/"))
    for path in runtime:
        if path not in governed:
            errors.append({"path": path, "code": "runtime_tree_forbidden"})
    rules = {p: digest(snapshot.read(p)) for p in sorted(tracked)
             if PurePosixPath(p).name in {".gitignore", ".gitattributes"}}
    return {
        "schema_version": "evidence-retention-audit/v3", "source": snapshot.source,
        "scope": "tracked Artifact Manifest declarations; not a payload sensitivity scan",
        "archive_index_sha256": digest(snapshot.read(ARCHIVE_INDEX)) if ARCHIVE_INDEX in tracked else None,
        "git_rule_sha256": rules,
        "summary": {"tracked_files": len(tracked), "tracked_declarations": len(rows),
                    "rules": dict(sorted(Counter(r["rule"] for r in rows).items())),
                    "errors": len(errors), "runtime_tracked": len(runtime),
                    "tracked_run_specs_requiring_content_policy": sum(
                        bool(r.get("policy_review_fields")) for r in rows)},
        "untracked_declaration_counts": dict(sorted(absent.items())),
        "errors": errors, "records": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--ref", help="commit to inspect; default: index blobs")
    parser.add_argument("--output", type=Path, help="write diagnostic JSON, never source artifacts")
    parser.add_argument("--check", action="store_true", help="exit 1 for enforced rule violations; policy review remains open")
    args = parser.parse_args()
    result = audit(GitSnapshot(args.root, args.ref))
    output = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
        print(json.dumps(result["summary"], sort_keys=True))
    else:
        print(output, end="")
    return int(args.check and bool(result["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
