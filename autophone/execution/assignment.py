"""Standard Preflight, Provisioning, Run and Cleanup execution sequence."""
from __future__ import annotations

from pathlib import Path
from contextlib import nullcontext
from typing import Any, Callable, Mapping

from autophone.device import DeviceSession, VPhoneDeviceSession
from autophone.device.lock import ScreenLock, ScreenQuarantineMarkerPresent
from autophone.device.stage_guard import StageGuard, StageQuiesceError
from autophone.logging import get_logger
from autophone.rig.agent import AgentExecutionContext, AssignmentCancelled, DEVICE_QUIESCE_UNVERIFIED
from autophone.rig.maintenance import RigMaintenance
from autophone.rig.provisioning import RigProvisioner

from .artifacts import seal_artifacts
from .run import RunExecution
from .run_types import RunCollectionError


log = get_logger(__name__)

#: Assignment 终态枚举，与 `assignment-result.v1.json` 的 `status` 及控制面 `_TERMINAL_ATTEMPTS` 一致。
_ASSIGNMENT_TERMINAL_STATUSES = frozenset(
    {"passed", "failed", "error", "blocked", "cancelled", "indeterminate"},
)

#: Run 总体状态不在 Assignment 终态枚举内时的对照。`partial` 表示无产品失败、但有用例被
#: capability / fixture / infra 阻断（docs/regression-runtime-contract.md「总体状态与退出码」），
#: 属于前置条件缺口，Assignment 终态记 `blocked`；Run 自身的 `partial` 原样留在 `product_verdict`
#: 与 `run_result.status`。其余未列出的状态（含 `pending`）按原规则记 `error`。
_RUN_STATUS_AS_ASSIGNMENT_STATUS = {"partial": "blocked"}


#: ScreenLock 隔离标记使 Assignment 未执行时的结构化原因（BL-224），写入 Stage Result 与
#: Assignment Result 的 ``reason``。
SCREEN_QUARANTINED = "screen_quarantined"
SCREEN_QUARANTINE_REPORT_SCHEMA = "screen-quarantine-report/v1"


def screen_quarantine_state(lock: ScreenLock, devices) -> dict[str, Any]:
    """只读汇总若干设备键上的隔离标记；不取锁、不建目录、不访问设备。"""
    from autophone.util import now_iso

    keys = sorted({str(item) for item in devices if str(item or "").strip()})
    markers = []
    for key in keys:
        status = lock.quarantine_status(device=key)
        if status is None:
            continue
        marker = status.get("marker") or {}
        markers.append({
            "device": key, "path": status.get("path"), "sha256": status.get("sha256"),
            **{name: marker.get(name) for name in ("reason", "owner_id", "at", "pid") if name in marker},
            **({"error": status["error"]} if status.get("error") else {}),
        })
    return {
        "schema_version": SCREEN_QUARANTINE_REPORT_SCHEMA,
        "quarantined": bool(markers),
        "lock_root": str(lock.root),
        "devices": keys,
        "markers": markers,
        "observed_at": now_iso(),
    }


def _assignment_status_for_run(run_status: str) -> str:
    if run_status in _ASSIGNMENT_TERMINAL_STATUSES:
        return run_status
    return _RUN_STATUS_AS_ASSIGNMENT_STATUS.get(run_status, "error")


def _product_verdict(run_result: Mapping[str, Any] | None) -> str:
    """Product Verdict 只抄 Run Result 的总体状态；没有 Run Result 时为 `not_run`。

    控制面 `complete_assignment` 拒收 `product_verdict != run_result.status` 的 Assignment
    Result（`Assignment Result product verdict mismatch`）；Assignment Result 不另写第二份
    产品结论，Assignment 终态的映射只作用于 `status`。
    """
    if run_result is None:
        return "not_run"
    return str(run_result.get("status") or "")

DeviceSessionFactory = Callable[[AgentExecutionContext], DeviceSession]
ProfileLoader = Callable[[str], Any]
MaintenanceFactory = Callable[[DeviceSession, list[str]], RigMaintenance]


def _device_factory(context: AgentExecutionContext) -> DeviceSession:
    """默认设备会话：不带包仓库安装实现。

    `VPhoneDeviceSession.install_build` 需要注入 `install_package_fn`，而
    `autophone.execution` 不得反向 import `autophone.platform`（BL-003 判据 A）。
    Rig Agent 的真实执行器由 `autophone.execution.handlers.create_executor` 装配，
    它传入自己的 `device_factory` 并在那里注入包仓库实现；用本默认工厂时
    `install_build` 会 fail-closed 抛 `DeviceProtocolError`。
    """
    return VPhoneDeviceSession(context.target, context.write_fence)


def _maintenance_factory(
    device: DeviceSession,
    capability_support: list[str],
) -> RigMaintenance:
    return RigMaintenance(device, capability_support=capability_support)


class AssignmentExecution:
    """Own stage order, failure conversion and observed-state publication."""

    def __init__(
        self,
        run_execution: RunExecution,
        profile_loader: ProfileLoader,
        *,
        device_factory: DeviceSessionFactory = _device_factory,
        maintenance_factory: MaintenanceFactory = _maintenance_factory,
        closure_scope: Callable[..., Any] | None = None,
        screen_lock=None,
        reset_factory=None,
    ) -> None:
        self.screen_lock = screen_lock
        self.reset_factory = reset_factory
        self.closure_scope = closure_scope or (lambda *_: nullcontext(None))
        self.run_execution = run_execution
        self.profile_loader = profile_loader
        self.device_factory = device_factory
        self.maintenance_factory = maintenance_factory

    def _lock(self) -> ScreenLock:
        return self.screen_lock or ScreenLock()

    def screen_quarantine_report(self, target: Any) -> dict[str, Any]:
        """本 Rig 的设备键（rig_id 与 VM 目录名）上的隔离标记，供 Agent 上报控制面（BL-224）。"""
        return screen_quarantine_state(self._lock(), (target.rig_id, Path(target.vm_dir).name))

    def execute(
        self,
        assignment: Mapping[str, Any],
        context: AgentExecutionContext,
    ) -> Mapping[str, Any]:
        guard = StageGuard(
            owner_id=f"{assignment['assignment_id']}:{assignment['attempt']['attempt_no']}",
            rig_id=context.target.rig_id, generation=context.target.rig_generation,
            check_lease=context.write_fence.check,
            # 阶段切换释放已持有资源时只核对租约与 fencing：Run 内被取消后，collect 切换
            # 仍须能释放 crash-trap 等句柄，否则 quiesce 失败、设备被隔离（BL-220 第二轮）。
            # 没有该方法的 Fence 退回 `check`（更严格，不放宽）。
            check_release=getattr(context.write_fence, 'check_release', None),
            socket_path=context.target.socket, frida_endpoint=context.target.frida_endpoint,
            ssh_endpoint=(context.target.ssh_host, context.target.ssh_port),
            quarantine=lambda failures: context.write_fence.transport.record_stage(
                str(assignment['assignment_id']), int(assignment['attempt']['attempt_no']),
                int(assignment['attempt']['fencing']), 'quiesce', {
                    'status': 'failed', 'device_access_released': False,
                    'errors': list(failures),
                },
            ),
        )
        from .world_identity import assignment_scope
        binding = dict(assignment_id=str(assignment['assignment_id']),
                       attempt_no=int(assignment['attempt']['attempt_no']), fencing=int(assignment['attempt']['fencing']),
                       rig_id=context.target.rig_id, generation=context.target.rig_generation)
        with guard.scope("preflight"), assignment_scope(binding), self.closure_scope(assignment, context) as closure_dir:
            return self._execute_guarded(assignment, context, guard, closure_dir)

    def _execute_guarded(self, assignment, context, guard: StageGuard, closure_dir=None):
        from autophone.reset_contract import validate_reset_spec
        validate_reset_spec(assignment.get('reset_policy'))
        assignment_id = str(assignment["assignment_id"])
        attempt = assignment.get("attempt") or {}
        attempt_no = int(attempt["attempt_no"])
        fencing = int(attempt["fencing"])
        transport = context.write_fence.transport
        run_spec = dict(assignment.get("run_spec") or {})
        app = str(run_spec.get("app") or (assignment.get("desired_state") or {}).get("app") or "")
        profile = self.profile_loader(app)
        device = self.device_factory(context)
        required = [str(item) for item in assignment.get("required_capabilities") or []]
        maintenance = self.maintenance_factory(device, required)

        recorded: set[str] = set()
        active_stage = "preflight"
        artifact_source: Path | None = closure_dir
        run_result: dict[str, Any] | None = None
        claim: dict[str, Any] = {"markers": frozenset(), "device": str(run_spec.get("device") or "")}
        try:
            # 接单核对（BL-224）：标记存在时 Run 必然取不到 ScreenLock，复位也需要该锁。
            # 不做 Preflight 探针与 Provisioning 回读，直接以 Cleanup 回执结束，不访问设备。
            quarantine = screen_quarantine_state(self._lock(), (
                context.target.rig_id, Path(context.target.vm_dir).name, run_spec.get("device"),
            ))
            # 接单时已存在的标记不是本 Attempt 写下的（同一 run_id 重试时 owner 相同），
            # 收尾判断「本 Attempt 的 quiesce 失败」时排除它们（BL-220 剩余项）。
            claim["markers"] = frozenset(
                str(item.get("sha256")) for item in quarantine["markers"] if item.get("sha256")
            )
            if quarantine["quarantined"]:
                self._skip_remaining(
                    transport, assignment_id, attempt_no, fencing, recorded,
                    reason=SCREEN_QUARANTINED,
                )
                active_stage = 'cleanup'
                return self._finish(
                    transport=transport,
                    context=context,
                    reset_policy=assignment.get("reset_policy"), profile=profile,
                    device=device,
                    assignment_id=assignment_id,
                    attempt_no=attempt_no,
                    fencing=fencing,
                    claim=claim,
                    status="blocked",
                    run_result=None,
                    artifact_source=None,
                    rig_disposition="quarantine",
                    fixture_disposition="unknown",
                    detail=_quarantine_detail(quarantine),
                    reason=SCREEN_QUARANTINED,
                    run_id=str(run_spec.get("run_id") or ""),
                )
            context.check_control()
            snapshot = maintenance.prepare(
                profile,
                need_app=bool(run_spec),
                need_frida=any(
                    item in {"frida", "api_capture", "location_injection"}
                    for item in required
                ),
            )
            preflight = {
                "status": "passed" if snapshot.rig.vm_uds.get("available") else "blocked",
                "rig_snapshot": snapshot.to_dict(),
            }
            self._stage(transport, assignment_id, attempt_no, fencing, "preflight", preflight)
            recorded.add("preflight")
            if preflight["status"] != "passed":
                self._skip_remaining(
                    transport, assignment_id, attempt_no, fencing, recorded,
                    reason="preflight_blocked",
                )
                active_stage = 'cleanup'
                return self._finish(
                    transport=transport,
                    context=context,
                    reset_policy=assignment.get("reset_policy"), profile=profile,
                    device=device,
                    assignment_id=assignment_id,
                    attempt_no=attempt_no,
                    fencing=fencing,
                    claim=claim,
                    status="blocked",
                    run_result=None,
                    artifact_source=None,
                    rig_disposition="quarantine",
                    fixture_disposition="unknown",
                    detail=snapshot.rig.recovery_advice(),
                )

            context.check_control()
            active_stage = "provisioning"
            guard.transition("provisioning")
            provisioner = RigProvisioner(
                device,
                lambda observed, capabilities: transport.update_observed_state(
                    context.target,
                    observed,
                    capabilities,
                ),
            )
            provisioning = provisioner.ensure(
                dict(assignment.get("desired_state") or {}),
                app=app,
                bundle_id=str(profile.bundle_id),
                capability_state=snapshot.capability_state,
                observed_state=transport.read_observed_state(context.target),
            ).to_dict()
            self._stage(
                transport, assignment_id, attempt_no, fencing,
                "provisioning", provisioning,
            )
            recorded.add("provisioning")

            context.check_control()
            active_stage = "run"
            guard.transition("execute")
            if run_spec:
                from autophone.execution.candidate_lifecycle import candidate_assignment_scope
                with candidate_assignment_scope(assignment, context, device, snapshot.capability_state):
                    run_result, artifact_source = self.run_execution.execute_with_artifacts(
                        run_spec,
                        device=device,
                        rig_snapshot=snapshot,
                        run_dir=self._run_dir(run_spec),
                    )
                run_stage = {"status": "passed", "run_result": run_result}
            else:
                run_result = None
                run_stage = {"status": "passed", "detail": "no Run requested"}
            self._stage(transport, assignment_id, attempt_no, fencing, "run", run_stage)
            recorded.add("run")

            context.check_control()
            terminal = _assignment_status_for_run(str((run_result or {}).get("status") or "passed"))
            active_stage = 'cleanup'
            return self._finish(
                transport=transport,
                context=context,
                reset_policy=assignment.get("reset_policy"), profile=profile,
                device=device,
                assignment_id=assignment_id,
                attempt_no=attempt_no,
                fencing=fencing,
                claim=claim,
                status=terminal,
                run_result=run_result,
                artifact_source=artifact_source,
                rig_disposition="reusable",
                fixture_disposition="preserved",
                run_id=str(run_spec.get("run_id") or ""),
            )
        except AssignmentCancelled as exc:
            if active_stage == 'cleanup':
                # Cleanup 中设备操作不再因取消被拒（`AssignmentControl.check_device_write`）；
                # 若仍有取消从收尾里抛出，不得第二次执行复位，交给既有恢复与隔离。
                raise
            self._record_failure_stage(
                transport, assignment_id, attempt_no, fencing,
                active_stage, recorded, "cancelled", str(exc),
            )
            self._skip_remaining(
                transport, assignment_id, attempt_no, fencing, recorded,
                reason="cancel_requested",
            )
            return self._finish(
                transport=transport,
                context=context,
                reset_policy=assignment.get("reset_policy"), profile=profile,
                device=device,
                assignment_id=assignment_id,
                attempt_no=attempt_no,
                fencing=fencing,
                claim=claim,
                status="cancelled",
                run_result=run_result,
                artifact_source=artifact_source,
                rig_disposition="needs_inspection",
                fixture_disposition="unknown",
                detail=str(exc),
                run_id=str(run_spec.get("run_id") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - stage failures become one terminal result
            if active_stage == 'cleanup':
                # A failed finalization must not issue the reset actions twice.
                # Leave the Attempt/Lease for existing recovery and quarantine.
                raise
            if active_stage == "run" and isinstance(exc, ScreenQuarantineMarkerPresent):
                # Run 取 ScreenLock 时标记已存在（接单核对之后才出现）：lease 未取得，Run 没有
                # 在该锁下访问设备。按未执行收尾，不按 Run 故障处理（BL-224）。
                detail = f"{type(exc).__name__}: {exc}"
                self._stage(transport, assignment_id, attempt_no, fencing, "run", {
                    "status": "blocked", "reason": SCREEN_QUARANTINED, "detail": detail,
                })
                recorded.add("run")
                return self._finish(
                    transport=transport,
                    context=context,
                    reset_policy=assignment.get("reset_policy"), profile=profile,
                    device=device,
                    assignment_id=assignment_id,
                    attempt_no=attempt_no,
                    fencing=fencing,
                    claim=claim,
                    status="blocked",
                    run_result=None,
                    artifact_source=artifact_source,
                    rig_disposition="quarantine",
                    fixture_disposition="unknown",
                    detail=detail,
                    reason=SCREEN_QUARANTINED,
                    run_id=str(run_spec.get("run_id") or ""),
                )
            if (
                active_stage == "run"
                and isinstance(exc, RunCollectionError)
                and str(exc.run_result.get("run_id") or "") == str(run_spec.get("run_id") or "")
            ):
                run_result = dict(exc.run_result)
                artifact_source = exc.run_dir
            detail = f"{type(exc).__name__}: {exc}"
            self._record_failure_stage(
                transport, assignment_id, attempt_no, fencing,
                active_stage, recorded, "error", detail,
            )
            self._skip_remaining(
                transport, assignment_id, attempt_no, fencing, recorded,
                reason=f"{active_stage}_failed",
            )
            return self._finish(
                transport=transport,
                context=context,
                reset_policy=assignment.get("reset_policy"), profile=profile,
                device=device,
                assignment_id=assignment_id,
                attempt_no=attempt_no,
                fencing=fencing,
                claim=claim,
                status="error",
                run_result=run_result,
                artifact_source=artifact_source,
                rig_disposition="needs_inspection",
                fixture_disposition="unknown",
                detail=detail,
                run_id=str(run_spec.get("run_id") or ""),
                quiesce_failures=exc.failures if isinstance(exc, StageQuiesceError) else (),
            )

    def _finish(
        self,
        *,
        transport: Any,
        context: AgentExecutionContext,
        device: DeviceSession,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        status: str,
        run_result: Mapping[str, Any] | None,
        artifact_source: Path | None,
        rig_disposition: str,
        fixture_disposition: str,
        detail: str | None = None,
        reset_policy=None, profile=None,
        reason: str | None = None,
        run_id: str = "",
        claim: Mapping[str, Any] | None = None,
        quiesce_failures: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        # 进入 Cleanup：此后的复位、回读与资源释放属于受控收尾，取消请求不再拒绝这些设备操作；
        # `context.write_fence.check()` 仍向控制面核对 Lease、Fencing 与 Generation，失效即拒绝
        # （BL-220）。先于 transition 调用，transition 释放上一阶段资源时同样适用。
        context.assignment_control.begin_cleanup()
        guard = StageGuard.current()
        lock = self._lock()
        reset_owner = f'reset-{assignment_id}-{attempt_no}'
        own = {run_id, reset_owner, f'{assignment_id}:{attempt_no}'} - {''}
        claim = claim or {}
        devices = (context.target.rig_id, Path(context.target.vm_dir).name, claim.get("device"))
        # quiesce 未确认（BL-220 剩余项）：本 Attempt 持有的资源未能确认释放。来源依次为 StageGuard
        # 记录的失败（Run 收尾 collect 切换等）、执行器捕获的 StageQuiesceError、本 Attempt 写下的
        # ScreenLock 标记，以及下面 cleanup 切换本身的失败。此后不再访问设备、不复位。
        unverified = self._unverified_quiesce(guard, lock, devices, own, claim, quiesce_failures)
        if not unverified and guard is not None:
            try:
                guard.transition("cleanup")
            except StageQuiesceError as exc:
                unverified = exc.failures or ("stage quiesce failed",)
        from autophone.reset_contract import effective_policy
        from .reset import ResetCoordinator, HttpFixtureResetAdapter
        binding = dict(assignment_id=assignment_id, attempt_no=attempt_no, fencing=fencing,
                       host_id=context.target.host_id, rig_id=context.target.rig_id,
                       rig_generation=context.target.rig_generation)
        policy = effective_policy(reset_policy, context.target)
        adapters = {name: HttpFixtureResetAdapter(config) for name, config in
                    (getattr(profile, 'raw', {}).get('reset_adapters') or {}).items()}
        coordinator = (self.reset_factory(device) if self.reset_factory is not None else
                       ResetCoordinator(device, fixture_adapters=adapters))
        # 复位前核对复位锁设备键上的隔离标记（BL-224）。标记不是本 Attempt 写下的（上面已排除
        # 本 Attempt 的 quiesce 失败）时不取锁、不复位，回执记复位未执行。
        quarantine = screen_quarantine_state(lock, (context.target.rig_id,))
        reset_receipt = None
        if unverified:
            quarantine = None
        elif quarantine["quarantined"]:
            from .reset import reset_not_performed
            log.warning(
                "assignment %s: reset not performed, ScreenLock quarantined: %s",
                assignment_id, _quarantine_detail(quarantine),
            )
            reset_receipt = reset_not_performed(policy, binding=binding, reason=SCREEN_QUARANTINED)
            if guard is not None:
                guard.transition('collect')
            reason = reason or SCREEN_QUARANTINED
            note = _quarantine_detail(quarantine)
            if "release-quarantine" not in str(detail or ""):
                detail = f"{detail}; {note}" if detail else note
        else:
            quarantine = None
            with lock.acquire(device=context.target.rig_id, run_id=reset_owner):
                context.write_fence.check()
                reset_receipt = coordinator.reset(policy, binding=binding)
                # Freeze all resource producers before releasing the reset ScreenLock.
                if guard is not None:
                    try:
                        guard.transition('collect')
                    except StageQuiesceError as exc:
                        # 与 Run 收尾一致：释放 ScreenLock 之前写隔离标记（BL-220 剩余项）。
                        lock.quarantine(device=context.target.rig_id, owner_id=reset_owner, failures=exc.failures)
                        unverified = exc.failures or ("stage quiesce failed",)
        if unverified:
            from .reset import reset_not_performed
            self._hold_unverified_quiesce(
                transport, assignment_id, attempt_no, fencing,
                guard=guard, lock=lock, devices=devices, own=own, claim=claim,
                failures=unverified, rig_id=context.target.rig_id,
            )
            if reset_receipt is None:
                reset_receipt = reset_not_performed(policy, binding=binding, reason=DEVICE_QUIESCE_UNVERIFIED)
            reason = DEVICE_QUIESCE_UNVERIFIED
            note = _quiesce_detail(unverified, context.target.rig_id)
            detail = f"{detail}; {note}" if detail else note
        rig_disposition = reset_receipt['device_axis']['disposition']
        fixture_disposition = reset_receipt['external_fixture_axis']['disposition']
        not_run_under_quarantine = (
            quarantine is not None and run_result is None and status in {"blocked", "cancelled"}
        )
        if (rig_disposition != 'reusable' or fixture_disposition == 'failed') and not not_run_under_quarantine:
            status = 'error'
        spool_dir = (
            context.target.log_dir
            / "artifact-spool"
            / assignment_id
            / f"attempt-{attempt_no}"
        )
        refs: list[dict[str, Any]] = []
        cleanup_errors: list[str] = []
        from autophone.execution.closure_journal import ClosureConflict, ClosureJournal
        journal = ClosureJournal(artifact_source) if artifact_source is not None else None
        if journal is not None and journal.path.exists() and not journal.quarantine.exists():
            try:
                journal.record_reset(reset_receipt, expected_version=journal.read()['version'])
                journal.materialize_evidence()
            except ClosureConflict as exc:
                # 这次 reset 已经真做了；写不进 Journal 是**记账冲突**（同一 run_id 被复用
                # 时上一次的 receipt 还冻在那儿），不是设备故障。按已有的 Cleanup 失败面
                # 收成本次 Assignment 的 error + quarantine：让异常上抛会被 Agent 包成
                # ExecutorContractError，`run_forever` 据此停掉整个 rig agent 进程，一次
                # run_id 复用就把这台 rig 整个隔离掉。
                cleanup_errors.append(f'Closure reset receipt: {type(exc).__name__}: {exc}')
        closure = journal.projection() if journal is not None else None
        if closure and closure['quarantined']:
            cleanup_errors.append('Closure inputs quarantined')
        if unverified:
            cleanup_errors.append(f"{DEVICE_QUIESCE_UNVERIFIED}: " + "; ".join(unverified))
        sealed = None
        try:
            if artifact_source is not None and (artifact_source / 'artifact-manifest.json').exists():
                from autophone.observation.artifacts import append_artifacts, default_phase2_artifacts
                # Reset is recorded after the Run result; register its final bytes
                # before copying the manifest and receipts into the immutable spool.
                append_artifacts(artifact_source, [
                    artifact for artifact in default_phase2_artifacts(artifact_source)
                    if artifact.path in {'closure-intent.json', 'reset-receipt.json',
                                         'closure-intent.quarantine.json'}
                ])
            sealed = seal_artifacts(
                artifact_source,
                spool_dir,
                owner_id=f"{assignment_id}:{attempt_no}",
            )
            refs = [dict(item) for item in sealed.refs]
        except Exception as exc:  # noqa: BLE001 - the receipt preserves the failure
            cleanup_errors.append(f"Artifact sealing: {type(exc).__name__}: {exc}")

        close_receipt: Mapping[str, Any] | None = None
        try:
            close = getattr(device, "close", None)
            close_receipt = close() if callable(close) else None
            if not isinstance(close_receipt, Mapping) or close_receipt.get('status') != 'closed':
                cleanup_errors.append('Device close: closed state not confirmed')
        except Exception as exc:  # noqa: BLE001 - the receipt preserves the failure
            cleanup_errors.append(f"Device close: {type(exc).__name__}: {exc}")

        cleanup: dict[str, Any] = {
            "status": "passed" if not cleanup_errors else "error",
            "artifacts_sealed": sealed is not None,
            "artifact_count": len(refs),
            # quiesce 未确认时不声称已释放：本 Attempt 的设备会话已关闭，但持有的资源未确认释放。
            "device_access_released": (
                not unverified
                and isinstance(close_receipt, Mapping) and close_receipt.get("status") == "closed"
            ),
        }
        if quarantine is not None:
            cleanup["reset"] = "not_performed"
            cleanup["screen_quarantine"] = quarantine
        if unverified:
            cleanup["quiesce"] = {"status": "failed", "errors": list(unverified)}
            if reset_receipt["device_axis"]["action_result"].get("reason") == DEVICE_QUIESCE_UNVERIFIED:
                cleanup["reset"] = "not_performed"
        if sealed is not None:
            cleanup["spool_manifest"] = sealed.manifest.as_uri()
        if close_receipt is not None:
            cleanup["device_session"] = dict(close_receipt)
        if cleanup_errors:
            cleanup["detail"] = "; ".join(cleanup_errors)
            status = "error"
            rig_disposition = "quarantine"
        self._stage(
            transport, assignment_id, attempt_no, fencing,
            "cleanup", cleanup,
        )
        result = {
            "schema_version": "assignment-result/v1",
            "assignment_id": assignment_id,
            "attempt_no": attempt_no,
            "fencing": fencing,
            "status": status,
            "product_verdict": _product_verdict(run_result),
            "run_result": dict(run_result) if run_result is not None else None,
            "cleanup_receipt": cleanup,
            "rig_disposition": rig_disposition,
            "fixture_disposition": fixture_disposition,
            "artifact_refs": refs,
            "closure": closure,
            "reset_receipt": reset_receipt,
        }
        if detail:
            result["detail"] = detail
        if reason:
            result["reason"] = reason
        from autophone.contracts import validate_or_raise
        validate_or_raise('assignment-result.v1.json', result)
        return result

    @staticmethod
    def _unverified_quiesce(guard, lock, devices, own, claim, failures) -> tuple[str, ...]:
        if guard is not None and guard.quiesce_failures:
            return guard.quiesce_failures
        if failures:
            return tuple(failures)
        preexisting = claim.get("markers") or frozenset()
        mine = [
            marker for marker in screen_quarantine_state(lock, devices)["markers"]
            if marker.get("owner_id") in own and marker.get("sha256") not in preexisting
        ]
        return tuple(
            f"ScreenLock marker written by {marker.get('owner_id')} on {marker.get('device')}"
            for marker in mine
        )

    def _hold_unverified_quiesce(
        self, transport, assignment_id, attempt_no, fencing, *,
        guard, lock, devices, own, claim, failures, rig_id,
    ) -> None:
        """quiesce 未确认后的收尾前置：停止设备访问、补记 quiesce 阶段、保证 ScreenLock 标记存在。"""
        recorded_by_guard = guard is not None and bool(guard.quiesce_failures)
        if guard is not None and not recorded_by_guard:
            # 失败不是 StageGuard 自己发现的：切到只允许宿主操作的 collect，释放其登记的资源。
            # 再失败时由 StageGuard 回调记录 quiesce 阶段。
            try:
                guard.transition("collect")
            except StageQuiesceError:
                recorded_by_guard = bool(guard.quiesce_failures)
        if not recorded_by_guard:
            # 控制面据 quiesce 阶段结果把 rig 置为 quarantine，并只对它接受释放未确认的完成回执。
            self._stage(transport, assignment_id, attempt_no, fencing, "quiesce", {
                "status": "failed", "device_access_released": False, "errors": list(failures),
            })
        preexisting = claim.get("markers") or frozenset()
        present = [
            marker for marker in screen_quarantine_state(lock, devices)["markers"]
            if marker.get("sha256") not in preexisting
        ]
        if not present:
            lock.quarantine(device=rig_id, owner_id=f"{assignment_id}:{attempt_no}", failures=tuple(failures))
        log.error(
            "assignment %s: %s; reset not performed, Rig disposition quarantine: %s",
            assignment_id, DEVICE_QUIESCE_UNVERIFIED, "; ".join(failures),
        )

    @classmethod
    def _skip_remaining(
        cls,
        transport: Any,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        recorded: set[str],
        *,
        reason: str,
    ) -> None:
        for stage in ("preflight", "provisioning", "run"):
            if stage in recorded:
                continue
            cls._stage(
                transport, assignment_id, attempt_no, fencing,
                stage, {"status": "skipped", "reason": reason},
            )
            recorded.add(stage)

    @classmethod
    def _record_failure_stage(
        cls,
        transport: Any,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        stage: str,
        recorded: set[str],
        status: str,
        detail: str,
    ) -> None:
        if stage in recorded:
            return
        cls._stage(
            transport, assignment_id, attempt_no, fencing,
            stage, {"status": status, "detail": detail},
        )
        recorded.add(stage)

    @staticmethod
    def _stage(
        transport: Any,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        stage: str,
        result: Mapping[str, Any],
    ) -> None:
        # 唯一的 Stage Result 出口，也是「这条 assignment 走到哪一步」的唯一日志出口。
        log.info(
            "stage %s: status=%s assignment=%s attempt=%s%s%s",
            stage,
            result.get("status"),
            assignment_id,
            attempt_no,
            f" reason={result['reason']}" if result.get("reason") else "",
            f" detail={result['detail']}" if result.get("detail") else "",
        )
        transport.record_stage(
            assignment_id,
            attempt_no,
            fencing,
            stage,
            dict(result),
        )

    @staticmethod
    def _run_dir(run_spec: Mapping[str, Any]) -> Path | None:
        value = run_spec.get("run_dir")
        return Path(str(value)) if value else None


def _quiesce_detail(failures, device: str) -> str:
    """quiesce 未确认时 Assignment Result ``detail`` 的说明，带处置步骤。"""
    return (
        f"{DEVICE_QUIESCE_UNVERIFIED}: owned device resources were not confirmed released "
        f"({'; '.join(failures)}); the Rig is quarantined and the Rig Agent stops after this result. "
        "Restart it with scripts/start_vphone.sh, then release the ScreenLock marker with "
        f"`autophone device screenlock release-quarantine --device {device} --actor <who> "
        "--reason <text> --confirm-quiesced`"
    )


def _quarantine_detail(state: Mapping[str, Any]) -> str:
    """Assignment Result ``detail`` 与日志用的一行说明，带解除命令。"""
    devices = [str(item.get("device")) for item in state.get("markers") or []]
    device = devices[0] if devices else "<device>"
    return (
        f"{SCREEN_QUARANTINED}: ScreenLock quarantine marker present for {', '.join(devices) or 'unknown'}; "
        f"release with `autophone device screenlock release-quarantine --device {device} "
        "--actor <who> --reason <text> --confirm-quiesced`"
    )
