"""Post-action instance-level Entity Readback (Stage 2).

Runs after a Case recipe body finishes (after ``body_end`` API mark and before
teardown/cleanup) for ANY case declaring ``entity_binding.readback`` — not
restricted to Draft Trials or Golden Path Runs.

Core invariants:
- Resolves ``readback.with`` arguments from literals, ``{from_entity}``,
  ``{from_context}``, and ``{from_fixture}`` (fail closed on any unresolved ref).
- Enforces that ``readback.op`` is a registered, non-irreversible (read-only)
  Operation before dispatching.
- Emits an isolated ``ctx.api_mark("entity_readback", ...)`` so readback HTTP
  calls never bleed into the Case's interaction API window.
- Evaluates ``readback.expect_context`` against captured readback context +
  resolved bound entity (supporting scalar equality, ``eq``/``ne``/``contains``/
  ``exists`` predicates, reference dicts, and ``${entity.id}`` /
  ``${entity.<attr>}`` / ``${<var>}`` substitution).
- Redacts sensitive fields, computes SHA-256 / HMAC digests, and writes
  ``entity-readback.json`` into the Run artifacts directory.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Callable

from autophone.execution.fixture_readback import (
    _redact,
    action_ref,
    redact_text,
    value_digest,
)

logger = logging.getLogger(__name__)

ENTITY_READBACK_EVIDENCE_NAME = "entity-readback.json"
ENTITY_READBACK_EVIDENCE_SCHEMA = "autophone.entity-readback-evidence/1"

REASON_BINDING_UNRESOLVED = "entity_readback_binding_unresolved"
REASON_OPERATION_INVALID = "entity_readback_operation_invalid"
REASON_OPERATION_IRREVERSIBLE = "entity_readback_operation_irreversible"
REASON_OPERATION_FAILED = "entity_readback_operation_failed"
REASON_INFRA_ERROR = "entity_readback_infra_error"
REASON_EXPECT_MISMATCH = "entity_readback_expect_mismatch"
REASON_UNRESOLVED_VAR = "entity_readback_unresolved_var"
REASON_NOT_EXECUTED = "entity_readback_not_executed"

_VAR_RE = re.compile(r"\$\{([A-Za-z0-9_.-]+)\}")
_PREDICATE_OPS = frozenset({"eq", "ne", "contains", "exists", "empty", "regex"})


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _last_ok_ref(actions: list[dict[str, Any]]) -> str:
    if not actions or not actions[-1].get("ok"):
        return ""
    return action_ref(str(actions[-1].get("action_id") or ""))


def _extract_entity_field(resolved_entity: Any | None, field: str) -> tuple[bool, Any]:
    if resolved_entity is None:
        return False, None
    if isinstance(resolved_entity, Mapping):
        if field == "id":
            val = resolved_entity.get("entity_id")
            return (val not in (None, "")), val
        if field == "type":
            val = resolved_entity.get("entity_type")
            return (val not in (None, "")), val
        attrs = resolved_entity.get("attributes")
        if isinstance(attrs, Mapping) and field in attrs and attrs[field] is not None:
            return True, attrs[field]
        return False, None
    if field == "id":
        val = getattr(resolved_entity, "entity_id", None)
        return (val not in (None, "")), val
    if field == "type":
        val = getattr(resolved_entity, "entity_type", None)
        return (val not in (None, "")), val
    attrs = getattr(resolved_entity, "attributes", None)
    if isinstance(attrs, Mapping) and field in attrs and attrs[field] is not None:
        return True, attrs[field]
    return False, None


def _template_vars(
    resolved_entity: Any | None,
    fixture_vars: Mapping[str, Any] | None,
    runtime_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if fixture_vars:
        out.update(dict(fixture_vars))
    if runtime_context:
        for key, value in runtime_context.items():
            if value is not None:
                out.setdefault(str(key), value)
    if resolved_entity is not None:
        if isinstance(resolved_entity, Mapping):
            tvars = resolved_entity.get("template_vars")
            if isinstance(tvars, Mapping):
                out.update(dict(tvars))
            etype = resolved_entity.get("entity_type")
            eid = resolved_entity.get("entity_id")
            attrs = resolved_entity.get("attributes")
        else:
            tvars = getattr(resolved_entity, "template_vars", None)
            if isinstance(tvars, Mapping):
                out.update(dict(tvars))
            etype = getattr(resolved_entity, "entity_type", None)
            eid = getattr(resolved_entity, "entity_id", None)
            attrs = getattr(resolved_entity, "attributes", None)
        if etype not in (None, ""):
            out["entity.type"] = etype
        if eid not in (None, ""):
            out["entity.id"] = eid
        if isinstance(attrs, Mapping):
            for key, value in attrs.items():
                if value is not None:
                    out[f"entity.{key}"] = value
    return out


def _subst_string(value: str, tvars: Mapping[str, Any]) -> tuple[Any, list[str]]:
    missing: list[str] = []
    full = _VAR_RE.fullmatch(value)
    if full is not None:
        var_name = full.group(1)
        if var_name in tvars and tvars[var_name] is not None:
            return tvars[var_name], []
        return value, [var_name]

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        if var_name in tvars and tvars[var_name] is not None:
            return str(tvars[var_name])
        missing.append(var_name)
        return match.group(0)

    rendered = _VAR_RE.sub(_replace, value)
    return rendered, missing


def _resolve_ref_value(
    raw: Any,
    *,
    resolved_entity: Any | None,
    fixture_vars: Mapping[str, Any],
    runtime_context: Mapping[str, Any],
    tvars: Mapping[str, Any],
    label: str,
) -> tuple[Any, str | None, list[str]]:
    """Resolve literal, ``{from_entity}``, ``{from_context}``, ``{from_fixture}``, or ``${...}``."""
    if isinstance(raw, Mapping):
        if "from_entity" in raw and len(raw) == 1:
            field = str(raw["from_entity"] or "").strip()
            ok, val = _extract_entity_field(resolved_entity, field)
            binding = f"entity:{field}"
            if not ok:
                return None, binding, [f"{label}<-{binding}"]
            return val, binding, []
        if "from_context" in raw and len(raw) == 1:
            key = str(raw["from_context"] or "").strip()
            val = runtime_context.get(key)
            binding = f"context:{key}"
            if val in (None, ""):
                return None, binding, [f"{label}<-{binding}"]
            return val, binding, []
        if "from_fixture" in raw and len(raw) == 1:
            key = str(raw["from_fixture"] or "").strip()
            val = fixture_vars.get(key)
            binding = f"fixture:{key}"
            if val in (None, ""):
                return None, binding, [f"{label}<-{binding}"]
            return val, binding, []
    if isinstance(raw, str):
        rendered, missing = _subst_string(raw, tvars)
        if missing:
            return rendered, None, [f"{label}<-${{{name}}}" for name in missing]
        return rendered, None, []
    return raw, None, []


def resolve_readback_args(
    readback_spec: Mapping[str, Any],
    *,
    resolved_entity: Any | None,
    fixture_vars: Mapping[str, Any] | None = None,
    runtime_context: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """Resolve ``readback.with`` parameters and return ``(args, bindings, unresolved)``."""
    fx = dict(fixture_vars or {})
    rctx = dict(runtime_context or {})
    tvars = _template_vars(resolved_entity, fx, rctx)
    raw_with = readback_spec.get("with") or {}
    args: dict[str, Any] = {}
    bindings: dict[str, str] = {}
    unresolved: list[str] = []
    if isinstance(raw_with, Mapping):
        for param, raw_val in raw_with.items():
            name = str(param)
            val, binding, missing = _resolve_ref_value(
                raw_val,
                resolved_entity=resolved_entity,
                fixture_vars=fx,
                runtime_context=rctx,
                tvars=tvars,
                label=name,
            )
            if binding is not None:
                bindings[name] = binding
            if missing:
                unresolved.extend(missing)
            else:
                args[name] = val
    return args, bindings, unresolved


def _lookup_context(context: Mapping[str, Any], key: str) -> tuple[bool, Any]:
    if key in context:
        return True, context[key]
    if "." in key:
        cur: Any = context
        for part in key.split("."):
            if isinstance(cur, Mapping) and part in cur:
                cur = cur[part]
            else:
                return False, None
        return True, cur
    return False, None


def _scalar_eq(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return float(actual) == float(expected)
    return actual == expected


def _parse_expect_spec(
    raw: Any,
    *,
    resolved_entity: Any | None,
    fixture_vars: Mapping[str, Any],
    runtime_context: Mapping[str, Any],
    tvars: Mapping[str, Any],
    key: str,
) -> tuple[str, Any, list[str]]:
    """Normalize an ``expect_context`` entry to ``(op, expected, unresolved)``."""
    if isinstance(raw, Mapping):
        # Reference dict -> scalar equality against resolved ref
        if set(raw.keys()) <= {"from_entity", "from_context", "from_fixture"} and len(raw) == 1:
            val, _binding, missing = _resolve_ref_value(
                raw,
                resolved_entity=resolved_entity,
                fixture_vars=fixture_vars,
                runtime_context=runtime_context,
                tvars=tvars,
                label=key,
            )
            return "eq", val, missing
        # Explicit {op: ..., value: ...} predicate
        if "op" in raw and str(raw.get("op")) in _PREDICATE_OPS:
            op = str(raw["op"])
            raw_val = raw.get("value", True if op == "exists" else None)
            val, _binding, missing = _resolve_ref_value(
                raw_val,
                resolved_entity=resolved_entity,
                fixture_vars=fixture_vars,
                runtime_context=runtime_context,
                tvars=tvars,
                label=key,
            )
            return op, val, missing
        # Shorthand {eq: v} / {ne: v} / {contains: v} / {exists: bool}
        if len(raw) == 1:
            only_key = next(iter(raw))
            if str(only_key) in _PREDICATE_OPS:
                op = str(only_key)
                val, _binding, missing = _resolve_ref_value(
                    raw[only_key],
                    resolved_entity=resolved_entity,
                    fixture_vars=fixture_vars,
                    runtime_context=runtime_context,
                    tvars=tvars,
                    label=key,
                )
                return op, val, missing
    val, _binding, missing = _resolve_ref_value(
        raw,
        resolved_entity=resolved_entity,
        fixture_vars=fixture_vars,
        runtime_context=runtime_context,
        tvars=tvars,
        label=key,
    )
    return "eq", val, missing


def evaluate_expect_context(
    expect_context: Mapping[str, Any] | None,
    observed_context: Mapping[str, Any],
    *,
    resolved_entity: Any | None = None,
    fixture_vars: Mapping[str, Any] | None = None,
    runtime_context: Mapping[str, Any] | None = None,
) -> tuple[bool, list[dict[str, Any]], list[str]]:
    """Evaluate ``readback.expect_context`` against captured readback context."""
    fx = dict(fixture_vars or {})
    rctx = dict(runtime_context or {})
    tvars = _template_vars(resolved_entity, fx, rctx)
    checks: list[dict[str, Any]] = []
    unresolved_all: list[str] = []
    for raw_key, raw_spec in (expect_context or {}).items():
        key = str(raw_key)
        op, expected, unresolved = _parse_expect_spec(
            raw_spec,
            resolved_entity=resolved_entity,
            fixture_vars=fx,
            runtime_context=rctx,
            tvars=tvars,
            key=key,
        )
        present, actual = _lookup_context(observed_context, key)
        if unresolved:
            unresolved_all.extend(unresolved)
            checks.append({
                "key": key,
                "op": op,
                "ok": False,
                "reason": "unresolved_var",
                "want": _redact(key, expected),
                "got": _redact(key, actual) if present else "<missing>",
                "unresolved": list(unresolved),
            })
            continue
        if op == "exists":
            want_exists = bool(expected)
            is_present = present and actual not in (None, "")
            ok = is_present if want_exists else (not is_present)
        elif op == "empty":
            want_empty = True if expected is None else bool(expected)
            is_empty = (not present) or actual in (None, "", [], {})
            ok = is_empty if want_empty else (not is_empty)
        elif op == "eq":
            ok = present and _scalar_eq(actual, expected)
        elif op == "ne":
            ok = present and not _scalar_eq(actual, expected)
        elif op == "contains":
            if not present or actual is None or expected is None:
                ok = False
            elif isinstance(actual, (list, tuple, set)):
                ok = any(_scalar_eq(item, expected) for item in actual)
            else:
                ok = str(expected) in str(actual)
        elif op == "regex":
            if not present or actual is None or not isinstance(expected, str):
                ok = False
            else:
                try:
                    ok = re.search(expected, str(actual)) is not None
                except re.error:
                    ok = False
        else:
            ok = False
        checks.append({
            "key": key,
            "op": op,
            "ok": ok,
            "want": _redact(key, expected),
            "got": _redact(key, actual) if present else "<missing>",
        })
    all_ok = all(item["ok"] for item in checks) and not unresolved_all
    return all_ok, checks, unresolved_all


def _json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _json_safe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    return str(value)


def make_entity_readback_record(
    *,
    case_id: str,
    op: str,
    entity_type: str,
    entity_id: Any,
    status: str,
    reason: str = "",
    detail: str = "",
    readback_args: Mapping[str, Any] | None = None,
    readback_bindings: Mapping[str, str] | None = None,
    observed_context: Mapping[str, Any] | None = None,
    checks: list[dict[str, Any]] | None = None,
    readback_ref: str = "",
    readback_actions: list[str] | None = None,
    wrote_keys: list[str] | None = None,
) -> dict[str, Any]:
    secret = secrets.token_bytes(32)
    raw_args = {str(k): _json_safe_value(v) for k, v in (readback_args or {}).items()}
    raw_ctx = {str(k): _json_safe_value(v) for k, v in (observed_context or {}).items()}
    redacted_args = {k: _redact(k, v) for k, v in raw_args.items()}
    args_sha = {k: value_digest(k, v, secret) for k, v in raw_args.items()}
    redacted_ctx = {k: _redact(k, v) for k, v in raw_ctx.items()}
    ctx_sha = {k: value_digest(k, v, secret) for k, v in raw_ctx.items()}
    redacted_entity_id = (
        _redact("entity_id", str(entity_id))
        if entity_id not in (None, "")
        else None
    )
    record: dict[str, Any] = {
        "case_id": str(case_id).upper(),
        "entity_type": str(entity_type or ""),
        "entity_id": redacted_entity_id,
        "op": str(op or ""),
        "status": status,
        "ok": True if status == "pass" else (None if status == "not_executed" else False),
        "reason": str(reason or ""),
        "detail": redact_text(detail),
        "readback_ref": str(readback_ref or ""),
        "readback_actions": list(readback_actions or []),
        "readback_args": redacted_args,
        "readback_args_sha256": args_sha,
        "readback_bindings": dict(readback_bindings or {}),
        "observed_context": redacted_ctx,
        "observed_context_sha256": ctx_sha,
        "wrote_keys": list(wrote_keys or []),
        "checks": list(checks or []),
    }
    record["record_sha256"] = _canonical_sha256(record)
    return record


def not_executed_record(
    case: Mapping[str, Any],
    *,
    resolved_entity: Any | None = None,
    reason: str = REASON_NOT_EXECUTED,
    detail: str = "entity readback did not execute",
) -> dict[str, Any]:
    binding = case.get("entity_binding") if isinstance(case.get("entity_binding"), Mapping) else {}
    readback = binding.get("readback") if isinstance(binding.get("readback"), Mapping) else {}
    _, eid = _extract_entity_field(resolved_entity, "id")
    _, etype = _extract_entity_field(resolved_entity, "type")
    return make_entity_readback_record(
        case_id=str(case.get("id") or ""),
        op=str(readback.get("op") or ""),
        entity_type=str(etype or binding.get("entity_type") or ""),
        entity_id=eid,
        status="not_executed",
        reason=reason,
        detail=detail,
    )


def run_entity_readback(
    ctx: Any,
    case: Mapping[str, Any],
    *,
    resolved_entity: Any | None = None,
    fixture_vars: Mapping[str, Any] | None = None,
    dispatch: Callable[[Any, dict[str, Any]], Any] | None = None,
    expected_errors: tuple[type[BaseException], ...] | None = None,
) -> dict[str, Any]:
    """Execute post-body instance-level readback for a Case declaring ``entity_binding.readback``."""
    from autophone.execution import effects
    from autophone.execution.contracts import CaseBlocked
    from autophone.execution.engine.actions import StepFailure
    from autophone.execution.engine.recipe import (
        INFRA_ERRORS,
        RecipeError,
        dispatch_step,
        validate_recipe,
    )

    dispatch_fn = dispatch or dispatch_step
    caught_errors = expected_errors or (StepFailure, CaseBlocked, RecipeError, *INFRA_ERRORS)

    case_id = str(case.get("id") or "")
    binding = case.get("entity_binding") if isinstance(case.get("entity_binding"), Mapping) else {}
    readback_spec = binding.get("readback") if isinstance(binding.get("readback"), Mapping) else {}
    op_name = str(readback_spec.get("op") or "").strip()
    _, eid = _extract_entity_field(resolved_entity, "id")
    _, etype = _extract_entity_field(resolved_entity, "type")
    entity_type = str(etype or binding.get("entity_type") or "")

    pre_fixture = dict(getattr(ctx, "fixture_order", None) or {})
    pre_api_ctx = dict(getattr(ctx, "api_assertion_context", None) or {})
    pre_rb_ctx = dict(getattr(ctx, "entity_readback_context", None) or {})
    runtime_ctx = {**pre_fixture, **pre_api_ctx, **pre_rb_ctx}

    if not op_name:
        return make_entity_readback_record(
            case_id=case_id,
            op="",
            entity_type=entity_type,
            entity_id=eid,
            status="fail",
            reason=REASON_OPERATION_INVALID,
            detail="entity_binding.readback.op is required",
        )

    args, bindings, unresolved = resolve_readback_args(
        readback_spec,
        resolved_entity=resolved_entity,
        fixture_vars=fixture_vars,
        runtime_context=runtime_ctx,
    )
    if unresolved:
        return make_entity_readback_record(
            case_id=case_id,
            op=op_name,
            entity_type=entity_type,
            entity_id=eid,
            status="fail",
            reason=REASON_BINDING_UNRESOLVED,
            detail="unresolved entity readback argument(s): " + ", ".join(unresolved),
            readback_args=args,
            readback_bindings=bindings,
        )

    step: dict[str, Any] = {"operation": op_name}
    if args:
        step["args"] = dict(args)

    # Static fail-closed enforcement: operation must be registered and non-irreversible.
    try:
        validate_recipe([step])
    except RecipeError as exc:
        return make_entity_readback_record(
            case_id=case_id,
            op=op_name,
            entity_type=entity_type,
            entity_id=eid,
            status="fail",
            reason=REASON_OPERATION_INVALID,
            detail=f"invalid readback operation {op_name!r}: {exc}",
            readback_args=args,
            readback_bindings=bindings,
        )

    decision = effects.step_effect(step)
    if decision.irreversible:
        return make_entity_readback_record(
            case_id=case_id,
            op=op_name,
            entity_type=entity_type,
            entity_id=eid,
            status="fail",
            reason=REASON_OPERATION_IRREVERSIBLE,
            detail=(
                f"readback operation {op_name!r} is irreversible "
                f"(effect={decision.effect}, reason={decision.reason}); refused"
            ),
            readback_args=args,
            readback_bindings=bindings,
        )

    # Isolate readback HTTP calls from the case's interaction API window, including any
    # nested dispatch_step ("operation") or symbol-bound steps executed inside readback.
    prior_housekeeping = getattr(ctx, "_housekeeping_window_tag", None)
    setattr(ctx, "_housekeeping_window_tag", "entity_readback")
    if hasattr(ctx, "_next_api_mark"):
        setattr(ctx, "_next_api_mark", None)
    api_mark = getattr(ctx, "api_mark", None)
    if callable(api_mark):
        try:
            api_mark("entity_readback", operation=op_name, window_tag="entity_readback")
        except TypeError:
            api_mark("entity_readback", operation=op_name)

    first = len(getattr(ctx, "results", []) or [])
    prior_phase = getattr(ctx, "evidence_phase", None)
    setattr(ctx, "evidence_phase", "entity_readback")
    op_return: Any = None
    op_error = ""
    is_infra = False
    try:
        op_return = dispatch_fn(ctx, step)
    except caught_errors as exc:
        is_infra = isinstance(exc, INFRA_ERRORS)
        res = getattr(exc, "result", None)
        op_error = (
            getattr(res, "detail", None)
            or getattr(exc, "detail", None)
            or f"{type(exc).__name__}: {exc}"
        )
    finally:
        setattr(ctx, "evidence_phase", prior_phase)
        setattr(ctx, "_housekeeping_window_tag", prior_housekeeping)

    results = getattr(ctx, "results", []) or []
    rb_actions = [
        {"action_id": getattr(r, "action_id", ""), "ok": bool(getattr(r, "ok", False))}
        for r in results[first:]
        if getattr(r, "action_id", "")
    ]
    action_ids = [item["action_id"] for item in rb_actions]
    readback_ref = _last_ok_ref(rb_actions)

    post_fixture = dict(getattr(ctx, "fixture_order", None) or {})
    post_api_ctx = dict(getattr(ctx, "api_assertion_context", None) or {})
    post_rb_ctx = dict(getattr(ctx, "entity_readback_context", None) or {})
    returned_ctx = dict(op_return) if isinstance(op_return, Mapping) else {}
    observed_context: dict[str, Any] = {
        **post_fixture,
        **post_api_ctx,
        **post_rb_ctx,
        **returned_ctx,
    }
    wrote_keys = sorted({
        *(k for k, v in post_fixture.items() if pre_fixture.get(k) != v),
        *(k for k, v in post_api_ctx.items() if pre_api_ctx.get(k) != v),
        *(k for k, v in post_rb_ctx.items() if pre_rb_ctx.get(k) != v),
        *(str(k) for k in returned_ctx),
    })

    if op_error:
        return make_entity_readback_record(
            case_id=case_id,
            op=op_name,
            entity_type=entity_type,
            entity_id=eid,
            status="fail",
            reason=REASON_INFRA_ERROR if is_infra else REASON_OPERATION_FAILED,
            detail=op_error,
            readback_args=args,
            readback_bindings=bindings,
            observed_context=observed_context,
            readback_ref=readback_ref,
            readback_actions=action_ids,
            wrote_keys=wrote_keys,
        )

    expect_context = readback_spec.get("expect_context")
    if isinstance(expect_context, Mapping) and expect_context:
        all_ok, checks, unresolved_vars = evaluate_expect_context(
            expect_context,
            observed_context,
            resolved_entity=resolved_entity,
            fixture_vars=fixture_vars,
            runtime_context=runtime_ctx,
        )
        if not all_ok:
            if unresolved_vars:
                reason = REASON_UNRESOLVED_VAR
                detail = "unresolved expect_context variable(s): " + ", ".join(unresolved_vars)
            else:
                reason = REASON_EXPECT_MISMATCH
                failed = [c for c in checks if not c.get("ok")]
                detail = "; ".join(
                    f"{c['key']} ({c['op']} want={c['want']!r} got={c['got']!r})"
                    for c in failed
                ) or "entity readback expect_context mismatch"
            return make_entity_readback_record(
                case_id=case_id,
                op=op_name,
                entity_type=entity_type,
                entity_id=eid,
                status="fail",
                reason=reason,
                detail=detail,
                readback_args=args,
                readback_bindings=bindings,
                observed_context=observed_context,
                checks=checks,
                readback_ref=readback_ref,
                readback_actions=action_ids,
                wrote_keys=wrote_keys,
            )
    else:
        checks = []

    return make_entity_readback_record(
        case_id=case_id,
        op=op_name,
        entity_type=entity_type,
        entity_id=eid,
        status="pass",
        reason="",
        detail=f"entity readback {op_name} passed",
        readback_args=args,
        readback_bindings=bindings,
        observed_context=observed_context,
        checks=checks,
        readback_ref=readback_ref,
        readback_actions=action_ids,
        wrote_keys=wrote_keys,
    )


def build_entity_readback_evidence(
    run_id: str,
    outcomes: Iterable[Any],
) -> dict[str, Any] | None:
    """Build ``entity-readback.json`` payload for all outcomes with entity readback records."""
    cases: dict[str, dict[str, Any]] = {}
    for outcome in outcomes:
        record = getattr(outcome, "entity_readback", None)
        if not isinstance(record, Mapping):
            continue
        cases[str(getattr(outcome, "id", "") or "").upper()] = dict(record)
    if not cases:
        return None
    return {
        "schema": ENTITY_READBACK_EVIDENCE_SCHEMA,
        "run_id": str(run_id),
        "cases": cases,
    }


def write_entity_readback_evidence(
    run_dir: Path,
    run_id: str,
    outcomes: Iterable[Any],
) -> Path | None:
    """Persist ``entity-readback.json`` into ``run_dir`` when any case ran entity readback."""
    from autophone.contracts import write_json

    doc = build_entity_readback_evidence(run_id, outcomes)
    if doc is None:
        return None
    path = write_json(Path(run_dir) / ENTITY_READBACK_EVIDENCE_NAME, doc)
    logger.info(
        "Entity Readback 证据已写出：%s",
        ", ".join(
            f"{cid}:{entry.get('op')}={entry.get('status')}"
            for cid, entry in doc["cases"].items()
        ),
    )
    return path


__all__ = [
    "ENTITY_READBACK_EVIDENCE_NAME",
    "ENTITY_READBACK_EVIDENCE_SCHEMA",
    "REASON_BINDING_UNRESOLVED",
    "REASON_EXPECT_MISMATCH",
    "REASON_INFRA_ERROR",
    "REASON_NOT_EXECUTED",
    "REASON_OPERATION_FAILED",
    "REASON_OPERATION_INVALID",
    "REASON_OPERATION_IRREVERSIBLE",
    "REASON_UNRESOLVED_VAR",
    "build_entity_readback_evidence",
    "evaluate_expect_context",
    "make_entity_readback_record",
    "not_executed_record",
    "resolve_readback_args",
    "run_entity_readback",
    "write_entity_readback_evidence",
]
