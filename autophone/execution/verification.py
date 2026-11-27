from __future__ import annotations

import html
import json
import math
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

import yaml

from autophone.assets.apps import AppProfile
from autophone.contracts import ContractError, validate_or_raise, write_json
from autophone.assets.operations import resolve_setup
from autophone.util import now_iso as _now

CHECKPOINT_STATUSES = {"pending", "passed", "failed", "error", "skipped"}
VERIFICATION_STATUSES = {"pending_agent", "passed", "failed", "error"}
REVIEW_STATUSES = {"approved", "rejected", "needs_changes"}

#: `AUTOPHONE_VERIFICATION_AGENT_CMD` 的内置执行器取值（BL-067 活机入口 (a)）。取该值时验证单执行器
#: 不起子进程，在 Run 线程内调用 `ai_verify.run_builtin_verification`，与 Run 共用同一 StageGuard。
#: 判定为去除首尾空白后的精确相等；其他任何取值（含旧的 `... ai verify-agent` 命令行）都按外部命令以
#: 子进程运行，行为不变。不按命令行规范化匹配：同一 CLI 有 `uv run` / `.venv/bin/python` / 附加参数等多种
#: 写法，规范化规则会把带额外参数或不同解释器的外部命令误判为内置。
BUILTIN_VERIFY_AGENT = "builtin:verify-agent"

#: 内置执行器的 provider / model / 单次费用授权上限 / CLI 停止阈值环境变量；Run Spec `verification.agent` 优先。
AGENT_SETTING_ENVS = {
    "provider": "AUTOPHONE_VERIFICATION_AGENT_PROVIDER",
    "model": "AUTOPHONE_VERIFICATION_AGENT_MODEL",
    "max_budget_usd": "AUTOPHONE_VERIFICATION_AGENT_MAX_BUDGET_USD",
    "stop_budget_usd": "AUTOPHONE_VERIFICATION_AGENT_STOP_BUDGET_USD",
}
_AGENT_PROVIDERS = {"claude", "codex", "auto"}

#: CLI 停止阈值缺省 = 授权上限 × 该比例（BL-067 控费，2026-10-14 裁定）。理由：claude `--max-budget-usd`
#: 只在轮次之间检查累计费用，最后一轮的费用会整轮计入；vm-alt 活机第二轮 go-home 在 1 美元阈值下单次
#: 实际 1.93 美元（两轮），单轮约 1 美元级别。阈值取授权上限的一半，给最后一轮留出余量。比例可由
#: `AUTOPHONE_VERIFICATION_AGENT_STOP_BUDGET_RATIO`（(0, 1]）调整；显式 `stop_budget_usd` 优先于比例。
DEFAULT_STOP_BUDGET_RATIO = 0.5
STOP_BUDGET_RATIO_ENV = "AUTOPHONE_VERIFICATION_AGENT_STOP_BUDGET_RATIO"


def is_builtin_verify_agent(command: str | None) -> bool:
    return str(command or "").strip() == BUILTIN_VERIFY_AGENT


def _positive_budget(value: Any, source: str) -> float:
    try:
        if isinstance(value, bool):
            raise ValueError
        budget = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"max_budget_usd from {source} must be a positive finite number, got {value!r}") from None
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError(f"max_budget_usd from {source} must be a positive finite number, got {value!r}")
    return budget


def verification_agent_settings(declared: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """解析内置执行器设置：Run Spec `verification.agent` > 环境变量 > 缺省。

    provider 缺省为 None（由 `ai.resolve_provider` 按 `AUTOPHONE_AI_PROVIDER` 与默认值解析）；model
    缺省为 None（后端默认模型，结果中以 CLI 回报的模型留痕）；max_budget_usd 是授权上限，缺省为 None
    （不设上限）；stop_budget_usd 是传给 CLI 的停止阈值，缺省为授权上限 × `DEFAULT_STOP_BUDGET_RATIO`
    （见 `_resolve_stop_budget`）；timeout_s 与外部命令同为
    `AUTOPHONE_VERIFICATION_AGENT_TIMEOUT_S`（缺省 300）。取值非法时抛 ValueError，由调用方按 error 回填。
    """
    declared = dict(declared or {})
    settings: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for key, env_name in AGENT_SETTING_ENVS.items():
        if declared.get(key) is not None:
            value, source = declared[key], "run_spec"
        elif os.environ.get(env_name, "").strip():
            value, source = os.environ[env_name].strip(), "env"
        else:
            value, source = None, "default"
        if value is not None and key in ("max_budget_usd", "stop_budget_usd"):
            value = _positive_budget(value, source)
        elif value is not None:
            value = str(value).strip() or None
        if key == "provider" and value is not None and value not in _AGENT_PROVIDERS:
            raise ValueError(f"provider from {source} must be one of {sorted(_AGENT_PROVIDERS)}, got {value!r}")
        settings[key] = value
        sources[key] = source
    _resolve_stop_budget(settings, sources)
    settings["timeout_s"] = _agent_timeout_s()
    sources["timeout_s"] = "env" if os.environ.get("AUTOPHONE_VERIFICATION_AGENT_TIMEOUT_S") else "default"
    settings["sources"] = sources
    return settings


def _stop_budget_ratio() -> tuple[float, str]:
    raw = os.environ.get(STOP_BUDGET_RATIO_ENV, "").strip()
    if not raw:
        return DEFAULT_STOP_BUDGET_RATIO, "default"
    try:
        ratio = float(raw)
    except ValueError:
        ratio = math.nan
    if not math.isfinite(ratio) or not 0 < ratio <= 1:
        raise ValueError(f"{STOP_BUDGET_RATIO_ENV} must be a number in (0, 1], got {raw!r}")
    return ratio, "env"


def _resolve_stop_budget(settings: dict[str, Any], sources: dict[str, str]) -> None:
    """授权上限与 CLI 停止阈值分离：缺省阈值 = 授权上限 × 比例；显式阈值须不大于授权上限。"""
    cap = settings.get("max_budget_usd")
    stop = settings.get("stop_budget_usd")
    if cap is None:
        if stop is not None:
            raise ValueError(
                f"stop_budget_usd from {sources['stop_budget_usd']} requires max_budget_usd (the authorized limit)")
        return
    if stop is None:
        ratio, ratio_source = _stop_budget_ratio()
        settings["stop_budget_usd"] = round(cap * ratio, 6)
        sources["stop_budget_usd"] = f"ratio:{ratio_source}"
        settings["stop_budget_ratio"] = ratio
        return
    if stop > cap:
        raise ValueError(f"stop_budget_usd {stop} from {sources['stop_budget_usd']} exceeds max_budget_usd {cap}")


def _normalize_checkpoint(item: Any, index: int) -> dict[str, Any]:
    if isinstance(item, str):
        return {"id": f"C{index}", "title": item, "status": "pending", "evidence": []}
    if isinstance(item, dict):
        return {
            "id": str(item.get("id") or f"C{index}"),
            "title": str(item.get("title") or item.get("checkpoint") or ""),
            "status": str(item.get("status") or "pending"),
            "evidence": list(item.get("evidence") or []),
        }
    return {"id": f"C{index}", "title": str(item), "status": "pending", "evidence": []}


def load_verification_spec(spec: dict[str, Any], profile: AppProfile) -> dict[str, Any]:
    declaration = dict(spec.get("verification") or {})
    path_value = declaration.get("path")
    loaded: dict[str, Any] = {}
    source: str | None = None
    if path_value:
        path = Path(str(path_value))
        if not path.is_absolute():
            path = profile.profile_dir / path
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            # 验证单是人/agent 手写资产，顶层写成 list/字符串必然发生；给契约错误
            # 而不是后面 .get() 的裸 AttributeError。
            raise ContractError(f"verification yaml must be a mapping, got {type(raw).__name__}: {path}")
        loaded = raw
        source = str(path)

    requirement = declaration.get("requirement") or loaded.get("requirement") or spec["run_id"]
    draft_id = declaration.get("draft_id") or loaded.get("draft_id")
    checkpoints = declaration.get("checkpoints") or loaded.get("checkpoints") or []
    normalized = [_normalize_checkpoint(item, idx) for idx, item in enumerate(checkpoints, 1)]
    precondition = loaded.get("precondition") or {}
    # precondition.setup 里若引用了操作库的操作，加载期就 fail-loud 校验（操作存在/active、
    # 参数合契约）——坏引用别等跑起来才炸。解析结果是派生量，不进 verification-result。
    resolve_setup(profile, precondition)
    return {
        "schema_version": "v1",
        "run_id": spec["run_id"],
        "app": spec["app"],
        "requirement": requirement,
        "draft_id": draft_id,
        "source": source,
        "precondition": precondition,
        "checkpoints": normalized,
        "status": "pending_agent",
        "detail": "verification executor is scaffolded; agent execution is not wired yet",
    }


def _executor_error(verification: dict[str, Any], detail: str) -> dict[str, Any]:
    out = dict(verification)
    out["status"] = "error"
    out["detail"] = detail
    return out


def run_verification_executor(
    run_dir: Path,
    verification: dict[str, Any],
    *,
    agent: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge optional agent/executor output into a verification result.

    Default mode stays pending. A real executor can be connected without changing
    RunSpec by either writing JSON and pointing AUTOPHONE_VERIFICATION_AGENT_RESULT_JSON
    at it, or by configuring AUTOPHONE_VERIFICATION_AGENT_CMD. The command gets
    input/output file paths through env vars and may either write JSON to the
    output path or print JSON to stdout.

    AUTOPHONE_VERIFICATION_AGENT_CMD=builtin:verify-agent runs the built-in agent
    in this thread (no subprocess), inside the caller's ScreenLock and StageGuard
    context.  ``agent`` (Run Spec ``verification.agent``) only applies to the
    built-in executor; declaring it for any other executor fails closed.
    """
    result_path = os.environ.get("AUTOPHONE_VERIFICATION_AGENT_RESULT_JSON")
    command = os.environ.get("AUTOPHONE_VERIFICATION_AGENT_CMD")
    if not result_path and not command:
        return verification

    input_path = write_json(run_dir / "verification-agent-input.json", verification)
    output_path = run_dir / "verification-agent-output.json"
    builtin = not result_path and is_builtin_verify_agent(command)
    if agent and not builtin:
        return _executor_error(
            verification,
            "verification.agent settings require AUTOPHONE_VERIFICATION_AGENT_CMD="
            f"{BUILTIN_VERIFY_AGENT}; the configured executor cannot apply them",
        )
    try:
        if result_path:
            executor_result = json.loads(Path(result_path).read_text(encoding="utf-8"))
            write_json(output_path, executor_result)
        elif builtin:
            # 延迟导入：只有内置执行器真正运行时才加载 AI 层，回归链不触达 autophone.application.ai。
            from autophone.application import ai_verify

            executor_result = ai_verify.run_builtin_verification(
                verification,
                run_dir=run_dir,
                evidence_dir=run_dir / "shots",
                settings=verification_agent_settings(agent),
                call_record_path=run_dir / "verification-agent-call.json",
            )
            write_json(output_path, executor_result)
            if executor_result.get("status") == "error":
                # 与子进程版一致：执行器报 error 时不合并 checkpoint，只记总状态与原因。
                return _executor_error(
                    verification, f"verification agent error: {executor_result.get('detail') or ''}",
                )
        else:
            env = dict(os.environ)
            env["AUTOPHONE_VERIFICATION_INPUT"] = str(input_path)
            env["AUTOPHONE_VERIFICATION_OUTPUT"] = str(output_path)
            env["AUTOPHONE_VERIFICATION_RUN_DIR"] = str(run_dir)
            env["AUTOPHONE_VERIFICATION_EVIDENCE_DIR"] = str(run_dir / "shots")
            completed = subprocess.run(
                shlex.split(str(command)),
                check=False,
                capture_output=True,
                text=True,
                env=env,
                timeout=_agent_timeout_s(),
            )
            if completed.returncode != 0:
                return _executor_error(
                    verification,
                    f"verification agent exited {completed.returncode}: {completed.stderr.strip()}",
                )
            if output_path.exists():
                executor_result = json.loads(output_path.read_text(encoding="utf-8"))
            else:
                executor_result = json.loads(completed.stdout)
                write_json(output_path, executor_result)
    except Exception as exc:  # noqa: BLE001 - executor failures are verification evidence
        return _executor_error(verification, f"verification executor error: {type(exc).__name__}: {exc}")

    return _merge_executor_result(verification, executor_result)


def _agent_timeout_s() -> float:
    raw = os.environ.get("AUTOPHONE_VERIFICATION_AGENT_TIMEOUT_S")
    if not raw:
        return 300.0
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 300.0


def _executor_result_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("executor result must be an object")
    checkpoints = payload.get("checkpoints") or []
    if not isinstance(checkpoints, list):
        raise ValueError("executor result checkpoints must be a list")
    return {
        "result_status": str(payload.get("status") or "unspecified"),
        "checkpoint_count": len([item for item in checkpoints if isinstance(item, dict)]),
    }


def verification_agent_doctor(
    *,
    result_json: str | None = None,
    agent_cmd: str | None = None,
    run_agent: bool = False,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    env_result = os.environ.get("AUTOPHONE_VERIFICATION_AGENT_RESULT_JSON")
    env_command = os.environ.get("AUTOPHONE_VERIFICATION_AGENT_CMD")
    configured_result = result_json or env_result
    configured_command = agent_cmd or env_command
    result: dict[str, Any] = {
        "schema_version": "v1",
        "result_json_configured": bool(configured_result),
        "result_json_source": "argument" if result_json else ("env" if env_result else "none"),
        "agent_cmd_configured": bool(configured_command),
        "agent_cmd_source": "argument" if agent_cmd else ("env" if env_command else "none"),
        "run_agent": run_agent,
        "status": "configured" if (configured_result or configured_command) else "not_configured",
    }
    if configured_command:
        result["agent_cmd_kind"] = "builtin" if is_builtin_verify_agent(configured_command) else "external"
    if configured_result:
        try:
            summary = _executor_result_summary(json.loads(Path(configured_result).read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001 - doctor reports configuration health
            result["status"] = "invalid_result_json"
            result["detail"] = type(exc).__name__
            return result
        result.update(summary)
        return result
    if not configured_command or not run_agent:
        return result
    if is_builtin_verify_agent(configured_command):
        # 内置执行器只在 Run 的 ScreenLock 与 StageGuard 内运行；doctor 不起子进程、不调用模型。
        result["detail"] = "builtin verify-agent runs in-process inside a verification Run; doctor does not run it"
        return result

    with tempfile.TemporaryDirectory(prefix="autophone-verification-doctor-") as tmp:
        run_dir = Path(tmp)
        input_path = write_json(run_dir / "verification-agent-input.json", {
            "schema_version": "v1",
            "run_id": "autophone-verification-doctor",
            "app": "doctor",
            "requirement": "verification agent doctor",
            "checkpoints": [{"id": "C1", "title": "agent command protocol", "status": "pending", "evidence": []}],
            "status": "pending_agent",
        })
        output_path = run_dir / "verification-agent-output.json"
        env = dict(os.environ)
        env["AUTOPHONE_VERIFICATION_INPUT"] = str(input_path)
        env["AUTOPHONE_VERIFICATION_OUTPUT"] = str(output_path)
        try:
            completed = subprocess.run(
                shlex.split(str(configured_command)),
                check=False,
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout_s or _agent_timeout_s(),
            )
        except Exception as exc:  # noqa: BLE001 - doctor reports agent command health
            result["status"] = "agent_error"
            result["detail"] = type(exc).__name__
            return result
        result["returncode"] = completed.returncode
        if completed.returncode != 0:
            result["status"] = "agent_failed"
            return result
        try:
            if output_path.exists():
                payload = json.loads(output_path.read_text(encoding="utf-8"))
            else:
                payload = json.loads(completed.stdout)
            result.update(_executor_result_summary(payload))
        except Exception as exc:  # noqa: BLE001
            result["status"] = "invalid_agent_output"
            result["detail"] = type(exc).__name__
            return result
    result["status"] = "healthy"
    return result


def _normalize_status(value: Any) -> str:
    text = str(value or "pending").lower()
    aliases = {
        "pass": "passed",
        "ok": "passed",
        "fail": "failed",
        "blocked": "failed",
    }
    text = aliases.get(text, text)
    return text if text in CHECKPOINT_STATUSES else "pending"


# 验证总状态的严重度序：合并执行器顶层与 checkpoint 推导时取「更坏」用（越大越坏）。
_STATUS_SEVERITY = {"passed": 0, "pending_agent": 1, "failed": 2, "error": 3}


def _derive_status(checkpoints: list[dict[str, Any]]) -> str:
    statuses = [_normalize_status(item.get("status")) for item in checkpoints]
    if any(status == "error" for status in statuses):
        return "error"
    if any(status == "failed" for status in statuses):
        return "failed"
    if statuses and all(status == "passed" for status in statuses):
        return "passed"
    return "pending_agent"


def _merge_executor_result(verification: dict[str, Any], executor_result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(executor_result, dict):
        raise ValueError("executor result must be an object")
    by_id = {
        str(item.get("id") or ""): item
        for item in executor_result.get("checkpoints") or []
        if isinstance(item, dict)
    }
    checkpoints: list[dict[str, Any]] = []
    for checkpoint in verification.get("checkpoints") or []:
        merged = dict(checkpoint)
        update = by_id.get(str(checkpoint.get("id") or ""))
        if update:
            merged["status"] = _normalize_status(update.get("status"))
            merged["evidence"] = list(dict.fromkeys(
                list(merged.get("evidence") or []) + [str(item) for item in update.get("evidence") or []]
            ))
            if update.get("detail"):
                merged["detail"] = str(update["detail"])
        checkpoints.append(merged)

    out = dict(verification)
    out["checkpoints"] = checkpoints
    # fail-closed：总状态取「checkpoints 推导」与「执行器顶层 status」中**更坏**的一个。
    # 执行器顶层只能让结果更坏（error/failed 硬失败必须保留），绝不能让它更绿——这样
    # {"status":"passed","checkpoints":[]} 不会把 pending 刷成假绿（审查 P1），而
    # {"status":"error","checkpoints":[]} 也不会被软化成 pending_agent（复审补洞）。
    executor_status = {"pass": "passed", "ok": "passed", "fail": "failed"}.get(
        str(executor_result.get("status") or ""), str(executor_result.get("status") or ""))
    derived = _derive_status(checkpoints)
    candidates = [derived]
    if executor_status in VERIFICATION_STATUSES:
        candidates.append(executor_status)
    out["status"] = max(candidates, key=_STATUS_SEVERITY.__getitem__)
    out["detail"] = str(
        executor_result.get("detail")
        or (f"verification executor reported {executor_status}" if executor_status else "verification executor completed")
    )
    return out


def write_verification_outputs(run_dir: Path, verification: dict[str, Any]) -> tuple[Path, Path]:
    # 与 platform-run-result.json 同一口径：promotion gate 消费这个文件，
    # 落盘前必须贴合契约（契约先于服务）。
    validate_or_raise("verification-result.v1.json", verification)
    result_path = write_json(run_dir / "verification-result.json", verification)
    report_payload = dict(verification)
    try:
        run_spec = json.loads((run_dir / "run-spec.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        run_spec = {}
    if isinstance(run_spec.get("store_context"), dict):
        report_payload["store_context"] = run_spec["store_context"]
    report_path = run_dir / "verification-report.html"
    report_path.write_text(render_verification_html(report_payload), encoding="utf-8")
    return result_path, report_path


def review_verification_result(
    run_dir: Path,
    *,
    status: str,
    reviewer: str,
    note: str = "",
) -> dict[str, Any]:
    status = str(status)
    if status not in REVIEW_STATUSES:
        raise ValueError(f"review status must be one of {sorted(REVIEW_STATUSES)}")
    result_path = run_dir / "verification-result.json"
    if not result_path.exists():
        raise FileNotFoundError(result_path)
    verification = json.loads(result_path.read_text(encoding="utf-8"))
    event = {
        "status": status,
        "reviewer": str(reviewer or "unknown"),
        "note": str(note or ""),
        "reviewed_at": _now(),
    }
    verification["review_status"] = status
    verification["reviewed_by"] = event["reviewer"]
    verification["review_note"] = event["note"]
    verification["reviewed_at"] = event["reviewed_at"]
    reviews = list(verification.get("reviews") or [])
    reviews.append(event)
    verification["reviews"] = reviews
    write_verification_outputs(run_dir, verification)
    return verification


def render_verification_html(verification: dict[str, Any]) -> str:
    store = verification.get("store_context") or {}
    store_html = ""
    if store:
        store_html = (
            f"<p>Store: <b>{html.escape(str(store.get('store_code') or ''))}</b> · "
            f"{html.escape(str(store.get('name') or ''))} · "
            f"catalog <code>{html.escape(str(store.get('catalog_snapshot_id') or ''))}</code></p>"
        )
    rows = []
    for checkpoint in verification.get("checkpoints") or []:
        evidence = "<br>".join(html.escape(str(item)) for item in checkpoint.get("evidence") or []) or "-"
        detail = html.escape(str(checkpoint.get("detail") or ""))
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(checkpoint.get('id') or ''))}</td>"
            f"<td>{html.escape(str(checkpoint.get('title') or ''))}</td>"
            f"<td>{html.escape(str(checkpoint.get('status') or 'pending'))}</td>"
            f"<td>{detail}</td>"
            f"<td>{evidence}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>Verification Report</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 24px; color: #16202a; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #d8dee8; padding: 8px 10px; text-align: left; vertical-align: top; }}
th {{ background: #eef2f6; }}
.pending {{ color: #9a6700; }}
</style>
</head>
<body>
<h1>{html.escape(str(verification.get("requirement") or "Verification"))}</h1>
<p>Run: {html.escape(str(verification.get("run_id") or ""))}</p>
{store_html}
<p>Status: <span class="pending">{html.escape(str(verification.get("status") or ""))}</span></p>
<p>Review: {html.escape(str(verification.get("review_status") or "unreviewed"))}
{html.escape(str(" by " + verification["reviewed_by"] if verification.get("reviewed_by") else ""))}
{html.escape(str(" - " + verification["review_note"] if verification.get("review_note") else ""))}</p>
<p>{html.escape(str(verification.get("detail") or ""))}</p>
<table>
<thead><tr><th>ID</th><th>Checkpoint</th><th>Status</th><th>Detail</th><th>Evidence</th></tr></thead>
<tbody>{"".join(rows)}</tbody>
</table>
</body>
</html>
"""
