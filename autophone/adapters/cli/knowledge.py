from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from autophone.adapters.cli import _common as common


def _knowledge_run_root(args: argparse.Namespace) -> Path:
    return args.out_root or (common.STATE_ROOT / "runs" / "platform")


def cmd_knowledge_inventory(args: argparse.Namespace) -> int:
    from autophone.knowledge.inventory import inventory

    print(common.json.dumps(inventory(args.app), ensure_ascii=False, indent=2))
    return 0


def cmd_knowledge_compile(args: argparse.Namespace) -> int:
    from autophone.application import run_index
    from autophone.knowledge.store import merge_drafts
    from autophone.observation.compiler import compile_and_write, propose

    run_root = _knowledge_run_root(args)
    run_dir = common._find_run_dir(run_root, args.target_run_id)
    path = compile_and_write(run_dir, app=args.app)
    indexed = (run_dir / "platform-run-result.json").is_file()
    if indexed:
        run_index.ingest_run(run_dir, run_root=run_root)
    payload = common.json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, Any] = {
        "observations": str(path),
        "count": len(payload.get("observations") or []),
        "indexed": indexed,
    }
    if args.propose or args.write_catalog:
        drafts = propose(payload)
        out["draft_claims"] = drafts
        if args.write_catalog:
            out["catalog"] = str(merge_drafts(args.app, drafts))
    print(common.json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_knowledge_verify(args: argparse.Namespace) -> int:
    from autophone.knowledge.evidence_verifier import verify
    from autophone.knowledge.store import load_claims

    observations: list[dict[str, Any]] = []
    for path in _knowledge_run_root(args).glob("*/*/observations.json"):
        try:
            observations.extend(common.json.loads(path.read_text(encoding="utf-8")).get("observations") or [])
        except (OSError, common.json.JSONDecodeError):
            continue
    claims = load_claims(args.app).get("claims") or []
    decisions = [
        verify(
            claim,
            observations,
            current_subject_build_key=args.subject_build_key,
        )
        for claim in claims
    ]
    print(common.json.dumps({"app": args.app, "decisions": decisions}, ensure_ascii=False, indent=2))
    return 0


def cmd_knowledge_registry_diff(args: argparse.Namespace) -> int:
    from autophone.packages import store as packages
    from autophone.knowledge.diff import registry_diff

    manifest = packages.load_manifest(args.app)
    out = registry_diff(
        manifest,
        manifest,
        before_slot=args.before_slot,
        after_slot=args.after_slot,
    )
    print(common.json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["status"] != "build_not_found" else 1


def cmd_knowledge_static_snapshot(args: argparse.Namespace) -> int:
    from autophone.assets.static_snapshot import write_snapshot

    output = args.output or args.ipa.with_suffix(".static-snapshot.json")
    path = write_snapshot(args.ipa, output)
    print(path)
    return 0


def cmd_knowledge_static_diff(args: argparse.Namespace) -> int:
    from autophone.knowledge.diff import static_diff

    before = common.json.loads(args.before.read_text(encoding="utf-8"))
    after = common.json.loads(args.after.read_text(encoding="utf-8"))
    print(common.json.dumps(static_diff(before, after), ensure_ascii=False, indent=2))
    return 0


def cmd_knowledge_query(args: argparse.Namespace) -> int:
    from autophone.knowledge.query import KnowledgeQueryEngine

    params = common.json.loads(args.params) if args.params else {}
    result = KnowledgeQueryEngine(
        args.app,
        run_root=_knowledge_run_root(args),
    ).execute(args.number, **params)
    print(common.json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "answer" else 1


def cmd_knowledge_prep_card(args: argparse.Namespace) -> int:
    from autophone.knowledge.preparation_card import build_preparation_card

    card = build_preparation_card(
        args.app,
        args.case_id,
        trial_draft_recipes=bool(args.trial_draft_recipes),
        run_root=_knowledge_run_root(args),
    )
    print(common.json.dumps(card, ensure_ascii=False, indent=2))
    return 0 if card.get("status") != "case_not_found" else 1


def cmd_knowledge_view(args: argparse.Namespace) -> int:
    from autophone.knowledge.app_view import build_app_knowledge_view

    view = build_app_knowledge_view(
        args.app,
        statuses=args.status or None,
        trial_draft_recipes=bool(args.trial_draft_recipes),
        run_root=_knowledge_run_root(args),
    )
    print(common.json.dumps(view, ensure_ascii=False, indent=2))
    return 1 if view.get("error") else 0


def cmd_knowledge_fixture_requirements(args: argparse.Namespace) -> int:
    from autophone.assets.apps import load_app_profile
    from autophone.assets.fixture_requirements import validate_app_fixture_requirements

    out = validate_app_fixture_requirements(load_app_profile(args.app))
    print(common.json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["valid"] else 1


def cmd_knowledge_golden_path_plan(args: argparse.Namespace) -> int:
    from autophone.knowledge.golden_path import plan_golden_path

    out = plan_golden_path(args.app, args.path_id, run_root=_knowledge_run_root(args))
    print(common.json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_knowledge_golden_path_verify(args: argparse.Namespace) -> int:
    from autophone.knowledge.golden_path import verify_golden_path_run

    run_dir = common._find_run_dir(_knowledge_run_root(args), args.target_run_id)
    out = verify_golden_path_run(args.app, args.path_id, run_dir)
    print(common.json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["outcome"] == "passed" else 1


def cmd_knowledge_coverage(args: argparse.Namespace) -> int:
    from autophone.knowledge.coverage import evaluate, load_coverage

    if args.cells is None:
        raw = load_coverage(args.app)
    else:
        text = args.cells.read_text(encoding="utf-8")
        raw = common.yaml.safe_load(text) if args.cells.suffix.lower() in {".yaml", ".yml"} else common.json.loads(text)
    cells = raw.get("cells") if isinstance(raw, dict) else raw
    print(common.json.dumps(evaluate(cells or []), ensure_ascii=False, indent=2))
    return 0


def cmd_knowledge_generate(args: argparse.Namespace) -> int:
    from autophone.knowledge.docs import generate_inventory

    path = generate_inventory(args.output)
    print(path)
    return 0


def cmd_knowledge_experiments(args: argparse.Namespace) -> int:
    from autophone.knowledge.experiments import load_experiments

    print(common.json.dumps(load_experiments(args.app), ensure_ascii=False, indent=2))
    return 0


def cmd_knowledge_control_emit(args: argparse.Namespace) -> int:
    from autophone.knowledge.experiments import emit_control

    run_dir = common._find_run_dir(_knowledge_run_root(args), args.target_run_id)
    print(common.json.dumps(
        emit_control(
            run_dir,
            app=args.app,
            experiment_id=args.experiment,
            arm_id=args.arm,
        ),
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def cmd_knowledge_bind(args: argparse.Namespace) -> int:
    from autophone.knowledge.implementation_binding import bind
    from autophone.knowledge.store import load_claims

    snapshot = common.json.loads(args.snapshot.read_text(encoding="utf-8"))
    print(common.json.dumps(
        bind(load_claims(args.app).get("claims") or [], snapshot),
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def cmd_knowledge_family_suggest(args: argparse.Namespace) -> int:
    from autophone.assets.apps import load_app_profile
    from autophone.application.cases import CaseRecipeApplication
    from autophone.knowledge.family_candidates import save_candidates

    profile = load_app_profile(args.app)
    rows = CaseRecipeApplication(profile).registry_rows()
    if args.case_id:
        missing = set(args.case_id) - {row["id"] for row in rows}
        if missing:
            raise ValueError(f"Case not found: {sorted(missing)}")
        rows = [row for row in rows if row["id"] in args.case_id]
    else:
        rows = [row for row in rows if not row.get("family_id")]
    out = save_candidates(profile, rows)
    print(common.json.dumps({"app": args.app, "candidates": out}, ensure_ascii=False, indent=2))
    return 0


def cmd_knowledge_family_accept(args: argparse.Namespace) -> int:
    from autophone.assets.apps import load_app_profile
    from autophone.application.cases import CaseLifecycleApplication, CaseLifecycleApplicationError

    application = CaseLifecycleApplication(
        load_app_profile(args.app), negctl_root=common.STATE_ROOT / "runs" / "negctl",
    )
    try:
        out = application.accept_family_candidate(args.candidate_id)
    except CaseLifecycleApplicationError as exc:
        print(common.json.dumps({"status": "rejected", "code": exc.status_code,
                                 "detail": exc.detail}, ensure_ascii=False))
        return 1
    print(common.json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    knowledge = sub.add_parser("knowledge", help="App knowledge compile, verify, diff and query")
    knowledge_sub = knowledge.add_subparsers(dest="knowledge_cmd", required=True)
    family_suggest = knowledge_sub.add_parser("family-suggest", help="write reviewable Family suggestions to state storage")
    family_suggest.add_argument("--case-id", action="append", default=None)
    family_suggest.set_defaults(func=cmd_knowledge_family_suggest, needs_app=True)
    family_accept = knowledge_sub.add_parser("family-accept", help="explicitly accept one Family suggestion using CAS")
    family_accept.add_argument("candidate_id")
    family_accept.set_defaults(func=cmd_knowledge_family_accept, needs_app=True)
    knowledge_inventory = knowledge_sub.add_parser("inventory")
    knowledge_inventory.set_defaults(func=cmd_knowledge_inventory, needs_app=True)
    knowledge_compile = knowledge_sub.add_parser("compile")
    knowledge_compile.add_argument("target_run_id")
    knowledge_compile.add_argument("--propose", action="store_true")
    knowledge_compile.add_argument("--write-catalog", action="store_true")
    knowledge_compile.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    knowledge_compile.set_defaults(func=cmd_knowledge_compile, needs_app=True)
    knowledge_verify = knowledge_sub.add_parser("verify")
    knowledge_verify.add_argument("--subject-build-key", default=None)
    knowledge_verify.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    knowledge_verify.set_defaults(func=cmd_knowledge_verify, needs_app=True)
    knowledge_registry_diff = knowledge_sub.add_parser("registry-diff")
    knowledge_registry_diff.add_argument("before_slot")
    knowledge_registry_diff.add_argument("after_slot")
    knowledge_registry_diff.set_defaults(func=cmd_knowledge_registry_diff, needs_app=True)
    knowledge_snapshot = knowledge_sub.add_parser("static-snapshot")
    knowledge_snapshot.add_argument("ipa", type=common.Path)
    knowledge_snapshot.add_argument("--output", type=common.Path, default=None)
    knowledge_snapshot.set_defaults(func=cmd_knowledge_static_snapshot)
    knowledge_static_diff = knowledge_sub.add_parser("static-diff")
    knowledge_static_diff.add_argument("before", type=common.Path)
    knowledge_static_diff.add_argument("after", type=common.Path)
    knowledge_static_diff.set_defaults(func=cmd_knowledge_static_diff)
    knowledge_query = knowledge_sub.add_parser("query")
    knowledge_query.add_argument("number", type=int, choices=range(1, 9))
    knowledge_query.add_argument("--params", default="{}", help="query parameters as JSON")
    knowledge_query.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    knowledge_query.set_defaults(func=cmd_knowledge_query, needs_app=True)
    knowledge_prep_card = knowledge_sub.add_parser(
        "prep-card", help="unified Case preparation card (read only; never runs a Trial)")
    knowledge_prep_card.add_argument("case_id")
    knowledge_prep_card.add_argument("--trial-draft-recipes", action="store_true")
    knowledge_prep_card.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    knowledge_prep_card.set_defaults(func=cmd_knowledge_prep_card, needs_app=True)
    knowledge_view = knowledge_sub.add_parser(
        "view", help="App knowledge view: per-Case relations and blockers (read only)")
    knowledge_view.add_argument(
        "--status", action="append", default=None,
        help="only Cases with this status (repeatable), e.g. active or draft")
    knowledge_view.add_argument("--trial-draft-recipes", action="store_true")
    knowledge_view.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    knowledge_view.set_defaults(func=cmd_knowledge_view, needs_app=True)
    knowledge_fixtures = knowledge_sub.add_parser(
        "fixture-requirements", help="validate declared business Fixture requirements")
    knowledge_fixtures.set_defaults(func=cmd_knowledge_fixture_requirements, needs_app=True)
    knowledge_golden = knowledge_sub.add_parser(
        "golden-path", help="golden path plan and Run evidence verification (never executes)")
    knowledge_golden_sub = knowledge_golden.add_subparsers(
        dest="knowledge_golden_cmd", required=True)
    knowledge_golden_plan = knowledge_golden_sub.add_parser("plan")
    knowledge_golden_plan.add_argument("path_id")
    knowledge_golden_plan.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    knowledge_golden_plan.set_defaults(func=cmd_knowledge_golden_path_plan, needs_app=True)
    knowledge_golden_verify = knowledge_golden_sub.add_parser("verify")
    knowledge_golden_verify.add_argument("path_id")
    knowledge_golden_verify.add_argument("target_run_id")
    knowledge_golden_verify.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    knowledge_golden_verify.set_defaults(func=cmd_knowledge_golden_path_verify, needs_app=True)
    knowledge_coverage = knowledge_sub.add_parser("coverage")
    knowledge_coverage.add_argument("cells", type=common.Path, nargs="?", default=None)
    knowledge_coverage.set_defaults(func=cmd_knowledge_coverage, needs_app=True)
    knowledge_generate = knowledge_sub.add_parser("generate")
    knowledge_generate.add_argument("--output", type=common.Path, default=None)
    knowledge_generate.set_defaults(func=cmd_knowledge_generate)
    knowledge_bind = knowledge_sub.add_parser("bind")
    knowledge_bind.add_argument("snapshot", type=common.Path)
    knowledge_bind.set_defaults(func=cmd_knowledge_bind, needs_app=True)
    knowledge_experiments = knowledge_sub.add_parser(
        "experiments", help="declared controlled experiments (M3b source of truth)")
    knowledge_experiments.set_defaults(func=cmd_knowledge_experiments, needs_app=True)
    knowledge_control = knowledge_sub.add_parser(
        "control", help="emit knowledge-control.json for one arm of a declared experiment")
    knowledge_control_sub = knowledge_control.add_subparsers(
        dest="knowledge_control_cmd", required=True)
    knowledge_control_emit = knowledge_control_sub.add_parser("emit")
    knowledge_control_emit.add_argument("target_run_id")
    knowledge_control_emit.add_argument("--experiment", required=True)
    knowledge_control_emit.add_argument("--arm", required=True)
    knowledge_control_emit.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    knowledge_control_emit.set_defaults(func=cmd_knowledge_control_emit, needs_app=True)
