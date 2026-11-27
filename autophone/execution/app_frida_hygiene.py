"""被测 App 的 Frida 残留处置：Run 前残留检测与 Run 后终止（BL-083 2026-10-12 第八轮）。

背景（BL-083 第七轮「SIGBUS 调查」，原始证据未随公开版发布）：
19 次可定位的被测 App 崩溃中 18 次签名相同（异常展开经页内偏移 ``0x2258``、``0x0b0`` 的非模块帧，
跳到页内偏移 ``0x113c`` 后以 execute 访问违例结束）。崩溃进程都有 Frida 注入痕迹（``Memory Tag 255``
区域、``ax_dump-v6.dylib``），第七轮 3 次 SIGBUS 都在 Run 结束、crash_trap 摘除后 1.7～6.4 s。
推断（未在该 App 上直接验证）：Frida 摘除后残留的钩子或蹦床导致崩溃。崩溃根因不在本模块范围。

用户裁定（2026-10-12）两项缓解：

(a) Run 结束、最后一次 Frida 摘除之后，按项目现有 terminate 路径（``CaseContext.terminate_app``：
    UDS ``app_terminate`` 后轮询 ``app_list`` 直到 pid 消失）终止被测 App，记录 pid、时点与结果。
(b) Run 开始前，若被测 App 进程带 Frida 注入残留，先冷重启（terminate + launch）再继续准备。
    读不到判据时 fail-open 并记录，不改变准备阶段的其他判定。

BL-244（2026-10-23）：默认保留 (a) 的顺序。原生 detach 等待超过平台阈值时，Run owner
在仍有效的设备权限下先终止本 Run 的 App，再通过 StageGuard 确认所有会话释放；证据记录
``release_fallback=frida_detach_wait``。终止、超时或取消都不能代替资源释放验证。

残留判据（只读，与 SpringBoard 安装前检查 v2 判据相同，见 ``autophone.device.springboard_residue``）：

1. ``app_list`` 取被测 bundle 的 pid；不在运行则无需检查（``not_running``）。
2. guest ``ps``（``PS_COMMAND``）按该 pid 取 ``(pid, 启动时间, 可执行路径)``；
3. ``procexp <pid> regions`` 解析区域表，出现任一标签为 ``Tag: 255`` 的区域即判 ``residue``；
4. 再执行一次 ``ps``，pid 与启动时间须不变，否则不可判定。

任何一步失败（shell 失败、非零退出、输出无法解析、进程在检查期间变化）判 ``check_failed``，
调用方不冷重启（fail-open），并在证据中记录原因。

有状态 App 保护：``RigMaintenance._STATEFUL_RECOVERY_PROTECTED_BUNDLES`` 中的 bundle 默认不终止、
不冷重启（记 ``stateful_app_protected``），只有 ``AUTOPHONE_NO_ORDER_MAINTENANCE=1`` 时放行，与
准备阶段复位和 Frida 冷重启的既有保护一致。BL-226：残留检测只用 guest shell 读进程表与区域表，
不 attach、不读 AX；冷重启由 ``relaunch_app`` 完成（含前台化），之后的 AX/Frida 仍由准备阶段在
App 前台化后进行。

证据：两项结果写入 Run 目录 ``app-frida-hygiene.json``（``SCHEMA``），并进 Artifact Manifest。
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from autophone.device.springboard import PS_COMMAND
from autophone.device.springboard_residue import PROCEXP, RESIDUE_TAG, parse_regions
from autophone.logging import get_logger

logger = get_logger(__name__)

SCHEMA = "autophone.app-frida-hygiene/1"
EVIDENCE_NAME = "app-frida-hygiene.json"
ENV_SWITCH = "AUTOPHONE_APP_FRIDA_HYGIENE"
PROTECTED_ENV = "AUTOPHONE_NO_ORDER_MAINTENANCE"
PROTECTED_REASON = "stateful_app_protected"
NO_FRIDA_REASON = "no_frida_attached"

CRITERION = {
    "tag": RESIDUE_TAG,
    "match": "any_region",
    "source": "procexp <pid> regions",
    "identity": "ps pid+lstart unchanged across the read",
}

ShellFn = Callable[[str], "tuple[int, str]"]
PidFn = Callable[[], "int | None"]

# `  1914 Mon Oct 12 00:27:35 2026     /var/containers/Bundle/Application/<uuid>/Example.app/Example`
_PS_ROW = re.compile(
    r"^\s*(?P<pid>\d+)\s+"
    r"(?P<started>[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d{1,2}:\d{2}:\d{2}\s+\d{4})\s+"
    r"(?P<comm>\S.*?)\s*$"
)


class _CheckError(RuntimeError):
    """残留检查本身不可判定。"""


def hygiene_enabled() -> bool:
    """开关：环境 ``AUTOPHONE_APP_FRIDA_HYGIENE`` 为 0/false/off/no 时关闭，默认开。"""
    value = os.environ.get(ENV_SWITCH)
    if value is None:
        return True
    return value.strip().lower() not in ("0", "false", "off", "no", "")


def protected_reason(bundle_id: str) -> str | None:
    """有状态 App 的既有保护；放行条件与 RigMaintenance 相同。"""
    from autophone.rig.maintenance import RigMaintenance

    if bundle_id in RigMaintenance._STATEFUL_RECOVERY_PROTECTED_BUNDLES and \
            os.environ.get(PROTECTED_ENV) != "1":
        return PROTECTED_REASON
    return None


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _ps_identity(shell: ShellFn, pid: int) -> dict[str, Any]:
    code, output = shell(PS_COMMAND)
    if code != 0:
        raise _CheckError(f"ps exited {code}: {str(output).strip()[:200]!r}")
    parsed = 0
    for line in str(output).splitlines():
        row = _PS_ROW.match(line)
        if row is None:
            continue
        parsed += 1
        if int(row.group("pid")) == pid:
            return {"pid": pid, "started": " ".join(row.group("started").split()),
                    "comm": row.group("comm")}
    if not parsed:
        raise _CheckError("ps output has no row in the expected format")
    raise _CheckError(f"pid {pid} is not in the guest process table")


def inspect_app_residue(
    shell: ShellFn,
    pid: int,
    *,
    procexp: str = PROCEXP,
    regions_reader: ShellFn | None = None,
    use_springboard_probe: bool = False,
) -> dict[str, Any]:
    """只读判定 pid 进程是否带 Frida 残留；不抛异常，结论写在 ``verdict``。

    返回 ``verdict``：``clean``、``residue`` 或 ``check_failed``（带 ``detail``）。
    """
    started = time.monotonic()
    record: dict[str, Any] = {
        "verdict": "check_failed", "detail": "", "criterion": dict(CRITERION),
        "command": None, "process": None, "regions_parsed": 0, "tag255_regions": [],
        "elapsed_ms": 0,
    }
    try:
        before = _ps_identity(shell, pid)
        record["process"] = before
        if use_springboard_probe and regions_reader is None:
            from autophone.device import springboard_residue as sb_residue

            count, regions = sb_residue._read_regions(shell, pid, record)
            record["regions_parsed"] = count
        else:
            command = f"{procexp} {pid} regions"
            record["command"] = command
            code, output = (regions_reader or shell)(command)
            if code != 0:
                raise _CheckError(f"procexp exited {code}: {str(output).strip()[:200]!r}")
            regions = parse_regions(str(output))
            record["regions_parsed"] = len(regions)
            if not regions:
                raise _CheckError("procexp regions output has no row in the expected format")
        after = _ps_identity(shell, pid)
        if (after["pid"], after["started"]) != (before["pid"], before["started"]):
            raise _CheckError(f"process changed during the check: {before} -> {after}")
        tag255 = [region for region in regions if region.tag == RESIDUE_TAG]
        record["tag255_regions"] = [region.to_dict() for region in tag255]
        if tag255:
            record.update(verdict="residue",
                          detail=f"{len(tag255)} region(s) tagged {RESIDUE_TAG!r} in pid {pid}")
        else:
            record.update(verdict="clean", detail=f"no {RESIDUE_TAG!r} region in pid {pid}")
    except Exception as exc:  # noqa: BLE001 - 检查失败即不可判定，调用方 fail-open
        record.update(verdict="check_failed", detail=f"{type(exc).__name__}: {exc}")
    record["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return record


def pre_run_residue_check(
    bundle_id: str,
    *,
    app_pid: PidFn,
    shell: ShellFn,
    relaunch: Callable[[], Any],
    inspect: Callable[[int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run 前：被测 App 带残留时冷重启。返回证据记录，不抛异常。

    ``action``：``none``（未运行、干净或不可判定）、``cold_restart``（检出残留并已冷重启）、
    ``skipped``（受保护 bundle）。冷重启失败时 ``result=failed``，准备阶段照常继续，由其自身的
    就绪检查给出结论（不以本步骤掩盖或替代其他 blocker）。
    """
    record: dict[str, Any] = {"bundle_id": bundle_id, "checked_at": _now(), "action": "none"}
    reason = protected_reason(bundle_id)
    if reason:
        record.update(action="skipped", reason=reason)
        logger.info("Run 前 Frida 残留检测跳过：%s 受保护（%s）", bundle_id, reason)
        return record
    try:
        pid = app_pid()
    except Exception as exc:  # noqa: BLE001 - pid 读不到：fail-open
        pid = None
        record["pid_probe_error"] = f"{type(exc).__name__}: {exc}"
    if pid is None:
        record.update(verdict="check_failed", detail="app_list pid probe unavailable")
        logger.warning("Run 前 Frida 残留检测不可判定（fail-open，不冷重启）：%s app_list 不可用",
                       bundle_id)
        return record
    record["pid"] = pid
    if pid <= 0:
        record.update(verdict="not_running", detail=f"{bundle_id} is not running")
        logger.info("Run 前 Frida 残留检测：%s 未运行，无需检查", bundle_id)
        return record
    check = inspect(pid) if inspect is not None else inspect_app_residue(shell, pid)
    record.update(verdict=check["verdict"], detail=check["detail"], check=check)
    if check["verdict"] == "check_failed":
        logger.warning("Run 前 Frida 残留检测不可判定（fail-open，不冷重启）：pid=%s %s",
                       pid, check["detail"])
        return record
    if check["verdict"] == "clean":
        logger.info("Run 前 Frida 残留检测：pid=%s 无 Tag 255 区域", pid)
        return record
    logger.warning("Run 前 Frida 残留检测：pid=%s 带 %d 个 Tag 255 区域，先冷重启 %s",
                   pid, len(check["tag255_regions"]), bundle_id)
    record["action"] = "cold_restart"
    record["restart_started_at"] = _now()
    try:
        relaunch()
    except Exception as exc:  # noqa: BLE001 - 冷重启失败由准备阶段的就绪检查给出结论
        detail = getattr(getattr(exc, "result", None), "detail", None) or f"{type(exc).__name__}: {exc}"
        record.update(result="failed", restart_detail=str(detail), restart_ended_at=_now())
        logger.error("Run 前冷重启失败（准备阶段继续）：%s", detail)
        return record
    record["restart_ended_at"] = _now()
    try:
        new_pid = app_pid()
    except Exception as exc:  # noqa: BLE001
        new_pid = None
        record["pid_after_probe_error"] = f"{type(exc).__name__}: {exc}"
    record["pid_after"] = new_pid
    record["result"] = "ok" if new_pid not in (None, 0, pid) else "unverified"
    logger.info("Run 前冷重启完成：pid %s -> %s（%s）", pid, new_pid, record["result"])
    return record


def post_run_terminate(
    bundle_id: str,
    *,
    app_pid: PidFn,
    terminate: Callable[[], Any],
    last_detach_at: str | None,
) -> dict[str, Any]:
    """终止本 Run 的被测 App 并记录进程结果；Frida 释放由调用方另行验证。"""
    record: dict[str, Any] = {"bundle_id": bundle_id, "last_detach_at": last_detach_at}
    reason = protected_reason(bundle_id)
    if reason:
        record.update(result="skipped", reason=reason)
        logger.info("Run 后终止被测 App 跳过：%s 受保护（%s）", bundle_id, reason)
        return record
    try:
        pid = app_pid()
    except Exception as exc:  # noqa: BLE001
        pid = None
        record["pid_probe_error"] = f"{type(exc).__name__}: {exc}"
    record["pid"] = pid
    if pid == 0:
        record.update(result="not_running", checked_at=_now())
        logger.info("Run 后终止被测 App：%s 已不在运行", bundle_id)
        return record
    record["terminate_started_at"] = _now()
    try:
        terminate()
    except Exception as exc:  # noqa: BLE001 - 终止失败只记录，不改变 Run 结论
        detail = getattr(getattr(exc, "result", None), "detail", None) or f"{type(exc).__name__}: {exc}"
        record.update(result="failed", detail=str(detail), terminated_at=_now())
        logger.error("Run 后终止被测 App 失败：pid=%s %s", pid, detail)
        return record
    record["terminated_at"] = _now()
    try:
        after = app_pid()
    except Exception as exc:  # noqa: BLE001
        after = None
        record["pid_after_probe_error"] = f"{type(exc).__name__}: {exc}"
    record["pid_after"] = after
    record["result"] = "terminated" if after == 0 else "unverified"
    logger.info("Run 后终止被测 App：pid=%s → %s（最后一次 Frida 摘除 %s）",
                pid, record["result"], last_detach_at)
    return record


def post_run_skipped_no_frida(
    bundle_id: str,
    *,
    app_pid: PidFn | None = None,
) -> dict[str, Any]:
    """记录本次 Run 未发生 Frida attach 时的干净收尾证据，不终止被测 App。"""
    record: dict[str, Any] = {
        "bundle_id": bundle_id,
        "last_detach_at": None,
        "result": "skipped",
        "reason": NO_FRIDA_REASON,
        "checked_at": _now(),
    }
    if app_pid is not None:
        try:
            record["pid"] = app_pid()
        except Exception as exc:  # noqa: BLE001 - 仅补充现场 pid，fail-open
            record["pid"] = None
            record["pid_probe_error"] = f"{type(exc).__name__}: {exc}"
    logger.info(
        "Run 后终止被测 App 跳过：%s 本次 Run 未发生 Frida attach（%s）",
        bundle_id,
        NO_FRIDA_REASON,
    )
    return record


def write_evidence(run_dir: Path, *, pre_run: dict[str, Any] | None,
                   post_run: dict[str, Any] | None) -> Path:
    from autophone.contracts import write_json

    return write_json(Path(run_dir) / EVIDENCE_NAME,
                      {"schema": SCHEMA, "pre_run": pre_run, "post_run": post_run})


# ── 设备端口（默认实现） ──────────────────────────────────────────────────


class DevicePorts:
    """经 UDS 的默认端口：``app_list`` 取 pid、guest shell、``CaseContext`` 的 terminate/relaunch。"""

    def __init__(self, bundle_id: str, shot_dir: Path, *, sock_path: str | None = None) -> None:
        from autophone.execution.engine.actions import CaseContext

        kwargs: dict[str, Any] = {"case_id": "_app_frida_hygiene", "shot_dir": shot_dir, "timeout": 15.0}
        if sock_path:
            kwargs["sock_path"] = sock_path
        self.ctx = CaseContext(**kwargs)
        self.bundle_id = bundle_id

    def app_pid(self) -> int | None:
        from autophone.device.uds import request

        resp, _ = request({"t": "app_list", "filter": "running"},
                          sock_path=self.ctx.sock_path, timeout=10)
        if not isinstance(resp, dict) or resp.get("ok") is False:
            return None
        for app in resp.get("apps") or []:
            if app.get("bundle_id") == self.bundle_id:
                try:
                    pid = int(app.get("pid") or 0)
                except (TypeError, ValueError):
                    pid = 0
                if pid > 0:
                    return pid
        return 0

    def shell(self, command: str) -> tuple[int, str]:
        from autophone.device.shell import run_shell

        result = run_shell(command, timeout_ms=30_000, sock_path=self.ctx.sock_path)
        return result.code, (result.stdout or "") + (result.stderr or "")

    def inspect_residue(self, pid: int) -> dict[str, Any]:
        from autophone.device.target import resolve_protocol

        use_probe = resolve_protocol(self.ctx.sock_path) == "upstream_v1"
        return inspect_app_residue(self.shell, pid, use_springboard_probe=use_probe)

    def terminate(self) -> Any:
        return self.ctx.terminate_app(self.bundle_id)

    def relaunch(self) -> Any:
        return self.ctx.relaunch_app(self.bundle_id)


__all__ = [
    "CRITERION",
    "EVIDENCE_NAME",
    "NO_FRIDA_REASON",
    "SCHEMA",
    "DevicePorts",
    "hygiene_enabled",
    "inspect_app_residue",
    "post_run_skipped_no_frida",
    "post_run_terminate",
    "pre_run_residue_check",
    "protected_reason",
    "write_evidence",
]
