from __future__ import annotations

import argparse

from autophone.adapters.cli import _common as common


def cmd_run_index_status(args: argparse.Namespace) -> int:
    from autophone.application import run_index

    run_root = args.out_root or (common.STATE_ROOT / "runs" / "platform")
    print(common.json.dumps(run_index.status(run_root), ensure_ascii=False, indent=2))
    return 0


def cmd_run_index_rebuild(args: argparse.Namespace) -> int:
    from autophone.application import run_index

    run_root = args.out_root or (common.STATE_ROOT / "runs" / "platform")
    report = run_index.rebuild(run_root, force=getattr(args, "force", False))
    print(common.json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 2


def register(sub: argparse._SubParsersAction) -> None:
    run_index = sub.add_parser("run-index", help="derived SQLite index over platform run results")
    run_index_sub = run_index.add_subparsers(dest="run_index_cmd", required=True)
    run_index_status = run_index_sub.add_parser("status")
    run_index_status.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    run_index_status.set_defaults(func=cmd_run_index_status)
    run_index_rebuild = run_index_sub.add_parser("rebuild")
    run_index_rebuild.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    run_index_rebuild.add_argument(
        "--force", action="store_true",
        help="即使扫不到任何 run 结果也重建（会丢掉现有索引行）",
    )
    run_index_rebuild.set_defaults(func=cmd_run_index_rebuild)
