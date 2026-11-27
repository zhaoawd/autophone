from __future__ import annotations

import argparse

from autophone.adapters.cli import _common as common


def _scheduler(args: argparse.Namespace):
    from autophone.application import schedule_runner as scheduler_mod

    return scheduler_mod.Scheduler(
        common._service(args),
        config_path=args.config,
    )


def cmd_schedule_list(args: argparse.Namespace) -> int:
    print(common.json.dumps({"schedules": _scheduler(args).list()}, ensure_ascii=False, indent=2))
    return 0


def cmd_schedule_fire(args: argparse.Namespace) -> int:
    event = _scheduler(args).fire(args.schedule_id, background=False)
    print(common.json.dumps(event, ensure_ascii=False, indent=2))
    return 0 if event.get("status") == "fired" else 1


def cmd_schedule_enable(args: argparse.Namespace) -> int:
    item = _scheduler(args).update_enabled(args.schedule_id, True)
    print(common.json.dumps(item, ensure_ascii=False, indent=2))
    return 0


def cmd_schedule_disable(args: argparse.Namespace) -> int:
    item = _scheduler(args).update_enabled(args.schedule_id, False)
    print(common.json.dumps(item, ensure_ascii=False, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    from autophone.application import schedule_runner as scheduler_mod

    schedule = sub.add_parser("schedule", help="single-process cron schedules for platform runs")
    schedule.add_argument("--config", type=common.Path, default=scheduler_mod.DEFAULT_CONFIG_PATH)
    schedule.add_argument("--out-root", type=common.Path, default=common.argparse.SUPPRESS)
    schedule_sub = schedule.add_subparsers(dest="schedule_cmd", required=True)
    schedule_list = schedule_sub.add_parser("list")
    schedule_list.set_defaults(func=cmd_schedule_list)
    schedule_fire = schedule_sub.add_parser("fire")
    schedule_fire.add_argument("schedule_id")
    schedule_fire.set_defaults(func=cmd_schedule_fire)
    schedule_enable = schedule_sub.add_parser("enable")
    schedule_enable.add_argument("schedule_id")
    schedule_enable.set_defaults(func=cmd_schedule_enable)
    schedule_disable = schedule_sub.add_parser("disable")
    schedule_disable.add_argument("schedule_id")
    schedule_disable.set_defaults(func=cmd_schedule_disable)
