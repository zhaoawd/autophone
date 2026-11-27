from __future__ import annotations

import os

from autophone.control._psycopg import psycopg_error_types

from autophone.application.device import (
    ForegroundApplication,
)
from autophone.application.embed_access import (
    EmbedAccessError,
)
from autophone.application.device_access import (
    DeviceAccessError,
)
from fastapi import HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi import APIRouter
from .deps import HttpDeps

def install_helpers(deps: HttpDeps) -> None:

    def _foreground() -> ForegroundApplication:
        return deps.ForegroundApplication(
            keeper=deps.foreground_keeper,
            profile_loader=deps.load_app_profile,
            default_device=deps.DEFAULT_DEVICE,
        )

    deps._foreground = _foreground

    def _device_access_http_error(error: DeviceAccessError) -> HTTPException:
        return deps.HTTPException(status_code=error.status, detail=error.detail())

    deps._device_access_http_error = _device_access_http_error

    def _embed_access_http_error(error: EmbedAccessError) -> HTTPException:
        return deps.HTTPException(
            status_code=error.status,
            detail={"code": error.code, "message": str(error)},
        )

    deps._embed_access_http_error = _embed_access_http_error

    def _embed_start_http_error(device: str, error: HTTPException) -> HTTPException:
        """兑换时 `console_start` 失败的取值：`device_busy` 补上持有者，其余原样。

        §6.2 要求 ``device_busy`` 附持有者类型与开始时间。嵌入页此刻还没有任何凭证，
        自己去问设备状态会被鉴权挡住，所以由服务端在这里补齐。
        """
        detail = error.detail if isinstance(error.detail, dict) else {"message": str(error.detail)}
        if detail.get("code") != "device_busy":
            return error
        holder: dict = {"owner_kind": "web_control"}
        try:
            status = deps._device_session_call(device, "console_device_status", {})
        except deps.HTTPException:
            status = {}
        if status.get("active"):
            holder["session_id"] = status.get("session_id")
            holder["started_at"] = status.get("started_at")
        return deps.HTTPException(
            status_code=error.status_code,
            detail={**detail, "holder": holder},
        )

    deps._embed_start_http_error = _embed_start_http_error

    def _platform_of(request: Request):
        """按 `Authorization: Bearer` 认出接入方（§5.5）。

        只认平台凭证：会话访问凭证（`X-Console-Access-Token`）在这里不构成身份，
        Basic Auth 也不构成——B 端接口与看板接口是两套准入，互不代替。
        """
        header = str(request.headers.get("authorization") or "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        try:
            return deps.embed_access.authenticate_platform(token)
        except deps.EmbedAccessError as error:
            raise deps._embed_access_http_error(error) from error

    deps._platform_of = _platform_of

    def _preview_stream(device: str, *, interval_ms: int, max_consecutive_failures: int = 5):
        """MJPEG 帧循环：每帧一次通道往返，复用同一条预览会话（不每帧建会话）。"""
        failures = 0
        while failures < max_consecutive_failures:
            try:
                frame = deps._device_sessions(device).call(device, "preview_frame", {})
            except (deps.DeviceSessionRefused, deps.HTTPException):
                failures += 1
            else:
                failures = 0
                if not frame.get("jpeg_b64"):
                    # Run 执行中、执行侧尚未取图（BL-227）：不是失败，也不推空帧。
                    deps.time.sleep(interval_ms / 1000)
                    continue
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Cache-Control: no-store\r\n\r\n"
                    + deps.base64.b64decode(frame["jpeg_b64"]) + b"\r\n"
                )
            deps.time.sleep(interval_ms / 1000)

    deps._preview_stream = _preview_stream

    def _release_unusable_console_session(
        device: str, session_id: str, controller_token: str | None,
    ) -> None:
        """创建成功但签不出凭证时把会话还回去；关不掉只记日志，不盖掉原始失败原因。"""
        if not session_id:
            return
        try:
            deps._device_session_call(device, "console_stop", {
                "session_id": session_id, "controller_token": controller_token,
            })
        except Exception as exc:  # noqa: BLE001 - 首要目标是把原始失败报给调用方
            deps.logger.error(
                f"device console rollback failed: session={session_id} device={device}: {exc}"
            )

    deps._release_unusable_console_session = _release_unusable_console_session

    def _device_sessions(device: str):
        """交互通道的执行位置（BL-100）：兼容路径在本进程，控制面模式在 rig 进程。

        - 控制面未配置：返回 `local_device_sessions`。单 VM 兼容路径，本进程就是设备持有者，
          行为与本条之前完全一致；
        - 控制面已配置：返回 `RemoteDeviceSessions`，操作经 `autophone-control` 的
          `POST /api/control/device-sessions/{device}/ops` 转发到服务该设备的 rig agent。
          web 进程不再自己驱动设备，也不再看「设备是不是就挂在本机上」这个部署事实；
        - 控制面已配置但连不上：`_configured_control_or_http_error()` 直接 503，不能因为
          「读不出控制面状态」就当成未配置落回兼容路径（BL-026）。

        `device` 参数保留在签名里是为了让「按设备取执行位置」这件事在调用点可见；当前
        提交侧对所有设备用同一个控制面地址，路由由控制面按 device 完成。
        """
        if deps._configured_control_or_http_error() is None:
            return deps.local_device_sessions
        return deps.RemoteDeviceSessions(deps.control_http_base_url())

    deps._device_sessions = _device_sessions

    def _device_session_call(device: str, op: str, payload: dict) -> dict:
        try:
            return deps._device_sessions(device).call(device, op, payload)
        except deps.DeviceSessionRefused as exc:
            raise deps.HTTPException(
                status_code=exc.status,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    deps._device_session_call = _device_session_call


def create_router(deps: HttpDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/version")
    def api_version() -> dict:
        return deps.build_identity()

    deps.api_version = api_version

    @router.get("/", response_class=HTMLResponse)
    def platform_home(request: Request) -> str:
        # Preserve bookmarks to the application workspace.
        query = request.query_params
        if deps.create_requirements_router and "rig" not in query and any(key in query for key in ("demo", "app", "run", "rrun")):
            return deps.render_dashboard_html()
        from .web import _fingerprint_static_assets

        html = (deps.static_dir / "platform.html").read_text(encoding="utf-8")
        workspace = '<a href="/workspace">应用工作区</a>' if deps.create_requirements_router else ""
        return _fingerprint_static_assets(html.replace("<!-- workspace -->", workspace), deps.static_dir)

    @router.get("/api/platform/targets")
    def platform_targets() -> dict:
        control = deps._configured_control_or_http_error()
        if control is None:
            return {"mode": "local", "targets": [{"rig_id": deps.DEFAULT_DEVICE, "status": "local"}]}
        try:
            rigs = control.list_rigs()
            connected = deps._access_connected_devices() if rigs else set()
        except (OSError, ValueError, *psycopg_error_types()) as exc:
            raise deps.ControlPlaneUnavailable("Rig inventory unavailable") from exc
        if connected is None:
            raise deps.ControlPlaneUnavailable("Device session registry unavailable")
        targets = []
        for rig in rigs:
            device, status = rig["rig_id"], rig["status"]
            if status == "online" and device not in connected:
                status = "offline"
            targets.append({"rig_id": device, "status": status})
        return {"mode": "control", "targets": targets}

    @router.get("/workspace", response_class=HTMLResponse)
    def index() -> str:
        if deps.create_requirements_router is None:
            raise HTTPException(404, "Install autophone[delivery] to enable the application workspace")
        return deps.render_dashboard_html()

    deps.index = index

    @router.get("/embed/device-console", response_class=HTMLResponse)
    def device_console_embed(
        response: Response,
        device: str = deps.Query(...),
        mode: str = deps.Query(...),
        launch: str = deps.Query(""),
    ) -> str:
        """远程操作嵌入页（BL-102 + BL-103）：看板弹窗与 B 端都用 iframe 加载它。

        只返回页面本体。设备访问仍走既有 console 路由 ``/api/devices/{device}/console/...``，
        页面带的是会话访问凭证（BL-103），拿凭证的启动凭证在 ``launch`` 参数里。

        响应头按**启动凭证所属接入方**下发 ``Content-Security-Policy: frame-ancestors``
        （§5.4）：没带启动凭证或凭证已过期时退到 autophone 自己的来源，也就是只有自家看板
        能嵌。这里只是**看**一眼启动凭证（不作废），一次性由兑换口施加。

        两个 query 参数必填，缺一即 4xx：``device`` 是设备键，``mode`` 只取 ``control``
        （可操作）或 ``view``（只读）。页面另认可选的 ``embed``（宿主给的会话标识）、
        ``host_origin``（宿主来源）与 ``demo``（离线预览）。
        """
        device = str(device or "").strip()
        if not device:
            raise deps.HTTPException(
                status_code=422,
                detail={"code": "validation", "message": "device is required"},
            )
        if mode not in ("control", "view"):
            raise deps.HTTPException(
                status_code=422,
                detail={"code": "validation", "message": "mode must be control or view"},
            )
        ticket = deps.embed_access.peek_launch(launch)
        platform_id = ticket.platform_id if ticket is not None else deps.EMBED_SELF_PLATFORM
        response.headers["Content-Security-Policy"] = (
            f"frame-ancestors {deps.embed_access.frame_ancestors(platform_id)}"
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return deps.render_device_console_embed_html()

    deps.device_console_embed = device_console_embed

    @router.post(deps.EMBED_LAUNCH_PATH)
    def embed_launch_token(payload: dict | None = None) -> dict:
        """签发一次性启动凭证（BL-103 / §5.2）。

        autophone 自己的宿主适配层在打开弹窗前调它：走既有 Basic Auth，未配置凭证的开发
        模式下同样可取。签出的凭证只写进 iframe URL，长期凭证不进 URL。

        入参：``device`` 必填；``mode`` 取 ``control`` / ``view``（默认 control）；
        ``platform`` 默认 autophone 自己；``session_id`` 可选，给「绑定一条已存在的会话」
        用（只读观看）。B 端后端自带平台凭证的创建口是 ``POST /api/access/sessions``，
        归 BL-104；在那之前，为别的接入方签发启动凭证只能由 autophone 侧操作者发起。
        """
        body = payload or {}
        device = str(body.get("device") or "").strip()
        mode = str(body.get("mode") or "control").strip()
        platform_id = str(body.get("platform") or deps.EMBED_SELF_PLATFORM).strip()
        session_id = str(body.get("session_id") or "").strip() or None
        try:
            token, ticket = deps.embed_access.issue_launch(
                platform_id=platform_id,
                subject=deps._current_actor(),
                device=device,
                mode=mode,
                console_session_id=session_id,
            )
        except deps.EmbedAccessError as error:
            raise deps._embed_access_http_error(error) from error
        return {
            "launch_token": token,
            "platform": ticket.platform_id,
            "device": ticket.device,
            "mode": ticket.mode,
            "subject": ticket.subject,
            "session_id": ticket.console_session_id,
            "expires_at": ticket.expires_at,
        }

    deps.embed_launch_token = embed_launch_token

    @router.post(deps.EMBED_EXCHANGE_PATH)
    def embed_device_console_session(request: Request, payload: dict | None = None) -> dict:
        """启动凭证换会话访问凭证（BL-103 / §5.1、§5.2）。

        control 模式且启动凭证没有绑定会话时，**由本进程**开 console 会话：
        ``controller_token`` 就此留在 web 进程的授权记录里，响应里没有它，浏览器也拿不到
        （§四）。之后嵌入页的每一次 ``event`` / ``heartbeat`` / ``stop`` 都由服务端从凭证
        解析出会话再取 token 传给 rig。

        启动凭证在进入这里的一刻作废，成功与否都不退回：创建失败（例如 ``device_busy``）
        要由宿主重新签一张。会话已开但响应丢失时，该会话按心跳超时回收（§8.5）。
        """
        body = payload or {}
        token = str(body.get("launch_token") or request.headers.get(deps.LAUNCH_TOKEN_HEADER) or "")
        try:
            ticket = deps.embed_access.redeem_launch(token)
        except deps.EmbedAccessError as error:
            raise deps._embed_access_http_error(error) from error

        session_id = ticket.console_session_id or ""
        controller_token: str | None = None
        started: dict = {}
        if ticket.mode == "control" and not session_id:
            try:
                started = deps._device_session_call(ticket.device, "console_start", {
                    "request_id": f"embed-{ticket.ticket_id}",
                })
            except deps.HTTPException as error:
                raise deps._embed_start_http_error(ticket.device, error) from error
            session_id = str(started.get("session_id") or "")
            controller_token = started.get("controller_token")
            # 刚拿到 lease 就证明这台设备上先前那条 `active` 授权背后的会话已经不在了
            # （心跳超时回收、rig 重启）。不清掉它，`uq_active_grant_per_device` 会让
            # 这台设备再也签不出授权（BL-104）。
            if not ticket.grant_id:
                deps.embed_access.close_open_grant_for_device(ticket.device, reason="session_gone")

        try:
            access_token, grant = deps.embed_access.issue_access(
                ticket, console_session_id=session_id, controller_token=controller_token,
            )
        except EmbedAccessError as error:
            raise deps._embed_access_http_error(error) from error
        except deps.GrantConflict as conflict:
            raise deps.HTTPException(status_code=409, detail={
                "code": f"grant_conflict_{conflict.kind}",
                "message": str(conflict),
            }) from conflict
        result = dict(grant.public())
        result["access_token"] = access_token
        result["access_header"] = deps.CONSOLE_ACCESS_HEADER
        result["started_at"] = started.get("started_at")
        result["geometry"] = started.get("geometry")
        return result

    deps.embed_device_console_session = embed_device_console_session

    @router.get(deps.ACCESS_DEVICES_PATH)
    def access_devices(request: Request) -> dict:
        """接入方可见的设备列表：在线、通道连接、持有者（§6.3）。"""
        platform = deps._platform_of(request)
        try:
            return {"platform": platform.platform_id, "devices": deps.device_access.devices(platform)}
        except deps.DeviceAccessError as error:
            raise deps._device_access_http_error(error) from error

    deps.access_devices = access_devices

    @router.post(deps.ACCESS_SESSIONS_PATH)
    def access_create_session(request: Request, payload: dict | None = None) -> dict:
        """分配设备并创建会话（§10.1）。

        入参 `request_id`（创建幂等的键）、`user`、`mode`，可选 `device`、`ttl_s`。
        出参含 `session_id`、`embed_url`（带一次性启动凭证）、`device`、`mode`、
        `expires_at`；`controller_token` **不在**出参里，任何分支都不在。
        全部候选被占用时 409 `device_busy`，`candidates` 附各候选的持有者类型与开始时间。
        """
        platform = deps._platform_of(request)
        body = payload or {}
        try:
            return deps.device_access.create_session(
                platform,
                request_id=str(body.get("request_id") or ""),
                user=str(body.get("user") or ""),
                mode=str(body.get("mode") or "control"),
                device=str(body.get("device") or "") or None,
                ttl_s=body.get("ttl_s"),
            )
        except deps.DeviceAccessError as error:
            raise deps._device_access_http_error(error) from error

    deps.access_create_session = access_create_session

    @router.get(deps.ACCESS_SESSIONS_PATH + "/{session_id}")
    def access_get_session(session_id: str, request: Request) -> dict:
        """会话状态（§10.1）。`session_id` 收 grant_id 或 console 会话标识。"""
        platform = deps._platform_of(request)
        try:
            return deps.device_access.get_session(platform, session_id)
        except deps.DeviceAccessError as error:
            raise deps._device_access_http_error(error) from error

    deps.access_get_session = access_get_session

    @router.delete(deps.ACCESS_SESSIONS_PATH + "/{session_id}")
    def access_revoke_session(session_id: str, request: Request) -> dict:
        """撤销，触发 §八 的清理流程（§7.2 第 5 条的第一个入口）。"""
        platform = deps._platform_of(request)
        try:
            return deps.device_access.revoke_session(
                platform, session_id, reason="revoked",
                actor=f"platform:{platform.platform_id}",
            )
        except deps.DeviceAccessError as error:
            raise deps._device_access_http_error(error) from error

    deps.access_revoke_session = access_revoke_session

    @router.post(deps.ACCESS_SESSIONS_PATH + "/{session_id}/kick")
    def access_kick_session(session_id: str, request: Request) -> dict:
        """看板管理员踢出（§7.2 第 5 条的第二个入口）。

        路径多一段，所以它落在 Basic Auth 之后而不是平台凭证豁免里：踢出是运维动作，
        不是 B 端动作。与 `DELETE` 走同一条关闭流程，只是 `reason` 记 `kicked`。
        事件表（BL-116）的 actor 记 `dashboard:<Basic Auth 用户>`；未配置
        `AUTOPHONE_AUTH` 时中间件记的是 `local`，原样写入，不当作已认证身份。
        """
        who = str(getattr(request.state, "actor", "") or "local")
        try:
            return deps.device_access.revoke_session(
                None, session_id, reason="kicked", actor=f"dashboard:{who}",
            )
        except deps.DeviceAccessError as error:
            raise deps._device_access_http_error(error) from error

    deps.access_kick_session = access_kick_session

    @router.get("/api/access/grants")
    def access_list_grants() -> dict:
        """看板侧的授权记录清单（Basic Auth）：现在谁在操作哪台设备。"""
        rows = deps.embed_access.grants.list()
        return {"grants": [row.public() for row in rows]}

    deps.access_list_grants = access_list_grants

    @router.post("/api/access/reservations/reconcile")
    def access_reconcile_reservations(payload: dict | None = None) -> dict:
        """`rig_reservations` 与 console 会话的对账入口（§十三，Basic Auth）。"""
        body = payload or {}
        control = deps._configured_control_or_http_error()
        if control is None:
            raise deps.HTTPException(status_code=409, detail={
                "code": "control_plane_not_configured",
                "message": "reservation reconciliation needs the control plane",
            })
        try:
            return deps.reconcile_console_reservations(
                control=control,
                call=lambda device, op, data: deps._device_sessions(device).call(device, op, data),
                grants=deps.embed_access.grants,
                devices=body.get("devices") or None,
                actor=deps._current_actor(),
                apply=bool(body.get("apply", True)),
            )
        except deps.ControlStateError as exc:
            raise deps.HTTPException(status_code=409, detail=str(exc)) from exc

    deps.access_reconcile_reservations = access_reconcile_reservations

    @router.get("/health")
    def health() -> dict:
        return {"ok": True, "service": "autophone-run"}

    deps.health = health

    @router.get("/api/vm/recovery")
    def vm_recovery() -> dict:
        """自动恢复链的只读投影（设计文档 §5，BL-075 PR 3）。

        数据**只来自** `scripts/lib/stage_events.sh` 落下的结构化事件（阶段事件 +
        五类恢复事件）：不解析 `down-alerts.log` / `autorestart.log` 的文案，也不拿
        `_probe_health_guard()` 的 `on_down` 代理值冒充证据。没有结构化事件时是
        `status="none"`（页面显示「无恢复记录」），不是成功态。
        本接口只读，不提供任何手工 start / recover / stop 入口。
        """
        from autophone.observation.run_log_feed import recovery_projection

        environment = dict(os.environ)
        target = deps.device_application.target()
        environment.setdefault(
            "VPHONE_LOG_DIR",
            str(deps.Path(target["vm_dir"]) / "logs"),
        )
        return recovery_projection(environment)

    deps.vm_recovery = vm_recovery

    @router.get("/api/vm/installed-apps")
    def vm_installed_apps() -> dict:
        """枚举 VM 里已装的第三方 app（不是经 autophone 装的），供
        「从 VM 接入」直接建档——无需 IPA、不装机。

        走 guest UDS ``app_list filter:"user"``（上游 AppFilter 已只返回用户 app、
        不含系统 app）。每个 bundle_id 标注是否已被现有 profile 接入（``onboarded`` +
        ``app``），并给一个 ``suggested_app`` slug 供前端预填。设备不可达 / app_list
        报错 → 503（fail-closed，不掺假空列表）。
        """
        try:
            return deps.device_application.installed_apps()
        except deps.DeviceApplicationError as exc:
            raise deps.HTTPException(status_code=503, detail=str(exc)) from exc

    deps.vm_installed_apps = vm_installed_apps

    @router.get("/api/vm/foreground")
    def get_foreground() -> dict:
        """前台保活当前状态（钉的是哪个 app、看门狗是否在跑、上一拍动作）。"""
        return deps._foreground().status()

    deps.get_foreground = get_foreground

    @router.get("/api/vm/target")
    def get_vm_target() -> dict:
        """Return the exact startup-selected VM, distinct from its logical device key."""
        return deps.device_application.target()

    deps.get_vm_target = get_vm_target

    @router.post("/api/vm/foreground")
    def set_foreground(payload: dict | None = None) -> dict:
        """钉住一个 app 为前台保活目标（dashboard 左上角切 app 时调用）。

        入参 ``{"app": <slug>, "device": <可选, 默认 vphone-0>}``；从 profile 解析
        bundle_id。看门狗每拍把它拉回前台，run 占设备时自动暂停（见 ForegroundKeeper）。
        """
        try:
            return deps._foreground().pin(payload or {})
        except deps.ForegroundApplicationError as exc:
            raise deps.HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    deps.set_foreground = set_foreground

    @router.delete("/api/vm/foreground")
    def clear_foreground() -> dict:
        """解除前台保活（停止把任何 app 拉回前台）。"""
        return deps._foreground().clear()

    deps.clear_foreground = clear_foreground

    @router.get("/api/apps/{app_name}/health")
    def app_case_health(app_name: str) -> dict:
        profile = deps.load_app_profile(app_name)
        try:
            return deps.health_mod.app_health(
                deps.service.run_root,
                app_name,
                cases_path=profile.cases_path(),
                operations_path=profile.operations_path(),
                api_expectations_path=profile.api_expectations_path(),
                negctl_root=deps.STATE_ROOT / "runs" / "negctl",
                build_label=profile.build_label,
                recipes_dir=profile.recipes_dir(),
            )
        except Exception as exc:  # noqa: BLE001
            raise deps.HTTPException(status_code=503, detail=f"run index unavailable: {exc}") from exc

    deps.app_case_health = app_case_health

    @router.get("/devices/{device}/status")
    def device_status(device: str) -> dict:
        return deps._device_session_call(device, "device_status", {})

    deps.device_status = device_status

    @router.get("/devices/{device}/screen.jpg")
    def screen_frame(device: str):
        """单帧快照（短连接 GET）。

        MJPEG 长连接（``multipart/x-mixed-replace``）会被中间代理整条缓冲，导致内网
        其它机器画面永不刷新。前端默认轮询这个端点：每次都是一次普通的短 GET，代理
        不缓冲，跨内网/代理可靠。返回 JPEG 字节；设备不可达时 503。

        Run 执行期间（BL-227）返回执行侧最近帧，响应头带 `X-Frame-Source: executor`、
        `X-Frame-Captured-At`、`X-Frame-Stale-Seconds`、`X-Frame-Run-Id`；执行侧尚未取图时
        返回 202 JSON（`code=executor_frame_pending`），不是 503 `device_error`。
        """
        frame = deps._device_session_call(device, "preview_frame", {})
        from fastapi.responses import JSONResponse, Response

        headers = {"Cache-Control": "no-store"}
        if frame.get("source") == "executor":
            # Run 执行中（BL-227）：画面来自执行侧最近帧，不是本次请求现取的。
            headers["X-Frame-Source"] = "executor"
            if frame.get("run_id"):
                headers["X-Frame-Run-Id"] = str(frame["run_id"])
            if not frame.get("jpeg_b64"):
                headers["X-Frame-State"] = str(frame.get("state") or "executor_frame_pending")
                return JSONResponse(
                    status_code=202,
                    content={
                        "code": headers["X-Frame-State"],
                        "source": "executor",
                        "run_id": frame.get("run_id") or None,
                        "message": "执行中，等待执行侧画面",
                    },
                    headers=headers,
                )
            headers["X-Frame-Captured-At"] = f"{float(frame.get('captured_at') or 0.0):.3f}"
            headers["X-Frame-Stale-Seconds"] = f"{float(frame.get('stale_seconds') or 0.0):.3f}"
        return Response(
            content=deps.base64.b64decode(frame["jpeg_b64"]),
            media_type="image/jpeg",
            headers=headers,
        )

    deps.screen_frame = screen_frame

    @router.get("/devices/{device}/screen.mjpeg")
    def screen_stream(device: str, interval_ms: int = deps.Query(default=800, ge=250, le=5000)):
        # 第一帧同步取：连接建立前就能把「设备不可达 / 通道不通」变成一个 HTTP 状态码，
        # 而不是一条已经 200 了的空流。
        deps._device_session_call(device, "preview_frame", {})
        return deps.StreamingResponse(
            deps._preview_stream(device, interval_ms=interval_ms),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    deps.screen_stream = screen_stream

    @router.get("/api/devices/{device}/console")
    def device_console_status(device: str) -> dict:
        """本机看板问：现在有没有人在页面上直接操作这台设备。"""
        return deps._device_session_call(device, "console_device_status", {})

    deps.device_console_status = device_console_status

    @router.post("/api/devices/{device}/console")
    def device_console_start(device: str, payload: dict | None = None) -> dict:
        """开一条纯远程操作会话：占 ScreenLock，不建录制目录、不产 trace。

        `request_id` 是创建幂等的键（BL-107 / platform-device-console-embed.md §8.2）：
        带同一个 ID 并发或重试拿回同一条会话，不二次占锁。调用方没给时**由 web 侧生成**
        一个，好让这一条会话在日志与预留 owner 里可追溯；但生成的 ID 每次调用都不同，
        所以这种调用没有跨调用幂等——要幂等就自己带稳定 ID 来。

        创建成功时**同时给创建者签一份会话访问凭证**（`access_token`，接入方是 autophone
        自己，subject 取当前 actor）。五条会话路由只认这一种凭证（开发模式下 Basic Auth
        不构成身份，§5.1），所以不签的话这条会话开出来就没有结束入口，设备会被占到心跳
        超时。`controller_token` 不再进响应：它留在授权记录里，由服务端在动作路由上取出
        交给 rig（§四、§5.1）。
        """
        request_id = str((payload or {}).get("request_id") or "").strip()
        request_id = request_id or f"web-{deps.secrets.token_hex(8)}"
        started = deps._device_session_call(device, "console_start", {"request_id": request_id})
        result = dict(started)
        controller_token = result.pop("controller_token", None)
        session_id = str(result.get("session_id") or "")
        try:
            access_token, grant = deps.embed_access.issue_session_access(
                platform_id=deps.EMBED_SELF_PLATFORM,
                subject=deps._current_actor(),
                device=device,
                console_session_id=session_id,
                controller_token=controller_token,
                request_id=f"console-{request_id}",
            )
        except (deps.EmbedAccessError, deps.GrantConflict) as error:
            # 签不出凭证的会话没有结束入口，留着就是占着设备直到心跳超时：当场把它关掉，
            # 再把失败原样报出来（fail-closed，§8.1）。
            deps._release_unusable_console_session(device, session_id, controller_token)
            if isinstance(error, deps.GrantConflict):
                raise deps.HTTPException(status_code=409, detail={
                    "code": f"grant_conflict_{error.kind}",
                    "message": str(error),
                }) from error
            raise deps._embed_access_http_error(error) from error
        result["access_token"] = access_token
        result["access_header"] = deps.CONSOLE_ACCESS_HEADER
        result["grant_id"] = grant.grant_id
        result["access_expires_at"] = grant.expires_at
        return result

    deps.device_console_start = device_console_start

    @router.get("/api/devices/{device}/console/{session_id}")
    def device_console_view(device: str, session_id: str) -> dict:
        return deps._device_session_call(device, "console_view", {"session_id": session_id})

    deps.device_console_view = device_console_view

    @router.get("/api/devices/{device}/console/{session_id}/frame")
    def device_console_frame(device: str, session_id: str):
        frame = deps._device_session_call(device, "console_frame", {"session_id": session_id})
        from fastapi.responses import Response

        return Response(
            content=deps.base64.b64decode(frame["jpeg_b64"]),
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store",
                "X-Frame-Seq": str(frame["seq"]),
                "X-Captured-At": str(frame["captured_at"]),
            },
        )

    deps.device_console_frame = device_console_frame

    @router.post("/api/devices/{device}/console/{session_id}/event")
    def device_console_event(
        device: str, session_id: str, request: Request, payload: dict | None = None,
    ) -> dict:
        return deps._device_session_call(device, "console_event", {
            "session_id": session_id,
            "event": payload or {},
            "controller_token": deps._controller_token(request),
        })

    deps.device_console_event = device_console_event

    @router.post("/api/devices/{device}/console/{session_id}/heartbeat")
    def device_console_heartbeat(device: str, session_id: str, request: Request) -> dict:
        return deps._device_session_call(device, "console_heartbeat", {
            "session_id": session_id,
            "controller_token": deps._controller_token(request),
        })

    deps.device_console_heartbeat = device_console_heartbeat

    @router.post("/api/devices/{device}/console/{session_id}/stop")
    def device_console_stop(device: str, session_id: str, request: Request) -> dict:
        result = deps._device_session_call(device, "console_stop", {
            "session_id": session_id,
            "controller_token": deps._controller_token(request),
        })
        # 会话结束即作废指向它的会话访问凭证：凭证的寿命跟着授权记录走（§四），
        # 会话没了还留着凭证只会让下一次请求拿到 404 而不是明确的「已撤销」。
        deps.embed_access.revoke_session(session_id, reason="console_stop")
        return result

    deps.device_console_stop = device_console_stop

    @router.get("/api/doctor")
    def doctor() -> dict:
        """平台健康检查聚合：vsock_health / screenshot / guest shell / AI provider 四源探活。

        各源 fail-open（一个挂不连累整卡），整体 status 取最差。轻量探活——不驱动被测 app、
        不跑完整 shell_smoke、不 probe 真模型调用。doctor 在 thunk 内懒 import，import 期不触达 VM/AI。"""
        from autophone.application.doctor import run_doctor

        return run_doctor()

    deps.doctor = doctor

    return router
