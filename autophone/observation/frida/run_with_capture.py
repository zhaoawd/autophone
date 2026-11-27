#!/usr/bin/env python3
# coding=utf8
"""M2 — 关联层 orchestrator（host 侧）：抓取会话 × phase2 用例 runner 对齐。

把 M1 的长连抓取（spawn-gate net_tap.js）与 phase2 的 UI 驱动 runner 合到同一个被测实例上：

  spawn-gate 拉起目标 App（Frida 持 script，含 rpc.mark）→ 暖机到可用界面
    → 用同一实例跑 phase2 用例（UDS 驱动），把 CaptureHandle 注入 CaseContext
    → 每个动作 _record 自动 api_mark → 抓到的 API 调用按时间窗归到 (case_id, step)
    → 收尾合并落 runs/<date>/<run_id>/net/capture.jsonl（case_id/step 已回填）。

抓取与 UI 解耦：CaseContext 只认 capture.mark()，不感知 Frida（§六）。

跑（默认用例 R21「检查更新」——触发 updateCheck 业务接口，便于看归因）：
    AP_CASE=case-id AP_CASES_FILE=cases.yaml AP_BUNDLE_ID=com.example.app AP_WARMUP=12 uv run --group frida python -m autophone.observation.frida.run_with_capture
环境变量：AP_FRIDA_HOST AP_BUNDLE_ID AP_CASE AP_CASES_FILE AP_WARMUP(冷启动暖机秒,12)
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from autophone.execution.engine.runner import (
    load_cases, run_case, synthesize_status,
)
from autophone.observation.frida import api_oracle, net_capture
from autophone.observation.frida.governance import redact_assertion, redact_summary
from autophone.observation.frida.objc_bundle_probe import require_bundle_id, spawn_gate
from autophone.device.frida_endpoint import resolve_frida_host
from autophone.device.stage_guard import OperationClass, guarded_device_operation

FRIDA_HOST = resolve_frida_host()
WARMUP = float(os.environ.get("AP_WARMUP", "12"))
AGENT_JS = net_capture.AGENT_JS


class CaptureHandle:
    """注入 CaseContext.capture 的最小句柄（§六）：只暴露 mark()，包住 frida rpc。
    mark 失败吞掉——抓取链路绝不拖垮被测用例。"""

    def __init__(self, exports):
        self._ex = exports
        self.marks = 0

    def mark(
        self, case_id: str, step: str, operation: str | None = None, *,
        action_id: str | None = None, subject_epoch_ref: str | None = None,
    ) -> None:
        try:
            self._ex.mark(case_id, step, operation, action_id, subject_epoch_ref)
        except TypeError:
            self._ex.mark(case_id, step, operation)
        self.marks += 1


def _run_dir() -> Path:
    now = datetime.now()
    d = Path(net_capture.RUNS_DIR) / now.strftime("%Y%m%d") / now.strftime("%H%M%S") / "net"
    d.mkdir(parents=True, exist_ok=True)
    return d


@guarded_device_operation(OperationClass.FRIDA, identity=lambda a: {"frida_endpoint": resolve_frida_host()})
def main() -> int:
    try:
        bundle_id = require_bundle_id()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    case_id = os.environ.get("AP_CASE", "").strip()
    cases_file = os.environ.get("AP_CASES_FILE", "").strip()
    if not case_id or not cases_file:
        raise SystemExit("set AP_CASE and AP_CASES_FILE to select a case")
    import frida

    cases = {c["id"]: c for c in load_cases(Path(cases_file))}
    case = cases.get(case_id)
    if case is None:
        print(f"!! 用例 {case_id} 不在 {cases_file}")
        return 2

    events: list[dict] = []

    def on_message(message, data):
        if message.get("type") == "send":
            events.append(message["payload"])
        elif message.get("type") == "error":
            events.append({"ev": "js_error", "desc": message.get("description")})

    out_dir = _run_dir()
    shot_dir = out_dir.parent / "shots"
    shot_dir.mkdir(parents=True, exist_ok=True)
    sock = None  # runner 自解析
    device = frida.get_device_manager().add_remote_device(resolve_frida_host())

    outcome = None
    capture_error: str | None = None
    try:
        with spawn_gate(device, bundle_id, AGENT_JS, on_message) as (pid, _s, ex):
            print(f"# spawn-gated pid={pid}；暖机 {WARMUP:.0f}s 等 app 到可用界面...")
            time.sleep(WARMUP)
            handle = CaptureHandle(ex)
            print(f"# 跑用例 {case_id}（UDS 驱动同一实例，自动 api_mark）...")
            from autophone.execution.engine.actions import resolve_sock
            outcome = run_case(case, shot_dir, resolve_sock(), 15.0, capture=handle)
            print(f"# 用例完成：status={outcome.status}  marks={handle.marks}  detail={outcome.detail[:80]}")
            time.sleep(2)  # 收尾窗口：等末尾动作触发的响应回来
    except FileNotFoundError as e:
        print("!! agent bundle 缺失：", e)
        return 1
    except Exception as e:  # noqa: BLE001
        capture_error = repr(e)
        print("!! 抓取/spawn 异常：", capture_error)

    merged = net_capture.merge(events)
    jsonl = out_dir / "capture.jsonl"
    with open(jsonl, "w", encoding="utf-8") as f:
        for rec in merged:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    marks = [e for e in events if e.get("ev") == "mark"]
    attributed = [r for r in merged if r.get("case_id")]
    steps = sorted({r.get("step") for r in attributed if r.get("step")})

    # ④ 断言层 + §七-bis 脱敏：对该 case 评估 expectations，结果脱敏后挂上 outcome（流进 result.json/HTML）
    if outcome is not None:
        exps = api_oracle.load_expectations()
        oracle_ctx = api_oracle.fixture_context_vars_from_env()
        assertions = api_oracle.evaluate_case(case_id, merged, exps, ctx=oracle_ctx)
        api_st = api_oracle.api_status(assertions)
        outcome.api_assertions = [redact_assertion(a) for a in assertions]
        outcome.api_calls = [redact_summary(c) for c in attributed if not c.get("noise")]
        outcome.net_jsonl = str(jsonl)
        ui_st = outcome.status
        outcome.status = synthesize_status(ui_st, api_st)
        print(f"# API 断言：{api_st}  (UI={ui_st} → 合成={outcome.status})  "
              f"assertions={len(assertions)}")
    print("=" * 64)
    print("M2 — run_with_capture 汇总")
    print(f"  case={case_id}  status={outcome.status if outcome else 'n/a'}")
    print(f"  merged → {jsonl}（{len(merged)} 条）")
    print(f"  mark 事件={len(marks)}  归因到 case 的调用={len(attributed)}/{len(merged)}")
    print(f"  命中的 step：{steps or '（无——窗口内无业务调用，可换触发接口的用例或拉长暖机）'}")
    for r in attributed:
        print(f"    [{r.get('step')}] {r.get('method')} {r.get('status')} "
              f"{(r.get('url') or '')[:60]}")
    print("=" * 64)

    # fail-closed 验收门槛——避免「没抓到 / 异常中断」被记为绿
    fail_reasons: list[str] = []
    if capture_error is not None:
        fail_reasons.append(f"capture_exception:{capture_error}")
    if outcome is None:
        fail_reasons.append("outcome_none")
    if not marks:
        fail_reasons.append("no_mark_events")
    if not attributed:
        fail_reasons.append("no_attributed_call")
    js_errors = [e for e in events if e.get("ev") == "js_error"]
    if js_errors:
        fail_reasons.append(f"agent_js_error×{len(js_errors)}")
    if fail_reasons:
        print(f"!! M2 验收 FAIL: {'; '.join(fail_reasons)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
