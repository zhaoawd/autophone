"""Cleanup Readback：Body 与 teardown 之后的安全清理与权威读回（BL-083 2026-10-12 第八轮）。

§7.1 波次 A 要求有副作用的 Case 具备资源身份、权威读回与安全清理。Fixture 声明（契约 /2）的
可选字段 ``cleanup`` 给出清理 Operation 与独立的只读 Readback Operation：

* 清理 Operation 在 Body 与 Case teardown 之后执行（与 teardown 同在 ``finally``，Body 失败也执行）；
* Readback 参数 ``{from_context: k}`` 取清理之后的 Fixture Context，``{from_baseline: k}`` 取 Fixture
  Readback ready 时的基线值（setup 中 provider 写入、经独立 Readback 证明的值）；
* ``changed_keys`` 中的键在清理后必须不同于基线（例如 ``app_pid``：证明清理冷启动了新进程）。

只对带 Fixture Readback 计划的 Case 执行（显式草稿试跑与带黄金路径标记的 Run）。基线未就绪
（setup 或 Fixture Readback 未通过）时不执行清理，记录 ``not_executed``。证据写入 Run 目录
``cleanup-readback.json``，值按 governance 规则脱敏，原值只以摘要记录。
"""
from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Any, Callable, Iterable

from autophone.assets.fixture_requirements import (
    CLEANUP_READBACK_EVIDENCE_NAME,
    CLEANUP_READBACK_EVIDENCE_SCHEMA,
    cleanup_ref,
)
from autophone.execution.fixture_readback import _redact, action_ref, redact_text, value_digest

logger = logging.getLogger(__name__)

REASON_BASELINE_NOT_READY = "fixture_baseline_not_ready"
REASON_CLEANUP_FAILED = "cleanup_operation_failed"
REASON_BINDING_UNRESOLVED = "cleanup_readback_binding_unresolved"
REASON_KEY_UNCHANGED = "cleanup_identity_unchanged"
REASON_READBACK_FAILED = "cleanup_readback_failed"


def declared_cleanups(plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [req for req in (plan or {}).get("requirements") or [] if isinstance(req.get("cleanup"), dict)]


def _last_ok_ref(actions: list[dict[str, Any]]) -> str:
    if not actions or not actions[-1].get("ok"):
        return ""
    return action_ref(str(actions[-1].get("action_id") or ""))


def _record(requirement: dict[str, Any], *, status: str, reason: str = "", detail: str = "",
            **extra: Any) -> dict[str, Any]:
    cleanup = requirement["cleanup"]
    return {
        "fixture_id": str(requirement.get("id") or ""),
        "cleanup_digest": requirement.get("cleanup_digest"),
        "status": status,
        "reason": reason,
        "detail": redact_text(detail),
        "cleanup_op": str(cleanup.get("op") or ""),
        "readback_op": str((cleanup.get("readback") or {}).get("op") or ""),
        "cleanup_ref": "",
        "readback_ref": "",
        "readback_args": {},
        "readback_args_sha256": {},
        "readback_bindings": {},
        "changed_keys": {},
        **extra,
    }


def not_executed_records(plan: dict[str, Any] | None, detail: str) -> list[dict[str, Any]]:
    return [_record(req, status="not_executed", reason=REASON_BASELINE_NOT_READY, detail=detail)
            for req in declared_cleanups(plan)]


def run_cleanup_readbacks(
    ctx: Any,
    plan: dict[str, Any],
    baseline: dict[str, Any],
    ready_fixtures: set[str],
    dispatch: Callable[[Any, dict[str, Any]], Any],
    expected_errors: tuple[type[BaseException], ...],
) -> list[dict[str, Any]]:
    """Dispatch each declared cleanup and its Readback; one record per declared cleanup.

    ``expected_errors`` are the failures a step may raise (StepFailure, CaseBlocked, RecipeError
    and infrastructure errors); they become a ``failed`` record, never an exception.
    """
    records: list[dict[str, Any]] = []
    for requirement in declared_cleanups(plan):
        fixture_id = str(requirement.get("id") or "")
        cleanup = requirement["cleanup"]
        if fixture_id not in ready_fixtures:
            records.append(_record(requirement, status="not_executed", reason=REASON_BASELINE_NOT_READY,
                                   detail="Fixture Readback did not reach ready; no baseline to restore"))
            continue
        secret = secrets.token_bytes(32)
        first = len(ctx.results)
        step = {"operation": str(cleanup["op"]), "args": dict(cleanup.get("with") or {})}
        # 执行器据此给证据文件加阶段前缀，不覆盖 setup 阶段的同名证据。
        prior_phase = getattr(ctx, "evidence_phase", None)
        ctx.evidence_phase = "cleanup"
        try:
            dispatch(ctx, step)
            cleanup_error = ""
        except expected_errors as exc:
            result = getattr(exc, "result", None)
            cleanup_error = getattr(result, "detail", None) or getattr(exc, "detail", None) or \
                f"{type(exc).__name__}: {exc}"
        finally:
            ctx.evidence_phase = prior_phase
        cleanup_actions = [{"action_id": r.action_id, "ok": bool(r.ok)}
                           for r in ctx.results[first:] if getattr(r, "action_id", "")]
        cleanup_ref_value = _last_ok_ref(cleanup_actions)
        if cleanup_error or not cleanup_ref_value:
            records.append(_record(requirement, status="failed", reason=REASON_CLEANUP_FAILED,
                                   detail=cleanup_error or "cleanup operation recorded no successful action",
                                   cleanup_ref=cleanup_ref_value))
            continue
        current = dict(ctx.fixture_order or {})
        changed: dict[str, Any] = {}
        unchanged: list[str] = []
        for key in cleanup.get("changed_keys") or []:
            before, after = baseline.get(key), current.get(key)
            is_changed = after not in (None, "") and after != before
            changed[key] = {"baseline_sha256": value_digest(key, before, secret) if before is not None else None,
                            "after_sha256": value_digest(key, after, secret) if after is not None else None,
                            "changed": is_changed}
            if not is_changed:
                unchanged.append(key)
        args: dict[str, Any] = {}
        bindings: dict[str, str] = {}
        unresolved: list[str] = []
        readback = cleanup.get("readback") or {}
        for param, value in (readback.get("with") or {}).items():
            ref = cleanup_ref(value)
            if ref is None:
                args[param] = value
                continue
            source, key = ref
            resolved = (baseline if source == "baseline" else current).get(key)
            bindings[param] = f"{source}:{key}"
            if not isinstance(resolved, str) or not resolved:
                unresolved.append(f"{param}<-{source}:{key}")
                resolved = ""
            args[param] = resolved
        display_args = {param: _redact(param, value) for param, value in args.items()}
        args_sha = {param: value_digest(param, value, secret) for param, value in args.items()}
        common = dict(cleanup_ref=cleanup_ref_value, readback_args=display_args,
                      readback_args_sha256=args_sha, readback_bindings=bindings, changed_keys=changed)
        if unchanged:
            records.append(_record(requirement, status="failed", reason=REASON_KEY_UNCHANGED,
                                   detail="identity unchanged after cleanup: " + ", ".join(unchanged),
                                   **common))
            continue
        if unresolved:
            records.append(_record(requirement, status="failed", reason=REASON_BINDING_UNRESOLVED,
                                   detail="unresolved binding: " + ", ".join(unresolved), **common))
            continue
        first = len(ctx.results)
        try:
            dispatch(ctx, {"operation": str(readback.get("op")), "args": args})
            readback_error = ""
        except expected_errors as exc:
            result = getattr(exc, "result", None)
            readback_error = getattr(result, "detail", None) or getattr(exc, "detail", None) or \
                f"{type(exc).__name__}: {exc}"
        readback_actions = [{"action_id": r.action_id, "ok": bool(r.ok)}
                            for r in ctx.results[first:] if getattr(r, "action_id", "")]
        readback_ref_value = _last_ok_ref(readback_actions)
        if readback_error or not readback_ref_value:
            records.append(_record(requirement, status="failed", reason=REASON_READBACK_FAILED,
                                   detail=readback_error or "readback recorded no successful action",
                                   readback_ref=readback_ref_value, **{k: v for k, v in common.items()}))
            continue
        records.append(_record(requirement, status="verified",
                               detail=f"cleanup {step['operation']} then readback {readback.get('op')} passed",
                               readback_ref=readback_ref_value, **common))
    return records


def summary(records: list[dict[str, Any]]) -> str:
    return ", ".join(f"{r['fixture_id']}={r['status']}" + (f"({r['reason']})" if r.get("reason") else "")
                     for r in records)


def build_cleanup_readback_evidence(run_id: str, outcomes: Iterable[Any]) -> dict[str, Any] | None:
    cases: dict[str, list[dict[str, Any]]] = {}
    for outcome in outcomes:
        records = getattr(outcome, "cleanup_readback", None)
        if records:
            cases[str(outcome.id).upper()] = [dict(record) for record in records]
    if not cases:
        return None
    return {"schema": CLEANUP_READBACK_EVIDENCE_SCHEMA, "run_id": run_id, "cases": cases}


def write_cleanup_readback_evidence(run_dir: Path, run_id: str, outcomes: Iterable[Any]) -> Path | None:
    from autophone.contracts import write_json

    doc = build_cleanup_readback_evidence(run_id, outcomes)
    if doc is None:
        return None
    path = write_json(Path(run_dir) / CLEANUP_READBACK_EVIDENCE_NAME, doc)
    logger.info("Cleanup Readback 证据已写出：%s",
                ", ".join(f"{case}:{summary(entries)}" for case, entries in doc["cases"].items()))
    return path


__all__ = [
    "build_cleanup_readback_evidence",
    "declared_cleanups",
    "not_executed_records",
    "run_cleanup_readbacks",
    "write_cleanup_readback_evidence",
]

