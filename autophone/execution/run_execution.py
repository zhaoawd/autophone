"""Run execution, device phases and evidence collection."""
from __future__ import annotations
import logging
import copy
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterable
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from autophone.paths import CODE_ROOT, STATE_ROOT
from autophone.packages import store as package_store
from autophone.record import replay_timing
from autophone.rig.maintenance import RigMaintenance
from autophone.observation.capture import ax_scope, ensure_ax_backend_supported
from autophone.assets.promotion_identity import current_draft_recipe_identity, selector_is_draft_trial
from autophone.assets.apps import AppProfile, load_app_profile
from autophone.execution.app_recipes import import_app_recipes
from autophone.execution.recipe_identity import (
    RECIPE_READ_KEY,
    attach_recipe_identities,
    recipe_identity_candidates,
    recipe_read_record,
)
from autophone.execution.negative_control import (
    NEGATIVE_CONTROL_STEPS_KEY,
    attach_negative_control_records,
    bind_negative_control,
    bind_operation_witnesses,
    negative_control_case_ids,
)
from autophone.execution.cleanup_readback import write_cleanup_readback_evidence
from autophone.execution.entity_readback import write_entity_readback_evidence
from autophone.execution.planning import has_execution_planner
from autophone.execution.fixture_readback import (
    bind_fixture_readback_plans,
    write_fixture_readback_evidence,
)
from autophone.execution.golden_path_binding import (
    GOLDEN_PATH_LIVE_DOCUMENT_KEY,
    bind_golden_path,
    golden_path_live_steps,
)
from autophone.observation.artifacts import Artifact, append_artifacts, default_phase2_artifacts, write_artifact_manifest
from autophone.contracts import ContractError, write_json
from autophone.device.lock import ScreenLock
from autophone.device.stage_guard import StageGuard, StageAccessDenied, StageQuiesceError
from autophone.logging import bind_run, has_run_router, is_worker_logging, run_logfile
from autophone.execution.run_abort import run_abort_reason
from autophone.execution.operation_run import device_prep, load_operation_spec, raw_operation_def, raw_steps_of, run_operation_executor, write_operation_outputs
from autophone.observation.run_result import RunResultAssembler, plane_mode
from autophone.observation.telemetry import collect_telemetry_record
from autophone.util import now_iso as _now
from autophone.execution.verification import load_verification_spec, run_verification_executor, write_verification_outputs
from autophone.execution.run_types import ExecutionBundle, PendingPhase2Collection, RunCollectionError, RunHandle, Executor

logger = logging.getLogger(__name__)

#: 平台预启动动作记录（`_launch_app`）的证据文件名，位于 shots 目录。
PLATFORM_LAUNCH_EVIDENCE_NAME = "platform-launch-actions.json"
# 自动补登手机号读回的 AX attach 等待上限（秒），与 operation run 的单次 attach 上限相同。
MOBILE_READBACK_AX_ATTACH_TIMEOUT_S = 10.0
FRIDA_RELEASE_GRACE_S = 5.0



def _device_locale_meta(run_dir: Path) -> dict[str, Any] | None:
    """Run 报告展示的设备语言：`world-identity.json` 中 `locale_context` 的摘要（BL-207/BL-213）。

    只读已落盘的派生文件，不读 closure journal（journal 读到损坏会转入隔离）。没有该文件、
    文件不可解析或旧记录没有 `locale_context` 时返回 None，报告不显示该项。
    """
    try:
        world = json.loads((Path(run_dir) / "world-identity.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    context = world.get("locale_context") if isinstance(world, dict) else None
    if not isinstance(context, dict):
        return None
    return {
        key: context.get(key)
        for key in ("state", "preferred_language", "current_locale", "current_locale_scope", "reason")
        if context.get(key) is not None
    }


def _probe_world_installed_subject(run_dir: Path, submission) -> dict[str, Any] | None:
    """Only derive version fields from this Run and the bound target's world."""
    from autophone.device.target import resolve_target
    try:
        world = json.loads((run_dir / 'world-identity.json').read_text(encoding='utf-8'))
        if not isinstance(world, dict) or world.get('run_id') != submission.run_id:
            return None
        subject = world.get('subject_build_ref')
        if not isinstance(subject, dict):
            return None
        expected = submission.candidate_build_ref or submission.installed_build_ref
        installed = subject.get('installed_build_ref')
        if not isinstance(expected, dict) or not isinstance(installed, dict):
            return None
        if expected.get('app') != submission.app or any(
                expected.get(key) != installed.get(key)
                for key in ('app', 'slot', 'build_id', 'artifact_sha256', 'source_revision')):
            return None
        target, recorded = resolve_target(), world.get('vm_instance_ref')
        if not isinstance(recorded, dict) or any(
                not recorded.get(key) or Path(recorded[key]).resolve() != value.resolve()
                for key, value in (('vm_dir', target.vm_dir), ('socket', target.sock))):
            return None
        return subject
    except (OSError, ValueError, TypeError):
        return None


def _confirmed_trial_case_ids(spec: Mapping[str, Any]) -> set[str]:
    """Return case IDs explicitly acknowledged in ``spec['trial_confirmation']``."""
    selector = spec.get("case_selector") or {}
    if not selector_is_draft_trial(selector):
        return set()
    markers = spec.get("trial_confirmation")
    if not isinstance(markers, list):
        return set()
    confirmed: set[str] = set()
    for row in markers:
        if (
            isinstance(row, Mapping)
            and row.get("acknowledged") is True
            and isinstance(row.get("case_id"), str)
            and row["case_id"].strip()
        ):
            confirmed.add(row["case_id"].strip().upper())
    return confirmed


def _trial_effect_context(
    spec: Mapping[str, Any],
    profile: AppProfile,
    *,
    selected_ids: list[str] | None = None,
) -> tuple[bool, bool, set[str], str]:
    """Derive M4 draft-trial & isolation policy flags from RunSpec and AppProfile."""
    from autophone.execution.effects import profile_trial_data_isolation

    selector = spec.get("case_selector") or {}
    trial_draft_recipes = selector_is_draft_trial(selector)
    confirmed_ids = _confirmed_trial_case_ids(spec)
    ids_source = selected_ids if selected_ids is not None else (selector.get("ids") or [])
    pinned_ids = {str(cid).upper() for cid in ids_source if str(cid)}
    all_confirmed = bool(trial_draft_recipes and pinned_ids and pinned_ids.issubset(confirmed_ids))
    isolation = profile_trial_data_isolation(profile)
    return trial_draft_recipes, all_confirmed, confirmed_ids, isolation


class AppLaunchBlocked(str):
    """`_launch_app` 的失败文案；启动以 `CaseBlocked` 结束时另带结构化 blocker。

    继承 str：既有调用方（Rig 维护、operation 前置、测试替身）仍按文案处理；
    回归组启动与 operation 前置经 `.blocker` 取 scope / code。
    """

    blocker: dict[str, str]

    def __new__(cls, detail: str, blocker_payload: Mapping[str, Any]) -> "AppLaunchBlocked":
        value = super().__new__(cls, detail)
        value.blocker = {str(k): str(v) for k, v in blocker_payload.items()}
        return value


def launch_blocker(launch_error: str | None) -> dict[str, str] | None:
    """平台预启动失败携带的结构化 blocker；普通失败文案或成功返回 None。"""
    payload = getattr(launch_error, "blocker", None)
    return dict(payload) if isinstance(payload, Mapping) else None

def run_abort_blocker(abort_reason: str) -> dict[str, str]:
    """Run 中止原因 → 未执行 Case 的结构化 blocker（BL-083，2026-10-11 用户裁定）。

    登录闸中止（`login_state …`）属于会话前置不成立，记 `scope=fixture`、
    `code=authenticated_session_unavailable`、`capability=authenticated_session`
    （与 capability_requirements 为需登录用例派生的 requirement 同名）。其余中止原因
    （协作式中止、gate 失败、运行期资源清理失败等）没有更细的分类，记 `scope=rig`：
    协作式中止为 `run_aborted_by_request`，其余为 `run_aborted`。原文写在 Case 的 detail。
    """
    from autophone.execution.contracts import app_liveness_blocker, blocker

    reason = str(abort_reason or "")
    # 自动补登中被测 App 结束或画面离开该 App（`自动补登失败（stage=app_crashed|app_left_foreground）`）
    # 不是会话前置问题，按 rig 范围记 app_process_exited / app_not_foreground（BL-083 第七轮）。
    stage_match = re.search(r"stage=([a-z_]+)", reason) if reason.startswith("login_state") else None
    liveness = app_liveness_blocker(stage_match.group(1)) if stage_match else None
    if liveness is not None:
        return liveness
    if reason.startswith("login_state"):
        return blocker(
            "fixture", "authenticated_session_unavailable",
            capability="authenticated_session",
            recovery="restore a verified logged-in session (auto_login or a login fixture), "
                     "then rerun the aborted group",
        )
    if reason.startswith("aborted by"):
        return blocker(
            "rig", "run_aborted_by_request",
            recovery="resubmit the Run once the abort request is resolved",
        )
    return blocker(
        "rig", "run_aborted",
        recovery="read the Run abort_reason, rig-snapshot.json and run.log, then rerun",
    )


def materialize_unexecuted_cases(
    cases: list[dict[str, Any]],
    outcomes: list[Any],
    *,
    blocker_payload: Mapping[str, Any],
    detail: str,
) -> list[Any]:
    """为中止后未执行的 Case 生成 blocked 结果；已有结果的 Case 不重复生成。"""
    from autophone.execution.engine.runner import CaseOutcome

    seen = {str(outcome.id) for outcome in outcomes}
    return [
        CaseOutcome(
            id=str(case.get("id") or ""),
            status="blocked",
            module=str(case.get("module") or ""),
            item=str(case.get("item") or ""),
            row=case.get("row") if isinstance(case.get("row"), int) else None,
            detail=detail,
            blocker=dict(blocker_payload),
        )
        for case in cases
        if str(case.get("id") or "") and str(case.get("id") or "") not in seen
    ]


_VSTATUS_TO_STATUS = {"passed": "passed", "failed": "failed", "error": "error", "skipped": "skipped"}


_VSTATUS_TO_VERDICT = {"passed": "pass", "failed": "fail", "error": "fail", "skipped": "skipped"}


_OPSTATUS_TO_STATUS = {
    "pending_execution": "pending",
    "passed": "passed",
    "failed": "failed",
    "blocked": "blocked",
    "error": "error",
    # 协作式中止（BL-062）：与用例回归 RunResult(aborted=True) → reduce_regression_status
    # 的 blocked 同口径；operation-result 自身保留 aborted，以区分「被中止」与「失败」。
    "aborted": "blocked",
}


_OPSTATUS_TO_VERDICT = {
    "pending_execution": "pending",
    "passed": "pass",
    "failed": "fail",
    "blocked": "skipped",
    "error": "fail",
    # 与用例回归路径 aborted → ui plane verdict=skipped、detail=abort_reason 同口径。
    "aborted": "skipped",
}


_OPSTEP_TO_STEP_STATE = {
    "pending": "todo",
    "pending_execution": "todo",
    "passed": "pass",
    "skipped": "done",
    "failed": "failed",
    "error": "failed",
}


def _build_identity_match(
    ref: Mapping[str, Any] | None, observation: Mapping[str, Any] | None,
) -> str | None:
    """Compare the Host's frozen selection with what the device reported.

    No observation means ``not_observed`` — never ``match``.  Absence of
    evidence is not evidence of identity.
    """
    if ref is None:
        return None
    if not observation:
        return "not_observed"
    pairs = (
        (ref.get("short_version"), observation.get("short_version")),
        (ref.get("build_version"), observation.get("build_version")),
    )
    compared = [(a, b) for a, b in pairs if a is not None and b is not None]
    if not compared:
        return "not_observed"
    return "match" if all(str(a) == str(b) for a, b in compared) else "mismatch"


def _operation_step_state(status: Any) -> str:
    return _OPSTEP_TO_STEP_STATE.get(str(status or "pending"), "todo")


def _aborted_before_start(operation: dict[str, Any], reason: str) -> dict[str, Any]:
    """启动前见中止信号：执行器未运行，步骤保持 pending，状态记 aborted、detail 记原因。"""
    out = dict(operation)
    out["status"] = "aborted"
    out["detail"] = reason
    return out


def _default_capture_session_factory(
    profile: AppProfile, spec: dict[str, Any], shot_dir: Path
) -> Any | None:
    """默认抓取会话工厂：委托 capture.build_session（懒 import，fail-open 返回 None）。

    shot_dir 透传给 attach 模式做 attach 前的 OCR ready gate（spawn 模式不用）。"""
    from autophone.observation import capture as capture_mod

    return capture_mod.build_session(profile, spec, shot_dir)


def _default_recorder_factory(run_dir: Path, spec: dict[str, Any]) -> Any | None:
    """默认录屏器工厂：委托 recording.build_recorder（懒 import，开关关闭 → None）。"""
    from autophone.record import recording as recording_mod

    return recording_mod.build_recorder(run_dir, spec)


_RUN_FRIDA_ACTIVITY: ContextVar[dict[str, Any] | None] = ContextVar(
    "_run_frida_activity",
    default=None,
)


def _profile_ax_uses_frida(profile: AppProfile) -> bool:
    """Whether AX observation for this profile attaches Frida in-process."""
    from autophone.observation.capture import _ax_enabled

    if not _ax_enabled(profile):
        return False
    return ensure_ax_backend_supported(profile) == "frida_inproc"


def _profile_or_spec_needs_frida(spec: Mapping[str, Any], profile: AppProfile) -> bool:
    """Baseline Frida need derived from run spec and profile declarations."""
    if plane_mode(dict(spec), "api") != "disabled":
        return True
    if _profile_ax_uses_frida(profile):
        return True
    profile_reqs = getattr(profile, "requirements", {}) or {}
    if isinstance(profile_reqs, Mapping) and any(
        str(profile_reqs.get(capability) or "disabled") != "disabled"
        for capability in ("api_capture", "location_injection", "frida")
    ):
        return True
    return False


def _regression_needs_frida(
    spec: Mapping[str, Any],
    profile: AppProfile,
    required_cases: Iterable[Mapping[str, Any]],
) -> bool:
    """Whether a regression run requires Frida attachment for any active capability."""
    from autophone.assets.entity_binding import case_requires_api_capture

    if _profile_or_spec_needs_frida(spec, profile):
        return True
    cases = list(required_cases)
    if any(
        mode != "disabled"
        for case in cases
        for capability, mode in (case.get("requirements") or {}).items()
        if capability in {"api_capture", "location_injection", "frida"}
    ):
        return True
    return any(case_requires_api_capture(case) for case in cases)


def _crash_trap_enabled(
    spec: Mapping[str, Any],
    profile: AppProfile | None = None,
    *,
    need_frida: bool | None = None,
) -> bool:
    """崩溃兜底捕获开关。优先级：run-spec 显式 `crash_trap` > 环境 `AUTOPHONE_CRASH_TRAP` > 按 Frida 需求默认。
    纯免 Frida Run（`need_frida=False` 且禁用 API 抓取 / `guest_axruntime`）缺省关闭，避免引入
    `Tag: 255` 脏页；其余（`frida_inproc` / 开启 API 抓取 / 显式 `AUTOPHONE_CRASH_TRAP=1` /
    spec `crash_trap:true`）默认开（无 frida 时仍由会话自身 fail-open）。"""
    if "crash_trap" in spec:
        return bool(spec.get("crash_trap"))
    env = os.environ.get("AUTOPHONE_CRASH_TRAP")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "off", "no", "")
    if need_frida is not None:
        return bool(need_frida)
    if profile is not None:
        return _profile_or_spec_needs_frida(spec, profile)
    return True


def _default_crash_trap_factory(
    profile: AppProfile,
    spec: dict[str, Any],
    run_dir: Path,
    *,
    need_frida: bool | None = None,
) -> Any | None:
    """默认崩溃兜底工厂：开关开时建一个 CrashTrapSession（懒 import，fail-open）；关或缺依赖 → None。
    会话在整个 execute 阶段后台挂着，(re)attach 被测 app，把原始崩点落到 run_dir/planes/crash/。
    平台默认始终是 observe-only，不中和 App/SDK 的信号 handler。"""
    if not _crash_trap_enabled(spec, profile, need_frida=need_frida):
        return None
    try:
        from autophone.observation.frida.crash_trap import CrashTrapSession
    except Exception:  # noqa: BLE001 - 无依赖即不挂，fail-open
        return None
    out_dir = run_dir / "planes" / "crash"
    return CrashTrapSession(bundle_id_or_name=str(profile.bundle_id), out_dir=out_dir, verbose=False)


def _default_app_hygiene_factory(profile: AppProfile, spec: dict[str, Any], run_dir: Path) -> Any | None:
    """Run 前残留检测与 Run 后终止的设备端口（BL-083 第八轮）；开关关或无 bundle 时为 None。"""
    from autophone.execution import app_frida_hygiene as hygiene

    if not hygiene.hygiene_enabled() or not str(profile.bundle_id or "").strip():
        return None
    return hygiene.DevicePorts(str(profile.bundle_id), run_dir / "shots" / "_app_frida_hygiene")


class RunExecution:
    """Run Spec -> collected Run Result, over state/persistence and device ports."""

    def __init__(self, *, lock: ScreenLock, rig_maintenance: RigMaintenance,
                 executor: Executor | None,
                 run_dir: Callable[[str], Path], persist_result: Callable[..., Path],
                 set_active: Callable[..., None], update_active: Callable[..., None],
                 clear_active: Callable[[str], None], assert_store_context: Callable[..., None],
                 bind_store_context: Callable[..., Any],
                 capture_session_factory: Callable[..., Any], recorder_factory: Callable[..., Any],
                 crash_trap_factory: Callable[..., Any], telemetry_sleep: Callable[[float], None],
                 location_quiesce_fn: Callable[..., Any] | None,
                 app_hygiene_factory: Callable[..., Any] | None = None) -> None:
        self.lock = lock
        self.rig_maintenance = rig_maintenance
        self.executor = executor or self._execute_phase2
        self._uses_default_executor = executor is None
        self._run_dir = run_dir
        self.persist_run_result = persist_result
        self._set_active = set_active
        self._update_active = update_active
        self._clear_active = clear_active
        self._assert_operation_store_context = assert_store_context
        self._bind_operation_store_context = bind_store_context
        self.capture_session_factory = capture_session_factory
        self.recorder_factory = recorder_factory
        self.crash_trap_factory = crash_trap_factory
        self._telemetry_sleep = telemetry_sleep
        self.location_quiesce_fn = location_quiesce_fn
        self.app_hygiene_factory = app_hygiene_factory or _default_app_hygiene_factory

    def execute(self, spec: dict[str, Any], profile: AppProfile, run_id: str, run_dir: Path, device: str) -> RunHandle:
        if plane_mode(spec, "api") == "controlled" and spec.get("type") != "operation":
            raise ContractError("controlled_operation_required")
        guard = StageGuard.current()
        if guard is not None:
            guard.transition("collect")
            return self._execute_scoped(spec, profile, run_id, run_dir, device)

        def check_screen_owner():
            holder = self.lock.live_status(device=device)
            if not holder.get("locked") or holder.get("run_id") != run_id:
                raise StageAccessDenied("screen_execution_ownership_missing")

        # Compatibility Runs have a ScreenLock authority, not a Rig Generation.
        guard = StageGuard(
            owner_id=run_id, rig_id=device, generation=0, check_lease=check_screen_owner,
            quarantine=lambda failures: self.lock.quarantine(
                device=device, owner_id=run_id, failures=failures,
            ),
        )
        with guard.scope("collect"):
            return self._execute_scoped(spec, profile, run_id, run_dir, device)

    def _execute_scoped(self, spec, profile, run_id, run_dir, device) -> RunHandle:
        # Decision D8: validate ax_backend at run startup preflight before any
        # screen execution or ax_scope runs (supported: frida_inproc,
        # guest_axruntime; unknown or unsupported backends fail closed).
        ensure_ax_backend_supported(profile)
        if spec["type"] == "verification":
            return self._submit_verification(spec, profile, run_id, run_dir)
        if spec["type"] == "operation":
            return self._submit_operation(spec, profile, run_id, run_dir)
        return self._run_regression(spec, profile, run_id, run_dir, device)

    def _app_hygiene_ports(self, spec: dict[str, Any], profile: AppProfile, run_dir: Path) -> Any | None:
        try:
            return self.app_hygiene_factory(profile, spec, run_dir)
        except Exception:  # noqa: BLE001 - 构造失败不拦回归
            logger.warning("App Frida 残留处置端口构造失败（跳过）", exc_info=True)
            return None

    def _pre_run_residue_check(self, profile: AppProfile, ports: Any | None) -> dict[str, Any] | None:
        """Run 前（crash_trap 与准备阶段 attach 之前）检测被测 App 的 Frida 残留，带残留时冷重启。"""
        if ports is None:
            return None
        from autophone.execution import app_frida_hygiene as hygiene

        try:
            return hygiene.pre_run_residue_check(
                str(profile.bundle_id), app_pid=ports.app_pid, shell=ports.shell,
                relaunch=ports.relaunch, inspect=getattr(ports, "inspect_residue", None))
        except Exception as exc:  # noqa: BLE001 - fail-open：检测自身异常不改变准备阶段判定
            logger.warning("Run 前 Frida 残留检测异常（fail-open）：%s: %s", type(exc).__name__, exc)
            return {"bundle_id": str(profile.bundle_id), "action": "none",
                    "verdict": "check_failed", "detail": f"{type(exc).__name__}: {exc}"}

    def _post_run_terminate(self, profile: AppProfile, run_dir: Path, ports: Any | None,
                            pre_run: dict[str, Any] | None, *,
                            release_fallback: dict[str, Any] | None = None,
                            on_release_wait: Callable[[], None] | None = None,
                            frida_attached: bool = True) -> None:
        """先确认 Frida 释放；等待超限时终止本 Run 的 App，再验证资源并落证据。

        若本次 Run 未发生 Frida attach 且 Run 前残留检测干净，则跳过无必要的 ``app_terminate``
        并记录干净收尾证据，保留活进程供后续复用或只读核验。"""
        if ports is None:
            return
        from autophone.execution import app_frida_hygiene as hygiene

        post_run: dict[str, Any] | None
        interrupted = sys.exc_info()[0]
        if interrupted is not None and issubclass(interrupted, (KeyboardInterrupt, SystemExit)):
            post_run = {"bundle_id": str(profile.bundle_id), "result": "skipped",
                        "reason": f"interrupted: {interrupted.__name__}"}
        else:
            post_run = dict(release_fallback) if release_fallback else None
            guard = StageGuard.current()
            if guard is not None:
                # 等待期间保留当前 owner token，必要时仍能终止自己的 App。
                # transition 随后验证所有资源；等待超限不代表释放成功。
                try:
                    if (not release_fallback and on_release_wait is not None
                            and not guard.wait_for_idle(FRIDA_RELEASE_GRACE_S)):
                        on_release_wait()
                        post_run = dict(release_fallback) if release_fallback else None
                    guard.transition("execute")
                    if post_run is not None:
                        post_run["release_verified_at"] = _now()
                except Exception as exc:  # noqa: BLE001 - quiesce 失败由 collect 切换上报与隔离
                    post_run = {"result": "skipped", **(post_run or {}), "bundle_id": str(profile.bundle_id),
                                "release_verification": "failed",
                                "reason": f"quiesce_failed: {type(exc).__name__}: {exc}"}
                    logger.error("Run 后终止被测 App 跳过：Frida 资源未确认释放（%s）", exc)
            pre_run_clean = (
                isinstance(pre_run, Mapping)
                and pre_run.get("action") == "none"
                and pre_run.get("verdict") in {"clean", "not_running"}
            )
            if post_run is None and not frida_attached and pre_run_clean:
                post_run = hygiene.post_run_skipped_no_frida(
                    str(profile.bundle_id),
                    app_pid=ports.app_pid,
                )
            elif post_run is None:
                try:
                    post_run = hygiene.post_run_terminate(
                        str(profile.bundle_id), app_pid=ports.app_pid, terminate=ports.terminate,
                        last_detach_at=getattr(self, "_crash_trap_stopped_at", None))
                except Exception as exc:  # noqa: BLE001 - 终止异常不改变 Run 结论
                    logger.error("Run 后终止被测 App 异常：%s: %s", type(exc).__name__, exc)
                    post_run = {"bundle_id": str(profile.bundle_id), "result": "failed",
                                "detail": f"{type(exc).__name__}: {exc}"}
        try:
            hygiene.write_evidence(run_dir, pre_run=pre_run, post_run=post_run)
        except Exception:  # noqa: BLE001 - 证据落盘失败不改变 Run 结论
            logger.warning("App Frida 残留处置证据落盘失败", exc_info=True)

    @contextmanager
    def _crash_trap_scope(self, spec: dict[str, Any], profile: AppProfile, run_dir: Path, *,
                          need_frida: bool | None = None,
                          on_release_wait: Callable[[], None] | None = None):
        """execute 阶段的崩溃兜底：起 crash_trap 后台会话，结束时停并把捕获数落 run 日志/摘要。
        全程 fail-open——工厂返回 None（开关关/无 frida）或起停抛错都不影响回归收口。"""
        session = None
        self._crash_trap_stopped_at = None
        try:
            if self.crash_trap_factory is _default_crash_trap_factory:
                session = _default_crash_trap_factory(
                    profile, spec, run_dir, need_frida=need_frida,
                )
            else:
                session = self.crash_trap_factory(profile, spec, run_dir)
        except Exception:  # noqa: BLE001 - 构造失败不拦回归
            logger.debug("crash_trap 构造失败（忽略）", exc_info=True)
            session = None
        if session is None:
            yield None
            return
        activity = _RUN_FRIDA_ACTIVITY.get()
        if activity is not None:
            activity["crash_trap_opened"] = True
        try:
            session.start()
        except Exception:  # noqa: BLE001
            logger.debug("crash_trap 启动失败（忽略）", exc_info=True)
            yield None
            return
        try:
            from autophone.observation.frida.crash_trap import crash_trap_scope

            with crash_trap_scope(session):
                yield session
        finally:
            try:
                if on_release_wait is not None and hasattr(session, "request_stop"):
                    session.request_stop()
                    if not session.wait_stopped(FRIDA_RELEASE_GRACE_S):
                        on_release_wait()
                summary = session.stop()
                self._crash_trap_stopped_at = _now()
                crashes = summary.get("crashes") or []
                if crashes:
                    top = crashes[0]
                    pcf = top.get("pc_frame") or {}
                    where = (f"{pcf.get('module')}+{pcf.get('offset')}"
                             if pcf.get("offset") else top.get("pc"))
                    logger.error("crash_trap 捕获 %d 次原始崩溃！首个 type=%s @ %s fault=%s（详见 %s）",
                                 len(crashes), top.get("type"), where, top.get("fault_address"),
                                 summary.get("events_path"))
                elif summary.get("available") is False:
                    logger.debug("crash_trap 未挂（%s）", summary.get("error"))
                else:
                    logger.info("crash_trap 收口：本 run 无崩溃捕获（attach %d 次）",
                                len(summary.get("attaches") or []))
            except Exception:  # noqa: BLE001 - 停会话失败不拦收口
                logger.debug("crash_trap 停止失败（忽略）", exc_info=True)

    def _run_regression(
        self, spec, profile, run_id, run_dir, device,
    ) -> RunHandle:
        from autophone.execution.closure_journal import ClosureJournal
        with ClosureJournal(run_dir).execution_owner(run_id):
            return self._run_regression_owned(spec, profile, run_id, run_dir, device)

    def _run_regression_owned(
        self,
        spec: dict[str, Any],
        profile: AppProfile,
        run_id: str,
        run_dir: Path,
        device: str,
    ) -> RunHandle:
        collection_frozen = False
        # run 启动即钉住每条用例的内容版本：先读 YAML 建快照，后续执行/编辑都不影响
        # 本 run 写进 result 的 revision——这样「R67 绿了」能回答绿的是哪一版。
        case_meta = self._case_meta_snapshot(profile)

        self._set_active(run_id, {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "app": str(spec["app"]),
            "device": device,
            "type": str(spec["type"]),
            "status": "queued",
            "queued_at": _now(),
        })

        timestamps: dict[str, str] = {"queued_at": _now()}
        phase_status = {
            "provision": {"status": "ok", "started_at": _now(), "ended_at": _now()},
            "execute": {"status": "skipped"},
            "collect": {"status": "skipped"},
        }

        # 队列登记不在这里做——它必须在主进程、且覆盖所有 type，见 submit()。
        # Context stays live through the complete finalization path.  A worker's
        # outer dispatch scope covers this one too, so an exception after report
        # generation still carries its run route and cannot disappear from logs.
        from autophone.record.recording import defer_materialization

        log_stack = ExitStack()
        recording_jobs = log_stack.enter_context(defer_materialization())
        log_stack.enter_context(bind_run(run_id, run_dir=run_dir, stage="run"))
        # Standalone tests/tools that did not call setup_logging still retain the
        # old direct file behaviour. Web/CLI parents use the root run router; a
        # worker sends its records to that parent instead of writing concurrently.
        if not has_run_router() and not is_worker_logging():
            log_stack.enter_context(run_logfile(run_dir / "run.log"))
        logger.info("run 入队 %s app=%s device=%s type=%s",
                    run_id, spec["app"], device, spec["type"])

        pending_error: BaseException | None = None
        try:
            bundle: ExecutionBundle | PendingPhase2Collection | None = None
            from autophone.execution.closure_journal import ClosureJournal
            closure = ClosureJournal(run_dir)
            if not closure.is_assignment_bound(run_id):
                closure.open(run_id=run_id, device=device)
            else:
                from autophone.execution.closure_journal import ClosureConflict
                existing = closure.read()
                if existing['closure_state'] != 'open' or existing['run_id'] != run_id or existing['device'] != device:
                    raise ClosureConflict('Assignment closure is not an unused matching intent')
            with self._screen_execution(
                device=device, run_id=run_id, run_dir=run_dir,
                spec=spec, profile=profile,
            ):
                self._crash_trap_stopped_at = None
                self._update_active(run_id, status="running")
                timestamps["execute_started_at"] = _now()
                phase_status["execute"] = {"status": "ok", "started_at": timestamps["execute_started_at"]}
                # BL-083 第八轮 (b)：crash_trap 与准备阶段 attach 之前，被测 App 带 Frida 残留时先冷重启。
                hygiene_ports = self._app_hygiene_ports(spec, profile, run_dir)
                hygiene_pre = self._pre_run_residue_check(profile, hygiene_ports)
                release_fallback: dict[str, Any] = {}

                def terminate_for_release() -> None:
                    if release_fallback or hygiene_ports is None:
                        return
                    interrupted = sys.exc_info()[0]
                    if interrupted is not None and issubclass(interrupted, (KeyboardInterrupt, SystemExit)):
                        return
                    from autophone.execution import app_frida_hygiene as hygiene

                    logger.warning("Frida 释放等待超过 %.1fs，终止本 Run 被测 App 后继续验证释放：%s",
                                   FRIDA_RELEASE_GRACE_S, profile.bundle_id)
                    release_fallback.update(hygiene.post_run_terminate(
                        str(profile.bundle_id), app_pid=hygiene_ports.app_pid,
                        terminate=hygiene_ports.terminate, last_detach_at=None))
                    release_fallback["release_fallback"] = "frida_detach_wait"

                frida_activity: dict[str, Any] = {
                    "evaluated": False,
                    "need_frida": False,
                    "rig_frida_probed": False,
                    "capture_opened": False,
                    "crash_trap_opened": False,
                    "frida_inproc_ax": _profile_ax_uses_frida(profile),
                }
                activity_token = _RUN_FRIDA_ACTIVITY.set(frida_activity)
                # 回归准备阶段独占 Frida 就绪探测。开始执行用例后再启动崩溃捕获。
                try:
                    with ExitStack() as crash_capture:
                        def start_crash_capture() -> None:
                            effective_need_frida = (
                                bool(frida_activity["need_frida"])
                                if frida_activity["evaluated"]
                                else None
                            )
                            crash_capture.enter_context(self._crash_trap_scope(
                                spec, profile, run_dir,
                                need_frida=effective_need_frida,
                                on_release_wait=terminate_for_release,
                            ))

                        try:
                            if self._uses_default_executor:
                                bundle = self._execute_phase2(
                                    spec, profile, run_dir, before_cases=start_crash_capture)
                            else:
                                start_crash_capture()
                                bundle = self.executor(spec, profile, run_dir)
                        except KeyboardInterrupt:
                            # 用户中断（Ctrl-C）：标记为 error 并传播中断信号
                            phase_status["execute"]["status"] = "failed"
                            phase_status["execute"]["ended_at"] = _now()
                            self._update_active(run_id, status="error")
                            logger.warning("run %s 被用户中断", run_id)
                            raise
                        except Exception as e:
                            # 任何异常（包括 runner 内部错误）：记录详细信息并标记为 error
                            phase_status["execute"]["status"] = "failed"
                            phase_status["execute"]["ended_at"] = _now()
                            self._update_active(run_id, status="error")
                            logger.error("run %s executor 异常：%s", run_id, type(e).__name__, exc_info=True)
                            raise
                        timestamps["execute_ended_at"] = _now()
                        phase_status["execute"]["ended_at"] = timestamps["execute_ended_at"]
                finally:
                    _RUN_FRIDA_ACTIVITY.reset(activity_token)
                    frida_attached = (
                        not frida_activity["evaluated"]
                        or bool(frida_activity["need_frida"])
                        or bool(frida_activity["rig_frida_probed"])
                        or bool(frida_activity["capture_opened"])
                        or bool(frida_activity["crash_trap_opened"])
                        or bool(frida_activity["frida_inproc_ax"])
                    )
                    # 正常在 detach 后终止；BL-244 等待超限时已执行终止，仍须验证释放。
                    self._post_run_terminate(
                        profile, run_dir, hygiene_ports, hygiene_pre,
                        release_fallback=release_fallback,
                        on_release_wait=terminate_for_release,
                        frida_attached=frida_attached,
                    )
            from autophone.observation.telemetry import freeze_telemetry_inputs
            frozen_inputs = {
                "kind": "run-service-outcome/v1",
                "run_spec": copy.deepcopy(spec),
                "telemetry_required": (plane_mode(spec, "telemetry") != "disabled"
                                       and bool((profile.raw.get("capabilities") or {}).get("telemetry"))),
                "execution_outcome": self.assemble_result(
                    spec, profile, bundle.phase2_result, timestamps, phase_status,
                    case_meta=case_meta,
                ),
            }
            input_error = None
            try:
                frozen_inputs['pending_collection'] = (
                    bundle.snapshot(run_dir) if isinstance(bundle, PendingPhase2Collection) else None)
                frozen_inputs['telemetry_inputs'] = freeze_telemetry_inputs(
                    profile_raw=profile.raw, spec=spec, run_dir=run_dir, timestamps=timestamps,
                    case_docs=bundle.case_docs if isinstance(bundle, PendingPhase2Collection) else (),
                ) if frozen_inputs['telemetry_required'] else None
            except Exception as exc:
                input_error = exc
                frozen_inputs['freeze_error'] = f'{type(exc).__name__}: {exc}'
            closure.freeze(frozen_inputs, expected_version=closure.read()['version'])
            collection_frozen = True
            # 出队不在这里做：worker 可能在子进程里，改到的是它自己那份队列。
            # 主进程的出队见 submit()（同步路径）与 _submit_background（后台路径）。
            phase_status["collect"] = {
                "status": "ok",
                "started_at": timestamps["execute_ended_at"],
            }
            assert bundle is not None
            try:
                if input_error is not None:
                    raise input_error
                if isinstance(bundle, PendingPhase2Collection):
                    bundle = PendingPhase2Collection.restore(
                        closure.read()["frozen_inputs"]["pending_collection"], run_dir)
                    bundle = self._materialize_phase2(bundle, run_dir)
                telemetry_json, telemetry_payload = self._write_telemetry(
                    run_dir=run_dir,
                    spec=spec,
                    profile=profile,
                    phase2_result=bundle.phase2_result,
                    timestamps=timestamps,
                )
            except Exception as exc:
                phase_status["collect"]["status"] = "failed"
                timestamps["collect_ended_at"] = _now()
                phase_status["collect"]["ended_at"] = timestamps["collect_ended_at"]
                result = self.assemble_result(
                    spec, profile, bundle.phase2_result, timestamps, phase_status,
                    case_meta=case_meta,
                )
                self.persist_run_result(run_dir, result)
                write_artifact_manifest(run_dir, run_id, default_phase2_artifacts(run_dir))
                raise RunCollectionError(
                    f"collect failed: {type(exc).__name__}: {exc}",
                    run_result=result, run_dir=run_dir,
                ) from exc
            timestamps["collect_ended_at"] = _now()
            phase_status["collect"]["ended_at"] = timestamps["collect_ended_at"]
            bundle = ExecutionBundle(
                phase2_result=bundle.phase2_result,
                result_json=bundle.result_json,
                report_md=bundle.report_md,
                report_html=bundle.report_html,
                cases_path=bundle.cases_path,
                coverage_json=bundle.coverage_json,
                coverage_html=bundle.coverage_html,
                telemetry_json=telemetry_json,
                telemetry_payload=telemetry_payload,
            )
            result = self.assemble_result(
                spec,
                profile,
                bundle.phase2_result,
                timestamps,
                phase_status,
                telemetry=telemetry_payload,
                case_meta=case_meta,
            )
            self.persist_run_result(run_dir, result)
            manifest_path = write_artifact_manifest(
                run_dir,
                run_id,
                default_phase2_artifacts(run_dir),
            )
            logger.info("run 完成 %s status=%s", run_id, result.get("status"))
            return RunHandle(run_id=run_id, run_dir=run_dir, result=result, manifest_path=manifest_path)
        except BaseException as exc:
            # This includes post-execute collection/manifest failures.  Previously
            # it escaped after the run handler had been removed, leaving only a
            # misleading minimal result in the dashboard.
            pending_error = exc
            if isinstance(exc, Exception):
                logger.exception("run %s 收口异常", run_id)
            raise
        finally:
            # Executor exceptions still retain the frozen recording. This
            # finally runs only after the ScreenLock context has exited.
            if not collection_frozen:
                for recorder, frozen in recording_jobs:
                    recorder.materialize(frozen)
            self._clear_active(run_id)
            # 执行期进度文件只服务实时左栏，run 收口（成功/出错/中止皆然）后即清——终态左栏
            # 改以 platform-run-result.json 的 cases 为准。fail-open：删不掉不影响收口。
            try:
                (run_dir / "run-progress.json").unlink(missing_ok=True)
            except OSError:
                pass
            log_stack.close()
            if (run_dir / 'artifact-manifest.json').exists() and not is_worker_logging():
                # Include the completion/error record after all Run-scoped logging.
                # Spawned worker logs are drained and registered by the parent watcher.
                try:
                    append_artifacts(run_dir, [artifact for artifact in default_phase2_artifacts(run_dir)
                                               if artifact.kind == 'log'])
                except Exception as manifest_error:
                    detail = f'Run log manifest finalization failed: {manifest_error}'
                    logger.error('%s: run=%s', detail, run_id, exc_info=True)
                    if pending_error is not None:
                        pending_error.add_note(detail)
                    else:
                        raise RunCollectionError(detail, run_result=result, run_dir=run_dir) from manifest_error

    @staticmethod
    def _requires_screen_lock(spec: dict[str, Any], profile: AppProfile) -> bool:
        """这条 run 是否会竞争设备 ScreenLock。

        regression 恒碰屏；verification 只有真 agent 执行器启用时碰屏；operation 以
        ``device_prep`` 为单一判据（内置碰屏操作或 typed IR）。只做 API/解析校验的任务
        不进设备等待队列，因为它们不会阻塞后续屏幕任务。
        """
        if spec.get("candidate_ref") is not None or plane_mode(spec, "api") == "controlled":
            return True
        run_type = str(spec.get("type") or "")
        if run_type == "regression":
            return True
        if run_type == "verification":
            return bool(os.environ.get("AUTOPHONE_VERIFICATION_AGENT_CMD"))
        if run_type != "operation":
            return False
        declaration = spec.get("operation") or {}
        name = str(declaration.get("name") or "")
        op_id = str(declaration.get("draft_id") or "") or None
        return device_prep(name, raw_steps_of(profile, name, op_id=op_id), app=profile.app) is not None

    @staticmethod
    def _screen_started_path(run_dir: Path) -> Path:
        return run_dir / ".screen-execution-started.json"

    @contextmanager
    def _screen_execution(
        self, *, device: str, run_id: str, run_dir: Path, spec=None, profile=None,
    ):
        """拿到 ScreenLock 后留下跨进程可见的永久出队标记。

        只靠 ``lock.status`` 临时隐藏 holder 会有回跳：锁释放后、collect/落盘尚未结束前，
        同一 run 会再次出现在「等待执行」。marker 从首次拿锁一直保留到主进程清队列，
        status() 因而能稳定区分 waiting 与 already-started。
        """
        with self.lock.acquire(device=device, run_id=run_id):
            guard = StageGuard.current()
            try:
                if profile is not None:
                    ensure_ax_backend_supported(profile)
                if spec is not None and profile is not None:
                    from autophone.knowledge.trial_confirmation import validate_trial_confirmation

                    validate_trial_confirmation(spec, profile)
                if guard is not None:
                    guard.transition("execute")
                if spec is not None and profile is not None:
                    from autophone.execution.candidate_lifecycle import activate_run_candidate
                    activate_run_candidate(spec, profile, run_dir)
                    from .world_identity import capture_world
                    capture_world(spec, profile, run_dir)
                if self.location_quiesce_fn is not None and not self.location_quiesce_fn(
                    device, 5.0
                ):
                    raise RuntimeError(
                        f"location sessions did not quiesce for device {device!r}"
                    )
                write_json(self._screen_started_path(run_dir), {
                    "run_id": run_id,
                    "device": device,
                    "started_at": _now(),
                })
                yield
            finally:
                if guard is not None:
                    try:
                        guard.transition("collect")
                    except StageQuiesceError as exc:
                        self.lock.quarantine(device=device, owner_id=run_id, failures=exc.failures)
                        raise

    def _submit_verification(
        self,
        spec: dict[str, Any],
        profile: AppProfile,
        run_id: str,
        run_dir: Path,
    ) -> RunHandle:
        queued_at = _now()
        verification = load_verification_spec(spec, profile)
        verification, execute_phase = self._run_device_executor(
            spec, run_id, run_dir, queued_at,
            active=bool(os.environ.get("AUTOPHONE_VERIFICATION_AGENT_CMD")),
            # 内置 verify-agent（AGENT_CMD=builtin:verify-agent）在本线程、ScreenLock 与 StageGuard
            # execute 阶段内运行；外部命令仍以子进程运行（BL-067 活机入口 (a)）。
            run=lambda: run_verification_executor(
                run_dir, verification, agent=(spec.get("verification") or {}).get("agent"),
            ),
            profile=profile,
        )
        write_verification_outputs(run_dir, verification)
        status = str(verification.get("status") or "pending_agent")
        platform_status = _VSTATUS_TO_STATUS.get(status, "pending")
        ui_verdict = _VSTATUS_TO_VERDICT.get(status, "pending")
        result = {
            "schema_version": "v1",
            "run_id": run_id,
            "type": "verification",
            "app": spec["app"],
            "device": spec["device"],
            "package": self._package_snapshot(run_dir, spec),
            "status": platform_status,
            "planes": {
                "ui": {
                    "mode": "advisory",
                    "verdict": ui_verdict,
                    "detail": str(verification.get("detail") or ""),
                },
                "function": {"mode": "disabled", "verdict": "not_configured"},
                "api": {"mode": "disabled", "verdict": "not_configured"},
                "telemetry": {"mode": "disabled", "verdict": "not_configured"},
            },
            "cases": [
                {
                    "id": str(checkpoint["id"]),
                    "status": _VSTATUS_TO_STATUS.get(str(checkpoint.get("status") or "pending"), "pending"),
                    "planes": {
                        "ui": {
                            "mode": "advisory",
                            "verdict": _VSTATUS_TO_VERDICT.get(str(checkpoint.get("status") or "pending"), "pending"),
                            "detail": str(checkpoint.get("detail") or checkpoint["title"]),
                        }
                    },
                }
                for checkpoint in verification.get("checkpoints", [])
            ],
            "artifacts": "artifact-manifest.json",
            "lock_phase": {
                "provision": {"status": "ok", "started_at": queued_at, "ended_at": queued_at},
                "execute": execute_phase,
                "collect": {"status": "ok", "started_at": queued_at, "ended_at": _now()},
            },
            "timestamps": {
                "queued_at": queued_at,
                "collect_ended_at": _now(),
            },
        }
        if profile.env is not None:
            result["env"] = profile.env
        if spec.get("store_context") is not None:
            result["store_context"] = copy.deepcopy(spec["store_context"])
        self.persist_run_result(run_dir, result)
        manifest_path = self._write_verification_manifest(run_dir, run_id)
        return RunHandle(run_id=run_id, run_dir=run_dir, result=result, manifest_path=manifest_path)

    def _run_device_executor(
        self,
        spec: dict[str, Any],
        run_id: str,
        run_dir: Path,
        queued_at: str,
        *,
        active: bool,
        run: Any,
        profile: AppProfile | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        """跑可能碰屏的执行器。active=True（真 agent 命令）→ 进 ScreenLock execute phase 串行
        （roadmap §七 屏幕串行不变量；审查点 3）；active=False（无执行器/纯结果注入，不碰屏）→
        直接跑、execute 标 skipped。返回 (执行器结果, lock_phase.execute)。"""
        if not active and spec.get("candidate_ref") is None:
            return run(), {"status": "skipped"}
        started = _now()
        # 与 regression 同一把 self.lock：若回归 run 持锁，这里 flock 阻塞直到释放——单 VM 串行。
        with self._screen_execution(
            device=str(spec["device"]), run_id=run_id, run_dir=run_dir,
            spec=spec, profile=profile,
        ):
            if profile is not None:
                self._probe_build_identity(profile, run_dir, ready_evidence=None)
            result = run()
        return result, {"status": "ok", "started_at": started, "ended_at": _now()}

    def _write_verification_manifest(self, run_dir: Path, run_id: str) -> Path:
        verification_artifacts = [
            Artifact("run-spec.json", "other", share_policy="forbidden"),
            Artifact("run-submission.json", "other", retention="local_only", share_policy="forbidden"),
            Artifact("build-identity.json", "other", retention="local_only", share_policy="forbidden"),
            Artifact("world-identity.json", "other", retention="local_only", share_policy="forbidden"),
            Artifact("platform-run-result.json", "report"),
            Artifact("verification-result.json", "report"),
            Artifact("verification-report.html", "report", share_policy="attach_allowed"),
        ]
        if (run_dir / "verification-agent-input.json").exists():
            verification_artifacts.append(
                Artifact("verification-agent-input.json", "other", retention="local_only", share_policy="forbidden")
            )
        if (run_dir / "verification-agent-output.json").exists():
            verification_artifacts.append(
                Artifact("verification-agent-output.json", "other", retention="local_only", share_policy="forbidden")
            )
        if (run_dir / "verification-agent-call.json").exists():
            # 内置执行器的单次调用留痕（AiResult：device_tool_trace / cost_usd / usage / error_code）。
            verification_artifacts.append(
                Artifact("verification-agent-call.json", "other", retention="local_only", share_policy="forbidden")
            )
        verification_path = run_dir / "verification-result.json"
        try:
            verification = json.loads(verification_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            verification = {}
        evidence_paths: set[str] = set()
        for checkpoint in verification.get("checkpoints") or []:
            if not isinstance(checkpoint, dict):
                continue
            for value in checkpoint.get("evidence") or []:
                rel = str(value or "")
                path = Path(rel)
                if not rel or path.is_absolute() or ".." in path.parts or rel in evidence_paths:
                    continue
                target = run_dir / path
                if not target.is_file() or target.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                    continue
                evidence_paths.add(rel)
                # AI 维度 allowed（2026-09-07 裁定）：验证单 checkpoint 截图与用例截图同口径，可不经
                # --allow-raw 单独进净化包；retention / share_policy 不变（只本机、禁止外发）。
                verification_artifacts.append(Artifact(
                    rel,
                    "screenshot",
                    sensitivity="sensitive",
                    redaction_status="raw",
                    retention="local_only",
                    share_policy="forbidden",
                    ai_policy="allowed",
                ))
        return write_artifact_manifest(run_dir, run_id, verification_artifacts)

    def _submit_operation(
        self,
        spec: dict[str, Any],
        profile: AppProfile,
        run_id: str,
        run_dir: Path,
    ) -> RunHandle:
        queued_at = _now()
        if plane_mode(spec, "api") == "required":
            raise ContractError(
                "type=operation 的 API plane 当前只支持 advisory capture；"
                "尚无 Operation 级 API oracle，不能把无断言抓取冒充 required 通过"
            )
        operation = load_operation_spec(spec, profile)
        # Checkpoint evidence needs the frozen build label during execution.
        operation["build"] = profile.build_label
        self._assert_operation_store_context(spec)
        self._bind_operation_store_context(operation, spec)
        name = str((operation.get("operation") or {}).get("name") or "")
        op_id = str((operation.get("operation") or {}).get("id") or "")
        # 原始 steps（typed IR 时可被 dispatcher 合成执行器真跑）——既决定碰不碰屏，又喂执行器。
        raw_steps = raw_steps_of(profile, name, op_id=op_id)
        # 倍速回放（record-replay-design §倍速回放）：speed 是 executor 控制项（独立于 with 业务入参），
        # timing 按操作 provenance.from 找录制 trace 重算 post-gap、与 raw_steps 等长对齐（失配 fail-open
        # → None → 均匀 DWELL）。两者只对 typed-IR 合成执行器生效，内置执行器忽略。
        try:
            replay_speed = replay_timing.validate_speed((spec.get("operation") or {}).get("replay_speed"))
        except ValueError as exc:
            raise ContractError(str(exc)) from exc
        op_def = raw_operation_def(profile, name, op_id=op_id)
        provenance = op_def.get("provenance") or {}
        recording_from = provenance.get("from")
        step_events = provenance.get("step_events")
        replay_post_gaps = replay_timing.resolve_post_gaps(
            recording_from, len(raw_steps),
            step_events=step_events if isinstance(step_events, list) else None,
            steps=raw_steps,   # 没有映射的老 operation：能证明的那种由 trace 自己补出来
        )
        screen_runtime = self._operation_screen_runtime(profile, op_def)
        operation_runtime = (
            {"store_context": spec["store_context"]}
            if spec.get("store_context") is not None else None
        )
        prep = device_prep(name, raw_steps, app=profile.app)  # None = 不碰屏 → 内联跑、execute 标 skipped
        if prep is None and (spec.get("candidate_ref") is not None or plane_mode(spec, "api") == "controlled"):
            prep = {}  # Candidate activation and controlled capture require device execution.
        if prep is None:
            # 中止协作点 ①（启动前，BL-062）：不碰屏的 Operation 不排 ScreenLock，执行器前读一次。
            abort_reason = run_abort_reason(run_dir)
            if abort_reason:
                operation = _aborted_before_start(operation, abort_reason)
            else:
                operation = run_operation_executor(run_dir, operation, raw_steps=raw_steps,
                                                   replay_speed=replay_speed, replay_timing=replay_post_gaps,
                                                   app=profile.app, runtime_context=operation_runtime,
                                                   root=getattr(profile, "root", None) or CODE_ROOT,
                                                   abort_check=lambda: run_abort_reason(run_dir),
                                                   **screen_runtime)
            execute_phase = {"status": "skipped"}
            api_mode = plane_mode(spec, "api")
            api_plane = (
                {"mode": "disabled", "verdict": "not_configured"}
                if api_mode == "disabled"
                else {
                    "mode": api_mode,
                    "verdict": "skipped",
                    "detail": "该 Operation 不碰设备，未建立 App 进程内 API 抓取会话",
                }
            )
        else:
            operation, execute_phase, api_plane = self._run_operation_on_device(
                spec, profile, run_id, run_dir, operation, prep, raw_steps,
                replay_speed=replay_speed, replay_timing=replay_post_gaps,
                runtime_context=operation_runtime,
                **screen_runtime,
            )
        if plane_mode(spec, "api") == "controlled":
            api_plane["scenario"] = spec["planes"]["api"]["scenario"]
        # 把本 run 的回放倍速钉进 operation-result（缺省 1.0）：转正门据此只计 1x（倍速回放仅供调试/取数）。
        operation["replay_speed"] = replay_speed
        if spec.get("store_context") is not None:
            operation["store_context"] = copy.deepcopy(spec["store_context"])
        runtime_actions = list(operation.get("action_trace") or [])
        write_operation_outputs(run_dir, operation)
        execute_started_at = str(execute_phase.get("started_at") or queued_at)
        execute_ended_at = str(execute_phase.get("ended_at") or _now())
        try:
            started_dt = datetime.fromisoformat(execute_started_at.replace("Z", "+00:00"))
            ended_dt = datetime.fromisoformat(execute_ended_at.replace("Z", "+00:00"))
            duration_ms = max(0.0, (ended_dt - started_dt).total_seconds() * 1000)
        except (TypeError, ValueError):
            duration_ms = 0.0
        from types import SimpleNamespace
        operation_phase2 = SimpleNamespace(outcomes=[
            SimpleNamespace(
                id=str((operation.get("operation") or {}).get("id") or "operation"),
                started_at=execute_started_at,
                duration_ms=duration_ms,
                actions=runtime_actions,
            )
        ])
        operation_timestamps = {
            "execute_started_at": execute_started_at,
            "execute_ended_at": execute_ended_at,
        }
        telemetry_payload: dict[str, Any] | None = None
        try:
            _, telemetry_payload = self._write_telemetry(
                run_dir=run_dir,
                spec=spec,
                profile=profile,
                phase2_result=operation_phase2,
                timestamps=operation_timestamps,
            )
        except Exception:  # noqa: BLE001 - operation readback is advisory
            logger.warning("operation dataTrace readback failed", exc_info=True)
        status = str(operation.get("status") or "pending_execution")
        platform_status = _OPSTATUS_TO_STATUS.get(status, "pending")
        ui_verdict = _OPSTATUS_TO_VERDICT.get(status, "pending")
        telemetry_plane = {"mode": "disabled", "verdict": "not_configured"}
        if telemetry_payload is not None:
            event_count = int(telemetry_payload.get("event_count") or 0)
            telemetry_plane = {
                "mode": "advisory",
                "verdict": "captured" if event_count else "pending",
                "detail": (
                    f"dataTrace matched {event_count} event(s)"
                    if event_count
                    else f"dataTrace status={telemetry_payload.get('status') or 'empty'}"
                ),
            }
        result = {
            "schema_version": "v1",
            "run_id": run_id,
            "type": "operation",
            "app": spec["app"],
            "device": spec["device"],
            "package": self._package_snapshot(run_dir, spec),
            "status": platform_status,
            # 操作只在 UI 面取证（做没做成）；功能/API/telemetry 不是操作的裁决面。
            "planes": {
                "ui": {"mode": "advisory", "verdict": ui_verdict, "detail": str(operation.get("detail") or "")},
                "function": {"mode": "disabled", "verdict": "not_configured"},
                "api": api_plane,
                "telemetry": telemetry_plane,
            },
            "steps": [
                {
                    "label": str(step.get("text") or f"步骤 {step['index']}"),
                    "state": _operation_step_state(step.get("status")),
                    "note": str(step.get("detail") or ""),
                    "time": str(step.get("time") or ""),
                }
                for step in operation.get("steps", [])
            ],
            "cases": [
                {
                    "id": f"S{step['index']}",
                    "item": str(step.get("text") or f"步骤 {step['index']}"),
                    "status": _VSTATUS_TO_STATUS.get(str(step.get("status") or "pending"), "pending"),
                    "planes": {
                        "ui": {
                            "mode": "advisory",
                            "verdict": _VSTATUS_TO_VERDICT.get(str(step.get("status") or "pending"), "pending"),
                            "detail": str(step.get("detail") or step.get("text") or ""),
                        }
                    },
                }
                for step in operation.get("steps", [])
            ],
            "artifacts": "artifact-manifest.json",
            "lock_phase": {
                "provision": {"status": "ok", "started_at": queued_at, "ended_at": queued_at},
                "execute": execute_phase,
                "collect": {"status": "ok", "started_at": queued_at, "ended_at": _now()},
            },
            "timestamps": {
                "queued_at": queued_at,
                "execute_started_at": execute_started_at,
                "execute_ended_at": execute_ended_at,
                "collect_ended_at": _now(),
            },
        }
        if profile.env is not None:
            result["env"] = profile.env
        if isinstance(operation.get("blocker"), dict):
            result["blocker"] = copy.deepcopy(operation["blocker"])
        if operation.get("status") in {"failed", "blocked", "error"}:
            from autophone.execution.failure_context import failure_fields
            result.update(failure_fields(operation.get("failure_stage", "not_recorded"), operation.get("failed_definition")))
        if spec.get("store_context") is not None:
            result["store_context"] = copy.deepcopy(spec["store_context"])
        self.persist_run_result(run_dir, result)
        manifest_path = self._write_operation_manifest(run_dir, run_id)
        return RunHandle(run_id=run_id, run_dir=run_dir, result=result, manifest_path=manifest_path)

    def _operation_screen_runtime(self, profile: AppProfile, op_def: dict[str, Any]) -> dict[str, Any]:
        expectations = op_def.get("screen_expectations")
        starts_with = op_def.get("screen_starts_with")
        if not expectations and not starts_with:
            return {}
        try:
            path = profile.screens_path()
        except ValueError as exc:
            raise ContractError(str(exc)) from exc
        if not path.exists():
            raise ContractError(f"operation 声明了 screen sidecar，但 screen asset 不存在：{path}")
        out: dict[str, Any] = {"screen_asset_path": path}
        if expectations:
            out["screen_expectations"] = expectations
        if starts_with:
            out["screen_starts_with"] = starts_with
        return out

    def _run_operation_on_device(
        self,
        spec: dict[str, Any],
        profile: AppProfile,
        run_id: str,
        run_dir: Path,
        operation: dict[str, Any],
        prep: dict[str, bool],
        raw_steps: list[Any] | None = None,
        *,
        replay_speed: float = 1.0,
        replay_timing: list[float] | None = None,
        screen_asset_path: str | Path | None = None,
        screen_expectations: dict[Any, str] | None = None,
        screen_starts_with: str | None = None,
        runtime_context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """碰屏 operation 的执行：进 ScreenLock execute phase 串行（与 regression/verification 同一
        把锁，§七 屏幕串行不变量），跑前按 prep 解锁 + 拉起 App（锁屏/App 不在前台会让执行器
        全失真，如 login 在锁屏上误判 already_in 假通过）。返回 (执行器结果, lock_phase.execute)。

        前置失败（解锁/launch blocker）→ 不跑 executor，操作收 status=error、execute 标 failed，
        诚实暴露「设备没准备好」而非让 do_login 在错误界面上假通过。

        当 run spec 请求 API plane 时，抓取会话与 Operation executor 位于同一个
        ScreenLock execute 窗口。advisory 抓取前置失败可降级为普通执行。
        controlled 抓取失败会使 Operation 失败。Operation 开始后不重复执行。"""
        started = _now()
        shot_dir = run_dir / "shots"
        shot_dir.mkdir(parents=True, exist_ok=True)
        api_mode = plane_mode(spec, "api")
        api_plane: dict[str, Any] = (
            {"mode": "disabled", "verdict": "not_configured"}
            if api_mode == "disabled"
            else {"mode": api_mode, "verdict": "pending", "detail": "API 抓取会话尚未执行"}
        )
        with self._screen_execution(
            device=str(spec["device"]), run_id=run_id, run_dir=run_dir,
            spec=spec, profile=profile,
        ):
            # 中止协作点 ①（启动前，BL-062）：排队态线程拿到 ScreenLock 后、解锁 / 拉起 App 前读信号。
            # 与用例回归 _execute_phase2_cases 同位置：此时尚未碰设备，直接收口为 aborted，不跑执行器。
            abort_reason = run_abort_reason(run_dir)
            if abort_reason:
                if api_mode != "disabled":
                    api_plane = {
                        "mode": api_mode,
                        "verdict": "skipped",
                        "detail": "run 已中止，未建立 API 抓取会话",
                    }
                return (
                    _aborted_before_start(operation, abort_reason),
                    {"status": "skipped", "started_at": started, "ended_at": _now()},
                    api_plane,
                )
            blocker: str | None = None
            if prep.get("unlock"):
                blocker = self._ensure_unlocked(shot_dir)
            if blocker is None and prep.get("launch") and api_mode != "controlled":
                blocker = self._launch_app(profile, shot_dir)
            if blocker is not None:
                out = dict(operation)
                out["status"] = "error"
                out["failure_stage"] = "rig_readiness"
                out["failed_definition"] = {"status": "unknown"}
                out["detail"] = f"设备前置失败，未执行操作：{blocker}"
                structured = launch_blocker(blocker)
                if structured is not None:
                    # 启动以 CaseBlocked 结束（如 SpringBoard 重启）：与 Operation 内阻断同口径。
                    out["status"] = "blocked"
                    out["blocker"] = structured
                if api_mode != "disabled":
                    api_plane = {
                        "mode": api_mode,
                        "verdict": "skipped",
                        "detail": "设备前置失败，未建立 API 抓取会话",
                    }
                return (
                    out,
                    {"status": "failed", "started_at": started, "ended_at": _now()},
                    api_plane,
                )

            self._probe_build_identity(
                profile,
                run_dir,
                ready_evidence={
                    "available": True,
                    "evidence": "operation device preparation passed",
                },
            )

            def execute_operation(capture_session: Any = None) -> dict[str, Any]:
                return run_operation_executor(
                    run_dir,
                    operation,
                    raw_steps=raw_steps,
                    replay_speed=replay_speed,
                    replay_timing=replay_timing,
                    screen_asset_path=screen_asset_path,
                    screen_expectations=screen_expectations,
                    screen_starts_with=screen_starts_with,
                    app=profile.app,
                    root=getattr(profile, "root", None) or CODE_ROOT,
                    runtime_context=runtime_context,
                    **({"capture_session": capture_session} if capture_session is not None else {}),
                    # 中止协作点 ②（执行中）：typed-IR 合成执行器逐步边界读信号，见即停。
                    # 已执行步骤的副作用不回滚。
                    abort_check=lambda: run_abort_reason(run_dir),
                )

            if api_mode == "controlled":
                from autophone.observation.controlled_capture import execute, wait_ready
                result, api_plane = execute(
                    run_dir, spec, profile, shot_dir, operation,
                    self.capture_session_factory, execute_operation, wait_ready,
                )
            elif api_mode == "disabled":
                result = execute_operation()
            else:
                from autophone.observation import capture as capture_mod

                try:
                    session = self.capture_session_factory(profile, spec, shot_dir)
                except Exception as exc:  # noqa: BLE001 capture is advisory for operation runs
                    logger.warning("Operation API 抓取会话构造失败，改走普通执行", exc_info=True)
                    session = None
                    api_plane = {
                        "mode": api_mode,
                        "verdict": "pending",
                        "detail": f"抓取会话构造失败：{type(exc).__name__}: {exc}",
                    }
                if session is None:
                    if api_plane.get("detail") == "API 抓取会话尚未执行":
                        api_plane = {
                            "mode": api_mode,
                            "verdict": "pending",
                            "detail": "抓取面未启用；Operation 已按普通路径执行",
                        }
                    result = execute_operation()
                else:
                    execution_started = False
                    result = None
                    session_error = ""
                    try:
                        with session:
                            if getattr(session, "spawn_gated", False):
                                ready = capture_mod.wait_for_spawn_ready(shot_dir, profile)
                                if not ready.get("ok"):
                                    raise RuntimeError(
                                        f"spawn 后屏幕未就绪：{ready.get('detail') or ready}"
                                    )
                            execution_started = True
                            result = execute_operation(capture_session=session)
                    except Exception as exc:  # noqa: BLE001 do not duplicate a started operation
                        session_error = f"{type(exc).__name__}: {exc}"
                        if not execution_started:
                            logger.warning("Operation API 抓取前置失败，改走普通执行：%s", session_error)
                            result = execute_operation()
                        elif result is None:
                            raise
                        else:
                            logger.warning(
                                "Operation 已执行，API 抓取收口失败；保留 Operation 结果且不重跑：%s",
                                session_error,
                            )
                    if execution_started:
                        try:
                            # Read after __exit__, including when cleanup raised after sealing.
                            merged = session.merged()
                            capture_mod.write_capture_jsonl(run_dir, list(merged))
                            events = getattr(session, "events", None)
                            if events is not None:
                                events_path = run_dir / "planes/api/private/events.jsonl"
                                events_path.parent.mkdir(parents=True, exist_ok=True)
                                with events_path.open("w", encoding="utf-8") as stream:
                                    for event in list(events):
                                        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
                            api_plane = {
                                "mode": api_mode,
                                "verdict": "captured",
                                "detail": f"已抓取 {len(merged)} 条 Operation 窗口接口调用（无断言）",
                            }
                            if session_error:
                                api_plane["detail"] += f"；会话收口异常：{session_error}"
                        except Exception as exc:  # noqa: BLE001 host persistence must not rerun operation
                            api_plane = {
                                "mode": api_mode,
                                "verdict": "pending",
                                "detail": f"Operation 已执行，但抓取产物持久化失败：{type(exc).__name__}: {exc}",
                            }
                            if session_error:
                                api_plane["detail"] += f"；会话收口异常：{session_error}"
                    elif session_error:
                        api_plane = {
                            "mode": api_mode,
                            "verdict": "pending",
                            "detail": f"未形成 API 抓取产物：{session_error}",
                        }
                    assert result is not None
        return (
            result,
            {"status": "failed" if api_mode == "controlled" and api_plane.get("verdict") != "pass" else "ok",
             "started_at": started, "ended_at": _now()},
            api_plane,
        )

    def _write_operation_manifest(self, run_dir: Path, run_id: str) -> Path:
        controlled = (run_dir / "planes/api/private/scenario.json").is_file()
        operation_artifacts = [
            Artifact("run-spec.json", "other", share_policy="forbidden"),
            Artifact("run-submission.json", "other", retention="local_only", share_policy="forbidden"),
            Artifact("build-identity.json", "other", retention="local_only", share_policy="forbidden"),
            Artifact("world-identity.json", "other", retention="local_only", share_policy="forbidden"),
            Artifact("platform-run-result.json", "report"),
            Artifact("operation-result.json", "report"),
            Artifact("operation-report.html", "report", share_policy="attach_allowed"),
        ]
        for name in ("operation-executor-input.json", "operation-executor-output.json"):
            if (run_dir / name).exists():
                operation_artifacts.append(
                    Artifact(name, "other", retention="local_only", share_policy="forbidden")
                )
        telemetry_artifacts = (
            ("planes/telemetry/events.json", "telemetry_events", "redacted", "shareable", "summary_only"),
            ("planes/telemetry/query-summary.json", "telemetry_query_summary", "redacted", "shareable", "summary_only"),
            ("planes/telemetry/raw.json", "telemetry_raw", "raw", "local_only", "forbidden"),
        )
        for rel, kind, redaction, retention, share_policy in telemetry_artifacts:
            if (run_dir / rel).exists():
                operation_artifacts.append(Artifact(
                    rel,
                    kind,
                    sensitivity="sensitive" if redaction == "raw" else "internal",
                    redaction_status=redaction,
                    retention=retention,
                    share_policy=share_policy,
                ))
        api_capture = run_dir / "planes" / "api" / "capture.jsonl"
        if api_capture.is_file():
            operation_artifacts.append(Artifact(
                "planes/api/capture.jsonl",
                "api_capture",
                sensitivity="sensitive",
                redaction_status="raw",
                retention="local_only",
                share_policy="forbidden",
                ai_policy="forbidden",
            ))
        for private_file in sorted((run_dir / "planes/api/private").rglob("*")):
            if not private_file.is_file() or private_file.is_symlink():
                continue
            operation_artifacts.append(Artifact(
                str(private_file.relative_to(run_dir)),
                "other",
                sensitivity="sensitive",
                redaction_status="raw",
                retention="local_only",
                share_policy="forbidden",
                ai_policy="forbidden",
            ))
        # create_order 执行器写到 run_dir/fixture/ 的造数产物：raw 含 PII（手机号/地址/坐标）→
        # 一律 local_only + forbidden；脱敏副本可分享摘要（对齐 §2.4 治理不变量）。
        raw_fixture = ("fixture/order_create_payload.json", "fixture/fixture_context.json",
                       "fixture/order_create_response.json")
        redacted_fixture = ("fixture/order_create_payload.redacted.json",
                            "fixture/fixture_context.redacted.json")
        for rel in raw_fixture:
            if (run_dir / rel).exists():
                operation_artifacts.append(Artifact(
                    rel, "fixture", sensitivity="sensitive", redaction_status="raw",
                    retention="local_only", share_policy="forbidden",
                ))
        for rel in redacted_fixture:
            if (run_dir / rel).exists():
                operation_artifacts.append(Artifact(
                    rel, "fixture", sensitivity="internal", redaction_status="redacted",
                    retention="shareable", share_policy="summary_only",
                ))
        # login 执行器把验证码裁图/登录页截图落 run_dir/shots/——设备画面含 PII（预填手机号/登录态）→
        # 逐文件登记为 sensitive/raw/local_only/forbidden（同 phase2 截图治理），禁止外发。
        shots_dir = run_dir / "shots"
        if shots_dir.exists():
            for shot in sorted(shots_dir.glob("**/*")):
                if not shot.is_file():
                    continue
                rel = shot.relative_to(run_dir).as_posix()
                kind = "screenshot" if shot.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} else "other"
                # controlled 截图禁止 AI 使用；自然采集沿用现有截图策略。
                operation_artifacts.append(Artifact(
                    rel, kind, sensitivity="sensitive", redaction_status="raw",
                    retention="local_only", share_policy="forbidden",
                    ai_policy="forbidden" if controlled else ("allowed" if kind == "screenshot" else ""),
                ))
        operation_artifacts.extend(a for a in default_phase2_artifacts(run_dir)
                                   if a.path.startswith(("planes/ax/private/", "operations/")))
        return write_artifact_manifest(run_dir, run_id, operation_artifacts)

    def _execute_phase2(self, spec: dict[str, Any], profile: AppProfile, run_dir: Path, *,
                        before_cases: Callable[[], None] | None = None) -> PendingPhase2Collection:
        from autophone.record.recording import defer_materialization

        with defer_materialization() as recordings:
            result, cases_path, case_docs = self._execute_phase2_cases(
                spec, profile, run_dir, before_cases=before_cases)
        report_meta = {
            "run_id": str(spec.get("run_id") or run_dir.name),
            "app": str(spec.get("app") or ""),
            "device": str(spec.get("device") or ""),
            "type": str(spec.get("type") or "regression"),
            "generated_at": _now(),
        }
        device_locale = _device_locale_meta(run_dir)
        if device_locale is not None:
            report_meta["device_locale"] = device_locale
        return PendingPhase2Collection(
            phase2_result=copy.deepcopy(result),
            cases_path=cases_path,
            case_docs=tuple(copy.deepcopy(case_docs)),
            report_meta=report_meta,
            recordings=tuple(recordings),
        )

    def _execute_phase2_cases(self, spec: dict[str, Any], profile: AppProfile, run_dir: Path, *,
                              before_cases: Callable[[], None] | None = None):
        from autophone.execution.engine.runner import CaseOutcome, RunResult, select_cases
        from autophone.execution.engine.cases_loader import load_cases
        from autophone.execution.contracts import blocker
        from autophone.execution.capability_requirements import plan_cases

        ax_backend = ensure_ax_backend_supported(profile)
        selector = spec.get("case_selector") or {}
        cases_path = profile.cases_path()
        case_docs = load_cases(cases_path)
        shot_dir = run_dir / "shots"
        trial_draft_recipes, all_confirmed, confirmed_case_ids, trial_data_isolation = (
            _trial_effect_context(spec, profile)
        )
        screen_asset_doc = self._screen_asset_doc(profile)
        # 中止协作点 ①（启动前）：排队态线程从 flock 醒来进这里即见标志——此时尚未启动 app /
        # 跑用例，零真副作用，直接收口为 aborted（→ status=blocked），不录屏不启动。
        abort_reason = run_abort_reason(run_dir)
        if abort_reason:
            activity = _RUN_FRIDA_ACTIVITY.get()
            if activity is not None:
                activity["evaluated"] = True
                activity["need_frida"] = False
            result = RunResult(aborted=True, abort_reason=abort_reason, ax_backend=ax_backend)
        else:
            selected_preview = select_cases(copy.deepcopy(case_docs), selector=selector)
            symbol_binding = (
                self._resolve_run_symbol_binding(spec, profile, run_dir)
                if self._selection_uses_symbols(profile, selected_preview, selector)
                else None
            )
            with self._draft_recipe_scope(
                profile,
                selector,
                spec=spec,
                symbol_binding=symbol_binding,
                screen_asset_doc=screen_asset_doc,
            ) as draft_recipe_identities:
                selected = select_cases(copy.deepcopy(case_docs), selector=selector)
                selected = self._bind_draft_recipe_trials(
                    profile,
                    selected,
                    draft_recipe_identities,
                    selector=selector,
                    symbol_binding=symbol_binding,
                    screen_asset_doc=screen_asset_doc,
                )
                # 区分力反向 Run：按声明对唯一被选 Case 的 Recipe 施加扰动（只改进程内计划）。
                negctl_request = spec.get("negative_control")
                negctl_case_id = (
                    str(negctl_request.get("case_id") or "").upper()
                    if isinstance(negctl_request, Mapping)
                    else ""
                )
                selected = bind_negative_control(
                    profile,
                    selected,
                    negctl_request,
                    symbol_binding=symbol_binding,
                    screen_asset_doc=screen_asset_doc,
                    trial_draft_recipes=trial_draft_recipes,
                    confirm_trial_effects=(
                        bool(negctl_case_id) and negctl_case_id in confirmed_case_ids
                    ),
                    trial_data_isolation=trial_data_isolation,
                )
                negative_ids = negative_control_case_ids(selected)
                # 黄金路径 Run（run-spec.golden_path）：核对声明摘要与 Case 归属，标记该 Case，
                # live Recipe 在此读取一次并冻结为本 Run 的执行步骤与身份来源。
                selected = bind_golden_path(
                    profile,
                    selected,
                    spec.get("golden_path"),
                    symbol_binding=symbol_binding,
                    screen_asset_doc=screen_asset_doc,
                )
                # 显式草稿试跑或黄金路径 Run 的用例若声明了业务 Fixture，Runner 在 setup 与主体
                # 之间执行其独立 Readback 并产出 fixture-readback.json；普通运行不绑定。
                selected = bind_fixture_readback_plans(profile, selected)
                execution_groups = None
                plan_blocks: list[dict[str, Any]] = []
                plan_name = spec.get("execution_plan")
                if plan_name == "current":
                    from autophone.execution.planning import execution_planner

                    plan = execution_planner(profile.app).build_current_plan(selected)
                    runnable_ids = {case_id for group in plan.groups for case_id in group.ids}
                    runnable_selection = [case for case in selected if str(case.get("id") or "") in runnable_ids]
                    execution_groups = [
                        {
                            "label": group.label,
                            "ids": list(group.ids),
                            **(
                                {"runtime_order": group.runtime_order}
                                if group.runtime_order else {}
                            ),
                            **(
                                {"requires_pass_case_id": group.requires_pass_case_id}
                                if group.requires_pass_case_id else {}
                            ),
                            **(
                                {"requires_duty_state": group.requires_duty_state}
                                if group.requires_duty_state else {}
                            ),
                        }
                        for group in plan.groups
                    ]
                    # The plan states why each case is deferred; the run only
                    # relays it.  Naming the gap here instead would collapse
                    # unrelated deferrals into one blocker code again.
                    plan_blocks = [
                        {"case_id": deferred.case_id, "blocker": deferred.blocker}
                        for deferred in plan.deferred
                    ]
                else:
                    runnable_selection = selected
                runnable, asset_blocks = self._admit_selected_cases(
                    profile,
                    runnable_selection,
                    trial_draft_recipes=trial_draft_recipes,
                    confirmed_case_ids=confirmed_case_ids,
                    trial_data_isolation=trial_data_isolation,
                    symbol_binding=symbol_binding,
                    screen_asset_doc=screen_asset_doc,
                )
                runnable = bind_operation_witnesses(profile, runnable)
                capability_blocks: list[dict[str, Any]] = []
                recipe_identity_candidates_by_id: dict[str, dict[str, Any]] = {}
                if not runnable:
                    activity = _RUN_FRIDA_ACTIVITY.get()
                    if activity is not None:
                        activity["evaluated"] = True
                        activity["need_frida"] = False
                    result = RunResult(aborted=False, abort_reason="", ax_backend=ax_backend)
                else:
                    api_mode = plane_mode(spec, "api")
                    required_cases = self._derive_case_requirements(
                        profile, runnable, api_plane_mode=api_mode,
                    )
                    # BL-186：执行前读取每条用例将要执行的 Recipe 内容一次，执行后按 Runner
                    # 实际执行的 steps 摘要核对，再把身份写入结果。不改变用例选择与执行。
                    try:
                        recipe_identity_candidates_by_id = recipe_identity_candidates(
                            profile, required_cases
                        )
                    except Exception:  # noqa: BLE001 - identity evidence never blocks a Run
                        logger.warning("Recipe 执行身份候选读取失败，本 Run 不记录执行身份", exc_info=True)
                        recipe_identity_candidates_by_id = {}
                    if api_mode == "required":
                        # Run-level required API evidence is a hard floor for
                        # every selected case, even if the recipe's normal lane
                        # is advisory.  This keeps the planner and guard in one
                        # capability contract rather than discovering it after a
                        # UI side effect has already happened.
                        for case in required_cases:
                            requirements = dict(case.get("requirements") or {})
                            requirements["api_capture"] = "required"
                            case["requirements"] = requirements
                    need_frida = _regression_needs_frida(spec, profile, required_cases)
                    activity = _RUN_FRIDA_ACTIVITY.get()
                    if activity is not None:
                        activity["evaluated"] = True
                        activity["need_frida"] = need_frida
                        if need_frida:
                            activity["rig_frida_probed"] = True
                    snapshot = self._prepare_rig(
                        profile, shot_dir, need_app=True, need_frida=need_frida)
                    availability = self._capability_availability(snapshot, profile=profile)
                    if availability["vm_uds"] and availability["app_ui"]:
                        self._probe_build_identity(
                            profile,
                            run_dir,
                            ready_evidence=snapshot.to_dict().get("app_ui"),
                        )
                        if symbol_binding is not None:
                            self._verify_probed_symbol_binding(
                                profile, spec, run_dir, symbol_binding
                            )
                    if not availability["vm_uds"] or not availability["app_ui"]:
                        missing = "vm_uds" if not availability["vm_uds"] else "app_ui"
                        capability_blocks = [
                            {
                                "case_id": str(case.get("id") or ""),
                                "blocker": blocker(
                                    "rig", f"{missing}_unavailable", capability=missing,
                                    recovery=snapshot.recovery_advice(),
                                ),
                            }
                            for case in required_cases
                        ]
                        result = RunResult(
                            aborted=True,
                            abort_reason=snapshot.recovery_advice(),
                            ax_backend=ax_backend,
                        )
                    else:
                        capability_plan = plan_cases(required_cases, availability)
                        runner_cases = [
                            item["case"] for item in capability_plan
                            if item["state"].startswith("runnable")
                        ]
                        capability_blocks = [
                            {"case_id": str(item["case"].get("id") or ""), "blocker": item["blocker"]}
                            for item in capability_plan
                            if item["state"] == "blocked_capability"
                        ]
                        if runner_cases:
                            if before_cases is not None:
                                before_cases()
                            result = self._run_cases(
                                spec, profile, run_dir, shot_dir, cases_path, selector,
                                selected_cases=runner_cases, execution_groups=execution_groups,
                                ax_backend=ax_backend,
                                trial_draft_recipes=trial_draft_recipes,
                                confirm_trial_effects=all_confirmed,
                                confirmed_trial_case_ids=confirmed_case_ids,
                                trial_data_isolation=trial_data_isolation,
                                symbol_binding=symbol_binding,
                                screen_asset_doc=screen_asset_doc,
                            )
                        else:
                            result = RunResult(aborted=False, abort_reason="", ax_backend=ax_backend)
                    result.rig_snapshot = snapshot
                    result.ax_backend = getattr(result, "ax_backend", None) or ax_backend
                    write_json(run_dir / "rig-snapshot.json", snapshot.to_dict())
                result.ax_backend = getattr(result, "ax_backend", None) or ax_backend
                result.outcomes = self._merge_preflight_outcomes(
                    selected, result.outcomes, asset_blocks + capability_blocks + plan_blocks, CaseOutcome)
                case_binding_by_id = {
                    str(case.get("id") or "").upper(): str(case["_symbol_binding_sha256"])
                    for case in runnable
                    if case.get("_symbol_binding_sha256") is not None
                }
                for case in selected:
                    key = str(case.get("id") or "").upper()
                    if key and key not in case_binding_by_id and case.get("_symbol_binding_sha256") is not None:
                        case_binding_by_id[key] = str(case["_symbol_binding_sha256"])
                for outcome in result.outcomes:
                    key = str(outcome.id).upper()
                    identity = draft_recipe_identities.get(key)
                    # 反向 Run 执行的是扰动后的 steps，不是冻结的草稿，不写 Trial 身份。
                    if (
                        identity is not None
                        and getattr(outcome, "exec_via", "") == "recipe"
                        and key not in negative_ids
                    ):
                        outcome.draft_recipe_identity = dict(identity)
                    if getattr(outcome, "binding_sha256", None) is None and key in case_binding_by_id:
                        outcome.binding_sha256 = case_binding_by_id[key]
                attach_recipe_identities(result.outcomes, recipe_identity_candidates_by_id)
                for outcome in result.outcomes:
                    bound_sha = getattr(outcome, "binding_sha256", None)
                    if bound_sha is not None and isinstance(getattr(outcome, "recipe_identity", None), dict):
                        outcome.recipe_identity["binding_sha256"] = str(bound_sha)
                attach_negative_control_records(result.outcomes, selected)
                self._record_confirmed_symbol_claims(
                    profile,
                    spec,
                    run_dir,
                    result.outcomes,
                    ax_backend=ax_backend,
                )
                write_fixture_readback_evidence(
                    run_dir, str(spec.get("run_id") or run_dir.name), result.outcomes
                )
                write_cleanup_readback_evidence(
                    run_dir, str(spec.get("run_id") or run_dir.name), result.outcomes
                )
                write_entity_readback_evidence(
                    run_dir, str(spec.get("run_id") or run_dir.name), result.outcomes
                )
        return result, cases_path, case_docs

    @staticmethod
    def _selection_uses_symbols(
        profile: AppProfile,
        cases: list[dict[str, Any]],
        selector: dict[str, Any],
    ) -> bool:
        import yaml
        from autophone.execution.engine.recipe import recipe_uses_symbols

        if not hasattr(profile, "recipes_dir"):
            return False
        draft_trial = selector_is_draft_trial(selector)
        pinned = {str(cid).upper() for cid in (selector.get("ids") or []) if str(cid)}
        for case in cases:
            cid = str(case.get("id") or "").upper()
            if not cid:
                continue
            candidates: list[Path] = []
            if draft_trial and cid in pinned:
                draft_path = profile.recipes_dir() / "drafts" / f"{cid.lower()}.yaml"
                if draft_path.is_file():
                    candidates.append(draft_path)
            else:
                live_path = profile.recipes_dir() / f"{cid.lower()}.yaml"
                if live_path.is_file():
                    candidates.append(live_path)
                elif case.get("status") == "draft":
                    draft_path = profile.recipes_dir() / "drafts" / f"{cid.lower()}.yaml"
                    if draft_path.is_file():
                        candidates.append(draft_path)
            for path in candidates:
                try:
                    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                except (OSError, yaml.YAMLError):
                    continue
                if isinstance(doc, Mapping) and recipe_uses_symbols(doc.get("steps")):
                    return True
        return False

    def _safe_package_snapshot(self, run_dir: Path, spec: dict[str, Any]) -> dict[str, Any] | None:
        if not (run_dir / "run-submission.json").is_file():
            return None
        try:
            return self._package_snapshot(run_dir, spec)
        except Exception:  # noqa: BLE001 - absent or legacy run-submission falls back cleanly
            return None

    def _resolve_run_symbol_binding(
        self,
        spec: dict[str, Any],
        profile: AppProfile,
        run_dir: Path,
    ) -> Any:
        from autophone.assets import app_symbols
        from autophone.assets.build_identity import load_document as load_build_identity_document

        build_identity_doc = load_build_identity_document(run_dir)
        package_snapshot = self._safe_package_snapshot(run_dir, spec)
        candidate_ref = spec.get("candidate_ref")
        raw_build_id = spec.get("build_id")
        if not raw_build_id and isinstance(candidate_ref, Mapping) and candidate_ref.get("build_id"):
            raw_build_id = str(candidate_ref["build_id"])
        if not raw_build_id and isinstance(package_snapshot, Mapping):
            ref = package_snapshot.get("registry_build_ref")
            if isinstance(ref, Mapping) and ref.get("build_id"):
                raw_build_id = str(ref["build_id"])
        return app_symbols.load_symbol_binding_for_app(
            profile,
            build_id=str(raw_build_id) if raw_build_id else None,
            build_identity_doc=build_identity_doc,
            package_snapshot=package_snapshot,
            allow_default_build=False,
        )

    def _verify_probed_symbol_binding(
        self,
        profile: AppProfile,
        spec: dict[str, Any],
        run_dir: Path,
        frozen_binding: Any,
    ) -> None:
        from autophone.assets import app_symbols
        from autophone.assets.build_identity import load_document as load_build_identity_document

        doc = load_build_identity_document(run_dir)
        if not isinstance(doc, Mapping):
            return
        state = str(doc.get("identity_state") or "")
        if state == "mismatch":
            raise app_symbols.SymbolBindingMismatchError(
                f"probed device build-identity is mismatched for app {profile.app!r}; "
                "refusing to execute symbol-bound recipes"
            )
        if state != "confirmed":
            return
        package_snapshot = self._safe_package_snapshot(run_dir, spec)
        probed = app_symbols.load_symbol_binding_for_app(
            profile,
            build_identity_doc=doc,
            package_snapshot=package_snapshot,
            allow_default_build=False,
        )
        if (
            probed.artifact_content_digest != frozen_binding.artifact_content_digest
            or probed.binding_sha256 != frozen_binding.binding_sha256
        ):
            raise app_symbols.SymbolBindingMismatchError(
                f"probed confirmed build-identity symbol binding "
                f"(digest={probed.artifact_content_digest[:12]}, binding={probed.binding_sha256[:12]}) "
                f"disagrees with frozen run symbol binding "
                f"(digest={frozen_binding.artifact_content_digest[:12]}, "
                f"binding={frozen_binding.binding_sha256[:12]})"
            )

    def _admit_selected_cases(
        self,
        profile: AppProfile,
        runnable_selection: list[dict[str, Any]],
        *,
        trial_draft_recipes: bool,
        confirmed_case_ids: set[str],
        trial_data_isolation: str,
        symbol_binding: Any | None,
        screen_asset_doc: Mapping[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        from autophone.assets.app_symbols import SymbolBindingError
        from autophone.execution.admission import (
            admit_case,
            admit_recipe_steps,
            partition_admitted_cases,
        )
        from autophone.execution.contracts import blocker
        from autophone.execution.engine.recipe import (
            RecipeError,
            expand_symbol_steps,
            load_recipe,
            operation_scope,
            recipe_has_unresolved_symbols,
            steps_binding_sha256,
        )

        prepared: list[dict[str, Any]] = []
        pre_blocked_by_id: dict[str, dict[str, Any]] = {}
        has_live_symbol_case = False
        for raw_case in runnable_selection:
            case = dict(raw_case)
            cid = str(case.get("id") or "")
            is_plain_live = (
                case.get("_trial_recipe_document") is None
                and case.get(GOLDEN_PATH_LIVE_DOCUMENT_KEY) is None
                and case.get("_trial_recipe_missing") is not True
                and case.get("status") == "active"
                and case.get("automation") == "ui_ready"
                and hasattr(profile, "recipes_dir")
            )
            if is_plain_live:
                rpath = profile.recipes_dir() / f"{cid.lower()}.yaml"
                if rpath.is_file():
                    try:
                        live_steps = load_recipe(rpath)
                    except (OSError, RecipeError, ValueError):
                        live_steps = None
                    if isinstance(live_steps, list) and recipe_has_unresolved_symbols(live_steps):
                        has_live_symbol_case = True
                        try:
                            expanded = expand_symbol_steps(
                                live_steps,
                                symbol_binding=symbol_binding,
                                screen_asset_doc=screen_asset_doc,
                            )
                            case["_symbol_expanded_recipe_steps"] = expanded
                            bound_sha = steps_binding_sha256(expanded)
                            if bound_sha is not None:
                                case["_symbol_binding_sha256"] = bound_sha
                        except (RecipeError, SymbolBindingError, ValueError) as exc:
                            pre_blocked_by_id[cid] = {
                                "case_id": cid,
                                "admitted": False,
                                "blocker": blocker(
                                    "asset",
                                    "invalid_live_recipe",
                                    recovery=f"repair the live recipe: {exc}",
                                ),
                            }
            prepared.append(case)

        confirm_flags = {
            str(case.get("id") or "").upper() in confirmed_case_ids for case in prepared
        }
        if not has_live_symbol_case and not pre_blocked_by_id and len(confirm_flags) <= 1:
            uniform_confirmed = next(iter(confirm_flags), False)
            return partition_admitted_cases(
                profile,
                cases=prepared,
                trial_draft_recipes=trial_draft_recipes,
                confirm_trial_effects=uniform_confirmed,
                trial_data_isolation=trial_data_isolation,
            )

        profile_root = getattr(profile, "root", None) or CODE_ROOT
        if has_live_symbol_case:
            import_app_recipes(profile.app, root=profile_root)
        runnable: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        boundary_before_next = False
        for case in prepared:
            cid = str(case.get("id") or "")
            if cid in pre_blocked_by_id:
                blocked.append(pre_blocked_by_id[cid])
                boundary_before_next = True
                continue
            expanded_live = case.get("_symbol_expanded_recipe_steps")
            if isinstance(expanded_live, list):
                with operation_scope(profile.app, root=profile_root):
                    step_decision = admit_recipe_steps(
                        profile,
                        expanded_live,
                        mode="active",
                        trial_data_isolation=trial_data_isolation,
                    )
                decision = {
                    "case_id": cid,
                    "admitted": step_decision["admitted"],
                    "blocker": step_decision["blocker"],
                }
            else:
                decision = admit_case(
                    profile,
                    case,
                    trial_draft_recipes=trial_draft_recipes,
                    confirm_trial_effects=cid.upper() in confirmed_case_ids,
                    trial_data_isolation=trial_data_isolation,
                )
            if not decision["admitted"]:
                blocked.append(decision)
                boundary_before_next = True
                continue
            planned = dict(case)
            if boundary_before_next:
                planned["_break_shared_precondition_before"] = True
                boundary_before_next = False
            runnable.append(planned)
        return runnable, blocked

    def _record_confirmed_symbol_claims(
        self,
        profile: AppProfile,
        spec: dict[str, Any],
        run_dir: Path,
        outcomes: list[Any],
        *,
        ax_backend: str,
    ) -> None:
        """Append external ``symbol.unique_visibility`` claims ONLY on confirmed builds."""
        from autophone.assets import app_symbols
        from autophone.assets.build_identity import load_document as load_build_identity_document

        observations: list[tuple[str, dict[str, Any]]] = []
        for outcome in outcomes:
            obs_list = getattr(outcome, "symbol_observations", None)
            if not isinstance(obs_list, list):
                continue
            for item in obs_list:
                if isinstance(item, Mapping):
                    observations.append((str(getattr(outcome, "id", "") or ""), dict(item)))
        if not observations:
            return
        build_identity_doc = load_build_identity_document(run_dir)
        if (
            not isinstance(build_identity_doc, Mapping)
            or build_identity_doc.get("identity_state") != "confirmed"
        ):
            return
        package_snapshot = self._safe_package_snapshot(run_dir, spec)
        try:
            confirmed = app_symbols.resolve_artifact_identity_for_app(
                profile,
                build_identity_doc=build_identity_doc,
                package_snapshot=package_snapshot,
                allow_default_build=False,
            )
        except app_symbols.SymbolBindingError as exc:
            logger.warning("confirmed build-identity symbol claim skipped: %s", exc)
            return

        seen: set[tuple[str, str, str, str, str]] = set()
        build_id_ref = (
            f"runs/{run_dir.name}/build-identity.json"
            if (run_dir / "build-identity.json").is_file()
            else None
        )
        for case_id, obs in observations:
            symbol_id = str(obs.get("symbol_id") or "").strip()
            digest = str(obs.get("artifact_content_digest") or "").strip().lower()
            binding_sha = str(obs.get("binding_sha256") or "").strip().lower()
            screen_id = str(obs.get("screen_id") or "").strip()
            obs_backend = str(obs.get("ax_backend") or ax_backend).strip()
            if not (symbol_id and digest and binding_sha and screen_id and obs_backend):
                continue
            if digest != confirmed.artifact_content_digest:
                logger.warning(
                    "skipping unique_visibility claim for %s: observation digest %s != confirmed %s",
                    symbol_id, digest[:12], confirmed.artifact_content_digest[:12],
                )
                continue
            dedup_key = (symbol_id, digest, binding_sha, screen_id, obs_backend)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            refs: list[str] = []
            if build_id_ref is not None:
                refs.append(build_id_ref)
            ax_ref = obs.get("ax_evidence_ref")
            if isinstance(ax_ref, str) and ax_ref.strip():
                refs.append(f"runs/{run_dir.name}/{ax_ref.strip().lstrip('/')}")
            try:
                app_symbols.record_unique_visibility_claim(
                    profile,
                    symbol_id=symbol_id,
                    artifact_content_digest=digest,
                    binding_sha256=binding_sha,
                    screen_id=screen_id,
                    identity_state="confirmed",
                    ax_backend=obs_backend,
                    evidence_refs=refs,
                    artifact_sha256=(
                        confirmed.artifact_sha256 if confirmed.kind == "ipa_build" else None
                    ),
                    provenance_extra={"run_id": run_dir.name, "case_id": case_id},
                )
            except Exception as exc:  # noqa: BLE001 - claim write failure must not corrupt run outcome
                logger.warning("failed to record symbol.unique_visibility claim for %s: %s", symbol_id, exc)

    def _materialize_phase2(self, pending: PendingPhase2Collection, run_dir: Path) -> ExecutionBundle:
        from autophone.execution.engine.report import write_reports

        for recorder, frozen in pending.recordings:
            recorder.materialize(frozen)
        result = pending.phase2_result
        cases_path = pending.cases_path
        report_meta = pending.report_meta
        result_json, report_md, report_html = write_reports(
            result, run_dir, cases_path=cases_path, meta=report_meta, case_docs=pending.case_docs)
        coverage_json, coverage_html = self.write_coverage(
            run_dir=run_dir,
            run_id=str(report_meta["run_id"]),
            cases_path=cases_path,
            phase2_result_path=result_json,
            case_docs=pending.case_docs,
        )
        # Re-render the Phase 2 HTML after coverage.json exists so the unified
        # report shell embeds the Phase 5 coverage panel instead of forcing
        # reviewers to open a separate file.
        from autophone.execution.engine.html_report import write_html_report
        report_html = write_html_report(result_json)
        return ExecutionBundle(
            phase2_result=result,
            result_json=result_json,
            report_md=report_md,
            report_html=report_html,
            cases_path=cases_path,
            coverage_json=coverage_json,
            coverage_html=coverage_html,
        )

    @contextmanager
    def _draft_recipe_scope(
        self,
        profile: AppProfile,
        selector: dict[str, Any],
        *,
        spec: Mapping[str, Any] | None = None,
        symbol_binding: Any | None = None,
        screen_asset_doc: Mapping[str, Any] | None = None,
    ):
        """草稿试跑通道：校验并冻结显式点名的 draft recipe 身份。

        边界（不破「AI 产物不自行入回归库」不变量）：
        - 只在 selector 声明了 draft 生命周期且带 ids 时生效，普通回归 run 一字不动；
        - draft steps 只经 Case 计划传给 recipe_scope 的 ContextVar override，不写全局 RECIPES；
        - 缺失的显式 draft path 由 AssetAdmission 阻断，不回退 live recipe 或 script；
        - 冻结前过 App 静态契约，草稿写错在起跑前拒绝。
        """
        import yaml

        from autophone.assets.case_ids import (
            assert_reserved_recipe_binding,
            id_reservations,
        )
        from autophone.execution.effects import profile_trial_data_isolation
        from autophone.execution.engine.recipe import operation_scope, validate_recipe_for_app

        statuses = [str(s) for s in (selector.get("statuses") or [])]
        ids = [str(i) for i in (selector.get("ids") or []) if str(i)]
        # 两条触发路：① draft-status 用例试跑（原有）；② active 规范用例的草稿 recipe 试跑
        # （trial_draft_recipes，案 R107/R108…——case status 是 active，draft 的只是它的 recipe，
        #   不能靠 statuses:['draft'] 选中）。两路都只对显式点名的 ids 生效。
        draft_trial = "draft" in statuses or bool(selector.get("trial_draft_recipes"))
        if not draft_trial or not ids or not hasattr(profile, "recipes_dir"):
            yield {}
            return
        try:
            registry = yaml.safe_load(profile.cases_path().read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ContractError(f"读取 Case Registry 失败，不能注册 draft recipe：{exc}") from exc
        try:
            reservations = id_reservations(registry)
        except ValueError as exc:
            raise ContractError(f"draft recipe 绑定非法：{exc}") from exc
        cases_by_id = {
            str(case.get("id") or "").upper(): case
            for case in (registry.get("cases") or [])
            if isinstance(case, dict) and case.get("id")
        }
        candidates: list[tuple[str, Path, int]] = []
        identities: dict[str, dict[str, Any]] = {}
        seen_candidates: set[str] = set()
        for cid in ids:
            key = cid.upper()
            if key in seen_candidates:
                continue
            seen_candidates.add(key)
            path = profile.recipes_dir() / "drafts" / f"{cid.lower()}.yaml"
            if not path.exists():
                continue  # 显式缺草稿由后续 AssetAdmission 阻断，不回退 live 或 script。
            try:
                assert_reserved_recipe_binding(registry, key)
            except ValueError as exc:
                raise ContractError(f"draft recipe 绑定非法：{exc}") from exc
            case = cases_by_id.get(key)
            if case is None:
                raise ContractError(f"draft recipe {path} 没有同 ID Case，不能试跑")
            revision = case.get("revision")
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                raise ContractError(f"用例 {key} revision 非法，不能冻结草稿试跑身份")
            candidates.append((key, path, revision))
        if not candidates:
            yield {}
            return
        import_app_recipes(
            profile.app, root=getattr(profile, "root", None) or CODE_ROOT
        )
        confirmed_ids = _confirmed_trial_case_ids(spec) if spec is not None else set()
        isolation = profile_trial_data_isolation(profile)
        for key, path, revision in candidates:
            try:
                recipe_bytes = path.read_bytes()
                recipe_doc = yaml.safe_load(recipe_bytes) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise ContractError(f"读取 draft recipe {path} 失败：{exc}") from exc
            if not isinstance(recipe_doc, dict):
                raise ContractError(f"draft recipe {path} 必须是对象")
            if str(recipe_doc.get("id") or "").upper() != key:
                raise ContractError(f"draft recipe {path} 的 id 必须等于 {key}")
            steps = recipe_doc.get("steps")
            if not isinstance(steps, list) or not steps:
                raise ContractError(f"draft recipe {path} 缺少非空 steps")
            with operation_scope(
                profile.app,
                root=getattr(profile, "root", None) or CODE_ROOT,
            ):
                validate_recipe_for_app(
                    steps,
                    profile,
                    symbol_binding=symbol_binding,
                    screen_asset_doc=screen_asset_doc,
                    effect_mode="draft",
                    trial_draft_recipes=True,
                    confirm_trial_effects=key in confirmed_ids,
                    trial_data_isolation=isolation,
                )
            identities[key] = {
                "case_revision": revision,
                "reserved_external_id": reservations.get(key),
                "draft_recipe_sha256": hashlib.sha256(recipe_bytes).hexdigest(),
            }
        if identities:
            logger.info("草稿试跑：冻结 draft recipe %s", sorted(identities))
        yield identities

    @staticmethod
    def _bind_draft_recipe_trials(
        profile: AppProfile,
        cases: list[dict[str, Any]],
        identities: dict[str, dict[str, Any]],
        *,
        selector: dict[str, Any] | None = None,
        symbol_binding: Any | None = None,
        screen_asset_doc: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Freeze each selected draft document onto its in-memory Case plan."""
        import yaml
        from autophone.assets.app_symbols import SymbolBindingError
        from autophone.execution.engine.recipe import (
            RecipeError,
            expand_symbol_steps,
            recipe_has_unresolved_symbols,
            steps_binding_sha256,
        )

        bound: list[dict[str, Any]] = []
        selector = selector or {}
        statuses = {str(value) for value in (selector.get("statuses") or [])}
        explicit_trial = "draft" in statuses or bool(selector.get("trial_draft_recipes"))
        requested = {
            str(value).upper() for value in (selector.get("ids") or []) if str(value)
        }
        for raw_case in cases:
            case = dict(raw_case)
            key = str(case.get("id") or "").upper()
            identity = identities.get(key)
            if identity is None:
                if explicit_trial and key in requested:
                    case["_trial_recipe_missing"] = True
                bound.append(case)
                continue
            current_identity = current_draft_recipe_identity(profile, key)
            if current_identity != identity:
                raise ContractError(
                    f"draft recipe {key} 身份在试跑计划绑定前发生变化"
                )
            path = profile.recipes_dir() / "drafts" / f"{key.lower()}.yaml"
            try:
                recipe_bytes = path.read_bytes()
                document = yaml.safe_load(recipe_bytes) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise ContractError(f"读取 draft recipe {path} 失败：{exc}") from exc
            if (
                not isinstance(document, dict)
                or str(document.get("id") or "").upper() != key
                or not isinstance(document.get("steps"), list)
                or not document["steps"]
            ):
                raise ContractError(f"draft recipe {path} 不能冻结为试跑计划")
            if hashlib.sha256(recipe_bytes).hexdigest() != identity.get(
                "draft_recipe_sha256"
            ):
                raise ContractError(f"draft recipe {path} 在试跑计划冻结前发生变化")
            frozen_doc = copy.deepcopy(document)
            if recipe_has_unresolved_symbols(frozen_doc["steps"]):
                try:
                    frozen_doc["steps"] = expand_symbol_steps(
                        frozen_doc["steps"],
                        symbol_binding=symbol_binding,
                        screen_asset_doc=screen_asset_doc,
                    )
                    bound_sha = steps_binding_sha256(frozen_doc["steps"])
                except (RecipeError, SymbolBindingError) as exc:
                    raise ContractError(f"draft recipe {path} 符号绑定展开失败：{exc}") from exc
                if bound_sha is not None:
                    case["_symbol_binding_sha256"] = bound_sha
            case["_trial_recipe_identity"] = dict(identity)
            case["_trial_recipe_document"] = frozen_doc
            bound.append(case)
        return bound

    @staticmethod
    def _trial_recipe_overrides(
        cases: list[dict[str, Any]] | None,
    ) -> dict[str, list[dict[str, Any]] | None]:
        overrides: dict[str, list[dict[str, Any]] | None] = {}
        for case in cases or []:
            key = str(case.get("id") or "").upper()
            negative_steps = case.get(NEGATIVE_CONTROL_STEPS_KEY)
            if isinstance(negative_steps, list) and negative_steps:
                overrides[key] = copy.deepcopy(negative_steps)
                continue
            if case.get("_trial_recipe_missing") is True:
                overrides[key] = None
                continue
            # Golden path Run: the live Recipe frozen at binding runs, not a later read.
            frozen_live = golden_path_live_steps(case)
            if frozen_live is not None:
                overrides[key] = frozen_live
                continue
            expanded_live = case.get("_symbol_expanded_recipe_steps")
            if isinstance(expanded_live, list) and expanded_live:
                overrides[key] = copy.deepcopy(expanded_live)
                continue
            document = case.get("_trial_recipe_document")
            if not isinstance(document, dict):
                continue
            steps = document.get("steps")
            if isinstance(steps, list) and steps:
                overrides[key] = copy.deepcopy(steps)
        return overrides

    def _progress_writer(self, run_dir: Path) -> Callable[[list[dict[str, Any]]], None]:
        """返回一个进度回调：把 runner 吐出的逐用例快照原子写到 run-progress.json，
        供 detail() 暴露给实时看板的用例左栏（生命周期：pending/executing/pass/fail/...）。
        终态以 platform-run-result.json 的 cases 为准，本文件仅服务执行期。"""
        def _write(cases: list[dict[str, Any]]) -> None:
            write_json(run_dir / "run-progress.json", {"updated_at": _now(), "cases": cases})
        return _write

    def _run_cases(
        self,
        spec: dict[str, Any],
        profile: AppProfile,
        run_dir: Path,
        shot_dir: Path,
        cases_path: Path,
        selector: dict[str, Any],
        *,
        selected_cases: list[dict[str, Any]] | None = None,
        execution_groups: list[dict[str, Any]] | None = None,
        ax_backend: str | None = None,
        trial_draft_recipes: bool = False,
        confirm_trial_effects: bool = False,
        confirmed_trial_case_ids: set[str] | None = None,
        trial_data_isolation: str | None = None,
        symbol_binding: Any | None = None,
        screen_asset_doc: dict[str, Any] | None = None,
    ) -> Any:
        from autophone.execution.engine.runner import CaseOutcome, RunResult

        import contextlib

        resolved_ax_backend = ax_backend or ensure_ax_backend_supported(profile)
        resolved_isolation = (
            trial_data_isolation
            if trial_data_isolation is not None
            else _trial_effect_context(spec, profile)[3]
        )
        run_context_kwargs: dict[str, Any] = {
            "ax_backend": resolved_ax_backend,
            "trial_draft_recipes": trial_draft_recipes,
            "confirm_trial_effects": confirm_trial_effects,
            "confirmed_trial_case_ids": confirmed_trial_case_ids,
            "trial_data_isolation": resolved_isolation,
            "symbol_binding": symbol_binding,
            "screen_asset_doc": screen_asset_doc,
        }

        # 录屏器（seam，fail-open）：用例段全程后台轮询 screenshot 攒帧，退出时停录 + mux 成
        # recording.mp4。开关关闭 / 构造异常 → None，退回 nullcontext，回归行为不变。
        try:
            recorder = self.recorder_factory(run_dir, spec)
        except Exception:  # noqa: BLE001 录屏器构造异常视同禁用
            recorder = None
        with (recorder if recorder is not None else contextlib.nullcontext()):
            # 启动链第一步：唤醒/解锁——锁屏会让 launch+登录探测全失真，必须先离开锁屏。
            lock_blocker = self._ensure_unlocked(shot_dir)
            if lock_blocker:
                return RunResult(
                    aborted=True,
                    abort_reason=lock_blocker,
                    ax_backend=resolved_ax_backend,
                )
            # 混跑分组（替代原「互斥前置硬阻塞」）：selector 同时选中「需登录」(required) 与
            # 「需登出」(logged_out) 用例时，run 级前置态一遍跑不完——但不必让用户手动拆成两个
            # run，这里自动拆成**有序分组**逐组跑：先跑 logged_out 组（无 Frida、不补登），再跑
            # 需登录组（补登 + 抓取），每组内保持原相对顺序。无冲突 → None，单遍跑、行为不变。
            if (
                execution_groups is None
                and selected_cases is not None
                and len(selected_cases) > 1
                and has_execution_planner(str(getattr(profile, "app", "") or ""))
            ):
                # Dashboard multi-select submits an ordinary ids selector, not
                # the named full-catalog plan.  Apps that register an execution
                # planner still group the selection by their case metadata, so
                # authored flow steps keep their declared order.
                from autophone.execution.planning import execution_planner

                execution_groups = [
                    {
                        "label": group.label,
                        "ids": list(group.ids),
                        **(
                            {"runtime_order": group.runtime_order}
                            if group.runtime_order else {}
                        ),
                    }
                    for group in execution_planner(profile.app).build_selected_groups(selected_cases)
                ]
            groups = self._partition_by_execution_groups(
                selector, selected_cases=selected_cases, execution_groups=execution_groups)
            if groups is None:
                groups = self._partition_by_login_precondition(
                    cases_path, selector, selected_cases=selected_cases)
            if groups is None:
                return self._run_cases_for_state(
                    spec, profile, run_dir, shot_dir, cases_path, selector,
                    self._progress_writer(run_dir), selected_cases=selected_cases,
                    **run_context_kwargs)
            results: list[Any] = []
            done_snaps: list[dict[str, Any]] = []
            # 组计划（label / 成员 ids / 逐组 status）：让「拆成哪几组、每组哪些用例、
            # 当前跑到第几组」进进度快照与最终结果，前端实时视图/详情页才能按组分节展示——
            # 否则组边界只在后端，前端看到的是一条平铺进度（组间重启置态像卡住）。
            group_plan = [
                {"label": g["label"], "ids": list(g["ids"]), "status": "pending"}
                for g in groups
            ]
            for gi, g in enumerate(groups):
                group_plan[gi]["status"] = "running"
                gate = self._group_precondition_gap(g, run_dir, prior_results=results, app=str(getattr(profile, "app", "")))
                if gate is not None:
                    gap_blocker, gap_detail = gate
                    result = RunResult(
                        aborted=False,
                        abort_reason="",
                        outcomes=[
                            CaseOutcome(
                                id=str(case.get("id") or ""),
                                status="blocked",
                                module=str(case.get("module") or ""),
                                item=str(case.get("item") or ""),
                                row=case.get("row") if isinstance(case.get("row"), int) else None,
                                detail=gap_detail,
                                blocker=dict(gap_blocker),
                            )
                            for case in g["cases"]
                        ],
                        ax_backend=resolved_ax_backend,
                    )
                    results.append(result)
                    current_snaps = [
                        {"id": o.id, "item": o.item, "status": o.status, "group": g["label"]}
                        for o in result.outcomes
                    ]
                    group_plan[gi]["status"] = "done"
                    self._grouped_progress_writer(
                        run_dir, done_snaps, g["label"], group_plan
                    )(current_snaps)
                    done_snaps += current_snaps
                    logger.info(
                        "跳过执行组 %s：%s，%d 个用例标记 %s",
                        g["label"], gap_detail, len(result.outcomes), gap_blocker.get("code"),
                    )
                    continue
                runtime_order = str(g.get("runtime_order") or "")
                runtime_kwargs = (
                    {"runtime_order": runtime_order, "group_plan_entry": group_plan[gi]}
                    if runtime_order else {}
                )
                result = self._run_cases_for_state(
                    spec, profile, run_dir, shot_dir, cases_path, g["selector"],
                    self._grouped_progress_writer(run_dir, done_snaps, g["label"], group_plan),
                    selected_cases=g["cases"], **runtime_kwargs, **run_context_kwargs)
                results.append(result)
                done_snaps = done_snaps + [
                    {"id": o.id, "item": getattr(o, "item", ""), "status": o.status,
                     "group": g["label"]}
                    for o in result.outcomes
                ]
                group_plan[gi]["status"] = "aborted" if result.aborted else "done"
                # 任一组出现中止（设备掉线 / 协作式中止）→ 停后续组，已跑组结果保留、overall 收 blocked。
                if result.aborted:
                    self._block_cases_after_group_abort(result, groups, gi, done_snaps)
                    break
            return self._merge_run_results(results, group_plan)

    @staticmethod
    def _block_cases_after_group_abort(
        result: Any,
        groups: list[dict[str, Any]],
        aborted_index: int,
        done_snaps: list[dict[str, Any]],
    ) -> None:
        """组中止后，把本组未执行与后续未开始的 Case 记为 blocked（带 blocker）。

        BL-083 首轮（2026-10-11）登录闸中止后，`read_only_navigation` 组 10 条 Case 不在
        `cases[]` 中，PostgreSQL `runs.result`、`platform-run-result.json` 与 `index.sqlite`
        都没有这些行，可运行率只能按缺失计数。本组已有结构化 blocked 结果（如启动失败）时
        沿用其 blocker，否则按中止原因分类（`run_abort_blocker`）。Run 仍为 aborted，
        总体状态不变。
        """
        reason = str(getattr(result, "abort_reason", "") or "")
        structured = next(
            (dict(o.blocker) for o in result.outcomes
             if getattr(o, "status", "") == "blocked" and isinstance(getattr(o, "blocker", None), Mapping)),
            None,
        )
        payload = structured or run_abort_blocker(reason)
        aborted = groups[aborted_index]
        filled = materialize_unexecuted_cases(
            list(aborted["cases"]), result.outcomes, blocker_payload=payload,
            detail=f"execution group {aborted['label']} aborted before this case ran: {reason}",
        )
        for later in groups[aborted_index + 1:]:
            filled += materialize_unexecuted_cases(
                list(later["cases"]), result.outcomes + filled, blocker_payload=payload,
                detail=(f"execution group {later['label']} not started after group "
                        f"{aborted['label']} aborted: {reason}"),
            )
        if not filled:
            return
        result.outcomes = list(result.outcomes) + filled
        done_snaps.extend(
            {"id": o.id, "item": o.item, "status": o.status,
             "group": next((g["label"] for g in groups if o.id in {str(c.get("id")) for c in g["cases"]}), "")}
            for o in filled
        )
        logger.info(
            "执行组 %s 中止：%d 个未执行用例记为 blocked（%s/%s）",
            aborted["label"], len(filled), payload.get("scope"), payload.get("code"),
        )

    def _run_cases_for_state(
        self,
        spec: dict[str, Any],
        profile: AppProfile,
        run_dir: Path,
        shot_dir: Path,
        cases_path: Path,
        selector: dict[str, Any],
        progress_cb: Callable[[list[dict[str, Any]]], None],
        *,
        selected_cases: list[dict[str, Any]] | None = None,
        runtime_order: str = "",
        group_plan_entry: dict[str, Any] | None = None,
        ax_backend: str | None = None,
        trial_draft_recipes: bool = False,
        confirm_trial_effects: bool = False,
        confirmed_trial_case_ids: set[str] | None = None,
        trial_data_isolation: str | None = None,
        symbol_binding: Any | None = None,
        screen_asset_doc: dict[str, Any] | None = None,
    ) -> Any:
        """单一登录前置态的用例组执行（今日 run 的每态逻辑，被 _run_cases 单遍或分组复用）。

        约定 selector 里的用例**登录前置同质**（要么全非 logged_out、要么全 logged_out）——
        混跑的拆分在上游由 _partition_by_login_precondition 完成，本方法只负责跑好一态。
        recorder/解锁在 _run_cases 里统管，本方法不重复。
        """
        from autophone.execution.engine.runner import RunResult, run
        from autophone.execution.engine.recipe import operation_scope, recipe_scope

        resolved_ax_backend = ax_backend or ensure_ax_backend_supported(profile)
        resolved_isolation = (
            trial_data_isolation
            if trial_data_isolation is not None
            else _trial_effect_context(spec, profile)[3]
        )
        resolved_screen_doc = (
            screen_asset_doc
            if screen_asset_doc is not None
            else self._screen_asset_doc(profile)
        )
        result = None
        # 登录态是否为本组的前置：仅当被选用例里确有 required 用例时成立。
        login_required = self._login_gate_applies(
            cases_path, selector, selected_cases=selected_cases)
        # ── 登录态闸前移：在 capture/AX attach 之前、于**无 Frida** 环境把登录态置成
        #    persisted。补登（若发生）绝不穿过抓取会话，避开登录 completion block 穿 Frida
        #    包装层的风险。普通 uiopen 启动 → 探测 → 允许 auto_login。例外：补登读回手机号时
        #    短暂 attach 一个只注入 ax_dump、不装 hook 的 AX 会话，手机号步骤结束即关闭（BL-083 第六轮）。
        if login_required:
            launch_error = self._launch_app(profile, shot_dir)
            if launch_error:
                result = self._launch_aborted_result(launch_error, selected_cases)
            else:
                login_blocker = self._ensure_login_state(
                    shot_dir, auto_login=bool(spec.get("auto_login", True)),
                    profile=profile, run_dir=run_dir)
                if login_blocker:
                    result = RunResult(
                        aborted=True,
                        abort_reason=login_blocker,
                        ax_backend=resolved_ax_backend,
                    )
        # ── API 抓取（专项二补齐 Hole 1）：只要 api plane 未禁用，且满足
        #    (需要登录态 OR 任一被选用例声明 entity_binding/requirements 需要 api_capture)
        #    即进入 _run_with_capture；不再对 login_precondition="any" 的实体校验用例跳过抓取。
        api_capture_required = self._api_capture_applies(
            cases_path, selector, selected_cases=selected_cases,
        )
        capture_skip_reason: str | None = None
        if (
            result is None
            and plane_mode(spec, "api") != "disabled"
            and (login_required or api_capture_required)
        ):
            result, capture_skip_reason = self._run_with_capture(
                spec, profile, run_dir, shot_dir, cases_path, selector, progress_cb,
                selected_cases=selected_cases, runtime_order=runtime_order,
                group_plan_entry=group_plan_entry,
                ax_backend=resolved_ax_backend,
                trial_draft_recipes=trial_draft_recipes,
                confirm_trial_effects=confirm_trial_effects,
                confirmed_trial_case_ids=confirmed_trial_case_ids,
                trial_data_isolation=resolved_isolation,
                symbol_binding=symbol_binding,
                screen_asset_doc=resolved_screen_doc,
                login_required=login_required)
        elif (
            result is None
            and not login_required
            and not api_capture_required
            and plane_mode(spec, "api") != "disabled"
        ):
            # 非「需要登录态」且无 API 抓取需求的组按策略跳过抓取会话——
            # 这不是能力缺失，报告面板应说明「按策略跳过」而不是退回笼统的 frida 猜测文案。
            capture_skip_reason = "本组用例无需登录态，按策略跳过抓取会话（非能力缺失）"
        # ── 普通启动路径：capture 未启用/未跑起来（fail-open 退回），或登录验证类组。
        #    始终重新 uiopen 一次（已在跑=前台化；capture spawn-gate 拆解后=冷启回登录态
        #    persisted 的首页），再跑用例。登录态已在上面置妥，此处不重复补登。
        if result is None:
            launch_error = self._launch_app(profile, shot_dir)
            if launch_error:
                result = self._launch_aborted_result(launch_error, selected_cases)
            else:
                from autophone.observation import capture as capture_mod

                import_app_recipes(
                    profile.app, root=getattr(profile, "root", None) or CODE_ROOT
                )
                fixtures_path = (
                    profile.api_fixtures_path()
                    if hasattr(profile, "api_fixtures_path") and profile.api_fixtures_path().is_file()
                    else None
                )
                # AX 是 UI 寻址能力，不是 API capture 的附属品。profile 显式启用时，普通
                # UI-only 路径也要注入同一个 provider；否则点餐用例在 --api 关闭后会悄然
                # 退回小字 OCR/固定坐标。attach 失败仍由 ax_scope fail-open，runner 照跑。
                with ax_scope(capture_mod.build_run_ax_session(profile, run_dir)) as ax_provider:
                    with operation_scope(
                        profile.app,
                        root=getattr(profile, "root", None) or CODE_ROOT,
                    ), recipe_scope(
                        profile.app,
                        root=getattr(profile, "root", None) or CODE_ROOT,
                        overrides=self._trial_recipe_overrides(selected_cases),
                    ):
                        result = run(
                            cases_path,
                            selector=selector,
                            sock_path=None,
                            shot_dir=shot_dir,
                            timeout=15.0,
                            ax=ax_provider,
                            allowed_effects=set(profile.allowed_effects),
                            screen_asset_doc=resolved_screen_doc,
                            initial_fixture_order=dict(
                                ((spec.get("fixture") or {}).get("order") or {})
                            ),
                            # 中止协作点 ②（执行中）：逐用例边界读标志，见即停（已跑用例结果保留，
                            # overall 收口为 blocked）。已发生的真副作用不回滚。
                            abort_check=lambda: run_abort_reason(run_dir),
                            progress_cb=progress_cb,
                            selected_cases=selected_cases,
                            ax_backend=resolved_ax_backend,
                            trial_draft_recipes=trial_draft_recipes,
                            confirm_trial_effects=confirm_trial_effects,
                            confirmed_trial_case_ids=confirmed_trial_case_ids,
                            trial_data_isolation=resolved_isolation,
                            symbol_binding=symbol_binding,
                            api_fixtures_path=fixtures_path,
                        )
            if capture_skip_reason and not result.aborted:
                result.capture_skip_reason = capture_skip_reason
        if result is not None and not result.aborted:
            # 专项二补齐 Hole 2：若任一声明 entity_binding 的用例因抓包降级/禁用/失败尚未进入
            # 联合裁决，必须以 api_captured=False fail-closed 裁决，绝不允许静默 pass。
            result = self._finalize_missing_entity_joint_verdicts(
                profile,
                result,
                selected_cases=selected_cases,
                cases_path=cases_path,
                selector=selector,
                capture_skip_reason=capture_skip_reason,
            )
        if getattr(result, "ax_backend", None) is None:
            result.ax_backend = resolved_ax_backend
        return result

    def _group_precondition_gap(
        self,
        group: dict[str, Any],
        run_dir: Path,
        *,
        prior_results: list[Any],
        app: str,
    ) -> tuple[dict[str, str], str] | None:
        """Return ``(blocker, detail)`` when a group's precondition is not met.

        Two gate kinds, and the difference matters:

        ``requires_pass_case_id`` waits on a prior case's verdict.  It is only
        honest when the required case *leaves behind* what the group needs — a
        case that resets state on teardown proves nothing about the state the
        next group starts in.

        ``requires_duty_state`` names the state itself and establishes it here,
        so a red duty case no longer blocks unrelated order work and a green one
        no longer licenses running against an App its own teardown put back
        off duty.
        """
        from autophone.execution.contracts import blocker

        required_case_id = str(group.get("requires_pass_case_id") or "")
        if required_case_id:
            dependency = next(
                (
                    outcome
                    for prior in prior_results
                    for outcome in prior.outcomes
                    if str(outcome.id) == required_case_id
                ),
                None,
            )
            if dependency is None or str(dependency.status) != "pass":
                return (
                    blocker(
                        "fixture", "predecessor_failed",
                        recovery=f"repair {required_case_id}, then rerun this group",
                    ),
                    f"fixture blocked: predecessor_failed; required {required_case_id}=pass",
                )

        duty_state = str(group.get("requires_duty_state") or "")
        if duty_state:
            ok, detail = self._ensure_duty_state(duty_state, run_dir, label=str(group.get("label") or ""), app=app)
            if not ok:
                return (
                    blocker(
                        "fixture", "duty_state_not_met",
                        capability=f"duty_state:{duty_state}",
                        recovery=(
                            f"restore a verified path to {duty_state} "
                            "before creating or dispatching orders"
                        ),
                    ),
                    f"fixture blocked: duty_state_not_met; ensure {duty_state} failed: {detail}",
                )
        return None

    @staticmethod
    def _ensure_duty_state(target: str, run_dir: Path, *, label: str, app: str) -> tuple[bool, str]:
        """Drive the App to duty state ``target`` once per group and verify by readback.

        This is the same ``ensure_duty_state`` executor the cases use in setup,
        run once at the group boundary instead of once per case: it is a no-op
        when the App is already there, and it only moves along transitions the
        duty controller has verified, so a missing edge fails closed rather than
        guessing its way into an unknown state.
        """
        from autophone.execution.operation_run import run_operation_executor

        operation_dir = run_dir / "operations" / f"group-{label or 'unlabelled'}-ensure_duty_state"
        operation_dir.mkdir(parents=True, exist_ok=True)
        logger.info("执行组 %s 前置：ensure_duty_state(%s)", label, target)
        try:
            out = run_operation_executor(
                operation_dir,
                {
                    "operation": {"name": "ensure_duty_state"},
                    "args": {"target": target, "purpose": "setup"},
                },
                app=app,
            )
        except Exception as exc:  # noqa: BLE001 - a dead fixture must block, not crash the run
            return False, f"{type(exc).__name__}: {exc}"
        detail = str(out.get("detail") or out.get("status") or "")
        return str(out.get("status")) == "passed", detail

    @staticmethod
    def _partition_by_execution_groups(
        selector: dict[str, Any],
        *,
        selected_cases: list[dict[str, Any]] | None,
        execution_groups: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]] | None:
        """Turn an app execution plan into stable, serial runner groups.

        The plan is already filtered by admission/capability checks.  Empty
        groups are omitted; any deferred cases are represented separately as
        preflight blockers and never reach this method.
        """
        if not execution_groups or selected_cases is None:
            return None
        by_id = {str(case.get("id") or ""): case for case in selected_cases}
        groups: list[dict[str, Any]] = []
        for raw in execution_groups:
            label = str(raw.get("label") or "")
            ids = [str(case_id) for case_id in (raw.get("ids") or []) if str(case_id) in by_id]
            if not label or not ids:
                continue
            groups.append({
                "label": label,
                "ids": ids,
                "selector": {**selector, "ids": ids},
                "cases": [by_id[case_id] for case_id in ids],
                "requires_pass_case_id": str(raw.get("requires_pass_case_id") or ""),
                "requires_duty_state": str(raw.get("requires_duty_state") or ""),
                "runtime_order": str(raw.get("runtime_order") or ""),
            })
        return groups or None

    def _partition_by_login_precondition(
        self, cases_path: Path, selector: dict[str, Any],
        *, selected_cases: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]] | None:
        """混跑 → 有序分组子选择器；无混跑 → None（单遍跑，行为不变）。

        仅当被选用例**同时**含 required 与 logged_out（互斥前置）时才拆，拆法：
          ① logged_out 组（先跑，登出态、不补登不抓取）；② 其余组（required + any，补登 + 抓取）。
        两组各自复制原 selector 并把 `ids` 收窄成该组用例——`ids` 与 statuses/tag 是交集
        （case_selector.py），故收窄只会更小、不会绕过原过滤。组内保持 select_cases 的原相对顺序
        （稳定分组，不重排），只把两态从相邻切成先后，规避「一遍无法同时满足两态」。

        fail-open：无冲突 / 加载异常 → None，交单遍路径的 _login_gate_applies + _ensure_login_state
        各自 fail-closed 兜底（与旧硬闸退化行为一致，只是不再整 run 阻塞）。
        """
        if self._login_precondition_conflict(
            cases_path, selector, selected_cases=selected_cases) is None:
            return None
        from autophone.execution.engine.cases_loader import load_cases
        from autophone.execution.engine.runner import select_cases

        try:
            selected = (list(selected_cases) if selected_cases is not None else
                        select_cases(load_cases(cases_path), selector=selector))
        except Exception:  # noqa: BLE001 conflict 已检出说明能加载，这里仍防御性退回单遍
            return None
        logged_out_ids = [
            str(c.get("id")) for c in selected
            if (c.get("login_precondition") or "required") == "logged_out"
        ]
        other_ids = [
            str(c.get("id")) for c in selected
            if (c.get("login_precondition") or "required") != "logged_out"
        ]
        return [
            {"label": "logged_out", "ids": logged_out_ids,
             "selector": {**selector, "ids": logged_out_ids},
             "cases": [c for c in selected if str(c.get("id")) in set(logged_out_ids)]},
            {"label": "logged_in", "ids": other_ids,
             "selector": {**selector, "ids": other_ids},
             "cases": [c for c in selected if str(c.get("id")) in set(other_ids)]},
        ]

    def _grouped_progress_writer(
        self, run_dir: Path, done_snaps: list[dict[str, Any]],
        group_label: str, group_plan: list[dict[str, Any]],
    ) -> Callable[[list[dict[str, Any]]], None]:
        """分组跑的进度回调：把已跑组的终态快照 done_snaps 拼在本组实时快照前，
        实时看板左栏才不会在切到下一组时「丢掉」上一组用例。终态仍以最终报告为准。

        每条本组用例盖上 group_label（done_snaps 里的历史条已在上游盖好），并把 group_plan
        （逐组 status，随主循环推进就地更新——写盘时已跑组=done、本组=running、未跑组=pending）
        顶层写进 run-progress.json，前端据此画组分节头 + 高亮当前组。"""
        def _write(cases: list[dict[str, Any]]) -> None:
            stamped = [{**c, "group": group_label} for c in cases]
            write_json(
                run_dir / "run-progress.json",
                {"updated_at": _now(), "cases": done_snaps + stamped, "groups": group_plan},
            )

        return _write

    @staticmethod
    def _merge_run_results(results: list[Any], group_plan: list[dict[str, Any]] | None = None) -> Any:
        """合并分组子 run 的 RunResult：outcomes 按组顺序拼接；任一组 aborted → 整体 aborted
        （abort_reason 合并）；capture_skip_reason 取各组非空并去重（logged_out 组的「按策略跳过」
        文案供其用例的 API 面板显示，需登录组抓到即在 run 级判 captured、不看此字段）。

        group_plan 非空时把组计划挂到 merged.groups + 建 id→label 映射 merged.group_by_id，
        供 _platform_result 把组信息落进最终结果 cases，详情页据此按组分节（同实时视图）。"""
        from autophone.execution.engine.runner import RunResult

        outcomes: list[Any] = []
        reasons: list[str] = []
        skip_reasons: list[str] = []
        aborted = False
        for r in results:
            outcomes.extend(r.outcomes)
            if r.aborted:
                aborted = True
                if r.abort_reason:
                    reasons.append(r.abort_reason)
            if getattr(r, "capture_skip_reason", None):
                skip_reasons.append(r.capture_skip_reason)
        merged_ax_backend = next(
            (getattr(r, "ax_backend", None) for r in results if getattr(r, "ax_backend", None) is not None),
            None,
        )
        merged = RunResult(
            aborted=aborted,
            abort_reason="；".join(reasons),
            outcomes=outcomes,
            ax_backend=merged_ax_backend,
        )
        if skip_reasons:
            merged.capture_skip_reason = "；".join(dict.fromkeys(skip_reasons))
        if group_plan:
            merged.groups = group_plan
            merged.group_by_id = {
                str(cid): g["label"] for g in group_plan for cid in g["ids"]
            }
        return merged

    @staticmethod
    def _merge_preflight_outcomes(
        selected: list[dict[str, Any]],
        executed: list[Any],
        preflight_blocks: list[dict[str, Any]],
        outcome_type: Any,
    ) -> list[Any]:
        """Restore selector order and materialize pre-device blockers.

        Admission and capability planning happen before the device execution
        path.  The report must still enumerate every requested case, so rejected
        entries become normal ``CaseOutcome(status=blocked)`` values rather than
        silently vanishing.
        """
        blocked_by_id = {str(item["case_id"]): item["blocker"] for item in preflight_blocks}
        executed_by_id = {str(outcome.id): outcome for outcome in executed}
        merged: list[Any] = []
        for case in selected:
            case_id = str(case.get("id") or "")
            raw_blocker = blocked_by_id.get(case_id)
            if raw_blocker is not None:
                merged.append(outcome_type(
                    id=case_id,
                    status="blocked",
                    module=str(case.get("module") or ""),
                    item=str(case.get("item") or ""),
                    row=case.get("row") if isinstance(case.get("row"), int) else None,
                    detail=(f"{raw_blocker.get('scope', 'preflight')} blocked: "
                            f"{raw_blocker.get('code')}") ,
                    blocker=raw_blocker,
                    failure_stage=("rig_readiness" if raw_blocker.get("scope") == "rig"
                                   and raw_blocker.get("code") in {"app_ui_unavailable", "vm_uds_unavailable", "frida_unavailable"}
                                   else "not_recorded"),
                    failed_definition={"status": "unknown"},
                ))
                continue
            outcome = executed_by_id.get(case_id)
            if outcome is not None:
                merged.append(outcome)
        return merged

    def _derive_case_requirements(
        self,
        profile: AppProfile,
        cases: list[dict[str, Any]],
        *,
        api_plane_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        """Derive each selected case's capabilities from its live/draft recipe."""
        from autophone.execution.capability_requirements import derive_requirements
        from autophone.execution.engine.recipe import recipe_document_from_bytes

        import_app_recipes(
            profile.app, root=getattr(profile, "root", None) or CODE_ROOT
        )
        derived: list[dict[str, Any]] = []
        for raw_case in cases:
            case = dict(raw_case)
            case_id = str(case.get("id") or "").lower()
            trial_document = case.get("_trial_recipe_document")
            trial_identity = case.get("_trial_recipe_identity")
            if trial_document is not None:
                if not isinstance(trial_document, dict) or not isinstance(trial_identity, dict):
                    raise ContractError(f"用例 {case_id} 的 draft trial 计划非法")
                document = copy.deepcopy(trial_document)
            elif isinstance(case.get(GOLDEN_PATH_LIVE_DOCUMENT_KEY), dict):
                # Golden path Run: plan capabilities from the frozen live document;
                # RECIPE_READ_KEY already holds the digests of the same bytes.
                document = copy.deepcopy(case[GOLDEN_PATH_LIVE_DOCUMENT_KEY])
            else:
                recipe_path = profile.recipes_dir() / f"{case_id}.yaml"
                if case.get("status") == "draft" and not recipe_path.exists():
                    recipe_path = profile.recipes_dir() / "drafts" / f"{case_id}.yaml"
                if recipe_path.exists():
                    # Read once: the same bytes plan capabilities and back the
                    # BL-186 execution identity recorded after the run.
                    recipe_bytes = recipe_path.read_bytes()
                    document = recipe_document_from_bytes(recipe_bytes, recipe_path)
                    case[RECIPE_READ_KEY] = recipe_read_record(
                        profile, case_id, recipe_path, recipe_bytes, document
                    )
                else:
                    document = {"steps": []}
            from autophone.record.case_recipe import attribution_gaps
            attribution = attribution_gaps(profile, document)
            if attribution:
                raise ContractError(f"用例 {case_id} 录制归因校验失败：{','.join(attribution)}")
            case["requirements"] = derive_requirements(
                profile, case, document, api_plane_mode=api_plane_mode,
            )
            derived.append(case)
        return derived

    @staticmethod
    def _capability_availability(
        snapshot: Any,
        *,
        profile: AppProfile | None = None,
    ) -> dict[str, bool]:
        base = snapshot.availability()
        frida = bool(base.get("frida"))
        availability = {
            **base,
            # Capture and location injection both use the Frida tunnel today;
            # names remain separate so a future non-Frida location backend does
            # not require a result-schema migration.
            "api_capture": frida,
            "location_injection": frida,
            "frida": frida,
            # Authentication is a group precondition provisioned later by the
            # existing login gate, not a host capability to pre-block here.
            "authenticated_session": True,
        }
        if profile is not None:
            from autophone.observation.capture import _ax_enabled

            if _ax_enabled(profile):
                backend = ensure_ax_backend_supported(profile)
                availability["ax"] = (
                    bool(base.get("vm_uds"))
                    if backend == "guest_axruntime"
                    else frida
                )
            else:
                availability["ax"] = False
        return availability

    def _prepare_rig(self, profile: AppProfile, shot_dir: Path, *, need_app: bool, need_frida: bool) -> Any:
        # Rig admission precedes _run_cases, so its screen preparation must run
        # before pid/ready probes. Keep the later check for relaunch/lock races.
        return self.rig_maintenance.prepare_run(
            profile, shot_dir, need_app=need_app, need_frida=need_frida,
            prepare_app=lambda: self._ensure_unlocked(shot_dir / "rig-readiness"),
        )

    def _run_with_capture(
        self,
        spec: dict[str, Any],
        profile: AppProfile,
        run_dir: Path,
        shot_dir: Path,
        cases_path: Path,
        selector: dict[str, Any],
        progress_cb: Callable[[list[dict[str, Any]]], None] | None = None,
        *,
        selected_cases: list[dict[str, Any]] | None = None,
        runtime_order: str = "",
        group_plan_entry: dict[str, Any] | None = None,
        ax_backend: str | None = None,
        trial_draft_recipes: bool = False,
        confirm_trial_effects: bool = False,
        confirmed_trial_case_ids: set[str] | None = None,
        trial_data_isolation: str | None = None,
        symbol_binding: Any | None = None,
        screen_asset_doc: dict[str, Any] | None = None,
        login_required: bool = True,
    ) -> tuple[Any | None, str | None]:
        """在 Frida 抓取会话里跑用例并落 planes/api。返回 (result, skip_reason)：
        skip_reason 只在 result is None（fail-open 退回普通启动）时可能非空，是真实失败
        原因（供报告 API 面板显示，而不是笼统的「如 frida-server 隧道未起」猜测文案）。

        会话拥有 app 启动（spawn 模式=spawn-gate 替代 uiopen；attach 模式=uiopen 普通启动后
        attach 前台 pid），登录仍走 UI 检查；退出后合并产物、逐用例 api_oracle 断言挂回
        outcome（合成 UI×API 态）。

        **fail-open 的两段口径（防重复真副作用）**：
          · 用例**尚未跑**（工厂/spawn-gate/__enter__ 等前置炸了，result 仍为 None）→ 返回 None，
            让 _execute_phase2 退回普通启动路径重跑——此时还没产生任何副作用，重跑安全。
          · 用例**已跑**（result 在手，订单/上下岗等真副作用已发生）→ **绝不返回 None**：哪怕
            session.merged() / finalize_api_plane() 抛错，也保留并返回原 result，API 证据缺失留作
            pending（不 finalize）。否则调用方按 None 退回会把同一回归**再跑一遍**，重复真副作用。
        """
        from autophone.execution.engine.runner import RunResult, run
        from autophone.execution.engine.recipe import operation_scope, recipe_scope
        from autophone.observation import capture as capture_mod

        try:
            session = self.capture_session_factory(profile, spec, shot_dir)
        except Exception as exc:  # noqa: BLE001 工厂构造异常视同无能力
            logger.warning("API 抓取会话构造异常，退回普通启动、不采接口数据", exc_info=True)
            session = None
            return None, f"抓取会话构造异常: {exc}"
        if session is None:
            # build_session 已在内部记原因（缺 frida / agent 未 build）；这里只标退回。
            logger.info("API 抓取面未启用（无抓取能力），本 run 走普通启动、不采接口数据")
            return None, "抓取面未启用（缺 frida 能力探测 / capture 策略未开）"

        result: Any = None
        merged: Any = None
        activity = _RUN_FRIDA_ACTIVITY.get()
        if activity is not None:
            activity["capture_opened"] = True
        try:
            with session:
                if getattr(session, "spawn_gated", False):
                    ready = capture_mod.wait_for_spawn_ready(shot_dir, profile)
                    if not ready.get("ok"):
                        fg = capture_mod.probe_app_foreground_support()
                        logger.warning(
                            "API 抓取 spawn 后屏幕未就绪，退回普通启动、不采接口数据："
                            "%s（guest app_foreground=%s；no 多半是 guest build 降级、"
                            "Frida spawn 到不了前台，需 VM 更新重启）",
                            ready.get("detail") or ready,
                            capture_mod.describe_app_foreground_support(fg),
                        )
                        return None, f"spawn 后屏幕未就绪: {ready.get('detail') or ready}"
                    logger.info(
                        "API 抓取 spawn 后屏幕已就绪：reason=%s attempts=%s evidence=%s",
                        ready.get("reason"), ready.get("attempts"), ready.get("evidence_ref"),
                    )
                # 登录态已在进入 capture 前于**无 Frida** 环境置成 persisted（见 _run_cases：
                # 登录态闸前移）。抓取会话内只做**只读复探、严禁补登**（auto_login=False），
                # 避免登录 completion block 穿 Frida 包装层。复探又掉登录页 = 前置态在 attach
                # 前后漂移 → 直接 abort（不在 session 内补登），交调用方按 fail-open 语义处理。
                recheck = (
                    self._ensure_login_state(shot_dir, auto_login=False)
                    if login_required
                    else None
                )
                if recheck:
                    result = RunResult(
                        aborted=True,
                        abort_reason=f"login_state changed before capture: {recheck}",
                    )
                else:
                    import_app_recipes(
                        profile.app, root=getattr(profile, "root", None) or CODE_ROOT
                    )
                    fixtures_path = (
                        profile.api_fixtures_path()
                        if hasattr(profile, "api_fixtures_path") and profile.api_fixtures_path().is_file()
                        else None
                    )
                    resolved_ax_backend = ax_backend or ensure_ax_backend_supported(profile)
                    resolved_isolation = (
                        trial_data_isolation
                        if trial_data_isolation is not None
                        else _trial_effect_context(spec, profile)[3]
                    )
                    resolved_screen_doc = (
                        screen_asset_doc
                        if screen_asset_doc is not None
                        else self._screen_asset_doc(profile)
                    )
                    # AX 结构寻址（capabilities.ax.enabled）：与 capture 会话并存的第二个 frida
                    # attach（已验证同 pid 双 session 共存），让 tap_ax 走结构寻址、破 OCR 脆性。
                    # fail-open：建/开失败 → ax_provider=None，tap_ax 退 fallback_xy，run 照跑。
                    with ax_scope(capture_mod.build_run_ax_session(profile, run_dir)) as ax_provider:
                        with operation_scope(
                            profile.app,
                            root=getattr(profile, "root", None) or CODE_ROOT,
                        ), recipe_scope(
                            profile.app,
                            root=getattr(profile, "root", None) or CODE_ROOT,
                            overrides=self._trial_recipe_overrides(selected_cases),
                        ):
                            result = run(
                                cases_path,
                                selector=selector,
                                sock_path=None,
                                shot_dir=shot_dir,
                                timeout=15.0,
                                capture=session.handle,
                                ax=ax_provider,
                                allowed_effects=set(profile.allowed_effects),
                                screen_asset_doc=resolved_screen_doc,
                                initial_fixture_order=dict(
                                    ((spec.get("fixture") or {}).get("order") or {})
                                ),
                                abort_check=lambda: run_abort_reason(run_dir),
                                progress_cb=progress_cb or self._progress_writer(run_dir),
                                selected_cases=selected_cases,
                                ax_backend=resolved_ax_backend,
                                trial_draft_recipes=trial_draft_recipes,
                                confirm_trial_effects=confirm_trial_effects,
                                confirmed_trial_case_ids=confirmed_trial_case_ids,
                                trial_data_isolation=resolved_isolation,
                                symbol_binding=symbol_binding,
                                api_fixtures_path=fixtures_path,
                            )
                merged = session.merged()
        except Exception as exc:  # noqa: BLE001 会话/启动/run 异常
            from autophone.device.ax_session import UnsupportedAxBackendError
            if isinstance(exc, UnsupportedAxBackendError):
                raise
            # result is None → 用例没跑起来，安全退回普通路径；非 None → 用例已跑，绝不重跑。
            if result is None:
                logger.warning(
                    "API 抓取会话启动/进入失败，用例尚未跑、安全退回普通启动重跑：%s", exc,
                    exc_info=True,
                )
                return None, str(exc)
            logger.warning(
                "API 抓取会话运行中异常，用例已执行（保留结果，不重跑）；"
                "对 API 必需的 entity_binding 用例执行 fail-closed 联合裁决",
                exc_info=True,
            )
            result = self._finalize_missing_entity_joint_verdicts(
                profile,
                result,
                selected_cases=selected_cases,
                cases_path=cases_path,
                selector=selector,
                capture_skip_reason=str(exc),
            )
            return result, str(exc)
        # finalize 是纯 host 后处理：失败也不该丢弃已跑的 result 去重跑；
        # 但对要求 API 证据的 entity_binding 用例仍须 fail-closed 裁决（api_evidence_missing）。
        try:
            selected_statuses = tuple(str(s) for s in (selector.get("statuses") or ()))
            expectation_statuses = (
                ("active", "draft") if "draft" in selected_statuses else ("active",)
            )
            finalized = capture_mod.finalize_api_plane(
                run_dir,
                merged,
                result,
                app=profile.app,
                expectation_statuses=expectation_statuses,
                cases=selected_cases,
                profile=profile,
                api_captured=True,
            )
            result = finalized.run_result
            summary = finalized.summary
            logger.info(
                "API 抓取面落盘：调用 %d 条（归因 %d）· 断言用例 %d · 断言 %d（fail %d）",
                summary.get("calls", 0), summary.get("attributed", 0),
                summary.get("asserted_cases", 0), summary.get("assertions", 0), summary.get("fail", 0),
            )
        except Exception as exc:  # noqa: BLE001 finalize 失败 → 普通过度 pending；entity_binding 必 fail-closed
            logger.warning(
                "API 抓取面 finalize 失败，对要求 API 的 entity_binding 用例执行 fail-closed 联合裁决",
                exc_info=True,
            )
            result = self._finalize_missing_entity_joint_verdicts(
                profile,
                result,
                selected_cases=selected_cases,
                cases_path=cases_path,
                selector=selector,
                capture_skip_reason=f"finalize_api_plane failed: {exc}",
            )
            return result, f"finalize_api_plane failed: {exc}"
        return result, None

    def write_coverage(
        self,
        *,
        run_dir: Path,
        run_id: str,
        cases_path: Path,
        phase2_result_path: Path,
        case_docs: tuple[dict[str, Any], ...] | None = None,
    ) -> tuple[Path | None, Path | None]:
        try:
            from autophone.observation.coverage.generate import generate_coverage
            from autophone.observation.coverage.html import write_coverage_html
        except Exception:  # noqa: BLE001
            return None, None
        coverage = generate_coverage(
            cases_path=cases_path,
            run_id=run_id,
            phase2_result_path=phase2_result_path,
            case_docs=case_docs,
        )
        coverage_json = write_json(run_dir / "coverage.json", coverage)
        coverage_html = run_dir / "coverage.html"
        write_coverage_html(coverage, coverage_html)
        return coverage_json, coverage_html

    def _write_telemetry(
        self,
        *,
        run_dir: Path,
        spec: dict[str, Any],
        profile: AppProfile,
        phase2_result: Any,
        timestamps: dict[str, str],
    ) -> tuple[Path | None, dict[str, Any] | None]:
        if plane_mode(spec, "telemetry") == "disabled":
            return None, None
        telemetry_cap = ((profile.raw.get("capabilities") or {}).get("telemetry") or {})
        if not telemetry_cap:
            return None, None
        from autophone.execution.closure_journal import ClosureJournal
        journal = ClosureJournal(run_dir)
        query_once = None
        if journal.path.exists():
            row = journal.read()
            if 'telemetry_started' not in (row.get('collection_checkpoints') or {}):
                journal.checkpoint('telemetry_started', {}, expected_version=row['version'])
            from autophone.execution.collection_queries import CollectionQueries
            query_once = CollectionQueries(journal)
        path, payload = collect_telemetry_record(
            run_dir=run_dir,
            spec=spec,
            profile_raw=profile.raw,
            phase2_result=phase2_result,
            timestamps=timestamps,
            cases_path=profile.cases_path() if not journal.path.exists() else None,
            sleep=self._telemetry_sleep,
            frozen_inputs=row['frozen_inputs'].get('telemetry_inputs') if journal.path.exists() else None,
            query_once=query_once,
        )
        if journal.path.exists():
            artifacts = []
            for artifact in sorted((run_dir / 'planes' / 'telemetry').rglob('*')):
                if artifact.is_file():
                    data = artifact.read_bytes()
                    artifacts.append({'path': str(artifact.relative_to(run_dir)),
                                      'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
            row = journal.read()
            journal.checkpoint('telemetry_complete', {'payload': payload, 'artifacts': artifacts},
                               expected_version=row['version'])
        return path, payload

    @staticmethod
    def _launch_aborted_result(launch_error: str, selected_cases: list[dict[str, Any]] | None) -> Any:
        """组启动失败的 RunResult。启动带结构化 blocker 时，本组用例逐条记为 blocked 并带该
        blocker（与预检 `vm_uds_unavailable` / `app_ui_unavailable` 同一结构），不只留文案。"""
        from autophone.execution.engine.runner import CaseOutcome, RunResult

        structured = launch_blocker(launch_error)
        outcomes = [] if structured is None else [
            CaseOutcome(
                id=str(case.get("id") or ""),
                status="blocked",
                module=str(case.get("module") or ""),
                item=str(case.get("item") or ""),
                row=case.get("row") if isinstance(case.get("row"), int) else None,
                detail=str(launch_error),
                blocker=dict(structured),
                failure_stage="rig_readiness", failed_definition={"status": "unknown"},
            )
            for case in selected_cases or []
        ]
        return RunResult(aborted=True, abort_reason=str(launch_error), outcomes=outcomes)

    def _launch_app(self, profile: AppProfile, shot_dir: Path) -> str | None:
        from autophone.observation.capture import _ready_markers, _ready_timeout
        from dataclasses import asdict

        from autophone.execution.contracts import CaseBlocked
        from autophone.execution.engine.actions import CaseContext, StepFailure

        ctx = CaseContext(case_id="_platform_launch", shot_dir=shot_dir, timeout=15.0)
        logger.info("启动被测 App %s", profile.bundle_id)
        try:
            ctx.launch_app(
                profile.bundle_id,
                ready_markers=_ready_markers(profile),
                springboard_icon_preview=self._springboard_icon_preview(profile),
                startup_timeout_s=_ready_timeout(profile),
            )
        except CaseBlocked as exc:
            # 设施类阻断（如 SpringBoard 重启）：保留 scope / code，并把本次启动的动作记录
            # （含 springboard_guard）写成证据文件，由 blocker.evidence_ref 指向。
            payload = dict(exc.blocker)
            evidence = shot_dir / PLATFORM_LAUNCH_EVIDENCE_NAME
            try:
                evidence.parent.mkdir(parents=True, exist_ok=True)
                write_json(evidence, {"actions": [asdict(result) for result in ctx.results]})
                payload["evidence_ref"] = evidence.relative_to(shot_dir.parent).as_posix()
            except Exception:  # noqa: BLE001 - 证据写入失败不改变阻断结论
                logger.warning("平台预启动动作记录写入失败", exc_info=True)
            detail = f"app launch blocked: {payload.get('scope')}/{payload.get('code')}: {exc.detail}"
            logger.error("App 启动阻断：%s", detail)
            return AppLaunchBlocked(detail, payload)
        except StepFailure as exc:
            logger.error("App 启动失败：%s", exc.result.detail)
            return f"app launch failed: {exc.result.detail}"
        except (ConnectionRefusedError, ConnectionError, FileNotFoundError) as exc:
            # vphone UDS 连不上 = 镜像没起来：给清晰提示，别把 errno 抛给用户
            logger.error("无法连接虚拟机（vphone UDS）：%s: %s — 镜像可能未启动", type(exc).__name__, exc)
            return (
                "无法连接虚拟机（vphone UDS 连接被拒绝）：iOS 镜像可能未启动。"
                "请先启动 VM 并确认 vphone.sock 可用后重试。"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("App 启动失败：%s: %s", type(exc).__name__, exc)
            return f"app launch failed: {type(exc).__name__}: {exc}"
        return None

    @staticmethod
    def _springboard_icon_preview(profile: AppProfile) -> tuple[int, int] | None:
        raw = (((profile.raw.get("capabilities") or {}).get("launch") or {})
               .get("springboard_icon_preview"))
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            return None
        try:
            return int(raw[0]), int(raw[1])
        except (TypeError, ValueError):
            return None

    def _ensure_unlocked(self, shot_dir: Path) -> str | None:
        """复用设备层的锁屏检测、Home 解锁与有界复检。

        锁屏判据优先读 SpringBoard 原生状态（经本 rig 的 Frida 端点）；熄屏时 UDS 截图
        停在旧帧，只看截图会把锁屏判成未锁屏（2026-10-05 vm）。原生状态不可用时
        回退截图 OCR。"""
        from autophone.device.unlock import ensure_unlocked
        from autophone.execution.engine.actions import resolve_sock
        from autophone.observation.vphone_native_state import native_lock_state

        return ensure_unlocked(shot_dir, sock_path=resolve_sock(), lock_probe=native_lock_state)

    def _login_gate_applies(
        self,
        cases_path: Path,
        selector: dict[str, Any],
        *,
        selected_cases: list[dict[str, Any]] | None = None,
    ) -> bool:
        """登录态闸是否适用于本 run。

        登录态是**每条用例各自的前置**，不是 run 级全局不变量：绝大多数用例（订单/上下岗等）
        默认需要「已登录」前置（login_precondition 缺省即 required），App 掉登录就该 fail-fast；
        但登录类用例（R47 登录-成功 / R48 / R49）的正确前置态是**未登录/已登出**，拿「必须已登录」
        去拦它们逻辑正好拧反。故闸只在被选用例里**确有 required 用例**时才探测/阻塞；纯登录类
        (logged_out/any) 的 run 不被拦。

        fail-closed：选择/加载失败时返回 True（按需要登录处理，不误放掉保护性闸）。
        """
        from autophone.execution.engine.cases_loader import load_cases
        from autophone.execution.engine.runner import select_cases

        try:
            selected = (list(selected_cases) if selected_cases is not None else
                        select_cases(load_cases(cases_path), selector=selector))
        except Exception:  # noqa: BLE001 加载/选择异常 → 保守按需要登录处理
            return True
        if not selected:
            return True
        return any((c.get("login_precondition") or "required") == "required" for c in selected)

    @staticmethod
    def _reject_unpinned_draft_selector(spec: dict[str, Any]) -> None:
        """submit 预检：草稿试跑只允许显式点名。selector 含 draft 生命周期却不带 ids 一律拒
        ——草稿（含 AI 产物）不该被 module/suite/tag 圈选批量带跑进回归面（评审决议 2026-07-09），
        点名试跑是 draft 用例进转正证据链的唯一执行通道。"""
        selector = spec.get("case_selector") or {}
        statuses = [str(s) for s in (selector.get("statuses") or [])]
        ids = [str(i) for i in (selector.get("ids") or []) if str(i)]
        if ("draft" in statuses or bool(selector.get("trial_draft_recipes"))) and not ids:
            raise ContractError(
                "draft 用例/草稿 recipe 试跑必须显式点名（case_selector.ids），不允许按 module/suite/tag 圈选")

    def _api_capture_applies(
        self,
        cases_path: Path,
        selector: dict[str, Any],
        *,
        selected_cases: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Whether any selected case explicitly requires API capture (e.g. via entity_binding)."""
        from autophone.assets.entity_binding import case_requires_api_capture
        from autophone.execution.engine.cases_loader import load_cases
        from autophone.execution.engine.runner import select_cases

        try:
            selected = (
                list(selected_cases)
                if selected_cases is not None
                else select_cases(load_cases(cases_path), selector=selector)
            )
        except Exception:  # noqa: BLE001
            return False
        return any(
            case_requires_api_capture(case)
            or (case.get("requirements") or {}).get("api_capture") == "required"
            for case in selected
        )

    def _finalize_missing_entity_joint_verdicts(
        self,
        profile: AppProfile,
        result: Any,
        *,
        selected_cases: list[dict[str, Any]] | None,
        cases_path: Path,
        selector: dict[str, Any],
        capture_skip_reason: str | None = None,
    ) -> Any:
        """Fail-closed joint arbitration for entity_binding cases when API capture was absent or failed."""
        from autophone.assets.entity_binding import case_has_entity_binding, resolve_case_entity
        from autophone.execution.engine.cases_loader import load_cases
        from autophone.execution.engine.runner import _redacted_resolved_entity, select_cases
        from autophone.observation.frida import api_oracle
        from autophone.observation.joint_verdict import arbitrate_joint_verdict

        try:
            cases = (
                list(selected_cases)
                if selected_cases is not None
                else select_cases(load_cases(cases_path), selector=selector)
            )
        except Exception:  # noqa: BLE001
            cases = []
        cases_by_id = {
            str(case.get("id") or "").upper(): case
            for case in cases
            if isinstance(case, Mapping) and case.get("id")
        }
        if not any(case_has_entity_binding(c) for c in cases_by_id.values()):
            return result

        env_ctx = api_oracle.fixture_context_vars_from_env()
        app_fixtures = (
            profile.api_fixtures_path()
            if hasattr(profile, "api_fixtures_path")
            else None
        )
        for outcome in getattr(result, "outcomes", []) or []:
            if getattr(outcome, "joint_verdict", None) is not None:
                continue
            cid = str(getattr(outcome, "id", "") or "").upper()
            case_def = cases_by_id.get(cid)
            if case_def is None or not case_has_entity_binding(case_def):
                continue
            raw_status = str(getattr(outcome, "status", "") or "")
            if raw_status in ("error", "blocked", "skipped"):
                continue
            ui_status = str(getattr(outcome, "ui_status", "") or raw_status)
            outcome.ui_status = ui_status
            fixture_ctx = {
                **env_ctx,
                **(api_oracle.flat_fixture_vars(app_fixtures, cid) if app_fixtures is not None else {}),
            }
            runtime_ctx = dict(getattr(outcome, "api_assertion_context", None) or {})
            resolved = resolve_case_entity(
                case_def,
                fixture_vars=fixture_ctx,
                runtime_context=runtime_ctx,
            )
            if getattr(outcome, "resolved_entity", None) is None and resolved is not None:
                outcome.resolved_entity = _redacted_resolved_entity(resolved)
            jv = arbitrate_joint_verdict(
                ui_status=ui_status,
                api_status=None,
                api_assertions=[],
                api_captured=False,
                readback_record=getattr(outcome, "entity_readback", None),
                entity_binding=case_def.get("entity_binding"),
                resolved_entity=resolved,
            )
            outcome.joint_verdict = jv
            if jv.get("status") != "pass":
                outcome.status = str(jv.get("status") or "fail")
                if ui_status == "pass" or not getattr(outcome, "fail_kind", ""):
                    outcome.fail_kind = str(jv.get("fail_kind") or "")
                    detail_msg = str(jv.get("detail") or "")
                    if capture_skip_reason and outcome.fail_kind == "api_evidence_missing":
                        detail_msg = (
                            f"{detail_msg} ({capture_skip_reason})"
                            if detail_msg
                            else str(capture_skip_reason)
                        )
                    outcome.detail = detail_msg
        return result

    def _reject_uncaptured_api_required(self, spec: dict[str, Any], profile: AppProfile) -> None:
        """submit 预检：`api=required` 但本 run 不会抓取 → fail-fast，别让它永久 pending。

        登录态闸前移后，API 抓取对「需登录态」或「显式要求 API 抓取（如 entity_binding.api_window）」
        的 run 启用；纯登录验证类且无 API 抓取需求的 run 默认禁 capture（见 _run_cases）。此时若
        spec 显式要求 `api=required`，提交即拒。`disabled`/`advisory` 不受影响。

        fail-closed：`_login_gate_applies` 加载失败返回 True（按需登录处理）→ 不误拒。
        """
        if plane_mode(spec, "api") != "required":
            return
        selector = spec.get("case_selector") or {}
        if (
            not self._login_gate_applies(profile.cases_path(), selector)
            and not self._api_capture_applies(profile.cases_path(), selector)
        ):
            raise ContractError(
                "api=required 与纯登录验证 selector 冲突：登录验证类 run（被选用例无 required，"
                "默认禁用 API 抓取）永远采不到接口数据，overall 会卡在 pending。请把 api 降为 "
                "disabled/advisory，或改选包含「需登录」或声明 entity_binding API 断言的用例。"
            )

    def _login_precondition_conflict(
        self,
        cases_path: Path,
        selector: dict[str, Any],
        *,
        selected_cases: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """混跑检测：被选用例是否混入互斥的登录前置。返回非 None = 检出冲突（供
        _partition_by_login_precondition 决定是否拆成有序分组）。

        `required`（需已登录）与 `logged_out`（需登出态）是**互斥前置**——一遍跑无法同时满足
        （登录态闸前移后更明显：pre-login 把态推成 persisted，logged_out 用例必然踩空）。检出后
        **不再整 run 阻塞**，而是自动拆成有序分组（logged_out 组先跑）分别执行。`any` 与两者都
        相容，不触发冲突。

        fail-open：加载/选择异常返回 None（不误判为冲突；真正的登录态保护由 _login_gate_applies +
        _ensure_login_state 兜底，那两处对加载失败各自 fail-closed）。
        """
        from autophone.execution.engine.cases_loader import load_cases
        from autophone.execution.engine.runner import select_cases

        try:
            selected = (list(selected_cases) if selected_cases is not None else
                        select_cases(load_cases(cases_path), selector=selector))
        except Exception:  # noqa: BLE001 加载/选择异常 → 不在此误拦
            return None
        preconds = {(c.get("login_precondition") or "required") for c in selected}
        if "required" in preconds and "logged_out" in preconds:
            return (
                "login_precondition 冲突：本 run 同时选中「需登录」(required) 与「需登出」"
                "(logged_out) 用例，前置态互斥——自动拆成有序分组（logged_out 组先跑）分别执行"
            )
        return None

    def _ensure_login_state(
        self, shot_dir: Path, *, auto_login: bool = False,
        profile: AppProfile | None = None, run_dir: Path | None = None,
    ) -> str | None:
        import time

        from autophone.execution.fixtures.login_state import probe_login_state

        # 冷启动竞态：uiopen 返回 pid ≠ App 已前台化渲染完业务首页。pid 一出现 launch_app
        # 就返回，此刻屏幕可能还停在桌面/启动画面，单次探测会把桌面误判成 unknown。
        # 对 unknown 重试（needs_login/persisted 都是确定态，立即返回不重试），给冷启动留时间。
        attempts = 6
        delay_s = 1.0
        result = None
        # 自动补登只尝试一次：补登后回到 loop 复探，仍 needs_login 则 fail-fast（防死循环）。
        did_auto_login = False
        for attempt in range(1, attempts + 1):
            try:
                result = probe_login_state(live=True, out_dir=shot_dir / "login_state")
            except Exception as exc:  # noqa: BLE001
                logger.error("登录态探测异常：%s: %s", type(exc).__name__, exc)
                return f"login_state unknown: {type(exc).__name__}: {exc}"
            if result.status == "persisted":
                logger.info("登录态 ok (persisted)%s", f"（第 {attempt} 次探测）" if attempt > 1 else "")
                return None
            if result.status == "needs_login":
                # 仅当 run 显式带 auto_login flag 时才补登；缺省保持 fail-fast，
                # 让「验证登录失败」等用例（需停在登出态）不被自动登录污染。
                if auto_login and not did_auto_login:
                    logger.warning("登录态 needs_login，run 带 auto_login → 自动补登中 — %s", result.detail)
                    blocker = self._auto_login(shot_dir, profile=profile, run_dir=run_dir)
                    if blocker:
                        return blocker
                    did_auto_login = True
                    continue  # 补登后回到 loop 头重新探测确认
                logger.warning("登录态未就绪：needs_login — %s", result.detail)
                return f"login_state needs_login: {result.detail}"
            # unknown：可能 App 还在冷启动过渡，等一会重试
            if attempt < attempts:
                logger.debug(
                    "登录态 unknown（第 %d/%d 次，App 可能仍在冷启动）：%s — %.1fs 后重试",
                    attempt, attempts, result.detail, delay_s,
                )
                time.sleep(delay_s)
        logger.warning("登录态未就绪（重试 %d 次仍 unknown）：%s", attempts, result.detail)
        return f"login_state {result.status}: {result.detail}"

    def _auto_login(
        self, shot_dir: Path, *, profile: AppProfile | None = None, run_dir: Path | None = None,
    ) -> str | None:
        """needs_login 时在登录闸内自动补登。

        补登序列由 App 插件注册的 ``auto_login`` 能力提供（``apps/<app>/plugin.py``），
        签名为 ``do_login(out_dir, **kwargs) -> dict``，返回值至少含 ``stage``。
        成功返回 None（交回 loop 复探确认）；不可用/失败返回 needs_login 阻塞串，
        让 run 仍按 fail-fast 中止——补登是尽力而为，绝不把失败掩盖成已登录。

        profile 开启 AX 时，给 do_login 一个延迟打开的 AX 会话，只用于读回手机号输入框的值
        （BL-083 第五轮：整帧 OCR 0.5 低于读回阈值 0.6，字段实际已显示目标号）。do_login 在
        手机号步骤结束时关闭它；AX 不可用时 do_login 退回裁剪 OCR。
        """
        login_dir = shot_dir / "auto_login"
        login_dir.mkdir(parents=True, exist_ok=True)
        try:
            from autophone.app_plugins import app_capability

            if profile is None:
                raise ValueError("App Profile is required to resolve auto_login")
            do_login = app_capability(profile.app, "auto_login")
        except Exception as exc:  # noqa: BLE001 App 未注册补登能力或插件依赖缺失
            logger.error("自动补登不可用（App 未提供 auto_login 能力）：%s", exc)
            return f"login_state needs_login: 自动补登不可用（{type(exc).__name__}: {exc}）"
        # login_wait>0：空帧（黑帧/转场）兜底轮询上限；有内容的帧单帧即定论。
        login_kwargs: dict[str, Any] = {"login_wait": 8.0}
        readback_ax = self._mobile_readback_ax(profile, run_dir)
        if readback_ax is not None:
            login_kwargs["mobile_readback_ax"] = readback_ax
        try:
            login = do_login(login_dir, **login_kwargs)
        except Exception as exc:  # noqa: BLE001 登录序列异常
            logger.error("自动补登异常：%s: %s", type(exc).__name__, exc)
            return f"login_state needs_login: 自动补登异常（{type(exc).__name__}: {exc}）"
        finally:
            if readback_ax is not None:
                try:
                    readback_ax.close()
                except Exception:  # noqa: BLE001 收尾失败不改变补登结论
                    logger.debug("手机号读回 AX 会话收尾失败", exc_info=True)
        readback = [r for r in (login.get("mobile_readback") or []) if "source" in r]
        if readback:
            logger.info(
                "自动补登手机号读回：%d 次，来源 %s，最后一次 matched=%s",
                len(readback), ",".join(sorted({str(r.get("source")) for r in readback})),
                readback[-1].get("matched"),
            )
        # do_login 的判定（stage、probe 来源、验证码尝试）落盘：2026-10-11 BL-083 首轮只在
        # run.log 留下 stage，auto_login/ 目录为空，无法从产物还原 probe-first 的判定依据。
        try:
            write_json(login_dir / "do_login_result.json", login)
        except Exception as exc:  # noqa: BLE001 证据落盘失败不改变补登结论
            logger.debug("自动补登结果落盘失败：%r", exc)
        stage = login.get("stage")
        if stage in {"logged_in", "already_in"}:
            logger.info("自动补登完成：stage=%s", stage)
            if login.get("late_login"):
                logger.info("自动补登为晚到登录：%s", login["late_login"])
            return None
        if stage == "uncertain" and login.get("app_alive") is True:
            # 偶发“认证已持久化、登录页仍未切走”：do_login 的终态窗判
            # uncertain，但冷启后直接落已登录首页。这里只做一次冷启并把控制权交回
            # _ensure_login_state 的权威 persisted/needs_login 复探；若认证其实失败，
            # 复探仍会看到登录页并 fail-fast，不会把 uncertain 假放成已登录。
            try:
                from autophone.execution.engine.actions import CaseContext

                ctx = CaseContext(
                    case_id="_auto_login_recover",
                    shot_dir=login_dir / "relaunch",
                    timeout=15.0,
                )
                ctx.relaunch_app(str(getattr(profile, "bundle_id", "") or ""))
            except Exception as exc:  # noqa: BLE001 冷启失败仍按登录阻塞收口
                logger.warning(
                    "自动补登状态未定且冷启恢复失败：%s: %s",
                    type(exc).__name__,
                    exc,
                )
                return (
                    "login_state needs_login: 自动补登状态未定且冷启恢复失败"
                    f"（{type(exc).__name__}: {exc}）"
                )
            logger.warning("自动补登 stage=uncertain；活进程已冷启，交回登录闸复探持久会话")
            return None
        logger.warning("自动补登失败：stage=%s — %s", stage, login)
        return f"login_state needs_login: 自动补登失败（stage={stage}）"

    @staticmethod
    def _mobile_readback_ax(profile: AppProfile | None, run_dir: Path | None) -> Any | None:
        """自动补登手机号读回用的 AX 会话（延迟打开）；profile 未开 AX 或构造失败时为 None。"""
        if profile is None or run_dir is None:
            return None
        try:
            from autophone.device.ax_session import UnsupportedAxBackendError
            from autophone.observation import capture as capture_mod

            session = capture_mod.build_run_ax_session(
                profile, run_dir, attach_timeout_s=MOBILE_READBACK_AX_ATTACH_TIMEOUT_S,
            )
        except UnsupportedAxBackendError:
            raise
        except Exception as exc:  # noqa: BLE001 AX 只是读回首选来源，不可用时 do_login 走裁剪 OCR
            logger.warning("自动补登手机号读回的 AX 会话构造失败，改用裁剪 OCR：%s: %s",
                           type(exc).__name__, exc)
            return None
        if session is None:
            return None
        return capture_mod.LazyAxSession(session)

    def _case_meta_snapshot(self, profile: AppProfile) -> dict[str, dict[str, Any]]:
        """run 启动时读 cases YAML，按 id 钉住 {revision, status} 快照。

        缺字段时回退 revision=1 / status=""；未迁移条目不得被推断为 Active。
        """
        from autophone.execution.engine.cases_loader import load_cases

        snapshot: dict[str, dict[str, Any]] = {}
        try:
            rows = load_cases(profile.cases_path())
        except Exception:  # noqa: BLE001 — 快照失败不应阻断 run，退化为无 revision
            return snapshot
        for case in rows:
            cid = str(case.get("id") or "")
            if not cid:
                continue
            rev = case.get("revision")
            snapshot[cid] = {
                "revision": int(rev) if isinstance(rev, int) else 1,
                "status": str(case.get("status") or ""),
            }
        return snapshot

    def _screen_asset_doc(self, profile: AppProfile) -> dict[str, Any] | None:
        """Best-effort A5 screen asset load for blocking-overlay recovery & tap_ax screen_id."""
        path: Path | None = None
        try:
            candidate = profile.screens_path()
            if candidate.exists():
                path = candidate
        except Exception:
            path = None
        if path is None:
            fallback = profile.profile_dir / "screens.yaml"
            if fallback.exists():
                path = fallback
        if path is None:
            return None
        try:
            from autophone.assets.screens import load_screen_asset

            return load_screen_asset(path)
        except Exception:  # noqa: BLE001 - screen assets are optional for runner
            logger.warning("screen asset unavailable for app=%s path=%s", profile.app, path, exc_info=True)
            return None

    def _package_snapshot_for_app(self, app: str) -> dict[str, Any]:
        try:
            profile = load_app_profile(app)
        except Exception:
            profile = None
        return self._package_snapshot_for_profile(profile, app=app)

    def _package_snapshot_for_profile(
        self,
        profile: AppProfile | None,
        *,
        app: str | None = None,
        device_observation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build snapshot for a live app query, not for a run result.

        Run results project the frozen submission sidecar (``_package_snapshot``)
        so a package added or installed mid-run cannot rewrite the identity of
        what ran.  This path answers "what is on the Target right now", so it
        reads the current install receipt on purpose.
        """
        app_name = app or (profile.app if profile is not None else "")
        root = profile.host_state_root if profile is not None else STATE_ROOT
        ref = None
        if app_name:
            try:
                receipt = package_store.load_target_receipt(app_name, root=root)
                ref = dict(receipt.get("build_ref") or {}) if receipt else None
            except Exception:
                logger.debug("package snapshot unavailable for app=%s", app_name, exc_info=True)
                ref = None
        observation = dict(device_observation) if device_observation else None
        return {
            "active_slot": str(ref["slot"]) if ref and ref.get("slot") else None,
            "active_version": (
                str(ref["short_version"]) if ref and ref.get("short_version") else None
            ),
            "active_sha256": (
                str(ref["artifact_sha256"]) if ref and ref.get("artifact_sha256") else None
            ),
            "device_version": (
                str(observation.get("short_version")) if observation and observation.get("short_version") else None
            ),
            "profile_build_label": profile.build_label if profile is not None else None,
            "registry_build_ref": ref,
            "device_build_observation": observation,
            "build_identity_match": _build_identity_match(ref, observation),
        }

    @staticmethod
    def _package_snapshot(run_dir: Path, spec: dict[str, Any]) -> dict[str, Any]:
        from autophone.assets import build_identity
        from autophone.execution import run_submission

        return run_submission.package_projection(
            run_dir,
            expected_run_id=str(spec.get("run_id") or run_dir.name),
            expected_app=str(spec.get("app") or ""),
            device_short_version=build_identity.device_short_version(run_dir),
            device_observation=build_identity.device_observation(run_dir),
            probe_match=(build_identity.load_document(run_dir) or {}).get("match"),
        )

    @staticmethod
    def _probe_build_identity(
        profile: AppProfile,
        run_dir: Path,
        *,
        ready_evidence: dict[str, Any] | None,
    ) -> None:
        """Read the installed version tuple without changing the Run verdict."""
        from autophone.assets import build_identity
        from autophone.execution import run_submission
        from autophone.device.uds import request

        run_id = run_dir.name
        app = profile.app
        submission = run_submission.load(run_dir, run_id, app)
        if submission.registry_candidate_ref and submission.registry_candidate_ref.get("candidate_kind") == "rn_update":
            from autophone.execution.candidate_lifecycle import reprobe_run_candidate
            reprobe_run_candidate(profile, run_dir, run_id)
            return
        try:
            manifest = package_store.load_manifest(
                app,
                root=profile.host_state_root,
                code_root=profile.root,
            )
            world_subject = _probe_world_installed_subject(run_dir, submission)
            installed_ref = run_submission.installed_comparison_basis(
                submission, world_subject=world_subject,
            )[0]
            expected_marker: bytes | None = None
            if installed_ref is not None:
                try:
                    from autophone.packages.build_identity import expected_build_marker

                    expected_marker = expected_build_marker(
                        installed_ref,
                        root=profile.host_state_root,
                        code_root=profile.root,
                    )
                except Exception:  # noqa: BLE001 - absent/unverifiable marker stays unconfirmed
                    expected_marker = None
            observation, match, epoch = build_identity.probe(
                bundle_id=profile.bundle_id,
                request_fn=request,
                submission_ref=submission.registry_build_ref,
                registry_entries=manifest.get("packages") or [],
                registry_schema_version=manifest.get("schema_version"),
                ready_evidence=ready_evidence,
                installed_ref=installed_ref,
                expected_build_marker=expected_marker,
                world_subject=world_subject,
            )
            build_identity.write_probe(
                run_dir,
                run_id=run_id,
                app=app,
                observation=observation,
                match=match,
                epoch=epoch,
            )
        except Exception as exc:  # noqa: BLE001 - absence is structured evidence
            build_identity.write_unavailable(
                run_dir,
                run_id=run_id,
                app=app,
                reason=f"device_probe_failed:{type(exc).__name__}:{exc}",
            )
            logger.warning("Build Identity 读取失败 app=%s: %s", app, exc)

    def assemble_result(
        self,
        spec: dict[str, Any],
        profile: AppProfile,
        phase2_result: Any,
        timestamps: dict[str, str],
        phase_status: dict[str, Any],
        telemetry: dict[str, Any] | None = None,
        case_meta: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return RunResultAssembler().assemble(
            spec,
            phase2_result,
            package=self._package_snapshot(self._run_dir(str(spec["run_id"])), spec),
            env=profile.env,
            timestamps=timestamps,
            phase_status=phase_status,
            telemetry=telemetry,
            case_meta=case_meta,
            ax_backend=getattr(profile, "ax_backend", None),
        )
