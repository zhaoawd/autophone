"""Independent upstream-side observation for API interception sessions.

The Frida event stream cannot prove that an upstream service saw no request.  This
module speaks a deliberately tiny begin/end protocol to an independently deployed
observer backed by gateway/service logs.  When it is not configured, evidence says
``unconfigured``; it never turns a local short-circuit decision into zero-touch proof.
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Iterator


Transport = Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]
UPSTREAM_OBSERVER_HTTP_TIMEOUT_S = 15.0


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
            timeout=UPSTREAM_OBSERVER_HTTP_TIMEOUT_S,
        ) as response:  # noqa: S310 trusted operator URL
            parsed = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise RuntimeError(f"upstream observer 请求失败: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("upstream observer 响应必须是对象")
    return parsed


@contextlib.contextmanager
def upstream_witness_context(
    session_id: str,
    app: str,
    env: str,
    scenario: dict[str, Any],
    metadata: dict[str, Any],
    *,
    base_url: str | None = None,
    token: str | None = None,
    transport: Transport = _http_transport,
) -> Iterator[dict[str, Any]]:
    """Observe the session from an independent gateway/service-log system.

    Protocol:
      ``POST {base}/begin`` -> ``{observation_id}``
      ``POST {base}/end``   -> ``{observed_requests: int, ...}``

    The observer owns request attribution.  Only an exact zero count returned by a
    completed begin/end pair becomes ``verified_zero_touch``.
    """
    configured_url = str(
        base_url if base_url is not None
        else os.environ.get("AUTOPHONE_SCENARIO_UPSTREAM_OBSERVER_URL", "")
    ).strip().rstrip("/")
    auth_token = str(
        token if token is not None
        else os.environ.get("AUTOPHONE_SCENARIO_UPSTREAM_OBSERVER_TOKEN", "")
    ).strip()
    state: dict[str, Any] = {
        "upstream": {
            "status": "unconfigured",
            "independent": False,
            "verified_zero_touch": False,
        },
        "_finish": lambda: None,
    }
    if not configured_url:
        yield state
        return

    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    rules = []
    for rule in scenario.get("rules") or []:
        match = rule.get("match") if isinstance(rule, dict) else {}
        rules.append({
            "rule_id": rule.get("rule_id"),
            "method": match.get("method"),
            "host": match.get("host"),
            "path": match.get("path"),
            "path_prefix": match.get("path_prefix"),
            "path_suffix": match.get("path_suffix"),
        })
    begin_payload = {
        "session_id": session_id, "app": app, "env": env,
        "rules": rules, "metadata": _safe_metadata(metadata),
    }
    try:
        begin = transport(f"{configured_url}/begin", begin_payload, headers)
        observation_id = str(begin.get("observation_id") or "").strip()
        if not observation_id:
            raise RuntimeError("upstream observer begin 缺 observation_id")
        state["upstream"] = {
            "status": "active", "independent": True,
            "verified_zero_touch": False, "observation_id": observation_id,
            "observer": configured_url,
        }
    except Exception as exc:  # noqa: BLE001 observer failure must remain explicit evidence
        state["upstream"] = {
            "status": "error", "independent": True,
            "verified_zero_touch": False, "error": str(exc),
            "observer": configured_url,
        }
        yield state
        return

    finished = False

    def finish() -> None:
        nonlocal finished
        if finished:
            return
        finished = True
        try:
            ended = transport(
                f"{configured_url}/end",
                {"session_id": session_id, "observation_id": observation_id},
                headers,
            )
            count = ended.get("observed_requests")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise RuntimeError("upstream observer end.observed_requests 必须是非负整数")
            state["upstream"] = {
                "status": "verified_zero" if count == 0 else "requests_observed",
                "independent": True,
                "verified_zero_touch": count == 0,
                "observed_requests": count,
                "observation_id": observation_id,
                "observer": configured_url,
                "detail": copy.deepcopy(ended.get("detail")),
            }
        except Exception as exc:  # noqa: BLE001
            state["upstream"] = {
                "status": "error", "independent": True,
                "verified_zero_touch": False, "error": str(exc),
                "observation_id": observation_id, "observer": configured_url,
            }

    state["_finish"] = finish
    try:
        yield state
    finally:
        finish()


def _safe_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Only send routing identifiers; never forward bodies, headers or credentials."""
    allowed = {"kind", "upstream_policy"}
    return {key: copy.deepcopy(value) for key, value in metadata.items() if key in allowed}
