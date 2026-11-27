from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import inspect
from pathlib import Path
from autophone.execution.release_lock import (
    ReleaseLockBusy,
)
from autophone.assets.policy import ReadonlyAssetsError
from autophone.paths import AppAssetRootError
from autophone.application.control_plane import (
    ControlPlaneUnavailable,
)
from autophone.application.uploads import UploadApplication
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from autophone.logging import get_logger
from .deps import HttpDeps

log = get_logger(__name__)


class RuntimeShutdownError(RuntimeError):
    """看板收尾有步骤失败（BL-219）。全部步骤执行完之后才抛出，``failures`` 按执行顺序列出。"""

    def __init__(self, failures: list[tuple[str, BaseException]]) -> None:
        self.failures = list(failures)
        super().__init__(
            "dashboard shutdown steps failed: "
            + "; ".join(f"{name}: {type(exc).__name__}: {exc}" for name, exc in self.failures)
        )


#: 本进程最近一次收尾失败的步骤名；`serve` 在 uvicorn 返回后据此给出非 0 退出码（BL-219）。
#: uvicorn 对 lifespan 收尾失败只记 `Application shutdown failed`，进程仍以 0 退出。
_last_shutdown_failures: list[str] = []


def last_shutdown_failures() -> list[str]:
    """本进程最近一次看板收尾中失败的步骤名（按执行顺序）；未收尾或全部成功时为空。"""
    return list(_last_shutdown_failures)


def runtime_shutdown_steps(deps: HttpDeps) -> list[tuple[str, Callable[[], Any]]]:
    """看板 lifespan 收尾的有序步骤；返回协程的步骤会被等待。运行时释放锁不在其中，由调用方最后释放。"""
    return [
        ("scenario_runtime", deps.scenario_runtime.shutdown),
        ("recording_sessions", lambda: deps.recording_sessions.close_all("server_shutdown")),
        ("device_console", deps.device_console.shutdown),
        ("location_session_coordinator",
         lambda: deps.location_session_coordinator.shutdown(preserve_checkpoints=True)),
        ("package_gate_jobs", lambda: deps.package_gate_jobs.shutdown(wait=False)),
        ("ai_jobs", lambda: deps.ai_jobs.shutdown(wait=False)),
        ("scheduler_loop", deps._stop_scheduler_loop),
    ]


async def shutdown_runtime(deps: HttpDeps) -> None:
    """逐项执行看板收尾，一步失败不跳过其后步骤（BL-219）。

    每步结果记一条日志（成功 INFO，失败 ERROR 带步骤名与堆栈）；全部执行完后有失败则记下失败
    步骤（`last_shutdown_failures`）并抛 `RuntimeShutdownError`，经 lifespan 交给 ASGI 服务器报告
    收尾失败。
    """
    failures: list[tuple[str, BaseException]] = []
    _last_shutdown_failures.clear()
    steps = runtime_shutdown_steps(deps)
    for name, step in steps:
        try:
            outcome = step()
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as exc:  # noqa: BLE001 - every later step still runs; reported below
            failures.append((name, exc))
            log.error("dashboard shutdown step failed: step=%s: %s: %s", name, type(exc).__name__, exc,
                      exc_info=exc)
        else:
            log.info("dashboard shutdown step done: step=%s", name)
    _last_shutdown_failures.extend(name for name, _ in failures)
    if failures:
        log.error(
            "dashboard shutdown finished with failures: %d/%d steps failed: %s",
            len(failures), len(steps), ", ".join(name for name, _ in failures),
        )
        raise RuntimeShutdownError(failures) from failures[0][1]
    log.info("dashboard shutdown finished: %d steps done", len(steps))


def install_helpers(deps: HttpDeps) -> None:

    @asynccontextmanager
    async def _lifespan(_: FastAPI):
        deps._acquire_runtime_release_lock()
        try:
            recoveries = await asyncio.to_thread(deps.service.recover_pending_closures)
            for recovery in recoveries:
                if recovery['status'] not in {'complete', 'settled'}:
                    deps.logger.warning('Run closure recovery: %s', recovery)
            deps.device_console.start_scanner()
            await deps._start_scheduler_loop()
            try:
                yield
            finally:
                await shutdown_runtime(deps)
        finally:
            deps._release_runtime_release_lock()

    deps._lifespan = _lifespan

    async def _readonly_assets_error_handler(_request: Request, error: ReadonlyAssetsError) -> JSONResponse:
        return deps.JSONResponse(status_code=409, content={"detail": error.as_detail()})

    deps._readonly_assets_error_handler = _readonly_assets_error_handler

    async def _app_asset_root_error_handler(_request: Request, error: AppAssetRootError) -> JSONResponse:
        # AUTOPHONE_APP_ASSET_DIR 开启时，未随开关移动的入口拒绝执行，不落回仓库里的 apps/。
        return deps.JSONResponse(status_code=409, content={"detail": error.as_detail()})

    deps._app_asset_root_error_handler = _app_asset_root_error_handler

    async def _release_lock_busy_handler(_request: Request, error: ReleaseLockBusy) -> JSONResponse:
        # A background run worker could not take its shared release lease, so an
        # exclusive release update is in flight.  Report it as a conflict instead of
        # letting it surface as an unhandled 500.
        return deps.JSONResponse(
            status_code=409,
            content={"detail": {"code": "release_update_active", "message": str(error)}},
        )

    deps._release_lock_busy_handler = _release_lock_busy_handler

    def _foreground_interval() -> float:
        try:
            return max(1.0, float(deps.os.environ.get("AUTOPHONE_FOREGROUND_INTERVAL_S", "5")))
        except ValueError:
            return 5.0

    deps._foreground_interval = _foreground_interval

    def _await_location_quiesced(device: str, timeout_s: float) -> bool:
        return deps.location_session_coordinator.await_quiesced(device, timeout_s)

    deps._await_location_quiesced = _await_location_quiesced

    async def _scheduler_loop() -> None:
        while True:
            await deps.asyncio.sleep(deps.scheduler_mod.TICK_SECONDS)
            try:
                deps.schedule_runner.tick()
            except deps.asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                deps.logger.exception("schedule tick failed")

    deps._scheduler_loop = _scheduler_loop

    async def _start_scheduler_loop() -> None:
        pass  # State is held by HttpDeps.
        if deps.os.environ.get("AUTOPHONE_SCHEDULER_ENABLED", "1") in ("0", "false", "False"):
            return
        if deps._schedule_task is None or deps._schedule_task.done():
            deps._schedule_task = deps.asyncio.create_task(deps._scheduler_loop())

    deps._start_scheduler_loop = _start_scheduler_loop

    async def _stop_scheduler_loop() -> None:
        pass  # State is held by HttpDeps.
        if deps._schedule_task is None:
            return
        deps._schedule_task.cancel()
        try:
            await deps._schedule_task
        except deps.asyncio.CancelledError:
            pass
        deps._schedule_task = None

    deps._stop_scheduler_loop = _stop_scheduler_loop

    def _load_basic_auth_users() -> dict[str, str]:
        """Parse ``AUTOPHONE_AUTH`` (``user:pass,user2:pass2``) into a credential map.

        Empty / unset → returns ``{}`` → auth is disabled (local-dev behaviour is
        unchanged). Setting it on the ``serve`` host turns on Basic Auth for every
        request except ``/health``.
        """
        raw = deps.os.environ.get("AUTOPHONE_AUTH", "").strip()
        users: dict[str, str] = {}
        for pair in raw.split(","):
            pair = pair.strip()
            user, sep, pw = pair.partition(":")
            if sep and user and pw:
                users[user] = pw
        return users

    deps._load_basic_auth_users = _load_basic_auth_users

    async def _call_as_actor(request: Request, call_next, actor: str):
        request.state.actor = actor
        actor_token = deps._REQUEST_ACTOR.set(actor)
        key_token = deps._REQUEST_IDEMPOTENCY_KEY.set(request.headers.get("idempotency-key"))
        try:
            return await call_next(request)
        finally:
            deps._REQUEST_IDEMPOTENCY_KEY.reset(key_token)
            deps._REQUEST_ACTOR.reset(actor_token)

    deps._call_as_actor = _call_as_actor

    def _current_idempotency_key() -> str | None:
        return deps.validate_idempotency_key(deps._REQUEST_IDEMPOTENCY_KEY.get())

    deps._current_idempotency_key = _current_idempotency_key

    def _uploads(*, case_stage_root: Path | None = None) -> UploadApplication:
        return deps.UploadApplication(
            package_store=deps.pkg,
            max_ipa_bytes=deps.MAX_IPA_UPLOAD_BYTES,
            max_case_xlsx_bytes=deps.MAX_CASE_XLSX_BYTES,
            case_stage_root=case_stage_root,
        )

    deps._uploads = _uploads

    def _access_connected_devices() -> set[str] | None:
        """交互通道上当前连着哪些设备。

        控制面未配置时返回 `None`（不知道，也不需要知道：本进程就是设备持有者，
        分配不拿这一项过滤）。已配置时读控制面的通道注册表。
        """
        if deps._configured_control_or_http_error() is None:
            return None
        import urllib.request

        url = f"{deps.control_http_base_url()}/api/control/device-sessions"
        with urllib.request.urlopen(url, timeout=5.0) as response:  # noqa: S310 - 本机控制面
            document = deps.json.loads(response.read().decode("utf-8"))
        return {
            str(row.get("device") or "") for row in (document.get("devices") or [])
            if str(row.get("device") or "")
        }

    deps._access_connected_devices = _access_connected_devices

    def _basic_auth_identity(request: Request) -> str | None:
        """这条请求是否已由 Basic Auth 认出一个真实用户。

        未配置 `AUTOPHONE_AUTH` 时恒为 `None`：中间件那条开发模式放行分支记的 actor 是
        `local`，它不是身份。会话授权因此不会被开发模式跳过（§5.1 最后一段）。
        """
        if not deps._BASIC_AUTH_USERS:
            return None
        actor = getattr(request.state, "actor", None)
        if isinstance(actor, str) and actor in deps._BASIC_AUTH_USERS:
            return actor
        return None

    deps._basic_auth_identity = _basic_auth_identity

    def _expire_console_grant(token: str) -> None:
        """到期语义（§8.5）：web 侧拒绝动作与心跳之外，还要真的把设备收回来。

        只对「凭证有效但授权已过期」这一种拒绝触发关闭；未知凭证、签名不符、已被撤销的
        都不触发（前两种没有对应记录，第三种已经在关或已关）。关闭走 `revoke_session`
        的同一条流程，`reason=expired`。
        """
        grant = deps.embed_access.peek_access(token)
        if grant is None or grant.status not in ("creating", "active"):
            return
        if grant.expires_at > deps.time.time():
            return
        try:
            deps.device_access.expire_grant(grant)
        except Exception as exc:  # noqa: BLE001 - 拒绝这条请求是首要目标，关闭失败只记日志
            deps.logger.error(
                f"device access expiry cleanup failed: grant={grant.grant_id} "
                f"device={grant.device}: {exc}"
            )

    deps._expire_console_grant = _expire_console_grant

    def _resolve_console_grant(request: Request, device: str, session_id: str, action: str):
        """console 会话路由的准入：返回会话访问凭证的授权记录，或 `None`（Basic Auth 身份）。

        判定顺序固定：带凭证就按凭证判（凭证不对不回落 Basic Auth，免得一个坏凭证被当成
        「没带凭证」放行）；不带凭证才看 Basic Auth；两者都没有就是 401。
        """
        token = request.headers.get(deps.CONSOLE_ACCESS_HEADER) or ""
        if token:
            grant = deps.embed_access.resolve_access(token)
            if grant is None:
                deps._expire_console_grant(token)
                raise deps.HTTPException(status_code=401, detail={
                    "code": "console_access_invalid",
                    "message": "the session access credential is unknown, expired or revoked",
                })
            if grant.device != device:
                raise deps.HTTPException(status_code=403, detail={
                    "code": "console_access_device_mismatch",
                    "message": "the credential is bound to another device",
                })
            if grant.console_session_id != session_id:
                raise deps.HTTPException(status_code=403, detail={
                    "code": "console_access_session_mismatch",
                    "message": "the credential is bound to another console session",
                })
            if action in deps.CONSOLE_CONTROL_ACTIONS and grant.mode != "control":
                raise deps.HTTPException(status_code=403, detail={
                    "code": "console_access_read_only",
                    "message": f"a view credential cannot call {action}",
                })
            return grant
        if deps._basic_auth_identity(request) is not None:
            return None
        raise deps.HTTPException(status_code=401, detail={
            "code": "console_access_required",
            "message": "a session access credential is required for console session routes",
        })

    deps._resolve_console_grant = _resolve_console_grant

    async def _console_session_access(request: Request, call_next):
        """console 五条会话路由的鉴权闸（`view` / `frame` / `event` / `heartbeat` / `stop`）。

        它挂在 `_basic_auth` **里面**（本函数先注册即更内层），所以进到这里时 Basic Auth
        已经判完、`request.state.actor` 已经写好，两种身份可以在同一处比较。

        创建入口 `POST /api/devices/{device}/console` 不在闸内：会话访问凭证按 §5.1 不能
        创建会话，创建仍只认 Basic Auth（或开发模式）。
        """
        match = deps._CONSOLE_SESSION_PATH.match(request.url.path)
        if match is None:
            return await call_next(request)
        device = deps.unquote(match.group("device"))
        session_id = deps.unquote(match.group("session"))
        action = (match.group("action") or "/view").lstrip("/")
        try:
            grant = deps._resolve_console_grant(request, device, session_id, action)
        except deps.HTTPException as error:
            return deps.JSONResponse(status_code=error.status_code, content={"detail": error.detail})
        if grant is None:
            return await call_next(request)
        # 凭证路径：actor 记成 `platform:<platform_id>/<user>`，controller_token 由路由从
        # 授权记录里取（`_controller_token`），浏览器不经手。
        request.state.console_grant = grant
        return await deps._call_as_actor(request, call_next, grant.actor())

    deps._console_session_access = _console_session_access

    async def _basic_auth(request: Request, call_next):
        # Disabled when no credentials configured; /health stays open for probes.
        #
        # 兑换口 `EMBED_EXCHANGE_PATH` 也不走 Basic Auth：它是嵌入页拿凭证的第一步，
        # 跨域 iframe 带不了 Basic 凭据，一次性启动凭证就是它的凭证，路由自己校验并按
        # 启动凭证写 actor。豁免只到这一条路径为止——console 会话路由仍要凭证（见
        # `_console_session_access`）。
        path = request.url.path
        # 页面与脚本不含设备数据；画面与动作继续由会话授权校验。
        if request.method in {"GET", "HEAD"} and path in {
            "/embed/device-console", "/static/embed/device-console.js",
        }:
            return await deps._call_as_actor(request, call_next, "embed-page")
        if path == deps.EMBED_EXCHANGE_PATH:
            return await deps._call_as_actor(request, call_next, "embed-exchange")
        # 四条 B 端后端接口（§10.1）用平台凭证（`Authorization: Bearer`），不是 Basic Auth：
        # 它们的调用方是 B 端后端，没有看板账号。路由自己校验凭证并写 actor；这条豁免只覆盖
        # §10.1 的四条形状，`/api/access/sessions/{id}/kick`（管理员踢出）不在其中。
        if deps._ACCESS_API_PATH.match(path):
            return await deps._call_as_actor(request, call_next, "platform-api")
        # console 五条会话路由**带了会话访问凭证时**也不走 Basic Auth：跨域 iframe 带不了
        # Basic 凭据，而这五条自己有更严的闸（`_console_session_access`：凭证要绑到这台
        # 设备、这条会话、这个动作）。放行只到「把判定交给内层」为止——凭证不对仍是 401，
        # 但 code 是 `console_access_invalid`，不再是浏览器会弹框的 Basic 挑战。
        # 不带这个头时照旧 Basic Auth：会话闸不因此少一层。
        if request.headers.get(deps.CONSOLE_ACCESS_HEADER) and deps._CONSOLE_SESSION_PATH.match(path):
            return await deps._call_as_actor(request, call_next, "console-access")
        if not deps._BASIC_AUTH_USERS or path == "/health":
            return await deps._call_as_actor(request, call_next, "local" if not deps._BASIC_AUTH_USERS else "anonymous")
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, pw = deps.base64.b64decode(header[6:]).decode("utf-8").partition(":")
                expected = deps._BASIC_AUTH_USERS.get(user)
                if expected is not None and deps.secrets.compare_digest(pw, expected):
                    return await deps._call_as_actor(request, call_next, user)
            except Exception:
                pass  # fall through to 401
        return deps.PlainTextResponse(
            "401 Unauthorized",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="autophone"'},
        )

    deps._basic_auth = _basic_auth

    async def _no_store_api(request: Request, call_next):
        """API 是控制面（用例/包/run 的真状态），响应一律 no-store。

        路由默认不带任何缓存头（无 Cache-Control/ETag/Last-Modified），WebKit/Safari 会把重复的
        fetch GET 当可缓存 → 编辑用例存盘后 render() 重新拉清单命中浏览器缓存、读回编辑前的旧快照，
        再打开编辑器就「保存了又没了」。给 /api/ 响应显式 no-store 断掉这条缓存（/static/ 资源不受影响，
        仍可缓存）。screen.jpg 已各自带 no-store，这里覆盖一遍也无害。"""
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    deps._no_store_api = _no_store_api

    def _controller_token(request: Request) -> str | None:
        """取这条请求要交给 rig 的 ``controller_token``。

        凭证路径（BL-103）：从 `_console_session_access` 解析出的授权记录里取，请求头里
        带来的同名值一律不用——``controller_token`` 不出浏览器，客户端也就无从提供它。
        Basic Auth 路径与录制面仍读 ``X-Controller-Token`` 头，rig 侧 token 语义不变。
        """
        grant = getattr(request.state, "console_grant", None)
        if grant is not None:
            return grant.controller_token
        return request.headers.get("x-controller-token") or None

    deps._controller_token = _controller_token

    def _control_plane_unavailable_error(exc: ControlPlaneUnavailable) -> HTTPException:
        """「控制面已配置但不可用」的统一 HTTP 取值：503 + 稳定 code（BL-026）。

        503 而不是 500：本机确实配置了控制面，故障是**依赖不可用**，重试有意义。
        """
        return deps.HTTPException(
            status_code=503,
            detail={"code": deps.ControlPlaneUnavailable.code, "message": str(exc)},
        )

    deps._control_plane_unavailable_error = _control_plane_unavailable_error

    async def _control_plane_unavailable_handler(
        _request: Request, exc: ControlPlaneUnavailable,
    ) -> JSONResponse:
        """兜底：任何路由走到「控制面已配置但不可用」都以 503 收场，不是 500。

        显式判定仍留在 `create_run()` 与 `_require_legacy_device_mode()`（判据点名的
        两处），这里只保证其余经 `configured_control_plane()` 的路由取值一致。
        """
        error = deps._control_plane_unavailable_error(exc)
        return deps.JSONResponse(status_code=error.status_code, content={"detail": error.detail})

    deps._control_plane_unavailable_handler = _control_plane_unavailable_handler

    def _configured_control_or_http_error():
        """区分「未配置」与「已配置但不可用」：前者 `None`，后者 503（BL-026）。

        返回 `None` 的调用方会落回单 VM 兼容路径，所以「已配置但不可用」绝不能走这条
        返回——那正是本条待办要消除的静默回退。
        """
        try:
            return deps.configured_control_plane()
        except deps.ControlPlaneUnavailable as exc:
            raise deps._control_plane_unavailable_error(exc) from exc

    deps._configured_control_or_http_error = _configured_control_or_http_error

    def _control_or_http_error():
        try:
            return deps.require_control_plane()
        except deps.ControlPlaneNotConfigured as exc:
            raise deps.HTTPException(status_code=503, detail=str(exc)) from exc
        except deps.ControlPlaneUnavailable as exc:
            raise deps._control_plane_unavailable_error(exc) from exc

    deps._control_or_http_error = _control_or_http_error

    def _submit_scene(scene: str, request: dict, **kwargs) -> dict:
        try:
            return deps.ai_scenes.submit(
                scene,
                request,
                actor=kwargs.pop("actor", deps._current_actor()),
                idempotency_key=kwargs.pop(
                    "idempotency_key",
                    deps._current_idempotency_key(),
                ),
                **kwargs,
            )
        except deps.AiSceneApplicationError as exc:
            raise deps.HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    deps._submit_scene = _submit_scene
