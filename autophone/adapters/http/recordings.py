from __future__ import annotations

import base64

from autophone.record.web import WebRecordError
from autophone.application.recording_session import recording_device
from autophone.paths import APP_ASSET_ROOT, STATE_ROOT
from fastapi import Request
from fastapi import APIRouter
from .deps import HttpDeps

def install_helpers(deps: HttpDeps) -> None:

    def _raise_web_record(exc: WebRecordError) -> None:
        raise deps.HTTPException(
            status_code=exc.status,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    deps._raise_web_record = _raise_web_record

    def _recording_call(app: str, recording_id: str, op: str, payload: dict | None = None) -> dict:
        try:
            device = recording_device(app, recording_id, root=deps.recording_sessions.root)
            return deps._device_session_call(device, op, {
                **(payload or {}), "app": app, "recording_id": recording_id,
            })
        except WebRecordError as exc:
            deps._raise_web_record(exc)
        except deps.recordings.RecordingError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc

    deps._recording_call = _recording_call


def create_router(deps: HttpDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/apps/{app_name}/recordings/{recording_id:path}/case-recipe")
    def recording_case_context(app_name: str, recording_id: str) -> dict:
        from autophone.record import case_recipe
        try:
            return case_recipe.context(app_name, recording_id, root=deps.recording_sessions.root)
        except (ValueError, OSError, deps.ContractError) as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/api/apps/{app_name}/recordings/{recording_id:path}/case-recipe")
    def recording_case_recipe(app_name: str, recording_id: str, payload: dict) -> dict:
        from autophone.record import case_recipe
        if set(payload) - {"binding", "save"} or not isinstance(payload.get("save", False), bool):
            raise deps.HTTPException(status_code=422, detail="invalid case recipe request")
        try:
            return case_recipe.generate(app_name, recording_id, payload.get("binding"),
                                        save=payload.get("save", False), root=deps.recording_sessions.root, source="web")
        except (ValueError, OSError, deps.ContractError) as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc

    deps.recording_case_context = recording_case_context
    deps.recording_case_recipe = recording_case_recipe

    @router.get("/api/apps/{app_name}/recordings")
    def list_recordings(app_name: str, device: str = deps.Query(default=deps.DEFAULT_DEVICE)) -> dict:
        deps.load_app_profile(app_name)
        rows = deps.recordings.list_recordings(app_name)
        holder = deps.service.lock.recording_holder(device=device)
        if holder is not None:
            held_id = holder.get("recording_id")
            held_row = next((r for r in rows if r.get("recording_id") == held_id), None)
            if held_row is not None and held_row.get("status") != "recording":
                deps.service.lock.clear_recording(device=device, recording_id=str(held_id))
                holder = None
        return {
            "app": app_name,
            "recordings_root": str(deps.recordings.RECORDINGS_ROOT),
            "recordings": rows,
            "recording_holder": holder,
            "start_options": deps.recordings.start_options(app_name),
        }

    deps.list_recordings = list_recordings

    @router.post("/api/apps/{app_name}/recordings/start")
    def start_recording(app_name: str, payload: dict | None = None) -> dict:
        body = payload or {}
        profile = deps.load_app_profile(app_name)
        device = str(body.get("device") or deps.DEFAULT_DEVICE)
        root = deps.recording_sessions.root
        state_root = STATE_ROOT if root == APP_ASSET_ROOT else root
        return deps._device_session_call(device, "record_start", {
            "app": app_name, "device": device,
            "build": str(body.get("build") or profile.build_label or ""),
            "starts_with": body.get("starts_with") or None,
            "surface": str(body.get("surface") or "desktop"),
            "script": body.get("script") if isinstance(body.get("script"), dict) else None,
            "state_root": str(state_root.resolve()),
        })

    deps.start_recording = start_recording

    @router.post("/api/apps/{app_name}/recordings/replay-batch")
    def submit_replay_batch(app_name: str, payload: dict | None = None) -> dict:
        """录制页多选批量回放（record-replay-batch）：服务端解析 draft（不信前端）+ 顺序 worker。

        draft 解析失败 / 含 deprecated（非 active/draft）→ 422，不产生半个批次。
        """
        deps.load_app_profile(app_name)
        body = payload or {}
        recording_ids = body.get("recording_ids")
        if not isinstance(recording_ids, list) or not recording_ids:
            raise deps.HTTPException(status_code=422, detail="recording_ids 必须是非空数组")
        device = str(body.get("device") or deps.DEFAULT_DEVICE)
        try:
            replay_kwargs = {
                "app": app_name,
                "device": device,
                "recording_ids": [str(r) for r in recording_ids],
                "replay_speed": body.get("replay_speed"),
            }
            if body.get("telemetry_identity") is not None:
                replay_kwargs["telemetry_identity"] = body.get("telemetry_identity")
            return deps.service.replay_batch_service.submit_replay_batch(**replay_kwargs)
        except deps.ContractError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc

    deps.submit_replay_batch = submit_replay_batch

    @router.get("/api/apps/{app_name}/replay-batches")
    def list_replay_batches(app_name: str) -> dict:
        deps.load_app_profile(app_name)
        return {"app": app_name, "batches": deps.service.replay_batch_service.replay_batches(app=app_name)}

    deps.list_replay_batches = list_replay_batches

    @router.get("/api/apps/{app_name}/replay-batches/{batch_id}")
    def replay_batch_detail(app_name: str, batch_id: str) -> dict:
        deps.load_app_profile(app_name)
        view = deps.service.replay_batch_service.replay_batch_detail(batch_id)
        if view is None or view.get("app") != app_name:
            raise deps.HTTPException(status_code=404, detail=f"批次不存在：{batch_id}")
        return view

    deps.replay_batch_detail = replay_batch_detail

    @router.get("/api/apps/{app_name}/recordings/{recording_id:path}/telemetry")
    def recording_telemetry(app_name: str, recording_id: str) -> dict:
        deps.load_app_profile(app_name)
        try:
            return deps.recordings.recording_data_trace(app_name, recording_id)
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except deps.recordings.RecordingError as exc:
            raise deps.HTTPException(status_code=409, detail=str(exc)) from exc

    deps.recording_telemetry = recording_telemetry

    @router.post("/api/apps/{app_name}/recordings/{recording_id:path}/telemetry/query")
    def query_recording_telemetry(
        app_name: str,
        recording_id: str,
        payload: dict | None = None,
    ) -> dict:
        profile = deps.load_app_profile(app_name)
        body = payload or {}
        if set(body) != {"identity"}:
            raise deps.HTTPException(status_code=422, detail="only identity is accepted")
        try:
            return deps.recordings.query_recording_data_trace(
                app_name,
                recording_id,
                body.get("identity"),
                profile_raw=profile.raw,
            )
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except (deps.recordings.RecordingError, deps.TelemetryQueryValidationError, deps.DataTraceQueryError) as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        except deps.DataTraceConfigurationError as exc:
            raise deps.HTTPException(status_code=503, detail=str(exc)) from exc
        except (deps.DataTraceAdapterError, deps.DataTraceBackendError, deps.DataTraceContractError) as exc:
            raise deps.HTTPException(status_code=502, detail=str(exc)) from exc

    deps.query_recording_telemetry = query_recording_telemetry

    @router.get("/api/apps/{app_name}/recordings/{recording_id:path}/session")
    def web_record_session(app_name: str, recording_id: str) -> dict:
        deps.load_app_profile(app_name)
        try:
            return deps._recording_call(app_name, recording_id, "record_view")
        except deps.WebRecordError as exc:
            deps._raise_web_record(exc)

    deps.web_record_session = web_record_session

    @router.get("/api/apps/{app_name}/recordings/{recording_id:path}/session/frame")
    def web_record_frame(app_name: str, recording_id: str):
        deps.load_app_profile(app_name)
        try:
            frame = deps._recording_call(app_name, recording_id, "record_frame")
            data, seq, captured_at = base64.b64decode(frame["jpeg_b64"]), frame["seq"], frame["captured_at"]
        except deps.WebRecordError as exc:
            deps._raise_web_record(exc)
        from fastapi.responses import Response
        return Response(
            content=data,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store",
                "X-Frame-Seq": str(seq),
                "X-Captured-At": str(captured_at),
            },
        )

    deps.web_record_frame = web_record_frame

    @router.post("/api/apps/{app_name}/recordings/{recording_id:path}/session/heartbeat")
    def web_record_heartbeat(app_name: str, recording_id: str, request: Request) -> dict:
        try:
            return deps._recording_call(app_name, recording_id, "record_heartbeat", {"controller_token": deps._controller_token(request)})
        except deps.WebRecordError as exc:
            deps._raise_web_record(exc)

    deps.web_record_heartbeat = web_record_heartbeat

    @router.post("/api/apps/{app_name}/recordings/{recording_id:path}/session/event")
    def web_record_event(app_name: str, recording_id: str, request: Request, payload: dict | None = None) -> dict:
        try:
            return deps._recording_call(app_name, recording_id, "record_event", {"body": payload or {}, "controller_token": deps._controller_token(request)})
        except deps.WebRecordError as exc:
            deps._raise_web_record(exc)

    deps.web_record_event = web_record_event

    @router.post("/api/apps/{app_name}/recordings/{recording_id:path}/session/label")
    def web_record_label(app_name: str, recording_id: str, request: Request, payload: dict | None = None) -> dict:
        """给已提交的某一步命名。不碰设备，纯元数据——但提升期靠它把「人知道的语义」带上。"""
        try:
            return deps._recording_call(app_name, recording_id, "record_label", {"body": payload or {}, "controller_token": deps._controller_token(request)})
        except deps.WebRecordError as exc:
            deps._raise_web_record(exc)

    deps.web_record_label = web_record_label

    @router.post("/api/apps/{app_name}/recordings/{recording_id:path}/session/finish")
    def web_record_finish(app_name: str, recording_id: str, request: Request) -> dict:
        try:
            return deps._recording_call(app_name, recording_id, "record_finish", {"controller_token": deps._controller_token(request)})
        except deps.WebRecordError as exc:
            deps._raise_web_record(exc)

    deps.web_record_finish = web_record_finish

    @router.post("/api/apps/{app_name}/recordings/{recording_id:path}/session/cancel")
    def web_record_cancel(app_name: str, recording_id: str, request: Request) -> dict:
        try:
            return deps._recording_call(app_name, recording_id, "record_cancel", {"controller_token": deps._controller_token(request)})
        except deps.WebRecordError as exc:
            deps._raise_web_record(exc)

    deps.web_record_cancel = web_record_cancel

    @router.post("/api/apps/{app_name}/recordings/{recording_id:path}/session/claim")
    def web_record_claim(app_name: str, recording_id: str, request: Request) -> dict:
        try:
            return deps._recording_call(app_name, recording_id, "record_claim", {"controller_token": deps._controller_token(request)})
        except deps.WebRecordError as exc:
            deps._raise_web_record(exc)

    deps.web_record_claim = web_record_claim

    @router.post("/api/apps/{app_name}/recordings/{recording_id:path}/cancel")
    def cancel_recording(app_name: str, recording_id: str) -> dict:
        deps.load_app_profile(app_name)
        return deps._recording_call(app_name, recording_id, "record_desktop_cancel")

    deps.cancel_recording = cancel_recording

    @router.post("/api/apps/{app_name}/recordings/{recording_id:path}/lift-preview")
    def lift_preview_recording(app_name: str, recording_id: str, payload: dict | None = None) -> dict:
        """提升预览（不落库）：AI 把 trace 提升成 typed IR，回 {steps, decisions, summary}。
        落库前逐步审查的取数入口——AI 只在此创建期在环，审完拿编辑后的 steps 再走 /land。"""
        deps.load_app_profile(app_name)
        body = payload or {}
        try:
            deps.recordings.lift_preview_thunk(
                app_name,
                recording_id,
                provider=body.get("provider") or None,
                model=body.get("model") or None,
                script=body.get("script") if isinstance(body.get("script"), dict) else None,
            )
        except deps.recordings.RecordingError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        return deps._submit_scene("lift-recording", {
            "app": app_name, "recording_id": recording_id,
            "provider": body.get("provider") or None, "model": body.get("model") or None,
            "script": body.get("script") if isinstance(body.get("script"), dict) else None,
        }, provider=body.get("provider") or None)

    deps.lift_preview_recording = lift_preview_recording

    @router.post("/api/apps/{app_name}/recordings/{recording_id:path}/lift-preview-step")
    def lift_preview_recording_step(app_name: str, recording_id: str, payload: dict | None = None) -> dict:
        """重新提升单个 trace event，用于落库前审查时修复某一步 AI 调用失败。"""
        deps.load_app_profile(app_name)
        body = payload or {}
        try:
            index = int(body.get("index"))
        except (TypeError, ValueError) as exc:
            raise deps.HTTPException(status_code=422, detail="index is required") from exc
        try:
            deps.recordings.lift_preview_step_thunk(
                app_name,
                recording_id,
                index=index,
                provider=body.get("provider") or None,
                model=body.get("model") or None,
            )
        except deps.recordings.RecordingError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        return deps._submit_scene("lift-recording-step", {
            "app": app_name, "recording_id": recording_id, "index": index,
            "provider": body.get("provider") or None, "model": body.get("model") or None,
        }, provider=body.get("provider") or None)

    deps.lift_preview_recording_step = lift_preview_recording_step

    @router.get("/api/apps/{app_name}/recordings/{recording_id:path}/lift-preview-cache")
    def get_lift_preview_cache(app_name: str, recording_id: str) -> dict:
        """获取录制的提升预览缓存。有缓存 → {cached: true, result, provider, model, timestamp}，无缓存 → {cached: false}。"""
        deps.load_app_profile(app_name)
        cache = deps.recordings.get_lift_preview_cache(recording_id)
        if cache is None:
            return {"cached": False}
        return {"cached": True, **cache}

    deps.get_lift_preview_cache = get_lift_preview_cache

    @router.post("/api/apps/{app_name}/recordings/{recording_id:path}/recorded-steps")
    def land_recorded_steps(app_name: str, recording_id: str, payload: dict) -> dict:
        deps.load_app_profile(app_name)
        name = str(payload.get("name") or "").strip()
        if not name:
            raise deps.HTTPException(status_code=422, detail="operation name is required")
        try:
            return deps.recordings.land_recorded_steps(app_name, recording_id, name=name)
        except (deps.recordings.RecordingError, deps.ContractError) as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/api/apps/{app_name}/recordings/{recording_id:path}/land")
    def land_recording(app_name: str, recording_id: str, payload: dict | None = None) -> dict:
        body = payload or {}
        op_name = str(body.get("name") or "").strip()
        if not op_name:
            raise deps.HTTPException(status_code=422, detail="operation name is required")
        summary = str(body.get("summary") or "").strip() or op_name
        # 落库前审查通过后，前端回传编辑后的 steps → 直接落库不再跑 AI；省略则老路径现提升现落库。
        steps = body.get("steps")
        if steps is not None and not isinstance(steps, list):
            raise deps.HTTPException(status_code=422, detail="steps must be a list when provided")
        # step_events：每个 step 来自哪个录制事件（审查页按行透传）。只服务回放节奏，对不上就
        # 由 land 侧整块丢弃，故这里只校验形态、不 fail-closed。
        step_events = body.get("step_events")
        if step_events is not None and not isinstance(step_events, list):
            raise deps.HTTPException(status_code=422, detail="step_events must be a list when provided")
        try:
            deps.recordings.land_recording_thunk(
                app_name,
                recording_id,
                op_name=op_name,
                summary=summary,
                provider=body.get("provider") or None,
                model=body.get("model") or None,
                steps=steps,
                step_events=step_events,
                script_alignment=body.get("script_alignment") if isinstance(body.get("script_alignment"), dict) else None,
                script=body.get("script") if isinstance(body.get("script"), dict) else None,
            )
        except deps.recordings.RecordingError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        return deps._submit_scene("land-recording", {
            "app": app_name, "recording_id": recording_id, "op_name": op_name, "summary": summary,
            "provider": body.get("provider") or None, "model": body.get("model") or None,
            "steps": steps, "step_events": step_events,
            "script_alignment": body.get("script_alignment") if isinstance(body.get("script_alignment"), dict) else None,
            "script": body.get("script") if isinstance(body.get("script"), dict) else None,
        }, provider=body.get("provider") or None)

    deps.land_recording = land_recording

    @router.patch("/api/apps/{app_name}/recordings/{recording_id:path}")
    def edit_recording(app_name: str, recording_id: str, payload: dict | None = None) -> dict:
        deps.load_app_profile(app_name)
        body = payload or {}
        try:
            return deps.recordings.update_recording(app_name, recording_id, body)
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except deps.recordings.RecordingError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc

    deps.edit_recording = edit_recording

    @router.get("/api/apps/{app_name}/recordings/{recording_id:path}/shot/{file}")
    def recording_shot(app_name: str, recording_id: str, file: str):
        deps.load_app_profile(app_name)
        try:
            target = deps.recordings.shot_path(recording_id, file)
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except deps.recordings.RecordingError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        return deps.FileResponse(target, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    deps.recording_shot = recording_shot

    @router.delete("/api/apps/{app_name}/recordings/{recording_id:path}")
    def delete_recording(app_name: str, recording_id: str) -> dict:
        try:
            result = deps.recordings.delete_recording(app_name, recording_id)
            device = result.get("device")
            if device:
                deps.service.lock.clear_recording(device=str(device), recording_id=recording_id)
            return result
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except deps.recordings.RecordingError as exc:
            raise deps.HTTPException(status_code=409, detail=str(exc)) from exc

    deps.delete_recording = delete_recording

    return router
