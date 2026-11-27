from __future__ import annotations

from fastapi import APIRouter
from .deps import HttpDeps

def install_helpers(deps: HttpDeps) -> None:
    pass


def create_router(deps: HttpDeps) -> APIRouter:
    router = APIRouter()

    @router.post("/api/apps/{app_name}/impact/plan")
    def impact_plan(app_name: str, payload: dict | None = None) -> dict:
        """change manifest → 受影响页面状态 + 安全路径。默认只算不落盘（dry-run）。"""
        from autophone.observation.impact import manifest as impact_manifest
        from autophone.observation.impact import service as impact_service

        body = payload or {}
        manifest_doc = body.get("change_manifest")
        if not isinstance(manifest_doc, dict):
            raise deps.HTTPException(status_code=422, detail="change_manifest 必填（change-manifest.v1 对象）")
        try:
            plan = impact_service.plan_change(
                app_name,
                manifest_doc,
                build=body.get("build") or None,
                consumer_context=body.get("consumer_context") or None,
            )
        except (impact_manifest.ChangeManifestError, impact_service.ImpactError) as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        except deps.ContractError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        if not body.get("persist"):
            return {"ok": True, "persisted": False, "plan": plan}
        run_dir = deps.STATE_ROOT / "runs" / "impact" / str(plan["change_id"])
        written = impact_service.stage_run_inputs(
            run_dir,
            manifest_doc=manifest_doc,
            plan=plan,
            consumer_context=body.get("consumer_context") or None,
        )
        return {"ok": True, "persisted": True, "run_dir": str(run_dir), **written, "plan": plan}

    deps.impact_plan = impact_plan

    @router.post("/api/apps/{app_name}/impact/runs")
    def impact_run_create(app_name: str, payload: dict | None = None) -> dict:
        """创建 Impact Run；缺省只固化计划，``execute=true`` 才执行 Active 路径。

        设备动作复用 ``RunService.submit(type=operation)``。Candidate、Blocked、空路径、运行时
        非 Active Operation、effect 漂移和敏感 CTA 均不会进入该调用。
        """
        from autophone.observation.impact import manifest as impact_manifest
        from autophone.observation.impact import service as impact_service

        body = payload or {}
        if "execute" in body and not isinstance(body.get("execute"), bool):
            raise deps.HTTPException(status_code=422, detail="execute 必须是 boolean")
        manifest_doc = body.get("change_manifest")
        if not isinstance(manifest_doc, dict):
            raise deps.HTTPException(status_code=422, detail="change_manifest 必填（change-manifest.v1 对象）")
        try:
            plan = impact_service.plan_change(
                app_name,
                manifest_doc,
                build=body.get("build") or None,
                consumer_context=body.get("consumer_context") or None,
            )
        except (impact_manifest.ChangeManifestError, impact_service.ImpactError) as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        except deps.ContractError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        device = str(body.get("device") or deps.DEFAULT_DEVICE)
        requires_device = any(
            target.get("execution") in ("execute", "advisory")
            and bool(dict(target.get("route") or {}).get("steps"))
            for target in plan["targets"]
        )
        if body.get("execute") and requires_device:
            deps._configured_control_or_http_error()
            held = deps.service.lock.human_holder(device=device)
            if held is not None:
                raise deps.HTTPException(
                    status_code=409,
                    detail=f"设备正被 {held.get('owner_kind')} {held.get('owner_id')} 占用，拒绝执行 Impact Run",
                )
        run_id = f"{deps.time.strftime('%Y%m%d-%H%M%S')}-{app_name}-impact-{deps.secrets.token_hex(2)}"
        run_dir = deps.STATE_ROOT / "runs" / "impact" / run_id
        scenario_ref = None
        if "api_scenario" in body:
            from autophone.observation.impact import controlled_input
            try:
                scenario_ref = controlled_input.stage_reference(
                    deps.STATE_ROOT / "runs" / "impact", body["api_scenario"], run_dir, app=app_name,
                )
                plan["api_scenario"] = controlled_input.describe_input(run_dir, scenario_ref, app=app_name)
            except controlled_input.ControlledInputError as exc:
                raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        written = impact_service.stage_run_inputs(
            run_dir,
            manifest_doc=manifest_doc,
            plan=plan,
            consumer_context=body.get("consumer_context") or None,
        )
        executable = [t for t in plan["targets"] if t.get("execution") in ("execute", "advisory")]
        response = {
            "ok": True,
            "run_id": run_id,
            "run_dir": str(run_dir),
            **written,
            "targets": len(plan["targets"]),
            "executable": len(executable),
            "admission": plan["admission"],
            "execution": "pending",
            "note": "计划已固化；缺省不自动上机，显式 execute=true 才执行 Active 路径",
        }
        if not body.get("execute"):
            return response

        def _run_operation(name: str, child_run_id: str) -> dict:
            from autophone.execution.dispatch import RunDispatcher
            api_plane = {"mode": "advisory"}
            if scenario_ref is not None:
                api_plane = controlled_input.stage_child(
                    run_dir, scenario_ref, deps.service._run_dir(child_run_id),
                    run_id=child_run_id, app=app_name, device=device,
                )
            handle = RunDispatcher(deps.service).submit({
                "schema_version": "v1",
                "run_id": child_run_id,
                "type": "operation",
                "app": app_name,
                "device": device,
                "operation": {"name": name, "with": {}},
                "planes": {"api": api_plane},
                "requested_by": str(body.get("requested_by") or deps._current_actor()),
                "requested_at": deps.now_iso(),
            }, pin_device=True)
            out = handle.summary()
            detail = str(
                (((handle.result.get("planes") or {}).get("ui") or {}).get("detail")) or ""
            )
            if detail:
                out["detail"] = detail
            return out

        try:
            executed = impact_service.execute_run(
                run_dir,
                run_id=run_id,
                operation_runner=_run_operation,
                run_dir_resolver=lambda child_run_id: deps.service.queries.find_run_dir(child_run_id),
            )
        except (impact_service.ImpactError, deps.ContractError, deps.ControlStateError, ValueError) as exc:
            raise deps.HTTPException(status_code=409, detail=str(exc)) from exc
        response.update({
            "execution": executed["execution"]["status"],
            "execution_summary": executed["execution"]["summary"],
            "execution_path": executed["execution_path"],
            "result_path": executed["result_path"],
            "report_path": executed["report_path"],
            "note": "Active 路径执行已收口；API/语义差异证据缺失的已到达目标保持 inconclusive",
        })
        return response

    deps.impact_run_create = impact_run_create

    @router.get("/api/apps/{app_name}/impact/index")
    def impact_index(app_name: str, build: str | None = None, rebuild: bool = False) -> dict:
        """派生影响索引。``rebuild=true`` 时先扫 runs 树重建（索引可删可重建，不是真源）。"""
        from autophone.observation.impact import service as impact_service

        try:
            if rebuild:
                payload = impact_service.rebuild_index(app_name, build=build)
                return {"app": app_name, "rebuilt": True, "path": payload["path"], "index": payload["index"]}
            doc = impact_service.read_index(app_name, build=build)
        except impact_service.ImpactError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        if doc is None:
            raise deps.HTTPException(
                status_code=404,
                detail=f"impact index 尚未构建：app={app_name} build={build or 'profile.build_label'}；"
                       "用 ?rebuild=true 或 CLI `impact index` 生成",
            )
        return {"app": app_name, "rebuilt": False, "index": doc}

    deps.impact_index = impact_index

    @router.get("/api/apps/{app_name}/impact/review/{screen_id}")
    def impact_screen_review(
        app_name: str, screen_id: str, build: str | None = None
    ) -> dict:
        """页面模块人审工作台：聚合 Screen、Binding、Operation 与运行证据。"""
        from autophone.observation.impact import service as impact_service

        try:
            profile = deps.load_app_profile(app_name)
            return impact_service.screen_review(
                profile,
                screen_id,
                build=build,
                operation_decider=lambda op: deps.service.promotions.operation_promotion_decision(
                    draft_id=str(op.get("id") or ""), profile=profile, op_def=op
                ),
            )
        except impact_service.ImpactError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except (deps.ContractError, ValueError) as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc

    deps.impact_screen_review = impact_screen_review

    @router.get("/api/impact/runs/{run_id}")
    def impact_run_detail(run_id: str) -> dict:
        """一次 impact run 的计划与结论。"""
        from autophone.observation.impact import observations as observations_mod
        from autophone.observation.impact import service as impact_service

        run_dir = deps.STATE_ROOT / "runs" / "impact" / run_id
        if not run_dir.exists():
            matches = sorted(
                (deps.STATE_ROOT / "runs").glob(f"**/{run_id}"),
                key=lambda p: p.stat().st_mtime, reverse=True,
            )
            run_dir = next((m for m in matches if m.is_dir()), run_dir)
        if not run_dir.exists():
            raise deps.HTTPException(status_code=404, detail=f"impact run not found: {run_id}")
        try:
            plan = impact_service.load_plan(run_dir)
        except impact_service.ImpactError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        result = impact_service.read_result(run_dir)
        execution = impact_service.read_execution(run_dir)
        report = run_dir / "impact" / "report.html"
        return {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "plan": plan,
            "execution": execution,
            "result": result,
            "report_html": str(report) if report.exists() else None,
            "observations": len(observations_mod.load_observations(run_dir)),
        }

    deps.impact_run_detail = impact_run_detail

    @router.post("/api/apps/{app_name}/impact/bindings/{binding_id}/review")
    def impact_binding_review(app_name: str, binding_id: str, payload: dict | None = None) -> dict:
        """人审一条 FieldBinding：改置信度/状态并附证据。

        ``correlated → causal`` 必须带 negative_control_runs 或 authority=backend_contract，
        否则 409——把相关性当因果会直接错报受影响页面。
        """
        from autophone.observation.impact import bindings as impact_bindings
        from autophone.observation.impact import service as impact_service

        body = payload or {}
        try:
            item = impact_service.review_binding(
                app_name,
                binding_id,
                confidence=body.get("confidence") or None,
                status=body.get("status") or None,
                evidence=body.get("evidence") or {},
                note=body.get("note") or None,
            )
        except KeyError as exc:
            raise deps.HTTPException(status_code=404, detail=f"binding not found: {binding_id}") from exc
        except impact_bindings.BindingError as exc:
            raise deps.HTTPException(status_code=409, detail=str(exc)) from exc
        except impact_service.ImpactError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, "binding": item}

    deps.impact_binding_review = impact_binding_review

    return router
