from __future__ import annotations
from dataclasses import asdict, dataclass
import copy
import hashlib
from pathlib import Path
from autophone.assets.apps import AppProfile
from typing import Any, Callable

class RunCollectionError(RuntimeError):
    """Host collection failed after the semantic execution result was frozen."""

    def __init__(self, message: str, *, run_result: dict[str, Any], run_dir: Path):
        super().__init__(message)
        self.run_result = run_result
        self.run_dir = run_dir


@dataclass(frozen=True)
class ExecutionBundle:
    phase2_result: Any
    result_json: Path
    report_md: Path
    report_html: Path
    cases_path: Path
    coverage_json: Path | None = None
    coverage_html: Path | None = None
    telemetry_json: Path | None = None
    telemetry_payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class PendingPhase2Collection:
    """Execution snapshot awaiting Host-only reports and recording mux."""
    phase2_result: Any
    cases_path: Path
    case_docs: tuple[dict[str, Any], ...]
    report_meta: dict[str, Any]
    recordings: tuple[Any, ...]

    def snapshot(self, run_dir: Path) -> dict[str, Any]:
        """Persist report values and verified recording inputs before collection."""
        extensions = {}
        for name in ('groups', 'group_by_id', 'rig_snapshot'):
            value = getattr(self.phase2_result, name, None)
            if value is not None:
                extensions[name] = copy.deepcopy(value.to_dict() if hasattr(value, 'to_dict') else value)
        references = set()
        for outcome in self.phase2_result.outcomes:
            references.update(outcome.shots)
            if outcome.net_jsonl:
                references.add(outcome.net_jsonl)
            for action in outcome.actions:
                if action.get('shot_path'):
                    references.add(action['shot_path'])
                references.update(action.get('aux_shots') or [])
        root = run_dir.resolve()
        artifacts = []
        for reference in sorted(references):
            path = Path(reference)
            path = (path if path.is_absolute() else root / path).resolve()
            relative = path.relative_to(root)
            data = path.read_bytes() if path.exists() else None
            artifacts.append({'path': str(relative), 'sha256': hashlib.sha256(data).hexdigest()
                              if data is not None else None})
        return {
            "schema_version": "pending-phase2-collection/v1",
            "phase2_result": asdict(self.phase2_result),
            "extensions": extensions,
            "artifacts": artifacts,
            "cases_path": str(self.cases_path),
            "case_docs": copy.deepcopy(list(self.case_docs)),
            "report_meta": copy.deepcopy(self.report_meta),
            "recordings": [recorder.export_frozen(frozen, root=run_dir)
                           for recorder, frozen in self.recordings],
        }

    @classmethod
    def restore(cls, snapshot: dict[str, Any], run_dir: Path) -> "PendingPhase2Collection":
        """Rehydrate values only; never start recording or execute device actions."""
        from autophone.execution.engine.runner import CaseOutcome, RunResult
        from autophone.record.recording import BackgroundRecorder

        if snapshot.get("schema_version") != "pending-phase2-collection/v1":
            raise ValueError("unsupported pending collection version")
        values = copy.deepcopy(snapshot["phase2_result"])
        values["outcomes"] = [CaseOutcome(**outcome) for outcome in values["outcomes"]]
        result = RunResult(**values)
        for name, value in snapshot.get('extensions', {}).items():
            if name not in {'groups', 'group_by_id', 'rig_snapshot'}:
                raise ValueError('unsupported execution result extension')
            setattr(result, name, copy.deepcopy(value))
        root = run_dir.resolve()
        for item in snapshot.get('artifacts', []):
            path = (root / item['path']).resolve()
            path.relative_to(root)
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            if digest != item['sha256']:
                raise ValueError(f"frozen report artifact changed: {item['path']}")
        return cls(
            phase2_result=result, cases_path=Path(snapshot["cases_path"]),
            case_docs=tuple(copy.deepcopy(snapshot["case_docs"])),
            report_meta=copy.deepcopy(snapshot["report_meta"]),
            recordings=tuple(BackgroundRecorder.restore_frozen(item, root=run_dir)
                             for item in snapshot["recordings"]),
        )


@dataclass(frozen=True)
class RunHandle:
    run_id: str
    run_dir: Path
    result: dict[str, Any]
    manifest_path: Path

    def summary(self) -> dict[str, Any]:
        report = self.run_dir / "report.html"
        if self.result.get("type") == "verification":
            report = self.run_dir / "verification-report.html"
        elif self.result.get("type") == "operation":
            report = self.run_dir / "operation-report.html"
        return {
            "run_id": self.run_id,
            **({"assignment_id": self.result["assignment_id"]} if self.result.get("assignment_id") else {}),
            "status": self.result.get("status"),
            "run_dir": str(self.run_dir),
            "run_result": str(self.run_dir / "platform-run-result.json"),
            "artifact_manifest": str(self.manifest_path),
            "report_html": str(report),
        }


Executor = Callable[[dict[str, Any], AppProfile, Path], ExecutionBundle | PendingPhase2Collection]
