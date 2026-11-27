"""断言回放门禁第一步（零 AI）：draft API 断言 × 历史抓包 → 三分类（BL-071）。

来源：docs/platform-human-review-two-stage.md §7.1。对每条 draft 断言：按 app + case 查找历史
抓包（`runs/**/planes/api/capture.jsonl`），装载该 case 的 fixture 上下文，执行确定性引擎
`api_oracle.evaluate_case`，自动分为三类：

| 类别（category） | 含义 | 处置 |
| --- | --- | --- |
| replay_pass_candidate | 至少一个归因窗口回放 pass | 通过回放的候选；人工确认是否保留 |
| attribution_or_fixture_queue | 有归因调用但全部窗口 fail | 待归因 / 待修 fixture 队列；本步骤不调用 AI |
| no_attributed_capture | 同 app 下没有 case_id 归因到该断言的调用 | 转「待补证据」需求：先查旧产物能否补归因，否则带抓包重跑 |

不变量：
- **按 app 隔离归因**（§4.2、§4.6 测量错误二）：run 的 app 由 `platform-run-result.json` →
  `run-spec.json` → `result.json.meta` 依次取；取不到的 run 记入 `unattributed_run_dirs`，不参与
  任何 app 的归因，也不按目录名猜。
- **逐 case 提供归因后的调用窗口 + fixture 上下文**（测量错误一）：每个 run 是一个窗口，只把
  `case_id` 相同的调用交给 evaluate_case；`${var}` 缺失由引擎判 unresolved_var，不静默放行。
- **零 AI**：本模块不 import `autophone.application.ai`，报告 `ai_used=false`。
- 只读：不改断言资产、不改 run 产物。

`url_glob_hits` 是 §4.6 说的弱信号（不依赖 case_id、只依赖 app 分区的 URL 命中数），只用于
提示「归因能否修复」，不参与分类。
"""
from __future__ import annotations

import fnmatch
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from autophone.observation.frida.api_oracle import (
    api_status,
    evaluate_case,
    flat_fixture_vars,
    normalize_expectations,
)

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - PyYAML 缺席时读 JSON 仍可用
    yaml = None  # type: ignore[assignment]

CATEGORY_REPLAY_PASS = "replay_pass_candidate"
CATEGORY_TRIAGE_QUEUE = "attribution_or_fixture_queue"
CATEGORY_NO_EVIDENCE = "no_attributed_capture"
CATEGORIES: tuple[str, ...] = (CATEGORY_REPLAY_PASS, CATEGORY_TRIAGE_QUEUE, CATEGORY_NO_EVIDENCE)

DISPOSITIONS = {
    CATEGORY_REPLAY_PASS: "通过回放的候选：人工确认该断言是否需要保留（目标业务前提是否成立由人判断）",
    CATEGORY_TRIAGE_QUEUE: "待归因 / 待修 fixture 队列：本步骤不调用 AI，分诊留到校准完成之后（§7.3）",
    CATEGORY_NO_EVIDENCE: "无可归因抓包：不进人审队列，转待补证据需求；先检查旧产物能否补 app/case 归因，否则带抓包重新执行",
}

CAPTURE_REL = ("planes", "api", "capture.jsonl")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def run_app(run_dir: Path) -> str | None:
    """run 目录归属的 app。三处元数据依次取，都取不到返回 None（不按目录名猜）。"""
    doc = _read_json(run_dir / "platform-run-result.json")
    if doc and doc.get("app"):
        return str(doc["app"])
    doc = _read_json(run_dir / "run-spec.json")
    if doc:
        spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
        app = doc.get("app") or spec.get("app")
        if app:
            return str(app)
    doc = _read_json(run_dir / "result.json")
    if doc and isinstance(doc.get("meta"), dict) and doc["meta"].get("app"):
        return str(doc["meta"]["app"])
    return None


def _run_id(run_dir: Path) -> str:
    for name, key in (("platform-run-result.json", "run_id"), ("run-spec.json", "run_id")):
        doc = _read_json(run_dir / name)
        if doc and doc.get(key):
            return str(doc[key])
    doc = _read_json(run_dir / "result.json")
    if doc and isinstance(doc.get("meta"), dict) and doc["meta"].get("run_id"):
        return str(doc["meta"]["run_id"])
    return run_dir.name


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict):
            out.append(doc)
    return out


def collect_app_captures(app: str, run_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """枚举 run_root 下全部抓包窗口，只保留归属 app 的；返回 (windows, unattributed_run_dirs)。

    window = {run_dir, run_id, calls}；calls 是该 run 的全部抓包记录（含未归因 case 的调用，
    供 url_glob 弱信号用），按 case 的切分在 classify 里做。
    """
    windows: list[dict[str, Any]] = []
    unattributed: list[str] = []
    if not run_root.exists():
        return windows, unattributed
    for capture in sorted(run_root.rglob("capture.jsonl")):
        if tuple(capture.parts[-3:]) != CAPTURE_REL:
            continue
        run_dir = capture.parents[2]
        owner = run_app(run_dir)
        if owner is None:
            unattributed.append(str(run_dir))
            continue
        if owner != app:
            continue
        windows.append({"run_dir": run_dir, "run_id": _run_id(run_dir), "calls": _read_jsonl(capture)})
    return windows, unattributed


def load_expectation_entries(path: Path, statuses: tuple[str, ...] | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """读断言资产原文 → (doc, 按 status 过滤后的条目列表，保留 provenance/revision/title)。"""
    if not path.exists():
        return {}, []
    if path.suffix.lower() == ".json":
        doc = json.loads(path.read_text(encoding="utf-8"))
    else:
        if yaml is None:
            raise RuntimeError(f"PyYAML is required to read expectations from {path}")
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        return {}, []
    exps = doc.get("expectations")
    if not isinstance(exps, list):
        # legacy map 形没有 status；当作全部条目
        return doc, [
            {"id": k, "status": None, **(v if isinstance(v, dict) else {})}
            for k, v in doc.items() if k != "meta"
        ]
    entries = [
        e for e in exps
        if isinstance(e, dict) and e.get("id") and (statuses is None or e.get("status") in statuses)
    ]
    return doc, entries


def _url_glob_hits(entry: dict[str, Any], calls: list[dict[str, Any]]) -> int:
    hits = 0
    rules = [r for r in (entry.get("expect") or []) if isinstance(r, dict)]
    for call in calls:
        url = str(call.get("url") or "")
        method = str(call.get("method") or "").upper()
        for rule in rules:
            match = rule.get("match") or {}
            glob = match.get("url_glob")
            want_method = str(match.get("method") or "").upper()
            if glob and fnmatch.fnmatch(url, str(glob)) and (not want_method or want_method == method):
                hits += 1
                break
    return hits


def classify_entry(
    entry: dict[str, Any],
    windows: list[dict[str, Any]],
    expectations: dict[str, Any],
    ctx: dict[str, Any],
) -> dict[str, Any]:
    """单条断言的三分类。windows 必须已按 app 隔离。"""
    case_id = str(entry["id"])
    considered: list[dict[str, Any]] = []
    for window in windows:
        case_calls = [c for c in window["calls"] if c.get("case_id") == case_id]
        if not case_calls:
            continue
        results = evaluate_case(case_id, case_calls, expectations, ctx=ctx)
        fail_reasons = Counter(str(r.get("reason")) for r in results if r.get("verdict") == "fail")
        considered.append({
            "run_id": window["run_id"],
            "run_dir": str(window["run_dir"]),
            "attributed_calls": len(case_calls),
            "api_status": api_status(results),
            "assertions": len(results),
            "fail_reasons": dict(sorted(fail_reasons.items())),
        })
    if not considered:
        category = CATEGORY_NO_EVIDENCE
    elif any(w["api_status"] == "pass" for w in considered):
        category = CATEGORY_REPLAY_PASS
    else:
        category = CATEGORY_TRIAGE_QUEUE
    row: dict[str, Any] = {
        "id": case_id,
        "title": entry.get("title"),
        "status": entry.get("status"),
        "revision": entry.get("revision"),
        "provenance": entry.get("provenance"),
        "category": category,
        "disposition": DISPOSITIONS[category],
        "fixture_vars": sorted(ctx),
        "windows": considered,
    }
    if category == CATEGORY_NO_EVIDENCE:
        all_calls = [c for w in windows for c in w["calls"]]
        row["url_glob_hits"] = _url_glob_hits(entry, all_calls)
    return row


def classify_app_drafts(
    app: str,
    *,
    run_root: Path,
    statuses: tuple[str, ...] | None = ("draft",),
    expectations_path: Path | None = None,
    fixtures_path: Path | None = None,
) -> dict[str, Any]:
    """对 app 的断言资产（默认只取 draft）做零 AI 回放三分类，返回自包含报告 dict。

    expectations_path / fixtures_path 缺省从 app profile 解析（`apps/<app>/api_expectations.yaml`、
    `apps/<app>/api_fixtures.yaml`）；测试可显式传入。
    """
    if expectations_path is None or fixtures_path is None:
        from autophone.assets.apps import load_app_profile

        profile = load_app_profile(app)
        expectations_path = expectations_path or profile.api_expectations_path()
        fixtures_path = fixtures_path or profile.api_fixtures_path()

    doc, entries = load_expectation_entries(expectations_path, statuses)
    expectations = normalize_expectations(doc, statuses=statuses)
    windows, unattributed = collect_app_captures(app, run_root)
    fixtures_src: Path | None = fixtures_path if fixtures_path.exists() else None

    rows = []
    for entry in entries:
        ctx = flat_fixture_vars(fixtures_src, str(entry["id"]))
        rows.append(classify_entry(entry, windows, expectations, ctx))

    counts = Counter(row["category"] for row in rows)
    return {
        "schema_version": "v1",
        "kind": "api-assertion-replay",
        "app": app,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "ai_used": False,
        "statuses": list(statuses) if statuses is not None else None,
        "expectations_path": str(expectations_path),
        "fixtures_path": str(fixtures_path),
        "run_root": str(run_root),
        "capture_windows": len(windows),
        "unattributed_run_dirs": unattributed,
        "counts": {category: counts.get(category, 0) for category in CATEGORIES},
        "entries": rows,
    }
