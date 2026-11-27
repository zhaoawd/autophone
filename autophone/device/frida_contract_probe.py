#!/usr/bin/env python3
# coding=utf8
"""M0a — Frida 传输契约探针（host 侧）。

Phase 4 的整套抓取接法（§5 传输层 / §6 关联层）都押在「frida-python 长连」上：
用 `script.on('message')` 收事件、`send(payload, data)` 走 body 二进制旁路、
`rpc.exports.mark()` 让 phase2 runner 打点。但 phase3 的现实证据是当前工具走 CLI
子进程，理由是 raw `create_script` 可能没有 ObjC bridge。这个矛盾不实测消掉，
后面三层全是凭想象搭楼。

本探针**边界极窄**：只连一次常驻 session，验四件事，跑完输出能力矩阵就收工。
**不 hook NSURLSession、不碰 runner、不碰 oracle。** 成功或失败都是有价值证据。

四件事：
  1. 常驻 session  —— frida-python 能 attach 到运行中的 app 并保持会话
  2. ObjC.available —— 脚本里 ObjC bridge 真能用（phase3 的 CLI-only 结论是否成立）
  3. rpc.exports    —— host 能调进脚本（ping 往返 + mark 触发 send）
  4. send data 旁路 —— payload + 二进制 data 能完整收到

依赖：需安装 Frida/OCR 依赖组：
    uv sync --group frida
    uv run --group frida python -m autophone.device.frida_contract_probe
前置：guest re.frida.server 在跑，且 host 已建隧道 `ssh -L 27042:127.0.0.1:27042 ...`。

环境变量（与 phase3 一致）：
    AP_FRIDA_HOST  默认 127.0.0.1:27042
    AP_APP_NAME    目标 App 的显示名或进程名子串
"""
from __future__ import annotations

import json
import os
import sys
import time
from autophone.device.frida_endpoint import resolve_frida_host
from autophone.device.stage_guard import OperationClass, guarded_device_operation

FRIDA_HOST = resolve_frida_host()
JS_PATH = os.path.join(os.path.dirname(__file__), "frida_contract_probe.js")


def _exports(script):
    """frida >=16 用 exports_sync（同步），旧版用 exports。两边都兼容。"""
    return getattr(script, "exports_sync", None) or script.exports


def _resolve_pid(device, app_name: str, bundle_id: str | None = None) -> int | None:
    # 给了 bundle_id 时先按 identifier 精确匹配：profile 的 display_name 可能与设备上的
    # App 显示名不一致（2026-09-24 food_ordering_v2：profile「Food Ordering Verification」、
    # 设备「Food AI Verification」），按名称子串匹配会找不到进程。
    if bundle_id:
        for app in device.enumerate_applications():
            if app.identifier == bundle_id and app.pid:
                return app.pid
    # 显示名与 Frida 进程名可能不同；无 bundle_id 时按名称子串匹配进程。
    for p in device.enumerate_processes():
        if app_name in p.name:
            return p.pid
    return None


@guarded_device_operation(OperationClass.FRIDA, identity=lambda a: {"frida_endpoint": a["host"] or resolve_frida_host()})
def probe(*, host: str | None = None, app_name: str | None = None,
          bundle_id: str | None = None) -> dict:
    """Run the contract probe. Returns the capability matrix dict."""
    app_name = (app_name or os.environ.get("AP_APP_NAME") or "").strip()
    bundle_id = (bundle_id or "").strip()
    if not app_name and not bundle_id:
        return {"error": "pass a Bundle ID or set AP_APP_NAME to select an App"}
    # frida-python 本身可不可 import，是契约的第 0 项
    try:
        import frida
    except ImportError as e:
        return {"frida_python": False, "error": f"import frida failed: {e} "
                "（需用 uv run --group frida 跑，见本文件头部）"}

    matrix: dict = {
        "frida_python": True,
        "session": False,
        "objc_available": None,
        "objc_sample": None,
        "hook_primitives": None,
        "rpc_ping": False,
        "rpc_mark": False,
        "send_data": False,
        "session_alive_after_rpc": False,
    }
    msgs: list[tuple[dict, bytes | None]] = []

    def on_message(message, data):
        msgs.append((message, data))

    # ── 连接（常驻 remote device，不是 CLI 一次性会话）──────────────────
    host = host or resolve_frida_host()
    device = frida.get_device_manager().add_remote_device(host)
    pid = _resolve_pid(device, app_name, bundle_id)
    if pid is None:
        matrix["error"] = (f"{app_name} not running over {host} "
                           "（app 没起 / 隧道没通 / frida-server 没跑）")
        return matrix

    # attach/create/load 都可能抛（M0a 实测：部分 App 会在 `probing dyld` 阶段直接失败）——
    # 兜住，失败也归档成机器可读证据，而不是抛出退出、丢掉矩阵/MATRIX_JSON。
    session = None
    from autophone.device.stage_guard import StageGuard
    from autophone.device.frida_lifetime import FridaLifetime
    lifetime = None
    try:
        session = device.attach(pid)
        if StageGuard.current() is not None:
            lifetime = FridaLifetime(session, host=host)
        matrix["session"] = True
        with open(JS_PATH, "r", encoding="utf-8") as f:
            js = f.read()
        script = session.create_script(js)
        if lifetime is not None:
            lifetime.add_script(script)
        script.on("message", (lambda message, data:
                  lifetime.accept_event(lambda: on_message(message, data)))
                  if lifetime is not None else on_message)
        script.load()
    except Exception as e:  # noqa: BLE001 — 失败本身是契约证据
        matrix["attach_error"] = repr(e)
        # attach 成功但 create_script/load 失败时 session 已建，得 detach 干净，
        # 否则 guest 侧留一个挂死的会话。
        if session is not None:
            try:
                lifetime.dispose() if lifetime is not None else session.detach()
            except Exception:  # noqa: BLE001
                pass
        return matrix
    time.sleep(0.4)  # 收 load 时的 objc/loaded 两条 message

    ex = _exports(script)
    if lifetime is not None:
        ex = lifetime.exports(ex)

    # ① ObjC.available（来自 load 时的 objc message）
    for m, _ in msgs:
        p = m.get("payload") or {}
        if p.get("ev") == "objc":
            matrix["objc_available"] = bool(p.get("available"))
            matrix["objc_sample"] = p.get("sample")
            matrix["hook_primitives"] = p.get("primitives")

    # ② rpc.ping 往返
    try:
        matrix["rpc_ping"] = (ex.ping() == "pong")
    except Exception as e:  # noqa: BLE001 — 失败本身是契约结果
        matrix["rpc_ping_error"] = repr(e)

    # ③ rpc.mark 触发 + 从 rpc 里 send() 出来
    before = len(msgs)
    try:
        ex.mark("M0a", "probe")
        time.sleep(0.3)
        matrix["rpc_mark"] = any(
            (m.get("payload") or {}).get("ev") == "mark"
            for m, _ in msgs[before:]
        )
    except Exception as e:  # noqa: BLE001
        matrix["rpc_mark_error"] = repr(e)

    # ④ send(payload, data) 二进制旁路：5 字节完整到达
    before = len(msgs)
    try:
        ex.send_data()  # frida 把 python snake_case 映射到 JS camelCase sendData
        time.sleep(0.3)
        for m, data in msgs[before:]:
            if (m.get("payload") or {}).get("ev") == "probe_data":
                matrix["send_data"] = (data is not None and len(data) == 5
                                       and bytes(data) == bytes([1, 2, 3, 4, 5]))
    except Exception as e:  # noqa: BLE001
        matrix["send_data_error"] = repr(e)

    # 会话在多次 RPC 之后仍活着 = 长连成立（区别于 phase3 的秒级一次性会话）
    try:
        matrix["session_alive_after_rpc"] = (ex.ping() == "pong")
    except Exception as e:  # noqa: BLE001
        matrix["session_alive_after_rpc_error"] = repr(e)

    try:
        lifetime.dispose() if lifetime is not None else session.detach()
    except Exception:  # noqa: BLE001
        pass

    return matrix


def _verdict(m: dict) -> tuple[str, list[str]]:
    """从矩阵推「主路可走 / 需降级」结论 + 降级说明（对接 §5/§6 文档）。"""
    # attach 都没成 → 契约根本没测到，别误报成「能力降级」（如 probing dyld 失败）。
    if m.get("attach_error") or not m.get("session"):
        return ("BLOCKED — attach 失败，契约未测（非机制问题，见 attach_error）；"
                "换可 attach 的实例后重跑", [])
    # 传输契约（§5/§6 押注的）：长连 + RPC + send(data)。ObjC 单列，因它是「需 bundle」而非「不可用」。
    transport = ["frida_python", "session", "rpc_ping", "rpc_mark",
                 "send_data", "session_alive_after_rpc"]
    notes: list[str] = []
    transport_ok = all(m.get(k) for k in transport)
    prims = m.get("hook_primitives") or {}
    interceptor_ok = bool(prims.get("Interceptor"))

    if transport_ok and m.get("objc_available"):
        return "GREEN — 走 send()+RPC 主路（§5 传输层 + §6 marker 关联）", notes
    if transport_ok and interceptor_ok and not m.get("objc_available"):
        notes.append("传输契约（长连/RPC/send-data）全绿 → §5/§6 主路成立，无需退 CLI")
        notes.append("ObjC bridge 不是自动全局（frida 17 行为），但 Interceptor/Module/CModule 在 → "
                     "抓取层 net_tap.js 需 bundle frida-objc-bridge（frida-compile）；这是 M0b 第一步")
        return "GREEN*（传输 OK，抓取层需 bundle ObjC bridge）", notes
    if not m.get("objc_available"):
        notes.append("ObjC bridge 不可用，且 Interceptor 也缺 → 需在 M0b 确认 hook 可行性")
    if not (m.get("rpc_ping") and m.get("rpc_mark")):
        notes.append("RPC 不可用 → §6 marker 降级为纯时间窗归因（runner 调不进脚本）")
    if not m.get("send_data"):
        notes.append("send(data) 旁路不可用 → body 退回行协议 base64（大 body 有截断风险）")
    if not (m.get("frida_python") and m.get("session")):
        notes.append("frida-python 长连不成立 → 整套退回 phase3 式 CLI 子进程")
    return "DEGRADED — 部分能力需降级（见下）", notes


def main() -> int:
    app_name = os.environ.get("AP_APP_NAME", "").strip()
    m = probe(app_name=app_name)
    verdict, notes = _verdict(m)

    print("=" * 60)
    print("M0a — Frida 传输契约能力矩阵")
    print(f"  host={FRIDA_HOST}  app={app_name}")
    print("-" * 60)
    rows = [
        ("frida-python import", "frida_python"),
        ("常驻 session attach", "session"),
        ("ObjC.available", "objc_available"),
        ("rpc.ping 往返", "rpc_ping"),
        ("rpc.mark → send()", "rpc_mark"),
        ("send(payload,data) 旁路", "send_data"),
        ("多次 RPC 后会话仍活", "session_alive_after_rpc"),
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
    for n in notes:
        print(f"  · {n}")
    print("=" * 60)
    # 机器可读一行，便于后续归档
    print("MATRIX_JSON " + json.dumps(m, ensure_ascii=False))

    # 硬失败 → exit 1：连不上（app 没起/隧道断，无 session）或 attach 后 create/load 抛错
    # （session=True 但 attach_error 已记）。只有「会话建成 + 脚本加载成功」才算 0。
    if m.get("attach_error") or not m.get("session"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
