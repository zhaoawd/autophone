"""Platform self-acceptance harness.

The public interface accepts one declarative acceptance spec and returns one
self-contained evidence directory.  Individual device/run details stay behind
this module; callers only consume the aggregate verdict and artifact paths.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Callable

from autophone.observation.artifacts import Artifact, write_artifact_manifest
from autophone.contracts import validate_or_raise, write_json
from autophone.util import now_iso


Doctor = Callable[[], dict[str, Any]]
RunSubmitter = Callable[[dict[str, Any]], Any]
Probe = Callable[[dict[str, Any]], dict[str, Any]]


# 公开版示例坐标（上海人民广场附近，WGS-84）。真实验收用 CLI 参数覆写。
DEFAULT_LOCATION_LATITUDE = 31.2304
DEFAULT_LOCATION_LONGITUDE = 121.4737


def _default_location_probe(spec: dict[str, Any]) -> dict[str, Any]:
    """经统一定位会话向系统 CoreLocation 投递一个定点样本，再停止会话。"""
    import time

    from autophone.location.contracts import (
        LocationCoordinate,
        LocationSample,
        LocationSessionSpec,
        LocationSourceSpec,
        LocationTargetSpec,
    )
    from autophone.location.coordinator import LocationSessionCoordinator

    declaration = dict(spec.get("location") or {})
    latitude = float(declaration.get("latitude", DEFAULT_LOCATION_LATITUDE))
    longitude = float(declaration.get("longitude", DEFAULT_LOCATION_LONGITUDE))
    device = str(spec["device"])
    acceptance_id = str(spec.get("acceptance_id") or "location")
    owner = f"acceptance:{acceptance_id}"
    session = LocationSessionSpec(
        session_id=f"acceptance-location-{acceptance_id}",
        owner=owner,
        device=device,
        source=LocationSourceSpec(
            source_id=f"acceptance-fixed-{acceptance_id}",
            kind="fixed",
            coordinate_system="wgs84",
        ),
        targets=(
            LocationTargetSpec(
                target_id=f"system:{device}",
                kind="system_core_location",
                device=device,
                coordinate_system="wgs84",
            ),
        ),
    )
    sample = LocationSample(
        producer_sequence=0,
        emitted_wall_at=now_iso(),
        emitted_monotonic_s=time.monotonic(),
        coordinate=LocationCoordinate(latitude, longitude, "wgs84"),
    )
    coordinator = LocationSessionCoordinator()
    started: dict[str, Any] = {}
    stopped: dict[str, Any] = {}
    try:
        started = coordinator.start(session, initial_sample=sample).as_dict()
    finally:
        try:
            stopped = coordinator.stop(session.session_id, owner=owner).as_dict()
        except Exception as exc:  # noqa: BLE001 - report stop failure as evidence
            stopped = {"state": "stop_failed", "detail": f"{type(exc).__name__}: {exc}"}
        coordinator.shutdown()
    return {
        "ok": started.get("state") == "running" and started.get("last_producer_sequence") == 0,
        "target": {"latitude": latitude, "longitude": longitude},
        "session": started,
        "detached": stopped.get("state") == "stopped",
    }


def _run_probe_check(
    *,
    check_id: str,
    label: str,
    spec: dict[str, Any],
    probe: Probe,
) -> dict[str, Any]:
    try:
        evidence = probe(spec)
    except Exception as exc:  # noqa: BLE001 - acceptance must always persist a verdict
        return {
            "id": check_id,
            "kind": "probe",
            "required": True,
            "status": "error",
            "detail": f"{label} raised {type(exc).__name__}: {exc}",
            "evidence": {},
        }
    ok = bool(evidence.get("ok"))
    detail = str(evidence.get("detail") or "").strip()
    receipt_status = str(evidence.get("status") or "").strip()
    check_status = "passed" if ok else ("blocked" if receipt_status == "blocked" else "failed")
    return {
        "id": check_id,
        "kind": "probe",
        "required": True,
        "status": check_status,
        "detail": (
            f"{label} passed" if ok
            else f"{label} {check_status}{': ' + detail if detail else ''}"
        ),
        "evidence": evidence,
    }


def _verification_evidence_gaps(run_dir: Path) -> list[str]:
    result_path = run_dir / "verification-result.json"
    manifest_path = run_dir / "artifact-manifest.json"
    if not result_path.is_file():
        return ["verification-result.json: missing"]
    if not manifest_path.is_file():
        return ["artifact-manifest.json: missing"]
    try:
        verification = json.loads(result_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"evidence metadata unreadable: {type(exc).__name__}"]
    registered = {
        str(item.get("path"))
        for item in manifest.get("artifacts") or []
        if isinstance(item, dict) and item.get("path")
    }
    gaps: list[str] = []
    for checkpoint in verification.get("checkpoints") or []:
        if not isinstance(checkpoint, dict):
            continue
        checkpoint_id = str(checkpoint.get("id") or "checkpoint")
        evidence = [str(value) for value in checkpoint.get("evidence") or [] if str(value)]
        if not evidence:
            gaps.append(f"{checkpoint_id}: no evidence")
            continue
        for rel in evidence:
            path = Path(rel)
            if path.is_absolute() or ".." in path.parts:
                gaps.append(f"{checkpoint_id}: unsafe evidence path {rel}")
            elif rel not in registered:
                gaps.append(f"{checkpoint_id}: evidence not registered {rel}")
            elif not (run_dir / path).is_file():
                gaps.append(f"{checkpoint_id}: evidence file missing {rel}")
    return gaps


@dataclass(frozen=True)
class AcceptanceHandle:
    acceptance_id: str
    run_dir: Path
    result: dict[str, Any]
    manifest_path: Path

    @property
    def status(self) -> str:
        return str(self.result["status"])


def _summary(result: dict[str, Any]) -> str:
    lines = [
        f"# Platform acceptance · {result['acceptance_id']}",
        "",
        f"- App: `{result['app']}`",
        f"- Device: `{result['device']}`",
        f"- Status: **{result['status']}**",
        "",
        "## Checks",
        "",
    ]
    for check in result["checks"]:
        lines.append(f"- `{check['id']}`: **{check['status']}** — {check['detail']}")
    return "\n".join(lines) + "\n"


def run_acceptance(
    spec: dict[str, Any],
    *,
    out_root: Path,
    doctor: Doctor | None = None,
    submit_run: RunSubmitter | None = None,
    location_probe: Probe | None = None,
) -> AcceptanceHandle:
    """Run the declared platform checks and persist one aggregate verdict."""
    validate_or_raise("acceptance-spec.v1.json", spec)
    acceptance_id = str(spec["acceptance_id"])
    started_at = now_iso()
    run_dir = out_root / datetime.now().strftime("%Y%m%d") / acceptance_id
    run_dir.mkdir(parents=True, exist_ok=False)
    write_json(run_dir / "acceptance-spec.json", spec)

    checks: list[dict[str, Any]] = []
    if "rig" in spec.get("checks", []):
        if doctor is None:
            from autophone.application.doctor import run_doctor

            doctor = run_doctor
        try:
            evidence = doctor()
        except Exception as exc:  # noqa: BLE001 - acceptance must always persist a verdict
            checks.append({
                "id": "rig",
                "kind": "probe",
                "required": True,
                "status": "error",
                "detail": f"Rig doctor raised {type(exc).__name__}: {exc}",
                "evidence": {},
            })
        else:
            healthy = evidence.get("status") == "healthy"
            checks.append({
                "id": "rig",
                "kind": "probe",
                "required": True,
                "status": "passed" if healthy else "blocked",
                "detail": "Rig doctor healthy" if healthy else f"Rig doctor {evidence.get('status') or 'unknown'}",
                "evidence": {"doctor": evidence},
            })

    rig_check = next((item for item in checks if item["id"] == "rig"), None)
    rig_blocked = rig_check is not None and rig_check["status"] != "passed"

    if "location" in spec.get("checks", []):
        if rig_blocked:
            checks.append({
                "id": "location", "kind": "probe", "required": True,
                "status": "skipped", "detail": "Skipped because rig did not pass", "evidence": {},
            })
        else:
            checks.append(_run_probe_check(
                check_id="location",
                label="Location probe",
                spec=spec,
                probe=location_probe or _default_location_probe,
            ))

    if "regression" in spec.get("checks", []):
        declaration = dict(spec.get("regression") or {})
        case_ids = [str(value) for value in declaration.get("case_ids") or ["R123"]]
        if rig_blocked:
            checks.append({
                "id": "regression",
                "kind": "run",
                "required": True,
                "status": "skipped",
                "detail": "Skipped because rig did not pass",
                "evidence": {"case_ids": case_ids},
            })
        else:
            if submit_run is None:
                from autophone.execution.run_service import RunService

                submit_run = RunService().submit
            child_spec = {
                "schema_version": "v1",
                "run_id": f"{acceptance_id}-regression",
                "type": "regression",
                "app": str(spec["app"]),
                "device": str(spec["device"]),
                "auto_login": True,
                "case_selector": {"ids": case_ids},
                "planes": {
                    "ui": {"mode": "required"},
                    "function": {"mode": "disabled"},
                    "api": {"mode": str(declaration.get("api_mode") or "disabled")},
                    "telemetry": {"mode": "disabled"},
                },
                "requested_by": "platform-acceptance",
                "requested_at": now_iso(),
            }
            try:
                child = submit_run(child_spec)
            except Exception as exc:  # noqa: BLE001 - acceptance must always persist a verdict
                checks.append({
                    "id": "regression",
                    "kind": "run",
                    "required": True,
                    "status": "error",
                    "detail": f"Curated regression raised {type(exc).__name__}: {exc}",
                    "evidence": {"case_ids": case_ids},
                })
            else:
                child_status = str(child.result.get("status") or "error")
                check_status = "passed" if child_status == "passed" else (
                    "blocked" if child_status == "blocked" else (
                        "error" if child_status == "error" else "failed"
                    )
                )
                checks.append({
                    "id": "regression",
                    "kind": "run",
                    "required": True,
                    "status": check_status,
                    "detail": f"Curated regression {child_status}",
                    "evidence": {
                        "run_id": str(child.run_id),
                        "run_status": child_status,
                        "run_dir": str(child.run_dir),
                        "case_ids": case_ids,
                    },
                })

    if "verification" in spec.get("checks", []):
        declaration = dict(spec.get("verification") or {})
        path = str(declaration.get("path") or "verification/platform-smoke.yaml")
        if rig_blocked:
            checks.append({
                "id": "verification",
                "kind": "run",
                "required": True,
                "status": "skipped",
                "detail": "Skipped because rig did not pass",
                "evidence": {"path": path},
            })
        else:
            if submit_run is None:
                from autophone.execution.run_service import RunService

                submit_run = RunService().submit
            child_spec = {
                "schema_version": "v1",
                "run_id": f"{acceptance_id}-verification",
                "type": "verification",
                "app": str(spec["app"]),
                "device": str(spec["device"]),
                "verification": {"path": path},
                "requested_by": "platform-acceptance",
                "requested_at": now_iso(),
            }
            try:
                child = submit_run(child_spec)
            except Exception as exc:  # noqa: BLE001 - acceptance must always persist a verdict
                checks.append({
                    "id": "verification",
                    "kind": "run",
                    "required": True,
                    "status": "error",
                    "detail": f"Verification raised {type(exc).__name__}: {exc}",
                    "evidence": {"path": path},
                })
            else:
                child_status = str(child.result.get("status") or "error")
                if child_status == "passed":
                    evidence_gaps = _verification_evidence_gaps(Path(child.run_dir))
                    check_status = "failed" if evidence_gaps else "passed"
                elif child_status in {"pending", "blocked"}:
                    evidence_gaps = []
                    check_status = "blocked"
                elif child_status == "error":
                    evidence_gaps = []
                    check_status = "error"
                else:
                    evidence_gaps = []
                    check_status = "failed"
                evidence = {
                    "run_id": str(child.run_id),
                    "run_status": child_status,
                    "run_dir": str(child.run_dir),
                    "path": path,
                }
                if evidence_gaps:
                    evidence["evidence_gaps"] = evidence_gaps
                checks.append({
                    "id": "verification",
                    "kind": "run",
                    "required": True,
                    "status": check_status,
                    "detail": (
                        "Verification passed without complete evidence"
                        if evidence_gaps else f"Verification {child_status}"
                    ),
                    "evidence": evidence,
                })

    statuses = {item["status"] for item in checks if item["required"]}
    if "error" in statuses:
        status = "error"
    elif "failed" in statuses:
        status = "failed"
    elif "blocked" in statuses:
        status = "blocked"
    else:
        status = "passed"
    ended_at = now_iso()
    result = {
        "schema_version": "v1",
        "acceptance_id": acceptance_id,
        "app": str(spec["app"]),
        "device": str(spec["device"]),
        "status": status,
        "checks": checks,
        "started_at": started_at,
        "ended_at": ended_at,
        "artifacts": "artifact-manifest.json",
    }
    validate_or_raise("acceptance-result.v1.json", result)
    write_json(run_dir / "acceptance-result.json", result)
    (run_dir / "summary.md").write_text(_summary(result), encoding="utf-8")
    artifacts = [
        Artifact("acceptance-spec.json", "other", retention="local_only", share_policy="forbidden"),
        Artifact("acceptance-result.json", "report"),
        Artifact("summary.md", "report"),
    ]
    manifest_path = write_artifact_manifest(run_dir, acceptance_id, artifacts)
    return AcceptanceHandle(acceptance_id, run_dir, result, manifest_path)
