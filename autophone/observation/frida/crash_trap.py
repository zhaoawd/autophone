#!/usr/bin/env python3
# coding=utf8
"""phase4 crash_trap —— 捕获被测 App 的**原始** SIGBUS/SIGSEGV 崩点。

用途：App 内的第三方崩溃上报 SDK 可能在自己的信号 handler 里 `kill(getpid(), 捕获信号)` 重抛，
guest 自带 .ips 的 faulting 帧因此落在重抛点，原始出错线程现场丢失。本工具用 Frida 的
`Process.setExceptionHandler`（走 Mach task 异常端口，早于 POSIX handler）在**原始出错线程**上
抓到真 pc + fault 地址 + backtrace。默认只观察，不修改 App 的信号 handler；只有显式传
`--neutralize-handlers` 才会摘掉第三方崩溃上报 SDK 的 handler。见 agent/src/crash_trap.js。

用法（需活 VM + frida 隧道 27042；用 --group frida 跑）：

    AP_APP_NAME=<bundle id 或进程名> uv run --group frida python3 -m autophone.observation.frida.crash_trap --settle 600

它 attach 到当前在跑的被测 App，装 trap，然后**在另一个终端照常触发会跑挂 app 的 run**（或手动操作
到崩），trap 一旦捕获就打印符号化 backtrace 并落 jsonl。app 崩溃重启后 pid 变了，本工具会自动重新 attach
（re-arm），所以一个 --settle 窗口内能连着抓多次崩溃。host 侧符号化用 --binary 指向拉下来的 App 二进制
（stripped 也能给出 module+offset；系统库由 Frida 直接给符号名）。
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from autophone.device.stage_guard import StageGuard, StageAccessDenied, OperationClass
from autophone.logging import get_logger
from autophone.device.stage_threads import start_device_thread
from autophone.device.frida_cleanup import release_call
from autophone.paths import STATE_ROOT
from autophone.device.frida_endpoint import resolve_frida_host

FRIDA_HOST = resolve_frida_host()
APP_NAME = os.environ.get("AP_APP_NAME", "")
AGENT_JS = Path(__file__).resolve().parent / "agent" / "_build" / "crash_trap.js"
AGENT_JS_SRC = Path(__file__).resolve().parent / "agent" / "src" / "crash_trap.js"
RUNS_DIR = STATE_ROOT / "runs" / "platform"


log = get_logger(__name__)
_ACTIVE_TRAP: ContextVar[CrashTrapSession | None] = ContextVar("active_crash_trap", default=None)
ATTACH_TIMEOUT_S = 5.0


@contextmanager
def crash_trap_scope(session):
    token = _ACTIVE_TRAP.set(session)
    try:
        yield
    finally:
        _ACTIVE_TRAP.reset(token)


@contextmanager
def defer_crash_trap_attach():
    """Keep optional instrumentation out of a deterministic App startup."""
    session = _ACTIVE_TRAP.get()
    if isinstance(session, CrashTrapSession):
        with session.attach_gate:
            yield
    else:
        yield


class _ProducerAuthorityEnded(Exception):
    """后台线程的阶段检查被租约侧拒绝（Agent 的取消、租约失效等），线程据此正常结束。"""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.cause = cause


#: 结束 attach 循环的检查结果：StageGuard 拒绝或租约侧拒绝；两者都不当作 attach 失败重试。
_STOP_PRODUCING = (StageAccessDenied, _ProducerAuthorityEnded)


def _agent_source() -> str:
    # agent 无 import，可直接加载源码。_build 是 gitignored 的派生物，优先它会让旧 bundle
    # 静默绕过新的安全默认；因此源码在场时必须以源码为真源。
    if AGENT_JS_SRC.exists():
        path = AGENT_JS_SRC
    else:
        from autophone.observation.frida.agent_bundle import ensure_agent_bundle

        path = ensure_agent_bundle(AGENT_JS, "crash-trap")
    return path.read_text(encoding="utf-8")


def _crash_evidence(crash: Any) -> dict[str, Any] | None:
    """Turn Frida's optional Crash object into JSON-safe host evidence."""
    if crash is None:
        return None
    out: dict[str, Any] = {}
    for name in ("summary", "report", "parameters"):
        try:
            value = getattr(crash, name, None)
        except Exception:  # noqa: BLE001 - detached callback must never fail
            continue
        if value is None:
            continue
        try:
            json.dumps(value)
            out[name] = value
        except (TypeError, ValueError):
            out[name] = str(value)
    return out or {"description": str(crash)}


def _pid_via_app_list(bundle_id: str) -> int | None:
    """bundle_id → pid 走 guest UDS app_list（运行器自己用的权威映射，app-agnostic）。fail-open 返回 None。"""
    try:
        from autophone.device.uds import request
        resp, _ = request({"t": "app_list", "filter": "running"})
        for app in resp.get("apps", []):
            if app.get("bundle_id") == bundle_id:
                pid = int(app.get("pid") or 0)
                return pid if pid > 0 else None
    except StageAccessDenied:
        raise
    except Exception:  # noqa: BLE001 - UDS 不可用就回退 frida 名匹配
        return None
    return None


def _resolve_pid(device, app_name: str) -> int | None:
    # 传的是 bundle id（含点）→ 先走 UDS app_list 精确映射（app 无关、最稳）。
    if "." in app_name:
        pid = _pid_via_app_list(app_name)
        if pid is not None:
            return pid
    # 回退：frida 进程名匹配。frida 进程名可能是 App 的显示名，不一定等于可执行文件名。
    aliases = {app_name} if app_name else set()
    for p in device.enumerate_processes():
        if any(a in p.name for a in aliases):
            return p.pid
    return None


def _fmt_frame(fr: dict[str, Any]) -> str:
    mod = fr.get("module") or "?"
    off = fr.get("offset")
    sym = fr.get("symbol")
    pc = fr.get("pc")
    tail = f" {sym}" if sym else ""
    loc = f"{mod}+{off}" if off is not None else (mod if mod != "?" else pc)
    return f"{loc}{tail}"


def _print_crash(payload: dict[str, Any]) -> None:
    print("\n" + "!" * 72)
    print(f"!! 原始崩点捕获 #{payload.get('n')}  type={payload.get('type')}  "
          f"fault_address={payload.get('fault_address')}")
    mem = payload.get("memory")
    if mem:
        print(f"   memory: {mem.get('operation')} @ {mem.get('address')}")
    print(f"   thread={payload.get('thread_id')}  pc={payload.get('pc')}  lr={payload.get('lr')}")
    pcf = payload.get("pc_frame")
    if pcf:
        print(f"   PC   -> {_fmt_frame(pcf)}")
    lrf = payload.get("lr_frame")
    if lrf:
        print(f"   LR   -> {_fmt_frame(lrf)}")
    bt = payload.get("backtrace") or []
    mode = payload.get("backtrace_mode", "accurate")
    print(f"   backtrace ({mode}, {len(bt)} frames):")
    for i, fr in enumerate(bt):
        print(f"     #{i:<2} {_fmt_frame(fr)}")
    if payload.get("backtrace_error"):
        print(f"   backtrace_error: {payload['backtrace_error']}")
    print("!" * 72 + "\n", flush=True)


class CrashTrapSession:
    """跨一段时间的崩溃捕获会话：后台线程轮询 app pid，(re)attach 装 crash_trap agent，把崩溃/日志
    事件落到 out_dir/crash_trap_events.jsonl。**全程 fail-open**——无 frida / attach 不上 / agent 报错都
    只记事件不抛，绝不拖垮调用方（回归 run）。

    两种驱动：
      - CLI：`run_until(deadline)` 前台跑到 settle 到点或 Ctrl-C；
      - 平台：`with CrashTrapSession(...) as s: <跑回归>`，__enter__ 起后台线程、__exit__ 停并 join。

    verbose=True 时把 armed/crash/log 打到 stdout（CLI 用）；平台后台挂时 verbose=False，只落 jsonl，
    崩溃事件由调用方读回。"""

    def __init__(self, *, bundle_id_or_name: str = APP_NAME, frida_host: str | None = None,
                 out_dir: Path, swallow: bool = False, verbose: bool = False,
                 neutralize_handlers: bool = False, poll: float = 0.5) -> None:
        self._stage_guard = StageGuard.current()
        self._producer_binding = None
        self.app_name = bundle_id_or_name
        self.frida_host = resolve_frida_host(frida_host)
        self.out_dir = out_dir
        self.swallow = swallow
        self.neutralize_handlers = neutralize_handlers
        self.verbose = verbose
        self.poll = poll
        self.events_path = out_dir / "crash_trap_events.jsonl"
        self.result: dict[str, Any] = {
            "crashes": [], "attaches": [], "detaches": [], "events_path": str(self.events_path),
            "available": None, "error": None,
            "mode": "neutralize_handlers" if neutralize_handlers else "observe_only",
        }
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ev_f = None
        self._diagnostic_session_id = uuid4().hex
        self._attach_sequence = 0
        self.attach_gate = threading.Lock()

    def try_begin_attach(self) -> bool:
        return self.attach_gate.acquire(blocking=False)

    def end_attach(self) -> None:
        self.attach_gate.release()

    def _attach_with_diagnostics(self, device, pid: int, attempt_id: str):
        """Record the native attach interval independently of script loading."""
        started_ns = time.monotonic_ns()
        fields = {
            "attempt_id": attempt_id, "pid": pid, "app": self.app_name,
            "host_pid": os.getpid(), "clock": "host",
        }
        self._write_attach_event({"ev": "attach_start", **fields, "ts": time.time(),
                                  "monotonic_ns": started_ns})
        outcome = "interrupted"
        error_type = None
        try:
            import frida

            with frida.Cancellable() as cancellable:
                timer = threading.Timer(ATTACH_TIMEOUT_S, cancellable.cancel)
                timer.daemon = True
                timer.start()
                try:
                    session = device.attach(pid)
                finally:
                    timer.cancel()
                    timer.join()
            outcome = "succeeded"
            return session
        except Exception as exc:
            outcome = "failed"
            error_type = type(exc).__name__
            raise
        finally:
            ended_ns = time.monotonic_ns()
            self._write_attach_event({"ev": "attach_end", **fields, "ts": time.time(),
                                      "monotonic_ns": ended_ns, "outcome": outcome,
                                      "error_type": error_type,
                                      "duration_ms": (ended_ns - started_ns) / 1_000_000})
            log.debug("crash-trap attach pid=%s attempt=%s outcome=%s duration_ms=%.3f",
                      pid, attempt_id, outcome, (ended_ns - started_ns) / 1_000_000)

    def _write_attach_event(self, event: dict[str, Any]) -> None:
        try:
            self._write_event(event)
        except (OSError, ValueError):
            # Diagnostic IO must not discard a successfully acquired native session.
            log.error("crash-trap attach diagnostic write failed", exc_info=True)

    # —— context manager（平台后台挂） ——
    def __enter__(self) -> "CrashTrapSession":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("crash_trap_already_started")
        self._check_stage()
        self._producer_binding = StageGuard.producer_binding(
            frozenset({OperationClass.FRIDA, OperationClass.READ}))
        with StageGuard.bind(self._producer_binding or StageGuard.binding()):
            self._thread = start_device_thread(
                lambda: self._loop(None), name="crash-trap", frida_endpoint=self.frida_host)

    def _check_stage(self) -> None:
        guard = self._stage_guard or StageGuard.for_device_call()
        if guard is not None:
            guard.assert_allowed(OperationClass.FRIDA, frida_endpoint=self.frida_host)
            self._stage_guard = guard

    def _check_producer(self) -> None:
        """attach 循环内的阶段检查。

        `StageAccessDenied` 原样上抛。其他异常来自 StageGuard 的租约检查（Rig Agent 上为
        `AssignmentCancelled`、`StaleDeviceWrite`），表示本 Attempt 不再允许新的设备操作：转为
        `_ProducerAuthorityEnded`，由 `_loop` 记录后正常结束线程，`finally` 照常 detach
        （BL-220 剩余项：此前以未捕获异常结束线程）。
        """
        try:
            self._check_stage()
        except StageAccessDenied:
            raise
        except Exception as exc:  # noqa: BLE001 - lease-side refusals end the producer
            raise _ProducerAuthorityEnded(exc) from exc

    def request_stop(self) -> None:
        self._stop.set()
        if self._producer_binding is not None:
            self._producer_binding.guard.revoke_producer(self._producer_binding)

    def wait_stopped(self, timeout: float) -> bool:
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            return not self._thread.is_alive()
        return True

    def stop(self, join_timeout: float = 5.0) -> dict[str, Any]:
        self.request_stop()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
            # Managed evidence cannot be frozen while its producer still runs.
            if self._stage_guard is not None:
                while self._thread.is_alive():
                    self._thread.join(timeout=0.1)
        return self.result

    def run_until(self, deadline: float | None) -> dict[str, Any]:
        """前台跑（CLI）：deadline=None 表示无限，直到 Ctrl-C。"""
        try:
            self._check_stage()
            with StageGuard.operation_if_bound(OperationClass.FRIDA, frida_endpoint=self.frida_host):
                self._loop(deadline)
        except KeyboardInterrupt:
            if self.verbose:
                print("\n# 手动中断，收尾。", flush=True)
        return self.result

    def _say(self, line: str) -> None:
        if self.verbose:
            print(line, flush=True)

    def _write_event(self, payload: dict[str, Any]) -> None:
        if self._ev_f is not None:
            self._ev_f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._ev_f.flush()

    def _on_message(self, message, data):  # noqa: ANN001 - frida callback shape
        if message.get("type") == "send":
            p = message["payload"]
            self._write_event(p)
            ev = p.get("ev")
            if ev == "crash":
                self.result["crashes"].append(p)
                if self.verbose:
                    _print_crash(p)
            elif ev == "ready":
                self._say(f"# trap armed on pid={p.get('pid')} arch={p.get('arch')} "
                          f"main={p.get('main_module')}")
            elif ev == "log":
                self._say(f"  {p.get('line')}")
        elif message.get("type") == "error":
            self._say(f"  [js error] {message.get('description')}")
            self._write_event({"ev": "js_error", "desc": message.get("description")})

    def _on_detached(self, reason: str, crash: Any) -> None:
        """Persist the host-side detach reason even when JS died with the target."""
        payload = {
            "ev": "detached", "reason": str(reason), "crash": _crash_evidence(crash),
            "ts": time.time(),
        }
        self.result["detaches"].append(payload)
        self._write_event(payload)
        self._say(f"# session detached: reason={reason} crash={payload['crash']}")

    def _loop(self, deadline: float | None) -> None:
        try:
            import frida
        except Exception as e:  # noqa: BLE001 - 无 frida = fail-open no-op
            self.result["available"] = False
            self.result["error"] = f"frida 不可用（需 --group frida）：{e!r}"
            self._say(f"# crash_trap 跳过：{self.result['error']}")
            return

        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._ev_f = self.events_path.open("a", encoding="utf-8")
        self.result["available"] = True
        try:
            self._check_stage()
            device = frida.get_device_manager().add_remote_device(self.frida_host)
            js = _agent_source()
        except Exception as e:  # noqa: BLE001 - 隧道没通等，fail-open
            self.result["available"] = False
            self.result["error"] = f"frida device/agent 不可用：{e!r}"
            self._say(f"# crash_trap 跳过：{self.result['error']}")
            self._ev_f.close(); self._ev_f = None
            return

        attached_pid: int | None = None
        session = None
        sessions = []
        if self._stage_guard is not None:
            def verify_cleanup():
                # The admitted producer owns native cleanup until its thread exits.
                if self.result.get("cleanup_verified") is not True:
                    raise RuntimeError("crash trap cleanup unverified: " +
                                       repr(self.result.get("cleanup_errors")))
            self._stage_guard.register_resource(verify_cleanup)
        self._say(f"# crash_trap: (re)attach {self.app_name} over {self.frida_host}")

        try:
            while not self._stop.is_set() and (deadline is None or time.time() < deadline):
                self._check_producer()
                pid = _resolve_pid(device, self.app_name)
                if pid is None:
                    attached_pid = None
                    session = None
                    self._stop.wait(self.poll)
                    continue
                if pid == attached_pid and session is not None:
                    self._stop.wait(self.poll)
                    continue
                # 新 pid（首次或崩溃重启后）——重新 attach + 装 trap
                attempt_id = None
                phase = "admission"
                if not self.try_begin_attach():
                    self._stop.wait(self.poll)
                    continue
                try:
                    self._check_producer()
                    # The App may have restarted while the gate was held.
                    if _resolve_pid(device, self.app_name) != pid:
                        continue
                    self._attach_sequence += 1
                    attempt_id = f"{self._diagnostic_session_id}:{self._attach_sequence}"
                    phase = "attach"
                    session = self._attach_with_diagnostics(device, pid, attempt_id)
                    sessions.append(session)
                    phase = "script_setup"
                    session.on("detached", self._on_detached)
                    self._check_producer()
                    script = session.create_script(js)
                    script.on("message", self._on_message)
                    self._check_producer()
                    script.load()
                    if self.swallow:
                        try:
                            exports = getattr(script, "exports_sync", None) or script.exports
                            self._check_producer()
                            exports.set_swallow(True)
                        except _STOP_PRODUCING:
                            raise
                        except Exception as e:  # noqa: BLE001
                            self._say(f"  # set_swallow 失败: {e!r}")
                    if self.neutralize_handlers:
                        try:
                            exports = getattr(script, "exports_sync", None) or script.exports
                            self._check_producer()
                            exports.set_neutralize_handlers(True)
                        except _STOP_PRODUCING:
                            raise
                        except Exception as e:  # noqa: BLE001
                            self._say(f"  # set_neutralize_handlers 失败: {e!r}")
                    attached_pid = pid
                    self.result["attaches"].append(
                        {"pid": pid, "at": datetime.now().isoformat(timespec="seconds"),
                         "attempt_id": attempt_id})
                    self._say(f"# attached pid={pid}")
                except _STOP_PRODUCING:
                    raise
                except Exception as e:  # noqa: BLE001 - attach 失败本身是证据（probing dyld 等）
                    self._say(f"  # attach pid={pid} 失败: {e!r}（{self.poll}s 后重试）")
                    self._write_event({"ev": "attach_error", "pid": pid, "desc": repr(e),
                                       "attempt_id": attempt_id, "phase": phase,
                                       "ts": time.time(), "monotonic_ns": time.monotonic_ns()})
                    session = None
                    attached_pid = None
                finally:
                    self.end_attach()
                self._stop.wait(self.poll)
        except StageAccessDenied as exc:
            self.result["error"] = str(exc)
        except _ProducerAuthorityEnded as exc:
            self.result["error"] = str(exc)
            self.result["stopped_by"] = type(exc.cause).__name__
            log.info("crash-trap stopped: device access no longer admitted: %s", exc)
        finally:
            # These detaches complete the already-admitted producer operation.
            # A revoked token admits no new polling/attach work; transition waits
            # for this cleanup before releasing its in-flight reservation.
            cleanup_errors = []
            for attachment in reversed(sessions):
                try:
                    if getattr(attachment, "is_detached", False) is not True:
                        release_call(attachment.detach)
                except Exception as exc:  # noqa: BLE001 - preserve cleanup evidence
                    cleanup_errors.append(str(exc))
            self.result["cleanup_verified"] = not cleanup_errors
            self.result["cleanup_errors"] = cleanup_errors
            if self._ev_f is not None:
                self._ev_f.close()
                self._ev_f = None


def trap(settle: float, out_dir: Path, swallow: bool,
         neutralize_handlers: bool = False) -> dict[str, Any]:
    sess = CrashTrapSession(
        out_dir=out_dir, swallow=swallow, neutralize_handlers=neutralize_handlers, verbose=True,
    )
    print(f"# crash_trap: 等 {settle:.0f}s，自动 (re)attach {APP_NAME} over {FRIDA_HOST}", flush=True)
    print("# → 现在去另一个终端触发会跑挂 app 的 run（或手动操作到崩），trap 会抓真现场。", flush=True)
    return sess.run_until(time.time() + settle)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--settle", type=float, default=600.0,
                   help="监听窗口秒数（默认 600）；期间自动 re-attach")
    p.add_argument("--out", type=Path, default=None,
                   help="输出目录；默认 runs/platform/<date>/crash-trap-<ts>")
    p.add_argument("--swallow", action="store_true",
                   help="吞掉异常保活（危险，状态可能已损坏，仅临时排查用）；默认让进程照常崩")
    p.add_argument(
        "--neutralize-handlers", action="store_true",
        help="修改被测进程的致命信号 handler（危险，仅用于排除第三方崩溃 SDK 重抛污染）",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.out is not None:
        out_dir = args.out
    else:
        now = datetime.now()
        out_dir = RUNS_DIR / now.strftime("%Y%m%d") / f"crash-trap-{now.strftime('%H%M%S')}"

    result = trap(args.settle, out_dir, args.swallow, args.neutralize_handlers)

    print("=" * 64)
    print("crash_trap 结束")
    print(f"  host={FRIDA_HOST}  app={APP_NAME}")
    print(f"  mode={result['mode']}")
    print(f"  events → {result['events_path']}")
    print(f"  attach 次数: {len(result['attaches'])}  捕获崩溃: {len(result['crashes'])}")
    for c in result["crashes"]:
        pcf = c.get("pc_frame") or {}
        loc = _fmt_frame(pcf) if pcf else c.get("pc")
        print(f"    - #{c.get('n')} {c.get('type')} @ {loc}  fault={c.get('fault_address')}")
    print("=" * 64)
    print("RESULT_JSON " + json.dumps(
        {"attaches": len(result["attaches"]), "crashes": len(result["crashes"]),
         "events_path": result["events_path"]}, ensure_ascii=False))
    return 0 if result["crashes"] else 0  # 没抓到崩溃不算工具失败（可能这次没崩）


if __name__ == "__main__":
    raise SystemExit(main())
