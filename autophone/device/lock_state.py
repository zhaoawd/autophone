"""Pure text classification for device lock-screen state."""

from __future__ import annotations

import re

from autophone.logging import get_logger

LOG = get_logger(__name__)

LOGIN_MARKERS = ("账号密码登录", "图片验证码", "请输入密码", "请输入图片验证码")
CUSTOMER_SERVICE_MARKERS = ("在线客服", "智能客服", "人工客服", "客服中心")

_LOCK_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
_LOCK_HOUR_RE = re.compile(r"^\d{1,2}$")
_LOCK_DATE_RE = re.compile(r"\d{1,2}月\d{1,2}日|周[一二三四五六日天]")
# 充电时锁屏的日期行会暂时换成电量提示，与日期同位，按同一判据处理。
_LOCK_CHARGING_RE = re.compile(r"目前电量|正在充电|已充电")

# BL-233：OCR 会把锁屏时钟数字位上的 `0` 读成字母 `O`/`o`（2026-10-09 vm 实测
# `O7:54` 等 4 帧）。只在整段文本呈时钟形状时把这两个字母改回 `0`：`H:MM`/`HH:MM`，
# 或两位小时（熄屏淡显锁屏只读出小时）；至少含一个真实数字，归一后须为有效时间
# （小时 0～23、分钟 0～59），否则保留原文。`l`/`I`/`|`→`1`、`D`/`Q`→`0` 无实测样本，
# 不纳入。归一结果只用于时钟判据，登录、客服与日期判据仍用 OCR 原文。
_CLOCK_LOOKALIKE_DIGITS = str.maketrans({"O": "0", "o": "0"})
_CLOCK_SHAPE_RE = re.compile(r"^([0-9Oo]{1,2}):([0-9Oo]{2})$")
_HOUR_SHAPE_RE = re.compile(r"^[0-9Oo]{2}$")
_ASCII_DIGITS = frozenset("0123456789")


def _normalize_clock_text(text: str) -> str:
    """Map look-alike letters back to digits only inside a clock-shaped token."""
    stripped = text.strip()
    if not any(ch in _ASCII_DIGITS for ch in stripped):
        return stripped
    clock = _CLOCK_SHAPE_RE.match(stripped)
    if clock is not None:
        hour = clock.group(1).translate(_CLOCK_LOOKALIKE_DIGITS)
        minute = clock.group(2).translate(_CLOCK_LOOKALIKE_DIGITS)
        if 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59:
            normalized = f"{hour}:{minute}"
            if normalized != stripped:
                LOG.debug("锁屏时钟 OCR 文本归一：%r → %r", stripped, normalized)
            return normalized
        return stripped
    if _HOUR_SHAPE_RE.match(stripped):
        hour = stripped.translate(_CLOCK_LOOKALIKE_DIGITS)
        if 0 <= int(hour) <= 23:
            if hour != stripped:
                LOG.debug("锁屏小时 OCR 文本归一：%r → %r", stripped, hour)
            return hour
    return stripped


def looks_like_lockscreen(texts: list[str] | None) -> bool:
    """Return whether sparse OCR text identifies an iOS lock screen."""
    items = [t for t in (texts or []) if str(t).strip()]
    joined = "\n".join(items)
    if any(m in joined for m in LOGIN_MARKERS) or any(m in joined for m in CUSTOMER_SERVICE_MARKERS):
        return False
    clock_items = [_normalize_clock_text(str(t)) for t in items]
    has_time = any(_LOCK_TIME_RE.match(t) for t in clock_items)
    has_hour_only = any(
        _LOCK_HOUR_RE.match(t) and 0 <= int(t) <= 23
        for t in clock_items
    )
    has_date = any(
        _LOCK_DATE_RE.search(str(t)) or _LOCK_CHARGING_RE.search(str(t))
        for t in items
    )
    return (has_time or has_hour_only) and has_date and len(items) <= 5


__all__ = ["CUSTOMER_SERVICE_MARKERS", "LOGIN_MARKERS", "looks_like_lockscreen"]
