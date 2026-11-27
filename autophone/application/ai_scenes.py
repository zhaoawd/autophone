"""AI scene registration and orchestration without HTTP concerns."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from autophone.assets.policy import assert_assets_writable
from autophone.application.ai_jobs import (
    AiJobConflict,
    AiJobQueueFull,
    AiSceneNotRegistered,
    SceneContext,
)
from autophone.util import now_iso


class AiSceneApplicationError(RuntimeError):
    def __init__(self, status_code: int, detail: dict[str, Any]) -> None:
        super().__init__(str(detail.get("message") or detail))
        self.status_code = status_code
        self.detail = detail


class AiSceneApplication:
    """Own the fixed scene registry and all scene-specific orchestration."""

    ASSET_WRITING_SCENES = frozenset({
        "suggest-ops",
        "land-recording",
        "draft-operation",
        "convert-case",
    })

    def __init__(
        self,
        *,
        jobs: Any,
        run_service: Any,
        recordings: Any,
        fix: Any,
        default_device: str,
    ) -> None:
        self.jobs = jobs
        self.run_service = run_service
        self.recordings = recordings
        self.fix = fix
        self.default_device = default_device

    def register(self) -> None:
        handlers = {
            "suggest-ops": self.suggest_ops,
            "lift-recording": self.lift_recording,
            "lift-recording-step": self.lift_recording_step,
            "land-recording": self.land_recording,
            "diagnose": self.diagnose,
            "propose-fix": self.propose_fix,
            "draft-operation": self.draft_operation,
            "convert-case": self.convert_case,
            "probe": self.probe,
            "ask": self.ask,
            "ask-execution": self.ask_execution,
        }
        for scene, handler in handlers.items():
            self.jobs.register(scene, handler)

    def submit(
        self,
        scene: str,
        request: dict,
        *,
        actor: str,
        idempotency_key: str | None,
        **kwargs: Any,
    ) -> dict:
        if scene in self.ASSET_WRITING_SCENES:
            assert_assets_writable(f"submit {scene} asset job")
        try:
            return self.jobs.submit_registered(
                scene,
                request,
                actor=actor,
                idempotency_key=idempotency_key,
                **kwargs,
            )
        except AiJobQueueFull as exc:
            raise AiSceneApplicationError(429, {
                "code": "ai_queue_full",
                "message": str(exc),
            }) from exc
        except AiJobConflict as exc:
            raise AiSceneApplicationError(409, {
                "code": "idempotency_conflict",
                "message": str(exc),
            }) from exc
        except AiSceneNotRegistered as exc:
            raise AiSceneApplicationError(500, {
                "code": "ai_scene_not_registered",
                "message": str(exc),
            }) from exc

    @staticmethod
    def suggest_ops(ctx: SceneContext):
        body = ctx.request
        if body.get("case_ids") is not None:
            from autophone.application.ai_suggest_ops import suggest_for_drafts

            return suggest_for_drafts(body["app"], case_ids=list(body["case_ids"]))
        from autophone.application.ai_suggest_ops import suggest_for_case

        return suggest_for_case(
            body["app"],
            body["case_id"],
            provider=body.get("provider"),
            model=body.get("model"),
        )

    def lift_recording(self, ctx: SceneContext):
        body = ctx.request
        ctx.emit({
            "phase": "preparing",
            "type": "progress",
            "progress": 3,
            "message": "已进入 AI 分析队列，正在准备录制素材",
        })
        result = self.recordings.lift_preview_thunk(
            body["app"],
            body["recording_id"],
            provider=body.get("provider"),
            model=body.get("model"),
            script=body.get("script"),
            progress=ctx.emit,
        )()
        ctx.emit({
            "phase": "completed",
            "type": "progress",
            "progress": 100,
            "message": "AI 分析完成，可以修改并确认关联",
        })
        return result

    def lift_recording_step(self, ctx: SceneContext):
        body = ctx.request
        return self.recordings.lift_preview_step_thunk(
            body["app"],
            body["recording_id"],
            index=int(body["index"]),
            provider=body.get("provider"),
            model=body.get("model"),
        )()

    def land_recording(self, ctx: SceneContext):
        body = ctx.request
        return self.recordings.land_recording_thunk(
            body["app"],
            body["recording_id"],
            op_name=body["op_name"],
            summary=body["summary"],
            provider=body.get("provider"),
            model=body.get("model"),
            steps=body.get("steps"),
            step_events=body.get("step_events"),
            script_alignment=body.get("script_alignment"),
            script=body.get("script"),
        )()

    @staticmethod
    def diagnose(ctx: SceneContext):
        from autophone.application.ai_diagnose import diagnose_run

        body = ctx.request
        return diagnose_run(
            Path(body["run_dir"]),
            provider=body.get("provider"),
            model=body.get("model"),
            allow_raw=bool(body.get("allow_raw")),
        )

    def propose_fix(self, ctx: SceneContext):
        body = ctx.request
        return self.fix.propose_fix(
            Path(body["run_dir"]),
            patch=body.get("patch"),
            target=body.get("target"),
        )

    @staticmethod
    def draft_operation(ctx: SceneContext):
        from autophone.application.ai_draft import draft_operation

        body = ctx.request
        return draft_operation(
            body["app"],
            body["requirement"],
            source_text=body.get("source_text"),
            provider=body.get("provider"),
            model=body.get("model"),
        )

    @staticmethod
    def convert_case(ctx: SceneContext):
        from autophone.application.ai_convert import convert_case

        body = ctx.request

        def progress(event: dict) -> None:
            ctx.emit({**event, "phase": f"intake_{event.get('stage', 'work')}"})

        result = convert_case(
            body["app"],
            body["case_id"],
            provider=body.get("provider"),
            model=body.get("model"),
            note=body.get("note"),
            progress=progress,
        )
        if result.get("needs_extraction") and not result.get("error"):
            return {**result, "ok": True, "outcome": "needs_extraction"}
        return result

    @staticmethod
    def probe(ctx: SceneContext):
        from autophone.application.ai import AiRequest, invoke

        body = ctx.request
        result = invoke(AiRequest(
            prompt="reply with the single word: pong",
            tools="none",
            provider=body.get("provider"),
            timeout_s=30.0,
        ))
        contract = result.to_contract()
        if result.ok:
            contract["outcome"] = "probed"
        return contract

    def ask(self, ctx: SceneContext):
        body = ctx.request
        mode = body["mode"]
        shot_path = Path(body["shot_path"]) if body.get("shot_path") else None
        if mode != "interactive":
            from autophone.application.ai_explore import explore

            return explore(
                body["prompt"],
                device=False,
                provider=body.get("provider"),
                model=body.get("model"),
                snapshot=mode == "snapshot",
                shot_path=shot_path,
            )

        from autophone.contracts import write_json
        from autophone.application import ai_agent
        from autophone.application.ai_explore import _default_screenshot

        if shot_path is None:
            raise RuntimeError("interactive scene missing shot_path")
        frozen_path = Path(body["frozen_path"])
        holder = self.run_service.lock.status(device=self.default_device)

        def waiting() -> None:
            ctx.emit({"phase": "waiting_screen", "type": "status", "text": "等待 ScreenLock"})

        ctx.emit({"phase": "waiting_screen", "type": "status", "text": "等待稳定设备起点"})
        with ai_agent.acquire_screen_cancelable(
            self.run_service.lock,
            device=self.default_device,
            run_id=f"{ctx.job_id}-planning",
            cancel_token=ctx.cancel_token,
            on_wait=waiting,
        ):
            shot = _default_screenshot(shot_path)
            if not shot:
                raise RuntimeError("interactive planning screenshot failed")
            state = ai_agent.capture_device_identity(
                body.get("app"),
                device=self.default_device,
            )
            state["captured_at"] = now_iso()
            state["captured_under_lock"] = True
        ctx.emit({"phase": "planning", "type": "status", "text": "生成并冻结计划"})
        planning, frozen = ai_agent.plan_task(
            body["prompt"],
            actor=ctx.actor,
            app=body.get("app"),
            device_state=state,
            provider=body.get("provider"),
            model=body.get("model"),
            timeout_s=300.0,
            screenshot=lambda _destination: shot,
            shot_path=shot_path,
        )
        if frozen is None:
            return {
                "ok": False,
                "error": planning.error or "planner returned no structured plan",
                "provider": planning.provider,
                "model": planning.model,
            }
        frozen_document = frozen.to_dict()
        write_json(frozen_path, {**frozen_document, "plan_digest": frozen.digest})
        metadata = {
            **dict(body.get("metadata") or {}),
            "lock_holder_before_wait": (
                holder.get("run_id") if holder.get("locked") else None
            ),
            "captured_under_lock": True,
            "captured_at": frozen.captured_at,
            "device_state": state,
            "frozen_plan_ref": str(frozen_path),
        }
        self.jobs.update(
            ctx.job_id,
            plan_digest=frozen.digest,
            approval_expires_at=frozen.approval_expires_at,
            metadata=metadata,
        )
        if frozen.tier != ai_agent.AUTONOMOUS:
            return {
                "ok": True,
                "outcome": "pending_confirmation",
                "tier": frozen.tier,
                "reason": frozen.reason,
                "plan": [vars(action) for action in frozen.actions],
                "frozen_plan": frozen_document,
                "plan_digest": frozen.digest,
                "approval_expires_at": frozen.approval_expires_at,
                "evidence_ref": frozen.evidence_ref,
                "provider": planning.provider,
                "model": planning.model,
                "cost_usd": planning.cost_usd,
                "duration_s": planning.duration_s,
            }
        ctx.emit({"phase": "waiting_screen", "type": "status", "text": "计划可自主执行，等待设备"})
        result = ai_agent.execute_frozen_plan(
            frozen,
            expected_digest=frozen.digest,
            actor=ctx.actor,
            consent="device_session",
            provider=body.get("provider"),
            model=body.get("model"),
            timeout_s=300.0,
            lock=self.run_service.lock,
            cancel_token=ctx.cancel_token,
            on_wait=waiting,
            run_id=ctx.job_id,
        )
        return {
            **result,
            "frozen_plan": frozen_document,
            "plan_digest": frozen.digest,
        }

    def ask_execution(self, ctx: SceneContext):
        from autophone.application.ai_agent import FrozenPlan, execute_frozen_plan

        body = ctx.request
        frozen = FrozenPlan.from_dict(body["frozen_plan"])

        def waiting() -> None:
            ctx.emit({"phase": "waiting_screen", "type": "status", "text": "等待 ScreenLock"})

        ctx.emit({"phase": "waiting_screen", "type": "status", "text": "等待执行冻结计划"})
        return execute_frozen_plan(
            frozen,
            expected_digest=body["plan_digest"],
            actor=ctx.actor,
            consent=body["data_consent"],
            provider=body.get("provider"),
            model=body.get("model"),
            lock=self.run_service.lock,
            cancel_token=ctx.cancel_token,
            on_wait=waiting,
            run_id=ctx.job_id,
        )
