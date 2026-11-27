"""Minimal `autophone.executor` provider example.

Loaded by `autophone.rig.plugins.create_executor(target=..., run_root=...)` or
directly via `--executor-factory` / `AUTOPHONE_AGENT_EXECUTOR_FACTORY`.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from autophone.rig.agent import (
    AgentExecutionContext,
    AssignmentCancelled,
    StaleDeviceWrite,
)
from autophone.rig.target import DeviceTarget, served_device_keys
from autophone.util import now_iso


class TemplateAssignmentExecutor:
    """Minimal schema-valid `AgentAssignmentExecutor` implementation."""

    def __init__(self, *, target: DeviceTarget, run_root: Path | None = None) -> None:
        self.target = target
        self.run_root = run_root

    def screen_quarantine_report(self, target: DeviceTarget) -> dict[str, Any]:
        """Optional hook called by `RigAgent.report_screen_quarantine()`."""
        return {
            "schema_version": "screen-quarantine-report/v1",
            "quarantined": False,
            "lock_root": str(target.runtime_dir),
            "devices": served_device_keys(target),
            "markers": [],
            "observed_at": now_iso(),
        }

    def execute(
        self,
        assignment: Mapping[str, Any],
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        """Execute one Assignment and return a valid `assignment-result/v1` dict."""
        assignment_id = str(assignment["assignment_id"])
        attempt = assignment.get("attempt") or {}
        attempt_no = int(attempt["attempt_no"])
        fencing = int(attempt["fencing"])
        run_spec = assignment.get("run_spec")

        try:
            # Check cooperative cancellation and Control Plane write fencing
            # before touching any device resource.
            context.check_control()
            context.write_fence.check()
        except StaleDeviceWrite:
            # Never fabricate a completion receipt once lease/fencing is lost.
            raise
        except AssignmentCancelled:
            # Enter Cleanup before releasing handles so cancellation never blocks
            # cleanup checks, then return an explicit Cleanup receipt.
            context.assignment_control.begin_cleanup()
            context.write_fence.check_release()
            return {
                "schema_version": "assignment-result/v1",
                "assignment_id": assignment_id,
                "attempt_no": attempt_no,
                "fencing": fencing,
                "status": "cancelled",
                "product_verdict": "not_run",
                "run_result": None,
                "cleanup_receipt": {
                    "status": "passed",
                    "artifacts_sealed": True,
                    "device_access_released": True,
                },
                "rig_disposition": "reusable",
                "fixture_disposition": "not_required",
                "artifact_refs": [],
                "closure": None,
                "detail": "Assignment cancelled before template work started",
            }

        # Replace this stub with real device execution guarded by `context.write_fence`.
        context.assignment_control.begin_cleanup()
        context.write_fence.check_release()

        run_result: dict[str, Any] | None = None
        product_verdict = "not_run"
        if isinstance(run_spec, Mapping) and run_spec.get("run_id"):
            run_result = {
                "run_id": str(run_spec["run_id"]),
                "status": "passed",
            }
            product_verdict = "passed"

        return {
            "schema_version": "assignment-result/v1",
            "assignment_id": assignment_id,
            "attempt_no": attempt_no,
            "fencing": fencing,
            "status": "passed",
            "product_verdict": product_verdict,
            "run_result": run_result,
            "cleanup_receipt": {
                "status": "passed",
                "artifacts_sealed": True,
                "device_access_released": True,
            },
            "rig_disposition": "reusable",
            "fixture_disposition": "not_required",
            "artifact_refs": [],
            "closure": None,
            "detail": f"Template executor completed on rig {context.target.rig_id}",
        }


def create_executor(
    *,
    target: DeviceTarget,
    run_root: Path | None = None,
) -> TemplateAssignmentExecutor:
    """Entry-point factory matching `autophone.rig.main` keyword arguments."""
    return TemplateAssignmentExecutor(target=target, run_root=run_root)
