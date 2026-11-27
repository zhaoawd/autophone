"""Trusted post-detach direct-response probe for generic attach scenarios.

Mechanical Frida unload/detach does not prove that the App can reach the original
endpoint again.  An operator-provided probe service must trigger the same App flow
after detach and correlate the resulting upstream request/response.  This adapter
only sends routing projections; it never exports scenario bodies or headers.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable

from autophone.scenario.rules import CompiledScenario


Transport = Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]
RECOVERY_PROBE_HTTP_TIMEOUT_S = 30.0


def _http_transport(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=RECOVERY_PROBE_HTTP_TIMEOUT_S,
        ) as response:  # noqa: S310 operator URL
            parsed = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise RuntimeError(f"detach recovery probe 请求失败: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("detach recovery probe 响应必须是对象")
    return parsed


def post_detach_recovery_probe(
    compiled: CompiledScenario,
    *,
    base_url: str | None = None,
    token: str | None = None,
    transport: Transport = _http_transport,
) -> dict[str, Any]:
    """Ask a trusted rig service to trigger and observe the direct path.

    The service response is intentionally evidence-shaped rather than a bare
    ``verified`` claim.  Verification requires at least one correlated upstream
    request, at least one successful original response, and zero scenario markers.
    """
    configured_url = str(
        base_url if base_url is not None
        else os.environ.get("AUTOPHONE_SCENARIO_RECOVERY_PROBE_URL", "")
    ).strip().rstrip("/")
    auth_token = str(
        token if token is not None
        else os.environ.get("AUTOPHONE_SCENARIO_RECOVERY_PROBE_TOKEN", "")
    ).strip()
    if not configured_url:
        return {"status": "unconfigured", "verified": False}

    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    rules = []
    for rule in compiled.rules:
        match = rule.get("match") or {}
        rules.append({
            "rule_id": rule.get("rule_id"),
            "method": match.get("method"),
            "host": match.get("host"),
            "path": match.get("path"),
            "path_prefix": match.get("path_prefix"),
            "path_suffix": match.get("path_suffix"),
        })
    payload = {
        "app": compiled.app,
        "env": compiled.env,
        "scenario_id": compiled.scenario_id,
        "rule_snapshot_hash": compiled.snapshot_hash,
        "rules": rules,
    }
    try:
        result = transport(f"{configured_url}/probe", payload, headers)
        direct = result.get("observed_direct_requests")
        successful = result.get("successful_original_responses")
        markers = result.get("scenario_markers")
        for name, value in (
            ("observed_direct_requests", direct),
            ("successful_original_responses", successful),
            ("scenario_markers", markers),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise RuntimeError(f"detach recovery probe {name} 必须是非负整数")
        verified = direct > 0 and successful > 0 and markers == 0
        return {
            "status": "verified_direct" if verified else "not_verified",
            "verified": verified,
            "independent": True,
            "observed_direct_requests": direct,
            "successful_original_responses": successful,
            "scenario_markers": markers,
            "probe": configured_url,
            "detail": result.get("detail"),
        }
    except Exception as exc:  # noqa: BLE001 evidence failure must remain explicit
        return {
            "status": "error", "verified": False, "independent": True,
            "probe": configured_url, "error": str(exc),
        }


def external_recovery_probe_factory(
    compiled: CompiledScenario,
) -> Callable[[CompiledScenario], dict[str, Any]] | None:
    """Production factory; no configured service means an explicit unconfigured seam."""
    if not os.environ.get("AUTOPHONE_SCENARIO_RECOVERY_PROBE_URL", "").strip():
        return None
    return post_detach_recovery_probe
