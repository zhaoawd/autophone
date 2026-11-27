from __future__ import annotations

from pathlib import Path
from autophone.application.cases import (
    CaseLifecycleApplication,
    CaseRecipeApplication,
    CaseRecipeApplicationError,
)
from fastapi import Request
from fastapi import APIRouter
from .deps import HttpDeps

def install_helpers(deps: HttpDeps) -> None:

    def _case_lifecycle(profile) -> CaseLifecycleApplication:
        return deps.CaseLifecycleApplication(
            profile,
            negctl_root=deps.STATE_ROOT / "runs" / "negctl",
            activation_policy=deps.activation_blocker,
            proof_loader=deps.load_discrimination_proof,
            discrimination_required=deps.case_requires_discrimination,
            gap_evaluator=deps.discrimination_gaps,
        )

    deps._case_lifecycle = _case_lifecycle

    def _case_import_stage_dir() -> Path:
        return deps._uploads().case_stage_dir()

    deps._case_import_stage_dir = _case_import_stage_dir

    def _case_import_app_stage_dir(app_name: str) -> Path:
        try:
            return deps._uploads(case_stage_root=deps._case_import_stage_dir()).case_app_stage_dir(app_name)
        except deps.UploadApplicationError as error:
            raise ValueError(str(error)) from error

    deps._case_import_app_stage_dir = _case_import_app_stage_dir

    async def _stage_request_xlsx(request: Request, app_name: str) -> Path:
        try:
            return await deps._uploads().stage_case_xlsx(
                app_name,
                request.stream(),
                content_length=deps._request_content_length(request),
                stage_directory=deps._case_import_app_stage_dir(app_name),
            )
        except deps.UploadApplicationError as error:
            raise deps.HTTPException(error.status_code, detail=error.detail) from error

    deps._stage_request_xlsx = _stage_request_xlsx

    def _preview_case_import(profile, xlsx_path: Path) -> dict:
        """解析模板 + 比对现存 external_id，算出本次将新增/去重命中，供 UI 预览。"""

        try:
            return CaseRecipeApplication(profile).preview_import(xlsx_path)
        except CaseRecipeApplicationError as exc:
            raise ValueError(str(exc)) from exc

    deps._preview_case_import = _preview_case_import


def create_router(deps: HttpDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/apps/{app_name}/cases")
    def cases(
        app_name: str,
        automation: str | None = None,
        include_deprecated: bool = False,
    ) -> dict:
        profile = deps.load_app_profile(app_name)
        rows = deps.CaseRecipeApplication(profile).rows()
        if not include_deprecated:
            # 软删的用例默认不进列表/勾选；历史 run 仍引用它，故 YAML 里保留可追溯
            rows = [case for case in rows if case.get("status") != "deprecated"]
        if automation:
            rows = [case for case in rows if case.get("automation") == automation]
        # 新模板导入会把 import_semantics 固化进 YAML；对历史导入/手工创建的用例则在
        # 读模型即时补一份，避免用户得重导（或批量改写真源）才能在右栏看到整理后的内容。
        # 这是展示派生数据，不参与 runner，也不写回 cases YAML。
        from autophone.assets.case_import.import_helpers import derive_import_semantics

        def _semantic_lines(value) -> list[str]:
            if isinstance(value, list):
                return [str(item) for item in value]
            return [str(value)] if value else []

        for case in rows:
            derived = derive_import_semantics(
                item=str(case.get("item") or ""),
                precondition=_semantic_lines(case.get("precondition")),
                steps=_semantic_lines(case.get("steps")),
                expects=_semantic_lines(case.get("expects")),
                source="derived",
            )
            # 已存的导入解析只要仍与当前三段用例语义对齐，就保留其 import 来源；若人工改过
            # precondition/steps/expects，立即用新版字段替换展示模型，不把过期解析拿给右栏。
            stored = case.get("import_semantics")
            if isinstance(stored, dict) and stored.get("input_fingerprint") == derived["input_fingerprint"]:
                continue
            case["import_semantics"] = derived
        # 派生区分力态（ADR-0003：soft-hold 作为派生 exception，不改 status、不落库）：仅对
        # **active 且可执行**的用例就地算一份 discrimination={state,...}，供面板标「该 build 区分力
        # 未复验」。stale/toothless/unproven 等非 proven 态即一条派生 exception；proven/not_required
        # 不出告警。require_discrimination:false 时整 app 不算（字段缺省即视作无此维度）。
        if profile.require_discrimination:
            from autophone.assets.promotion_identity import (
                current_artifact_content_digest as _default_artifact_digest,
                current_discrimination_context as _default_disc_context,
            )

            negctl_root = deps.STATE_ROOT / "runs" / "negctl"
            deps_manifest = deps.STATE_ROOT / "packages" / profile.app / "manifest.json"
            state_root = (
                deps.STATE_ROOT
                if deps_manifest.is_file()
                else (getattr(profile, "host_state_root", None) or deps.STATE_ROOT)
            )
            artifact_digest_fn = getattr(deps, "current_artifact_content_digest", None) or _default_artifact_digest
            disc_context_fn = getattr(deps, "current_discrimination_context", None) or _default_disc_context
            artifact_digest = artifact_digest_fn(profile, state_root=state_root)
            for case in rows:
                if case.get("status") == "active" and deps.case_requires_discrimination(case):
                    case_id = str(case.get("id"))
                    disc_ctx = disc_context_fn(
                        profile,
                        case_id=case_id,
                        current_artifact_content_digest=artifact_digest,
                        state_root=state_root,
                    )
                    proof = deps.load_discrimination_proof(
                        negctl_root,
                        case_id=case_id,
                        app=profile.app,
                        preferred_build_label=profile.build_label,
                        **disc_ctx,
                    )
                    case["discrimination"] = deps.discrimination_status(
                        required=True,
                        proof=proof,
                        current_build_label=profile.build_label,
                        **disc_ctx,
                    )
        # 草稿用例读、写都只用 recipes/drafts/<id>.yaml；execution_recipe_target 是编辑器保存
        # Recipe 时的写入位置，草稿用例在 live 路径另有文件时带 legacy_live_path。
        case_recipes = deps.CaseRecipeApplication(profile)
        for case in rows:
            case_id = str(case.get("id") or "")
            lifecycle = str(case.get("status") or "")
            recipe = case_recipes.load(case_id, lifecycle=lifecycle)
            if recipe is not None:
                case["execution_recipe"] = recipe
            target = case_recipes.recipe_target(case_id, lifecycle=lifecycle)
            if target is not None:
                case["execution_recipe_target"] = target
            case["execution"] = case_recipes.derive_execution(case, recipe)
        return {
            "app": app_name,
            "cases_path": str(profile.cases_path()),
            "count": len(rows),
            "cases": rows,
        }

    deps.cases = cases

    @router.patch("/api/apps/{app_name}/cases/{case_id}")
    def edit_case(app_name: str, case_id: str, payload: dict | None = None) -> dict:
        """Edit one Case through the Case Lifecycle application Module."""
        profile = deps.load_app_profile(app_name)
        body = payload or {}
        changes = {key for key in body if key not in {"id", "execution_recipe"}}
        try:
            result = deps._case_lifecycle(profile).edit(case_id, body)
        except deps.CaseLifecycleApplicationError as exc:
            raise deps.HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        deps.logger.info(
            "case edited: app=%s case=%s fields=%s recipe_changed=%s "
            "status=%s revision=%s",
            app_name, case_id, sorted(changes), "execution_recipe" in body,
            result.get("status"), result.get("revision"),
        )
        return result

    deps.edit_case = edit_case

    @router.post("/api/apps/{app_name}/cases/from-verification")
    def promote_verification(app_name: str, payload: dict | None = None) -> dict:
        """把一张审核通过的验证单收录成未分类 Case Draft。

        body: {run_id, overrides?}。fail-closed：非验证单 / 未审核通过 / 缺 draft_id /
        已收录过 → 422。Recipe、连续 Trial、人审和 Active 转正由 Case Draft 专用流程完成。
        """
        body = payload or {}
        run_id = str(body.get("run_id") or "").strip()
        if not run_id:
            raise deps.HTTPException(status_code=422, detail="run_id is required")
        profile = deps.load_app_profile(app_name)
        try:
            result = deps.service.promotions.promote_verification(
                run_id, profile=profile, overrides=body.get("overrides") or None
            )
            deps.logger.info(
                f"verification ingested as case draft: app={app_name} run={run_id} "
                f"→ case={result.get('id')} status={result.get('status')}"
            )
            return result
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except (deps.ContractError, ValueError) as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc

    deps.promote_verification = promote_verification

    @router.post("/api/apps/{app_name}/cases/{case_id}/deprecate")
    def deprecate_case(app_name: str, case_id: str, payload: dict | None = None) -> dict:
        """软删一条用例：status=deprecated（+ 可选 reason）。历史 run 仍可追溯。"""
        profile = deps.load_app_profile(app_name)
        reason = (payload or {}).get("reason") or None
        try:
            result = deps._case_lifecycle(profile).deprecate(case_id, reason=reason)
            deps.logger.info(f"case deprecated: app={app_name} case={case_id} reason={reason!r}")
            return result
        except deps.CaseLifecycleApplicationError as exc:
            raise deps.HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    deps.deprecate_case = deprecate_case

    @router.post("/api/apps/{app_name}/cases/batch-deprecate")
    def batch_deprecate_cases(app_name: str, payload: dict | None = None) -> dict:
        """批量软删多条用例：一次调用、一次汇总。单条失败 fail-soft，返回 {deprecated, failed}。"""
        ids = (payload or {}).get("ids") or []
        if not isinstance(ids, list) or not all(isinstance(i, (str, int)) for i in ids):
            raise deps.HTTPException(status_code=422, detail="ids must be a list of case ids")
        if not ids:
            raise deps.HTTPException(status_code=422, detail="ids is empty")
        profile = deps.load_app_profile(app_name)
        result = deps._case_lifecycle(profile).deprecate_many(
            [str(item) for item in ids],
            reason=(payload or {}).get("reason") or None,
        )
        deps.logger.info(
            f"cases batch-deprecated: app={app_name} requested={len(ids)} "
            f"deprecated={len(result['deprecated'])} failed={len(result['failed'])}"
        )
        return result

    deps.batch_deprecate_cases = batch_deprecate_cases

    @router.post("/api/apps/{app_name}/cases/batch-activate")
    def batch_activate_cases(app_name: str, payload: dict | None = None) -> dict:
        """Activate eligible non-ui_ready Cases through the lifecycle Module."""
        ids = (payload or {}).get("ids") or []
        if not isinstance(ids, list) or not all(isinstance(item, (str, int)) for item in ids):
            raise deps.HTTPException(status_code=422, detail="ids must be a list of case ids")
        if not ids:
            raise deps.HTTPException(status_code=422, detail="ids is empty")
        profile = deps.load_app_profile(app_name)
        result = deps._case_lifecycle(profile).activate_many([str(item) for item in ids])
        deps.logger.info(
            "cases batch-activated: app=%s requested=%s activated=%s failed=%s",
            app_name, len(ids), result["activated_count"], result["failed_count"],
        )
        return result

    deps.batch_activate_cases = batch_activate_cases

    @router.post("/api/apps/{app_name}/cases")
    def add_case(app_name: str, payload: dict | None = None) -> dict:
        """手工新增一条用例。默认 status=draft（需复核转正才进回归），item 必填。

        body 业务字段：module/item/level/automation/precondition/steps/expects/
        case_type/test_point/owner/remark/order_type/external_id，以及 business_family/
        family_id/pair_id/family_variant。
        """
        profile = deps.load_app_profile(app_name)
        try:
            result = deps._case_lifecycle(profile).add(payload or {})
            deps.logger.info(
                f"case added: app={app_name} id={result.get('id')} status={result.get('status')} "
                f"item={result.get('item')!r}"
            )
            return result
        except deps.CaseLifecycleApplicationError as exc:
            raise deps.HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    deps.add_case = add_case

    @router.get("/api/cases/template")
    def download_case_template():
        """下载通用用例导入模板 xlsx（autophone/_data/test-cases-template.xlsx，随包发布）。"""
        template = deps.Path(__file__).resolve().parents[2] / "_data" / "test-cases-template.xlsx"
        template = template.resolve()
        if not template.is_file():
            raise deps.HTTPException(status_code=404, detail="用例模板文件不存在")
        return deps.FileResponse(
            template,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="test-cases-template.xlsx",
        )

    deps.download_case_template = download_case_template

    @router.post("/api/apps/{app_name}/cases/import/stage")
    async def stage_case_import(request: Request, app_name: str) -> dict:
        """上传通用用例模板 xlsx，解析后回预览（不落库）：新增 vs 按 external_id 去重的命中。

        返回 {staged_path, total, new, dup, new_sample, dup_external_ids}，以及 Family 建议
        （family_rows/family_snapshot）与有效性、准备度建议（suggestion_rows/suggestion_context，
        BL-038 B2，只读、不落库）；commit 端按 staged_path 真正写入 draft。
        """
        profile = deps.load_app_profile(app_name)  # 422 早失败：app 不存在就别收文件
        dest = await deps._stage_request_xlsx(request, app_name)
        try:
            preview = deps._preview_case_import(profile, dest)
        except SystemExit as exc:  # from_template fail-loud（表头识别不到等）
            dest.unlink(missing_ok=True)
            deps.logger.warning(f"case import stage rejected: app={app_name} file={dest.name} reason={exc}")
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            dest.unlink(missing_ok=True)
            deps.logger.warning(
                f"case import stage contract rejected: app={app_name} "
                f"file={dest.name} reason={exc}"
            )
            raise deps.HTTPException(status_code=422, detail={
                "code": "case_import_contract_error",
                "message": str(exc),
            }) from exc
        except Exception:
            dest.unlink(missing_ok=True)
            raise
        deps.logger.info(
            f"case import staged: app={app_name} file={dest.name} "
            f"total={preview['total']} new={preview['new']} dup={preview['dup']}"
        )
        return preview

    deps.stage_case_import = stage_case_import

    @router.post("/api/apps/{app_name}/cases/import/commit")
    def commit_case_import(app_name: str, payload: dict | None = None) -> dict:
        """把暂存的模板解析结果按 external_id 去重后追加为 draft。body: {staged_path}。"""
        from autophone.application.cases import (
            CaseImportApplication,
            CaseImportApplicationError,
        )

        CaseImportApplication.ensure_writable()
        body = payload or {}
        staged_path = str(body.get("staged_path") or "").strip()
        if not staged_path:
            raise deps.HTTPException(status_code=422, detail={
                "code": "case_import_staged_path_required",
                "message": "staged_path is required",
            })
        # Profile 校验必须早于创建 App staging 目录；无效 App 不应在共享 tmp 根下留下目录。
        profile = deps.load_app_profile(app_name)
        try:
            stage_dir = deps._case_import_app_stage_dir(app_name)
        except ValueError as exc:
            raise deps.HTTPException(status_code=422, detail={
                "code": "case_import_invalid_app_stage",
                "message": str(exc),
            }) from exc
        try:
            committed = CaseImportApplication(
                profile, stage_dir, batch_state_root=deps.STATE_ROOT,
            ).commit(
                staged_path,
                family_selections=body.get("family_selections"),
                family_snapshot=body.get("family_snapshot"),
                actor=deps._current_actor(),
                source_filename=body.get("source_filename"),
            )
        except CaseImportApplicationError as exc:
            deps.logger.warning(
                "case import commit rejected: app=%s code=%s reason=%s",
                app_name,
                exc.detail.get("code"),
                exc,
            )
            raise deps.HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

        added = committed["added"]
        deps.logger.info(
            f"case import committed ({committed['mode']}): app={app_name} "
            f"parsed={committed['parsed_count']} "
            f"added={len(added)} ids={added}"
        )
        # 导入即起一个异步 AI job，对新追加的 draft 批量推荐前置/后置操作（落 suggested_*，待人确认）。
        # Registry commit 与 staged 消费已经完成；辅助 job 提交 fail-open，失败状态随成功响应返回。
        resp: dict = {"app": app_name, "added": added, "count": len(added)}
        # 导入批次回执（BL-038 阶段 B1）：Registry 已提交后写，fail-open；失败只在状态字段里说明。
        for key in ("batch_id", "batch_receipt_status", "batch_receipt_error"):
            if key in committed:
                resp[key] = committed[key]
        if added:
            try:
                resp["suggest_job"] = deps._submit_scene(
                    "suggest-ops", {"app": app_name, "case_ids": added}
                )
            except deps.HTTPException as exc:
                deps.logger.warning(
                    f"case import suggest job submission failed: app={app_name} "
                    f"ids={added} status={exc.status_code} detail={exc.detail!r}"
                )
                resp["suggest_job_status"] = "failed"
                resp["suggest_job_error"] = {
                    "status_code": exc.status_code,
                    "detail": exc.detail,
                }
            except Exception as exc:  # noqa: BLE001 - auxiliary job must not reverse a committed import
                deps.logger.exception(
                    f"case import suggest job submission failed: app={app_name} ids={added}"
                )
                resp["suggest_job_status"] = "failed"
                resp["suggest_job_error"] = {
                    "status_code": 500,
                    "detail": f"suggest-ops job submission failed: {type(exc).__name__}",
                }
            else:
                resp["suggest_job_status"] = "submitted"
        return resp

    deps.commit_case_import = commit_case_import

    def _case_import_batch_app(app_name: str) -> None:
        """未知 App 与非法 App 标识都按 404；只读接口不在状态根下建目录。"""
        from autophone.knowledge.case_import_batches import batch_directory

        try:
            batch_directory(app_name, state_root=deps.STATE_ROOT)
            deps.load_app_profile(app_name)
        except (FileNotFoundError, ValueError) as exc:
            raise deps.HTTPException(status_code=404, detail={
                "code": "app_not_found",
                "message": f"app not found: {app_name}",
            }) from exc

    @router.get("/api/apps/{app_name}/cases/import/batches")
    def case_import_batches(
        app_name: str, limit: int = deps.Query(default=10, ge=1, le=50),
    ) -> dict:
        """最近的导入批次回执（新→旧）。只读；损坏的回执单列 error，不静默丢弃。"""
        from autophone.knowledge.case_import_batches import list_batches

        _case_import_batch_app(app_name)
        batches = list_batches(app_name, limit=limit, state_root=deps.STATE_ROOT)
        return {"app": app_name, "count": len(batches), "batches": batches}

    deps.case_import_batches = case_import_batches

    @router.get("/api/apps/{app_name}/cases/import/batches/{batch_id}/progress")
    def case_import_batch_progress(app_name: str, batch_id: str) -> dict:
        """某导入批次的接入进度：逐 Case 复用知识视图的 Family 与准备卡行。

        只读：不执行试跑、不写 Case、不调用 AI。未知批次 404；回执损坏或 Case Registry
        不可解析时 422，detail 为 ``{code, message}``。
        """
        import yaml

        from autophone.knowledge.case_import_batches import (
            CaseImportBatchError,
            build_batch_progress,
        )

        _case_import_batch_app(app_name)
        try:
            return build_batch_progress(
                app_name, batch_id,
                state_root=deps.STATE_ROOT,
                run_root=deps.service.run_root,
            )
        except CaseImportBatchError as exc:
            deps.logger.warning(
                "case import batch receipt invalid: app=%s batch_id=%s reason=%s",
                app_name, batch_id, exc,
            )
            raise deps.HTTPException(status_code=422, detail={
                "code": "case_import_batch_invalid",
                "message": str(exc),
            }) from exc
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail={
                "code": "case_import_batch_not_found",
                "message": str(exc),
            }) from exc
        except (ValueError, TypeError, yaml.YAMLError) as exc:
            deps.logger.warning(
                "case import batch progress unavailable: app=%s batch_id=%s: %s",
                app_name, batch_id, exc, exc_info=True,
            )
            raise deps.HTTPException(status_code=422, detail={
                "code": "case_import_progress_unavailable",
                "message": f"{type(exc).__name__}: {exc}",
            }) from exc

    deps.case_import_batch_progress = case_import_batch_progress

    @router.post("/api/apps/{app_name}/cases/{case_id}/suggest-ops")
    def suggest_case_ops(app_name: str, case_id: str, payload: dict | None = None) -> dict:
        """对单条用例（重新）AI 推荐前置/后置操作，写回 suggested_setup/teardown（异步 job）。

        编辑器「让 AI 重新推荐」按钮用；body 可带 {provider, model}。fail-closed 由 suggest 层保证。
        """
        body = payload or {}
        provider = body.get("provider") or None
        model = body.get("model") or None

        return deps._submit_scene("suggest-ops", {
            "app": app_name, "case_id": case_id, "provider": provider, "model": model,
        }, provider=provider)

    deps.suggest_case_ops = suggest_case_ops

    @router.get("/api/apps/{app_name}/case-drafts")
    def case_drafts(app_name: str, required_passes: int = deps.Query(default=3, ge=1, le=20)) -> dict:
        """用例/recipe 草稿转正列表：生命周期 draft，或 active 但 recipe 仍为 draft。"""
        import yaml

        profile = deps.load_app_profile(app_name)
        rows: list[tuple[dict, dict | None]] = []
        try:
            cases = deps.CaseRecipeApplication(profile).rows()
        except (OSError, yaml.YAMLError, AttributeError, TypeError) as err:
            raise deps.HTTPException(status_code=422, detail={
                "code": "case_draft_registry_invalid",
                "message": str(err),
            }) from err
        if any(not isinstance(case, dict) for case in cases):
            raise deps.HTTPException(status_code=422, detail={
                "code": "case_draft_registry_invalid",
                "message": "Case Registry cases 必须是对象列表",
            })
        case_recipes = deps.CaseRecipeApplication(profile)
        for case in cases:
            lifecycle = str(case.get("status") or "")
            recipe = case_recipes.load(str(case.get("id") or ""), lifecycle=lifecycle)
            if lifecycle == "draft" or (lifecycle == "active" and (recipe or {}).get("source") == "draft"):
                rows.append((case, recipe))
        all_runs = deps.service.promotions.case_draft_runs(app=profile.app)
        by_case: dict[str, list[dict]] = {}
        for run in all_runs:
            by_case.setdefault(str(run.get("case_id") or ""), []).append(run)
        out = []
        for case, recipe in rows:
            cid = str(case.get("id") or "")
            case_runs = by_case.get(cid, [])
            try:
                gate = deps.service.promotions.case_promotion_decision(
                    case_id=cid, required_passes=required_passes, profile=profile,
                    runs=case_runs, case=case)
            except deps.ContractError as err:
                # 列表读取须保留其他可评估的草稿；当前 Case/draft identity 非法时，
                # 将该条目收口为不可转正的结构化 gap。写入口仍直接调用 service，继续
                # fail-closed 抛 ContractError。
                gate = deps.service.promotions.case_promotion_decision(
                    case_id=cid, required_passes=required_passes, profile=None,
                    runs=[], case=case)
                gap = "current_draft_recipe_identity_invalid"
                gate["ready"] = False
                base_gaps = [
                    item for item in gate.get("gaps", [])
                    if item != "current_draft_recipe_identity_missing"
                ]
                gate["gaps"] = list(dict.fromkeys([*base_gaps, gap]))
                gate["runs"] = case_runs
                gate["identity_rejected_runs"] = case_runs
                gate["current_draft_recipe_identity"] = None
                gate["evidence_runs"] = []
                gate["error"] = {"code": gap, "message": str(err)}
            lifecycle = str(case.get("status") or "")
            trial_selector = ({"ids": [cid], "statuses": ["draft"]}
                              if lifecycle == "draft"
                              else {"ids": [cid], "trial_draft_recipes": True})
            out.append({
                "id": cid,
                "status": lifecycle,
                "item": case.get("item") or "",
                "module": case.get("module") or "",
                "level": case.get("level") or "",
                "automation": case.get("automation") or "",
                "revision": case.get("revision"),
                "recipe": ({"status": recipe.get("status"), "source": recipe.get("source"),
                            "path": recipe.get("path")} if recipe else None),
                "trial_selector": trial_selector,
                "promotion_scope": gate.get("promotion_scope"),
                "target_status": gate.get("target_status"),
                "target_automation": gate.get("target_automation"),
                "gate": gate,
            })
        return {"app": app_name, "count": len(out), "case_drafts": out}

    deps.case_drafts = case_drafts

    @router.post("/api/apps/{app_name}/case-drafts/{case_id}/promote")
    def promote_case_draft_endpoint(app_name: str, case_id: str, payload: dict | None = None) -> dict:
        """用例/recipe 草稿转正：闸门 ready 才放行（fail-closed，与列表面同判据）。

        draft case → active+ui_ready；active case + draft recipe → 只发布 recipe，保留 automation lane。
        """
        body = payload or {}
        profile = deps.load_app_profile(app_name)
        try:
            result = deps.service.promotions.promote_case_draft(
                profile=profile,
                case_id=case_id,
                required_passes=int(body.get("required_passes") or 3),
            )
        except deps.ContractError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        deps.logger.info(
            f"case draft promoted: app={app_name} case={case_id} "
            f"recipe={result.get('recipe_path')} streak={(result.get('decision') or {}).get('streak')}"
        )
        return result

    deps.promote_case_draft_endpoint = promote_case_draft_endpoint

    @router.get("/api/apps/{app_name}/cases/{case_id}/telemetry")
    def case_telemetry(app_name: str, case_id: str) -> dict:
        try:
            deps.load_app_profile(app_name)
        except (FileNotFoundError, ValueError) as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            return deps.service.queries.latest_case_telemetry(app_name, case_id)
        except deps.TelemetryQueryValidationError as exc:
            raise deps.HTTPException(status_code=409, detail=str(exc)) from exc

    deps.case_telemetry = case_telemetry

    @router.get("/api/apps/{app_name}/cases/{case_id}/api-capture")
    def case_api_capture(app_name: str, case_id: str, allow_raw: bool = False) -> dict:
        """某用例最近一次回归 run 抓到的接口数据（按 case_id 归因）。供用例编辑页就地查看。

        复用 run 抓取面的脱敏/分组；返回里带 run_id，复制/重放仍走 /api/runs/{run_id}/api-replay。
        """
        return deps.service.queries.latest_case_api_capture(app_name, case_id, allow_raw=allow_raw)

    deps.case_api_capture = case_api_capture

    return router
