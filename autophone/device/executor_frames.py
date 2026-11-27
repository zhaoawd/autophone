"""执行侧最近帧（BL-227）。

Run 执行期间，本进程存在活动 Stage 作用域；`StageGuard.for_device_call` 拒绝未绑定
Stage 的线程访问设备（BL-076），所以预览通道不能再自己截图。执行器（已绑定 Stage 的
线程，含后台录屏 producer）照常经 `autophone.device.uds.request` 取得设备画面，本模块在
该调用返回后记下「最近一帧」，预览通道返回这一帧而不访问设备。

- 写入点只有 `uds.request`：执行侧所有带图的 UDS 响应（截图、录屏帧、带 `screen` 的动作
  回包）都经过它，因此不新增设备访问；记录只保存响应里已有的 base64 字符串引用，
  不解码、不落盘。频率等于执行侧取图的频率（默认录屏 `AUTOPHONE_RECORD_FPS=4`）。
- 只记录已绑定 Stage 的调用；按 socket 实际路径分键，每个设备只保留一帧，内存上限是
  每设备一帧 JPEG（约 40～60 KB 的 base64 字符串）。
- 帧带作用域身份（StageGuard authority）、Stage owner、Rig、Stage 名与 run_id。作用域退出后
  该帧不再返回，避免把上一次 Run 的画面当成当前画面。
- 记录在进程内存里：预览通道与执行器必须在同一进程（控制面模式下是 Rig Agent 进程）。
  跨进程（例如兼容路径把 Run 放进子进程）时本进程没有活动 Stage，预览照常直接截图。
"""
from __future__ import annotations

import itertools
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from autophone.device.stage_guard import StageGuard

#: 预览帧来源标记，HTTP 响应头 `X-Frame-Source` 与状态字段沿用同一取值。
FRAME_SOURCE = "executor"
#: 无最近帧时预览通道返回的状态码（不是 `device_error`）。
FRAME_PENDING = "executor_frame_pending"
#: 最近帧不超过该秒数时，设备状态的 `online` 以该帧为依据记为 True。
ONLINE_FRESH_S = 10.0


@dataclass(frozen=True)
class ExecutorFrame:
    socket: str
    image_b64: str
    captured_at: float
    seq: int
    authority_id: str
    owner_id: str
    rig_id: str
    stage: str
    run_id: str
    command: str


_LOCK = threading.Lock()
_FRAMES: dict[str, ExecutorFrame] = {}
_SEQ = itertools.count(1)


def _socket_key(sock_path: Any) -> str:
    return os.path.realpath(os.fspath(sock_path))


def record_response(
    command: str,
    response: Any,
    *,
    sock_path: Any,
    clock: Callable[[], float] = time.time,
) -> ExecutorFrame | None:
    """在已绑定 Stage 的调用返回后记下响应里的画面；未绑定或无图时不记录。"""
    binding = StageGuard.binding()
    if binding is None:
        return None
    if not isinstance(response, Mapping) or response.get("ok") is not True:
        return None
    image = response.get("image")
    if not isinstance(image, str) or not image:
        return None
    from autophone.logging import current_run_id

    token = binding.token
    frame = ExecutorFrame(
        socket=_socket_key(sock_path),
        image_b64=image,
        captured_at=float(clock()),
        seq=next(_SEQ),
        authority_id=binding.guard.authority_id,
        owner_id=token.owner_id,
        rig_id=token.rig_id,
        stage=token.stage,
        run_id=current_run_id(),
        command=str(command or ""),
    )
    with _LOCK:
        _FRAMES[frame.socket] = frame
    return frame


def latest(sock_path: Any) -> ExecutorFrame | None:
    """该设备最近一帧；记录它的 Stage 作用域已退出时返回 None。"""
    with _LOCK:
        frame = _FRAMES.get(_socket_key(sock_path))
    if frame is None or not StageGuard.authority_active(frame.authority_id):
        return None
    return frame


def preview_state(
    sock_path: Any,
    *,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any] | None:
    """预览通道的执行侧画面状态。

    本进程没有活动 Stage 时返回 None，调用方保持直接截图。有活动 Stage 时返回：
    ``state="frame"``（含 ``jpeg_b64``、``captured_at``、``stale_seconds``、``run_id``）或
    ``state=FRAME_PENDING``（执行侧尚未取图）。本函数不访问设备。
    """
    if not StageGuard.active_in_process():
        return None
    frame = latest(sock_path)
    if frame is None:
        return {"source": FRAME_SOURCE, "state": FRAME_PENDING}
    return {
        "source": FRAME_SOURCE,
        "state": "frame",
        "seq": frame.seq,
        "captured_at": frame.captured_at,
        "stale_seconds": round(max(0.0, float(clock()) - frame.captured_at), 3),
        "run_id": frame.run_id,
        "stage_owner": frame.owner_id,
        "stage": frame.stage,
        "rig_id": frame.rig_id,
        "jpeg_b64": frame.image_b64,
    }


def _reset_for_tests() -> None:
    with _LOCK:
        _FRAMES.clear()


__all__ = [
    "ExecutorFrame",
    "FRAME_PENDING",
    "FRAME_SOURCE",
    "ONLINE_FRESH_S",
    "latest",
    "preview_state",
    "record_response",
]
