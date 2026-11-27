#!/usr/bin/env python3
# coding=utf8
"""可复用抓取会话（host 侧）：spawn-gate net_tap.js → 整场存活收事件 → merge。

把 run_with_capture.main 里验证过的「spawn-gate + 暖机 + 事件收集 + 合并」生命周期抽成
一个上下文管理器，供 platform runner（`autophone.observation.capture`）在跑回归用例时复用：

    with CaptureSession(frida_host, bundle_id, agent_js, warmup=12) as sess:
        run(..., capture=sess.handle)        # UDS 驱动同一被测实例，自动 api_mark
    merged = sess.merged()                   # 退出后合并 req/resp → §五 schema 单条/行

frida 只在 __enter__ 里懒 import——本模块可在无 frida 环境 import（platform 层 fail-open 探测靠这点）。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from threading import Event, Lock
from functools import wraps

from autophone.device.stage_guard import StageGuard, OperationClass
from autophone.device.stage_threads import start_device_thread
from autophone.device.frida_lifetime import FridaLifetime
from typing import Any, Callable

from autophone.observation.frida import net_capture
from autophone.observation.frida.agent_bundle import _bundle_is_fresh
from autophone.observation.frida.objc_bundle_probe import _exports, spawn_gate

logger = logging.getLogger(__name__)
ATTACH_TRANSIENT_RETRY_S = 0.5
_TRANSIENT_ATTACH_ERRORS = ("script has been destroyed", "the connection is closed")


def _stage_capture(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        lifetime = getattr(self, "_lifetime", None)
        if method.__name__ in {"__exit__", "teardown"} and lifetime is not None and lifetime.closed:
            return method(self, *args, **kwargs)
        guard = self._stage_guard or StageGuard.for_device_call()
        if guard is None:
            return method(self, *args, **kwargs)
        with guard.operation(OperationClass.FRIDA, frida_endpoint=self.frida_host):
            self._stage_guard = guard
            return method(self, *args, **kwargs)
    return call


class CaptureAttachError(RuntimeError):
    """uiopen+attach 启动模型的前置失败（uiopen 失败 / 界面未就绪 / 找不到 pid / attach 失败）。

    __enter__ 抛它即代表「用例尚未跑」——service 的 fail-open 会据此安全退回普通启动重跑。"""


def _assert_agent_bundle_fresh(agent_js: str) -> Path:
    """Reject a stale compiled Frida agent before it can yield a false API result.

    Bundles are intentionally gitignored.  Under the standard layout,
    ``agent/_build/<name>.js`` is generated from ``agent/src/<name>.js``.
    Other caller-provided paths retain their existing load behavior.
    """
    from autophone.observation.frida.agent_bundle import _managed_bundle_paths, ensure_agent_bundle

    requested = Path(agent_js)
    bundle = (
        ensure_agent_bundle(requested)
        if _managed_bundle_paths(requested) is not None
        else requested
    )
    if bundle.parent.name != "_build":
        return bundle
    source = bundle.parent.parent / "src" / bundle.name
    if not source.is_file():
        return bundle
    if not _bundle_is_fresh(bundle, source):
        raise CaptureAttachError(
            f"agent bundle stale: {bundle}; rebuild with "
            "`cd autophone/observation/frida/agent && "
            "FRIDA_COMPILE=../../../.venv/bin/frida-compile npm run build:net`"
        )
    return bundle


class CaptureHandle:
    """注入 CaseContext.capture 的稳定句柄：包住 frida rpc，并可在 App 冷启后重绑。

    mark 失败吞掉——抓取链路绝不拖垮被测用例（§六：抓取与 UI 解耦）。"""

    def __init__(
        self,
        exports: Any,
        events: list[dict] | None = None,
        *,
        reopen_fn: Callable[[], None] | None = None,
        identity_path: Path | None = None,
        run_id: str | None = None,
        app: str | None = None,
        pid: int | None = None,
        mark_timeout_s: float = 1.0,
        frida_host: str | None = None,
    ) -> None:
        self._stage_guard = StageGuard.current()
        self.frida_host = frida_host
        self._ex = exports
        self._events = events
        self._reopen_fn = reopen_fn
        self._identity_path = identity_path
        self._run_id = run_id
        self._app = app
        self._mark_timeout_s = max(0.0, float(mark_timeout_s))
        self._mark_lock = Lock()
        self._mark_disabled = False
        self._action_meta: dict[str, dict[str, str]] = {}
        self.subject_epoch_ref: str | None = None
        self.marks = 0
        self._bind_subject(pid)

    @property
    def action_meta(self) -> dict[str, dict[str, str]]:
        """Host-side mark metadata keyed by ``action_id`` (e.g. ``symbol_id``, ``window_tag``)."""
        with self._mark_lock:
            return {key: dict(meta) for key, meta in self._action_meta.items()}

    @_stage_capture
    def rebind(self, exports: Any, *, pid: int | None = None) -> None:
        """保留 runner 持有的 handle 身份，只替换其背后的新进程 RPC exports。"""
        with self._mark_lock:
            self._ex = exports
            self._mark_disabled = False
        self._bind_subject(pid)

    @_stage_capture
    def _bind_subject(self, pid: int | None) -> None:
        if self._identity_path is None or not self._run_id or not self._app or not pid:
            return
        try:
            from autophone.assets.build_identity import record_frida_epoch, rpc_build_info

            info = rpc_build_info(self._ex)
            epoch = record_frida_epoch(
                self._identity_path.parent,
                run_id=self._run_id,
                app=self._app,
                pid=int(pid),
                build_info=info,
            )
            self.subject_epoch_ref = epoch.epoch_ref
        except Exception as exc:  # noqa: BLE001 - capture remains usable without identity RPC
            logger.warning("Frida SubjectEpoch 记录失败: %s", exc)

    @_stage_capture
    def reopen(self) -> None:
        """App 冷启后重建 attach，并让后续 mark 落到新进程。

        spawn-gate 会话不支持中途冷启；显式抛错比继续向已死亡进程 mark 而假绿更安全。
        """
        if self._reopen_fn is None:
            raise CaptureAttachError("当前抓取会话不支持 App 冷启后重绑")
        self._reopen_fn()

    @_stage_capture
    def mark(
        self,
        case_id: str,
        step: str,
        operation: str | None = None,
        *,
        action_id: str | None = None,
        subject_epoch_ref: str | None = None,
        symbol_id: str | None = None,
        window_tag: str | None = None,
    ) -> None:
        with self._mark_lock:
            if self._mark_disabled:
                return
            if action_id:
                meta: dict[str, str] = {}
                if symbol_id:
                    meta["symbol_id"] = str(symbol_id)
                if window_tag:
                    meta["window_tag"] = str(window_tag)
                if meta:
                    self._action_meta[str(action_id)] = meta
            exports = self._ex
            finished = Event()
            errors: list[Exception] = []

            def mark_remote() -> None:
                try:
                    try:
                        exports.mark(
                            case_id,
                            step,
                            operation,
                            action_id,
                            subject_epoch_ref,
                        )
                    except TypeError:
                        exports.mark(case_id, step, operation)
                except Exception as exc:  # noqa: BLE001 抓取链路不拖垮用例
                    errors.append(exc)
                finally:
                    finished.set()

            start_device_thread(
                target=mark_remote,
                name="autophone-capture-mark",
                frida_endpoint=self.frida_host,
            )
            if not finished.wait(self._mark_timeout_s):
                # exports_sync 在目标进程退出后可能永久等不到 Frida RPC 响应。禁用当前
                # exports 的后续 marker；App 冷启并 rebind 后会重新启用。
                self._mark_disabled = True
                logger.warning(
                    "Frida mark timeout after %gs; disable markers until capture rebind",
                    self._mark_timeout_s,
                )
                return
            if errors:
                return
            self.marks += 1

    def merged(self) -> list[dict]:
        if self._events is None:
            return []
        meta = self.action_meta
        return net_capture.merge(self._events, action_meta=meta or None)


class CaptureSession:
    """spawn-gate 抓取会话上下文管理器。__enter__ 拉起被测实例并暖机，handle 注入 runner。"""

    spawn_gated = True

    def __init__(
        self,
        frida_host: str,
        bundle_id: str,
        agent_js: str,
        *,
        warmup: float = 12.0,
        settle: float = 2.0,
        on_loaded: Callable[[Any], None] | None = None,
        on_closing: Callable[[Any], None] | None = None,
        install_capture: bool = True,
        identity_path: Path | None = None,
        run_id: str | None = None,
        app: str | None = None,
        controlled_identity: dict | None = None,
    ) -> None:
        self._controlled_identity = controlled_identity
        self.cleanup = None
        self.failure_codes = []
        self._device = None
        self._owned_pid = None
        self._stage_guard = StageGuard.current()
        self.frida_host = frida_host
        self.bundle_id = bundle_id
        self.agent_js = agent_js
        self.warmup = warmup
        self.settle = settle
        # install_capture：resume 后是否装 delegate 抓取 hook（net_tap 挂起态不敢在 load 期装，会崩）。
        # 纯拦截短路场景传 False（不需要抓取，且省掉全类枚举）；抓取场景 True（默认）。
        self.install_capture = install_capture
        # on_loaded(exports)：load 后、resume 前跑（接口场景「规则先于 resume」，见 spawn_gate.after_load）。
        self._on_loaded = on_loaded
        self._on_closing = on_closing
        self._identity_path = identity_path
        self._run_id = run_id
        self._app = app
        self.events: list[dict] = []
        self.handle: CaptureHandle | None = None
        self.pid: int | None = None
        self.js_errors: list[dict] = []
        self.seal_receipt: dict | None = None
        self._gate = None
        self._lifetime = None

    def _on_message(self, message: dict, data: Any) -> None:
        if message.get("type") == "send":
            self.events.append(message["payload"])
        elif message.get("type") == "error":
            err = {"ev": "js_error", "desc": message.get("description")}
            self.events.append(err)
            self.js_errors.append(err)

    @_stage_capture
    def __enter__(self) -> "CaptureSession":
        self.agent_js = str(_assert_agent_bundle_fresh(self.agent_js))
        import frida  # 懒 import：无 frida 环境也能 import 本模块（platform fail-open 探测）

        device = frida.get_device_manager().add_remote_device(self.frida_host)
        self._device = device
        self._gate = spawn_gate(
            device, self.bundle_id, self.agent_js, self._on_message,
            after_load=self._on_loaded, install_capture=self.install_capture,
            **({"on_spawn": self._bind_owned_spawn, "before_close": self._close_owned_spawn}
               if self._controlled_identity is not None else {}),
            **({"frida_host": self.frida_host,
                "on_lifetime": lambda lifetime: setattr(self, "_lifetime", lifetime)}
               if self._stage_guard is not None or self._controlled_identity is not None else {}),
        )
        _pid, _script, exports = self._gate.__enter__()
        self.pid = int(_pid)
        time.sleep(self.warmup)  # 冷启动暖机到可用界面
        self.handle = CaptureHandle(
            exports,
            self.events,
            frida_host=self.frida_host,
            identity_path=self._identity_path,
            run_id=self._run_id,
            app=self._app,
            pid=int(_pid),
        )
        return self

    def _bind_owned_spawn(self, pid):
        if type(pid) is not int or pid <= 0 or self._owned_pid is not None:
            raise RuntimeError('controlled_spawn_identity_invalid')
        self.pid = self._owned_pid = pid

    def _close_owned_spawn(self):
        if self.cleanup is not None:
            return
        if self._owned_pid is None or self.pid != self._owned_pid or self._device is None:
            raise RuntimeError('controlled_cleanup_unverified')
        # The original Frida device is retained; no routing fallback or bundle kill.
        try:
            self._device.kill(self._owned_pid)
        except Exception:
            pass  # Only the same-device absence query can prove termination.
        try:
            absent = not any(p.pid == self._owned_pid for p in self._device.enumerate_processes())
        except Exception:
            absent = False
        if not absent:
            self.failure_codes.append('controlled_cleanup_unverified')
            raise RuntimeError('controlled_cleanup_unverified')
        captures = {e.get('capture_id') for e in self.events if e.get('capture_id')}
        self.cleanup = {**self._controlled_identity,
                        'capture_id': next(iter(captures)) if len(captures) == 1 else None,
                        'pid': self._owned_pid, 'owned_spawn': True,
                        'terminated': True, 'process_absent': True}

    @_stage_capture
    def __exit__(self, exc_type, exc, tb) -> None:
        # settle：等末尾动作触发的响应回来，再 detach
        if exc_type is None and self.settle and not (self._lifetime and self._lifetime.closed):
            time.sleep(self.settle)
        if self._gate is not None:
            try:
                if self.handle is not None and not (self._lifetime and self._lifetime.closed):
                    if self._on_closing is not None:
                        try:
                            self._on_closing(self.handle._ex)
                        except Exception:
                            if self._controlled_identity is not None:
                                self.failure_codes.append('controlled_closing_failed')
                                raise
                            logger.warning('capture_on_closing_failed')
                    self.seal_receipt = net_capture.seal_capture(
                        self.handle._ex, self.events, frida_host=self.frida_host)
                    if self._controlled_identity is not None and (
                        self.seal_receipt.get('ack_verified') is not True
                        or self.seal_receipt.get('ok') is not True
                        or self.seal_receipt.get('pending') != []
                    ):
                        self.failure_codes.append('controlled_seal_failed')
                        raise RuntimeError('controlled_seal_failed')
            finally:
                self._gate.__exit__(exc_type, exc, tb)
                self._gate = None

    def merged(self) -> list[dict]:
        """退出后按 task_ptr 合并 req/resp（case_id/step 已由 mark 时间窗回填）。"""
        meta = self.handle.action_meta if self.handle is not None else None
        return net_capture.merge(self.events, action_meta=meta or None)


def _default_uiopen(bundle_id: str) -> None:
    """普通启动被测 app；原生 UDS app_launch 不可用时退到 shell uiopen。"""
    import shlex

    from autophone.device.target import resolve_target
    from autophone.device.uds import request
    from autophone.device.shell import ShellError, run_shell

    try:
        response, _ = request(
            {"t": "app_launch", "bundle_id": bundle_id, "screen": False},
            sock_path=str(resolve_target().sock),
            timeout=20.0,
            parse_image=False,
        )
    except Exception as exc:  # noqa: BLE001 old/partially upgraded guest
        native_failure = str(exc)
    else:
        if response.get("ok"):
            return
        native_failure = f"rejected: {response.get('error')!r}"

    try:
        r = run_shell(f"uiopen --bundleid {shlex.quote(bundle_id)}", timeout_ms=15_000)
    except ShellError as exc:
        raise CaptureAttachError(
            f"native app_launch {bundle_id!r} 失败: {native_failure}; uiopen 异常: {exc}"
        ) from exc
    if r.code != 0:
        raise CaptureAttachError(
            f"native app_launch {bundle_id!r} 失败: {native_failure}; "
            f"uiopen exit {r.code}: {r.stderr.strip()!r}"
        )


def _default_pid(
    bundle_id: str,
    *,
    deadline_s: float = 5.0,
    frida_host: str | None = None,
) -> int:
    """轮询 guest app_list 拿前台/运行中的 bundle_id pid（>0），超时返回 0。

    uiopen 只前台化已运行 app、冷启未运行的 app——两种情况都靠这里确认进程已 materialize。"""
    from autophone.device.frida_process import pid_via_frida
    from autophone.device.uds import request

    end = time.monotonic() + deadline_s
    while True:
        resp, _ = request({"t": "app_list", "filter": "running"}, timeout=5)
        for app in resp.get("apps", []):
            if app.get("bundle_id") == bundle_id:
                pid = int(app.get("pid") or 0)
                if pid > 0:
                    return pid
        pid = pid_via_frida(bundle_id, frida_host=frida_host)
        if pid > 0:
            return pid
        if time.monotonic() >= end:
            return 0
        time.sleep(0.2)


def _default_attach(frida_host: str, pid: int) -> Any:
    import frida  # 懒 import：无 frida 环境也能 import 本模块

    device = frida.get_device_manager().add_remote_device(frida_host)
    return device.attach(pid)


class AttachSession:
    """uiopen + frida.attach 抓取会话（当前 guest 上 spawn-gate 已坏的替代启动模型）。

    spawn-gate 在这台 guest 上 spawn 后黑屏；补 uiopen 虽亮屏但 Frida 仍挂在 spawn 出的
    旧 pid、真正的前台 UI 是另一个 pid → 抓的不是 runner 操作的那个进程（假绿）。改法是
    **普通启动 + attach 到前台 pid**：

      __enter__: uiopen 启动 → ready gate（OCR 命中可用界面，证 dyld 起好再 attach，
      早 attach 会复现 `probing dyld`）→ app_list 取前台 pid → frida.attach(pid) → load
      net_tap → handle。
      __exit__: **只 detach，不 kill**（app 不是本会话 spawn 的，生死不由抓取层决定）。

    代价：抓不到 app 冷启动最早期请求（attach 在 ready 之后）；登录回归关心的是 runner
    动作触发的请求，attach-after-ready 完整覆盖。

    seam（uiopen_fn/pid_fn/ready_fn/attach_fn）全可注入，便于免 frida/VM 测整条生命周期。"""

    spawn_gated = False  # 自己在 __enter__ 内完成 ready+attach，不走 service 的 spawn-ready gate

    def __init__(
        self,
        frida_host: str,
        bundle_id: str,
        agent_js: str,
        *,
        ready_fn: Callable[[], dict] | None = None,
        settle: float = 2.0,
        uiopen_fn: Callable[[str], None] | None = None,
        pid_fn: Callable[[str], int] | None = None,
        attach_fn: Callable[[str, int], Any] | None = None,
        on_loaded: Callable[[Any], None] | None = None,
        on_closing: Callable[[Any], None] | None = None,
        install_capture: bool = True,
        teardown_timeout_s: float = 2.0,
        identity_path: Path | None = None,
        run_id: str | None = None,
        app: str | None = None,
    ) -> None:
        self._stage_guard = StageGuard.current()
        self.frida_host = frida_host
        self.bundle_id = bundle_id
        self.agent_js = agent_js
        self.settle = settle
        self._ready_fn = ready_fn
        # install_capture：load 后是否装 delegate 抓取 hook（attach 本就活进程，load 期装也安全，
        # 但与 spawn 路径统一走 rpc）。纯拦截短路场景传 False。
        self.install_capture = install_capture
        self.teardown_timeout_s = max(0.0, float(teardown_timeout_s))
        # on_loaded(exports)：script.load() 之后、返回「已生效」之前跑。接口场景在此下发规则 +
        # 自检 hook（§六）；抛异常 → 就地清理 attach（下方 except），__enter__ 失败、会话不成立（fail-closed）。
        self._on_loaded = on_loaded
        self._on_closing = on_closing
        self._identity_path = identity_path
        self._run_id = run_id
        self._app = app
        self._uiopen_fn = uiopen_fn or _default_uiopen
        self._pid_fn = pid_fn or (
            lambda bundle_id: _default_pid(bundle_id, frida_host=self.frida_host)
        )
        self._attach_fn = attach_fn or _default_attach
        self.events: list[dict] = []
        self.handle: CaptureHandle | None = None
        self.js_errors: list[dict] = []
        self.seal_receipt: dict | None = None
        self.pid: int | None = None
        self._session: Any = None
        self._script: Any = None
        # __enter__ 失败时的就地清理结果（{unloaded, detached, errors}）；接口场景据此判 detach
        # 是否确认、未确认则 kill 升级（§8.2）。成功进入则保持 None。
        self.enter_cleanup: dict[str, Any] | None = None
        self._lifetime = None

    def _on_message(self, message: dict, data: Any) -> None:
        if self._lifetime is not None:
            self._lifetime.accept_event(lambda: self._append_message(message, data))
        else:
            self._append_message(message, data)

    def _append_message(self, message: dict, data: Any) -> None:
        if message.get("type") == "send":
            self.events.append(message["payload"])
        elif message.get("type") == "error":
            err = {"ev": "js_error", "desc": message.get("description")}
            self.events.append(err)
            self.js_errors.append(err)

    @_stage_capture
    def _open(self) -> "AttachSession":
        self.agent_js = str(_assert_agent_bundle_fresh(self.agent_js))
        # 1. 普通方式启动 App（不 spawn）
        self._uiopen_fn(self.bundle_id)
        # 2. ready gate：OCR 命中可用界面再 attach（dyld 未起好时 attach 会 probing dyld 失败）
        ready = self._ready_fn() if self._ready_fn else {"ok": True, "reason": "no-gate"}
        if not ready.get("ok"):
            raise CaptureAttachError(f"attach 前界面未就绪: {ready.get('detail') or ready}")
        # 3. 找当前前台/运行中的 pid（就是 runner 即将操作的那个实例）
        pid = self._pid_fn(self.bundle_id)
        if pid <= 0:
            raise CaptureAttachError(f"uiopen 后未找到 {self.bundle_id!r} 的运行 pid")
        # 4. attach + load 生产抓取 agent
        with open(self.agent_js, "r", encoding="utf-8") as f:
            js = f.read()
        session = None
        script = None
        try:
            session = self._attach_fn(self.frida_host, pid)
            if self._stage_guard is not None:
                self._lifetime = FridaLifetime(session, host=self.frida_host)
            script = session.create_script(js)
            if self._lifetime is not None:
                self._lifetime.add_script(script)
            script.on("message", self._on_message)
            script.load()
            exports = _exports(script)
            if self._lifetime is not None:
                exports = self._lifetime.exports(exports)
            if self._on_loaded is not None:
                # Capture filters and scenario rules must be configured before
                # installing passive hooks, otherwise traffic in this window is
                # observed with the unsafe/default policy.
                self._on_loaded(exports)
            if self.install_capture:
                # net_tap load 期不再自动装 delegate 抓取 hook（与 spawn 统一）；attach 到活进程后显式装。
                _fn = getattr(exports, "install_capture_hooks", None) or getattr(
                    exports, "installCaptureHooks", None
                )
                if _fn is not None:
                    _fn()
        except Exception as exc:  # noqa: BLE001 attach/load/on_loaded 失败 → 前置失败，安全退回普通启动
            # __enter__ 未成功返回 → __exit__ 不会被调用：就地清理已建立的脚本/attach session，
            # 否则原 pid 上残留半加载脚本 / 活 attach session，污染随后 fail-open 的普通启动与下次 capture。
            # **每步成败记进 self.enter_cleanup（不再吞掉）**：on_loaded 抛错时 script 已 load、
            # on_loaded 可能已显式安装 Interceptor.replace；detach 若失败短路 hook 会残留——
            # 接口场景据此 kill 升级（§8.2 / review Finding 1）。只读抓取只安装 attach
            # 观察器，但仍如实记录清理结果。
            self._script = script
            self._session = session
            self.enter_cleanup = self.teardown()   # teardown 逐项上报、把 _script/_session 置 None
            raise CaptureAttachError(f"attach pid={pid} / load agent 失败: {exc}") from exc
        self._session = session
        self._script = script
        self.pid = pid
        if self.handle is None:
            self.handle = CaptureHandle(
                exports,
                self.events,
                frida_host=self.frida_host,
                reopen_fn=self.reopen,
                identity_path=self._identity_path,
                run_id=self._run_id,
                app=self._app,
                pid=pid,
            )
        else:
            # CaseContext/runner 持有进入会话时的 handle；不能换对象，只能重绑 exports。
            self.handle.rebind(exports, pid=pid)
        self.enter_cleanup = None
        logger.info(
            "uiopen+attach 抓取就绪：pid=%s ready=%s（spawn-gate not usable on this guest, "
            "using uiopen+attach capture）",
            pid, ready.get("reason"),
        )
        return self

    @_stage_capture
    def __enter__(self) -> "AttachSession":
        return self._open_with_transient_retry(stage="首次 attach")

    def _open_with_transient_retry(self, *, stage: str) -> "AttachSession":
        try:
            return self._open()
        except CaptureAttachError as exc:
            # app_list/ready gate 已通过后，Frida transport 或 script.load 仍可能撞上
            # 新进程 materialize 尾部。_open 已完整回收半 attach；只对活机坐实的
            # 两种瞬态做一次重试，其它错误及第二次失败照旧上抛。
            if not any(marker in str(exc) for marker in _TRANSIENT_ATTACH_ERRORS):
                raise
            logger.warning(
                "%s 碰到瞬态抓取错误，%.1fs 后重试一次：%s",
                stage, ATTACH_TRANSIENT_RETRY_S, exc,
            )
            time.sleep(ATTACH_TRANSIENT_RETRY_S)
            return self._open()

    @_stage_capture
    def reopen(self) -> None:
        """冷启后 detach 旧进程并 attach 当前新 PID，事件流和 handle 身份保持不变。"""
        old_pid = self.pid
        cleanup = self.teardown()
        if cleanup["errors"]:
            # 旧进程通常已被 kill -9；此时 unload/detach 报错是可预期的，仍应尝试绑定新进程。
            logger.warning("旧抓取进程 pid=%s 清理未完全确认：%s", old_pid, cleanup["errors"])
        self.pid = None
        self._open_with_transient_retry(stage="冷启重绑")
        logger.info("抓取会话冷启重绑完成：old_pid=%s new_pid=%s", old_pid, self.pid)

    @_stage_capture
    def __exit__(self, exc_type, exc, tb) -> None:
        # settle：等末尾动作触发的响应回来，再 detach
        if exc_type is None and self.settle:
            time.sleep(self.settle)
        self.teardown()

    @_stage_capture
    def teardown(self) -> dict[str, Any]:
        """卸载脚本 + detach（不 kill；app 非本会话 spawn）。

        返回 ``{'unloaded', 'detached', 'errors'}``——**每步成败逐项上报、不吞异常**，
        供接口场景 close() 的 fail-closed 清理闸判定（§8.2：unload/detach 未确认 →
        cleanup_verified=false → 挡下一场景 / 升级 kill）。只读抓取的 __exit__ 忽略返回值，
        行为不变（best-effort detach）。幂等：已 teardown 过再调返回全 True。"""
        if self._script is not None and self.handle is not None and not (self._lifetime and self._lifetime.closed):
            if self._on_closing is not None:
                try:
                    self._on_closing(self.handle._ex)
                except Exception:
                    logger.warning('capture_on_closing_failed')
            self.seal_receipt = net_capture.seal_capture(
                self.handle._ex, self.events, timeout_s=self.teardown_timeout_s,
                frida_host=self.frida_host)
        if self._lifetime is not None:
            lifetime = self._lifetime
            if lifetime.closed:
                return dict(lifetime.receipt)
            lifetime.closing = True
            finished = Event()
            def dispose():
                try:
                    lifetime.dispose()
                except Exception:
                    pass  # StageGuard retains unconfirmed attachments.
                finally:
                    finished.set()
            start_device_thread(dispose, name="autophone-capture-dispose", frida_endpoint=self.frida_host)
            self._script = self._session = None
            if not finished.wait(self.teardown_timeout_s):
                return {"unloaded": False, "detached": False, "errors": ["dispose timeout"]}
            return dict(lifetime.receipt or {"unloaded": False, "detached": False, "errors": ["dispose not admitted"]})
        errors: list[str] = []
        unloaded = self._script is None
        detached = self._session is None
        script = self._script
        self._script = None
        if script is not None:
            finished = Event()
            unload_errors: list[str] = []

            def unload_remote(script=script, finished=finished, unload_errors=unload_errors) -> None:
                try:
                    script.unload()
                except Exception as exc:  # noqa: BLE001
                    unload_errors.append(f"unload: {exc}")
                finally:
                    finished.set()

            start_device_thread(
                target=unload_remote,
                name="autophone-capture-unload",
                frida_endpoint=self.frida_host,
            )
            if finished.wait(self.teardown_timeout_s):
                if unload_errors:
                    errors.extend(unload_errors)
                else:
                    unloaded = True
            else:
                errors.append(
                    f"unload timeout after {self.teardown_timeout_s:g}s"
                )
        session = self._session
        self._session = None
        if session is not None:
            finished = Event()
            detach_errors: list[str] = []

            def detach_remote() -> None:
                try:
                    session.detach()  # 只 detach；app 非本会话 spawn，不 kill
                except Exception as exc:  # noqa: BLE001
                    detach_errors.append(f"detach: {exc}")
                finally:
                    finished.set()

            start_device_thread(
                target=detach_remote,
                name="autophone-capture-detach",
                frida_endpoint=self.frida_host,
            )
            if finished.wait(self.teardown_timeout_s):
                if detach_errors:
                    errors.extend(detach_errors)
                else:
                    detached = True
            else:
                errors.append(
                    f"detach timeout after {self.teardown_timeout_s:g}s"
                )
        return {"unloaded": unloaded, "detached": detached, "errors": errors}

    def merged(self) -> list[dict]:
        meta = self.handle.action_meta if self.handle is not None else None
        return net_capture.merge(self.events, action_meta=meta or None)
