"""Trial-time production of Fixture Readback evidence (usecase assetization §10).

A Case declares its business Fixtures in ``apps/<app>/fixture_requirements.yaml``
(``autophone.assets.fixture_requirements``).  The provider of each Fixture is a
Case ``setup`` Operation, so the Runner already executes it.  This module adds
the missing half for explicit Draft Trials and golden path Runs:

1. ``bind_fixture_readback_plans`` attaches the declared requirements of each
   identity-bound Trial Case, and of the Case of a Run submitted for a golden
   path (``autophone.execution.golden_path_binding``), to its in-memory run plan.  A Case with any
   declaration issue (document level, case entry or row level) is bound too,
   so the Runner can block it.
2. Before any setup side effect, ``static_block`` rejects Readbacks the Runner
   cannot execute: any declaration issue (including the removed ``check``
   form), an unregistered or malformed Operation, or an Operation whose
   registered effect is irreversible.
3. After setup, ``resolve_readback_step`` resolves ``{from_context: <key>}``
   arguments from values the provider wrote into the Fixture Context, the
   Runner dispatches the Readback Operation through the registered-Operation
   dispatcher, and ``judge`` decides ``ready`` with the same function the
   preparation card uses (``fixture_runtime_states``).
4. ``write_fixture_readback_evidence`` writes ``fixture-readback.json``
   (``autophone.fixture-readback-evidence/2``).

Context values come from the Fixture Context carrier ``CaseContext.fixture_order``.
Each recorded key carries its source, the last writer in setup order:
``provider`` (the declared provider during this Case's setup), ``setup``
(another setup Operation), ``preset`` (carried in before setup, e.g. by a
flow) or ``readback`` (a key the Readback supplied).  Only ``provider`` and
``readback`` sources can be ready; a bound argument additionally needs the
value the provider wrote.  Writes are inferred by comparing values before and
after each setup Operation, so a provider that rewrites a preset value with
the same value is recorded as ``preset`` and fails closed.  A Readback must not
rebind a key that already had a value.  Context values, bound arguments and
record details pass through the Phase 4/5 governance redaction before they are
recorded; the raw values only feed digests (see
``autophone.assets.fixture_requirements``).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from pathlib import Path
from typing import Any, Iterable

from autophone.assets.fixture_requirements import (
    FIXTURE_READBACK_EVIDENCE_NAME,
    FIXTURE_READBACK_EVIDENCE_SCHEMA,
    canonical_sha256,
    declared_fixtures,
    fixture_runtime_states,
    load_fixture_requirements,
    readback_args_declared,
    validate_case_fixtures,
)

logger = logging.getLogger(__name__)

READBACK_PLAN_KEY = "_fixture_readback_plan"
RESULT_JSON = "result.json"
# ``static_block`` key when a Case is invalid without a parsable requirement.
CASE_LEVEL = "*"

# Record reasons.  They explain a non-ready record; ``status`` stays within the
# evidence contract (``ready`` / ``failed``).
REASON_CONTRACT_INVALID = "fixture_contract_invalid"
REASON_OPERATION_INVALID = "readback_operation_invalid"
REASON_OPERATION_IRREVERSIBLE = "readback_operation_irreversible"
REASON_NOT_EXECUTED = "readback_not_executed"
REASON_SETUP_NOT_READY = "provider_setup_not_ready"
REASON_PROVIDER_NOT_EXECUTED = "provider_not_executed"
REASON_BINDING_UNRESOLVED = "readback_binding_unresolved"
REASON_READBACK_FAILED = "readback_operation_failed"
REASON_READBACK_INFRA_ERROR = "readback_infra_error"
REASON_IDENTITY_REBOUND = "fixture_identity_rebound_by_readback"


def action_ref(action_id: str) -> str:
    """Reference to one recorded action in the Run's ``result.json``."""
    return f"{RESULT_JSON}#action={action_id}" if action_id else ""


def _redact(key: str, value: Any) -> Any:
    """Governance redaction of one context value (sensitive key names, phone/ID numbers)."""
    from autophone.execution.fixtures.contract import redact_sensitive

    return redact_sensitive({key: value})[key]


def redact_text(text: str) -> str:
    """Governance redaction of free text (exception and step details)."""
    from autophone.execution.fixtures.contract import redact_sensitive

    out = redact_sensitive(str(text or ""))
    return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)


def value_digest(key: str, value: Any, secret: bytes) -> str:
    """Digest of one raw value, computed before redaction.

    A value the governance redaction leaves unchanged gets a plain SHA-256.  A
    sensitive value (the redaction changes it) gets an HMAC under ``secret``,
    a per-entry random key that is never recorded, so a short identifier such
    as a phone number cannot be recovered by enumerating the digest.
    """
    if _redact(key, value) == value:
        return canonical_sha256(value)
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "hmac-sha256:" + hmac.new(secret, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


# ── plan binding (RunExecution side) ─────────────────────────────────────────


def plan_for_case(profile: Any, case: dict[str, Any]) -> dict[str, Any] | None:
    """Declared Fixtures of one Case as a JSON-safe run plan, or None.

    None only when the Case declares nothing and the declaration document has
    no issue that could concern it.  A document-level issue (unreadable file,
    wrong schema) may hide this Case's declarations, so it binds a plan.
    """
    case_id = str(case.get("id") or "").upper()
    document = load_fixture_requirements(profile)
    rows = document["cases"].get(case_id, [])
    issues = [dict(issue) for issue in document["issues"]
              if issue.get("case_id") in {"", case_id}]
    if rows:
        issues += [issue.to_dict() for issue in validate_case_fixtures(
            profile, case, rows, schema=document["schema"])]
    if not rows and not issues:
        return None
    return {
        "case_id": case_id,
        "declaration_path": document["path"],
        "declaration_schema": document["schema"],
        "requirements": declared_fixtures(rows),
        "issues": issues,
    }


def bind_fixture_readback_plans(profile: Any, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach Readback plans to explicit Draft Trial Cases and golden path Cases only.

    Two kinds of Case get a plan: a Case bound to a frozen draft identity
    (``_trial_recipe_identity``, an explicit Draft Trial), and the Case of a
    Run submitted for a golden path (``golden_path_binding.GOLDEN_PATH_KEY``,
    set only after the marker was checked against the declaration and the live
    Recipe was frozen).  Other Cases, including every ordinary regression Case,
    keep their existing execution unchanged.
    """
    from autophone.execution.golden_path_binding import GOLDEN_PATH_KEY

    bound: list[dict[str, Any]] = []
    for raw in cases:
        case = dict(raw)
        if (
            isinstance(case.get("_trial_recipe_identity"), dict)
            or isinstance(case.get(GOLDEN_PATH_KEY), dict)
        ):
            plan = plan_for_case(profile, case)
            if plan is not None:
                case[READBACK_PLAN_KEY] = plan
        bound.append(case)
    return bound


# ── runner side ──────────────────────────────────────────────────────────────


def _provider_op(requirement: dict[str, Any]) -> str:
    return str((requirement.get("provider") or {}).get("op") or "")


def context_sources(
    requirement: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    setup_trace: list[dict[str, Any]],
) -> dict[str, str]:
    """Source of every declared context key that has a value.

    ``before`` is the Fixture Context at the end of setup.  A key is
    ``provider`` only when the provider was its last setup writer and the value
    still equals what the provider wrote; ``setup`` when another setup
    Operation wrote it last, or when a setup-written key no longer has a value
    (a Readback must not re-supply a deleted identity); ``preset`` when no
    setup Operation wrote it; ``readback`` only for a key setup never wrote.
    """
    provider = _provider_op(requirement)
    written_by_provider = provider_values(requirement, setup_trace)
    last_writer: dict[str, str] = {}
    for entry in setup_trace:
        for key in entry.get("wrote_keys") or []:
            last_writer[key] = str(entry.get("operation") or "")
    out: dict[str, str] = {}
    for key in requirement.get("context_keys") or []:
        writer = last_writer.get(key)
        if before.get(key) not in (None, ""):
            if writer is None:
                out[key] = "preset"
            elif writer == provider and written_by_provider.get(key) == before.get(key):
                out[key] = "provider"
            else:
                out[key] = "setup"
        elif after.get(key) not in (None, ""):
            out[key] = "readback" if writer is None else "setup"
    return out


def provider_values(requirement: dict[str, Any], setup_trace: list[dict[str, Any]]) -> dict[str, Any]:
    """Values the declared provider wrote, last write winning."""
    provider = _provider_op(requirement)
    out: dict[str, Any] = {}
    for entry in setup_trace:
        if entry.get("operation") == provider:
            out.update(entry.get("written") or {})
    return out


def resolve_readback_step(
    requirement: dict[str, Any],
    context: dict[str, Any],
    sources: dict[str, str] | None = None,
    written: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Recipe step with context references resolved, and unresolved problems.

    A bound argument resolves only to a non-empty string the provider wrote
    into the Fixture Context during this Case's setup, and only while the
    context still holds that same value.  A preset or other setup value is not
    a provider identity.  Unresolved arguments keep an empty placeholder and
    are reported; the caller must not dispatch.  ``sources=None`` skips the
    source checks (static placeholder validation).
    """
    bindings = dict(requirement.get("readback_bindings") or {})
    args: dict[str, Any] = {}
    unresolved: list[str] = []
    for param, value in readback_args_declared(requirement).items():
        if param not in bindings:
            args[param] = value
            continue
        key = bindings[param]
        resolved = context.get(key)
        if not isinstance(resolved, str) or not resolved:
            unresolved.append(f"{param}<-{key}: no string value")
            resolved = ""
        elif sources is not None and sources.get(key) != "provider":
            unresolved.append(f"{param}<-{key}: source={sources.get(key)}, not provider")
            resolved = ""
        elif sources is not None and (written or {}).get(key) != resolved:
            unresolved.append(f"{param}<-{key}: value differs from the provider-written value")
            resolved = ""
        args[param] = resolved
    op = str((requirement.get("readback") or {}).get("op") or "")
    return {"operation": op, "args": args}, unresolved


def static_block(plan: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Reasons, per fixture_id, why a Readback cannot run.  Empty = all runnable.

    Evaluated before setup so an unexecutable Readback never costs a provider
    side effect (for example a consumed UAT order).  Any declaration issue
    blocks, even when no requirement could be parsed (key ``*``).  Bound
    arguments are checked with an empty-string placeholder; their real values
    exist only after the provider ran.
    """
    from autophone.execution import effects
    from autophone.execution.engine.recipe import RecipeError, validate_recipe

    requirements = list(plan.get("requirements") or [])
    issues = list(plan.get("issues") or [])
    if issues:
        detail = "; ".join(
            f"{issue.get('fixture_id') or '-'}:{issue.get('code')}" for issue in issues
        )
        blocked = {req["id"]: (REASON_CONTRACT_INVALID, detail) for req in requirements}
        return blocked or {CASE_LEVEL: (REASON_CONTRACT_INVALID, detail)}
    if not requirements:
        return {CASE_LEVEL: (REASON_CONTRACT_INVALID, "plan declares no Fixture requirement")}
    blocked = {}
    for req in requirements:
        step, _ = resolve_readback_step(req, {})
        try:
            validate_recipe([step])
        except RecipeError as exc:
            blocked[req["id"]] = (REASON_OPERATION_INVALID, str(exc))
            continue
        decision = effects.step_effect(step)
        if decision.irreversible:
            blocked[req["id"]] = (
                REASON_OPERATION_IRREVERSIBLE,
                f"readback operation {step['operation']} effect={decision.effect} "
                f"reason={decision.reason}",
            )
    return blocked


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def capture_context(
    requirement: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Declared context values and the keys the Readback rebound.

    A value present before the Readback is authoritative.  The Readback may
    supply a key that was empty; it must not replace an existing value.
    """
    context: dict[str, Any] = {}
    rebound: list[str] = []
    for key in requirement.get("context_keys") or []:
        prior = before.get(key)
        later = after.get(key)
        if prior not in (None, ""):
            if later not in (None, "") and later != prior:
                rebound.append(key)
            context[key] = _json_value(prior)
        elif later not in (None, ""):
            context[key] = _json_value(later)
    return context, rebound


def make_record(
    requirement: dict[str, Any],
    *,
    status: str,
    reason: str,
    detail: str,
    context: dict[str, Any] | None = None,
    sources: dict[str, str] | None = None,
    provider_ref: str = "",
    readback_ref: str = "",
    provider_trace: dict[str, Any] | None = None,
    readback_actions: list[str] | None = None,
    readback_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bindings = dict(requirement.get("readback_bindings") or {})
    raw_args = dict(readback_args or {})
    raw_context = dict(context or {})
    secret = secrets.token_bytes(32)  # never recorded; see value_digest
    args = dict(raw_args)
    arg_digests: dict[str, str] = {}
    for param, key in bindings.items():
        if param in raw_args:
            args[param] = _redact(key, raw_args[param])
            arg_digests[param] = value_digest(key, raw_args[param], secret)
    trace = None
    if provider_trace is not None:
        trace = {name: value for name, value in provider_trace.items() if name != "written"}
    return {
        "fixture_id": requirement["id"],
        "kind": requirement.get("kind") or "",
        "requirement_digest": requirement["requirement_digest"],
        "status": status,
        "reason": reason,
        "detail": redact_text(detail),
        "context": {key: _redact(key, value) for key, value in raw_context.items()},
        "context_sha256": {key: value_digest(key, value, secret) for key, value in raw_context.items()},
        "context_sources": dict(sources or {}),
        "provider_ref": provider_ref,
        "readback_ref": readback_ref,
        "readback_args": args,
        "readback_args_sha256": arg_digests,
        "readback_bindings": bindings,
        "provider": {
            "op": _provider_op(requirement) or None,
            "with": dict((requirement.get("provider") or {}).get("with") or {}),
            "trace": trace,
        },
        "readback": requirement.get("readback"),
        "readback_actions": list(readback_actions or []),
    }


def not_executed_records(
    plan: dict[str, Any],
    *,
    reason: str,
    detail: str,
    per_fixture: dict[str, tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Failed records for Fixtures whose Readback did not run."""
    per_fixture = per_fixture or {}
    out = []
    for req in plan.get("requirements") or []:
        own_reason, own_detail = per_fixture.get(req["id"], (reason, detail))
        out.append(make_record(req, status="failed", reason=own_reason, detail=own_detail))
    return out


def provider_trace_for(requirement: dict[str, Any], setup_trace: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Last setup trace entry of the declared provider Operation."""
    op = _provider_op(requirement)
    matches = [entry for entry in setup_trace if entry.get("operation") == op]
    return dict(matches[-1]) if matches else None


def _last_ok_ref(actions: list[dict[str, Any]]) -> str:
    """Reference to the last action, only when that action succeeded."""
    if not actions or not actions[-1].get("ok"):
        return ""
    return action_ref(str(actions[-1].get("action_id") or ""))


def judge(
    case_id: str,
    requirement: dict[str, Any],
    *,
    readback_ok: bool,
    readback_detail: str,
    readback_actions: list[dict[str, Any]],
    readback_args: dict[str, Any],
    setup_trace: list[dict[str, Any]],
    before: dict[str, Any],
    after: dict[str, Any],
    failure_reason: str = REASON_READBACK_FAILED,
) -> dict[str, Any]:
    """Build one record; ``ready`` only when the preparation card would agree.

    ``readback_actions`` are ``{"action_id", "ok"}`` in recording order; the
    Readback and provider references point at their last action and are empty
    unless that action succeeded.
    """
    context, rebound = capture_context(requirement, before, after)
    sources = context_sources(requirement, before, after, setup_trace)
    provider_trace = provider_trace_for(requirement, setup_trace)
    provider_ref = _last_ok_ref(list((provider_trace or {}).get("actions") or []))
    readback_ref = _last_ok_ref(readback_actions)
    common = dict(
        context=context, sources=sources, provider_ref=provider_ref, readback_ref=readback_ref,
        provider_trace=provider_trace,
        readback_actions=[str(action.get("action_id") or "") for action in readback_actions],
        readback_args=readback_args,
    )
    if provider_trace is None or not provider_ref:
        return make_record(requirement, status="failed", reason=REASON_PROVIDER_NOT_EXECUTED,
                           detail="provider operation has no successful recorded setup action",
                           **common)
    if not readback_ok:
        return make_record(requirement, status="failed", reason=failure_reason,
                           detail=readback_detail, **common)
    if rebound:
        return make_record(requirement, status="failed", reason=REASON_IDENTITY_REBOUND,
                           detail="readback changed an existing context value: " + ", ".join(rebound),
                           **common)
    record = make_record(requirement, status="ready", reason="", detail=readback_detail, **common)
    doc = {"schema": FIXTURE_READBACK_EVIDENCE_SCHEMA, "cases": {case_id.upper(): [_entry(record)]}}
    state = fixture_runtime_states([requirement], doc, case_id=case_id)[0]
    if state["state"] != "ready":
        return make_record(requirement, status="failed", reason=str(state.get("reason") or ""),
                           detail=str(state.get("detail") or readback_detail), **common)
    return record


# ── evidence writer (RunExecution side) ──────────────────────────────────────


def _entry(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "fixture_id": record["fixture_id"],
        "requirement_digest": record["requirement_digest"],
        "status": record["status"],
        "context": dict(record.get("context") or {}),
        "context_sha256": dict(record.get("context_sha256") or {}),
        "context_sources": dict(record.get("context_sources") or {}),
        "provider_ref": record.get("provider_ref") or "",
        "readback_ref": record.get("readback_ref") or "",
        "readback_args": dict(record.get("readback_args") or {}),
        "readback_args_sha256": dict(record.get("readback_args_sha256") or {}),
        "readback_bindings": dict(record.get("readback_bindings") or {}),
    }


def build_fixture_readback_evidence(run_id: str, outcomes: Iterable[Any]) -> dict[str, Any] | None:
    """Evidence document for every Case outcome that carries Readback records."""
    cases: dict[str, list[dict[str, Any]]] = {}
    for outcome in outcomes:
        records = getattr(outcome, "fixture_readback", None)
        if records is None:
            continue
        cases[str(outcome.id).upper()] = [_entry(record) for record in records]
    if not cases:
        return None
    return {"schema": FIXTURE_READBACK_EVIDENCE_SCHEMA, "run_id": run_id, "cases": cases}


def write_fixture_readback_evidence(run_dir: Path, run_id: str, outcomes: Iterable[Any]) -> Path | None:
    from autophone.contracts import write_json

    doc = build_fixture_readback_evidence(run_id, outcomes)
    if doc is None:
        return None
    path = write_json(Path(run_dir) / FIXTURE_READBACK_EVIDENCE_NAME, doc)
    logger.info(
        "Fixture Readback 证据已写出：%s",
        ", ".join(
            f"{case}:{entry['fixture_id']}={entry['status']}"
            for case, entries in doc["cases"].items() for entry in entries
        ) or "（无可解析的 Fixture 声明）",
    )
    return path
