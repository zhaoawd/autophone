from __future__ import annotations

import argparse

from autophone.adapters.cli import _common as common
from autophone.adapters.cli import case as case_commands
from autophone.adapters.cli import package as package_commands
from autophone.adapters.cli import symbols as symbols_commands


def cmd_app_list(args: argparse.Namespace) -> int:
    apps = []
    for profile in common.list_app_profiles():
        apps.append({
            "app": profile.app,
            "display_name": profile.display_name,
            "bundle_id": profile.bundle_id,
            "env": profile.env,
            "cases": str(profile.cases_path()),
        })
    print(common.json.dumps({"apps": apps}, ensure_ascii=False, indent=2))
    return 0


def cmd_app_show(args: argparse.Namespace) -> int:
    profile = common.load_app_profile(args.app_name)
    print(common.json.dumps(profile.raw, ensure_ascii=False, indent=2))
    return 0


def cmd_app_add(args: argparse.Namespace) -> int:
    profile = common.create_app_profile(
        args.app_name,
        args.bundle_id,
        args.display_name,
        env=args.env,
        login_method=args.login,
        reset=args.reset,
    )
    print(common.json.dumps(
        {"created": args.app_name, "profile": profile.raw},
        ensure_ascii=False, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    app = sub.add_parser("app", help="manage app profiles, packages, and cases")
    app_sub = app.add_subparsers(dest="app_cmd", required=True)
    app_list = app_sub.add_parser("list")
    app_list.set_defaults(func=cmd_app_list)
    app_show = app_sub.add_parser("show")
    app_show.add_argument("app_name")
    app_show.set_defaults(func=cmd_app_show)

    app_add = app_sub.add_parser("add", help="register a new app profile (scaffolds apps/<app>/)")
    app_add.add_argument("app_name", help="app id / 目录名（小写 slug）")
    app_add.add_argument("--bundle-id", required=True, dest="bundle_id")
    app_add.add_argument("--display-name", required=True, dest="display_name")
    app_add.add_argument("--env", default=None)
    app_add.add_argument("--login", choices=["script", "manual"], default="manual")
    app_add.add_argument("--reset", choices=["ui_home", "relaunch", "none"], default="ui_home")
    app_add.set_defaults(func=cmd_app_add)
    package_commands.register(app_sub)
    case_commands.register(app_sub)
    symbols_commands.register(app_sub)
