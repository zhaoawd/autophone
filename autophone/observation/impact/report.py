"""Impact Run 的人读报告（影响图谱 §12.4）。

每个变更项要能一眼看到：改了什么 key、走了哪个接口的哪个字段、落在哪个页面状态的哪个
区域、怎么到达的、基线/当前/差异三联图、以及 verdict + 置信度 + Prod 安全裁决 + gap。

图片 base64 内嵌，报告自包含（能直接发给人看，不依赖 run 目录还在）。敏感区域的原图
不进这里——遮罩在生成帧的时候就做了，报告只呈现已遮罩的帧。
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Mapping

from autophone.observation.impact.result import (
    API_AND_UI_CHANGED,
    API_CHANGED_UI_UNCHANGED,
    API_UNCHANGED_UI_CHANGED,
    CACHED_OR_NOT_REFRESHED,
    INCONCLUSIVE,
    NOT_REACHED,
)

_VERDICT_STYLE: dict[str, tuple[str, str]] = {
    API_AND_UI_CHANGED: ("#16a34a", "✓ 接口与页面同时变化"),
    API_CHANGED_UI_UNCHANGED: ("#0284c7", "○ 接口变化，页面无可见变化"),
    API_UNCHANGED_UI_CHANGED: ("#d97706", "△ 页面变化，目标字段未变"),
    CACHED_OR_NOT_REFRESHED: ("#64748b", "· 未刷新或命中缓存"),
    NOT_REACHED: ("#dc2626", "! 未到达"),
    INCONCLUSIVE: ("#7c3aed", "? 证据不足"),
}

_CONFIDENCE_LABEL = {
    "causal": "causal（控制变量或权威契约）",
    "correlated": "correlated（多次稳定共现）",
    "observed": "observed（同现，仅候选）",
    "stale": "stale（证据失效，待重新标定）",
}


def _esc(value: Any) -> str:
    return (
        str(value if value is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _img(run_dir: Path, rel: Any) -> str:
    if not rel:
        return '<div class="noimg">无</div>'
    path = Path(str(rel))
    if not path.is_absolute():
        path = run_dir / path
    if not path.exists():
        return f'<div class="noimg">缺帧：{_esc(rel)}</div>'
    suffix = path.suffix.lower().lstrip(".") or "png"
    mime = "jpeg" if suffix in ("jpg", "jpeg") else suffix
    data = base64.b64encode(path.read_bytes()).decode()
    return f'<img src="data:image/{mime};base64,{data}" loading="lazy">'


def _kv(label: str, value: Any) -> str:
    return f'<div class="kv"><span>{_esc(label)}</span><b>{_esc(value)}</b></div>'


def _variant(selector: Mapping[str, Any] | None) -> str:
    items = dict(selector or {})
    if not items:
        return "*（该 screen 的所有状态）"
    return " · ".join(f"{key}={items[key]}" for key in sorted(items))


def _route(plan_target: Mapping[str, Any]) -> str:
    route = dict(plan_target.get("route") or {})
    if route.get("status") != "ready":
        return f'<span class="bad">路径不可用：{_esc(route.get("reason") or "未说明")}</span>'
    steps = route.get("steps") or []
    if not steps:
        return "冷启动复位后即在目标页面"
    chain = " → ".join(
        f'{_esc(step.get("operation"))}<span class="dim">({_esc(step.get("to"))})</span>'
        for step in steps
    )
    effects = route.get("effects") or []
    tail = f' <span class="bad">effects: {_esc(", ".join(effects))}</span>' if effects else ""
    return chain + tail


def _change_lookup(manifest: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for change in (manifest or {}).get("changes") or []:
        if not isinstance(change, dict):
            continue
        for key in ("data_key", "config_key"):
            if change.get(key):
                out[str(change[key])] = change
    return out


def render(
    result: Mapping[str, Any],
    *,
    plan: Mapping[str, Any] | None = None,
    manifest: Mapping[str, Any] | None = None,
    run_dir: Path | None = None,
) -> str:
    run_dir = Path(run_dir or ".")
    plan_targets = {
        str(t.get("target_id")): t for t in ((plan or {}).get("targets") or [])
    }
    controlled = bool(result.get("api_scenario"))
    changes = {} if controlled else _change_lookup(manifest)
    summary = dict(result.get("summary") or {})
    safety = dict(result.get("safety") or {})

    cards: list[str] = []
    for row in result.get("targets") or []:
        target_id = str(row.get("target_id"))
        plan_target = plan_targets.get(target_id, {})
        verdict = str(row.get("verdict"))
        color, label = _VERDICT_STYLE.get(verdict, ("#64748b", verdict))
        api = dict(row.get("api") or {})
        ui = dict(row.get("ui") or {})
        frames = {} if controlled else dict(row.get("frames") or {})

        keys = [] if controlled else list(plan_target.get("data_keys") or [])
        key_rows = []
        for key in keys:
            change = changes.get(key, {})
            old = change.get("old")
            new = change.get("new")
            delta = f"{old} → {new}" if (old is not None or new is not None) else "（未提供 old/new）"
            key_rows.append(_kv(key, delta))

        blocker = dict(row.get("blocker") or {})
        blocker_html = (
            f'<div class="blocker">blocker: <b>{_esc(blocker.get("kind"))}</b>'
            f' — {_esc(blocker.get("detail"))}</div>'
            if blocker else ""
        )
        changed_paths = api.get("changed_paths")
        comparison_details = {key: row[key] for key in ("comparison_sources", "api_comparison", "semantic_comparison", "variant_comparison", "controlled_validation") if key in row}
        cards.append(f"""
<section class="card" style="border-left-color:{color}">
  <header>
    <span class="verdict" style="background:{color}">{_esc(label)}</span>
    <h3>{_esc(row.get("screen_id"))}{" · 受控接口变化 / controlled_api_change" if row.get("source_kind") == "controlled_api_change" else ""}</h3>
    <span class="dim">{_esc(target_id)}{" · " + _esc(row.get("binding_id")) if row.get("binding_id") else ""}</span>
  </header>
  <div class="grid">
    <div class="col">
      <h4>变更字段</h4>
      {''.join(key_rows) or '<div class="dim">未提供 data/config key</div>'}
      {_kv("接口", api.get("endpoint_key") or "（未绑定接口）")}
      {_kv("响应字段", ("、".join(str(p) for p in changed_paths) or "（无）") if changed_paths is not None else "（未知）")}
      {_kv("接口是否采到", "是" if api.get("observed") else "否")}
      {_kv("字段是否变化", {True: "是", False: "否", None: "未知"}[api.get("changed")])}
    </div>
    <div class="col">
      <h4>页面状态与路径</h4>
      {_kv("variant", _variant(row.get("variant_selector")))}
      {_kv("region", "、".join(str(r) for r in (row.get("regions") or [])) or "（整页）")}
      <div class="kv"><span>路径</span><b>{_route(plan_target)}</b></div>
      {_kv("置信度", _CONFIDENCE_LABEL.get(str(row.get("confidence")), row.get("confidence") or "—"))}
      {_kv("证据 run", "、".join(str(r) for r in (row.get("evidence_runs") or [])) or "—")}
    </div>
    <div class="col">
      <h4>页面判定</h4>
      {_kv("到达", "是" if ui.get("reached") else "否")}
      {_kv("AX", {True: "变化", False: "无变化", None: "未知"}[ui.get("ax_changed")])}
      {_kv("语义(OCR/AX)", {True: "变化", False: "无变化", None: "未知"}[ui.get("semantic_changed")])}
      {_kv("视觉(advisory)", {True: "变化", False: "无变化", None: "未知"}[ui.get("visual_changed")])}
      {_kv("变化区域", "、".join(str(r) for r in (ui.get("changed_regions") or [])) or "—")}
      {_kv("遮罩区域", "、".join(str(r) for r in (ui.get("masked_regions") or [])) or "—")}
    </div>
  </div>
  {blocker_html}
  {_kv("API 证据", api.get("note") or "—")}
  {_kv("语义证据", ui.get("note") or "—")}
  {_kv("AX 结构差异", json.dumps(ui.get("ax_changes"), ensure_ascii=False) if "ax_changes" in ui else "未知")}
  <details><summary>比较来源与完整性</summary><pre>{_esc(json.dumps(comparison_details, ensure_ascii=False, indent=2))}</pre></details>
  <div class="shots">
    <figure><figcaption>基线</figcaption>{_img(run_dir, frames.get("baseline"))}</figure>
    <figure><figcaption>当前</figcaption>{_img(run_dir, frames.get("current"))}</figure>
    <figure><figcaption>差异热力图</figcaption>{_img(run_dir, frames.get("diff"))}</figure>
  </div>
</section>""")

    gap_rows = "".join(
        f'<li><b>{_esc(g.get("kind"))}</b> — {_esc(g.get("detail"))}</li>'
        for g in result.get("gaps") or []
    ) or '<li class="dim">无</li>'

    verdict_chips = "".join(
        f'<span class="chip" style="background:{_VERDICT_STYLE.get(v, ("#64748b", v))[0]}">'
        f'{_esc(v)} {int(n)}</span>'
        for v, n in sorted((summary.get("by_verdict") or {}).items())
    ) or '<span class="dim">无目标</span>'

    violations = int(safety.get("violations") or 0)
    safety_class = "bad" if violations else "ok"

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>影响报告 {_esc(result.get("change_id"))} · {_esc(result.get("app"))}</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",Inter,sans-serif;margin:0;
     background:#f8fafc;color:#0f172a;font-size:13px}}
.wrap{{max-width:1180px;margin:0 auto;padding:22px}}
h1{{font-size:19px;margin:0 0 4px}}
h3{{margin:0;font-size:15px}}
h4{{margin:0 0 6px;font-size:12px;color:#475569;text-transform:uppercase;letter-spacing:.04em}}
.meta{{color:#475569;margin-bottom:14px}}
.bar{{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:10px 0 18px}}
.chip{{color:#fff;border-radius:999px;padding:3px 10px;font-weight:700;font-size:12px}}
.card{{background:#fff;border:1px solid #e2e8f0;border-left-width:5px;border-radius:10px;
      padding:14px;margin-bottom:14px}}
.card header{{display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap}}
.verdict{{color:#fff;border-radius:6px;padding:3px 9px;font-weight:700;font-size:12px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:14px}}
.kv{{display:flex;gap:8px;padding:3px 0;border-bottom:1px dashed #eef2f7;line-height:1.5}}
.kv span{{color:#64748b;min-width:92px;flex:none}}
.kv b{{font-weight:600;word-break:break-all}}
.dim{{color:#94a3b8}}
.bad{{color:#dc2626;font-weight:700}}
.ok{{color:#16a34a;font-weight:700}}
.blocker{{margin-top:10px;background:#fef2f2;border:1px solid #fecaca;border-radius:7px;
        padding:8px 10px;color:#991b1b}}
.shots{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:12px}}
.shots figure{{margin:0;text-align:center}}
.shots figcaption{{font-size:11px;color:#64748b;margin-bottom:4px}}
.shots img{{width:100%;border:1px solid #e2e8f0;border-radius:7px}}
.noimg{{border:1px dashed #cbd5e1;border-radius:7px;padding:24px 6px;color:#94a3b8;font-size:12px}}
.panel{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px;margin-bottom:14px}}
ul{{margin:6px 0 0;padding-left:18px}}
li{{margin:3px 0}}
</style></head>
<body><div class="wrap">
<h1>页面 × API 影响报告</h1>
<div class="meta">
  {_esc(result.get("app"))} · build {_esc(result.get("build"))} · env {_esc(result.get("env"))}
  · change <b>{_esc(result.get("change_id"))}</b> · run {_esc(result.get("run_id"))}
  · 生成于 {_esc(result.get("generated_at"))}
</div>
<div class="bar">
  <span class="chip" style="background:#0f172a">目标 {int(summary.get("total") or 0)}</span>
  <span class="chip" style="background:#334155">到达 {int(summary.get("reached") or 0)}</span>
  {verdict_chips}
  <span class="{safety_class}">Prod 越权动作 {violations}</span>
  <span class="dim">输入质量 {_esc((plan or {}).get("input_quality") or "—")}</span>
</div>
{''.join(cards) or '<div class="panel dim">本次没有可执行目标</div>'}
<div class="panel">
  <h4>覆盖缺口 / gap</h4>
  <ul>{gap_rows}</ul>
</div>
<div class="panel">
  <h4>Prod 安全裁决</h4>
  {_kv("被拒 effect", "、".join(str(e) for e in (safety.get("denied_effects") or [])) or "—")}
  {_kv("未获准却执行", violations)}
  {_kv("说明", safety.get("note") or "计划内无写操作，或全部 effect 已登记")}
</div>
</div></body></html>"""


def write_report(
    run_dir: Path,
    result: Mapping[str, Any],
    *,
    plan: Mapping[str, Any] | None = None,
    manifest: Mapping[str, Any] | None = None,
    filename: str = "impact/report.html",
) -> Path:
    run_dir = Path(run_dir)
    path = run_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render(result, plan=plan, manifest=manifest, run_dir=run_dir), encoding="utf-8"
    )
    return path


def write_result(run_dir: Path, result: Mapping[str, Any]) -> Path:
    path = Path(run_dir) / "impact" / "result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
