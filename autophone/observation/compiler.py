"""Deterministic Run Artifact to Observation compiler."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from autophone.assets import build_identity
from autophone.contracts import validate_or_raise, write_json
from autophone.util import read_json

OUTPUT = "observations.json"
CONTROL = "knowledge-control.json"
LEGACY_ACTION_RULE = "case-ordinal-sha256.v1"
IMPACT_ACTION_RULE = "impact-run-seq-sha256.v1"


def _id(prefix: str, *parts: Any) -> str:
    raw = "\0".join(str(part) for part in parts).encode()
    return f"{prefix}-{hashlib.sha256(raw).hexdigest()[:24]}"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _read_jsonl_strict(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at {path}:{line_no}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"JSONL row must be an object at {path}:{line_no}")
        rows.append(value)
    return rows


def _build_scope(identity: dict[str, Any]) -> dict[str, Any]:
    key = identity.get("subject_build_key")
    if key and identity.get("identity_state") in {"confirmed", "version_only"}:
        return {"state": "confirmed", "subject_build_key": key}
    return {"state": "unknown", "subject_build_key": None}


def _fallback_epoch(identity: dict[str, Any], run_id: str) -> str:
    live = [
        row for row in identity.get("subject_epochs") or []
        if row.get("superseded_at") is None
    ]
    if live:
        return str(live[-1].get("epoch_ref") or f"epoch-unconfirmed:{run_id}")
    epochs = identity.get("subject_epochs") or []
    if epochs:
        return str(epochs[-1].get("epoch_ref") or f"epoch-unconfirmed:{run_id}")
    return f"epoch-unconfirmed:{run_id}"


def _evidence_ref(run_dir: Path, value: Any) -> str:
    """Prefer Run-relative artifact references without inventing missing files."""
    path = Path(str(value))
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.relative_to(run_dir).as_posix()
    except ValueError:
        # Historical artifacts can embed the former checkout's absolute Run
        # path.  Recover only the suffix below the same run_id and never emit
        # a Host checkout path into the normalized observation.
        matching = [index for index, part in enumerate(path.parts) if part == run_dir.name]
        if matching and matching[-1] + 1 < len(path.parts):
            return Path(*path.parts[matching[-1] + 1:]).as_posix()
        return path.name


def _impact_observations(
    run_dir: Path,
    *,
    run_id: str,
    build_scope: dict[str, Any],
    subject_epoch_ref: str,
    source_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Project page-impact observations into the canonical knowledge model.

    ``observation.v1`` remains the append-only acquisition contract.  This
    adapter preserves its Screen/API association and evidence references while
    assigning deterministic legacy action IDs to historical rows that predate
    the cross-pipeline action identity contract.
    """
    path = run_dir / "impact" / "observations.jsonl"
    rows: list[dict[str, Any]] = []
    source_ref = path.relative_to(run_dir).as_posix()
    for source in source_rows if source_rows is not None else _read_jsonl_strict(path):
        validate_or_raise("observation.v1.json", source)
        action = source.get("action") or {}
        native_action_id = str(source.get("action_id") or "")
        legacy = not bool(native_action_id)
        action_id = native_action_id or _id(
            "legacy-impact-action",
            run_id,
            source.get("seq"),
            action.get("operation"),
            action.get("step"),
            IMPACT_ACTION_RULE,
        )
        epoch_ref = str(source.get("subject_epoch_ref") or subject_epoch_ref)
        from_state = source.get("from") or {}
        to_state = source.get("to") or {}
        frame_refs = [str(value) for value in source.get("frame_refs") or [] if value]
        if not frame_refs:
            frame_refs = [
                str(value)
                for value in (from_state.get("frame_ref"), to_state.get("frame_ref"))
                if value
            ]
        evidence_refs = [source_ref]
        evidence_refs.extend(frame_refs)
        if source.get("context_ref"):
            evidence_refs.append(str(source["context_ref"]))
        common = {
            "run_id": run_id,
            "case_id": None,
            "operation": action.get("operation"),
            "action_id": action_id,
            "subject_epoch_ref": epoch_ref,
            "occurred_at": str(action.get("t_start") or ""),
            "correlation_state": "legacy_derived" if legacy else "native",
            "legacy_rule_version": IMPACT_ACTION_RULE if legacy else None,
            "build_scope": build_scope,
        }
        rows.append({
            "id": _id("obs", run_id, action_id, "screen_transition"),
            "type": "screen_transition",
            **common,
            "producer": {
                "kind": "impact_observation_assembler",
                "state": "current",
                "liveness": (
                    "screen_checkpoint_and_action_window"
                    if source.get("status") == "passed"
                    else "blocked_window_recorded"
                ),
            },
            "evidence_refs": evidence_refs,
            "payload": {
                "reported_build_label": source.get("build"),
                "from_screen": from_state.get("screen_id"),
                "from_variant": from_state.get("variant") or {},
                "to_screen": to_state.get("screen_id"),
                "to_variant": to_state.get("variant") or {},
                "status": source.get("status"),
                "blocker": source.get("blocker"),
                "endpoint_count": len(source.get("endpoints") or []),
            },
        })
        for ordinal, endpoint in enumerate(source.get("endpoints") or [], 1):
            api_ref = str(endpoint.get("api_ref") or "")
            endpoint_evidence = [source_ref]
            if api_ref:
                endpoint_evidence.append(api_ref)
            rows.append({
                "id": _id(
                    "obs", run_id, action_id, "api_call",
                    endpoint.get("endpoint_key"), ordinal,
                ),
                "type": "api_call",
                **common,
                "producer": {
                    "kind": "impact_observation_assembler",
                    "state": "current",
                    "liveness": "canonical_endpoint_observed",
                },
                "evidence_refs": endpoint_evidence,
                "payload": {
                    "method": endpoint.get("method"),
                    "endpoint": (
                        f"{endpoint.get('host_alias') or ''}"
                        f"{endpoint.get('path_template') or ''}"
                    ),
                    "endpoint_key": endpoint.get("endpoint_key"),
                    "host": endpoint.get("host_alias"),
                    "path": endpoint.get("path_template"),
                    "status": endpoint.get("status"),
                    "step": action.get("step"),
                    "trigger": endpoint.get("trigger"),
                    "noise": bool(endpoint.get("noise")),
                    "response_paths": endpoint.get("response_paths") or [],
                    "from_screen": from_state.get("screen_id"),
                    "to_screen": to_state.get("screen_id"),
                    "api_ref": api_ref or None,
                    "reported_build_label": source.get("build"),
                    "not_observed": False,
                },
            })
    return rows


def _apply_control_bindings(
    observations: list[dict[str, Any]],
    control: dict[str, Any],
) -> None:
    validate_or_raise("knowledge-run-control.v1.json", control)
    bindings = list(control.get("bindings") or [])
    match_counts = [0] * len(bindings)
    assignments: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for observation in observations:
        matched: list[tuple[int, dict[str, Any]]] = []
        for index, binding in enumerate(bindings):
            match = binding.get("match") or {}
            if all(
                str(observation.get("id" if key == "observation_id" else key) or "") == str(value)
                for key, value in match.items()
            ):
                matched.append((index, binding))
        if len(matched) > 1:
            raise ValueError(f"multiple knowledge-control bindings match {observation.get('id')}")
        if not matched:
            continue
        index, binding = matched[0]
        match_counts[index] += 1
        assignments.append((observation, binding))
    missing = [index + 1 for index, count in enumerate(match_counts) if count == 0]
    if missing:
        label = ", ".join(str(index) for index in missing)
        noun = "binding" if len(missing) == 1 else "bindings"
        raise ValueError(f"knowledge-control {noun} {label} matched no observations")
    for observation, binding in assignments:
        if binding.get("contrast") is not None:
            observation["contrast"] = dict(binding["contrast"])
        if binding.get("independent_readback") is True:
            observation["independent_readback"] = True


def compile_run(run_dir: Path, *, app: str | None = None) -> dict[str, Any]:
    spec = read_json(run_dir / "run-spec.json") or {}
    platform = read_json(run_dir / "platform-run-result.json") or {}
    phase2 = read_json(run_dir / "result.json") or {}
    identity = build_identity.load_document(run_dir) or {}
    impact_path = run_dir / "impact" / "observations.jsonl"
    impact_rows = _read_jsonl_strict(impact_path)
    impact_meta = impact_rows[0] if impact_rows else {}
    run_id = str(
        platform.get("run_id") or spec.get("run_id")
        or impact_meta.get("run_id") or run_dir.name
    )
    resolved_app = str(
        app or platform.get("app") or spec.get("app")
        or impact_meta.get("app") or "unknown"
    )
    build_scope = _build_scope(identity)
    fallback_epoch = _fallback_epoch(identity, run_id)
    observations: list[dict[str, Any]] = []

    for case in phase2.get("cases") or []:
        case_id = str(case.get("id") or "")
        for ordinal, action in enumerate(case.get("actions") or [], 1):
            action_id = str(action.get("action_id") or "")
            legacy = not bool(action_id)
            if legacy:
                action_id = _id("legacy-action", run_id, case_id, ordinal, LEGACY_ACTION_RULE)
            epoch_ref = str(action.get("subject_epoch_ref") or fallback_epoch)
            evidence = list(dict.fromkeys(
                _evidence_ref(run_dir, value) for value in [
                    action.get("shot_path"), *(action.get("aux_shots") or [])
                ] if value
            ))
            observations.append({
                "id": _id("obs", run_id, action_id, "ui_action"),
                "type": "ui_action",
                "run_id": run_id,
                "case_id": case_id or None,
                "operation": action.get("operation"),
                "action_id": action_id,
                "subject_epoch_ref": epoch_ref,
                "occurred_at": str(action.get("recorded_at") or ""),
                "correlation_state": "legacy_derived" if legacy else "native",
                "legacy_rule_version": LEGACY_ACTION_RULE if legacy else None,
                "build_scope": build_scope,
                "producer": {
                    "kind": "action_engine",
                    "state": "legacy_unknown" if legacy else "current",
                    "liveness": "recorded_after_action",
                },
                "evidence_refs": evidence,
                "payload": {
                    "action": action.get("action"),
                    "ok": bool(action.get("ok")),
                    "kind": action.get("kind") or "",
                    "detail": action.get("detail") or "",
                },
            })

    capture_path = run_dir / "planes" / "api" / "capture.jsonl"
    capture_rows = [] if impact_path.is_file() else _read_jsonl(capture_path)
    for ordinal, call in enumerate(capture_rows, 1):
        native_action_id = str(call.get("action_id") or "")
        legacy = not bool(native_action_id)
        action_id = native_action_id or _id(
            "legacy-api-action", run_id, call.get("case_id"), call.get("step"), ordinal,
        )
        raw_url = str(call.get("url") or "")
        parsed = urlsplit(raw_url)
        observations.append({
            "id": _id("obs", run_id, action_id, "api_call", ordinal),
            "type": "api_call",
            "run_id": run_id,
            "case_id": call.get("case_id"),
            "operation": call.get("operation"),
            "action_id": action_id,
            "subject_epoch_ref": str(call.get("subject_epoch_ref") or fallback_epoch),
            "occurred_at": str(call.get("t_req") or ""),
            "correlation_state": "legacy_derived" if legacy else "native",
            "legacy_rule_version": LEGACY_ACTION_RULE if legacy else None,
            "build_scope": build_scope,
            "producer": {
                "kind": "net_tap",
                "state": "current" if identity else "legacy_unknown",
                "liveness": "response_observed" if call.get("t_resp") else "request_only",
            },
            "evidence_refs": [capture_path.relative_to(run_dir).as_posix()],
            "payload": {
                "method": call.get("method"),
                "endpoint": f"{parsed.netloc}{parsed.path}" if parsed.netloc else parsed.path,
                "host": parsed.netloc or call.get("host"),
                "path": parsed.path,
                "status": call.get("status"),
                "step": call.get("step"),
                "not_observed": False,
            },
        })

    observations.extend(_impact_observations(
        run_dir,
        run_id=run_id,
        build_scope=build_scope,
        subject_epoch_ref=fallback_epoch,
        source_rows=impact_rows,
    ))

    control_path = run_dir / CONTROL
    if control_path.is_file():
        control = read_json(control_path)
        if not isinstance(control, dict):
            raise ValueError("knowledge-control.json must contain an object")
        _apply_control_bindings(observations, control)

    payload = {
        "schema_version": "v1",
        "run_id": run_id,
        "app": resolved_app,
        "observations": observations,
    }
    validate_or_raise("observations.v1.json", payload)
    return payload


def compile_and_write(run_dir: Path, *, app: str | None = None) -> Path:
    path = write_json(run_dir / OUTPUT, compile_run(run_dir, app=app))
    manifest = run_dir / "artifact-manifest.json"
    if manifest.is_file():
        from autophone.observation.artifacts import Artifact, append_artifacts

        append_artifacts(run_dir, [
            Artifact(OUTPUT, "other", retention="local_only", share_policy="forbidden"),
            Artifact(CONTROL, "other", retention="local_only", share_policy="forbidden"),
        ])
    return path


def propose(observations: dict[str, Any], *, source: str = "deterministic") -> list[dict[str, Any]]:
    """Create reviewable draft candidates without changing the claim catalog."""
    claims: list[dict[str, Any]] = []
    for obs in observations.get("observations") or []:
        payload = obs.get("payload") or {}
        if (
            obs.get("type") == "api_call"
            and payload.get("endpoint")
            and not payload.get("noise")
        ):
            kind = "endpoint_observed"
            statement = (
                f"{payload.get('method') or 'UNKNOWN'} {payload['endpoint']} "
                f"was observed in action window {obs.get('action_id')}"
            )
        elif obs.get("type") == "ui_action" and payload.get("ok"):
            kind = "action_observed"
            statement = f"Action {payload.get('action')} completed in case {obs.get('case_id')}"
        else:
            continue
        claims.append({
            "id": _id("claim", kind, statement),
            "status": "draft",
            "kind": kind,
            "statement": statement,
            "scope": obs.get("build_scope") or {"state": "unknown"},
            "evidence_refs": [obs.get("id")],
            "evidence_level": "observed",
            "evidence_state": "current",
            "provenance": {"source": source},
        })
    return claims
