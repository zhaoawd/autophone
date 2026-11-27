from __future__ import annotations

import argparse
from typing import Any

from autophone.adapters.cli import _common as common


def cmd_ai_doctor(args: argparse.Namespace) -> int:
    from autophone.application import ai as ai_capability

    result = ai_capability.ai_doctor(probe=args.probe, provider=args.provider, timeout_s=args.timeout)
    print(common.json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "healthy" else 1


def cmd_ai_ask(args: argparse.Namespace) -> int:
    from autophone.application import ai as ai_capability
    from autophone.application.ai_explore import explore

    context_dirs = []
    if args.context_run:
        run_root = args.out_root or (common.STATE_ROOT / "runs" / "platform")
        bundle = ai_capability.build_ai_context(
            common._find_run_dir(run_root, args.context_run), allow_raw=args.allow_raw)
        context_dirs = [bundle.bundle_dir]
    # device → 经 ScreenLock 串行；非 device 默认纯文本，--screenshot 才表示本次明确同意外发当前屏。
    # device 路径流式把每步打到 stderr（执行过程可见，不污染 stdout 的最终答案）。
    def _on_step(ev):
        if ev.get("type") == "tool":
            print(f"  · {ev['name']} {ev.get('summary') or ''}".rstrip(), file=common.sys.stderr)
        else:
            print(f"  … {ev.get('text', '')}", file=common.sys.stderr)

    result = explore(
        args.prompt,
        device=args.device,
        provider=args.provider,
        model=args.model,
        timeout_s=args.timeout,
        context_dirs=context_dirs,
        allow_raw=args.allow_raw,
        snapshot=args.screenshot,
        on_event=_on_step if args.device else None,
    )
    if not result.ok:
        print(result.error or "ai invocation failed", file=common.sys.stderr)
        return 1
    print(result.text)
    return 0


def cmd_ai_verify_agent(args: argparse.Namespace) -> int:
    from autophone.application.ai_verify import verify_agent_main

    return verify_agent_main(
        input_path=args.input, output_path=args.output,
        provider=args.provider, model=args.model,
    )


def cmd_ai_diagnose(args: argparse.Namespace) -> int:
    from autophone.application.ai_diagnose import diagnose_run

    run_root = args.out_root or (common.STATE_ROOT / "runs" / "platform")
    run_dir = common._find_run_dir(run_root, args.run_id)
    out = diagnose_run(
        run_dir, provider=args.provider, model=args.model, allow_raw=args.allow_raw,
        include_images=not args.no_images,
    )
    print(common.json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


def cmd_ai_draft(args: argparse.Namespace) -> int:
    from autophone.application.ai_draft import draft_operation

    source_text = None
    if args.from_file:
        source_text = common.Path(args.from_file).read_text(encoding="utf-8")
    out = draft_operation(
        args.app, args.requirement,
        source_text=source_text, from_ref=args.from_ref, note=args.note, run_id=args.run_id,
        provider=args.provider, model=args.model,
    )
    print(common.json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


def cmd_ai_convert(args: argparse.Namespace) -> int:
    from autophone.application.ai_convert import convert_case

    out = convert_case(
        args.app, args.case_id,
        note=args.note, provider=args.provider, model=args.model,
    )
    print(common.json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


def cmd_ai_convert_material(args: argparse.Namespace) -> int:
    """集成用例 xlsx → 三份草稿（AI 降解器 P2，§六(2)）。逐行降解，全落 draft。"""
    common.assert_assets_writable("convert material to asset drafts")
    from autophone.application.ai_conversion import (
        convert_row,
        dry_run_convert_material,
        read_material_rows,
        requires_order_payment_isolation,
    )

    rows = read_material_rows(common.Path(args.xlsx))
    indexed_rows = list(enumerate(rows))
    if args.row is not None:
        indexed_rows = [(args.row, rows[args.row])] if 0 <= args.row < len(rows) else []
    else:
        if args.offset < 0:
            print(common.json.dumps({"ok": False, "error": "--offset must be >= 0"}, ensure_ascii=False))
            return 2
        indexed_rows = indexed_rows[args.offset:]
    if args.row is None and args.limit is not None:
        indexed_rows = indexed_rows[: args.limit]
    excluded_order_payment_count = 0
    if args.exclude_order_payment:
        before = len(indexed_rows)
        indexed_rows = [pair for pair in indexed_rows if not requires_order_payment_isolation(pair[1])]
        excluded_order_payment_count = before - len(indexed_rows)
    if not indexed_rows:
        reason = "no rows to convert after --exclude-order-payment" if args.exclude_order_payment else "no rows to convert"
        print(common.json.dumps({"ok": False, "error": reason}, ensure_ascii=False))
        return 1

    if args.dry_run_summary:
        def _progress(event: dict[str, Any]) -> None:
            prefix = (
                f"[dry-run] {event['ordinal']}/{event['total']} "
                f"row={event['row']} external_id={event.get('external_id') or '-'}"
            )
            if event["event"] == "start":
                print(f"{prefix} start {event.get('item') or ''}", file=common.sys.stderr, flush=True)
                return
            if event.get("ok"):
                missing = len(event.get("missing_operations") or [])
                manual = len(event.get("needs_manual") or [])
                duration = event.get("duration_s")
                duration_text = f" duration={duration:.3f}s" if isinstance(duration, (int, float)) else ""
                print(
                    f"{prefix} done status={event.get('status') or 'draft'} "
                    f"missing_ops={missing} needs_manual={manual}{duration_text}",
                    file=common.sys.stderr, flush=True,
                )
                return
            duration = event.get("duration_s")
            duration_text = f" duration={duration:.3f}s" if isinstance(duration, (int, float)) else ""
            print(f"{prefix} failed{duration_text} error={event.get('error')}", file=common.sys.stderr, flush=True)

        out = dry_run_convert_material(
            args.app, indexed_rows, note=args.note, provider=args.provider, model=args.model,
            retries=args.retries, progress=_progress,
        )
        out["source_total"] = len(rows)
        out["row_indices"] = {"first": indexed_rows[0][0], "last": indexed_rows[-1][0]}
        out["excluded_order_payment_count"] = excluded_order_payment_count
        print(common.json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("ok") else 1

    existing_external_ids: set[str] = set()
    if args.resume:
        existing_external_ids = _material_existing_external_ids(args.app)
        print(
            f"[convert] resume enabled existing_source_keys={len(existing_external_ids)}",
            file=common.sys.stderr,
            flush=True,
        )

    results = []
    ok_all = True
    total = len(indexed_rows)
    for ordinal, (row_index, row) in enumerate(indexed_rows, start=1):
        import time

        started = time.monotonic()
        external_id = str(row.get("external_id") or "").strip()
        source_ref = str(row.get("source_ref") or "").strip()
        source_key = external_id or source_ref
        item = row.get("item") or ""
        prefix = (
            f"[convert] {ordinal}/{total} "
            f"row={row_index} external_id={external_id or '-'}"
            + (f" source_ref={source_ref}" if source_ref else "")
        )
        print(f"{prefix} start {item}", file=common.sys.stderr, flush=True)
        if args.resume and source_key and source_key in existing_external_ids:
            duration_s = round(time.monotonic() - started, 3)
            reason = "existing_external_id" if external_id else "existing_source_ref"
            print(
                f"{prefix} skipped {reason} duration={duration_s:.3f}s",
                file=common.sys.stderr,
                flush=True,
            )
            results.append({
                "ok": True,
                "skipped": True,
                "reason": reason,
                "row": row_index,
                "external_id": external_id,
                "source_ref": source_ref or None,
                "item": item,
                "duration_s": duration_s,
            })
            continue

        try:
            out = convert_row(args.app, row, note=args.note, provider=args.provider, model=args.model,
                              retries=args.retries)
        except Exception as exc:
            out = {
                "ok": False,
                "error": str(exc) or type(exc).__name__,
                "exception_type": type(exc).__name__,
            }
        duration_s = round(time.monotonic() - started, 3)
        out = {
            **out,
            "row": row_index,
            "external_id": external_id or None,
            "source_ref": source_ref or None,
            "item": item,
            "duration_s": duration_s,
        }
        ok_all = ok_all and bool(out.get("ok"))
        results.append(out)
        if args.resume and out.get("ok") and source_key:
            existing_external_ids.add(source_key)
        if out.get("ok"):
            print(
                f"{prefix} done status={out.get('status') or 'draft'} "
                f"case_id={out.get('case_id') or '-'} "
                f"missing_ops={len(out.get('missing_operations') or [])} "
                f"needs_manual={len(out.get('needs_manual') or [])} "
                f"duration={duration_s:.3f}s",
                file=common.sys.stderr,
                flush=True,
            )
        else:
            print(
                f"{prefix} failed duration={duration_s:.3f}s error={out.get('error')}",
                file=common.sys.stderr,
                flush=True,
            )
    print(common.json.dumps({
        "total": len(results),
        "excluded_order_payment_count": excluded_order_payment_count,
        "results": results,
    }, ensure_ascii=False, indent=2))
    return 0 if ok_all else 1


def _material_existing_external_ids(app: str) -> set[str]:
    """Return source keys already present in the app case library.

    Old marketing rows use ``raw.external_id``; source sheets without that
    column use the deterministic ``raw.source_ref`` instead.
    """
    profile = common.load_app_profile(app)
    path = profile.cases_path()
    if not path.exists():
        return set()
    try:
        import yaml
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"cannot read existing cases for --resume: {path}: {exc}") from exc
    out: set[str] = set()
    for case in doc.get("cases") or []:
        if not isinstance(case, dict):
            continue
        raw = case.get("raw") or {}
        if not isinstance(raw, dict):
            continue
        source_key = str(raw.get("external_id") or raw.get("source_ref") or "").strip()
        if source_key:
            out.add(source_key)
    return out


def cmd_ai_pull_extraction(args: argparse.Namespace) -> int:
    from autophone.application.ai_extract import run_extraction_pull

    out = run_extraction_pull(
        args.app, args.case_id,
        max_draft_attempts=args.max_draft_attempts,
        provider=args.provider, model=args.model,
    )
    print(common.json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


def cmd_ai_propose_cases(args: argparse.Namespace) -> int:
    if args.commit:
        common.assert_assets_writable("commit AI case proposals")
    from autophone.application.ai_propose import propose_cases, commit_candidates

    source_text = common.Path(args.from_file).read_text(encoding="utf-8") if args.from_file else (args.requirement or "")
    out = propose_cases(
        args.app, source_text, max_candidates=args.max_candidates,
        provider=args.provider, model=args.model,
    )
    if out.get("ok") and args.commit:
        committed = commit_candidates(args.app, out["candidates"], note=args.note)
        out["commit"] = committed
        out["ok"] = bool(committed.get("ok"))
    print(common.json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


def cmd_ai_suggest_ops(args: argparse.Namespace) -> int:
    if not args.dry_run:
        common.assert_assets_writable("write AI operation suggestions")
    from autophone.application.ai_suggest_ops import suggest_for_case, suggest_for_drafts

    if args.case:
        out = suggest_for_case(
            args.app, args.case, write=not args.dry_run,
            provider=args.provider, model=args.model,
        )
    else:
        out = suggest_for_drafts(
            args.app, status=args.status, write=not args.dry_run,
            provider=args.provider, model=args.model,
        )
    print(common.json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


def cmd_ai_task(args: argparse.Namespace) -> int:
    from autophone.application.ai_agent import run_task

    out = run_task(
        args.task, app=args.app, familiar=args.familiar, approved=args.approved,
        provider=args.provider, model=args.model, timeout_s=args.timeout,
    )
    print(common.json.dumps(out, ensure_ascii=False, indent=2))
    # pending_confirmation 不是失败——退 0 让人据 plan/evidence 决策（带 --approved 二次跑落地）。
    return 0 if out.get("ok") else 1


def cmd_ai_report(args: argparse.Namespace) -> int:
    from autophone.application.ai_agent import report

    result = report(
        args.prompt, device=args.device, provider=args.provider,
        model=args.model, timeout_s=args.timeout,
    )
    if not result.ok:
        print(result.error or "ai report failed", file=common.sys.stderr)
        return 1
    print(result.text)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    ai = sub.add_parser("ai", help="call local agent CLI (claude/codex) for non-deterministic work")
    ai_sub = ai.add_subparsers(dest="ai_cmd", required=True)
    ai_doctor = ai_sub.add_parser("doctor", help="CLI capability detection (presence/version/default provider)")
    ai_doctor.add_argument("--probe", action="store_true", help="run one tiny real call to prove the bridge")
    ai_doctor.add_argument("--provider", choices=["claude", "codex", "auto"], default=None)
    ai_doctor.add_argument("--timeout", type=float, default=30.0)
    ai_doctor.set_defaults(func=cmd_ai_doctor)
    ai_ask = ai_sub.add_parser("ask", help="one-off prompt to the agent (text out)")
    ai_ask.add_argument("prompt")
    ai_ask.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    ai_ask_mode = ai_ask.add_mutually_exclusive_group()
    ai_ask_mode.add_argument("--device", action="store_true", help="drive device via vphone_mcp under ScreenLock")
    ai_ask_mode.add_argument(
        "--screenshot",
        action="store_true",
        help="explicitly consent to capture and send this request's current-screen snapshot; capture failure aborts",
    )
    ai_ask.add_argument("--provider", choices=["claude", "codex", "auto"], default=None)
    ai_ask.add_argument("--model", default=None)
    ai_ask.add_argument("--timeout", type=float, default=300.0)  # 镜像 ai.DEFAULT_TIMEOUT_S；不顶层 import AI 层（回归链严禁触达）
    ai_ask.add_argument("--context-run", default=None, help="feed a run's sanitized ai-context bundle (§5.2)")
    ai_ask.add_argument("--allow-raw", action="store_true", help="DANGER: include raw/forbidden artifacts in the AI context")
    ai_ask.set_defaults(func=cmd_ai_ask)
    ai_verify = ai_sub.add_parser("verify-agent", help="verification AGENT_CMD adapter: drive device per checkpoint (AI-2)")
    ai_verify.add_argument("--input", default=None, help="verification-agent-input.json (default: $AUTOPHONE_VERIFICATION_INPUT)")
    ai_verify.add_argument("--output", default=None, help="output path (default: $AUTOPHONE_VERIFICATION_OUTPUT)")
    ai_verify.add_argument("--provider", choices=["claude", "codex", "auto"], default=None)
    ai_verify.add_argument("--model", default=None)
    ai_verify.set_defaults(func=cmd_ai_verify_agent)
    ai_diagnose = ai_sub.add_parser("diagnose", help="AI failure attribution over a run's sanitized artifacts (AI-1)")
    ai_diagnose.add_argument("run_id")
    ai_diagnose.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    ai_diagnose.add_argument("--provider", choices=["claude", "codex", "auto"], default=None)
    ai_diagnose.add_argument("--model", default=None)
    ai_diagnose.add_argument("--allow-raw", action="store_true", help="DANGER: include raw screenshots/captures in the diagnosis context")
    ai_diagnose.add_argument("--no-images", action="store_true", help="不随 prompt 附带净化包里 ai_policy 放行的截图（治理判定不变）")
    ai_diagnose.set_defaults(func=cmd_ai_diagnose)
    ai_draft = ai_sub.add_parser("draft-operation", help="draft a reusable operation recipe into the registry as status=draft (AI-4)")
    ai_draft.add_argument("app")
    ai_draft.add_argument("requirement", help="what the operation should accomplish")
    ai_draft.add_argument("--from-file", default=None, help="conversation trace / exploration notes file to distill steps from")
    ai_draft.add_argument("--from-ref", default=None, help="provenance.from: source conversation/session id")
    ai_draft.add_argument("--note", default=None, help="extra provenance note")
    ai_draft.add_argument("--run-id", default=None, help="provenance.run_id if distilled from a run")
    ai_draft.add_argument("--provider", choices=["claude", "codex", "auto"], default=None)
    ai_draft.add_argument("--model", default=None)
    ai_draft.set_defaults(func=cmd_ai_draft)

    ai_convert = ai_sub.add_parser("convert", help="convert an NL case into a draft typed-IR recipe (AI-5, P1 转换器脊)")
    ai_convert.add_argument("app")
    ai_convert.add_argument("case_id", help="case id to convert, e.g. R47")
    ai_convert.add_argument("--note", default=None, help="extra provenance note")
    ai_convert.add_argument("--provider", choices=["claude", "codex", "auto"], default=None)
    ai_convert.add_argument("--model", default=None)
    ai_convert.set_defaults(func=cmd_ai_convert)

    ai_convert_material = ai_sub.add_parser(
        "convert-material",
        help="降解集成用例 xlsx 一行 → case+api_expectations+fixtures 三份草稿（P2，§六）")
    ai_convert_material.add_argument("app")
    ai_convert_material.add_argument("xlsx", help="集成用例 xlsx（营销/点餐模块）")
    ai_convert_material.add_argument("--row", type=int, default=None, help="只降解第 N 行（0 起）")
    ai_convert_material.add_argument("--offset", type=int, default=0, help="从第 N 行开始批量降解（0 起；与 --limit 配合分批）")
    ai_convert_material.add_argument("--limit", type=int, default=None, help="只降解前 N 行")
    ai_convert_material.add_argument(
        "--dry-run-summary", action="store_true",
        help="逐行降解到临时资产，只输出分箱统计，不写真实 cases/api_expectations/fixtures")
    ai_convert_material.add_argument("--note", default=None, help="extra provenance note")
    ai_convert_material.add_argument("--provider", choices=["claude", "codex", "auto"], default=None)
    ai_convert_material.add_argument("--model", default=None)
    ai_convert_material.add_argument(
        "--retries", type=int, default=1,
        help="单行瞬态失败（超时/截断 JSON/schema 漂移）重打次数（默认 1；0=不重试）")
    ai_convert_material.add_argument(
        "--resume", action="store_true",
        help="断点续跑：跳过用例库里 raw.external_id 已存在的行，避免重复追加草稿")
    ai_convert_material.add_argument(
        "--exclude-order-payment", action="store_true",
        help="按来源标题/步骤/API 静态排除支付或下单路径；排除行不发送给 AI、不写入资产")
    ai_convert_material.set_defaults(func=cmd_ai_convert_material)

    ai_pull = ai_sub.add_parser("pull-extraction",
                                help="auto-pull: convert a case, auto-draft the missing precondition operation on needs_extraction (AI-6, P3 抽取环)")
    ai_pull.add_argument("app")
    ai_pull.add_argument("case_id", help="case id to convert + pull extraction for, e.g. R80")
    ai_pull.add_argument("--max-draft-attempts", type=int, default=2, dest="max_draft_attempts",
                         help="auto-draft 连续失败几次后升 stall（默认 2）")
    ai_pull.add_argument("--provider", choices=["claude", "codex", "auto"], default=None)
    ai_pull.add_argument("--model", default=None)
    ai_pull.set_defaults(func=cmd_ai_pull_extraction)

    ai_propose = ai_sub.add_parser("propose-cases",
                                   help="propose candidate cases from free-form requirements for human curation")
    ai_propose.add_argument("app")
    ai_propose.add_argument("requirement", nargs="?", default=None, help="free-form requirement text (or use --from-file)")
    ai_propose.add_argument("--from-file", default=None, help="requirements doc / digest file to propose from")
    ai_propose.add_argument("--max-candidates", type=int, default=10, dest="max_candidates")
    ai_propose.add_argument("--commit", action="store_true", help="write proposed candidates into the case library as status=draft (human then curates by activating)")
    ai_propose.add_argument("--note", default=None, help="extra provenance note")
    ai_propose.add_argument("--provider", choices=["claude", "codex", "auto"], default=None)
    ai_propose.add_argument("--model", default=None)
    ai_propose.set_defaults(func=cmd_ai_propose_cases)

    ai_suggest = ai_sub.add_parser("suggest-ops",
                                   help="recommend operation-library ops for a case's preconditions/post-actions into suggested_setup/teardown (待确认)")
    ai_suggest.add_argument("app")
    ai_suggest.add_argument("--case", default=None, help="单条用例 id（如 R47）；省略则按 --status 批量")
    ai_suggest.add_argument("--status", default="draft", help="批量模式只处理该状态的用例（默认 draft）")
    ai_suggest.add_argument("--dry-run", action="store_true", help="只算不写回 suggested_*")
    ai_suggest.add_argument("--provider", choices=["claude", "codex", "auto"], default=None)
    ai_suggest.add_argument("--model", default=None)
    ai_suggest.set_defaults(func=cmd_ai_suggest_ops)

    ai_task = ai_sub.add_parser("task",
                                help="best-effort agent: plan a one-off task, pause before irreversible/outward actions (AI-8, P5 Lane 2)")
    ai_task.add_argument("task", help="自然语言任务（如「给张三发条消息」）")
    ai_task.add_argument("--app", default=None, help="被测 app id（有 profile 才算「已知」；未知/首次走整盘预批）")
    ai_task.add_argument("--familiar", dest="familiar", action="store_true", default=None,
                         help="人工强制「熟悉」（默认不带 = 从当前构建的成功驱动史证据派生，见 familiarity.py）")
    ai_task.add_argument("--unfamiliar", dest="familiar", action="store_false",
                         help="人工强制「不熟悉」→ 整盘预批（覆写证据派生）")
    ai_task.add_argument("--approved", action="store_true", help="人已就上轮 plan 批准 → 放行执行（含不可逆动作）")
    ai_task.add_argument("--provider", choices=["claude", "codex", "auto"], default=None)
    ai_task.add_argument("--model", default=None)
    ai_task.add_argument("--timeout", type=float, default=300.0)  # 镜像 ai.DEFAULT_TIMEOUT_S；不顶层 import AI 层
    ai_task.set_defaults(func=cmd_ai_task)

    ai_report = ai_sub.add_parser("report",
                                  help="best-effort agent: read-only screen report, zero side-effects (AI-8, P5 Lane 3)")
    ai_report.add_argument("prompt", help="要看/汇报什么（如「今天有哪些待处理订单」）")
    ai_report.add_argument("--device", action="store_true", help="经 vphone_mcp 给设备工具面（只读约束靠契约，碰屏仍进 ScreenLock）；否则喂当前屏纯推理")
    ai_report.add_argument("--provider", choices=["claude", "codex", "auto"], default=None)
    ai_report.add_argument("--model", default=None)
    ai_report.add_argument("--timeout", type=float, default=300.0)
    ai_report.set_defaults(func=cmd_ai_report)
