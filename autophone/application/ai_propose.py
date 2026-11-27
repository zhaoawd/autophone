"""ai_propose.py — AI-7 候选用例提案（platform-ai-language-to-execution P4 候选入口）。

把**自由需求 / 聊天摘要 / 需求文档**喂给本地 agent，提一份**结构化候选用例清单**（item + 前置 +
步骤 + 预期），供人 curate——人决定「哪些意图值得成 case」（产品/意图判断，AI 不独占，§三第一审点）。

与 P1（`ai_convert`：把已 curate 的 NL 用例转成 recipe）衔接：本模块在它之前，把模糊需求收敛成候选
清单。两条铁律：
1. **提案 read-only**：`propose_cases` 不写任何东西，只返回候选；落库须显式 `commit_candidates`
   （CLI `--commit`），且**恒 `status=draft`**——人在 draft 里挑活的 = curate。AI 绝不自行入 active 库。
2. **fail-closed**：agent 没回结构化候选 / 候选缺 item → ok=False，不静默返回空、不写脏。

provenance 用现有 enum `source: conversation`（AI 来源进 note），与 ai_draft 同纪律。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from autophone.application.ai import AiRequest, CommandRunner, _subprocess_runner, invoke
from autophone.application.ai_output_schema import drop_optional_nulls, strict_transport_schema
from autophone.assets.apps import load_app_profile
from autophone.assets.cases import append_case

# 候选用例内容 schema——只要业务字段；id/status/revision/automation 等由 commit 装配。
# item 必填（用例名称，与 POST /cases 一致）；其余可选。additionalProperties:false 防漂移。
_CANDIDATE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["item"],
    "properties": {
        "item": {"type": "string", "minLength": 1, "description": "用例名称（一句话目标）"},
        "module": {"type": "string", "description": "归属模块"},
        "precondition": {"type": "array", "items": {"type": "string"}},
        "steps": {"type": "array", "items": {"type": "string"}},
        "expects": {"type": "array", "items": {"type": "string"}},
        "level": {"enum": ["高", "中", "低"], "description": "优先级"},
        "case_type": {"type": "string"},
        "test_point": {"type": "string"},
        "rationale": {"type": "string", "description": "为何从需求里提出这条（供人 curate 时判断，不入库）"},
    },
}
_PROPOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["candidates"],
    "properties": {"candidates": {"type": "array", "minItems": 1, "items": _CANDIDATE}},
}
# 传输 schema：可选字段改为必填可空，满足 provider 结构化输出约束；回包删去 null 后仍按内容 schema 校验。
_PROPOSE_TRANSPORT_SCHEMA: dict[str, Any] = strict_transport_schema(_PROPOSE_SCHEMA)

_SYSTEM = (
    "你是 autophone 的「需求→候选用例」提案 agent。读给定的自由需求/聊天摘要/需求文档，提一份"
    "**具体、可测**的候选回归用例清单：每条给 item（一句话目标）、precondition（前置）、steps（步骤）、"
    "expects（可观测预期）、可选 level/module/case_type/test_point，并给一句 rationale 说明从需求哪里提出。\n"
    "原则：\n"
    "  - **只提需求里有依据的**，不要臆造功能；模糊处宁可少提、在 rationale 里标注存疑。\n"
    "  - 每条是**一个可独立验证的意图**，预期必须可观测（能被断言），别写「正常」「成功」这类不可测词。\n"
    "  - 这些是**提案**，交给人 curate（人决定哪些值得成 case）——不要假设它们会直接入库。"
)


def _build_prompt(source_text: str, max_candidates: int) -> str:
    return (
        f"以下是需求/对话/文档，请据此提**至多 {max_candidates} 条**候选用例（宁缺毋滥）：\n\n"
        f"{source_text}"
    )


def propose_cases(
    app: str,
    source_text: str,
    *,
    max_candidates: int = 10,
    provider: str | None = None,
    model: str | None = None,
    timeout_s: float = 300.0,
    runner: CommandRunner = _subprocess_runner,
) -> dict[str, Any]:
    """从自由需求提一份候选用例清单。**read-only**——只返回 `{ok, candidates}`，不写库。

    fail-closed：agent 没回结构化候选 / 不贴 schema（如缺 item）→ ok=False。
    """
    load_app_profile(app)  # 校验 app 存在（fail-loud）
    if not (source_text or "").strip():
        return {"ok": False, "error": "source_text 为空——没有需求可提案"}

    req = AiRequest(
        prompt=_build_prompt(source_text, max_candidates),
        system=_SYSTEM,
        tools="none",
        json_schema=_PROPOSE_TRANSPORT_SCHEMA,
        provider=provider,
        model=model,
        timeout_s=timeout_s,
    )
    res = invoke(req, runner=runner)
    if not res.ok or not isinstance(res.json, dict):
        return {"ok": False, "error": res.error or "propose agent returned no structured candidates"}

    from jsonschema import Draft202012Validator
    payload = drop_optional_nulls(_PROPOSE_SCHEMA, res.json)
    errs = sorted(Draft202012Validator(_PROPOSE_SCHEMA).iter_errors(payload), key=lambda e: list(e.path))
    if errs:
        detail = "；".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errs[:5])
        return {"ok": False, "error": f"候选不贴合 schema（拒绝返回脏提案）：{detail}"}

    return {"ok": True, "candidates": payload["candidates"],
            "provider": res.provider, "model": res.model}


def _candidate_to_fields(c: dict[str, Any], *, prov_note: str) -> dict[str, Any]:
    """把一条候选装配成 v2 case 业务 dict（镜像 api._build_case_fields）。automation 留空——AI 提案
    **未经自动化可行性复核**，分类交人/转换器（不冒认 ui_ready）；provenance 标 AI 来源。"""
    return {
        "module": str(c.get("module") or "").strip(),
        "item": str(c["item"]).strip(),
        "order_type": None,
        "level": str(c.get("level") or "中").strip(),
        "automation": "",
        "automation_reason": "AI 提案，自动化可行性待复核",
        "desc": "",
        "precondition": [str(x) for x in (c.get("precondition") or [])],
        "steps": [str(x) for x in (c.get("steps") or [])],
        "expects": [str(x) for x in (c.get("expects") or [])],
        "suitable": {"api": "", "ui": ""},
        "case_type": str(c.get("case_type") or "").strip(),
        "test_point": str(c.get("test_point") or "").strip(),
        "owner": "",
        "remark": "",
        "raw": {"external_id": ""},
        "provenance": {"source": "conversation", "note": prov_note},
    }


def commit_candidates(
    app: str,
    candidates: list[dict[str, Any]],
    *,
    cases_path: Path | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """把（人挑过的）候选清单**落库为 draft 用例**，返回 `{ok, created:[ids]}`。curate = 人事后在
    draft 里挑活的（activate）；本函数只负责把提案变成可审的 draft，绝不写 active。每条 item 必填，
    缺则 fail-closed 整批不写（避免半批脏数据）。"""
    profile = load_app_profile(app)
    path = cases_path or profile.cases_path()
    bad = [i for i, c in enumerate(candidates) if not str((c or {}).get("item") or "").strip()]
    if bad:
        return {"ok": False, "error": f"候选 #{bad} 缺 item，整批拒绝落库（fail-closed）"}

    prov_note = "AI propose (P4 候选入口)" + (f"; {note}" if note else "")
    created: list[str] = []
    for c in candidates:
        case = append_case(path, _candidate_to_fields(c, prov_note=prov_note), status="draft")
        created.append(case["id"])
    return {"ok": True, "created": created, "count": len(created)}
