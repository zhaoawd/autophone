"""Control Plane owned scheduling and expired-Lease reconciliation loop."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from autophone.logging import get_logger

from .interface import SchedulingControl


logger = get_logger(__name__)

NotifyAssignment = Callable[[dict[str, Any]], Awaitable[bool]]
ConnectedRigs = Callable[[], Awaitable[set[tuple[str, str]]]]


class ControlScheduler:
    """Run the Scheduler and Lease reaper inside the Control Plane process."""

    def __init__(
        self,
        control: SchedulingControl,
        notify: NotifyAssignment,
        connected_rigs: ConnectedRigs,
        *,
        interval_s: float = 0.5,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("scheduler interval_s must be positive")
        self.control = control
        self.notify = notify
        self.connected_rigs = connected_rigs
        self.interval_s = interval_s
        self._stopped = asyncio.Event()
        # 每 interval_s 跑一轮，所以「还在等」只在原因变化时记一条，否则日志会被
        # 同一条 wait_reason 刷满，反而看不见派单决策。
        self._reported_wait_reasons: dict[str, str] = {}

    async def run_once(self) -> dict[str, Any]:
        expired: list[str] = []
        assigned: list[str] = []
        while True:
            connected = await self.connected_rigs()
            result = await asyncio.to_thread(
                self.control.reconcile_scheduling,
                connected_rigs=connected,
            )
            expired.extend(result.expired)
            if result.expired:
                logger.info("reaped %d expired Rig Lease(s)", len(result.expired))
            assignment = result.assignment
            if not assignment or assignment.get("status") != "assigned":
                self._log_waiting(assignment)
                break
            assignment_id = str(assignment["assignment_id"])
            self._reported_wait_reasons.pop(assignment_id, None)
            logger.info(
                "event=assignment_dispatched assignment_id=%s host_id=%s rig_id=%s "
                "attempt=%s fencing=%s",
                assignment_id,
                assignment.get("bound_host_id"),
                assignment.get("bound_rig_id"),
                assignment.get("current_attempt_no"),
                assignment.get("current_fencing"),
            )
            assigned.append(assignment_id)
            await self.notify(assignment)
        return {"expired": expired, "assigned": assigned}

    def _log_waiting(self, assignment: dict[str, Any] | None) -> None:
        if not assignment:
            return
        assignment_id = str(assignment.get("assignment_id") or "")
        reason = str(assignment.get("wait_reason") or "unknown")
        if not assignment_id or self._reported_wait_reasons.get(assignment_id) == reason:
            return
        self._reported_wait_reasons[assignment_id] = reason
        logger.info("assignment %s still queued: wait_reason=%s", assignment_id, reason)

    async def run_forever(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - the process loop must survive transient DB errors
                logger.exception("Control Scheduler iteration failed")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.interval_s)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stopped.set()
