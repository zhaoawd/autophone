from __future__ import annotations

import argparse

from autophone.adapters.cli import _common as common


def cmd_failure_sample(args: argparse.Namespace) -> int:
    spec = common.failure_sample_spec(
        run_id=args.run_id,
        app=args.app,
        device=args.device,
        requested_by=args.requested_by,
    )
    handle = common.create_failure_sample(common._service(args), spec)
    print(common.json.dumps(handle.summary(), ensure_ascii=False, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    failure_sample = sub.add_parser("failure-sample")
    failure_sample.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    failure_sample.set_defaults(func=cmd_failure_sample, needs_app=True)
