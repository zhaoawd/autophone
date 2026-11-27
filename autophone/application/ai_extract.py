"""ai_extract.py — AI-6 抽取 auto-pull 驱动（platform-ai-language-to-execution P3）。

把 ADR-0004 的统一闭环补上**自动供给侧**：用例转换撞 `needs_extraction`（无操作能达成某前置态）→
自动起草那条缺失操作（AI-4，witness 可选）→ 等区分力闸验 teeth + 人审转正 → resume 转换。本驱动是
该环的**创建期编排**，串起 `ai_convert.convert_case`（AI-5）与 `ai_draft.draft_operation`（AI-4）。

**ADR-0004 原把 auto-supply 列为 deferred（do NOT build）；L2E P3（grilling Q5）决定落地闭环 auto-pull，
连同 ADR 一并改写。** 反转 deferral 的安全护栏（与 ADR-0004 复用同一套，不新设信任域）：
  1. 起草的操作恒 `status=draft`，**绝不自动转正**——转正仍走既有冻结门（人审 + N greens + 区分力闸）。
  2. **经验性 witness gating 是活 VM 边界**：本驱动只把操作起成 draft，「实跑 flip 看 witness 翻成预测对偶
     stage」由 negctl 在活 VM 上做；驱动据 `draft_operation` 的 gate 读数如实报「待验」，不假装已证 teeth。
  3. **stall 必须显形**：auto-draft 连续 K 次过不了（瞎编/名对值错/witness flip_arg 非 param 等 fail-closed）
     → 升一条 `stalled` 结果，落到人（此抽取需人写操作/witness），不无限重试、不静默吞。
  4. **resume 不在本驱动闭合**：起草的操作还是 draft、未注册进 dispatcher，convert 看不到它——故「resume
     convert」须等操作转正（活 VM 验 + 人审）后才成立。驱动到 `drafted_pending_gate` 即止，诚实留边界。

回归执行链永不 import 本模块（创建期编排，AI 在环）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from autophone.application.ai import CommandRunner, _subprocess_runner
from autophone.application.ai_convert import _extraction_case_context, _load_case, convert_case
from autophone.application.ai_draft import draft_operation
from autophone.assets.apps import load_app_profile


def _extraction_requirement(token: str, reason: str) -> str:
    """把抽取需求（缺的前置态 token + 转换器给的理由）拼成喂 draft_operation 的需求串。
    起草的是**提供该前置态的 fixture 操作**——多数无 witness（纯造态）；只有「成功本身即用例期望」
    那类（如负路登录）draft_operation 才据系统提示自行声明 witness。"""
    base = f"提供前置态「{token}」：起草一条可复用操作，把设备驱动到该态作为后续用例的前置 fixture。"
    return base + (f" 转换器给的缺料理由：{reason}" if reason else "")


def run_extraction_pull(
    app: str,
    case_id: str,
    *,
    max_draft_attempts: int = 2,
    provider: str | None = None,
    model: str | None = None,
    convert_runner: CommandRunner = _subprocess_runner,
    draft_runner: CommandRunner = _subprocess_runner,
    cases_path: Path | None = None,
    recipes_dir: Path | None = None,
    ops_path: Path | None = None,
) -> dict[str, Any]:
    """跑一轮抽取 auto-pull。返回 `{ok, stage, …}`，stage ∈：

      - `converted`             转换成功（无缺料），recipe 草稿已落 → ok=True
      - `drafted_pending_gate`  撞缺料，已自动起草缺失操作为 draft（待活 VM 验 witness + 人审转正） → ok=True
      - `stalled`               撞缺料，但 auto-draft 连续 max_draft_attempts 次 fail-closed → ok=False（升 stall 给人）
      - `convert_error`         转换 fail-closed（非缺料，如瞎编 verb）→ ok=False

    `ok` = 本轮是否健康推进（converted / drafted_pending_gate 都算推进；stall/error 不算）。
    """
    case_id = case_id.upper()
    conv = convert_case(app, case_id, provider=provider, model=model,
                        runner=convert_runner, cases_path=cases_path, recipes_dir=recipes_dir,
                        ops_path=ops_path, require_active_operations=True)
    if conv.get("ok"):
        return {"ok": True, "stage": "converted", "case_id": case_id,
                "recipe": conv["recipe"], "path": conv["path"]}
    if "needs_extraction" not in conv:
        return {"ok": False, "stage": "convert_error", "case_id": case_id,
                "error": conv.get("error", "convert failed")}

    token = conv["needs_extraction"]
    reason = conv.get("reason", "")
    requirement = _extraction_requirement(token, reason)
    try:
        source_case = _load_case(cases_path or load_app_profile(app).cases_path(), case_id)
    except (OSError, yaml.YAMLError, AttributeError, TypeError) as exc:
        return {"ok": False, "stage": "convert_error", "case_id": case_id,
                "error": f"source case unreadable before extraction: {exc}"}
    context = conv.get("case_context")
    if source_case is None or not context or _extraction_case_context(source_case) != context:
        return {"ok": False, "stage": "convert_error", "case_id": case_id,
                "error": "source case changed before extraction"}
    source_text = json.dumps(context, ensure_ascii=False)

    errors: list[str] = []
    for _ in range(max(1, max_draft_attempts)):
        draft = draft_operation(app, requirement, from_ref=f"extraction:case:{case_id}",
                                source_text=source_text,
                                note=f"auto-pull for needs_extraction={token!r}",
                                provider=provider, model=model,
                                runner=draft_runner, ops_path=ops_path)
        if draft.get("ok"):
            return {"ok": True, "stage": "drafted_pending_gate", "case_id": case_id,
                    "needs_extraction": token, "operation": draft["operation"],
                    "gate": draft["gate"],
                    "note": ("操作已起草为 draft；待 negctl 在活 VM 验 witness teeth + 人审转正后，"
                             "操作注册进 dispatcher，方可 resume 转换该用例")}
        errors.append(draft.get("error", "draft failed"))

    return {"ok": False, "stage": "stalled", "case_id": case_id,
            "needs_extraction": token, "attempts": max(1, max_draft_attempts), "errors": errors,
            "note": ("auto-draft 连续 fail-closed，升 stall：此抽取需人写操作/witness（"
                     "或修被测 build）——不无限重试、不静默吞")}
