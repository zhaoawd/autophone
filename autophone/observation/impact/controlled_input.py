"""Freeze private API inputs and validate their source, mutation and Run binding.

Only references and compiled-rule digests belong in public contracts. The
snapshot embeds the exact trusted source bytes so later loads are independent
of mutable source files. Dispatch currently shares the host filesystem.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from autophone.observation.impact import comparison, endpoints, lifecycle_evidence
from autophone.scenario.rules import CompiledScenario, compile_rules

IMPACT_REL = "impact/private/api-scenario.json"
CHILD_REL = "planes/api/private/scenario.json"
_CAPTURE = "planes/api/capture.jsonl"
_EVENTS = "planes/api/private/events.jsonl"
_SOURCE_KEYS = {
    "run_id",
    "capture_sha256",
    "capture_events_sha256",
    "lines",
    "body_sha256",
    "body",
}
_INPUT_KEYS = {"schema_version", "app", "scenario", "source", "mutation", "oracle"}
_FROZEN_KEYS = _INPUT_KEYS | {
    "run_id",
    "device",
    "rule_snapshot_hash",
    "_frozen_source",
}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_PATH = re.compile(r"\$(?:\.[A-Za-z_][A-Za-z0-9_]*|\[(?:0|[1-9][0-9]*)\])+\Z")
_BLOCKED = re.compile(r"url|uri|link|price|order|store|login|auth|token", re.IGNORECASE)
# Whole camelCase/snake_case words that contain a blocked substring by accident.
# Every other word keeps the substring check, so ``jumpurl`` or ``storeid`` stay blocked.
_BENIGN_WORDS = frozenset({
    "author", "authors", "border", "borders", "curious", "curly", "during",
    "maturity", "obscurity", "purity", "restore", "restored", "security",
})
_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+")


def _sensitive_path(path: str) -> bool:
    for field in re.findall(r"\.([A-Za-z_][A-Za-z0-9_]*)", path):
        kept = _WORD.sub(
            lambda m: "/" if m.group().lower() in _BENIGN_WORDS else m.group(), field
        )
        if _BLOCKED.search(kept):
            return True
    return False


class ControlledInputError(ValueError):
    """Fail closed with a fixed code; never include source URLs or values."""

    def __init__(self, code: str = "controlled_input_invalid") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ControlledInput:
    compiled: CompiledScenario
    snapshot_sha256: str
    document: dict[str, Any]
    reference: dict[str, str]


def _reject(code="controlled_input_invalid"):
    raise ControlledInputError(code)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object(value, keys=None):
    if not isinstance(value, dict) or (keys is not None and set(value) != keys):
        _reject()
    return value


def _json(data):
    try:
        return comparison.json_value(data)
    except ValueError:
        raise ControlledInputError() from None


def _safe_path(root: Path, rel: str, *, exists=True) -> Path:
    if not isinstance(rel, str) or not rel or "\\" in rel:
        _reject("controlled_reference_invalid")
    part = Path(rel)
    if part.is_absolute() or ".." in part.parts or part.as_posix() != rel:
        _reject("controlled_reference_invalid")
    root = Path(root)
    # Refuse even in-domain symlinks, including the root and ancestor directories.
    for path in [root, *root.parents]:
        if path.is_symlink():
            _reject("controlled_reference_invalid")
    path = root
    for component in part.parts:
        path /= component
        if path.is_symlink():
            _reject("controlled_reference_invalid")
    if not path.resolve().is_relative_to(root.resolve()) or (
        exists and not path.is_file()
    ):
        _reject("controlled_reference_invalid")
    return path


def _read_ref(root: Path, ref: Mapping, allowed: str) -> tuple[dict, dict]:
    ref = dict(ref) if isinstance(ref, Mapping) else ref
    _object(ref, {"path", "sha256"})
    if (
        ref["path"] != allowed
        or not isinstance(ref["sha256"], str)
        or not _SHA.fullmatch(ref["sha256"])
    ):
        _reject("controlled_reference_invalid")
    try:
        data = _safe_path(root, ref["path"]).read_bytes()
    except OSError:
        raise ControlledInputError("controlled_reference_invalid") from None
    if _sha(data) != ref["sha256"]:
        _reject("controlled_snapshot_digest_mismatch")
    doc = _object(_json(data))
    registry = _object(_json(_safe_path(root, "artifact-manifest.json").read_bytes()))
    members = [
        item
        for item in registry.get("artifacts") or []
        if isinstance(item, dict) and item.get("path") == ref["path"]
    ]
    if (
        registry.get("run_id") != doc.get("run_id")
        or len(members) != 1
        or members[0].get("sha256") != ref["sha256"]
        or any(
            members[0].get(key) != value
            for key, value in {
                "retention": "local_only",
                "share_policy": "forbidden",
                "ai_policy": "forbidden",
                "redaction_status": "raw",
            }.items()
        )
    ):
        _reject("controlled_snapshot_registry_mismatch")
    return doc, dict(ref)


def _default_source_resolver(run_id: str) -> Path:
    from autophone.application.run_queries import RunQueries
    from autophone.paths import STATE_ROOT

    return RunQueries(
        STATE_ROOT / "runs/platform",
        active_runs=lambda: {},
        persist_result=lambda *_: None,
    ).find_run_dir(run_id)


def _identity(document: dict, *, run_id: str, app: str, device: str | None = None):
    if (
        document.get("run_id") != run_id
        or document.get("app") != app
        or not isinstance(document.get("device"), str)
        or not document["device"]
        or (device is not None and document["device"] != device)
    ):
        _reject("controlled_identity_mismatch")


def _source_validation(doc: dict, frozen: dict, *, private_dir: Path) -> str:
    """Rebuild and validate every selected physical member from frozen raw bytes."""
    source = _object(doc.get("source"), _SOURCE_KEYS)
    if not isinstance(source["run_id"], str) or not _ID.fullmatch(source["run_id"]):
        _reject()
    lines = source["lines"]
    if (
        not isinstance(lines, list)
        or not lines
        or any(type(n) is not int or n < 1 for n in lines)
        or len(set(lines)) != len(lines)
    ):
        _reject()
    frozen = _object(frozen, {"run_spec", "capture", "events", "run_spec_sha256"})
    for name in ("run_spec", "capture", "events"):
        if not isinstance(frozen[name], str):
            _reject()
    for content, expected in (
        (frozen["capture"], source["capture_sha256"]),
        (frozen["events"], source["capture_events_sha256"]),
        (frozen["run_spec"], frozen["run_spec_sha256"]),
        (source["body"], source["body_sha256"]),
    ):
        if (
            not isinstance(content, str)
            or not isinstance(expected, str)
            or not _SHA.fullmatch(expected)
        ):
            _reject()
        if _sha(content.encode()) != expected:
            _reject("controlled_source_digest_mismatch")
    run_spec = _object(_json(frozen["run_spec"]))
    _identity(
        run_spec, run_id=source["run_id"], app=doc["app"], device=doc.get("device")
    )
    planes = _object(run_spec.get("planes", {}))
    api = _object(planes.get("api", {}))
    if api.get("mode") == "controlled" or "scenario" in api:
        _reject("controlled_source_not_real")
    rows = [
        (i, _object(_json(line)))
        for i, line in enumerate(frozen["capture"].splitlines(), 1)
        if line.strip()
    ]
    events = [
        _object(_json(line)) for line in frozen["events"].splitlines() if line.strip()
    ]
    if any(
        row.get("scenario")
        or row.get("injected")
        or row.get("source_kind") == "controlled"
        for _, row in rows
    ):
        _reject("controlled_source_not_real")
    if any(
        str(e.get("ev") or "").startswith("scenario")
        or e.get("injected")
        or e.get("scenario")
        for e in events
    ):
        _reject("controlled_source_not_real")
    selected = [row for line, row in rows if line in lines]
    if len(selected) != len(lines):
        _reject()
    url = selected[0].get("url")
    if not isinstance(url, str):
        _reject()
    try:
        split = urlsplit(url)
    except ValueError:
        raise ControlledInputError("controlled_source_invalid") from None
    if (
        split.scheme not in ("http", "https")
        or not split.hostname
        or split.username
        or split.password
        or split.fragment
    ):
        _reject()
    endpoint = endpoints.endpoint_key("GET", url)
    # The existing lifecycle validator verifies seal ordering/coverage, raw merge
    # equality and physical task identity. Materialize only inside a private tmp.
    private_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(
        prefix=".validate-source-", dir=private_dir
    ) as temporary:
        root = Path(temporary)
        for rel, value in ((_CAPTURE, frozen["capture"]), (_EVENTS, frozen["events"])):
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value.encode())
        try:
            retained, _ = lifecycle_evidence.select(
                root=root,
                run_id=source["run_id"],
                rows=rows,
                endpoint=endpoint,
                events_sha256=source["capture_events_sha256"],
                capture_sha256=source["capture_sha256"],
                strategy="consistent_complete_response_set",
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ControlledInputError("controlled_source_invalid") from None
    if {line for line, _ in retained} != set(lines):
        _reject("controlled_source_members_mismatch")
    try:
        for _, row in retained:
            comparison._complete_set_response(row)
        if len({comparison._request_identity(row) for _, row in retained}) != 1:
            _reject("controlled_source_invalid")
    except comparison.EvidenceError:
        raise ControlledInputError("controlled_source_invalid") from None
    from autophone.observation.frida.net_capture import merge

    rebuilt = {line: row for (line, _), row in zip(rows, merge(events))}
    for line in lines:
        row = rebuilt[line]
        terminal = _object(row.get("task_lifecycle"))
        if (
            row.get("method") != "GET"
            or row.get("url") != url
            or row.get("status") != 200
            or row.get("resp_body_text") != source["body"]
            or row.get("resp_body_truncated")
            or row.get("resp_body_err")
            or row.get("resp_error")
            or terminal.get("terminal") is not True
            or terminal.get("state") != 3
            or terminal.get("error_code") is not None
            or terminal.get("native_response_present") is not True
            or terminal.get("snapshot_errors") != []
        ):
            _reject("controlled_source_invalid")
    return url


def _mutation(doc: dict, effective: str):
    mutation = _object(doc.get("mutation"), {"path", "before_sha256", "marker"})
    oracle = _object(doc.get("oracle"), {"channel", "region", "before_text", "marker"})
    path, marker = mutation["path"], mutation["marker"]
    if (
        not isinstance(path, str)
        or not _PATH.fullmatch(path)
        or _sensitive_path(path)
        or not isinstance(marker, str)
        or not marker.strip()
        or oracle["channel"] not in ("ax", "ocr")
        or not isinstance(oracle["region"], str)
        or not _ID.fullmatch(oracle["region"])
        or oracle["marker"] != marker
    ):
        _reject("controlled_mutation_invalid")
    before = _json(doc["source"]["body"])
    after = _json(effective)

    def has_marker(value):
        if isinstance(value, str):
            return marker in value
        if isinstance(value, dict):
            return any(has_marker(item) for item in value.values())
        if isinstance(value, list):
            return any(has_marker(item) for item in value)
        return False

    if has_marker(before):
        _reject("controlled_mutation_invalid")
    expected = copy.deepcopy(before)
    parts = [
        field or int(index)
        for field, index in re.findall(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[([0-9]+)\]", path)
    ]
    parent = expected
    try:
        for part in parts[:-1]:
            if isinstance(part, str) and not isinstance(parent, dict):
                _reject()
            if isinstance(part, int) and not isinstance(parent, list):
                _reject()
            parent = parent[part]
        leaf = parts[-1]
        if isinstance(leaf, str) and not isinstance(parent, dict):
            _reject()
        if isinstance(leaf, int) and not isinstance(parent, list):
            _reject()
        original = parent[leaf]
        if (
            not isinstance(original, str)
            or not original.strip()
            or oracle["before_text"] != original
            or mutation["before_sha256"] != _sha(original.encode())
            or marker in doc["source"]["body"]
        ):
            _reject("controlled_mutation_invalid")
        parent[leaf] = marker
    except (KeyError, IndexError, TypeError):
        raise ControlledInputError("controlled_mutation_invalid") from None
    # Strict JSON types: Python equality alone treats true as 1.
    canonical = lambda value: json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    if canonical(expected) != canonical(after):
        _reject("controlled_mutation_diff_invalid")


def _validate(doc: dict, *, private_dir: Path, freeze=False) -> CompiledScenario:
    _object(doc, _FROZEN_KEYS)
    if doc.get("schema_version") != "v1" or not isinstance(doc.get("app"), str):
        _reject()
    url = _source_validation(doc, doc.get("_frozen_source"), private_dir=private_dir)
    scenario = _object(doc.get("scenario"))
    if (
        set(scenario) - {"id", "app", "env", "rules", "name", "description"}
        or scenario.get("app") != doc["app"]
    ):
        _reject()
    rules = scenario.get("rules")
    if not isinstance(rules, list) or len(rules) != 1:
        _reject()
    rule = _object(rules[0])
    if set(rule) - {"rule_id", "name", "match", "action", "times", "assert_hit"}:
        _reject()
    match, action = _object(rule.get("match")), _object(rule.get("action"))
    split = urlsplit(url)
    if (
        set(match) - {"method", "host", "path", "query", "exact_url"}
        or match.get("method") != "GET"
        or match.get("host") != split.hostname
        or match.get("path") != (split.path or "/")
        or ("exact_url" in match and match["exact_url"] != url)
        or set(action) - {"type", "status", "headers", "body"}
        or action.get("type") != "fixed_json"
        or type(action.get("status")) is not int
        or action["status"] != 200
        or not isinstance(action.get("body"), str)
        or rule.get("times") != {"mode": "always"}
        or rule.get("assert_hit") is not True
    ):
        _reject("controlled_rule_invalid")
    headers = action.get("headers", {})
    if not isinstance(headers, dict):
        _reject("controlled_rule_invalid")
    seen = set()
    allowed = {
        "content-type": {"application/json", "application/json; charset=utf-8"},
        "cache-control": {"no-store"},
    }
    for key, value in headers.items():
        lower = key.lower()
        if lower in seen or lower not in allowed or value not in allowed[lower]:
            _reject("controlled_rule_invalid")
        seen.add(lower)
    if freeze:
        match["exact_url"] = url
    elif match.get("exact_url") != url:
        _reject("controlled_rule_invalid")
    _mutation(doc, action["body"])
    try:
        compiled = compile_rules(scenario)
    except ValueError:
        raise ControlledInputError("controlled_rule_invalid") from None
    query = parse_qs(split.query, keep_blank_values=True)
    if any(
        query.get(key) != [value]
        for key, value in compiled.rules[0]["match"].get("query", {}).items()
    ):
        _reject("controlled_rule_invalid")
    if freeze:
        doc["rule_snapshot_hash"] = compiled.snapshot_hash
    elif doc.get("rule_snapshot_hash") != compiled.snapshot_hash:
        _reject("controlled_rule_digest_mismatch")
    return compiled


def _write_snapshot(root: Path, rel: str, doc: dict) -> dict:
    from autophone.observation.artifacts import (
        Artifact,
        append_artifacts,
        write_artifact_manifest,
    )

    data = (
        json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()
    target = _safe_path(root, rel, exists=False)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A previously frozen input is immutable, including on retries.
    if target.exists():
        if target.read_bytes() != data:
            _reject("controlled_snapshot_exists")
    else:
        # Create with private permissions; no window with umask-derived mode.
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
            output.write(data)
        target.chmod(0o600)  # O_CREAT mode is still masked by umask.
    artifact = Artifact(
        rel,
        "other",
        sensitivity="sensitive",
        redaction_status="raw",
        retention="local_only",
        share_policy="forbidden",
        ai_policy="forbidden",
    )
    _safe_path(root, "artifact-manifest.json", exists=False)
    if (root / "artifact-manifest.json").is_file():
        append_artifacts(root, [artifact])
    else:
        write_artifact_manifest(root, str(doc["run_id"]), [artifact])
    return {"path": rel, "sha256": _sha(data)}


def stage_input(
    source_path: Path,
    impact_run_dir: Path,
    *,
    app: str,
    source_resolver: Callable[[str], Path] | None = None,
) -> dict:
    """Read source only through a trusted Run query, then freeze exact private bytes."""
    try:
        source_path = Path(source_path)
        # Input may be an explicit local CLI path; its symlinks remain forbidden.
        doc = _object(
            _json(_safe_path(source_path.parent, source_path.name).read_bytes()),
            _INPUT_KEYS,
        )
        if doc["app"] != app:
            _reject("controlled_identity_mismatch")
        source = _object(doc.get("source"), _SOURCE_KEYS)
        if not isinstance(source["run_id"], str) or not _ID.fullmatch(source["run_id"]):
            _reject()
        root = Path((source_resolver or _default_source_resolver)(source["run_id"]))
        contents = {
            name: _safe_path(root, rel).read_bytes().decode("utf-8")
            for name, rel in (
                ("run_spec", "run-spec.json"),
                ("capture", _CAPTURE),
                ("events", _EVENTS),
            )
        }
        source_spec = _object(_json(contents["run_spec"]))
        doc.update(
            run_id=Path(impact_run_dir).name,
            device=source_spec.get("device"),
            rule_snapshot_hash="",
            _frozen_source={
                **contents,
                "run_spec_sha256": _sha(contents["run_spec"].encode()),
            },
        )
        _validate(
            doc,
            freeze=True,
            private_dir=_safe_path(
                Path(impact_run_dir), IMPACT_REL, exists=False
            ).parent,
        )
        return _write_snapshot(Path(impact_run_dir), IMPACT_REL, doc)
    except ControlledInputError:
        raise
    except (OSError, UnicodeError, KeyError, TypeError, AttributeError, ValueError):
        raise ControlledInputError() from None


def stage_child(
    parent_run_dir: Path,
    ref: Mapping,
    child_run_dir: Path,
    *,
    run_id: str,
    app: str,
    device: str,
) -> dict:
    """Create a child-local immutable copy before submitting its Operation."""
    try:
        doc, _ = _read_ref(Path(parent_run_dir), ref, IMPACT_REL)
        _identity(doc, run_id=Path(parent_run_dir).name, app=app, device=device)
        _validate(
            doc,
            private_dir=_safe_path(Path(child_run_dir), CHILD_REL, exists=False).parent,
        )
        doc["run_id"] = run_id
        reference = _write_snapshot(Path(child_run_dir), CHILD_REL, doc)
        return {"mode": "controlled", "scenario": reference}
    except ControlledInputError:
        raise
    except (OSError, KeyError, TypeError, AttributeError, ValueError):
        raise ControlledInputError() from None


def load_for_run(run_dir: Path, spec: Mapping) -> ControlledInput | None:
    """Validate a controlled snapshot; ordinary modes cannot carry a scenario."""
    try:
        planes = _object(spec.get("planes", {}))
        api = _object(planes.get("api", {}))
        if api.get("mode") != "controlled":
            if "scenario" in api:
                _reject("controlled_mode_required")
            return None
        doc, ref = _read_ref(Path(run_dir), api.get("scenario"), CHILD_REL)
        _identity(
            doc,
            run_id=spec.get("run_id"),
            app=spec.get("app"),
            device=spec.get("device"),
        )
        compiled = _validate(
            doc, private_dir=_safe_path(Path(run_dir), CHILD_REL).parent
        )
        return ControlledInput(compiled, ref["sha256"], doc, ref)
    except ControlledInputError:
        raise
    except (OSError, KeyError, TypeError, AttributeError, ValueError):
        raise ControlledInputError() from None


def describe_input(run_dir: Path, ref: Mapping, *, app: str) -> dict:
    """Return safe plan/result metadata after validating the private parent input."""
    try:
        doc, reference = _read_ref(Path(run_dir), ref, IMPACT_REL)
        _identity(doc, run_id=Path(run_dir).name, app=app)
        compiled = _validate(
            doc, private_dir=_safe_path(Path(run_dir), IMPACT_REL).parent
        )
        return {
            "evidence_type": "controlled_api_change",
            "rule_snapshot_hash": compiled.snapshot_hash,
            "scenario": reference,
        }
    except ControlledInputError:
        raise
    except (OSError, KeyError, TypeError, AttributeError, ValueError):
        raise ControlledInputError() from None


def stage_reference(
    impact_runs_root: Path, request: Mapping, impact_run_dir: Path, *, app: str
) -> dict:
    """HTTP accepts an existing private snapshot in the server's Impact namespace."""
    try:
        _object(request, {"run_id", "path", "sha256"})
        run_id = request["run_id"]
        if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
            _reject("controlled_reference_invalid")
        parent = Path(impact_runs_root) / run_id
        reference = {"path": request["path"], "sha256": request["sha256"]}
        doc, _ = _read_ref(parent, reference, IMPACT_REL)
        _identity(doc, run_id=run_id, app=app)
        _validate(
            doc,
            private_dir=_safe_path(
                Path(impact_run_dir), IMPACT_REL, exists=False
            ).parent,
        )
        doc["run_id"] = Path(impact_run_dir).name
        return _write_snapshot(Path(impact_run_dir), IMPACT_REL, doc)
    except ControlledInputError:
        raise
    except (OSError, KeyError, TypeError, AttributeError, ValueError):
        raise ControlledInputError() from None
