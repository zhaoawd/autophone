"""AI specialization of the shared persistent background job executor."""
import os
import tempfile
from pathlib import Path
from typing import Any
from autophone.paths import STATE_ROOT
from autophone.application.background_jobs import (
    BackgroundJobService, Job as AiJob, JobConflict as AiJobConflict,
    JobQueueFull as AiJobQueueFull, JobNotRegistered as AiSceneNotRegistered,
    CancelToken, JobCanceled, SceneContext, current_cancel_token, request_digest,
    ACTIVE_STATUSES, TERMINAL_STATUSES, _normalize,
)


class AiJobService(BackgroundJobService):
    result_failure_code = "ai_result_failed"

    def __init__(self, **kwargs: Any) -> None:
        if kwargs.get("root") is None:
            kwargs["root"] = STATE_ROOT / "runs" / ".ai" / "jobs"
        if kwargs.get("retention_days") is None:
            kwargs["retention_days"] = int(os.environ.get("AUTOPHONE_AI_JOB_RETENTION_DAYS", "30"))
        if kwargs.get("max_terminal_jobs") is None:
            kwargs["max_terminal_jobs"] = int(os.environ.get("AUTOPHONE_AI_JOB_MAX_TERMINAL", "1000"))
        super().__init__(**kwargs)


class AiJobRunner(AiJobService):
    """Legacy view kept for older direct Python callers during the HTTP migration.

    It still uses the persistent/bounded implementation, but maps the old three
    status strings and string error field at its boundary.  Production API code
    instantiates ``AiJobService`` and therefore exposes only the new contract.
    """

    def __init__(self) -> None:
        super().__init__(root=Path(tempfile.mkdtemp(prefix="autophone-ai-job-legacy-")))

    @staticmethod
    def _legacy(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
        if snapshot is None:
            return None
        out = dict(snapshot)
        status = out.get("status")
        out["status"] = "running" if status in {"queued", "running"} else ("done" if status == "succeeded" else status)
        if isinstance(out.get("error"), dict):
            out["error"] = out["error"].get("message") or out["error"].get("code")
        return out

    def submit(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._legacy(super().submit(*args, **kwargs)) or {}

    def get(self, job_id: str, *, actor: str | None = None) -> dict[str, Any] | None:
        return self._legacy(super().get(job_id, actor=actor))
