from __future__ import annotations

import argparse
import os

from autophone.adapters.extensions import register_extensions
from autophone.adapters.cli import _common as common
from autophone.adapters.cli import acceptance as acceptance_commands
from autophone.adapters.cli import ai as ai_commands
from autophone.adapters.cli import app as app_commands
from autophone.adapters.cli import control as control_commands
from autophone.adapters.cli import control_plane_env
from autophone.adapters.cli import device as device_commands
from autophone.adapters.cli import doctor as doctor_commands
from autophone.adapters.cli import effects as effects_commands
from autophone.adapters.cli import failure_sample as failure_sample_commands
from autophone.adapters.cli import impact as impact_commands
from autophone.adapters.cli import knowledge as knowledge_commands
from autophone.adapters.cli import location as location_commands
from autophone.adapters.cli import rig as rig_commands
from autophone.adapters.cli import multicase as multicase_commands
from autophone.adapters.cli import operation as operation_commands
from autophone.adapters.cli import replay as replay_commands
from autophone.adapters.cli import record as record_commands
from autophone.adapters.cli import report as report_commands
from autophone.adapters.cli import run as run_commands
from autophone.adapters.cli import run_index as run_index_commands
from autophone.adapters.cli import schedule as schedule_commands
from autophone.adapters.cli import screens as screens_commands
from autophone.adapters.cli import self_update as self_update_commands
from autophone.adapters.cli import serve as serve_commands
from autophone.adapters.cli import telemetry as telemetry_commands
from autophone.adapters.cli import verification as verification_commands


EXPERIMENTAL_CLI_COMMANDS: frozenset[str] = frozenset({
    "acceptance",
    "effects",
    "failure-sample",
    "impact",
    "knowledge",
    "multicase",
    "run-index",
    "telemetry",
    "verification",
})


def _show_experimental_cli() -> bool:
    return os.environ.get("AUTOPHONE_CLI_SHOW_EXPERIMENTAL", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _hide_experimental_from_help(sub: argparse._SubParsersAction) -> None:
    if _show_experimental_cli():
        return
    sub._choices_actions = [
        action for action in sub._choices_actions
        if action.dest not in EXPERIMENTAL_CLI_COMMANDS
    ]
    visible = [name for name in sub.choices if name not in EXPERIMENTAL_CLI_COMMANDS]
    sub.metavar = "{" + ",".join(visible) + "}"


def build_parser(*, include_extensions: bool = True) -> argparse.ArgumentParser:
    parser = common.argparse.ArgumentParser(
        prog="autophone",
        description="Operate and manage vphone-cli iOS virtual machines",
        epilog=(
            "Experimental subcommands are hidden from default --help; "
            "set AUTOPHONE_CLI_SHOW_EXPERIMENTAL=1 to list them."
        ),
    )
    parser.add_argument("--version", action=common._VersionAction, help="show the build version and exit")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="日志级别：默认 INFO，-v 起 DEBUG(更详细的动作/中间态)")
    parser.add_argument("--app", default=None, help="app id; defaults to the only installed app profile")
    from autophone.application.device import DeviceApplication

    parser.set_defaults(_device_explicit=False)
    parser.add_argument("--device", default=DeviceApplication().default_device, action=common._DeviceAction)
    parser.add_argument("--out-root", type=common.Path, default=None)
    parser.add_argument("--requested-by", default="local")
    parser.add_argument("--run-id", default=None)

    sub = parser.add_subparsers(dest="cmd", required=True)

    self_update_commands.register(sub)
    serve_commands.register(sub)
    doctor_commands.register(sub)
    run_commands.register(sub)
    replay_commands.register(sub)
    record_commands.register(sub)
    report_commands.register(sub)
    run_index_commands.register(sub)
    acceptance_commands.register(sub)
    schedule_commands.register(sub)
    multicase_commands.register(sub)
    effects_commands.register(sub)
    failure_sample_commands.register(sub)
    telemetry_commands.register(sub)
    verification_commands.register(sub)
    operation_commands.register(sub)
    screens_commands.register(sub)
    impact_commands.register(sub)
    ai_commands.register(sub)
    knowledge_commands.register(sub)
    control_commands.register(sub)
    device_commands.register(sub)
    rig_commands.register(sub)
    location_commands.register(sub)
    app_commands.register(sub)
    if include_extensions:
        register_extensions("cli", sub)
    _hide_experimental_from_help(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "req":
        # BL-016 裁定：子命令改名 workflow；别名只提示、不改行为，下一个 MINOR 版本移除。
        print(
            "deprecated: 子命令 `req` 已改名为 `workflow`，`req` 别名将在下一个 MINOR 版本移除；"
            "行为完全等价，请改用 `autophone workflow …`",
            file=common.sys.stderr,
        )
        args.cmd = "workflow"
    if args.cmd == "workflow" and getattr(args, "req_cmd", None) == "workflow":
        # 阶段协调器子命令改名 stage（避免 `workflow workflow …`）；旧内层名只提示、不改行为。
        print(
            "deprecated: 子命令 `workflow workflow` 已改名为 `workflow stage`，旧内层名将在下一个 MINOR 版本移除；"
            "行为完全等价，请改用 `autophone workflow stage …`",
            file=common.sys.stderr,
        )
        args.req_cmd = "stage"
    common.setup_logging(common.verbosity_to_level(getattr(args, "verbose", 0)))
    if args.cmd != "serve":
        # 控制面判定的输入在这里补齐：`start_vphone.sh` 把引导回执写进 Rig 运行目录的
        # control-plane.env，而 CLI 通常在另一个没有 export AUTOPHONE_DATABASE_URL 的终端
        # 执行。不读这份回执时 `configured_control_plane()` 返回 None，设备执行入口会静默
        # 落回单 VM 兼容路径（`app package install --rig-id …` 因此不派单也不报错）。
        # `serve` 自己在 cmd_serve 里解析并打印判定行（还有 `--no-control-plane` 要先处理），
        # 不在这里重复。
        control_plane_env.load_control_plane_env()
    # 全局 --app 只对「真正消费 app」的命令（needs_app=True：run/replay/failure-sample/
    # ai draft）强制解析；其余控制面命令（doctor/telemetry/verification/report/
    # ai diagnose…）与 app 无关，装了多个 app 也不该被挡。promotion evaluate 默认 app-aware
    # （消费 profile 算区分力闸），但歧义时由命令自身报错（非 needs_app），--human-only 可绕。
    # serve 与 app 子命令组用
    # 位置参数 app_name，从不消费全局 --app。统一做 best-effort 解析（单 app 时填好默认值），
    # 仅 needs_app 命令在「零个/多个 app」歧义下 fail-closed。
    app_optional_run_action = args.cmd == "run" and getattr(args, "run_action", None) == "overturn"
    if args.cmd not in ("serve", "app") and getattr(args, "app", None) is None:
        args.app = common.default_app()
        if args.app is None and getattr(args, "needs_app", False) and not app_optional_run_action:
            parser.error("--app is required: zero or multiple app profiles are installed")
    try:
        if args.cmd in {"self-update", "serve"} or common.inherited_exclusive_release_lock(common.STATE_ROOT):
            return int(args.func(args))
        try:
            with common.release_lock(common.STATE_ROOT, exclusive=False, blocking=False):
                return int(args.func(args))
        except common.ReleaseLockBusy as exc:
            print(
                common.json.dumps(
                    {"ok": False, "error": {"code": "release_update_active", "message": str(exc)}},
                    ensure_ascii=False,
                ),
                file=common.sys.stderr,
            )
            return 3
    except common.DeliveryError as exc:
        # 交付需求闭环全程 fail-closed。调用方通过稳定 code 处理拒绝结果。
        print(
            common.json.dumps(
                {"refused": exc.code, "detail": str(exc)},
                ensure_ascii=False,
                indent=2,
            ),
            file=common.sys.stderr,
        )
        return 2
    except (
        common.ControlPlaneUnavailable,
        common.ControlPlaneRequiredForRigTarget,
        common.TargetRigPinRefused,
    ) as exc:
        # 控制面已配置但连不上（BL-026）。与「未配置」区分：未配置时
        # `configured_control_plane()` 返回 None，命令按单 VM 兼容路径继续；到这里
        # 说明本机确实要走派单通道而通道坏了，一律拒绝，不代为回退去抢设备。
        # `control_plane_required_for_rig_target` 是同一条口径的另一面：点名了目标 Rig
        # 而本机没有控制面，同样拒绝，不把参数当没看见。`TargetRigPinRefused` 是控制面
        # 已配置、目标 Rig 未能按 `--device` 固定（BL-218），在提交 Assignment 之前拒绝。
        print(
            common.json.dumps(
                {"refused": exc.code, "detail": str(exc)},
                ensure_ascii=False,
                indent=2,
            ),
            file=common.sys.stderr,
        )
        return 2
    except common.RunDispatchError as exc:
        # 派单通道的结构化拒绝（如目标 Rig 上报 ScreenLock 隔离，BL-224）：未提交或未等到
        # Assignment 终态，按稳定 code 输出，不打印 traceback。
        print(common.json.dumps(exc.to_dict(), ensure_ascii=False, indent=2), file=common.sys.stderr)
        return 2
    except common.ReadonlyAssetsError as exc:
        print(common.json.dumps({"error": exc.as_detail()}, ensure_ascii=False), file=common.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
