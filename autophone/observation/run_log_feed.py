"""BL-075：Run 日志与结构化恢复事件的只读游标投影。

设计见 `docs/platform-vm-management-task-console.md` §4.1（RunLogFeed 模块边界与游标
语义）与 §5 / §6.2（自动恢复可见性消费的五类事件）。

本模块**只读**：不写日志、不触发恢复、不改任何状态。两个读取面：

1. `RunLogFeed`：按字节游标增量读一个 Run 的 `events.jsonl`（缺失时退回 `run.log`
   的纯文本行）。游标只在完整换行之后推进，文件尾的半行留到下次读取。
2. `recovery_projection()`：读 `scripts/lib/stage_events.sh` 落下的 JSONL 通道
   （阶段事件 + 五类恢复事件），投影成设备页只读区块要的状态。

游标语义（§4.1，fail-closed）：

| 情况 | 处置 |
| --- | --- |
| 游标 > 当前文件长度 | `status="cursor_invalid"`，调用方重取快照，不静默从头读 |
| 游标落在行中间 | 向下取整到上一个换行边界，`cursor_adjusted=True` 回报实际生效游标 |
| 文件比游标短（截断或轮转） | 同第一条：`status="cursor_invalid"` |
| 两份日志都缺失 | `status="no_logs"`，不返回空数组冒充正常 |
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

STATUS_OK = "ok"
STATUS_CURSOR_INVALID = "cursor_invalid"
STATUS_NO_LOGS = "no_logs"

SOURCE_EVENTS = "events.jsonl"
SOURCE_TEXT = "run.log"

# run_id 只允许出现在单个路径段里：不含分隔符、不是 `.` / `..`，因此拼不出受管根目录
# 之外的路径。解析出的目录仍会再做一次 run_root 包含性校验（双保险）。
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")

_BACKWARD_CHUNK = 64 * 1024


class RunLogFeedError(ValueError):
    """run_id 不合法，或解析出的 run 目录落在受管根目录之外。"""


@dataclass(frozen=True)
class LogPage:
    """一次读取的结果。`events` 之外的字段都是给调用方判定用的，不做展示。"""

    run_id: str
    status: str
    source: str
    cursor: int
    next_cursor: int
    size: int
    events: tuple[dict[str, Any], ...] = ()
    cursor_adjusted: bool = False
    has_more: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "source": self.source,
            "cursor": self.cursor,
            "next_cursor": self.next_cursor,
            "size": self.size,
            "events": list(self.events),
            "cursor_adjusted": self.cursor_adjusted,
            "has_more": self.has_more,
        }


def _floor_to_line_start(handle: Any, cursor: int) -> int:
    """把落在行中间的游标向下取整到上一个换行边界之后的位置。"""

    if cursor <= 0:
        return 0
    start = cursor
    while start > 0:
        window = min(_BACKWARD_CHUNK, start)
        start -= window
        handle.seek(start)
        chunk = handle.read(cursor - start)
        index = chunk.rfind(b"\n")
        if index >= 0:
            return start + index + 1
    return 0


def _decode_line(raw: bytes) -> dict[str, Any]:
    """一行 JSONL → 事件字典。损坏行单独标注，不让整页失效（§4.1）。"""

    text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"malformed": True, "message": text}
    if isinstance(parsed, dict):
        return parsed
    return {"malformed": True, "message": text}


def _text_line(raw: bytes) -> dict[str, Any]:
    """`run.log` 的一行。只装进 `message`，不从中文文案里反推 level 或 outcome。"""

    return {"message": raw.decode("utf-8", errors="replace").rstrip("\r\n")}


class RunLogFeed:
    """Run 日志的只读投影。

    `resolve_run_dir` 由调用方注入（HTTP 适配传 `RunService.queries.find_run_dir`），
    默认实现只在 `run_root` 下按日期分区 glob，测试不必拉起 RunService。
    """

    def __init__(
        self,
        run_root: Path | str,
        *,
        resolve_run_dir: Callable[[str], Path] | None = None,
    ) -> None:
        self.run_root = Path(run_root)
        self._resolve = resolve_run_dir or self._default_resolve

    # ---- 路径解析 ----

    def _default_resolve(self, run_id: str) -> Path:
        matches = sorted(
            [*self.run_root.glob(f"*/{run_id}"), *self.run_root.glob(f"*/{run_id}.attempt-*")],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not matches:
            raise FileNotFoundError(f"run not found: {run_id}")
        return matches[0]

    def run_dir(self, run_id: str) -> Path:
        if not _RUN_ID_RE.match(run_id or ""):
            raise RunLogFeedError(f"illegal run_id: {run_id!r}")
        run_dir = Path(self._resolve(run_id)).resolve()
        root = self.run_root.resolve()
        if not run_dir.is_relative_to(root):
            raise RunLogFeedError(f"run dir escapes the managed root: {run_dir}")
        return run_dir

    def _source_path(self, run_id: str, source: str | None) -> tuple[Path | None, str]:
        run_dir = self.run_dir(run_id)
        events = run_dir / SOURCE_EVENTS
        text = run_dir / SOURCE_TEXT
        if source == SOURCE_EVENTS:
            return (events, SOURCE_EVENTS) if events.is_file() else (None, "")
        if source == SOURCE_TEXT:
            return (text, SOURCE_TEXT) if text.is_file() else (None, "")
        if events.is_file():
            return events, SOURCE_EVENTS
        if text.is_file():
            return text, SOURCE_TEXT
        return None, ""

    # ---- 读取 ----

    def read(
        self,
        run_id: str,
        *,
        cursor: int | None = None,
        limit: int = 500,
        source: str | None = None,
    ) -> LogPage:
        """从 `cursor`（字节）之后增量读，最多 `limit` 条完整行。"""

        path, kind = self._source_path(run_id, source)
        if path is None:
            return LogPage(
                run_id=run_id, status=STATUS_NO_LOGS, source="", cursor=0, next_cursor=0, size=0
            )
        size = path.stat().st_size
        start = 0 if cursor is None else int(cursor)
        if start < 0 or start > size:
            # 越界与「文件比游标短」（截断 / 轮转）是同一处置：显式失效，不静默从头读。
            return LogPage(
                run_id=run_id,
                status=STATUS_CURSOR_INVALID,
                source=kind,
                cursor=start,
                next_cursor=size,
                size=size,
            )
        decode = _decode_line if kind == SOURCE_EVENTS else _text_line
        events: list[dict[str, Any]] = []
        with path.open("rb") as handle:
            effective = _floor_to_line_start(handle, start)
            handle.seek(effective)
            offset = effective
            while len(events) < limit:
                raw = handle.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    # 文件尾的半行不消费，游标停在它前面，下一次连同后续字节一起读。
                    break
                offset += len(raw)
                events.append(decode(raw))
        return LogPage(
            run_id=run_id,
            status=STATUS_OK,
            source=kind,
            cursor=effective,
            next_cursor=offset,
            size=size,
            events=tuple(events),
            cursor_adjusted=effective != start,
            has_more=offset < size,
        )

    def read_before(
        self,
        run_id: str,
        *,
        cursor: int,
        limit: int = 200,
        source: str | None = None,
    ) -> LogPage:
        """向前加载：返回结束位置不超过 `cursor` 的最后 `limit` 条完整行。"""

        path, kind = self._source_path(run_id, source)
        if path is None:
            return LogPage(
                run_id=run_id, status=STATUS_NO_LOGS, source="", cursor=0, next_cursor=0, size=0
            )
        size = path.stat().st_size
        end = int(cursor)
        if end < 0 or end > size:
            return LogPage(
                run_id=run_id,
                status=STATUS_CURSOR_INVALID,
                source=kind,
                cursor=end,
                next_cursor=size,
                size=size,
            )
        decode = _decode_line if kind == SOURCE_EVENTS else _text_line
        with path.open("rb") as handle:
            stop = _floor_to_line_start(handle, end)
            start = self._start_of_last_lines(handle, stop, limit)
            handle.seek(start)
            payload = handle.read(stop - start)
        events = [decode(line + b"\n") for line in payload.split(b"\n") if line.strip()]
        return LogPage(
            run_id=run_id,
            status=STATUS_OK,
            source=kind,
            cursor=start,
            next_cursor=stop,
            size=size,
            events=tuple(events),
            cursor_adjusted=stop != end,
            has_more=start > 0,
        )

    @staticmethod
    def _start_of_last_lines(handle: Any, stop: int, limit: int) -> int:
        """从 `stop` 往前数 `limit` 个换行，返回第 limit 行的起始偏移。"""

        if stop <= 0 or limit <= 0:
            return 0
        seen = 0
        position = stop
        while position > 0:
            window = min(_BACKWARD_CHUNK, position)
            position -= window
            handle.seek(position)
            chunk = handle.read(window)
            index = len(chunk)
            while index > 0:
                index = chunk.rfind(b"\n", 0, index)
                if index < 0:
                    break
                seen += 1
                if seen > limit:
                    return position + index + 1
        return 0


# ----------------------------------------------------------------------------
# 结构化恢复事件（§6.2）：设备页只读区块的唯一数据来源。
# ----------------------------------------------------------------------------

RECOVERY_EVENTS = (
    "down_detected",
    "guard_decision",
    "recovery_started",
    "recovery_succeeded",
    "recovery_failed",
)
STAGE_EVENTS = ("stage_start", "stage_end", "stage_error")

RECOVERY_STATUS_NONE = "none"
RECOVERY_STATUS_DISABLED = "disabled"
RECOVERY_STATUS_RECOVERING = "recovering"
RECOVERY_STATUS_LIMIT_REACHED = "limit_reached"
RECOVERY_STATUS_SUCCEEDED = "succeeded"
RECOVERY_STATUS_FAILED = "failed"


def recovery_event_dir(env: Mapping[str, str] | None = None) -> Path:
    """结构化恢复事件的目录。

    优先级与 `autophone/recovery_observation.py:observation_root` 一致，中间多一个
    `AUTOPHONE_RIG_RUNTIME_DIR/recovery-events`——那是 BL-074 落地时 `on_vphone_down.sh`
    的默认落点（`$RUNTIME_DIR/recovery-events/<YYYY-MM-DD>.jsonl`，§6.4）。
    已配置的高优先级路径具有权威性；路径不存在时仍返回该路径，由调用方报告空状态，
    避免回退到其他 VM 或历史默认目录。
    """

    environment = os.environ if env is None else env
    explicit = environment.get("AUTOPHONE_RECOVERY_EVENT_DIR")
    if explicit:
        return Path(explicit)
    logs = environment.get("AUTOPHONE_RIG_LOG_DIR") or environment.get("VPHONE_LOG_DIR")
    if logs:
        return Path(logs) / "recovery-events"
    runtime = environment.get("AUTOPHONE_RIG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "recovery-events"
    return Path(environment.get("HOME", str(Path.home()))) / ".autophone" / "recovery-events"


def read_recovery_events(
    env: Mapping[str, str] | None = None, *, limit: int = 400
) -> tuple[Path | None, list[dict[str, Any]]]:
    """读最新一个日期分区里的事件（阶段事件与恢复事件同通道，§6.4）。"""

    directory = recovery_event_dir(env)
    if not directory.is_dir():
        return None, []
    partitions = sorted(path for path in directory.glob("*.jsonl") if path.is_file())
    if not partitions:
        return None, []
    latest = partitions[-1]
    lines = latest.read_bytes().split(b"\n")
    events = [_decode_line(line) for line in lines if line.strip()]
    return latest, events[-limit:]


def first_stage_error(events: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """本次尝试窗口里的**首条** `stage_error`。

    BL-074 落地注登记的口径：授权失败会连发两条——先
    `stage_error{code:"authorization_required"}`，`die` 再补一条
    `stage_error{code:"startup_failed"}`。首条带的是可操作的原因，第二条只是退出路径的
    通用码，所以消费端一律取首条；不要取最后一条，也不要把两条都显示成两次失败。
    """

    for event in events:
        if event.get("event") == "stage_error":
            return dict(event)
    return None


def _window_from(events: Sequence[Mapping[str, Any]], anchor: str) -> list[dict[str, Any]]:
    """从最后一条 `anchor` 事件起的窗口；没有这条事件时返回全部事件。"""

    for index in range(len(events) - 1, -1, -1):
        if events[index].get("event") == anchor:
            return [dict(item) for item in events[index:]]
    return [dict(item) for item in events]


@dataclass
class _RecoveryState:
    decision: str | None = None
    used: int | None = None
    limit: int | None = None
    window_sec: int | None = None
    attempt: int | None = None
    detected_at: str | None = None
    diagnostics: str | None = None
    duration_s: Any = None
    health_state: str | None = None
    failed_stage: str | None = None
    exit_code: Any = None
    error: str | None = None
    status: str = RECOVERY_STATUS_NONE
    seen: list[str] = field(default_factory=list)


def recovery_projection(
    env: Mapping[str, str] | None = None, *, tail: int = 20
) -> dict[str, Any]:
    """设备页只读区块的投影（§5 的六个展示项）。

    只消费结构化事件；不解析 `down-alerts.log` / `autorestart.log` 的文案，也不使用
    `_probe_health_guard()` 的 `on_down` 代理值。没有事件时是 `status="none"`
    （「无恢复记录」），不是空的成功态。
    """

    environment = os.environ if env is None else env
    source, events = read_recovery_events(environment)
    enabled = str(environment.get("VPHONE_AUTORESTART", "1")) != "0"
    state = _RecoveryState()
    # 两级窗口：本次掉线事件（从最后一条 down_detected 起）决定展示什么；本次尝试
    # （从窗口内最后一条 recovery_started 起）决定 stage_error 取谁——上一轮尝试的
    # 失败码不该显示成本轮的原因。
    window = _window_from(events, "down_detected") if events else []
    attempt_window = _window_from(window, "recovery_started") if window else []
    for event in window:
        name = event.get("event")
        if name in RECOVERY_EVENTS or name in STAGE_EVENTS:
            state.seen.append(str(name))
        if name == "down_detected":
            state.detected_at = event.get("detected_at") or event.get("ts")
            state.diagnostics = event.get("diagnostics")
        elif name == "guard_decision":
            state.decision = event.get("decision")
            state.used = _as_int(event.get("used"))
            state.limit = _as_int(event.get("limit"))
            state.window_sec = _as_int(event.get("window_sec"))
            if state.decision == "skip-limit":
                state.status = RECOVERY_STATUS_LIMIT_REACHED
            elif state.decision == "disabled":
                state.status = RECOVERY_STATUS_DISABLED
        elif name == "recovery_started":
            state.attempt = _as_int(event.get("attempt"))
            state.limit = _as_int(event.get("limit")) or state.limit
            state.duration_s = None
            state.health_state = None
            state.failed_stage = None
            state.exit_code = None
            state.error = None
            state.status = RECOVERY_STATUS_RECOVERING
        elif name == "recovery_succeeded":
            state.attempt = _as_int(event.get("attempt")) or state.attempt
            state.duration_s = event.get("duration_s")
            state.health_state = event.get("health_state")
            state.status = RECOVERY_STATUS_SUCCEEDED
        elif name == "recovery_failed":
            state.attempt = _as_int(event.get("attempt")) or state.attempt
            state.failed_stage = event.get("stage")
            state.exit_code = event.get("exit_code")
            state.error = event.get("error")
            state.status = RECOVERY_STATUS_FAILED
    if not enabled and state.status == RECOVERY_STATUS_NONE:
        state.status = RECOVERY_STATUS_DISABLED
    stage_error = first_stage_error(attempt_window)
    return {
        "status": state.status,
        "enabled": enabled,
        "source": str(source) if source is not None else None,
        "decision": state.decision,
        "used": state.used,
        "limit": state.limit,
        "window_sec": state.window_sec,
        "attempt": state.attempt,
        "detected_at": state.detected_at,
        "diagnostics": state.diagnostics,
        "duration_s": state.duration_s,
        "health_state": state.health_state,
        "failed_stage": state.failed_stage or (stage_error or {}).get("stage"),
        "exit_code": state.exit_code,
        "error": state.error,
        "stage_error": stage_error,
        "events": _tail(window, tail),
    }


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _tail(events: Iterable[Mapping[str, Any]], count: int) -> list[dict[str, Any]]:
    items = [dict(event) for event in events]
    return items[-count:] if count > 0 else []
