#!/usr/bin/env python3
# coding=utf8
"""M1 — 传输层（host 侧）：长连抓取 + 按 task_ptr 合并 → §五 schema JSONL。

复用 M0b 的 spawn-gating（`objc_bundle_probe.spawn_gate`）拉起目标 App，加载生产版
抓取 agent（`agent/_build/net_tap.js`），整场存活地收 §4.2 的 req/resp 事件：
  - body 走 frida send(payload, data) 二进制旁路，在 on_message 里按 (ev, task_ptr) 收齐；
  - 收尾时按 **task_ptr**（进程内唯一，§4.2 主关联键）把 req 与 resp 合并成单条；
  - resp 不来的孤立 req 也照样落（看得到超时/断网）。

产物：`autophone/observation/frida/runs/<date>/<run_id>/net/capture.jsonl`，一行一条合并后的调用（§五 schema）。
M1 只落 capture，**不进 oracle**。body 落 base64；JSON/文本可解时附 *_body_text 便于人读（gzip 自动解）。

跑（spawn 冷启动 + 人工操作触发业务请求）：
    cd autophone/observation/frida/agent && npm install && FRIDA_COMPILE=../../../.venv/bin/frida-compile npm run build
    AP_SETTLE=30 uv run --group frida python -m autophone.observation.frida.net_capture
环境变量：AP_FRIDA_HOST(127.0.0.1:27042) AP_BUNDLE_ID(必填) AP_SETTLE(20)
          AP_CAPTURE_DELEGATES(1；置 0 跳过全类枚举的 delegate 响应 hook，保留请求/完成块抓取)
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import signal
import time
import zlib
import uuid
from collections.abc import Mapping
from contextlib import nullcontext
from datetime import datetime
from threading import Event
from typing import Any

from autophone.device.stage_threads import start_device_thread

from autophone.paths import STATE_ROOT

from autophone.observation.frida.objc_bundle_probe import require_bundle_id, spawn_gate
from autophone.device.frida_endpoint import resolve_frida_host
from autophone.device.stage_guard import OperationClass, guarded_device_operation

FRIDA_HOST = resolve_frida_host()
SETTLE = float(os.environ.get("AP_SETTLE", "20"))
CAPTURE_DELEGATES = os.environ.get("AP_CAPTURE_DELEGATES", "1").strip().lower() not in {
    "0", "false", "no", "off",
}
AGENT_JS = os.path.join(os.path.dirname(__file__), "agent", "_build", "net_tap.js")
# runs 根：与 phase2 对齐为 runs/<date>/<run_id>/net/，M1 暂用 phase4 自己的 runs（M2 接 runner 时改指 phase2）
RUNS_DIR = str(STATE_ROOT / "runs" / "phase4")
# 录制 pid 文件：SETTLE=0「录到信号」模式下写，供 viewer stop 时定位并发 SIGTERM。
RECORDING_PID = os.path.join(RUNS_DIR, ".recording.pid")

# 噪声 deny glob：心跳/崩溃/埋点上报等非业务流量，可通过 AUTOPHONE_NET_CAPTURE_NOISE_GLOBS（逗号分隔）追加。
# 命中的调用打 noise=true：仍全量落原始 JSONL（可回溯），但不参与归因摘要与断言。
import fnmatch as _fnmatch

NOISE_DENY_GLOBS = [
    item.strip()
    for item in os.environ.get("AUTOPHONE_NET_CAPTURE_NOISE_GLOBS", "").split(",")
    if item.strip()
]


def is_noise(url: str | None) -> bool:
    if not url:
        return False
    return any(_fnmatch.fnmatch(url, g) for g in NOISE_DENY_GLOBS)


def _run_dir() -> str:
    now = datetime.now()
    d = os.path.join(RUNS_DIR, now.strftime("%Y%m%d"), now.strftime("%H%M%S"), "net")
    os.makedirs(d, exist_ok=True)
    return d


def _maybe_text(raw: bytes | None, content_encoding: str | None) -> str | None:
    """body 字节 → 明文字符串：先按 content-encoding 解 gzip/deflate，再尝试 utf-8。"""
    if not raw:
        return None
    data = raw
    enc = (content_encoding or "").lower()
    try:
        if "gzip" in enc:
            data = gzip.decompress(raw)
        elif "deflate" in enc:
            data = zlib.decompress(raw)
    except Exception:  # noqa: BLE001
        # 编码头说压缩了但解不开（截断/非完整包）→ 退回原字节尝试
        data = raw
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # 只在像文本时回带（避免把二进制误当文本灌进报告）
    return text if text.isprintable() or "\n" in text or text.strip().startswith(("{", "[")) else text


def seal_capture(exports, events: list[dict], *, timeout_s: float = 1.0,
                 frida_host: str | None = None) -> dict:
    """Bound drain and verify the delivered ACK before the script is unloaded.

    An ACK proves event delivery up to the cutoff. Nonempty pending tasks remain
    incomplete evidence, even when the ACK itself is verified.
    """
    nonce = uuid.uuid4().hex
    timeout_s = max(0.0, float(timeout_s))
    events.append({"ev": "capture_seal_requested", "nonce": nonce})
    finished = Event()
    try:
        import frida
        cancellable = frida.Cancellable()
    except ImportError:
        cancellable = None
    result: list[dict] = []
    errors: list[str] = []

    def seal_remote():
        try:
            fn = getattr(exports, "seal_capture", None) or getattr(exports, "sealCapture", None)
            if fn is None:
                errors.append("capture_seal_unsupported")
            else:
                with cancellable if cancellable is not None else nullcontext():
                    result.append(fn(max(0, int(timeout_s * 800)), nonce))
        except Exception:  # no RPC response data in diagnostics
            errors.append("capture_seal_rpc_failed")
        finally:
            finished.set()

    def failed(reason):
        receipt = {"ev": "capture_seal_failed", "nonce": nonce,
                   "ack_verified": False, "reason": reason}
        events.append(receipt)
        return receipt

    try:
        start_device_thread(seal_remote, name="autophone-capture-seal", frida_endpoint=frida_host)
    except Exception:
        return failed("capture_seal_not_admitted")
    deadline = time.monotonic() + timeout_s
    if not finished.wait(timeout_s):
        if cancellable is not None:
            cancellable.cancel()
        return failed("capture_seal_timeout")
    if errors:
        return failed(errors[0])
    ack = result[0] if result else None
    if (not isinstance(ack, dict) or ack.get("ev") != "capture_sealed"
            or ack.get("nonce") != nonce or ack.get("ok") is not True
            or not isinstance(ack.get("capture_id"), str)
            or not isinstance(ack.get("event_seq"), int)
            or not isinstance(ack.get("pending"), list)):
        return failed("capture_seal_ack_invalid")
    while not any(e == ack for e in events):
        if time.monotonic() >= deadline:
            return failed("capture_seal_ack_not_delivered")
        time.sleep(min(0.005, max(0, deadline - time.monotonic())))
    delivered = [e for e in events if e.get("capture_id") == ack["capture_id"]]
    if ([e.get("event_seq") for e in delivered] != list(range(1, ack["event_seq"] + 1))
            or not delivered or delivered[-1] != ack):
        return failed("capture_sequence_invalid")
    receipt = {"ev": "capture_seal_verified", "nonce": nonce, "ack_verified": True,
               "capture_id": ack["capture_id"], "last_event_seq": ack["event_seq"],
               "pending_count": len(ack["pending"])}
    events.append(receipt)
    return dict(ack, ack_verified=True)


@guarded_device_operation(OperationClass.FRIDA, identity=lambda a: {"frida_endpoint": resolve_frida_host()})
def capture(out_dir: str | None = None, *, bundle_id: str | None = None) -> tuple[list[dict], dict]:
    bundle_id = require_bundle_id(bundle_id)
    import frida

    events: list[dict] = []

    def on_message(message, data):
        if message.get("type") == "send":
            events.append(message["payload"])  # body 已在 payload.body_b64（guest 出 base64）
        elif message.get("type") == "error":
            events.append({"ev": "js_error", "desc": message.get("description")})

    device = frida.get_device_manager().add_remote_device(resolve_frida_host())
    meta = {"settle": SETTLE, "bundle_id": bundle_id}
    # SETTLE=0：「录到信号」模式——不固定秒数，等 SIGTERM/SIGINT 再收尾（页面 stop 触发）。
    stop = {"flag": False}

    def _on_sig(signum, frame):
        stop["flag"] = True
    if SETTLE == 0:
        signal.signal(signal.SIGTERM, _on_sig)
        signal.signal(signal.SIGINT, _on_sig)

    try:
        with spawn_gate(
            device,
            bundle_id,
            AGENT_JS,
            on_message,
            install_capture=CAPTURE_DELEGATES,
        ) as (pid, _s, ex):
            meta["pid"] = pid
            try:
                meta["rpc_ping"] = (ex.ping() == "pong")
            except Exception as e:  # noqa: BLE001
                meta["rpc_ping_error"] = repr(e)
            if SETTLE == 0:
                # 写 pid 文件（含 run_dir），供 viewer stop 定位
                if out_dir:
                    try:
                        with open(RECORDING_PID, "w") as f:
                            f.write(json.dumps({"pid": os.getpid(), "run_dir": out_dir}))
                    except OSError:
                        pass
                print(f"# spawn-gated pid={pid}；录制中（在 app 里操作触发请求），"
                      f"收到 SIGTERM/SIGINT 即收尾...", flush=True)
                while not stop["flag"]:
                    time.sleep(0.3)
                print("# 收到停止信号，收尾落盘", flush=True)
            else:
                print(f"# spawn-gated pid={pid}；长连收流 {SETTLE:.0f}s"
                      f"（期间在 app 里造单/进订单列表触发业务请求）...", flush=True)
                time.sleep(SETTLE)
            meta["capture_seal"] = seal_capture(ex, events, frida_host=resolve_frida_host())
    except FileNotFoundError as e:
        meta["error"] = str(e)
    except Exception as e:  # noqa: BLE001
        meta["spawn_error"] = repr(e)
    finally:
        if SETTLE == 0:
            try:
                os.path.exists(RECORDING_PID) and os.remove(RECORDING_PID)
            except OSError:
                pass
    return events, meta


def merge(
    events: list[dict],
    *,
    action_meta: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict]:
    """按 capture_id/task_instance_id 配对 req/resp，旧事件按 task_ptr FIFO 配对。

    缺响应的请求保留；生命周期终态不合成 HTTP 响应。

    坑：NSURLSessionTask 的对象指针（task_ptr）**会被 OS/RN 回收复用**——一条 task 走完
    （resp 到）后其指针被下一条 task 重用。旧实现按 tp 建 dict、后来者覆盖，复用指针的
    第二个 req 会把第一条记录连带已配的 resp 一起冲掉（长会话数百请求时错配严重）。
    正解：指针在其 req→resp 存活窗内唯一（下条 task 必等本条 resp 完成才拿到该指针），
    故同 tp 维护 FIFO 开放队列，resp 配最早未闭合的那条 req——回收复用下仍精确。"""
    open_by_tp: dict[str, list[dict]] = {}   # tp → 已见 req、未见 resp 的记录 FIFO 队列
    by_instance: dict[tuple, dict] = {}
    lifecycles: dict[tuple, dict] = {}
    merged: list[dict] = []                   # 按 req 到达序（含孤立 resp 占位）平铺
    marks = [e for e in events if e.get("ev") == "mark"]

    def attach_body(rec: dict, b64: str | None, prefix: str, enc: str | None):
        if not b64:
            return
        rec[f"{prefix}_body_b64"] = b64
        try:
            txt = _maybe_text(base64.b64decode(b64), enc)
        except Exception:  # noqa: BLE001
            txt = None
        if txt is not None:
            rec[f"{prefix}_body_text"] = txt

    for e in events:
        if e.get("ev") == "req":
            tp = e.get("task_ptr")
            if tp is None:
                continue
            r = e.get("req") or {}
            rec = {
                "task_instance_id": e.get("task_instance_id"), "capture_id": e.get("capture_id"),
                "req_event_seq": e.get("event_seq"),
                "task_ptr": tp, "session_ptr": e.get("session_ptr"), "tid": e.get("tid"),
                "method": r.get("method"), "url": r.get("url"), "host": r.get("host"),
                "req_headers": r.get("headers") or {},
                "req_content_encoding": r.get("content_encoding"),
                "req_body_len": e.get("body_len"), "req_body_truncated": e.get("body_truncated"),
                "req_body_err": e.get("body_err"), "req_body_stream": r.get("body_stream"),
                "req_body_absent": e.get("body_absent"),
                "status": None, "ms": None,
                "t_req": e.get("ts"), "t_resp": None,
                "case_id": None, "step": None, "operation": None, "action_id": None,
                "subject_epoch_ref": None,
                "noise": is_noise(r.get("url")),   # §六 deny glob：噪声标记，不参与断言/摘要，仍留原始 JSONL
            }
            attach_body(rec, e.get("body_b64"), "req", r.get("content_encoding"))
            merged.append(rec)
            if e.get("task_instance_id"):
                by_instance[(e.get("capture_id"), e["task_instance_id"])] = rec
            else:
                open_by_tp.setdefault(tp, []).append(rec)
        elif e.get("ev") == "resp":
            tp = e.get("task_ptr")
            identity = (e.get("capture_id"), e.get("task_instance_id"))
            queue = open_by_tp.get(tp) if not e.get("task_instance_id") else None
            if e.get("task_instance_id") and identity in by_instance:
                rec = by_instance[identity]
            elif queue:
                rec = queue.pop(0)   # FIFO：配最早未闭合的 req
            else:                    # resp 先到/无对应 req（少见）→ 建占位
                rec = {"task_ptr": tp, "url": None, "method": None, "t_req": None,
                       "case_id": None, "step": None, "operation": None, "action_id": None,
                       "subject_epoch_ref": None}
                rec.update(task_instance_id=e.get("task_instance_id"), capture_id=e.get("capture_id"))
                merged.append(rec)
            rec["resp_event_seq"] = e.get("event_seq")
            rec["status"] = e.get("status")
            rec["resp_headers"] = e.get("resp_headers") or {}
            rec["resp_content_encoding"] = e.get("content_encoding")
            rec["resp_body_len"] = e.get("body_len")
            rec["resp_body_truncated"] = e.get("body_truncated")
            rec["resp_body_err"] = e.get("body_err")
            rec["resp_error"] = e.get("error")
            if isinstance(e.get("projection"), dict):
                rec["resp_projection"] = e["projection"]
            rec["t_resp"] = e.get("ts")
            if rec.get("t_req") and e.get("ts"):
                rec["ms"] = e["ts"] - rec["t_req"]
            attach_body(rec, e.get("body_b64"), "resp", e.get("content_encoding"))

        elif e.get("ev") == "task_lifecycle" and e.get("task_instance_id"):
            lifecycles[(e.get("capture_id"), e["task_instance_id"])] = e

    for identity, rec in by_instance.items():
        if identity in lifecycles:
            rec["task_lifecycle"] = lifecycles[identity]

    # 时间窗回填 case/step（M1 一般无 mark，留作 M2 接 runner 后生效）
    if marks:
        marks_sorted = sorted(marks, key=lambda m: m.get("ts") or 0)
        for rec in merged:
            tr = rec.get("t_req")
            if tr is None:
                continue
            cur = None
            for m in marks_sorted:
                if (m.get("ts") or 0) <= tr:
                    cur = m
                else:
                    break
            if cur:
                rec["case_id"] = cur.get("case_id")
                rec["step"] = cur.get("step")
                rec["operation"] = cur.get("operation")
                rec["action_id"] = cur.get("action_id")
                rec["subject_epoch_ref"] = cur.get("subject_epoch_ref")
                cur_action_id = cur.get("action_id")
                meta = (
                    (action_meta or {}).get(str(cur_action_id))
                    if cur_action_id
                    else None
                )
                symbol_id = cur.get("symbol_id")
                if symbol_id is None and isinstance(meta, Mapping):
                    symbol_id = meta.get("symbol_id")
                window_tag = cur.get("window_tag")
                if window_tag is None and isinstance(meta, Mapping):
                    window_tag = meta.get("window_tag")
                if symbol_id is not None:
                    rec["symbol_id"] = str(symbol_id)
                if window_tag is not None:
                    rec["window_tag"] = str(window_tag)
    return merged


def main() -> int:
    try:
        bundle_id = require_bundle_id()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    out_dir = _run_dir()
    jsonl = os.path.join(out_dir, "capture.jsonl")
    events, meta = capture(out_dir=os.path.dirname(out_dir), bundle_id=bundle_id)  # 传 run_dir（net 的上级）

    if meta.get("error") or meta.get("spawn_error"):
        print("!! capture 未跑成：", meta.get("error") or meta.get("spawn_error"))
        return 1

    merged = merge(events)
    with open(jsonl, "w", encoding="utf-8") as f:
        for rec in merged:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    n_req = sum(1 for e in events if e.get("ev") == "req")
    n_resp = sum(1 for e in events if e.get("ev") == "resp")
    paired = sum(1 for r in merged if r.get("t_req") and r.get("t_resp"))
    orphan_req = sum(1 for r in merged if r.get("t_req") and not r.get("t_resp"))
    with_req_body = sum(1 for r in merged if r.get("req_body_b64"))
    with_resp_body = sum(1 for r in merged if r.get("resp_body_b64"))
    errs = [e for e in events if e.get("ev", "").endswith("_err") or e.get("ev") == "js_error"]

    print("=" * 64)
    print("M1 — net_capture 汇总")
    print(
        f"  bid={bundle_id}  pid={meta.get('pid')}  settle={meta['settle']:.0f}s  "
        f"delegate_hooks={CAPTURE_DELEGATES}  rpc_ping={meta.get('rpc_ping')}"
    )
    print(f"  merged → {jsonl}（{len(merged)} 条，含真实 URL/body，不入库）")
    print("-" * 64)
    print(f"  事件：req(resume)={n_req}  resp(completion)={n_resp}")
    print(f"  合并：完整 req-resp={paired}  孤立 req(无响应)={orphan_req}")
    print(f"  body：req 带 body={with_req_body}  resp 带 body={with_resp_body}（base64 入库，文本可解附 *_body_text）")
    if errs:
        print(f"  agent 内错误：{errs[:5]}{' ...' if len(errs) > 5 else ''}")
    print("=" * 64)

    # fail-closed 验收门槛——M1 要的是「落出含明文 req/resp 的 JSONL，按 task_ptr 正确合并」
    fail_reasons: list[str] = []
    if errs:
        fail_reasons.append(f"agent_errors×{len(errs)}")
    if n_req == 0:
        fail_reasons.append("no_req_events")
    if n_resp == 0:
        fail_reasons.append("no_resp_events")
    if paired == 0:
        fail_reasons.append("no_paired_req_resp")
    if with_req_body == 0 and with_resp_body == 0:
        fail_reasons.append("no_body_captured")
    if fail_reasons:
        print(f"!! M1 验收 FAIL: {'; '.join(fail_reasons)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
