"""交互通道的 rig 侧：会话循环（BL-100）。

这个循环与 `RigAgent` 的 Assignment 循环**并列**跑在同一个 rig agent 进程里，用自己的
一条 WebSocket。不复用 Assignment 那条：`WebSocketAgentTransport` 是一条加锁的同步
socket，控制面主动发来的消息在它的请求循环里会被丢弃（见
`autophone/control/device_session.py` 模块 docstring）。

循环形态：连上 → 报 hello（host/rig/generation + 本 rig 服务的设备键）→ 逐条收请求、
在本线程里执行、回结果。**串行执行**：一条会话同一时刻只允许一个在飞动作本来就是
device console 的不变量（模块 docstring 第 2 条），串行让它在通道层也自然成立，
不需要第二把锁。

**掉线即还锁。** 读循环因任何原因退出（对端关闭、网络断、控制面重启）都会走到
`ops.close_all()`，关闭本 rig 上的 console 与录制会话，并归还 ScreenLock lease。这是
「rig 掉线不泄漏 lease」的那条明确路径；心跳过期（`expire_heartbeats`）是它的兜底，
不是替代——心跳兜底要等 90 秒，掉线是当场可知的事实。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit

from autophone.application.device_session import (
    DEVICE_SESSION_WS_PATH,
    DeviceSessionRefused,
)
from autophone.logging import get_logger


log = get_logger(__name__)


class DeviceSessionExecutor(Protocol):
    """会话循环认识的执行面，就是 `DeviceSessionOps`。"""

    def call(self, device: str, op: str, payload: dict[str, Any]) -> dict[str, Any]: ...

    def close_all(self, reason: str) -> dict[str, Any]: ...


def device_session_ws_url(control_ws_url: str) -> str:
    """由 Agent WebSocket 地址推出交互通道地址：同一个进程，换一条路径。"""
    parts = urlsplit(str(control_ws_url or "").strip())
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"control websocket url is not absolute: {control_ws_url!r}")
    return urlunsplit((parts.scheme, parts.netloc, DEVICE_SESSION_WS_PATH, "", ""))


class DeviceSessionAgent:
    """一条会话循环：连上控制面、执行操作、掉线还锁、退避重连。"""

    def __init__(
        self,
        url: str,
        *,
        host_id: str,
        rig_id: str,
        rig_generation: int,
        devices: list[str],
        ops: DeviceSessionExecutor,
        connect: Callable[[str, float], Any] | None = None,
        open_timeout_s: float = 10.0,
        reconnect_delay_s: float = 1.0,
    ) -> None:
        self.url = url
        self.host_id = host_id
        self.rig_id = rig_id
        self.rig_generation = int(rig_generation)
        self.devices = sorted({str(item).strip() for item in devices if str(item).strip()})
        if not self.devices:
            raise ValueError("a device session agent must serve at least one device key")
        self.ops = ops
        self.open_timeout_s = open_timeout_s
        self.reconnect_delay_s = reconnect_delay_s
        self._connect = connect or _default_connect
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = threading.Event()

    # ── 生命周期 ──────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run_forever, name=f"device-session-{self.rig_id}", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - 一次连接失败不该杀掉循环
                log.warning(
                    "device session channel dropped: %s: %s", type(exc).__name__, exc,
                )
            finally:
                self.connected.clear()
                # 对端不在了：会话再也收不到 stop，lease 只能由本进程还。
                self._release_all("channel_disconnected")
            if self._stop.wait(self.reconnect_delay_s):
                return

    def run_once(self) -> None:
        socket = self._connect(self.url, self.open_timeout_s)
        try:
            socket.send(json.dumps({
                "type": "hello",
                "host_id": self.host_id,
                "rig_id": self.rig_id,
                "rig_generation": self.rig_generation,
                "devices": self.devices,
            }))
            ready = json.loads(socket.recv(timeout=self.open_timeout_s))
            if not isinstance(ready, Mapping) or ready.get("type") != "ready":
                raise RuntimeError(f"device session handshake rejected: {ready}")
            self.connected.set()
            log.info(
                "device session channel open: rig=%s devices=%s", self.rig_id, self.devices,
            )
            while not self._stop.is_set():
                try:
                    raw = socket.recv(timeout=1.0)
                except TimeoutError:
                    continue
                self._answer(socket, json.loads(raw))
        finally:
            self.connected.clear()
            try:
                socket.close()
            except Exception:  # noqa: BLE001 - 关不掉也要继续走还锁路径
                pass

    # ── 一次请求 ──────────────────────────────────────────────────────────────
    def _answer(self, socket: Any, message: Any) -> None:
        if not isinstance(message, Mapping) or message.get("type") != "request":
            return
        request_id = str(message.get("request_id") or "")
        device = str(message.get("device") or "")
        op = str(message.get("op") or "")
        payload = message.get("payload")
        started = time.perf_counter()
        try:
            result = self.ops.call(
                device, op, dict(payload) if isinstance(payload, Mapping) else {},
            )
            response: dict[str, Any] = {
                "type": "result", "request_id": request_id, "ok": True, "result": result,
            }
        except DeviceSessionRefused as exc:
            response = {
                "type": "result", "request_id": request_id, "ok": False,
                "error": exc.to_dict(),
            }
        except Exception as exc:  # noqa: BLE001 - 执行面异常也要有回执，不能挂住提交侧
            response = {
                "type": "result", "request_id": request_id, "ok": False,
                "error": {
                    "status": 503,
                    "code": "device_error",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            }
        log.debug(
            "device session op %s on %s: ok=%s %.1fms",
            op, device, response.get("ok"), (time.perf_counter() - started) * 1000,
        )
        socket.send(json.dumps(response, ensure_ascii=False))

    def _release_all(self, reason: str) -> None:
        try:
            receipt = self.ops.close_all(reason)
        except Exception as exc:  # noqa: BLE001 - 还锁失败必须留痕，不能静默
            log.error("device session lease release failed: %s: %s", type(exc).__name__, exc)
            return
        if receipt.get("closed_sessions"):
            log.info(
                "device session leases released: reason=%s sessions=%s",
                reason, receipt.get("closed_sessions"),
            )


def _default_connect(url: str, open_timeout_s: float) -> Any:
    from websockets.sync.client import connect

    return connect(url, open_timeout=open_timeout_s)


__all__ = [
    "DEVICE_SESSION_WS_PATH",
    "DeviceSessionAgent",
    "DeviceSessionExecutor",
    "device_session_ws_url",
]
