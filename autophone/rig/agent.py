"""Rig Agent runtime with reconnect sync and fail-closed device writes."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from autophone.logging import bind_run, get_logger

from .journal import AgentJournal
from .target import DeviceTarget, served_device_keys
from .source_identity import SourceProcessStale, assert_process_source_current


log = get_logger(__name__)

#: 上报失败（如控制面尚不认识该消息）后的重试间隔；Agent 主循环约每秒一轮，不能每轮都重试。
SCREEN_QUARANTINE_REPORT_RETRY_S = 30.0

#: Agent 接单后在执行器之前判定本进程源码过期时的拒单原因（BL-214）。
RIG_AGENT_STALE = "rig_agent_stale"

#: Assignment 的 Run Spec 点名的设备不是本 rig 时的拒单原因（BL-217）。
DEVICE_MISMATCH = "device_mismatch"

#: 执行前检测到 VM 进程已不在时的拒单原因。
VM_PROCESS_DEAD = "vm_process_dead"

#: 重连时本地执行日志只有 ``assignment_started``、没有完成或停止记录的 Attempt 的中断原因（BL-240）。
#: 这样的 Attempt 不再执行，向控制面报告中断，由 BL-076 的闭包路径处置。
AGENT_RESTARTED_AFTER_START = "agent_restarted_after_start"

#: ``execute_assignment`` 对不再执行的 Attempt 返回的 ``status``（BL-240）。
NOT_REPLAYED = "not_replayed"

#: StageGuard 未能确认释放本 Attempt 已持有的设备资源时，Assignment Result 的 ``reason``
#: （BL-220 剩余项）。控制面只对记录过 quiesce 失败的 Attempt 接受该原因的完成回执。
DEVICE_QUIESCE_UNVERIFIED = "device_quiesce_unverified"


def _assignment_device(assignment: Mapping[str, Any]) -> str:
    """Run Spec 点名的设备；没有 Run Spec 或未点名时为空（BL-217）。

    交付工作流设备工作的 Run Spec 不含 ``device``，包安装与恢复 Assignment 没有 Run Spec，
    这些 Assignment 由 ``target_rig_id`` 定位，不在此核对。
    """
    if assignment.get("kind") == "recovery":
        return ""
    run_spec = assignment.get("run_spec")
    if not isinstance(run_spec, Mapping):
        return ""
    return str(run_spec.get("device") or "").strip()


def _assignment_run_id(assignment: Mapping[str, Any]) -> str:
    run_spec = assignment.get("run_spec")
    if isinstance(run_spec, Mapping):
        return str(run_spec.get("run_id") or "")
    return ""


class StaleDeviceWrite(RuntimeError):
    pass


class AssignmentCancelled(RuntimeError):
    pass


class ExecutorContractError(RuntimeError):
    """Executor escaped without a trustworthy Cleanup or completion receipt."""


class AssignmentDeclineNotAcknowledged(RuntimeError):
    """设备不符的拒单未被控制面确认（BL-217）。

    不在 Agent 的退出类异常之列：`run_forever` 按断线处理并重连，握手重放仍持有租约的
    Assignment 时重新核对并再次拒单；租约先到期时按原逻辑收为 `indeterminate`。
    """


class AgentRestartRequired(RuntimeError):
    """终态已被控制面接受，但本进程不得继续接单，须由 bring-up 重启（BL-220 剩余项）。

    quiesce 未确认时，未释放的 Frida 会话等句柄可能仍留在本进程里；ScreenLock 标记记录的写入
    进程即本进程，BL-223 的解除入口要求它已退出。进程退出同时关闭这些宿主侧连接。
    """


class AssignmentControl:
    """Share Lease-loss and cooperative-cancel state with the executor.

    取消是协作式的：它在阶段边界（`check`）和 Cleanup 之前的设备操作（`check_device_write`）
    处生效。执行器进入 Cleanup（`begin_cleanup`）之后，复位、回读与资源释放属于取消路径本身，
    设备操作不再因取消被拒，只受租约与 fencing 约束（BL-220）。StageGuard 释放已持有资源
    （`check_release`）在任何阶段都不因取消被拒。租约失效在任何阶段都拒绝。
    """

    def __init__(self) -> None:
        self.cancel_requested = threading.Event()
        self.lease_lost = threading.Event()
        self.cleanup_started = threading.Event()
        self._lease_error: Exception | None = None
        self._cancel_before_cleanup = False
        self._lock = threading.Lock()

    def request_cancel(self) -> None:
        self.cancel_requested.set()

    def mark_lease_lost(self, error: Exception) -> None:
        with self._lock:
            if self._lease_error is None:
                self._lease_error = error
        self.lease_lost.set()

    def begin_cleanup(self) -> None:
        """执行器进入 Cleanup；记下此刻取消是否已被执行器看到。只生效一次。"""
        with self._lock:
            if self.cleanup_started.is_set():
                return
            self._cancel_before_cleanup = self.cancel_requested.is_set()
            self.cleanup_started.set()

    def cancel_applied(self) -> bool:
        """取消是否在执行器进入 Cleanup 之前已被看到，即是否可能改变了执行。

        执行器从未调用 `begin_cleanup` 时，以调用此方法的时刻为界（Agent 在执行器返回后、
        完成前最后一次心跳之前调用）。
        """
        with self._lock:
            if self.cleanup_started.is_set():
                return self._cancel_before_cleanup
        return self.cancel_requested.is_set()

    def _check_lease(self) -> None:
        if self.lease_lost.is_set():
            error = self._lease_error
            raise StaleDeviceWrite(
                f"Lease renewal failed: {type(error).__name__}: {error}"
            ) from error

    def check(self) -> None:
        """阶段边界的协作检查：租约失效或取消都会中止后续阶段。"""
        self._check_lease()
        if self.cancel_requested.is_set():
            raise AssignmentCancelled("Assignment cancellation requested")

    def check_device_write(self) -> None:
        """设备操作前的本地检查：Cleanup 之前取消即拒绝；Cleanup 中只看租约。"""
        self._check_lease()
        if self.cancel_requested.is_set() and not self.cleanup_started.is_set():
            raise AssignmentCancelled("Assignment cancellation requested")

    def check_release(self) -> None:
        """释放已持有设备资源前的本地检查：只看租约，不因取消拒绝。

        StageGuard 在阶段切换（含 Run 内的 collect 切换）时释放本 Assignment 已持有的句柄
        （Frida 会话、挂起进程等）。释放属于取消后的收尾，取消不得阻止它，否则资源释放记为失败、
        设备被隔离（BL-220 第二轮）。新的设备操作仍走 `check_device_write`；不调用
        `begin_cleanup`，不改变 `cancel_applied` 的判定。
        """
        self._check_lease()


class AgentTransport(Protocol):
    def connect(
        self,
        target: DeviceTarget,
        capability_support: list[str],
        source_identity: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...
    def receive_assignment(self) -> dict[str, Any] | None: ...
    def heartbeat(self, assignment_id: str, attempt_no: int, fencing: int) -> dict[str, Any]: ...
    def validate_write(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        target: DeviceTarget,
    ) -> dict[str, Any]: ...
    def record_stage(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        stage: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]: ...
    def read_observed_state(self, target: DeviceTarget) -> dict[str, Any]: ...
    def publish_assignment_observed_state(
        self, target: DeviceTarget, assignment_id: str, attempt_no: int, fencing: int,
        observed_state: Mapping[str, Any], capability_state: Mapping[str, Any],
    ) -> dict[str, Any]: ...
    def update_observed_state(
        self,
        target: DeviceTarget,
        observed_state: Mapping[str, Any],
        capability_state: Mapping[str, Any],
    ) -> dict[str, Any]: ...
    def report_screen_quarantine(self, state: Mapping[str, Any]) -> dict[str, Any]: ...
    def complete(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        result: Mapping[str, Any],
    ) -> dict[str, Any]: ...
    def decline(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        *,
        reason: str,
        detail: str,
    ) -> dict[str, Any]: ...
    def report_interrupted(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        *,
        reason: str,
        detail: str,
    ) -> dict[str, Any]: ...
    def close(self) -> None: ...


class AgentAssignmentExecutor(Protocol):
    def execute(
        self,
        assignment: Mapping[str, Any],
        context: "AgentExecutionContext",
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class DeviceWriteFence:
    transport: AgentTransport
    target: DeviceTarget
    assignment_id: str
    attempt_no: int
    fencing: int
    assignment_control: AssignmentControl | None = None

    def check(self) -> None:
        # 本地先查租约与取消（Cleanup 中不因取消拒绝，见 `AssignmentControl`），
        # 再由控制面核对 Lease、Fencing、Rig 身份与 Generation；后者在任何阶段都必须通过。
        if self.assignment_control is not None:
            self.assignment_control.check_device_write()
        self._validate_with_control_plane()

    def check_release(self) -> None:
        """StageGuard 释放已持有资源前的检查：取消不拒绝，租约失效与控制面拒绝仍拒绝。"""
        if self.assignment_control is not None:
            self.assignment_control.check_release()
        self._validate_with_control_plane()

    def _validate_with_control_plane(self) -> None:
        try:
            result = self.transport.validate_write(
                self.assignment_id,
                self.attempt_no,
                self.fencing,
                self.target,
            )
        except Exception as exc:
            raise StaleDeviceWrite(
                f"device write rejected for {self.assignment_id}/"
                f"{self.attempt_no}/{self.fencing}: {exc}"
            ) from exc
        if result.get("valid") is not True:
            raise StaleDeviceWrite(
                f"device write rejected for {self.assignment_id}/"
                f"{self.attempt_no}/{self.fencing}"
            )

    def run(self, action: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        self.check()
        return action(*args, **kwargs)


@dataclass(frozen=True)
class AgentExecutionContext:
    target: DeviceTarget
    write_fence: DeviceWriteFence
    journal: AgentJournal
    assignment_control: AssignmentControl = field(default_factory=AssignmentControl)

    def check_control(self) -> None:
        self.assignment_control.check()


class RigAgent:
    """Own one Rig and execute only work synchronized for that Rig identity."""

    def __init__(
        self,
        target: DeviceTarget,
        transport: AgentTransport,
        executor: AgentAssignmentExecutor,
        *,
        capability_support: list[str] | None = None,
        source_identity: Mapping[str, Any] | None = None,
        reconnect_delay_s: float = 1.0,
        heartbeat_interval_s: float = 15.0,
        vm_alive_probe: Callable[[DeviceTarget], bool | None] | None = None,
        down_observer: Callable[[DeviceTarget], None] | None = None,
        vm_liveness_interval_s: float = 2.0,
        idle_source_check_interval_s: float | None = None,
    ) -> None:
        self.target = target
        self.transport = transport
        self.executor = executor
        self.capability_support = sorted(set(capability_support or []))
        # BL-151：进程内代码是启动时快照，身份在包首次导入时冻结，之后不再变——
        # 重连时上报的仍是这个进程真正在执行的那份代码。
        if source_identity is None:
            from .source_identity import process_source_identity as compute_source_identity

            source_identity = compute_source_identity()
        self.source_identity = dict(source_identity)
        self.reconnect_delay_s = reconnect_delay_s
        if heartbeat_interval_s <= 0:
            raise ValueError("heartbeat_interval_s must be positive")
        self.heartbeat_interval_s = heartbeat_interval_s
        if vm_liveness_interval_s < 0:
            raise ValueError("vm_liveness_interval_s must be non-negative")
        self.vm_alive_probe = vm_alive_probe
        self.down_observer = down_observer
        self.vm_liveness_interval_s = vm_liveness_interval_s
        if idle_source_check_interval_s is not None and idle_source_check_interval_s < 0:
            raise ValueError("idle_source_check_interval_s must be non-negative")
        self.idle_source_check_interval_s = idle_source_check_interval_s
        self.target.ensure_local_directories()
        self.journal = AgentJournal(target.log_dir / "agent-execution.jsonl")
        self._stopped = False
        self._screen_quarantine_reported: tuple[Any, ...] | None = None
        self._screen_quarantine_retry_at = 0.0
        self._vm_dead_reported: bool = False
        self._last_vm_check_at: float = 0.0

    def report_screen_quarantine(self, *, force: bool = False) -> bool:
        """把本 Rig 的 ScreenLock 隔离标记状态上报控制面；状态变化或 ``force`` 时才发送（BL-224）。

        标记在 Agent 所在宿主的锁根里，控制面与提交方可能不在同机，派单与提交据此上报判断。
        读状态与发送失败都不使 Agent 退出：上报只服务派单前的早退，执行器在接单与取锁时仍按
        本机标记 fail-closed。返回本次是否发送成功。
        """
        source = getattr(self.executor, "screen_quarantine_report", None)
        send = getattr(self.transport, "report_screen_quarantine", None)
        if not callable(source) or not callable(send):
            return False
        if not force and time.monotonic() < self._screen_quarantine_retry_at:
            return False
        try:
            state = dict(source(self.target))
        except Exception as exc:  # noqa: BLE001 - a read failure must not stop the Agent
            log.warning("screen quarantine state unreadable: %s: %s", type(exc).__name__, exc)
            self._screen_quarantine_retry_at = time.monotonic() + SCREEN_QUARANTINE_REPORT_RETRY_S
            return False
        fingerprint = (
            bool(state.get("quarantined")),
            tuple(sorted(
                (str(item.get("device")), str(item.get("sha256")))
                for item in state.get("markers") or []
            )),
        )
        if not force and fingerprint == self._screen_quarantine_reported:
            return False
        try:
            send(state)
        except Exception as exc:  # noqa: BLE001 - retried later; dispatch still has the executor check
            log.warning(
                "screen quarantine report not accepted (retry in %.0fs): %s: %s",
                SCREEN_QUARANTINE_REPORT_RETRY_S, type(exc).__name__, exc,
            )
            self._screen_quarantine_reported = None
            self._screen_quarantine_retry_at = time.monotonic() + SCREEN_QUARANTINE_REPORT_RETRY_S
            return False
        self._screen_quarantine_reported = fingerprint
        self._screen_quarantine_retry_at = 0.0
        if fingerprint[0]:
            log.warning(
                "screen quarantine reported: rig=%s markers=%s",
                self.target.rig_id, [item.get("path") for item in state.get("markers") or []],
            )
        else:
            log.info("screen quarantine reported clear: rig=%s", self.target.rig_id)
        return True

    def synchronize(self) -> list[dict[str, Any]]:
        from .source_identity import describe_identity

        log.info(
            "connecting to Control Plane: host=%s rig=%s generation=%s capabilities=%s %s",
            self.target.host_id,
            self.target.rig_id,
            self.target.rig_generation,
            self.capability_support,
            describe_identity(self.source_identity),
        )
        assignments = self.transport.connect(
            self.target, self.capability_support, self.source_identity,
        )
        self.journal.append("synchronized", {
            "host_id": self.target.host_id,
            "rig_id": self.target.rig_id,
            "rig_generation": self.target.rig_generation,
            "assignment_count": len(assignments),
        })
        log.info(
            "handshake accepted: host=%s rig=%s generation=%s resync_assignments=%d",
            self.target.host_id,
            self.target.rig_id,
            self.target.rig_generation,
            len(assignments),
        )
        closed, unfinished = self.journal.replay_state()
        replay = []
        for assignment in assignments:
            key = (
                str(assignment.get("assignment_id")),
                int((assignment.get("attempt") or {}).get("attempt_no") or 0),
            )
            if key in closed:
                continue
            if key in unfinished:
                # BL-240：前一个 Agent 进程已开始执行这个 Attempt 却没有留下结果；不重放，报告中断。
                self._report_interrupted(key[0], key[1], unfinished[key])
                continue
            replay.append(assignment)
        return replay

    def _report_interrupted(
        self, assignment_id: str, attempt_no: int, fencing: int,
    ) -> dict[str, Any] | None:
        """报告一个已开始未结束的 Attempt 中断；返回控制面确认，未确认时返回 None（BL-240）。

        确认后控制面已释放该 Attempt 的 Lease（旧 Fencing 的设备写入随之被拒绝），把 Attempt 收为
        ``indeterminate``、登记资源闭包并隔离 Rig；本进程记 ``assignment_interrupted``，此后不再报告。
        未确认时（旧控制面不认识该消息、断线、身份不符）只记录，不重放：Lease 按原逻辑到期回收，
        下次重连再报告一次。两种情况下本进程都继续接单；Rig 隔离后只会收到闭包恢复 Assignment。
        """
        detail = (
            f"local execution journal records assignment_started for {assignment_id}/{attempt_no} "
            f"(fencing {fencing}) without a completion or stop record"
        )
        report = getattr(self.transport, "report_interrupted", None)
        try:
            if not callable(report):
                raise AttributeError("agent transport does not support attempt_interrupted")
            acknowledged = report(
                assignment_id, attempt_no, fencing,
                reason=AGENT_RESTARTED_AFTER_START, detail=detail,
            )
        except Exception as exc:  # noqa: BLE001 - never replay; Lease expiry settles the Attempt
            log.error(
                "assignment %s/%s started by a previous Agent process is not replayed; "
                "interruption report not acknowledged (Lease expiry will settle it): %s: %s",
                assignment_id, attempt_no, type(exc).__name__, exc,
            )
            self.journal.append("interrupt_report_not_acknowledged", {
                "assignment_id": assignment_id,
                "attempt_no": attempt_no,
                "fencing": fencing,
                "reason": AGENT_RESTARTED_AFTER_START,
                "error": f"{type(exc).__name__}: {exc}",
            })
            return None
        record = dict(acknowledged) if isinstance(acknowledged, Mapping) else {}
        log.warning(
            "assignment %s/%s started by a previous Agent process is not replayed; "
            "Control Plane recorded the interruption: status=%s",
            assignment_id, attempt_no, record.get("status"),
        )
        self.journal.append("assignment_interrupted", {
            "assignment_id": assignment_id,
            "attempt_no": attempt_no,
            "fencing": fencing,
            "reason": AGENT_RESTARTED_AFTER_START,
            "status": record.get("status"),
        })
        return record

    def execute_assignment(self, assignment: Mapping[str, Any]) -> dict[str, Any]:
        """Claim one Assignment, keeping its identity on every log record.

        The operator-visible narrative of this process lives here: without the
        claim/terminal pair the Agent log stays empty while PostgreSQL is the
        only place the work is observable at all.
        """
        assignment_id = str(assignment["assignment_id"])
        attempt = assignment.get("attempt") or {}
        with bind_run(_assignment_run_id(assignment), stage="assignment"):
            log.info(
                "claimed assignment %s: attempt=%s fencing=%s kind=%s rig=%s",
                assignment_id,
                attempt.get("attempt_no"),
                attempt.get("fencing"),
                assignment.get("kind"),
                assignment.get("bound_rig_id"),
            )
            try:
                completed = self._execute_assignment(assignment)
            except Exception as exc:  # noqa: BLE001 - the raise is re-raised untouched
                log.error(
                    "assignment %s stopped: %s: %s",
                    assignment_id,
                    type(exc).__name__,
                    exc,
                )
                raise
            record = completed if isinstance(completed, Mapping) else {}
            terminal = record.get("result")
            terminal = terminal if isinstance(terminal, Mapping) else {}
            log.info(
                "assignment %s finished: status=%s product_verdict=%s rig_disposition=%s",
                assignment_id,
                record.get("status"),
                terminal.get("product_verdict"),
                terminal.get("rig_disposition"),
            )
            if terminal.get("reason") == DEVICE_QUIESCE_UNVERIFIED:
                # 终态已提交、租约已释放、rig 已隔离；本进程可能仍持有未释放的设备句柄，不再接单。
                self.journal.append("agent_restart_required", {
                    "assignment_id": assignment_id,
                    "attempt_no": attempt.get("attempt_no"),
                    "reason": DEVICE_QUIESCE_UNVERIFIED,
                })
                raise AgentRestartRequired(
                    f"{DEVICE_QUIESCE_UNVERIFIED}: assignment {assignment_id}/{attempt.get('attempt_no')} "
                    "completed with rig_disposition=quarantine; restart the Rig Agent "
                    "(start_vphone.sh) before releasing the ScreenLock marker"
                )
            return completed

    def _execute_assignment(self, assignment: Mapping[str, Any]) -> dict[str, Any]:
        assignment_id = str(assignment["assignment_id"])
        attempt = assignment.get("attempt") or {}
        attempt_no = int(attempt["attempt_no"])
        fencing = int(attempt["fencing"])
        if (
            assignment.get("bound_host_id") != self.target.host_id
            or assignment.get("bound_rig_id") != self.target.rig_id
            or int(assignment.get("bound_generation") or 0) != self.target.rig_generation
        ):
            raise StaleDeviceWrite(f"assignment target does not match Agent target: {assignment_id}")
        # BL-240：同一 Attempt 不执行第二次。握手重放已在 `synchronize` 过滤；这里覆盖派单通知
        # 与握手结果重复送达同一 Attempt 的情形。
        closed, unfinished = self.journal.replay_state()
        if (assignment_id, attempt_no) in unfinished:
            acknowledged = self._report_interrupted(
                assignment_id, attempt_no, unfinished[(assignment_id, attempt_no)],
            )
            return {
                "assignment_id": assignment_id,
                "status": NOT_REPLAYED,
                "reason": AGENT_RESTARTED_AFTER_START,
                "interruption_acknowledged": acknowledged is not None,
            }
        if (assignment_id, attempt_no) in closed:
            log.warning(
                "assignment %s/%s already has a local closing record; not executed again",
                assignment_id, attempt_no,
            )
            return {"assignment_id": assignment_id, "status": NOT_REPLAYED, "reason": "already_closed"}
        try:
            assert_process_source_current()
        except SourceProcessStale as exc:
            self.journal.append("assignment_stopped", {
                "assignment_id": assignment_id,
                "attempt_no": attempt_no,
                "reason": RIG_AGENT_STALE,
                "error": str(exc),
            })
            self._decline(assignment_id, attempt_no, fencing, RIG_AGENT_STALE, str(exc))
            raise
        mismatch = self._device_mismatch(assignment)
        if mismatch is not None:
            # 设备不符说明派单与本 rig 不一致，不说明本进程不可信：拒单后继续接单，
            # 与 `rig_agent_stale`（本进程源码过期，拒单后退出）不同（BL-217）。
            declined = self._decline(assignment_id, attempt_no, fencing, DEVICE_MISMATCH, mismatch)
            if declined is None:
                raise AssignmentDeclineNotAcknowledged(
                    f"{DEVICE_MISMATCH} decline not acknowledged: {assignment_id}/{attempt_no}"
                )
            return declined
        if assignment.get("kind") != "recovery" and self._probe_vm_alive() is False:
            self._report_vm_down(idle=False)
            declined = self._decline(
                assignment_id,
                attempt_no,
                fencing,
                VM_PROCESS_DEAD,
                "VM process is not running; declining before execution",
            )
            if declined is None:
                raise AssignmentDeclineNotAcknowledged(
                    f"{VM_PROCESS_DEAD} decline not acknowledged: {assignment_id}/{attempt_no}"
                )
            return declined
        assignment_control = AssignmentControl()
        context = AgentExecutionContext(
            target=self.target,
            write_fence=DeviceWriteFence(
                self.transport,
                self.target,
                assignment_id,
                attempt_no,
                fencing,
                assignment_control,
            ),
            journal=self.journal,
            assignment_control=assignment_control,
        )
        heartbeat = self.transport.heartbeat(assignment_id, attempt_no, fencing)
        if heartbeat.get("status") == "cancel_requested":
            assignment_control.request_cancel()
        self.journal.append("assignment_started", {
            "assignment_id": assignment_id,
            "attempt_no": attempt_no,
            "fencing": fencing,
        })
        monitor_stop = threading.Event()
        monitor = threading.Thread(
            target=self._monitor_assignment,
            args=(
                assignment_id,
                attempt_no,
                fencing,
                assignment_control,
                monitor_stop,
            ),
            name=f"lease-{self.target.rig_id}-{assignment_id}",
            daemon=True,
        )
        monitor.start()
        try:
            result = dict(self.executor.execute(assignment, context))
            # 在完成前最后一次心跳之前定下取消是否生效。
            cancel_applied = assignment_control.cancel_applied()
        except AssignmentCancelled as exc:
            self.journal.append("assignment_stopped", {
                "assignment_id": assignment_id,
                "attempt_no": attempt_no,
                "reason": "executor_cancelled_without_cleanup",
                "error": str(exc),
            })
            raise ExecutorContractError(
                "Executor propagated cancellation without a Cleanup receipt"
            ) from exc
        except StaleDeviceWrite:
            self.journal.append("assignment_stopped", {
                "assignment_id": assignment_id,
                "attempt_no": attempt_no,
                "reason": "stale_fencing",
            })
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed without inventing Cleanup
            self.journal.append("assignment_stopped", {
                "assignment_id": assignment_id,
                "attempt_no": attempt_no,
                "reason": "executor_failed_without_cleanup",
                "error": f"{type(exc).__name__}: {exc}",
            })
            raise ExecutorContractError(
                "Executor failed without a Cleanup receipt"
            ) from exc
        finally:
            monitor_stop.set()
            monitor.join(timeout=max(1.0, self.heartbeat_interval_s + 1.0))
        if assignment_control.lease_lost.is_set():
            self.journal.append("assignment_stopped", {
                "assignment_id": assignment_id,
                "attempt_no": attempt_no,
                "reason": "lease_renewal_failed",
            })
            assignment_control.check()
        try:
            final_heartbeat = self.transport.heartbeat(
                assignment_id,
                attempt_no,
                fencing,
            )
        except Exception as exc:
            self.journal.append("assignment_stopped", {
                "assignment_id": assignment_id,
                "attempt_no": attempt_no,
                "reason": "lease_renewal_failed_before_completion",
            })
            raise StaleDeviceWrite(
                f"final Lease renewal failed: {type(exc).__name__}: {exc}"
            ) from exc
        if final_heartbeat.get("status") == "cancel_requested":
            assignment_control.request_cancel()
        if assignment_control.cancel_requested.is_set() and not cancel_applied:
            # 取消在执行器进入 Cleanup 之后（收尾期间或完成前最后一次心跳）才被看到：
            # 执行与收尾都没有因它改变，终态沿用执行结果，不记 `cancelled`，以免与 Run Result、
            # `runs` 行和批次条目相矛盾（BL-220）。取消请求本身仍留在控制面的 `cancel_request`。
            log.info(
                "assignment %s: cancellation requested after the executor entered Cleanup; "
                "keeping terminal status %s",
                assignment_id,
                result.get("status"),
            )
            self.journal.append("assignment_cancel_not_applied", {
                "assignment_id": assignment_id,
                "attempt_no": attempt_no,
                "status": result.get("status"),
            })
            note = "cancellation requested after the executor entered Cleanup; not applied"
            result = {
                **result,
                "detail": f"{result['detail']}; {note}" if result.get("detail") else note,
            }
        elif (
            assignment_control.cancel_requested.is_set()
            and result.get("status") != "cancelled"
            and result.get("reason") != DEVICE_QUIESCE_UNVERIFIED
        ):
            # 取消在 Cleanup 之前已被看到，但执行器未自行标 `cancelled`（如恢复执行器把取消收成
            # error）：只改 Assignment 终态。已有 Run Result 时 Product Verdict 仍是它的 status：
            # 控制面拒收与 run_result.status 不一致的 product_verdict。quiesce 未确认的终态不改：
            # 控制面对它只接受 `error`。
            run_result = result.get("run_result")
            result = {
                **result,
                "status": "cancelled",
                "product_verdict": (
                    str(run_result.get("status") or "")
                    if isinstance(run_result, Mapping) else "not_run"
                ),
                "detail": "Assignment cancellation requested",
            }
        try:
            completed = self.transport.complete(
                assignment_id,
                attempt_no,
                fencing,
                result,
            )
        except Exception as exc:
            self.journal.append("assignment_stopped", {
                "assignment_id": assignment_id,
                "attempt_no": attempt_no,
                "reason": "completion_not_acknowledged",
                "error": f"{type(exc).__name__}: {exc}",
            })
            raise ExecutorContractError(
                "Assignment completion was not acknowledged"
            ) from exc
        self.journal.append("assignment_completed", {
            "assignment_id": assignment_id,
            "attempt_no": attempt_no,
            "fencing": fencing,
            "status": result.get("status"),
        })
        return completed

    def _device_mismatch(self, assignment: Mapping[str, Any]) -> str | None:
        """Run Spec 点名的设备不在本 rig 的设备键中时返回说明，否则返回 None（BL-217）。

        设备键与控制面 `resolve_device_rig` 的匹配规则一致：`rig_id` 或 VM 目录名。未点名设备的
        Assignment（交付工作流、包安装、恢复）不核对。
        """
        device = _assignment_device(assignment)
        if not device:
            return None
        keys = served_device_keys(self.target)
        if device in keys:
            return None
        return (
            f"run_spec.device={device!r} is not served by rig {self.target.rig_id!r} "
            f"(device keys: {', '.join(keys)})"
        )

    def _decline(
        self, assignment_id: str, attempt_no: int, fencing: int, reason: str, detail: str,
    ) -> dict[str, Any] | None:
        """执行器之前拒单：请控制面把 Attempt 收为 `blocked` 并释放租约（BL-214、BL-217）。

        只在没有心跳、没有阶段结果时调用，控制面据此核对设备未被本 Attempt 触碰。成功时返回控制面
        给出的 Assignment 记录；失败时只记录并返回 None：租约按原逻辑过期，Attempt 收为
        `indeterminate`。
        """
        decline = getattr(self.transport, "decline", None)
        try:
            if not callable(decline):
                raise AttributeError("agent transport does not support decline")
            declined = decline(assignment_id, attempt_no, fencing, reason=reason, detail=detail)
        except Exception as exc:  # noqa: BLE001 - the caller decides between exit and reconnect
            log.error(
                "assignment %s decline not acknowledged: reason=%s: %s: %s",
                assignment_id, reason, type(exc).__name__, exc,
            )
            self.journal.append("decline_not_acknowledged", {
                "assignment_id": assignment_id,
                "attempt_no": attempt_no,
                "reason": reason,
                "error": f"{type(exc).__name__}: {exc}",
            })
            return None
        log.warning(
            "assignment %s declined before execution: reason=%s detail=%s", assignment_id, reason, detail,
        )
        self.journal.append("assignment_declined", {
            "assignment_id": assignment_id,
            "attempt_no": attempt_no,
            "fencing": fencing,
            "reason": reason,
        })
        return dict(declined) if isinstance(declined, Mapping) else {"assignment_id": assignment_id}

    def _monitor_assignment(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        assignment_control: AssignmentControl,
        stopped: threading.Event,
    ) -> None:
        while not stopped.wait(self.heartbeat_interval_s):
            try:
                heartbeat = self.transport.heartbeat(
                    assignment_id,
                    attempt_no,
                    fencing,
                )
            except Exception as exc:  # noqa: BLE001 - foreground observes the stored failure
                assignment_control.mark_lease_lost(exc)
                return
            if heartbeat.get("status") == "cancel_requested":
                assignment_control.request_cancel()

    def _report_vm_down(self, *, idle: bool) -> None:
        if self._vm_dead_reported:
            return
        self._vm_dead_reported = True
        log.warning("vm process is not running: rig=%s idle=%s", self.target.rig_id, idle)
        if idle:
            self.journal.append("idle_vm_down_detected", {"rig_id": self.target.rig_id})
        if self.down_observer is not None:
            try:
                self.down_observer(self.target)
            except Exception as exc:  # noqa: BLE001 - down observation must not stop the Agent
                log.warning(
                    "down observer failed: rig=%s %s: %s",
                    self.target.rig_id, type(exc).__name__, exc,
                )

    def _probe_vm_alive(self) -> bool | None:
        if self.vm_alive_probe is None:
            return None
        try:
            alive = self.vm_alive_probe(self.target)
        except Exception as exc:  # noqa: BLE001 - liveness probe errors must not crash the Agent
            log.warning(
                "vm liveness probe failed: rig=%s %s: %s",
                self.target.rig_id, type(exc).__name__, exc,
            )
            return None
        if alive is True:
            self._vm_dead_reported = False
        return alive

    def _check_idle_vm_liveness(self) -> None:
        if self.vm_alive_probe is None:
            return
        now = time.monotonic()
        if self._last_vm_check_at != 0.0 and now - self._last_vm_check_at < self.vm_liveness_interval_s:
            return
        self._last_vm_check_at = now
        if self._probe_vm_alive() is False:
            self._report_vm_down(idle=True)

    def run_once(self) -> bool:
        assignment = self.transport.receive_assignment()
        if assignment is None:
            return False
        self.execute_assignment(assignment)
        return True

    def run_forever(self) -> None:
        pending: list[dict[str, Any]] = []
        last_source_check_at = time.monotonic()
        while not self._stopped:
            try:
                pending.extend(self.synchronize())
                self.report_screen_quarantine(force=True)
                while not self._stopped:
                    self.report_screen_quarantine()
                    assignment = pending.pop(0) if pending else self.transport.receive_assignment()
                    if assignment is not None:
                        self.execute_assignment(assignment)
                    elif not self._stopped:
                        self._check_idle_vm_liveness()
                        if (
                            not pending
                            and not self._stopped
                            and self.idle_source_check_interval_s is not None
                            and self.idle_source_check_interval_s > 0
                            and time.monotonic() - last_source_check_at >= self.idle_source_check_interval_s
                        ):
                            last_source_check_at = time.monotonic()
                            try:
                                assert_process_source_current()
                            except SourceProcessStale as exc:
                                self.journal.append("agent_source_stale_idle", {
                                    "rig_id": self.target.rig_id,
                                    "error": str(exc),
                                })
                                raise
            except (ExecutorContractError, StaleDeviceWrite, SourceProcessStale, AgentRestartRequired) as exc:
                log.error("rig agent stopping: %s: %s", type(exc).__name__, exc)
                self.transport.close()
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect is the transport contract
                self.journal.append("transport_disconnected", {
                    "error": f"{type(exc).__name__}: {exc}",
                })
                log.warning(
                    "transport disconnected (%s: %s), reconnecting in %.1fs",
                    type(exc).__name__,
                    exc,
                    self.reconnect_delay_s,
                )
                self.transport.close()
                if not self._stopped:
                    time.sleep(self.reconnect_delay_s)

    def stop(self) -> None:
        self._stopped = True
        self.transport.close()
