"""Package gate jobs own a separate queue, store and public snapshot."""
from typing import Any
from autophone.application.background_jobs import BackgroundJobService, SceneContext


class PackageGateJobs(BackgroundJobService):
    def __init__(self, gate: Any, run_service: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.gate = gate
        self.run_service = run_service
        self.register("package-gate", self._run_gate)

    def _run_gate(self, ctx: SceneContext) -> dict:
        from autophone.application.control_plane import configured_control_plane
        configured_control_plane()
        body = ctx.request
        return self.gate.run_gate(
            self.run_service, body["app"], body["slot"],
            selector=body["selector"], verify=body["verify"],
            **{key: body[key] for key in ("build_id", "baseline_policy", "baseline_build_id") if key in body},
        )

    @staticmethod
    def _snapshot(value: dict | None) -> dict | None:
        if value is None:
            return None
        fields = ("job_id", "actor", "status", "phase", "created_at", "started_at",
                  "ended_at", "result", "error", "duration_s")
        return {"kind": "package-gate", **{key: value[key] for key in fields}}

    def submit_gate(self, request: dict, **kwargs: Any) -> dict:
        from autophone.application.control_plane import configured_control_plane
        configured_control_plane()
        return self._snapshot(self.submit_registered("package-gate", request, **kwargs))

    def get(self, job_id: str, *, actor: str | None = None) -> dict | None:
        return self._snapshot(super().get(job_id, actor=actor))
