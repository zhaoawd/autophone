from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
import yaml
from autophone.application.control_plane import (
    ControlPlaneRequiredForRigTarget,
    ControlPlaneUnavailable,
    TargetRigPinRefused,
)
from autophone.paths import CODE_ROOT, STATE_ROOT
from autophone.assets import screens as screen_assets
from autophone.assets.policy import ReadonlyAssetsError, assert_assets_writable
from autophone.assets.apps import create_app_profile, default_app, list_app_profiles, load_app_profile
from autophone.errors import DeliveryError
from autophone.application.failure_sample import create_failure_sample, failure_sample_spec
from autophone.logging import export_level_to_env, setup_logging, verbosity_to_level
from autophone.execution.release_lock import (
    ReleaseLockBusy,
    inherited_exclusive_release_lock,
    release_lock,
)
from autophone.execution.dispatch import RunDispatchError, RunDispatcher
from autophone.execution.run_service import RunService
from autophone.observation.telemetry import telemetry_backend_doctor
from autophone.util import now_iso as _now, write_json_atomic
from autophone.execution.verification import verification_agent_doctor
from autophone.version import get_version
from autophone.rig.control_plane_env import (
    CONTROL_PLANE_ENV_FILE,
    CONTROL_PLANE_ENV_KEYS,
    DEFAULT_RIG_ID,
    DEFAULT_RIG_RUNTIME_ROOT,
)

__all__ = [
    "Any",
    "CODE_ROOT",
    "ControlPlaneRequiredForRigTarget",
    "ControlPlaneUnavailable",
    "DeliveryError",
    "Mapping",
    "Path",
    "ReadonlyAssetsError",
    "TargetRigPinRefused",
    "ReleaseLockBusy",
    "RunDispatchError",
    "RunDispatcher",
    "RunService",
    "STATE_ROOT",
    "_now",
    "argparse",
    "assert_assets_writable",
    "create_app_profile",
    "create_failure_sample",
    "datetime",
    "default_app",
    "export_level_to_env",
    "failure_sample_spec",
    "get_version",
    "hashlib",
    "inherited_exclusive_release_lock",
    "json",
    "list_app_profiles",
    "load_app_profile",
    "logging",
    "os",
    "release_lock",
    "screen_assets",
    "setup_logging",
    "subprocess",
    "sys",
    "telemetry_backend_doctor",
    "uuid",
    "verbosity_to_level",
    "verification_agent_doctor",
    "write_json_atomic",
    "yaml",
]


class _VersionAction(argparse.Action):
    """Resolve the build identity only when --version is actually requested.

    ``action="version"`` evaluates its text while the parser is built, which
    would run ``git describe``/``rev-parse`` on every CLI invocation in a source
    checkout.  Packaged releases short-circuit on version.json and never paid it.
    """

    def __init__(self, option_strings, dest=argparse.SUPPRESS, help=None):
        super().__init__(option_strings, dest=dest, default=argparse.SUPPRESS, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        print(get_version())
        parser.exit()


class _DeviceAction(argparse.Action):
    """保留 --device 是否由调用方明确提供。"""

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        setattr(namespace, "_device_explicit", True)


def emit_json(payload: Any) -> int:
    """把结果打到 stdout 并给出退出码 0。

    面向用户的结果一律走 stdout（日志走 stderr，见 `autophone/logging.py`），
    所以这里用 `print` 而不是 logger。原名 `autophone.cli._req_emit`。
    """
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _run_id(prefix: str = "run") -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{prefix}-{uuid.uuid4().hex[:6]}"


def _regression_spec(args: argparse.Namespace, selector: dict[str, Any] | None) -> dict[str, Any]:
    # API 面缺省 disabled；--api required|advisory 开抓取（经 Frida attach/spawn-gate 抓接口，
    # 落 planes/api/capture.jsonl，逐用例 api_oracle 断言 × UI 合成）。required=API fail 拖垮 overall，
    # advisory=只留证据不改判。
    api_mode = getattr(args, "api", None) or "disabled"
    spec = {
        "schema_version": "v1",
        "run_id": args.run_id or _run_id(args.app),
        "type": "regression",
        "app": args.app,
        "device": args.device,
        # 缺省补登（绝大多数用例需要已登录前置）；--no-auto-login 关掉，
        # 留给「验证登录失败」等需停在登出态的特殊用例。
        "auto_login": bool(getattr(args, "auto_login", True)),
        "planes": {
            "ui": {"mode": "required"},
            "function": {"mode": "disabled"},
            "api": {"mode": api_mode},
            "telemetry": {"mode": "disabled"},
        },
        "requested_by": args.requested_by,
        "requested_at": _now(),
    }
    if selector:
        spec["case_selector"] = selector
    if getattr(args, "execution_plan", None):
        spec["execution_plan"] = args.execution_plan
    fixture_order = getattr(args, "fixture_order", None)
    if fixture_order:
        spec["fixture"] = {"order": fixture_order}
    return spec


def _service(args: argparse.Namespace) -> RunService:
    return RunService(run_root=args.out_root)


def _dispatcher(args: argparse.Namespace, service: RunService | None = None) -> RunDispatcher:
    """命令行提交一条 Run 的唯一入口（BL-024，目标架构 §16.0 兼容路径退出第三步①）。

    控制面已配置且这条 Run 需要真实设备时经 Assignment 派单，与页面 `POST /runs` 同一条
    通道；未配置时走单 VM 兼容路径。判定与等待都在 `RunDispatcher` 里，CLI 只做接线。
    """
    return RunDispatcher(service if service is not None else _service(args))


def _find_run_dir(run_root: Path, run_id: str) -> Path:
    matches = sorted(run_root.glob(f"*/{run_id}"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        raise FileNotFoundError(f"run not found under {run_root}: {run_id}")
    return matches[0]
