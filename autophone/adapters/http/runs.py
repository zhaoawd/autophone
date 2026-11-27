from __future__ import annotations

from fastapi import Request
from fastapi import APIRouter

from autophone.observation.run_log_feed import (
    STATUS_CURSOR_INVALID,
    RunLogFeed,
    RunLogFeedError,
)

from autophone.execution.dispatch import RunDispatchError

from .deps import HttpDeps

# 一次 SSE 心跳最多带多少条日志。上限同时是「日志追加不拖垮整条 detail 流」的闸：
# 超出的部分留给下一拍或历史接口（`?before=`），不在一条事件里堆进浏览器。
LOG_APPEND_LIMIT = 200


def _int_or_none(value: str | None) -> int | None:
    """`Last-Event-ID` 是任意字符串，解析不出非负整数就当没带（不报错、不中断流）。"""
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


# Assignment 非终态 → 运行视图的展示状态。`cancel_requested` 对应页面已有的「中止中」。
_ASSIGNMENT_DISPLAY_STATUS = {"cancel_requested": "aborting"}


def _assignment_active(row: dict, assignment: dict) -> dict:
    """控制面 Run 行与 Assignment 投影成 `/api/runs/{id}` 的 `active`（BL-227）。"""
    raw = str(assignment["status"])
    return {
        **(row.get("run_spec") or {}),
        "status": _ASSIGNMENT_DISPLAY_STATUS.get(raw, raw),
        "assignment_status": raw,
        "assignment_id": row["assignment_id"],
        "source": "control_plane",
    }


def _pinned_target_rig(deps: HttpDeps, control, device: str, explicit: str | None) -> str:
    """按 Run Spec 的 `device` 解析唯一 Rig，作为 Assignment 的 `target_rig_id`（BL-215）。

    与 CLI、计划任务、包门禁同一口径：设备名必须对应唯一 Rig，否则 Run Result 记的设备与
    实际执行的 Rig 可能不一致。解析不出时 422 `device_rig_unresolved`；请求体显式给了
    `target_rig_id` 但与解析结果不同，同样 422 `target_rig_device_mismatch`。两种情况都在
    提交 Assignment 之前拒绝。判定由 `pin_target_rig` 实现，包安装路由（BL-218）复用同一函数。
    """
    from autophone.application.control_plane import TargetRigPinRefused, pin_target_rig

    try:
        return pin_target_rig(control, device, explicit)
    except TargetRigPinRefused as exc:
        raise deps.HTTPException(status_code=422, detail=exc.to_detail()) from exc


def install_helpers(deps: HttpDeps) -> None:

    def _run_log_feed() -> RunLogFeed:
        """按 RunService 当前的 run 根目录建读取面（BL-075 PR 2）。

        每次调用新建：`run_root` 在测试与多实例里可变，缓存住会让读取面钉在旧根上。
        构造本身只存两个引用，没有 IO。
        """
        return RunLogFeed(
            deps.service.run_root,
            resolve_run_dir=deps.service.queries.find_run_dir,
        )

    deps._run_log_feed = _run_log_feed

    def _run_event_terminal(detail: dict) -> bool:
        result = detail.get("result") or {}
        active = detail.get("active") or {}
        status = result.get("status")
        if status is None:
            status = active.get("status")
        return str(status or "") in {"passed", "failed", "error", "done", "blocked", "aborted"}

    deps._run_event_terminal = _run_event_terminal


def create_router(deps: HttpDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/apps/{app_name}/operations/{op_id}/promotion")
    def operation_promotion_decision(
        app_name: str,
        op_id: str,
        required_passes: int = deps.Query(default=3, ge=1, le=20),
    ) -> dict:
        """Operation draft promotion gate.  Reads operation runs, not verification runs."""
        import yaml

        profile = deps.load_app_profile(app_name)
        path = profile.operations_path()
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {} if path.exists() else {}
        op = next((o for o in (doc.get("operations") or []) if str(o.get("id")) == op_id), None)
        if op is None:
            raise deps.HTTPException(status_code=404, detail=f"operation not found: {op_id}")
        return deps.service.promotions.operation_promotion_decision(
            draft_id=op_id, required_passes=required_passes, profile=profile, op_def=op
        )

    deps.operation_promotion_decision = operation_promotion_decision

    @router.post("/runs")
    def create_run(spec: dict) -> dict:
        from autophone.rig.source_identity import SourceProcessStale, assert_process_source_current

        try:
            assert_process_source_current()
        except SourceProcessStale as exc:
            raise deps.HTTPException(
                status_code=409, detail={"code": exc.code, "message": str(exc)},
            ) from exc
        # 后台执行：HTTP 不阻塞到 run 跑完，立即返回 status=queued 的 handle，前端拿 run_id
        # 即刻切实时屏幕流（detail()/history() 据 active 注册表反映在跑状态）。合约校验仍同步抛。
        body = dict(spec)
        telemetry_identity = body.pop("telemetry_identity", None)
        # 这是 Web 看板的主运行入口——来源是已知的，补上 run-spec 的 requested_by，
        # 设备队列才说得出「这条是谁提的」（CLI 走 --requested-by，rerun 已显式传 web）。
        # 调用方显式给了就不覆盖。
        body.setdefault("requested_by", "web")
        # 控制面已配置但连不上 → 503（BL-026）。这里若把异常当成「未配置」，下面的
        # `service.submit()` 会绕过 Scheduler 直接抢设备执行一条没人登记的 run。
        control = deps._configured_control_or_http_error()
        if control is not None:
            assignment_options = {
                "priority": str(body.pop("priority", "normal")),
                "target_rig_id": body.pop("target_rig_id", None),
                "required_capabilities": body.pop("required_capabilities", []),
                "desired_state": body.pop("desired_state", {}),
                "reset_policy": body.pop("reset_policy", {}),
            }
            try:
                deps.validate_or_raise("run-spec.v1.json", body)
                # 黄金路径标记在派单前核对（路径已声明、Case 属于该路径、摘要与 selector 一致）。
                from autophone.assets.golden_paths import validate_golden_path_request

                validate_golden_path_request(body)
                from autophone.knowledge.trial_confirmation import validate_trial_confirmation

                validate_trial_confirmation(body)
                assignment_options["target_rig_id"] = _pinned_target_rig(
                    deps, control, str(body.get("device") or ""), assignment_options["target_rig_id"],
                )
                assignment = control.submit_assignment(
                    {
                        "kind": "run",
                        "requires_rig": True,
                        "run_spec": body,
                        **assignment_options,
                    },
                    deps._current_idempotency_key() or f"run:{body.get('run_id')}",
                )
            except (deps.ContractError, deps.ControlStateError) as exc:
                to_detail = getattr(exc, "to_detail", None)
                if getattr(exc, "code", None) == "screen_quarantined" and callable(to_detail):
                    # 目标 Rig 上报了 ScreenLock 隔离标记（BL-224）：未创建 Assignment，409 带解除命令。
                    detail = dict(to_detail())
                    detail["message"] = (
                        f"设备 {body.get('device')!r} 对应的 Rig {detail.get('rig_id')!r} 上报 ScreenLock 隔离标记，"
                        f"未提交 Run；请在该 Rig 宿主核对后执行解除命令（先不带 --confirm-quiesced 预检）："
                        f"{detail.get('release_command')}"
                    )
                    raise deps.HTTPException(status_code=409, detail=detail) from exc
                raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
            return {
                "run_id": body.get("run_id"),
                "assignment_id": assignment["assignment_id"],
                "status": assignment["status"],
            }
        held = deps.service.lock.human_holder(device=str(body.get("device") or ""))
        if held is not None:
            raise deps.HTTPException(
                status_code=409,
                detail=f"设备正被 {held.get('owner_kind')} {held.get('owner_id')} 占用（有人正在操作设备），"
                       f"请先结束后再提交 run",
            )
        try:
            if telemetry_identity is None:
                handle = deps.service.submit(body, background=True)
            else:
                handle = deps.service.submit(
                    body,
                    background=True,
                    telemetry_identity=telemetry_identity,
                )
        except deps.ContractError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        return handle.summary()

    deps.create_run = create_run

    @router.post("/api/runs/{run_id}/rerun")
    def rerun_run(run_id: str, payload: dict | None = None) -> dict:
        body = payload or {}
        try:
            detail = deps.service.queries.detail(run_id)
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        result = detail.get("result") or {}
        active = detail.get("active") or {}
        device = str(result.get("device") or active.get("device") or body.get("device") or "")
        if device:
            held = deps.service.lock.human_holder(device=device)
            if held is not None:
                raise deps.HTTPException(
                    status_code=409,
                    detail=f"设备正被 {held.get('owner_kind')} {held.get('owner_id')} 占用（有人正在操作设备），"
                           f"请先结束后再重跑",
                )
        try:
            handle = deps.service.rerun(
                run_id,
                requested_by=str(body.get("requested_by") or "web"),
                background=True,
            )
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except RunDispatchError as exc:
            if exc.code not in {"host_process_stale", "screen_quarantined"}:
                raise
            raise deps.HTTPException(
                status_code=409, detail={"code": exc.code, "message": str(exc), **exc.details},
            ) from exc
        except (deps.ContractError, deps.ControlStateError) as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        return handle.summary()

    deps.rerun_run = rerun_run

    @router.get("/api/runs/history")
    def run_history(
        limit: int = deps.Query(default=20, ge=1, le=100),
        app: str | None = deps.Query(default=None),
        status: str | None = deps.Query(default=None, description="精确态；'failed' 特例含 error/blocked"),
        type: str | None = deps.Query(default=None),
        before: int | None = deps.Query(default=None, description="游标：只取 result_mtime_ns 小于此值（加载更多）"),
    ) -> dict:
        return {"runs": deps.service.queries.history(limit=limit, app=app, status=status, type=type, before=before)}

    deps.run_history = run_history

    @router.get("/api/runs/index/status")
    def run_index_status() -> dict:
        from autophone.application import run_index

        try:
            return run_index.status(deps.service.run_root)
        except Exception as exc:  # noqa: BLE001
            raise deps.HTTPException(status_code=503, detail=f"run index unavailable: {exc}") from exc

    deps.run_index_status = run_index_status

    @router.get("/api/schedules")
    def schedules() -> dict:
        try:
            return {"schedules": deps.schedule_runner.list()}
        except deps.scheduler_mod.SchedulerError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc

    deps.schedules = schedules

    @router.patch("/api/schedules/{schedule_id}")
    def update_schedule(schedule_id: str, payload: dict | None = None) -> dict:
        payload = payload or {}
        if set(payload) - {"enabled"}:
            raise deps.HTTPException(status_code=422, detail="only enabled can be updated")
        if "enabled" not in payload or not isinstance(payload["enabled"], bool):
            raise deps.HTTPException(status_code=422, detail="enabled must be a boolean")
        try:
            return deps.schedule_runner.update_enabled(schedule_id, bool(payload["enabled"]))
        except deps.scheduler_mod.SchedulerError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc

    deps.update_schedule = update_schedule

    @router.post("/api/schedules/{schedule_id}/fire")
    def fire_schedule(schedule_id: str) -> dict:
        try:
            return deps.schedule_runner.fire(schedule_id, background=True)
        except RunDispatchError as exc:
            if exc.code not in {"host_process_stale", "screen_quarantined"}:
                raise
            raise deps.HTTPException(
                status_code=409, detail={"code": exc.code, "message": str(exc), **exc.details},
            ) from exc
        except (deps.scheduler_mod.SchedulerError, deps.ControlStateError) as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc

    deps.fire_schedule = fire_schedule

    @router.get("/api/verifications")
    def verifications(draft_id: str | None = None) -> dict:
        return {"runs": deps.service.queries.verification_runs(draft_id=draft_id)}

    deps.verifications = verifications

    @router.get("/api/promotion/{draft_id}")
    def promotion_decision(
        draft_id: str,
        app: str | None = deps.Query(default=None),
        required_passes: int = deps.Query(default=3, ge=1, le=20),
    ) -> dict:
        """验证单证据决策。传 ``app`` 时纳入区分力；结果不直接授权 Active Case。

        验证单先收录为 Case Draft，Active 转正仍由 Case Draft 专用门决定。
        """
        profile = deps.load_app_profile(app) if app else None
        return deps.service.promotions.promotion_decision(
            draft_id=draft_id, required_passes=required_passes, profile=profile
        )

    deps.promotion_decision = promotion_decision

    @router.post("/api/runs/{run_id}/case-draft-review")
    def review_case_draft_run(run_id: str, payload: dict | None = None) -> dict:
        """给草稿试跑 run 里的某条用例记人审结论（转正闸的 human_review 证据）。"""
        body = payload or {}
        case_id = str(body.get("case_id") or "")
        if not case_id:
            raise deps.HTTPException(status_code=422, detail="case_id 必填")
        try:
            return deps.service.promotions.review_case_draft(
                run_id,
                case_id=case_id,
                status=str(body.get("status") or ""),
                reviewer=str(body.get("reviewer") or "web"),
                note=str(body.get("note") or ""),
            )
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except deps.ContractError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc

    deps.review_case_draft_run = review_case_draft_run

    @router.post("/api/failure-sample")
    def create_failure_sample_run(payload: dict | None = None) -> dict:
        body = payload or {}
        try:
            spec = deps.failure_sample_spec(
                run_id=body.get("run_id"),
                app=body.get("app"),
                device=body.get("device") or deps.DEFAULT_DEVICE,
                requested_by=body.get("requested_by") or "web",
            )
            handle = deps.create_failure_sample(deps.service, spec)
        except deps.ContractError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        return handle.summary()

    deps.create_failure_sample_run = create_failure_sample_run

    @router.get("/api/runs/{run_id}")
    def run_detail(run_id: str) -> dict:
        control = deps.configured_control_plane() if deps.configured_control_plane else None
        try:
            detail = deps.service.queries.detail(run_id)
        except FileNotFoundError as exc:
            if control is None:
                raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
            try:
                row = control.get_run(run_id)
                assignment = control.get_assignment(row["assignment_id"])
            except deps.ControlStateError as missing:
                raise deps.HTTPException(status_code=404, detail=str(missing)) from missing
            from autophone.execution.dispatch import _run_result, _TERMINAL_ASSIGNMENT_STATUSES
            spec = row.get("run_spec") or {}
            terminal = assignment["status"] in _TERMINAL_ASSIGNMENT_STATUSES
            detail = {
                "run_id": run_id, "run_dir": None, "artifacts": {},
                "active": None if terminal else _assignment_active(row, assignment),
                "result": _run_result(assignment, spec) if terminal else None,
            }
        else:
            if control is not None and detail.get("result") is None and not detail.get("active"):
                # 控制面模式下 Run 在 Rig Agent 进程执行：Agent 写的 run 目录本机可见，但看板
                # 进程没有 active 登记；结果落盘前按 Assignment 状态投影，避免显示「排队」（BL-227）。
                from autophone.execution.dispatch import _TERMINAL_ASSIGNMENT_STATUSES
                try:
                    row = control.get_run(run_id)
                    assignment = control.get_assignment(row["assignment_id"])
                except deps.ControlStateError:
                    pass
                else:
                    if assignment["status"] not in _TERMINAL_ASSIGNMENT_STATUSES:
                        detail["active"] = _assignment_active(row, assignment)
        if control is not None:
            detail['recoveries'] = control.get_run_recoveries(run_id)
            if detail.get("run_dir"):
                from autophone.application.run_index import ingest_recoveries
                ingest_recoveries(run_id, detail['recoveries'], run_root=deps.service.run_root)
        return detail

    deps.run_detail = run_detail

    @router.get("/api/runs/{run_id}/logs")
    def run_logs(
        run_id: str,
        cursor: int | None = deps.Query(default=None, ge=0),
        before: int | None = deps.Query(default=None, ge=0),
        limit: int = deps.Query(default=500, ge=1, le=2000),
    ) -> dict:
        """Run 日志的游标分页读取（设计文档 §4.2 的历史接口）。

        `cursor` 向后增量读，`before` 向前加载更早的日志；两者都按字节游标定位，
        `before` 优先。游标失效（越界 / 文件被截断）返回 409 +
        `code=cursor_invalid`，调用方据此重取一次快照，服务端不静默从头读。
        """
        feed = deps._run_log_feed()
        try:
            page = (
                feed.read_before(run_id, cursor=before, limit=limit)
                if before is not None
                else feed.read(run_id, cursor=cursor, limit=limit)
            )
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except RunLogFeedError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc
        if page.status == STATUS_CURSOR_INVALID:
            raise deps.HTTPException(
                status_code=409,
                detail={
                    "code": STATUS_CURSOR_INVALID,
                    "size": page.size,
                    "source": page.source,
                    "message": "游标已失效（越界或文件被截断），请重取快照",
                },
            )
        return page.as_dict()

    deps.run_logs = run_logs

    @router.get("/api/runs/{run_id}/events")
    async def run_events(
        run_id: str,
        request: Request,
        interval_s: float = deps.Query(default=1.0, ge=0.5, le=10.0),
        cursor: int | None = deps.Query(default=None, ge=0),
        logs: bool = deps.Query(default=True),
    ):
        """SSE stream for dashboard run detail updates.

        The dashboard still renders from the same ``service.queries.detail()`` payload as
        ``GET /api/runs/{id}``; this endpoint only changes the transport. It emits
        when the snapshot changes, sends heartbeat comments while unchanged, and
        closes after a terminal run state so browsers do not keep idle streams.

        同一条连接上分事件类型（设计文档 §4.2）：`detail` 是原有全量快照，
        `log_append` 只带游标之后的日志与 `next_cursor`，页面据此只更新日志子树。
        **不新增第二条 SSE**——实时页已经占着一条 EventSource 与一条画面长连接。
        起始游标取查询参数 `cursor` 与 `Last-Event-ID` 里较大的合法值：重连时浏览器
        自动带 `Last-Event-ID`，而刷新后的页面带查询参数。
        """
        try:
            run_detail(run_id)
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc

        def _resume_cursor() -> int | None:
            header = _int_or_none(getattr(request, "headers", {}).get("last-event-id"))
            candidates = [value for value in (_int_or_none(cursor), header) if value is not None]
            return max(candidates) if candidates else None

        def _feed():
            """取日志读取面。取不到就不推日志——它是观测面，不该连累 detail 流。"""
            if logs is False:
                return None
            try:
                return deps._run_log_feed()
            except Exception:  # 运行服务未提供 run 根目录（直调 handler 的契约测试等）
                return None

        async def events():
            last_sig = ""
            last_heartbeat = deps.time.monotonic()
            feed = _feed()
            log_cursor = _resume_cursor()
            # 游标失效后停止推送日志：客户端收到 error 会重取快照并带新游标重连。
            # 继续从 0 重推等于把整份日志再发一遍，且客户端无法区分是新数据还是重放。
            log_stalled = False

            def read_logs():
                nonlocal log_cursor, log_stalled
                if feed is None or log_stalled:
                    return None
                try:
                    page = feed.read(run_id, cursor=log_cursor, limit=LOG_APPEND_LIMIT)
                except FileNotFoundError:
                    return None  # Assignment 排队时尚无 Run 目录，下一次继续读取。
                except (RunLogFeedError, OSError):
                    log_stalled = True
                    return None
                if page.status == STATUS_CURSOR_INVALID:
                    log_stalled = True
                    return page
                log_cursor = page.next_cursor
                return page

            def render_logs(page) -> str:
                if page is None:
                    return ""
                if page.status == STATUS_CURSOR_INVALID:
                    body = deps.json.dumps(
                        {"code": STATUS_CURSOR_INVALID, "size": page.size, "source": page.source},
                        ensure_ascii=False,
                    )
                    return f"event: error\ndata: {body}\n\n"
                if not page.events:
                    return ""
                body = deps.json.dumps(page.as_dict(), ensure_ascii=False, default=str)
                # SSE 事件 ID 用 next_cursor：浏览器重连时把它放进 Last-Event-ID，
                # 服务端据此接着上次的位置推，不重不漏。
                return f"id: {page.next_cursor}\nevent: log_append\ndata: {body}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    detail = run_detail(run_id)
                except (FileNotFoundError, deps.HTTPException):
                    yield "event: error\ndata: {\"detail\":\"run not found\"}\n\n"
                    break
                payload = deps.json.dumps(detail, ensure_ascii=False, default=str)
                sig = deps.json.dumps(detail, sort_keys=True, default=str)
                now = deps.time.monotonic()
                changed = sig != last_sig
                terminal = False
                if changed:
                    yield f"event: detail\ndata: {payload}\n\n"
                    last_sig = sig
                    last_heartbeat = now
                    terminal = bool(deps._run_event_terminal(detail))
                # 终态也先把剩下的日志推完再断开，否则最后几行只能靠刷新才看得到。
                chunk = render_logs(read_logs())
                if chunk:
                    yield chunk
                    last_heartbeat = now
                if terminal:
                    break
                if not changed and not chunk and now - last_heartbeat >= 15:
                    yield ": heartbeat\n\n"
                    last_heartbeat = now
                await deps.asyncio.sleep(interval_s)

        return deps.StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    deps.run_events = run_events

    @router.get("/api/runs/{run_id}/trace")
    def run_trace(run_id: str) -> dict:
        """执行 Trace（docs/run-execution-trace.md）：把 run 产物拼成瀑布流 span 树 + 提速分析聚合。
        实时页「执行时序」面板的数据源。终态读 result.json，run 仍在跑时按 progress 降级（partial）。"""
        from autophone.execution.run_trace import build_trace
        try:
            return build_trace(deps.service.queries.detail(run_id))
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc

    deps.run_trace = run_trace

    @router.get("/api/runs/{run_id}/api-capture")
    def run_api_capture(run_id: str, allow_raw: bool = False) -> dict:
        try:
            return deps.service.queries.api_capture(run_id, allow_raw=allow_raw)
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc

    deps.run_api_capture = run_api_capture

    @router.get("/api/runs/{run_id}/telemetry")
    def run_telemetry(run_id: str) -> dict:
        try:
            return deps.service.queries.telemetry(run_id)
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except deps.TelemetryQueryValidationError as exc:
            raise deps.HTTPException(status_code=409, detail=str(exc)) from exc

    deps.run_telemetry = run_telemetry

    @router.post("/api/runs/{run_id}/telemetry/refresh")
    def refresh_run_telemetry(run_id: str, payload: dict | None = None) -> dict:
        body = payload or {}
        if set(body) - {"actor"}:
            raise deps.HTTPException(status_code=422, detail="only actor is accepted")
        try:
            return deps.service.queries.refresh_telemetry(
                run_id,
                actor=str(body.get("actor") or "web"),
            )
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except deps.ContractError as exc:
            raise deps.HTTPException(status_code=409, detail=str(exc)) from exc
        except deps.TelemetryQueryValidationError as exc:
            raise deps.HTTPException(status_code=409, detail=str(exc)) from exc

    deps.refresh_run_telemetry = refresh_run_telemetry

    @router.post("/api/runs/{run_id}/api-replay")
    def run_api_replay(run_id: str, payload: dict | None = None) -> dict:
        """重放抓取面里第 idx 条 API 调用（host 侧重发原始请求）。外发动作，前端显式触发 + 二次确认。"""
        body = payload or {}
        try:
            idx = int(body.get("idx"))
        except (TypeError, ValueError):
            raise deps.HTTPException(status_code=422, detail="idx 必填且须为整数") from None
        allow_raw = bool(body.get("allow_raw"))
        try:
            return deps.service.queries.replay_api_call(run_id, idx, allow_raw=allow_raw)
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except IndexError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc

    deps.run_api_replay = run_api_replay

    @router.post("/api/runs/{run_id}/manual/resume")
    def run_manual_resume(run_id: str, payload: dict | None = None) -> dict:
        body = payload or {}
        try:
            return deps.service.resume_manual(
                run_id,
                actor=str(body.get("actor") or "web"),
                note=str(body.get("note") or ""),
            )
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except deps.ManualCheckpointError as exc:
            raise deps.HTTPException(status_code=409, detail=str(exc)) from exc

    deps.run_manual_resume = run_manual_resume

    @router.post("/api/runs/{run_id}/abort")
    def run_abort(run_id: str, payload: dict | None = None) -> dict:
        """中止一条在跑/排队的 run（协作式）。已收口的 run → 409。"""
        body = payload or {}
        control = deps._configured_control_or_http_error()
        if control is not None:
            try:
                spec_path = deps.service.queries.find_run_dir(run_id) / "run-spec.json"
                spec = deps.json.loads(spec_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                spec = None
            if spec is not None and not deps.service.requires_device(spec):
                control = None  # 持久规格证明为本地 Host-only Run，才允许本地中止。
        if control is not None:
            try:
                row = control.get_run(run_id)
                assignment = control.cancel_assignment(
                    row["assignment_id"], str(body.get("actor") or "web"), str(body.get("reason") or "user_requested_abort"),
                )
            except deps.ControlStateError as exc:
                raise deps.HTTPException(status_code=409, detail=str(exc)) from exc
            return {"run_id": run_id, "assignment_id": row["assignment_id"], "status": assignment["status"]}
        try:
            return deps.service.request_abort(
                run_id,
                actor=str(body.get("actor") or "web"),
                reason=str(body.get("reason") or ""),
            )
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except deps.RunNotAbortable as exc:
            raise deps.HTTPException(status_code=409, detail=str(exc)) from exc

    deps.run_abort = run_abort

    @router.post("/api/runs/{run_id}/verification/review")
    def run_verification_review(run_id: str, payload: dict | None = None) -> dict:
        body = payload or {}
        try:
            return deps.service.promotions.review_verification(
                run_id,
                status=str(body.get("status") or ""),
                reviewer=str(body.get("reviewer") or "web"),
                note=str(body.get("note") or ""),
            )
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc

    deps.run_verification_review = run_verification_review

    @router.post("/api/runs/{run_id}/operation/review")
    def run_operation_review(run_id: str, payload: dict | None = None) -> dict:
        body = payload or {}
        try:
            return deps.service.promotions.review_operation(
                run_id,
                status=str(body.get("status") or ""),
                reviewer=str(body.get("reviewer") or "web"),
                note=str(body.get("note") or ""),
            )
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise deps.HTTPException(status_code=422, detail=str(exc)) from exc

    deps.run_operation_review = run_operation_review

    @router.get("/api/runs/{run_id}/artifact/{artifact_path:path}")
    def run_artifact(run_id: str, artifact_path: str):
        try:
            run_dir = deps.service.queries.find_run_dir(run_id)
        except FileNotFoundError as exc:
            raise deps.HTTPException(status_code=404, detail=str(exc)) from exc
        target = (run_dir / artifact_path).resolve()
        root = run_dir.resolve()
        if root not in target.parents and target != root:
            raise deps.HTTPException(status_code=400, detail="artifact path escapes run directory")
        if not target.exists() or not target.is_file():
            raise deps.HTTPException(status_code=404, detail=f"artifact not found: {artifact_path}")
        return deps.FileResponse(target)

    deps.run_artifact = run_artifact

    return router
