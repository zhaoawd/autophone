from __future__ import annotations

import argparse

from autophone.adapters.cli import _common as common


def cmd_telemetry_doctor(args: argparse.Namespace) -> int:
    if getattr(args, "provider", "legacy") == "data_trace":
        from autophone.observation.data_trace import data_trace_doctor

        result = data_trace_doctor(
            endpoint=args.endpoint,
            # --query 保留为兼容别名；dataTrace 没有身份时只探 token，不做全表查询。
            probe_token=bool(args.probe_token or args.query),
            timeout_s=args.timeout,
        )
        print(common.json.dumps(result, ensure_ascii=False, indent=2))
        if not result.get("configured"):
            return 1
        if result.get("status") == "probe_failed":
            return 1
        return 0
    result = common.telemetry_backend_doctor(
        backend_url=args.backend_url,
        query=args.query,
        timeout_s=args.timeout,
    )
    print(common.json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("configured"):
        return 1
    if result.get("status") == "query_failed":
        return 1
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    telemetry = sub.add_parser("telemetry")
    telemetry_sub = telemetry.add_subparsers(dest="telemetry_cmd", required=True)
    telemetry_doctor = telemetry_sub.add_parser("doctor")
    telemetry_doctor.add_argument(
        "--provider", choices=["legacy", "data_trace"], default="legacy"
    )
    telemetry_doctor.add_argument("--endpoint", choices=["dev", "prod"], default="dev")
    telemetry_doctor.add_argument("--probe-token", action="store_true")
    telemetry_doctor.add_argument("--backend-url", default=None)
    telemetry_doctor.add_argument("--query", action="store_true")
    telemetry_doctor.add_argument("--timeout", type=float, default=None)
    telemetry_doctor.set_defaults(func=cmd_telemetry_doctor)
