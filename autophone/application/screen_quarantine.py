"""ScreenLock 隔离标记的只读状态与显式解除（BL-223）。

隔离标记 ``runs/.locks/<device>.quarantine.json`` 由 ``ScreenLock.quarantine`` 在 StageGuard
quiesce 失败时写入，含义是「设备静默未验证」：写标记的进程可能仍持有未确认释放的
Frida 会话，或留有经 spawn 挂起、未确认结束的进程。标记存在时 ``try_lease`` 抛
``ScreenQuarantined``。Recovery Receipt 只把 Rig 拉回 online，不处理该标记。

解除条件（全部满足才归档标记，任一不满足返回结构化拒绝，标记保持原位）：

1. 标记可解析，该设备 ScreenLock 未被持有（在 flock 下核对，见 ``ScreenLock.release_quarantine``）；
2. 标记记录的写入进程已退出（旧标记无 pid 时记为未知）；
3. 控制面已配置时：设备能解析为唯一 Rig，且该 Rig 无非终态 Assignment、无未释放 Lease、
   无未关闭 recovery incident；控制面已配置但不可用时拒绝；
4. 设备探针通过（BL-232 阶段 2 起，回执 ``screen-quarantine-release/2``）：guest 辅助程序经 UDS
   读到 notify 锁屏状态（``lock_state``，含 SpringBoard 唯一存在且拉起已满等待时间）、Frida 只枚举
   进程即列出 SpringBoard（``frida_enumeration``，不 attach）、UDS ``app_list`` 往返到 guest（``uds``）。
   阶段 2 之前第 4 项为「Frida 读到 SpringBoard ``isUILocked``」（回执字段 ``frida_springboard``），
   该读法会 attach SpringBoard，已停用；``lock_state`` 读不到时第 4 项不通过，不回退到 attach；
5. 操作者显式 ``confirm_quiesced``。宿主侧没有可靠手段枚举其他客户端遗留的 Frida 会话或
   列出经 spawn 挂起的进程，第 2、4 项只是必要条件，所以总是要求人工确认，并在回执中记为
   ``operator_attested``。不带确认时执行全部核对并返回 ``quiesce_confirmation_required``，
   可作为预检。
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from autophone.device.lock import QuarantineReleaseRefused, ScreenLock, process_started_at
from autophone.logging import get_logger

logger = get_logger(__name__)

__all__ = ["QuarantineReleaseRefused", "ScreenQuarantineService", "probe_device_quiesce"]

DeviceProbe = Callable[..., Mapping[str, Any]]


def default_device_target_path(device: str) -> Path:
    from autophone.rig.control_plane_env import DEFAULT_RIG_RUNTIME_ROOT

    return DEFAULT_RIG_RUNTIME_ROOT / device / "device-target.json"


#: Frida 只枚举进程的上限（秒）。
FRIDA_ENUMERATION_TIMEOUT_S = 10.0


def probe_device_quiesce(*, frida_endpoint: str, socket: str) -> dict[str, Any]:
    """三项只读探针，不经 ScreenLock、不 attach 任何进程（BL-232 阶段 2）。

    ``lock_state`` 经 UDS 执行 autophone guest 辅助程序（首次使用时推送到
    ``/var/jb/tmp/autophone-lockstate-*``），不受 ``AUTOPHONE_NATIVE_LOCK_STATE`` 开关控制。
    """
    from autophone.device import probe_uds_state
    from autophone.device.frida import probe_enumeration
    from autophone.observation.vphone_native_state import probe_native_lock_state

    native = dict(probe_native_lock_state(sock_path=socket))
    enumeration = dict(probe_enumeration(host=frida_endpoint, timeout_s=FRIDA_ENUMERATION_TIMEOUT_S))
    matrix = enumeration.get("matrix") if isinstance(enumeration.get("matrix"), Mapping) else {}
    uds = dict(probe_uds_state(socket, timeout=3.0))
    return {
        "lock_state": {
            "socket": socket,
            "passed": native.get("ok") is True and type(native.get("locked")) is bool,
            **{key: native.get(key) for key in (
                "stage", "locked", "lockstate", "has_blanked_screen", "springboard", "source",
                "detail", "elapsed_ms", "confirmed_after_wait_s",
            ) if key in native},
        },
        "frida_enumeration": {
            "endpoint": frida_endpoint,
            "passed": enumeration.get("ok") is True and matrix.get("enum") is True,
            "mode": matrix.get("mode"),
            "target": matrix.get("target"),
            "pid": matrix.get("pid"),
            "processes_listed": matrix.get("processes_listed"),
            **{key: enumeration.get(key) for key in ("stage", "detail", "elapsed_ms") if key in enumeration},
        },
        "uds": {
            "socket": socket,
            "passed": uds.get("available") is True,
            **{key: uds.get(key) for key in ("state", "evidence") if key in uds},
        },
    }


def _owner_process(marker: Mapping[str, Any], *, started_at=process_started_at) -> dict[str, Any]:
    pid = marker.get("pid")
    if pid is None:
        return {"pid": None, "state": "unknown", "detail": "marker predates pid recording"}
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return {"pid": pid, "state": "exited"}
    except PermissionError:
        pass
    started = started_at(pid)
    try:
        written = datetime.fromisoformat(str(marker["at"]))
    except ValueError:
        written = None
    if started is not None and written is not None and started > written:
        # 同一 pid 的进程晚于标记启动：pid 已被复用，写标记的进程已退出。
        return {"pid": pid, "state": "exited", "detail": f"pid reused, started {started.isoformat()}"}
    return {"pid": pid, "state": "alive",
            "started_at": started.isoformat() if started is not None else None}


class ScreenQuarantineService:
    def __init__(
        self, *, lock: ScreenLock | None = None,
        control_plane: Callable[[], Any] | None = None,
        probe: DeviceProbe = probe_device_quiesce,
        started_at: Callable[[int], datetime | None] = process_started_at,
        target_path: Callable[[str], Path] = default_device_target_path,
    ) -> None:
        self.lock = lock or ScreenLock()
        if control_plane is None:
            from autophone.application.control_plane import configured_control_plane

            control_plane = configured_control_plane
        self._control_plane = control_plane
        self._probe = probe
        self._started_at = started_at
        self._target_path = target_path

    def status(self, device: str) -> dict[str, Any]:
        quarantine = self.lock.quarantine_status(device=device)
        return {
            "device": device,
            "lock_root": str(self.lock.root),
            "lock": self.lock.status(device=device),
            "quarantined": quarantine is not None,
            "quarantine": quarantine,
        }

    def _endpoints(self, device: str, frida_endpoint: str | None, socket: str | None) -> dict[str, Any]:
        if frida_endpoint and socket:
            return {"source": "explicit", "frida_endpoint": frida_endpoint, "socket": socket}
        from autophone.rig.target import DeviceTarget, TargetConfigurationError

        path = self._target_path(device)
        try:
            target = DeviceTarget.load(path)
        except TargetConfigurationError as exc:
            raise QuarantineReleaseRefused(
                "device_target_unresolved",
                f"{exc}; pass --frida-host and --socket explicitly",
            ) from exc
        if target.rig_id != device and target.vm_dir.name != device:
            raise QuarantineReleaseRefused(
                "device_target_unresolved", f"{path} belongs to rig {target.rig_id!r}",
            )
        return {"source": str(path), "frida_endpoint": frida_endpoint or target.frida_endpoint,
                "socket": socket or str(target.socket)}

    def _control_plane_check(self, device: str) -> dict[str, Any]:
        from autophone.application.control_plane import ControlPlaneUnavailable

        try:
            control = self._control_plane()
        except ControlPlaneUnavailable as exc:
            raise QuarantineReleaseRefused("control_plane_unavailable", str(exc)) from exc
        if control is None:
            return {"configured": False}
        try:
            rig_id = control.resolve_device_rig(device)
            activity = dict(control.rig_activity(rig_id))
        except Exception as exc:  # noqa: BLE001 - any query failure is fail-closed
            raise QuarantineReleaseRefused(
                "control_plane_query_failed", f"{type(exc).__name__}: {exc}",
            ) from exc
        return {"configured": True, "rig_id": rig_id, **activity}

    def release(
        self, device: str, *, actor: str, reason: str, confirm_quiesced: bool = False,
        frida_endpoint: str | None = None, socket: str | None = None,
    ) -> dict[str, Any]:
        def verify(marker: dict[str, Any]) -> dict[str, Any]:
            checks: dict[str, Any] = {}

            def refuse(code: str, message: str) -> QuarantineReleaseRefused:
                return QuarantineReleaseRefused(code, message, details={"checks": checks})

            owner = _owner_process(marker, started_at=self._started_at)
            checks["owner_process"] = owner
            if owner["state"] == "alive":
                raise refuse("marker_owner_process_alive",
                             f"pid {owner['pid']} that wrote the marker is still running")
            control = self._control_plane_check(device)
            checks["control_plane"] = control
            blockers = [key for key in ("active_assignments", "unreleased_leases", "active_recoveries")
                        if control.get(key)]
            if blockers:
                raise refuse("rig_activity_present", "; ".join(
                    f"{key}={control[key]}" for key in blockers))
            endpoints = self._endpoints(device, frida_endpoint, socket)
            probes = dict(self._probe(frida_endpoint=endpoints["frida_endpoint"], socket=endpoints["socket"]))
            checks["device_probe"] = {"endpoints": endpoints, **probes}
            failed = sorted(name for name, value in probes.items()
                            if not (isinstance(value, Mapping) and value.get("passed") is True))
            if failed:
                raise refuse("device_probe_failed", "failed: " + ", ".join(failed))
            checks["quiesce_confirmation"] = {
                "kind": "operator_attested" if confirm_quiesced else "missing",
                "actor": actor,
                "basis": "host cannot enumerate leftover Frida sessions or suspended processes",
            }
            if not confirm_quiesced:
                raise refuse("quiesce_confirmation_required",
                             "automatic checks passed; rerun with --confirm-quiesced after checking the device")
            return checks

        try:
            receipt = self.lock.release_quarantine(device=device, actor=actor, reason=reason, verify=verify)
        except QuarantineReleaseRefused as exc:
            logger.warning("screen quarantine release refused: device=%s code=%s message=%s",
                           device, exc.code, exc.message)
            raise
        logger.info("screen quarantine released: device=%s actor=%s marker_sha256=%s receipt=%s",
                    device, receipt["actor"], receipt["marker_sha256"], receipt["receipt_path"])
        return receipt
