"""Verdict 评估与 impact-result 组装（影响图谱 §9.3 / §11.3 / §12）。

六种结论，每一种都是有效结论：

    api_and_ui_changed        接口字段变了，页面对应区域也变了
    api_changed_ui_unchanged  接口变了但页面没动 —— 这是**有效结果**，不是失败
    api_unchanged_ui_changed  页面变了但目标字段没动 —— 大概率归因错了或另有来源
    cached_or_not_refreshed   目标接口这次压根没发/没变，页面也没动
    not_reached               没到达目标页面状态（必须带 blocker）
    inconclusive              证据不足以下结论

两条硬规则：

- **视觉差异不单独定结论。** 语义（OCR/AX）和 API oracle 给业务结论，visual diff 首期只
  作 advisory；只有视觉在动而语义未知时，结论是 inconclusive 而不是「变了」。
- **没到达要写清为什么。** 每个目标要么有截图证据，要么有明确 blocker，报告完整率算的
  就是这一条。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from autophone.contracts import validate_or_raise
from autophone.util import now_iso

API_AND_UI_CHANGED = "api_and_ui_changed"
API_CHANGED_UI_UNCHANGED = "api_changed_ui_unchanged"
API_UNCHANGED_UI_CHANGED = "api_unchanged_ui_changed"
CACHED_OR_NOT_REFRESHED = "cached_or_not_refreshed"
NOT_REACHED = "not_reached"
INCONCLUSIVE = "inconclusive"

VERDICTS = (
    API_AND_UI_CHANGED,
    API_CHANGED_UI_UNCHANGED,
    API_UNCHANGED_UI_CHANGED,
    CACHED_OR_NOT_REFRESHED,
    NOT_REACHED,
    INCONCLUSIVE,
)


def _tri(value: Any) -> bool | None:
    return None if value is None else bool(value)


def evaluate_verdict(
    *,
    reached: bool,
    api_observed: bool,
    api_changed: bool | None,
    semantic_changed: bool | None,
    visual_changed: bool | None = None,
) -> str:
    """单个目标的 verdict 判定。参数含义见模块 docstring。"""
    if not reached:
        return NOT_REACHED
    api_changed = _tri(api_changed)
    semantic_changed = _tri(semantic_changed)
    if not api_observed:
        # 目标接口这次没发：页面多半吃了缓存或没触发刷新。此时若语义明确变了，
        # 说明变化另有来源，仍要报 api_unchanged_ui_changed 让人去查。
        if semantic_changed:
            return API_UNCHANGED_UI_CHANGED
        if semantic_changed is None:
            return INCONCLUSIVE
        return CACHED_OR_NOT_REFRESHED
    if api_changed is None or semantic_changed is None:
        return INCONCLUSIVE
    if api_changed and semantic_changed:
        return API_AND_UI_CHANGED
    if api_changed and not semantic_changed:
        return API_CHANGED_UI_UNCHANGED
    if not api_changed and semantic_changed:
        return API_UNCHANGED_UI_CHANGED
    return CACHED_OR_NOT_REFRESHED


def _target_result(
    plan_target: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    reached = bool(evidence.get("reached"))
    api_observed = bool(evidence.get("api_observed"))
    api_changed = _tri(evidence.get("api_changed"))
    semantic_changed = _tri(evidence.get("semantic_changed"))
    visual_changed = _tri(evidence.get("visual_changed"))
    verdict = evaluate_verdict(
        reached=reached,
        api_observed=api_observed,
        api_changed=api_changed,
        semantic_changed=semantic_changed,
        visual_changed=visual_changed,
    )
    if reached and any((evidence.get(field) or {}).get("status") == "unavailable"
                       for field in ("api_comparison", "semantic_comparison")):
        verdict = INCONCLUSIVE
    if (reached and plan_target.get("variant_selector")
            and (evidence.get("variant_comparison") or {}).get("status") != "compared"):
        verdict = INCONCLUSIVE
    row: dict[str, Any] = {
        "target_id": str(plan_target.get("target_id")),
        "screen_id": str(plan_target.get("screen_id") or "unknown"),
        "verdict": verdict,
    }
    if plan_target.get("binding_id"):
        row["binding_id"] = str(plan_target["binding_id"])
    if plan_target.get("variant_selector"):
        row["variant_selector"] = dict(plan_target["variant_selector"])
    if plan_target.get("regions"):
        row["regions"] = list(plan_target["regions"])
    if plan_target.get("confidence"):
        row["confidence"] = str(plan_target["confidence"])

    api: dict[str, Any] = {"observed": api_observed}
    if plan_target.get("endpoint_key"):
        api["endpoint_key"] = str(plan_target["endpoint_key"])
    api["changed"] = api_changed
    if "changed_paths" in evidence:
        api["changed_paths"] = [str(p) for p in evidence["changed_paths"]]
    if evidence.get("status") is not None:
        api["status"] = int(evidence["status"])
    if evidence.get("api_note"):
        api["note"] = str(evidence["api_note"])
    row["api"] = api

    ui: dict[str, Any] = {"reached": reached}
    ui["semantic_changed"] = semantic_changed
    ui["ax_changed"] = evidence.get("ax_changed")
    if "ax_changes" in evidence:
        ui["ax_changes"] = list(evidence["ax_changes"])
    ui["visual_changed"] = visual_changed
    if evidence.get("visual_score") is not None:
        ui["visual_score"] = float(evidence["visual_score"])
    # 视觉层首期恒 advisory：它可以指出「这块像素动了」，但不负责下业务结论
    ui["visual_advisory"] = True
    if evidence.get("changed_regions"):
        ui["changed_regions"] = [str(r) for r in evidence["changed_regions"]]
    if evidence.get("masked_regions"):
        ui["masked_regions"] = [str(r) for r in evidence["masked_regions"]]
    if evidence.get("ui_note"):
        ui["note"] = str(evidence["ui_note"])
    row["ui"] = ui
    for field in ("comparison_sources", "api_comparison", "semantic_comparison", "variant_comparison", "controlled_validation"):
        if field in evidence:
            row[field] = dict(evidence[field])

    if evidence.get("source_kind"):
        row["source_kind"] = str(evidence["source_kind"])
    frames = dict(evidence.get("frames") or {})
    if frames:
        row["frames"] = {
            "baseline": frames.get("baseline"),
            "current": frames.get("current"),
            "diff": frames.get("diff"),
        }
    if evidence.get("evidence_runs"):
        row["evidence_runs"] = [str(r) for r in evidence["evidence_runs"]]

    blocker = evidence.get("blocker")
    if isinstance(blocker, Mapping) and blocker.get("kind"):
        row["blocker"] = {"kind": str(blocker["kind"])}
        if blocker.get("detail"):
            row["blocker"]["detail"] = str(blocker["detail"])
    elif verdict == NOT_REACHED:
        # 报告完整率红线：没到达却说不出原因，本身就是缺陷
        reason = str(plan_target.get("blocked_reason") or "unknown")
        row["blocker"] = {
            "kind": reason,
            "detail": str((plan_target.get("route") or {}).get("reason") or "未提供 blocker 说明"),
        }
    return row


def build_result(
    *,
    run_id: str,
    plan: Mapping[str, Any],
    evidence: Mapping[str, Mapping[str, Any]] | None = None,
    extra_gaps: Iterable[Mapping[str, Any]] = (),
    executed_effects: Sequence[str] = (),
) -> dict[str, Any]:
    """plan + 每目标证据 → impact-result.v1。

    plan 里已经 blocked 的目标不需要证据，直接落 ``not_reached`` + blocked_reason；
    这保证「计划了几个目标，报告里就有几个目标」，不会静默丢。
    """
    evidence = dict(evidence or {})
    rows: list[dict[str, Any]] = []
    for plan_target in plan.get("targets") or []:
        target_id = str(plan_target.get("target_id"))
        rows.append(_target_result(plan_target, evidence.get(target_id, {})))

    by_verdict: dict[str, int] = {}
    for row in rows:
        by_verdict[row["verdict"]] = by_verdict.get(row["verdict"], 0) + 1
    gaps = [
        {"kind": str(g.get("kind") or "gap"), "detail": str(g.get("detail") or "")}
        for g in list(plan.get("gaps") or []) + list(extra_gaps)
    ]

    admission = dict(plan.get("admission") or {})
    allowed = {str(e) for e in admission.get("allowed_effects") or []}
    violations = sorted({str(e) for e in executed_effects if str(e) not in allowed})

    doc = {
        "schema_version": "v1",
        "run_id": str(run_id),
        "app": str(plan.get("app")),
        "build": str(plan.get("build")),
        "change_id": str(plan.get("change_id")),
        "env": str(plan.get("env") or ""),
        "generated_at": now_iso(),
        "plan_ref": "impact/plan.json",
        "targets": rows,
        "summary": {
            "total": len(rows),
            "reached": sum(1 for row in rows if row.get("ui", {}).get("reached")),
            "by_verdict": by_verdict,
            "gaps": len(gaps),
        },
        "safety": {
            "denied_effects": list(admission.get("denied_effects") or []),
            "violations": len(violations),
            **({"note": f"未获准却已执行的 effect：{violations}"} if violations else {}),
        },
        "gaps": gaps,
    }
    if "api_scenario" in plan:
        doc["api_scenario"] = dict(plan["api_scenario"])
    validate_or_raise("impact-result.v1.json", doc)
    return doc
