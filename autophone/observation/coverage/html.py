"""Render Phase 5 coverage.html from generated coverage data."""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any

STATUS_ORDER = ("pass", "partial", "fail", "error", "skipped", "uncovered")


def _esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _status_class(status: str) -> str:
    return status if status in STATUS_ORDER else "unknown"


def _status_label(status: str) -> str:
    return status.upper() if status else "UNKNOWN"


def _count_total(counts: dict[str, Any]) -> int:
    total = 0
    for value in counts.values():
        try:
            total += int(value)
        except (TypeError, ValueError):
            continue
    return total


def _render_counts(counts: dict[str, Any]) -> str:
    total = _count_total(counts)
    pills = [f'<span class="count total">total <b>{total}</b></span>']
    for status in STATUS_ORDER:
        value = counts.get(status, 0)
        pills.append(
            f'<span class="count status-{status}">{_esc(status)} <b>{_esc(value)}</b></span>'
        )
    return "".join(pills)


def _render_artifacts(artifacts: dict[str, Any]) -> str:
    if not artifacts:
        return '<span class="muted">none</span>'
    rows = []
    for key in sorted(artifacts):
        value = artifacts.get(key)
        if value:
            rows.append(f'<span><b>{_esc(key)}</b><code>{_esc(value)}</code></span>')
    if not rows:
        return '<span class="muted">none</span>'
    return "".join(rows)


def _render_gap_groups(gaps: list[dict[str, Any]]) -> str:
    if not gaps:
        return '<div class="empty">no gaps</div>'
    cards = []
    for gap in gaps:
        reason = str(gap.get("reason") or "unknown")
        case_ids = gap.get("case_ids") or []
        ids = "".join(f'<code>{_esc(cid)}</code>' for cid in case_ids)
        cards.append(
            '<section class="gap-card">'
            f'<div class="gap-head"><b>{_esc(reason)}</b><span>{_esc(gap.get("count", len(case_ids)))}</span></div>'
            f'<div class="gap-ids">{ids}</div>'
            '</section>'
        )
    return "".join(cards)


def _render_metric_map(values: dict[str, Any] | None) -> str:
    if not values:
        return '<span class="muted">none</span>'
    return "".join(
        f'<span class="mini"><b>{_esc(key)}</b>{_esc(value)}</span>'
        for key, value in sorted(values.items())
    )


def _render_tags(values: list[Any] | None, *, limit: int | None = None) -> str:
    values = values or []
    if not values:
        return '<span class="muted">none</span>'
    shown = values if limit is None else values[:limit]
    tags = "".join(f'<code>{_esc(value)}</code>' for value in shown)
    if limit is not None and len(values) > limit:
        tags += f'<span class="muted">+{len(values) - limit} more</span>'
    return tags


def _render_case_row(case: dict[str, Any]) -> str:
    status = str(case.get("status") or "unknown")
    row_class = f"status-{_status_class(status)}"
    gaps = case.get("gaps") or []
    refs = case.get("evidence_refs") or []
    source_refs = case.get("source_refs") or []
    evidence = list(dict.fromkeys([str(ref) for ref in refs + source_refs if ref]))
    return (
        f'<tr class="{row_class}">'
        f'<td><span class="status">{_esc(_status_label(status))}</span></td>'
        f'<td><b>{_esc(case.get("case_id"))}</b><span class="row-num">row {_esc(case.get("row") or "")}</span></td>'
        f'<td>{_esc(case.get("module"))}<small>{_esc(case.get("item"))}</small></td>'
        f'<td>{_esc(case.get("order_type") or "-")}</td>'
        f'<td>{_esc(case.get("fixture_id") or "-")}</td>'
        f'<td class="num">{_esc(case.get("ui_steps", 0))}</td>'
        f'<td class="num">{_esc(case.get("api_calls", 0))}</td>'
        f'<td>{_render_metric_map(case.get("api_assertions"))}</td>'
        f'<td>{_render_metric_map(case.get("verifier"))}</td>'
        f'<td class="tags">{_render_tags(gaps)}</td>'
        f'<td class="refs">{_render_tags(evidence, limit=4)}</td>'
        '</tr>'
    )


def render_coverage_html(coverage: dict[str, Any]) -> str:
    """Return a self-contained HTML coverage report.

    The renderer does not infer coverage. It displays the generated coverage
    JSON, so the report remains rebuildable from run artifacts.
    """

    counts = coverage.get("counts") or {}
    cases = coverage.get("cases") or []
    gaps = coverage.get("gaps") or []
    artifacts = coverage.get("artifacts") or {}
    case_rows = "".join(_render_case_row(case) for case in cases if isinstance(case, dict))

    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>Phase 5 Coverage</title>
<style>
  :root {{
    color-scheme: light;
    --bg: #f8fafc;
    --panel: #ffffff;
    --ink: #111827;
    --muted: #64748b;
    --line: #d8dee9;
    --pass: #16803c;
    --partial: #9a6700;
    --fail: #b42318;
    --error: #b54708;
    --skipped: #5b6472;
    --uncovered: #4f46e5;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 24px;
    background: var(--bg);
    color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 14px;
  }}
  header {{ margin-bottom: 18px; }}
  h1 {{ margin: 0 0 8px; font-size: 24px; letter-spacing: 0; }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 10px 18px; color: var(--muted); }}
  .summary, .gaps, .artifacts {{
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 14px;
    margin-bottom: 16px;
  }}
  .counts {{ display: flex; flex-wrap: wrap; gap: 8px; }}
  .count {{
    display: inline-flex;
    gap: 6px;
    align-items: center;
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 4px 10px;
    background: #fff;
  }}
  .count b {{ font-variant-numeric: tabular-nums; }}
  .status-pass {{ color: var(--pass); }}
  .status-partial {{ color: var(--partial); }}
  .status-fail {{ color: var(--fail); }}
  .status-error {{ color: var(--error); }}
  .status-skipped {{ color: var(--skipped); }}
  .status-uncovered {{ color: var(--uncovered); }}
  .gaps {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }}
  .gap-card {{
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 10px;
    background: #fbfcff;
  }}
  .gap-head {{ display: flex; justify-content: space-between; gap: 10px; margin-bottom: 8px; }}
  .gap-head span {{
    min-width: 24px;
    text-align: center;
    border-radius: 999px;
    background: #eef2ff;
    color: #3730a3;
    padding: 1px 7px;
  }}
  .gap-ids, .tags, .refs {{ display: flex; flex-wrap: wrap; gap: 4px; }}
  .artifacts {{ display: flex; flex-wrap: wrap; gap: 8px 14px; }}
  .artifacts span {{ display: inline-flex; gap: 6px; align-items: center; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); }}
  th, td {{ padding: 8px 10px; border-bottom: 1px solid var(--line); vertical-align: top; text-align: left; }}
  th {{ color: var(--muted); font-weight: 600; background: #f1f5f9; position: sticky; top: 0; }}
  tbody tr:hover {{ background: #f8fafc; }}
  td small {{ display: block; color: var(--muted); margin-top: 3px; }}
  .status {{ font-weight: 700; }}
  .row-num {{ display: block; color: var(--muted); font-size: 12px; margin-top: 2px; }}
  .num {{ font-variant-numeric: tabular-nums; text-align: right; }}
  code {{
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12px;
    background: #eef2f7;
    border: 1px solid #d9e1ec;
    border-radius: 4px;
    padding: 1px 4px;
    word-break: break-word;
  }}
  .mini {{
    display: inline-flex;
    gap: 4px;
    margin: 0 4px 4px 0;
    border: 1px solid var(--line);
    border-radius: 4px;
    padding: 2px 5px;
  }}
  .mini b {{ color: var(--muted); }}
  .muted, .empty {{ color: var(--muted); }}
  @media (max-width: 900px) {{
    body {{ padding: 12px; }}
    table {{ display: block; overflow-x: auto; white-space: nowrap; }}
    td small {{ white-space: normal; }}
  }}
</style>
</head>
<body>
<header>
  <h1>Phase 5 Coverage</h1>
  <div class="meta">
    <span>schema <code>{_esc(coverage.get("schema"))}</code></span>
    <span>run <code>{_esc(coverage.get("run_id"))}</code></span>
    <span>cases <code>{_esc(coverage.get("cases_yaml"))}</code></span>
  </div>
</header>
<section class="summary"><div class="counts">{_render_counts(counts)}</div></section>
<section class="gaps">{_render_gap_groups(gaps)}</section>
<section class="artifacts">{_render_artifacts(artifacts)}</section>
<main>
  <table>
    <thead>
      <tr>
        <th>Status</th><th>Case</th><th>Module / Item</th><th>Order</th>
        <th>Fixture</th><th>UI</th><th>API</th><th>API Assertions</th>
        <th>Verifier</th><th>Gaps</th><th>Evidence</th>
      </tr>
    </thead>
    <tbody>{case_rows}</tbody>
  </table>
</main>
</body>
</html>"""


def write_coverage_html(coverage: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_coverage_html(coverage), encoding="utf-8")
    return path
