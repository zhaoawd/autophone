"""Impact plan execution orchestration over the existing operation runner.

This module does not drive a device.  It consumes an ``impact-plan.v1`` and
calls an injected operation runner for the already-planned active operation
names.  The production adapter is ``RunService.submit(type=operation)``, so
ScreenLock, device preparation and operation evidence keep their existing
single implementation.

The executor re-checks operation lifecycle and Effect Admission immediately
before every route.  A plan is therefore not an authorization token that stays
valid after its assets change.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from autophone.contracts import validate_or_raise
from autophone.observation.impact import admission as admission_mod
from autophone.observation.impact import context as context_mod
from autophone.util import now_iso


OperationRunner = Callable[[str, str], Mapping[str, Any] | Any]
EXECUTABLE_TARGETS = frozenset({"execute", "advisory"})
ROUTE_RESET_OPERATIONS = frozenset({"reset_to_home", "relaunch_home"})


class ImpactExecutionError(ValueError):
    """The persisted plan cannot be executed safely or deterministically."""


def _blocker(kind: str, detail: str) -> dict[str, str]:
    return {"kind": str(kind), "detail": str(detail)}


def _route_key(target: Mapping[str, Any]) -> str:
    route = dict(target.get("route") or {})
    operations = [str(step.get("operation") or "") for step in route.get("steps") or []]
    return json.dumps(
        {
            "operations": operations,
            "required_context": dict(target.get("required_context") or {}),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _runner_payload(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    summary = getattr(value, "summary", None)
    if callable(summary):
        payload = summary()
        if isinstance(payload, Mapping):
            return dict(payload)
    raise ImpactExecutionError(
        f"operation runner 返回值不受支持：{type(value).__name__}"
    )


def _runtime_route_blocker(
    route_steps: Sequence[Mapping[str, Any]],
    *,
    operations: Mapping[str, Mapping[str, Any]],
    allowed_effects: Sequence[str],
    consumer_context: Mapping[str, Any] | None,
    executor_effects: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    """Revalidate lifecycle, declaration and sensitive CTA immediately before execution."""
    for index, step in enumerate(route_steps):
        name = str(step.get("operation") or "")
        operation = operations.get(name)
        if operation is None or str(operation.get("status") or "") != "active":
            return _blocker(
                "operation_not_active",
                f"计划引用的 operation={name or '<empty>'} 当前不是 Active，拒绝执行",
            )
        if index == 0:
            navigation = dict(operation.get("navigation") or {})
            raw_steps = operation.get("steps") or []
            first_step = raw_steps[0] if raw_steps and isinstance(raw_steps[0], Mapping) else {}
            reset_operation = str(first_step.get("operation") or "")
            if not navigation.get("reset") or reset_operation not in ROUTE_RESET_OPERATIONS:
                return _blocker(
                    "route_start_not_resettable",
                    f"route 首个 operation={name} 未同时声明 navigation.reset，"
                    "并从受支持的复位动作开始",
                )
        navigation = dict(operation.get("navigation") or {})
        planned_revision = step.get("revision")
        current_revision = operation.get("revision")
        if not isinstance(planned_revision, int) or planned_revision != current_revision:
            return _blocker(
                "plan_asset_drift",
                f"operation={name} revision 已变化或 plan 未钉版本："
                f"plan={planned_revision!r} current={current_revision!r}",
            )
        planned_from = str(step.get("from") or "")
        planned_to = str(step.get("to") or "")
        current_from = str(navigation.get("from") or "")
        current_to = str(navigation.get("to") or "")
        if (planned_from, planned_to) != (current_from, current_to):
            return _blocker(
                "plan_asset_drift",
                f"operation={name} navigation 已变化："
                f"plan=({planned_from or '*'}->{planned_to or '*'}) "
                f"current=({current_from or '*'}->{current_to or '*'})",
            )
        checkpoint_screens = {
            str(value)
            for value in dict(operation.get("screen_expectations") or {}).values()
            if str(value)
        }
        if current_to and current_to not in checkpoint_screens:
            return _blocker(
                "route_checkpoint_missing",
                f"operation={name} 没有声明 navigation.to={current_to} 的 Screen checkpoint",
            )
        context_ok, missing_context = context_mod.context_satisfies(
            consumer_context,
            navigation.get("requires") or {},
        )
        if not context_ok:
            return _blocker(
                "consumer_context_drift",
                f"operation={name} 当前上下文不满足 navigation.requires：{missing_context}",
            )
        safety, effects, findings = admission_mod.operation_safety(
            dict(operation),
            operations=operations,
            executor_effects=executor_effects,
        )
        planned_effects = sorted({str(item) for item in (step.get("effects") or [])})
        if sorted(set(effects)) != planned_effects:
            return _blocker(
                "plan_asset_drift",
                f"operation={name} 的 effect 已变化：plan={planned_effects} current={sorted(set(effects))}",
            )
        if findings:
            marker = str(findings[0].get("marker") or "")
            blocker_kind = admission_mod.finding_blocker(findings)
            detail = (
                f"operation={name} 的 executor 引用 {marker!r} 缺少可审计 effect 声明，拒绝执行"
                if blocker_kind == "effect_declaration_missing"
                else f"operation={name} 命中敏感 CTA {marker!r}，拒绝执行"
            )
            return _blocker(
                blocker_kind,
                detail,
            )
        planned_safety = str(step.get("safety") or admission_mod.READ_ONLY)
        if safety != planned_safety:
            return _blocker(
                "plan_asset_drift",
                f"operation={name} 的 safety 已变化：plan={planned_safety} current={safety}",
            )
        if safety == admission_mod.MUTATING and not effects:
            return _blocker(
                "undeclared_mutating_operation",
                f"operation={name} 声明为 mutating 但没有可审计 effect，拒绝执行",
            )
        decision = admission_mod.decide(allowed_effects, effects)
        if not decision.ok:
            return _blocker("effect_not_admitted_runtime", decision.note)
    return None


def execute(
    *,
    run_id: str,
    plan: Mapping[str, Any],
    operations: Mapping[str, Mapping[str, Any]],
    operation_runner: OperationRunner,
    consumer_context: Mapping[str, Any] | None = None,
    executor_effects: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute exact route groups and return ``impact-execution.v1``.

    Targets with identical operation paths and required context share one route
    execution. Candidate and blocked targets never reach the runner. An empty
    path is not treated as reachability proof; a target still needs an explicit
    operation/checkpoint to enter the final report as reached.
    """
    started_at = now_iso()
    allowed_effects = [
        str(item) for item in dict(plan.get("admission") or {}).get("allowed_effects") or []
    ]
    targets = [dict(item) for item in plan.get("targets") or []]
    target_rows: dict[str, dict[str, Any]] = {}
    route_groups: dict[str, list[dict[str, Any]]] = {}

    for target in targets:
        target_id = str(target.get("target_id") or "")
        execution = str(target.get("execution") or "blocked")
        base = {
            "target_id": target_id,
            "execution": execution,
            "status": "skipped",
            "operations": [],
            "evidence_runs": [],
        }
        if execution == "candidate":
            base["blocker"] = _blocker(
                "not_scheduled",
                "Observed Binding 只列候选，不进入自动影响执行",
            )
            target_rows[target_id] = base
            continue
        if execution == "blocked":
            base["status"] = "blocked"
            base["blocker"] = _blocker(
                str(target.get("blocked_reason") or "plan_blocked"),
                str(dict(target.get("route") or {}).get("reason") or "目标已在计划阶段阻断"),
            )
            target_rows[target_id] = base
            continue
        if execution not in EXECUTABLE_TARGETS:
            raise ImpactExecutionError(
                f"target={target_id} execution={execution!r} 不在闭集内"
            )
        base["status"] = "blocked"
        target_rows[target_id] = base
        route_groups.setdefault(_route_key(target), []).append(target)

    operation_runs: list[dict[str, Any]] = []
    executed_effects: list[str] = []
    ordinal = 0
    for members in route_groups.values():
        route = dict(members[0].get("route") or {})
        route_steps = [dict(item) for item in route.get("steps") or []]
        operation_names = [str(item.get("operation") or "") for item in route_steps]
        member_ids = [str(item.get("target_id") or "") for item in members]
        if route.get("status") != "ready":
            blocker = _blocker("route_unavailable", str(route.get("reason") or "路径未就绪"))
        elif not route_steps:
            blocker = _blocker(
                "execution_proof_missing",
                "目标位于起始 Screen，但计划没有 Active Operation/checkpoint 可证明本次已到达",
            )
        else:
            blocker = _runtime_route_blocker(
                route_steps,
                operations=operations,
                allowed_effects=allowed_effects,
                consumer_context=consumer_context,
                executor_effects=executor_effects,
            )

        route_run_ids: list[str] = []
        if blocker is None:
            for step, operation_name in zip(route_steps, operation_names):
                ordinal += 1
                child_run_id = f"{run_id}-op-{ordinal:03d}"
                try:
                    payload = _runner_payload(operation_runner(operation_name, child_run_id))
                    status = str(payload.get("status") or "error")
                    actual_run_id = str(payload.get("run_id") or child_run_id)
                    detail = str(payload.get("detail") or "")
                except Exception as exc:  # noqa: BLE001 operation boundary becomes target blocker
                    status = "error"
                    actual_run_id = child_run_id
                    detail = f"{type(exc).__name__}: {exc}"
                route_run_ids.append(actual_run_id)
                row: dict[str, Any] = {
                    "operation": operation_name,
                    "run_id": actual_run_id,
                    "status": status,
                    "target_ids": member_ids,
                }
                if detail:
                    row["detail"] = detail
                operation_runs.append(row)
                for effect in step.get("effects") or []:
                    name = str(effect)
                    if name and name not in executed_effects:
                        executed_effects.append(name)
                if status != "passed":
                    blocker = _blocker(
                        "operation_failed",
                        f"operation={operation_name} run={actual_run_id} status={status}"
                        + (f" detail={detail}" if detail else ""),
                    )
                    break

        for target in members:
            target_id = str(target.get("target_id") or "")
            row = target_rows[target_id]
            row["operations"] = operation_names
            row["evidence_runs"] = route_run_ids
            if blocker is None:
                row["status"] = "passed"
            else:
                row["status"] = "blocked"
                row["blocker"] = blocker

    ordered_rows = [target_rows[str(target.get("target_id") or "")] for target in targets]
    scheduled = sum(row["execution"] in EXECUTABLE_TARGETS for row in ordered_rows)
    reached = sum(row["status"] == "passed" for row in ordered_rows)
    blocked = sum(row["status"] == "blocked" for row in ordered_rows)
    skipped = sum(row["status"] == "skipped" for row in ordered_rows)
    if scheduled == 0:
        status = "no_executable_targets"
    elif reached == scheduled:
        status = "completed"
    elif reached:
        status = "partial"
    else:
        status = "blocked"

    doc = {
        "schema_version": "v1",
        "run_id": str(run_id),
        "app": str(plan.get("app") or ""),
        "build": str(plan.get("build") or ""),
        "status": status,
        "started_at": started_at,
        "ended_at": now_iso(),
        "targets": ordered_rows,
        "operation_runs": operation_runs,
        "executed_effects": executed_effects,
        "summary": {
            "total": len(ordered_rows),
            "scheduled": scheduled,
            "reached": reached,
            "blocked": blocked,
            "skipped": skipped,
            "operation_runs": len(operation_runs),
        },
    }
    validate_or_raise("impact-execution.v1.json", doc)
    return doc


def evidence(execution_doc: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Project execution reachability into ``impact-result`` evidence inputs."""
    out: dict[str, dict[str, Any]] = {}
    for row in execution_doc.get("targets") or []:
        target_id = str(row.get("target_id") or "")
        reached = row.get("status") == "passed"
        item: dict[str, Any] = {
            "reached": reached,
            "api_observed": False,
            "api_changed": None,
            "semantic_changed": None,
            "visual_changed": None,
            "evidence_runs": [str(value) for value in row.get("evidence_runs") or []],
        }
        if reached:
            item["ui_note"] = (
                "Active Operation 路径执行通过；本轮未提供 API/语义差异证据，"
                "结论保持 inconclusive"
            )
        else:
            item["blocker"] = dict(row.get("blocker") or _blocker(
                "execution_blocked", "执行未形成到达证据"
            ))
        out[target_id] = item
    return out
