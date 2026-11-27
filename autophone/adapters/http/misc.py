from __future__ import annotations

import os

from autophone import paths as platform_paths
from autophone import version as version_mod
from autophone.application import case_exceptions as exc
from fastapi import APIRouter
from .deps import HttpDeps

#: 显式声明「这个进程是为自动化测试起的」的环境变量。只有取这些值才算开启。
TEST_MODE_ENV = "AUTOPHONE_TEST_MODE"
_TRUE_VALUES = {"1", "true", "yes", "on"}


def test_mode_enabled(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get(TEST_MODE_ENV, "")).strip().lower() in _TRUE_VALUES


def runtime_identity_payload(environ: dict[str, str] | None = None) -> dict:
    """Report which code, state and App asset roots this process is actually serving.

    浏览器回归复用一个已经起着的 dashboard 时，必须先确认那个进程跑的是本次要测的
    源码、用的是隔离状态目录、并且是按测试模式起的。版本号不足以判定：同一版本在两个
    checkout 上相同，未提交改动也只是一个后缀，所以这里同时给出 `code_root`、HEAD 和
    工作区脏标记。

    本接口只读，不含凭据。配置了 `AUTOPHONE_AUTH` 时它和其余 `/api/*` 一样走鉴权，
    只有 `/health` 对探针开放。
    """
    env = os.environ if environ is None else environ
    build = version_mod.build_identity()
    source = version_mod.source_identity()
    return {
        "service": "autophone-run",
        "test_mode": test_mode_enabled(env),
        "build": {
            "version": str(build["version"]),
            "commit": str(build["commit"]),
            "source": str(build.get("source") or "unknown"),
        },
        "source": source,
        "roots": {
            "code": str(platform_paths.CODE_ROOT),
            "state": str(platform_paths.STATE_ROOT),
            "asset": str(platform_paths.ASSET_ROOT),
            "artifact": str(platform_paths.ARTIFACT_ROOT),
            "package": str(platform_paths.PACKAGE_ROOT),
            # App 资产根（`apps/<app>/` 所在目录）。浏览器回归在写入用例库前核对它。
            "app_asset": str(platform_paths.APP_ASSET_ROOT),
        },
        "app_asset_root": {
            "path": str(platform_paths.APP_ASSET_ROOT),
            "overridden": platform_paths.app_asset_root_overridden(),
            "source": (
                platform_paths.APP_ASSET_ENV
                if platform_paths.app_asset_root_overridden()
                else "CODE_ROOT"
            ),
        },
        "control_plane": bool(str(env.get("AUTOPHONE_DATABASE_URL", "")).strip()),
    }

def install_helpers(deps: HttpDeps) -> None:

    def _exception_view(profile) -> exc.ExceptionView:
        return deps.exc.ExceptionView(
            profile,
            catalog=deps.service.run_catalog,
            negctl_root=deps.service.negctl_root,
        )

    deps._exception_view = _exception_view


def create_router(deps: HttpDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/runtime/identity")
    def runtime_identity() -> dict:
        """源码身份、状态根与测试模式的只读投影，见 `runtime_identity_payload`。"""
        return runtime_identity_payload()

    deps.runtime_identity = runtime_identity

    @router.get("/api/apps/{app_name}/exceptions")
    def list_exceptions(
        app_name: str,
        kind: str | None = None,
        include_muted: bool = True,
        experimental: bool = False,
    ) -> dict:
        """一个 app 当前派生的 exception（默认 triage/failure/drift 三类核心运营面，ADR-0003）。

        `kind` 过滤某一类；`include_muted=false` 只看尚无人表态（disposition）的那些。
        `experimental=true`（或直接 `kind=twin|extraction`）才并入实验性诊断类。
        `needs_attention` = 未被 disposition 静默的条数（面板红点）。
        """
        profile = deps.load_app_profile(app_name)
        return deps._exception_view(profile).current(
            kind=kind,
            include_muted=include_muted,
            experimental=experimental,
        )

    deps.list_exceptions = list_exceptions

    @router.post("/api/apps/{app_name}/exceptions/disposition")
    def set_exception_disposition(app_name: str, payload: dict | None = None) -> dict:
        """对一条 derived exception 记下人的态度（acknowledged/snoozed/triaged/wontfix）。

        body: {case_id, kind, signature, stance, note?, until?, target?, by?}。fail-closed：
        stance/kind 非法 → 422；该 (case_id,kind,signature) **当前不再派生出 exception** → 409
        （不能给不存在的条件下 disposition）。写盘时顺手 prune 掉所有已失配的旧 stance（auto-clear）。
        """
        body = payload or {}
        profile = deps.load_app_profile(app_name)
        try:
            record = deps._exception_view(profile).set_disposition(
                case_id=str(body.get("case_id") or ""),
                kind=str(body.get("kind") or ""),
                signature=str(body.get("signature") or ""),
                stance=str(body.get("stance") or ""),
                note=body.get("note"),
                until=body.get("until"),
                target=body.get("target"),
                by=body.get("by"),
            )
        except ValueError as exc_err:
            msg = str(exc_err)
            status = 409 if isinstance(exc_err, deps.exc.ExceptionNotLive) else 422
            raise deps.HTTPException(status_code=status, detail=msg) from exc_err
        deps.logger.info(
            f"exception disposition set: app={app_name} key={record['case_id']}|"
            f"{record['kind']}|{record['signature']} stance={record['stance']}"
        )
        return record

    deps.set_exception_disposition = set_exception_disposition

    @router.post("/api/apps/{app_name}/exceptions/disposition/clear")
    def clear_exception_disposition(app_name: str, payload: dict | None = None) -> dict:
        """撤掉一条 disposition（让该 exception 重新回到「待表态」）。"""
        body = payload or {}
        profile = deps.load_app_profile(app_name)
        existed = deps._exception_view(profile).clear_disposition(
            case_id=str(body.get("case_id") or ""),
            kind=str(body.get("kind") or ""),
            signature=str(body.get("signature") or ""),
        )
        return {"cleared": existed}

    deps.clear_exception_disposition = clear_exception_disposition

    @router.get("/api/apps/{app_name}/operations")
    def operations(app_name: str, include_deprecated: bool = True) -> dict:
        """操作库（RPA recipe 注册表）。默认带上 deprecated，让前端按 status 客户端过滤、计数准。

        操作无 expects（断言属于用例）；运行一条操作走 ``POST /runs`` 的 type=operation。
        """
        profile = deps.load_app_profile(app_name)
        from autophone.application import OperationApplication

        return OperationApplication(
            profile,
            promotion_decision=deps.service.promotions.operation_promotion_decision,
        ).list(include_deprecated=include_deprecated)

    deps.operations = operations

    @router.get("/api/apps/{app_name}/operations/{op_id}")
    def operation_detail(app_name: str, op_id: str) -> dict:
        """Return one operation with full editable content for human review."""
        from autophone.application import OperationApplication, OperationApplicationError

        profile = deps.load_app_profile(app_name)
        try:
            return OperationApplication(profile).get(op_id)
        except OperationApplicationError as exc:
            raise deps.HTTPException(status_code=404, detail=exc.detail) from exc

    deps.operation_detail = operation_detail

    @router.patch("/api/apps/{app_name}/operations/{op_id}/steps")
    def patch_operation_steps(app_name: str, op_id: str, payload: dict | None = None) -> dict:
        """Save reviewed operation typed-IR steps.

        The operation remains draft/active as-is; changing steps is a content change,
        so operations_store increments revision.
        """
        from autophone.application import OperationApplication, OperationApplicationError

        profile = deps.load_app_profile(app_name)
        try:
            return OperationApplication(profile).update_steps(op_id, (payload or {}).get("steps"))
        except OperationApplicationError as exc:
            status = 404 if exc.code == "not_found" else 422
            raise deps.HTTPException(status_code=status, detail=exc.detail) from exc

    deps.patch_operation_steps = patch_operation_steps

    @router.post("/api/apps/{app_name}/operations/{op_id}/activate")
    def activate_operation(app_name: str, op_id: str, force: bool = False) -> dict:
        """把一条操作 draft→active（纯 status 变更不动 revision；已 active 幂等）。

        **转正恒走冻结门**（record-replay-design §五 / platform-ai 不变量）：默认调
        `operation_promotion_decision` 兜全门——**人审 approved + 连续 N 次 passed 回放 +
        区分力闸**（ADR-0001，对该操作声明了 witness 时按当前 build 校验有牙 negative-control
        证据）——任一不满足 fail-closed 抛 409，写边界不再只查区分力。这堵的就是「无 witness、
        无 review、零 passed 的 draft 直接激活」那条旁路。

        `force=true` 是**显式人工 override 逃生口**：绕过证据门直接激活，留 WARNING 审计痕迹
        （部署/补救场景人手强转）；active 名称唯一性仍在写锁内强制校验。profile 设 require_discrimination:false 只放宽区分力那一条，
        人审 + 连续 passed 仍由门把守。
        """
        profile = deps.load_app_profile(app_name)
        from autophone.application import OperationApplication, OperationApplicationError

        try:
            return OperationApplication(
                profile,
                promotion_decision=deps.service.promotions.operation_promotion_decision,
            ).activate(op_id, force=force)
        except OperationApplicationError as exc:
            status = 404 if exc.code == "not_found" else 409 if exc.code.startswith("promotion_") else 422
            raise deps.HTTPException(status_code=status, detail=exc.detail) from exc

    deps.activate_operation = activate_operation

    @router.post("/api/apps/{app_name}/operations/{op_id}/deprecate")
    def deprecate_operation(
        app_name: str, op_id: str, payload: dict | None = None, force: bool = False,
    ) -> dict:
        """软删一条操作：status=deprecated（+ 可选 reason），不动 revision。

        **降级闸**（BL-038）：该操作为 active，且某条 active Case 的 live Recipe 或
        setup/teardown/post_case_cleanup 按 name 引用它、废弃后不再解析到 active 条目时，返回
        409，detail 为 ``{code: operation_referenced, message, op_id, name, references}``，
        references 逐条给出 App/Case/位置。引用扫描失败时 409 ``reference_scan_failed``。
        ``force=true`` 是显式逃生口：放行、写 WARNING 审计日志并把摘要写入 ``deprecate_reason``；
        响应带 ``forced``、``affected_references``（扫描失败时为 null）与 ``scan_error``。
        """
        profile = deps.load_app_profile(app_name)
        reason = (payload or {}).get("reason") or None
        from autophone.application import OperationApplication, OperationApplicationError

        try:
            result = OperationApplication(profile).deprecate(op_id, reason=reason, force=force)
        except OperationApplicationError as exc:
            status = (
                404 if exc.code == "not_found"
                else 409 if exc.code in {"operation_referenced", "reference_scan_failed"}
                else 422
            )
            detail = (
                {"code": exc.code, "message": exc.detail, **exc.context}
                if status == 409 else exc.detail
            )
            raise deps.HTTPException(status_code=status, detail=detail) from exc
        deps.logger.info(
            f"operation deprecated: app={app_name} op={op_id} reason={reason!r} "
            f"force={force} affected={len(result.get('affected_references') or [])}"
        )
        return result

    deps.deprecate_operation = deprecate_operation

    @router.get("/api/apps/{app_name}/screens/{build}")
    def get_screen_asset(app_name: str, build: str) -> dict:
        """Load one build's screen skeleton asset."""
        profile = deps.load_app_profile(app_name)
        path = profile.screens_path(build)
        if not path.exists():
            raise deps.HTTPException(status_code=404, detail=f"screen asset not found: {path}")
        try:
            doc = deps.screen_assets.load_screen_asset(path)
        except deps.screen_assets.ScreenAssetError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        return {"app": app_name, "build": build, "path": str(path), **doc}

    deps.get_screen_asset = get_screen_asset

    @router.post("/api/apps/{app_name}/screens/{build}/draft")
    def draft_screen_asset(app_name: str, build: str, payload: dict | None = None) -> dict:
        """Generate a draft screen asset from OCR samples."""
        deps.assert_assets_writable("draft screen asset")
        profile = deps.load_app_profile(app_name)
        body = payload or {}
        try:
            samples = body.get("samples") or []
            if body.get("classify_ai"):
                samples = deps.screen_draft.classify_ocr_samples_with_ai(
                    samples,
                    provider=body.get("provider") or None,
                    model=body.get("model") or None,
                    timeout_s=float(body.get("timeout_s") or 120.0),
                )
            doc = deps.screen_draft.draft_screens_from_ocr_samples(
                samples,
                app=app_name,
                build=build,
                min_samples=int(body.get("min_samples") or 1),
                must_threshold=float(body.get("must_threshold") or 1.0),
                any_threshold=float(body.get("any_threshold") or 0.5),
                forbid_threshold=float(body.get("forbid_threshold") or 1.0),
                any_min_hits=int(body.get("any_min_hits") or 1),
            )
            path = deps.screen_draft.write_screen_draft(
                profile.screens_path(build),
                doc,
                overwrite=bool(body.get("overwrite")),
            )
        except deps.screen_draft.ScreenDraftError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        except deps.screen_assets.ScreenAssetError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, "path": str(path), "asset": doc}

    deps.draft_screen_asset = draft_screen_asset

    @router.post("/api/apps/{app_name}/screens/{build}/{screen_id}/promote")
    def promote_screen_asset(app_name: str, build: str, screen_id: str, payload: dict | None = None) -> dict:
        """Promote a draft screen to active after telemetry + human review evidence."""
        profile = deps.load_app_profile(app_name)
        body = payload or {}
        try:
            out = deps.screen_assets.promote_screen_if_ready(
                profile.screens_path(build),
                screen_id,
                body.get("telemetry") or [],
                review=body.get("review") or {},
                required_matches=int(body.get("required_matches") or 3),
            )
        except KeyError as exc:
            raise deps.HTTPException(status_code=404, detail=f"screen not found: {screen_id}") from exc
        except deps.screen_assets.ScreenAssetError as exc:
            raise deps.HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "screen": out}

    deps.promote_screen_asset = promote_screen_asset

    @router.get("/api/apps/{app_name}/screens/{build}/telemetry")
    def screen_telemetry_summary(app_name: str, build: str) -> dict:
        """Summarize screen checkpoint telemetry from operation runs."""
        deps.load_app_profile(app_name)
        operation_results: list[dict] = []
        for path in deps.service.run_root.glob("*/*/operation-result.json"):
            try:
                payload = deps.json.loads(path.read_text(encoding="utf-8"))
            except (OSError, deps.json.JSONDecodeError):
                continue
            if payload.get("app") != app_name:
                continue
            # operation-result older/newer payloads may or may not include build;
            # when present, respect it. Otherwise keep the row so early S5 telemetry
            # still works on current run artifacts.
            if payload.get("build") not in (None, build):
                continue
            operation_results.append(payload)
        rows = deps.screen_assets.checkpoint_rows_from_operation_results(operation_results)
        return {
            "app": app_name,
            "build": build,
            "runs": len(operation_results),
            "telemetry": deps.screen_assets.summarize_checkpoint_telemetry(rows),
        }

    deps.screen_telemetry_summary = screen_telemetry_summary

    @router.get("/api/apps/{app}/trend")
    def app_trend(app: str, limit: int = 20) -> dict:
        """跨 run 执行耗时趋势（trace P3）：某 app 最近 N 份回归 run 的墙钟/慢用例 p50·p95 漂移、
        变慢回归用例、动词耗时漂移。把「这次比上次慢在哪」量化。"""
        return deps.service.queries.app_trend(app, limit=max(1, min(limit, 100)))

    deps.app_trend = app_trend

    @router.post("/api/apps/{app_name}/telemetry/query")
    def app_telemetry_query(app_name: str, payload: dict | None = None) -> dict:
        """人工行为查询；服务端收口字段/操作符并只返回标准化脱敏结果。"""

        try:
            profile = deps.load_app_profile(app_name)
        except (FileNotFoundError, ValueError) as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            return deps.query_telemetry(
                app=app_name,
                profile_raw=profile.raw,
                request=payload or {},
            )
        except (deps.TelemetryQueryValidationError, deps.DataTraceQueryError) as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        except deps.DataTraceConfigurationError as exc:
            raise deps.HTTPException(status_code=503, detail=str(exc)) from exc
        except (deps.DataTraceAdapterError, deps.DataTraceBackendError, deps.DataTraceContractError) as exc:
            raise deps.HTTPException(status_code=502, detail=str(exc)) from exc

    deps.app_telemetry_query = app_telemetry_query

    return router
