"""``ScenarioController`` —— 接口场景深模块（docs §四~§八）。

对页面/runner 只暴露很小的接口；NSURLSession / hook 细节全收进 net_tap agent + backend。
启动拆成 **preflight → start** 两段，避免把运行态事实塞进启动前的计划：

    validate(scenario)                 -> [Diagnostic]        # 规则静态校验 + 环境闸
    preflight(scenario, mode, policy)  -> LaunchPlan          # 启动前可知
    start(scenario, launch_plan)       -> ScenarioSession     # 启动后事实
    status(session)                    -> health + hits       # 实时命中
    close(session)                     -> evidence            # 强清 + 校验（fail-closed）

**fail-closed 三闸（§八，绝不复用只读抓取的 fail-open）**：
  闸1 start 自检：agent ``scenarioHealth().interception_ready`` 不过 → start 失败、会话不成立
      （挡「agent 装了但 hook 没拦到目标网络栈」的静默失败）。由 ``session.apply_scenario`` 在
      on_loaded 时抛，spawn 会 kill 挂起 pid、attach 就地清理 attach（结构性 fail-closed）。
  闸2 规则编译：``validate_or_raise`` 有 error → 会话不成立。
  闸3 零命中：close 时无任一「已应用」注入事件（且有 assert_hit 规则）→ 判「场景未生效」= fail，非 skip。

backend 是 seam（``session.build_backend`` 生产实现走同一个 net_tap agent）；测试注入 fake，免 frida/VM。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from autophone.scenario.rules import (
    CompiledScenario,
    Diagnostic,
    ScenarioError,
    env_gate,
    validate,
    validate_or_raise,
)

logger = logging.getLogger(__name__)

# 挂载模型 → 副作用（§三）。
_RELAUNCH = {"spawn": True, "attach": False}

# 单设备单 App 单活动会话（§8.2 / §十二）：键必须同时含 device + app。
#
# 不能只靠「open 前先 close」：若两个 controller 同时读到注册表为空，都会 attach，随后才
# 各自登记，导致同一 pid 有两份短路 hook。每个 key 的 RLock 把 close-old → enter → register
# 收成一个原子临界区；不同设备/应用不互相串行。它是 controller 层的本地最后防线，调用方仍
# 应在实际碰屏工作流中持有同一 device 的 ScreenLock。
_ACTIVE_SESSIONS: "dict[tuple[str, str], ScenarioSession]" = {}
_SESSION_LOCKS: "dict[tuple[str, str], threading.RLock]" = {}
_SESSION_LOCKS_GUARD = threading.Lock()


class ScenarioStartError(RuntimeError):
    """start 前置失败（闸1 hook 自检不过 / backend 启动失败）。会话不成立。"""


@dataclass(frozen=True)
class HookHealth:
    """agent ``scenarioHealth()`` 的结构化投影（§六）。诚实上报，attach 比 spawn 弱一档。"""

    hook_installed: bool          # resume 短路点已装到活 NSURLSessionTask
    nsurlsession_class: bool      # 活 NSURLSession/Task 类可解
    block_runtime: bool           # 合成回调所需 Block ABI 原语在场
    interception_ready: bool      # ① 三者皆真 → 短路可用（闸1 判据）
    coverage_gap: bool            # ② attach 拦不到挂载前请求（物理约束）
    mode: str | None = None
    configured_rules: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "HookHealth":
        d = d or {}
        return cls(
            hook_installed=bool(d.get("hook_installed")),
            nsurlsession_class=bool(d.get("nsurlsession_class")),
            block_runtime=bool(d.get("block_runtime")),
            interception_ready=bool(d.get("interception_ready")),
            coverage_gap=bool(d.get("coverage_gap")),
            mode=d.get("mode"),
            configured_rules=int(d.get("configured_rules") or 0),
            raw=dict(d),
        )


@dataclass(frozen=True)
class LaunchPlan:
    """启动前可知（§4.1）。``target_pid`` 不在此——attach 的 pid 是 ready 之后才得到的。"""

    instrumentation_mode: str     # spawn | attach
    requires_relaunch: bool
    surface_policy: str           # preserve_only | recover_home（§五）
    startup_coverage: str         # 陈述事实，不给猜测性 bool（§4.1）
    rule_snapshot_hash: str       # 编译后 rules 的指纹（§6.1）
    scenario_id: str
    app: str
    env: str
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(d.is_error for d in self.diagnostics)

    def as_dict(self) -> dict[str, Any]:
        return {
            "instrumentation_mode": self.instrumentation_mode,
            "requires_relaunch": self.requires_relaunch,
            "surface_policy": self.surface_policy,
            "startup_coverage": self.startup_coverage,
            "rule_snapshot_hash": self.rule_snapshot_hash,
            "scenario_id": self.scenario_id,
            "app": self.app,
            "env": self.env,
            "blocked": self.blocked,
            "diagnostics": [
                {"level": d.level, "message": d.message, "rule_id": d.rule_id}
                for d in self.diagnostics
            ],
        }


@dataclass
class ScenarioSession:
    """启动后事实（§4.1）。start 返回、贯穿手工测试会话、close 收尾。"""

    session_id: str
    scenario_id: str
    device: str
    app: str
    instrumentation_mode: str
    rule_snapshot_hash: str
    hook_health: HookHealth
    surface_outcome: str          # preserved | reset | attached_unverified | unknown
    pid: int | None = None
    cleanup_verified: bool = False
    closed: bool = False
    _backend: Any = None
    _compiled: CompiledScenario | None = None


def scenario_hits(events: list[dict]) -> list[dict]:
    """从事件流抽出命中事件（§8.1，命中在拦截决策点落，不靠时间窗）。"""
    return [e for e in events if e.get("ev") == "scenario_hit"]


def applied_hits(events: list[dict]) -> list[dict]:
    """只算效果侧已确认的命中。

    completion-block 通路的 ``applied=true`` 沿用既有语义；delegate 通路的同一字段只表示
    「已短路并接受调度」，必须再看到同 rule/task/ordinal 的
    ``scenario_delivery:completed`` 才能算生效。这样 delegate 队列没执行或回调抛错时不会假绿。
    """
    completed = {
        _hit_key(e)
        for e in events
        if e.get("ev") == "scenario_delivery" and e.get("phase") == "completed"
    }
    out: list[dict] = []
    for hit in scenario_hits(events):
        if not hit.get("applied"):
            continue
        if (hit.get("controlled") is True or str(hit.get("via") or "").startswith("delegate")) and _hit_key(hit) not in completed:
            continue
        out.append(hit)
    return out


def _task_key(event: dict[str, Any]) -> tuple:
    if event.get("capture_id") or event.get("task_instance_id"):
        return ("instance", event.get("capture_id"), event.get("task_instance_id"))
    return ("legacy", str(event.get("task_ptr") or ""))


def _hit_key(event: dict[str, Any]) -> tuple:
    """Join sequenced evidence by capture/task instance; retain legacy pointer joins."""
    return (*_task_key(event), str(event.get("rule_id") or ""), int(event.get("ordinal") or 0))


def annotate_hits(merged: list[dict], events: list[dict]) -> list[dict]:
    """Attach scenario annotations without crossing task-instance boundaries."""
    confirmed = {_hit_key(h) for h in applied_hits(events)}
    by_task = {_task_key(h): h for h in scenario_hits(events)}
    for rec in merged:
        h = by_task.get(_task_key(rec))
        if h:
            rec["scenario"] = {
                "rule_id": h.get("rule_id"),
                "action": h.get("action"),
                "ordinal": h.get("ordinal"),
                "applied": bool(h.get("applied")),
                "delivery_confirmed": _hit_key(h) in confirmed,
                "reason": h.get("reason"),
            }
    return merged


class ScenarioController:
    """接口场景控制器。``backend_factory`` 是 seam：生产走 net_tap agent，测试注入 fake。"""

    def __init__(
        self,
        *,
        backend_factory: Callable[..., Any],
        prod_hosts: "set[str] | frozenset[str]" = frozenset(),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # backend_factory(compiled, *, mode, surface_policy, on_health) -> backend
        self._backend_factory = backend_factory
        self._prod_hosts = frozenset(prod_hosts)
        self._clock = clock

    # ── 校验（闸2 前置）───────────────────────────────────────────────────────────
    def validate(self, scenario: dict[str, Any]) -> list[Diagnostic]:
        """规则静态校验 + 环境闸，合并诊断（不抛，纯查）。"""
        diags = list(validate(scenario))
        seen = {(d.level, d.message, d.rule_id) for d in diags}
        for d in env_gate(scenario, prod_hosts=self._prod_hosts):
            if (d.level, d.message, d.rule_id) not in seen:
                diags.append(d)
        return diags

    # ── preflight：启动前可知（§4.1）────────────────────────────────────────────────
    def preflight(
        self, scenario: dict[str, Any], *, mode: str, surface_policy: str = "recover_home"
    ) -> LaunchPlan:
        mode = _norm_mode(mode)
        surface_policy = _norm_policy(surface_policy)
        diags = self.validate(scenario)
        # 编译一次拿指纹；编译失败（error）时 hash 缺省空串、diagnostics 已含 error → blocked。
        snapshot_hash = ""
        try:
            compiled = validate_or_raise(scenario, prod_hosts=self._prod_hosts)
            snapshot_hash = compiled.snapshot_hash
        except ScenarioError:
            pass  # 诊断里已有 error，LaunchPlan.blocked=True
        return LaunchPlan(
            instrumentation_mode=mode,
            requires_relaunch=_RELAUNCH.get(mode, False),
            surface_policy=surface_policy,
            startup_coverage=_coverage_text(mode),
            rule_snapshot_hash=snapshot_hash,
            scenario_id=str(scenario.get("id") or ""),
            app=str(scenario.get("app") or ""),
            env=str(scenario.get("env") or "").strip().lower(),
            diagnostics=diags,
        )

    # ── start：启动后事实（§4.1）+ 闸1/闸2 ─────────────────────────────────────────
    def start(
        self,
        scenario: dict[str, Any],
        launch_plan: LaunchPlan | None = None,
        *,
        mode: str | None = None,
        surface_policy: str = "recover_home",
        session_id: str | None = None,
        device: str = "vphone-0",
    ) -> ScenarioSession:
        mode = _norm_mode(mode or (launch_plan.instrumentation_mode if launch_plan else "attach"))
        surface_policy = _norm_policy(
            surface_policy if launch_plan is None else launch_plan.surface_policy
        )
        if launch_plan is not None and launch_plan.blocked:
            raise ScenarioStartError(
                "LaunchPlan 已阻断（规则/环境闸 error），拒绝启动：" + "; ".join(
                    d.message for d in launch_plan.diagnostics if d.is_error))

        # 闸2：编译 + 环境闸（有 error 抛 ScenarioError）。
        compiled = validate_or_raise(scenario, prod_hosts=self._prod_hosts)

        # §6.1 快照绑定（闸6）：LaunchPlan 是 preflight 确认过的不可变版本；start 时重新编译的
        # 规则必须与之逐位一致，否则「preflight 确认规则 A、拿同一 plan 启动改过的规则 B」会漏过。
        if launch_plan is not None:
            self._assert_plan_matches(launch_plan, compiled)

        device = _norm_device(device)
        key = (device, compiled.app)
        # §8.2：同一 device+app 的「close existing → attach → register」不可被另一 start 穿插。
        with _activity_lock(key):
            self._close_active(device, compiled.app)

            sid = session_id or f"sc-{uuid.uuid4().hex[:12]}"

            # backend.enter() 内在 on_loaded 时下发规则 + 自检 hook（闸1）；不过即抛，会话不成立。
            backend = self._backend_factory(
                compiled, mode=mode, surface_policy=surface_policy,
            )
            try:
                backend.enter()
            except ScenarioStartError:
                self._abort_start(backend)   # §8.2：enter 抛错时 script 可能已 load、hook 已装 → 强清+升级
                raise
            except Exception as exc:  # noqa: BLE001 backend 启动/attach/自检失败 → 会话不成立
                self._abort_start(backend)
                raise ScenarioStartError(f"接口场景启动失败（{mode}）：{exc}") from exc

            health = HookHealth.from_dict(getattr(backend, "health_raw", None))
            if not health.interception_ready:
                # 双保险：backend 未在 on_loaded 挡住时（如返回 not-ready 但没抛），这里兜底 fail-closed。
                # 与 close 同口径强清：teardown 未确认 → kill 升级（不再只 best-effort teardown 后忽略）。
                cleanup = _strict_teardown(backend)
                raise ScenarioStartError(
                    f"闸1 hook 自检未过（interception_ready=false）：{health.raw}；cleanup={cleanup}")

            logger.info(
                "接口场景已启动：session=%s device=%s scenario=%s mode=%s pid=%s hash=%s coverage_gap=%s rules=%s",
                sid, device, compiled.scenario_id, mode, getattr(backend, "pid", None),
                compiled.snapshot_hash, health.coverage_gap, len(compiled.rules),
            )
            session = ScenarioSession(
                session_id=sid,
                scenario_id=compiled.scenario_id,
                device=device,
                app=compiled.app,
                instrumentation_mode=mode,
                rule_snapshot_hash=compiled.snapshot_hash,
                hook_health=health,
                surface_outcome=str(getattr(backend, "surface_outcome", "unknown") or "unknown"),
                pid=getattr(backend, "pid", None),
                _backend=backend,
                _compiled=compiled,
            )
            _ACTIVE_SESSIONS[key] = session   # device+app 粒度登记（§8.2）
            return session

    @staticmethod
    def _assert_plan_matches(launch_plan: LaunchPlan, compiled: CompiledScenario) -> None:
        """闸6：start 的编译产物必须与 preflight 确认过的 LaunchPlan 逐位一致（§6.1）。"""
        mismatches: list[str] = []
        if launch_plan.rule_snapshot_hash and launch_plan.rule_snapshot_hash != compiled.snapshot_hash:
            mismatches.append(
                f"规则快照不一致（plan={launch_plan.rule_snapshot_hash} != 现编译={compiled.snapshot_hash}）")
        if launch_plan.scenario_id and launch_plan.scenario_id != compiled.scenario_id:
            mismatches.append(f"scenario_id 不一致（{launch_plan.scenario_id} != {compiled.scenario_id}）")
        if launch_plan.app and launch_plan.app != compiled.app:
            mismatches.append(f"app 不一致（{launch_plan.app} != {compiled.app}）")
        if launch_plan.env and launch_plan.env != compiled.env:
            mismatches.append(f"env 不一致（{launch_plan.env} != {compiled.env}）")
        if mismatches:
            raise ScenarioStartError(
                "LaunchPlan 与启动规则不符（preflight 确认后规则被改？两段式确认失效）：" + "；".join(mismatches))

    def _close_active(self, device: str, app: str) -> None:
        """open 前先 close 同一 device+App 的既有会话（调用方已持有该 key 的 activity lock）。"""
        key = (device, app)
        prior = _ACTIVE_SESSIONS.get(key)
        if prior is None or prior.closed:
            _ACTIVE_SESSIONS.pop(key, None)
            return
        logger.info("接口场景 open 前先 close 既有会话：device=%s app=%s prior_session=%s",
                    device, app, prior.session_id)
        try:
            self.close(prior)
        except Exception as exc:  # noqa: BLE001 旧会话清理异常不吞——记录，但不阻断（close 内已尽力强清+升级）
            logger.warning("关闭既有会话失败（device=%s app=%s session=%s）：%s",
                           device, app, prior.session_id, exc)
        finally:
            _ACTIVE_SESSIONS.pop(key, None)

    def _abort_start(self, backend: Any) -> dict[str, Any]:
        """start 失败（enter 抛错）后的强清 + kill 升级（§8.2，不吞异常）。

        attach 的 enter 抛错时 script 可能已 load（Interceptor.replace 已装到活网络栈），
        单靠 backend 的 best-effort 清理可能残留短路 hook。优先用 backend.abort()（attach 会
        读 __enter__ 就地清理结果、detach 未确认则 kill）；缺则退回与 close 同口径的 _strict_teardown。"""
        abort = getattr(backend, "abort", None)
        try:
            res = abort() if callable(abort) else _strict_teardown(backend)
        except Exception as exc:  # noqa: BLE001 清理本身抛 → 记为未确认，不静默
            logger.error("接口场景 start 失败清理异常：%s", exc)
            res = {"verified": False, "errors": [str(exc)]}
        if not (res or {}).get("verified"):
            logger.warning("接口场景 start 失败清理未确认（可能残留短路 hook）：%s", res)
        return res or {}

    # ── status：实时命中（横幅数据源，§八 闸4）─────────────────────────────────────
    def status(self, session: ScenarioSession) -> dict[str, Any]:
        events = list(getattr(session._backend, "events", []) or [])
        hits = applied_hits(events)
        unapplied = [h for h in scenario_hits(events) if not h.get("applied")]
        last = hits[-1] if hits else None
        return {
            "session_id": session.session_id,
            "scenario_id": session.scenario_id,
            "device": session.device,
            "mode": session.instrumentation_mode,
            "pid": session.pid,
            "hits": len(hits),
            "last_rule": last.get("rule_id") if last else None,
            "unapplied_matches": len(unapplied),   # 匹配但无 completion block（诚实暴露，非静默）
            "coverage_gap": session.hook_health.coverage_gap,
            "hook_health": session.hook_health.raw,
            "closed": session.closed,
        }

    # ── close：证据 + 强清 + 校验（闸3 + §8.2 不吞异常 + kill 升级）───────────────────
    def close(self, session: ScenarioSession) -> dict[str, Any]:
        # 与 start 同一 device+app 临界区：close 与新 start 不得交错 teardown/attach。
        with _activity_lock((session.device, session.app)):
            return self._close_locked(session)

    def _close_locked(self, session: ScenarioSession) -> dict[str, Any]:
        """在 ``_activity_lock((device, app))`` 内执行 close。"""
        backend = session._backend
        events = list(getattr(backend, "events", []) or [])
        merged = annotate_hits(backend.merged(), events)
        hits = applied_hits(events)
        unapplied = [h for h in scenario_hits(events) if not h.get("applied")]

        # 闸3：零命中兜底。有 assert_hit 规则却零「已应用」命中 → 场景未生效 = fail（非 skip）。
        compiled = session._compiled
        assert_rules = [r["rule_id"] for r in (compiled.rules if compiled else []) if r.get("assert_hit", True)]
        hit_rule_ids = {h.get("rule_id") for h in hits}
        missed = [rid for rid in assert_rules if rid not in hit_rule_ids]
        effective = not missed
        verdict = "effective" if effective else "not_effective"

        # §8.2 强清：teardown 逐项上报、不吞异常；未确认 → 升级 kill。
        cleanup = _strict_teardown(backend)
        session.cleanup_verified = bool(cleanup.get("verified"))
        session.closed = True
        key = (session.device, session.app)
        if _ACTIVE_SESSIONS.get(key) is session:
            _ACTIVE_SESSIONS.pop(key, None)   # 解除该 device+App 活动会话登记（§8.2）

        result = {
            "session_id": session.session_id,
            "scenario_id": session.scenario_id,
            "device": session.device,
            "verdict": verdict,               # effective | not_effective（闸3）
            "hits": len(hits),
            "hit_rules": sorted(hit_rule_ids - {None}),
            "missed_rules": missed,           # assert_hit 却零命中的规则（未生效原因）
            "unapplied_matches": len(unapplied),
            "cleanup_verified": session.cleanup_verified,
            "cleanup": cleanup,
            "rule_snapshot_hash": session.rule_snapshot_hash,
            "merged": merged,
            "scenario_hits": scenario_hits(events),
        }
        level = logger.info if effective and session.cleanup_verified else logger.warning
        level(
            "接口场景已关闭：session=%s verdict=%s hits=%s missed=%s cleanup_verified=%s",
            session.session_id, verdict, len(hits), missed, session.cleanup_verified,
        )
        return result


def _strict_teardown(backend: Any) -> dict[str, Any]:
    """teardown → 校验 → 未确认则 kill 升级（§8.2）。返回 {verified, escalated, ...}。

    detach 未确认时进程上可能还半活着、仍在短路请求：进程一杀 hook 必随之消失，比 detach 更硬。"""
    td = backend.teardown() or {}
    verified = bool(td.get("unloaded")) and bool(td.get("detached")) and not td.get("errors")
    out = {
        "unloaded": bool(td.get("unloaded")),
        "detached": bool(td.get("detached")),
        "errors": list(td.get("errors") or []),
        "escalated": False,
        "verified": verified,
    }
    if verified:
        probe = getattr(backend, "verify_recovery", None)
        if callable(probe):
            try:
                restoration = probe()
                out["restoration"] = (
                    restoration if isinstance(restoration, dict)
                    else {"status": "invalid", "verified": False}
                )
            except Exception as exc:  # noqa: BLE001 proof failure is evidence, not detach failure
                out["restoration"] = {
                    "status": "error", "verified": False, "error": str(exc),
                }
        return out
    # 升级到 kill：detach 没确认 → 强杀 App，确认 pid 消失即解除 device+app 闸。
    kill = getattr(backend, "kill", None)
    if callable(kill):
        try:
            killed = bool(kill())
            out["escalated"] = True
            out["killed"] = killed
            out["verified"] = killed
        except Exception as exc:  # noqa: BLE001 kill 也失败 → 保持未验证，挡下一场景
            out["kill_error"] = str(exc)
    return out


def _norm_mode(mode: str | None) -> str:
    m = (mode or "attach").strip().lower()
    return m if m in ("attach", "spawn") else "attach"


def _norm_policy(policy: str | None) -> str:
    p = (policy or "recover_home").strip().lower()
    # attach_here：手工第三态（跳过 OCR gate、仅 pid 校验就地挂载，§五）。
    return p if p in ("preserve_only", "recover_home", "attach_here") else "recover_home"


def _norm_device(device: str | None) -> str:
    """活动会话闸的设备键；空值不能退化成全局/未知键。"""
    d = str(device or "").strip()
    if not d:
        raise ScenarioStartError("device 必填：接口场景活动会话闸必须按 device+app 建立")
    return d


def _activity_lock(key: tuple[str, str]) -> threading.RLock:
    """返回稳定的 per-(device, app) 可重入锁，创建过程受短全局锁保护。"""
    with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _SESSION_LOCKS[key] = lock
        return lock


def _coverage_text(mode: str) -> str:
    if mode == "attach":
        return ("attach 仅覆盖挂载后请求；登录/冷启首屏接口在挂载前触发，物理上拦不到（§三）。"
                "某接口是否冷启专属除非有历史证据否则 unknown。")
    return "spawn 挂起态挂 hook，冷启请求零逃逸、fail-closed 为结构性（§3.1）。"
