from __future__ import annotations

import argparse
from typing import Any

from autophone.adapters.cli import _common as common


def _operation_spec(args: argparse.Namespace, with_args: dict[str, Any]) -> dict[str, Any]:
    operation: dict[str, Any] = {"name": args.name, "with": with_args}
    draft_id = getattr(args, "draft_id", None)
    if draft_id:  # 转正归因：跑一条 draft 回放时带上 draft_id，run 据此统计连续 passed（M2）
        operation["draft_id"] = draft_id
    spec = {
        "schema_version": "v1",
        "run_id": args.run_id or common._run_id(args.app),
        "type": "operation",
        "app": args.app,
        "device": args.device,
        "operation": operation,
        "requested_by": args.requested_by,
        "requested_at": common._now(),
    }

    api_mode = getattr(args, "api", None) or "disabled"
    if api_mode != "disabled":
        spec["planes"] = {"api": {"mode": api_mode}}
    return spec


def cmd_operation_run(args: argparse.Namespace) -> int:
    """触发一条 type=operation run（roadmap §382 待接入口）。

    --with 是操作入参的 JSON 对象（由操作的 params 契约校验，瞎传 key fail-loud）。
    退出码：passed→0；pending（无执行器，诚实「未接」态，同 verification）→0；failed/error→1。
    """
    with_args: dict[str, Any] = {}
    if args.with_args:
        try:
            with_args = common.json.loads(args.with_args)
        except common.json.JSONDecodeError as exc:
            print(f"--with 不是合法 JSON：{exc}", file=common.sys.stderr)
            return 2
        if not isinstance(with_args, dict):
            print("--with 必须是 JSON 对象（操作入参 name→value）", file=common.sys.stderr)
            return 2
    # BL-215：按 Run Spec 的 device 固定目标 Rig，不交给任一 Rig 认领。
    handle = common._dispatcher(args).submit(_operation_spec(args, with_args), pin_device=True)
    print(common.json.dumps(handle.summary(), ensure_ascii=False, indent=2))
    return 0 if handle.result.get("status") in ("passed", "pending") else 1


def cmd_operation_review(args: argparse.Namespace) -> int:
    result = common._service(args).promotions.review_operation(
        args.run_id,
        status=args.status,
        reviewer=args.reviewer,
        note=args.note,
    )
    print(common.json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    operation = sub.add_parser("operation", help="trigger a type=operation run (operations registry)")
    operation_sub = operation.add_subparsers(dest="operation_cmd", required=True)
    operation_run = operation_sub.add_parser("run", help="run one registered operation by name")
    operation_run.add_argument("name", help="操作库里的稳定语义名，如 login / create_order")
    operation_run.add_argument("--api", choices=["disabled", "advisory"], default="disabled",
                               help="capture API evidence through the existing RunService capture scope")
    operation_run.add_argument("--with", dest="with_args", default=None,
                               help="操作入参，JSON 对象（如 '{\"mode\":\"dry_run\"}'）；由操作 params 契约校验")
    operation_run.add_argument("--draft-id", default=None,
                               help="转正漏斗里被验证的 operation draft id（如 OP12）；跑 draft 回放时带上做归因统计")
    operation_run.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    operation_run.set_defaults(func=cmd_operation_run, needs_app=True)
    operation_review = operation_sub.add_parser("review", help="给一条 operation run 落人审结论（转正闸要 approved）")
    operation_review.add_argument("run_id")
    operation_review.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    operation_review.add_argument("--status", choices=["approved", "rejected", "needs_changes"], required=True)
    operation_review.add_argument("--reviewer", default="local")
    operation_review.add_argument("--note", default="")
    operation_review.set_defaults(func=cmd_operation_review)
