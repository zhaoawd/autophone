from __future__ import annotations

import argparse

from autophone.adapters.cli import _common as common


def cmd_self_update(args: argparse.Namespace) -> int:
    from autophone.application.release_manager import ReleaseError, ReleaseManager

    try:
        manager = ReleaseManager(args.install_root, keep_releases=args.keep_releases)
        action = args.self_update_action
        if action == "status":
            result = manager.status()
        elif action == "check":
            result = manager.check(args.source, version=args.release_version, level="stage")
        elif action == "stage":
            staged = manager.stage(args.source, version=args.release_version)
            result = {
                "status": "staged",
                "version": staged.version,
                "release_dir": str(staged.release_dir),
                "reused": staged.reused,
                "compatibility": staged.compatibility,
            }
        elif action == "apply":
            result = manager.apply(
                source=args.source,
                version=args.release_version,
                allow_irreversible=args.allow_irreversible,
                full=args.full,
                legacy_root=args.adopt,
            )
        elif action == "rollback":
            result = manager.rollback(
                to_version=args.to_version,
                restore_state_snapshot=args.restore_snapshot,
                full=args.full,
            )
        elif action == "recover":
            result = manager.recover()
        else:  # pragma: no cover - argparse closes the vocabulary.
            raise ReleaseError(f"unknown self-update action: {action}")
    except ReleaseError as exc:
        print(common.json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=common.sys.stderr)
        return 1
    print(common.json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    self_update = sub.add_parser("self-update", help="managed release install, upgrade and rollback")
    self_update.add_argument(
        "--install-root",
        type=common.Path,
        default=common.Path(common.os.environ.get("AUTOPHONE_INSTALL_ROOT", "~/autophone")).expanduser(),
    )
    self_update.add_argument("--keep-releases", type=int, default=3)
    self_update_sub = self_update.add_subparsers(dest="self_update_action", required=True)
    self_update_status = self_update_sub.add_parser("status")
    self_update_status.set_defaults(func=cmd_self_update)
    self_update_check = self_update_sub.add_parser("check")
    self_update_check.add_argument("source")
    self_update_check.add_argument("--version", dest="release_version", default=None)
    self_update_check.set_defaults(func=cmd_self_update)
    self_update_stage = self_update_sub.add_parser("stage")
    self_update_stage.add_argument("source")
    self_update_stage.add_argument("--version", dest="release_version", default=None)
    self_update_stage.set_defaults(func=cmd_self_update)
    self_update_apply = self_update_sub.add_parser("apply")
    self_update_apply.add_argument("source", nargs="?", default=None)
    self_update_apply.add_argument("--version", dest="release_version", default=None)
    self_update_apply.add_argument("--allow-irreversible", action="store_true")
    self_update_apply.add_argument("--full", action="store_true")
    self_update_apply.add_argument("--adopt", type=common.Path, default=None, help="import B-class state from a legacy flat install")
    self_update_apply.set_defaults(func=cmd_self_update)
    self_update_rollback = self_update_sub.add_parser("rollback")
    self_update_rollback.add_argument("--to", dest="to_version", default=None)
    self_update_rollback.add_argument("--restore-snapshot", action="store_true")
    self_update_rollback.add_argument("--full", action="store_true")
    self_update_rollback.set_defaults(func=cmd_self_update)
    self_update_recover = self_update_sub.add_parser("recover")
    self_update_recover.set_defaults(func=cmd_self_update)
