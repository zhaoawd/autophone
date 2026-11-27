"""ai_agent.py — AI-8 agent 侧两条 best-effort lane（platform-ai-language-to-execution P5）。

资产侧（P1-P4）追**完整覆盖**、产物经区分力闸 + 人审才转 active；本模块是**另一条执行面**：日常一次性
任务的 best-effort 通用手机 agent，**不进回归库、无转正门**（§五不变量①），只与资产侧共用 `vphone_mcp`
设备面 + `ScreenLock` 串行。

两条 lane（§四）：
- **Lane 3 读屏汇报**（`report`）：零副作用，放开跑还一段结构化摘要（"看今天要关注啥"）。
- **Lane 2 一次性任务**（`run_task`）：先**只读规划**出整份动作清单（不碰设备），host 侧按**可逆性分级**
  定门——清单含未知/不可逆动作即**整盘（或节点）拿去预批、确认前一步不执行**（不做半程执行器：不会先跑前序、
  跑到不可逆点才停），附"即将干什么"+ 截图证据，人 go 后才执行 approved plan。

**安全不 best-effort，成功率才 best-effort**（§四）：blast radius 被钉死——不可逆/对外动作**绝不**在没拿到
人批的情况下被执行。这条是本模块的脊，host 侧确定性可证（`classify_action`/`classify_plan` + `run_task`
的门）；真机执行是 best-effort 的活 VM 边界（同 P2/P3，executor 为可注入接缝）。

信任纪律：不信任 agent 自我声明的可逆性——self-declared `reversible=true` 也要过 host 侧标记复核，
命中不可逆/对外标记一律判不可逆（AI 不能把自己的危险动作标成安全的）。
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from autophone.paths import STATE_ROOT
from autophone.application.ai import (
    AiRequest,
    AiResult,
    CommandRunner,
    DEFAULT_TIMEOUT_S,
    _subprocess_runner,
    invoke,
    require_device_actions,
    unmanaged_device_refusal_result,
)
from autophone.assets.apps import load_app_profile
from autophone.application.ai_explore import ScreenshotFn, _default_screenshot
from autophone.execution.effects import (
    IRREVERSIBLE,
    REVERSIBLE,
    classify_freeform_action,
)
from autophone.application.familiarity import Familiarity, app_familiarity
from autophone.application.ai_jobs import request_digest
from autophone.device.lock import ScreenLock, ScreenLockBusy
from autophone.util import now_iso as _now

from autophone.device.target import default_device

DEVICE = default_device()

# plan 分级（run_task 的门）：
AUTONOMOUS = "autonomous"                  # 熟悉 app + 纯可逆 → 放开跑，只还末端汇报
CHECKPOINT = "checkpoint"                  # 熟悉 app + 含不可逆 → 跑到不可逆前暂停，逐点确认
FULL_PLAN_APPROVAL = "full_plan_approval"  # 未知 app / 首次 → 整盘预批，任何动作前都要人 go


@dataclass
class Action:
    """规划阶段产出的一条意图动作。`reversible` 是 agent 自我声明，host 侧 `classify_action` 会复核。"""
    verb: str
    target: str = ""
    intent: str = ""
    reversible: bool | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Action":
        return cls(verb=str(d.get("verb") or ""), target=str(d.get("target") or ""),
                   intent=str(d.get("intent") or ""), reversible=d.get("reversible"))


@dataclass
class PlanClassification:
    tier: str
    first_irreversible: int | None  # plan 中第一个不可逆动作的 index（无则 None）
    reason: str


@dataclass(frozen=True)
class FrozenPlan:
    """The exact plan and device identity approved for later execution."""

    task: str
    actions: tuple[Action, ...]
    tier: str
    reason: str
    first_irreversible: int | None
    app: str | None
    device: str
    build: str | None
    frontmost_bundle: str | None
    evidence_ref: str | None
    captured_at: str
    approval_expires_at: str
    actor: str
    familiarity: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "actions": [vars(action) for action in self.actions],
            "tier": self.tier,
            "reason": self.reason,
            "first_irreversible": self.first_irreversible,
            "app": self.app,
            "device": self.device,
            "build": self.build,
            "frontmost_bundle": self.frontmost_bundle,
            "evidence_ref": self.evidence_ref,
            "captured_at": self.captured_at,
            "approval_expires_at": self.approval_expires_at,
            "actor": self.actor,
            "familiarity": self.familiarity,
        }

    @property
    def digest(self) -> str:
        return request_digest(self.to_dict())

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FrozenPlan":
        return cls(
            task=str(value["task"]),
            actions=tuple(Action.from_dict(item) for item in value["actions"]),
            tier=str(value["tier"]),
            reason=str(value["reason"]),
            first_irreversible=value.get("first_irreversible"),
            app=value.get("app"),
            device=str(value["device"]),
            build=value.get("build"),
            frontmost_bundle=value.get("frontmost_bundle"),
            evidence_ref=value.get("evidence_ref"),
            captured_at=str(value["captured_at"]),
            approval_expires_at=str(value["approval_expires_at"]),
            actor=str(value["actor"]),
            familiarity=dict(value.get("familiarity") or {}),
        )


def classify_action(a: Action) -> str:
    """单个动作可逆性分级。fail-closed 偏保守：agent 自报 reversible=False、或**动作本体**（verb+target）
    命中不可逆/对外标记，一律判 IRREVERSIBLE（不信任 AI 把危险动作标成安全的）。其余判 REVERSIBLE
    （读/导航放开）。**只扫 verb+target，不扫 intent**——intent 是叙述性说明（如 type 步 intent「填入要
    发送的消息内容」含「发送」却并非发送动作），扫它会误拦上游步骤；动作的副作用由 verb+target 定。"""
    return classify_freeform_action(a.verb, a.target, reversible=a.reversible).effect


def classify_plan(plan: list[Action], *, app_known: bool, familiar: bool) -> PlanClassification:
    """整盘分级（§四 Lane 2）：未知 app / 首次 → 整盘预批；熟悉路径才降级——含不可逆动作走 checkpoint
    （不可逆前暂停），纯可逆才 autonomous（放开跑）。"""
    first_irr = next((i for i, a in enumerate(plan) if classify_action(a) == IRREVERSIBLE), None)
    if not app_known or not familiar:
        why = "未知 app" if not app_known else "首次/不熟悉路径"
        return PlanClassification(FULL_PLAN_APPROVAL, first_irr, f"{why} → 整盘预批")
    if first_irr is not None:
        tgt = plan[first_irr].target or plan[first_irr].verb
        return PlanClassification(CHECKPOINT, first_irr, f"含不可逆/对外动作「{tgt}」→ 暂停待 go")
    return PlanClassification(AUTONOMOUS, None, "熟悉 app + 纯可逆 → 放开跑")


# ─────────────────────────── 规划（只读，无副作用） ───────────────────────────

_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["verb", "target", "intent", "reversible"],
    "properties": {
        "verb": {"type": "string", "minLength": 1, "description": "动作（tap/swipe/type/read/...）"},
        "target": {"type": "string", "description": "作用对象（按钮/字段文案）"},
        "intent": {"type": "string", "description": "这步要达成什么"},
        "reversible": {"type": "boolean", "description": "是否可逆（发送/提交/支付类填 false）"},
    },
}
_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["plan", "rationale"],
    "properties": {
        "plan": {"type": "array", "minItems": 1, "items": _ACTION_SCHEMA},
        "rationale": {"type": "string"},
    },
}

_PLAN_SYSTEM = (
    "你是 autophone agent lane 的**规划器**。只**看当前屏 + 任务**，给一份达成任务的**有序动作清单**，"
    "每条 verb/target/intent，并据实标 reversible——**发送/提交/支付/下单/删除/发布等对外或不可逆动作填 "
    "reversible:false**。这是规划，**不要执行**；走不通就如实少写、在 rationale 里说明卡点。"
)

_REPORT_SYSTEM = (
    "你是 autophone agent lane 的**读屏汇报器**（Lane 3，零副作用）。只**看**当前屏并回答，"
    "**绝不点按/输入/改任何状态**。给一段可核对的结构化摘要。"
)


def _build_plan_prompt(task: str) -> str:
    return f"任务：{task}\n\n看当前屏，给达成它的有序动作清单（含 reversible 标注）。"


def _propose_plan(
    task: str, *, provider: str | None, model: str | None, timeout_s: float,
    runner: CommandRunner, screenshot: ScreenshotFn, shot_path: Path | None,
) -> tuple[AiResult, Path | None]:
    """只读规划：喂当前屏截图做纯推理（tools=none，**不碰设备**），产出结构化 plan。"""
    dest = shot_path or (STATE_ROOT / "runs" / ".ai" / "agent-shot.jpg")
    shot = screenshot(dest)
    req = AiRequest(
        prompt=_build_plan_prompt(task), system=_PLAN_SYSTEM, tools="none",
        images=[shot] if shot else [], json_schema=_PLAN_SCHEMA,
        provider=provider, model=model, timeout_s=timeout_s,
    )
    return invoke(req, runner=runner), shot


# ─────────────────────────── Lane 2：一次性任务 ───────────────────────────

# 执行接缝：拿到（已批的/纯可逆的）plan，经设备面真驱动。默认实现走 agent + vphone_mcp 进 ScreenLock；
# 真机执行是 best-effort 活 VM 边界，单测注入 fake 验证「门」——pending 时绝不调它。
ExecutorFn = Callable[[str, list[Action], str], AiResult]


def _default_executor(
    task: str, plan: list[Action], run_id: str, *,
    approved: bool = False,
    provider: str | None = None, model: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S, runner: CommandRunner = _subprocess_runner,
    lock: ScreenLock | None = None, device_name: str = DEVICE,
) -> AiResult:
    """把（已批的）plan 交 agent 经 vphone_mcp 真驱动到完成。§五不变量①：碰屏必经 ScreenLock 串行
    （与 verification/regression 同一把锁）。"""
    steps = "\n".join(f"  {i+1}. {a.verb} {a.target} — {a.intent}".rstrip()
                      for i, a in enumerate(plan))
    authorization = (
        "人工批准本次计划"
        if approved else "宿主策略允许熟悉 App 的纯可逆计划自动执行"
    )
    prompt = (f"任务：{task}\n授权来源：{authorization}。\n\n"
              f"按以下清单在设备上执行到完成：\n{steps}\n\n"
              "遇到清单外状态停止并报告；执行完简述结果。")
    req = AiRequest(prompt=prompt, tools="device", provider=provider, model=model, timeout_s=timeout_s)
    refused = unmanaged_device_refusal_result(req)
    if refused is not None:  # 控制面已配置且无 StageGuard：取锁、起 CLI 之前拒绝
        return refused
    lock = lock or ScreenLock()
    with lock.acquire(device=device_name, run_id=run_id):
        # 计划非空即需要设备动作：设备工具留痕里没有一次成功调用时不报告为执行成功（BL-067）
        return require_device_actions(invoke(req, runner=runner))


def _app_known(app: str | None) -> bool:
    if not app:
        return False
    try:
        load_app_profile(app)
        return True
    except Exception:  # noqa: BLE001 - 未知 app 是 agent lane 的合法常态，不是错误
        return False


def _uds_frontmost() -> tuple[str | None, str | None]:
    """UDS ``app_foreground`` 读数，返回 ``(bundle_id, source)``。

    BL-232 阶段 1（P3）：身份锚点不再经 Frida attach SpringBoard 读前台。只有响应带
    ``source`` 且不为 ``unknown`` 时才采用其 ``bundle_id``（主屏为空串）；旧 guest 不带
    ``source``、新 guest 报 ``unknown``、命令不支持或设备不可达时，前台记为不可得
    （``None``）。与 ``execution/foreground.py`` 判定可信来源的口径相同。
    """
    try:
        from autophone.device.uds import request
        from autophone.execution.engine.actions import resolve_sock

        response, _sample = request({"t": "app_foreground"}, sock_path=resolve_sock(), timeout=5)
    except Exception:  # noqa: BLE001 - 读不到前台即记为不可得，不阻断计划
        return None, None
    if not isinstance(response, dict) or response.get("ok") is not True:
        return None, None
    source = response.get("source")
    if not isinstance(source, str) or not source or source == "unknown":
        return None, source if isinstance(source, str) and source else None
    return str(response.get("bundle_id") or ""), source


def capture_device_identity(app: str | None, *, device: str = DEVICE) -> dict[str, Any]:
    """Read the identity anchors frozen with an interactive plan.

    前台锚点只取 UDS ``app_foreground`` 的可信读数（见 ``_uds_frontmost``），不 attach
    SpringBoard。iOS 26 guest 常报 ``source=unknown``，此时前台锚点为 ``None``，锚点只
    剩 device、app、build（评估文档 §4.3 P3：前台变化不再被锚点检出）。
    """
    build = None
    bundle_id = None
    if app:
        try:
            profile = load_app_profile(app)
            build = profile.build_label
            bundle_id = profile.bundle_id
        except Exception:  # unknown app is a legitimate full-plan-approval case
            pass
    frontmost_bundle, frontmost_source = _uds_frontmost()
    return {
        "device": device,
        "app": app,
        "build": build,
        "expected_bundle": bundle_id,
        "frontmost_bundle": frontmost_bundle,
        "frontmost_source": frontmost_source,
    }


def plan_task(
    task: str,
    *,
    actor: str,
    app: str | None,
    device_state: dict[str, Any],
    provider: str | None = None,
    model: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    runner: CommandRunner = _subprocess_runner,
    screenshot: ScreenshotFn = _default_screenshot,
    shot_path: Path | None = None,
    familiarity_fn: Callable[[str | None], "Familiarity"] | None = None,
    approval_window_s: int = 900,
) -> tuple[AiResult, FrozenPlan | None]:
    """Plan only.  This function has no execution path and returns a digestible plan."""
    res, shot = _propose_plan(
        task,
        provider=provider,
        model=model,
        timeout_s=timeout_s,
        runner=runner,
        screenshot=screenshot,
        shot_path=shot_path,
    )
    if not res.ok or not isinstance(res.json, dict) or not res.json.get("plan"):
        return res, None
    actions = tuple(Action.from_dict(item) for item in res.json["plan"])
    fam_fn = familiarity_fn or (lambda a: app_familiarity(a))
    fam = fam_fn(app)
    classification = classify_plan(actions, app_known=_app_known(app), familiar=fam.familiar)
    captured_at = str(device_state.get("captured_at") or _now())
    expires = (datetime.now().astimezone() + timedelta(seconds=approval_window_s)).isoformat(timespec="seconds")
    frozen = FrozenPlan(
        task=task,
        actions=actions,
        tier=classification.tier,
        reason=classification.reason,
        first_irreversible=classification.first_irreversible,
        app=app,
        device=str(device_state.get("device") or DEVICE),
        build=device_state.get("build"),
        frontmost_bundle=device_state.get("frontmost_bundle"),
        evidence_ref=str(shot) if shot else None,
        captured_at=captured_at,
        approval_expires_at=expires,
        actor=actor,
        familiarity={"familiar": fam.familiar, "reason": fam.reason, "evidence": fam.evidence},
    )
    return res, frozen


def _canceled(token: Any | None) -> bool:
    return bool(token is not None and getattr(token, "canceled", False))


@contextmanager
def acquire_screen_cancelable(
    lock: ScreenLock,
    *,
    device: str,
    run_id: str,
    cancel_token: Any | None = None,
    on_wait: Callable[[], None] | None = None,
    poll_s: float = 2.0,
) -> Iterator[None]:
    """Poll non-blocking lock acquisition so waiting remains cancellable."""
    while True:
        if _canceled(cancel_token):
            raise RuntimeError("interactive job canceled while waiting for ScreenLock")
        cm = lock.acquire(device=device, run_id=run_id, blocking=False)
        try:
            cm.__enter__()
        except ScreenLockBusy:
            if on_wait:
                on_wait()
            if cancel_token is not None and hasattr(cancel_token, "wait"):
                cancel_token.wait(poll_s)
            else:
                time.sleep(poll_s)
            continue
        try:
            yield
        finally:
            cm.__exit__(None, None, None)
        return


def _invoke_frozen_executor(
    task: str,
    plan: list[Action],
    run_id: str,
    *,
    provider: str | None,
    model: str | None,
    timeout_s: float,
    runner: CommandRunner,
) -> AiResult:
    """Invoke the device adapter for an already-frozen plan; caller holds ScreenLock."""
    steps = "\n".join(
        f"  {i + 1}. {action.verb} {action.target} — {action.intent}".rstrip()
        for i, action in enumerate(plan)
    )
    prompt = (
        f"任务：{task}\n\n严格按以下冻结清单在设备上执行，不得重规划、增加或替换动作：\n{steps}\n\n"
        "遇到清单外状态立即停止并报告；执行完简述结果。"
    )
    req = AiRequest(prompt=prompt, tools="device", provider=provider, model=model, timeout_s=timeout_s)
    return require_device_actions(invoke(req, runner=runner))


def execute_frozen_plan(
    frozen_plan: FrozenPlan,
    *,
    expected_digest: str,
    actor: str,
    consent: str,
    provider: str | None = None,
    model: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    runner: CommandRunner = _subprocess_runner,
    executor: ExecutorFn | None = None,
    lock: ScreenLock | None = None,
    state_probe: Callable[[str | None], dict[str, Any]] = capture_device_identity,
    cancel_token: Any | None = None,
    on_wait: Callable[[], None] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Validate and execute exactly ``frozen_plan``.  There is no planner call here."""
    if frozen_plan.digest != expected_digest:
        return {"ok": True, "outcome": "rejected", "code": "plan_digest_mismatch"}
    if frozen_plan.actor != actor:
        return {"ok": True, "outcome": "rejected", "code": "actor_mismatch"}
    if consent != "device_session":
        return {"ok": True, "outcome": "rejected", "code": "consent_required"}
    try:
        if datetime.fromisoformat(frozen_plan.approval_expires_at) <= datetime.now().astimezone():
            return {"ok": True, "outcome": "rejected", "code": "plan_expired"}
    except ValueError:
        return {"ok": True, "outcome": "rejected", "code": "invalid_plan_expiry"}

    if executor is None:
        # 默认执行器走 tools=device：控制面已配置且无 StageGuard 时，在取锁、读设备状态之前拒绝
        refused = unmanaged_device_refusal_result(
            AiRequest(prompt=frozen_plan.task, tools="device", provider=provider, model=model))
        if refused is not None:
            return {"ok": False, "outcome": "rejected", "code": refused.error_code,
                    "text": "", "error": refused.error, "provider": refused.provider,
                    "model": refused.model, "plan_digest": frozen_plan.digest}
    lock = lock or ScreenLock()
    run_id = run_id or f"agent-frozen-{_now()}"
    with acquire_screen_cancelable(
        lock,
        device=frozen_plan.device,
        run_id=run_id,
        cancel_token=cancel_token,
        on_wait=on_wait,
    ):
        current = state_probe(frozen_plan.app)
        anchors = ("device", "app", "build", "frontmost_bundle")
        frozen_state = {
            "device": frozen_plan.device,
            "app": frozen_plan.app,
            "build": frozen_plan.build,
            "frontmost_bundle": frozen_plan.frontmost_bundle,
        }
        if any(current.get(key) != frozen_state.get(key) for key in anchors):
            return {
                "ok": True,
                "outcome": "rejected",
                "code": "screen_state_changed",
                "expected": frozen_state,
                "actual": {key: current.get(key) for key in anchors},
            }
        if _canceled(cancel_token):
            raise RuntimeError("interactive job canceled before first device action")
        if executor is None:
            result = _invoke_frozen_executor(
                frozen_plan.task,
                list(frozen_plan.actions),
                run_id,
                provider=provider,
                model=model,
                timeout_s=timeout_s,
                runner=runner,
            )
        else:
            result = executor(frozen_plan.task, list(frozen_plan.actions), run_id)
    return {
        "ok": bool(result.ok),
        "outcome": "answered" if result.ok else "rejected",
        "text": result.text,
        "error": result.error,
        "provider": result.provider,
        "model": result.model,
        "plan_digest": frozen_plan.digest,
    }
def run_task(
    task: str,
    *,
    app: str | None = None,
    familiar: bool | None = None,
    approved: bool = False,
    provider: str | None = None,
    model: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    runner: CommandRunner = _subprocess_runner,
    screenshot: ScreenshotFn = _default_screenshot,
    shot_path: Path | None = None,
    executor: ExecutorFn | None = None,
    familiarity_fn: Callable[[str | None], "Familiarity"] | None = None,
) -> dict[str, Any]:
    """Lane 2 一次性任务。先只读规划 → host 侧按可逆性分级定门 → 自主跑/暂停待批。

    返回 `{ok, status, tier, plan, ...}`。`status`：
    - `executed`：autonomous（熟悉+纯可逆）或已 approved → 经 executor 真驱动（best-effort 结果在 `result`）。
    - `pending_confirmation`：需人 go（checkpoint/full_plan_approval 且未 approved）——附 `plan`/`gated_at`/
      `evidence`（截图），**绝不执行不可逆动作**。
    - `failed`：规划 fail-closed（agent 没回结构化 plan）。

    `approved=True` 表示人已就本 plan 批准（CLI 二次调用带 `--approved`），方放行执行。`familiar`：
    `None`（默认）→ 经 `familiarity.app_familiarity` **从证据派生**（当前构建的成功驱动史，见该模块）；显式 `True`/`False`
    则人工覆写。派生信号落进返回的 `familiarity` 字段供核对。`familiarity_fn` 是派生接缝（单测注入）。
    """
    if not (task or "").strip():
        return {"ok": False, "status": "failed", "error": "task 为空"}

    res, shot = _propose_plan(task, provider=provider, model=model, timeout_s=timeout_s,
                              runner=runner, screenshot=screenshot, shot_path=shot_path)
    if not res.ok or not isinstance(res.json, dict) or not res.json.get("plan"):
        return {"ok": False, "status": "failed",
                "error": res.error or "规划器没回结构化 plan（fail-closed，不盲动设备）"}

    # familiar=None → 派生（证据驱动，§ADR-0003 现算不落存）；显式值 → 人工覆写。
    fam: Familiarity | None = None
    if familiar is None:
        fam_fn = familiarity_fn or (lambda a: app_familiarity(a))
        fam = fam_fn(app)
        familiar_resolved = fam.familiar
    else:
        familiar_resolved = familiar
        fam = Familiarity(familiar, "人工覆写", {"override": True})

    plan = [Action.from_dict(d) for d in res.json["plan"]]
    cls = classify_plan(plan, app_known=_app_known(app), familiar=familiar_resolved)
    plan_out = [vars(a) for a in plan]
    familiarity_out = {"familiar": fam.familiar, "reason": fam.reason, "evidence": fam.evidence}

    # 门：需人批且未批 → 暂停，绝不执行。这条是 blast-radius 脊。
    if cls.tier in (CHECKPOINT, FULL_PLAN_APPROVAL) and not approved:
        return {
            "ok": True, "status": "pending_confirmation", "tier": cls.tier, "reason": cls.reason,
            "plan": plan_out, "gated_at": cls.first_irreversible,
            "evidence": str(shot) if shot else None,
            "rationale": res.json.get("rationale", ""),
            "familiarity": familiarity_out,
        }

    run_id = f"agent-{_now()}"
    if executor is not None:
        result = executor(task, plan, run_id)
    else:
        result = _default_executor(
            task, plan, run_id, approved=approved,
            provider=provider, model=model, timeout_s=timeout_s, runner=runner,
        )
    return {
        "ok": bool(result.ok), "status": "executed", "tier": cls.tier, "reason": cls.reason,
        "plan": plan_out, "result": result.text, "error": result.error,
        "provider": result.provider, "model": result.model,
        "familiarity": familiarity_out,
    }


# ─────────────────────────── Lane 3：读屏汇报 ───────────────────────────


def report(
    prompt: str,
    *,
    device: bool = False,
    provider: str | None = None,
    model: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    runner: CommandRunner = _subprocess_runner,
    screenshot: ScreenshotFn = _default_screenshot,
    shot_path: Path | None = None,
    lock: ScreenLock | None = None,
    device_name: str = DEVICE,
) -> AiResult:
    """Lane 3 读屏汇报：零副作用，回一段结构化摘要。`device=False` 喂当前屏纯推理；`device=True` 经
    vphone_mcp 给设备工具面（**只读约束靠 system 契约**，碰屏仍进 ScreenLock 串行）。"""
    if device:
        req = AiRequest(prompt=prompt, system=_REPORT_SYSTEM, tools="device",
                        provider=provider, model=model, timeout_s=timeout_s)
        refused = unmanaged_device_refusal_result(req)
        if refused is not None:  # 控制面已配置且无 StageGuard：取锁、起 CLI 之前拒绝
            return refused
        lock = lock or ScreenLock()
        with lock.acquire(device=device_name, run_id=f"report-{_now()}"):
            return invoke(req, runner=runner)

    dest = shot_path or (STATE_ROOT / "runs" / ".ai" / "report-shot.jpg")
    shot = screenshot(dest)
    req = AiRequest(prompt=prompt, system=_REPORT_SYSTEM, tools="none",
                    images=[shot] if shot else [], provider=provider, model=model, timeout_s=timeout_s)
    return invoke(req, runner=runner)
