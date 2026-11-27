from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from autophone.device.target import default_device

DEFAULT_DEVICE = default_device()

from PIL import Image, ImageDraw

from autophone.assets.apps import default_app, load_app_profile
from autophone.observation.artifacts import default_phase2_artifacts, write_artifact_manifest
from autophone.contracts import ContractError, validate_or_raise, write_json
from autophone.execution.run_service import RunService
from autophone.execution.run_types import RunHandle
from autophone.util import now_iso as _now
from autophone.version import stamp_run_spec
from autophone.execution.engine.cases_loader import load_cases
from autophone.execution.engine.runner import CaseOutcome, RunResult


def _run_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-failure-sample-{uuid.uuid4().hex[:6]}"


def _first_active_case(app: str) -> dict[str, Any]:
    """Choose a catalogued case so the generated coverage can account for it."""
    profile = load_app_profile(app)
    for case in load_cases(profile.cases_path()):
        if case.get("status") == "active" and case.get("id"):
            return case
    raise ContractError(f"app {app!r} has no active case for the failure sample")


def _selected_active_case(profile, spec: dict[str, Any]) -> dict[str, Any]:
    ids = ((spec.get("case_selector") or {}).get("ids") or [])
    if len(ids) != 1:
        raise ContractError("failure sample requires exactly one selected case")
    case_id = str(ids[0])
    for case in load_cases(profile.cases_path()):
        if str(case.get("id")) == case_id and case.get("status") == "active":
            return case
    raise ContractError(f"failure sample case {case_id!r} is not an active case for {profile.app!r}")


def failure_sample_spec(
    *,
    run_id: str | None = None,
    app: str | None = None,
    device: str = DEFAULT_DEVICE,
    requested_by: str = "local",
) -> dict[str, Any]:
    if app is None:
        app = default_app()
        if app is None:
            raise ContractError("app is required: zero or multiple app profiles are installed")
    sample_case = _first_active_case(app)
    return {
        "schema_version": "v1",
        "run_id": run_id or _run_id(),
        "type": "regression",
        "app": app,
        "device": device,
        "case_selector": {"ids": [str(sample_case["id"])]},
        "planes": {
            "ui": {"mode": "required"},
            "function": {"mode": "disabled"},
            "api": {"mode": "disabled"},
            "telemetry": {"mode": "disabled"},
        },
        "requested_by": requested_by,
        "requested_at": _now(),
    }


def create_failure_sample(service: RunService, spec: dict[str, Any]) -> RunHandle:
    """Create a controlled failed run for report/governance review.

    The sample is intentionally device-free: it validates the consumer surface
    for failed runs without relying on a flaky live app state to produce one.
    """
    spec = stamp_run_spec(spec)
    validate_or_raise("run-spec.v1.json", spec)
    profile = load_app_profile(str(spec["app"]))
    sample_case = _selected_active_case(profile, spec)
    case_id = str(sample_case["id"])
    run_id = str(spec["run_id"])
    run_dir = service._run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "run-spec.json", spec)

    shot_path = _write_sample_shot(run_dir / "shots" / f"{case_id}_failure_sample.jpg", case_id)
    phase2_result = RunResult(
        aborted=False,
        abort_reason="",
        outcomes=[
            CaseOutcome(
                id=case_id,
                status="fail",
                module=str(sample_case.get("module") or ""),
                item=str(sample_case.get("item") or ""),
                row=sample_case.get("row"),
                detail="controlled failure sample: expected update prompt was absent",
                duration_ms=128.0,
                actions=[
                    {
                        "action": "tap",
                        "ok": True,
                        "detail": "opened settings screen for failure sample",
                        "elapsed_ms": 42.0,
                        "shot_path": str(shot_path),
                    },
                    {
                        "action": "assert_present",
                        "ok": False,
                        "detail": "missing expected text: 已经是最新版本",
                        "elapsed_ms": 86.0,
                        "shot_path": str(shot_path),
                    },
                ],
                shots=[str(shot_path)],
            )
        ],
    )

    from autophone.execution.engine.html_report import write_html_report
    from autophone.execution.engine.report import write_reports

    cases_path = profile.cases_path()
    result_json, _report_md, _report_html = write_reports(phase2_result, run_dir, cases_path=cases_path)
    service.execution.write_coverage(
        run_dir=run_dir,
        run_id=run_id,
        cases_path=cases_path,
        phase2_result_path=result_json,
    )
    write_html_report(result_json)

    ts = _now()
    timestamps = {
        "queued_at": ts,
        "execute_started_at": ts,
        "execute_ended_at": ts,
        "collect_ended_at": _now(),
    }
    phase_status = {
        "provision": {"status": "ok", "started_at": ts, "ended_at": ts},
        "execute": {"status": "ok", "started_at": ts, "ended_at": ts},
        "collect": {
            "status": "ok",
            "started_at": timestamps["execute_ended_at"],
            "ended_at": timestamps["collect_ended_at"],
        },
    }
    result = service.execution.assemble_result(spec, profile, phase2_result, timestamps, phase_status)
    service.persist_run_result(run_dir, result)
    manifest_path = write_artifact_manifest(run_dir, run_id, default_phase2_artifacts(run_dir))
    return RunHandle(run_id=run_id, run_dir=run_dir, result=result, manifest_path=manifest_path)


def _write_sample_shot(path: Path, case_id: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (900, 1600), "#172033")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 900, 180), fill="#0f172a")
    draw.text((48, 54), f"{case_id} controlled failure sample", fill="#e2e8f0")
    draw.rectangle((64, 260, 836, 720), outline="#ef4444", width=8)
    draw.text((104, 320), "Expected text was not found", fill="#fca5a5")
    draw.text((104, 390), "missing: already latest version", fill="#fca5a5")
    draw.text((104, 520), "This image proves report screenshot navigation.", fill="#cbd5e1")
    draw.rectangle((64, 920, 836, 1240), outline="#334155", width=4)
    draw.text((104, 980), "Share gate must block this raw screenshot.", fill="#93c5fd")
    image.save(path, format="JPEG", quality=88)
    return path
