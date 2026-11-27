"""Business Fixture requirement declarations and their runtime evidence contract.

A Case declares the business state it needs as Fixture requirements
(``apps/<app>/fixture_requirements.yaml``).  Each requirement names the Setup
Operation that provides the state, an independent Readback Operation, the real
identity keys the Fixture Context must record, its reversibility/effect and its
lease.

This module only validates declarations and runtime evidence.  It never runs a
provider or Readback: a declaration is a statement of intent, not a fact.  A
Fixture counts as ``ready`` only when a Run wrote ``fixture-readback.json``
whose entry binds the same requirement digest, a non-empty Readback reference,
every declared context key and, from contract version 2 on, the Readback
arguments the Runner actually passed.

Contract versions
-----------------
``autophone.case-fixture-requirements/2``
    ``readback`` must be ``{op: <name>, with: {...}}``; the ``check`` form of
    version 1 is rejected (``fixture_readback_check_unsupported``).  A Readback
    argument may reference the Fixture Context as ``{from_context: <key>}``.  The
    key must be one of the requirement's ``context_keys`` and the argument must
    be a declared ``string`` parameter of the Readback Operation.
``autophone.case-fixture-requirements/1`` (legacy, read only)
    Still loaded and validated with the version 2 rules, without rewriting the
    file.  A context reference needs version 2
    (``fixture_readback_binding_requires_v2``) and ``check`` is rejected as in
    version 2.

``autophone.fixture-readback-evidence/2``
    Each entry also records ``readback_args`` (the arguments passed to the
    Readback Operation after resolving context references, redacted for
    display), ``readback_bindings`` (argument name to context key),
    ``context_sources`` (context key to ``provider`` / ``setup`` / ``preset`` /
    ``readback``: the last writer of the value into the Fixture Context) and
    the digests ``context_sha256`` / ``readback_args_sha256`` of the raw values
    before redaction.  A Fixture is ready only when every declared context key
    comes from ``provider`` or ``readback`` and every bound argument digest
    equals the digest of its provider-written context value.
``autophone.fixture-readback-evidence/1`` (legacy)
    Still readable, but never ready (``fixture_readback_evidence_legacy``): it
    carries no argument, source or provider-reference guarantees.

Digests: a value the governance redaction leaves unchanged is recorded as
``sha256:<hex>`` of its canonical JSON.  A value the redaction changes (a
sensitive key name, phone or ID number) is recorded as ``hmac-sha256:<hex>``
under a random key that is discarded after the entry is built: a plain hash
of a short identifier can be enumerated, while a discarded key still lets the
card compare an argument with its context value inside the same entry.

The card recomputes every ``sha256:`` digest from the recorded display value
and, for a non-sensitive bound argument, also compares the display values.  An
``hmac-sha256:`` digest can only be compared with another digest of the same
entry.  The evidence is not signed: consistent edits of values and digests
together are not detected.  When the Run's ``artifact-manifest.json`` records
a SHA-256 for ``fixture-readback.json``, the file must match it.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from autophone.assets.apps import AppProfile

FIXTURE_REQUIREMENTS_SCHEMA = "autophone.case-fixture-requirements/2"
FIXTURE_REQUIREMENTS_SCHEMA_V1 = "autophone.case-fixture-requirements/1"
FIXTURE_REQUIREMENTS_SCHEMAS = frozenset({FIXTURE_REQUIREMENTS_SCHEMA, FIXTURE_REQUIREMENTS_SCHEMA_V1})
FIXTURE_READBACK_EVIDENCE_SCHEMA = "autophone.fixture-readback-evidence/2"
FIXTURE_READBACK_EVIDENCE_SCHEMA_V1 = "autophone.fixture-readback-evidence/1"
FIXTURE_READBACK_EVIDENCE_SCHEMAS = frozenset({
    FIXTURE_READBACK_EVIDENCE_SCHEMA, FIXTURE_READBACK_EVIDENCE_SCHEMA_V1,
})
FIXTURE_READBACK_EVIDENCE_NAME = "fixture-readback.json"
# Cleanup Readback（BL-083 2026-10-12 第八轮，§7.1 波次 A「资源身份 + 权威读回 + 安全清理」）：
# 声明了 ``cleanup`` 的 Fixture 在 Body 与 teardown 之后执行清理 Operation，再以独立的只读
# Readback Operation 证明状态已回到 setup 时的基线；证据写入 ``cleanup-readback.json``。
CLEANUP_READBACK_EVIDENCE_SCHEMA = "autophone.cleanup-readback-evidence/1"
CLEANUP_READBACK_EVIDENCE_NAME = "cleanup-readback.json"
CLEANUP_STATUSES = frozenset({"verified", "failed", "not_executed"})

REVERSIBILITY = frozenset({"read_only", "reversible", "irreversible"})
LEASES = frozenset({"none", "exclusive"})
EVIDENCE_STATUSES = frozenset({"ready", "failed"})
# Who put a context value into the Fixture Context: the declared provider, another
# setup Operation, a value carried in before setup (flow / run initial fixture),
# or the Readback itself (a key the provider left empty).
CONTEXT_SOURCES = frozenset({"provider", "setup", "preset", "readback"})
# Sources that prove this Case produced the identity: the declared provider
# wrote it during setup, or the Readback supplied a key the provider left empty.
READY_CONTEXT_SOURCES = frozenset({"provider", "readback"})
# ``{from_context: <key>}`` marks a Readback argument resolved from the Fixture
# Context the provider recorded.  Context values are identity strings, so only
# ``string`` Operation parameters may be bound.
CONTEXT_REF_KEY = "from_context"
# Cleanup Readback 参数还可引用 setup 结束时（Fixture Readback ready 时）的基线值：
# ``{from_baseline: <key>}``；``{from_context: <key>}`` 在 cleanup 中指清理 Operation 之后的当前值。
BASELINE_REF_KEY = "from_baseline"
BINDABLE_PARAM_TYPES = frozenset({"string"})

_FIELDS = frozenset({
    "id", "kind", "summary", "provider", "readback", "context_keys",
    "reversibility", "effect", "lease",
})
# 仅供展示的可选字段，不进 ``requirement_digest``：修改说明文字不使已有 Readback 证据失效。
# ``trial_preparation``：首次试跑前（例如新安装构建后）须先完成的人工或平台步骤，字符串列表；
# 准备卡在 Fixture Readback 行未验证时把它追加为下一步。
TRIAL_PREPARATION_FIELD = "trial_preparation"
_DISPLAY_FIELDS = frozenset({TRIAL_PREPARATION_FIELD})
# ``cleanup``：可选的清理声明，不进 ``requirement_digest``（加清理声明不使已有 Fixture Readback 证据
# 失效）；其自身摘要 ``cleanup_digest`` 由 Cleanup Readback 证据绑定。形态：
# ``{op, with?, readback: {op, with}, changed_keys?: [context key]}``。``changed_keys`` 中的键在清理后
# 必须与基线值不同（例如 ``app_pid``：证明清理确实冷启动了新进程）。
CLEANUP_FIELD = "cleanup"
_CLEANUP_FIELDS = frozenset({"op", "with", "readback", "changed_keys"})
_READBACK_FIELDS = frozenset({"op", "with"})
VALUE_DIGEST = re.compile(r"^(sha256|hmac-sha256):[0-9a-f]{64}$")


@dataclass(frozen=True)
class FixtureIssue:
    """One static contract violation.  ``fixture_id`` is empty for case-level issues."""

    case_id: str
    fixture_id: str
    code: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def fixture_requirements_path(profile: AppProfile) -> Path:
    return profile.profile_dir / "fixture_requirements.yaml"


def requirement_digest(requirement: dict[str, Any]) -> str:
    """Stable digest of the declared fields; runtime evidence must bind to it."""
    payload = {key: requirement.get(key) for key in sorted(_FIELDS)}
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_sha256(value: Any) -> str:
    """``sha256:<hex>`` of a value's canonical JSON (the non-sensitive digest form)."""
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_readback_evidence(run_dir: Path) -> tuple[Any, dict[str, Any]]:
    """Read one Run's ``fixture-readback.json`` and cross-check the Artifact Manifest.

    Returns ``(evidence, manifest_check)``.  ``manifest_check["state"]`` is
    ``match``, ``mismatch`` or ``not_recorded`` (no manifest, or no digest for
    the file).  A mismatch replaces the evidence with an invalid marker, so no
    Fixture can be judged ready from a file that changed after the manifest.
    """
    return load_manifest_checked_evidence(run_dir, FIXTURE_READBACK_EVIDENCE_NAME)


def load_manifest_checked_evidence(run_dir: Path, name: str) -> tuple[Any, dict[str, Any]]:
    """Read one Run evidence JSON and cross-check its SHA-256 in the Artifact Manifest."""
    path = Path(run_dir) / name
    if not path.is_file():
        return None, {"state": "not_recorded", "detail": "evidence file missing"}
    data = path.read_bytes()
    try:
        evidence = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"invalid": str(exc)}, {"state": "not_recorded", "detail": "evidence unreadable"}
    manifest_path = Path(run_dir) / "artifact-manifest.json"
    recorded = None
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        for item in (manifest.get("artifacts") if isinstance(manifest, dict) else None) or []:
            if isinstance(item, dict) and item.get("path") == name:
                recorded = item.get("sha256")
    if not recorded:
        return evidence, {"state": "not_recorded", "detail": "artifact manifest has no digest for the file"}
    actual = hashlib.sha256(data).hexdigest()
    if actual != recorded:
        return ({"invalid": f"{name} differs from artifact-manifest.json"},
                {"state": "mismatch", "detail": f"manifest {recorded} != file {actual}"})
    return evidence, {"state": "match", "detail": recorded}


def is_context_ref(value: Any) -> bool:
    """True for any value shaped like a context reference (valid or not)."""
    return isinstance(value, dict) and CONTEXT_REF_KEY in value


def context_ref_key(value: Any) -> str | None:
    """The referenced context key of a well-formed ``{from_context: <key>}``."""
    if not isinstance(value, dict) or set(value) != {CONTEXT_REF_KEY}:
        return None
    key = value[CONTEXT_REF_KEY]
    return key.strip() if isinstance(key, str) and key.strip() else None


def readback_args_declared(requirement: dict[str, Any]) -> dict[str, Any]:
    readback = requirement.get("readback")
    args = readback.get("with") if isinstance(readback, dict) else None
    return dict(args) if isinstance(args, dict) else {}


def readback_bindings(requirement: dict[str, Any]) -> dict[str, str]:
    """Readback argument name → referenced context key (well-formed refs only)."""
    out: dict[str, str] = {}
    for param, value in readback_args_declared(requirement).items():
        key = context_ref_key(value)
        if key is not None:
            out[str(param)] = key
    return out


def load_fixture_requirements(profile: AppProfile) -> dict[str, Any]:
    """Read the declaration document.

    Returns ``{"present": bool, "schema": str | None, "cases": {case_id: [requirement, ...]},
    "issues": [...]}``.  A missing file means nothing is declared; a malformed
    file is an issue, not an empty declaration.  A legacy version 1 document is
    read as is and never rewritten.
    """
    path = fixture_requirements_path(profile)
    if not path.is_file():
        return {"present": False, "schema": None, "path": str(path), "cases": {}, "issues": []}
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return _invalid_document(path, f"{type(exc).__name__}: {exc}")
    if not isinstance(doc, dict):
        return _invalid_document(path, "root must be a mapping")
    schema = doc.get("schema")
    if schema not in FIXTURE_REQUIREMENTS_SCHEMAS:
        return _invalid_document(
            path, f"schema must be {FIXTURE_REQUIREMENTS_SCHEMA} (or legacy {FIXTURE_REQUIREMENTS_SCHEMA_V1})")
    cases = doc.get("cases")
    if cases is None:
        cases = {}
    if not isinstance(cases, dict):
        return _invalid_document(path, "cases must be a mapping of case_id to a list")
    out: dict[str, list[Any]] = {}
    issues: list[FixtureIssue] = []
    for case_id, rows in cases.items():
        key = str(case_id).upper()
        if not isinstance(rows, list):
            issues.append(FixtureIssue(key, "", "fixture_requirements_invalid_case_entry",
                                       "case entry must be a list"))
            continue
        out[key] = list(rows)
    return {"present": True, "schema": schema, "path": str(path), "cases": out,
            "issues": [issue.to_dict() for issue in issues]}


def _invalid_document(path: Path, detail: str) -> dict[str, Any]:
    return {
        "present": True,
        "schema": None,
        "path": str(path),
        "cases": {},
        "issues": [FixtureIssue("", "", "fixture_requirements_invalid_document", detail).to_dict()],
    }


def _op_ref(value: Any) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(value, dict) or not str(value.get("op") or "").strip():
        return None
    args = value.get("with")
    if args is not None and not isinstance(args, dict):
        return None
    return str(value["op"]).strip(), dict(args or {})


def validate_case_fixtures(
    profile: AppProfile,
    case: dict[str, Any],
    rows: list[Any],
    *,
    schema: str | None = None,
) -> list[FixtureIssue]:
    """Static contract checks for one Case's declared Fixture requirements.

    ``schema`` is the declaration document version; None means the current one.
    """
    from autophone.assets.operations import OperationError, load_operations, resolve_operation

    schema = schema or FIXTURE_REQUIREMENTS_SCHEMA
    case_id = str(case.get("id") or "").upper()
    setup_ops = {
        str(ref.get("op"))
        for ref in case.get("setup") or []
        if isinstance(ref, dict) and ref.get("op")
    }
    allowed_effects = set(profile.allowed_effects or [])
    issues: list[FixtureIssue] = []
    seen: set[str] = set()

    def add(fixture_id: str, code: str, detail: str = "") -> None:
        issues.append(FixtureIssue(case_id, fixture_id, code, detail))

    def _check_readback(row: dict[str, Any], label: str,
                        provider: tuple[str, dict[str, Any]] | None,
                        keys: list[str] | None) -> None:
        readback = row.get("readback")
        if isinstance(readback, dict) and "check" in readback:
            add(label, "fixture_readback_check_unsupported",
                "readback must be {op: <name>, with: {...}}; the check form is not executable")
            return
        readback_op = _op_ref(readback)
        if readback_op is None:
            add(label, "fixture_readback_missing", "readback must be {op: <name>, with: {...}}")
            return
        extra = sorted(set(readback) - _READBACK_FIELDS)
        if extra:
            add(label, "fixture_readback_invalid", f"unknown readback field(s): {', '.join(extra)}")
        op_name, args = readback_op
        if provider is not None and op_name == provider[0]:
            add(label, "fixture_readback_not_independent",
                "Readback must not reuse the provider operation")
        try:
            op_params = {
                str(param.get("name")): param
                for param in (load_operations(profile).get(op_name) or {}).get("params") or []
                if isinstance(param, dict)
            }
        except OperationError:
            op_params = {}
        literal: dict[str, Any] = {}
        placeholders: dict[str, Any] = {}
        for param, value in args.items():
            if not is_context_ref(value):
                literal[param] = value
                continue
            if schema == FIXTURE_REQUIREMENTS_SCHEMA_V1:
                add(label, "fixture_readback_binding_requires_v2",
                    f"argument {param} references the Fixture Context; declare schema "
                    f"{FIXTURE_REQUIREMENTS_SCHEMA}")
                continue
            key = context_ref_key(value)
            if key is None:
                add(label, "fixture_readback_binding_invalid",
                    f"argument {param} must be {{{CONTEXT_REF_KEY}: <context key>}}")
                continue
            if keys is not None and key not in keys:
                add(label, "fixture_readback_binding_unknown_key",
                    f"argument {param} references {key}, which is not a declared context key")
            spec = op_params.get(str(param))
            if spec is None:
                add(label, "fixture_readback_binding_unknown_param",
                    f"{op_name} declares no parameter {param}")
                continue
            if spec.get("type") not in BINDABLE_PARAM_TYPES:
                add(label, "fixture_readback_binding_type_mismatch",
                    f"parameter {param} is {spec.get('type')!r}; a context reference binds "
                    f"only {sorted(BINDABLE_PARAM_TYPES)}")
                continue
            placeholders[param] = ""
        try:
            resolve_operation(profile, op_name, {**literal, **placeholders})
        except OperationError as exc:
            add(label, "fixture_readback_invalid", str(exc))

    def _check_cleanup(cleanup: Any, label: str, keys: list[str] | None) -> None:
        cleanup_op = _op_ref(cleanup)
        if cleanup_op is None:
            add(label, "fixture_cleanup_invalid", "cleanup must be {op: <name>, with: {...}, readback: {...}}")
            return
        extra = sorted(set(cleanup) - _CLEANUP_FIELDS)
        if extra:
            add(label, "fixture_cleanup_invalid", f"unknown cleanup field(s): {', '.join(extra)}")
        if any(is_context_ref(v) or is_baseline_ref(v) for v in cleanup_op[1].values()):
            add(label, "fixture_cleanup_invalid", "cleanup operation arguments must be literals")
        else:
            try:
                resolve_operation(profile, cleanup_op[0], cleanup_op[1])
            except OperationError as exc:
                add(label, "fixture_cleanup_invalid", str(exc))
        readback = cleanup.get("readback")
        readback_op = _op_ref(readback)
        if readback_op is None or set(readback) - _READBACK_FIELDS:
            add(label, "fixture_cleanup_readback_invalid", "cleanup readback must be {op: <name>, with: {...}}")
            return
        if readback_op[0] == cleanup_op[0]:
            add(label, "fixture_cleanup_readback_not_independent",
                "cleanup Readback must not reuse the cleanup operation")
        try:
            op_params = {
                str(param.get("name")): param
                for param in (load_operations(profile).get(readback_op[0]) or {}).get("params") or []
                if isinstance(param, dict)
            }
        except OperationError:
            op_params = {}
        resolved: dict[str, Any] = {}
        for param, value in readback_op[1].items():
            ref = cleanup_ref(value)
            if ref is None and (is_context_ref(value) or is_baseline_ref(value)):
                add(label, "fixture_cleanup_binding_invalid",
                    f"argument {param} must be {{{CONTEXT_REF_KEY}|{BASELINE_REF_KEY}: <context key>}}")
                continue
            if ref is None:
                resolved[param] = value
                continue
            if keys is not None and ref[1] not in keys:
                add(label, "fixture_cleanup_binding_unknown_key",
                    f"argument {param} references {ref[1]}, which is not a declared context key")
            spec = op_params.get(str(param))
            if spec is None or spec.get("type") not in BINDABLE_PARAM_TYPES:
                add(label, "fixture_cleanup_binding_unknown_param",
                    f"{readback_op[0]} declares no string parameter {param}")
                continue
            resolved[param] = ""
        try:
            resolve_operation(profile, readback_op[0], resolved)
        except OperationError as exc:
            add(label, "fixture_cleanup_readback_invalid", str(exc))
        changed = cleanup.get("changed_keys", [])
        if not isinstance(changed, list) or not all(isinstance(k, str) and k for k in changed) or (
                keys is not None and set(changed) - set(keys)):
            add(label, "fixture_cleanup_changed_keys_invalid",
                "changed_keys must list declared context keys")

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            add(f"#{index}", "fixture_requirement_not_mapping")
            continue
        fixture_id = str(row.get("id") or "").strip()
        label = fixture_id or f"#{index}"
        if not fixture_id:
            add(label, "fixture_id_missing")
        elif fixture_id in seen:
            add(label, "fixture_id_duplicate")
        seen.add(fixture_id)
        unknown = sorted(set(row) - _FIELDS - _DISPLAY_FIELDS - {CLEANUP_FIELD})
        if unknown:
            add(label, "fixture_unknown_field", ", ".join(unknown))
        if TRIAL_PREPARATION_FIELD in row:
            notes = row[TRIAL_PREPARATION_FIELD]
            if not isinstance(notes, list) or not all(isinstance(n, str) and n.strip() for n in notes):
                add(label, "fixture_trial_preparation_invalid",
                    f"{TRIAL_PREPARATION_FIELD} must be a list of non-empty strings")
        if not str(row.get("kind") or "").strip():
            add(label, "fixture_kind_missing")

        keys = row.get("context_keys")
        keys_valid = isinstance(keys, list) and bool(keys) and all(
            isinstance(key, str) and key.strip() for key in keys
        )
        if not keys_valid:
            add(label, "fixture_context_keys_missing",
                "context_keys must list the real identity fields the Context records")

        provider = _op_ref(row.get("provider"))
        if provider is None:
            add(label, "fixture_provider_missing", "provider must be {op: <name>, with: {...}}")
        elif any(is_context_ref(value) for value in provider[1].values()):
            add(label, "fixture_provider_binding_unsupported",
                "only readback arguments may reference the Fixture Context")
        else:
            try:
                resolve_operation(profile, provider[0], provider[1])
            except OperationError as exc:
                add(label, "fixture_provider_invalid", str(exc))
        if provider is not None and provider[0] not in setup_ops:
            add(label, "fixture_provider_not_in_setup",
                f"provider {provider[0]} is not a Case setup operation")

        _check_readback(row, label, provider, keys if keys_valid else None)
        if CLEANUP_FIELD in row:
            _check_cleanup(row[CLEANUP_FIELD], label, keys if keys_valid else None)

        reversibility = row.get("reversibility")
        if reversibility not in REVERSIBILITY:
            add(label, "fixture_reversibility_invalid",
                f"reversibility must be one of {sorted(REVERSIBILITY)}")
        effect = str(row.get("effect") or "").strip()
        if reversibility == "irreversible":
            if not effect:
                add(label, "fixture_effect_missing", "irreversible Fixture must name its effect")
            elif effect not in allowed_effects:
                add(label, "fixture_effect_not_allowed",
                    f"effect {effect} is not in App allowed_effects")
        lease = row.get("lease", "none")
        if lease not in LEASES:
            add(label, "fixture_lease_invalid", f"lease must be one of {sorted(LEASES)}")
    return issues


def validate_app_fixture_requirements(profile: AppProfile) -> dict[str, Any]:
    """Validate every declaration of one App against its Case Registry."""
    document = load_fixture_requirements(profile)
    registry: Any = {}
    if profile.cases_path().is_file():
        registry = yaml.safe_load(profile.cases_path().read_text(encoding="utf-8")) or {}
    rows_in = registry.get("cases") if isinstance(registry, dict) else None
    cases = {
        str(row.get("id") or "").upper(): row
        for row in rows_in or []
        if isinstance(row, dict)
    }
    issues = list(document["issues"])
    declared: dict[str, list[dict[str, Any]]] = {}
    for case_id, rows in document["cases"].items():
        case = cases.get(case_id)
        if case is None:
            issues.append(FixtureIssue(case_id, "", "fixture_requirements_unknown_case").to_dict())
            continue
        issues += [issue.to_dict() for issue in validate_case_fixtures(
            profile, case, rows, schema=document["schema"])]
        declared[case_id] = declared_fixtures(rows)
    return {
        "schema": FIXTURE_REQUIREMENTS_SCHEMA,
        "document_schema": document["schema"],
        "app": profile.app,
        "path": document["path"],
        "present": document["present"],
        "valid": not issues,
        "cases": declared,
        "issues": issues,
    }


def declared_fixtures(rows: list[Any]) -> list[dict[str, Any]]:
    """Display projection of declarations with their binding digest."""
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append({
            "id": str(row.get("id") or ""),
            "kind": str(row.get("kind") or ""),
            "summary": str(row.get("summary") or ""),
            "provider": row.get("provider"),
            "readback": row.get("readback"),
            "readback_bindings": readback_bindings(row),
            "context_keys": list(row.get("context_keys") or []),
            "reversibility": row.get("reversibility"),
            "effect": row.get("effect"),
            "lease": row.get("lease", "none"),
            TRIAL_PREPARATION_FIELD: trial_preparation(row),
            "requirement_digest": requirement_digest(row),
            CLEANUP_FIELD: row.get(CLEANUP_FIELD) if isinstance(row.get(CLEANUP_FIELD), dict) else None,
            "cleanup_digest": cleanup_digest(row),
        })
    return out


def is_baseline_ref(value: Any) -> bool:
    return isinstance(value, dict) and BASELINE_REF_KEY in value


def cleanup_ref(value: Any) -> tuple[str, str] | None:
    """``("context"|"baseline", key)`` of a well-formed cleanup Readback reference."""
    if not isinstance(value, dict) or len(value) != 1:
        return None
    (ref_kind, key), = value.items()
    if ref_kind not in (CONTEXT_REF_KEY, BASELINE_REF_KEY) or not isinstance(key, str) or not key.strip():
        return None
    return ("context" if ref_kind == CONTEXT_REF_KEY else "baseline"), key.strip()


def cleanup_digest(row: Any) -> str | None:
    """Digest of the ``cleanup`` declaration; Cleanup Readback evidence binds to it."""
    cleanup = row.get(CLEANUP_FIELD) if isinstance(row, dict) else None
    return canonical_sha256(cleanup) if isinstance(cleanup, dict) else None


def load_cleanup_evidence(run_dir: Path) -> tuple[Any, dict[str, Any]]:
    return load_manifest_checked_evidence(run_dir, CLEANUP_READBACK_EVIDENCE_NAME)


def cleanup_runtime_states(
    requirements: list[dict[str, Any]],
    evidence: Any,
    *,
    case_id: str,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Per declared cleanup: ``verified`` / ``failed`` / ``missing`` / ``unverified``.

    ``verified`` only when the entry binds the current ``cleanup_digest``, has
    status ``verified`` and non-empty cleanup and Readback action references.
    """
    out: list[dict[str, Any]] = []
    declared = [req for req in requirements if req.get("cleanup_digest")]
    doc_ok = isinstance(evidence, dict) and evidence.get("schema") == CLEANUP_READBACK_EVIDENCE_SCHEMA
    run_ok = doc_ok and (run_id is None or evidence.get("run_id") in (None, run_id))
    entries = {
        str(entry.get("fixture_id")): entry
        for entry in (((evidence or {}).get("cases") or {}).get(case_id.upper()) or [] if doc_ok else [])
        if isinstance(entry, dict)
    }
    for req in declared:
        fixture_id = str(req.get("id") or "")
        entry = entries.get(fixture_id)
        state, reason = "verified", ""
        if evidence is None:
            state, reason = "missing", "cleanup-readback.json missing"
        elif not doc_ok:
            state, reason = "unverified", "cleanup evidence invalid or schema mismatch"
        elif not run_ok:
            state, reason = "unverified", "cleanup evidence belongs to another run"
        elif entry is None:
            state, reason = "missing", "no cleanup entry for this Fixture"
        elif entry.get("cleanup_digest") != req.get("cleanup_digest"):
            state, reason = "unverified", "cleanup declaration changed after the run"
        elif entry.get("status") == "not_executed":
            state, reason = "missing", str(entry.get("reason") or "cleanup not executed")
        elif entry.get("status") != "verified":
            state, reason = "failed", str(entry.get("reason") or entry.get("status"))
        elif not str(entry.get("cleanup_ref") or "").strip() or not str(entry.get("readback_ref") or "").strip():
            state, reason = "unverified", "cleanup or Readback action reference missing"
        out.append({"fixture_id": fixture_id, "state": state, "reason": reason,
                    "detail": str((entry or {}).get("detail") or "")})
    return out


def trial_preparation(row: Any) -> list[str]:
    """声明中合法的 ``trial_preparation`` 条目；缺省或形态不符时为空（形态问题由校验另报）。"""
    notes = row.get(TRIAL_PREPARATION_FIELD) if isinstance(row, dict) else None
    if not isinstance(notes, list):
        return []
    return [n.strip() for n in notes if isinstance(n, str) and n.strip()]


def validate_readback_evidence(doc: Any, *, run_id: str | None = None) -> list[str]:
    """Shape errors for one ``fixture-readback.json`` document (version 1 or 2)."""
    if not isinstance(doc, dict):
        return ["evidence root must be a mapping"]
    errors: list[str] = []
    schema = doc.get("schema")
    if schema not in FIXTURE_READBACK_EVIDENCE_SCHEMAS:
        errors.append(f"schema must be {FIXTURE_READBACK_EVIDENCE_SCHEMA} "
                      f"(or legacy {FIXTURE_READBACK_EVIDENCE_SCHEMA_V1})")
    if run_id is not None and str(doc.get("run_id") or "") != run_id:
        errors.append("run_id does not match the Run directory")
    cases = doc.get("cases")
    if not isinstance(cases, dict):
        return [*errors, "cases must be a mapping"]
    for case_id, entries in cases.items():
        if not isinstance(entries, list):
            errors.append(f"cases.{case_id} must be a list")
            continue
        for index, entry in enumerate(entries):
            where = f"cases.{case_id}[{index}]"
            if not isinstance(entry, dict):
                errors.append(f"{where} must be a mapping")
                continue
            if not str(entry.get("fixture_id") or "").strip():
                errors.append(f"{where}.fixture_id is required")
            if entry.get("status") not in EVIDENCE_STATUSES:
                errors.append(f"{where}.status must be one of {sorted(EVIDENCE_STATUSES)}")
            if not str(entry.get("requirement_digest") or "").strip():
                errors.append(f"{where}.requirement_digest is required")
            if not isinstance(entry.get("context") or {}, dict):
                errors.append(f"{where}.context must be a mapping")
            if schema == FIXTURE_READBACK_EVIDENCE_SCHEMA:
                if not isinstance(entry.get("readback_args"), dict):
                    errors.append(f"{where}.readback_args must be a mapping")
                bindings = entry.get("readback_bindings")
                if not isinstance(bindings, dict) or not all(
                    isinstance(value, str) for value in bindings.values()
                ):
                    errors.append(f"{where}.readback_bindings must map argument names to context keys")
                sources = entry.get("context_sources")
                if not isinstance(sources, dict) or not all(
                    value in CONTEXT_SOURCES for value in sources.values()
                ):
                    errors.append(f"{where}.context_sources must map context keys to "
                                  f"{sorted(CONTEXT_SOURCES)}")
                for field in ("context_sha256", "readback_args_sha256"):
                    digests = entry.get(field)
                    if not isinstance(digests, dict) or not all(
                        isinstance(value, str) and VALUE_DIGEST.match(value)
                        for value in digests.values()
                    ):
                        errors.append(f"{where}.{field} must map names to sha256:<hex> or "
                                      "hmac-sha256:<hex> digests")
    return errors


def _binding_mismatch(req: dict[str, Any], entry: dict[str, Any]) -> str | None:
    """Why the recorded Readback arguments do not prove the declared binding (v2)."""
    bindings = dict(req.get("readback_bindings") or {})
    if dict(entry.get("readback_bindings") or {}) != bindings:
        return "recorded readback_bindings differ from the declaration"
    sources = entry.get("context_sources") or {}
    not_provider = sorted(key for key in bindings.values() if sources.get(key) != "provider")
    if not_provider:
        return "bound context key(s) not written by the provider: " + ", ".join(not_provider)
    args = dict(entry.get("readback_args") or {})
    literal = {
        param: value for param, value in readback_args_declared(req).items()
        if param not in bindings
    }
    if set(args) != set(literal) | set(bindings) or any(
        args.get(param) != value for param, value in literal.items()
    ):
        return "recorded readback_args differ from the declared arguments"
    context_digests = entry.get("context_sha256") or {}
    arg_digests = entry.get("readback_args_sha256") or {}
    context = entry.get("context") or {}
    unmatched = sorted(
        param for param, key in bindings.items()
        if not arg_digests.get(param) or arg_digests.get(param) != context_digests.get(key)
        # A non-sensitive argument is displayed unredacted: its value must match too.
        or (str(arg_digests.get(param)).startswith("sha256:") and args.get(param) != context.get(key))
    )
    if unmatched:
        return "bound argument digest differs from its context value: " + ", ".join(unmatched)
    return None


def _digest_mismatch(req: dict[str, Any], entry: dict[str, Any]) -> str | None:
    """Why the recorded digests do not describe the recorded display values (v2)."""
    context = entry.get("context") or {}
    context_digests = entry.get("context_sha256") or {}
    problems: list[str] = []
    for key in req["context_keys"]:
        digest = str(context_digests.get(key) or "")
        if not digest:
            problems.append(f"context {key}: digest missing")
        elif digest.startswith("sha256:") and digest != canonical_sha256(context.get(key)):
            problems.append(f"context {key}: digest does not match the recorded value")
    args = entry.get("readback_args") or {}
    for param, digest in (entry.get("readback_args_sha256") or {}).items():
        if str(digest).startswith("sha256:") and digest != canonical_sha256(args.get(param)):
            problems.append(f"argument {param}: digest does not match the recorded value")
    return "; ".join(problems) or None


def fixture_runtime_states(
    requirements: list[dict[str, Any]],
    evidence: Any,
    *,
    case_id: str,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Per-Fixture runtime state from one Run's readback evidence.

    ``requirements`` are ``declared_fixtures`` rows.  Anything short of a
    complete, digest-bound ``ready`` entry whose recorded Readback arguments
    match the declaration stays ``unverified`` or ``failed``.
    """
    if evidence is None:
        return [
            {"fixture_id": req["id"], "state": "unverified",
             "reason": "fixture_readback_evidence_missing"}
            for req in requirements
        ]
    errors = validate_readback_evidence(evidence, run_id=run_id)
    if errors:
        return [
            {"fixture_id": req["id"], "state": "unverified",
             "reason": "fixture_readback_evidence_invalid", "detail": "; ".join(errors)}
            for req in requirements
        ]
    if evidence.get("schema") != FIXTURE_READBACK_EVIDENCE_SCHEMA:
        return [
            {"fixture_id": req["id"], "state": "unverified",
             "reason": "fixture_readback_evidence_legacy",
             "detail": f"{evidence.get('schema')} evidence is readable but never ready"}
            for req in requirements
        ]
    entries = {
        str(entry.get("fixture_id")): entry
        for entry in (evidence.get("cases") or {}).get(case_id.upper()) or []
        if isinstance(entry, dict)
    }
    out: list[dict[str, Any]] = []
    for req in requirements:
        entry = entries.get(req["id"])
        state = {"fixture_id": req["id"]}
        if entry is None:
            out.append({**state, "state": "unverified", "reason": "fixture_readback_entry_missing"})
            continue
        if entry.get("requirement_digest") != req["requirement_digest"]:
            out.append({**state, "state": "unverified", "reason": "fixture_requirement_changed"})
            continue
        if entry.get("status") != "ready":
            out.append({**state, "state": "failed", "reason": "fixture_readback_failed",
                        "readback_ref": entry.get("readback_ref")})
            continue
        if not str(entry.get("readback_ref") or "").strip():
            out.append({**state, "state": "unverified", "reason": "fixture_readback_ref_missing"})
            continue
        context = entry.get("context") or {}
        missing = [key for key in req["context_keys"] if context.get(key) in (None, "")]
        if missing:
            out.append({**state, "state": "unverified", "reason": "fixture_context_incomplete",
                        "detail": ", ".join(missing)})
            continue
        sources = entry.get("context_sources") or {}
        foreign = [key for key in req["context_keys"] if sources.get(key) not in READY_CONTEXT_SOURCES]
        if foreign:
            out.append({**state, "state": "unverified", "reason": "fixture_context_source_unverified",
                        "detail": ", ".join(f"{key}={sources.get(key)}" for key in foreign)})
            continue
        digest_problem = _digest_mismatch(req, entry)
        if digest_problem is not None:
            out.append({**state, "state": "unverified", "reason": "fixture_readback_digest_mismatch",
                        "detail": digest_problem})
            continue
        mismatch = _binding_mismatch(req, entry)
        if mismatch is not None:
            out.append({**state, "state": "unverified", "reason": "fixture_readback_binding_unverified",
                        "detail": mismatch})
            continue
        out.append({**state, "state": "ready", "readback_ref": entry.get("readback_ref"),
                    "provider_ref": entry.get("provider_ref"),
                    "readback_args": entry.get("readback_args"),
                    "context_sources": entry.get("context_sources")})
    return out
