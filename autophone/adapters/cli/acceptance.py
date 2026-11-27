from __future__ import annotations

import argparse
from typing import Any

from autophone.adapters.cli import _common as common


def cmd_acceptance_run(args: argparse.Namespace) -> int:
    from autophone.application import acceptance

    acceptance_id = args.run_id or common._run_id("platform-acceptance")
    spec: dict[str, Any] = {
        "schema_version": "v1",
        "acceptance_id": acceptance_id,
        "app": args.app,
        "device": args.device,
        "checks": list(args.checks),
    }
    if "regression" in args.checks:
        spec["regression"] = {"case_ids": list(args.ids), "api_mode": args.api}
    if "verification" in args.checks:
        spec["verification"] = {"path": args.verification_path}
    if "location" in args.checks:
        spec["location"] = {
            "latitude": args.location_lat,
            "longitude": args.location_lng,
        }
    run_root = args.out_root or (common.STATE_ROOT / "runs" / "platform")
    handle = acceptance.run_acceptance(
        spec,
        out_root=args.acceptance_root,
        submit_run=common.RunService(run_root=run_root).submit,
    )
    print(common.json.dumps({
        "acceptance_id": handle.acceptance_id,
        "status": handle.status,
        "run_dir": str(handle.run_dir),
        "checks": handle.result.get("checks") or [],
    }, ensure_ascii=False, indent=2))
    return 0 if handle.status == "passed" else 1


def register(sub: argparse._SubParsersAction) -> None:
    acceptance = sub.add_parser("acceptance", help="run the platform live-acceptance harness")
    acceptance_sub = acceptance.add_subparsers(dest="acceptance_cmd", required=True)
    acceptance_run = acceptance_sub.add_parser("run")
    acceptance_run.add_argument(
        "--acceptance-root",
        type=common.Path,
        default=common.STATE_ROOT / "runs" / "platform-acceptance",
    )
    acceptance_run.add_argument(
        "--checks",
        nargs="+",
        choices=[
            "rig", "regression", "verification", "location",
        ],
        default=["rig", "regression", "verification"],
    )
    acceptance_run.add_argument("--ids", nargs="+", default=["R123"])
    acceptance_run.add_argument(
        "--verification-path",
        default="verification/platform-smoke.yaml",
    )
    acceptance_run.add_argument("--api", choices=["disabled", "advisory", "required"], default="disabled")
    acceptance_run.add_argument("--location-lat", type=float, default=31.2304)
    acceptance_run.add_argument("--location-lng", type=float, default=121.4737)
    acceptance_run.set_defaults(func=cmd_acceptance_run, needs_app=True)

