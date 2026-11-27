"""交互通道的操作面与提交侧客户端（BL-100）。

这是「交互通道」的中间层：**一个操作集合，两个执行位置**。

- `DeviceSessionOps` 是操作本体。它不认识控制面，也不认识 HTTP——只认识一个
  device console 管理器、录制会话操作面、一个截图函数和一个设备状态函数。持有设备的进程构造它：
  单 VM 兼容路径下是 web 进程自己，控制面模式下是 rig agent 进程；
- `RemoteDeviceSessions` 是提交侧客户端。web 进程在控制面模式下用它，把同一组操作名
  经控制面转发到持设备的那台 rig。

两边操作名与返回形状逐字相同，所以「控制面已配置」与否只改变操作在哪个进程执行，
不改变页面看到的结果。

与 Assignment 的分工：Assignment 是一次性工作单元（提交 → 排队 → 派单 → 执行 → 回报），
适合一个 run；交互通道是**会话**——一条会话内的多次动作共享同一条 ScreenLock lease，
单次往返不经数据库、不落表。两者并列，互不替代。

**会话租约不变量**（`autophone/application/device_console.py` 模块 docstring 列为不得回退）：
console 会话从 start 到 stop 持有同一条 lease，动作之间不放开设备。本模块不碰 lease，
lease 的取还全在 `DeviceConsoleManager` 里，所以这条不变量随操作一起搬到 rig 进程，
不会因为多了一层转发而被拆开。

预览帧（`/devices/{d}/screen.jpg`、`screen.mjpeg`、console `frame`）也走这条通道：
`preview_frame` 复用一条**每设备一条**的预览会话，不每帧新建。预览会话**不持 lease**
（今天的 `screen.jpg` 也不持），所以它不会挡住 run；它只是把帧序号与空闲回收收进一个
可观测的对象里。

Run 执行期间（本进程有活动 Stage，BL-227）：`StageGuard` 拒绝未绑定 Stage 的设备访问，
`preview_frame` 改为返回执行侧最近一帧（`source=executor`，带 `captured_at`、
`stale_seconds`、`run_id`），不访问设备；执行侧尚未取图时返回
`state=executor_frame_pending`。`device_status` 同时附带 `executor_preview` 摘要（不含图像）。
"""
from __future__ import annotations

import base64
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from autophone.rig.session_protocol import RECORDING_SESSION_OPS

# 通道认识的全部操作名。提交侧与执行侧共用这一份，拼错的操作名在两端都 fail-closed。
DEVICE_SESSION_OPS = frozenset({
    "device_status",
    "screen_holder",
    "system_location",
    "location_lock_status",
    "preview_frame",
    "console_device_status",
    "console_start",
    "console_view",
    "console_frame",
    "console_event",
    "console_heartbeat",
    "console_stop",
}) | RECORDING_SESSION_OPS

# 交互通道的 rig 侧 WebSocket 路径。控制面挂它、rig agent 连它，共用这一份常量。
DEVICE_SESSION_WS_PATH = "/api/control/device-session/ws"

# 预览会话空闲多久回收。它不持 lease，回收丢掉帧缓存与序号，不影响设备互斥。
PREVIEW_IDLE_TTL_S = 300.0
PREVIEW_MIN_INTERVAL_S = 0.12

# 提交侧等一次往返的上限。必须大于设备侧驱动自己的超时（UDS 15s），否则本地先超时、
# 设备那边动作照样落地，页面会以为没点上而重点一次。
REQUEST_TIMEOUT_S = 30.0


class DeviceSessionRefused(RuntimeError):
    """通道给出的结构化拒绝：带 HTTP 取值与稳定 code，两端逐字透传。"""

    def __init__(self, message: str, *, status: int = 409, code: str = "conflict") -> None:
        super().__init__(message)
        self.status = status
        self.code = code

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "code": self.code, "message": str(self)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DeviceSessionRefused":
        return cls(
            str(payload.get("message") or "device session refused"),
            status=int(payload.get("status") or 503),
            code=str(payload.get("code") or "device_session_error"),
        )


class DeviceSessions(Protocol):
    """提交侧只看得见这一个方法：对某台设备执行一个操作。"""

    def call(self, device: str, op: str, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class _PreviewSession:
    session_id: str
    device: str
    opened_at: float
    last_used_at: float
    frame_seq: int = 0
    frame_at: float = 0.0
    frame_data: bytes | None = None
    frame_gate: threading.Lock = field(default_factory=threading.Lock)


class DeviceSessionOps:
    """交互通道的操作本体，在**持有设备的那个进程**里执行。

    构造它的进程就是设备持有者：兼容路径下是 web 进程，控制面模式下是 rig agent。
    所有 lease 语义都在注入的 `console` 里，本类只做操作名分发与预览会话复用。
    """

    def __init__(
        self,
        *,
        console: Any,
        recordings: Any = None,
        device_aliases: dict[str, str] | None = None,
        human_holder: Callable[..., dict | None] | None = None,
        screenshot: Callable[[], bytes],
        status: Callable[[str], dict[str, Any]],
        target: Callable[[], dict[str, Any]],
        executor_preview: Callable[[], dict[str, Any] | None] | None = None,
        clock: Callable[[], float] = time.time,
        preview_idle_ttl_s: float = PREVIEW_IDLE_TTL_S,
        preview_min_interval_s: float = PREVIEW_MIN_INTERVAL_S,
    ) -> None:
        self.console = console
        self.recordings = recordings
        self.device_aliases = device_aliases
        self._human_holder = human_holder
        self._screenshot = screenshot
        self._status = status
        self._target = target
        # BL-227：返回 None 表示本进程没有活动 Stage（照常直接截图）；否则返回执行侧画面状态。
        self._executor_preview = executor_preview
        self.clock = clock
        self.preview_idle_ttl_s = preview_idle_ttl_s
        self.preview_min_interval_s = preview_min_interval_s
        self._previews: dict[str, _PreviewSession] = {}
        self._guard = threading.Lock()

    # ── 操作分发 ──────────────────────────────────────────────────────────────
    def call(self, device: str, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        device = str(device or "").strip()
        if not device:
            raise DeviceSessionRefused("device is required", status=422, code="validation")
        if self.device_aliases is not None:
            if device not in self.device_aliases:
                raise DeviceSessionRefused("device is not served by this Rig", status=409, code="device_mismatch")
            device = self.device_aliases[device]
        if op not in DEVICE_SESSION_OPS:
            raise DeviceSessionRefused(
                f"unknown device session op: {op}", status=422, code="unknown_op",
            )
        try:
            if op in RECORDING_SESSION_OPS:
                if self.recordings is None:
                    raise DeviceSessionRefused("recording executor unavailable", status=503, code="recording_unavailable")
                return self.recordings.call(device, op, dict(payload or {}))
            handler = getattr(self, f"_op_{op}")
            return handler(device, dict(payload or {}))
        except DeviceSessionRefused:
            raise
        except Exception as exc:  # noqa: BLE001 - 设备侧错误统一成一种线上形状
            # `DeviceConsoleError` 与 `DeviceApplicationError` 都在这里收敛：前者自带
            # status/code，原样透传；其余一律 503 device_error（拒绝，不猜测）。
            raise DeviceSessionRefused(
                str(exc),
                status=int(getattr(exc, "status", 503) or 503),
                code=str(getattr(exc, "code", "") or "device_error"),
            ) from exc

    def close_all(self, reason: str) -> dict[str, Any]:
        """会话终止路径：关闭 console 与录制会话，丢掉预览会话。

        rig 掉线（WebSocket 读循环退出）时必须走到这里，否则 lease 会留在 ScreenLock 里，
        没有任何进程再去还它。
        """
        try:
            closed = self.console.close_all(reason)
        finally:
            if self.recordings is not None:
                self.recordings.close_all(reason)
        with self._guard:
            previews = len(self._previews)
            self._previews.clear()
        return {"closed_sessions": closed, "closed_previews": previews, "reason": reason}

    # ── 预览会话 ──────────────────────────────────────────────────────────────
    def _preview(self, device: str) -> _PreviewSession:
        now = self.clock()
        with self._guard:
            for key, session in list(self._previews.items()):
                if now - session.last_used_at > self.preview_idle_ttl_s:
                    self._previews.pop(key, None)
            session = self._previews.get(device)
            if session is None:
                session = _PreviewSession(
                    session_id=f"pv-{secrets.token_hex(6)}",
                    device=device,
                    opened_at=now,
                    last_used_at=now,
                )
                self._previews[device] = session
            session.last_used_at = now
            return session

    # ── 操作实现 ──────────────────────────────────────────────────────────────
    def _location_target(self, device: str) -> dict[str, Any]:
        target = self._target()
        if device not in {target["device"], target["vm"]}:
            raise DeviceSessionRefused("device is not served by this Rig", code="device_mismatch")
        return target

    def _op_location_lock_status(self, device: str, payload: dict[str, Any]) -> dict[str, Any]:
        target = self._location_target(device)
        return self.console.lock.status(device=target["device"])

    def _op_system_location(self, device: str, payload: dict[str, Any]) -> dict[str, Any]:
        from autophone.device.upstream_location import LOCATION
        from autophone.device.location_transport import UDSSystemLocationTransport

        if payload.get("t") not in LOCATION | {"location_current"}:
            raise DeviceSessionRefused("unknown system location operation", status=422, code="validation")
        allowed_fields = {
            "t", "mode", "owner", "generation", "coordinate_system", "heartbeat_s",
            "watchdog_s", "on_timeout", "replace", "persist", "producer_sequence",
            "lat", "lon", "alt", "hacc", "vacc", "speed", "course", "timestamp", "paused",
        }
        if payload.keys() - allowed_fields:
            raise DeviceSessionRefused("unsupported location parameters", status=422, code="validation")
        target = self._location_target(device)
        lease = self.console.lock.try_lease(
            device=target["device"], owner_kind="run", owner_id="system-location",
        )
        if lease is None:
            raise DeviceSessionRefused("device is busy", code="location_device_locked")
        try:
            response = UDSSystemLocationTransport(target["socket"]).request(payload)
            return dict(response)
        finally:
            lease.release()

    def _op_screen_holder(self, device: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload:
            raise DeviceSessionRefused("screen_holder accepts no parameters", status=422, code="validation")
        if self._human_holder is None:
            raise DeviceSessionRefused("ScreenLock reader unavailable", status=503, code="screen_holder_unavailable")
        try:
            holder = self._human_holder(device=device)
        except Exception as exc:
            raise DeviceSessionRefused(
                "ScreenLock state could not be read", status=503, code="screen_holder_unavailable",
            ) from exc
        return {"device": device, "holder": holder}

    def _executor_view(self) -> dict[str, Any] | None:
        if self._executor_preview is None:
            return None
        view = self._executor_preview()
        return dict(view) if isinstance(view, dict) else None

    def _op_device_status(self, device: str, _payload: dict[str, Any]) -> dict[str, Any]:
        status = dict(self._status(device))
        status["target"] = self._target()
        executor = self._executor_view()
        if executor is not None:
            executor.pop("jpeg_b64", None)
            status["executor_preview"] = executor
            # 在线探针在 Run 期间被 StageGuard 拒绝（RunService 记 None）；执行侧最近帧足够新时，
            # 以它证明设备在应答 UDS。没有新帧时保持「未探测」，不判为离线。
            if status.get("online") is None:
                from autophone.device.executor_frames import ONLINE_FRESH_S

                fresh = (
                    executor.get("state") == "frame"
                    and float(executor.get("stale_seconds") or 0.0) <= ONLINE_FRESH_S
                )
                if fresh:
                    status["online"] = True
                    status["online_source"] = "executor_frame"
        return status

    def _op_preview_frame(self, device: str, _payload: dict[str, Any]) -> dict[str, Any]:
        session = self._preview(device)
        executor = self._executor_view()
        if executor is not None:
            # Run 持有 Stage：不访问设备，只转交执行侧最近帧或等待状态（BL-227）。
            return {"preview_session_id": session.session_id, **executor}
        with session.frame_gate:
            if session.frame_data is None or self.clock() - session.frame_at >= self.preview_min_interval_s:
                try:
                    data = self._screenshot()
                except Exception as exc:  # noqa: BLE001 - 设备侧任何失败都是一个 503
                    raise DeviceSessionRefused(str(exc), status=503, code="device_error") from exc
                session.frame_data = data
                session.frame_at = self.clock()
                session.frame_seq += 1
            return {
                "preview_session_id": session.session_id,
                "seq": session.frame_seq,
                "captured_at": session.frame_at,
                "jpeg_b64": base64.b64encode(session.frame_data).decode("ascii"),
            }

    def _op_console_device_status(self, device: str, _payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.console.device_status(device))

    def _op_console_start(self, device: str, payload: dict[str, Any]) -> dict[str, Any]:
        # `request_id` 是创建幂等的键（BL-107 / 设计文档 §8.2）：同一 ID 并发或重试
        # 拿回同一条会话，不二次占锁。提交侧不给就退回「无跨调用幂等」的旧行为。
        return dict(self.console.start(device, request_id=payload.get("request_id")))

    def _op_console_view(self, device: str, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.console.view(_session_id(payload), device=device))

    def _op_console_frame(self, device: str, payload: dict[str, Any]) -> dict[str, Any]:
        data, seq, captured_at = self.console.frame(_session_id(payload), device=device)
        return {
            "seq": int(seq),
            "captured_at": float(captured_at),
            "jpeg_b64": base64.b64encode(data).decode("ascii"),
        }

    def _op_console_event(self, device: str, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.console.event(
            _session_id(payload),
            dict(payload.get("event") or {}),
            payload.get("controller_token"),
            device=device,
        ))

    def _op_console_heartbeat(self, device: str, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self.console.heartbeat(
            _session_id(payload), payload.get("controller_token"), device=device,
        ))

    def _op_console_stop(self, device: str, payload: dict[str, Any]) -> dict[str, Any]:
        # `reason` 让到期与撤销（设计文档 §7.2 第 5 条）复用同一条关闭路径，
        # 缺省仍是用户点「结束操作」的 `stopped`。
        return dict(self.console.stop(
            _session_id(payload), payload.get("controller_token"), device=device,
            reason=str(payload.get("reason") or "stopped"),
        ))


def _session_id(payload: dict[str, Any]) -> str:
    value = str(payload.get("session_id") or "").strip()
    if not value:
        raise DeviceSessionRefused("session_id is required", status=422, code="validation")
    return value


class RemoteDeviceSessions:
    """提交侧客户端：把一个操作 POST 给控制面，由它转发到持设备的 rig。

    只用标准库 urllib：这条路径要在 web 进程的请求线程里同步跑完，加一个 HTTP 客户端
    依赖换不到任何东西。
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = REQUEST_TIMEOUT_S,
        opener: Callable[[urllib.request.Request, float], Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._opener = opener or (lambda request, timeout: urllib.request.urlopen(request, timeout=timeout))

    def call(self, device: str, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        if op not in DEVICE_SESSION_OPS:
            raise DeviceSessionRefused(
                f"unknown device session op: {op}", status=422, code="unknown_op",
            )
        body = json.dumps({"op": op, "payload": dict(payload or {})}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/control/device-sessions/{device}/ops",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, self.timeout_s) as response:
                document = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise _refused_from_http_error(exc) from exc
        except Exception as exc:  # noqa: BLE001 - 控制面不可达是一个 503
            raise DeviceSessionRefused(
                f"control plane device session channel unreachable: {exc}",
                status=503,
                code="device_session_unavailable",
            ) from exc
        if op == "screen_holder" and (
            not isinstance(document, dict) or document.get("ok") is not True
            or not isinstance(document.get("result"), dict)
        ):
            raise DeviceSessionRefused(
                "invalid screen_holder response", status=503, code="screen_holder_unavailable",
            )
        result = document.get("result")
        return dict(result) if isinstance(result, dict) else {}


def read_human_holder_for_device(*, device: str, local_human_holder: Callable[..., dict | None]) -> dict | None:
    """Select the authoritative lock reader without a failure fallback."""
    from autophone.application.control_plane import configured_control_plane

    control = configured_control_plane()
    if control is None:
        return local_human_holder(device=device)
    return read_remote_human_holder(control, device=device)


def read_remote_human_holder(control: Any, *, device: str) -> dict | None:
    """Query the registered Rig lock; unknown responses never mean unoccupied."""
    from autophone.device.lock import HUMAN_OWNER_KINDS

    try:
        rig_id = control.resolve_device_rig(device)
        if not isinstance(rig_id, str) or not rig_id:
            raise ValueError("registered Rig ID is missing")
    except Exception as exc:
        raise DeviceSessionRefused(
            f"ScreenLock target could not be resolved: {exc}", status=503, code="screen_holder_unavailable",
        ) from exc
    response = RemoteDeviceSessions(control_http_base_url()).call(rig_id, "screen_holder", {})
    if not isinstance(response, dict):
        raise DeviceSessionRefused("invalid screen_holder response", status=503, code="screen_holder_unavailable")
    holder = response.get("holder")
    valid = set(response) == {"device", "holder"} and response["device"] == rig_id
    if holder is not None:
        valid = valid and isinstance(holder, dict)
        if valid:
            valid = (holder.get("device") == rig_id and holder.get("locked") is True
                     and holder.get("owner_kind") in HUMAN_OWNER_KINDS
                     and isinstance(holder.get("owner_id"), str) and bool(holder["owner_id"])
                     and type(holder.get("pid")) is int and holder["pid"] > 0)
    if not valid:
        raise DeviceSessionRefused(
            "invalid screen_holder response", status=503, code="screen_holder_unavailable",
        )
    return holder


def _refused_from_http_error(exc: urllib.error.HTTPError) -> DeviceSessionRefused:
    try:
        detail = json.loads(exc.read().decode("utf-8")).get("detail")
    except Exception:  # noqa: BLE001 - 读不出正文就退回状态码本身
        detail = None
    if isinstance(detail, dict):
        return DeviceSessionRefused(
            str(detail.get("message") or exc.reason or "device session refused"),
            status=int(exc.code),
            code=str(detail.get("code") or "device_session_error"),
        )
    return DeviceSessionRefused(
        str(detail or exc.reason or "device session refused"),
        status=int(exc.code),
        code="device_session_error",
    )


def control_http_base_url(env: dict[str, str] | None = None) -> str:
    """控制面 HTTP 基址：显式 env 优先，其次由 Agent WebSocket 地址推导。

    `AUTOPHONE_CONTROL_WS` 是 `start_vphone.sh` 写进 `control-plane.env` 的两键之一
    （CLAUDE.md「Control Plane」段），形如 `ws://127.0.0.1:8100/api/control/agent/ws`。
    交互通道与 Agent WebSocket 同属一个 `autophone-control` 进程，所以同一个 host:port。
    """
    import os
    from urllib.parse import urlsplit, urlunsplit

    source = env if env is not None else os.environ
    explicit = str(source.get("AUTOPHONE_CONTROL_HTTP") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    ws_url = str(source.get("AUTOPHONE_CONTROL_WS") or "").strip()
    if ws_url:
        parts = urlsplit(ws_url)
        scheme = "https" if parts.scheme == "wss" else "http"
        if parts.netloc:
            return urlunsplit((scheme, parts.netloc, "", "", "")).rstrip("/")
    return "http://127.0.0.1:8100"


__all__ = [
    "DEVICE_SESSION_OPS",
    "DEVICE_SESSION_WS_PATH",
    "DeviceSessionOps",
    "DeviceSessionRefused",
    "DeviceSessions",
    "RemoteDeviceSessions",
    "control_http_base_url",
]
