"""run_trace.py — 把 run 产物拼成执行瀑布流（docs/run-execution-trace.md, P1）。

纯函数，喂 `service.detail(run_id)` 的 dict，吐归一化 span 树 + 提速分析聚合，作为
实时页「执行时序」面板的唯一数据源。三层时序原料全来自现有产物，零新埋点：

- run 阶段 / t0：`detail["result"]` 的 `timestamps` + `lock_phase`（service._platform_result）
- 用例条：`detail["phase2"]["cases"]` 的 `started_at` + `duration_ms`（CaseOutcome）
- 动作条：每个 case 的 `actions[]`，动作起点 = `recorded_at − elapsed_ms`（ActionResult）

终态读 phase2（result.json，含完整 actions[]）；run 仍在跑（无 result.json）时退回
`detail["progress"]` 仅按用例状态出条，timing 留空，标 partial=True。fail-open：任何字段缺失
都降级而非抛错——trace 是观测面，绝不能拖垮 run 详情接口。

与同包 `autophone/execution/engine/report.py` 的职责边界：`report.py` 在 run 结束时写
`result.json` 与 `report.md`，是 run 产物的**生产者**；本模块只读已有产物投影出时序视图，
不写任何文件、不参与执行。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

SCHEMA_VERSION = "v1"
_SLOW_CASES_TOPN = 6
_SLOW_ACTIONS_TOPN = 8


def _epoch_ms(value: Any) -> float | None:
    """ISO 时间串 → epoch 毫秒。None/空/不可解析一律 None（调用方降级），绝不抛。"""
    if value in (None, ""):
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp() * 1000.0
    except ValueError:
        return None


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """最近秩百分位（nearest-rank）。sorted_vals 已升序且非空。"""
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = max(1, min(len(sorted_vals), round(pct / 100.0 * len(sorted_vals))))
    return sorted_vals[rank - 1]


def _status_of_case(raw: str) -> str:
    """runner 吐 pass/fail/error/skipped；progress 也可能给 executing/pending。统一归一。"""
    raw = str(raw or "")
    if raw in ("pass", "passed"):
        return "pass"
    if raw in ("fail", "failed"):
        return "fail"
    if raw in ("executing", "running"):
        return "running"
    if raw in ("error", "skipped", "pending"):
        return raw
    return raw or "pending"


def _action_span(action: dict[str, Any], t0: float | None) -> dict[str, Any]:
    elapsed = action.get("elapsed_ms")
    elapsed = float(elapsed) if isinstance(elapsed, (int, float)) else 0.0
    end = _epoch_ms(action.get("recorded_at"))
    # 动作起点 = 结束时刻 − 时长（recorded_at 是 _record 收尾打的，见 actions.py:311）
    start_ms = None
    if end is not None and t0 is not None:
        start_ms = _round(end - elapsed - t0)
    # 子段（trace P2）：screenshot/ocr/enhance/dispatch/settle 等，看穿 elapsed_ms。
    spans = [s for s in (action.get("spans") or []) if isinstance(s, dict)]
    return {
        "name": action.get("action") or "",
        "level": "action",
        "start_ms": start_ms,
        "dur_ms": _round(elapsed),
        "status": "pass" if action.get("ok") else "fail",
        "meta": {
            "detail": action.get("detail") or "",
            "kind": action.get("kind") or "",
        },
        "spans": [
            {"name": s.get("name") or "", "ms": _round(s.get("ms")), "count": int(s.get("count") or 0)}
            for s in spans
        ],
    }


def _case_span(case: dict[str, Any], t0: float | None) -> dict[str, Any]:
    actions = [a for a in (case.get("actions") or []) if isinstance(a, dict)]
    children = [_action_span(a, t0) for a in actions]
    dur = case.get("duration_ms")
    dur = float(dur) if isinstance(dur, (int, float)) else 0.0
    start_ms = None
    case_start = _epoch_ms(case.get("started_at"))
    if case_start is not None and t0 is not None:
        start_ms = _round(case_start - t0)
    else:
        # 老 run 无 started_at：用首个有定位的动作起点兜底
        anchored = [c["start_ms"] for c in children if c["start_ms"] is not None]
        if anchored:
            start_ms = min(anchored)
    return {
        "name": case.get("id") or "",
        "level": "case",
        "start_ms": start_ms,
        "dur_ms": _round(dur),
        "status": _status_of_case(case.get("status")),
        "meta": {
            "item": case.get("item") or "",
            "exec_via": case.get("exec_via") or "",
        },
        "children": children,
    }


def _phase_spans(lock_phase: dict[str, Any], t0: float | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name in ("provision", "execute", "collect"):
        seg = lock_phase.get(name)
        if not isinstance(seg, dict):
            continue
        start = _epoch_ms(seg.get("started_at"))
        end = _epoch_ms(seg.get("ended_at"))
        start_ms = _round(start - t0) if (start is not None and t0 is not None) else None
        dur_ms = _round(end - start) if (start is not None and end is not None) else None
        out.append({
            "name": name,
            "level": "phase",
            "start_ms": start_ms,
            "dur_ms": dur_ms,
            "status": seg.get("status") or "",
            "meta": {},
        })
    return out


def _verb_breakdown(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[float]] = {}
    for a in actions:
        elapsed = a.get("elapsed_ms")
        if not isinstance(elapsed, (int, float)):
            continue
        buckets.setdefault(a.get("action") or "", []).append(float(elapsed))
    rows = []
    for verb, vals in buckets.items():
        vals.sort()
        rows.append({
            "verb": verb,
            "count": len(vals),
            "total_ms": _round(sum(vals)),
            "p50_ms": _round(_percentile(vals, 50)),
            "p95_ms": _round(_percentile(vals, 95)),
        })
    rows.sort(key=lambda r: r["total_ms"] or 0, reverse=True)
    return rows


def _span_breakdown(all_actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把所有动作的子段（trace P2）按名汇总：screenshot/ocr/enhance/dispatch/settle 各占多少。
    这是「看穿黑盒」的最终产出——直接回答耗时大头在截图/识别/派发还是 settle 轮询。
    再补一行「其他」= Σelapsed − Σ子段，即未埋点剩余（wait 轮询 sleep、find_text 计算等）。"""
    buckets: dict[str, list[float]] = {}
    order: list[str] = []
    instrumented = 0.0
    action_total = 0.0
    for a in all_actions:
        el = a.get("elapsed_ms")
        if isinstance(el, (int, float)):
            action_total += float(el)
        for s in a.get("spans") or []:
            if not isinstance(s, dict):
                continue
            name = s.get("name") or ""
            ms = s.get("ms")
            if not isinstance(ms, (int, float)):
                continue
            if name not in buckets:
                buckets[name] = [0.0, 0]
                order.append(name)
            buckets[name][0] += float(ms)
            buckets[name][1] += int(s.get("count") or 0)
            instrumented += float(ms)
    rows = [
        {"name": n, "total_ms": _round(buckets[n][0]), "count": int(buckets[n][1])}
        for n in order
    ]
    rows.sort(key=lambda r: r["total_ms"] or 0, reverse=True)
    other = round(action_total - instrumented, 1)
    if other > 0 and rows:  # 仅在确有埋点时补「其他」，否则全是未埋点没意义
        rows.append({"name": "其他", "total_ms": other, "count": 0})
    return rows


def _max_gap(case_spans: list[dict[str, Any]]) -> dict[str, Any] | None:
    """用例内相邻动作之间的最大空档（idle/未落账开销）。"""
    best: dict[str, Any] | None = None
    for cs in case_spans:
        segs = [
            (c["start_ms"], c["start_ms"] + (c["dur_ms"] or 0), c["name"])
            for c in cs["children"] if c["start_ms"] is not None
        ]
        segs.sort()
        for (prev_start, prev_end, prev_name), (next_start, _ne, _nn) in zip(segs, segs[1:]):
            gap = round(next_start - prev_end, 1)
            if gap > 0 and (best is None or gap > best["gap_ms"]):
                best = {"case_id": cs["name"], "after_action": prev_name, "gap_ms": gap}
    return best


def _headline(
    *,
    execute_ms: float | None,
    sum_action_ms: float | None,
    overhead_ms: float | None,
    overhead_pct: float | None,
    span_breakdown: list[dict[str, Any]],
    slowest_actions: list[dict[str, Any]],
) -> str | None:
    """把耗时明细压成一句结论，回答「本次瓶颈是什么」，不必让人自己心算瀑布图。

    优先级：单个动作跑出离谱大头（远超其余同类）> 某个子段（截图/OCR/settle）占比过半
    > 间隙（未埋点 overhead）过半 > 无明显瓶颈（分布均匀）。阈值都是经验值，只为挑出
    「值得先看」的一条，不追求精确归因。"""
    if not execute_ms:
        return None
    top_action = slowest_actions[0] if slowest_actions else None
    if top_action and isinstance(top_action.get("elapsed_ms"), (int, float)):
        pct = round(top_action["elapsed_ms"] / execute_ms * 100.0, 1)
        if pct >= 25:
            detail = f"（{top_action['detail']}）" if top_action.get("detail") else ""
            return (
                f"本次瓶颈：单个 {top_action['action']} 动作耗时 {top_action['elapsed_ms']:.0f}ms"
                f"{detail}，占执行总时长 {pct}%"
            )
    top_span = next((s for s in span_breakdown if s.get("name") != "其他"), None)
    if top_span and sum_action_ms:
        pct = round((top_span.get("total_ms") or 0) / sum_action_ms * 100.0, 1)
        if pct >= 40:
            return (
                f"本次瓶颈：{top_span['name']} 环节累计 {top_span['total_ms']:.0f}ms，"
                f"占动作总耗时 {pct}%"
            )
    if overhead_pct is not None and overhead_pct >= 30:
        return f"本次耗时 {overhead_pct}% 落在动作之外的空档（{overhead_ms:.0f}ms 未被埋点覆盖）"
    return "本次耗时分布均匀，无单点瓶颈"


def _summary(
    *,
    phase_spans: list[dict[str, Any]],
    case_spans: list[dict[str, Any]],
    all_actions: list[dict[str, Any]],
    wall_ms: float | None,
) -> dict[str, Any]:
    execute_ms = next(
        (p["dur_ms"] for p in phase_spans if p["name"] == "execute" and p["dur_ms"]), None
    )
    if execute_ms is None:
        execute_ms = _round(sum((c["dur_ms"] or 0) for c in case_spans)) or None
    sum_action_ms = _round(
        sum(float(a["elapsed_ms"]) for a in all_actions if isinstance(a.get("elapsed_ms"), (int, float)))
    )
    overhead_ms = None
    if execute_ms and sum_action_ms is not None:
        overhead_ms = _round(max(0.0, execute_ms - sum_action_ms))

    def _pct(part: float | None, whole: float | None) -> float | None:
        if not whole or part is None:
            return None
        return round(part / whole * 100.0, 1)

    slowest_cases = sorted(
        (c for c in case_spans if c["dur_ms"]), key=lambda c: c["dur_ms"], reverse=True
    )[:_SLOW_CASES_TOPN]
    slowest_actions = sorted(
        (a for a in all_actions if isinstance(a.get("elapsed_ms"), (int, float))),
        key=lambda a: a["elapsed_ms"], reverse=True,
    )[:_SLOW_ACTIONS_TOPN]
    span_breakdown = _span_breakdown(all_actions)
    slowest_actions_rows = [
        {"action": a.get("action") or "", "elapsed_ms": _round(float(a["elapsed_ms"])),
         "detail": a.get("detail") or "", "ok": bool(a.get("ok"))}
        for a in slowest_actions
    ]
    overhead_pct = _pct(overhead_ms, execute_ms)
    return {
        "wall_ms": _round(wall_ms),
        "execute_ms": execute_ms,
        "execute_pct": _pct(execute_ms, wall_ms),
        "sum_action_ms": sum_action_ms,
        "overhead_ms": overhead_ms,
        "overhead_pct": overhead_pct,
        "case_count": len(case_spans),
        "action_count": len(all_actions),
        "verbs": _verb_breakdown(all_actions),
        "span_breakdown": span_breakdown,
        "slowest_cases": [
            {"id": c["name"], "item": c["meta"]["item"], "dur_ms": c["dur_ms"], "status": c["status"]}
            for c in slowest_cases
        ],
        "slowest_actions": slowest_actions_rows,
        "max_gap": _max_gap(case_spans),
        "headline": _headline(
            execute_ms=execute_ms,
            sum_action_ms=sum_action_ms,
            overhead_ms=overhead_ms,
            overhead_pct=overhead_pct,
            span_breakdown=span_breakdown,
            slowest_actions=slowest_actions_rows,
        ),
    }


def build_trace(detail: dict[str, Any]) -> dict[str, Any]:
    """run 详情 → 执行 Trace（spans 树 + summary）。任何缺失字段都降级，绝不抛。"""
    result = detail.get("result") or {}
    phase2 = detail.get("phase2") or {}
    progress = detail.get("progress") or {}
    run_id = detail.get("run_id") or result.get("run_id") or ""

    timestamps = result.get("timestamps") or {}
    lock_phase = result.get("lock_phase") or {}
    # t0：排队时刻优先，退回 provision 起点
    t0 = _epoch_ms(timestamps.get("queued_at")) or _epoch_ms(
        (lock_phase.get("provision") or {}).get("started_at")
    )
    end_epoch = _epoch_ms(timestamps.get("collect_ended_at"))
    wall_ms = (end_epoch - t0) if (end_epoch is not None and t0 is not None) else None

    cases_raw = phase2.get("cases")
    partial = not isinstance(cases_raw, list) or not cases_raw
    if partial:
        # run 仍在跑：无 result.json，按 progress 仅出用例状态条（无 timing）
        prog_cases = progress.get("cases") if isinstance(progress.get("cases"), list) else []
        case_spans = [
            {
                "name": c.get("id") or "", "level": "case", "start_ms": None,
                "dur_ms": _round(c.get("duration_ms")) if isinstance(c.get("duration_ms"), (int, float)) else None,
                "status": _status_of_case(c.get("status")),
                "meta": {"item": c.get("item") or "", "exec_via": ""}, "children": [],
            }
            for c in prog_cases if isinstance(c, dict)
        ]
    else:
        case_spans = [_case_span(c, t0) for c in cases_raw if isinstance(c, dict)]

    phase_spans = _phase_spans(lock_phase, t0)
    all_actions: list[dict[str, Any]] = []
    if not partial:
        for c in cases_raw:
            if isinstance(c, dict):
                all_actions.extend(a for a in (c.get("actions") or []) if isinstance(a, dict))

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "t0": timestamps.get("queued_at") or (lock_phase.get("provision") or {}).get("started_at"),
        "wall_ms": _round(wall_ms),
        "partial": partial,
        "phases": phase_spans,
        "cases": case_spans,
        "summary": _summary(
            phase_spans=phase_spans, case_spans=case_spans,
            all_actions=all_actions, wall_ms=wall_ms,
        ),
    }


# ── 跨 run 趋势（trace P3，docs/run-execution-trace.md §八 P3）────────────────
_TREND_REGRESS_RATIO = 1.3   # 最近一次 > 历史基线 × 此值 才算变慢（滤抖动）
_TREND_REGRESS_FLOOR_MS = 800.0  # 且绝对值要够大，免得小用例百分比噪声误报


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def build_trend(traces: list[dict[str, Any]], *, app: str = "") -> dict[str, Any]:
    """一组 run 的 trace（build_trace 输出）→ 跨 run 趋势。回答「这次比上次慢在哪」。

    入参顺序无所谓——内部按 t0 升序排（旧→新）便于画趋势线。partial（仍在跑、无 timing）
    的 trace 跳过。fail-open：任何缺字段降级，绝不抛。产出：
      - runs[]：每 run 墙钟/execute 占比/过败计数（时间序列）
      - cases[]：每 case_id 历次 dur 采样 + p50/p95 + 最近值 + 基线 + 变慢判定
      - verbs[]：每动词 p50/p95 漂移序列
      - regressions[]：最近一次显著变慢的用例子集（cases 里 regressed=True 的）
    """
    usable = [t for t in traces if isinstance(t, dict) and not t.get("partial")]
    usable.sort(key=lambda t: _epoch_ms(t.get("t0")) or 0.0)

    runs: list[dict[str, Any]] = []
    case_samples: dict[str, dict[str, Any]] = {}
    case_order: list[str] = []
    verb_series: dict[str, dict[str, list[float]]] = {}

    for tr in usable:
        run_id = tr.get("run_id") or ""
        summary = tr.get("summary") or {}
        cases = tr.get("cases") or []
        passed = sum(1 for c in cases if c.get("status") == "pass")
        failed = sum(1 for c in cases if c.get("status") in ("fail", "error"))
        runs.append({
            "run_id": run_id,
            "t0": tr.get("t0"),
            "wall_ms": tr.get("wall_ms"),
            "execute_ms": summary.get("execute_ms"),
            "execute_pct": summary.get("execute_pct"),
            "overhead_pct": summary.get("overhead_pct"),
            "pass": passed,
            "fail": failed,
            "total": len(cases),
        })
        for c in cases:
            cid = c.get("name") or ""
            dur = c.get("dur_ms")
            if not cid or not isinstance(dur, (int, float)):
                continue
            if cid not in case_samples:
                case_samples[cid] = {"id": cid, "item": (c.get("meta") or {}).get("item") or "", "samples": []}
                case_order.append(cid)
            # item 用最新一条的（升序遍历，后者覆盖）
            if (c.get("meta") or {}).get("item"):
                case_samples[cid]["item"] = c["meta"]["item"]
            case_samples[cid]["samples"].append(
                {"run_id": run_id, "dur_ms": _round(float(dur)), "status": c.get("status")}
            )
        for v in summary.get("verbs") or []:
            verb = v.get("verb") or ""
            if not verb:
                continue
            ser = verb_series.setdefault(verb, {"p50": [], "p95": []})
            if isinstance(v.get("p50_ms"), (int, float)):
                ser["p50"].append(float(v["p50_ms"]))
            if isinstance(v.get("p95_ms"), (int, float)):
                ser["p95"].append(float(v["p95_ms"]))

    cases_out: list[dict[str, Any]] = []
    for cid in case_order:
        rec = case_samples[cid]
        durs = [s["dur_ms"] for s in rec["samples"] if isinstance(s["dur_ms"], (int, float))]
        if not durs:
            continue
        last = durs[-1]
        prior = durs[:-1]
        baseline = _median(prior)  # 历史基线（不含本次）
        regressed = bool(
            baseline is not None
            and last > baseline * _TREND_REGRESS_RATIO
            and last > _TREND_REGRESS_FLOOR_MS
        )
        delta_pct = _round((last - baseline) / baseline * 100.0) if baseline else None
        cases_out.append({
            "id": cid,
            "item": rec["item"],
            "samples": rec["samples"],
            "n": len(durs),
            "p50_ms": _round(_percentile(sorted(durs), 50)),
            "p95_ms": _round(_percentile(sorted(durs), 95)),
            "last_ms": _round(last),
            "baseline_ms": _round(baseline),
            "delta_pct": delta_pct,
            "regressed": regressed,
        })
    cases_out.sort(key=lambda c: c["last_ms"] or 0, reverse=True)

    verbs_out = [
        {
            "verb": verb,
            "p50_trend": [_round(x) for x in ser["p50"]],
            "p95_trend": [_round(x) for x in ser["p95"]],
            "last_p50_ms": _round(ser["p50"][-1]) if ser["p50"] else None,
            "last_p95_ms": _round(ser["p95"][-1]) if ser["p95"] else None,
        }
        for verb, ser in verb_series.items()
    ]
    verbs_out.sort(key=lambda v: v["last_p95_ms"] or 0, reverse=True)

    return {
        "schema_version": SCHEMA_VERSION,
        "app": app,
        "run_count": len(runs),
        "runs": runs,
        "cases": cases_out,
        "regressions": [c for c in cases_out if c["regressed"]],
        "verbs": verbs_out,
    }
