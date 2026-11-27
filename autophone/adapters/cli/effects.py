from __future__ import annotations

import argparse

from autophone.adapters.cli import _common as common


def cmd_effects_audit(args: argparse.Namespace) -> int:
    from autophone.execution.effect_audit import audit_apps

    out = audit_apps(list(args.apps or []))
    print(common.json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    effects = sub.add_parser("effects", help="effect policy audit/report-only tooling")
    effects_sub = effects.add_subparsers(dest="effects_cmd", required=True)
    effects_audit = effects_sub.add_parser("audit", help="report irreversible effects before A3 enforce")
    effects_audit.add_argument("--apps", nargs="+", default=None)
    effects_audit.set_defaults(func=cmd_effects_audit)
