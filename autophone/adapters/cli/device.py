"""`autophone device screenlock …`：ScreenLock 隔离标记的只读状态与显式解除（BL-223）。"""
from __future__ import annotations

import argparse

from autophone.adapters.cli import _common as common


def _service():
    from autophone.application.screen_quarantine import ScreenQuarantineService

    return ScreenQuarantineService()


def cmd_screenlock_status(args: argparse.Namespace) -> int:
    print(common.json.dumps(_service().status(args.device), ensure_ascii=False, indent=2))
    return 0


def cmd_screenlock_release_quarantine(args: argparse.Namespace) -> int:
    from autophone.application.screen_quarantine import QuarantineReleaseRefused
    from autophone.rig.control_plane_env import load_control_plane_env

    # 控制面判定按目标设备读 start_vphone.sh 写的回执，而不是默认 Rig 的那份。
    load_control_plane_env(rig_id=args.device)
    try:
        receipt = _service().release(
            args.device, actor=args.actor, reason=args.reason,
            confirm_quiesced=args.confirm_quiesced,
            frida_endpoint=args.frida_host, socket=args.socket,
        )
    except QuarantineReleaseRefused as exc:
        print(common.json.dumps({"ok": False, "error": exc.to_dict()}, ensure_ascii=False, indent=2))
        return 2
    print(common.json.dumps({"ok": True, "receipt": receipt}, ensure_ascii=False, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    device = sub.add_parser("device", help="设备级本机状态（ScreenLock）")
    device_sub = device.add_subparsers(dest="device_cmd", required=True)
    screenlock = device_sub.add_parser("screenlock", help="ScreenLock 隔离标记")
    screenlock_sub = screenlock.add_subparsers(dest="screenlock_cmd", required=True)

    status = screenlock_sub.add_parser("status", help="只读：锁状态与隔离标记")
    status.add_argument("--device", required=True)
    status.set_defaults(func=cmd_screenlock_status)

    release = screenlock_sub.add_parser(
        "release-quarantine",
        help="核对前置与设备探针后归档隔离标记并写解除回执；不带 --confirm-quiesced 时只核对",
    )
    release.add_argument("--device", required=True)
    release.add_argument("--actor", required=True)
    release.add_argument("--reason", required=True)
    release.add_argument(
        "--confirm-quiesced", action="store_true",
        help="操作者确认设备上没有残留 Frida 会话或挂起进程；回执记为 operator_attested",
    )
    release.add_argument("--frida-host", default=None,
                         help="host:port，只用于 Frida 只枚举探针；默认取 /tmp/autophone-rigs/<device>/device-target.json")
    release.add_argument("--socket", default=None,
                         help="UDS socket，用于锁屏状态辅助程序与 app_list 探针；默认同上")
    release.set_defaults(func=cmd_screenlock_release_quarantine)
