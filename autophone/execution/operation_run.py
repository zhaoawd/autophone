"""operation_run.py — type=operation 的 run 装配（与 verification 对称）。

把一条操作引用（RunSpec.operation: {name, with}）解析成可落盘的 OperationResult：
按操作库的 param 契约解析入参（fail-loud）、把操作 steps 展开成逐步条目，产出
operation-result.json + operation-report.html。

没有执行器的 Operation 保持 status=pending_execution。内置执行器、typed IR 合成执行器或
AUTOPHONE_OPERATION_EXECUTOR_RESULT_JSON 提供的执行器产物通过同一结果合并出口形成 OperationResult。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import ExitStack
import html
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from autophone.paths import APP_ASSET_ROOT
from autophone.assets.apps import AppProfile
from autophone.contracts import ContractError, validate_or_raise, write_json
from autophone.assets.operations import OperationError, load_operations, resolve_operation
from autophone.assets.operations import _validate_args as _validate_operation_args

# aborted：run 级中止信号（autophone/execution/run_abort.py）在启动前或 typed-IR 逐步边界被读到，
# 执行协作停下。平台 RunResult 映射为 blocked，与用例回归路径 RunResult(aborted=True) 同口径。
OPERATION_STATUSES = {"pending_execution", "passed", "failed", "blocked", "error", "aborted"}
STEP_STATUSES = {"pending", "passed", "failed", "skipped"}
from autophone.execution.operation_adapters import operation_binding, operation_runtime

_OPERATION_RUNTIME = operation_runtime


#: 声明了这些能力需求的 Operation（`register_operation(..., requires=...)`）在 typed-IR 合成执行器
#: 回放时获得 App 的 AX 会话。既支持传统 ``frida`` 需求（``frida_inproc``），也支持免 Frida 的
#: ``ax`` 需求（``guest_axruntime``）；会话仍按 Profile 的 ``capabilities.ax.enabled`` 与
#: ``capabilities.ax.backend`` 构造，与回归 Run 的注入方式一致。
AX_SESSION_REQUIREMENTS = frozenset({"frida", "ax"})
#: 直接依赖 AX 会话的 typed-IR action 动词（即使 Operation 未声明 ``requires={"frida"}``，
#: 在 Profile 开启 ``capabilities.ax.enabled`` 时也注入延迟打开的 AX 会话）。
AX_SESSION_ACTIONS = frozenset({"tap_ax"})
# 只因 Operation 声明 AX 需求而构造的会话：单次 frida attach 的等待上限（秒）。
# 2026-09-24 vm op1a、op1c 的 attach 各阻塞约 25s 后失败。
OPERATION_AX_ATTACH_TIMEOUT_S = 10.0


def steps_need_ax_session(app: str | None, steps: Sequence[Any], *, root: Path = APP_ASSET_ROOT) -> bool:
    """typed-IR steps 是否需要 AX 会话（直接使用 AX action 或调用声明了 AX/Frida 需求的 Operation）。

    须在 App recipes 导入之后调用。"""
    from autophone.execution.engine.recipe import operation_requirements_for_app

    for step in steps:
        if not isinstance(step, Mapping):
            continue
        if str(step.get("action") or "") in AX_SESSION_ACTIONS:
            return True
        if (
            app
            and step.get("operation")
            and operation_requirements_for_app(app, str(step["operation"]), root=root)
            & AX_SESSION_REQUIREMENTS
        ):
            return True
    return False


_PARAM_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "file": lambda v: isinstance(v, str),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "bool": lambda v: isinstance(v, bool),
}


def is_bare_self_call(steps: Sequence[Any], name: str) -> bool:
    """注册 steps 是否恰好是一条不带参数的 ``{operation: <name>}``。"""
    if len(steps) != 1 or not isinstance(steps[0], Mapping):
        return False
    step = steps[0]
    return (
        not set(step) - {"operation", "args"}
        and str(step.get("operation") or "") == name
        and not step.get("args")
    )


def bind_self_call_args(
    raw_steps: Sequence[Any],
    name: str,
    args: Mapping[str, Any] | None,
    params: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[Any], dict[str, Any] | None]:
    """§10.10「args 不插值进 steps」的唯一窄例外（BL-038，用户 2026-09-24 裁定）。

    条件：注册 steps 恰好只有一步，该步是 ``{operation: <本 Operation 名>}``，且不带 ``args``
    （或为空）。满足时把已按 Operation 参数契约解析的 ``with`` 参数原样交给这一步，效果等同内置
    执行器绑定；返回的绑定记录写入 operation-result 的 ``step_args_binding`` 供审计。其他情形
    （多步、调用其他 Operation、步骤自带参数、action 步骤）一律不插值，原样返回。

    参数值须符合声明的 ``type``（``_validate_args`` 只查键与枚举，不查类型）；不符时抛
    ``OperationError``，不派发。枚举与未知键已由 RunSpec 解析阶段拒绝。
    """
    steps = list(raw_steps)
    bound = dict(args or {})
    if not bound or not is_bare_self_call(steps, name):
        return steps, None
    declared = {str(p.get("name")): p for p in params or [] if isinstance(p, Mapping)}
    for key, value in bound.items():
        spec = declared.get(key)
        if spec is None:
            raise OperationError(f"操作 {name} 不接受参数：{key}")
        check = _PARAM_TYPE_CHECKS.get(str(spec.get("type") or ""))
        if value is not None and check is not None and not check(value):
            raise OperationError(
                f"操作 {name} 参数 {key}={value!r} 类型不符（声明 {spec.get('type')}）"
            )
    return (
        [{"operation": name, "args": bound}],
        {"source": "with", "step_index": 1, "operation": name, "args": bound},
    )


def is_typed_ir_steps(steps: Any) -> bool:
    """steps 是否是可被 case-IR dispatcher 直接执行的 typed IR（每条都是 {action|operation} 字典）。
    全 prose 字符串（distilled/legacy）或混合 → False：无合成执行器，保持 pending（旧行为不变）。"""
    return bool(steps) and all(isinstance(s, dict) for s in steps)


def raw_operation_def(profile: AppProfile, name: str | None = None, *, op_id: str | None = None) -> dict[str, Any]:
    """Return an operation definition by id or active name from the raw registry."""
    import yaml

    path = profile.operations_path()
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    operations = doc.get("operations") or []
    if op_id:
        for op in operations:
            if str(op.get("id")) == op_id:
                return op
        return {}
    if name:
        active = [op for op in operations if op.get("name") == name and op.get("status") == "active"]
        if len(active) > 1:
            raise OperationError(f"active 操作 name 重复：{name}（{path}）")
        return active[0] if active else {}
    return {}


def raw_steps_of(profile: AppProfile, name: str, *, op_id: str | None = None) -> list[Any]:
    """操作库里钉版本的**原始** steps（prose 字符串或 typed IR 字典，未展开成 {index,text,status}）。
    service 用它判 device_prep + 喂 run_operation_executor 的 raw_steps——避免把展开后的逐步条目误当
    可执行 IR。操作不存在则空列表（上层 resolve 会先 fail-loud）。"""
    return raw_operation_def(profile, name, op_id=op_id).get("steps") or []


def _step_text(step: Any) -> str:
    """把一条 step 渲染成 operation-result 的人读 `text`。prose 字符串原样；typed IR 渲染成紧凑
    摘要（'action tap_text query=订单' / 'operation reset_to_home entry=我的'），让报告里 dict step 可读。"""
    if isinstance(step, dict):
        if "action" in step:
            params = " ".join(f"{k}={v}" for k, v in step.items() if k != "action")
            return f"action {step['action']}" + (f" {params}" if params else "")
        if "operation" in step:
            args = step.get("args") or {}
            params = " ".join(f"{k}={v}" for k, v in args.items())
            return f"operation {step['operation']}" + (f" {params}" if params else "")
    return str(step)


def load_operation_spec(spec: dict[str, Any], profile: AppProfile) -> dict[str, Any]:
    """从 RunSpec 解析操作引用，产出 pending 的 OperationResult（fail-loud 校验）。"""
    from autophone.observation.capture import ensure_ax_backend_supported

    ensure_ax_backend_supported(profile)
    declaration = dict(spec.get("operation") or {})
    name = str(declaration.get("name") or "").strip()
    if not name:
        raise ContractError("type=operation 的 RunSpec 必须带 operation.name")
    # 转正归因：带 draft_id 即「显式回放这条 draft」——放行 draft 状态（否则 M2 死锁：draft 永远跑不起来
    # 就攒不出连续 passed、过不了冻结门），并钉死 draft_id 必须就是被解析操作的 id（防止把一条 active 操作
    # 的 passed 误归因到任意 draft，污染连续 passed 统计）。无 draft_id 仍只认 active（铁律不变）。
    draft_id = str(declaration.get("draft_id") or "").strip()
    if draft_id:
        op = raw_operation_def(profile, op_id=draft_id)
        if not op:
            raise OperationError(f"draft operation 未找到：{draft_id}（app={profile.app}）")
        if op.get("status") not in {"active", "draft"}:
            raise OperationError(f"操作 {draft_id} 状态为 {op.get('status')!r}，仅 active/draft 可显式回放")
        if name and name != str(op.get("name") or ""):
            raise OperationError(
                f"draft_id={draft_id} 指向操作 {op.get('name')!r}，与 RunSpec operation.name={name!r} 不一致"
            )
        resolved_args = _validate_operation_args(op, declaration.get("with") or {})
        resolved_name = str(op["name"])
        resolved_id = str(op["id"])
        resolved_revision = int(op["revision"])
        resolved_summary = str(op.get("summary") or "")
    else:
        resolved = resolve_operation(profile, name, declaration.get("with") or {}, allow_statuses=("active",))
        op = raw_operation_def(profile, name, op_id=resolved.id)
        resolved_args = resolved.args
        resolved_name = resolved.name
        resolved_id = resolved.id
        resolved_revision = resolved.revision
        resolved_summary = resolved.summary

    # steps 从操作库定义取（钉版本），展开成逐步条目；execute 步暂标 skipped。
    steps = [
        {"index": i, "text": _step_text(text), "status": "pending"}
        for i, text in enumerate(op.get("steps") or [], 1)
    ]
    result: dict[str, Any] = {
        "schema_version": "v1",
        "run_id": str(spec["run_id"]),
        "app": str(spec["app"]),
        "operation": {"name": resolved_name, "id": resolved_id, "revision": resolved_revision},
        "summary": resolved_summary,
        "args": resolved_args,
        "steps": steps,
        "status": "pending_execution",
        "detail": "operation executor is scaffolded; execution is not wired yet",
    }
    # 转正归因（M2，record-replay-design §五）：draft_id 已在上方校验（必须 == resolved.id），
    # 随 dict(operation) 浅拷在执行器/掩码/落盘间流转。与 verification.draft_id 对称。
    if draft_id:
        result["draft_id"] = draft_id
    return result


def _exec_typed_ir(
    run_dir: Path,
    steps: list[dict[str, Any]],
    *,
    replay_speed: float = 1.0,
    replay_timing: list[float] | None = None,
    screen_asset_path: str | Path | None = None,
    screen_expectations: dict[Any, str] | None = None,
    screen_starts_with: str | None = None,
    screen_statuses: tuple[str, ...] = ("active",),
    screen_telemetry_statuses: tuple[str, ...] = ("draft",),
    checkpoint_ocr_runner: Callable[[Path], dict[str, Any]] | None = None,
    checkpoint_ax: Any = None,
    checkpoint_identity: dict | None = None,
    checkpoint_retries: int = 3,
    checkpoint_interval_s: float = 0.2,
    allowed_effects: set[str] | None = None,
    ax_backend: str | None = None,
    effect_mode: str | None = None,
    trial_data_isolation: str = "unisolated",
    abort_check: Callable[[], str | None] | None = None,
    capture_session: Any = None,
) -> dict[str, Any]:
    """合成执行器：一条纯 typed-IR operation 没有手写执行器，就用 case-IR dispatcher 顺序派发它的
    steps 来真跑——这是「AI-4 起草 → 跑 → 转正」闭环里缺的那段「跑」（A）。逐步 dispatch + 快照分级：
    完成→passed；StepFailure→failed（其后步骤 skipped）；INFRA_ERRORS→error。**operation 合法零
    assert**（断言属用例，schema 禁 operation 有 expects），故全程跑通即 passed，不套 case recipe 的
    vacuous→error 规则。逐步循环（非一次性 run_recipe）是为了给 operation-result 每步独立状态——
    operation 步可展开成多个 ActionResult，按 ctx.results 对不齐 registry step，故按 step 快照。
    args 不插值进 steps（插值=DSL，§10.10 否决）：第一版 params 仅声明/文档，steps 字面执行，与 case IR 一致。
    唯一例外由调用方 `bind_self_call_args` 决定：注册 steps 恰好是一条不带参数的自调用时，传入的
    steps 已带上经契约校验的 `with` 参数；本函数不区分，仍字面执行。

    倍速回放（record-replay-design §倍速回放）：每步 dispatch **成功后**按 `replay_timing[i]`（录制真实
    post-gap）÷ `replay_speed` 睡一段 dwell，并在 dwell 末尾抓一张 `replay_after_*.jpg` 取数。dwell 只动
    `time.sleep`，**绝不碰 dispatch 内部的 `_settle` 与断言**——倍速纯观感/取数，对 pass-fail 零影响。
    `replay_timing` 缺省/失配 → 每步均匀 DWELL_MIN（dwell_ms(0,speed)）。截图失败 **fail-open**：记
    WARNING、该步 shot 标 null，不拖垮回放（第一阶段；后续在这个钩子上接 API/页面元素取数）。

    协作式中止（BL-062）：`abort_check` 默认 None，行为不变。传入时在起点 checkpoint 前与每一步
    dispatch 前读一次（与用例回归 runner 的逐用例边界同一形态：返回人读原因串即中止）。见到中止：
    status=aborted、detail=原因，当前步及其后步骤记 skipped，不再 dispatch、不再做后续 checkpoint；
    已执行步骤的结果与副作用保留，不回滚。正在执行的一步不被抢占，dwell 与该步 checkpoint 照常完成。

    屏骨架 checkpoint 分级（BL-060，screen-skeleton-context §十一.6）：期望 screen 在资产里的 status
    属于 `screen_statuses`（默认 active）→ 原 fail-fast 判定，行为不变；属于 `screen_telemetry_statuses`
    （默认 draft）且不属于 `screen_statuses` → 只出遥测（`_telemetry_checkpoint_screen`），该步与整条 run
    的状态不受匹配结果影响。期望 screen 不在资产里或为其他 status → 按 fail-fast 处理（与原行为一致）。"""
    import logging

    from autophone.record import replay_timing as rt
    from autophone.assets import screens as screen_assets
    from autophone.device.ocr_target import run_ocr
    from autophone.execution.contracts import CaseBlocked
    from autophone.execution.engine.actions import CaseContext, StepFailure, resolve_sock
    from autophone.execution.engine.recipe import INFRA_ERRORS, dispatch_step, recipe_uses_symbols

    log = logging.getLogger(__name__)
    step_blocker: dict[str, Any] | None = None
    failure_context: dict[str, Any] = {}
    shot_dir = run_dir / "shots"
    resolved_ax_backend = ax_backend or getattr(checkpoint_ax, "ax_backend", None)
    ctx = CaseContext(case_id="operation", shot_dir=shot_dir,
                      sock_path=resolve_sock(), timeout=15.0,
                      allowed_effects=allowed_effects, capture=capture_session,
                      suppress_overlay_dismiss=recipe_uses_symbols(steps),
                      ax_backend=resolved_ax_backend,
                      effect_mode=effect_mode,
                      trial_data_isolation=trial_data_isolation)
    screen_doc = screen_assets.load_screen_asset(Path(screen_asset_path)) if screen_asset_path else None
    ctx.screen_asset_doc = screen_doc
    ctx.ax = checkpoint_ax
    ctx.checkpoint_identity = checkpoint_identity
    checkpoint_ocr = checkpoint_ocr_runner or run_ocr
    expectations = _normalize_screen_expectations(screen_expectations or {})

    def _dwell_and_grab(i: int) -> str | None:
        """dispatch 成功后：按倍速睡 dwell，末尾抓一张 after-shot（fail-open，返回文件名或 None）。"""
        post_gap = replay_timing[i - 1] if replay_timing and i - 1 < len(replay_timing) else 0.0
        time.sleep(rt.dwell_ms(post_gap, replay_speed) / 1000.0)
        try:
            shot_dir.mkdir(parents=True, exist_ok=True)
            fp = shot_dir / f"replay_after_{i:03d}.jpg"
            fp.write_bytes(ctx._grab_raw())
            return fp.name
        except Exception as e:  # noqa: BLE001 取数截图是 advisory，失败不拖垮回放（fail-open）
            log.warning("replay after-shot 取数失败（step %d）：%s: %s", i, type(e).__name__, e)
            return None

    def _abort_reason(boundary: str) -> str | None:
        if abort_check is None:
            return None
        reason = abort_check()
        if reason:
            log.warning("operation 回放协作式中止：%s（边界 %s，已执行 %d 步）",
                        reason, boundary, len(out_steps))
        return reason or None

    def _telemetry_only(expected: str) -> dict[str, Any] | None:
        """期望 screen 只出遥测时返回其资产条目，否则 None（走 fail-fast）。"""
        screen = _screen_entry(screen_doc, expected)
        status_ = str((screen or {}).get("status") or "")
        if screen is None or status_ in screen_statuses or status_ not in screen_telemetry_statuses:
            return None
        return screen

    telemetry_match_statuses = tuple(dict.fromkeys((*screen_statuses, *screen_telemetry_statuses)))

    def _telemetry_checkpoint(expected_screen: dict[str, Any], label: str) -> dict[str, Any]:
        checkpoint = _telemetry_checkpoint_screen(
            ctx,
            shot_dir,
            screen_doc=screen_doc,
            expected_screen=expected_screen,
            label=label,
            ocr_runner=checkpoint_ocr,
            statuses=telemetry_match_statuses,
            retries=checkpoint_retries,
            interval_s=checkpoint_interval_s,
        )
        if checkpoint["status"] != "passed":
            log.info("screen checkpoint 只出遥测、不判失败（%s）：%s", label, _checkpoint_detail(checkpoint))
        return checkpoint

    out_steps: list[dict[str, Any]] = []
    status, detail, stopped = "passed", "", False
    abort_reason = _abort_reason("start") if screen_doc is not None and screen_starts_with else None
    if abort_reason:
        status, detail, stopped = "aborted", abort_reason, True
    start_telemetry_screen = (
        _telemetry_only(screen_starts_with) if screen_doc is not None and screen_starts_with else None
    )
    if not stopped and start_telemetry_screen is not None:
        # 起点 checkpoint 没有对应的步骤行；只出遥测时不影响 run，结果只记日志（与 active 起点同样不落盘）。
        _telemetry_checkpoint(start_telemetry_screen, "start")
    elif not stopped and screen_doc is not None and screen_starts_with:
        checkpoint = _checkpoint_screen(
            ctx,
            shot_dir,
            screen_doc=screen_doc,
            expected=screen_starts_with,
            label="start",
            ocr_runner=checkpoint_ocr,
            statuses=screen_statuses,
            retries=checkpoint_retries,
            interval_s=checkpoint_interval_s,
        )
        if checkpoint["status"] != "passed":
            status = "failed"
            detail = _checkpoint_detail(checkpoint)
            stopped = True
    for i, step in enumerate(steps, 1):
        if stopped:  # 失败后续步骤全 skipped（与 _map_login_result 等内置执行器同口径）
            out_steps.append({"index": i, "status": "skipped", "detail": ""})
            continue
        # 中止协作点（逐步边界）：本步 dispatch 前读信号，见即停；本步及其后记 skipped。
        abort_reason = _abort_reason(f"before_step_{i:03d}")
        if abort_reason:
            status, detail, stopped = "aborted", abort_reason, True
            out_steps.append({"index": i, "status": "skipped", "detail": ""})
            continue
        try:
            # 记录该步骤开始前的动作数量，用于提取该步骤产生的所有动作
            results_before = len(ctx.results)
            dispatch_step(ctx, step)
            shot = _dwell_and_grab(i)
            # 提取该步骤产生的所有动作的 detail（operation 步骤可能产生多个动作）
            step_results = ctx.results[results_before:]
            if step_results:
                # 多个动作时，取第一个动作的 detail（与截图时序一致）
                detail = step_results[0].detail
            else:
                detail = ""
            row = {"index": i, "status": "passed", "detail": detail}
            if shot:
                row["replay_shot"] = shot
            expected_screen = expectations.get(i - 1)
            telemetry_screen = (
                _telemetry_only(expected_screen) if screen_doc is not None and expected_screen else None
            )
            if telemetry_screen is not None:
                row["screen_checkpoint"] = _telemetry_checkpoint(telemetry_screen, f"after_{i:03d}")
            elif screen_doc is not None and expected_screen:
                checkpoint = _checkpoint_screen(
                    ctx,
                    shot_dir,
                    screen_doc=screen_doc,
                    expected=expected_screen,
                    label=f"after_{i:03d}",
                    ocr_runner=checkpoint_ocr,
                    statuses=screen_statuses,
                    retries=checkpoint_retries,
                    interval_s=checkpoint_interval_s,
                )
                row["screen_checkpoint"] = checkpoint
                if checkpoint["status"] != "passed":
                    row["status"] = "failed"
                    row["detail"] = _checkpoint_detail(checkpoint)
                    status, detail, stopped = "failed", row["detail"], True
            out_steps.append(row)
        except StepFailure as e:
            from autophone.execution.failure_context import exception_stage, failure_fields
            failure_context = failure_fields(exception_stage(e, "operation_execution"))
            failure_context["_failure_local"] = bool(getattr(e, "_failure_local", False))
            out_steps.append({"index": i, "status": "failed", "detail": e.result.detail})
            status, detail, stopped = "failed", e.result.detail, True
        except CaseBlocked as e:
            from autophone.execution.failure_context import exception_stage, failure_fields
            failure_context = failure_fields(exception_stage(e, "operation_execution"))
            failure_context["_failure_local"] = bool(getattr(e, "_failure_local", False))
            # 前置条件缺口（如 AX 会话不可用）：与回归 Runner 同口径记为 blocked，不记为失败。
            out_steps.append({"index": i, "status": "failed", "detail": e.detail})
            status, detail, stopped, step_blocker = "blocked", e.detail, True, dict(e.blocker)
        except INFRA_ERRORS as e:
            from autophone.execution.failure_context import exception_stage, failure_fields
            failure_context = failure_fields(exception_stage(e, "operation_execution"))
            failure_context["_failure_local"] = bool(getattr(e, "_failure_local", False))
            out_steps.append({"index": i, "status": "failed", "detail": f"{type(e).__name__}: {e}"})
            status, detail, stopped = "error", f"{type(e).__name__}: {e}", True
    if status == "passed":
        pace = "原速" if replay_speed == 1.0 else f"{replay_speed:g}x"
        detail = f"typed-IR operation 完成（{len(steps)} 步经 dispatcher 驱动，{pace}回放）"
    payload: dict[str, Any] = {} if step_blocker is None else {"blocker": step_blocker}
    return {
        **payload,
        **failure_context,
        "status": status,
        "steps": out_steps,
        "detail": detail,
        # 只把动作名/时刻/耗时送给同进程 telemetry 归因；detail、输入文本、
        # 截图路径都不进入该 runtime-only seam。
        "action_trace": [
            {
                "action": result.action,
                "action_id": result.action_id,
                "subject_epoch_ref": result.subject_epoch_ref,
                "ok": result.ok,
                "elapsed_ms": result.elapsed_ms,
                "recorded_at": result.recorded_at,
            }
            for result in ctx.results
        ],
    }


def _normalize_screen_expectations(raw: dict[Any, str]) -> dict[int, str]:
    out: dict[int, str] = {}
    for key, value in raw.items():
        try:
            out[int(key)] = str(value)
        except (TypeError, ValueError):
            continue
    return out


def _checkpoint_build_evidence(run_dir: Path, identity: dict) -> dict | None:
    from autophone.assets import build_identity
    from autophone.execution import run_submission
    from autophone.observation.impact.comparison import digest

    if not (run_dir / "run-submission.json").is_file():
        return None  # Legacy/offline executor contract has no Package Store projection.
    try:
        package = run_submission.package_projection(
            run_dir, expected_run_id=identity['run_id'], expected_app=identity['app'],
            device_short_version=build_identity.device_short_version(run_dir),
            device_observation=build_identity.device_observation(run_dir),
            probe_match=(build_identity.load_document(run_dir) or {}).get('match'),
        )
        sources = {name: {'path': name, 'sha256': digest((run_dir / name).read_bytes())}
                   for name in ('run-submission.json', 'world-identity.json')}
        return {'package': package, 'sources': sources}
    except (OSError, ValueError, KeyError, TypeError):
        return {"package": {}, "sources": {}}



def _checkpoint_screen(
    ctx: Any,
    shot_dir: Path,
    *,
    screen_doc: dict[str, Any],
    expected: str,
    label: str,
    ocr_runner: Callable[[Path], dict[str, Any]],
    statuses: tuple[str, ...],
    retries: int,
    interval_s: float,
) -> dict[str, Any]:
    from autophone.assets import screens as screen_assets

    attempts = max(1, int(retries))
    last: dict[str, Any] = {
        "status": "failed",
        "expected": expected,
        "matched": None,
        "reason": "not_run",
    }
    for attempt in range(1, attempts + 1):
        shot_dir.mkdir(parents=True, exist_ok=True)
        shot = shot_dir / f"checkpoint_{label}_{attempt:02d}.jpg"
        try:
            shot.write_bytes(ctx._grab_raw())
            ocr = ocr_runner(shot)
            match = screen_assets.match_screens(ocr, screen_doc.get("screens") or [], statuses=statuses)
            matched = match.screen_id if match else None
            ok = matched == expected
            last = {
                "status": "passed" if ok else "failed",
                "expected": expected,
                "matched": matched,
                "reason": "matched" if ok else ("wrong_page" if matched else "unknown_screen"),
                # operation-result 的 shot 契约是 run-relative；图片实际落在
                # run_dir/shots/，不能只存 basename，否则 Web artifact URL 会指向根目录。
                "shot": shot.relative_to(shot_dir.parent).as_posix(),
                "attempt": attempt,
                "tokens_hit": list(match.tokens_hit) if match else [],
                "tokens_missed": list(match.tokens_missed) if match else [],
            }
            if ok:
                from autophone.observation.ax_checkpoint import capture
                last["ax_evidence"] = capture(
                    getattr(ctx, "ax", None), run_dir=shot_dir.parent,
                    identity=getattr(ctx, "checkpoint_identity", None) or {},
                    checkpoint=last, label=label,
                    build_evidence=_checkpoint_build_evidence(shot_dir.parent, ctx.checkpoint_identity),
                    ax_backend=getattr(ctx, "ax_backend", None),
                )
                return last
        except Exception as exc:  # noqa: BLE001 required checkpoint frame/ocr/match failure is a real failure
            from autophone.device.ax_session import UnsupportedAxBackendError
            if isinstance(exc, UnsupportedAxBackendError):
                raise
            last = {
                "status": "failed",
                "expected": expected,
                "matched": None,
                "reason": "checkpoint_error",
                "shot": shot.relative_to(shot_dir.parent).as_posix(),
                "attempt": attempt,
                "error": f"{type(exc).__name__}: {exc}",
            }
        if attempt < attempts:
            time.sleep(max(0.0, interval_s))
    return last


def _screen_entry(screen_doc: dict[str, Any] | None, screen_id: str) -> dict[str, Any] | None:
    for screen in (screen_doc or {}).get("screens") or []:
        if isinstance(screen, dict) and str(screen.get("id")) == screen_id:
            return screen
    return None


def _telemetry_checkpoint_screen(
    ctx: Any,
    shot_dir: Path,
    *,
    screen_doc: dict[str, Any],
    expected_screen: dict[str, Any],
    label: str,
    ocr_runner: Callable[[Path], dict[str, Any]],
    statuses: tuple[str, ...],
    retries: int,
    interval_s: float,
) -> dict[str, Any]:
    """只出遥测的屏骨架 checkpoint（BL-060，§十一.6：未冻结签名只出遥测、不 fail run）。

    与 `_checkpoint_screen` 同样自取 required frame、同样按 retries/interval 沉降重试，直到 clean match
    或用尽次数，记录最后一次。差异只在判定口径与字段，调用方不据此改变步骤或 run 状态：

    - 匹配集合为 `statuses`（active ∪ draft）。整帧同时命中多个签名 → `reason=signature_conflict`，
      不计 clean match（转正门把它记为 `signature_conflict_present`）。
    - `tokens_hit` / `tokens_missed` / `forbid_hit` 取**期望签名**对该帧的单独评估，非 clean 时如实写入，
      转正门据此记 `must_miss_present` / `forbid_conflict_present`。
    - 附加 `screen_status`（期望 screen 的资产状态）、`enforcement=telemetry_only`；命中某签名时附加
      `matched_status`。active 的 fail-fast 行不带这些字段。
    """
    from autophone.assets import screens as screen_assets

    expected = str(expected_screen["id"])
    base = {"screen_status": str(expected_screen.get("status") or ""), "enforcement": "telemetry_only"}
    status_by_id = {
        str(screen.get("id")): str(screen.get("status") or "")
        for screen in screen_doc.get("screens") or []
        if isinstance(screen, dict)
    }
    attempts = max(1, int(retries))
    last: dict[str, Any] = {
        "status": "failed",
        "expected": expected,
        "matched": None,
        "reason": "not_run",
        **base,
    }
    for attempt in range(1, attempts + 1):
        shot_dir.mkdir(parents=True, exist_ok=True)
        shot = shot_dir / f"checkpoint_{label}_{attempt:02d}.jpg"
        shot_rel = shot.relative_to(shot_dir.parent).as_posix()
        try:
            shot.write_bytes(ctx._grab_raw())
            ocr = ocr_runner(shot)
            own = screen_assets.match_screen(ocr, expected_screen)
            evidence = {
                "tokens_hit": list(own.tokens_hit) if not own.forbid_hit else [],
                "tokens_missed": list(own.tokens_missed),
                "forbid_hit": list(own.forbid_hit),
            }
            try:
                match = screen_assets.match_screens(ocr, screen_doc.get("screens") or [], statuses=statuses)
            except screen_assets.ScreenSignatureConflict as conflict:
                last = {
                    "status": "failed",
                    "expected": expected,
                    "matched": None,
                    "reason": "signature_conflict",
                    "shot": shot_rel,
                    "attempt": attempt,
                    **evidence,
                    "error": f"{type(conflict).__name__}: {conflict}",
                    **base,
                }
            else:
                matched = match.screen_id if match else None
                ok = matched == expected
                last = {
                    "status": "passed" if ok else "failed",
                    "expected": expected,
                    "matched": matched,
                    "reason": "matched" if ok else ("wrong_page" if matched else "unknown_screen"),
                    "shot": shot_rel,
                    "attempt": attempt,
                    **evidence,
                    **base,
                }
                if matched:
                    last["matched_status"] = status_by_id.get(matched, "")
                if ok:
                    from autophone.observation.ax_checkpoint import capture
                    last["ax_evidence"] = capture(
                        getattr(ctx, "ax", None), run_dir=shot_dir.parent,
                        identity=getattr(ctx, "checkpoint_identity", None) or {},
                        checkpoint=last, label=label,
                        build_evidence=_checkpoint_build_evidence(shot_dir.parent, ctx.checkpoint_identity),
                        ax_backend=getattr(ctx, "ax_backend", None),
                    )
                    return last
        except Exception as exc:  # noqa: BLE001 telemetry-only frame/ocr failure is recorded, never raised
            from autophone.device.ax_session import UnsupportedAxBackendError
            if isinstance(exc, UnsupportedAxBackendError):
                raise
            last = {
                "status": "failed",
                "expected": expected,
                "matched": None,
                "reason": "checkpoint_error",
                "shot": shot_rel,
                "attempt": attempt,
                "error": f"{type(exc).__name__}: {exc}",
                **base,
            }
        if attempt < attempts:
            time.sleep(max(0.0, interval_s))
    return last


def _checkpoint_detail(checkpoint: dict[str, Any]) -> str:
    return (
        "screen checkpoint failed "
        f"expected={checkpoint.get('expected')} "
        f"matched={checkpoint.get('matched')} "
        f"reason={checkpoint.get('reason')}"
    )


def has_builtin_executor(name: str, *, app: str | None = None) -> bool:
    """该操作 name 是否已接内置执行器（前端据此区分「真驱动」vs「执行器待接」）。

    注意：env 注入的 AUTOPHONE_OPERATION_EXECUTOR_RESULT_JSON 对所有操作生效，但那是
    agent/测试的旁路，不反映「这条操作本身可不可跑」，故此处只看运行时绑定。
    """
    binding = operation_binding(app, name)
    return binding is not None and binding.executor is not None


# 碰屏 Operation 的设备前置声明语义：
#   unlock=执行器跑之前必须先离开锁屏（锁屏会让截图/OCR/launch 全失真）；
#   launch=必须先把被测 App 拉到前台（如 login：do_login 假设停在前台登录页，自己不 launch）。
# 未声明设备前置 = 不碰屏（如 create_order：只构造/POST payload，不驱动屏幕）→ 不进 ScreenLock、
# execute phase 标 skipped。scan_duty/set_location 走 frida spawn-gate 自己拉起 App，故只需 unlock。
def device_prep(name: str, steps: Any = None, *, app: str | None = None) -> dict[str, bool] | None:
    """该碰屏操作的设备前置需求；None=不碰屏（不进 ScreenLock，execute 标 skipped）。

    显式绑定优先。否则若 steps 是 typed IR——它经 dispatcher 截图/点击驱动屏幕，
    必须先解锁（锁屏会让截图/OCR/tap 全失真），故默认 `{unlock:True, launch:False}`；launch 留 False，
    因 typed-IR operation 通常以 reset_to_home/open_options 起手自行导航（要冷启 App 应显式写进 steps）。"""
    binding = operation_binding(app, name)
    if binding is not None and binding.device is not None:
        return binding.device.to_dict()
    if is_typed_ir_steps(steps):
        return {"unlock": True, "launch": False}
    return None


def _redact_args_for_persist(operation: dict[str, Any]) -> dict[str, Any]:
    """返回 Operation 的浅副本，按运行时绑定掩码敏感入参（不改原对象）。"""
    name = str((operation.get("operation") or {}).get("name") or "")
    binding = operation_binding(str(operation.get("app") or ""), name)
    keys = frozenset({"password", "mobile"}) | (binding.sensitive_args if binding is not None else frozenset())
    def redact(args: dict[str, Any]) -> dict[str, Any]:
        return {k: ("***" if (k in keys and v not in (None, "")) else v) for k, v in args.items()}

    out = dict(operation)
    if isinstance(out.get("args"), dict):
        out["args"] = redact(out["args"])
    binding = out.get("step_args_binding")
    if isinstance(binding, dict) and isinstance(binding.get("args"), dict):
        out["step_args_binding"] = {**binding, "args": redact(binding["args"])}
    return out


@dataclass(frozen=True)
class OperationExecutionRequest:
    operation: Mapping[str, Any]
    raw_steps: tuple[Any, ...] = ()


@dataclass(frozen=True)
class OperationExecutionContext:
    run_dir: Path
    app: str | None = None
    root: Path = APP_ASSET_ROOT
    replay_speed: float = 1.0
    replay_timing: tuple[float, ...] | None = None
    screen_asset_path: str | Path | None = None
    screen_expectations: Mapping[Any, str] | None = None
    screen_starts_with: str | None = None
    runtime: Mapping[str, Any] | None = None
    capture_session: Any = None
    #: run 级中止检查（BL-062）：返回人读原因串即中止，None 表示继续。只有 typed-IR 合成执行器在
    #: 逐步边界读它；env 注入执行器与内置执行器没有逐步边界，只受调用方启动前检查约束。
    abort_check: Callable[[], str | None] | None = None


def execute_operation(request: OperationExecutionRequest, context: OperationExecutionContext) -> dict[str, Any]:
    from autophone.execution.failure_context import FAILURE_STATUSES, failure_fields
    out = _execute_operation(request, context)
    if out.get("status") in FAILURE_STATUSES:
        out = {**out, **failure_fields(out.get("failure_stage", "operation_execution"), out.get("failed_definition"))}
    else:
        out.pop("failure_stage", None)
        out.pop("failed_definition", None)
    return out


def _execute_operation(
    request: OperationExecutionRequest,
    context: OperationExecutionContext,
) -> dict[str, Any]:
    """执行操作。优先级：env 注入的执行器产物 > 内置执行器 > **typed-IR 合成执行器** > 保持 pending。

    执行器产物形态（最小）：{"status": passed|failed|blocked|error, "steps": [{index, status, detail?}], "detail"?}。
    `raw_steps` 是操作库里钉版本的**原始** steps（未展开成 {index,text,status} 前）；当某操作没有内置
    执行器但其 steps 是 typed IR 时，用 `_exec_typed_ir` 经 dispatcher 真跑（A：让 AI 起草的 typed-IR
    operation 从 pending 走到 passed/failed，喂得了转正门）。

    `replay_speed`/`replay_timing` 只对 **typed-IR 合成执行器**生效（倍速回放节奏 + 取数 dwell）；
    内置执行器（login 等手写控制流）与 env 注入旁路不受影响。

    `context.app` 是 app slug，`context.root` 是其 registry root；两者共同定位 typed-IR
    合成执行器所需的 App recipe 注册表。
    """
    operation = dict(request.operation)
    operation.pop("failure_stage", None)
    operation.pop("failed_definition", None)
    raw_steps = list(request.raw_steps)
    run_dir = context.run_dir
    replay_speed = context.replay_speed
    replay_timing = list(context.replay_timing) if context.replay_timing is not None else None
    screen_asset_path = context.screen_asset_path
    screen_expectations = (
        dict(context.screen_expectations) if context.screen_expectations is not None else None
    )
    screen_starts_with = context.screen_starts_with
    app = context.app
    root = context.root
    runtime_context = context.runtime

    # ① env 注入（agent/测试显式产物）最高优先，便于绕过内置执行器。
    result_path = os.environ.get("AUTOPHONE_OPERATION_EXECUTOR_RESULT_JSON")
    if result_path:
        write_json(run_dir / "operation-executor-input.json", operation)
        try:
            payload = json.loads(Path(result_path).read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 执行器读取失败本身是 run 证据
            out = dict(operation)
            out["status"] = "error"
            out["detail"] = f"operation executor error: {type(exc).__name__}: {exc}"
            return out
        write_json(run_dir / "operation-executor-output.json", payload)
        return _merge_executor_result(operation, payload)

    # ② 内置执行器（按操作 name）。
    name = str((operation.get("operation") or {}).get("name") or "")
    binding = operation_binding(app or str(operation.get("app") or ""), name)
    builtin = binding.executor if binding is not None else None
    if builtin is not None:
        token = _OPERATION_RUNTIME.set(runtime_context)
        try:
            try:
                payload = builtin(run_dir, operation.get("args") or {})
            finally:
                _OPERATION_RUNTIME.reset(token)
        except Exception as exc:  # noqa: BLE001 - 执行器失败本身是 run 证据，fail-soft 进结果
            out = dict(operation)
            out["status"] = "error"
            out["detail"] = f"operation executor error: {type(exc).__name__}: {exc}"
            return out
        write_json(run_dir / "operation-executor-output.json", payload)
        return _merge_executor_result(operation, payload)

    # ②.5 typed-IR 合成执行器：无内置执行器、但 steps 是 typed IR → dispatcher 顺序派发真跑。
    # 内置执行器优先（②）——有手写执行器的操作（login 等）其 steps 是 prose、本就不命中这里。
    if is_typed_ir_steps(raw_steps):
        # 导入 app recipes 模块，以注册 operation 执行器（dispatch_step 需要）
        from autophone.device.ax_session import UnsupportedAxBackendError

        recipe_scope = None
        allowed_effects = None
        profile = None
        ax_backend: str | None = None
        trial_data_isolation = "unisolated"
        if app:
            from autophone.assets.apps import load_app_profile
            from autophone.execution.app_recipes import import_app_recipes
            from autophone.execution.effects import profile_trial_data_isolation
            from autophone.execution.engine.recipe import operation_scope
            from autophone.observation.capture import ensure_ax_backend_supported

            import_app_recipes(app, root=root)
            profile = load_app_profile(app, root=root)
            ax_backend = ensure_ax_backend_supported(profile)
            allowed_effects = set(profile.allowed_effects)
            trial_data_isolation = profile_trial_data_isolation(profile)
            recipe_scope = operation_scope(app, root=root)
        args_binding: dict[str, Any] | None = None
        try:
            params = None
            if operation.get("args") and is_bare_self_call(raw_steps, name):
                # 只有可能传参时才读参数声明；其余 Operation 的执行路径不变。
                op_id = str((operation.get("operation") or {}).get("id") or "")
                if profile is not None and op_id:
                    params = raw_operation_def(profile, op_id=op_id).get("params")
            exec_steps, args_binding = bind_self_call_args(
                raw_steps, name, operation.get("args"), params,
            )
            exec_kwargs: dict[str, Any] = {
                "replay_speed": replay_speed,
                "replay_timing": replay_timing,
            }
            if ax_backend is not None:
                exec_kwargs["ax_backend"] = ax_backend
            if profile is not None:
                exec_kwargs["trial_data_isolation"] = trial_data_isolation
                exec_kwargs["effect_mode"] = "draft" if operation.get("draft_id") else "active"
            if context.capture_session is not None:
                exec_kwargs["capture_session"] = context.capture_session
            if screen_asset_path is not None:
                exec_kwargs["screen_asset_path"] = screen_asset_path
            if screen_expectations is not None:
                exec_kwargs["screen_expectations"] = screen_expectations
            if screen_starts_with is not None:
                exec_kwargs["screen_starts_with"] = screen_starts_with
            if context.abort_check is not None:
                exec_kwargs["abort_check"] = context.abort_check
            with ExitStack() as stack:
                if recipe_scope is not None:
                    stack.enter_context(recipe_scope)
                # AX 会话两个来源：屏骨架检查点的 AX 证据（BL-065），以及 steps 中 Operation 声明的
                # AX 需求（BL-038）。两者都没有时不构造会话，其他 Operation 行为不变。
                checkpoints = bool(screen_expectations or screen_starts_with)
                ax_owner = (
                    {"capture_session": context.capture_session}
                    if getattr(context.capture_session, "_controlled_identity", None) is not None
                    else {}
                )
                if profile is not None and not checkpoints and steps_need_ax_session(
                    app, exec_steps, root=root,
                ):
                    # 只有 Operation 声明的需求时延迟到首次 AX 使用再 attach：Operation 可能先冷启动
                    # App，执行前预开的会话会挂到即将被杀的旧进程上。attach 等待有界。
                    from autophone.observation.capture import build_run_ax_session, lazy_ax_scope
                    provider = build_run_ax_session(
                        profile, run_dir, attach_timeout_s=OPERATION_AX_ATTACH_TIMEOUT_S,
                        **ax_owner,
                    )
                    if provider is not None:
                        exec_kwargs['checkpoint_ax'] = stack.enter_context(lazy_ax_scope(provider))
                elif profile is not None and checkpoints:
                    from autophone.observation.capture import lazy_ax_scope, build_run_ax_session
                    provider = build_run_ax_session(
                        profile, run_dir, attach_timeout_s=OPERATION_AX_ATTACH_TIMEOUT_S,
                        **ax_owner,
                    )
                    if provider is not None:
                        exec_kwargs['checkpoint_identity'] = {
                            **{k: operation.get(k) for k in ('run_id', 'app', 'build', 'operation')},
                            'bundle_id': str(profile.bundle_id),
                        }
                        exec_kwargs['checkpoint_ax'] = stack.enter_context(lazy_ax_scope(provider))
                if recipe_scope is None:
                    payload = _exec_typed_ir(run_dir, exec_steps, **exec_kwargs)
                else:
                    payload = _exec_typed_ir(run_dir, exec_steps, allowed_effects=allowed_effects, **exec_kwargs)
        except UnsupportedAxBackendError:
            raise
        except Exception as exc:  # noqa: BLE001 - 执行器失败本身是 run 证据，fail-soft 进结果
            out = dict(operation)
            out["status"] = "error"
            out["detail"] = f"operation executor error: {type(exc).__name__}: {exc}"
            from autophone.execution.failure_context import definition_identity, exception_stage, failure_fields
            identity = operation.get("operation") or {}
            owner = definition_identity("operation", identity.get("id"), identity.get("revision")) if getattr(exc, "_failure_local", False) else None
            out.update(failure_fields(exception_stage(exc, "operation_execution"), owner))
            if args_binding is not None:
                out["step_args_binding"] = args_binding
            return out
        write_json(run_dir / "operation-executor-output.json", payload)
        out = _merge_executor_result(operation, payload, local_dispatch=True)
        if args_binding is not None:
            # 审计：记录实际传给自调用步骤的参数与来源（RunSpec operation.with）。
            out["step_args_binding"] = args_binding
        return out

    # ③ 无执行器 → 保持 pending（与 verification pending_agent 同口径）。
    return operation


def run_operation_executor(
    run_dir: Path,
    operation: dict[str, Any],
    *,
    raw_steps: Sequence[Any] | None = None,
    replay_speed: float = 1.0,
    replay_timing: Sequence[float] | None = None,
    screen_asset_path: str | Path | None = None,
    screen_expectations: Mapping[Any, str] | None = None,
    screen_starts_with: str | None = None,
    app: str | None = None,
    root: Path = APP_ASSET_ROOT,
    runtime_context: Mapping[str, Any] | None = None,
    abort_check: Callable[[], str | None] | None = None,
    capture_session: Any = None,
) -> dict[str, Any]:
    """Compatibility Adapter for the pre-M1 call shape."""
    return execute_operation(
        OperationExecutionRequest(
            operation=operation,
            raw_steps=tuple(raw_steps or ()),
        ),
        OperationExecutionContext(
            run_dir=run_dir,
            app=app,
            root=root,
            replay_speed=replay_speed,
            replay_timing=tuple(replay_timing) if replay_timing is not None else None,
            screen_asset_path=screen_asset_path,
            screen_expectations=screen_expectations,
            screen_starts_with=screen_starts_with,
            runtime=runtime_context,
            capture_session=capture_session,
            abort_check=abort_check,
        ),
    )


def _merge_executor_result(operation: dict[str, Any], payload: Any, *, local_dispatch: bool = False) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise OperationError("operation executor result must be an object")
    out = dict(operation)
    by_index = {int(s["index"]): s for s in (payload.get("steps") or []) if isinstance(s, dict) and "index" in s}
    merged_steps = []
    # 平台 run 路径传入钉版本 operation（带 steps，逐步回填进报告）；recipe 桥
    # （_op_scan_duty/_op_create_order）只传 {operation:{name},args} 的最小壳、且只回读
    # status/detail，没有 steps 可合并——缺键当空列表，两条路径同一出口不再 KeyError。
    for step in operation.get("steps") or []:
        ev = by_index.get(int(step["index"]))
        if ev:
            new = dict(step)
            st = str(ev.get("status") or step["status"])
            new["status"] = st if st in STEP_STATUSES else step["status"]
            if ev.get("detail"):
                new["detail"] = str(ev["detail"])
            if ev.get("screen_checkpoint"):
                new["screen_checkpoint"] = ev["screen_checkpoint"]
            merged_steps.append(new)
        else:
            merged_steps.append(step)
    out["steps"] = merged_steps
    status = str(payload.get("status") or "")
    status = {"pass": "passed", "ok": "passed", "fail": "failed"}.get(status, status)
    out["status"] = status if status in OPERATION_STATUSES else "error"
    out["detail"] = str(payload.get("detail") or "operation executor completed")
    if isinstance(payload.get("fixture_context"), dict):
        out["fixture_context"] = payload["fixture_context"]
    if isinstance(payload.get("health_fixture"), dict):
        out["health_fixture"] = payload["health_fixture"]
    if isinstance(payload.get("face_policy"), dict):
        out["face_policy"] = payload["face_policy"]
    if isinstance(payload.get("schedule_fixture"), dict):
        out["schedule_fixture"] = payload["schedule_fixture"]
    if isinstance(payload.get("location_session"), dict):
        # 统一 Location Session 的注入回执是这条 run 的证据，不是运行期临时载体：
        # 落盘后才能回答“这次定位到底按哪个 spec、投给哪个 target、被不被接受”。
        out["location_session"] = payload["location_session"]
    if isinstance(payload.get("blocker"), dict):
        # 部分现有 Adapter 仍以 status=error + blocker 返回前置条件缺口。Operation
        # Execution 的公开结果在统一出口将其规范化为 blocked，并保留结构化 blocker。
        out["status"] = "blocked"
        out["blocker"] = dict(payload["blocker"])
    if out["status"] in {"failed", "blocked", "error"}:
        from autophone.execution.failure_context import definition_identity, failure_fields
        identity = operation.get("operation") or {}
        owner = definition_identity("operation", identity.get("id"), identity.get("revision")) if local_dispatch and payload.get("_failure_local") is True else None
        out.update(failure_fields(payload.get("failure_stage", "operation_execution") if local_dispatch else "operation_execution", owner))
    if isinstance(payload.get("action_trace"), list):
        out["action_trace"] = [
            {
                "action": str(item.get("action") or ""),
                "action_id": str(item.get("action_id") or ""),
                "subject_epoch_ref": str(item.get("subject_epoch_ref") or ""),
                "ok": bool(item.get("ok")),
                "elapsed_ms": item.get("elapsed_ms"),
                "recorded_at": str(item.get("recorded_at") or ""),
            }
            for item in payload["action_trace"]
            if isinstance(item, dict)
        ]
    return out


def write_operation_outputs(run_dir: Path, operation: dict[str, Any]) -> tuple[Path, Path]:
    """落盘前贴合契约（契约先于服务）。返回 (result_json, report_html)。

    敏感入参在此唯一持久化口掩码：result.json 与 report.html 都是可分享产物，密码/手机号
    不能进。执行器已先消费真值（run_operation_executor 早于本函数），掩码只作用于落盘副本。
    """
    operation = dict(operation)
    operation.pop("fixture_context", None)
    operation = _redact_args_for_persist(operation)
    validate_or_raise("operation-result.v1.json", operation)
    result_path = write_json(run_dir / "operation-result.json", operation)
    report_path = run_dir / "operation-report.html"
    report_path.write_text(render_operation_html(operation), encoding="utf-8")
    return result_path, report_path


REVIEW_STATUSES = {"approved", "rejected", "needs_changes"}


def review_operation_result(
    run_dir: Path,
    *,
    status: str,
    reviewer: str,
    note: str = "",
) -> dict[str, Any]:
    """给一条 operation run 落人审结论（与 review_verification_result 对称）。

    转正闸（evaluate_promotion_gate）要求一条 approved 人审 + N 连续 passed——operation draft 走
    回放转正同一道门，故 operation-result 也要能 review。最新结论冗余进顶层 review_status/reviewed_*，
    全量事件追加进 reviews[]。写回后重渲染报告 + 重过 operation-result.v1 schema（fail-closed）。
    """
    status = str(status)
    if status not in REVIEW_STATUSES:
        raise ValueError(f"review status must be one of {sorted(REVIEW_STATUSES)}")
    result_path = run_dir / "operation-result.json"
    if not result_path.exists():
        raise FileNotFoundError(result_path)
    operation = json.loads(result_path.read_text(encoding="utf-8"))
    from autophone.util import now_iso
    event = {"status": status, "reviewer": str(reviewer or "unknown"),
             "note": str(note or ""), "reviewed_at": now_iso()}
    operation["review_status"] = status
    operation["reviewed_by"] = event["reviewer"]
    operation["review_note"] = event["note"]
    operation["reviewed_at"] = event["reviewed_at"]
    operation["reviews"] = list(operation.get("reviews") or []) + [event]
    write_operation_outputs(run_dir, operation)
    return operation


def render_operation_html(operation: dict[str, Any]) -> str:
    op = operation.get("operation") or {}
    args = operation.get("args") or {}
    store = operation.get("store_context") or {}
    rows = []
    for step in operation.get("steps") or []:
        detail = html.escape(str(step.get("detail") or ""))
        checkpoint = step.get("screen_checkpoint") or {}
        shot = str(checkpoint.get("shot") or "") if isinstance(checkpoint, dict) else ""
        # Checkpoint shots are executor-owned run-relative paths.  Refuse schemes and
        # parent traversal before embedding them into the local report.
        shot_parts = Path(shot).parts
        if shot and ":" not in shot and ".." not in shot_parts:
            href = html.escape(shot, quote=True)
            shot_html = (
                f'<a href="{href}"><img src="{href}" loading="lazy" '
                'alt="步骤截图" style="max-width:180px;max-height:320px;display:block">查看原图</a>'
            )
        else:
            shot_html = ""
        rows.append(
            f"<tr><td>{step.get('index')}</td><td>{html.escape(str(step.get('text') or ''))}</td>"
            f"<td>{html.escape(str(step.get('status') or ''))}</td><td>{detail}</td>"
            f"<td>{shot_html}</td></tr>"
        )
    args_rows = "".join(
        f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(str(v))}</td></tr>" for k, v in args.items()
    )
    blocker = operation.get("blocker") if isinstance(operation.get("blocker"), dict) else None
    blocker_html = ""
    if blocker:
        blocker_html = (
            "<h2>阻塞信息</h2>"
            f"<p><b>范围：</b>{html.escape(str(blocker.get('scope') or ''))}"
            f" · <b>代码：</b><code>{html.escape(str(blocker.get('code') or ''))}</code></p>"
            f"<p><b>恢复：</b>{html.escape(str(blocker.get('recovery') or ''))}</p>"
        )
        if blocker.get("capability"):
            blocker_html += (
                f"<p><b>Capability：</b>{html.escape(str(blocker['capability']))}</p>"
            )
        if blocker.get("evidence_ref"):
            blocker_html += (
                f"<p><b>Evidence：</b><code>{html.escape(str(blocker['evidence_ref']))}</code></p>"
            )
    store_html = ""
    if store:
        store_html = (
            "<h2>门店上下文</h2>"
            f"<p><b>{html.escape(str(store.get('store_code') or ''))}</b> · "
            f"{html.escape(str(store.get('name') or ''))} · "
            f"{html.escape(str(store.get('city_name') or ''))}</p>"
            f"<p>目录快照：<code>{html.escape(str(store.get('catalog_snapshot_id') or ''))}</code>"
            f" · 状态：{html.escape(str(store.get('status') or ''))}</p>"
        )
    return (
        "<!doctype html><meta charset=utf-8>"
        f"<title>operation {html.escape(str(op.get('name') or ''))}</title>"
        "<style>body{font-family:-apple-system,sans-serif;margin:2rem}table{border-collapse:collapse}"
        "td,th{border:1px solid #ccc;padding:4px 8px}</style>"
        f"<h1>操作 {html.escape(str(op.get('name') or ''))} "
        f"<small>{html.escape(str(op.get('id') or ''))} r{op.get('revision')}</small></h1>"
        f"<p>{html.escape(str(operation.get('summary') or ''))}</p>"
        f"<p><b>状态：</b>{html.escape(str(operation.get('status') or ''))} — {html.escape(str(operation.get('detail') or ''))}</p>"
        + (f"<p><b>draft：</b>{html.escape(str(operation.get('draft_id')))}</p>" if operation.get("draft_id") else "")
        + (f"<p><b>人审：</b>{html.escape(str(operation.get('review_status')))}"
           f"{html.escape(' by ' + str(operation['reviewed_by']) if operation.get('reviewed_by') else '')}"
           f"{html.escape(' — ' + str(operation['review_note']) if operation.get('review_note') else '')}</p>"
           if operation.get("review_status") else "")
        + blocker_html
        + store_html
        + f"<h2>入参</h2><table><tr><th>param</th><th>value</th></tr>{args_rows}</table>"
        "<h2>步骤</h2><table><tr><th>#</th><th>步骤</th><th>状态</th><th>详情</th><th>截图</th></tr>"
        f"{''.join(rows)}</table>"
    )
