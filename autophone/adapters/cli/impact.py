from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from autophone.adapters.cli import _common as common


def _impact_run_dir(args: argparse.Namespace) -> Path:
    """`<run_id>` 位置参数 → run 目录。给的是路径就直接用，否则在 runs 树里找。"""
    candidate = common.Path(args.run)
    if candidate.exists() and candidate.is_dir():
        return candidate
    run_root = getattr(args, "out_root", None) or (common.STATE_ROOT / "runs")
    matches = sorted(
        common.Path(run_root).glob(f"**/{args.run}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for match in matches:
        if match.is_dir():
            return match
    raise FileNotFoundError(f"run not found: {args.run}（在 {run_root} 下）")


def cmd_impact_observe(args: argparse.Namespace) -> int:
    from autophone.observation.impact import service as impact_service

    try:
        run_dir = _impact_run_dir(args)
        payload = impact_service.observe(
            run_dir, app=args.app, build=args.build, write=not args.dry_run
        )
    except Exception as exc:  # noqa: BLE001 CLI boundary
        print(f"impact observe failed: {exc}", file=common.sys.stderr)
        return 1
    print(common.json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_impact_index(args: argparse.Namespace) -> int:
    from autophone.observation.impact import service as impact_service

    try:
        payload = impact_service.rebuild_index(
            args.app_name, build=args.build,
            runs_root=(args.runs_root or (common.STATE_ROOT / "runs")),
        )
    except Exception as exc:  # noqa: BLE001 CLI boundary
        print(f"impact index failed: {exc}", file=common.sys.stderr)
        return 1
    index = payload["index"]
    print(common.json.dumps({
        "path": payload["path"],
        "app": index["app"],
        "build": index["build"],
        "counts": index["counts"],
        "gaps": index["gaps"],
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_impact_triage(args: argparse.Namespace) -> int:
    from autophone.observation.impact import service as impact_service
    from autophone.observation.impact import triage as impact_triage

    try:
        profile = common.load_app_profile(args.app_name)
        build = impact_service.resolve_build(profile, args.build)
        index_doc = impact_service.read_index(profile, build=build)
        if index_doc is None:
            raise ValueError(
                f"impact index 尚未构建：app={profile.app} build={build}；先运行 impact index"
            )
        payload = impact_triage.build(index_doc)
        if args.out:
            common.write_json_atomic(common.Path(args.out), payload)
    except Exception as exc:  # noqa: BLE001 CLI boundary
        print(f"impact triage failed: {exc}", file=common.sys.stderr)
        return 1
    print(common.json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_impact_plan(args: argparse.Namespace) -> int:
    from autophone.observation.impact import manifest as impact_manifest
    from autophone.observation.impact import service as impact_service

    try:
        manifest_doc = impact_manifest.load(args.manifest)
        context = impact_service.load_consumer_context(args.context)
        plan = impact_service.plan_change(
            args.app, manifest_doc, build=args.build, consumer_context=context,
            start_screen=getattr(args, "start_screen", "home"),
        )
    except Exception as exc:  # noqa: BLE001 CLI boundary
        print(f"impact plan failed: {exc}", file=common.sys.stderr)
        return 1
    if args.dry_run:
        print(common.json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    run_dir = common.Path(args.out) if args.out else (common.STATE_ROOT / "runs" / "impact" / plan["change_id"])
    written = impact_service.stage_run_inputs(
        run_dir, manifest_doc=manifest_doc, plan=plan, consumer_context=context,
    )
    print(common.json.dumps({"run_dir": str(run_dir), **written,
                      "targets": len(plan["targets"]), "gaps": len(plan["gaps"]),
                      "admission": plan["admission"]["decision"]},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_impact_run(args: argparse.Namespace) -> int:
    """Plan and synchronously execute Active routes through RunService operations."""
    from autophone.observation.impact import manifest as impact_manifest
    from autophone.observation.impact import service as impact_service

    try:
        manifest_doc = impact_manifest.load(args.manifest)
        context = impact_service.load_consumer_context(args.context)
        plan = impact_service.plan_change(
            args.app, manifest_doc, build=args.build, consumer_context=context,
            start_screen=getattr(args, "start_screen", "home"),
        )
        run_id = args.run_id or common._run_id(f"{args.app}-impact")
        run_dir = common.Path(args.out) if args.out else (common.STATE_ROOT / "runs" / "impact" / run_id)
        run_service = common._service(args)
        scenario_ref = None
        if getattr(args, "api_scenario", None) is not None:
            from autophone.observation.impact import controlled_input
            scenario_ref = controlled_input.stage_input(
                args.api_scenario, run_dir, app=args.app,
                source_resolver=run_service.queries.find_run_dir,
            )
            plan["api_scenario"] = controlled_input.describe_input(run_dir, scenario_ref, app=args.app)
        impact_service.stage_run_inputs(
            run_dir, manifest_doc=manifest_doc, plan=plan, consumer_context=context,
        )
        # 子 run 与 `run` / `replay` / `operation run` 同一条提交通道（BL-024）；run 目录仍由
        # `run_service` 解析，两条路径共用同一个 run root。
        dispatcher = common._dispatcher(args, run_service)

        def _run_operation(name: str, child_run_id: str) -> dict[str, Any]:
            api_plane = {"mode": "advisory"}
            if scenario_ref is not None:
                api_plane = controlled_input.stage_child(
                    run_dir, scenario_ref, run_service._run_dir(child_run_id),
                    run_id=child_run_id, app=args.app, device=args.device,
                )
            handle = dispatcher.submit({
                "schema_version": "v1",
                "run_id": child_run_id,
                "type": "operation",
                "app": args.app,
                "device": args.device,
                "operation": {"name": name, "with": {}},
                "planes": {"api": api_plane},
                "requested_by": args.requested_by,
                "requested_at": common._now(),
            }, pin_device=True)  # BL-215：与 HTTP impact 入口同一口径
            payload = handle.summary()
            detail = str(
                (((handle.result.get("planes") or {}).get("ui") or {}).get("detail")) or ""
            )
            if detail:
                payload["detail"] = detail
            return payload

        payload = impact_service.execute_run(
            run_dir,
            run_id=run_id,
            operation_runner=_run_operation,
            baseline_manifest_path=getattr(args, "baseline_manifest", None),
            run_dir_resolver=lambda child_run_id: run_service.queries.find_run_dir(child_run_id),
        )
    except Exception as exc:  # noqa: BLE001 CLI boundary
        print(f"impact run failed: {exc}", file=common.sys.stderr)
        return 1
    execution = payload["execution"]
    print(common.json.dumps({
        "run_id": run_id,
        "run_dir": str(run_dir),
        "status": execution["status"],
        "execution": payload["execution_path"],
        "result": payload["result_path"],
        "report": payload["report_path"],
        "summary": execution["summary"],
        "safety": payload["result"]["safety"],
    }, ensure_ascii=False, indent=2))
    return 0 if execution["status"] in ("completed", "no_executable_targets") else 1


def cmd_impact_report(args: argparse.Namespace) -> int:
    from autophone.observation.impact import service as impact_service

    try:
        run_dir = _impact_run_dir(args)
        evidence = None
        if args.evidence:
            evidence = common.json.loads(common.Path(args.evidence).read_text(encoding="utf-8"))
        payload = impact_service.build_report(run_dir, evidence=evidence)
    except Exception as exc:  # noqa: BLE001 CLI boundary
        print(f"impact report failed: {exc}", file=common.sys.stderr)
        return 1
    print(common.json.dumps({
        "result_path": payload["result_path"],
        "report_path": payload["report_path"],
        "summary": payload["result"]["summary"],
        "safety": payload["result"]["safety"],
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_impact_bindings_list(args: argparse.Namespace) -> int:
    from autophone.observation.impact import bindings as impact_bindings
    from autophone.observation.impact import service as impact_service

    try:
        profile = common.load_app_profile(args.app_name)
        doc = impact_service.load_bindings(profile)
        build = impact_service.resolve_build(profile, args.build)
    except Exception as exc:  # noqa: BLE001 CLI boundary
        print(f"impact bindings failed: {exc}", file=common.sys.stderr)
        return 1
    rows = [
        {
            "id": item.get("id"),
            "status": item.get("status"),
            "declared_confidence": item.get("confidence"),
            "confidence": impact_bindings.effective_confidence(item, current_build=build),
            "screen_id": (item.get("target") or {}).get("screen_id"),
            "region": (item.get("target") or {}).get("region"),
            "response_path": item.get("response_path"),
        }
        for item in impact_bindings.list_bindings(doc, statuses=None)
    ]
    print(common.json.dumps({"app": args.app_name, "build": build, "total": len(rows), "bindings": rows},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_impact_bindings_review(args: argparse.Namespace) -> int:
    from autophone.observation.impact import service as impact_service

    evidence: dict[str, Any] = {}
    if args.positive_run:
        evidence["positive_runs"] = list(args.positive_run)
    if args.negative_control_run:
        evidence["negative_control_runs"] = list(args.negative_control_run)
    if args.authority:
        evidence["authority"] = args.authority
    try:
        item = impact_service.review_binding(
            args.app_name, args.binding_id,
            confidence=args.confidence, status=args.status,
            evidence=evidence, note=args.note,
        )
    except Exception as exc:  # noqa: BLE001 CLI boundary
        print(f"impact bindings review failed: {exc}", file=common.sys.stderr)
        return 1
    print(common.json.dumps(item, ensure_ascii=False, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    impact = sub.add_parser(
        "impact", help="页面 × API 影响图谱：观测归因 / 派生索引 / 变更计划 / 报告")
    impact_sub = impact.add_subparsers(dest="impact_cmd", required=True)

    impact_observe = impact_sub.add_parser(
        "observe", help="把一个 run 的 action mark × API capture × screen checkpoint 合并成 observations")
    impact_observe.add_argument("run", help="run id 或 run 目录路径")
    impact_observe.add_argument("--build", default=None)
    impact_observe.add_argument("--out-root", type=common.Path, default=None, help="runs 根目录（找 run id 用）")
    impact_observe.add_argument("--dry-run", action="store_true", help="只算不落盘")
    impact_observe.set_defaults(func=cmd_impact_observe)

    impact_index = impact_sub.add_parser(
        "index", help="重建派生影响索引（binding + observation → 反查表 + 覆盖缺口）")
    impact_index.add_argument("app_name")
    impact_index.add_argument("--build", default=None)
    impact_index.add_argument("--runs-root", type=common.Path, default=None)
    impact_index.set_defaults(func=cmd_impact_index)

    impact_triage = impact_sub.add_parser(
        "triage", help="按可观测证据形态分诊未绑定 Endpoint/Screen（不生成 Binding）")
    impact_triage.add_argument("app_name")
    impact_triage.add_argument("--build", default=None)
    impact_triage.add_argument("--out", type=common.Path, default=None)
    impact_triage.set_defaults(func=cmd_impact_triage)

    impact_plan = impact_sub.add_parser(
        "plan", help="后端变更清单 → 受影响页面状态与安全路径")
    impact_plan.add_argument("manifest", type=common.Path, help="change-manifest.v1 JSON 路径")
    impact_plan.add_argument("--build", default=None)
    impact_plan.add_argument("--start-screen", default="home", help="路径规划起点；unknown 不假定当前页面")
    impact_plan.add_argument("--context", type=common.Path, default=None, help="ConsumerContext JSON/YAML")
    impact_plan.add_argument("--out", type=common.Path, default=None, help="落 plan 的 run 目录")
    impact_plan.add_argument("--dry-run", action="store_true", help="只打印计划，不落盘")
    impact_plan.set_defaults(func=cmd_impact_plan, needs_app=True)

    impact_run = impact_sub.add_parser(
        "run", help="按计划通过既有 Active Operation/ScreenLock 执行并生成影响报告")
    impact_run.add_argument("manifest", type=common.Path, help="change-manifest.v1 JSON 路径")
    impact_run.add_argument("--build", default=None)
    impact_run.add_argument("--start-screen", default="home", help="路径规划起点；unknown 通过已注册复位操作进入")
    impact_run.add_argument("--context", type=common.Path, default=None, help="ConsumerContext JSON/YAML")
    impact_run.add_argument("--out", type=common.Path, default=None, help="Impact Run 目录")
    impact_run.add_argument("--api-scenario", type=common.Path, default=None,
                            help="本地受控 GET 场景输入；执行前冻结到私有目录")
    impact_run.add_argument("--baseline-manifest", type=common.Path, default=None,
                            help="显式冻结的基线 manifest；执行前复制并校验，不覆盖历史基线")
    impact_run.set_defaults(func=cmd_impact_run, needs_app=True)

    impact_report = impact_sub.add_parser(
        "report", help="plan + 证据 → impact/result.json + impact/report.html")
    impact_report.add_argument("run", help="run id 或 run 目录路径")
    impact_report.add_argument("--evidence", type=common.Path, default=None,
                               help="每目标证据 JSON：{target_id: {reached, api_observed, ...}}")
    impact_report.add_argument("--out-root", type=common.Path, default=None)
    impact_report.set_defaults(func=cmd_impact_report)

    impact_bindings = impact_sub.add_parser(
        "bindings", help="字段→页面区域绑定表（人审资产）")
    impact_bindings_sub = impact_bindings.add_subparsers(dest="impact_bindings_cmd", required=True)
    impact_bindings_list = impact_bindings_sub.add_parser("list")
    impact_bindings_list.add_argument("app_name")
    impact_bindings_list.add_argument("--build", default=None)
    impact_bindings_list.set_defaults(func=cmd_impact_bindings_list)
    impact_bindings_review = impact_bindings_sub.add_parser(
        "review", help="人审改置信度/状态；correlated→causal 需 negative control 或后端权威契约")
    impact_bindings_review.add_argument("app_name")
    impact_bindings_review.add_argument("binding_id")
    impact_bindings_review.add_argument("--confidence", choices=["observed", "correlated", "causal", "stale"])
    impact_bindings_review.add_argument("--status", choices=["draft", "active", "deprecated", "archived"])
    impact_bindings_review.add_argument("--positive-run", action="append", default=[])
    impact_bindings_review.add_argument("--negative-control-run", action="append", default=[])
    impact_bindings_review.add_argument("--authority", choices=["none", "backend_contract"], default=None)
    impact_bindings_review.add_argument("--note", default=None)
    impact_bindings_review.set_defaults(func=cmd_impact_bindings_review)
