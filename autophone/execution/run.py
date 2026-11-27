"""Run execution dispatcher independent of reporting and query services."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Mapping, Protocol

from autophone.device import DeviceSession
from autophone.rig.maintenance import RigStateSnapshot
from autophone.observation.composer import RunResultComposer
from autophone.observation.session import ObservationSession


class RunExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunExecutionContext:
    device: DeviceSession
    rig_snapshot: RigStateSnapshot
    run_dir: Path | None = None


@dataclass(frozen=True)
class RunHandlerOutput:
    result: Mapping[str, Any]
    artifact_source_dir: Path | None = None


class RunTypeHandler(Protocol):
    def execute(
        self,
        run_spec: Mapping[str, Any],
        context: RunExecutionContext,
    ) -> Mapping[str, Any] | RunHandlerOutput: ...


class RunExecution:
    """Dispatch Regression, Verification and Operation through one Interface."""

    def __init__(
        self,
        handlers: Mapping[str, RunTypeHandler],
        *,
        instrumentation_factory: Any | None = None,
        result_composer: RunResultComposer | None = None,
        admission_scope=None,
    ) -> None:
        self._handlers = dict(handlers)
        self.instrumentation_factory = instrumentation_factory
        self.result_composer = result_composer or RunResultComposer()
        self.admission_scope = admission_scope

    def execute(
        self,
        run_spec: Mapping[str, Any],
        *,
        device: DeviceSession,
        rig_snapshot: RigStateSnapshot,
        run_dir: Path | None = None,
    ) -> dict[str, Any]:
        result, _ = self.execute_with_artifacts(
            run_spec,
            device=device,
            rig_snapshot=rig_snapshot,
            run_dir=run_dir,
        )
        return result

    def execute_with_artifacts(
        self,
        run_spec: Mapping[str, Any],
        *,
        device: DeviceSession,
        rig_snapshot: RigStateSnapshot,
        run_dir: Path | None = None,
    ) -> tuple[dict[str, Any], Path | None]:
        run_type = str(run_spec.get("type") or "")
        handler = self._handlers.get(run_type)
        if handler is None:
            raise RunExecutionError(f"unsupported Run type: {run_type!r}")
        identity_resolver = getattr(handler, "expected_run_id", None)
        expected_run_id = (
            str(identity_resolver(run_spec)) if identity_resolver is not None
            else str(run_spec.get("run_id") or "")
        )
        if run_spec.get("run_id") and str(run_spec["run_id"]) != expected_run_id:
            raise RunExecutionError("Run Spec identity does not match handler input")
        context = RunExecutionContext(
            device=device,
            rig_snapshot=rig_snapshot,
            run_dir=run_dir,
        )
        instrumentation: ObservationSession | None = (
            self.instrumentation_factory(dict(run_spec), context)
            if self.instrumentation_factory is not None
            else None
        )
        admission = (self.admission_scope({**run_spec, 'run_id': expected_run_id}, context)
                     if self.admission_scope is not None and not getattr(handler, 'owns_world_identity', False)
                     else nullcontext(None))
        with admission as identity_root:
            if instrumentation is None:
                output = handler.execute(dict(run_spec), context)
            else:
                instrumentation.start()
                try:
                    output = handler.execute(dict(run_spec), context)
                except BaseException as failure:
                    try:
                        instrumentation.stop()
                    except Exception as cleanup_error:
                        if hasattr(failure, "add_note"):
                            failure.add_note(f"Observation cleanup failed: {cleanup_error}")
                    raise
                else:
                    evidence = instrumentation.stop()
        if isinstance(output, RunHandlerOutput):
            result = dict(output.result)
            artifact_source = output.artifact_source_dir
        else:
            result = dict(output)
            artifact_source = run_dir
        if instrumentation is not None:
            result = self.result_composer.compose(result, evidence)
        if identity_root is not None:
            from .closure_journal import ClosureJournal
            journal = ClosureJournal(identity_root)
            if journal.path.exists():
                world = journal.read().get('world_identity')
                if world is not None:
                    result['world_identity'] = world
                    artifact_source = artifact_source or identity_root
        if str(result.get("run_id") or "") != expected_run_id:
            raise RunExecutionError("Run Result identity does not match Run Spec")
        if not result.get("status"):
            raise RunExecutionError("Run Result status is required")
        return result, artifact_source
