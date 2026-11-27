#!/usr/bin/env python3
# coding=utf8
"""Phase 5 M0a — CoreLocation control probe.

This is the active counterpart to corelocation_discovery.py. It spawn-gates the
target app, configures a Frida CoreLocation mock before resume, then records
whether the app actually consumes the controlled location surface.

It does not unlock Phase 5 distance fixtures by itself. A distance matrix needs
this probe to show under/over-200m control evidence while the lifecycle runner
uses the same controlled app process.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from autophone.paths import STATE_ROOT

from autophone.observation.frida.objc_bundle_probe import _exports, require_bundle_id
from autophone.execution.fixtures.contract import write_json
from autophone.device.frida_endpoint import resolve_frida_host
from autophone.device.stage_guard import StageGuard, OperationClass, guarded_device_operation

FRIDA_HOST = resolve_frida_host()
DEFAULT_SETTLE = float(os.environ.get("AP_SETTLE", "20"))
AGENT_JS = Path(__file__).resolve().parents[2] / "observation" / "frida" / "agent" / "_build" / "corelocation_control.js"
RUNS_DIR = STATE_ROOT / "runs" / "phase5"

def _run_dir() -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return RUNS_DIR / ts / "gps"


def _events_path(out_dir: Path) -> Path:
    return out_dir / "corelocation_control_events.jsonl"


def _summary_path(out_dir: Path) -> Path:
    return out_dir / "corelocation_control_summary.json"


def _report_path(out_dir: Path) -> Path:
    return out_dir / "phase5-gps-spike-result.md"


def haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius_m = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2) ** 2
    return radius_m * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def destination_point(
    lat: float, lng: float, distance_m: float, bearing_deg: float = 0.0,
) -> tuple[float, float]:
    """从参照点 (lat,lng) 沿 bearing_deg 方位（0=正北，顺时针）走 distance_m 米后的坐标。

    haversine 的逆运算（球面大圆推算公式）。用于「把设备定位放到距门店或某坐标 N 米处」：
    由指定距离反推 mock 经纬度，传给 CoreLocation control。round-trip 误差小于 1 米。
    """
    radius_m = 6_371_000.0
    ang = distance_m / radius_m
    brg = math.radians(bearing_deg)
    phi1 = math.radians(lat)
    lam1 = math.radians(lng)
    phi2 = math.asin(
        math.sin(phi1) * math.cos(ang) + math.cos(phi1) * math.sin(ang) * math.cos(brg)
    )
    lam2 = lam1 + math.atan2(
        math.sin(brg) * math.sin(ang) * math.cos(phi1),
        math.cos(ang) - math.sin(phi1) * math.sin(phi2),
    )
    return math.degrees(phi2), (math.degrees(lam2) + 540) % 360 - 180


@contextmanager
def configured_spawn_gate(
    device: Any,
    bundle_id: str,
    agent_js: str,
    on_message: Any,
    config: dict[str, Any],
):
    from autophone.observation.frida.agent_bundle import ensure_agent_bundle
    agent_js = str(ensure_agent_bundle(agent_js, "gps-control"))
    if StageGuard.for_device_call() is not None:
        from autophone.observation.frida.objc_bundle_probe import spawn_gate
        configured = []
        with spawn_gate(device, bundle_id, agent_js, on_message,
                        after_load=lambda ex: configured.append(ex.configure(config)),
                        install_capture=False) as (pid, script, ex):
            yield pid, script, ex, configured[0]
        return
    pid = device.spawn([bundle_id])
    session = None
    try:
        session = device.attach(pid)
        with open(agent_js, "r", encoding="utf-8") as f:
            script = session.create_script(f.read())
        script.on("message", on_message)
        script.load()
        ex = _exports(script)
        configured = ex.configure(config)
        device.resume(pid)
        yield pid, script, ex, configured
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:  # noqa: BLE001
                pass
        try:
            device.kill(pid)
        except Exception:  # noqa: BLE001
            pass


@guarded_device_operation(OperationClass.FRIDA, identity=lambda a: {"frida_endpoint": resolve_frida_host()})
def run_control_probe(args: argparse.Namespace) -> dict[str, Any]:
    bundle_id = require_bundle_id(getattr(args, "bundle_id", None))
    import frida

    events: list[dict[str, Any]] = []

    def on_message(message: dict[str, Any], data: Any) -> None:
        if message.get("type") == "send":
            events.append(message["payload"])
        elif message.get("type") == "error":
            events.append({"ev": "js_error", "desc": message.get("description")})

    config = {
        "enabled": True,
        "sampleSeq": int(getattr(args, "sample_seq", 0)),
        "latitude": args.latitude,
        "longitude": args.longitude,
        "altitude": float(getattr(args, "altitude", 0.0)),
        "horizontalAccuracy": args.horizontal_accuracy,
        "verticalAccuracy": float(getattr(args, "vertical_accuracy", 5.0)),
        "course": float(getattr(args, "course", -1.0)),
        "speed": float(getattr(args, "speed", -1.0)),
        "timestampMs": int(getattr(args, "timestamp_ms", 0) or time.time() * 1000),
        "injectDelegate": not args.no_delegate_inject,
        "injectDelayMs": args.inject_delay_ms,
    }
    out: dict[str, Any] = {"settle": args.settle, "events": events, "config": config}
    device = frida.get_device_manager().add_remote_device(resolve_frida_host())
    try:
        with configured_spawn_gate(device, bundle_id, str(AGENT_JS), on_message, config) as (
            pid, _script, ex, configured,
        ):
            out["pid"] = pid
            out["configured"] = configured
            try:
                out["rpc_ping"] = (ex.ping() == "pong")
            except Exception as e:  # noqa: BLE001
                out["rpc_ping_error"] = repr(e)
            print(f"# spawn-gated pid={pid}; controlling CoreLocation for {args.settle:.0f}s...")
            time.sleep(args.settle)
    except FileNotFoundError as e:
        out["error"] = str(e)
    except Exception as e:  # noqa: BLE001 - failure is spike evidence
        out["spawn_error"] = repr(e)
    return out


def summarize_control(events: list[dict[str, Any]]) -> dict[str, Any]:
    surface = next((e for e in events if e.get("ev") == "surface"), {})
    configured = [e for e in events if e.get("ev") == "control_configured"]
    hooks_ok = [e for e in events if e.get("ev") == "hook_ok"]
    hooks_err = [e for e in events if e.get("ev") == "hook_err"]
    calls = [e for e in events if e.get("ev") == "cl_call"]
    class_calls = [e for e in events if e.get("ev") == "cl_class_call"]
    replacements = [e for e in events if e.get("ev") == "location_return_replaced"]
    injections = [e for e in events if e.get("ev") == "delegate_injected"]
    consumptions = [e for e in events if e.get("ev") == "location_consumed"]
    inject_skips = [e for e in events if e.get("ev") == "delegate_inject_skipped"]
    errors = [
        e for e in events
        if e.get("ev") == "js_error" or str(e.get("ev", "")).endswith("_error")
    ]

    class_map = surface.get("classes") or {}
    hook_keys = sorted({f"{e.get('cls')} {e.get('sel')}" for e in hooks_ok})
    call_selectors = Counter(e.get("sel") for e in calls)
    latest_config = configured[-1] if configured else {}

    if not class_map.get("CLLocationManager"):
        status = "unavailable"
        app_gps_control = "unavailable"
    elif hooks_err or errors:
        status = "instrumentation_error"
        app_gps_control = "unavailable"
    elif replacements or injections or consumptions:
        status = "corelocation_controlled"
        app_gps_control = "frida_mock"
    elif calls or class_calls:
        status = "corelocation_observed_without_control"
        app_gps_control = "unavailable"
    elif configured:
        status = "ready_but_unused"
        app_gps_control = "unavailable"
    else:
        status = "not_configured"
        app_gps_control = "unavailable"

    return {
        "status": status,
        "app_gps_control": app_gps_control,
        "classes": class_map,
        "hooks_ok": hook_keys,
        "hooks_err": hooks_err,
        "configured": bool(configured),
        "configured_location": {
            "latitude": latest_config.get("latitude"),
            "longitude": latest_config.get("longitude"),
            "horizontalAccuracy": latest_config.get("horizontalAccuracy"),
            "verticalAccuracy": latest_config.get("verticalAccuracy"),
            "altitude": latest_config.get("altitude"),
            "course": latest_config.get("course"),
            "speed": latest_config.get("speed"),
            "timestampMs": latest_config.get("timestampMs"),
            "sampleSeq": latest_config.get("sampleSeq"),
        } if latest_config else None,
        "call_count": len(calls),
        "class_call_count": len(class_calls),
        "call_selectors": dict(call_selectors),
        "replacement_count": len(replacements),
        "delegate_injection_count": len(injections),
        "consumption_count": len(consumptions),
        "consumed_sample_seqs": sorted({
            int(event["sampleSeq"])
            for event in consumptions
            if event.get("sampleSeq") is not None
        }),
        "delegate_inject_skips": inject_skips,
        "errors": errors,
    }


def write_outputs(
    out_dir: Path,
    events: list[dict[str, Any]],
    summary: dict[str, Any],
    report: str,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with _events_path(out_dir).open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    write_json(_summary_path(out_dir), summary)
    _report_path(out_dir).write_text(report, encoding="utf-8")


def render_report(summary: dict[str, Any]) -> str:
    loc = summary.get("configured_location") or {}
    return "\n".join([
        "# CoreLocation Control Result",
        "",
        f"- status: `{summary.get('status')}`",
        f"- app_gps_control: `{summary.get('app_gps_control')}`",
        f"- configured coordinate: `{loc.get('latitude')},{loc.get('longitude')}`",
        f"- reference coordinate: `{summary.get('store_latitude')},{summary.get('store_longitude')}`",
        f"- distance_to_store_m: `{summary.get('distance_to_store_m')}`",
        f"- distance_band: `{summary.get('distance_band')}`",
        f"- CoreLocation calls: `{summary.get('call_count')}`",
        f"- CoreLocation class calls: `{summary.get('class_call_count')}`",
        f"- location replacements: `{summary.get('replacement_count')}`",
        f"- delegate injections: `{summary.get('delegate_injection_count')}`",
        "",
        "Conclusion:",
        "",
        (
            "- CoreLocation Frida mock is a proven control candidate for this run."
            if summary.get("app_gps_control") == "frida_mock"
            else "- GPS distance cases remain blocked; this run did not prove active app GPS control."
        ),
        "",
    ]) + "\n"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--latitude", type=float, required=True, help="mock latitude")
    p.add_argument("--longitude", type=float, required=True, help="mock longitude")
    p.add_argument("--horizontal-accuracy", type=float, default=15.0)
    p.add_argument("--vertical-accuracy", type=float, default=5.0)
    p.add_argument("--altitude", type=float, default=0.0)
    p.add_argument("--course", type=float, default=-1.0)
    p.add_argument("--speed", type=float, default=-1.0)
    p.add_argument("--sample-seq", type=int, default=0)
    p.add_argument("--timestamp-ms", type=int, default=0)
    p.add_argument("--bundle-id", default=os.environ.get("AP_BUNDLE_ID"))
    p.add_argument("--store-latitude", type=float, default=os.environ.get("AP_STORE_LAT"))
    p.add_argument("--store-longitude", type=float, default=os.environ.get("AP_STORE_LNG"))
    p.add_argument("--settle", type=float, default=DEFAULT_SETTLE)
    p.add_argument("--inject-delay-ms", type=int, default=100)
    p.add_argument("--no-delegate-inject", action="store_true")
    p.add_argument("--out", type=Path, default=None,
                   help="output directory; default tools/phase5/runs/<ts>/gps")
    args = p.parse_args()
    try:
        args.bundle_id = require_bundle_id(args.bundle_id)
    except ValueError as exc:
        p.error(str(exc))
    if (args.store_latitude is None) != (args.store_longitude is None):
        p.error("provide both store coordinates to compute distance")
    return args


def main() -> int:
    args = parse_args()
    out = run_control_probe(args)
    events = out["events"]
    summary = summarize_control(events)
    summary.update({
        "frida_host": FRIDA_HOST,
        "bundle_id": args.bundle_id,
        "settle": args.settle,
        "pid": out.get("pid"),
        "rpc_ping": out.get("rpc_ping", False),
        "rpc_configured": out.get("configured"),
        "store_latitude": args.store_latitude,
        "store_longitude": args.store_longitude,
    })
    if args.store_latitude is not None:
        distance_m = haversine_meters(args.latitude, args.longitude, args.store_latitude, args.store_longitude)
        summary["distance_to_store_m"] = round(distance_m, 2)
        summary["distance_band"] = "under_200m" if distance_m < 200 else "over_200m"
    if out.get("error"):
        summary["error"] = out["error"]
    if out.get("spawn_error"):
        summary["spawn_error"] = out["spawn_error"]
    report = render_report(summary)

    out_dir = args.out or _run_dir()
    write_outputs(out_dir, events, summary, report)

    print("=" * 64)
    print("Phase 5 M0a — CoreLocation control probe")
    print(f"  host={FRIDA_HOST}  bid={args.bundle_id}  settle={args.settle:.0f}s")
    print(f"  target={args.latitude},{args.longitude}")
    if args.store_latitude is not None:
        print(f"  store={args.store_latitude},{args.store_longitude}")
        print(f"  distance={summary['distance_to_store_m']}m ({summary['distance_band']})")
    print(f"  raw → {_events_path(out_dir)}")
    print(f"  summary → {_summary_path(out_dir)}")
    print(f"  report → {_report_path(out_dir)}")
    print("-" * 64)
    print(f"  status: {summary['status']}")
    print(f"  app_gps_control: {summary['app_gps_control']}")
    print(f"  hooks ok: {len(summary['hooks_ok'])}")
    if summary["hooks_err"]:
        print(f"  hooks ERR: {summary['hooks_err'][:3]}")
    print(f"  calls: {summary['call_count']}")
    if summary["call_selectors"]:
        print(f"  selectors: {summary['call_selectors']}")
    print(f"  replacements: {summary['replacement_count']}")
    print(f"  delegate injections: {summary['delegate_injection_count']}")
    if summary.get("error") or summary.get("spawn_error"):
        print(f"  !! {summary.get('error') or summary.get('spawn_error')}")
    if summary["errors"]:
        print(f"  agent errors: {summary['errors'][:3]}")
    print("=" * 64)
    print("SUMMARY_JSON " + json.dumps(summary, ensure_ascii=False))

    if summary.get("error") or summary.get("spawn_error"):
        return 1
    if summary["status"] in {"unavailable", "instrumentation_error", "not_configured"}:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
