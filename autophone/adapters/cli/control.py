from __future__ import annotations

import argparse
import time

from autophone.adapters.cli import _common as common


def _control_plane():
    from autophone.application.control_plane import require_control_plane

    return require_control_plane()


def _host_coordination(*, evidence_root=None):
    from autophone.application.control_plane import require_host_coordination

    return require_host_coordination(evidence_root=evidence_root)


def cmd_control_assignment_submit(args: argparse.Namespace) -> int:
    plan = common.json.loads(args.plan.read_text(encoding="utf-8"))
    result = _control_plane().submit_assignment(plan, args.idempotency_key)
    print(common.json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_control_assignment_get(args: argparse.Namespace) -> int:
    print(common.json.dumps(
        _control_plane().get_assignment(args.assignment_id),
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def cmd_control_assignment_cancel(args: argparse.Namespace) -> int:
    result = _control_plane().cancel_assignment(
        args.assignment_id,
        args.actor,
        args.reason,
    )
    print(common.json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_control_assignment_rerun(args: argparse.Namespace) -> int:
    print(common.json.dumps(
        _control_plane().rerun_assignment(args.assignment_id, actor=args.actor, reason=args.reason),
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def cmd_control_assignment_retry(args: argparse.Namespace) -> int:
    print(common.json.dumps(
        _control_plane().retry_assignment(args.assignment_id),
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def cmd_control_runs_reconcile(args: argparse.Namespace) -> int:
    """列出（`--apply` 时收敛）Assignment 已终态而 `runs` 行仍为 queued/assigned 的存量行（BL-214）。

    默认只读；`--apply` 必须带 `--actor`，只改 `runs.status`，`result` 不变。
    """
    if args.apply and not str(args.actor or "").strip():
        print(common.json.dumps({"refused": "actor_required",
                                 "detail": "--apply requires --actor"}, ensure_ascii=False))
        return 2
    result = _control_plane().reconcile_stale_run_rows(apply=args.apply, actor=args.actor)
    print(common.json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_control_runs_cohort_freeze(args: argparse.Namespace) -> int:
    """按冻结说明生成可运行率 cohort 文件（BL-083）；只读 App 资产，不访问控制面。"""
    from autophone.application import runnability
    from autophone.contracts import ContractError

    try:
        spec = common.json.loads(args.spec.read_text(encoding="utf-8"))
        cohort = runnability.freeze_cohort(spec)
    except (OSError, ValueError, ContractError, runnability.RunnabilityError) as exc:
        print(common.json.dumps({"refused": "cohort_invalid", "detail": str(exc)},
                                ensure_ascii=False), file=common.sys.stderr)
        return 2
    text = common.json.dumps(cohort, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(common.json.dumps({"cohort": str(args.out),
                                 "sha256": runnability.sha256_file(args.out)}, ensure_ascii=False))
    else:
        common.sys.stdout.write(text)
    return 0


def cmd_control_runs_runnability(args: argparse.Namespace) -> int:
    """按冻结 cohort 只读出数（BL-083）：PostgreSQL 只读事务或 `index.sqlite` 只读连接。

    PostgreSQL 来源不经 `require_control_plane()`：那里会执行 schema 初始化（DDL）。
    """
    from autophone.application import runnability
    from autophone.application.control_plane import _configured_dsn_schema
    from autophone.contracts import ContractError

    dsn, schema = _configured_dsn_schema()
    run_root = args.run_root or (common.STATE_ROOT / "runs" / "platform")
    try:
        report = runnability.runnability_report(
            args.cohort, source=args.source, dsn=dsn, schema=schema, run_root=run_root,
        )
    except (ContractError, runnability.RunnabilityError) as exc:
        print(common.json.dumps({"refused": "runnability_input_invalid", "detail": str(exc)},
                                ensure_ascii=False), file=common.sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 数据源不可用时拒绝，不输出部分结果
        print(common.json.dumps({"refused": "runnability_source_unavailable",
                                 "detail": f"{type(exc).__name__}: {exc}"},
                                ensure_ascii=False), file=common.sys.stderr)
        return 2
    print(common.json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_control_rig_list(args: argparse.Namespace) -> int:
    print(common.json.dumps(
        {"rigs": _control_plane().list_rigs(include_deregistered=True)
         if args.all else _control_plane().list_rigs()},
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def cmd_control_rig_deregister(args: argparse.Namespace) -> int:
    result = _control_plane().deregister_rig(
        args.rig_id, args.rig_generation, actor=args.actor, reason=args.reason,
    )
    print(common.json.dumps(result, ensure_ascii=False, indent=2))
    return 0


_TERMINAL_ASSIGNMENT_STATUSES = frozenset({"passed", "failed", "error", "blocked", "cancelled", "indeterminate"})


def cmd_control_rig_recover(args: argparse.Namespace) -> int:
    """写 Recovery Receipt；有被中断（Lease 过期）且未闭包的 Attempt 时先请求闭包恢复 Assignment（BL-076）。

    `--wait-seconds N` 时等待该闭包恢复 Assignment 到终态并一并输出；它未通过或超时时退出码 3，
    Rig 保持 quarantine。不等待（默认 0）时只输出本次回执，退出码 0。
    """
    probe_result = common.json.loads(args.probe_result.read_text(encoding="utf-8"))
    control = _control_plane()
    result = control.recover_rig(
        args.rig_id,
        args.rig_generation,
        args.actor,
        probe_result,
    )
    closure = result.get("interrupted_closure") or {}
    if closure.get("status") != "requested" or args.wait_seconds <= 0:
        print(common.json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    deadline = time.monotonic() + args.wait_seconds
    assignment = control.get_assignment(closure["assignment_id"])
    while assignment.get("status") not in _TERMINAL_ASSIGNMENT_STATUSES and time.monotonic() < deadline:
        time.sleep(1.0)
        assignment = control.get_assignment(closure["assignment_id"])
    print(common.json.dumps({"receipt": result, "closure_assignment": assignment}, ensure_ascii=False, indent=2))
    return 0 if assignment.get("status") == "passed" else 3


def cmd_control_reservation_create(args: argparse.Namespace) -> int:
    spec = common.json.loads(args.spec.read_text(encoding="utf-8"))
    print(common.json.dumps(
        _control_plane().create_reservation(spec),
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def cmd_control_reservation_release(args: argparse.Namespace) -> int:
    print(common.json.dumps(
        _control_plane().release_reservation(args.reservation_id, args.actor),
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def cmd_control_access_list(args: argparse.Namespace) -> int:
    """列出授权记录（BL-104）。`controller_token` 不在输出里。"""
    from autophone.application.embed_access import EmbedAccessRegistry

    registry = EmbedAccessRegistry.from_environment()
    statuses = tuple(args.status) if args.status else None
    rows = registry.grants.list(statuses=statuses)
    print(common.json.dumps([row.public() for row in rows], ensure_ascii=False, indent=2))
    return 0


def cmd_control_access_reconcile(args: argparse.Namespace) -> int:
    """`rig_reservations` 与 console 会话的对账（设计文档 §十三）。

    两条规则：有预留无 lease → 释放预留；有 lease 无预留 → 补登记。
    `--dry-run` 只报不改。
    """
    from autophone.application.device_access import reconcile_console_reservations
    from autophone.application.device_session import (
        RemoteDeviceSessions,
        control_http_base_url,
    )
    from autophone.application.embed_access import EmbedAccessRegistry

    registry = EmbedAccessRegistry.from_environment()
    sessions = RemoteDeviceSessions(control_http_base_url())
    result = reconcile_console_reservations(
        control=_control_plane(),
        call=lambda device, op, payload: sessions.call(device, op, payload),
        grants=registry.grants,
        devices=args.device or None,
        actor=args.actor,
        apply=not args.dry_run,
    )
    print(common.json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_control_evidence_validate(args: argparse.Namespace) -> int:
    from autophone.control.evidence import evaluate_same_host_p0

    try:
        report = common.json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, common.json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read P0 report: {exc}") from exc
    if not isinstance(report, dict):
        raise SystemExit("P0 report must be a JSON object")
    result = evaluate_same_host_p0(
        report,
        verify_evidence=True,
        evidence_root=args.evidence_root or args.report.parent,
    )
    if args.output:
        common.write_json_atomic(args.output, result)
    else:
        print(common.json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "passed" else 2


def cmd_control_evidence_approve(args: argparse.Namespace) -> int:
    from autophone.control.evidence import build_capacity_admission_review

    try:
        report = common.json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, common.json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read P0 report: {exc}") from exc
    if not isinstance(report, dict):
        raise SystemExit("P0 report must be a JSON object")
    try:
        review = build_capacity_admission_review(
            report,
            reviewer=args.reviewer,
            reviewed_at=args.reviewed_at or common._now(),
            note=args.note,
            evidence_root=args.evidence_root or args.report.parent,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    common.write_json_atomic(args.output, review)
    return 0


def cmd_control_host_get(args: argparse.Namespace) -> int:
    print(common.json.dumps(
        _host_coordination().get_host(args.host_id),
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def _print_json(value) -> int:
    print(common.json.dumps(value, ensure_ascii=False, indent=2))
    return 0


def cmd_control_host_anomalies_list(args: argparse.Namespace) -> int:
    return _print_json(_host_coordination().list_host_anomalies(
        args.host_id, kind=args.kind, since=args.since, limit=args.limit,
    ))


def cmd_control_host_anomalies_show(args: argparse.Namespace) -> int:
    return _print_json(_host_coordination().get_host_anomaly(args.anomaly_id))


def cmd_control_host_anomalies_report(args: argparse.Namespace) -> int:
    return _print_json(_host_coordination().host_anomaly_report(
        args.host_id, since=args.since, include_test=args.include_test,
    ))


def cmd_control_host_anomalies_review(args: argparse.Namespace) -> int:
    return _print_json(_host_coordination().add_host_anomaly_review(
        args.anomaly_id, actor=args.actor, note=args.note, labels=args.label,
    ))


def cmd_control_host_allocatable(args: argparse.Namespace) -> int:
    return _print_json(_host_coordination().set_host_allocatable(
        args.host_id, memory_bytes=args.memory_gb * 1024**3, actor=args.actor, reason=args.reason,
    ))


def cmd_control_host_admission_mode(args: argparse.Namespace) -> int:
    return _print_json(_host_coordination().set_host_admission_mode(
        args.host_id, args.mode, actor=args.actor, reason=args.reason,
    ))


def cmd_control_host_breaker(args: argparse.Namespace) -> int:
    if args.reset:
        if not args.actor or not args.reason:
            raise SystemExit("--reset requires --actor and --reason")
        return _print_json(_host_coordination().reset_host_breaker(
            args.host_id, actor=args.actor, reason=args.reason,
        ))
    return _print_json(_host_coordination().host_breaker_state(args.host_id))


def cmd_control_host_admission_shadow(args: argparse.Namespace) -> int:
    return _print_json(_host_coordination().host_admission_shadow_report(args.host_id, since=args.since))


def cmd_control_host_capacity(args: argparse.Namespace) -> int:
    evidence = (
        common.json.loads(args.evidence.read_text(encoding="utf-8"))
        if args.evidence
        else {}
    )
    approval = (
        common.json.loads(args.approval.read_text(encoding="utf-8"))
        if args.approval
        else {}
    )
    # 提升 Capacity 时 store 要重新读取证据里的本地 file:// 文件；`--evidence-root`
    # 显式给出可信根目录，未给出时 store 只取 `AUTOPHONE_EVIDENCE_ROOT`。
    result = _host_coordination(evidence_root=args.evidence_root).change_host_capacity(
        args.host_id,
        args.capacity,
        actor=args.actor,
        reason=args.reason,
        evidence=evidence,
        approval=approval,
    )
    print(common.json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_control_host_maintenance_start(args: argparse.Namespace) -> int:
    result = _host_coordination().begin_host_maintenance(
        args.host_id,
        intent=args.intent,
        owner=args.owner,
        ttl_seconds=args.ttl_seconds,
    )
    print(common.json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_control_host_maintenance_complete(args: argparse.Namespace) -> int:
    result = common.json.loads(args.result.read_text(encoding="utf-8"))
    completed = _host_coordination().finish_host_maintenance(
        args.maintenance_id,
        owner=args.owner,
        result=result,
    )
    print(common.json.dumps(completed, ensure_ascii=False, indent=2))
    return 0


def cmd_control_recovery_get(args):
    print(common.json.dumps(_control_plane().get_recovery(args.recovery_id), ensure_ascii=False, indent=2))
    return 0


def cmd_control_recovery_list(args):
    recoveries = _control_plane().list_rig_recoveries(args.rig_id, active_only=args.active)
    print(common.json.dumps({'rig_id': args.rig_id, 'active_only': args.active, 'recoveries': recoveries},
                            ensure_ascii=False, indent=2))
    return 0


def cmd_control_recovery_resume(args):
    print(common.json.dumps(_control_plane().resume_recovery(args.recovery_id, retry=args.retry), ensure_ascii=False, indent=2))
    return 0


def cmd_control_recovery_deliver(args):
    from autophone.recovery_observation import deliver
    print(common.json.dumps(deliver(args.observation, _control_plane()), ensure_ascii=False, indent=2))
    return 0


def cmd_control_recovery_isolate(args):
    result = _control_plane().confirm_recovery_isolation(args.recovery_id, actor=args.actor, reason=args.reason)
    print(common.json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    control = sub.add_parser("control", help="PostgreSQL Control Plane client")
    control_sub = control.add_subparsers(dest="control_cmd", required=True)
    recovery = control_sub.add_parser('recovery')
    recovery_sub = recovery.add_subparsers(dest='recovery_cmd', required=True)
    get = recovery_sub.add_parser('get')
    get.add_argument('recovery_id')
    get.set_defaults(func=cmd_control_recovery_get)
    recovery_list = recovery_sub.add_parser('list', help='只读列出一个 Rig 的 recovery incident')
    recovery_list.add_argument('--rig-id', required=True)
    recovery_list.add_argument('--active', action='store_true',
                               help='只列未关闭的 incident（state 不是 recovered/isolation_confirmed）')
    recovery_list.set_defaults(func=cmd_control_recovery_list)
    resume = recovery_sub.add_parser('resume')
    resume.add_argument('recovery_id')
    resume.add_argument('--retry', action='store_true')
    resume.set_defaults(func=cmd_control_recovery_resume)
    deliver = recovery_sub.add_parser('deliver')
    deliver.add_argument('observation', type=common.Path)
    deliver.set_defaults(func=cmd_control_recovery_deliver)
    isolate = recovery_sub.add_parser('confirm-isolation')
    isolate.add_argument('recovery_id')
    isolate.add_argument('--actor', required=True)
    isolate.add_argument('--reason', required=True)
    isolate.set_defaults(func=cmd_control_recovery_isolate)
    control_assignment = control_sub.add_parser("assignment")
    control_assignment_sub = control_assignment.add_subparsers(
        dest="control_assignment_cmd", required=True,
    )
    control_assignment_submit = control_assignment_sub.add_parser("submit")
    control_assignment_submit.add_argument("plan", type=common.Path)
    control_assignment_submit.add_argument("--idempotency-key", required=True)
    control_assignment_submit.set_defaults(func=cmd_control_assignment_submit)
    control_assignment_get = control_assignment_sub.add_parser("get")
    control_assignment_get.add_argument("assignment_id")
    control_assignment_get.set_defaults(func=cmd_control_assignment_get)
    control_assignment_cancel = control_assignment_sub.add_parser("cancel")
    control_assignment_cancel.add_argument("assignment_id")
    control_assignment_cancel.add_argument("--actor", required=True)
    control_assignment_cancel.add_argument("--reason", required=True)
    control_assignment_cancel.set_defaults(func=cmd_control_assignment_cancel)
    control_assignment_rerun = control_assignment_sub.add_parser(
        "rerun", help="submit a new Assignment with a fresh run_id; the first attempt's evidence stays",
    )
    control_assignment_rerun.add_argument("assignment_id")
    control_assignment_rerun.add_argument("--actor", required=True)
    control_assignment_rerun.add_argument("--reason", required=True)
    control_assignment_rerun.set_defaults(func=cmd_control_assignment_rerun)
    control_assignment_retry = control_assignment_sub.add_parser("retry")
    control_assignment_retry.add_argument("assignment_id")
    control_assignment_retry.set_defaults(func=cmd_control_assignment_retry)
    control_runs = control_sub.add_parser("runs")
    control_runs_sub = control_runs.add_subparsers(dest="control_runs_cmd", required=True)
    control_runs_reconcile = control_runs_sub.add_parser(
        "reconcile", help="list or converge runs rows left at queued/assigned behind a terminal Assignment",
    )
    control_runs_reconcile.add_argument("--apply", action="store_true", help="write the change (default: dry-run)")
    control_runs_reconcile.add_argument("--actor", default=None)
    control_runs_reconcile.set_defaults(func=cmd_control_runs_reconcile)
    control_runs_freeze = control_runs_sub.add_parser(
        "cohort-freeze", help="freeze a runnability cohort (cases, selector, asset sha256, planned run ids)",
    )
    control_runs_freeze.add_argument("spec", type=common.Path, help="cohort freeze spec JSON")
    control_runs_freeze.add_argument("--out", type=common.Path, default=None)
    control_runs_freeze.set_defaults(func=cmd_control_runs_cohort_freeze)
    control_runs_runnability = control_runs_sub.add_parser(
        "runnability", help="read-only eligible/started/passed/blocked counts for a frozen cohort",
    )
    control_runs_runnability.add_argument("--cohort", type=common.Path, required=True)
    control_runs_runnability.add_argument("--source", choices=["pg", "sqlite"], default="pg")
    control_runs_runnability.add_argument(
        "--run-root", type=common.Path, default=None,
        help="run root holding index.sqlite for --source sqlite (default: <state root>/runs/platform)",
    )
    control_runs_runnability.set_defaults(func=cmd_control_runs_runnability)
    control_rig = control_sub.add_parser("rig")
    control_rig_sub = control_rig.add_subparsers(dest="control_rig_cmd", required=True)
    control_rig_list = control_rig_sub.add_parser("list")
    control_rig_list.add_argument("--all", action="store_true", help="include deregistered Rigs")
    control_rig_list.set_defaults(func=cmd_control_rig_list)
    control_rig_deregister = control_rig_sub.add_parser("deregister")
    control_rig_deregister.add_argument("rig_id")
    control_rig_deregister.add_argument("--rig-generation", type=int, required=True)
    control_rig_deregister.add_argument("--actor", required=True)
    control_rig_deregister.add_argument("--reason", required=True)
    control_rig_deregister.set_defaults(func=cmd_control_rig_deregister)
    control_rig_recover = control_rig_sub.add_parser("recover")
    control_rig_recover.add_argument("rig_id")
    control_rig_recover.add_argument("--rig-generation", type=int, required=True)
    control_rig_recover.add_argument("--actor", required=True)
    control_rig_recover.add_argument("--probe-result", type=common.Path, required=True)
    control_rig_recover.add_argument(
        "--wait-seconds", type=float, default=0.0,
        help="有被中断 Attempt 时等待闭包恢复 Assignment 到终态的秒数；未通过或超时退出码 3（BL-076）",
    )
    control_rig_recover.set_defaults(func=cmd_control_rig_recover)
    control_reservation = control_sub.add_parser("reservation")
    control_reservation_sub = control_reservation.add_subparsers(
        dest="control_reservation_cmd", required=True,
    )
    control_reservation_create = control_reservation_sub.add_parser("create")
    control_reservation_create.add_argument("spec", type=common.Path)
    control_reservation_create.set_defaults(func=cmd_control_reservation_create)
    control_reservation_release = control_reservation_sub.add_parser("release")
    control_reservation_release.add_argument("reservation_id")
    control_reservation_release.add_argument("--actor", required=True)
    control_reservation_release.set_defaults(func=cmd_control_reservation_release)
    control_access = control_sub.add_parser(
        "access", help="B 端接入授权记录（access_grants）与预留对账",
    )
    control_access_sub = control_access.add_subparsers(
        dest="control_access_cmd", required=True,
    )
    control_access_list = control_access_sub.add_parser("list")
    control_access_list.add_argument(
        "--status", action="append",
        choices=["creating", "active", "closing", "closed"],
        help="只列这些状态（可重复）",
    )
    control_access_list.set_defaults(func=cmd_control_access_list)
    control_access_reconcile = control_access_sub.add_parser("reconcile")
    control_access_reconcile.add_argument(
        "--device", action="append", help="额外检查的设备键（可重复）",
    )
    control_access_reconcile.add_argument("--actor", default="cli")
    control_access_reconcile.add_argument("--dry-run", action="store_true")
    control_access_reconcile.set_defaults(func=cmd_control_access_reconcile)
    control_evidence = control_sub.add_parser("evidence")
    control_evidence_sub = control_evidence.add_subparsers(
        dest="control_evidence_cmd", required=True,
    )
    control_evidence_validate = control_evidence_sub.add_parser("validate")
    control_evidence_validate.add_argument("report", type=common.Path)
    control_evidence_validate.add_argument("--output", type=common.Path)
    control_evidence_validate.add_argument("--evidence-root", type=common.Path)
    control_evidence_validate.set_defaults(func=cmd_control_evidence_validate)
    control_evidence_approve = control_evidence_sub.add_parser(
        "approve",
        help="bind an operator review to capacity_qualification evidence",
    )
    control_evidence_approve.add_argument("report", type=common.Path)
    control_evidence_approve.add_argument("--output", type=common.Path, required=True)
    control_evidence_approve.add_argument("--evidence-root", type=common.Path)
    control_evidence_approve.add_argument("--reviewer", required=True)
    control_evidence_approve.add_argument("--reviewed-at")
    control_evidence_approve.add_argument(
        "--note",
        required=True,
        help=(
            "review rationale; approval attests sustained single-Rig baselines, "
            "the dual-Rig long window, health/fatal/USB diagnostics, and 62078 callers"
        ),
    )
    control_evidence_approve.set_defaults(func=cmd_control_evidence_approve)
    control_host = control_sub.add_parser("host")
    control_host_sub = control_host.add_subparsers(
        dest="control_host_cmd", required=True,
    )
    control_host_get = control_host_sub.add_parser("get")
    control_host_get.add_argument("host_id")
    control_host_get.set_defaults(func=cmd_control_host_get)
    control_host_capacity = control_host_sub.add_parser("capacity")
    control_host_capacity.add_argument("host_id")
    control_host_capacity.add_argument("capacity", type=int)
    control_host_capacity.add_argument("--actor", required=True)
    control_host_capacity.add_argument("--reason", required=True)
    control_host_capacity.add_argument("--evidence", type=common.Path)
    control_host_capacity.add_argument(
        "--approval",
        type=common.Path,
        help="operator review Artifact required for configured Capacity above 1",
    )
    control_host_capacity.add_argument(
        "--evidence-root",
        type=common.Path,
        help=(
            "trusted root for local file:// evidence when raising Capacity; "
            "defaults to AUTOPHONE_EVIDENCE_ROOT"
        ),
    )
    control_host_capacity.set_defaults(func=cmd_control_host_capacity)
    control_host_allocatable = control_host_sub.add_parser("allocatable")
    control_host_allocatable.add_argument("host_id")
    control_host_allocatable.add_argument("--memory-gb", type=int, required=True)
    control_host_allocatable.add_argument("--actor", required=True)
    control_host_allocatable.add_argument("--reason", required=True)
    control_host_allocatable.set_defaults(func=cmd_control_host_allocatable)
    control_host_mode = control_host_sub.add_parser("admission-mode")
    control_host_mode.add_argument("host_id")
    control_host_mode.add_argument("mode", choices=("capacity", "resource"))
    control_host_mode.add_argument("--actor", required=True)
    control_host_mode.add_argument("--reason", required=True)
    control_host_mode.set_defaults(func=cmd_control_host_admission_mode)
    control_host_breaker = control_host_sub.add_parser("breaker")
    control_host_breaker.add_argument("host_id")
    control_host_breaker.add_argument("--reset", action="store_true")
    control_host_breaker.add_argument("--actor")
    control_host_breaker.add_argument("--reason")
    control_host_breaker.set_defaults(func=cmd_control_host_breaker)
    control_host_shadow = control_host_sub.add_parser("admission-shadow")
    control_host_shadow.add_argument("host_id")
    control_host_shadow.add_argument("--since", help="ISO 8601 time")
    control_host_shadow.set_defaults(func=cmd_control_host_admission_shadow)
    control_host_anomalies = control_host_sub.add_parser("anomalies")
    control_host_anomalies_sub = control_host_anomalies.add_subparsers(
        dest="control_host_anomalies_cmd", required=True,
    )
    anomalies_list = control_host_anomalies_sub.add_parser("list")
    anomalies_list.add_argument("host_id")
    anomalies_list.add_argument("--kind")
    anomalies_list.add_argument("--since", help="ISO 8601 time")
    anomalies_list.add_argument("--limit", type=int, default=100)
    anomalies_list.set_defaults(func=cmd_control_host_anomalies_list)
    anomalies_show = control_host_anomalies_sub.add_parser("show")
    anomalies_show.add_argument("anomaly_id")
    anomalies_show.set_defaults(func=cmd_control_host_anomalies_show)
    anomalies_report = control_host_anomalies_sub.add_parser("report")
    anomalies_report.add_argument("host_id")
    anomalies_report.add_argument("--since", help="ISO 8601 time")
    anomalies_report.add_argument("--include-test", action="store_true")
    anomalies_report.set_defaults(func=cmd_control_host_anomalies_report)
    anomalies_review = control_host_anomalies_sub.add_parser("review")
    anomalies_review.add_argument("anomaly_id")
    anomalies_review.add_argument("--actor", required=True)
    anomalies_review.add_argument("--note", required=True)
    anomalies_review.add_argument("--label", action="append", default=[])
    anomalies_review.set_defaults(func=cmd_control_host_anomalies_review)
    control_host_maintenance = control_host_sub.add_parser("maintenance")
    control_host_maintenance_sub = control_host_maintenance.add_subparsers(
        dest="control_host_maintenance_cmd", required=True,
    )
    control_host_maintenance_start = control_host_maintenance_sub.add_parser("start")
    control_host_maintenance_start.add_argument("host_id")
    control_host_maintenance_start.add_argument("--intent", required=True)
    control_host_maintenance_start.add_argument("--owner", required=True)
    control_host_maintenance_start.add_argument("--ttl-seconds", type=int, default=300)
    control_host_maintenance_start.set_defaults(
        func=cmd_control_host_maintenance_start,
    )
    control_host_maintenance_complete = control_host_maintenance_sub.add_parser("complete")
    control_host_maintenance_complete.add_argument("maintenance_id")
    control_host_maintenance_complete.add_argument("--owner", required=True)
    control_host_maintenance_complete.add_argument("--result", type=common.Path, required=True)
    control_host_maintenance_complete.set_defaults(
        func=cmd_control_host_maintenance_complete,
    )
