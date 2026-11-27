"""run_regression.py — M1 entry point (docs §十).

    python -m tools.phase2.run_regression --app my_app --cases cases.yaml --filter ui_ready
    python -m tools.phase2.run_regression --app my_app --cases cases.yaml --ids R125
    python -m tools.phase2.run_regression --app my_app --cases cases.yaml --ids R125 --sock /path/to/vphone.sock

Gates on device routability, runs the selected step-scripts, writes
runs/<ts>/{result.json,report.md}. Exit code: 0 only if the selection was
non-empty and every selected case actually executed with no fail/error/skip
and the run wasn't aborted; else 1 — so a typoed --ids, a dropped script
registration, or an empty selection reds the run instead of greening on nothing.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

from autophone.paths import STATE_ROOT

from autophone.logging import (
    bind_run, run_logfile, setup_logging, verbosity_to_level,
)
from autophone.execution.engine.report import write_reports
from autophone.execution.engine.runner import run

RUNS_DIR = STATE_ROOT / "runs" / "phase2"
DEPRECATION_NOTICE = (
    "DEPRECATION: tools.phase2.run_regression is a legacy compatibility entry. "
    "Prefer `uv run --group platform python3 -m autophone.cli --app <app> run "
    "[--tag <automation>|--ids ...]` for platform runs. This entry remains for "
    "migration-only direct Phase 2 runner checks."
)

# 需要 host 侧增强 OCR（magick）的用例：浅灰文字 + 水印页裸 OCR=0box，靠灰阶拉伸再识别。
# 见 tools/phase2/engine/actions.py 的 assert_present_enhanced / assert_region_nonempty。
# R127=协议规则验证（2026-07 目录整编前的旧编号 R71，用例本体未变）。
ENHANCED_OCR_CASES = {"R127"}


def preflight(selected_ids: set[str]) -> str | None:
    """运行前的 host 依赖自检。返回 None 表示通过，否则返回中止原因。
    只有当选中用例真的需要增强 OCR 时才要求 magick——避免给不相干的跑加无谓门槛。"""
    if selected_ids & ENHANCED_OCR_CASES and shutil.which("magick") is None:
        from autophone.execution.engine.actions import enhanced_ocr_backend_available
        if enhanced_ocr_backend_available():
            return None
        need = sorted(selected_ids & ENHANCED_OCR_CASES)
        return (f"用例 {need} 需要增强 OCR，但未找到 magick (ImageMagick) 或 Pillow。"
                f"请先 `uv sync`，或安装 ImageMagick：brew install imagemagick"
                f"（也可从选择中排除这些用例）")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Regression runner")
    ap.add_argument("--filter", dest="automation", default=None,
                    choices=["ui_ready", "manual_checkpoint", "needs_fixture", "blocked_by_infra"],
                    help="run only cases with this automation class")
    ap.add_argument("--ids", nargs="+", default=None, help="run only these case ids")
    ap.add_argument("--cases", type=Path, required=True, help="cases YAML path")
    ap.add_argument("--sock", default=None, help="vphone.sock path (else env/auto)")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--out", type=Path, default=None, help="run output dir")
    ap.add_argument("-v", "--verbose", action="count", default=0,
                    help="日志级别：默认 INFO，-v 起 DEBUG")
    ap.add_argument("--app", required=True, help="App Profile slug")
    args = ap.parse_args()
    print(DEPRECATION_NOTICE, file=sys.stderr)

    setup_logging(verbosity_to_level(args.verbose))

    if not args.automation and not args.ids:
        args.automation = "ui_ready"  # M1 default scope

    _now = datetime.now()
    out_dir = args.out or (RUNS_DIR / _now.strftime("%Y%m%d") / _now.strftime("%Y%m%d-%H%M%S"))
    shot_dir = out_dir / "shots"

    # host 依赖自检（在驱动 VM 前）：选中用例若需增强 OCR 而 magick 缺失，明确中止，
    # 而不是等用例执行时打成 error——后者会让通过数静默变化、不可复现（review P2）。
    from autophone.execution.engine.runner import load_cases, select_cases
    try:
        selected = select_cases(load_cases(args.cases), automation=args.automation, ids=args.ids)
        selected_ids = {c.get("id") for c in selected}
    except OSError as e:
        print(f"FAIL: cannot read cases {args.cases}: {e}", file=sys.stderr)
        return 1
    from autophone.execution.app_recipes import import_app_recipes
    import_app_recipes(args.app)
    pf = preflight(selected_ids)
    if pf:
        print(f"PREFLIGHT FAIL: {pf}", file=sys.stderr)
        return 1

    # 旧入口也要带关联标签并落 run.log（与平台 service 路径一致，docs §十 兼容要求）。
    # run_id 用输出目录名（时间戳），run_logfile 据此隔离。
    with bind_run(out_dir.name), run_logfile(out_dir / "run.log"):
        result = run(args.cases, automation=args.automation, ids=args.ids,
                     sock_path=args.sock, shot_dir=shot_dir, timeout=args.timeout)
        json_path, md_path, html_path = write_reports(result, out_dir, cases_path=args.cases)

    if result.aborted:
        print(f"ABORTED at gate: {result.abort_reason}", file=sys.stderr)
        print(f"report: {md_path}")
        print(f"html:   {html_path}")
        return 1

    c = result.counts
    total = sum(c.values())
    print(f"pass={c['pass']} fail={c['fail']} error={c['error']} skipped={c['skipped']}")
    print(f"report: {md_path}")
    print(f"json:   {json_path}")
    print(f"html:   {html_path}")
    # 绿条件：选到了用例，且每个都真跑了（无 fail/error/skipped）。
    # skipped 表示该用例没注册 step-script —— 一次「绿」绝不能是因为什么都没跑。
    if total == 0:
        print("FAIL: no cases selected (check --ids / --filter)", file=sys.stderr)
        return 1
    if c["skipped"]:
        print(f"FAIL: {c['skipped']} selected case(s) skipped (no step-script "
              f"registered) — green requires the selection to actually execute",
              file=sys.stderr)
    return 0 if (c["fail"] == 0 and c["error"] == 0 and c["skipped"] == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
