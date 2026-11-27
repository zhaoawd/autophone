"""Command-line composition root for one Rig Agent process."""
from __future__ import annotations

import argparse
import fcntl
import importlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from autophone.logging import get_logger, level_from_env, setup_logging

from .agent import RigAgent
from .device_session import DeviceSessionAgent, device_session_ws_url
from .source_identity import SourceProcessStale
from .target import DeviceTarget, served_device_keys
from .process_guard import AgentProcessError, agent_process_guard
from .transport import WebSocketAgentTransport


log = get_logger(__name__)

ExecutorFactory = Callable[..., Any]
DEFAULT_EXECUTOR_FACTORY = "autophone.rig.plugins:create_executor"
DEFAULT_DEVICE_SESSION_FACTORY = "autophone.rig.plugins:create_device_session_ops"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autophone-rig-agent")
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument(
        "--control-ws",
        default=os.environ.get(
            "AUTOPHONE_CONTROL_WS",
            "ws://127.0.0.1:8100/api/control/agent/ws",
        ),
    )
    parser.add_argument("--capability", action="append", default=[])
    parser.add_argument("--run-root", type=Path, default=None)
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=float(os.environ.get("AUTOPHONE_AGENT_HEARTBEAT_INTERVAL", "15")),
    )
    parser.add_argument(
        "--executor-factory",
        default=os.environ.get(
            "AUTOPHONE_AGENT_EXECUTOR_FACTORY",
            DEFAULT_EXECUTOR_FACTORY,
        ),
        help="import path in module:function form",
    )
    parser.add_argument(
        "--device-session-factory",
        default=os.environ.get(
            "AUTOPHONE_AGENT_DEVICE_SESSION_FACTORY",
            DEFAULT_DEVICE_SESSION_FACTORY,
        ),
        help="import path in module:function form for the interactive channel executor",
    )
    parser.add_argument(
        "--no-device-session",
        action="store_true",
        help="不起交互通道会话循环（排障用；页面的屏幕预览与 device console 会随之不可用）",
    )
    parser.add_argument(
        "--host-anomaly-watcher",
        action="store_true",
        help="参与 Host 异常记录（同机只有一个 Agent 实际工作）；start_vphone.sh 默认开启",
    )
    return parser


def load_executor_factory(spec: str) -> ExecutorFactory:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("executor factory must use module:function form")
    factory = getattr(importlib.import_module(module_name), attribute)
    if not callable(factory):
        raise TypeError(f"executor factory is not callable: {spec}")
    return factory


def build_device_session_agent(args: Any, target: DeviceTarget) -> DeviceSessionAgent:
    ops = load_executor_factory(args.device_session_factory)(
        target=target,
        run_root=args.run_root,
    )
    url = device_session_ws_url(args.control_ws)
    devices = served_device_keys(target)
    log.info("device session channel: url=%s devices=%s", url, devices)
    return DeviceSessionAgent(
        url,
        host_id=target.host_id,
        rig_id=target.rig_id,
        rig_generation=target.rig_generation,
        devices=devices,
        ops=ops,
    )


def _flush_logs() -> None:
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:  # noqa: BLE001
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass


def _observe_vm_down(target: DeviceTarget) -> None:
    from autophone.application.control_plane import (
        require_control_plane,
        reset_control_plane_cache,
    )
    from autophone.recovery_observation import atomic_json, deliver, persist_edge

    env = {**os.environ, **target.process_environment()}
    env.pop("AUTOPHONE_STAGE_LOG", None)
    config = target.runtime_dir / "control-plane.env"
    if config.is_file():
        dsn_added = False
        for line in config.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"AUTOPHONE_DATABASE_URL", "AUTOPHONE_CONTROL_WS"}:
                env.setdefault(key, value)
                if key not in os.environ:
                    os.environ[key] = value
                    if key == "AUTOPHONE_DATABASE_URL":
                        dsn_added = True
        if dsn_added:
            reset_control_plane_cache()
    edge_path = persist_edge(env)
    with edge_path.with_suffix(".lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = json.loads(edge_path.read_text(encoding="utf-8"))
        record["actor"] = "rig-agent"
        atomic_json(edge_path, record)
    try:
        deliver(edge_path, require_control_plane())
    except Exception as exc:  # noqa: BLE001 - best-effort; recovery_observation will retry
        log.warning(
            "inline down edge delivery deferred for %s: %s: %s",
            target.rig_id, type(exc).__name__, exc,
        )
    env["AUTOPHONE_DOWN_OBSERVATION"] = str(edge_path)
    subprocess.Popen(
        [sys.executable, "-m", "autophone.recovery_observation"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )


def main(argv: list[str] | None = None) -> None:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw_argv)
    # 这个进程由 start_vphone.sh 以 nohup 拉起，stdout/stderr 都进 rig-agent.log。
    # 不在这里配置日志，整个 Agent 就没有任何 sink，运行痕迹只剩 PostgreSQL 表。
    setup_logging(level_from_env())
    target = DeviceTarget.load(args.target)
    try:
        with agent_process_guard(target, args.target.resolve()):
            _run_agent(args, target)
    except AgentProcessError as exc:
        log.error("%s", exc)
        raise SystemExit(2) from exc
    except SourceProcessStale as exc:
        if os.environ.get("AUTOPHONE_AGENT_REEXEC_ON_STALE", "1") == "1":
            log.warning("rig agent source changed on disk; re-executing in-place: %s", exc)
            _flush_logs()
            os.execv(sys.executable, [sys.executable, "-m", "autophone.rig.main", *raw_argv])
            return
        raise


def _run_agent(args: Any, target: DeviceTarget) -> None:
    from autophone import host_anomaly

    os.environ.update(target.process_environment())
    log.info(
        "rig agent starting: rig=%s host=%s generation=%s control_ws=%s target=%s",
        target.rig_id,
        target.host_id,
        target.rig_generation,
        args.control_ws,
        args.target,
    )
    executor = load_executor_factory(args.executor_factory)(
        target=target,
        run_root=args.run_root,
    )
    source_check_interval = float(os.environ.get("AUTOPHONE_AGENT_SOURCE_CHECK_INTERVAL", "10"))
    agent = RigAgent(
        target,
        WebSocketAgentTransport(args.control_ws),
        executor,
        capability_support=args.capability,
        heartbeat_interval_s=args.heartbeat_interval,
        vm_alive_probe=lambda t: host_anomaly.vm_process_alive(t.public_dict()),
        down_observer=_observe_vm_down,
        idle_source_check_interval_s=source_check_interval,
    )
    device_session = None
    if not args.no_device_session:
        device_session = build_device_session_agent(args, target)
        device_session.start()
    watcher = None
    if args.host_anomaly_watcher:
        from .host_watcher import start_host_watcher

        watcher = start_host_watcher(target)
    try:
        agent.run_forever()
    except BaseException as exc:  # noqa: BLE001 - the exit reason must survive in the log
        log.error(
            "rig agent exiting: %s: %s",
            type(exc).__name__,
            exc,
            exc_info=not isinstance(exc, KeyboardInterrupt),
        )
        raise
    finally:
        if watcher is not None:
            watcher.stop()
        if device_session is not None:
            device_session.stop()


if __name__ == "__main__":
    main()
