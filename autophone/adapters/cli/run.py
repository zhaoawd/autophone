from __future__ import annotations

import argparse

from autophone.adapters.cli import _common as common


def cmd_run(args: argparse.Namespace) -> int:
    if getattr(args, "run_action", None) == "overturn":
        from autophone.execution import overturns

        if len(args.run_tail or []) < 2:
            print("usage: autophone run overturn <run_id> <case_id> --reason ...", file=common.sys.stderr)
            return 2
        if not args.reason:
            print("run overturn requires --reason", file=common.sys.stderr)
            return 2
        run_root = args.out_root or (common.STATE_ROOT / "runs" / "platform")
        event = overturns.append_overturn(
            run_root,
            run_id=str(args.run_tail[0]),
            case_id=str(args.run_tail[1]),
            reason=args.reason,
            source=args.source,
            evidence=args.evidence,
            actor=args.requested_by,
        )
        print(common.json.dumps({"ok": True, "event": event}, ensure_ascii=False, indent=2))
        return 0
    if getattr(args, "run_action", None):
        print(f"unknown run action: {args.run_action}", file=common.sys.stderr)
        return 2
    execution_plan = getattr(args, "execution_plan", None)
    fixture_order_raw = getattr(args, "fixture_order_json", None)
    if fixture_order_raw:
        try:
            fixture_order = common.json.loads(fixture_order_raw)
        except common.json.JSONDecodeError as exc:
            print(f"--fixture-order 不是合法 JSON：{exc}", file=common.sys.stderr)
            return 2
        if not isinstance(fixture_order, dict):
            print("--fixture-order 必须是 JSON 对象", file=common.sys.stderr)
            return 2
        args.fixture_order = fixture_order
    trial_draft_recipes = bool(getattr(args, "trial_draft_recipes", False))
    draft_cases = bool(getattr(args, "draft_cases", False))
    if trial_draft_recipes and not args.ids:
        print("--trial-draft-recipes requires --ids (draft trials must be explicitly pinned)",
              file=common.sys.stderr)
        return 2
    if draft_cases and not args.ids:
        print("--draft-cases requires --ids (draft trials must be explicitly pinned)",
              file=common.sys.stderr)
        return 2
    if execution_plan and (args.ids or args.tag):
        print("--execution-plan cannot be combined with --ids or --tag", file=common.sys.stderr)
        return 2
    golden_path_id = getattr(args, "golden_path", None)
    if golden_path_id and (execution_plan or args.tag):
        print("--golden-path cannot be combined with --execution-plan or --tag", file=common.sys.stderr)
        return 2
    if execution_plan:
        selector = None
    elif args.ids:
        selector = {"ids": args.ids}
        if draft_cases:
            selector["statuses"] = ["draft"]
        if trial_draft_recipes:
            selector["trial_draft_recipes"] = True
    elif golden_path_id:
        selector = None  # filled from the golden path declaration below
    else:
        selector = {"tag": args.tag or "ui_ready"}
    if golden_path_id:
        spec = _golden_path_spec(args, selector, str(golden_path_id))
        if spec is None:
            return 2
    else:
        spec = common._regression_spec(args, selector)
    confirmations = getattr(args, "confirm_trial_effects", None) or []
    if confirmations:
        try:
            from autophone.adapters.cli.trial_confirmation import load_trial_confirmations

            spec["trial_confirmation"] = load_trial_confirmations(confirmations)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            print(f"--confirm-trial-effects: {exc}", file=common.sys.stderr)
            return 2
    # BL-215：与 HTTP、计划任务、包门禁同一口径，按 Run Spec 的 device 固定目标 Rig。
    handle = common._dispatcher(args).submit(spec, pin_device=True)
    print(common.json.dumps(handle.summary(), ensure_ascii=False, indent=2))
    from autophone.execution.contracts import regression_exit_code
    return regression_exit_code(str(handle.result.get("status") or "error"))


def _golden_path_spec(
    args: argparse.Namespace, selector: dict | None, path_id: str
) -> dict | None:
    """Regression Run Spec marked for one golden path, or None after printing why not.

    Without ``--ids`` the selector is the one ``knowledge golden-path plan``
    gives for the Case's current lifecycle.  An explicit selector must equal
    it; ``validate_golden_path_request`` also checks that the path is declared
    and the Case belongs to it before anything is submitted.
    """
    from autophone.assets.apps import load_app_profile
    from autophone.assets.golden_paths import (
        GoldenPathError,
        case_lifecycle,
        find_golden_path,
        golden_path_selector,
        run_marker,
        validate_golden_path_request,
    )
    from autophone.contracts import ContractError

    try:
        profile = load_app_profile(args.app)
        declared = find_golden_path(profile, path_id)
        if selector is None:
            selector = golden_path_selector(declared, case_lifecycle(profile, declared["case_id"]))
        spec = common._regression_spec(args, selector)
        spec["golden_path"] = run_marker(declared)
        validate_golden_path_request(spec, profile)
    except (GoldenPathError, ContractError) as exc:
        print(f"--golden-path {path_id}: {exc}", file=common.sys.stderr)
        return None
    return spec


def register(sub: argparse._SubParsersAction) -> None:
    run = sub.add_parser("run", help="run deterministic regression cases")
    run.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    run.add_argument("--tag", default=None)
    run.add_argument("--ids", nargs="+", default=None)
    run.add_argument(
        "--draft-cases",
        action="store_true",
        help="显式点名试跑 status=draft 的用例；必须与 --ids 同用，不进入默认回归面",
    )
    run.add_argument(
        "--trial-draft-recipes",
        action="store_true",
        help="显式点名试跑 active case 的 recipes/drafts/<id>.yaml（不进入默认回归面）",
    )
    run.add_argument(
        "--confirm-trial-effects", type=common.Path, action="append", metavar="CARD_JSON",
        help="确认所给准备卡 JSON 的试跑副作用；每个 Case 一个文件，可重复；资产变更后须重新读取并确认",
    )
    run.add_argument(
        "--golden-path",
        default=None,
        metavar="PATH_ID",
        help="按 golden_paths.yaml 中点名的黄金路径提交：写入 run-spec.golden_path 标记，"
             "缺省 selector 取该 Case 当前生命周期下的计划 selector；只有带标记的 Run 为 live Recipe 用例绑定 Fixture Readback",
    )
    run.add_argument("--execution-plan", choices=["current"], default=None,
                     help="App 注册的当前批跑编排（apps/<app>/execution_plan.py）；按 App 声明的分组顺序执行")
    run.add_argument("run_action", nargs="?", help=common.argparse.SUPPRESS)
    run.add_argument("run_tail", nargs="*", help=common.argparse.SUPPRESS)
    run.add_argument("--reason", default=None, help=common.argparse.SUPPRESS)
    run.add_argument("--source", default="manual", help=common.argparse.SUPPRESS)
    run.add_argument("--evidence", default=None, help=common.argparse.SUPPRESS)
    run.add_argument("--api", choices=["required", "advisory"], default=None,
                     help="开 API 抓取面（缺省关）：required=API 断言 fail 拖垮 overall；advisory=只留证据不改判")
    run.add_argument(
        "--fixture-order", dest="fixture_order_json", default=None,
        help="显式冻结本 run 的 fixture.order JSON；用于把已证明的 orderNum/order_id/type 注入用例",
    )
    run.add_argument("--no-auto-login", dest="auto_login", action="store_false", default=True,
                     help="needs_login 时不自动补登（缺省补登；验证登录失败等需停在登出态的用例加这个）")
    run.set_defaults(func=cmd_run, needs_app=True)
