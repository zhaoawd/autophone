from __future__ import annotations

import argparse

from autophone.adapters.cli import _common as common


def cmd_verification_review(args: argparse.Namespace) -> int:
    result = common._service(args).promotions.review_verification(
        args.run_id,
        status=args.status,
        reviewer=args.reviewer,
        note=args.note,
    )
    print(common.json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_verification_doctor(args: argparse.Namespace) -> int:
    result = common.verification_agent_doctor(
        result_json=args.result_json,
        agent_cmd=args.agent_cmd,
        run_agent=args.run_agent,
        timeout_s=args.timeout,
    )
    print(common.json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") in {"not_configured", "invalid_result_json", "agent_error", "agent_failed", "invalid_agent_output"}:
        return 1
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    verification = sub.add_parser("verification")
    verification_sub = verification.add_subparsers(dest="verification_cmd", required=True)
    verification_doctor = verification_sub.add_parser("doctor")
    verification_doctor.add_argument("--result-json", default=None)
    verification_doctor.add_argument("--agent-cmd", default=None)
    verification_doctor.add_argument("--run-agent", action="store_true")
    verification_doctor.add_argument("--timeout", type=float, default=None)
    verification_doctor.set_defaults(func=cmd_verification_doctor)
    verification_review = verification_sub.add_parser("review")
    verification_review.add_argument("run_id")
    verification_review.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    verification_review.add_argument("--status", choices=["approved", "rejected", "needs_changes"], required=True)
    verification_review.add_argument("--reviewer", default="local")
    verification_review.add_argument("--note", default="")
    verification_review.set_defaults(func=cmd_verification_review)
