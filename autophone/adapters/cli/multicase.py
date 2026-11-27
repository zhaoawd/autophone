from __future__ import annotations

import argparse

from autophone.adapters.cli import _common as common


def cmd_multicase_metrics(args: argparse.Namespace) -> int:
    from autophone.execution.multicase_schedule import build_phase0_report

    selector = {"statuses": ["active"]}
    if args.automation:
        selector["tag"] = args.automation
    out = build_phase0_report(
        list(args.apps),
        run_root=args.out_root or (common.STATE_ROOT / "runs" / "platform"),
        actual_selector=selector,
        limit=args.limit,
    )
    print(common.json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


def register(sub: argparse._SubParsersAction) -> None:
    multicase = sub.add_parser("multicase", help="multi-case scheduling analysis and guardrails")
    multicase_sub = multicase.add_subparsers(dest="multicase_cmd", required=True)
    multicase_metrics = multicase_sub.add_parser("metrics", help="Phase 0 read-only multicase metrics")
    multicase_metrics.add_argument("--apps", nargs="+", required=True)
    multicase_metrics.add_argument("--automation", default="ui_ready")
    multicase_metrics.add_argument("--limit", type=int, default=20, help="recent run artifacts to scan per app")
    multicase_metrics.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    multicase_metrics.set_defaults(func=cmd_multicase_metrics)
