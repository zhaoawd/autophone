from __future__ import annotations

import argparse

from autophone.adapters.cli import _common as common


def cmd_report(args: argparse.Namespace) -> int:
    run_root = args.out_root or (common.STATE_ROOT / "runs" / "platform")
    if args.last:
        reports = sorted(run_root.glob("*/*/report.html"), key=lambda p: p.stat().st_mtime)
        if not reports:
            print(f"no platform report found under {run_root}", file=common.sys.stderr)
            return 1
        print(str(reports[-1]))
        return 0
    print(str(run_root))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    report = sub.add_parser("report", help="locate platform run reports")
    report.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    report.add_argument("--last", action="store_true")
    report.set_defaults(func=cmd_report)
