"""report.py — machine + human reports (docs §10.5).

Writes runs/<ts>/result.json (machine) and runs/<ts>/report.md (human). Failed /
errored cases get their frames called out so a reviewer can see the failing
screen and trace back to the source Excel `row`.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from autophone.execution.engine.runner import RunResult

_STATUS_ICON = {"pass": "✅", "fail": "❌", "error": "💥", "blocked": "⛔", "skipped": "⏭️"}


def _case_result(outcome: Any) -> dict[str, Any]:
    """One case of result.json; optional Draft Trial fields appear only when produced."""
    out = asdict(outcome)
    for key in (
        "fixture_readback",
        "cleanup_readback",
        "api_assertion_context",
        "entity_readback",
        "joint_verdict",
        "resolved_entity",
    ):
        if out.get(key) is None:
            out.pop(key, None)
    # BL-186：执行身份只在用例经 Recipe 执行时出现；steps 摘要是平台内部核对值，不落报告。
    if out.get("recipe_identity") is None:
        out.pop("recipe_identity", None)
    out.pop("recipe_steps_digest", None)
    out.pop("failure_in_recipe", None)
    if out.get("status") in {"fail", "error", "blocked"}:
        from autophone.execution.failure_context import failure_fields
        out.update(failure_fields(out.get("failure_stage") or "not_recorded", out.get("failed_definition")))
    for key in ("failure_stage", "failed_definition"):
        if out.get(key) is None:
            out.pop(key, None)
    # 失败步骤下标与区分力反向 Run 记录只在产生时出现。
    for key in ("failed_recipe_step", "negative_control", "operation_witnesses"):
        if out.get(key) is None:
            out.pop(key, None)
    return out


def write_reports(result: RunResult, out_dir: Path,
                  cases_path: "Path | None" = None,
                  meta: "dict | None" = None,
                  case_docs: "tuple[dict, ...] | None" = None) -> tuple[Path, Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "result.json"
    md_path = out_dir / "report.md"

    payload = {
        "aborted": result.aborted,
        "abort_reason": result.abort_reason,
        "counts": result.counts,
        "cases": [_case_result(o) for o in result.outcomes],
        "cases_yaml": str(cases_path) if cases_path else None,
        # 运行元数据（run_id/app/device/type/生成时刻）：HTML 报告 header 展示用，缺省 None
        # 时报告退回用目录名当 run_id。result.json 不过 schema 校验，加字段安全。
        "meta": meta or None,
    }
    if case_docs is not None:
        payload["case_docs"] = list(case_docs)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_md(result, meta), encoding="utf-8")

    # interactive HTML review page (self-contained, base64-embedded shots)
    from autophone.execution.engine.html_report import write_html_report
    html_path = write_html_report(json_path)
    return json_path, md_path, html_path


def device_locale_text(meta: "dict | None") -> str | None:
    """报告里的设备语言一行（BL-207/BL-213）；meta 没有 `device_locale` 时 None。"""
    locale = (meta or {}).get("device_locale")
    if not isinstance(locale, dict):
        return None
    text = (f"偏好语言 {locale.get('preferred_language') or 'unknown'} · "
            f"区域 {locale.get('current_locale') or 'unknown'}")
    if locale.get("state") != "observed":
        text += f"（{locale.get('state') or 'unprobed'}：{locale.get('reason') or '原因未记录'}）"
    return text


def _render_md(result: RunResult, meta: "dict | None" = None) -> str:
    lines: list[str] = ["# Phase 2 回归报告", ""]
    locale_text = device_locale_text(meta)
    if locale_text:
        lines += [f"设备语言：{locale_text}", ""]
    if result.aborted:
        lines += [f"> **运行中止（gate 未通过）**：{result.abort_reason}", "",
                  "设备不可路由，未执行任何用例——不产生假 pass（DoD #4）。"]
        return "\n".join(lines) + "\n"

    c = result.counts
    total = sum(c.values())
    passed = c["pass"]
    blocked = c.get("blocked", 0)
    rate = f"{passed}/{total}" + (f" ({passed / total:.0%})" if total else "")
    lines += [
        f"通过率：**{rate}**　|　"
        f"{_STATUS_ICON['pass']} {c['pass']}　{_STATUS_ICON['fail']} {c['fail']}　"
        f"{_STATUS_ICON['error']} {c['error']}　"
        + (f"{_STATUS_ICON['blocked']} {blocked}　" if blocked else "")
        + f"{_STATUS_ICON['skipped']} {c['skipped']}",
        "",
        "| 用例 | 状态 | 模块 / 测试项 | 耗时 | 备注 |",
        "|---|---|---|---|---|",
    ]
    for o in result.outcomes:
        icon = _STATUS_ICON.get(o.status, o.status)
        # 失败归类（§10.10）跟在状态后，让人一眼分清是定位丢失 / 歧义 / 断言不符等。
        status_cell = f"{icon} {o.status}" + (f" · {o.fail_kind}" if o.fail_kind else "")
        detail = (o.detail or "").replace("|", "\\|").replace("\n", " ")
        if any(a.get("action", "").startswith("manual_") for a in o.actions):
            detail = f"{detail}；含人工步骤" if detail else "含人工步骤"
        lines.append(f"| {o.id} | {status_cell} | {o.module} / {o.item} "
                     f"| {o.duration_ms:.0f}ms | {detail} |")

    # Expand failures / errors / blocked with their frames + step trace.
    bad = [o for o in result.outcomes if o.status in ("fail", "error", "blocked")]
    if bad:
        lines += ["", "## 失败 / 异常用例"]
        for o in bad:
            status_line = f"- 状态：`{o.status}`" + (f"　归类：`{o.fail_kind}`" if o.fail_kind else "")
            if o.status == "blocked" and o.blocker:
                status_line += (f"　blocker：`{o.blocker.get('scope', '')}/{o.blocker.get('code', '')}`"
                                + (f"　恢复：{o.blocker['recovery']}" if o.blocker.get("recovery") else ""))
            lines += ["", f"### {_STATUS_ICON.get(o.status, '')} {o.id} — {o.item}",
                      f"{status_line}　源 Excel 行：`{o.row}`",
                      f"- 原因：{o.detail}", "- 动作轨迹："]
            for a in o.actions:
                mark = "ok" if a["ok"] else "FAIL"
                lines.append(f"  - `{a['action']}` [{mark}] {a['detail']}")
            if o.shots:
                lines.append("- 失败帧：")
                for s in o.shots:
                    lines.append(f"  - ![]({s})")
    return "\n".join(lines) + "\n"
