"""runner.py — gate → run step-script → classify (docs §10.4).

Status is four-way: pass / fail (an assertion did not hold) / error (test
infrastructure broke) / skipped (no step-script for this case). fail and error
are kept apart on purpose — one is the app under test, the other is our rig.

Asserts live inside the step-script (the author picks OCR-friendly phrases from
the case's `expects`, §10.2), so the runner does not re-assert raw expect
sentences. To stop a script that asserts *nothing* from passing vacuously, a
normal completion with zero assert_* actions is downgraded to `error`.
"""
from __future__ import annotations

import logging
import signal
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from autophone.logging import bind_case
from autophone.execution.contracts import CaseBlocked, blocker
from autophone.assets.scheduling import (
    SchedulingContractError,
    contract_for_case,
)
from autophone.device.uds import GeometryDrift, ProtocolError, request
from autophone.device.ocr_target import OcrError
from autophone.execution.engine.actions import (
    CaseContext,
    RuntimeResourceScope,
    StepFailure,
    resolve_sock,
)
from autophone.execution.engine.cases_loader import load_cases  # noqa: F401  — re-export，run_regression 等从这里 import
from autophone.execution.engine.recipe import (
    RECIPES,
    RecipeError,
    dispatch_step,
    expand_symbol_steps,
    recipe_has_unresolved_symbols,
    recipe_steps_digest,
    recipe_uses_symbols,
    scoped_recipes,
    steps_binding_sha256,
    validate_recipe,
)
from autophone.execution.engine.registry import CASE_SCRIPTS
from autophone.execution import cleanup_readback as cleanup_rb
from autophone.execution import entity_readback as entity_rb
from autophone.execution import fixture_readback as fixture_rb
from autophone.execution.case_selection import CaseSelector
from autophone.observation.plane_evaluation import synthesize_status


class BodyExecutionTimeout(Exception):
    """Body execution exceeded global timeout."""
    pass


@contextmanager
def _body_execution_deadline(timeout_s: float | None):
    """Interrupt a case body only when this process owns the main thread.

    ``SIGALRM`` is process-global and Python only permits installing its handler
    from the main thread.  Web-triggered runs therefore execute in a dedicated
    worker process (see ``RunService._submit_background``), where this guard is
    again safe and effective.  Other embedding callers retain their existing
    behavior rather than cancelling an unrelated process alarm.
    """
    if timeout_s is None or timeout_s <= 0:
        yield
        return
    if threading.current_thread() is not threading.main_thread():
        logger.warning("body timeout unavailable outside the main thread")
        yield
        return

    def _timeout_handler(_signum, _frame):
        raise BodyExecutionTimeout(f"Body execution exceeded {timeout_s:g}s timeout")

    old_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _timeout_handler)
    old_timer = signal.setitimer(signal.ITIMER_REAL, timeout_s)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *old_timer)
        signal.signal(signal.SIGALRM, old_handler)


from autophone.execution.case_selection import select_cases as _select_cases
# 「哪些条目算 setup 引用」的定义只留这一份：multicase_schedule（分析层，不反过来依赖本模块）
# 与本模块曾各自维护一份逐字相同的过滤逻辑，有drift 成两份不同定义的风险（review 43e3cbf2
# finding #5）。norm_args/_norm_ref_args 不在合并之列——runner 这份对畸形 `with` fail-closed
# 抛错以阻断共享判定，multicase_schedule 那份只喂只读报表、坏参宽松当空 tuple 即可，两种
# 容错级别都是各自故意的，勉强合并会削弱这里的 fail-closed 保证。
from autophone.assets.scheduling import setup_refs as _setup_refs

logger = logging.getLogger(__name__)

# App 资产根默认就是本文件的 parents[3]（CODE_ROOT）；AUTOPHONE_APP_ASSET_DIR 开启时随之移动。
INFRA_ERRORS = (GeometryDrift, ProtocolError, OcrError, OSError, ConnectionError)


def _ir_disabled() -> bool:
    """Kill-switch for the case-IR cutover (§10.10). The production runner prefers a
    registered recipe over the hand-written step-script; set AUTOPHONE_CASE_IR to
    0/off/false/no to force every case back onto the proven CASE_SCRIPTS path
    WITHOUT a code change. Default (unset) = IR on."""
    import os
    return os.environ.get("AUTOPHONE_CASE_IR", "").strip().lower() in {"0", "off", "false", "no"}


def _share_disabled() -> bool:
    """Kill-switch for shared-precondition scheduling
    (docs/regression-shared-precondition-scheduling.md, M1). Set
    AUTOPHONE_SHARE_PRECONDITION to 0/off/false/no to force every case back onto a
    full per-case setup (the pre-M1 behaviour) WITHOUT a code change — also the
    documented A/B lever for timing the saving. Default (unset) = sharing on."""
    import os
    return os.environ.get("AUTOPHONE_SHARE_PRECONDITION", "").strip().lower() in {"0", "off", "false", "no"}


def _order_pool_disabled() -> bool:
    """Kill-switch for the narrow create_order M1 pool.

    Default (unset) = off per docs/order-ensure-precondition.md A0; set
    AUTOPHONE_ORDER_POOL=1/on/true/yes to temporarily re-enable the old narrow
    pool while fixture acquire becomes the single reuse source.
    """
    import os
    return os.environ.get("AUTOPHONE_ORDER_POOL", "").strip().lower() not in {"1", "on", "true", "yes"}


def _shared_flow_id(cases: list[dict[str, Any]]) -> str | None:
    """Return the one lifecycle flow shared by an already-selected suite."""
    if not cases:
        return None
    declared: list[set[str]] = []
    for case in cases:
        specs = [case.get("flow"), *(case.get("flows") or [])]
        declared.append({str(spec.get("id")) for spec in specs
                         if isinstance(spec, dict) and spec.get("id")})
    common = set.intersection(*declared)
    return next(iter(common)) if len(common) == 1 else None


def _implicit_flow_id(selected: list[dict[str, Any]]) -> str | None:
    """Honor an implicit shared flow only when the selection IS its valid chain.

    An ids subset (e.g. R99+R100 without R132) or a suite+ids truncation can
    still share one flow declaration while breaking the produces/requires
    chain; ids selection also preserves YAML order, which need not equal step
    order. Flow semantics (fixture inheritance + predecessor blocking) must
    not silently attach to such a selection — fall back to ordinary per-case
    runs instead. Explicit `flow:` selection is already validated fail-closed
    in select_cases and does not pass through here.
    """
    flow_id = _shared_flow_id(selected)
    if flow_id is None:
        return None
    from autophone.execution.case_selection import order_flow_cases
    try:
        ordered = order_flow_cases(selected, flow_id)
    except ValueError as exc:
        logger.warning("implicit flow %s disabled for this run: %s", flow_id, exc)
        return None
    if [case.get("id") for case in ordered] != [case.get("id") for case in selected]:
        logger.warning(
            "implicit flow %s disabled for this run: selection order %s differs from flow step order %s",
            flow_id, [case.get("id") for case in selected], [case.get("id") for case in ordered])
        return None
    return flow_id


@dataclass
class CaseOutcome:
    id: str
    status: str               # pass | fail | error | blocked | skipped
    module: str
    item: str
    row: int | None
    detail: str = ""
    # 已计划或运行中确认无法执行时的机器可读阻塞原因。None 保持旧产物兼容。
    blocker: dict[str, str] | None = None
    # 失败归类（§10.10）：fail 用例的 taxonomy kind（locator_missing/ambiguous_match/
    # assertion_failed/…），取自触发 StepFailure 的动作。pass/error/skipped 恒空。
    fail_kind: str = ""
    failure_stage: str | None = None
    failed_definition: dict[str, Any] | None = None
    # Internal marker; identity is attached only after the executed Recipe digest matches.
    failure_in_recipe: bool = False
    # 覆盖广度（部分覆盖防假绿）：case yaml 的 `coverage` 原样透传。"partial" 表示 recipe 只
    # 自动断言了用例 expects 的子集（其余走独立 discovery），报告据此打「部分覆盖」徽标，
    # 避免整卡 PASS 被误读成整条手工用例已覆盖。空 = 完整覆盖（默认，行为不变）。
    coverage: str = ""
    # IR 切换可观测性（§10.10）：本用例经哪条执行路径——"recipe"（IR dispatcher）/
    # "script"（手写 CASE_SCRIPTS）/""（skipped）。迁移期一眼看清哪些已上 IR。
    exec_via: str = ""
    # 显式 Case Draft Trial 的资产身份快照。平台在人审与转正时用它复核
    # Case revision、预留 external_id 与草稿 Recipe SHA-256；普通运行为 None。
    draft_recipe_identity: dict[str, Any] | None = None
    # BL-186：本用例实际执行的 Recipe 身份（来源 live/draft、App 相对路径、文件 SHA-256、
    # Case revision、预留 external_id）。平台在执行后按 recipe_steps_digest 核对再写入；
    # 未经 Recipe 执行的用例为 None，result.json 不出现该字段。
    recipe_identity: dict[str, Any] | None = None
    # Runner 实际执行的 Recipe steps 的规范摘要（recipe.recipe_steps_digest）。平台据此确认
    # 记录的文件摘要对应的就是被执行的内容；只在进程内与挂起快照中流转，不写入报告。
    recipe_steps_digest: str | None = None
    duration_ms: float = 0.0
    # 执行 Trace（docs/run-execution-trace.md）：用例起跑的墙钟时刻（ISO 毫秒）。瀑布流据此把
    # 用例条钉到绝对时间轴，免去靠「首动作 recorded_at − elapsed_ms」反推。skipped 用例恒空。
    started_at: str = ""
    actions: list[dict[str, Any]] = field(default_factory=list)
    shots: list[str] = field(default_factory=list)
    # 组内 teardown 去重：本条的复位交给下一条同名 `purpose: setup` 前置重建时，记下被
    # 延后的操作名。跳过的清理必须在产物里看得见——否则「teardown 没跑」只能靠读调度代码
    # 反推。空 = 照常执行（默认，行为不变）。
    deferred_teardown: list[str] = field(default_factory=list)
    # Phase 4（§七）：API 断言结果与归因调用摘要。dataclass 加字段即自动流进 result.json 与 HTML。
    # phase2 纯 UI 跑时这三项为空，HTML 不渲染「后端 API」面板，行为不变。
    api_calls: list[dict[str, Any]] = field(default_factory=list)       # 脱敏后调用摘要
    api_assertions: list[dict[str, Any]] = field(default_factory=list)  # 每条 expect 的三态结果
    net_jsonl: str | None = None                                       # 指向原始 JSONL（不内嵌 body）
    # API finalize 前保存纯 UI 结果。status 仍是 UI×API 合成总态；平台各 plane 用本字段
    # 避免 API fail 反向把已通过的 UI 面板也标红。空表示尚未经过 API finalize。
    ui_status: str = ""
    # 显式 Draft Trial 声明了业务 Fixture 时的逐 Fixture Readback 记录
    # （autophone.execution.fixture_readback）。平台据此写 fixture-readback.json；
    # 未绑定 Readback 计划的用例为 None。
    fixture_readback: list[dict[str, Any]] | None = None
    # Fixture 声明了 cleanup 时的逐 Fixture 清理与读回记录（autophone.execution.cleanup_readback，
    # BL-083 第八轮）。平台据此写 cleanup-readback.json；未声明 cleanup 的用例为 None。
    cleanup_readback: list[dict[str, Any]] | None = None
    # 经 Recipe 执行且主体以 fail 结束时，触发 StepFailure 的 Recipe 步骤下标（按实际执行的
    # steps 计）。区分力反向 Run 据此判断是否失败在声明的断言步骤；其余情况为 None。
    failed_recipe_step: int | None = None
    # 区分力反向 Run 实际施加的扰动记录（autophone.execution.negative_control）；普通用例为 None。
    negative_control: dict[str, Any] | None = None
    operation_witnesses: list[dict[str, Any]] | None = None
    # 按用例保存动作前绑定的 API 断言变量；不使用后续清理修改的 Fixture 身份。
    api_assertion_context: dict[str, Any] | None = None
    # 本用例执行时冻结的 app-symbols.v1 文件原始字节 SHA-256；未使用符号绑定时为 None。
    binding_sha256: str | None = None
    # 本用例执行期间冻结的 AX 观测后端（D8）；未显式配置时为 None。
    ax_backend: str | None = None
    # 本用例内成功唯一命中的符号步骤观测，供 confirmed 构建追加外部 unique_visibility claim。
    symbol_observations: list[dict[str, Any]] | None = None
    # 专项二：动作后实例级业务实体 Readback 记录（autophone.execution.entity_readback）。
    entity_readback: dict[str, Any] | None = None
    # 专项二：多平面（UI + API + Readback）联合裁决结果（autophone.observation.joint_verdict）。
    joint_verdict: dict[str, Any] | None = None
    # 专项二：本用例解析后的绑定业务实体脱敏快照。
    resolved_entity: dict[str, Any] | None = None


@dataclass
class RunResult:
    aborted: bool
    abort_reason: str
    outcomes: list[CaseOutcome] = field(default_factory=list)
    # capture 会话构造/启动失败、fail-open 退回普通启动时，真实失败原因（否则报告只能
    # 显示笼统猜测文案，见 autophone.execution.run_service._run_with_capture）。普通 UI-only
    # run 恒 None，不影响既有行为。
    capture_skip_reason: str | None = None
    # 本次运行启动期冻结的 AX 观测后端（D8）。
    ax_backend: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        c = {"pass": 0, "fail": 0, "error": 0, "skipped": 0}
        for o in self.outcomes:
            c[o.status] = c.get(o.status, 0) + 1
        return c


@dataclass
class SetupScope:
    """Batch scheduler → run_case 的共享前置作用域
    (docs/regression-shared-precondition-scheduling.md §4.1)。

    默认 None ⇒ run_case 行为与今日逐字节一致；其余 4 处调用者（phase4 capture、
    recipe_shadow、单条用例路径）均不传，行为不变。只表达三件事：
      ① skip_setup：login 层已被本批早先的「锚」用例建立，本条跳过重建；
      ② reused_from：锚用例 id，用于合成 reused 动作 + 日志（§5.3，让依赖不凭空消失）；
      ③ reprobe：主体失败后的归因复探 hook（§5.2），由 run_case 在自己的 try/except
         边界调用——外层 run() 拿不到失败上下文，复探只能在此处做。返回
         'persisted'（借来的 login 仍在 → 保持产品 fail）或 'regressed'（login 已回退 →
         把 fail 重判为 error）。"""
    skip_setup: bool = False
    reused_from: str = ""
    # -1 keeps the old contract: skip every setup step.  Phase 4 prefix sharing
    # passes an explicit count so later per-case setup steps still run.
    skip_setup_count: int = -1
    reused_ops: tuple[str, ...] = ()
    reprobe: Callable[[CaseContext], str] | None = None


def select_cases(
    cases: list[dict[str, Any]],
    *,
    selector: CaseSelector | dict[str, Any] | None = None,
    automation: str | None = None,
    ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    return _select_cases(cases, CaseSelector.from_raw(selector, automation=automation, ids=ids))


def _login_share_sig(case: dict[str, Any]) -> tuple[str, tuple] | None:
    """M1 共享候选签名（docs/regression-shared-precondition-scheduling.md §十一）。

    M1 唯一共享的层是 login，且**只在两条都满足时**才认它为共享候选：
      ① setup 恰为单个 `op: login`（多层栈/带其它层是 M3，不在 M1）；
      ② 无 teardown（M1 不共享任何需清理的层——`login` 本就无声明 inverse）。
    返回 ('login', 规范化 args) 作分组键（前置态身份的签名级派生，§3.1）；不合格返回
    None ⇒ 该用例保守逐用例自建、且**中断相邻共享段**（失配即忘 cache，见 run()）。"""
    if case.get("teardown"):
        return None
    setup = case.get("setup") or []
    if len(setup) != 1:
        return None
    s = setup[0]
    if not isinstance(s, dict) or s.get("op") != "login":
        return None
    # malformed `with`（非 dict：str/list/…）不在 scheduler 阶段炸整批——返回 None 交回
    # run_case()，由其 `_ref_to_step` 对非 dict with **per-case error**（fail-closed，不会按
    # 默认参跑假绿）。这里只负责「不当共享候选 + 不抛 TypeError 掀翻 run loop」。
    args = s.get("with")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None
    norm = tuple(sorted((str(k), repr(args[k])) for k in args))
    return ("login", norm)


def _expect_coupled_operations(case: dict[str, Any]) -> set[str]:
    return set(contract_for_case(case).expect_coupled_operations)


def _norm_ref_args(ref: dict[str, Any]) -> tuple[tuple[str, str], ...] | None:
    args = ref.get("with")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None
    return tuple(sorted((str(k), repr(args[k])) for k in args))


def _prefix_sig(refs: list[dict[str, Any]]) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...] | None:
    out: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for ref in refs:
        norm = _norm_ref_args(ref)
        if norm is None:
            return None
        out.append((str(ref["op"]), norm))
    return tuple(out)


def _ref_to_step(ref: dict[str, Any]) -> dict[str, Any]:
    step = {"operation": ref["op"]}
    args = ref.get("with")
    # Fail-closed on a malformed `with`：非 dict（str/list/标量）不能静默丢弃——否则
    # `with: "oops"` 会按**默认参数**跑出假绿。raise → run_case 接成 per-case error；
    # post-case cleanup 接成 cleanup warning，不影响上一条业务结论。
    if args is not None and not isinstance(args, dict):
        raise TypeError(
            f"operation {ref['op']!r} `with` must be a mapping, got {type(args).__name__}")
    if isinstance(args, dict) and args:
        step["args"] = args
    return step


def _order_pool_sig(case: dict[str, Any]) -> tuple[str, tuple] | None:
    """Narrow Phase 3 pool: single create_order, read-only opt-in, no teardown."""
    if _order_pool_disabled() or case.get("teardown"):
        return None
    if case.get("order_consumes") is not False:
        return None
    refs = _setup_refs(case)
    if len(refs) != 1 or str(refs[0].get("op")) != "create_order":
        return None
    if "create_order" in _expect_coupled_operations(case):
        return None
    norm = _norm_ref_args(refs[0])
    if norm is None:
        return None
    return ("create_order_pool", norm)


def _order_pool_refs(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [ref for ref in _setup_refs(case) if str(ref.get("op")) == "create_order"]


def _order_pool_release_plan(case: dict[str, Any]) -> dict[str, Any] | None:
    """Return the M2 fixture-pool release mode, or None when pooling is unsafe."""
    order_contract = contract_for_case(case).order_pool
    mode = order_contract.mode if order_contract is not None else ""
    produce = order_contract.produce if order_contract is not None else ""
    explicit_consumes = case.get("order_consumes")
    if explicit_consumes is False and mode in {"advance", "destructive"}:
        return None
    if explicit_consumes is True and mode in {"read_only", "advance"}:
        return None
    if mode == "advance":
        if not produce:
            return None
        return {"mode": "advance", "produce_state": produce}
    if mode == "read_only":
        return {"mode": "read_only"}
    if case.get("order_consumes") is False and mode in {"", "read_only"}:
        return {"mode": "read_only"}
    return None


def _share_order_case_eligible(case: dict[str, Any]) -> bool:
    """Fixture-level order pool gate.

    M2 consumes the stable scheduling.order_pool contract.  M3 allows
    create_order inside a setup stack, while still requiring exactly one
    create_order operation for a single lease.
    """
    if case.get("teardown"):
        return False
    if _order_pool_release_plan(case) is None:
        return False
    refs = _order_pool_refs(case)
    if len(refs) != 1:
        return False
    if "create_order" in _expect_coupled_operations(case):
        return False
    return _norm_ref_args(refs[0]) is not None


def _login_prefix_share(case: dict[str, Any]) -> tuple[str, tuple] | None:
    """M3 stack-diff seed: share a leading login and leave later setup steps live."""
    if case.get("teardown"):
        return None
    refs = _setup_refs(case)
    if len(refs) <= 1:
        return None
    first = refs[0]
    if str(first.get("op")) != "login":
        return None
    if "login" in _expect_coupled_operations(case):
        return None
    norm = _norm_ref_args(first)
    if norm is None:
        return None
    return ("login_prefix", norm)


def _declared_prefix_share(case: dict[str, Any]) -> tuple[tuple, int, tuple[str, ...]] | None:
    """Phase 4 prefix sharing from the stable case scheduling contract.

    Missing scheduling keeps the case conservative. Contract validation already
    proves that the declared operations are the actual setup prefix and are not
    expect-coupled.
    """
    if case.get("teardown"):
        return None
    scheduling = contract_for_case(case)
    prefix_ops = list(scheduling.shareable_prefix)
    if not prefix_ops:
        return None
    refs = _setup_refs(case)
    sig = _prefix_sig(refs[:len(prefix_ops)])
    if sig is None:
        return None
    return (("prefix", sig), len(prefix_ops), tuple(prefix_ops))


def _declared_restore_ops(steps: Any) -> tuple[str, ...] | None:
    """Ops of a teardown that consists purely of declared ``purpose: teardown`` restores.

    ``None`` means the teardown does something beyond restoring state, so it is
    never a candidate for deferral.
    """
    if not steps:
        return None
    ops: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            return None
        args = step.get("with")
        if not isinstance(args, dict) or str(args.get("purpose") or "") != "teardown":
            return None
        op = str(step.get("op") or "")
        if not op:
            return None
        ops.append(op)
    return tuple(ops)


def _declared_anchor_ops(steps: Any) -> frozenset[str]:
    """Ops a setup declares as ``purpose: setup`` state anchors."""
    return frozenset(
        str(step.get("op") or "")
        for step in (steps or [])
        if isinstance(step, dict) and isinstance(step.get("with"), dict)
        and str(step["with"].get("purpose") or "") == "setup"
        and step.get("op")
    )


def _deferrable_teardown(
    case: dict[str, Any], next_case: dict[str, Any] | None,
) -> tuple[str, ...]:
    """Restore ops the next case re-anchors anyway, so this case may skip them.

    A duty case ends by restoring ``off_duty`` while the next one opens by
    anchoring its own starting state, so the pair spends two full state
    operations reaching a state one of them would have reached alone.  Run
    20260728-101111 paid for 14 ``ensure_duty_state`` calls across 7 duty
    cases, 5 of them no-ops that only re-confirmed what the previous teardown
    had just established.

    The restore is redundant only when all of the following hold, so the
    default in every other shape is to run it:

    * there is a next case in this group — at a group boundary the teardown is
      the only thing that leaves the device clean for whatever runs next;
    * the teardown is *entirely* declared restores (``purpose: teardown``);
    * the next case re-declares every one of those ops as a ``purpose: setup``
      anchor.  Reading the declared pair rather than an op-name allowlist keeps
      this app-agnostic: an app that does not label its restores never defers.

    Deferring stays safe across a red case because the next case's anchor
    probes the live state and walks verified edges from wherever the device
    actually is — it never assumes the previous case left something behind.
    The bounded exposure is a next case that is skipped before its setup runs
    (cycle-skip / flow-blocked): the state stays dirty for exactly one case,
    whose own anchor then establishes it.
    """
    if next_case is None:
        return ()
    restores = _declared_restore_ops(case.get("teardown"))
    if restores is None:
        return ()
    anchors = _declared_anchor_ops(next_case.get("setup"))
    if not all(op in anchors for op in restores):
        return ()
    return restores


@dataclass(frozen=True)
class ShareCandidate:
    """一次共享判定的完整身份（§十一 M1-M4）。

    `kind` 是显式判别式；过去只靠 `reused_ops` 元组内容反推是哪种机制（如
    `reused_ops == ("login",)` 才接归因复探），新增一条判定分支就得在多处同步改、漏一处
    没有编译器/测试会拦（review 43e3cbf2 finding #6：多操作声明前缀里带 login 时复探没
    接上，finding #2）。`sig` 是跨相邻用例判「同一前置态身份」的等值键，形状因 kind 而异
    （login/create_order_pool 是二元组，declared_prefix 是嵌套元组）——比较时 kind 必须
    一起比，光比 sig 内容可能撞车。
    """
    kind: str  # "login" | "login_prefix" | "create_order_pool" | "declared_prefix"
    sig: tuple
    skip_count: int
    reused_ops: tuple[str, ...]


def _share_candidate(case: dict[str, Any], *, sharing: bool) -> ShareCandidate | None:
    if not sharing:
        return None
    login = _login_share_sig(case)
    if login is not None and "login" not in _expect_coupled_operations(case):
        return ShareCandidate(kind="login", sig=login, skip_count=1, reused_ops=("login",))
    login_prefix = _login_prefix_share(case)
    if login_prefix is not None:
        return ShareCandidate(kind="login_prefix", sig=login_prefix,
                              skip_count=1, reused_ops=("login",))
    order = _order_pool_sig(case)
    if order is not None:
        return ShareCandidate(kind="create_order_pool", sig=order, skip_count=1, reused_ops=("create_order",))
    prefix = _declared_prefix_share(case)
    if prefix is None:
        return None
    sig, skip_count, reused_ops = prefix
    return ShareCandidate(kind="declared_prefix", sig=sig, skip_count=skip_count, reused_ops=reused_ops)


def _make_login_reprobe() -> Callable[[CaseContext], str]:
    """构造 §5.2 的失败归因复探 hook：复用用例主体失败时，对当前屏做一次廉价
    blacklist liveness 探测，判借来的 login 是否仍在。fail-closed：非 `persisted`
    一律 'regressed'（needs_login / unknown / 探测异常都按前置回退处理）。phase5 的
    probe_login_state 惰性 import，仅复用用例真失败时才触达。"""
    def reprobe(ctx: CaseContext) -> str:
        from autophone.execution.fixtures.login_state import probe_login_state
        res = probe_login_state(live=True, sock_path=ctx.sock_path,
                                out_dir=ctx.shot_dir / "reprobe")
        return "persisted" if res.status == "persisted" else "regressed"
    return reprobe


def _post_case_cleanup_steps(case: dict[str, Any]) -> list[dict[str, Any]]:
    refs = case.get("post_case_cleanup") or []
    if not refs:
        return []
    if not isinstance(refs, list):
        raise TypeError("post_case_cleanup must be a list of operation refs")
    return [_ref_to_step(ref) for ref in refs]


def _run_post_case_cleanup(case: dict[str, Any], *, shot_dir: Path, sock_path: str,
                           timeout: float, capture: Any = None, ax: Any = None,
                           allowed_effects: set[str] | None = None,
                           screen_asset_doc: dict[str, Any] | None = None,
                           ax_backend: str | None = None,
                           effect_mode: str | None = None,
                           trial_draft_recipes: bool = False,
                           confirm_trial_effects: bool = False,
                           trial_data_isolation: str = "unisolated") -> None:
    """Best-effort cleanup outside the case outcome and outside real case attribution.

    This is intentionally narrower than teardown: failure is logged but never mutates
    the previous case result, and a neutral empty-case mark cuts the API attribution
    window so cleanup traffic cannot be claimed by the case that just finished.
    """
    try:
        steps = _post_case_cleanup_steps(case)
        validate_recipe(steps)
    except (KeyError, TypeError, RecipeError) as e:
        logger.warning("用例 %s post_case_cleanup 无效（忽略，不改业务结论）：%s",
                       case.get("id"), e)
        return
    if not steps:
        return
    cleanup_effect_mode = (
        effect_mode
        if effect_mode is not None
        else ("draft" if isinstance(case.get("_trial_recipe_document"), dict) else "active")
    )
    ctx = CaseContext(case_id="", shot_dir=shot_dir / "_post_case_cleanup",
                      sock_path=sock_path, timeout=timeout, capture=capture, ax=ax,
                      allowed_effects=allowed_effects,
                      screen_asset_doc=screen_asset_doc,
                      ax_backend=ax_backend or getattr(ax, "ax_backend", None),
                      effect_mode=cleanup_effect_mode,
                      trial_draft_recipes=trial_draft_recipes,
                      confirm_trial_effects=confirm_trial_effects,
                      trial_data_isolation=trial_data_isolation)
    if capture is not None:
        ctx.api_mark("post_case_cleanup", operation=None, window_tag="post_case_cleanup")
    logger.info("用例 %s 后置 cleanup 开始：%d 个操作", case.get("id"), len(steps))
    try:
        for step in steps:
            dispatch_step(ctx, step)
    except (StepFailure, *INFRA_ERRORS) as e:
        detail = e.result.detail if isinstance(e, StepFailure) else f"{type(e).__name__}: {e}"
        logger.warning("用例 %s post_case_cleanup 失败（忽略，不改业务结论）：%s",
                       case.get("id"), detail)


def _lease_path(lease: dict[str, Any], key: str) -> Path | None:
    value = lease.get(key)
    if value in (None, ""):
        return None
    return Path(str(value))


def gate(sock_path: str, timeout: float = 8.0) -> tuple[bool, str]:
    """Cheap routability gate (§10.4): one screenshot. If the device can't be
    reached we abort the whole run rather than emit false passes (DoD #4)."""
    try:
        obj, _ = request({"t": "screenshot"}, sock_path=sock_path, timeout=timeout)
    except INFRA_ERRORS as e:
        logger.error("gate 失败：设备不可达 via %s: %r", sock_path, e)
        return False, f"device not reachable via {sock_path}: {e!r}"
    if not obj.get("ok") and not obj.get("image"):
        logger.error("gate 失败：截图返回非 ok: %s", obj.get("error"))
        return False, f"gate screenshot not ok: {obj.get('error')}"
    logger.debug("gate ok via %s", sock_path)
    return True, "ok"


@dataclass
class FixtureReadbackRun:
    """Outcome of the Trial-time Readback stage between setup and body."""

    records: list[dict[str, Any]]
    span: tuple[int, int]
    status: str = "ready"            # ready | blocked | error
    detail: str = ""
    blocker: dict[str, str] | None = None


def _not_ready_summary(records: list[dict[str, Any]]) -> str:
    return ", ".join(
        f"{record['fixture_id']}={record.get('reason') or record['status']}"
        for record in records if record["status"] != "ready"
    )


def _run_fixture_readbacks(ctx: CaseContext, case_id: str, plan: dict[str, Any],
                           setup_trace: list[dict[str, Any]]) -> FixtureReadbackRun:
    """Dispatch each declared Readback Operation and judge every Fixture.

    Readbacks are registered Operations dispatched like any recipe step (effect
    policy, API marks and action records included).  A failed Readback never
    becomes ``ready``; an infrastructure error stops the stage and is reported
    as a rig error rather than a Fixture verdict.

    Every Fixture is judged against one snapshot of the Fixture Context taken
    at the end of setup.  A Readback may only add keys; those count for its own
    Fixture alone and join the drift baseline once that Readback finished.  Any
    later change or removal of a baseline value (setup snapshot or an earlier
    Readback's addition) makes every Fixture declaring that key non-ready,
    whichever Readback did it.
    """
    start = len(ctx.results)
    records: list[dict[str, Any]] = []
    stop: tuple[str, str, dict[str, str] | None] | None = None
    snapshot = dict(ctx.fixture_order or {})
    baseline = dict(snapshot)
    drifted: set[str] = set()

    def note_drift() -> None:
        current = ctx.fixture_order or {}
        drifted.update(
            key for key, value in baseline.items()
            if value not in (None, "") and current.get(key) != value
        )

    requirements = list(plan.get("requirements") or [])
    for requirement in requirements:
        if stop is not None:
            records.append(fixture_rb.make_record(
                requirement, status="failed", reason=fixture_rb.REASON_NOT_EXECUTED,
                detail=f"readback stage stopped: {stop[1]}"))
            continue
        before = snapshot
        sources = fixture_rb.context_sources(requirement, before, before, setup_trace)
        step, unresolved = fixture_rb.resolve_readback_step(
            requirement, before, sources, fixture_rb.provider_values(requirement, setup_trace))
        if unresolved:
            # A bound argument without a provider-written value would read back
            # some other entity; the Readback is not dispatched.
            context, _ = fixture_rb.capture_context(requirement, before, before)
            records.append(fixture_rb.make_record(
                requirement, status="failed", reason=fixture_rb.REASON_BINDING_UNRESOLVED,
                detail="unresolved context binding: " + ", ".join(unresolved),
                context=context, sources=sources,
                provider_trace=fixture_rb.provider_trace_for(requirement, setup_trace),
                readback_args=step["args"]))
            continue
        first = len(ctx.results)
        pre = dict(ctx.fixture_order or {})
        ok, detail = False, ""
        failure_reason = fixture_rb.REASON_READBACK_FAILED
        try:
            dispatch_step(ctx, step)
            ok, detail = True, f"readback operation {step['operation']} passed"
        except CaseBlocked as e:
            detail = e.detail
            stop = ("blocked", e.detail, e.blocker)
        except StepFailure as e:
            detail = e.result.detail
        except RecipeError as e:
            detail = f"RecipeError: {e}"
        except INFRA_ERRORS as e:
            detail = f"{type(e).__name__}: {e}"
            failure_reason = fixture_rb.REASON_READBACK_INFRA_ERROR
            stop = ("error", detail, None)
        actions = [{"action_id": r.action_id, "ok": bool(r.ok)}
                   for r in ctx.results[first:] if r.action_id]
        post = dict(ctx.fixture_order or {})
        note_drift()
        # Only this Readback's own additions count as its supplied keys.
        own = {key: value for key, value in post.items()
               if value not in (None, "") and pre.get(key) != value}
        # Its new keys become baseline: a later Readback may not change them either.
        baseline.update({key: value for key, value in own.items()
                         if baseline.get(key) in (None, "")})
        records.append(fixture_rb.judge(
            case_id, requirement,
            readback_ok=ok, readback_detail=detail, readback_actions=actions,
            readback_args=step["args"], setup_trace=setup_trace,
            before=before, after={**before, **own},
            failure_reason=failure_reason,
        ))
    note_drift()
    for requirement, record in zip(requirements, records):
        changed = sorted(set(requirement.get("context_keys") or []) & drifted)
        if record["status"] == "ready" and changed:
            record.update(
                status="failed", reason=fixture_rb.REASON_IDENTITY_REBOUND,
                detail="context changed during the Readback stage: " + ", ".join(changed),
            )
    span = (start, len(ctx.results))
    if stop is not None:
        status, detail, stop_blocker = stop
        return FixtureReadbackRun(records, span, status=status,
                                  detail=fixture_rb.redact_text(f"fixture readback {status}: {detail}"),
                                  blocker=stop_blocker)
    if all(record["status"] == "ready" for record in records):
        return FixtureReadbackRun(records, span)
    return FixtureReadbackRun(
        records, span, status="blocked",
        detail=f"fixture readback not ready: {_not_ready_summary(records)}",
        blocker=blocker(
            "fixture", "fixture_readback_not_ready",
            recovery="inspect fixture-readback.json, repair the Fixture or its Readback, then rerun the Trial",
            evidence_ref=fixture_rb.FIXTURE_READBACK_EVIDENCE_NAME,
        ),
    )


def _redacted_resolved_entity(resolved: Any | None) -> dict[str, Any] | None:
    if resolved is None:
        return None
    from autophone.execution.fixture_readback import _redact

    if isinstance(resolved, dict):
        entity_type = str(resolved.get("entity_type") or "")
        entity_id = resolved.get("entity_id")
        attributes = dict(resolved.get("attributes") or {})
        unresolved = list(resolved.get("unresolved") or ())
    else:
        entity_type = str(getattr(resolved, "entity_type", "") or "")
        entity_id = getattr(resolved, "entity_id", None)
        attributes = dict(getattr(resolved, "attributes", None) or {})
        unresolved = list(getattr(resolved, "unresolved", None) or ())
    return {
        "entity_type": entity_type,
        "entity_id": _redact("entity_id", str(entity_id)) if entity_id not in (None, "") else None,
        "attributes": {str(k): _redact(str(k), v) for k, v in attributes.items()},
        "unresolved": [str(item) for item in unresolved],
    }


def _collect_from_context_keys(value: Any, out: set[str]) -> None:
    if isinstance(value, dict):
        if "from_context" in value and len(value) == 1:
            key = str(value["from_context"] or "").strip()
            if key:
                out.add(key)
            return
        for item in value.values():
            _collect_from_context_keys(item, out)
    elif isinstance(value, list):
        for item in value:
            _collect_from_context_keys(item, out)


def run_case(case: dict[str, Any], shot_dir: Path, sock_path: str,
             timeout: float, capture: Any = None, ax: Any = None,
             prefer_recipe: bool = True, scope: "SetupScope | None" = None,
             order_pool: Any = None, allowed_effects: set[str] | None = None,
             screen_asset_doc: dict[str, Any] | None = None,
             initial_fixture_order: dict[str, Any] | None = None,
             flow_fixture: dict[str, Any] | None = None,
             runtime_scope: RuntimeResourceScope | None = None,
             defer_teardown_ops: tuple[str, ...] = (),
             body_timeout_s: float | None = 300.0,
             ax_backend: str | None = None,
             effect_mode: str | None = None,
             trial_draft_recipes: bool = False,
             confirm_trial_effects: bool = False,
             trial_data_isolation: str = "unisolated",
             symbol_binding: Any | None = None,
             fixture_vars: dict[str, Any] | None = None,
             api_fixtures_path: Path | None = None) -> CaseOutcome:
    cid = case["id"]
    resolved_ax_backend = ax_backend or getattr(ax, "ax_backend", None)
    base = dict(id=cid, module=case.get("module", ""), item=case.get("item", ""),
                row=case.get("row"), coverage=str(case.get("coverage") or ""),
                ax_backend=resolved_ax_backend)
    from autophone.execution.failure_context import definition_identity, exception_stage, failure_fields
    case_failure = failure_fields("case_execution", definition_identity("case", cid, case.get("revision")))

    try:
        contract_for_case(case)
    except SchedulingContractError as e:
        logger.error("用例 %s scheduling 契约无效：%s", cid, e)
        return CaseOutcome(**base, **case_failure, status="error", detail=f"scheduling invalid: {e}")

    # Setup/teardown execution: convert {'op': 'logout', 'with': {...}} to the
    # {'operation': 'logout', 'args': {...}} shape that dispatch_step understands.
    try:
        setup_steps = [_ref_to_step(s) for s in case.get("setup", [])]
        teardown_steps = [_ref_to_step(s) for s in case.get("teardown", [])]
    except (KeyError, TypeError) as e:
        logger.error("用例 %s setup/teardown 引用格式错误：%s", cid, e)
        return CaseOutcome(**base, **case_failure, status="error", detail=f"setup/teardown ref malformed: {e}")

    # Fail-closed（对齐内嵌 recipe）：setup/teardown 步骤同样过静态闸——引用未注册的
    # operation 或参数不符立刻 per-case error。否则坏引用直奔 dispatch_step 抛 RecipeError，
    # 而 RecipeError 不在下面的 except（StepFailure / INFRA_ERRORS）里，会掀翻整个 run loop。
    try:
        validate_recipe(setup_steps)
        validate_recipe(teardown_steps)
    except RecipeError as e:
        logger.error("用例 %s setup/teardown 无效：%s", cid, e)
        return CaseOutcome(**base, **case_failure, status="error", detail=f"setup/teardown invalid: {e}")

    # Single source of truth for recipes: the authored `apps/<app>/recipes/*.yaml`
    # (registered into RECIPES at import, validated on registration). The case yaml must
    # NOT carry an inline `recipe:` — that path was retired because it silently shadowed
    # the external recipe (a stale inline block beat the live file and produced wrong
    # results, e.g. R103). We fail-closed on any leftover inline key so it can never
    # shadow again; recipe_shadow.lint() scans the whole suite for the same offence.
    if isinstance(case.get("recipe"), list) and case.get("recipe"):
        logger.error("用例 %s 带内联 recipe（已退役，真源是 recipes/%s.yaml）", cid, cid.lower())
        return CaseOutcome(**base, **case_failure, status="error",
                           detail="inline recipe is retired; author it in recipes/*.yaml")

    # Executor resolution (the IR cutover, §10.10): a registered recipe wins over the
    # hand-written step-script, unless the kill-switch is set or the caller forces the
    # script path. recipe_shadow passes prefer_recipe=False for its path A so it still
    # measures script-vs-IR (not IR-vs-IR). The recipe is run by inlining dispatch_step
    # under the SAME try/except as a script, so both paths classify fail/error/kind
    # identically — recipes are validated at registration, so dispatch can't RecipeError
    # here. shot_dir/ax/capture wiring is shared.
    recipe_steps = None
    case_uses_symbols = False
    operation_witnesses: list[dict[str, Any]] = []
    # 正在执行的 Recipe 步骤下标；主体 StepFailure 时据此记录 failed_recipe_step。
    recipe_cursor: dict[str, int | None] = {"index": None}
    failure_context: dict[str, Any] = {}
    if prefer_recipe and not _ir_disabled():
        scoped = scoped_recipes()
        registry = RECIPES if scoped is None else scoped
        if scoped is not None and cid in registry and registry[cid] is None:
            logger.info("用例 %s 跳过：显式 draft trial 缺少对应 draft recipe", cid)
            return CaseOutcome(
                **base,
                status="skipped",
                detail="explicit draft trial has no matching draft recipe",
            )
        recipe_steps = registry.get(cid)
    if recipe_steps is not None:
        exec_via = "recipe"
        if recipe_has_unresolved_symbols(recipe_steps):
            try:
                recipe_steps = expand_symbol_steps(
                    recipe_steps,
                    symbol_binding=symbol_binding,
                    screen_asset_doc=screen_asset_doc,
                )
            except RecipeError as e:
                logger.error("用例 %s Recipe 符号绑定展开失败：%s", cid, e)
                return CaseOutcome(
                    **base,
                    **case_failure,
                    status="error",
                    exec_via=exec_via,
                    detail=f"recipe symbol binding invalid: {e}",
                )
        case_uses_symbols = recipe_uses_symbols(recipe_steps)
        try:
            case_binding_sha256 = steps_binding_sha256(recipe_steps)
        except RecipeError as e:
            logger.error("用例 %s Recipe 符号绑定元数据冲突：%s", cid, e)
            return CaseOutcome(
                **base,
                **case_failure,
                status="error",
                exec_via=exec_via,
                detail=f"recipe symbol binding invalid: {e}",
            )
        if case_binding_sha256 is None:
            raw_case_binding = case.get("_symbol_binding_sha256") or case.get("binding_sha256")
            if isinstance(raw_case_binding, str) and raw_case_binding.strip():
                case_binding_sha256 = raw_case_binding.strip()
        if case_binding_sha256 is not None:
            base["binding_sha256"] = case_binding_sha256
        try:
            base["recipe_steps_digest"] = recipe_steps_digest(recipe_steps)
        except Exception:  # noqa: BLE001 - identity evidence must never abort a Run
            logger.warning("用例 %s Recipe steps 摘要计算失败，执行身份记为未核对", cid)
            base["recipe_steps_digest"] = None

        def runnable(c: CaseContext) -> None:
            for index, step in enumerate(recipe_steps):
                recipe_cursor["index"] = index
                observation = None
                try:
                    observation = dispatch_step(c, step)
                except Exception as exc:
                    failure_context.update(failure_fields(exception_stage(exc, "case_execution")))
                    failure_context["failure_in_recipe"] = bool(getattr(exc, "_failure_local", False))
                    if isinstance(exc, StepFailure):
                        observation = exc.result
                    raise
                finally:
                    plan = (case.get("_operation_witness_plans") or {}).get(index)
                    if (plan and observation is not None
                            and case.get("_operation_witness_steps_digest") == base.get("recipe_steps_digest")):
                        operation_witnesses.append({
                            "step_index": index,
                            "operation_identity": dict(plan["operation_identity"]),
                            "stage": getattr(observation, "witness_stage", None),
                            "ok": observation.ok,
                            "shots": list(dict.fromkeys(
                                ([observation.shot_path] if observation.shot_path else [])
                                + observation.aux_shots)),
                        })
            recipe_cursor["index"] = None
    else:
        runnable = CASE_SCRIPTS.get(cid)
        exec_via = "script"
        if runnable is None:
            logger.info("用例 %s 跳过：无 recipe 也无 step-script", cid)
            return CaseOutcome(**base, status="skipped",
                               detail="no recipe or step-script registered")

    # Draft Trial Fixture Readback（usecase assetization §10）：计划由平台只对显式草稿试跑绑定。
    # 无法执行的 Readback 在 setup 之前阻断，避免为一个注定无法证明的 Fixture 付出 provider
    # 副作用（例如消耗一张 UAT 订单）。
    readback_plan = case.get(fixture_rb.READBACK_PLAN_KEY)
    if not isinstance(readback_plan, dict):
        readback_plan = None
    fixture_records: list[dict[str, Any]] | None = None
    cleanup_records: list[dict[str, Any]] | None = None
    if readback_plan is not None:
        static_blocks = fixture_rb.static_block(readback_plan)
        if static_blocks:
            fixture_records = fixture_rb.not_executed_records(
                readback_plan, reason=fixture_rb.REASON_NOT_EXECUTED,
                detail="another declared Fixture Readback cannot run",
                per_fixture=static_blocks,
            )
            summary = ", ".join(
                f"{fixture_id}={reason}" for fixture_id, (reason, _detail) in static_blocks.items()
            )
            logger.warning("用例 %s Fixture Readback 无法执行，setup 前阻断：%s", cid, summary)
            return CaseOutcome(
                **base, status="blocked", exec_via=exec_via,
                failure_stage="case_execution", failed_definition={"status": "unknown"},
                detail=f"fixture readback cannot run: {summary}",
                blocker=blocker(
                    "fixture", "fixture_readback_unexecutable",
                    recovery="declare an executable read-only Readback Operation for every Fixture",
                    evidence_ref=fixture_rb.FIXTURE_READBACK_EVIDENCE_NAME,
                ),
                fixture_readback=fixture_records,
            )

    # capture（Phase 4 抓取句柄）默认 None → CaseContext.api_mark 是 no-op，phase2 行为不变。
    # ax（AX 寻址提供方，AxSession）默认 None → tap_ax 退坐标兜底/fail，phase2 行为不变。
    # 已建好 frida 会话的 run orchestrator 才注入 ax，让 tap_ax 走结构寻址。
    case_effect_mode = (
        effect_mode
        if effect_mode is not None
        else ("draft" if isinstance(case.get("_trial_recipe_document"), dict) else "active")
    )
    ctx = CaseContext(case_id=cid, shot_dir=shot_dir, sock_path=sock_path,
                      timeout=timeout, capture=capture, ax=ax,
                      fixture_order=dict(
                          flow_fixture if flow_fixture is not None
                          else (initial_fixture_order or {})
                      ),
                      order_pool=order_pool if _share_order_case_eligible(case) else None,
                      allowed_effects=allowed_effects,
                      screen_asset_doc=screen_asset_doc,
                      suppress_overlay_dismiss=case_uses_symbols,
                      ax_backend=resolved_ax_backend,
                      effect_mode=case_effect_mode,
                      trial_draft_recipes=trial_draft_recipes,
                      confirm_trial_effects=confirm_trial_effects,
                      trial_data_isolation=trial_data_isolation,
                      runtime_scope=runtime_scope)
    # Preflight owns the effective requirement modes.  Runtime guards consume
    # this immutable-per-case view to decide whether missing evidence is an
    # advisory debt or a hard blocker.
    ctx.requirements = dict(case.get("requirements") or {})
    started_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
    t0 = time.perf_counter()
    status, detail, fail_kind = "pass", "", ""
    failed_recipe_step: int | None = None
    outcome_blocker: dict[str, str] | None = None
    deferred_teardown: list[str] = []
    # Fixture Readback ready 时的 Fixture Context 快照：Cleanup Readback 的 {from_baseline: k} 取这里。
    fixture_baseline: dict[str, Any] = {}
    entity_readback_record: dict[str, Any] | None = None
    entity_joint_verdict: dict[str, Any] | None = None
    resolved_entity_payload: dict[str, Any] | None = None
    preserved_ui_status: str = ""
    with bind_case(cid):
        logger.info("用例开始 %s | %s / %s (via %s)", cid, base["module"], base["item"], exec_via)
        try:
            # Setup（前置造数/复位）失败属于「测试没摆好」，归 error——不是产品 fail。
            # 用独立 try 把它和主体断言失败区分开：混成 fail 会污染 pass/fail/error 三态
            # 归因（看板会把「登录没造好」误报成「功能不符」）。setup 不过即跳过主体。
            setup_ok = True
            # 每个 setup Operation 的执行轨迹（执行/复用 + 动作 id），供 Fixture provider 引用。
            setup_trace: list[dict[str, Any]] = []

            def _dispatch_setup(steps: list[dict[str, Any]]) -> None:
                for setup_step in steps:
                    first = len(ctx.results)
                    prior = dict(ctx.fixture_order or {})
                    done = False
                    try:
                        dispatch_step(ctx, setup_step)
                        done = True
                    except Exception as exc:
                        failure_context.update(failure_fields(exception_stage(exc, "case_execution")))
                        raise
                    finally:
                        current = dict(ctx.fixture_order or {})
                        # Keys this Operation set or changed, inferred by comparing values;
                        # a value carried in before setup is not this Operation's identity.
                        written = {
                            key: value for key, value in current.items()
                            if value not in (None, "") and prior.get(key) != value
                        }
                        setup_trace.append({
                            "operation": str(setup_step.get("operation") or ""),
                            "state": "executed" if done else "failed",
                            "actions": [{"action_id": r.action_id, "ok": bool(r.ok)}
                                        for r in ctx.results[first:] if r.action_id],
                            "wrote_keys": sorted(written),
                            # Raw values stay in memory for binding checks; records drop them.
                            "written": written,
                        })

            if scope is not None and scope.skip_setup:
                # 共享前置：跳过已经由锚用例建立并证明的 setup 前缀。仍往 actions[] 落合成动作，
                # 让 trace 如实显示依赖来自锚，而不是凭空消失。
                skip_count = len(setup_steps) if scope.skip_setup_count < 0 else min(
                    scope.skip_setup_count, len(setup_steps))
                reused_ops = scope.reused_ops or ("login",)
                _t0 = time.perf_counter()
                if reused_ops == ("login",):
                    reused_rec = ctx._record("login_reused", True,
                                             f"login reused from {scope.reused_from or '?'}; "
                                             f"evidence=anchor-establishment", _t0)
                elif reused_ops == ("create_order",):
                    reused_rec = ctx._record("create_order_reused", True,
                                             f"create_order reused from {scope.reused_from or '?'}; "
                                             f"evidence=anchor-create-order", _t0)
                else:
                    reused_rec = ctx._record("setup_prefix_reused", True,
                                             f"setup prefix {list(reused_ops)} reused from "
                                             f"{scope.reused_from or '?'}; evidence=anchor-establishment", _t0)
                setup_trace.extend(
                    {"operation": op, "state": "reused", "reused_from": scope.reused_from or "",
                     "actions": ([{"action_id": reused_rec.action_id, "ok": bool(reused_rec.ok)}]
                                 if reused_rec.action_id else []),
                     "wrote_keys": [], "written": {}}
                    for op in reused_ops
                )
                logger.info("用例 %s 复用 %s 的前置 %s（跳过 %d 个 setup step）",
                            cid, scope.reused_from or "?", list(reused_ops), skip_count)
                remaining_setup = setup_steps[skip_count:]
                if remaining_setup:
                    logger.debug("执行剩余 setup: %d 个操作", len(remaining_setup))
                    try:
                        _dispatch_setup(remaining_setup)
                    except CaseBlocked as e:
                        status, detail, setup_ok, outcome_blocker = "blocked", e.detail, False, e.blocker
                    except StepFailure as e:
                        status, detail, setup_ok = "error", f"setup failed: {e.result.detail}", False
                    except INFRA_ERRORS as e:
                        status, detail, setup_ok = "error", f"setup failed: {type(e).__name__}: {e}", False
            elif setup_steps:
                logger.debug("执行 setup: %d 个操作", len(setup_steps))
                try:
                    _dispatch_setup(setup_steps)
                except CaseBlocked as e:
                    status, detail, setup_ok, outcome_blocker = "blocked", e.detail, False, e.blocker
                except StepFailure as e:
                    status, detail, setup_ok = "error", f"setup failed: {e.result.detail}", False
                except INFRA_ERRORS as e:
                    status, detail, setup_ok = "error", f"setup failed: {type(e).__name__}: {e}", False

            # Fixture Readback：setup 建好的业务状态须经独立 Readback 证明 ready，
            # 否则 Body 不得执行（§10）。setup 未成功时 Readback 不执行，记录为 failed。
            readback_span = (0, 0)
            if readback_plan is not None:
                if setup_ok:
                    readback = _run_fixture_readbacks(ctx, cid, readback_plan, setup_trace)
                    fixture_records, readback_span = readback.records, readback.span
                    fixture_baseline = dict(ctx.fixture_order or {})
                    if readback.status != "ready":
                        setup_ok = False
                        status, detail = readback.status, readback.detail
                        outcome_blocker = readback.blocker
                        log_rb = logger.error if readback.status == "error" else logger.warning
                        log_rb("用例 %s Fixture Readback 未就绪，跳过主体：%s", cid, readback.detail)
                else:
                    fixture_records = fixture_rb.not_executed_records(
                        readback_plan, reason=fixture_rb.REASON_SETUP_NOT_READY,
                        detail=f"setup did not complete: {detail}",
                    )

            # 主体仅在 setup 成功时跑。添加全局超时保护（5分钟）防止 body 执行永久挂起。
            if setup_ok:
                try:
                    with _body_execution_deadline(body_timeout_s):
                        runnable(ctx)
                    # Readback 动作证明 Fixture，不算作主体断言。
                    if not any(r.action.startswith("assert")
                               for i, r in enumerate(ctx.results)
                               if not readback_span[0] <= i < readback_span[1]):
                        allow_no_ui_assert = False
                        if isinstance(case.get("entity_binding"), dict):
                            from autophone.assets.entity_binding import (
                                case_has_entity_binding,
                                entity_binding_policy,
                            )
                            allow_no_ui_assert = (
                                case_has_entity_binding(case)
                                and not entity_binding_policy(case).get("require_ui", True)
                            )
                        if not allow_no_ui_assert:
                            status, detail = "error", f"{exec_via} asserted nothing (vacuous)"
                except BodyExecutionTimeout as e:
                    # Body 执行超时：归 error（infrastructure 问题，不是产品 fail）
                    status, detail = "error", f"body execution timeout: {e}"
                    logger.error("用例 %s body 执行超时（%ss）", cid, body_timeout_s)
                except CaseBlocked as e:
                    status, detail, outcome_blocker = "blocked", e.detail, e.blocker
                except StepFailure as e:
                    status, detail, fail_kind = "fail", e.result.detail, e.result.kind
                    failed_recipe_step = recipe_cursor["index"]
                    # 共享前置的牙在归因（§5.2）：复用用例主体失败时，借来的 login 可能已
                    # 悄悄回退——那不是产品 fail，是前置回退（rig error）。在「这个边界」复探
                    # （run() 拿不到失败上下文）：regressed → 把 fail 重判 error 并清 fail_kind。
                    if scope is not None and scope.reprobe is not None:
                        try:
                            verdict = scope.reprobe(ctx)
                        except Exception as pe:  # noqa: BLE001
                            # 复探自身炸了（UDS/OCR/截图坏）⇒ 无法证明借来的 login 仍在 ⇒
                            # fail-closed 当 regressed：判 error + 让 run() 清 cache（下条重建），
                            # 绝不 fail-open 保留 fail 又继续信任共享态。
                            logger.warning("用例 %s 复用复探异常 → fail-closed 判前置回退：%r", cid, pe)
                            verdict = "regressed"
                        if verdict != "persisted":
                            status, detail, fail_kind = "error", (
                                f"precondition regressed (login lost after reuse from "
                                f"{scope.reused_from or '?'}): {detail}"), ""
                            logger.warning("用例 %s 复用登录已回退 → 归 error（前置回退）", cid)
                except INFRA_ERRORS as e:
                    status, detail = "error", f"{type(e).__name__}: {e}"
                except Exception as e:  # noqa: BLE001
                    # 捕获所有未预期异常（包括可能导致 runner 崩溃的错误）
                    status, detail = "error", f"unexpected exception in body: {type(e).__name__}: {e}"
                    logger.error("用例 %s body 执行遇到未预期异常：%r", cid, e, exc_info=True)
                finally:
                    # 专项二：主体结束立即锁定 post-body housekeeping 窗口并发射 body_end 标记，
                    # 避免后续 Readback/teardown 内嵌 operation 或 symbol 步骤渗入交互窗口。
                    ctx._housekeeping_window_tag = "body_end"
                    ctx._next_api_mark = None
                    ctx.api_mark("body_end", operation=None, window_tag="body_end")

            # 专项二：动作后实例级业务实体 Readback（在 body_end 之后、teardown 之前；
            # 对任何声明 entity_binding.readback 的用例生效，不限于 Draft Trial / Golden Path）。
            if isinstance(case.get("entity_binding"), dict):
                from autophone.assets.entity_binding import (
                    case_has_entity_binding,
                    case_requires_api_capture,
                    case_requires_entity_readback,
                    entity_binding_policy,
                    resolve_case_entity,
                )
                from autophone.observation.frida import api_oracle

                if case_has_entity_binding(case):
                    env_ctx = api_oracle.fixture_context_vars_from_env()
                    file_vars = (
                        api_oracle.flat_fixture_vars(api_fixtures_path, cid)
                        if api_fixtures_path is not None
                        else {}
                    )
                    case_fixture_vars = (
                        dict(case.get("_fixture_vars") or {})
                        if isinstance(case.get("_fixture_vars"), dict)
                        else {}
                    )
                    effective_fixture_vars = {
                        **env_ctx,
                        **file_vars,
                        **case_fixture_vars,
                        **(fixture_vars or {}),
                    }
                    # Preserve referenced {from_context} keys from ctx.fixture_order into
                    # ctx.api_assertion_context before teardown can mutate fixture_order.
                    ctx_keys: set[str] = set()
                    _collect_from_context_keys(case.get("entity_binding"), ctx_keys)
                    for key in ctx_keys:
                        if (
                            key not in ctx.api_assertion_context
                            and key in (ctx.fixture_order or {})
                            and ctx.fixture_order[key] not in (None, "")
                        ):
                            ctx.api_assertion_context[key] = ctx.fixture_order[key]
                    runtime_ctx = {
                        **(ctx.fixture_order or {}),
                        **(ctx.api_assertion_context or {}),
                    }
                    resolved = resolve_case_entity(
                        case,
                        fixture_vars=effective_fixture_vars,
                        runtime_context=runtime_ctx,
                    )
                    resolved_entity_payload = _redacted_resolved_entity(resolved)
                    policy = entity_binding_policy(case)
                    if case_requires_entity_readback(case):
                        if setup_ok and (
                            status == "pass"
                            or (status == "fail" and not policy.get("require_ui", True))
                        ):
                            entity_readback_record = entity_rb.run_entity_readback(
                                ctx,
                                case,
                                resolved_entity=resolved,
                                fixture_vars=effective_fixture_vars,
                                dispatch=dispatch_step,
                            )
                        else:
                            entity_readback_record = entity_rb.not_executed_record(
                                case,
                                resolved_entity=resolved,
                                detail=(
                                    f"setup did not complete: {detail}"
                                    if not setup_ok
                                    else f"case body did not pass (status={status})"
                                ),
                            )
                    if not case_requires_api_capture(case) and status in ("pass", "fail"):
                        from autophone.observation.joint_verdict import arbitrate_joint_verdict

                        preserved_ui_status = status
                        entity_joint_verdict = arbitrate_joint_verdict(
                            ui_status=status,
                            api_status=None,
                            api_assertions=None,
                            api_captured=False,
                            readback_record=entity_readback_record,
                            entity_binding=case.get("entity_binding"),
                            resolved_entity=resolved,
                        )
                        if entity_joint_verdict.get("status") != "pass":
                            status = str(entity_joint_verdict.get("status") or "fail")
                            if preserved_ui_status == "pass" or not fail_kind:
                                fail_kind = str(entity_joint_verdict.get("fail_kind") or "")
                                detail = str(entity_joint_verdict.get("detail") or detail)
        finally:
            # 专项二：进入 teardown / cleanup_readback 前锁定 post_case_cleanup 窗口，
            # 确保嵌套 dispatch_step("operation") 或符号点击均带 window_tag="post_case_cleanup" 且无 symbol_id。
            ctx._housekeeping_window_tag = "post_case_cleanup"
            ctx._next_api_mark = None
            if (teardown_steps and not defer_teardown_ops) or cleanup_rb.declared_cleanups(readback_plan):
                ctx.api_mark("post_case_cleanup", operation=None, window_tag="post_case_cleanup")
            # Teardown 是清理/复位，必须在主体失败时也跑——否则失败用例把设备留在脏态，
            # 拖垮后续用例（这正是它该用 finally 而非紧跟主体的原因）。独立 try：teardown
            # 自身失败不掩盖主体信号——主体已 fail/error 只记 warning；主体本通过才把状态
            # 降级为 error（复位未达成，设备状态不可信）。
            if teardown_steps and defer_teardown_ops:
                # The next case in this group re-anchors the same state, so this
                # restore would only reach a state that case is about to reach
                # anyway.  Recorded on the outcome rather than silently dropped:
                # a cleanup that did not run has to stay visible in the product.
                logger.info("用例 %s teardown 交由下一条同名前置重建，本条跳过：%s",
                            cid, ", ".join(defer_teardown_ops))
                deferred_teardown = list(defer_teardown_ops)
            elif teardown_steps:
                logger.debug("执行 teardown: %d 个操作", len(teardown_steps))
                try:
                    for step in teardown_steps:
                        dispatch_step(ctx, step)
                except (StepFailure, *INFRA_ERRORS) as e:
                    td_detail = (e.result.detail if isinstance(e, StepFailure)
                                 else f"{type(e).__name__}: {e}")
                    logger.warning("用例 %s teardown 失败：%s", cid, td_detail)
                    if status == "pass":
                        status, detail = "error", f"teardown failed: {td_detail}"
                        failure_context.update(failure_fields(exception_stage(e, "case_execution")))
            # Cleanup Readback（BL-083 第八轮）：Fixture 声明了 cleanup 时，在 teardown 之后执行清理
            # Operation 并以独立只读 Readback 证明回到 setup 基线。清理未证实时与 teardown 失败同口径：
            # 主体本通过才降级为 error。
            if cleanup_rb.declared_cleanups(readback_plan):
                ready = {str(r.get("fixture_id")) for r in fixture_records or [] if r.get("status") == "ready"}
                try:
                    cleanup_records = cleanup_rb.run_cleanup_readbacks(
                        ctx, readback_plan, fixture_baseline, ready, dispatch_step,
                        (StepFailure, CaseBlocked, RecipeError, *INFRA_ERRORS))
                except Exception as e:  # noqa: BLE001 - 记录为 failed，不掩盖主体结论
                    logger.warning("用例 %s Cleanup Readback 异常：%r", cid, e)
                    cleanup_records = cleanup_rb.not_executed_records(
                        readback_plan, f"cleanup stage raised {type(e).__name__}: {e}")
                    for record in cleanup_records:
                        record.update(status="failed", reason="cleanup_stage_error")
                logger.info("用例 %s Cleanup Readback：%s", cid, cleanup_rb.summary(cleanup_records))
                failed = [r for r in cleanup_records if r["status"] == "failed"]
                if failed and status == "pass":
                    status, detail = "error", f"cleanup readback failed: {cleanup_rb.summary(failed)}"
            if ctx.order_pool is not None and ctx.order_pool_lease:
                plan = _order_pool_release_plan(case) or {"mode": "destructive"}
                consumed = True
                produce_state = None
                if status == "pass":
                    if plan.get("mode") == "read_only":
                        consumed = False
                    elif plan.get("mode") == "advance":
                        produce_state = str(plan.get("produce_state") or "")
                try:
                    ctx.order_pool.release(
                        cid,
                        consumed=consumed,
                        produce_state=produce_state,
                        probe_readback=_lease_path(ctx.order_pool_lease, "probe_readback"),
                        probe_live_capture=_lease_path(ctx.order_pool_lease, "probe_live_capture"),
                        platform_history=_lease_path(ctx.order_pool_lease, "platform_history"),
                    )
                except Exception as e:  # noqa: BLE001 - pool bookkeeping must not mask case truth
                    logger.warning("用例 %s order pool release 失败：%r", cid, e)
                    if status == "pass":
                        status, detail = "error", f"order pool release failed: {type(e).__name__}: {e}"
            resource_errors = ctx.close_runtime_resources()
            if resource_errors:
                cleanup_detail = "; ".join(resource_errors)
                logger.warning("用例 %s runtime resource 清理失败：%s", cid, cleanup_detail)
                if status == "pass":
                    status, detail = "error", f"runtime resource cleanup failed: {cleanup_detail}"
        dur = round((time.perf_counter() - t0) * 1000, 1)
        log_at = logger.info if status == "pass" else logger.warning
        log_at("用例结束 %s status=%s via=%s (%.0fms) %d 个动作%s",
               cid, status, exec_via, dur, len(ctx.results),
               f" | {detail}" if detail else "")

    actions = [vars(r) for r in ctx.results]
    shots = []
    for r in ctx.results:
        if r.shot_path and r.shot_path not in shots:
            shots.append(r.shot_path)
        for aux in r.aux_shots:  # 增强 OCR 等派生帧也进失败帧列表，md 报告可见判定输入
            if aux not in shots:
                shots.append(aux)
    # A flow is the one intentional exception to the ordinary per-case fixture
    # isolation.  Its mutable carrier remains private to runner.run(), and is
    # refreshed only after a passing step.  Failed steps never leak a half-made
    # order identity to their successors.
    if flow_fixture is not None and status == "pass":
        flow_fixture.clear()
        flow_fixture.update(ctx.fixture_order)
    if status in {"fail", "error", "blocked"}:
        if not failure_context:
            failure_context = failure_fields("case_execution")
    else:
        failure_context = {}
    return CaseOutcome(**base, **failure_context, status=status, detail=detail, blocker=outcome_blocker, fail_kind=fail_kind,
                       exec_via=exec_via, duration_ms=dur, started_at=started_at,
                       actions=actions, shots=shots, deferred_teardown=deferred_teardown,
                       ui_status=preserved_ui_status,
                       fixture_readback=fixture_records,
                       cleanup_readback=cleanup_records,
                       operation_witnesses=operation_witnesses or None,
                       api_assertion_context=dict(getattr(ctx, "api_assertion_context", None) or {}) or None,
                       failed_recipe_step=failed_recipe_step if status == "fail" else None,
                       symbol_observations=list(ctx._symbol_observations) or None,
                       entity_readback=entity_readback_record,
                       joint_verdict=entity_joint_verdict,
                       resolved_entity=resolved_entity_payload)


def _with_execution_group_runtime(fn):
    """Guarantee group resource cleanup across returns and scheduler errors."""

    @wraps(fn)
    def wrapped(*args, **kwargs):
        runtime_scope = RuntimeResourceScope()
        result = None
        try:
            result = fn(*args, _runtime_scope=runtime_scope, **kwargs)
            return result
        finally:
            resource_errors = runtime_scope.close()
            if resource_errors:
                cleanup_detail = "; ".join(resource_errors)
                logger.error(
                    "execution-group runtime resource 清理失败：%s",
                    cleanup_detail,
                )
                if result is not None:
                    result.aborted = True
                    result.abort_reason = (
                        f"runtime resource cleanup failed: {cleanup_detail}"
                    )

    return wrapped


@_with_execution_group_runtime
def run(cases_path: Path, *, selector: CaseSelector | dict[str, Any] | None = None,
        automation: str | None = None, ids: list[str] | None = None, sock_path: str | None = None,
        shot_dir: Path, timeout: float = 15.0, capture: Any = None, ax: Any = None,
        allowed_effects: set[str] | None = None,
        screen_asset_doc: dict[str, Any] | None = None,
        initial_fixture_order: dict[str, Any] | None = None,
        abort_check: Callable[[], str | None] | None = None,
        progress_cb: Callable[[list[dict[str, Any]]], None] | None = None,
        selected_cases: list[dict[str, Any]] | None = None,
        ax_backend: str | None = None,
        effect_mode: str | None = None,
        trial_draft_recipes: bool = False,
        confirm_trial_effects: bool = False,
        confirmed_trial_case_ids: set[str] | None = None,
        trial_data_isolation: str = "unisolated",
        symbol_binding: Any | None = None,
        api_fixtures_path: Path | None = None,
        _runtime_scope: RuntimeResourceScope | None = None) -> RunResult:
    resolved_ax_backend = ax_backend or getattr(ax, "ax_backend", None)
    resolved_fixtures_path = api_fixtures_path
    if resolved_fixtures_path is None:
        candidate_fixtures = Path(cases_path).resolve().parent.parent / "api_fixtures.yaml"
        if candidate_fixtures.is_file():
            resolved_fixtures_path = candidate_fixtures
    # Direct runner callers activate only the App selected by the case path.
    # Application execution already supplies a frozen recipe scope.
    if scoped_recipes() is None:
        from autophone.execution.app_recipes import import_app_recipes

        for app_dir in Path(cases_path).resolve().parents:
            if app_dir.parent.name == "apps":
                import_app_recipes(app_dir.name, root=app_dir.parent.parent)
                break
    sock_path = sock_path or resolve_sock()
    case_selector = CaseSelector.from_raw(selector, automation=automation, ids=ids)
    # The platform planner may supply an already-admitted run sequence.  It owns
    # both membership and private sharing-boundary markers; do not reload/reselect
    # it here or an asset/capability gap could be erased before scheduling.
    selected = list(selected_cases) if selected_cases is not None else select_cases(
        load_cases(cases_path), selector=case_selector)
    logger.info("run 开始：选中 %d 个用例 (selector=%s) sock=%s",
                len(selected), case_selector.to_dict(), sock_path)

    # 逐用例进度快照（默认 None → no-op，行为不变）：在每个用例边界把「全量用例清单 + 各自
    # 当前态」整体回吐给 progress_cb，供实时看板（用例库左栏）展示生命周期——哪条通过、哪条
    # 失败、哪条正在跑。pending → executing → pass/fail/error/skipped。fail-open：cb 抛错只 debug
    # 不打断回归。
    prog = [{"id": c["id"], "item": c.get("item", ""), "status": "pending"} for c in selected]

    def _emit() -> None:
        if progress_cb is None:
            return
        try:
            progress_cb([dict(p) for p in prog])
        except Exception as e:  # noqa: BLE001 进度上报失败绝不影响回归本身
            logger.debug("progress_cb 失败（忽略）：%r", e)

    ok, reason = gate(sock_path)
    if not ok:
        logger.error("run 在 gate 处中止：%s", reason)
        return RunResult(aborted=True, abort_reason=reason, ax_backend=resolved_ax_backend)

    _emit()  # gate 过后即吐一版全 pending 快照，左栏开跑前就列全用例

    # capture（Phase 4 抓取句柄）默认 None → 每个 run_case 的 api_mark no-op，行为不变；
    # 非 None 时逐用例注入同一句柄，抓到的 API 调用按 (case_id, step) 时间窗归因。
    # 共享前置调度：在 select_cases() 之后的**相邻**序列上，把连续、前置态身份相同的
    # 已证明前缀压成一次。默认仍只有 login；Phase3/4 只在稳定 scheduling 契约显式声明时触发。
    # established = 当前设备上「相信已建立」的 ShareCandidate；anchor = 真建它的那条用例。
    # T1 不重排（零顺序风险）；失配即忘 cache、绝不发明清理（§4.2 步 3a）。负控不经 run()，
    # 结构上碰不到本调度（§六）；env 杀开关可整体退回逐用例自建。
    sharing = not _share_disabled()
    active_flow_id = case_selector.flow or _implicit_flow_id(selected)
    flow_fixture: dict[str, Any] | None = {} if active_flow_id else None
    flow_failed_at = ""
    # 订单实例池依赖 App 侧的订单创建与回读适配器；公开版不内置，始终逐用例自建。
    order_pool = None
    established: ShareCandidate | None = None
    anchor_id = ""
    result = RunResult(aborted=False, abort_reason="", ax_backend=resolved_ax_backend)
    if _runtime_scope is None:  # only possible through an explicit __wrapped__ call
        raise RuntimeError("execution-group runtime scope was not initialized")
    for i, case in enumerate(selected):
        # 协作式中止（abort_check 默认 None → 无行为变化）：用例边界读外部信号，见即停。
        # 已跑用例结果保留在 outcomes，aborted=True 让 overall 收口为 blocked。
        if abort_check is not None:
            abort_reason = abort_check()
            if abort_reason:
                logger.warning("run 协作式中止：%s（已跑 %d 个用例）",
                               abort_reason, len(result.outcomes))
                result.aborted = True
                result.abort_reason = abort_reason
                break
        prog[i]["status"] = "executing"
        _emit()

        cycle_skip_reason = str(case.get("_cycle_skip_reason") or "")
        if cycle_skip_reason:
            # Runtime lifecycle planning may discover that the device has
            # already passed an earlier stage in this externally-dispatched
            # order cycle.  Do not run that case against an impossible
            # precondition (which would be a false product failure); retain a
            # visible skipped outcome so the user's selection never vanishes.
            outcome = CaseOutcome(
                id=str(case["id"]), status="skipped",
                module=str(case.get("module") or ""),
                item=str(case.get("item") or ""), row=case.get("row"),
                detail=cycle_skip_reason,
            )
            result.outcomes.append(outcome)
            prog[i]["status"] = outcome.status
            established, anchor_id = None, ""
            _emit()
            logger.info("用例 %s 按履约当前态跳过：%s", case["id"], cycle_skip_reason)
            continue

        if flow_failed_at:
            outcome = CaseOutcome(
                id=str(case["id"]), status="blocked", module=str(case.get("module") or ""),
                item=str(case.get("item") or ""), row=case.get("row"),
                detail=f"flow blocked: prerequisite {flow_failed_at} did not pass",
                blocker=blocker(
                    "fixture",
                    "predecessor_failed",
                    recovery=(f"repair prerequisite {flow_failed_at}, then rerun the complete flow"),
                ),
            )
            result.outcomes.append(outcome)
            prog[i]["status"] = outcome.status
            _emit()
            continue

        if case.get("_break_shared_precondition_before"):
            established, anchor_id = None, ""
        try:
            candidate = _share_candidate(case, sharing=sharing)
        except SchedulingContractError:
            # run_case 会把坏契约收成当前用例的 error；这里先禁用共享，避免一条坏资产
            # 在 scheduler 判定阶段掀翻整批。
            candidate = None
        scope = None
        if candidate is not None and candidate == established:
            # 相邻且同前置态身份 → 复用锚的前置：跳过已证明前缀。任何被复用的层里只要
            # 有一个 op 名带「login」字样就接归因复探（不再局限于 reused_ops 恰好
            # == ("login",) 这个精确元组——声明前缀里带真登录步的多操作场景同样该有
            # 复探兜底，见 finding #2）。create_order pool / 不含 login 的声明前缀目前
            # 没有对应的活体探针，reprobe 留空——下面的 cache 清除逻辑对这类无探针场景
            # 单独 fail-closed。
            reprobe = _make_login_reprobe() if any("login" in op for op in candidate.reused_ops) else None
            scope = SetupScope(skip_setup=True, reused_from=anchor_id,
                               skip_setup_count=candidate.skip_count, reused_ops=candidate.reused_ops,
                               reprobe=reprobe)

        case_confirmed = (
            str(case.get("id") or "").upper() in confirmed_trial_case_ids
            if confirmed_trial_case_ids is not None
            else confirm_trial_effects
        )
        outcome = run_case(
            case,
            shot_dir,
            sock_path,
            timeout,
            capture=capture,
            ax=ax,
            scope=scope,
            order_pool=order_pool,
            allowed_effects=allowed_effects,
            screen_asset_doc=screen_asset_doc,
            initial_fixture_order=initial_fixture_order,
            flow_fixture=flow_fixture,
            runtime_scope=_runtime_scope,
            # One execution group is one run() call, so "next in this list" is
            # exactly "next in this group" and the last case always restores.
            defer_teardown_ops=_deferrable_teardown(
                case, selected[i + 1] if i + 1 < len(selected) else None),
            ax_backend=resolved_ax_backend,
            effect_mode=effect_mode,
            trial_draft_recipes=trial_draft_recipes,
            confirm_trial_effects=case_confirmed,
            trial_data_isolation=trial_data_isolation,
            symbol_binding=symbol_binding,
            api_fixtures_path=resolved_fixtures_path,
        )
        result.outcomes.append(outcome)
        if flow_fixture is not None and outcome.status != "pass":
            flow_failed_at = str(case["id"])
        cleanup_configured = bool(case.get("post_case_cleanup"))
        if cleanup_configured:
            _run_post_case_cleanup(case, shot_dir=shot_dir, sock_path=sock_path,
                                   timeout=timeout, capture=capture, ax=ax,
                                   allowed_effects=allowed_effects,
                                   screen_asset_doc=screen_asset_doc,
                                   ax_backend=resolved_ax_backend,
                                   effect_mode=effect_mode,
                                   trial_draft_recipes=trial_draft_recipes,
                                   confirm_trial_effects=case_confirmed,
                                   trial_data_isolation=trial_data_isolation)

        # 更新 scheduler cache（失配即忘；建态需强证据；复用回退则清记忆让下条重建）。
        if not sharing:
            pass
        elif cleanup_configured:
            # cleanup 不改上一条 outcome，但可能清掉共享态；调度缓存必须同步失效。
            established, anchor_id = None, ""
        elif scope is not None:
            # 复用用例：复探判回退（→ error）忘 cache。没有复探兜底的机制（create_order
            # pool、不含 login 的声明前缀）一旦主体 `fail`，无法证明前置态仍在——不能像
            # 有复探那样区分「真产品 bug」与「前置回退」，fail-closed 直接忘 cache，不让
            # 下一条继续信任一个可能已经回退的共享态（finding #2；产品状态字段不动，
            # 仍如实报 fail，只是不再复用）。
            if outcome.status == "error" or (outcome.status == "fail" and scope.reprobe is None):
                established, anchor_id = None, ""
        elif candidate is not None:
            # 本条是共享锚：只有**主体也 pass** 才把它记成可信锚。`fail` 可能在主体期
            # 污染了页面/会话、甚至就是登录态在主体中途丢了（§九「失败用例污染共享态」）；
            # 锚无复探兜底（复探只挂在复用用例上），故 fail-closed：anchor 非 pass 一律不缓存，
            # 让下一条同前置用例重新 dispatch login。`error`（setup 没建成）同样不缓存。
            if outcome.status == "pass":
                established, anchor_id = candidate, case["id"]
            else:
                established, anchor_id = None, ""
        else:
            # 非共享候选（异/无/多层 setup，或带 teardown）→ 相邻段中断，忘 cache。
            established, anchor_id = None, ""

        prog[i]["status"] = outcome.status
        # 执行 Trace（docs/run-execution-trace.md）：已完成用例回填耗时，让 mid-run 瀑布流
        # 在结果落盘前就能逐条长出耗时（trace.build_trace 的 partial 路径读这个字段）。
        prog[i]["duration_ms"] = outcome.duration_ms
        _emit()
    c = result.counts
    logger.info("run 结束：pass=%d fail=%d error=%d skipped=%d",
                c["pass"], c["fail"], c["error"], c["skipped"])
    return result
