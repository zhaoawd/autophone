"""Composition root for the optional PostgreSQL Control Plane."""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注
    from autophone.control.interface import ControlPlaneInterface
    from autophone.control.host_coordination import HostCoordination

# `psycopg` 是可选依赖（`autophone[postgres]`）。本模块的异常类型会被 CLI 顶层 import
# （`autophone/cli.py` 的 main 据此拒绝），控制面实现的 import 留在函数体内，且仅在真正
# 构造/连接 PostgreSQL Control Plane 时才需要 `psycopg`。


class ControlPlaneNotConfigured(RuntimeError):
    """没有配置 `AUTOPHONE_DATABASE_URL`：本机按单 VM 兼容路径运行。

    这是一个正常状态，不是故障——`configured_control_plane()` 对它返回 `None`，
    调用方据此在本进程内直接执行（目标架构 §16.0 兼容路径退出第一步）。
    """

    code = "control_plane_not_configured"


class ControlPlaneRequiredForRigTarget(ControlPlaneNotConfigured):
    """调用方点名了目标 Rig，但本机没有配置控制面，无法派单。

    与父类分开一个 code：点名 Rig 说明这次执行要经 Assignment 落到**那台**设备，
    而单 VM 兼容路径只会在本机装、本机跑。此时静默回退等于「参数被接受却不起作用、
    控制面里没有任何记录、命令还退 0」，所以一律拒绝执行，由调用方先把控制面配起来
    （`scripts/start_vphone.sh` 引导后 `control-plane.env` 即有 DSN）。
    """

    code = "control_plane_required_for_rig_target"


class ControlPlaneUnavailable(RuntimeError):
    """DSN 已配置，但控制面连接或 schema 初始化失败（BL-026）。

    与 `ControlPlaneNotConfigured` 必须可区分：配置了控制面就说明本机的设备工作
    要经 Assignment 派单，此时连不上 PostgreSQL 是**故障**。若在这里落回单 VM
    兼容路径，页面与 CLI 会绕过 Scheduler 直接抢设备，既与「兼容路径退出」的方向
    相反，也让「控制面坏了」变成静默事实。调用方一律据此拒绝执行
    （HTTP 503 / CLI `refused` + 退出码 2），不代为回退。

    异常不进 `lru_cache`（Python 只缓存正常返回值），所以 PostgreSQL 恢复后
    下一次调用会重新连接，不需要重启进程或手动清缓存。
    """

    code = "control_plane_unavailable"

    def __init__(self, message: str, *, dsn: str = "", schema: str = "") -> None:
        super().__init__(message)
        self.dsn = dsn
        self.schema = schema


class TargetRigPinRefused(ValueError):
    """按设备固定目标 Rig 失败，提交方必须在提交 Assignment 之前拒绝（BL-215、BL-218）。

    `code` 取三个稳定值之一：

    - `device_required`：既没有显式目标 Rig，也没有 `device`；
    - `device_rig_unresolved`：`device` 未对应到唯一已注册的 Rig；
    - `target_rig_device_mismatch`：显式目标 Rig 与 `device` 解析出的 Rig 不同。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def to_detail(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


def pin_target_rig(
    control: object,
    device: str | None,
    explicit: str | None,
    *,
    subject: str = " Run",
    device_optional_with_explicit: bool = False,
) -> str:
    """按 `device` 解析唯一 Rig，作为 Assignment 的 `target_rig_id`。

    与 `POST /runs`（BL-215）、CLI、计划任务、包门禁同一口径：`device` 必须对应唯一
    已注册的 Rig；显式目标与解析结果不同则拒绝。`subject` 拼在拒绝消息「未提交」之后。`device_optional_with_explicit` 为真时，
    只给显式目标、不给 `device` 的请求原样采用显式目标（包安装路由沿用 `rig_id` 点名
    Rig 的既有用法）；为假时缺 `device` 一律 `device_required`。
    """
    from autophone.control.postgres import ControlStateError

    device = str(device or "").strip()
    explicit = str(explicit or "").strip() or None
    if not device:
        if explicit and device_optional_with_explicit:
            return explicit
        raise TargetRigPinRefused(
            "device_required",
            f"控制面已配置：请求必须给出 device（或显式目标 Rig），未提交{subject}",
        )
    try:
        resolved = str(control.resolve_device_rig(device))  # type: ignore[attr-defined]
    except ControlStateError as exc:
        raise TargetRigPinRefused(
            "device_rig_unresolved",
            f"设备 {device!r} 未对应到唯一已注册的 Rig，未提交{subject}；"
            f"请确认设备名与 Rig 注册信息一致后重试（{exc}）",
        ) from exc
    if explicit and explicit != resolved:
        raise TargetRigPinRefused(
            "target_rig_device_mismatch",
            f"target_rig_id={explicit!r} 与设备 {device!r} 对应的 Rig {resolved!r} 不一致，"
            f"未提交{subject}",
        )
    return resolved


def _redact_dsn(dsn: str) -> str:
    """去掉 DSN 里的口令，其余原样保留——错误信息要说得出连的是哪个库。"""
    return re.sub(r"://([^:/@]*):[^@]*@", r"://\1:***@", dsn)


def _configured_dsn_schema() -> tuple[str, str]:
    dsn = os.environ.get("AUTOPHONE_DATABASE_URL", "").strip()
    schema = os.environ.get("AUTOPHONE_DATABASE_SCHEMA", "autophone").strip() or "autophone"
    return dsn, schema


def _build_control_plane(
    dsn: str,
    schema: str,
    *,
    evidence_root: Path | str | None = None,
) -> ControlPlaneInterface:
    try:
        from autophone.control import ControlPlane, PostgresControlStore

        # 未点名时不传该参数：store 照旧回落到 `AUTOPHONE_EVIDENCE_ROOT`，缓存路径的
        # 构造调用与改动前一致。
        store = (
            PostgresControlStore(dsn, schema=schema)
            if evidence_root is None
            else PostgresControlStore(dsn, schema=schema, evidence_root=evidence_root)
        )
        store.initialize()
    except Exception as exc:  # noqa: BLE001 - 任何连接/初始化失败都是「已配置但不可用」
        raise ControlPlaneUnavailable(
            "control plane is configured but unavailable "
            f"(dsn={_redact_dsn(dsn)} schema={schema}): {type(exc).__name__}: {exc}",
            dsn=_redact_dsn(dsn),
            schema=schema,
        ) from exc
    return ControlPlane(store)


@lru_cache(maxsize=1)
def configured_control_plane() -> ControlPlaneInterface | None:
    """本进程该用的 ControlPlane；未配置返回 `None`，已配置但不可用抛异常。

    三态而不是两态：`None`（未配置，走兼容路径）、ControlPlane（可用）、
    `ControlPlaneUnavailable`（已配置但不可用，拒绝执行）。第三态若用返回 `None`
    表达，会被调用方当成第一态放行到兼容路径——见 `ControlPlaneUnavailable`。

    缓存实例的可信证据根目录只取 `AUTOPHONE_EVIDENCE_ROOT`；需要点名证据根目录的
    入口用 `require_control_plane(evidence_root=...)`。
    """
    dsn, schema = _configured_dsn_schema()
    if not dsn:
        return None
    return _build_control_plane(dsn, schema)


def require_control_plane(
    *,
    evidence_root: Path | str | None = None,
) -> ControlPlaneInterface:
    """返回已配置的 ControlPlane；未配置时抛 `ControlPlaneNotConfigured`。

    `evidence_root` 为 `None` 时返回本进程缓存的实例。显式给出时另建一个不进缓存的
    实例，其 store 用该目录作可信证据根目录评估本地 `file://` 证据（Capacity 试运行
    前置检查、Capacity 提升），不读 `AUTOPHONE_EVIDENCE_ROOT`。这样点名证据根目录的
    调用方（`live_p0 --evidence-root`、`control host capacity --evidence-root`）
    不必改写进程环境，也不改变其他调用方拿到的缓存实例（BL-083，2026-10-11 窗口）。
    """
    if evidence_root is None:
        control = configured_control_plane()
        if control is None:
            raise ControlPlaneNotConfigured("AUTOPHONE_DATABASE_URL is not configured")
        return control
    dsn, schema = _configured_dsn_schema()
    if not dsn:
        raise ControlPlaneNotConfigured("AUTOPHONE_DATABASE_URL is not configured")
    return _build_control_plane(dsn, schema, evidence_root=evidence_root)


def reset_control_plane_cache() -> None:
    configured_control_plane.cache_clear()


def require_host_coordination(
    *,
    evidence_root: Path | str | None = None,
) -> HostCoordination:
    """Resolve the Host interface from the shared PostgreSQL composition root.

    `evidence_root` 的含义同 `require_control_plane`。
    """
    return cast("HostCoordination", require_control_plane(evidence_root=evidence_root))
