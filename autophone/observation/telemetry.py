from __future__ import annotations

import json
import copy
import os
import hashlib
import secrets
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
import yaml

from autophone.contracts import write_json
from autophone.paths import app_asset_path, app_asset_root_overridden
from autophone.observation.telemetry_dictionary import (
    EventDictionary,
    TelemetryDictionaryError,
    compile_event_dictionary,
)
from autophone.observation.data_trace import (
    DataTraceClient,
    DataTraceConfigurationError,
    DataTraceFilter,
    DataTraceIdentity,
    DataTraceQuery,
    FilterOperator,
    config_from_env,
)
from autophone.observation.frida.governance import redact_url
from autophone.execution.engine.cases_loader import load_cases
from autophone.execution.fixtures.contract import redact_sensitive


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_API_IDENTITY_PREFIX = "api:"
_API_IDENTITY_BODIES = {
    "request": "req_body_text",
    "response": "resp_body_text",
}


class TelemetryQueryValidationError(ValueError):
    """人工查询请求不符合 autophone 的安全接口。"""


class TelemetryReplayUnavailable(RuntimeError):
    """A previous query may have run but no durable response is available."""


def _parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _event_time(event: dict[str, Any]) -> datetime | None:
    for key in ("occurred_at", "message_time", "ts", "timestamp", "event_time", "time"):
        parsed = _parse_time(event.get(key))
        if parsed is not None:
            return parsed
    return None


def _load_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(path)
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if path.suffix.lower() == ".jsonl":
        out = []
        for line in text.splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                out.append(item)
        return out
    data = json.loads(text)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        events = data.get("events")
        if isinstance(events, list):
            return [item for item in events if isinstance(item, dict)]
        return [data]
    return []


def _events_from_payload(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        events = data.get("events")
        if isinstance(events, list):
            return [item for item in events if isinstance(item, dict)]
        return [data]
    return []


def _backend_query_url() -> str | None:
    raw = os.environ.get("AUTOPHONE_TELEMETRY_BACKEND_URL")
    return raw.strip() if raw and raw.strip() else None


def _backend_timeout_s() -> float:
    raw = os.environ.get("AUTOPHONE_TELEMETRY_BACKEND_TIMEOUT_S")
    if not raw:
        return 10.0
    try:
        return max(0.1, float(raw))
    except ValueError:
        return 10.0


def _query_backend_events(
    *,
    url: str,
    spec: dict[str, Any],
    profile_raw: dict[str, Any],
    timestamps: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    params = {
        "run_id": str(spec.get("run_id") or ""),
        "app": str(spec.get("app") or ""),
        "device": str(spec.get("device") or ""),
        "env": str(profile_raw.get("env") or ""),
        "started_at": str(timestamps.get("execute_started_at") or ""),
        "ended_at": str(timestamps.get("execute_ended_at") or ""),
    }
    headers: dict[str, str] = {}
    token = os.environ.get("AUTOPHONE_TELEMETRY_BACKEND_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.get(url, params=params, headers=headers, timeout=_backend_timeout_s())
    response.raise_for_status()
    return _events_from_payload(response.json()), {
        "url_host": urlparse(url).netloc,
        "params": params,
    }


def telemetry_backend_doctor(
    *,
    backend_url: str | None = None,
    query: bool = False,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    env_url = _backend_query_url()
    url = (backend_url.strip() if backend_url else None) or env_url
    source = "argument" if backend_url else ("env" if env_url else "none")
    result: dict[str, Any] = {
        "schema_version": "v1",
        "configured": bool(url),
        "backend_source": source,
        "token_configured": bool(os.environ.get("AUTOPHONE_TELEMETRY_BACKEND_TOKEN")),
        "query": query,
        "queried": False,
        "status": "configured" if url else "not_configured",
    }
    if not url:
        return result
    result["url_host"] = urlparse(url).netloc
    if not query:
        return result

    params = {
        "run_id": "autophone-telemetry-doctor",
        "app": "doctor",
        "device": "doctor",
        "env": "",
        "started_at": "",
        "ended_at": "",
    }
    headers: dict[str, str] = {}
    token = os.environ.get("AUTOPHONE_TELEMETRY_BACKEND_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.get(url, params=params, headers=headers, timeout=timeout_s or _backend_timeout_s())
        response.raise_for_status()
        events = _events_from_payload(response.json())
    except Exception as exc:  # noqa: BLE001 - doctor should report adapter health, not throw
        result["queried"] = True
        result["status"] = "query_failed"
        result["detail"] = type(exc).__name__
        return result
    result.update({
        "queried": True,
        "status": "healthy",
        "event_count": len(events),
    })
    return result


def _action_points(phase2_result: Any) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for outcome in getattr(phase2_result, "outcomes", []) or []:
        for idx, action in enumerate(getattr(outcome, "actions", []) or [], 1):
            recorded_at = action.get("recorded_at") if isinstance(action, dict) else None
            points.append({
                "case_id": getattr(outcome, "id", ""),
                "action": action.get("action") if isinstance(action, dict) else "",
                "index": idx,
                "recorded_at": recorded_at,
                "elapsed_ms": action.get("elapsed_ms") if isinstance(action, dict) else None,
            })
    return points


def _latency_summary(events: list[dict[str, Any]], actions: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize event/action attribution skew, not dataTrace ingestion latency."""
    action_times = [_parse_time(action.get("recorded_at")) for action in actions]
    action_times = [item for item in action_times if item is not None]
    latencies: list[float] = []
    if not action_times:
        return {"count": 0, "samples": []}
    for event in events:
        ts = _event_time(event)
        if ts is None:
            continue
        nearest = min(action_times, key=lambda action_ts: abs((ts - action_ts).total_seconds()))
        latencies.append(round((ts - nearest).total_seconds() * 1000, 1))
    if not latencies:
        return {"count": 0, "samples": []}
    ordered = sorted(latencies)
    return {
        "count": len(ordered),
        "min_ms": ordered[0],
        "p50_ms": round(median(ordered), 1),
        "max_ms": ordered[-1],
        "samples": ordered[:20],
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    """Linear percentile compatible with the small M0 calibration sample sets."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile / 100.0
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _warehouse_latency_summary(events: list[Any]) -> dict[str, Any]:
    """Measure event occurrence -> dataTrace ingestion latency.

    Negative samples indicate a timestamp/zone contract problem and are excluded
    from percentiles while remaining visible in ``invalid_count``.
    """
    samples: list[float] = []
    invalid = 0
    missing = 0
    for event in events:
        occurred = getattr(event, "occurred_at", None)
        ingested = getattr(event, "ingested_at", None)
        if occurred is None or ingested is None:
            missing += 1
            continue
        latency_ms = (ingested - occurred).total_seconds() * 1000
        if latency_ms < 0:
            invalid += 1
            continue
        samples.append(latency_ms)
    ordered = sorted(samples)
    result: dict[str, Any] = {
        "count": len(ordered),
        "missing_count": missing,
        "invalid_count": invalid,
        "calibratable": bool(ordered) and not invalid,
    }
    if ordered:
        result.update({
            "min_ms": round(ordered[0], 1),
            "p50_ms": round(float(_percentile(ordered, 50) or 0), 1),
            "p95_ms": round(float(_percentile(ordered, 95) or 0), 1),
            "max_ms": round(ordered[-1], 1),
        })
    return result


def _load_event_dict(profile_raw: dict[str, Any]) -> dict[str, Any]:
    telemetry_cap = ((profile_raw.get("capabilities") or {}).get("telemetry") or {})
    dict_path = telemetry_cap.get("dict")
    if not dict_path:
        return {}
    if not profile_raw.get("app"):
        return {}
    # 相对路径按进程工作目录解析（既有行为）；App 资产根被移走时改按新根解析。
    root = (
        app_asset_path("apps", str(profile_raw["app"]))
        if app_asset_root_overridden()
        else Path("apps") / str(profile_raw["app"])
    )
    path = Path(str(dict_path))
    if not path.is_absolute():
        path = root / path
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"telemetry dict must be a mapping, got {type(raw).__name__}: {path}")
    return raw


def _expected_events(cases_path: Path, selected_ids: set[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for case in load_cases(cases_path):
        case_id = str(case.get("id") or "")
        if selected_ids and case_id not in selected_ids:
            continue
        for event in case.get("expects_telemetry") or []:
            out.append({"case_id": case_id, "event": str(event)})
    return out


def _nested_value(root: Any, dotted: str) -> Any:
    value = root
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _fixture_context(spec: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    inline = spec.get("fixture_context")
    if isinstance(inline, dict):
        return inline
    for relative in ("fixture/fixture_context.json", "fixture_context.json"):
        path = run_dir / relative
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _resolve_data_trace_identity(
    telemetry_cap: dict[str, Any],
    *,
    spec: dict[str, Any],
    run_dir: Path,
) -> tuple[DataTraceIdentity | None, dict[str, Any]]:
    # 录制页可为本次回放显式给手机号 / DeviceID。该值只跟随 worker
    # 的内存 spec，不写 run-spec.json / telemetry 产物；见 RunService.submit。
    runtime = spec.get("_runtime_telemetry_identity")
    if isinstance(runtime, dict):
        runtime_field = str(runtime.get("field") or "").strip()
        runtime_value = str(runtime.get("value") or "").strip()
        if runtime_field in {"phone_no", "device_id"} and runtime_value:
            return DataTraceIdentity(field=runtime_field, value=runtime_value), {
                "field": runtime_field,
                "resolved": True,
                "source": "record_replay_ui",
            }
    declaration = telemetry_cap.get("identity") or {}
    field = str(declaration.get("field") or "").strip()
    fixture = _fixture_context(spec, run_dir)
    for source in declaration.get("sources") or []:
        text = str(source or "").strip()
        value: Any = None
        source_kind: str | None = None
        if text.startswith("env:"):
            value = os.environ.get(text.removeprefix("env:"))
            source_kind = "env"
        elif text.startswith("fixture:"):
            value = _nested_value(fixture, text.removeprefix("fixture:"))
            source_kind = "fixture"
        elif text.startswith(_API_IDENTITY_PREFIX):
            value = _identity_from_api_capture(run_dir, text.removeprefix(_API_IDENTITY_PREFIX))
            source_kind = "api_capture"
        normalized = str(value or "").strip()
        if normalized:
            return DataTraceIdentity(field=field, value=normalized), {
                "field": field,
                "resolved": True,
                "source": source_kind,
            }
    return None, {"field": field or None, "resolved": False, "source": None}


def _identity_from_api_capture(run_dir: Path, selector: str) -> Any:
    """Resolve an explicitly allowlisted identity path from local API capture.

    Selector syntax is ``request.<json.path>`` or ``response.<json.path>``.
    Only JSON text bodies are inspected; headers, URLs, fuzzy key search and raw
    body persistence are deliberately unsupported.
    """
    body_name, dot, json_path = selector.partition(".")
    capture_key = _API_IDENTITY_BODIES.get(body_name)
    if not dot or not capture_key or not json_path:
        return None
    capture = run_dir / "planes" / "api" / "capture.jsonl"
    if not capture.exists():
        return None
    try:
        lines = capture.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    candidates: list[str] = []
    for line in lines:
        try:
            record = json.loads(line)
            body = json.loads(record.get(capture_key) or "")
        except (AttributeError, TypeError, ValueError):
            continue
        value = _nested_value(body, json_path)
        normalized = str(value or "").strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    # Multiple values mean the capture contains mixed identities. Fail closed.
    return candidates[0] if len(candidates) == 1 else None


def _identity_hash(run_id: str, value: str | None) -> str | None:
    if value in (None, ""):
        return None
    digest = hashlib.sha256(f"{run_id}:{value}".encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


def _case_attribution(
    occurred_at: datetime | None,
    identity_assurance: str,
    phase2_result: Any,
    *,
    clock_skew_s: float = 0.0,
) -> dict[str, Any]:
    unattributed = {
        "case_id": None,
        "action": None,
        "action_index": None,
        "attribution": {"mode": "unattributed", "confidence": "none", "delta_ms": None},
    }
    if occurred_at is None or identity_assurance == "response_conflict":
        return unattributed
    windows: list[tuple[datetime, datetime, Any]] = []
    for outcome in getattr(phase2_result, "outcomes", []) or []:
        started = _parse_time(getattr(outcome, "started_at", None))
        duration = getattr(outcome, "duration_ms", None)
        if started is None or not isinstance(duration, (int, float)):
            continue
        ended = started + timedelta(milliseconds=max(0.0, float(duration)))
        windows.append((started, ended, outcome))
    windows.sort(key=lambda item: item[0])
    for window_index, (started, ended, outcome) in enumerate(windows):
        lower = started
        if clock_skew_s > 0:
            lower = started - timedelta(seconds=clock_skew_s)
            # 只容忍事件时钟落在 case 开始之前；相邻 case 已结束的边界不能被跨过。
            if window_index:
                lower = max(lower, windows[window_index - 1][1])
        try:
            in_window = lower <= occurred_at <= ended
        except TypeError:
            continue
        if not in_window:
            continue
        preceding: list[tuple[datetime, int, dict[str, Any]]] = []
        for action_index, action in enumerate(getattr(outcome, "actions", []) or [], 1):
            if not isinstance(action, dict):
                continue
            action_time = _parse_time(action.get("recorded_at"))
            if action_time is None:
                continue
            try:
                if action_time <= occurred_at:
                    preceding.append((action_time, action_index, action))
            except TypeError:
                continue
        if not preceding:
            return {
                "case_id": str(getattr(outcome, "id", "") or "") or None,
                "action": None,
                "action_index": None,
                "attribution": {
                    "mode": "clock_skew_tolerated" if occurred_at < started else "time_inferred",
                    "confidence": "low",
                    "delta_ms": (
                        round((occurred_at - started).total_seconds() * 1000, 1)
                        if occurred_at < started else None
                    ),
                },
            }
        action_time, action_index, action = max(preceding, key=lambda item: item[0])
        delta_ms = round((occurred_at - action_time).total_seconds() * 1000, 1)
        return {
            "case_id": str(getattr(outcome, "id", "") or "") or None,
            "action": str(action.get("action") or "") or None,
            "action_index": action_index,
            "attribution": {
                "mode": "time_inferred",
                "confidence": "medium",
                "delta_ms": delta_ms,
            },
        }
    return unattributed


def _shareable_behavior_event(
    event: Any,
    *,
    run_id: str,
    phase2_result: Any,
    clock_skew_s: float = 0.0,
) -> dict[str, Any]:
    item = asdict(event)
    item.pop("raw", None)
    for key in ("occurred_at", "ingested_at"):
        value = item.get(key)
        item[key] = value.isoformat() if isinstance(value, datetime) else None
    for key in ("device_id", "user_code", "session_id", "sid"):
        item[key] = _identity_hash(run_id, item.get(key))
    item.pop("city", None)
    item["content"] = redact_sensitive(item.get("content"))
    item["content_cache"] = redact_sensitive(item.get("content_cache"))
    item["i_ext"] = redact_sensitive(item.get("i_ext"))
    item["request_params"] = redact_sensitive(item.get("request_params"))
    item["request_url"] = redact_url(item.get("request_url"))
    item["request_error"] = redact_sensitive(item.get("request_error"))
    item.update(_case_attribution(
        event.occurred_at,
        event.identity_assurance,
        phase2_result,
        clock_skew_s=clock_skew_s,
    ))
    return item


def _compiled_event_dict(value: Any) -> EventDictionary:
    if isinstance(value, EventDictionary):
        return value
    return compile_event_dictionary(value)


def _evaluate_assertions(
    *,
    cases_path: Path | None,
    phase2_result: Any,
    event_dict: Any,
    raw_events: list[dict[str, Any]],
    expected_events: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    if cases_path is None and expected_events is None:
        return []
    selected_ids = {str(outcome.id) for outcome in getattr(phase2_result, "outcomes", []) or []}
    expected = expected_events if expected_events is not None else _expected_events(cases_path, selected_ids)
    expected = [item for item in expected if not selected_ids or item['case_id'] in selected_ids]
    dictionary = _compiled_event_dict(event_dict)
    assertions: list[dict[str, Any]] = []
    for item in expected:
        event_name = item["event"]
        if not dictionary.has(event_name):
            assertions.append({
                "case_id": item["case_id"],
                "event": event_name,
                "verdict": "fail",
                "detail": "missing telemetry dictionary rule",
            })
            continue
        matches = [event for event in raw_events if dictionary.matches(event_name, event)]
        if not matches:
            assertions.append({
                "case_id": item["case_id"],
                "event": event_name,
                "verdict": "fail",
                "detail": "event not observed",
            })
            continue
        missing = dictionary.missing_required(event_name, matches)
        if missing:
            assertions.append({
                "case_id": item["case_id"],
                "event": event_name,
                "verdict": "fail",
                "detail": "missing required fields: " + ", ".join(missing),
                "match_count": len(matches),
            })
            continue
        assertions.append({
            "case_id": item["case_id"],
            "event": event_name,
            "verdict": "pass",
            "detail": f"observed {len(matches)} matching event(s)",
            "match_count": len(matches),
        })
    return assertions


def _required_gate_result(
    telemetry_cap: dict[str, Any],
    attempts: list[dict[str, Any]],
    *,
    polling_complete: bool,
    has_identity_conflict: bool,
    dictionary_valid: bool,
) -> dict[str, Any]:
    """判断“最终未命中”能否升级为真实漏埋 fail。

    命中事件本身足以判 pass；只有缺失结论需要证明列集、channel、身份和入仓延迟均已
    校准，并且本轮查询完整、无截断/坏行/身份冲突。
    """

    configured = telemetry_cap.get("required_gate")
    configured = configured if isinstance(configured, dict) else {}
    observed_versions = {
        str(item.get("column_set_version") or "") for item in attempts
    } - {""}
    configured_version = str(configured.get("column_set_version") or "")
    blockers: list[str] = []
    if not configured_version or observed_versions != {configured_version}:
        blockers.append("column_set_unverified")
    if configured.get("channel_verified") is not True:
        blockers.append("channel_unverified")
    if configured.get("identity_verified") is not True:
        blockers.append("identity_unverified")
    if configured.get("latency_calibrated") is not True:
        blockers.append("latency_uncalibrated")
    if not attempts:
        blockers.append("query_unavailable")
    if not polling_complete:
        blockers.append("deadline_not_reached")
    if any(item.get("possibly_truncated") for item in attempts):
        blockers.append("result_possibly_truncated")
    if any(int(item.get("malformed") or 0) for item in attempts):
        blockers.append("malformed_rows")
    if has_identity_conflict:
        blockers.append("identity_conflict")
    if not dictionary_valid:
        blockers.append("dictionary_invalid")
    blockers = list(dict.fromkeys(blockers))
    return {
        "ready": not blockers,
        "blockers": blockers,
        "configured": {
            "column_set_version": configured_version or None,
            "channel_verified": configured.get("channel_verified") is True,
            "identity_verified": configured.get("identity_verified") is True,
            "latency_calibrated": configured.get("latency_calibrated") is True,
        },
        "observed_column_set_versions": sorted(observed_versions),
        "polling_complete": polling_complete,
    }


def _write_data_trace_artifacts(
    *,
    run_dir: Path,
    spec: dict[str, Any],
    profile_raw: dict[str, Any],
    phase2_result: Any,
    timestamps: dict[str, str],
    status: str,
    identity_summary: dict[str, Any],
    attempts: list[dict[str, Any]],
    events: list[Any],
    assertions: list[dict[str, Any]],
    gaps: list[str],
    event_dictionary: EventDictionary | None = None,
    verdict_gate: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    actions = _action_points(phase2_result)
    dictionary = event_dictionary or compile_event_dictionary({})
    telemetry_cap = ((profile_raw.get("capabilities") or {}).get("telemetry") or {})
    try:
        clock_skew_s = max(0.0, float(telemetry_cap.get("attribution_clock_skew_s", 0)))
    except (TypeError, ValueError):
        clock_skew_s = 0.0
    shareable = [
        dictionary.annotate(_shareable_behavior_event(
            event,
            run_id=str(spec["run_id"]),
            phase2_result=phase2_result,
            clock_skew_s=clock_skew_s,
        ))
        for event in events
    ]
    payload = {
        "schema_version": "v1",
        "run_id": spec["run_id"],
        "app": spec["app"],
        "device": spec["device"],
        "env": profile_raw.get("env"),
        "mode": "backend_readback",
        "status": status,
        "window": {
            "started_at": timestamps.get("execute_started_at"),
            "ended_at": timestamps.get("execute_ended_at"),
        },
        "query": {
            "provider": "data_trace",
            "identity": identity_summary,
            "attempt_count": len(attempts),
            "summary": "planes/telemetry/query-summary.json",
        },
        "actions": actions,
        "events": shareable,
        "event_count": len(events),
        "assertions": assertions,
        "assertion_counts": {
            "pass": len([item for item in assertions if item.get("verdict") == "pass"]),
            "fail": len([item for item in assertions if item.get("verdict") == "fail"]),
        },
        "verdict_gate": verdict_gate,
        # ``latency_ms`` is retained for report/API compatibility; its precise
        # name makes clear it must never be used as the dataTrace p95 gate.
        "latency_ms": _latency_summary(shareable, actions),
        "attribution_latency_ms": _latency_summary(shareable, actions),
        "warehouse_latency_ms": _warehouse_latency_summary(events),
        "gaps": gaps,
    }
    plane_dir = run_dir / "planes" / "telemetry"
    write_json(plane_dir / "raw.json", {
        "schema_version": "v1",
        "run_id": spec["run_id"],
        "provider": "data_trace",
        "events": [event.raw for event in events],
    })
    write_json(plane_dir / "query-summary.json", {
        "schema_version": "v1",
        "run_id": spec["run_id"],
        "provider": "data_trace",
        "identity": identity_summary,
        "attempts": attempts,
        "warehouse_latency_ms": _warehouse_latency_summary(events),
        "verdict_gate": verdict_gate,
        "gaps": gaps,
    })
    path = write_json(plane_dir / "events.json", payload)
    return path, payload


def _collect_data_trace(
    *,
    run_dir: Path,
    spec: dict[str, Any],
    profile_raw: dict[str, Any],
    phase2_result: Any,
    timestamps: dict[str, str],
    cases_path: Path | None,
    data_trace_client: Any | None,
    sleep: Callable[[float], None],
    frozen_inputs: dict[str, Any] | None = None,
    query_once: Any | None = None,
) -> tuple[Path, dict[str, Any]]:
    telemetry_cap = ((profile_raw.get("capabilities") or {}).get("telemetry") or {})
    identity, identity_summary = _resolve_data_trace_identity(
        telemetry_cap,
        spec=spec,
        run_dir=run_dir,
    )
    if frozen_inputs is not None:
        identity_digest = _identity_hash(str(spec['run_id']), identity.value) if identity else None
        if identity_digest != frozen_inputs['identity_hash']:
            raise ValueError('frozen telemetry identity changed')
        identity_summary = frozen_inputs['identity_summary']
        if not frozen_inputs.get('config_error'):
            config_hash = hashlib.sha256(json.dumps(asdict(config_from_env(
                str(telemetry_cap.get('endpoint') or 'dev'))), sort_keys=True).encode()).hexdigest()
            if config_hash != frozen_inputs['config_hash']:
                raise ValueError('frozen telemetry connection settings changed')
    if identity is None:
        return _write_data_trace_artifacts(
            run_dir=run_dir,
            spec=spec,
            profile_raw=profile_raw,
            phase2_result=phase2_result,
            timestamps=timestamps,
            status="identity_unavailable",
            identity_summary=identity_summary,
            attempts=[],
            events=[],
            assertions=[],
            gaps=["telemetry_identity_unavailable"],
        )

    started = _parse_time(timestamps.get("execute_started_at"))
    ended = _parse_time(timestamps.get("execute_ended_at"))
    if started is None or ended is None:
        return _write_data_trace_artifacts(
            run_dir=run_dir,
            spec=spec,
            profile_raw=profile_raw,
            phase2_result=phase2_result,
            timestamps=timestamps,
            status="window_unavailable",
            identity_summary=identity_summary,
            attempts=[],
            events=[],
            assertions=[],
            gaps=["telemetry_window_unavailable"],
        )
    try:
        padding = max(0.0, float(telemetry_cap.get("query_padding_s", 0)))
    except (TypeError, ValueError):
        padding = 0.0
    try:
        if frozen_inputs is not None and frozen_inputs.get('config_error'):
            raise ValueError(frozen_inputs['config_error'])
        query = DataTraceQuery(
            platform_id=int(telemetry_cap.get("platform_id")),
            brand_id=int(telemetry_cap.get("brand_id")),
            channel=str(telemetry_cap.get("channel") or ""),
            started_at=started - timedelta(seconds=padding),
            ended_at=ended + timedelta(seconds=padding),
            identity=identity,
        )
        client = data_trace_client or DataTraceClient(
            config_from_env(str(telemetry_cap.get("endpoint") or "dev"))
        )
    except Exception as exc:  # noqa: BLE001 - invalid profile becomes a telemetry gap
        return _write_data_trace_artifacts(
            run_dir=run_dir,
            spec=spec,
            profile_raw=profile_raw,
            phase2_result=phase2_result,
            timestamps=timestamps,
            status="configuration_error",
            identity_summary=identity_summary,
            attempts=[],
            events=[],
            assertions=[],
            gaps=[f"telemetry_data_trace_configuration_error:{type(exc).__name__}"],
        )

    selected_ids = {str(outcome.id) for outcome in getattr(phase2_result, "outcomes", []) or []}
    expected = (frozen_inputs['expected_events'] if frozen_inputs is not None else
                (_expected_events(cases_path, selected_ids) if cases_path is not None else []))
    expected = [item for item in expected if not selected_ids or item['case_id'] in selected_ids]
    if expected:
        delays = telemetry_cap.get("poll_delays_s")
    elif str(spec.get("type") or "") == "operation":
        # dataTrace 是异步上报链。录制回放没有 expects_telemetry 可驱动多轮
        # polling，但仍须给仓库 10s 可见性窗口，避免动作刚结束就查出假空。
        delays = [telemetry_cap.get("record_replay_delay_s", 10)]
    else:
        delays = [0]
    if not isinstance(delays, list) or not delays:
        delays = [0]
    attempts: list[dict[str, Any]] = []
    merged: dict[str, Any] = {}
    gaps: list[str] = []
    try:
        event_dict = _compiled_event_dict(_frozen_dictionary(profile_raw, frozen_inputs))
    except (ValueError, yaml.YAMLError, TelemetryDictionaryError):
        event_dict = compile_event_dictionary({})
        gaps.append("telemetry_dictionary_invalid")
    assertions: list[dict[str, Any]] = []
    dictionary_valid = "telemetry_dictionary_invalid" not in gaps
    successful_queries = 0
    polling_complete = False
    for index, raw_delay in enumerate(delays, 1):
        try:
            delay = max(0.0, float(raw_delay))
        except (TypeError, ValueError):
            delay = 0.0
        if delay:
            sleep(delay)
        try:
            batch = (query_once(f'telemetry_query_{index}', asdict(query), lambda: client.query(query))
                     if query_once is not None else client.query(query))
        except TelemetryReplayUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - adapter gap must not become product failure
            gaps.append(f"telemetry_data_trace_error:{type(exc).__name__}")
            break
        successful_queries += 1
        for event in batch.events:
            merged[event.event_id] = event
        attempts.append({
            "attempt": index,
            "delay_s": delay,
            "queried_at": batch.queried_at.isoformat(),
            "raw_count": batch.raw_count,
            "event_count": len(batch.events),
            "malformed": batch.malformed,
            "effective_limit": batch.effective_limit,
            "possibly_truncated": batch.possibly_truncated,
            "query_fingerprint": batch.query_fingerprint,
            "column_set_version": batch.column_set_version,
            "identity_assurance": dict(Counter(event.identity_assurance for event in batch.events)),
            "event_id_sources": dict(Counter(event.event_id_source for event in batch.events)),
        })
        current = [
            _shareable_behavior_event(
                event,
                run_id=str(spec["run_id"]),
                phase2_result=phase2_result,
                clock_skew_s=max(
                    0.0,
                    float(telemetry_cap.get("attribution_clock_skew_s", 0) or 0),
                ),
            )
            for event in merged.values()
        ]
        assertions = _evaluate_assertions(
            cases_path=cases_path,
            phase2_result=phase2_result,
            event_dict=event_dict,
            raw_events=current,
            expected_events=expected,
        ) if expected else []
        if assertions and all(item.get("verdict") == "pass" for item in assertions):
            break
        if any(item.get("detail") == "missing telemetry dictionary rule" for item in assertions):
            gaps.append("telemetry_dictionary_rule_missing")
            break
        if index == len(delays):
            polling_complete = True

    events = list(merged.values())
    has_identity_conflict = any(
        event.identity_assurance == "response_conflict" for event in events
    )
    if has_identity_conflict:
        gaps.append("telemetry_identity_conflict")
    if any(int(item.get("malformed") or 0) for item in attempts):
        gaps.append("telemetry_malformed_rows")
    if any(item.get("possibly_truncated") for item in attempts):
        gaps.append("telemetry_result_possibly_truncated")
    verdict_gate = _required_gate_result(
        telemetry_cap,
        attempts,
        polling_complete=polling_complete,
        has_identity_conflict=has_identity_conflict,
        dictionary_valid=dictionary_valid,
    )
    if successful_queries == 0:
        status = "adapter_error"
    elif has_identity_conflict:
        status = "identity_conflict"
    elif assertions and all(item.get("verdict") == "pass" for item in assertions):
        status = "asserted"
    elif expected:
        if verdict_gate["ready"]:
            status = "assertion_failed"
        else:
            status = "assertion_pending"
            gaps.append("telemetry_assertion_not_proven")
            gaps.extend(
                f"telemetry_required_gate_{blocker}"
                for blocker in verdict_gate["blockers"]
            )
    elif events:
        status = "captured"
    else:
        status = "empty"
    return _write_data_trace_artifacts(
        run_dir=run_dir,
        spec=spec,
        profile_raw=profile_raw,
        phase2_result=phase2_result,
        timestamps=timestamps,
        status=status,
        identity_summary=identity_summary,
        attempts=attempts,
        events=events,
        assertions=assertions,
        gaps=list(dict.fromkeys(gaps)),
        event_dictionary=event_dict,
        verdict_gate=verdict_gate,
    )


def _frozen_dictionary(profile_raw, frozen_inputs):
    if frozen_inputs is None:
        return _load_event_dict(profile_raw)
    if frozen_inputs.get('dictionary_error'):
        raise ValueError(frozen_inputs['dictionary_error'])
    return frozen_inputs['dictionary']


def freeze_telemetry_inputs(*, profile_raw, spec, run_dir, timestamps, case_docs):
    """Freeze non-secret query settings and assertion inputs without querying a backend."""
    cap = (profile_raw.get('capabilities') or {}).get('telemetry') or {}
    if not cap:
        return None
    frozen = {'schema_version': 'telemetry-inputs/v1',
              'profile_raw': {'app': profile_raw.get('app'), 'env': profile_raw.get('env'),
                              'capabilities': {'telemetry': copy.deepcopy(cap)}},
              'timestamps': copy.deepcopy(timestamps),
              'expected_events': [{'case_id': str(case['id']), 'event': str(event)}
                                  for case in case_docs for event in case.get('expects_telemetry') or []],
              'backend_url': _backend_query_url(), 'dictionary': {}, 'dictionary_error': None,
              'events': None, 'source_error': None,
              'source': os.environ.get('AUTOPHONE_TELEMETRY_EVENTS_JSON')}
    try:
        frozen['dictionary'] = _load_event_dict(profile_raw)
    except (ValueError, yaml.YAMLError, TelemetryDictionaryError) as exc:
        frozen['dictionary_error'] = type(exc).__name__
    if frozen['source']:
        try:
            frozen['events'] = _load_events(Path(frozen['source']))
        except (OSError, ValueError) as exc:
            frozen['source_error'] = type(exc).__name__
    identity, summary = _resolve_data_trace_identity(cap, spec=spec, run_dir=run_dir)
    frozen['identity_hash'] = _identity_hash(str(spec['run_id']), identity.value) if identity else None
    frozen['identity_summary'] = summary
    if cap.get('provider') == 'data_trace' and cap.get('backend_query') and not frozen['source']:
        try:
            frozen['config_hash'] = hashlib.sha256(json.dumps(asdict(config_from_env(
                str(cap.get('endpoint') or 'dev'))), sort_keys=True).encode()).hexdigest()
        except (ValueError, DataTraceConfigurationError) as exc:
            frozen['config_error'] = type(exc).__name__
    return frozen


def collect_telemetry_record(
    *,
    run_dir: Path,
    spec: dict[str, Any],
    profile_raw: dict[str, Any],
    phase2_result: Any,
    timestamps: dict[str, str],
    events_source: Path | None = None,
    cases_path: Path | None = None,
    data_trace_client: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
    frozen_inputs: dict[str, Any] | None = None,
    query_once: Any | None = None,
) -> tuple[Path, dict[str, Any]]:
    if frozen_inputs is not None:
        profile_raw = frozen_inputs['profile_raw']
        timestamps = frozen_inputs['timestamps']
    telemetry_cap = ((profile_raw.get("capabilities") or {}).get("telemetry") or {})
    source = events_source
    if frozen_inputs is not None:
        source = Path(frozen_inputs['source']) if frozen_inputs['source'] else None
    elif source is None and os.environ.get("AUTOPHONE_TELEMETRY_EVENTS_JSON"):
        source = Path(str(os.environ["AUTOPHONE_TELEMETRY_EVENTS_JSON"]))
    if (
        source is None
        and telemetry_cap.get("backend_query")
        and telemetry_cap.get("provider") == "data_trace"
    ):
        return _collect_data_trace(
            run_dir=run_dir,
            spec=spec,
            profile_raw=profile_raw,
            phase2_result=phase2_result,
            timestamps=timestamps,
            cases_path=cases_path,
            data_trace_client=data_trace_client,
            sleep=sleep,
            frozen_inputs=frozen_inputs,
            query_once=query_once,
        )
    backend_url = frozen_inputs['backend_url'] if frozen_inputs is not None else _backend_query_url()
    backend_query: dict[str, Any] | None = None

    gaps: list[str] = []
    raw_events: list[dict[str, Any]] = []
    status = "not_configured"
    if not telemetry_cap.get("backend_query"):
        gaps.append("telemetry_backend_query_not_declared")
    elif source is None:
        if backend_url is None:
            gaps.append("telemetry_events_source_not_configured")
        else:
            try:
                def query_backend():
                    return _query_backend_events(url=backend_url, spec=spec,
                                                 profile_raw=profile_raw, timestamps=timestamps)
                request = {'url': backend_url, 'spec': spec, 'profile_raw': profile_raw, 'timestamps': timestamps}
                raw_events, backend_query = (query_once('telemetry_backend', request, query_backend)
                                              if query_once is not None else query_backend())
                status = "recorded"
            except TelemetryReplayUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - adapter health is evidence, not run-fatal
                status = "adapter_error"
                gaps.append(f"telemetry_backend_query_error:{type(exc).__name__}:{exc}")
    else:
        try:
            if frozen_inputs is not None and frozen_inputs.get('source_error'):
                raise ValueError(frozen_inputs['source_error'])
            raw_events = frozen_inputs['events'] if frozen_inputs is not None else _load_events(source)
            status = "recorded"
        except Exception as exc:  # noqa: BLE001 - D1 records adapter state, it does not fail the run
            status = "adapter_error"
            gaps.append(f"telemetry_events_source_error:{type(exc).__name__}:{exc}")

    actions = _action_points(phase2_result)
    try:
        event_dict = _compiled_event_dict(_frozen_dictionary(profile_raw, frozen_inputs))
    except (ValueError, yaml.YAMLError, TelemetryDictionaryError) as exc:
        # 字典坏掉是配置问题，记入 gaps 即可，不能拖垮 collect 段。
        event_dict = compile_event_dictionary({})
        gaps.append(f"telemetry_dict_error:{exc}")
    assertions = _evaluate_assertions(
        cases_path=cases_path,
        phase2_result=phase2_result,
        event_dict=event_dict,
        raw_events=raw_events,
        expected_events=frozen_inputs['expected_events'] if frozen_inputs is not None else None,
    ) if raw_events else []
    if raw_events and assertions:
        status = "asserted"
    events = [event_dict.annotate(item) for item in redact_sensitive(raw_events)]
    payload = {
        "schema_version": "v1",
        "run_id": spec["run_id"],
        "app": spec["app"],
        "device": spec["device"],
        "env": profile_raw.get("env"),
        "mode": "record_only",
        "status": status,
        "window": {
            "started_at": timestamps.get("execute_started_at"),
            "ended_at": timestamps.get("execute_ended_at"),
        },
        "query": {
            "backend_query_declared": bool(telemetry_cap.get("backend_query")),
            "dict": telemetry_cap.get("dict"),
            "source": str(source) if source else None,
            "backend": backend_query,
        },
        "actions": actions,
        "events": events,
        "event_count": len(raw_events),
        "assertions": assertions,
        "assertion_counts": {
            "pass": len([item for item in assertions if item["verdict"] == "pass"]),
            "fail": len([item for item in assertions if item["verdict"] == "fail"]),
        },
        "latency_ms": _latency_summary(raw_events, actions),
        "gaps": gaps,
    }
    path = write_json(run_dir / "planes" / "telemetry" / "events.json", payload)
    return path, payload


def _manual_time(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise TelemetryQueryValidationError(f"{field} must be an ISO datetime string")
    text = value.strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise TelemetryQueryValidationError(f"{field} must be an ISO datetime string") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TelemetryQueryValidationError(f"{field} must include a timezone offset")
    return parsed.astimezone(_SHANGHAI)


def _manual_filter(raw: Any) -> DataTraceFilter:
    if not isinstance(raw, dict):
        raise TelemetryQueryValidationError("each telemetry filter must be an object")
    if set(raw) - {"field", "op", "value", "json_field_key"}:
        raise TelemetryQueryValidationError("telemetry filter contains unknown keys")
    field = str(raw.get("field") or "").strip()
    op = str(raw.get("op") or "").strip()
    try:
        operator = FilterOperator(op)
    except ValueError as exc:
        raise TelemetryQueryValidationError("telemetry filter operator is not allowed") from exc
    value = raw.get("value")
    if operator is FilterOperator.IN:
        if not isinstance(value, list):
            raise TelemetryQueryValidationError("in filter value must be an array")
        value = tuple(str(item) for item in value)
    elif isinstance(value, (dict, list)):
        raise TelemetryQueryValidationError("telemetry filter value must be scalar")
    return DataTraceFilter(
        field=field,
        op=operator,
        value=value,
        json_field_key=(str(raw.get("json_field_key") or "").strip() or None),
    )


def _distribution(events: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    counts = Counter(str(item.get(field) or "未设置") for item in events)
    return [
        {"value": value, "count": count}
        for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _manual_query_summary(
    events: list[dict[str, Any]],
    *,
    batch: Any,
) -> dict[str, Any]:
    minute_counts: Counter[str] = Counter()
    for event in events:
        occurred = _parse_time(event.get("occurred_at"))
        if occurred is not None:
            minute_counts[occurred.isoformat(timespec="minutes")] += 1
    abnormal = 0
    for event in events:
        status = str(event.get("request_status") or "")
        try:
            bad_status = int(status) >= 400
        except ValueError:
            bad_status = False
        if bad_status or event.get("request_error"):
            abnormal += 1
    return {
        "event_count": len(events),
        "unattributed_count": sum(1 for item in events if not item.get("case_id")),
        "abnormal_request_count": abnormal,
        "raw_count": batch.raw_count,
        "malformed": batch.malformed,
        "effective_limit": batch.effective_limit,
        "possibly_truncated": batch.possibly_truncated,
        "queried_at": batch.queried_at.isoformat(),
        "query_fingerprint": batch.query_fingerprint,
        "column_set_version": batch.column_set_version,
        "identity_assurance": dict(Counter(item.get("identity_assurance") for item in events)),
        "event_id_sources": dict(Counter(item.get("event_id_source") for item in events)),
        "minute_trend": [
            {"minute": minute, "count": count}
            for minute, count in sorted(minute_counts.items())
        ],
        "distributions": {
            field: _distribution(events, field)
            for field in ("kind", "category", "classify")
        },
    }


def _attributes(
    events: list[dict[str, Any]],
    *,
    telemetry_cap: dict[str, Any],
    profile_raw: dict[str, Any],
    identity: DataTraceIdentity,
    identity_hash: str,
) -> dict[str, Any]:
    def values(field: str) -> list[str]:
        return sorted({str(item[field]) for item in events if item.get(field) not in (None, "")})

    return {
        "brand": values("brand"),
        "platform": (
            "iOS App" if telemetry_cap.get("platform_id") == 1
            else f"platform-{telemetry_cap.get('platform_id')}"
        ),
        "app_version": values("app_version"),
        "device_type": values("device_type"),
        "os_version": values("os_version"),
        "env": values("env") or ([str(profile_raw.get("env"))] if profile_raw.get("env") else []),
        "identity": {"field": identity.field, "masked": identity_hash},
        "sdk_bundles": values("bundle_id"),
    }


def query_telemetry(
    *,
    app: str,
    profile_raw: dict[str, Any],
    request: dict[str, Any],
    data_trace_client: Any | None = None,
) -> dict[str, Any]:
    """执行一次人工 dataTrace 查询并只返回标准化、脱敏、可分享的结果。"""

    if not isinstance(request, dict):
        raise TelemetryQueryValidationError("telemetry query body must be an object")
    if set(request) - {"started_at", "ended_at", "identity", "filters", "limit"}:
        raise TelemetryQueryValidationError("telemetry query contains unknown keys")
    telemetry_cap = ((profile_raw.get("capabilities") or {}).get("telemetry") or {})
    if not telemetry_cap.get("backend_query") or telemetry_cap.get("provider") != "data_trace":
        raise TelemetryQueryValidationError("app does not declare data_trace telemetry")
    raw_identity = request.get("identity")
    if not isinstance(raw_identity, dict) or set(raw_identity) != {"field", "value"}:
        raise TelemetryQueryValidationError("identity must contain exactly field and value")
    identity_field = str(raw_identity.get("field") or "").strip()
    identity_value = raw_identity.get("value")
    if identity_field not in {"phone_no", "user_code", "device_id", "session_id", "sid"}:
        raise TelemetryQueryValidationError("identity field is not allowed")
    if not isinstance(identity_value, str) or not identity_value.strip():
        raise TelemetryQueryValidationError("identity value must be a non-empty string")
    identity = DataTraceIdentity(
        field=identity_field,  # type: ignore[arg-type]
        value=identity_value.strip(),
    )
    raw_filters = request.get("filters", [])
    if not isinstance(raw_filters, list) or len(raw_filters) > 20:
        raise TelemetryQueryValidationError("filters must be an array with at most 20 items")
    filters = tuple(_manual_filter(item) for item in raw_filters)
    raw_limit = request.get("limit", 10_000)
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
        raise TelemetryQueryValidationError("limit must be an integer")
    started_at = _manual_time(request.get("started_at"), field="started_at")
    ended_at = _manual_time(request.get("ended_at"), field="ended_at")
    if ended_at < started_at:
        raise TelemetryQueryValidationError("ended_at must not be earlier than started_at")
    if ended_at - started_at > timedelta(days=3):
        raise TelemetryQueryValidationError("telemetry query window must not exceed 3 days")
    query = DataTraceQuery(
        platform_id=int(telemetry_cap.get("platform_id")),
        brand_id=int(telemetry_cap.get("brand_id")),
        channel=str(telemetry_cap.get("channel") or ""),
        started_at=started_at,
        ended_at=ended_at,
        identity=identity,
        filters=filters,
        limit=max(1, min(raw_limit, 10_000)),
    )
    client = data_trace_client or DataTraceClient(
        config_from_env(str(telemetry_cap.get("endpoint") or "dev"))
    )
    batch = client.query(query)
    try:
        dictionary = _compiled_event_dict(_load_event_dict(profile_raw))
    except (ValueError, yaml.YAMLError, TelemetryDictionaryError) as exc:
        raise TelemetryQueryValidationError("telemetry dictionary is invalid") from exc
    query_salt = f"manual:{secrets.token_hex(16)}"
    events = [
        dictionary.annotate(_shareable_behavior_event(
            event,
            run_id=query_salt,
            phase2_result=None,
        ))
        for event in batch.events
    ]
    summary = _manual_query_summary(events, batch=batch)
    return {
        "schema_version": "v1",
        "app": app,
        "env": profile_raw.get("env"),
        "window": {
            "started_at": query.started_at.isoformat(),
            "ended_at": query.ended_at.isoformat(),
        },
        "summary": summary,
        "attributes": _attributes(
            events,
            telemetry_cap=telemetry_cap,
            profile_raw=profile_raw,
            identity=identity,
            identity_hash=_identity_hash(query_salt, identity.value) or "redacted",
        ),
        "events": events,
    }


def telemetry_view_from_payload(
    payload: dict[str, Any],
    *,
    query_summary: dict[str, Any] | None = None,
    case_id: str | None = None,
) -> dict[str, Any]:
    """把落盘脱敏事件整理为 Web 所需的 case → action 轨迹。"""

    events = [item for item in (payload.get("events") or []) if isinstance(item, dict)]
    assertions = [item for item in (payload.get("assertions") or []) if isinstance(item, dict)]
    if case_id is not None:
        events = [item for item in events if str(item.get("case_id") or "") == case_id]
        assertions = [item for item in assertions if str(item.get("case_id") or "") == case_id]
    grouped: dict[str, dict[tuple[int | None, str], list[dict[str, Any]]]] = {}
    for event in events:
        case_key = str(event.get("case_id") or "unattributed")
        action_key = str(event.get("action") or "未归因动作")
        raw_index = event.get("action_index")
        action_index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else None
        grouped.setdefault(case_key, {}).setdefault((action_index, action_key), []).append(event)
    groups = []
    for case_key in sorted(grouped, key=lambda value: (value == "unattributed", value)):
        actions = []
        ordered_actions = sorted(
            grouped[case_key].items(),
            key=lambda item: (
                item[0][0] is None,
                item[0][0] if item[0][0] is not None else 0,
                item[0][1],
            ),
        )
        for (action_index, action), items in ordered_actions:
            items.sort(key=lambda item: str(item.get("occurred_at") or ""))
            actions.append({
                "action": action,
                "action_index": action_index,
                "events": items,
                "event_count": len(items),
            })
        groups.append({
            "case_id": None if case_key == "unattributed" else case_key,
            "unattributed": case_key == "unattributed",
            "event_count": sum(item["event_count"] for item in actions),
            "actions": actions,
        })
    attempts = (query_summary or {}).get("attempts") or []
    return {
        "available": True,
        "run_id": payload.get("run_id"),
        "app": payload.get("app"),
        "status": payload.get("status"),
        "window": payload.get("window"),
        "event_count": len(events),
        "assertions": assertions,
        "assertion_counts": {
            "pass": sum(1 for item in assertions if item.get("verdict") == "pass"),
            "fail": sum(1 for item in assertions if item.get("verdict") == "fail"),
        },
        "query": {
            "attempt_count": len(attempts),
            "last_queried_at": attempts[-1].get("queried_at") if attempts else None,
            "possibly_truncated": any(item.get("possibly_truncated") for item in attempts),
            "verdict_gate": (query_summary or {}).get("verdict_gate"),
            "gaps": (query_summary or {}).get("gaps") or payload.get("gaps") or [],
        },
        "groups": groups,
    }


def load_telemetry_view(run_dir: Path, *, case_id: str | None = None) -> dict[str, Any]:
    plane_dir = run_dir / "planes" / "telemetry"
    events_path = plane_dir / "events.json"
    if not events_path.exists():
        return {
            "available": False,
            "run_id": run_dir.name,
            "case_id": case_id,
            "event_count": 0,
            "groups": [],
            "assertions": [],
        }
    try:
        payload = json.loads(events_path.read_text(encoding="utf-8"))
        summary_path = plane_dir / "query-summary.json"
        summary = (
            json.loads(summary_path.read_text(encoding="utf-8"))
            if summary_path.exists() else {}
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise TelemetryQueryValidationError("telemetry artifact is invalid") from exc
    if not isinstance(payload, dict) or not isinstance(summary, dict):
        raise TelemetryQueryValidationError("telemetry artifact is invalid")
    return telemetry_view_from_payload(payload, query_summary=summary, case_id=case_id)
