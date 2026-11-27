"""接口场景控制面的长期会话 module。

HTTP 调用是离散的，但 Frida session 与 ``ScreenLock`` 必须从 ``start`` 一直活到
``close``/TTL。``ScenarioRuntime`` 用一个 owner thread 隐藏这段生命周期；调用方只学习
``preflight/start/status/close``，所有 controller 调用都回到同一 thread 执行。
"""
from __future__ import annotations

import copy
import contextlib
import logging
import math
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from autophone.device.lock import ScreenLock, ScreenLockBusy
from autophone.scenario.controller import LaunchPlan, ScenarioController
from autophone.scenario.interface import PreparedScenario, prepare

logger = logging.getLogger(__name__)
SCENARIO_COMMAND_TIMEOUT_S = 60.0


class ScenarioRuntimeError(RuntimeError):
    """控制面请求无法安全完成。"""


class ScenarioPlanNotFound(ScenarioRuntimeError):
    """plan 不存在、过期或已被 start 消费。"""


class ScenarioSessionNotFound(ScenarioRuntimeError):
    """session 不存在。"""


class ScenarioDeviceBusy(ScenarioRuntimeError):
    """设备 ScreenLock 已被其他工作流占用。"""


@dataclass
class _PlanRecord:
    scenario: dict[str, Any]
    plan: LaunchPlan
    prepared: PreparedScenario
    device: str
    created_at: float
    evidence: dict[str, Any]


@dataclass
class _Command:
    name: str
    reply: "queue.Queue[tuple[bool, Any]]"


@dataclass
class _Worker:
    session_id: str
    commands: "queue.Queue[_Command]"
    ready: "queue.Queue[tuple[bool, Any]]"
    cancel: threading.Event
    thread: threading.Thread
    run_id: str | None = None


class ScenarioRuntime:
    """把两段式 controller 变成可跨 HTTP 请求持有的临时会话。"""

    def __init__(
        self,
        *,
        controller_factory: Callable[[str], ScenarioController],
        lock: ScreenLock,
        clock: Callable[[], float] = time.monotonic,
        max_ttl_s: float = 900.0,
        plan_ttl_s: float = 300.0,
        startup_timeout_s: float = 60.0,
        command_timeout_s: float = SCENARIO_COMMAND_TIMEOUT_S,
        max_terminal_records: int = 256,
        evidence_store: Any | None = None,
        session_context_factory: Callable[
            [str, str, dict[str, Any]], Any
        ] | None = None,
        observation_context_factory: Callable[
            [str, str, str, dict[str, Any], dict[str, Any]], Any
        ] | None = None,
    ) -> None:
        self._controller_factory = controller_factory
        self._lock = lock
        self._clock = clock
        self._max_ttl_s = max_ttl_s
        self._plan_ttl_s = plan_ttl_s
        self._startup_timeout_s = startup_timeout_s
        self._command_timeout_s = command_timeout_s
        self._max_terminal_records = max(1, int(max_terminal_records))
        self._evidence_store = evidence_store
        self._session_context_factory = session_context_factory or (
            lambda _session_id, _app, _metadata: contextlib.nullcontext({"active": False})
        )
        self._observation_context_factory = observation_context_factory or (
            lambda _session_id, _app, _env, _scenario, _metadata:
                contextlib.nullcontext({
                    "upstream": {"status": "unconfigured", "independent": False,
                                 "verified_zero_touch": False},
                    "_finish": lambda: None,
                })
        )
        self._plans: dict[str, _PlanRecord] = {}
        self._workers: dict[str, _Worker] = {}
        self._terminal: dict[str, dict[str, Any]] = {}
        self._guard = threading.RLock()

    def preflight(
        self,
        app: str,
        scenario: dict[str, Any],
        *,
        mode: str,
        surface_policy: str = "recover_home",
        device: str = "vphone-0",
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """校验并冻结 plan；start 只接收 ``plan_id``，不能换 scenario 偷渡。"""
        if str(scenario.get("app") or "") != app:
            raise ScenarioRuntimeError(
                f"route app={app!r} 与 scenario.app={scenario.get('app')!r} 不一致"
            )
        controller = self._controller_factory(app)
        prepared = prepare(scenario, controller=controller, mode=mode,
                           surface_policy=surface_policy, device=str(device or "vphone-0"))
        plan = prepared.plan
        plan_id = f"sp-{uuid.uuid4().hex[:12]}"
        if not plan.blocked:
            with self._guard:
                self._purge_plans_locked()
                self._plans[plan_id] = _PlanRecord(
                    scenario=copy.deepcopy(scenario),
                    plan=plan,
                    prepared=prepared,
                    device=str(device or "vphone-0"),
                    created_at=self._clock(),
                    evidence=copy.deepcopy(evidence or {}),
                )
        out = plan.as_dict()
        out.update({
            "plan_id": None if plan.blocked else plan_id,
            "device": str(device or "vphone-0"),
        })
        return out

    def start(self, plan_id: str, *, ttl_s: float = 120.0) -> dict[str, Any]:
        """消费一次性 plan，owner thread 非阻塞取得 ScreenLock 后启动 session。"""
        try:
            ttl = float(ttl_s)
        except (TypeError, ValueError) as exc:
            raise ScenarioRuntimeError("ttl_s 必须是数字") from exc
        if not math.isfinite(ttl):
            raise ScenarioRuntimeError("ttl_s 必须是有限数字")
        if ttl <= 0 or ttl > self._max_ttl_s:
            raise ScenarioRuntimeError(f"ttl_s 必须在 (0, {self._max_ttl_s:g}] 内")
        with self._guard:
            self._purge_plans_locked()
            record = self._plans.pop(plan_id, None)
        if record is None:
            raise ScenarioPlanNotFound(f"scenario plan 不存在/已消费/已过期: {plan_id}")

        session_id = f"sc-{uuid.uuid4().hex[:12]}"
        commands: "queue.Queue[_Command]" = queue.Queue()
        ready: "queue.Queue[tuple[bool, Any]]" = queue.Queue(maxsize=1)
        cancel = threading.Event()
        worker = _Worker(
            session_id=session_id,
            commands=commands,
            ready=ready,
            cancel=cancel,
            thread=threading.Thread(
                target=self._worker_main,
                args=(session_id, record, ttl, commands, ready, cancel),
                name=f"scenario-{session_id}",
                daemon=True,
            ),
        )
        with self._guard:
            self._workers[session_id] = worker
        worker.thread.start()
        try:
            ok, value = ready.get(timeout=self._startup_timeout_s)
        except queue.Empty as exc:
            # 不跨线程强拆 Frida；owner thread 在 start 返回的第一刻看到 cancel 并立即 close。
            cancel.set()
            raise ScenarioRuntimeError(
                f"scenario start 超时（session={session_id}），owner thread 将在启动返回后立即清理"
            ) from exc
        if not ok:
            if isinstance(value, ScenarioRuntimeError):
                raise value
            raise ScenarioRuntimeError(str(value))
        return {**value, "plan_id": plan_id, "ttl_s": ttl}

    def status(self, session_id: str) -> dict[str, Any]:
        with self._guard:
            terminal = self._terminal.get(session_id)
            worker = self._workers.get(session_id)
        if terminal is not None:
            return copy.deepcopy(terminal)
        if worker is None:
            raise ScenarioSessionNotFound(f"scenario session 不存在: {session_id}")
        out = self._ask(worker, "status")
        if worker.run_id:
            out["run_id"] = worker.run_id
        return out

    def close(self, session_id: str) -> dict[str, Any]:
        """幂等 close；返回时 controller 清理与 ScreenLock 释放均已完成。"""
        with self._guard:
            terminal = self._terminal.get(session_id)
            worker = self._workers.get(session_id)
        if terminal is not None:
            return copy.deepcopy(terminal)
        if worker is None:
            raise ScenarioSessionNotFound(f"scenario session 不存在: {session_id}")
        return self._ask(worker, "close")

    def shutdown(self) -> None:
        with self._guard:
            session_ids = list(self._workers)
        for session_id in session_ids:
            try:
                self.close(session_id)
            except ScenarioRuntimeError:
                pass

    def _ask(self, worker: _Worker, name: str) -> dict[str, Any]:
        reply: "queue.Queue[tuple[bool, Any]]" = queue.Queue(maxsize=1)
        worker.commands.put(_Command(name=name, reply=reply))
        try:
            ok, value = reply.get(timeout=self._command_timeout_s)
        except queue.Empty as exc:
            raise ScenarioRuntimeError(
                f"scenario {name} 超时: {worker.session_id}"
            ) from exc
        if not ok:
            raise ScenarioRuntimeError(str(value))
        return copy.deepcopy(value)

    def _worker_main(
        self,
        session_id: str,
        record: _PlanRecord,
        ttl_s: float,
        commands: "queue.Queue[_Command]",
        ready: "queue.Queue[tuple[bool, Any]]",
        cancel: threading.Event,
    ) -> None:
        terminal: dict[str, Any] | None = None
        evidence_started = False
        pending_close: "queue.Queue[tuple[bool, Any]] | None" = None
        ready_sent = False
        fixture_state: dict[str, Any] | None = None
        observation_state: dict[str, Any] | None = None
        try:
            try:
                lock_context = self._lock.acquire(
                    device=record.device,
                    run_id=session_id,
                    blocking=False,
                )
                with contextlib.ExitStack() as stack:
                    stack.enter_context(lock_context)
                    fixture_state = stack.enter_context(
                        self._session_context_factory(
                            session_id,
                            record.plan.app,
                            copy.deepcopy(record.evidence),
                        )
                    )
                    observation_state = stack.enter_context(
                        self._observation_context_factory(
                            session_id,
                            record.plan.app,
                            record.plan.env,
                            copy.deepcopy(record.scenario),
                            copy.deepcopy(record.evidence),
                        )
                    )
                    record.prepared.start(session_id=session_id)
                    if cancel.is_set():
                        self._finish_observation(observation_state)
                        close_result = record.prepared.close()
                        terminal = self._terminal_result(
                            session_id, "startup_timeout", close_result
                        )
                        ready.put((False, ScenarioRuntimeError(
                            f"scenario start 已超时并清理: {session_id}"
                        )))
                        ready_sent = True
                        return
                    started = record.prepared.status()
                    started.update({"session_id": session_id, "device": record.device})
                    if self._evidence_store is not None:
                        try:
                            evidence_run = self._evidence_store.begin(
                                session_id=session_id,
                                app=record.plan.app,
                                env=record.plan.env,
                                device=record.device,
                                scenario_id=record.plan.scenario_id,
                                rule_snapshot_hash=record.plan.rule_snapshot_hash,
                                ttl_s=ttl_s,
                                metadata=record.evidence,
                            )
                            worker = self._workers.get(session_id)
                            if worker is not None:
                                worker.run_id = str(evidence_run.get("run_id") or "") or None
                            if worker is not None and worker.run_id:
                                started["run_id"] = worker.run_id
                            evidence_started = True
                        except Exception as exc:  # noqa: BLE001 evidence 不得破坏已成立的安全会话
                            started["evidence_error"] = str(exc)
                            logger.exception("scenario run 证据建档失败 session=%s", session_id)
                    ready.put((True, started))
                    ready_sent = True
                    deadline = self._clock() + ttl_s
                    while True:
                        remaining = max(0.0, deadline - self._clock())
                        try:
                            command = commands.get(timeout=remaining)
                        except queue.Empty:
                            self._finish_observation(observation_state)
                            close_result = record.prepared.close()
                            terminal = self._terminal_result(
                                session_id, "ttl", close_result
                            )
                            break
                        if command.name == "status":
                            try:
                                command.reply.put((True, record.prepared.status()))
                            except Exception as exc:  # noqa: BLE001 status 失败不应擅自结束活动会话
                                command.reply.put((False, exc))
                            continue
                        if command.name == "close":
                            pending_close = command.reply
                            self._finish_observation(observation_state)
                            close_result = record.prepared.close()
                            terminal = self._terminal_result(
                                session_id, "explicit", close_result
                            )
                            break
                        command.reply.put((False, f"未知 scenario command: {command.name}"))
            except ScreenLockBusy as exc:
                raise ScenarioDeviceBusy(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - owner thread 必须把错误交回调用方并收口状态
            terminal = {
                "session_id": session_id,
                "closed": True,
                "close_reason": "start_error" if not ready_sent else "runtime_error",
                "error": str(exc),
            }
            if not ready_sent:
                ready.put((False, exc))
                ready_sent = True
            if pending_close is not None:
                pending_close.put((False, exc))
                pending_close = None
        finally:
            terminal = terminal or {
                "session_id": session_id,
                "closed": True,
                "close_reason": "owner_exit",
            }
            if fixture_state and fixture_state.get("active"):
                snapshot = copy.deepcopy(fixture_state)
                terminal["fixture"] = snapshot
                close_payload = terminal.get("close")
                if isinstance(close_payload, dict):
                    cleanup = close_payload.setdefault("cleanup", {})
                    if isinstance(cleanup, dict):
                        cleanup["fixture"] = snapshot
                    if snapshot.get("verified") is not True:
                        close_payload["cleanup_verified"] = False
                if snapshot.get("verified") is not True:
                    terminal["cleanup_verified"] = False
                    terminal.setdefault(
                        "error",
                        str(snapshot.get("error") or "scenario fixture 恢复未确认"),
                    )
            if observation_state:
                terminal["observations"] = copy.deepcopy({
                    key: value for key, value in observation_state.items()
                    if not key.startswith("_")
                })
            if evidence_started and self._evidence_store is not None:
                try:
                    evidence_result = self._evidence_store.finish(session_id, terminal)
                    terminal["run_id"] = evidence_result.get("run_id")
                    terminal["run_status"] = evidence_result.get("status")
                except Exception as exc:  # noqa: BLE001 cleanup 已完成，证据失败需诚实上报但不反向卡锁
                    terminal["evidence_error"] = str(exc)
                    logger.exception("scenario run 证据收口失败 session=%s", session_id)
            with self._guard:
                self._workers.pop(session_id, None)
                self._terminal[session_id] = terminal
                while len(self._terminal) > self._max_terminal_records:
                    self._terminal.pop(next(iter(self._terminal)))
            # 必须在 ScreenLock context 退出后才应答 close，保证 close 返回即设备锁已释放。
            if pending_close is not None:
                pending_close.put((True, terminal))

    @staticmethod
    def _finish_observation(state: dict[str, Any] | None) -> None:
        """End the upstream window before detach/recovery can create real traffic."""
        if not state:
            return
        finish = state.get("_finish")
        if callable(finish):
            finish()

    @staticmethod
    def _terminal_result(
        session_id: str, reason: str, close_result: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "closed": True,
            "close_reason": reason,
            "verdict": close_result.get("verdict"),
            "cleanup_verified": close_result.get("cleanup_verified"),
            "close": close_result,
        }

    def _purge_plans_locked(self) -> None:
        cutoff = self._clock() - self._plan_ttl_s
        for plan_id, record in list(self._plans.items()):
            if record.created_at < cutoff:
                self._plans.pop(plan_id, None)


def production_controller(app_name: str) -> ScenarioController:
    """生产 adapter：当前 App profile + net_tap backend。"""
    from autophone.assets.apps import load_app_profile
    from autophone.scenario.recovery import external_recovery_probe_factory
    from autophone.scenario.session import make_backend_factory

    profile = load_app_profile(app_name)
    return ScenarioController(backend_factory=make_backend_factory(
        profile=profile,
        recovery_probe_factory=external_recovery_probe_factory,
    ))
