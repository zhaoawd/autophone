from __future__ import annotations

import argparse

from autophone.adapters.cli import _common as common
from autophone.adapters.cli.control_plane_env import (
    control_plane_env_path,
    load_control_plane_env,
    read_control_plane_env,
)

__all__ = [
    "cmd_serve",
    "control_plane_env_path",
    "read_control_plane_env",
    "register",
    "resolve_control_plane_for_serve",
    "validate_serve_security",
]


def resolve_control_plane_for_serve(args: argparse.Namespace) -> str:
    """Decide this `serve` process's path and return one line describing it.

    优先级：`--no-control-plane` > 进程已有的 `AUTOPHONE_DATABASE_URL` >
    `start_vphone.sh` 写的 env 文件。后两级的解析与应用由
    `autophone.adapters.cli.control_plane_env` 提供，与其它 CLI 入口同一份实现。
    """
    if getattr(args, "no_control_plane", False):
        # 只作用于本进程：env 文件由 start_vphone.sh 拥有，serve 不去改它。
        for key in common.CONTROL_PLANE_ENV_KEYS:
            common.os.environ.pop(key, None)
        return "control plane: off (--no-control-plane)"
    resolved = load_control_plane_env()
    if not resolved.configured:
        return (
            "control plane: off (no AUTOPHONE_DATABASE_URL, "
            f"no DSN in {resolved.path})"
        )
    return f"control plane: on ({resolved.dsn}, from {resolved.source})"


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ModuleNotFoundError:
        print(
            "uvicorn is required for `serve`. Install with "
            "`uv sync --group platform` or run via "
            "`uv run --group platform python3 -m autophone.cli serve`.",
            file=common.sys.stderr,
        )
        return 1

    # workers 是真入参而不是写死的 1：断言写死就永远不会失败，那道「单 worker」不变量看着
    # 有守卫其实没有。给了 --workers 就走 uvicorn 多进程，这里正好把它拦下。
    workers = int(getattr(args, "workers", 1) or 1)
    try:
        validate_serve_security(args.host, common.os.environ.get("AUTOPHONE_AUTH", ""), workers=workers)
    except ValueError as exc:
        print(str(exc), file=common.sys.stderr)
        return 2

    # setup_logging 已在 main() 调过，这里再确保一次(reload 子进程不经 main)，
    # 并接管 uvicorn access log 的 screen 流过滤(逻辑已搬进 autophone/logging.py)。
    level = common.verbosity_to_level(getattr(args, "verbose", 0))
    common.setup_logging(level)
    # reload 子进程 import api.py 不经 main()，把级别写进环境变量让它继承(-v 透传)。
    common.export_level_to_env(level)

    # Control Plane 判定必须在 uvicorn.run 之前完成：configured_control_plane() 带
    # lru_cache，进程启动时的 env 决定该进程终身走哪条路径。
    print(resolve_control_plane_for_serve(args), file=common.sys.stderr)
    # App 资产根的来源同样在启动时打出：开关会改变用例库、Recipe 等真源的位置。
    from autophone.paths import describe_app_asset_root

    print(describe_app_asset_root(), file=common.sys.stderr)
    print(f"autophone web → http://{args.host}:{args.port}", file=common.sys.stderr)
    # log_config=None：不让 uvicorn 装自己的 handler/格式器，使其 uvicorn/
    # uvicorn.access/uvicorn.error 三个 logger 恢复默认 propagate=True、冒泡到
    # root，统一走我们 setup_logging 的结构化格式(时间戳+级别+ctx+logger 名)。
    # uvicorn.access 上挂的 ScreenStreamFilter 仍生效(screen.jpg 轮询照样静音)。
    uvicorn.run(
        "autophone.adapters.http.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_config=None,
    )
    # uvicorn 对 lifespan 收尾失败只记日志、仍正常返回；收尾有步骤失败时以非 0 退出（BL-219）。
    # --reload 时应用在子进程里运行，本进程看不到收尾结果。
    from autophone.adapters.http.runtime_routes import last_shutdown_failures

    failed = last_shutdown_failures()
    if failed:
        print(f"dashboard shutdown steps failed: {', '.join(failed)}", file=common.sys.stderr)
        return 1
    return 0


def validate_serve_security(host: str, auth: str, *, workers: int = 1) -> None:
    """Fail closed for remote control-plane binds and process-local session gates."""
    import ipaddress

    if workers != 1:
        raise ValueError(
            "Web 原生录制/远程操作要求 dashboard 单 worker（会话 execution gate 是进程内锁，"
            "多 worker 各持一份会互相踩），请去掉 --workers 或设为 1")
    value = str(host or "").strip().strip("[]")
    loopback = value.lower() == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(value).is_loopback
        except ValueError:
            loopback = False
    valid_auth = any(
        bool(user and sep and password)
        for pair in str(auth or "").split(",")
        for user, sep, password in [pair.strip().partition(":")]
    )
    if not loopback and not valid_auth:
        raise ValueError(
            "拒绝在非 loopback 地址启动无鉴权控制面；请设置 AUTOPHONE_AUTH='user:password'"
        )


def register(sub: argparse._SubParsersAction) -> None:
    serve = sub.add_parser("serve", help="start the web dashboard (FastAPI on /)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true", help="auto-reload on code changes (dev)")
    serve.add_argument(
        "--no-control-plane", action="store_true",
        help="强制走单 VM 兼容路径：忽略 AUTOPHONE_DATABASE_URL 与 start_vphone.sh 写的 "
             "control-plane.env（无 PostgreSQL 时的一次性排障用）")
    serve.add_argument("--workers", type=int, default=1,
                       help="uvicorn 进程数；只支持 1（浏览器录制/远程操作的会话门是进程内锁）")
    serve.set_defaults(func=cmd_serve)
