from __future__ import annotations

import argparse
from typing import Any

from autophone.adapters.cli import _common as common


def cmd_screens_validate(args: argparse.Namespace) -> int:
    try:
        doc = common.screen_assets.load_screen_asset(args.path)
    except Exception as exc:  # noqa: BLE001 CLI boundary
        print(f"screen asset invalid: {exc}", file=common.sys.stderr)
        return 1
    print(common.json.dumps({
        "ok": True,
        "path": str(args.path),
        "build": (doc.get("meta") or {}).get("build"),
        "screens": [s.get("id") for s in doc.get("screens") or []],
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_screens_draft(args: argparse.Namespace) -> int:
    from autophone.application import screen_draft

    common.assert_assets_writable("draft screen asset")
    profile = common.load_app_profile(args.app)
    samples = common.json.loads(args.samples.read_text(encoding="utf-8"))
    if isinstance(samples, dict):
        samples = samples.get("samples") or []
    try:
        if args.classify_ai:
            samples = screen_draft.classify_ocr_samples_with_ai(
                samples,
                provider=args.provider,
                model=args.model,
                timeout_s=args.timeout_s,
            )
        doc = screen_draft.draft_screens_from_ocr_samples(
            samples,
            app=args.app,
            build=args.build,
            min_samples=args.min_samples,
            must_threshold=args.must_threshold,
            any_threshold=args.any_threshold,
            forbid_threshold=args.forbid_threshold,
            any_min_hits=args.any_min_hits,
        )
        path = args.out or profile.screens_path(args.build)
        screen_draft.write_screen_draft(path, doc, overwrite=args.overwrite)
    except Exception as exc:  # noqa: BLE001 CLI boundary
        print(f"screen draft failed: {exc}", file=common.sys.stderr)
        return 1
    print(common.json.dumps({"ok": True, "path": str(path), "screens": [s["id"] for s in doc["screens"]]},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_screens_telemetry(args: argparse.Namespace) -> int:
    run_root = args.out_root or (common.STATE_ROOT / "runs" / "platform")
    operation_results: list[dict[str, Any]] = []
    for path in run_root.glob("*/*/operation-result.json"):
        try:
            payload = common.json.loads(path.read_text(encoding="utf-8"))
        except (OSError, common.json.JSONDecodeError):
            continue
        if payload.get("app") != args.app:
            continue
        if args.build and payload.get("build") not in (None, args.build):
            continue
        operation_results.append(payload)
    rows = common.screen_assets.checkpoint_rows_from_operation_results(operation_results)
    summary = common.screen_assets.summarize_checkpoint_telemetry(
        rows,
        stale_threshold=args.stale_threshold,
        min_samples=args.min_samples,
    )
    print(common.json.dumps({"app": args.app, "build": args.build, "runs": len(operation_results), **summary},
                     ensure_ascii=False, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    screens = sub.add_parser("screens", help="screen skeleton assets and checkpoint telemetry")
    screens_sub = screens.add_subparsers(dest="screens_cmd", required=True)
    screens_validate = screens_sub.add_parser("validate", help="validate one screens/<build>.yaml asset")
    screens_validate.add_argument("path", type=common.Path)
    screens_validate.set_defaults(func=cmd_screens_validate)
    screens_draft = screens_sub.add_parser("draft", help="draft screens/<build>.yaml from OCR samples JSON")
    screens_draft.add_argument("samples", type=common.Path, help="JSON file: {samples:[{screen_id,ocr}]} or a samples array")
    screens_draft.add_argument("--build", required=True)
    screens_draft.add_argument("--out", type=common.Path, default=None)
    screens_draft.add_argument("--overwrite", action="store_true")
    screens_draft.add_argument("--classify-ai", action="store_true",
                               help="classify raw OCR samples with AI before deterministic draft generation")
    screens_draft.add_argument("--provider", default=None)
    screens_draft.add_argument("--model", default=None)
    screens_draft.add_argument("--timeout-s", type=float, default=120.0)
    screens_draft.add_argument("--min-samples", type=int, default=1)
    screens_draft.add_argument("--must-threshold", type=float, default=1.0)
    screens_draft.add_argument("--any-threshold", type=float, default=0.5)
    screens_draft.add_argument("--forbid-threshold", type=float, default=1.0)
    screens_draft.add_argument("--any-min-hits", type=int, default=1)
    screens_draft.set_defaults(func=cmd_screens_draft, needs_app=True)
    screens_telemetry = screens_sub.add_parser("telemetry", help="summarize operation screen_checkpoint telemetry")
    screens_telemetry.add_argument("--build", default=None)
    screens_telemetry.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    screens_telemetry.add_argument("--stale-threshold", type=float, default=0.5)
    screens_telemetry.add_argument("--min-samples", type=int, default=3)
    screens_telemetry.set_defaults(func=cmd_screens_telemetry, needs_app=True)
