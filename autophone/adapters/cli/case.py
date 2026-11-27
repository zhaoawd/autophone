from __future__ import annotations

import argparse
from typing import Any

from autophone.adapters.cli import _common as common


def cmd_case_import(args: argparse.Namespace) -> int:
    """从通用用例模板 xlsx 导入：种子与增量 merge 的新条目均为 draft。"""
    from autophone.application.cases import (
        CaseImportApplication,
        CaseImportApplicationError,
    )

    profile = common.load_app_profile(args.app_name)
    try:
        imported = CaseImportApplication(profile).import_source(
            args.xlsx,
            dry_run=args.dry_run,
        )
    except CaseImportApplicationError as exc:
        raise SystemExit(str(exc)) from exc
    if imported["mode"] == "merge" and not imported["added"]:
        print(common.json.dumps({"app": args.app_name, "added": [], "note": "无新增（按 external_id 去重）"},
                         ensure_ascii=False, indent=2))
        return 0
    payload = (
        {
            "app": args.app_name,
            "mode": "seed",
            "dry_run": args.dry_run,
            "count": imported["count"],
            "out": imported["out"],
        }
        if imported["mode"] == "seed"
        else {
            "app": args.app_name,
            "mode": "merge",
            "dry_run": args.dry_run,
            "added": imported["added"],
        }
    )
    print(common.json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_case_lint(args: argparse.Namespace) -> int:
    """CI 用例 lint：校验 Case Registry v2 与 active/ui_ready AssetAdmission。

    Registry 结构/整库不变量或 blocked_asset（live recipe 缺失/非法）归入 error，
    退出 1；历史兼容债务归入 warning，不改变退出码。省略 app_name 时扫全部 app。
    """
    from autophone.application.cases import CaseRecipeApplication

    profiles = ([common.load_app_profile(args.app_name)] if args.app_name else common.list_app_profiles())
    report: list[dict[str, Any]] = []
    error_count = 0
    warning_count = 0
    build_id = getattr(args, "build_id", None)
    for profile in profiles:
        result = CaseRecipeApplication(profile).lint(build_id=build_id)
        error_count += result["error_count"]
        warning_count += result["warning_count"]
        report.append(result)
    print(common.json.dumps({
        "apps": report,
        "error_count": error_count,
        "warning_count": warning_count,
        # 兼容旧输出；原 finding 是唯一阻断项，因此新语义等同全部 error 数。
        "finding_count": error_count,
    }, ensure_ascii=False, indent=2))
    return 1 if error_count else 0


def register(sub: argparse._SubParsersAction) -> None:
    case = sub.add_parser("case", help="case library: import from the generic xlsx template")
    case_sub = case.add_subparsers(dest="case_cmd", required=True)
    case_import = case_sub.add_parser(
        "import", help="import cases from the general case-template xlsx layout (seed or merge)")
    case_import.add_argument("app_name")
    case_import.add_argument("xlsx", type=common.Path, help="通用用例模板 xlsx 路径")
    case_import.add_argument("--dry-run", action="store_true", help="只打将写/追加的条数，不落盘")
    case_import.set_defaults(func=cmd_case_import)
    case_lint = case_sub.add_parser(
        "lint", help="validate Case Registry v2 and active/ui_ready AssetAdmission (CI gate)")
    case_lint.add_argument("app_name", nargs="?", default=None,
                           help="app id; omit to audit every app under apps/")
    case_lint.add_argument(
        "--build-id",
        default=None,
        help="Package Store build_id for resolving app-symbols.v1 bindings (defaults to promoted build)",
    )
    case_lint.set_defaults(func=cmd_case_lint)
