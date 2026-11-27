"""Host breaker state machine. Pure: the caller supplies the crash times and the clock.

阈值都是初始值，后续按 `control host anomalies report` 调整。
设计见 docs/platform-host-resource-admission.md §5。
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"
LOCKED = "locked"

CRASH_WINDOW = timedelta(minutes=30)
TRIP_CRASHES = 2
COOLDOWN = timedelta(minutes=30)
PROBE = timedelta(minutes=30)
LOCK_WINDOW = timedelta(hours=24)
LOCK_OPENS = 3
HALF_OPEN_LIMIT = 2


def concurrency_limit(state: str) -> int | None:
    """How many Rigs may hold a Lease; ``None`` leaves it to the resource decision."""
    if state in {OPEN, LOCKED}:
        return 1
    if state == HALF_OPEN:
        return HALF_OPEN_LIMIT
    return None


def next_state(
    state: str,
    since: datetime,
    *,
    crashes: Sequence[datetime],
    opens_in_lock_window: int,
    now: datetime,
    force_open: str | None = None,
) -> tuple[str, str] | None:
    """Return ``(new_state, reason)``, or ``None`` when nothing changes.

    ``crashes`` 只算进入当前状态之后的事故，一次事故不会被算两次。
    ``opens_in_lock_window`` 不含本次。
    """
    def open_or_lock(reason: str) -> tuple[str, str]:
        if opens_in_lock_window + 1 >= LOCK_OPENS:
            return LOCKED, f"{reason}; opened {opens_in_lock_window + 1} times in 24h"
        return OPEN, reason

    if state == LOCKED:
        return None
    current = [at for at in crashes if at >= since]
    if force_open is not None and state != OPEN:
        return open_or_lock(force_open)
    if state == CLOSED:
        recent = [at for at in current if now - at <= CRASH_WINDOW]
        if len(recent) >= TRIP_CRASHES:
            return open_or_lock(f"{len(recent)} crashes in {int(CRASH_WINDOW.total_seconds() / 60)} minutes")
        return None
    if state == OPEN:
        return (HALF_OPEN, "cooldown elapsed") if now - since >= COOLDOWN else None
    if state == HALF_OPEN:
        if current:
            return open_or_lock("crash while probing")
        return (CLOSED, "probe passed") if now - since >= PROBE else None
    raise ValueError(f"unknown breaker state: {state}")
