"""Append-only local execution journal for reconnect reconciliation."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


#: Rows that close an Attempt for replay purposes. ``assignment_interrupted`` is BL-240:
#: the Control Plane acknowledged that an unfinished Attempt was interrupted.
CLOSING_EVENTS = frozenset({"assignment_completed", "assignment_stopped", "assignment_interrupted"})


class AgentJournal:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def append(self, event: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        row = {"at": _now_iso(), "event": event, "payload": dict(payload)}
        encoded = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        return row

    def read(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        rows = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows

    def terminal_assignments(self) -> set[tuple[str, int]]:
        terminal: set[tuple[str, int]] = set()
        for row in self.read():
            if row.get("event") != "assignment_completed":
                continue
            payload = row.get("payload") or {}
            try:
                terminal.add((str(payload["assignment_id"]), int(payload["attempt_no"])))
            except (KeyError, TypeError, ValueError):
                continue
        return terminal

    def non_replayable_assignments(self) -> set[tuple[str, int]]:
        """Return Attempts that must not execute again after reconnect."""

        return self.replay_state()[0]

    def replay_state(self) -> tuple[set[tuple[str, int]], dict[tuple[str, int], int]]:
        """Return closed Attempts and Attempts that started without a closing record (BL-240).

        Closed: ``assignment_completed``, ``assignment_stopped`` or ``assignment_interrupted``
        (the Control Plane acknowledged the interruption report). Unfinished: an
        ``assignment_started`` row (mapped to its fencing) and no closing row — the Agent
        process that started the executor exited without recording an outcome.
        """

        closed: set[tuple[str, int]] = set()
        started: dict[tuple[str, int], int] = {}
        for row in self.read():
            event = row.get("event")
            if event not in CLOSING_EVENTS and event != "assignment_started":
                continue
            payload = row.get("payload") or {}
            try:
                key = (str(payload["assignment_id"]), int(payload["attempt_no"]))
            except (KeyError, TypeError, ValueError):
                continue
            if event == "assignment_started":
                try:
                    started[key] = int(payload["fencing"])
                except (KeyError, TypeError, ValueError):
                    started[key] = 0
            else:
                closed.add(key)
        unfinished = {key: fencing for key, fencing in started.items() if key not in closed}
        return closed, unfinished
