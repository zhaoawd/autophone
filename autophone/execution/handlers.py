"""Run type handlers used by the Rig Agent composition root."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from autophone.device import DeviceSession, VPhoneDeviceSession
from autophone.device.target import VPhoneTarget, device_write_scope, target_scope
from autophone.rig.agent import AgentExecutionContext
from autophone.rig.target import DeviceTarget

from .assignment import AssignmentExecution
from .run import RunExecution, RunExecutionContext, RunExecutionError, RunHandlerOutput


class _RunServiceHandler:
    run_type = ""

    def __init__(self, run_service: Any) -> None:
        self.run_service = run_service

    def execute(
        self,
        run_spec: Mapping[str, Any],
        context: RunExecutionContext,
    ) -> RunHandlerOutput:
        actual = str(run_spec.get("type") or "")
        if actual != self.run_type:
            raise RunExecutionError(
                f"{type(self).__name__} cannot execute Run type {actual!r}"
            )
        explicit = context.device.target
        from .world_identity import device_scope
        with target_scope(VPhoneTarget(
            cli_dir=explicit.cli_dir,
            vm_dir=explicit.vm_dir,
            sock=explicit.socket,
            device=explicit.rig_id,
            protocol=getattr(explicit, "protocol", "legacy"),
        )), device_write_scope(context.device.write_fence.check), device_scope(context.device):
            handle = self.run_service.submit(dict(run_spec))
        return RunHandlerOutput(dict(handle.result), Path(handle.run_dir))


class RegressionRunHandler(_RunServiceHandler):
    run_type = "regression"


class VerificationRunHandler(_RunServiceHandler):
    run_type = "verification"


class OperationRunHandler(_RunServiceHandler):
    run_type = "operation"


def build_run_handlers(run_service: Any) -> dict[str, Any]:
    from autophone.rig.plugins import load_run_handlers

    handlers = {
        "regression": RegressionRunHandler(run_service),
        "verification": VerificationRunHandler(run_service),
        "operation": OperationRunHandler(run_service),
    }
    for name, handler in load_run_handlers().items():
        if name in handlers:
            raise ValueError(f"duplicate Run handler: {name}")
        handlers[name] = handler
    return handlers


def __getattr__(name: str):
    if name == "DeliveryDeviceWorkHandler":
        from autophone.rig.plugins import load_run_handlers
        handler = load_run_handlers().get("delivery_device_work")
        if handler is not None:
            return type(handler)
    raise AttributeError(name)


def _device_factory(context: AgentExecutionContext) -> DeviceSession:
    """把包仓库的安装实现注入设备会话。

    设备会话通过注入操作安装固定 Build，并读取该 Build 的构建标记供 Provisioning
    核对（BL-237）；本执行器工厂装配包仓库实现，设备层不直接选择包仓库。
    """
    from autophone.packages import store as packages
    from autophone.packages.build_identity import stored_build_marker
    from autophone.packages.model import RegistryBuildRef

    def install_exact_build(build_ref: Mapping[str, Any], **options: Any) -> Mapping[str, Any]:
        # Device Control 只拿完整 Build Ref；解析（slot → Candidate、default_slot）已经在
        # Build Catalog 侧完成。这里把 dict 形态还原成 RegistryBuildRef 后按 build_id 精确安装。
        return packages.install_build_ref(RegistryBuildRef.from_dict(build_ref), **options)

    return VPhoneDeviceSession(
        context.target,
        context.write_fence,
        install_package_fn=install_exact_build,
        build_marker_fn=stored_build_marker,
    )


def create_device_session_ops(*, target: DeviceTarget, run_root: Path | None = None) -> Any:
    """交互通道的执行面（BL-100），在 rig agent 进程里装配。

    与 `create_executor` 共用装配根职责：本文件将
    `autophone.application.device_console` 的会话与 lease 实现注入交互通道。
    RunService 与设备控制台已归位，本文件已退出 R1 例外清单。

    注入的操作面：`DeviceConsoleManager`（会话与 ScreenLock lease 的真源，语义与 web 进程
    里那份逐字相同）、`DeviceApplication.screenshot`（预览帧，不持 lease；Run 持有 Stage 时
    改由 `executor_frames.preview_state` 返回执行侧最近帧，BL-227）、
    `RunService.status`（设备灯与队列投影）及 `RecordingSessionOps`（录制生命周期）。
    这些操作共用同一把 `ScreenLock`，所以 run、
    录制与 console 会话之间的互斥与兼容路径完全一致。

    这里的 `RunService` 与 `create_executor` 里那个是两个实例：构造不起线程、不占设备，
    只用它的 `lock` 与 `status`。合并成一个要改 `rig/main.py` 的工厂契约，不在本条范围内。

    `DeviceConsoleManager` 的预留登记面用默认实现（BL-107）：会话建立后往控制面写一条
    `rig_reservations`，关闭时释放。rig agent 由 `scripts/start_vphone.sh` 在导出
    `AUTOPHONE_DATABASE_URL` 之后拉起，所以这里不必额外传 DSN；控制面未配置时该实现
    自己 no-op，不登记也不报错。

    释放复位钩子（BL-104 / 设计文档 §7.3）也在这里注入：会话结束时回主屏、保留登录。
    授权记录（`access_grants`）不在 rig 侧——rig 只认会话与 lease，授权在 web 进程。
    """
    from autophone.application.console_release import ConsoleReleaseReset
    from autophone.application.device import DeviceApplication
    from autophone.application.recording_session import RecordingSessionOps
    from autophone.device.target import resolve_target
    from autophone.application.device_session import DeviceSessionOps
    from autophone.application.device_console import DeviceConsoleManager, UdsDeviceDriver
    from autophone.execution.run_service import RunService

    service = RunService(run_root=run_root)
    console = DeviceConsoleManager(
        lock=service.lock,
        # 释放复位（BL-104 / 设计文档 §7.3）：会话结束时回主屏、保留登录。
        # 它在还锁之前跑，抛错即「清理失败」（§8.4），设备进不可分配。
        release_cleanup=ConsoleReleaseReset(driver_factory=UdsDeviceDriver),
    )
    console.start_scanner()
    from autophone.device.executor_frames import preview_state

    device_application = DeviceApplication()
    resolved = resolve_target(environ={"VM_DIR": str(target.vm_dir)})
    alias = resolved.device
    # 执行器在本进程内经 target.socket 取图（`target_scope` 绑定同一 socket），最近帧按它分键。
    preview_socket = getattr(target, "socket", None) or resolved.sock
    device_aliases = {key: target.rig_id for key in (target.rig_id, alias)}
    return DeviceSessionOps(
        console=console,
        device_aliases=device_aliases,
        human_holder=service.lock.read_human_holder,
        recordings=RecordingSessionOps(
            lock=service.lock, device=target.rig_id, device_aliases=tuple(device_aliases),
        ),
        screenshot=device_application.screenshot,
        status=lambda device: service.status(device=device),
        target=device_application.target,
        # BL-227：Run 持有 Stage 期间预览返回执行侧最近帧，不访问设备。
        executor_preview=lambda: preview_state(preview_socket),
    )


def create_executor(*, target: DeviceTarget, run_root: Path | None = None) -> Any:
    """Build one Assignment executor without importing RunService in Agent Core."""
    mismatched = [
        name
        for name, value in target.process_environment().items()
        if str(os.environ.get(name) or "") != str(value)
    ]
    if mismatched:
        raise RuntimeError(
            "Rig Agent target environment is not bound before executor construction: "
            + ", ".join(sorted(mismatched))
        )
    from autophone.assets.apps import load_app_profile
    from autophone.execution.run_service import RunService

    service = RunService(run_root=run_root)
    from .interrupted_closure import InterruptedAttemptClosureExecution
    from .recovery import InterruptedRunCollection, RecoveryAssignmentExecution, RecoveryAwareExecutor
    return RecoveryAwareExecutor(AssignmentExecution(
        RunExecution(build_run_handlers(service), admission_scope=service.plugin_identity_scope),
        load_app_profile,
        device_factory=_device_factory,
        closure_scope=service.assignment_closure,
        screen_lock=service.lock,
    ), RecoveryAssignmentExecution(closure_recovery=InterruptedRunCollection(service)),
        closure=InterruptedAttemptClosureExecution(
            service.run_root, screen_lock=service.lock, collect=service.resume_collection,
            profile_loader=load_app_profile))
