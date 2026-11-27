#!/usr/bin/env python3
# coding=utf8
"""M0b-1 / M0b-2 — ObjC bridge bundle + spawn-gating 验证（host 侧，可复跑）。

M0a（`autophone/device/frida_contract_probe.py`）证明传输契约成立，但留了两个缺口：
  - M0b-1：裸 `create_script` 里 `ObjC` 不是全局（frida 17 行为）→ 抓取层解不了
    `ObjC.classes.NSURLSession`。补法：frida-compile 把 `frida-objc-bridge` 打进脚本。
  - M0b-2：attach **已运行**的部分 App 会 `probing dyld` 失败（python/CLI 一致，
    重启换 pid 仍失败 → 进程态固有，非实例不新鲜）。绕法：**spawn-gating**。

本探针把这两件事钉成**可复跑**的源内验证（不是文档里的片段）：
  spawn(bid) → attach → load 编译后的 bundle → resume → 收 objc_bundled + rpc.ping。
成功即证明「python-loaded 脚本能解 ObjC.classes」且「spawn-gating 能进被测 App」。

`spawn_gate()` 是 M0b-3 / M1 复用的启动模型入口：抓取层也得走 spawn 拉起 app、
整场存活、最后 detach，**不能 attach 一个已在前台的实例**。

依赖：Frida/OCR 依赖组（`uv sync --group frida`）。先 build bundle：
    cd autophone/observation/frida/agent && npm install
    ../../../.venv/bin/frida-compile src/objc_bridge_probe.js -o _build/objc_bridge_probe.js
跑：
    uv run --group frida python -m autophone.observation.frida.objc_bundle_probe

环境变量：
    AP_FRIDA_HOST   默认 127.0.0.1:27042
    AP_BUNDLE_ID    要探测的 App Bundle ID
    AP_AGENT_JS     默认 autophone/observation/frida/agent/_build/objc_bridge_probe.js
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from threading import Event, Timer

from autophone.device.stage_threads import start_device_thread

from autophone.device.frida_script import script_exports
from autophone.device.frida_endpoint import resolve_frida_host
from autophone.device.stage_guard import StageGuard, OperationClass, guarded_device_operation
from autophone.device.frida_lifetime import FridaLifetime

FRIDA_HOST = resolve_frida_host()
_DEFAULT_AGENT = os.path.join(
    os.path.dirname(__file__), "agent", "_build", "objc_bridge_probe.js"
)
AGENT_JS = os.environ.get("AP_AGENT_JS", _DEFAULT_AGENT)
logger = logging.getLogger(__name__)
DEFAULT_DETACH_TIMEOUT_S = 2.0
CAPTURE_HOOK_TIMEOUT_S = 30.0


# 单一定义在设备层（`autophone.device.frida_script`）；这里保留 `_exports` 这个名字
# 是为了不动本模块既有的四个调用方与它们的测试替身。
_exports = script_exports


def _install_capture_hooks(exports):
    """Bound the native RPC wait; resource release remains the caller's duty."""
    fn = getattr(exports, "install_capture_hooks", None) or getattr(
        exports, "installCaptureHooks", None)
    if fn is None:
        return
    import frida

    factory = getattr(frida, "Cancellable", None)
    if factory is None:  # Injected transports without the optional native SDK.
        return fn()
    cancellable = factory()
    expired = Event()

    def cancel_wait():
        expired.set()
        cancellable.cancel()

    timer = Timer(CAPTURE_HOOK_TIMEOUT_S, cancel_wait)
    timer.daemon = True
    logger.info("Installing capture hooks (timeout %.1fs)", CAPTURE_HOOK_TIMEOUT_S)
    with cancellable:
        timer.start()
        try:
            result = fn()
        except Exception as exc:
            if expired.is_set():
                raise TimeoutError(
                    f"install_capture_hooks timed out after {CAPTURE_HOOK_TIMEOUT_S:.1f}s"
                ) from exc
            raise
        finally:
            timer.cancel()
            timer.join()
    if expired.is_set():
        raise TimeoutError(f"install_capture_hooks timed out after {CAPTURE_HOOK_TIMEOUT_S:.1f}s")
    logger.info("Capture hooks installed")
    return result


def _detach_bounded(session, *, timeout_s: float) -> bool:
    """Detach without allowing a dead remote Frida transport to hang the run."""
    done = Event()

    def worker() -> None:
        try:
            session.detach()
        except Exception:  # noqa: BLE001 - caller still kills the owned spawn
            pass
        finally:
            done.set()

    start_device_thread(target=worker, name="frida-spawn-detach")
    done.wait(max(0.0, float(timeout_s)))
    return done.is_set()


@contextmanager
def spawn_gate(device, bundle_id: str, agent_js: str, on_message, *, after_load=None,
               install_capture: bool = True,
               detach_timeout_s: float = DEFAULT_DETACH_TIMEOUT_S,
               frida_host: str | None = None, on_lifetime=None, on_spawn=None, before_close=None):
    """spawn-gating 启动模型（M0b-3 / M1 复用）。

    attach 运行态的部分 App 会 `probing dyld` 失败，必须在 launch 时挂上：
    spawn → attach → load(编译后 bundle) → resume。退出时 detach + kill 干净。

    `after_load(exports)`（可选）在 **load 之后、resume 之前**跑——接口场景拦截靠它把
    规则下发到位并自检 hook（§六「规则先于 resume」：resume 后再配规则、冷启请求已漏）。
    它抛异常即中止启动：进程仍挂起、finally 直接 detach+kill 那个从未 resume 的 pid，
    fail-closed 是**结构性**的（挂起态 App 一个请求都没发，§3.1）。

    yield (pid, script, exports)。bundle 必须已经 frida-compile 过，否则脚本里没有 ObjC。
    """
    from autophone.observation.frida.agent_bundle import ensure_agent_bundle

    agent_js = str(ensure_agent_bundle(agent_js))
    guard = StageGuard.for_device_call()
    if guard is not None:
        with _managed_spawn_gate(
            device, bundle_id, agent_js, on_message, after_load=after_load,
            install_capture=install_capture, frida_host=resolve_frida_host(frida_host),
            on_lifetime=on_lifetime, on_spawn=on_spawn, before_close=before_close,
        ) as result:
            yield result
        return
    pid = device.spawn([bundle_id])
    session = None
    lifetime = None
    try:
        if on_spawn is not None:
            on_spawn(pid)
        session = device.attach(pid)
        if before_close is not None:
            lifetime = FridaLifetime(session, host=resolve_frida_host(frida_host), before_dispose=before_close)
            if on_lifetime is not None:
                on_lifetime(lifetime)
        with open(agent_js, "r", encoding="utf-8") as f:
            js = f.read()
        script = session.create_script(js)
        if lifetime is not None:
            lifetime.add_script(script)
        script.on("message", on_message)
        script.load()
        if after_load is not None:
            after_load(_exports(script))   # 规则先于 resume：抛错 → finally kill 挂起 pid，永不 resume
        device.resume(pid)
        # net_tap 挂起态不敢在 load 期装抓取 hook（全类枚举崩挂起进程）。resume 后进程已活，安全安装。
        # install_capture=False（纯拦截短路场景）跳过——拦截不需要抓取 hook。非 net_tap agent 无此 rpc，静默跳过。
        if install_capture:
            try:
                _install_capture_hooks(_exports(script))
            except TimeoutError:
                raise
            except Exception:  # noqa: BLE001 其它 agent 无此 export / 已装幂等
                pass
        yield pid, script, _exports(script)
    finally:
        if before_close is not None:
            before_close()
        if lifetime is not None:
            lifetime.dispose()
        elif session is not None:
            detached = _detach_bounded(session, timeout_s=detach_timeout_s)
            if not detached:
                logger.warning(
                    "Frida spawn session detach timed out after %.1fs; killing owned pid=%s",
                    detach_timeout_s,
                    pid,
                )
        if before_close is None:
            try:
                device.kill(pid)
            except Exception:  # noqa: BLE001
                pass


@contextmanager
def _managed_spawn_gate(device, bundle_id, agent_js, on_message, *, after_load,
                        install_capture, frida_host, on_lifetime, on_spawn=None, before_close=None):
    guard = StageGuard.for_device_call()
    lifetime = None
    killed = False
    with guard.operation(OperationClass.FRIDA, frida_endpoint=frida_host):
        pid = device.spawn([bundle_id])

        def kill_owned_process():
            nonlocal killed
            if killed:
                return
            try:
                if before_close is not None:
                    before_close()
                else:
                    device.kill(pid)
            except Exception:
                if before_close is not None:
                    raise
                # A failed transport call alone cannot confirm process exit.
                if any(p.pid == pid for p in device.enumerate_processes()):
                    raise
            killed = True
            release_process()

        release_process = guard.register_resource(kill_owned_process)
    try:
        with guard.operation(OperationClass.FRIDA, frida_endpoint=frida_host):
            if on_spawn is not None:
                on_spawn(pid)
            session = device.attach(pid)
            lifetime = FridaLifetime(session, host=frida_host, before_dispose=before_close)
            if on_lifetime is not None:
                on_lifetime(lifetime)
            with open(agent_js, encoding="utf-8") as source:
                script = session.create_script(source.read())
            lifetime.add_script(script)
            script.on("message", lambda message, data:
                      lifetime.accept_event(lambda: on_message(message, data)))
            script.load()
            exports = lifetime.exports(_exports(script))
            if after_load is not None:
                after_load(exports)
            device.resume(pid)
            if install_capture:
                _install_capture_hooks(exports)
        yield pid, script, exports
    finally:
        if not killed or (lifetime is not None and not lifetime.closed):
            with guard.operation(OperationClass.FRIDA, frida_endpoint=frida_host):
                try:
                    if before_close is not None:
                        before_close()
                    if lifetime is not None:
                        lifetime.dispose()
                finally:
                    kill_owned_process()


@guarded_device_operation(OperationClass.FRIDA, identity=lambda a: {"frida_endpoint": resolve_frida_host()})
def require_bundle_id(bundle_id: str | None = None) -> str:
    value = (bundle_id or os.environ.get("AP_BUNDLE_ID") or "").strip()
    if not value:
        raise ValueError("set AP_BUNDLE_ID or pass an explicit App Bundle ID")
    return value


def probe(bundle_id: str | None = None) -> dict:
    bundle_id = require_bundle_id(bundle_id)
    """spawn-gate 被测 App、加载编译后 bundle、验 ObjC 解类 + RPC。返回矩阵。"""
    try:
        import frida
    except ImportError as e:
        return {"frida_python": False,
                "error": f"import frida failed: {e}（需用 uv run --group frida 跑）"}

    matrix: dict = {
        "frida_python": True,
        "agent_js": AGENT_JS,
        "spawned": False,
        "session": False,
        "objc_available": None,
        "objc_nsurlsession": None,
        "objc_sample": None,
        "hook_primitives": None,
        "rpc_ping": False,
    }
    msgs: list[dict] = []

    def on_message(message, data):
        msgs.append(message)
        if message.get("type") == "error":
            matrix["js_error"] = message.get("description")

    device = frida.get_device_manager().add_remote_device(resolve_frida_host())

    try:
        with spawn_gate(device, bundle_id, AGENT_JS, on_message) as (pid, _script, ex):
            matrix["spawned"] = True
            matrix["session"] = True
            matrix["pid"] = pid
            time.sleep(1.5)  # 收 load 时的 objc_bundled / loaded
            try:
                matrix["rpc_ping"] = (ex.ping() == "pong")
            except Exception as e:  # noqa: BLE001
                matrix["rpc_ping_error"] = repr(e)
            for m in msgs:
                if m.get("type") != "send":
                    continue
                p = m.get("payload") or {}
                if p.get("ev") == "objc_bundled":
                    matrix["objc_available"] = bool(p.get("available"))
                    matrix["objc_nsurlsession"] = bool(p.get("nsurlsession"))
                    matrix["objc_sample"] = p.get("sample")
                    matrix["hook_primitives"] = p.get("primitives")
    except FileNotFoundError as e:
        matrix["error"] = str(e)
    except Exception as e:  # noqa: BLE001 — spawn/attach 失败本身是证据
        matrix["spawn_error"] = repr(e)

    return matrix


def _verdict(m: dict) -> str:
    if m.get("error"):
        return "BLOCKED — 前置缺失（见 error，多半是 bundle 没 build）"
    if m.get("spawn_error"):
        return "BLOCKED — spawn-gating 失败（见 spawn_error）；app 没装/隧道断/bid 错"
    if m.get("objc_available") and m.get("objc_nsurlsession") and m.get("rpc_ping"):
        return ("GREEN — bundle 后 python-loaded 脚本可解 ObjC.classes.NSURLSession，"
                "spawn-gating 进被测 App 成立 → 抓取主路打通（§4.6 M0b-1/2）")
    if m.get("session") and not m.get("objc_available"):
        return ("DEGRADED — 进了 app 但 ObjC 仍不可用：bundle 没编进 frida-objc-bridge？"
                "检查 _build 是否 frida-compile 过")
    return "DEGRADED — 见矩阵"


def main() -> int:
    try:
        bundle_id = require_bundle_id()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    m = probe(bundle_id)
    verdict = _verdict(m)

    print("=" * 60)
    print("M0b-1/M0b-2 — ObjC bundle + spawn-gating 能力矩阵")
    print(f"  host={FRIDA_HOST}  bid={bundle_id}")
    print(f"  agent={m.get('agent_js')}")
    print("-" * 60)
    rows = [
        ("frida-python import", "frida_python"),
        ("spawn-gating attach", "session"),
        ("ObjC.available（bundle 后）", "objc_available"),
        ("ObjC.classes.NSURLSession 解类", "objc_nsurlsession"),
        ("rpc.ping（python-loaded）", "rpc_ping"),
    ]
    for label, key in rows:
        v = m.get(key)
        mark = "✓" if v else ("?" if v is None else "✗")
        print(f"  [{mark}] {label}")
    if m.get("objc_sample"):
        print(f"      objc_sample: {m['objc_sample']}")
    if m.get("hook_primitives") is not None:
        present = [k for k, v in m["hook_primitives"].items() if v]
        print(f"      hook 原语在场: {', '.join(present) or '（无）'}")
    for key in m:
        if key.endswith("_error"):
            print(f"      {key}: {m[key]}")
    if m.get("error"):
        print(f"  !! {m['error']}")
    print("-" * 60)
    print(f"结论：{verdict}")
    print("=" * 60)
    print("MATRIX_JSON " + json.dumps(m, ensure_ascii=False))

    ok = bool(m.get("objc_available") and m.get("objc_nsurlsession")
              and m.get("rpc_ping"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
