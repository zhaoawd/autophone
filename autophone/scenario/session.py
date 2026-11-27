"""接口场景 backend —— 把编译规则接进 net_tap agent 的启动时序（§二/§六）。

**同一个 net_tap agent、同一次 attach/spawn**（不能起第二个 Frida session 去 attach 已挂 tap
的进程——会撞 probing dyld）。启动时序「规则先于 resume」靠 capture 会话的 ``on_loaded`` seam：

  - attach：``load → configureScenario → scenarioHealth → 返回「拦截已就绪」``（health 诚实带覆盖缺口）
  - spawn ：``load → configureScenario → scenarioHealth → resume``（health 不过 → kill 挂起 pid）

``apply_scenario`` 在 on_loaded 里下发规则 + 自检 hook；``interception_ready`` 不过即抛
``ScenarioStartError``（闸1）——attach 就地清理 attach、spawn 永不 resume 直接 kill。

本模块含活 frida 的生产实现；controller 只依赖抽象 backend（seam），单测注入 fake、免 frida/VM。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

from autophone.scenario.controller import ScenarioStartError
from autophone.scenario.rules import CompiledScenario
from autophone.device.frida_endpoint import resolve_frida_host

logger = logging.getLogger(__name__)


def _resolve_export(exports: Any, *names: str) -> Callable[..., Any]:
    """取 rpc export（frida 把 JS camelCase 转 snake_case；两种都试一遍）。缺则抛。"""
    for n in names:
        fn = getattr(exports, n, None)
        if callable(fn):
            return fn
    raise ScenarioStartError(f"net_tap agent 缺 rpc export: {names!r}（agent bundle 太旧？重编）")


def _surface_outcome(ready_result: dict[str, Any]) -> str:
    """从 ready gate 结果推 surface_outcome（§五）。纯函数，便于单测。

    attach_here（就地挂载）> preserved（当前页已就绪保留）> no-gate（未知）> reset（复位过）。"""
    r = ready_result or {}
    if r.get("attach_here"):
        return "attached_unverified"
    if r.get("preserved_visible_surface"):
        return "preserved"
    if r.get("reason") == "no-gate":
        return "unknown"
    return "reset"


def build_ready_fn(
    surface_policy: str, shot_dir: "Path | None", profile: Any
) -> "Callable[[], dict] | None":
    """按 surface_policy 选 ready gate（§五）——修「三种策略都绑同一个 _reset_then_wait_ready」。

    - ``attach_here``：跳过 OCR gate，仅 pid 校验就地挂载（人比 OCR 更强的 ready 判据）；
    - ``preserve_only``：当前页就绪则保留，**未就绪不自动复位**（ok=false，交测试者决策）；
    - ``recover_home``：保留失败则 reset_to_home 再等待（自动化回归默认）。"""
    if surface_policy == "attach_here":
        return lambda: {"ok": True, "reason": "attach-here", "attach_here": True}
    if shot_dir is None:
        return None   # 无 shot_dir（退化/测试路径）：AttachSession 走 no-gate
    from autophone.observation import capture as capture_mod
    import functools

    if surface_policy == "preserve_only":
        return functools.partial(capture_mod._wait_ready_preserve_only, shot_dir, profile)
    return functools.partial(capture_mod._reset_then_wait_ready, shot_dir, profile)


def apply_scenario(exports: Any, compiled: CompiledScenario, mode: str) -> dict[str, Any]:
    """on_loaded 回调：下发编译规则 + 自检 hook。闸1 不过抛 ScenarioStartError。

    返回 agent ``scenarioHealth()`` 原始 dict（backend 存进 ``health_raw`` 供 controller 投影）。"""
    configure = _resolve_export(exports, "configure_scenario", "configureScenario")
    health_fn = _resolve_export(exports, "scenario_health", "scenarioHealth")
    configure(compiled.payload(mode))                 # 规则先于 resume（§六）
    health = health_fn() or {}
    if not health.get("interception_ready"):
        # 闸1：hook 没装到活网络栈 / Block 原语缺 → 会话不成立。抛出后：
        # attach → AttachSession.__enter__ 就地 detach+unload；spawn → spawn_gate finally kill 挂起 pid。
        raise ScenarioStartError(
            f"闸1 hook 自检未过（interception_ready=false）：{health}")
    return health


class _AttachBackend:
    """attach 模式 backend：uiopen + attach 前台 pid + on_loaded 配规则/自检（§三 attach 列）。"""

    def __init__(
        self,
        compiled: CompiledScenario,
        *,
        frida_host: str,
        agent_js: str,
        bundle_id: str,
        ready_fn: Callable[[], dict] | None,
        surface_policy: str,
        kill_fn: Callable[[], bool] | None = None,
        recovery_probe: Callable[[CompiledScenario], dict[str, Any]] | None = None,
    ) -> None:
        self._compiled = compiled
        self._surface_policy = surface_policy
        self._kill_fn = kill_fn
        self._recovery_probe = recovery_probe
        self._ready_result: dict = {}
        self.health_raw: dict[str, Any] = {}
        self.surface_outcome = "unknown"
        self.pid: int | None = None

        from autophone.observation.frida.capture_session import AttachSession

        def _ready() -> dict:
            self._ready_result = (ready_fn() if ready_fn else {"ok": True, "reason": "no-gate"}) or {}
            return self._ready_result

        self._session = AttachSession(
            frida_host, bundle_id, agent_js,
            ready_fn=_ready,
            on_loaded=lambda ex: self.health_raw.update(
                apply_scenario(ex, compiled, "attach")),
            install_capture=False,  # 拦截短路不需要 delegate 抓取 hook（省全类枚举）
        )

    @property
    def events(self) -> list[dict]:
        return self._session.events

    def enter(self) -> None:
        try:
            self._session.__enter__()
        except Exception:
            # §8.2 闸1/attach 启动失败：__enter__ 里 script 可能已 load（Interceptor.replace 已装）。
            # AttachSession.__enter__ 失败时已就地清理并把结果记进 enter_cleanup；detach 未确认 → kill 升级，
            # 不让短路 hook 半死不活地残留在 App 里（这是 review Finding 1 的核心缺口）。
            cleanup = getattr(self._session, "enter_cleanup", None) or {}
            if cleanup.get("errors"):
                try:
                    self.kill()
                except Exception:  # noqa: BLE001 kill 也失败 → 交上层判未确认，不吞
                    logger.warning("attach 启动失败后 kill 升级也失败（app=%s）", self._compiled.app)
            raise
        self.pid = self._session.pid
        self.surface_outcome = _surface_outcome(self._ready_result)

    def merged(self) -> list[dict]:
        return self._session.merged()

    def teardown(self) -> dict[str, Any]:
        return self._session.teardown()          # 逐项上报、不吞异常（§8.2）

    def verify_recovery(self) -> dict[str, Any]:
        """Run a trusted post-detach probe, if the app integration registered one.

        A successful Frida detach only proves mechanical cleanup.  This probe must
        trigger the same endpoint after detach and positively identify a real
        upstream response; without it the result remains explicitly unobserved.
        """
        if self._recovery_probe is None:
            return {"status": "unconfigured", "verified": False}
        result = self._recovery_probe(self._compiled)
        if not isinstance(result, dict):
            raise RuntimeError("detach recovery probe 必须返回对象")
        return {**result, "verified": result.get("verified") is True}

    def abort(self) -> dict[str, Any]:
        """start 失败后的清理判定（controller._abort_start 调）：读 __enter__ 就地清理结果，
        detach 未确认则升级 kill（§8.2）。返回 {verified, escalated, ...}。"""
        cleanup = getattr(self._session, "enter_cleanup", None) or {}
        errs = list(cleanup.get("errors") or [])
        out: dict[str, Any] = {
            "unloaded": bool(cleanup.get("unloaded", True)),
            "detached": bool(cleanup.get("detached", True)),
            "errors": errs,
            "escalated": False,
            "verified": not errs,
        }
        if errs:   # detach/unload 未确认 → kill 升级
            try:
                killed = self.kill()
            except Exception as exc:  # noqa: BLE001
                out["kill_error"] = str(exc)
                killed = False
            out["escalated"] = True
            out["killed"] = killed
            out["verified"] = killed
        return out

    def kill(self) -> bool:
        if self._kill_fn:
            return bool(self._kill_fn())
        return _default_kill(self._compiled.app, self.pid)


class _SpawnBackend:
    """spawn 模式 backend：spawn-gate + on_loaded 在 resume 前配规则/自检（§三 spawn 列、§3.1）。"""

    def __init__(
        self,
        compiled: CompiledScenario,
        *,
        frida_host: str,
        agent_js: str,
        bundle_id: str,
        warmup: float = 12.0,
    ) -> None:
        self._compiled = compiled
        self.health_raw: dict[str, Any] = {}
        self.surface_outcome = "reset"           # spawn 冷启从首屏起
        self.pid: int | None = None

        from autophone.observation.frida.capture_session import CaptureSession

        self._session = CaptureSession(
            frida_host, bundle_id, agent_js, warmup=warmup,
            on_loaded=lambda ex: self.health_raw.update(
                apply_scenario(ex, compiled, "spawn")),
            install_capture=False,  # 拦截短路不需要 delegate 抓取 hook；且挂起态 spawn 装它会崩
        )

    @property
    def events(self) -> list[dict]:
        return self._session.events

    def enter(self) -> None:
        self._session.__enter__()

    def merged(self) -> list[dict]:
        return self._session.merged()

    def teardown(self) -> dict[str, Any]:
        # spawn-gate 退出即 detach + kill 挂起进程（结构性）；best-effort 上报。
        try:
            self._session.__exit__(None, None, None)
            return {"unloaded": True, "detached": True, "errors": []}
        except Exception as exc:  # noqa: BLE001
            return {"unloaded": False, "detached": False, "errors": [str(exc)]}

    def abort(self) -> dict[str, Any]:
        # spawn 启动失败是结构性 fail-closed：spawn_gate 的 finally 已 detach+kill 那个从未 resume
        # 的挂起 pid（objc_bundle_probe.spawn_gate），无残留短路 hook。这里仅诚实上报。
        return {"verified": True, "escalated": False, "structural": True, "errors": []}

    def kill(self) -> bool:
        return _default_kill(self._compiled.app, self.pid)


def _default_kill(app: str, pid: int | None) -> bool:
    """强杀被测 App（UDS app_terminate），确认 pid 消失。进程一杀 hook 必随之消失（§8.2）。"""
    try:
        from autophone.assets.apps import load_app_profile
        from autophone.device.uds import request

        bundle_id = load_app_profile(app).bundle_id
        request({"t": "app_terminate", "bundle_id": bundle_id}, timeout=8)
        resp, _ = request({"t": "app_list", "filter": "running"}, timeout=5)
        for a in resp.get("apps", []):
            if a.get("bundle_id") == bundle_id and int(a.get("pid") or 0) > 0:
                return False   # 还在跑 → kill 未确认
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("接口场景 kill 升级失败：%s", exc)
        return False


def make_backend_factory(
    *,
    profile: Any,
    shot_dir: Path | None = None,
    frida_host: str | None = None,
    agent_js: str | None = None,
    recovery_probe_factory: Callable[[CompiledScenario], Callable[[CompiledScenario], dict[str, Any]] | None] | None = None,
) -> Callable[..., Any]:
    """构造 controller 用的 backend_factory（生产：走 net_tap agent）。

    绑定 profile/shot_dir/frida_host/agent_js；返回的工厂签名对齐 ScenarioController：
    ``factory(compiled, *, mode, surface_policy) -> backend``。"""
    from autophone.observation.frida import net_capture

    host = resolve_frida_host(frida_host)
    agent = agent_js or str(net_capture.AGENT_JS)
    bundle_id = str(profile.bundle_id)

    def factory(compiled: CompiledScenario, *, mode: str, surface_policy: str) -> Any:
        if mode == "spawn":
            return _SpawnBackend(compiled, frida_host=host, agent_js=agent, bundle_id=bundle_id)
        # §五：ready gate 按 surface_policy 选，不再三种策略共用同一个复位函数。
        ready_fn = build_ready_fn(surface_policy, shot_dir, profile)
        return _AttachBackend(
            compiled, frida_host=host, agent_js=agent, bundle_id=bundle_id,
            ready_fn=ready_fn, surface_policy=surface_policy,
            recovery_probe=(recovery_probe_factory(compiled) if recovery_probe_factory else None),
        )

    return factory
