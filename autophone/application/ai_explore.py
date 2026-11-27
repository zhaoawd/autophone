"""ai_explore.py — AI-3 探索 / 一次性提问（platform-ai-capability §3.3）。

写脚本不划算的一次性任务（roadmap §2.2「登录看下订单列表有没有某按钮」）：

- ``device=True``：经 vphone_mcp 给 agent 设备工具面（同 §3.2 verification）。**真驱动碰屏必经
  ``ScreenLock`` 串行**（§5.1 不变量，与 verification/regression 同一把锁），而非旧 ``ai ask`` 直
  ``invoke`` 的旁路——这正是 AI-2 ``service._run_device_executor`` 立的接缝，explore 这里直接持锁复用。
- ``device=False``：默认是纯文本推理（``tools=none``）。只有调用方显式声明 ``snapshot=True``
  才采集并外发本次当前屏；截图失败时 fail-closed，不允许静默改变为纯文本请求。
"""
from __future__ import annotations

import base64
import os
from pathlib import Path
from secrets import token_hex
from typing import Callable

from autophone.paths import STATE_ROOT
from typing import Any

from autophone.application.ai import (
    AiRequest,
    AiResult,
    CommandRunner,
    DEFAULT_TIMEOUT_S,
    _subprocess_runner,
    _subprocess_stream_runner,
    invoke,
    invoke_streaming,
    unmanaged_device_refusal_result,
)
from autophone.device.lock import ScreenLock
from autophone.util import now_iso as _now

from autophone.device.target import default_device

DEVICE = default_device()  # explore 默认设备名，与 service 装配同一命名

# 截图接缝：抓一张当前屏存到 dest，返回路径；无活 VM/失败返回 None。
ScreenshotFn = Callable[[Path], "Path | None"]


class SnapshotCaptureError(RuntimeError):
    """调用方已同意 snapshot，但当前屏无法取得；禁止静默降级请求语义。"""


def _default_screenshot(dest: Path) -> Path | None:
    try:
        from autophone.device.uds import request
        from autophone.execution.engine.actions import resolve_sock

        # 默认彩色（AI 识别命中率高于灰阶）；VPHONE_MCP_SCREENSHOT_COLOR=0 可切回灰阶。
        color = os.environ.get("VPHONE_MCP_SCREENSHOT_COLOR", "1").strip().lower() not in (
            "0", "false", "no", "off", "",
        )
        obj, _ = request({"t": "screenshot", "color": color}, sock_path=resolve_sock(), timeout=5)
        image = obj.get("image")
        if not image:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(base64.b64decode(image))
        return dest
    except Exception:  # noqa: BLE001 - 上层按 snapshot 是否显式请求决定 fail-closed
        return None


def explore(
    prompt: str,
    *,
    device: bool = False,
    provider: str | None = None,
    model: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    context_dirs: list[Path] | None = None,
    allow_raw: bool = False,
    snapshot: bool = False,
    runner: CommandRunner = _subprocess_runner,
    stream_runner: CommandRunner = _subprocess_stream_runner,
    lock: ScreenLock | None = None,
    device_name: str = DEVICE,
    screenshot: ScreenshotFn = _default_screenshot,
    shot_path: Path | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> AiResult:
    """一次性提问。device=True 经屏幕锁驱动真机；否则默认纯文本推理。

    ``snapshot=True`` 表示调用方已取得本次当前屏的明确同意：截图必须成功并进入请求，
    否则在触达 provider 前抛 ``SnapshotCaptureError``。device=True 且传了 on_event 时走
    stream-json 增量回调（执行过程可见）；否则跑完才回。
    """
    context_dirs = context_dirs or []

    if device:
        if snapshot:
            raise ValueError("snapshot consent is only valid for tools=none exploration")
        req = AiRequest(
            prompt=prompt, tools="device", provider=provider, model=model,
            timeout_s=timeout_s, context_dirs=context_dirs, allow_raw=allow_raw,
        )
        # 控制面已配置且无 StageGuard 时，在取 ScreenLock、起 CLI 之前拒绝（不直连 VPHONE_SOCK）
        refused = unmanaged_device_refusal_result(req)
        if refused is not None:
            return refused
        lock = lock or ScreenLock()
        # §5.1：tools=device 真驱动必经 ScreenLock execute phase，与 verification/regression 同一把锁串行。
        with lock.acquire(device=device_name, run_id=f"explore-{_now()}"):
            if on_event is not None:
                return invoke_streaming(req, on_event=on_event, runner=stream_runner)
            return invoke(req, runner=runner)

    images: list[Path] = []
    if snapshot:
        dest = shot_path or (
            STATE_ROOT / "runs" / ".ai" / "explore" / token_hex(8) / "current-screen.jpg"
        )
        shot = screenshot(dest)
        if shot is None:
            raise SnapshotCaptureError(
                "current-screen snapshot was explicitly requested but capture failed"
            )
        images = [shot]
    req = AiRequest(
        prompt=prompt, tools="none", images=images, provider=provider, model=model,
        timeout_s=timeout_s, context_dirs=context_dirs, allow_raw=allow_raw,
    )
    return invoke(req, runner=runner)
