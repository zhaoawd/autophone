"""html_report.py — generate an interactive HTML review page from RunResult.

Usage (standalone):
    python -m autophone.execution.engine.html_report runs/20260608-120000/result.json

Writes report.html next to result.json.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

_STATUS_COLOR = {"pass": "#22c55e", "fail": "#ef4444", "error": "#f97316", "blocked": "#d97706", "skipped": "#94a3b8"}
_STATUS_ICON  = {"pass": "✅", "fail": "❌", "error": "💥", "blocked": "⛔", "skipped": "⏭️"}


def _img_tag(shot_path: str | None, out_dir: Path) -> str:
    """Embed shot as base64 so the HTML is self-contained."""
    if not shot_path:
        return ""
    p = Path(shot_path) if Path(shot_path).is_absolute() else out_dir / shot_path
    if not p.exists():
        return f'<span class="no-shot">截图不存在: {shot_path}</span>'
    data = base64.b64encode(p.read_bytes()).decode()
    return f'<img src="data:image/jpeg;base64,{data}" class="shot" loading="lazy">'


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _attr(s) -> str:
    """Escape for an HTML double-quoted attribute value (adds quote handling)."""
    return _esc(s).replace('"', "&quot;").replace("'", "&#39;")


def _evidence_link(reference: str, out_dir: Path) -> str:
    path = Path(reference)
    path = path if path.is_absolute() else out_dir / path
    try:
        relative = path.resolve().relative_to(out_dir.resolve())
    except ValueError:
        return f'<br>证据文件：<code>{_esc(reference)}</code>'
    return (f'<br>证据文件：<a href="{_attr(relative.as_posix())}" '
            f'target="_blank" rel="noopener">{_esc(relative.as_posix())}</a>')


def _load_coverage(payload: dict, out_dir: Path) -> dict | None:
    """Load a generated Phase 5 coverage object if this run has one.

    Coverage is an input artifact, not inferred here. This keeps Phase 2's
    review report from becoming a second source of truth.
    """
    inline = payload.get("coverage")
    if isinstance(inline, dict):
        return inline

    raw = payload.get("coverage_json") or (payload.get("artifacts") or {}).get("coverage_json")
    candidates = []
    if raw:
        p = Path(raw)
        candidates.append(p if p.is_absolute() else out_dir / p)
    candidates.append(out_dir / "coverage.json")

    for path in candidates:
        if not path.exists():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(doc, dict) and doc.get("schema") == "autophone.coverage/1":
            return doc
    return None


def _load_case_meta(payload: dict, out_dir: Path) -> dict[str, dict]:
    """Build id → original-case-spec map from the cases YAML, so the report can
    show each case's precondition / steps / expects next to the actual result."""
    if "case_docs" in payload:
        return {c["id"]: c for c in payload["case_docs"]}
    raw = payload.get("cases_yaml")
    if not raw:
        return {}
    candidates = [Path(raw)]
    # fall back to repo-relative if the absolute path moved
    if not Path(raw).is_absolute():
        candidates.append(out_dir / raw)
    for p in candidates:
        if p.exists():
            try:
                import yaml
                doc = yaml.safe_load(p.read_text(encoding="utf-8"))
                return {c["id"]: c for c in doc.get("cases", [])}
            except Exception:
                return {}
    return {}


_API_VERDICT = {"pass": ("#2e7d32", "✓"), "fail": ("#c62828", "✗")}


def _api_block(c: dict) -> str:
    """Phase 4「后端 API」面板（§七）：渲染脱敏后的断言三态 + 归因调用摘要。
    只展示摘要（method/url/status/字段判定），原始 body 不内嵌——回溯指向本地 net_jsonl。"""
    assertions = c.get("api_assertions") or []
    calls = c.get("api_calls") or []
    if not assertions and not calls:
        return ""  # 纯 UI 跑（无抓取）→ 不渲染，phase2 行为不变

    rows = []
    for a in assertions:
        color, icon = _API_VERDICT.get(a.get("verdict", ""), ("#777", "•"))
        match = a.get("match", {})
        target = _esc(match.get("url_glob") or match.get("method") or "")
        kind = "禁止" if a.get("kind") == "forbid" else "期望"
        rows.append(
            f'<tr><td class="mark" style="color:{color}">{icon}</td>'
            f'<td>{kind}</td><td><code>{target}</code></td>'
            f'<td>{_esc(a.get("reason",""))}</td>'
            f'<td>{_esc(a.get("detail",""))}</td></tr>')
    assert_html = ""
    if rows:
        assert_html = (
            '<table class="actions"><thead><tr><th></th><th>类型</th>'
            '<th>接口</th><th>判定</th><th>详情</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')

    call_rows = []
    for c2 in calls:
        rfp = c2.get("resp_body_fp") or {}
        scope = c2.get("operation") or c2.get("step") or ""
        if c2.get("operation") and c2.get("step"):
            scope = f"{c2.get('operation')} / {c2.get('step')}"
        call_rows.append(
            f'<tr><td>{_esc(scope)}</td>'
            f'<td>{_esc(c2.get("method") or "")}</td>'
            f'<td>{_esc(c2.get("status") or "")}</td>'
            f'<td><code>{_esc(c2.get("url") or "")}</code></td>'
            f'<td>{rfp.get("len","")}B/{_esc(rfp.get("sha256",""))}</td></tr>')
    calls_html = ""
    if call_rows:
        calls_html = (
            '<div class="trace-title">归因调用（脱敏摘要）</div>'
            '<table class="actions"><thead><tr><th>步骤</th><th>方法</th>'
            '<th>状态</th><th>URL</th><th>resp 指纹</th></tr></thead>'
            f'<tbody>{"".join(call_rows)}</tbody></table>')

    jsonl = c.get("net_jsonl")
    src = (f'<div class="auto-reason">原始抓取（含 body，不入库）：'
           f'<code>{_esc(jsonl)}</code></div>' if jsonl else "")
    return f"""
      <div class="spec">
        <div class="spec-head">🌐 后端 API 断言</div>
        {assert_html or '<span class="empty">无断言</span>'}
        {calls_html}
        {src}
      </div>"""


def _coverage_metric_map(values: dict | None) -> str:
    if not values:
        return '<span class="empty">—</span>'
    return "".join(
        f'<span class="cv-mini"><b>{_esc(k)}</b>{_esc(v)}</span>'
        for k, v in sorted(values.items())
    )


def _coverage_tags(values, limit: int = 5) -> str:
    values = values or []
    if not values:
        return '<span class="empty">—</span>'
    shown = values[:limit]
    tags = " ".join(f'<code>{_esc(v)}</code>' for v in shown)
    if len(values) > limit:
        tags += f'<span class="empty">+{len(values) - limit}</span>'
    return tags


def _coverage_block(coverage: dict | None) -> str:
    """Phase 5 Coverage panel embedded into the Phase 2 review report."""
    if not coverage:
        return ""
    counts = coverage.get("counts") or {}
    gaps = coverage.get("gaps") or []
    cases = coverage.get("cases") or []
    total = sum(int(v) for v in counts.values() if isinstance(v, int))
    pass_count = counts.get("pass", 0)
    partial = counts.get("partial", 0)
    gap_rows = []
    for gap in gaps[:8]:
        ids = _coverage_tags(gap.get("case_ids") or [], limit=8)
        reason = gap.get("reason") or ""
        label = "本次无运行结果 · " if reason == "run_missing" else ""
        gap_rows.append(
            f'<tr><td>{label}<code>{_esc(reason)}</code></td>'
            f'<td>{_esc(gap.get("count", 0))}</td><td>{ids}</td></tr>'
        )
    if not gap_rows:
        gap_rows.append('<tr><td colspan="3"><span class="empty">无 gap</span></td></tr>')

    interesting = [
        c for c in cases
        if c.get("status") in {"pass", "partial", "fail", "error"}
        or c.get("fixture_id")
        or c.get("api_assertions")
        or c.get("verifier")
    ][:12]
    case_rows = []
    for c in interesting:
        case_rows.append(
            f'<tr><td><span class="cv-status cv-{_attr(c.get("status") or "unknown")}">'
            f'{_esc((c.get("status") or "unknown").upper())}</span></td>'
            f'<td><b>{_esc(c.get("case_id") or "")}</b></td>'
            f'<td>{_esc(c.get("module") or "")}<small>{_esc(c.get("item") or "")}</small></td>'
            f'<td>{_esc(c.get("fixture_id") or "—")}</td>'
            f'<td>{_esc(c.get("ui_steps", 0))}</td>'
            f'<td>{_esc(c.get("api_calls", 0))}</td>'
            f'<td>{_coverage_metric_map(c.get("api_assertions"))}</td>'
            f'<td>{_coverage_metric_map(c.get("verifier"))}</td>'
            f'<td>{_coverage_tags(c.get("gaps") or [], limit=4)}</td></tr>'
        )
    if not case_rows:
        case_rows.append('<tr><td colspan="9"><span class="empty">暂无已运行/部分覆盖用例</span></td></tr>')

    return f"""
      <div class="coverage-panel">
        <div class="coverage-head">
          <div>
            <div class="coverage-title">App 用例库覆盖范围</div>
            <div class="coverage-sub">run <code>{_esc(coverage.get("run_id") or "")}</code></div>
          </div>
          <div class="coverage-kpis">
            <span>用例库总数 <b>{total}</b></span>
            <span class="cv-pass">pass <b>{_esc(pass_count)}</b></span>
            <span class="cv-partial">partial <b>{_esc(partial)}</b></span>
            <span>gaps <b>{len(gaps)}</b></span>
          </div>
        </div>
        <p class="coverage-note">此处按 App 用例库统计，包含本次没有运行结果的用例。<code>run_missing</code> 表示该用例在本次覆盖统计中缺少运行结果。</p>
        <div class="trace-title">覆盖缺口</div>
        <table class="actions coverage-table">
          <thead><tr><th>原因</th><th>数量</th><th>用例</th></tr></thead>
          <tbody>{''.join(gap_rows)}</tbody>
        </table>
        <div class="trace-title">已运行 / 部分覆盖用例</div>
        <table class="actions coverage-table">
          <thead><tr><th>状态</th><th>用例</th><th>模块 / 测试项</th><th>Fixture</th>
          <th>UI</th><th>API</th><th>API 断言</th><th>Verifier</th><th>Gaps</th></tr></thead>
          <tbody>{''.join(case_rows)}</tbody>
        </table>
      </div>"""


def _spec_block(meta: dict | None) -> str:
    """Render the original test-case spec (从 Excel/YAML) for reviewer cross-check."""
    if not meta:
        return ""
    def _list(items, cls):
        items = items or []
        if not items:
            return '<span class="empty">—</span>'
        return "<ol class='%s'>%s</ol>" % (
            cls, "".join(f"<li>{_esc(x)}</li>" for x in items))
    level = _esc(meta.get("level", ""))
    auto  = _esc(meta.get("automation", ""))
    reason = _esc(meta.get("automation_reason", ""))
    return f"""
      <div class="spec">
        <div class="spec-head">
          📑 原始用例（Excel 行 {meta.get('row','?')}　优先级 {level}　分类 {auto}）
        </div>
        <div class="spec-grid">
          <div><b>前置条件</b>{_list(meta.get('precondition'), 'pre')}</div>
          <div><b>测试步骤</b>{_list(meta.get('steps'), 'steps')}</div>
          <div><b>预期结果</b>{_list(meta.get('expects'), 'exp')}</div>
        </div>
        {f'<div class="auto-reason">分类依据：{reason}</div>' if reason else ''}
      </div>"""


def _fmt_dur(ms: float) -> str:
    """毫秒 → 人类可读（1.2s / 1m03s）。"""
    if ms <= 0:
        return "—"
    s = ms / 1000.0
    if s < 60:
        return f"{s:.1f}s"
    return f"{int(s // 60)}m{int(s % 60):02d}s"


def _meta_header(payload: dict, out_dir: Path, cases: list) -> str:
    """运行元数据条：run_id / app / device / 类型 / 总耗时 / 生成时刻。

    meta 缺省（CLI / 旧 run.json）时退回用目录名当 run_id，其余项跳过——
    报告对历史产物仍可渲染，不报错。"""
    meta = payload.get("meta") or {}
    run_id = _esc(meta.get("run_id") or out_dir.name)
    total_dur = sum(float(c.get("duration_ms") or 0) for c in cases)
    chips = [("RUN", run_id)]
    if meta.get("app"):
        chips.append(("APP", _esc(meta["app"])))
    if meta.get("device"):
        chips.append(("设备", _esc(meta["device"])))
    if meta.get("type"):
        chips.append(("类型", _esc(meta["type"])))
    from autophone.execution.engine.report import device_locale_text

    locale_text = device_locale_text(meta)
    if locale_text:
        chips.append(("设备语言", _esc(locale_text)))
    store = payload.get("store_context") or {}
    if store:
        chips.append(("门店", _esc(
            f"{store.get('store_code') or ''} · {store.get('name') or store.get('short_name') or ''}"
        )))
        chips.append(("门店目录", _esc(store.get("catalog_snapshot_id") or "—")))
    if total_dur > 0:
        chips.append(("总耗时", _fmt_dur(total_dur)))
    if meta.get("generated_at"):
        chips.append(("生成", _esc(meta["generated_at"])))
    cells = "".join(
        f'<div class="meta-cell"><span class="meta-k">{k}</span>'
        f'<span class="meta-v">{v}</span></div>'
        for k, v in chips
    )
    return f'<div class="meta-bar">{cells}</div>'


def render_html(payload: dict, out_dir: Path) -> str:
    aborted = payload.get("aborted", False)
    abort_reason = payload.get("abort_reason", "")
    counts = payload.get("counts", {})
    cases = payload.get("cases", [])
    case_meta = _load_case_meta(payload, out_dir)
    coverage_html = _coverage_block(_load_coverage(payload, out_dir))

    total  = sum(counts.values())
    passed = counts.get("pass", 0)
    rate   = f"{passed}/{total}" if total else "0/0"

    # ── per-case cards ──────────────────────────────────────────────────────
    case_cards = []
    for c in cases:
        st    = c["status"]
        color = _STATUS_COLOR.get(st, "#64748b")
        icon  = _STATUS_ICON.get(st, st)
        has_manual = any((a.get("action") or "").startswith("manual_") for a in c.get("actions", []))
        manual_tag = '<span class="manual-tag">含人工步骤</span>' if has_manual else ""
        # 失败归类（§10.10）：fail 用例带 taxonomy kind，summary 上一枚 chip 让人一眼分桶。
        kind_tag = f'<span class="kind-tag">{_esc(c["fail_kind"])}</span>' if c.get("fail_kind") else ""
        # 部分覆盖徽标（防假绿）：recipe 只自动断言了 expects 子集，整卡 PASS 不代表整条手工用例已覆盖。
        coverage_tag = ('<span class="coverage-tag" title="recipe 仅自动断言用例 expects 的子集，'
                        '其余项由独立 discovery 覆盖">部分覆盖</span>'
                        if str(c.get("coverage") or "") == "partial" else "")

        # action rows (compact table, no inline image — shots go in the filmstrip)
        action_rows = []
        for idx, a in enumerate(c.get("actions", []), 1):
            ok_cls  = "ok" if a["ok"] else "fail-step"
            ok_text = "✓" if a["ok"] else "✗"
            evidence_links = "".join(
                _evidence_link(reference, out_dir) for reference in a.get("evidence_refs") or []
            )
            action_rows.append(f"""
              <tr class="{ok_cls}">
                <td class="mark">{ok_text}</td>
                <td class="step-n">{idx}</td>
                <td><code>{_esc(a['action'])}</code></td>
                <td>{_esc(a.get('detail',''))}{evidence_links}</td>
                <td class="ms">{a.get('elapsed_ms',0):.0f}ms</td>
              </tr>""")

        actions_html = ""
        if action_rows:
            actions_html = f"""
            <div class="trace-title">动作轨迹</div>
            <table class="actions">
              <thead><tr><th></th><th>#</th><th>动作</th><th>详情</th><th>耗时</th></tr></thead>
              <tbody>{''.join(action_rows)}</tbody>
            </table>"""

        # filmstrip: larger, scrollable screenshots from the action trace
        film_cells = []
        for idx, a in enumerate(c.get("actions", []), 1):
            sp = a.get("shot_path")
            if sp:
                tag = _img_tag(sp, out_dir)
                if tag:
                    fail_cls = "" if a["ok"] else " film-fail"
                    film_cells.append(
                        f'<figure class="film{fail_cls}">{tag}'
                        f'<figcaption>#{idx} {_esc(a["action"])}</figcaption></figure>')
            # 增强 OCR 等派生帧：判定所依据的实际 OCR 输入，单独标注以便复核
            for aux in a.get("aux_shots") or []:
                tag = _img_tag(aux, out_dir)
                if tag:
                    film_cells.append(
                        f'<figure class="film film-aux">{tag}'
                        f'<figcaption>#{idx} {_esc(a["action"])} · 增强帧</figcaption></figure>')
        film_html = ""
        if film_cells:
            film_html = (f'<div class="trace-title">截图（点击放大）</div>'
                         f'<div class="filmstrip">{"".join(film_cells)}</div>')

        cid = c["id"]
        case_cards.append(f"""
        <details class="case-card" data-status="{_attr(st)}" data-id="{_attr(cid)}">
          <summary>
            <span class="badge" style="background:{color}">{icon} {_esc(st.upper())}</span>
            <span class="case-id">{_esc(cid)}</span>
            <span class="case-item">{_esc(c['module'])} / {_esc(c['item'])}</span>
            <span class="dur">{c['duration_ms']:.0f}ms</span>
            {kind_tag}
            {coverage_tag}
            {manual_tag}
            <span class="review-tag"></span>
          </summary>
          <div class="case-body">
            {_spec_block(case_meta.get(cid))}
            {f'<p class="detail-text">⚠️ {_esc(c["detail"])}</p>' if c.get("detail") else ""}
            {actions_html}
            {_api_block(c)}
            {film_html}
            <div class="review-box">
              <b>人工复核：</b>
              <button class="rv rv-ok"    data-verdict="confirm">✓ 确认通过</button>
              <button class="rv rv-bad"   data-verdict="reject">✗ 判为问题</button>
              <button class="rv rv-clear" data-verdict="">清除</button>
            </div>
          </div>
        </details>""")

    body_content = ""
    if aborted:
        body_content = (
            f'<div class="abort-banner">⚠️ 运行中止（gate 未通过）：{_esc(abort_reason)}</div>'
            f'{coverage_html}'
        )
    else:
        stat_pills = "".join(
            f'<span class="pill" style="background:{_STATUS_COLOR[k]}">'
            f'{_STATUS_ICON[k]} {k} {counts.get(k,0)}</span>'
            for k in ("pass","fail","error","blocked","skipped") if k != "blocked" or counts.get("blocked")
        )
        filter_btns = "".join(
            f'<button class="filter-btn" onclick="filter(\'{k}\')">'
            f'{_STATUS_ICON[k]} {k}</button>'
            for k in ("pass","fail","error","blocked","skipped")
        )
        body_content = f"""
        <div class="summary-bar">
          <span class="rate">本次运行结果 · 通过率 <strong>{rate}</strong></span>
          {stat_pills}
        </div>
        {coverage_html}
        <div class="filter-bar">
          <button class="filter-btn active" onclick="filter('all')">全部</button>
          {filter_btns}
          <button class="filter-btn" onclick="filter('unreviewed')">🔍 待复核</button>
          <button class="filter-btn" style="margin-left:auto" onclick="exportReview()">⬇ 导出复核结果</button>
        </div>
        <div id="cards">{''.join(case_cards)}</div>"""

    run_key = out_dir.name

    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>Phase 2 回归报告</title>
<style>
  body{{font-family:system-ui,sans-serif;margin:0;padding:16px 24px;background:#0f172a;color:#e2e8f0}}
  h1{{font-size:1.4rem;margin-bottom:12px}}
  .meta-bar{{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:16px;padding:11px 14px;
    background:#1e293b;border:1px solid #334155;border-radius:8px}}
  .meta-cell{{display:flex;flex-direction:column;gap:2px;min-width:0}}
  .meta-k{{font-size:.66rem;font-weight:700;letter-spacing:.05em;color:#64748b;text-transform:uppercase}}
  .meta-v{{font-size:.86rem;font-weight:600;color:#e2e8f0;font-variant-numeric:tabular-nums;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:340px}}
  .summary-bar{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}}
  .rate{{font-size:1.1rem}}
  .pill{{padding:3px 10px;border-radius:99px;font-size:.85rem;color:#fff}}
  .filter-bar{{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}}
  .filter-btn{{padding:4px 12px;border:1px solid #334155;border-radius:6px;background:#1e293b;
    color:#94a3b8;cursor:pointer;font-size:.85rem}}
  .filter-btn.active{{border-color:#60a5fa;color:#60a5fa}}
  .case-card{{background:#1e293b;border:1px solid #334155;border-radius:8px;
    margin-bottom:8px;overflow:hidden}}
  .case-card summary{{display:flex;align-items:center;gap:10px;padding:10px 14px;
    cursor:pointer;list-style:none;flex-wrap:wrap}}
  .case-card summary::-webkit-details-marker{{display:none}}
  .badge{{padding:2px 8px;border-radius:4px;font-size:.78rem;color:#fff;white-space:nowrap}}
  .case-id{{font-weight:700;font-size:.95rem}}
  .case-item{{color:#94a3b8;font-size:.88rem;flex:1}}
  .dur{{color:#64748b;font-size:.8rem;white-space:nowrap}}
  .manual-tag{{font-size:.75rem;padding:1px 8px;border-radius:99px;
    background:#713f12;color:#fde68a;white-space:nowrap}}
  .kind-tag{{font-size:.72rem;padding:1px 8px;border-radius:99px;font-family:ui-monospace,monospace;
    background:#3b1d1d;color:#fca5a5;white-space:nowrap}}
  .coverage-tag{{font-size:.72rem;padding:1px 8px;border-radius:99px;
    background:#78350f;color:#fcd34d;white-space:nowrap}}
  .case-body{{padding:12px 16px;border-top:1px solid #334155}}
  .detail-text{{color:#fbbf24;margin:0 0 10px;font-size:.88rem}}
  /* original test-case spec */
  .spec{{background:#172033;border:1px solid #334155;border-radius:6px;
    padding:10px 12px;margin-bottom:12px}}
  .spec-head{{color:#93c5fd;font-size:.85rem;margin-bottom:8px}}
  .spec-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
    gap:14px;font-size:.83rem}}
  .spec-grid > div{{background:#0f1a2e;border-radius:5px;padding:8px 10px}}
  .spec-grid b{{display:block;color:#64748b;margin-bottom:4px;font-weight:600}}
  .spec-grid ol{{margin:0;padding-left:18px;line-height:1.5}}
  .spec-grid ol.exp li{{color:#86efac}}
  .spec-grid .empty{{color:#475569}}
  .auto-reason{{color:#64748b;font-size:.78rem;margin-top:8px;
    border-top:1px solid #1e293b;padding-top:6px}}
  /* reviewer controls */
  .review-box{{margin-top:12px;padding-top:10px;border-top:1px solid #334155;
    font-size:.85rem}}
  .rv{{margin-left:6px;padding:4px 12px;border-radius:6px;border:1px solid #334155;
    background:#1e293b;color:#cbd5e1;cursor:pointer;font-size:.82rem}}
  .rv-ok:hover{{border-color:#22c55e;color:#22c55e}}
  .rv-bad:hover{{border-color:#ef4444;color:#ef4444}}
  .review-tag{{font-size:.75rem;padding:1px 8px;border-radius:99px}}
  .review-tag.confirm{{background:#14532d;color:#86efac}}
  .review-tag.reject{{background:#7f1d1d;color:#fca5a5}}
  table.actions{{width:100%;border-collapse:collapse;font-size:.82rem}}
  table.actions th{{text-align:left;color:#64748b;padding:4px 8px;
    border-bottom:1px solid #334155}}
  table.actions td{{padding:5px 8px;border-bottom:1px solid #1e293b;vertical-align:top}}
  td.step-n{{color:#64748b;width:24px}}
  tr.ok .mark{{color:#22c55e;font-weight:700}}
  tr.fail-step{{background:#3f1818}}
  tr.fail-step .mark{{color:#ef4444;font-weight:700}}
  td.ms{{color:#64748b;white-space:nowrap}}
  .trace-title{{color:#93c5fd;font-size:.82rem;margin:14px 0 6px;font-weight:600}}
  .coverage-panel{{background:#172033;border:1px solid #334155;border-radius:8px;
    padding:12px 14px;margin:12px 0 16px}}
  .coverage-head{{display:flex;align-items:flex-start;gap:12px;justify-content:space-between;
    flex-wrap:wrap}}
  .coverage-title{{color:#93c5fd;font-weight:700;font-size:.95rem}}
  .coverage-sub{{color:#64748b;font-size:.78rem;margin-top:2px}}
  .coverage-note{{color:#cbd5e1;font-size:.88rem;line-height:1.6;margin:12px 0}}
  .coverage-kpis{{display:flex;flex-wrap:wrap;gap:8px}}
  .coverage-kpis span{{border:1px solid #334155;border-radius:999px;padding:3px 9px;
    font-size:.8rem;background:#0f1a2e;color:#cbd5e1}}
  .coverage-kpis b{{font-variant-numeric:tabular-nums}}
  .cv-pass{{color:#86efac!important}}
  .cv-partial{{color:#fcd34d!important}}
  .cv-status{{font-size:.72rem;font-weight:700;white-space:nowrap}}
  .cv-pass.cv-status{{color:#86efac!important}}
  .cv-partial.cv-status{{color:#fcd34d!important}}
  .cv-fail.cv-status,.cv-error.cv-status{{color:#fca5a5!important}}
  .cv-skipped.cv-status,.cv-uncovered.cv-status{{color:#94a3b8!important}}
  .coverage-table small{{display:block;color:#64748b;margin-top:2px}}
  .cv-mini{{display:inline-flex;gap:4px;margin:0 4px 4px 0;border:1px solid #334155;
    border-radius:4px;padding:1px 5px;background:#0f1a2e}}
  .cv-mini b{{color:#64748b}}
  /* filmstrip: horizontal scroll of larger shots */
  .filmstrip{{display:flex;gap:12px;overflow-x:auto;padding:4px 0 10px;
    scroll-snap-type:x proximity}}
  figure.film{{margin:0;flex:0 0 auto;scroll-snap-align:start;text-align:center}}
  figure.film img.shot{{height:360px;width:auto;border-radius:6px;cursor:pointer;
    border:2px solid #334155;transition:border-color .15s}}
  figure.film img.shot:hover{{border-color:#60a5fa}}
  figure.film.film-fail img.shot{{border-color:#ef4444}}
  figure.film.film-aux img.shot{{border-color:#a78bfa;border-style:dashed}}
  figure.film figcaption{{color:#94a3b8;font-size:.72rem;margin-top:4px;
    max-width:170px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
  .no-shot{{color:#64748b;font-size:.78rem}}
  .abort-banner{{background:#7f1d1d;border:1px solid #ef4444;border-radius:8px;
    padding:16px;color:#fca5a5}}
  /* lightbox */
  #lb{{display:none;position:fixed;inset:0;background:#000c;z-index:999;
    align-items:center;justify-content:center}}
  #lb.open{{display:flex}}
  #lb img{{max-width:90vw;max-height:90vh;border-radius:8px}}
</style>
</head>
<body>
<h1>📋 Phase 2 回归报告</h1>
{_meta_header(payload, out_dir, cases)}
{body_content}
<div id="lb" onclick="this.classList.remove('open')">
  <img id="lb-img" src="" alt="">
</div>
<script>
const RUN_KEY = "run_review_" + {run_key!r};
let reviews = JSON.parse(localStorage.getItem(RUN_KEY) || "{{}}");

function tagFor(card){{
  const id = card.dataset.id, v = reviews[id] || "";
  const t = card.querySelector('.review-tag');
  if(t){{ t.className = "review-tag " + v;
          t.textContent = v==="confirm" ? "已确认" : v==="reject" ? "已驳回" : ""; }}
}}
function review(card, verdict){{
  const id = card.dataset.id;
  if(verdict) reviews[id]=verdict; else delete reviews[id];
  localStorage.setItem(RUN_KEY, JSON.stringify(reviews));
  tagFor(card);
}}
function exportReview(){{
  const rows = [["case_id","review"]];
  document.querySelectorAll('.case-card').forEach(c=>{{
    rows.push([c.dataset.id, reviews[c.dataset.id]||"unreviewed"]);
  }});
  const csv = rows.map(r=>r.join(",")).join("\\n");
  const blob = new Blob([csv], {{type:"text/csv"}});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "review_" + {run_key!r} + ".csv";
  a.click();
}}
function filter(st){{
  document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
  event.target.classList.add('active');
  document.querySelectorAll('.case-card').forEach(c=>{{
    let show;
    if(st==='all') show=true;
    else if(st==='unreviewed') show=!reviews[c.dataset.id];
    else show=c.dataset.status===st;
    c.style.display=show?'':'none';
  }});
}}
document.addEventListener('click',e=>{{
  if(e.target.matches('img.shot')){{
    document.getElementById('lb-img').src=e.target.src;
    document.getElementById('lb').classList.add('open');
  }}
}});
// review buttons via delegation — case id never inlined into JS/HTML
document.querySelectorAll('.rv').forEach(b=>b.addEventListener('click',e=>{{
  review(e.target.closest('.case-card'), e.target.dataset.verdict || "");
}}));
// restore saved review tags on load
document.querySelectorAll('.case-card').forEach(tagFor);
</script>
</body>
</html>"""


def write_html_report(result_json: Path) -> Path:
    payload  = json.loads(result_json.read_text(encoding="utf-8"))
    out_dir  = result_json.parent
    spec_path = out_dir / "run-spec.json"
    if spec_path.exists():
        try:
            run_spec = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            run_spec = {}
        if isinstance(run_spec.get("store_context"), dict):
            payload["store_context"] = run_spec["store_context"]
    html_path = out_dir / "report.html"
    html_path.write_text(render_html(payload, out_dir), encoding="utf-8")
    return html_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m autophone.execution.engine.html_report <result.json>")
        sys.exit(1)
    p = write_html_report(Path(sys.argv[1]))
    print(f"✅ 报告已生成: {p}")
