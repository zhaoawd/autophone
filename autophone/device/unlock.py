"""Shared lock-screen detection and bounded unlock for device execution paths."""

from __future__ import annotations

import base64
import time

from collections.abc import Callable
from pathlib import Path
from typing import Any

from autophone.logging import get_logger

LOG = get_logger(__name__)

#: ``(locked, evidence)``：``locked`` 为 None 表示原生状态不可用，evidence 写原因。
#: 回归执行链传入 ``autophone.observation.vphone_native_state.native_lock_state``。
LockProbe = Callable[[], tuple[bool | None, str]]


def ensure_unlocked(
    shot_dir: Path,
    *,
    request_fn: Callable[..., Any] | None = None,
    sock_path: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    lock_probe: LockProbe | None = None,
) -> str | None:
    """Detect the iOS lock screen, press Home at most twice, and verify exit.

    ``None`` means that the screen was already unlocked or that a bounded Home
    sequence left the lock screen. A string is a fail-closed blocker suitable
    for both regression execution and delivery state preparation.

    ``lock_probe`` 给出原生锁屏状态时以它为判据，按 Home 后也用它复核；屏幕熄灭时
    UDS 截图会停在旧帧，截图 OCR 不能区分。原生状态不可用（或未提供 ``lock_probe``）
    时沿用截图 OCR 判据，阻断说明写明判据来源。
    """
    from autophone.device.ocr_target import run_ocr
    from autophone.device.uds import request
    from autophone.device.lock_state import looks_like_lockscreen
    from autophone.device.shell import resolve_sock

    selected_request = request_fn or request
    selected_sock = sock_path or resolve_sock()
    out = shot_dir / "unlock"
    out.mkdir(parents=True, exist_ok=True)

    def _screen_texts(tag: str) -> list[str] | None:
        try:
            obj, _ = selected_request(
                {"t": "screenshot"},
                sock_path=selected_sock,
                timeout=5,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.error("解锁前截屏失败：%s: %s", type(exc).__name__, exc)
            return None
        image = obj.get("image")
        if not image:
            LOG.error("解锁前截屏无图像：%s", obj.get("error"))
            return None
        shot = out / f"{tag}.jpg"
        try:
            shot.write_bytes(base64.b64decode(image))
        except (OSError, ValueError) as exc:
            LOG.error("解锁截屏落盘失败：%s: %s", type(exc).__name__, exc)
            return None
        ocr = run_ocr(shot)
        return [
            str(box.get("text") or "")
            for box in ocr.get("boxes", [])
            if box.get("text")
        ]

    def _needs_home(texts: list[str]) -> bool:
        joined = "\n".join(texts)
        weekly_notification = (
            "周报可供查看" in joined and "日均屏幕时间" in joined
            and "继续接收" in joined and "关闭" in joined
        )
        return looks_like_lockscreen(texts) or weekly_notification

    def _native() -> tuple[bool | None, str]:
        if lock_probe is None:
            return None, "native lock state not configured"
        try:
            return lock_probe()
        except Exception as exc:  # noqa: BLE001 - unknown state, fall back to OCR
            return None, f"native lock state unavailable: {type(exc).__name__}: {exc}"

    locked, native_evidence = _native()
    if locked is False:
        LOG.info("未锁屏（判据：原生 %s）", native_evidence)
        return None
    if locked is None:
        source = f"截图 OCR；原生锁屏状态不可用：{native_evidence}"
        if lock_probe is not None:
            LOG.warning("原生锁屏状态不可用，回退截图 OCR 判据：%s", native_evidence)
        texts = _screen_texts("before")
        if texts is None:
            return "unlock failed: 无法截屏判断锁屏状态（设备/镜像不可达）"
        if not _needs_home(texts):
            LOG.info("未检测到锁屏（判据：%s）", source)
            return None
        LOG.info("检测到疑似锁屏（判据：%s），按 Home 键解锁", source)
    else:
        source = f"原生 {native_evidence}"
        LOG.info("检测到锁屏（判据：%s），按 Home 键解锁", source)
    for attempt in range(1, 3):
        try:
            response, _ = selected_request(
                {"t": "key", "name": "home"},
                sock_path=selected_sock,
                timeout=5,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.error("Home 键解锁失败：%s: %s", type(exc).__name__, exc)
            return f"unlock failed: home key error: {type(exc).__name__}: {exc}"
        if not response.get("ok"):
            LOG.error("Home 键被拒：%s", response.get("error"))
            return f"unlock failed: home key rejected: {response.get('error')}"
        sleep(1.0)
        if lock_probe is not None:
            locked, native_evidence = _native()
            if locked is False:
                LOG.info("解锁成功（Home 键 ×%d 后原生 %s）", attempt, native_evidence)
                return None
            if locked is True:
                source = f"原生 {native_evidence}"
                continue
            source = f"截图 OCR；原生锁屏状态不可用：{native_evidence}"
            LOG.warning("Home 后原生锁屏状态不可用，本次按截图 OCR 复核：%s", native_evidence)
        texts = _screen_texts(f"after{attempt}")
        if texts is None:
            return "unlock failed: 解锁后无法截屏校验"
        if not _needs_home(texts):
            LOG.info("解锁成功（Home 键 ×%d 后离开锁屏，判据：%s）", attempt, source)
            return None
    LOG.warning("按 Home 后仍停在锁屏，疑似设置了 passcode（判据：%s）", source)
    detail = "device locked: 按 Home 后仍停在锁屏（可能需要 passcode），请手动解锁后重试"
    if lock_probe is None:
        return detail
    return f"{detail}（判据：{source}）"


__all__ = ["LockProbe", "ensure_unlocked"]
