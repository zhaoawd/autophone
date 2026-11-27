"""ax_session.py — a live AX-dump provider for CaseContext.tap_ax.

Holds a Frida session into the target app and serves fresh accessibility dumps
on demand. This is the per-run wiring behind the `tap_ax` verb: inject once,
then `refresh()` per tap (the AX tree is per-screen, so each tap_ax re-dumps).

Pipeline (validated 2026-06-12, see docs/phase1b-ax-spike.md):
  open():     UDS file_put the dylib → Frida attach → Module.load (constructor
              activates accessibility in-process + registers the trigger)
  refresh():  Frida notify_post the trigger → UDS file_get the JSON → parse
  close():    detach

Frida is needed only to inject (this VM has no tweak loader) and to post the
Darwin trigger (no notifyutil on the guest); neither needs the ObjC bridge, so
raw create_script is fine (Frida 17 dropped the bridge from raw scripts). The
parse + node selection + points→native mapping live in ax_target (host-pure).

Needs the frida dep group and a tunnel to the guest frida-server:
    ssh -L 27042:127.0.0.1:27042 mobile@<guest>
"""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from functools import wraps
import json
import time
from threading import Event, Lock
from pathlib import Path
from typing import cast

from autophone.assets.apps import AxBackend, DEFAULT_AX_BACKEND
from autophone.device.stage_guard import OperationClass, StageGuard
from autophone.device.stage_threads import start_device_thread
from autophone.device.frida_lifetime import FridaLifetime
from autophone.device.uds import request
from autophone.device.ax_target import AxDump, parse_dump
from autophone.device.shell import resolve_sock
from autophone.paths import CODE_ROOT
from autophone.device.frida_endpoint import resolve_frida_host

# Bump the guest filename when the dumper protocol changes. Module.load keeps
# an already loaded image resident, so overwriting its old on-disk path would
# silently continue using stale code in a long-running app process.
# v3（2026-07-13）：dump 带 captured_at_ms（7f361007 源码加了字段但 .dylib 一直没重编，
# host 新鲜度闸把所有 dump 判 stale，tap_ax 自 07-11 起全线静默退盲点坐标——重编+改名收口）。
# v4 adds request identity and explicit completeness for checkpoint evidence.
# v5（2026-09-20，BL-154）：dylib 改为 arm64 + arm64e 双切片，才能注入 arm64e 的系统进程
# SpringBoard；`complete` 改为真正的布尔值（此前 `@(a && b)` 序列化成 1，host 侧
# `is not True` 判失配）。
# v6（2026-10-09，BL-232 阶段 3）：dump 增加 `dumper_version` 与被测 App 自身的激活状态
# （`application_state`、`key_window_scene_activation_state`、`key_window_scene_class`、
# `key_window_is_app_window`），交付状态准备据此判断前台是否被遮挡。改名是为了让已加载 v5 映像
# 的常驻进程也加载新映像；宿主另按 `dumper_version` 拒绝过期构建（源码加字段而 dylib 未重编时
# 不静默放行）。
# v7：节点 enabled 输出 JSON 布尔值，满足 checkpoint Schema。
# v8：dump 增加 variant_observation（只读可见文本与定位权限状态）。
# v9：节点增加 hittable 布尔值及（不可点击时）occluded_reason，基于 bounds/visibility/clipping、
# accessibilityViewIsModal 兄弟遮挡、更高层 UIWindow z-order 与同窗 hitTest:withEvent: 标注。
GUEST_DYLIB = "/var/jb/tmp/ax_dump-v9.dylib"
# dumper_version 下限：低于它或缺失表示 guest 上加载的是旧构建。
DUMPER_MIN_VERSION = 9
GUEST_DUMP = "/tmp/autophone_ax.json"
# 拉取 dump 时按 request_id 匹配的轮询上限与间隔（settle_s 之后开始计）。
DUMP_MATCH_TIMEOUT_S = 4.0
DUMP_MATCH_POLL_S = 0.25
DUMP_NOTIFICATION = "com.autophone.ax.dump"
DYLIB_SRC = os.environ.get("AUTOPHONE_AX_DYLIB") or str(CODE_ROOT / "support" / "ax_dump.dylib")

# frida 的应用枚举只列 LaunchServices 登记的用户 App；系统进程（SpringBoard 等）只在
# 进程枚举里以进程名出现。按 bundle id 找不到时按这张表回退到进程名（BL-154，2026-09-20
# 活机实测：三种 scope 的 enumerate_applications 都不含 com.apple.springboard）。
# BL-232 阶段 3（2026-10-09）起平台正式路径不再对 SpringBoard 建 AX 会话（交付状态准备的
# 系统弹窗检测改读被测 App 自身的激活状态）；本表与 dylib 的 arm64e 切片保留给人工工具
# （评估文档 §七第 8 项）。
SYSTEM_PROCESS_NAMES = {
    "com.apple.springboard": "SpringBoard",
}


def resolve_process_pid(dev, app: str) -> int:
    """先按 bundle id 在应用枚举里找在跑的 pid，找不到再按系统进程名在进程枚举里找。

    返回 0 表示没找到。应用枚举优先：同名进程可能有多个（App 扩展），bundle id 才是身份。
    """
    pid = next((a.pid for a in dev.enumerate_applications()
                if a.identifier == app and a.pid), 0)
    if pid:
        return int(pid)
    name = SYSTEM_PROCESS_NAMES.get(app)
    if not name:
        return 0
    matches = [p.pid for p in dev.enumerate_processes() if p.name == name and p.pid]
    if len(matches) != 1:
        return 0
    return int(matches[0])


# Inject + trigger only; native calls, no ObjC bridge. Module.getGlobalExportByName
# is the Frida-17 replacement for the removed static Module.getExportByName.
_AGENT_JS = """
rpc.exports = {
  load: function (p) {
    try { Module.load(p); return { ok: true }; }
    catch (e) { return { ok: false, error: e.message }; }
  },
  prepare: function (id) {
    var set = new NativeFunction(Module.getGlobalExportByName('setenv'), 'int', ['pointer', 'pointer', 'int']);
    return set(Memory.allocUtf8String('AUTOPHONE_AX_REQUEST_ID'), Memory.allocUtf8String(id), 1);
  },
  post: function (n) {
    var f = new NativeFunction(
      Module.getGlobalExportByName('notify_post'), 'int', ['pointer']);
    return f(Memory.allocUtf8String(n));
  }
};
"""


AX_BACKEND_UNSUPPORTED_CODE = "ax_backend_unsupported"
SUPPORTED_AX_BACKENDS: frozenset[str] = frozenset({"frida_inproc", "guest_axruntime"})


class AxUnavailable(RuntimeError):
    """Raised when the AX session cannot be established (no frida, no tunnel,
    app not running, injection rejected). Callers fail closed."""


class UnsupportedAxBackendError(AxUnavailable):
    """Raised fail-closed when an unsupported/unimplemented ``ax_backend`` is requested."""

    code: str = AX_BACKEND_UNSUPPORTED_CODE

    def __init__(self, ax_backend: str, detail: str | None = None) -> None:
        self.ax_backend = str(ax_backend)
        message = (
            f"{self.code}: ax_backend {self.ax_backend!r} is not supported"
            if detail is None
            else f"{self.code}: {detail}"
        )
        super().__init__(message)


def _guarded_ax(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        if method.__name__ == "close":
            if getattr(self, "ax_backend", DEFAULT_AX_BACKEND) == "guest_axruntime":
                return method(self, *args, **kwargs)
            if getattr(self, "_lifetime", None) is not None and self._lifetime.closed:
                self._session = self._script = None
                self._opened_live = False
                return
        with self._operation():
            return method(self, *args, **kwargs)
    return call


class AxSession:
    """Live AX-dump provider. Construct once per app launch, `open()`, then
    `refresh()` per tap_ax; `close()` (or use as a context manager) at the end.

    Supports both ``frida_inproc`` (Frida-injected ``ax_dump.dylib``) and
    ``guest_axruntime`` (out-of-process ``guest_ax_probe`` binary via UDS shell).
    """

    def __init__(self, app: str, *, host: str | None = None,
                 sock_path: str | None = None, out: str = "/tmp/ax_pulled.json",
                 push: bool = True, settle_s: float = 0.8,
                 detach_timeout_s: float = 2.0, request_fn=None,
                 attach_timeout_s: float | None = None,
                 borrowed_lifetime: FridaLifetime | None = None,
                 ax_backend: AxBackend | None = None,
                 target_pid: int | None = None,
                 probe_path: str | Path | None = None,
                 max_retries: int = 5,
                 retry_delay_ms: int = 150,
                 hit_test_nodes: bool = False) -> None:
        resolved_backend: str = (
            ("frida_inproc" if borrowed_lifetime is not None else DEFAULT_AX_BACKEND)
            if ax_backend is None
            else ax_backend
        )
        if resolved_backend not in SUPPORTED_AX_BACKENDS:
            raise UnsupportedAxBackendError(
                str(resolved_backend),
                f"ax_backend {resolved_backend!r} is not supported; "
                f"refusing to fall back to {DEFAULT_AX_BACKEND!r}",
            )
        if resolved_backend == "guest_axruntime" and borrowed_lifetime is not None:
            raise ValueError(
                "borrowed_lifetime requires ax_backend='frida_inproc' "
                "(cannot combine with ax_backend='guest_axruntime')"
            )
        self._stage_guard = StageGuard.current()
        self._request_fn = request_fn
        self.app = app
        self.ax_backend: AxBackend = cast(AxBackend, resolved_backend)
        self._borrowed_lifetime = borrowed_lifetime
        if self.ax_backend == "guest_axruntime":
            self.host = None
        else:
            self.host = borrowed_lifetime.host if borrowed_lifetime is not None else resolve_frida_host(host)
        self.sock_path = sock_path or resolve_sock()
        self.out = out
        self.push = push
        self.settle_s = settle_s
        self.detach_timeout_s = max(0.0, detach_timeout_s)
        # None = frida attach 不设上限（原行为）。给定时 attach 在设备线程中执行，超时抛
        # AxUnavailable；迟到完成的 session 由该线程自行 detach，不留在本对象上。
        self.attach_timeout_s = None if attach_timeout_s is None else max(0.0, float(attach_timeout_s))
        self._target_pid = target_pid
        self._probe_path = probe_path
        self._max_retries = max(0, int(max_retries))
        self._retry_delay_ms = max(10, int(retry_delay_ms))
        self._hit_test_nodes = bool(hit_test_nodes)
        self._guest_runner = None
        self._session = None
        self._script = None
        self._lifetime = None
        self._opened_live = False
        self._last_captured_at_ms = 0

    @contextmanager
    def _operation(self):
        guard = self._stage_guard or StageGuard.for_device_call()
        if guard is None:
            yield
            return
        if self.ax_backend == "guest_axruntime":
            with guard.operation(OperationClass.SHELL, socket_path=self.sock_path):
                self._stage_guard = guard
                yield
            return
        with guard.operation(OperationClass.FRIDA, frida_endpoint=self.host,
                             socket_path=self.sock_path):
            self._stage_guard = guard
            yield

    # ── lifecycle ────────────────────────────────────────────────────────────
    @_guarded_ax
    def open(self) -> "AxSession":
        if self.ax_backend == "guest_axruntime":
            from autophone.device.guest_ax_probe import GuestAxProbeRunner

            if self._guest_runner is None:
                self._guest_runner = GuestAxProbeRunner(
                    sock_path=self.sock_path,
                    request_fn=self._request_fn,
                    probe_path=self._probe_path,
                )
            if self.push:
                self._guest_runner.ensure_deployed()
            self._guest_runner.fetch_capabilities_screen()
            self._opened_live = True
            return self
        if self._borrowed_lifetime is None:
            try:
                import frida
            except ModuleNotFoundError as exc:
                raise AxUnavailable("frida not installed (uv sync --group frida)") from exc
        elif self._borrowed_lifetime.closed or self._borrowed_lifetime.closing:
            raise AxUnavailable("borrowed capture lifetime unavailable")

        if self.push:
            if not Path(DYLIB_SRC).exists():
                raise AxUnavailable(f"missing {DYLIB_SRC}; set AUTOPHONE_AX_DYLIB to a compiled AX dump dylib")
            r, _ = (self._request_fn or request)({"t": "file_put", "path": GUEST_DYLIB,
                            "load": DYLIB_SRC, "perm": "755"},
                           sock_path=self.sock_path, timeout=30)
            if not r.get("ok"):
                raise AxUnavailable(f"file_put dylib failed: {r}")

        if self._session is not None:
            # 上一次 open 在 attach 之后失败，或调用方未 close 就再次 open：先 detach 旧会话，
            # 不让新 attach 覆盖它。
            self.close()
        try:
            if self._borrowed_lifetime is not None:
                self._lifetime = self._borrowed_lifetime
                self._session = self._lifetime.session
            else:
                dev = frida.get_device_manager().add_remote_device(self.host)
                pid = resolve_process_pid(dev, self.app)
                if not pid:
                    raise AxUnavailable(f"{self.app} not running on {self.host}")
                self._session = self._attach(dev, pid)
                if self._stage_guard is not None:
                    self._lifetime = FridaLifetime(self._session, host=self.host)
            self._script = self._session.create_script(_AGENT_JS)
            if self._lifetime is not None:
                self._lifetime.add_script(self._script)
            self._script.load()
        except AxUnavailable:
            raise
        except Exception as exc:  # frida transport / attach failures
            raise AxUnavailable(f"frida attach to {self.app} failed: {exc}") from exc

        exports = (self._borrowed_lifetime.exports(self._script.exports_sync)
                   if self._borrowed_lifetime is not None else self._script.exports_sync)
        loaded = exports.load(GUEST_DYLIB)
        if not loaded.get("ok"):
            raise AxUnavailable(f"Module.load failed: {loaded.get('error')}")
        self._opened_live = True
        # The constructor activates accessibility; it settles a runloop turn later.
        time.sleep(0.5)
        return self

    def _attach(self, dev, pid: int):
        """frida attach；``attach_timeout_s`` 给定时有界等待。"""
        if self.attach_timeout_s is None:
            return dev.attach(pid)
        lock = Lock()
        finished = Event()
        outcome: dict = {}

        def attach() -> None:
            try:
                session = dev.attach(pid)
            except BaseException as exc:  # noqa: BLE001 - 交回调用线程判定
                with lock:
                    outcome["error"] = exc
                finished.set()
                return
            with lock:
                late = outcome.get("abandoned", False)
                if not late:
                    outcome["session"] = session
            finished.set()
            if late:
                try:
                    session.detach()
                except Exception:  # noqa: BLE001 — 迟到 session 的收尾是尽力而为
                    pass

        start_device_thread(attach, name="autophone-ax-attach",
                            frida_endpoint=self.host, socket_path=self.sock_path)
        finished.wait(self.attach_timeout_s)
        with lock:
            if "session" in outcome:
                return outcome["session"]
            if "error" in outcome:
                raise outcome["error"]
            outcome["abandoned"] = True
        raise AxUnavailable(
            f"frida attach to {self.app} (pid {pid}) did not finish within {self.attach_timeout_s:g}s")

    def _refresh_guest_axruntime(
        self,
        request_id: str | None = None,
        *,
        strict_unique_keys: bool = False,
    ) -> tuple[dict, AxDump]:
        from autophone.device.guest_ax_probe import normalize_probe_to_ax_v1
        from autophone.util import write_json_atomic

        if self._guest_runner is None:
            raise AxUnavailable("session not open")
        req_id = request_id or uuid.uuid4().hex
        app_row: dict | None = None
        if self._target_pid is not None and self._target_pid > 0:
            pid = int(self._target_pid)
            try:
                _, app_row = self._guest_runner.resolve_running_app(self.app)
            except AxUnavailable:
                app_row = None
        else:
            pid, app_row = self._guest_runner.resolve_running_app(self.app)

        probe_raw = self._guest_runner.run_probe(
            pid=pid,
            request_id=req_id,
            max_retries=self._max_retries,
            retry_delay_ms=self._retry_delay_ms,
            hit_test_nodes=self._hit_test_nodes,
            strict_unique_keys=strict_unique_keys,
        )
        short_ver, bundle_ver = self._guest_runner.resolve_bundle_versions(
            self.app, pid, probe_raw, app_row
        )
        cap_screen = self._guest_runner.fetch_capabilities_screen()
        normalized = normalize_probe_to_ax_v1(
            probe_raw,
            app=self.app,
            request_id=req_id,
            pid=pid,
            short_version=short_ver,
            bundle_version=bundle_ver,
            capabilities_screen=cap_screen,
        )
        ts = int(normalized["captured_at_ms"])
        if ts <= self._last_captured_at_ms:
            ts = self._last_captured_at_ms + 1
            normalized["captured_at_ms"] = ts
        dump = parse_dump(normalized, ax_backend=self.ax_backend)
        self._last_captured_at_ms = ts
        if self.out:
            out_path = Path(self.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(out_path, normalized)
        return normalized, dump

    @_guarded_ax
    def refresh(self, request_id: str | None = None) -> AxDump:
        """Trigger a fresh dump and pull+parse it. Raises AxUnavailable on a
        transport failure so tap_ax can fall back / fail closed.

        v5 起 dumper 是 one-shot：只在本进程收到 prepare(request_id) 后才写一次。refresh 必须先
        登记请求，否则 dumper 跳过、host 读到别的进程留下的旧文件——captured_at_ms 相对本会话的
        判据拦不住这种「别人刚写的」文件。`snapshot()` 传入自己的 request_id，避免二次登记把
        dumper 里的待处理请求换掉。拉取按 request_id 匹配轮询，不再只靠一次固定 settle。
        """
        if self.ax_backend == "guest_axruntime":
            _, dump = self._refresh_guest_axruntime(request_id=request_id)
            return dump
        if self._script is None:
            raise AxUnavailable("session not open")
        exports = self._lifetime.exports(self._script.exports_sync) if self._lifetime else self._script.exports_sync
        request_id = request_id or uuid.uuid4().hex
        if exports.prepare(request_id) != 0:
            raise AxUnavailable("AX dump request registration failed")
        exports.post(DUMP_NOTIFICATION)
        deadline = time.monotonic() + max(self.settle_s, 0.0) + DUMP_MATCH_TIMEOUT_S
        time.sleep(self.settle_s)
        raw: dict = {}
        while True:
            r, _ = (self._request_fn or request)({"t": "file_get", "path": GUEST_DUMP, "save": self.out},
                           sock_path=self.sock_path, timeout=20)
            if not r.get("ok"):
                raise AxUnavailable(f"file_get dump failed: {r}")
            try:
                raw = json.loads(Path(self.out).read_text(encoding="utf-8"))
            except Exception as exc:  # malformed/stale output must not direct a tap
                raise AxUnavailable(f"read AX dump failed: {exc}") from exc
            if raw.get("request_id") == request_id and raw.get("app") == self.app:
                break
            if time.monotonic() >= deadline:
                raise AxUnavailable(
                    f"AX dump identity mismatch: app={raw.get('app')!r} request_id={raw.get('request_id')!r}")
            time.sleep(DUMP_MATCH_POLL_S)
        try:
            captured_at_ms = int(raw.get("captured_at_ms") or 0)
        except (TypeError, ValueError) as exc:
            raise AxUnavailable(f"read AX dump failed: {exc}") from exc
        if captured_at_ms <= self._last_captured_at_ms:
            raise AxUnavailable(
                f"stale AX dump: captured_at_ms={captured_at_ms} "
                f"last={self._last_captured_at_ms}")
        raw_dumper_version = raw.get("dumper_version")
        if raw_dumper_version is None:
            if self._opened_live:
                raise AxUnavailable(
                    f"missing AX dumper_version (want >= {DUMPER_MIN_VERSION}); "
                    f"rebuild {DYLIB_SRC} and reload {GUEST_DYLIB}"
                )
        elif (
            isinstance(raw_dumper_version, bool)
            or not isinstance(raw_dumper_version, int)
            or raw_dumper_version < DUMPER_MIN_VERSION
        ):
            raise AxUnavailable(
                f"stale AX dumper_version={raw_dumper_version!r} < {DUMPER_MIN_VERSION}; "
                f"rebuild {DYLIB_SRC} and reload {GUEST_DYLIB}"
            )
        self._last_captured_at_ms = captured_at_ms
        if raw.get("ax_backend") not in (None, self.ax_backend):
            raise AxUnavailable(
                f"AX dump ax_backend mismatch: dump={raw.get('ax_backend')!r} "
                f"session={self.ax_backend!r}"
            )
        return parse_dump(raw, ax_backend=self.ax_backend)

    @_guarded_ax
    def snapshot(self) -> dict:
        """Capture evidence with a request nonce; legacy/stale dumps are rejected."""
        if self.ax_backend == "guest_axruntime":
            raw, _ = self._refresh_guest_axruntime(strict_unique_keys=True)
            if raw.get("foreground_active") is not True:
                reason = raw.get("foreground_occlusion_reason") or "not_foreground"
                raise AxUnavailable(f"guest_axruntime: app/scene not foreground active ({reason})")
            if not raw.get("nodes"):
                raise AxUnavailable("guest_axruntime: empty AX snapshot")
            if not raw.get("short_version") or not raw.get("bundle_version"):
                raise AxUnavailable(
                    "guest_axruntime: missing short_version/bundle_version for AX checkpoint"
                )
            if (raw.get("evidence_protocol") != 1 or raw.get("complete") is not True
                    or raw.get("truncated") is not False):
                raise AxUnavailable("AX evidence identity or completeness mismatch")
            return raw
        if self._script is None:
            raise AxUnavailable("session not open")
        request_id = uuid.uuid4().hex
        exports = self._lifetime.exports(self._script.exports_sync) if self._lifetime else self._script.exports_sync
        self.refresh(request_id=request_id)
        def unique_pairs(pairs):
            values = {}
            for key, value in pairs:
                if key in values:
                    raise AxUnavailable("ambiguous AX JSON keys")
                values[key] = value
            return values
        raw = json.loads(Path(self.out).read_bytes(), object_pairs_hook=unique_pairs)
        if (raw.get("request_id") != request_id or raw.get("app") != self.app
                or raw.get("evidence_protocol") != 1 or raw.get("complete") is not True
                or raw.get("truncated") is not False):
            raise AxUnavailable("AX evidence identity or completeness mismatch")
        if raw.get("ax_backend") not in (None, self.ax_backend):
            raise AxUnavailable(
                f"AX evidence ax_backend mismatch: dump={raw.get('ax_backend')!r} "
                f"session={self.ax_backend!r}"
            )
        raw["ax_backend"] = self.ax_backend
        return raw

    @_guarded_ax
    def reopen(self) -> "AxSession":
        """Replace a long-lived attachment whose dump no longer tracks overlays.

        Some app transitions leave the existing dumper session producing a
        valid-but-old accessibility tree.  Callers use this only as a bounded
        recovery after an expected overlay is absent; it is not a polling mode.
        """
        self.close()
        self._last_captured_at_ms = 0
        return self.open()

    @_guarded_ax
    def close(self) -> None:
        self._opened_live = False
        if self.ax_backend == "guest_axruntime":
            self._guest_runner = None
            return
        if self._borrowed_lifetime is not None:
            # Capture owns every script until owned-process absence is confirmed.
            self._session = self._script = None
            return
        if self._lifetime is not None:
            lifetime = self._lifetime
            lifetime.closing = True
            finished = Event()
            def dispose():
                try:
                    lifetime.dispose()
                except Exception:
                    pass  # StageGuard retains the handle and verifies it again.
                finally:
                    finished.set()
            start_device_thread(dispose, name="autophone-ax-dispose", frida_endpoint=self.host)
            finished.wait(self.detach_timeout_s)
            self._session = self._script = None
            return
        session = self._session
        # A remote frida-server can acknowledge AX RPCs yet block forever while
        # detaching.  Teardown must never turn a read-only discovery command into
        # an unbounded hang.  The daemon thread may finish later; dropping the
        # references here makes this session unusable either way. Managed calls
        # remain in-flight until detach returns, so a stage transition still waits.
        self._session = None
        self._script = None
        if session is None:
            return

        finished = Event()

        def detach() -> None:
            try:
                session.detach()
            except Exception:  # noqa: BLE001 — teardown is best-effort
                pass
            finally:
                finished.set()

        start_device_thread(detach, name="autophone-ax-detach",
                            frida_endpoint=self.host, socket_path=self.sock_path)
        finished.wait(self.detach_timeout_s)

    def __enter__(self) -> "AxSession":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()
