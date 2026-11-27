from __future__ import annotations

import copy
import hashlib
import json
import logging
import multiprocessing
import os
import queue
import secrets
import threading
import time
import traceback
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from autophone.device.runtime_probe import RuntimeAdmissionError
from autophone.paths import STATE_ROOT
from autophone.execution.release_lock import (
    ReleaseLockBusy,
    inherited_exclusive_release_lock,
    release_lock,
)
from autophone.version import inherit_run_identity, stamp_run_spec
from autophone.application import run_index
from autophone.application.run_queries import RunQueries
from autophone.rig.maintenance import RigMaintenance
from autophone.execution.run_types import RunCollectionError
from autophone.execution.run_types import RunHandle, Executor
from autophone.observation.run_result import plane_mode
from autophone.execution.run_execution import (
    RunExecution, _default_capture_session_factory, _default_recorder_factory,
    _default_crash_trap_factory,
)
from autophone.assets.promotion_service import PromotionService
from autophone.execution.promotion import promotion_execution
from autophone.record.replay_batch_service import ReplayBatchService
from autophone.assets import store_catalog as store_catalog_module
from autophone.assets.apps import AppProfile, load_app_profile
from autophone.observation.artifacts import (
    append_artifacts,
    default_phase2_artifacts,
    write_artifact_manifest,
)
from autophone.contracts import ContractError, validate_or_raise, write_json
from autophone.device.lock import ScreenLock
from autophone.assets.golden_paths import validate_golden_path_request
from autophone.assets.negative_control import validate_negative_control_request
from autophone.logging import (
    background_log_queue,
    bind_run,
    configure_worker_logging,
    flush_background_logs,
    level_from_env,
    redact_text,
    setup_logging,
)
from autophone.execution.manual import abort_manual_checkpoint, load_manual_state, resume_manual_checkpoint
from autophone.execution.run_abort import RunNotAbortable, request_run_abort
from autophone.device.target import default_device

DEFAULT_DEVICE = default_device()
from autophone.util import now_iso as _now

logger = logging.getLogger(__name__)


# 后台 worker 取到共享 release lease 后回给父进程的确认字。父进程等到它才返回
# 早期 handle，从而不留「父锁已放、子锁未取」的窗口。
_WORKER_LEASE_OK = "lease_acquired"
# spawn 需要重新起解释器并 import 整个 service 模块，冷启动可能要数秒。
_WORKER_LEASE_TIMEOUT_S = float(os.environ.get("AUTOPHONE_WORKER_LEASE_TIMEOUT_S") or 120)
# verification/checkpoint status → 平台 RunResult 的 status / plane verdict。
# 两张表覆盖 run 级与 case 级映射，未知值一律回退 pending。

# operation run status → 平台 RunResult 的 status / ui verdict（pending_execution 即执行器未接）。


def mint_run_id(app: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{app}-{secrets.token_hex(2)}"


def _spec_case_count(spec: dict[str, Any]) -> int | None:
    """这条 run 要跑几条用例——**只在能确定时**给数，否则 None（看板据此说「耗时未知」）。

    非 regression 的 run（operation / verification）恒为 1 条执行体。regression 只有
    在 selector 已把 ids 钉死时才知道条数（`execution_plan` 在 submit 阶段已物化成
    pinned selector）；tag / module / suite 这类要读 YAML 才能解析的选择器一律不猜——
    宁可不给估算，也不给一个偏大的数字。
    """
    if str(spec.get("type") or "") != "regression":
        return 1
    ids = ((spec.get("case_selector") or {}).get("ids")) or []
    return len(ids) or None


class RunService:
    def __init__(
        self,
        *,
        run_root: Path | None = None,
        negctl_root: Path | None = None,
        lock: ScreenLock | None = None,
        executor: Executor | None = None,
        capture_session_factory: Callable[[AppProfile, dict[str, Any], Path], Any] | None = None,
        recorder_factory: Callable[[Path, dict[str, Any]], Any] | None = None,
        crash_trap_factory: Callable[[AppProfile, dict[str, Any], Path], Any] | None = None,
        store_catalog: store_catalog_module.StoreCatalog | None = None,
        location_quiesce_fn: Callable[[str, float], bool] | None = None,
        telemetry_sleep: Callable[[float], None] | None = None,
        rig_maintenance: RigMaintenance | None = None,
        app_hygiene_factory: Callable[[AppProfile, dict[str, Any], Path], Any] | None = None,
    ) -> None:
        self.run_root = run_root or (STATE_ROOT / "runs" / "platform")
        # 区分力证据目录（negctl 产物），是 run_root 的兄弟 `runs/negctl`——与 ExceptionView /
        # ai_draft 同一真相源。可注入做 hermetic 测试。
        self.negctl_root = negctl_root or (self.run_root.parent / "negctl")
        # 批量回放记录目录（record-replay-batch），是 run_root 的兄弟 `runs/batches`——
        # 照 negctl_root 派生（不硬写全局 ROOT，单测临时 run_root 不分裂）。
        self.batches_root = self.run_root.parent / "batches"
        self.lock = lock or ScreenLock()
        # 注入过任一 seam 就不能走 spawn 子进程：子进程按路径重建 RunService，注入物拿不过去。
        self._background_process_safe = (
            executor is None
            and capture_session_factory is None
            and recorder_factory is None
            and crash_trap_factory is None
            and telemetry_sleep is None
            and rig_maintenance is None
            and app_hygiene_factory is None
        )
        self.rig_maintenance = rig_maintenance or RigMaintenance(
            None, capability_support=[],
            run_launch_app=lambda profile, shot_dir: self.execution._launch_app(profile, shot_dir),
        )

        # API 抓取会话工厂（seam）：默认 capture.build_session（fail-open）；测试注入 fake 免 frida。
        self.capture_session_factory = capture_session_factory or _default_capture_session_factory
        # 录屏器工厂（seam）：默认 recording.build_recorder（开关关闭 → None）；测试注入合成帧免 VM/ffmpeg。
        self.recorder_factory = recorder_factory or _default_recorder_factory
        # 崩溃兜底工厂（seam）：默认 CrashTrapSession（开关开且 frida 在场才挂，否则 None）；测试注入 fake 免 frida。
        self.crash_trap_factory = crash_trap_factory or _default_crash_trap_factory
        self.store_catalog = store_catalog
        self.location_quiesce_fn = location_quiesce_fn
        # 遥测轮询等待（seam）：默认 time.sleep，即生产语义不变——dataTrace 是异步上报链，
        # 每轮 poll 前要真等 telemetry 声明的可见性窗口（operation run 是 10s）。
        # 单测注入空实现即可跳过这段真等待；延迟值仍按原样记进 attempts[].delay_s。
        self._telemetry_sleep = telemetry_sleep or time.sleep
        self._pending: list[dict[str, Any]] = []
        self._pending_lock = threading.Lock()
        self._active: dict[str, dict[str, Any]] = {}
        self._active_lock = threading.Lock()
        # detail/history 文件回退等路径仍复用这个小缓存；目录发现与筛选由 RunCatalog 收口。
        self.execution = RunExecution(
            lock=self.lock, rig_maintenance=self.rig_maintenance, executor=executor,
            run_dir=self._run_dir,
            persist_result=lambda run_dir, result: self.persist_run_result(run_dir, result),
            set_active=self._set_active, update_active=self._update_active,
            clear_active=self._clear_active,
            assert_store_context=self._assert_operation_store_context,
            bind_store_context=self._bind_operation_store_context,
            capture_session_factory=self.capture_session_factory,
            recorder_factory=self.recorder_factory, crash_trap_factory=self.crash_trap_factory,
            telemetry_sleep=self._telemetry_sleep, location_quiesce_fn=self.location_quiesce_fn,
            app_hygiene_factory=app_hygiene_factory,
        )
        self.executor = self.execution.executor
        self.queries = RunQueries(
            self.run_root, active_runs=self._active_snapshot,
            persist_result=lambda run_dir, result: self.persist_run_result(run_dir, result),
        )
        self.run_catalog = self.queries.run_catalog
        self.promotions = PromotionService(
            negctl_root=self.negctl_root,
            promotion_runs=lambda **kwargs: self.run_catalog.promotion_runs(run_index.PromotionSubject(**kwargs)),
            find_run_dir=self.queries.find_run_dir, detail=self.queries.detail,
            verification_manifest=lambda *args: self.execution._write_verification_manifest(*args),
            operation_manifest=lambda *args: self.execution._write_operation_manifest(*args),
            execution=promotion_execution(),
        )
        from autophone.execution.dispatch import RunDispatcher
        from autophone.application.device_session import read_human_holder_for_device

        self.replay_batch_service = ReplayBatchService(
            self.batches_root, submit=lambda *args, **kwargs: self.submit_routed(*args, **kwargs),
            wait_assignment=lambda assignment, spec, **kwargs: RunDispatcher(self).wait(assignment, spec, **kwargs),
            find_run_dir=lambda run_id: self.find_run_dir(run_id),
            is_active=self._is_active,
            human_holder=lambda **kwargs: read_human_holder_for_device(
                local_human_holder=self.lock.human_holder, **kwargs,
            ),
        )

    def _active_snapshot(self) -> dict[str, Any]:
        with self._active_lock:
            return {key: dict(value) for key, value in self._active.items()}

    def submit_routed(self, spec, **kwargs):
        """提交方入口；Rig executor 继续使用 submit 执行已分配任务。"""
        from autophone.execution.dispatch import RunDispatcher
        return RunDispatcher(self).submit(spec, pin_device=True, **kwargs)

    def submit(
        self,
        spec: dict[str, Any],
        *,
        background: bool = False,
        telemetry_identity: dict[str, Any] | None = None,
    ) -> RunHandle:
        """提交一条 run。

        ``background=False``（默认）：**同步**执行，返回时已是终态——CLI/测试沿用此契约。
        ``background=True``：仅做快速建档（建目录、写 run-spec、登记 active=queued），把实际
        执行（拿 ScreenLock → executor → 收口）交给后台 worker，**立即返回 status=queued 的
        早期 handle**。默认 worker 是独立进程；注入测试 seam 时退回守护线程。控制面 ``POST /runs`` 走这条路：HTTP 不再阻塞到 run 跑完，前端拿到
        run_id 就能立刻切到实时屏幕流（detail()/history() 据 active 注册表实时反映在跑状态）。
        """
        spec = stamp_run_spec(copy.deepcopy(spec))
        validate_or_raise("run-spec.v1.json", spec)
        if spec["type"] not in {"regression", "verification", "operation"}:
            raise ContractError("RunService currently supports regression, verification and operation runs")

        profile = load_app_profile(str(spec["app"]))
        spec = self._materialize_execution_plan(spec, profile)
        spec = self._materialize_store_context(spec, profile)
        validate_or_raise("run-spec.v1.json", spec)
        if spec["type"] == "operation" and spec.get("store_context") is not None:
            self._assert_operation_store_context(spec)
        if spec["type"] == "regression":
            self.execution._reject_uncaptured_api_required(spec, profile)
            self.execution._reject_unpinned_draft_selector(spec)
        # 区分力反向 Run 只能是点名唯一 Case 的 regression Run（其余类型同样拒绝）。
        validate_negative_control_request(spec)
        # 黄金路径 Run：路径已声明、Case 属于该路径、声明摘要一致、selector 为计划给出的形态。
        validate_golden_path_request(spec, profile)
        from autophone.knowledge.trial_confirmation import validate_trial_confirmation

        validate_trial_confirmation(spec, profile)
        normalized_telemetry_identity: dict[str, str] | None = None
        if telemetry_identity is not None:
            if spec["type"] != "operation":
                raise ContractError("telemetry_identity is only available for operation replay")
            if not isinstance(telemetry_identity, dict) or set(telemetry_identity) != {"field", "value"}:
                raise ContractError("telemetry_identity must contain exactly field and value")
            identity_field = str(telemetry_identity.get("field") or "").strip()
            identity_value = telemetry_identity.get("value")
            if identity_field not in {"phone_no", "device_id"}:
                raise ContractError("telemetry_identity field must be phone_no or device_id")
            if not isinstance(identity_value, str) or not identity_value.strip():
                raise ContractError("telemetry_identity value is required")
            normalized_telemetry_identity = {
                "field": identity_field,
                "value": identity_value.strip(),
            }
        run_id = str(spec["run_id"])
        device = str(spec["device"])
        run_dir = self._run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        write_json(run_dir / "run-spec.json", spec)
        # Freeze platform-generated Registry state before queue registration or
        # background dispatch. Run Spec remains caller-owned input.
        from autophone.execution import run_submission

        run_submission.initialize(run_dir, spec, profile)
        runtime_spec = spec
        if normalized_telemetry_identity is not None:
            # Raw identity is deliberately added only after run-spec persistence and
            # schema validation. It travels to the worker in memory, then telemetry
            # artifacts retain only their existing hashed identity summary.
            runtime_spec = copy.deepcopy(spec)
            runtime_spec["_runtime_telemetry_identity"] = normalized_telemetry_identity
        # 队列登记必须发生在**主进程**、且在按 type 分派之前：
        # ① 后台 run 默认在 spawn 出来的子进程里执行（`_background_process_safe`），登记在
        #    子进程里做的话，API 主进程 status() 读到的永远是空队列——看板的队列深度、等待
        #    估算、发起人会全部落空；
        # ② 只登记真正竞争 ScreenLock 的 run。API-only operation / 未接 agent 的 verification
        #    不会阻塞设备，不能冒充「等待执行」。
        if self.execution._requires_screen_lock(spec, profile):
            self._register_pending(spec, run_id=run_id, device=device, run_dir=run_dir)
        if background:
            # 后台路径的出队由 worker 收尾时做（线程 finally / 进程 watcher）。
            return self._submit_background(runtime_spec, profile, run_id, run_dir, device)
        try:
            return self._dispatch(runtime_spec, profile, run_id, run_dir, device)
        except RuntimeAdmissionError as exc:
            self._write_error_result(spec, run_dir, run_id, device, error=exc)
            result = json.loads((run_dir / 'platform-run-result.json').read_text())
            return RunHandle(run_id=run_id, run_dir=run_dir, result=result,
                             manifest_path=run_dir / 'artifact-manifest.json')
        finally:
            self._clear_pending(run_id)

    @staticmethod
    def _materialize_execution_plan(spec: dict[str, Any], profile: AppProfile) -> dict[str, Any]:
        """Expand a named batch plan into an auditable, pinned selector."""
        plan_name = spec.get("execution_plan")
        if not plan_name:
            return spec
        from autophone.execution.planning import has_execution_planner

        if plan_name != "current":
            raise ContractError(f"unknown execution_plan={plan_name!r}")
        if not has_execution_planner(profile.app):
            raise ContractError(f"app={profile.app} does not register an execution planner")
        if spec.get("case_selector"):
            raise ContractError("execution_plan cannot be combined with case_selector")
        from autophone.execution.engine.cases_loader import load_cases
        from autophone.execution.planning import execution_planner

        plan = execution_planner(profile.app).build_current_plan(load_cases(profile.cases_path()))
        expanded = copy.deepcopy(spec)
        expanded["case_selector"] = plan.selector
        return expanded

    def _materialize_store_context(
        self, spec: dict[str, Any], profile: AppProfile
    ) -> dict[str, Any]:
        """Replace a browser store choice with catalog-owned run provenance."""

        selection = spec.get("store_context")
        if selection is None:
            return spec
        try:
            snapshot = store_catalog_module.materialize_run_context(
                profile,
                selection,
                catalog=self.store_catalog,
            )
        except store_catalog_module.StoreCatalogError as exc:
            raise ContractError(f"store_context: {exc}") from exc
        materialized = copy.deepcopy(spec)
        materialized["store_context"] = snapshot
        return materialized

    @staticmethod
    def _assert_operation_store_context(spec: dict[str, Any]) -> None:
        """Reject only an explicit operation override that diverges from the header."""

        context = spec.get("store_context")
        if not isinstance(context, dict):
            return
        selected = str(context.get("store_code") or "")
        args = ((spec.get("operation") or {}).get("with") or {})
        for name in ("store_code", "location_store_code"):
            if name not in args:
                continue
            actual = str(args.get(name) or "")
            if actual != selected:
                raise ContractError(
                    f"operation {name}={actual!r} conflicts with selected store {selected!r}; "
                    "switch the header store and submit again"
                )

    @staticmethod
    def _bind_operation_store_context(
        operation: dict[str, Any], spec: dict[str, Any]
    ) -> None:
        """Replace operation defaults with the selected store when the param exists."""

        context = spec.get("store_context")
        if not isinstance(context, dict):
            return
        selected = str(context.get("store_code") or "")
        args = operation.get("args") or {}
        for name in ("store_code", "location_store_code"):
            if name in args:
                args[name] = selected

    def rerun(
        self,
        source_run_id: str,
        *,
        new_run_id: str | None = None,
        requested_by: str = "web",
        background: bool = False,
    ) -> RunHandle:
        source_run_dir = self.find_run_dir(source_run_id)
        spec_path = source_run_dir / "run-spec.json"
        if not spec_path.exists():
            raise FileNotFoundError(f"run spec not found: {source_run_id}")
        source_spec = json.loads(spec_path.read_text(encoding="utf-8"))
        app = str(source_spec.get("app") or "run")
        if new_run_id is None:
            for _ in range(20):
                candidate = mint_run_id(app)
                if not self._run_dir(candidate).exists():
                    new_run_id = candidate
                    break
            if new_run_id is None:
                raise ContractError("failed to mint unique rerun id")
        if str(new_run_id) == str(source_spec.get("run_id") or source_run_id):
            raise ContractError("rerun requires a new run_id")

        new_spec = json.loads(json.dumps(source_spec))
        new_spec["run_id"] = str(new_run_id)
        try:
            source_rel = source_run_dir.relative_to(self.run_root).as_posix()
        except ValueError:
            source_rel = source_run_dir.as_posix()
        origin = dict(new_spec.get("origin") or {})
        origin["rerun_of_run_id"] = str(source_spec.get("run_id") or source_run_id)
        origin["rerun_of_run_dir"] = source_rel
        new_spec["origin"] = origin
        if requested_by:
            new_spec["requested_by"] = str(requested_by)
        new_spec["requested_at"] = _now()
        validate_or_raise("run-spec.v1.json", new_spec)
        return self.submit_routed(new_spec, background=background)

    def _dispatch(
        self,
        spec: dict[str, Any],
        profile: AppProfile,
        run_id: str,
        run_dir: Path,
        device: str,
    ) -> RunHandle:
        """按 type 同步执行到终态（建档已在 submit 完成）。"""
        return self.execution.execute(spec, profile, run_id, run_dir, device)

    def _submit_background(
        self,
        spec: dict[str, Any],
        profile: AppProfile,
        run_id: str,
        run_dir: Path,
        device: str,
    ) -> RunHandle:
        """登记 active=queued，把执行甩进后台，立即返回早期 handle。

        默认依赖的真机执行走独立进程，使 phase2 runner 在进程主线程内运行，从而可安全使用
        SIGALRM 中断卡住的 body。注入 executor/factory 的测试 seam 保留线程路径，避免丢失
        不可序列化的本地替身。
        """
        queued_at = _now()
        self._set_active(run_id, {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "app": str(spec["app"]),
            "device": device,
            "type": str(spec["type"]),
            "status": "queued",
            "queued_at": queued_at,
        })

        def _thread_worker() -> None:
            try:
                with bind_run(run_id, run_dir=run_dir, stage="dispatch"):
                    try:
                        self._dispatch(spec, profile, run_id, run_dir, device)
                    except Exception as exc:
                        logger.exception("后台 run 执行失败 %s", run_id)
                        self._write_error_result(spec, run_dir, run_id, device, error=exc)
            finally:
                self._clear_active(run_id)
                self._clear_pending(run_id)

        if self._background_process_safe:
            try:
                process = self._start_background_process(spec, run_id, run_dir, device)
            except Exception:
                self._clear_active(run_id)
                self._clear_pending(run_id)
                raise
            self._update_active(run_id, status="running")

            def _process_watcher() -> None:
                process.join()
                # Worker records are relayed asynchronously. Drain them before
                # materializing or refreshing evidence so logs are never absent
                # from the manifest merely because the run ended quickly.
                flush_background_logs()
                if not (run_dir / "platform-run-result.json").exists():
                    logger.error("后台 run 进程未生成结果 %s exitcode=%s", run_id, process.exitcode)
                    self._write_error_result(
                        spec, run_dir, run_id, device,
                        error=RuntimeError(f"background worker exited with code {process.exitcode}"),
                    )
                manifest_path = run_dir / "artifact-manifest.json"
                if manifest_path.exists():
                    log_artifacts = [
                        artifact for artifact in default_phase2_artifacts(run_dir)
                        if artifact.kind == "log"
                    ]
                    if log_artifacts:
                        append_artifacts(run_dir, log_artifacts)
                self._clear_active(run_id)
                # 子进程里的出队改不到主进程的队列，收尾必须在这里清。
                self._clear_pending(run_id)

            threading.Thread(target=_process_watcher, name=f"watch-{run_id}", daemon=True).start()
        else:
            threading.Thread(target=_thread_worker, name=f"run-{run_id}", daemon=True).start()
        early = {
            "schema_version": "v1",
            "run_id": run_id,
            "type": str(spec["type"]),
            "app": str(spec["app"]),
            "device": device,
            "status": "queued",
        }
        return RunHandle(
            run_id=run_id,
            run_dir=run_dir,
            result=early,
            manifest_path=run_dir / "artifact-manifest.json",
        )

    def _start_background_process(
        self, spec: dict[str, Any], run_id: str, run_dir: Path, device: str,
    ) -> multiprocessing.Process:
        context = multiprocessing.get_context("spawn")
        lease_channel = context.Queue()
        process = context.Process(
            target=_run_background_dispatch,
            args=(
                dict(spec),
                str(self.run_root),
                str(self.negctl_root),
                str(self.lock.root),
                run_id,
                str(run_dir),
                device,
                background_log_queue(),
                lease_channel,
            ),
        )
        process.start()
        try:
            self._await_worker_lease(process, lease_channel, run_id)
        finally:
            lease_channel.close()
        return process

    def _await_worker_lease(
        self,
        process: Any,
        lease_channel: Any,
        run_id: str,
    ) -> None:
        """Block until the spawned worker owns its shared release lease.

        The caller still holds its own shared lease here, so waiting closes the
        window in which neither process holds one.  A worker that cannot take the
        lease (an exclusive release update is in flight) turns into a submit
        error rather than a queued run with no lease.
        """
        deadline = time.monotonic() + _WORKER_LEASE_TIMEOUT_S
        status: str | None = None
        while status is None:
            try:
                status = str(lease_channel.get(timeout=0.2))
            except queue.Empty:
                if not process.is_alive():
                    status = (
                        "background worker exited before acquiring the release "
                        f"lease (exitcode={process.exitcode})"
                    )
                elif time.monotonic() >= deadline:
                    status = "timed out waiting for the background worker release lease"
        if status == _WORKER_LEASE_OK:
            return
        if process.is_alive():
            process.terminate()
        process.join(timeout=10)
        raise ReleaseLockBusy(f"run {run_id} was not started: {status}")

    def _write_error_result(
        self,
        spec: dict[str, Any],
        run_dir: Path,
        run_id: str,
        device: str,
        *,
        error: BaseException | None = None,
    ) -> None:
        """后台线程崩溃兜底：落一份最小终态 failed 结果，让前端轮询能到终态（不静默吊死）。

        已有 platform-run-result.json（执行链自己写过）则不覆盖。
        """
        from autophone.execution.closure_journal import ClosureConflict, ClosureQuarantined
        if isinstance(error, (ClosureConflict, ClosureQuarantined)):
            logger.error("closure admission rejected for %s: %s", run_id, error)
            return
        if error is not None:
            try:
                write_json(run_dir / "run-failure.json", {
                    "schema_version": "v1",
                    "run_id": run_id,
                    "stage": "collect" if isinstance(error, RunCollectionError) else "dispatch",
                    "occurred_at": _now(),
                    "exception_type": type(error).__name__,
                    "message": redact_text(error),
                    "traceback": redact_text("".join(traceback.format_exception(error))),
                })
            except Exception:
                logger.exception("写 run failure 证据失败 %s", run_id)
        # An exception before normal collection used to leave no manifest at all.
        # Keep the evidence discoverable even for this minimal fallback result.
        try:
            if not (run_dir / "artifact-manifest.json").exists():
                write_artifact_manifest(run_dir, run_id, default_phase2_artifacts(run_dir))
        except Exception:
            logger.exception("写失败 run 的产物清单失败 %s", run_id)
        result_path = run_dir / "platform-run-result.json"
        from autophone.execution.closure_journal import ClosureJournal
        journal = ClosureJournal(run_dir)
        if journal.path.exists() or journal.quarantine.exists():
            try:
                row = journal.read()
                if row['terminal_candidate'] is not None:
                    self.resume_result_publication(run_dir)
                    return
                frozen = row['frozen_inputs'] or {}
                if frozen.get('kind') == 'run-service-outcome/v1':
                    result = copy.deepcopy(frozen['execution_outcome'])
                    result.setdefault('lock_phase', {})['collect'] = {'status': 'failed'}
                    self.persist_run_result(run_dir, result)
                    write_artifact_manifest(run_dir, run_id, default_phase2_artifacts(run_dir))
                    return
            except Exception:
                logger.exception("closure recovery failed for %s; preserving original evidence", run_id)
                return
        if result_path.exists():
            return
        try:
            result = {
                "schema_version": "v1",
                "run_id": run_id,
                "type": str(spec["type"]),
                "app": str(spec["app"]),
                "device": device,
                "package": self.execution._package_snapshot(run_dir, spec),
                "status": "failed",
                "planes": {
                    "ui": {"mode": "advisory", "verdict": "fail", "detail": "run 执行异常终止（详见 run.log / run-failure.json）"},
                    "function": {"mode": "disabled", "verdict": "not_configured"},
                    "api": {"mode": "disabled", "verdict": "not_configured"},
                    "telemetry": {"mode": "disabled", "verdict": "not_configured"},
                },
                "timestamps": {"collect_ended_at": _now()},
            }
            if isinstance(error, RuntimeAdmissionError):
                result['status'] = 'blocked'
                result['planes']['ui'].update(verdict='skipped', detail=str(error))
                result['blocker'] = {'scope': 'capability', 'code': 'runtime_admission_blocked',
                    'capability': ','.join(error.blockers), 'evidence_ref': 'world-identity.json',
                    'recovery': 'restore required identity/capability evidence before a new Run'}
            if journal.path.exists():
                result['lock_phase'] = {'collect': {'status': 'skipped'}}
                row = journal.read()
                journal.freeze({"kind": "run-service-interrupted/v1",
                                "execution_outcome": copy.deepcopy(result),
                                "evidence_complete": False}, expected_version=row['version'])
            self.persist_run_result(run_dir, result)
            write_artifact_manifest(run_dir, run_id, default_phase2_artifacts(run_dir))
        except Exception:
            logger.exception("写兜底 error 结果失败 %s", run_id)

    def persist_run_result(self, run_dir: Path, result: dict[str, Any]) -> Path:
        from autophone.execution.closure_journal import ClosureJournal
        journal = ClosureJournal(run_dir)
        if journal.path.exists() or journal.quarantine.exists():
            journal.materialize_evidence()
            identity = journal.read().get('world_identity')
            if identity is not None:
                result['world_identity'] = copy.deepcopy(identity)
        result = copy.deepcopy(result)
        # RunSpec is the frozen provenance source.  Do not infer a current
        # version while refreshing a legacy result: that would rewrite history.
        try:
            frozen_spec = json.loads((run_dir / "run-spec.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            frozen_spec = {}
        result = inherit_run_identity(result, frozen_spec)
        if frozen_spec.get("candidate_ref") is not None:
            activation_path = Path(run_dir) / "candidate-activation.json"
            if activation_path.is_file():
                activation = json.loads(activation_path.read_text())
                if activation.get("registry_candidate_ref") != frozen_spec["candidate_ref"]:
                    raise ContractError("candidate activation result differs from frozen Run Spec")
                result["candidate_activation"] = activation
            from autophone.execution.candidate_lifecycle import validate_result
            validate_result(run_dir, frozen_spec, result.get("candidate_activation"), result)
        package = result.get("package") or {}
        device_version = package.get("device_version")
        if device_version and not result.get("app_version"):
            result["app_version"] = str(device_version)
        validate_or_raise("run-result.v1.json", result)
        from autophone.execution.closure_journal import ClosureJournal
        journal = ClosureJournal(run_dir)
        if journal.path.exists() or journal.quarantine.exists():
            row = journal.read()
            if row.get('world_identity'):
                result['world_identity'] = copy.deepcopy(row['world_identity'])
            if row['closure_state'] == 'open' and frozen_spec.get('type') in {'verification', 'operation'}:
                journal.freeze({'kind': 'device-run-outcome/v0', 'run_spec': frozen_spec},
                               expected_version=row['version'])
            journal.publish(result)
            path = journal.result_path
        else:
            path = write_json(run_dir / "platform-run-result.json", result)
        try:
            run_index.ingest_run(run_dir, run_root=self.run_root)
        except Exception:
            logger.warning("run_index ingest 失败（不影响 run 收口）: %s", run_dir, exc_info=True)
        return path

    def resume_result_publication(self, run_dir: Path) -> Path:
        """Reconcile prepared product bytes and refresh the existing Run index."""
        from autophone.execution.closure_journal import ClosureJournal
        run_dir = Path(run_dir).resolve()
        if not run_dir.is_relative_to(self.run_root.resolve()):
            raise ValueError('closure outside service run root')
        journal = ClosureJournal(run_dir)
        with journal.owner():
            return self._resume_result_publication_owned(journal)

    @contextmanager
    def plugin_identity_scope(self, spec, context):
        """Plugin executors freeze identity at their own actual ScreenLock boundary."""
        if spec.get('type') in {'regression', 'verification', 'operation'}:
            yield None
            return
        from autophone.device.lock import screen_admission
        from autophone.device.stage_guard import StageGuard
        from .world_identity import capture_world
        run_dir = context.run_dir or self._run_dir(str(spec['run_id']))
        profile = load_app_profile(str(spec['app']))
        def admitted(*, device, run_id):
            if device != context.device.target.rig_id:
                raise RuntimeAdmissionError(['plugin_screen_target_mismatch'])
            guard = StageGuard.current()
            if guard is not None:
                guard.transition('execute')
            capture_world({**spec, 'device': device}, profile, run_dir, session=context.device)
            return (lambda: guard.transition('collect')) if guard is not None else None
        with screen_admission(admitted):
            yield run_dir

    @contextmanager
    def assignment_closure(self, assignment, context):
        """Create the regression intent before Assignment preparation can touch the Rig."""
        from autophone.execution.closure_journal import ClosureJournal
        spec = assignment.get('run_spec') or {}
        if spec.get('type') != 'regression':
            yield None
            return
        run_id = str(spec['run_id'])
        attempt = assignment['attempt']
        run_dir = self._run_dir(run_id)
        if int(attempt['attempt_no']) > 1:
            run_dir = run_dir.with_name(f"{run_id}.attempt-{int(attempt['attempt_no'])}")
        journal = ClosureJournal(run_dir)
        with journal.owner():
            context.write_fence.check()
            journal.open(run_id=run_id, device=str(spec.get('device') or context.target.rig_id),
                         assignment={'assignment_id': str(assignment['assignment_id']),
                                     'attempt_no': int(attempt['attempt_no']), 'fencing': int(attempt['fencing']),
                                     'rig_id': context.target.rig_id, 'generation': context.target.rig_generation})
            with journal.bind_assignment(run_id):
                yield journal.root

    def _resume_result_publication_owned(self, journal) -> Path:
        run_dir = journal.root
        row = journal.read()
        journal.resume(expected_version=row['version'])
        write_artifact_manifest(run_dir, row['run_id'], default_phase2_artifacts(run_dir))
        run_index.ingest_run(run_dir, run_root=self.run_root)
        return journal.result_path

    def resume_collection(self, run_dir: Path, *, assignment_authority=None) -> Path:
        """Resume frozen Host collection, excluding live execution and device access."""
        from autophone.execution.closure_journal import (
            ClosureConflict, ClosureJournal, ClosureSettled, is_interrupted, is_settled)
        from autophone.execution.run_types import PendingPhase2Collection
        from autophone.device.stage_guard import StageGuard

        run_dir = Path(run_dir).resolve()
        if not run_dir.is_relative_to(self.run_root.resolve()):
            raise ValueError('closure outside service run root')
        journal = ClosureJournal(run_dir)
        with journal.owner(), ExitStack() as recovery_scope:
            row = journal.read()
            guard = StageGuard(owner_id=row['run_id'], rig_id=row['device'], generation=0,
                               check_lease=lambda: None)
            recovery_scope.enter_context(guard.scope('collect'))
            if row['terminal_candidate'] is not None:
                return self._resume_result_publication_owned(journal)
            if is_interrupted(row):
                # BL-076 G1: Lease-expired Attempt settled as interrupted and unclosed.
                raise ClosureSettled('closure settled as interrupted_unclosed')
            if is_settled(row):
                # BL-194: settled on the Assignment terminal record; no product inputs exist.
                raise ClosureSettled('closure settled by Assignment terminal authority')
            if row.get('assignment'):
                if assignment_authority is None:
                    raise ClosureConflict('Assignment recovery requires current Lease authority')
                assignment_authority(copy.deepcopy(row['assignment']))
            inputs = row['frozen_inputs'] or {}
            if inputs.get('kind') != 'run-service-outcome/v1' or not inputs.get('pending_collection'):
                raise ClosureConflict('complete collection inputs are unavailable')
            try:
                pending = PendingPhase2Collection.restore(inputs['pending_collection'], run_dir)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                journal.quarantine_inputs(f'frozen collection inputs invalid: {exc}')
            telemetry = None
            if (inputs.get('telemetry_required', True)
                    and plane_mode(inputs['run_spec'], 'telemetry') != 'disabled'):
                checkpoint = (row.get('collection_checkpoints') or {}).get('telemetry_complete')
                if checkpoint is None:
                    query_inputs = inputs.get('telemetry_inputs')
                    if not query_inputs:
                        raise ClosureConflict('telemetry collection recovery inputs are unavailable')
                    from autophone.assets.apps import AppProfile
                    self.execution._write_telemetry(
                        run_dir=run_dir, spec=inputs['run_spec'],
                        profile=AppProfile(raw=query_inputs['profile_raw'], root=run_dir),
                        phase2_result=pending.phase2_result, timestamps=query_inputs['timestamps'])
                    checkpoint = journal.read()['collection_checkpoints']['telemetry_complete']
                for artifact in checkpoint['artifacts']:
                    path = (run_dir / artifact['path']).resolve()
                    if not path.is_relative_to(run_dir):
                        journal.quarantine_inputs('telemetry artifact outside run directory')
                    try:
                        data = path.read_bytes()
                    except OSError as exc:
                        journal.quarantine_inputs(f'telemetry artifact unavailable: {exc}')
                    if len(data) != artifact['size'] or hashlib.sha256(data).hexdigest() != artifact['sha256']:
                        journal.quarantine_inputs('telemetry artifact changed')
                telemetry = checkpoint['payload']
            collection_error = None
            try:
                self.execution._materialize_phase2(pending, run_dir)
            except Exception as exc:
                collection_error = exc
            result = copy.deepcopy(inputs['execution_outcome'])
            if telemetry is not None:
                from autophone.observation.run_result import RunResultAssembler
                result = RunResultAssembler().refresh_telemetry(
                    inputs['run_spec'], pending.phase2_result, result, telemetry)
            ended_at = _now()
            result['lock_phase']['collect'] = {'status': 'failed' if collection_error else 'ok',
                                              'ended_at': ended_at}
            result.setdefault('timestamps', {})['collect_ended_at'] = ended_at
            if row.get('assignment'):
                assignment_authority(copy.deepcopy(row['assignment']))
            self.persist_run_result(run_dir, result)
            path = self._resume_result_publication_owned(journal)
            if collection_error:
                from autophone.execution.run_types import RunCollectionError
                raise RunCollectionError(f'collect recovery failed: {collection_error}',
                                         run_result=result, run_dir=run_dir) from collection_error
            return path

    def recover_pending_closures(self) -> list[dict[str, Any]]:
        """Scan durable journals; busy or incomplete runs remain untouched.

        A journal settled on its Assignment terminal record reports ``settled``. The scan
        never settles one itself: that needs the explicit ``closure_journal settle`` entry.
        """
        from autophone.execution.closure_journal import ClosureConflict, ClosureQuarantined, ClosureSettled
        from autophone.observation.telemetry import TelemetryReplayUnavailable

        results = []
        for path in sorted(self.run_root.rglob('closure-intent.json')):
            try:
                self.resume_collection(path.parent)
                status, detail = 'complete', None
            except ClosureSettled as exc:
                status = 'settled'
                detail = 'interrupted_unclosed' if 'interrupted_unclosed' in str(exc) else None
            except (ClosureConflict, TelemetryReplayUnavailable) as exc:
                status, detail = 'deferred', str(exc)
            except ClosureQuarantined as exc:
                status, detail = 'quarantined', str(exc)
            except Exception as exc:
                status, detail = 'failed', f'{type(exc).__name__}: {exc}'
            try:
                run_index.ingest_closure(path.parent, run_root=self.run_root)
            except Exception:
                logger.warning('closure index refresh failed: %s', path.parent, exc_info=True)
            results.append({'run_dir': str(path.parent), 'status': status, 'detail': detail})
        return results


    def _register_pending(
        self,
        spec: dict[str, Any],
        *,
        run_id: str,
        device: str,
        run_dir: Path,
    ) -> None:
        """把一条已提交、尚未拿到屏幕锁的 run 登记进队列。

        条目只带**已知为真**的口径：``app`` / ``type`` 决定看板怎么称呼这条任务，
        ``case_count`` 让「预计等待」按各 run 的实际规模估（一条单操作 run 不该按整套
        用例的历史耗时算）。``requested_by`` 是 run-spec 里的真实字段（web / local /
        platform-acceptance…）——**没有就不写**，前端因此不会显示编出来的发起人。
        """
        started_path = self.execution._screen_started_path(run_dir)
        # 同 run_id 被重新提交时不能继承旧 marker；run-spec 本身的唯一性由既有契约负责。
        started_path.unlink(missing_ok=True)
        entry: dict[str, Any] = {
            "run_id": run_id,
            "device": device,
            "queued_at": _now(),
            "app": str(spec["app"]),
            "type": str(spec["type"]),
            "case_count": _spec_case_count(spec),
            "_screen_started_path": str(started_path),
        }
        requested_by = str(spec.get("requested_by") or "").strip()
        if requested_by:
            entry["requested_by"] = requested_by
        with self._pending_lock:
            self._pending.append(entry)

    def _clear_pending(self, run_id: str) -> None:
        with self._pending_lock:
            removed = [item for item in self._pending if item["run_id"] == run_id]
            self._pending = [item for item in self._pending if item["run_id"] != run_id]
        for item in removed:
            marker = str(item.get("_screen_started_path") or "")
            if marker:
                Path(marker).unlink(missing_ok=True)

    def status(self, *, device: str = DEFAULT_DEVICE) -> dict[str, Any]:
        """返回设备状态，包括锁状态、队列深度和设备可达性。"""
        lock_status = self.lock.status(device=device)
        # 持锁那条已经在「当前执行」里展示，不该同时算进「等待执行」。用 ScreenLock
        # 判定而不是让 worker 自己出队：锁是文件锁、跨进程可见，而 worker 可能在子进程里。
        holder = str(lock_status.get("run_id") or "") if lock_status.get("locked") else ""
        with self._pending_lock:
            pending = []
            for item in self._pending:
                if item["device"] != device or item["run_id"] == holder:
                    continue
                marker = str(item.get("_screen_started_path") or "")
                if marker and Path(marker).exists():
                    continue
                pending.append({
                    key: value for key, value in item.items()
                    if not key.startswith("_")
                })

        # 检测设备是否在线：尝试发送 ping 请求
        online = self._check_device_online()

        status = {
            "device": device,
            "online": online,
            "lock": lock_status,
            # ScreenLock 隔离标记（BL-223）：存在时 Run 取锁必被拒，Rig 显示 online 也一样。
            "screen_quarantine": self.lock.quarantine_status(device=device),
            "queue_depth": len(pending),
            "pending": pending,
        }
        if online is None:
            # 本进程有活动 Stage，探针被 StageGuard 拒绝：在线状态未探测，不是离线（BL-227）。
            status["online_source"] = "stage_active_unprobed"
        return status

    def _check_device_online(self) -> bool | None:
        """探测设备是否可用（看板设备灯）——收敛到 `device.probe_uds_state`。

        以前发 `{"t":"ping"}` 并**故意不看 ok**（host UDS 面没有 ping 命令，会回
        `ok=false unknown command`），所以只证明「vphone-cli 进程活着」；guest 掉线时
        本方法照样报在线。现在统一走三态探针，只有 `guest_ready` 才算在线——即
        `app_list` 真穿过 vsock 到 vphoned。代价是一次 guest 往返（p50 约 60ms，
        对照 host 面 ping 约 7ms），换来的是判活与 Assignment preflight 同一口径。
        """
        from autophone.device import probe_uds_state
        from autophone.device.stage_guard import StageAccessDenied
        from autophone.execution.engine.actions import resolve_sock

        try:
            sock = resolve_sock()
        except Exception:  # noqa: BLE001 - 解析不出 target 一律算「不在线」
            return False
        try:
            return bool(probe_uds_state(sock, timeout=3.0)["available"])
        except StageAccessDenied:
            # Status projection carries no device authority during a managed Run:
            # the state is unprobed (None), not offline (BL-227).
            return None

    def history(
        self,
        *,
        limit: int = 20,
        app: str | None = None,
        status: str | None = None,
        type: str | None = None,
        before: int | None = None,
    ) -> list[dict[str, Any]]:
        return self.queries.history(limit=limit, app=app, status=status, type=type, before=before)

    def app_trend(self, app: str, *, limit: int = 20) -> dict[str, Any]:
        return self.queries.app_trend(app, limit=limit)

    def latest_case_verdicts(self, app: str, *, limit: int = 50) -> dict[str, dict[str, Any]]:
        return self.queries.latest_case_verdicts(app, limit=limit)

    def latest_case_api_capture(
        self, app: str, case_id: str, *, limit: int = 50, allow_raw: bool = False
    ) -> dict[str, Any]:
        return self.queries.latest_case_api_capture(app, case_id, limit=limit, allow_raw=allow_raw)

    def _read_result_cached(self, path: Path) -> dict[str, Any] | None:
        return self.queries._read_result_cached(path)

    def find_run_dir(self, run_id: str) -> Path:
        return self.queries.find_run_dir(run_id)

    def requires_device(self, spec: Mapping[str, Any]) -> bool:
        """这条 Run 是否需要真实设备，即 Work Plan 的 `requires_rig` 取值（BL-024）。

        判据与队列登记同一条（`_requires_screen_lock`）：会竞争设备 ScreenLock 的 Run 就是
        需要 rig 的 Run。只做 API / 解析 / 报告的 Run 不需要，按 §16.0 的 `requires_rig`
        两类路由归 Host-only 工作，不创建 Rig Assignment。
        """
        return self.execution._requires_screen_lock(dict(spec), load_app_profile(str(spec.get("app") or "")))

    def handle_for(self, run_id: str, result: Mapping[str, Any]) -> RunHandle:
        """按 `run_id` 与已知 Run Result 组一个 `RunHandle`（派单路径回读用，BL-024）。

        run 目录由执行侧建，这里只解析它：Assignment 由本机 rig 执行时 `find_run_dir` 能找到；
        找不到时退回今日目录的投影路径，让 handle 的形态仍然完整——CLI 打印的是路径，路径不
        存在时是执行侧的问题，不该让提交侧多抛一种错。
        """
        try:
            run_dir = self.find_run_dir(run_id)
        except FileNotFoundError:
            run_dir = self._run_dir(run_id)
        return RunHandle(
            run_id=run_id,
            run_dir=run_dir,
            result=dict(result),
            manifest_path=run_dir / "artifact-manifest.json",
        )

    def detail(self, run_id: str) -> dict[str, Any]:
        return self.queries.detail(run_id)

    def api_capture(self, run_id: str, *, allow_raw: bool = False) -> dict[str, Any]:
        return self.queries.api_capture(run_id, allow_raw=allow_raw)

    def telemetry(self, run_id: str) -> dict[str, Any]:
        return self.queries.telemetry(run_id)

    def latest_case_telemetry(
        self,
        app: str,
        case_id: str,
        *,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self.queries.latest_case_telemetry(app, case_id, limit=limit)


    def refresh_telemetry(
        self,
        run_id: str,
        *,
        actor: str = "web",
        data_trace_client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> dict[str, Any]:
        return self.queries.refresh_telemetry(run_id, actor=actor, data_trace_client=data_trace_client, sleep=sleep)

    def replay_api_call(
        self, run_id: str, idx: int, *, allow_raw: bool = False
    ) -> dict[str, Any]:
        return self.queries.replay_api_call(run_id, idx, allow_raw=allow_raw)

    def resume_manual(self, run_id: str, *, actor: str = "web", note: str = "") -> dict[str, Any]:
        run_dir = self.find_run_dir(run_id)
        return resume_manual_checkpoint(run_dir, actor=actor, note=note)

    def request_abort(self, run_id: str, *, actor: str = "web", reason: str = "") -> dict[str, Any]:
        """请求中止一条在跑/排队的 run（协作式，见 run_abort 模块说明）。

        - run 必须仍 active（在 _active 注册表里）——已收口的 run 无可中止对象，抛
          ``RunNotAbortable``（API 映 409）。
        - 落 run 级中止信号；执行链在协作检查点（executor 入口 / phase2 逐用例）读它。
          **排队态**：信号即时生效——线程从 flock 醒来进 executor 即见标志、跳过启动收口。
          **执行态**：在下一个用例边界协作停下（已发生的真副作用不回滚）。
        - 若正卡在人工断点，顺手落 manual abort 让等待循环立即跳出（与既有语义一致：
          该用例判 fail、run 收口）。
        """
        run_dir = self.find_run_dir(run_id)
        with self._active_lock:
            active = dict(self._active.get(run_id) or {})
        if not active:
            raise RunNotAbortable(f"run is not active (already finished or never queued): {run_id}")
        abort = request_run_abort(run_dir, actor=actor, reason=reason)
        manual = load_manual_state(run_dir)
        if (manual or {}).get("checkpoint", {}).get("status") == "waiting":
            try:
                abort_manual_checkpoint(run_dir, actor=actor, reason=reason or "run aborted")
            except FileNotFoundError:
                pass
        self._update_active(run_id, status="aborting")
        return {"run_id": run_id, "status": "aborting", "abort": abort}

    def verification_runs(self, *, draft_id: str | None = None) -> list[dict[str, Any]]:
        return self.queries.verification_runs(draft_id=draft_id)

    def promotion_decision(
        self, *, draft_id: str, required_passes: int = 3, profile: AppProfile | None = None
    ) -> dict[str, Any]:
        return self.promotions.promotion_decision(draft_id=draft_id, required_passes=required_passes, profile=profile)

    def review_verification(
        self,
        run_id: str,
        *,
        status: str,
        reviewer: str = "web",
        note: str = "",
    ) -> dict[str, Any]:
        return self.promotions.review_verification(run_id, status=status, reviewer=reviewer, note=note)

    def operation_runs(
        self, *, draft_id: str | None = None, app: str | None = None,
        revision: int | None = None,
    ) -> list[dict[str, Any]]:
        return self.promotions.operation_runs(draft_id=draft_id, app=app, revision=revision)

    def operation_promotion_decision(
        self, *, draft_id: str, required_passes: int = 3,
        profile: AppProfile | None = None, op_def: dict[str, Any] | None = None,
        app: str | None = None,
    ) -> dict[str, Any]:
        return self.promotions.operation_promotion_decision(draft_id=draft_id, required_passes=required_passes, profile=profile, op_def=op_def, app=app)


    # ---------------------------------------------------------------------
    # 用例草稿转正线（第三条流水线，对称 verification_runs / operation_runs）
    # ---------------------------------------------------------------------


    def case_draft_runs(self, *, app: str | None = None, case_id: str | None = None) -> list[dict[str, Any]]:
        return self.promotions.case_draft_runs(app=app, case_id=case_id)

    def case_promotion_decision(
        self, *, case_id: str, required_passes: int = 3,
        profile: AppProfile | None = None, runs: list[dict[str, Any]] | None = None,
        case: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.promotions.case_promotion_decision(case_id=case_id, required_passes=required_passes, profile=profile, runs=runs, case=case)


    def review_case_draft(
        self, run_id: str, *, case_id: str, status: str,
        reviewer: str = "web", note: str = "", profile: AppProfile | None = None,
    ) -> dict[str, Any]:
        return self.promotions.review_case_draft(run_id, case_id=case_id, status=status, reviewer=reviewer, note=note, profile=profile)

    def promote_case_draft(
        self, *, profile: AppProfile, case_id: str, required_passes: int = 3,
    ) -> dict[str, Any]:
        return self.promotions.promote_case_draft(profile=profile, case_id=case_id, required_passes=required_passes)


    def review_operation(
        self,
        run_id: str,
        *,
        status: str,
        reviewer: str = "web",
        note: str = "",
    ) -> dict[str, Any]:
        return self.promotions.review_operation(run_id, status=status, reviewer=reviewer, note=note)


    def promote_verification(
        self, run_id: str, *, profile: AppProfile, overrides: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self.promotions.promote_verification(run_id, profile=profile, overrides=overrides)

    def _set_active(self, run_id: str, payload: dict[str, Any]) -> None:
        with self._active_lock:
            self._active[run_id] = payload

    def _update_active(self, run_id: str, **updates: Any) -> None:
        with self._active_lock:
            if run_id in self._active:
                self._active[run_id].update(updates)

    def _clear_active(self, run_id: str) -> None:
        with self._active_lock:
            self._active.pop(run_id, None)

    def _is_active(self, run_id: Any) -> bool:
        if not run_id:
            return False
        with self._active_lock:
            return str(run_id) in self._active

    # ---------------------------------------------------------------------
    # 批量回放（record-replay-batch）
    # ---------------------------------------------------------------------
    # worker 两条之间阻塞等 settled → 任意时刻只有一条在飞，顺序确定、天然串行。

    def submit_replay_batch(
        self,
        *,
        app: str,
        device: str,
        recording_ids: list[str],
        replay_speed: Any = None,
        telemetry_identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.replay_batch_service.submit_replay_batch(app=app, device=device, recording_ids=recording_ids, replay_speed=replay_speed, telemetry_identity=telemetry_identity)


    def replay_batch_detail(self, batch_id: str) -> dict[str, Any] | None:
        return self.replay_batch_service.replay_batch_detail(batch_id)

    def replay_batches(self, *, app: str | None = None) -> list[dict[str, Any]]:
        return self.replay_batch_service.replay_batches(app=app)

    def _run_dir(self, run_id: str) -> Path:
        from autophone.execution.closure_journal import ClosureJournal
        bound = ClosureJournal.bound_directory(run_id)
        if bound is not None:
            return bound
        day = datetime.now().strftime("%Y%m%d")
        return self.run_root / day / run_id


def _run_background_dispatch(
    spec: dict[str, Any],
    run_root: str,
    negctl_root: str,
    lock_root: str,
    run_id: str,
    run_dir: str,
    device: str,
    log_queue: Any | None = None,
    lease_channel: Any | None = None,
) -> None:
    """Spawn-safe default background worker.

    The worker reconstructs only serializable service dependencies.  It must stay
    module-level so the macOS ``spawn`` start method can import it.

    The worker owns its own shared release lease.  ``spawn`` does not inherit the
    parent's descriptors, and the triggering CLI process exits as soon as it has
    printed the early handle, so without this the run would execute with no lease
    at all and a concurrent ``self-update apply`` could migrate state and switch
    ``current`` underneath it.
    """
    # A spawned process must not own terminal/global/run files. It relays records
    # to the Web parent, which serializes every durable sink. The no-queue branch
    # keeps direct invocation/backward-compatible tests useful.
    if log_queue is None:
        setup_logging(level_from_env())
    else:
        configure_worker_logging(log_queue, level_from_env())

    def _announce(status: str) -> None:
        if lease_channel is not None:
            lease_channel.put(status)

    with ExitStack() as lease:
        if not inherited_exclusive_release_lock(STATE_ROOT):
            try:
                lease.enter_context(
                    release_lock(STATE_ROOT, exclusive=False, blocking=False)
                )
            except ReleaseLockBusy as exc:
                # Fail closed: an exclusive release update is in flight, so this
                # run must not start.  The parent turns this into a submit error.
                _announce(f"release_update_active: {exc}")
                return
        _announce(_WORKER_LEASE_OK)
        service = RunService(
            run_root=Path(run_root),
            negctl_root=Path(negctl_root),
            lock=ScreenLock(root=Path(lock_root)),
        )
        profile = load_app_profile(str(spec["app"]))
        with bind_run(run_id, run_dir=Path(run_dir), stage="dispatch"):
            try:
                service._dispatch(spec, profile, run_id, Path(run_dir), device)
            except Exception as exc:
                logger.exception("后台 run 执行失败 %s", run_id)
                service._write_error_result(spec, Path(run_dir), run_id, device, error=exc)
                raise
