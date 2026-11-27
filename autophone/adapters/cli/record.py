"""Recording CLI: native pointer capture, window probes and draft Case compilation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from autophone.record import case_recipe


def cmd_window_probe(args: argparse.Namespace) -> int:
    from autophone.record.native_window import NativeWindowError, probe

    try:
        result = probe(args.config, window_id=args.window_id)
        print(json.dumps({"ok": True, "snapshot": result}, ensure_ascii=False, indent=2))
        return 0
    except (NativeWindowError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


def cmd_native(args: argparse.Namespace) -> int:
    import sys
    import time
    from autophone.assets.apps import load_app_profile
    from autophone.application.control_plane import require_control_plane
    from autophone.application.device_session import RemoteDeviceSessions, control_http_base_url
    from autophone.rig.control_plane_env import load_control_plane_env

    import math
    if not math.isfinite(args.duration) or args.duration < 0:
        print(json.dumps({"ok": False, "error": "duration must be finite and nonnegative"}))
        return 2
    load_control_plane_env()
    require_control_plane()
    profile = load_app_profile(args.app)
    remote = RemoteDeviceSessions(control_http_base_url())
    common = None
    completed = False
    try:
        result = remote.call(args.device, "record_start", {
            "app": args.app, "device": args.device, "surface": "native",
            "build": args.build or profile.build_label, "starts_with": args.starts_with,
            "state_root": str(profile.host_state_root.resolve()),
        })
        common = {"app": args.app, "recording_id": result["recording_id"],
                  "controller_token": result["controller_token"]}
        deadline = time.monotonic() + 120
        while True:
            view = remote.call(args.device, "record_view", common)
            if view["state"] == "live":
                break
            if view["state"] != "starting" or time.monotonic() > deadline:
                raise RuntimeError(view.get("last_error") or "native recorder did not start")
            time.sleep(.25)
        print("原生被动录制已启动；只采集 VPHONE 内容区点击和拖动。Ctrl+C 完成录制。", file=sys.stderr)
        deadline = time.monotonic() + args.duration if args.duration else None
        try:
            while deadline is None or time.monotonic() < deadline:
                remote.call(args.device, "record_heartbeat", common)
                view = remote.call(args.device, "record_view", common)
                if view["state"] != "live":
                    raise RuntimeError(view.get("last_error") or view["state"])
                time.sleep(.5)
        except KeyboardInterrupt:
            pass
        view = remote.call(args.device, "record_view", common)
        if view["committed_count"]:
            result = remote.call(args.device, "record_finish", common)
        else:
            result = remote.call(args.device, "record_cancel", common)
        completed = True
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    finally:
        if common is not None and not completed:
            remote.call(args.device, "record_cancel", common)


def cmd_case_recipe(args: argparse.Namespace) -> int:
    try:
        if args.bindings is None:
            result = case_recipe.context(args.app, args.recording_id)
        else:
            payload = json.loads(Path(args.bindings).read_text(encoding="utf-8"))
            result = case_recipe.generate(args.app, args.recording_id, payload, save=args.save)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("record", help="recording asset compilation")
    commands = parser.add_subparsers(dest="record_cmd", required=True)
    native = commands.add_parser("native", help="passively record pointer input in the real VPHONE window")
    native.add_argument("--device", required=True, help="Control Plane rig id")
    native.add_argument("--starts-with", help="Operation executed before observation starts")
    native.add_argument("--build", help="recorded build label; defaults to App profile")
    native.add_argument("--duration", type=float, default=0, help="seconds to observe; 0 waits for Ctrl+C")
    native.set_defaults(func=cmd_native, needs_app=True)
    case = commands.add_parser("case-recipe", help="inspect or compile explicit Case coverage")
    case.add_argument("recording_id")
    case.add_argument("--bindings", type=Path, help="explicit step and expectation bindings JSON")
    case.add_argument("--save", action="store_true", help="save a new draft; default only previews")
    case.set_defaults(func=cmd_case_recipe, needs_app=True)
    window = commands.add_parser("window-probe", help="read real VPHONE content geometry without device input")
    window.add_argument("--config", type=Path, required=True, help="exact VM config.plist")
    window.add_argument("--window-id", type=int, help="optional exact CGWindowID")
    window.set_defaults(func=cmd_window_probe, needs_app=False)
