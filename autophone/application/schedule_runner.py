from __future__ import annotations

import copy
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from autophone.device.target import default_device

DEFAULT_DEVICE = default_device()

from autophone.paths import CODE_ROOT
from autophone.assets.policy import assert_assets_writable
from autophone.util import read_json, write_json_atomic

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = CODE_ROOT / "config" / "schedules.yaml"
TICK_SECONDS = 60


class SchedulerError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt.astimezone() if dt.tzinfo else dt.astimezone()


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schedules": []}
    import yaml

    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise SchedulerError(f"{path} must be a YAML object")
    schedules = doc.get("schedules") or []
    if not isinstance(schedules, list):
        raise SchedulerError(f"{path}: schedules must be a list")
    return doc


def load_schedules(config_path: Path = DEFAULT_CONFIG_PATH) -> list[dict[str, Any]]:
    doc = _load_yaml(config_path)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in doc.get("schedules") or []:
        if not isinstance(raw, dict):
            raise SchedulerError("schedule entry must be an object")
        sid = str(raw.get("id") or "").strip()
        if not sid:
            raise SchedulerError("schedule id is required")
        if sid in seen:
            raise SchedulerError(f"duplicate schedule id: {sid}")
        seen.add(sid)
        if not str(raw.get("cron") or "").strip():
            raise SchedulerError(f"schedule {sid}: cron is required")
        spec = raw.get("spec") or {}
        if not isinstance(spec, dict):
            raise SchedulerError(f"schedule {sid}: spec must be an object")
        item = dict(raw)
        item["id"] = sid
        item["enabled"] = bool(raw.get("enabled", True))
        item["spec"] = spec
        out.append(item)
    return out


def _cron_next(cron: str, base: datetime) -> datetime:
    try:
        from croniter import croniter
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency message path
        raise SchedulerError("croniter is required; install with `uv sync --group platform`") from exc
    return croniter(cron, base).get_next(datetime).astimezone()


def _read_state(path: Path) -> dict[str, Any]:
    state = read_json(path) or {}
    if not isinstance(state.get("schedules"), dict):
        state["schedules"] = {}
    return state


def _state_for(state: dict[str, Any], schedule_id: str) -> dict[str, Any]:
    schedules = state.setdefault("schedules", {})
    item = schedules.setdefault(schedule_id, {})
    if not isinstance(item, dict):
        schedules[schedule_id] = item = {}
    return item


def set_enabled(config_path: Path, schedule_id: str, enabled: bool) -> None:
    assert_assets_writable("update schedule enabled state", path=config_path)
    if not config_path.exists():
        raise SchedulerError(f"schedule config not found: {config_path}")
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = None
    start_indent = ""
    entry_re = re.compile(r"^(\s*)-\s+id:\s*(.+?)\s*$")
    for idx, line in enumerate(lines):
        match = entry_re.match(line.rstrip("\n"))
        if match and match.group(2).strip().strip("'\"") == schedule_id:
            start = idx
            start_indent = match.group(1)
            break
    if start is None:
        raise SchedulerError(f"schedule not found: {schedule_id}")
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        match = entry_re.match(lines[idx].rstrip("\n"))
        if match and match.group(1) == start_indent:
            end = idx
            break
    child_indent = f"{start_indent}  "
    enabled_line = f"{child_indent}enabled: {'true' if enabled else 'false'}\n"
    enabled_re = re.compile(rf"^{re.escape(child_indent)}enabled:\s*")
    for idx in range(start + 1, end):
        if enabled_re.match(lines[idx]):
            lines[idx] = enabled_line
            config_path.write_text("".join(lines), encoding="utf-8")
            return
    lines.insert(start + 1, enabled_line)
    config_path.write_text("".join(lines), encoding="utf-8")


class Scheduler:
    def __init__(
        self,
        service: Any,
        *,
        config_path: Path = DEFAULT_CONFIG_PATH,
        state_path: Path | None = None,
        online_fn: Callable[[str], bool] | None = None,
        notify_fn: Callable[[dict[str, Any]], None] | None = None,
        started_at: datetime | None = None,
    ) -> None:
        self.service = service
        self.config_path = config_path
        self.state_path = state_path or (Path(service.run_root).parent / "schedules" / "state.json")
        self.online_fn = online_fn or self._service_online
        self.notify_fn = notify_fn or self._log_notification
        self.started_at = started_at or _now()

    def list(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        current = now or _now()
        schedules = load_schedules(self.config_path)
        state = _read_state(self.state_path)
        changed = self._refresh_run_statuses(state, schedules)
        if changed:
            self._save_state(state)
        return [self._summary(schedule, state, current) for schedule in schedules]

    def tick(self, *, now: datetime | None = None, background: bool = True) -> list[dict[str, Any]]:
        current = now or _now()
        schedules = load_schedules(self.config_path)
        state = _read_state(self.state_path)
        self._refresh_run_statuses(state, schedules)
        last_tick = _parse_dt(state.get("last_tick_at"))
        base = max(dt for dt in (last_tick, self.started_at) if dt is not None)
        events: list[dict[str, Any]] = []
        for schedule in schedules:
            if not schedule.get("enabled", True):
                continue
            due = _cron_next(str(schedule["cron"]), base)
            if due <= current:
                events.append(self._fire(schedule, state, current, background=background, due_at=due))
        state["last_tick_at"] = _iso(current)
        self._save_state(state)
        return events

    def fire(self, schedule_id: str, *, now: datetime | None = None, background: bool = True) -> dict[str, Any]:
        current = now or _now()
        schedule = self._require_schedule(schedule_id)
        state = _read_state(self.state_path)
        self._refresh_run_statuses(state, [schedule])
        event = self._fire(schedule, state, current, background=background, due_at=None)
        self._save_state(state)
        return event

    def update_enabled(self, schedule_id: str, enabled: bool, *, now: datetime | None = None) -> dict[str, Any]:
        set_enabled(self.config_path, schedule_id, enabled)
        return next(item for item in self.list(now=now) if item["id"] == schedule_id)

    def _require_schedule(self, schedule_id: str) -> dict[str, Any]:
        for schedule in load_schedules(self.config_path):
            if schedule["id"] == schedule_id:
                return schedule
        raise SchedulerError(f"schedule not found: {schedule_id}")

    def _summary(self, schedule: dict[str, Any], state: dict[str, Any], now: datetime) -> dict[str, Any]:
        item = dict(schedule)
        item["next_fire"] = _iso(_cron_next(str(schedule["cron"]), now)) if item.get("enabled", True) else None
        item["state"] = dict(_state_for(state, str(schedule["id"])))
        return item

    def _fire(
        self,
        schedule: dict[str, Any],
        state: dict[str, Any],
        now: datetime,
        *,
        background: bool,
        due_at: datetime | None,
    ) -> dict[str, Any]:
        schedule_id = str(schedule["id"])
        item = _state_for(state, schedule_id)
        from autophone.application.control_plane import configured_control_plane
        from autophone.execution.dispatch import RunDispatcher, _TERMINAL_ASSIGNMENT_STATUSES
        control = configured_control_plane()
        last_assignment_id = item.get("last_assignment_id")
        if last_assignment_id and control is not None:
            assignment = control.get_assignment(last_assignment_id)
            if assignment["status"] not in _TERMINAL_ASSIGNMENT_STATUSES:
                return self._skip(item, schedule_id, now, "skipped_overlap", "previous assignment is still active", due_at)
        last_run_id = item.get("last_run_id")
        if last_run_id and self._is_run_active(str(last_run_id)):
            return self._skip(item, schedule_id, now, "skipped_overlap", "previous run is still active", due_at)

        spec = self._build_spec(schedule, now)
        device = str(spec.get("device") or DEFAULT_DEVICE)
        if control is None and not self.online_fn(device):
            event = self._skip(item, schedule_id, now, "skipped_offline", f"device offline: {device}", due_at)
            self.notify_fn(event)
            return event

        handle = RunDispatcher(self.service).submit(spec, background=background, pin_device=True)
        item.update(
            {
                "last_fired": _iso(now),
                "last_run_id": handle.run_id,
                "last_assignment_id": handle.result.get("assignment_id"),
                "last_status": handle.result.get("status") if getattr(handle, "result", None) else "queued",
                "last_skip_reason": None,
            }
        )
        event = {
            "schedule_id": schedule_id,
            "status": "fired",
            "run_id": handle.run_id,
            "run_status": item["last_status"],
            **({"assignment_id": item["last_assignment_id"]} if item.get("last_assignment_id") else {}),
            "run_dir": str(handle.run_dir),
            "due_at": _iso(due_at) if due_at else None,
        }
        item["last_event"] = event
        return event

    def _skip(
        self,
        item: dict[str, Any],
        schedule_id: str,
        now: datetime,
        status: str,
        reason: str,
        due_at: datetime | None,
    ) -> dict[str, Any]:
        event = {
            "schedule_id": schedule_id,
            "status": status,
            "reason": reason,
            "due_at": _iso(due_at) if due_at else None,
            "at": _iso(now),
        }
        item.update(
            {
                "last_fired": item.get("last_fired"),
                "last_status": status,
                "last_skip_reason": reason,
                "last_event": event,
            }
        )
        return event

    def _build_spec(self, schedule: dict[str, Any], now: datetime) -> dict[str, Any]:
        spec = copy.deepcopy(schedule.get("spec") or {})
        schedule_id = str(schedule["id"])
        spec.setdefault("schema_version", "v1")
        spec.setdefault("type", "regression")
        spec.setdefault("device", DEFAULT_DEVICE)
        spec.setdefault("run_id", f"{now.strftime('%Y%m%d-%H%M%S')}-{schedule_id}")
        if spec.get("type") == "regression":
            spec.setdefault("planes", {"ui": {"mode": "required"}})
        origin = dict(spec.get("origin") or {})
        origin["schedule_id"] = schedule_id
        spec["origin"] = origin
        spec.setdefault("requested_by", "scheduler")
        spec.setdefault("requested_at", _iso(now))
        return spec

    def _service_online(self, device: str) -> bool:
        checker = getattr(self.service, "_check_device_online", None)
        if callable(checker):
            return bool(checker())
        return bool(self.service.status(device=device).get("online"))

    def _is_run_active(self, run_id: str) -> bool:
        detail = self._run_detail(run_id)
        return bool((detail.get("active") or {}).get("status"))

    def _run_status(self, run_id: str) -> str | None:
        detail = self._run_detail(run_id)
        active = detail.get("active") or {}
        if active.get("status"):
            return str(active["status"])
        result = detail.get("result") or {}
        if result.get("status"):
            return str(result["status"])
        return None

    def _run_detail(self, run_id: str) -> dict[str, Any]:
        try:
            return self.service.detail(run_id)
        except Exception:  # noqa: BLE001
            return {}

    def _refresh_run_statuses(self, state: dict[str, Any], schedules: list[dict[str, Any]]) -> bool:
        changed = False
        for schedule in schedules:
            item = _state_for(state, str(schedule["id"]))
            run_id = item.get("last_run_id")
            if not run_id:
                continue
            if item.get("last_assignment_id"):
                from autophone.application.control_plane import require_control_plane
                assignment = require_control_plane().get_assignment(item["last_assignment_id"])
                status = str(assignment["status"])
            else:
                status = self._run_status(str(run_id))
            if status and item.get("last_status") != status:
                item["last_status"] = status
                changed = True
        return changed

    def _save_state(self, state: dict[str, Any]) -> None:
        write_json_atomic(self.state_path, state)

    @staticmethod
    def _log_notification(event: dict[str, Any]) -> None:
        logger.warning("schedule event: %s", event)
