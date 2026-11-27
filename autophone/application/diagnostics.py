"""Protocol-independent VM and integration diagnostics orchestration."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import urllib.request
from dataclasses import dataclass
from typing import Any


from autophone.device.ocr_target import ocr_readiness


def springboard_region_probe_advisory() -> dict[str, Any]:
    """Report optional SpringBoard region probe readiness without blocking system UI/location."""
    from autophone.device.springboard_residue import REGION_PROBE_ENV
    from autophone.paths import CODE_ROOT

    configured = os.environ.get(REGION_PROBE_ENV, "").strip()
    if configured:
        return {
            "configured": True,
            "source": "env",
            "path": configured,
            "required_for": "third_party_ipa_install",
            "hint": f"已通过 {REGION_PROBE_ENV} 配置 SpringBoard 区域探针（{configured}）。",
        }
    local_built = CODE_ROOT / "dist" / "guest" / "autophone-springboard-regions"
    if local_built.is_file():
        return {
            "configured": True,
            "source": "local_build",
            "path": str(local_built),
            "required_for": "third_party_ipa_install",
            "hint": (
                f"已检测到宿主构建产物 {local_built}；启动时传入 "
                f"`autophone rig up --springboard-region-probe {local_built}` 即可自动暂存。"
            ),
        }
    return {
        "configured": False,
        "source": "none",
        "path": None,
        "required_for": "third_party_ipa_install",
        "hint": "未配置 springboard-region-probe（仅安装第三方 IPA 时需要，运行 scripts/build_guest_helpers.sh 生成）",
    }


@dataclass(frozen=True)
class DiagnosticRequest:
    app: str
    device: str
    sock: str
    timeout: float = 8.0
    api_base: str = "http://127.0.0.1:8000"


def _api_basic_auth_header(auth: str | None = None) -> str | None:
    """Build one Basic credential from ``AP_API_AUTH`` without logging its value."""
    credential = os.environ.get("AP_API_AUTH") if auth is None else auth
    if credential is None or credential == "":
        return None
    username, separator, password = credential.partition(":")
    if not separator or not username or not password:
        raise ValueError("AP_API_AUTH must be one non-empty user:password pair")
    encoded = base64.b64encode(credential.encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


def _api(base: str, method: str, path: str, body: dict | None = None,
         timeout: float = 8.0, *, auth: str | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    authorization = _api_basic_auth_header(auth)
    if authorization is not None:
        headers["Authorization"] = authorization
    req = urllib.request.Request(
        base.rstrip("/") + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode() or "{}")


class DiagnosticsApplication:
    def resolve_sock(self) -> str:
        from autophone.execution.engine.actions import resolve_sock

        return resolve_sock()

    def basic(self, request: DiagnosticRequest) -> dict[str, Any]:
        from autophone.execution.engine.runner import gate

        ok, reason = gate(request.sock, timeout=request.timeout)
        probe = springboard_region_probe_advisory()
        return {
            "schema_version": "v1",
            "app": request.app,
            "device": request.device,
            "sock": request.sock,
            "status": "healthy" if ok else "down",
            "detail": reason,
            "capabilities": {
                "system_ui_and_location_ready": bool(ok),
                "springboard_region_probe": probe,
                "host_vision_ocr": ocr_readiness(),
            },
        }

    def fast(self, request: DiagnosticRequest) -> dict[str, Any]:
        from autophone.device.frida import probe_enumeration
        from autophone.device.frida_process import default_frida_host
        from autophone.device.vsock_health_client import peek_state
        from autophone.execution.engine.runner import gate

        checks: dict[str, dict[str, Any]] = {}
        health_state = peek_state()
        vm_ok = bool(
            health_state.state == "healthy"
            and health_state.vsock_usable
        )
        checks["vm"] = {
            "passed": vm_ok,
            "state": health_state.state,
            "age_s": health_state.age_s,
        }

        uds_ok, uds_reason = gate(
            request.sock,
            timeout=min(request.timeout, 2.0),
        )
        checks["uds"] = {"passed": uds_ok, "detail": uds_reason}

        try:
            api_health = dict(_api(
                request.api_base,
                "GET",
                "/health",
                timeout=min(request.timeout, 3.0),
            ))
            api_ok = bool(
                api_health.get("ok") is True
                or api_health.get("status") == "ok"
            )
            checks["api"] = {
                "passed": api_ok,
                "service": api_health.get("service"),
            }
        except Exception as exc:  # noqa: BLE001 - report all diagnostic checks
            api_ok = False
            checks["api"] = {
                "passed": False,
                "detail": f"{type(exc).__name__}: {exc}",
            }

        # BL-232 阶段 1（R1）：只枚举进程，不 attach SpringBoard。
        frida = probe_enumeration(
            host=default_frida_host(),
            timeout_s=min(request.timeout, 5.0),
        )
        checks["frida"] = {
            "passed": frida.get("ok") is True,
            "stage": frida.get("stage"),
            "elapsed_ms": frida.get("elapsed_ms"),
            "matrix": frida.get("matrix"),
        }

        probe = springboard_region_probe_advisory()
        infra_ready = bool(
            vm_ok and uds_ok and api_ok and frida.get("ok") is True
        )
        return {
            "schema": "autophone.doctor.fast/v1",
            "status": "healthy" if infra_ready else "down",
            "app": request.app,
            "device": request.device,
            "sock": request.sock,
            "infra_ready": infra_ready,
            "checks": checks,
            "capabilities": {
                "system_ui_and_location_ready": bool(uds_ok),
                "springboard_region_probe": probe,
                "host_vision_ocr": ocr_readiness(),
            },
        }
