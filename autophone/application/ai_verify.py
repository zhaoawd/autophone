"""ai_verify.py — AI-2 验证单 agent 执行器（platform-ai-capability §3.2）。

把本地 agent 包成满足 `AUTOPHONE_VERIFICATION_AGENT_CMD` 协议的命令：读
`$AUTOPHONE_VERIFICATION_INPUT`（verification-agent-input.json：requirement + checkpoints +
precondition）→ 经 vphone_mcp 逐 checkpoint 驱动真机、绑证据、判 pass/fail → 写
`$AUTOPHONE_VERIFICATION_OUTPUT`（贴合 verification-executor-output.v1）。

run 装配 / 合并逻辑不改（run_verification_executor 已会调命令并按 id 合并回 checkpoints）。
但「真驱动碰屏」必须经 ScreenLock execute phase 串行——锁在 service._submit_verification 那侧加
（见审查点 3），本命令只负责「调 agent 产出回填」。真机驱动需活 VM + vphone_mcp。

BL-067 活机入口 (a)：`AUTOPHONE_VERIFICATION_AGENT_CMD=builtin:verify-agent` 时，验证单执行器不起
子进程，在 Run 线程内调用 `run_builtin_verification`。该调用与 Run 共用同一 StageGuard 上下文，
`tools=device` 走受管私有代理 socket，控制面已配置时不触发 `device_authority_required`。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from autophone.application import ai as _ai
from autophone.application.ai import (
    AI_BUDGET_LIMIT_UNSUPPORTED,
    AiError,
    AiRequest,
    AiResult,
    CommandRunner,
    _subprocess_runner,
    enforce_call_budget,
    invoke,
    provider_capabilities,
    require_device_actions,
)
from autophone.application.ai_output_schema import drop_optional_nulls, strict_transport_schema
from autophone.contracts import validate_or_raise, write_json

# 喂给模型的「内容 schema」——只要它产 status + checkpoints；schema_version 由我们盖后贴 strict 契约。
_MODEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["checkpoints"],
    "properties": {
        "status": {"enum": ["passed", "failed", "error", "pending_agent"]},
        "detail": {"type": "string"},
        "checkpoints": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "status"],
                "properties": {
                    "id": {"type": "string"},
                    "status": {"enum": ["pending", "passed", "failed", "error", "skipped"]},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "detail": {"type": "string"},
                },
            },
        },
    },
}
# 传输 schema：可选字段改为必填可空，满足 provider 结构化输出约束；回包删去 null 后再贴契约校验。
_MODEL_TRANSPORT_SCHEMA: dict[str, Any] = strict_transport_schema(_MODEL_SCHEMA)

_SYSTEM = (
    "你是 autophone 的验证单执行 agent。你有 vphone_mcp 设备工具（screenshot/tap/swipe/open_app 等），"
    "逐个 checkpoint 在真机上验证；先 screenshot 看当前屏，只在需求允许时操作，再 screenshot 确认。"
    "若需求明确只读，禁止 tap/swipe/open_app 或任何业务状态变更。"
    "每个 checkpoint 给 status(passed/failed/skipped) 和 detail；证据截图由宿主包装层统一保存，evidence 可留空。"
    "看不到预期就判 failed，不要臆造通过。最后按所有 checkpoint 给总 status。"
)

EvidenceCapture = Callable[..., dict[str, Any]]


def capture_checkpoint_evidence(
    result: dict[str, Any],
    *,
    run_dir: Path,
    evidence_dir: Path,
) -> dict[str, Any]:
    """Capture one host-owned final frame for every checkpoint.

    The wrapper, not the model, owns evidence files.  This keeps writes scoped
    to the verification Run and makes a screenshot failure fail closed.
    """
    from autophone.device.uds import request, save_image

    resolved_run = run_dir.resolve()
    resolved_evidence = evidence_dir.resolve()
    if resolved_evidence != resolved_run and resolved_run not in resolved_evidence.parents:
        raise ValueError("verification evidence directory escapes run directory")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    for index, checkpoint in enumerate(result.get("checkpoints") or [], 1):
        if not isinstance(checkpoint, dict):
            continue
        checkpoint_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(checkpoint.get("id") or index)).strip("-")
        checkpoint_id = checkpoint_id or str(index)
        shot = evidence_dir / f"verification-{checkpoint_id}.jpg"
        response, _sample = request({"t": "screenshot"})
        if not response.get("ok") or not response.get("image"):
            raise RuntimeError(f"checkpoint {checkpoint_id} screenshot failed")
        save_image(response, shot)
        rel = shot.relative_to(run_dir).as_posix()
        checkpoint.setdefault("evidence", [])
        if rel not in checkpoint["evidence"]:
            checkpoint["evidence"].append(rel)
    return result


def _build_prompt(verification: dict[str, Any]) -> str:
    lines = [
        f"验证需求：{verification.get('requirement') or verification.get('run_id')}",
        "逐个 checkpoint 在真机上执行并判定：",
    ]
    for cp in verification.get("checkpoints") or []:
        lines.append(f"- {cp.get('id')}: {cp.get('title')}")
    pre = verification.get("precondition") or {}
    if pre:
        lines.append(f"前置条件（已由平台准备，无需你造）：{json.dumps(pre, ensure_ascii=False)}")
    lines.append("输出每个 checkpoint 的 id/status/evidence/detail，以及总 status。")
    return "\n".join(lines)


def _budget_unsupported(req: AiRequest, max_budget_usd: float) -> AiResult | None:
    """provider 不能设单次上限时不调用、直接拒绝（与交付工作流 BL-036 G4 同一口径）。"""
    resolved, capabilities = provider_capabilities(req.provider)
    if capabilities.budget_limit:
        return None
    return AiResult(
        ok=False, text="", provider=resolved, model=req.model,
        error=(f"{AI_BUDGET_LIMIT_UNSUPPORTED}: provider {resolved} cannot enforce "
               f"max_budget_usd {max_budget_usd}; the call was not made"),
        max_budget_usd=max_budget_usd, error_code=AI_BUDGET_LIMIT_UNSUPPORTED,
    )


def _error_detail(res: AiResult) -> str:
    error = res.error or "verification agent returned no structured json"
    code = res.error_code
    if code and code not in error:
        return f"{code}: {error}"
    return error


def run_verification_agent(
    verification: dict[str, Any],
    *,
    provider: str | None = None,
    model: str | None = None,
    timeout_s: float = 600.0,
    runner: CommandRunner = _subprocess_runner,
    max_budget_usd: float | None = None,
    on_result: Callable[[AiResult], None] | None = None,
    stop_budget_usd: float | None = None,
) -> dict[str, Any]:
    """调 agent 逐 checkpoint 驱动真机，返回贴合 verification-executor-output.v1 的回填。

    fail-closed：agent 没回结构化 JSON → status=error + 空 checkpoints + detail，让上游可见。
    `max_budget_usd` 给出时：provider 不支持单次上限则不调用（`ai_budget_limit_unsupported`）；
    支持时下发 CLI 停止阈值（`stop_budget_usd`，缺省等于授权上限），并在调用后按授权上限核对回报成本
    （`enforce_call_budget`）。
    `on_result` 收到最终判定后的 `AiResult`（含 device_tool_trace、成本、错误码），供调用方留痕。
    """
    req = AiRequest(
        prompt=_build_prompt(verification),
        system=_SYSTEM,
        tools="device",
        json_schema=_MODEL_TRANSPORT_SCHEMA,
        provider=provider,
        model=model,
        timeout_s=timeout_s,
        max_budget_usd=max_budget_usd,
        stop_budget_usd=stop_budget_usd if max_budget_usd is not None else None,
    )
    res = _budget_unsupported(req, max_budget_usd) if max_budget_usd is not None else None
    if res is None:
        res = invoke(req, runner=runner)
        if max_budget_usd is not None:
            res = enforce_call_budget(res, max_budget_usd, stop_budget_usd=stop_budget_usd)
    # 逐 checkpoint 驱动真机必须有设备调用：留痕显示零次成功调用时按 error 回填（BL-067）
    res = require_device_actions(res)
    if on_result is not None:
        on_result(res)
    if not res.ok or not isinstance(res.json, dict):
        return {"schema_version": "v1", "status": "error", "checkpoints": [],
                "detail": _error_detail(res)}
    out = drop_optional_nulls(_MODEL_SCHEMA, dict(res.json))
    out["schema_version"] = "v1"
    out.setdefault("checkpoints", [])

    # fail-closed：回包必须覆盖**全部** expected checkpoint，且每个给出终态（非 pending）。
    # 否则即便顶层 status=passed 也不可信——缺判定的 checkpoint 会在合并后停在 pending，
    # 绝不能当成通过（审查 P1 假绿）。顶层 status 由 verification.py 从合并结果推导，这里只兜覆盖率。
    expected = {str(cp.get("id")) for cp in verification.get("checkpoints") or [] if cp.get("id")}
    returned = {str(cp.get("id")): str(cp.get("status") or "")
                for cp in out["checkpoints"] if isinstance(cp, dict) and cp.get("id")}
    missing = sorted(expected - returned.keys())
    still_pending = sorted(cid for cid in expected & returned.keys() if returned[cid] in ("", "pending"))
    if missing or still_pending:
        gaps = []
        if missing:
            gaps.append(f"未覆盖 checkpoint：{missing}")
        if still_pending:
            gaps.append(f"仍为 pending：{still_pending}")
        out["status"] = "error"
        out["detail"] = "验证 agent 回包不完整（fail-closed，拒绝假绿）：" + "；".join(gaps)

    validate_or_raise("verification-executor-output.v1.json", out)
    return out


def _capture_evidence(
    result: dict[str, Any],
    *,
    run_dir: Path,
    evidence_dir: Path,
    evidence_capture: EvidenceCapture | None,
) -> dict[str, Any]:
    """宿主为每个 checkpoint 补一张终态截图；采集失败按 error 回填（fail-closed）。"""
    if result.get("status") == "error":
        return result
    capture = evidence_capture or capture_checkpoint_evidence
    try:
        result = capture(result, run_dir=run_dir, evidence_dir=evidence_dir)
        validate_or_raise("verification-executor-output.v1.json", result)
    except Exception as exc:  # noqa: BLE001 - missing evidence must fail closed
        result["status"] = "error"
        result["detail"] = f"verification evidence capture failed: {type(exc).__name__}: {exc}"
    return result


def verify_agent_main(*, input_path: str | None = None, output_path: str | None = None,
                      provider: str | None = None, model: str | None = None,
                      runner: CommandRunner = _subprocess_runner,
                      evidence_capture: EvidenceCapture | None = None) -> int:
    """AGENT_CMD 入口：env（run_verification_executor 注入）或显式 flag 取 input/output。"""
    in_path = input_path or os.environ.get("AUTOPHONE_VERIFICATION_INPUT")
    out_path = output_path or os.environ.get("AUTOPHONE_VERIFICATION_OUTPUT")
    if not in_path or not out_path:
        raise SystemExit("ai verify-agent needs AUTOPHONE_VERIFICATION_INPUT/OUTPUT (or --input/--output)")
    verification = json.loads(Path(in_path).read_text(encoding="utf-8"))
    result = run_verification_agent(verification, provider=provider, model=model, runner=runner)
    run_dir_value = os.environ.get("AUTOPHONE_VERIFICATION_RUN_DIR")
    evidence_dir_value = os.environ.get("AUTOPHONE_VERIFICATION_EVIDENCE_DIR")
    if run_dir_value and evidence_dir_value:
        result = _capture_evidence(
            result, run_dir=Path(run_dir_value), evidence_dir=Path(evidence_dir_value),
            evidence_capture=evidence_capture,
        )
    write_json(Path(out_path), result)
    return 0 if result.get("status") != "error" else 1


def run_builtin_verification(
    verification: dict[str, Any],
    *,
    run_dir: Path,
    evidence_dir: Path,
    settings: Mapping[str, Any],
    call_record_path: Path | None = None,
    runner: CommandRunner | None = None,
    evidence_capture: EvidenceCapture | None = None,
) -> dict[str, Any]:
    """内置 verify-agent：在调用方线程内执行，返回贴合 verification-executor-output.v1 的回填。

    与 `verify_agent_main` 的子进程版共用 agent 调用与证据采集，输入输出契约相同；区别只在
    执行位置——调用方（验证单执行器）已持有 ScreenLock，并处于 Run 的 StageGuard execute 阶段，
    `invoke` 因此走受管私有代理 socket。`settings` 由 `verification_agent_settings` 解析
    （provider / model / max_budget_usd / stop_budget_usd / timeout_s）。`call_record_path` 给出时写入单次调用留痕
    （`AiResult` 契约：device_tool_trace、cost_usd、usage、error_code），供活机核对。
    """
    from autophone.util import now_iso

    record: dict[str, Any] = {
        "schema_version": "v1",
        "executor": "builtin:verify-agent",
        "settings": dict(settings),
        "started_at": now_iso(),
        "ai_result": None,
    }

    def remember(res: AiResult) -> None:
        try:
            record["ai_result"] = res.to_contract()
        except Exception as exc:  # noqa: BLE001 - 留痕失败不改判
            record["ai_result_error"] = f"{type(exc).__name__}: {exc}"

    try:
        result = run_verification_agent(
            verification,
            provider=settings.get("provider"),
            model=settings.get("model"),
            timeout_s=float(settings["timeout_s"]),
            max_budget_usd=settings.get("max_budget_usd"),
            stop_budget_usd=settings.get("stop_budget_usd"),
            runner=runner if runner is not None else _ai._subprocess_runner,
            on_result=remember,
        )
        result = _capture_evidence(
            result, run_dir=run_dir, evidence_dir=evidence_dir, evidence_capture=evidence_capture,
        )
        record["status"] = result.get("status")
        return result
    except AiError as exc:  # provider 解析失败等：按回填错误处理，不抛给 Run
        record["status"] = "error"
        return {"schema_version": "v1", "status": "error", "checkpoints": [],
                "detail": f"verification agent error: {exc}"}
    finally:
        if call_record_path is not None:
            record["ended_at"] = now_iso()
            write_json(call_record_path, record)
