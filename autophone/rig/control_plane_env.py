"""CLI 与 Rig 工具共用的 `control-plane.env` 解析。

`scripts/start_vphone.sh` 把引导成功的控制面 DSN 写进 Rig 运行目录的
`control-plane.env`（见 `.claude/skills/vm-bringup/references/control-plane.md`），
但这份回执原先只有 `serve` 读。其它 CLI 入口在没有 `export AUTOPHONE_DATABASE_URL`
的终端里判定「未配置控制面」，于是 `configured_control_plane()` 返回 `None`，
设备执行落回单 VM 兼容路径：`app package install --rig-id …` 被接受却不派单，
控制面里没有任何记录，命令也不报错。本模块把 `serve` 的解析逻辑抽出来给所有入口
共用，判定口径因此只有一处。

本模块只负责**解析与应用环境**，不判断控制面是否可用：DSN 设上之后，
「已配置但连不上」仍由 `configured_control_plane()` 抛 `ControlPlaneUnavailable`。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

CONTROL_PLANE_ENV_FILE = "control-plane.env"
CONTROL_PLANE_ENV_KEYS = (
    "AUTOPHONE_DATABASE_URL",
    "AUTOPHONE_DATABASE_SCHEMA",
    "AUTOPHONE_CONTROL_WS",
)
DEFAULT_RIG_ID = "vm"
DEFAULT_RIG_RUNTIME_ROOT = Path("/tmp/autophone-rigs")
# 每个白名单键在本进程生效值的来源；只记来源，不记取值（DSN 可能含凭据）。
SOURCE_PROCESS_ENV = "process_env"
SOURCE_ENV_FILE = CONTROL_PLANE_ENV_FILE
SOURCE_MISSING = "missing"


@dataclass(frozen=True)
class ControlPlaneEnv:
    """一次解析的结果：env 文件落点、解析出的 DSN 及其来源。

    `key_sources` 按 `CONTROL_PLANE_ENV_KEYS` 给出每个键的来源：`process_env`（解析前
    进程已有非空值）、`control-plane.env`（由本次解析从 env 文件补入）或 `missing`。
    """

    path: Path
    dsn: str = ""
    source: str = ""
    key_sources: Mapping[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.dsn)


def control_plane_env_path(env: Mapping[str, str] | None = None) -> Path:
    """Locate the env file `start_vphone.sh` writes for this Rig.

    CLI 常在另一个终端执行，拿不到 `start_vphone.sh` 导出的环境，所以这里按同一
    套默认值把路径推回来，而不是要求用户手工 export。
    """
    env = os.environ if env is None else env
    configured = env.get("AUTOPHONE_RIG_RUNTIME_DIR", "").strip()
    if configured:
        return Path(configured).expanduser() / CONTROL_PLANE_ENV_FILE
    rig_id = (env.get("AUTOPHONE_RIG_ID") or env.get("AP_DEVICE") or "").strip()
    if not rig_id:
        vm_dir = env.get("VM_DIR", "").strip()
        rig_id = Path(vm_dir).name if vm_dir else DEFAULT_RIG_ID
    return DEFAULT_RIG_RUNTIME_ROOT / rig_id / CONTROL_PLANE_ENV_FILE


def read_control_plane_env(path: Path) -> dict[str, str]:
    """Parse `KEY=VALUE` lines, keeping only the allowlisted Control Plane keys."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in CONTROL_PLANE_ENV_KEYS:
            values[key] = value.strip()
    return values


def load_control_plane_env(*, rig_id: str | None = None) -> ControlPlaneEnv:
    """把 env 文件里的白名单键补进本进程环境，并返回判定结果。

    rig_id 指定回执所属 Rig；显式 AUTOPHONE_RIG_RUNTIME_DIR 仍优先。
    优先级：进程已有的 `AUTOPHONE_DATABASE_URL` > `start_vphone.sh` 写的 env 文件。
    **不设硬编码 DSN 默认值**——否则「不配 PostgreSQL 排障」这条目标架构 §16.0 明确
    保留的路径会变得不可达。两处都没有 DSN 时不写入任何键，本进程保持单 VM 兼容路径。
    """
    env = dict(os.environ)
    if rig_id is not None:
        env["AUTOPHONE_RIG_ID"] = rig_id
    path = control_plane_env_path(env)
    loaded = read_control_plane_env(path)
    present = {
        key for key in CONTROL_PLANE_ENV_KEYS if os.environ.get(key, "").strip()
    }
    inherited = os.environ.get("AUTOPHONE_DATABASE_URL", "").strip()
    dsn = inherited or loaded.get("AUTOPHONE_DATABASE_URL", "").strip()
    if not dsn:
        return ControlPlaneEnv(path, key_sources=_key_sources(present, set()))
    # DSN 来自哪里都要补齐 env 文件里其余的白名单键。否则手工 export DSN 后起进程
    # 会漏掉 AUTOPHONE_CONTROL_WS 这类同批写入的取值，进程实际接的控制面与
    # start_vphone.sh 引导出来的那一个对不上。进程里只导出了空白值的键按未设置处理，
    # 否则 DSN 判定为已配置而进程环境里的 DSN 仍为空。
    filled: set[str] = set()
    for key, value in loaded.items():
        if value and key not in present:
            os.environ[key] = value
            filled.add(key)
    from autophone.application.control_plane import reset_control_plane_cache

    # 环境刚变过，之前若已判定过要重判：`configured_control_plane()` 带 lru_cache。
    reset_control_plane_cache()
    return ControlPlaneEnv(
        path,
        dsn,
        "environment" if inherited else str(path),
        _key_sources(present, filled),
    )


def _key_sources(present: set[str], filled: set[str]) -> dict[str, str]:
    return {
        key: (
            SOURCE_PROCESS_ENV
            if key in present
            else SOURCE_ENV_FILE
            if key in filled
            else SOURCE_MISSING
        )
        for key in CONTROL_PLANE_ENV_KEYS
    }


__all__ = [
    "SOURCE_ENV_FILE",
    "SOURCE_MISSING",
    "SOURCE_PROCESS_ENV",
    "ControlPlaneEnv",
    "control_plane_env_path",
    "load_control_plane_env",
    "read_control_plane_env",
]
