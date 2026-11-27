"""Stable, user-facing progress contract for case intake and automation.

The nine stages describe *business progress*, not worker-thread lifecycle.  AI
jobs may still be ``queued/running/succeeded`` while the journey is, for
example, waiting for a tester to clarify an app-specific precondition.

Keep this module deliberately small: producers emit stage events, while the Web
UI and future clients can render the same catalog without knowing converter
internals.
"""
from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "case-intake-progress.v1"

STAGES: tuple[dict[str, Any], ...] = (
    {
        "key": "import",
        "title": "导入用例",
        "explanation": "读取用例原文、来源编号和版本，保留可追溯的原始输入。",
        "why": "先固定输入，后续 AI 整理或人工修改才有可比对的基线。",
    },
    {
        "key": "identity",
        "title": "确认 App 与 Build",
        "explanation": "核对 App、环境、Bundle ID 与目标构建，避免知识和执行能力串用。",
        "why": "同品牌不同环境、壳包或版本可能有完全不同的页面与接口行为。",
    },
    {
        "key": "intent",
        "title": "理解用例意图",
        "explanation": "把前置条件、操作步骤和预期结果整理成可检查的测试语义。",
        "why": "平台需要先理解“要证明什么”，此阶段不会驱动设备或改变业务数据。",
    },
    {
        "key": "knowledge",
        "title": "匹配已有知识",
        "explanation": "按 App 与 Build 查找已确认的操作、页面、接口、数据夹具和历史证据。",
        "why": "优先复用经过验证的知识，减少重复探索和不可靠的临时点击。",
    },
    {
        "key": "design",
        "title": "设计执行链路",
        "explanation": "组合准备、主体操作、断言和清理，形成候选执行方案。",
        "why": "自然语言用例只有转成闭环执行链路，才可能稳定自动运行。",
    },
    {
        "key": "readiness",
        "title": "检查自动化条件",
        "explanation": "检查前置数据、操作能力、断言依据、副作用和证据是否齐全。",
        "why": "条件不满足时应明确暂停并给出补齐方式，不能让 AI 猜测后继续。",
    },
    {
        "key": "draft",
        "title": "生成执行草稿",
        "explanation": "生成通过静态校验的 typed-IR recipe 草稿，但不自动进入正式回归。",
        "why": "草稿可复核、可修改，避免未经确认的 AI 产物直接影响回归结果。",
    },
    {
        "key": "trial",
        "title": "试运行与验证",
        "explanation": "在目标 App、Build 和受控数据上执行，收集 UI、接口与日志证据。",
        "why": "一次可生成不等于稳定可运行，必须用真实执行证明链路和断言。",
    },
    {
        "key": "review",
        "title": "人审与转正",
        "explanation": "由测试人员确认语义、风险和证据，满足门槛后转入正式回归。",
        "why": "最终责任边界必须清楚；AI 提建议，平台留证据，人决定是否转正。",
    },
)

STAGE_BY_KEY = {stage["key"]: {**stage, "index": index} for index, stage in enumerate(STAGES, 1)}
STATUSES = frozenset(
    {"pending", "queued", "running", "completed", "attention", "waiting_user", "blocked", "failed"}
)


def new_journey(app: str, case_id: str) -> dict[str, Any]:
    """Return the serializable catalog attached to an accepted AI job."""
    return {
        "schema_version": SCHEMA_VERSION,
        "app": str(app),
        "case_id": str(case_id).upper(),
        "stage_total": len(STAGES),
        "current_stage": STAGES[0]["key"],
        "stages": [
            {
                "key": stage["key"],
                "index": index,
                "title": stage["title"],
                "explanation": stage["explanation"],
                "why": stage["why"],
                "status": "pending",
            }
            for index, stage in enumerate(STAGES, 1)
        ],
    }


def stage_event(
    stage: str,
    status: str,
    summary: str,
    *,
    explanation: str | None = None,
    why: str | None = None,
    evidence_refs: list[str] | tuple[str, ...] | None = None,
    next_actions: list[str] | tuple[str, ...] | None = None,
    blockers: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Build one validated progress event shared by API jobs and the Web UI."""
    if stage not in STAGE_BY_KEY:
        raise ValueError(f"unknown case-intake stage: {stage}")
    if status not in STATUSES:
        raise ValueError(f"unknown case-intake status: {status}")
    meta = STAGE_BY_KEY[stage]
    return {
        "schema_version": SCHEMA_VERSION,
        "type": "journey_stage",
        "stage": stage,
        "stage_index": meta["index"],
        "stage_total": len(STAGES),
        "title": meta["title"],
        "status": status,
        "summary": str(summary),
        "explanation": str(explanation or meta["explanation"]),
        "why": str(why or meta["why"]),
        "evidence_refs": [str(value) for value in (evidence_refs or [])],
        "next_actions": [str(value) for value in (next_actions or [])],
        "blockers": [str(value) for value in (blockers or [])],
    }
