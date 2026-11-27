from __future__ import annotations

import argparse

from autophone.adapters.cli import _common as common


def cmd_replay(args: argparse.Namespace) -> int:
    selector = {"ids": [args.case_id]}
    # BL-215：按 Run Spec 的 device 固定目标 Rig。
    handle = common._dispatcher(args).submit(
        common._regression_spec(args, selector), pin_device=True,
    )
    print(common.json.dumps(handle.summary(), ensure_ascii=False, indent=2))
    return 0 if handle.result.get("status") == "passed" else 1


def register(sub: argparse._SubParsersAction) -> None:
    replay = sub.add_parser("replay", help="replay a single regression case by id")
    replay.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    replay.add_argument("case_id")
    replay.add_argument("--record", action="store_true")
    replay.add_argument("--no-auto-login", dest="auto_login", action="store_false", default=True,
                        help="needs_login 时不自动补登（缺省补登）")
    replay.set_defaults(func=cmd_replay, needs_app=True)
