from __future__ import annotations

from pathlib import Path
from fastapi import APIRouter
from .deps import HttpDeps

def install_helpers(deps: HttpDeps) -> None:

    def _strict_ai_request(model, payload: dict | None):
        from pydantic import ValidationError

        try:
            return model.model_validate(payload or {})
        except ValidationError as exc:
            raise deps.HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc

    deps._strict_ai_request = _strict_ai_request

    def _resolve_ai_fix_run_dir(run_id: str, run_dir: str | None = None) -> Path:
        try:
            return deps.ai_fix_mod.resolve_run_dir(deps.service.run_root, run_id, run_dir=run_dir)
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except deps.ai_fix_mod.AiFixError as exc:
            text = str(exc)
            try:
                detail = deps.json.loads(text)
            except deps.json.JSONDecodeError:
                raise deps.HTTPException(status_code=422, detail=text) from exc
            if detail.get("error") == "ambiguous_run_id":
                raise deps.HTTPException(status_code=409, detail=detail) from exc
            raise deps.HTTPException(status_code=422, detail=detail) from exc

    deps._resolve_ai_fix_run_dir = _resolve_ai_fix_run_dir


def create_router(deps: HttpDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/ai/doctor")
    def ai_doctor() -> dict:
        """Static CLI/capability detection only. This GET never invokes a model."""
        from autophone.application import ai as ai_capability

        return ai_capability.ai_doctor()

    deps.ai_doctor = ai_doctor

    @router.post("/api/ai/probes")
    def ai_probe(payload: dict | None = None) -> dict:
        """True provider probe is an ordinary bounded/cancellable AI job."""
        from autophone.application.ai_admission import AiProbeRequest

        req = deps._strict_ai_request(AiProbeRequest, payload)
        return deps._submit_scene("probe", req.model_dump(), provider=req.provider)

    deps.ai_probe = ai_probe

    @router.get("/api/ai/verify-doctor")
    def ai_verify_doctor() -> dict:
        """AI-2 验证执行器挂载状态：AGENT_CMD / RESULT_JSON 是否配置（只报 bool + 来源，不回显命令本身）。"""
        from autophone.execution.verification import verification_agent_doctor

        return verification_agent_doctor()

    deps.ai_verify_doctor = ai_verify_doctor

    @router.get("/api/ai/jobs/{job_id}")
    def ai_job(job_id: str) -> dict:
        job = deps.ai_jobs.get(job_id, actor=deps._current_actor())
        if job is None:
            raise deps.HTTPException(status_code=404, detail=f"ai job not found: {job_id}")
        return job

    deps.ai_job = ai_job

    @router.get("/api/ai/jobs")
    def ai_job_list(status: str | None = None, limit: int = 50) -> dict:
        if status not in {None, "active"}:
            raise deps.HTTPException(status_code=422, detail="status must be active when supplied")
        return {"jobs": deps.ai_jobs.list(actor=deps._current_actor(), active_only=status == "active", limit=limit)}

    deps.ai_job_list = ai_job_list

    @router.post("/api/ai/jobs/{job_id}/cancel")
    def ai_job_cancel(job_id: str) -> dict:
        job = deps.ai_jobs.cancel(job_id, actor=deps._current_actor())
        if job is None:
            raise deps.HTTPException(status_code=404, detail=f"ai job not found: {job_id}")
        return job

    deps.ai_job_cancel = ai_job_cancel

    @router.post("/api/ai/diagnose")
    def ai_diagnose(payload: dict | None = None) -> dict:
        """AI-1 失败归因：喂 run 净化包给 agent 给结构化诊断（异步 job）。"""
        from autophone.application.ai_admission import AiDiagnoseRequest

        req = deps._strict_ai_request(AiDiagnoseRequest, payload)
        run_id = req.run_id
        try:
            run_dir = deps.service.queries.find_run_dir(run_id)
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        return deps._submit_scene("diagnose", {
            **req.model_dump(), "run_dir": str(run_dir),
        }, provider=req.provider)

    deps.ai_diagnose = ai_diagnose

    @router.post("/api/runs/{run_id}/ai/propose-fix")
    def ai_propose_fix(run_id: str, payload: dict | None = None, run_dir: str | None = deps.Query(default=None)) -> dict:
        """AI fix proposal. Fails closed on ambiguous run_id unless run_dir is supplied."""
        body = payload or {}
        target = deps._resolve_ai_fix_run_dir(run_id, run_dir=run_dir or body.get("run_dir"))
        patch = body.get("patch") if isinstance(body.get("patch"), dict) else None
        proposal_target = body.get("target") if isinstance(body.get("target"), dict) else None

        return deps._submit_scene("propose-fix", {
            "run_dir": str(target), "patch": patch, "target": proposal_target,
        })

    deps.ai_propose_fix = ai_propose_fix

    @router.post("/api/runs/{run_id}/ai/apply-fix")
    def ai_apply_fix(run_id: str, payload: dict | None = None, run_dir: str | None = deps.Query(default=None)) -> dict:
        body = payload or {}
        target = deps._resolve_ai_fix_run_dir(run_id, run_dir=run_dir or body.get("run_dir"))
        try:
            return deps.ai_fix_mod.apply_proposal(target)
        except deps.ai_fix_mod.AiFixError as exc:
            raise deps.HTTPException(status_code=409, detail=str(exc)) from exc

    deps.ai_apply_fix = ai_apply_fix

    @router.post("/api/runs/{run_id}/ai/reject-fix")
    def ai_reject_fix(run_id: str, payload: dict | None = None, run_dir: str | None = deps.Query(default=None)) -> dict:
        body = payload or {}
        target = deps._resolve_ai_fix_run_dir(run_id, run_dir=run_dir or body.get("run_dir"))
        try:
            return deps.ai_fix_mod.reject_proposal(target, actor=str(body.get("actor") or "web"), reason=str(body.get("reason") or ""))
        except deps.ai_fix_mod.AiFixError as exc:
            raise deps.HTTPException(status_code=409, detail=str(exc)) from exc

    deps.ai_reject_fix = ai_reject_fix

    @router.post("/api/ai/draft-operation")
    def ai_draft_operation(payload: dict | None = None) -> dict:
        """AI-4 操作起草：把需求冻结成一条 operation recipe，落库 status=draft（异步 job）。"""
        from autophone.application.ai_admission import AiDraftOperationRequest

        req = deps._strict_ai_request(AiDraftOperationRequest, payload)

        return deps._submit_scene("draft-operation", req.model_dump(), provider=req.provider)

    deps.ai_draft_operation = ai_draft_operation

    @router.post("/api/ai/convert-case")
    def ai_convert_case(payload: dict | None = None) -> dict:
        """AI-5 用例→recipe 转换：把一条自然语言用例转成 typed-IR recipe 草稿（异步 job）。

        产物恒 draft（落 ``recipes/drafts/<id>.yaml``，不进 active 回归库）。返回的
        ``result`` 形态：成功 ``{ok:True, recipe:{id,steps}, …}``；
        缺操作是成功业务结果 ``{ok:True, outcome:"needs_extraction", needs_extraction, reason, …}``；
        fail-closed 为 ``{ok:False, error}``。
        前端把 ``recipe.steps`` 填进编辑器供人复核，人显式保存才写 live recipe。
        """
        from autophone.application.ai_admission import AiConvertCaseRequest
        from autophone.application.case_intake_progress import new_journey

        req = deps._strict_ai_request(AiConvertCaseRequest, payload)

        return deps._submit_scene(
            "convert-case",
            req.model_dump(),
            provider=req.provider,
            metadata={"journey": new_journey(req.app, req.case_id)},
        )

    deps.ai_convert_case = ai_convert_case

    @router.post("/api/ai/ask")
    def ai_ask(payload: dict | None = None) -> dict:
        """AI-3 text/snapshot/interactive admission and execution.

        Every image-bearing request carries a receipt created for this job.  An
        interactive planning job freezes the exact plan; approval creates a child
        that can only execute that frozen plan and has no planner code path.
        """
        from autophone.application.ai_admission import AiAskRequest, admit_ask

        req = deps._strict_ai_request(AiAskRequest, payload)
        try:
            accepted = admit_ask(req, actor=deps._current_actor())
        except ValueError as exc:
            raise deps.HTTPException(status_code=422, detail={"code": "provider_capability", "message": str(exc)}) from exc

        mode = req.mode
        job_id = deps.ai_jobs.make_job_id("ask")
        shot_path = deps.STATE_ROOT / "runs" / ".ai" / "jobs" / job_id / "current-screen.jpg"

        metadata = {
            "request_mode": mode,
            "prompt_summary": req.prompt[:200],
            "provider_requested": req.provider,
            "artifact_refs": [str(shot_path)] if mode in {"snapshot", "interactive"} else [],
        }
        scene_request = {
            **req.model_dump(),
            "provider": accepted.provider,
            "shot_path": str(shot_path) if mode in {"snapshot", "interactive"} else None,
            "frozen_path": str(deps.STATE_ROOT / "runs" / ".ai" / "jobs" / job_id / "frozen-plan.json")
            if mode == "interactive" else None,
            "metadata": metadata,
        }
        return deps._submit_scene(
            "ask", scene_request,
            idempotency_request=req.model_dump(),
            job_id=job_id,
            metadata=metadata,
            actor=accepted.actor,
            lane=accepted.lane,
            provider=accepted.provider,
            consent_receipt=accepted.consent_receipt,
        )

    deps.ai_ask = ai_ask

    @router.post("/api/ai/ask/{planning_job_id}/approve")
    def ai_ask_approve(planning_job_id: str, payload: dict | None = None) -> dict:
        from datetime import datetime

        from autophone.application.ai_admission import AiAskApproveRequest
        from autophone.application.ai_agent import FrozenPlan

        req = deps._strict_ai_request(AiAskApproveRequest, payload)
        actor = deps._current_actor()
        parent = deps.ai_jobs.get(planning_job_id, actor=actor)
        if parent is None:
            raise deps.HTTPException(status_code=404, detail=f"ai job not found: {planning_job_id}")
        result = parent.get("result") or {}
        if parent.get("status") != "succeeded" or result.get("outcome") != "pending_confirmation":
            raise deps.HTTPException(status_code=409, detail={"code": "job_not_approvable"})
        if parent.get("plan_digest") != req.plan_digest:
            raise deps.HTTPException(status_code=409, detail={"code": "plan_digest_mismatch"})
        expiry = parent.get("approval_expires_at")
        if not expiry or datetime.fromisoformat(expiry) <= datetime.now().astimezone():
            raise deps.HTTPException(status_code=409, detail={"code": "plan_expired"})
        frozen_raw = result.get("frozen_plan")
        if not isinstance(frozen_raw, dict):
            raise deps.HTTPException(status_code=409, detail={"code": "frozen_plan_missing"})
        FrozenPlan.from_dict(frozen_raw)  # validate persisted shape before child creation
        child_id = deps.ai_jobs.make_job_id("ask-execution")
        receipt = {"type": "device_session", "confirmed_at": deps.now_iso()}

        # 审批一次性：原子占用后再派发。双击/重试的第二次拿不到占用直接 409，
        # 不会给同一冻结计划派两个真机执行 job（前端每次请求新 Idempotency-Key，
        # 幂等 key 挡不住这种重放）。
        consumed = deps.ai_jobs.consume_approval(planning_job_id)
        if consumed is None:
            raise deps.HTTPException(status_code=409, detail={"code": "job_not_approvable"})
        try:
            return deps._submit_scene(
                "ask-execution", {
                    "frozen_plan": frozen_raw, "plan_digest": req.plan_digest,
                    "data_consent": req.data_consent, "provider": parent.get("provider"),
                    "model": parent.get("model"),
                },
                idempotency_request={"planning_job_id": planning_job_id, **req.model_dump()},
                job_id=child_id,
                actor=actor,
                lane="device",
                provider=parent.get("provider"),
                parent_job_id=planning_job_id,
                plan_digest=req.plan_digest,
                approval_expires_at=expiry,
                consent_receipt=receipt,
                metadata={"request_mode": "interactive_execution"},
            )
        except BaseException:
            # 派发失败（队列满等）要把审批还回去，让用户可以重试。
            deps.ai_jobs.update(planning_job_id, result=consumed)
            raise

    deps.ai_ask_approve = ai_ask_approve

    return router
