"""Verify controlled local delivery without claiming physical HTTP success."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Mapping

from autophone.observation.impact import controlled_input as inputs
from autophone.observation.impact import lifecycle_evidence as lifecycle
from autophone.observation.impact.comparison import _complete_request_body, json_value

RECEIPT_REL = "planes/api/private/controlled-receipt.json"


class ControlledEvidenceError(ValueError):
    """Public error with only a fixed code, never source content."""

    def __init__(self, code="controlled_delivery_invalid"):
        self.code = code
        super().__init__(code)


def _require(condition, code="controlled_delivery_invalid"):
    if not condition:
        raise ControlledEvidenceError(code)


def _int(value, expected):
    return type(value) is int and value == expected


def _read_ledger(root, spec, events):
    refs = {}
    for rel in (lifecycle.EVENTS_REL, "planes/api/capture.jsonl"):
        data = inputs._safe_path(root, rel).read_bytes()
        refs[rel] = inputs._sha(data)
    registry = json_value(
        inputs._safe_path(root, "artifact-manifest.json").read_bytes()
    )
    private = [
        item
        for item in registry["artifacts"]
        if item.get("path") == lifecycle.EVENTS_REL
    ]
    _require(
        len(private) == 1
        and all(
            private[0].get(k) == v
            for k, v in dict(
                retention="local_only",
                ai_policy="forbidden",
                share_policy="forbidden",
                redaction_status="raw",
            ).items()
        )
    )
    rows = [
        json_value(line)
        for line in inputs._safe_path(root, "planes/api/capture.jsonl")
        .read_bytes()
        .splitlines()
        if line.strip()
    ]
    ledger = lifecycle.read_sealed_ledger(
        root=root,
        run_id=spec["run_id"],
        rows=list(enumerate(rows, 1)),
        events_sha256=refs[lifecycle.EVENTS_REL],
        capture_sha256=refs["planes/api/capture.jsonl"],
        child=True,
    )
    _require(
        json.dumps(ledger.events, sort_keys=True) == json.dumps(events, sort_keys=True),
        "controlled_events_mismatch",
    )
    return ledger


def _native_local(state):
    """Local callbacks preserve suspended native task and zero transport."""
    history = state["lifecycle"]
    _require(bool(history))
    for event in history:
        _require(
            _int(event.get("state"), 1)
            and _int(event.get("bytes_sent"), 0)
            and _int(event.get("bytes_received"), 0)
            and event.get("native_response_present") is False
            and event.get("callback_complete") is False
            and event.get("terminal") is False
            and event.get("response_observed") is False
            and _int(event.get("data_callbacks"), 0)
            and _int(event.get("observed_data_bytes"), 0)
            and event.get("error_code") is None
            and event.get("status") is None
            and event.get("identity_ambiguous") is False
            and event.get("coverage_gap") is False
            and event.get("snapshot_errors") == []
            and lifecycle.coverage(event.get("hook_coverage"))
            and event.get("identity_source") == "creation"
            and event.get("kind") not in ("resume_observed", "cancel_requested")
        )
    _require(
        history[0].get("kind") == "task_observed"
        and history[-1].get("kind") == "synthetic_completion"
        and history[-1].get("synthetic_callback_complete") is True
        and history[-1].get("terminal") is False
    )


def _delivery_receipt(root, spec, events, cleanup):
    loaded = inputs.load_for_run(root, spec)
    _require(loaded is not None, "controlled_input_required")
    ledger = _read_ledger(root, spec, events)
    _require(len(ledger.groups) == 1)
    capture_id = next(iter(ledger.groups))
    configured = [e for e in events if e.get("ev") == "scenario_configured"]
    _require(len(configured) == 1)
    config = configured[0]
    _require(
        config.get("controlled") is True
        and config.get("mode") == "spawn"
        and config.get("hash") == loaded.compiled.snapshot_hash
        and _int(config.get("rules"), 1)
        and type(config.get("pid")) is int
        and config["pid"] > 0
    )
    _require(isinstance(cleanup, Mapping), "controlled_cleanup_unverified")
    _require(
        all(
            cleanup.get(k) == v
            for k, v in dict(
                run_id=spec["run_id"],
                app=spec["app"],
                device=spec["device"],
                capture_id=capture_id,
            ).items()
        )
        and _int(cleanup.get("pid"), config["pid"])
        and all(
            cleanup.get(k) is True
            for k in ("owned_spawn", "terminated", "process_absent")
        ),
        "controlled_cleanup_unverified",
    )
    rule = loaded.compiled.rules[0]
    body = rule["action"]["body"].encode("utf-8")
    target = []
    for identity, state in ledger.tasks.items():
        _require(len(state["req"]) <= 1 and len(state["resp"]) <= 1)
        for request in state["req"]:
            _require(config["event_seq"] < request["event_seq"])
            meta = request.get("req")
            _require(
                isinstance(meta, dict)
                and isinstance(meta.get("url"), str)
                and bool(meta["url"])
            )
            if (
                meta["url"] == rule["match"]["exact_url"]
                and meta.get("method") == "GET"
            ):
                target.append((identity, state))
        if not state["req"]:
            _require(
                lifecycle.cancelled_without_response(state)
                and all(
                    e.get("request_observed") is False
                    and e.get("identity_source") == "creation"
                    and _int(e.get("bytes_sent"), 0)
                    and type(e.get("state")) is int
                    and e["state"] in (1, 2, 3)
                    and e.get("kind") != "resume_observed"
                    and e.get("snapshot_errors") == []
                    for e in state["lifecycle"]
                )
            )
    _require(bool(target), "controlled_zero_hits")
    target_ids = {identity for identity, _ in target}
    for identity, state in ledger.tasks.items():
        if identity not in target_ids and state["req"]:
            _require(bool(state["lifecycle"]))
            terminal = state["lifecycle"][-1]
            _require(
                _int(terminal.get("state"), 3)
                and terminal.get("terminal") is True
                and (
                    terminal.get("callback_complete") is True
                    or lifecycle.cancelled_without_response(state)
                )
            )
            _require(
                all(
                    e.get("identity_ambiguous") is False
                    and e.get("coverage_gap") is False
                    and e.get("snapshot_errors") == []
                    for e in state["lifecycle"]
                )
            )
    target.sort(key=lambda member: member[1]["req"][0]["event_seq"])
    protocol = [
        e
        for e in events
        if e.get("ev") in ("scenario_hit", "scenario_route", "scenario_delivery")
    ]
    identities = {identity for identity, _ in target}
    _require(
        all(
            (e.get("capture_id"), e.get("task_instance_id")) in identities
            for e in protocol
        )
    )
    _require(
        not any(
            e.get("ev") in ("scenario_err", "req_err", "resp_err", "wrap_err")
            for e in events
        )
    )
    members = []
    physical = {
        (row.get("capture_id"), row.get("task_instance_id")): row
        for row in ledger.rebuilt
    }
    for ordinal, (identity, state) in enumerate(target, 1):
        request = state["req"][0]
        _complete_request_body(physical[identity])
        _native_local(state)
        _require(
            config["event_seq"]
            < state["lifecycle"][0]["event_seq"]
            < request["event_seq"]
        )
        # Native and synthetic response streams are distinct. No physical response is valid here.
        _require(not state["resp"])
        matching = [
            e
            for e in protocol
            if (e.get("capture_id"), e.get("task_instance_id")) == identity
        ]
        _require(bool(matching))
        via = matching[0].get("via")
        _require(via in ("delegate", "completion"))
        for event in matching:
            _require(
                event.get("rule_id") == rule["rule_id"]
                and _int(event.get("ordinal"), ordinal)
                and event.get("controlled") is True
                and event.get("via") == via
                and event.get("task_ptr") == request.get("task_ptr")
                and event.get("rule_snapshot_hash") == loaded.compiled.snapshot_hash
                and event["event_seq"] > request["event_seq"]
            )
        phases = [(e["ev"], e.get("phase")) for e in matching]
        expected = [
            ("scenario_route", None),
            ("scenario_hit", None),
            ("scenario_delivery", "started"),
        ]
        if via == "delegate":
            expected.append(("scenario_delivery", "response_disposition"))
        expected += [
            ("scenario_delivery", "effective_body"),
            ("scenario_delivery", "completed"),
        ]
        _require(phases == expected)
        route, hit = matching[:2]
        _require(
            route.get("route") == "local"
            and route.get("original_resume_called") is False
            and hit.get("applied") is True
            and hit.get("action") == "fixed_json"
        )
        if via == "delegate":
            _require(_int(matching[3].get("disposition"), 1))
        effective = matching[-2]
        decoded = base64.b64decode(effective["body_b64"], validate=True)
        _require(
            decoded == body
            and _int(effective.get("body_len"), len(body))
            and effective.get("body_truncated") is False
            and _int(effective.get("status"), 200)
        )
        _require(
            effective["event_seq"]
            < state["lifecycle"][-1]["event_seq"]
            < matching[-1]["event_seq"]
        )
        _require(
            all(
                e.get("task_ptr") == request.get("task_ptr") for e in state["lifecycle"]
            )
        )
        members.append(
            dict(
                capture_id=identity[0],
                task_instance_id=identity[1],
                rule_id=rule["rule_id"],
                ordinal=ordinal,
                via=via,
                request_event_seq=request["event_seq"],
                completed_event_seq=matching[-1]["event_seq"],
                body_sha256=inputs._sha(body),
                effective_body=body.decode("utf-8"),
                native_states=state["lifecycle"],
            )
        )
    receipt = dict(
        schema_version="v1",
        run_id=spec["run_id"],
        app=spec["app"],
        device=spec["device"],
        scenario=loaded.reference,
        rule_snapshot_hash=loaded.compiled.snapshot_hash,
        events=ledger.events_ref,
        capture=ledger.capture_ref,
        cleanup=dict(cleanup),
        members=members,
    )
    return receipt


def _verify_delivery(root, spec, events, cleanup):
    receipt = _delivery_receipt(root, spec, events, cleanup)
    members = receipt["members"]
    ref = inputs._write_snapshot(root, RECEIPT_REL, receipt)
    return dict(
        status="verified",
        hit_count=len(members),
        completed_count=len(members),
        receipt=ref,
    )


def verify_delivery(
    run_dir: Path, spec: Mapping, events: list[dict], *, cleanup: Mapping | None = None
) -> dict:
    """Verify persisted evidence and owned-process cleanup; return only a safe receipt reference."""
    try:
        return _verify_delivery(Path(run_dir), spec, events, cleanup)
    except ControlledEvidenceError:
        raise
    except (ValueError, OSError, KeyError, TypeError, AttributeError):
        raise ControlledEvidenceError() from None


def select_response_set(run_dir: Path, spec: Mapping, *, endpoint: str) -> dict:
    """Revalidate a registered logical-delivery receipt; keep physical rows intact."""
    from autophone.observation.impact.controlled_comparison import select_response_set as select
    return select(run_dir, spec, endpoint=endpoint)


def validate_mutation_target(document: Mapping, target: Mapping) -> str:
    from autophone.observation.impact.controlled_comparison import validate_mutation_target as validate
    return validate(document, target)


def verify_text_oracle(**kwargs) -> dict:
    """Public controlled-evidence entry point for the frozen text oracle."""
    from autophone.observation.impact.controlled_oracle import verify_text_oracle as verify
    return verify(**kwargs)
