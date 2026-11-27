from __future__ import annotations

import argparse

from autophone.adapters.cli import _common as common


def _format_human_summary(payload: dict) -> str:
    caps = payload.get("capabilities") or {}
    ui_ready = bool(caps.get("system_ui_and_location_ready"))
    lines = [
        "[OK] 系统界面与定位就绪" if ui_ready else f"[FAIL] 系统界面与定位未就绪（{payload.get('detail') or payload.get('status')}）",
    ]
    checks = payload.get("checks")
    if isinstance(checks, dict):
        for name in ("vm", "uds", "api", "frida"):
            item = checks.get(name)
            if isinstance(item, dict):
                tag = "OK" if item.get("passed") else "FAIL"
                detail = item.get("detail") or item.get("state") or item.get("stage") or ""
                lines.append(f"[{tag}] {name}" + (f": {detail}" if detail else ""))
    ocr = caps.get("host_vision_ocr") or {}
    if ocr:
        lines.append(f"[{'OK' if ocr['ready'] else 'INFO'}] Vision OCR: {ocr['path']}")
    probe = caps.get("springboard_region_probe") or {}
    if probe.get("configured"):
        lines.append(f"[OK] {probe.get('hint')}")
    elif probe.get("hint"):
        lines.append(f"[INFO] {probe.get('hint')}")
    return "\n".join(lines)


def cmd_doctor(args: argparse.Namespace) -> int:
    from autophone.application.diagnostics import DiagnosticRequest, DiagnosticsApplication

    application = DiagnosticsApplication()
    sock = args.sock or application.resolve_sock()
    request = DiagnosticRequest(
        app=args.app,
        device=args.device,
        sock=sock,
        timeout=args.timeout,
        api_base=args.api_base,
    )
    if getattr(args, "fast", False):
        payload = application.fast(request)
        if getattr(args, "human", False):
            print(_format_human_summary(payload))
        else:
            print(common.json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["infra_ready"] else 1
    payload = application.basic(request)
    if getattr(args, "human", False):
        print(_format_human_summary(payload))
    else:
        print(common.json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "healthy" else 1


def register(sub: argparse._SubParsersAction) -> None:
    doctor = sub.add_parser("doctor", help="check VM, UDS, API, Frida and optional helper readiness")
    doctor.add_argument("--sock", default=None)
    doctor.add_argument("--timeout", type=float, default=8.0)
    doctor.add_argument(
        "--fast",
        action="store_true",
        help=(
            "bounded environment gate: VM/UDS/API/Frida enumeration; "
            "never launches or restarts an App"
        ),
    )
    doctor.add_argument(
        "--human",
        action="store_true",
        help="print human-readable tiered readiness lines instead of JSON",
    )
    doctor.add_argument(
        "--api-base",
        default=common.os.environ.get("AP_API_BASE", "http://127.0.0.1:8000"),
        help="control-plane base; AP_API_AUTH supplies one Basic Auth credential",
    )
    doctor.set_defaults(func=cmd_doctor)
