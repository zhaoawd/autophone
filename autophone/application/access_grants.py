"""授权记录（grant）的存放面（BL-104）。

对应 docs/platform-device-console-embed.md §四对象模型、§八状态机与 §十一 数据表。
本模块只管**一条授权记录怎么读写**：状态机的合法取值、三条互斥撞上时的错误分类、
两种存放位置（控制面 PostgreSQL 与进程内），不认识 HTTP、不认识设备、不做分配。

两种存放位置的分工与 §16.0 的兼容路径一致：

- 控制面已配置（`AUTOPHONE_DATABASE_URL` 有值）→ `ControlPlaneGrantStore`，
  记录写进 `access_grants` 表，web 进程重启后仍在；
- 控制面未配置 → `InMemoryGrantStore`，兼容路径，行为与 BL-103 的进程内授权表相同，
  进程重启即失效。这条**不是**降级：单 VM 兼容路径本来就没有控制面可写。
- 控制面已配置但连不上 → 抛 `ControlPlaneUnavailable`，不落回进程内（BL-026）。

三条互斥全部由存放层表达，不在应用层另加锁：

| 约束 | 契约 | `GrantConflict.kind` |
| --- | --- | --- |
| `request_id` 唯一 | 创建幂等（§8.2） | `request` |
| `uq_active_grant_per_device` | 设备互斥第二道（§十一），只管 `mode=control`，覆盖 `closing` | `device` |
| `uq_control_grant_per_subject` | 一人一台（§7.4） | `subject` |

**踢出与撤销的事件（BL-116，2026-09-23 裁定②）**：`access_grants` 行上的
`closed_reason` 只保存当前值，同一条授权上的多次关闭操作（撤销、踢出、到期、重试）
由只追加的 `access_grant_events` 表记录。事件只能随 `update(..., event=...)` 与授权
记录的状态变更一起写入（同一事务 / 同一把锁），存放面不提供事件的更新与删除入口，
读取走 `list_events()`。

时间在本模块的边界上一律是 **epoch 秒的 float**；PG 侧的 TIMESTAMPTZ 转换关在
`ControlPlaneGrantStore` 里，调用方看不到两种时间表示。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

logger = logging.getLogger(__name__)

# §8.1 的四态。`creating` 与 `active` 是「占着设备且可用」，`closing` 是「占着设备但
# 已拒绝新动作」，三者都在设备互斥的部分唯一索引条件里。
GRANT_CREATING = "creating"
GRANT_ACTIVE = "active"
GRANT_CLOSING = "closing"
GRANT_CLOSED = "closed"
GRANT_STATUSES = (GRANT_CREATING, GRANT_ACTIVE, GRANT_CLOSING, GRANT_CLOSED)
# 「还占着设备」的状态集合，与 `uq_active_grant_per_device` 的状态条件逐字一致。
# 该索引另有 `mode = 'control'` 条件（2026-09-09 裁定），所以本集合单独用不构成
# 设备互斥：判互斥要同时看 `mode`。
GRANT_OPEN_STATUSES = (GRANT_CREATING, GRANT_ACTIVE, GRANT_CLOSING)
# 「凭证还能用」的状态集合，与 `uq_control_grant_per_subject` 的条件一致。
GRANT_USABLE_STATUSES = (GRANT_CREATING, GRANT_ACTIVE)


# 只追加事件表 `access_grant_events` 的事件类型（BL-116）。三类都走
# `DeviceAccessService.revoke_session` 的同一条关闭路径，按 `reason` 区分：
# B 端 `DELETE` 记 `revoked`，看板管理员踢出记 `kicked`，到期回收记 `expired`。
GRANT_EVENT_REVOKED = "revoked"
GRANT_EVENT_KICKED = "kicked"
GRANT_EVENT_EXPIRED = "expired"
GRANT_EVENT_KINDS = (GRANT_EVENT_REVOKED, GRANT_EVENT_KICKED, GRANT_EVENT_EXPIRED)


class GrantConflict(RuntimeError):
    """撞上三条互斥之一。`kind` 取 `request` / `device` / `subject` / `unknown`。"""

    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__(message or f"access grant conflict: {kind}")
        self.kind = kind


class GrantNotFound(RuntimeError):
    """按 grant_id 找不到记录。"""


@dataclass
class GrantRecord:
    """一条授权记录。字段名与 `access_grants` 的列名逐字一致（§十一）。"""

    grant_id: str
    request_id: str
    platform_id: str
    subject: str
    device: str
    mode: str
    status: str = GRANT_CREATING
    console_session_id: str | None = None
    controller_token: str | None = None
    expires_at: float = 0.0
    closed_at: float | None = None
    closed_reason: str | None = None
    error_reason: str | None = None
    created_at: float = field(default_factory=time.time)

    def is_open(self) -> bool:
        """还占着设备（含 `closing`）。"""
        return self.status in GRANT_OPEN_STATUSES

    def is_usable(self, now: float) -> bool:
        """凭证还能用：状态在 creating / active，且没过期。"""
        return self.status in GRANT_USABLE_STATUSES and self.expires_at > now

    def actor(self) -> str:
        """写进 `_REQUEST_ACTOR` 的身份串（§5.1）。"""
        return f"platform:{self.platform_id}/{self.subject}"

    def public(self) -> dict[str, Any]:
        """能发给浏览器与 B 端的字段。

        `controller_token` 不在其中——这是本类的核心约束（§四）：它只在 web 进程与
        PG 里存在，任何响应体、任何日志都不带它。
        """
        return {
            "grant_id": self.grant_id,
            "request_id": self.request_id,
            "platform": self.platform_id,
            "subject": self.subject,
            "device": self.device,
            "mode": self.mode,
            "status": self.status,
            "session_id": self.console_session_id,
            "expires_at": self.expires_at,
            "closed_reason": self.closed_reason,
            "error_reason": self.error_reason,
        }


@dataclass(frozen=True)
class GrantEventSpec:
    """调用方提交的一条事件：发生了什么、谁做的、为什么。

    其余字段（事件标识、授权快照、前后状态、发生时间）由存放面在写入时补齐，
    调用方不能指定，免得事件内容与同一事务里的授权记录对不上。
    """

    kind: str
    actor: str
    reason: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.kind not in GRANT_EVENT_KINDS:
            raise ValueError(f"unknown access grant event kind: {self.kind!r}")
        if not str(self.actor or "").strip():
            raise ValueError("access grant event actor is required")
        if not str(self.reason or "").strip():
            raise ValueError("access grant event reason is required")


@dataclass(frozen=True)
class GrantEvent:
    """`access_grant_events` 的一行。字段名与列名逐字一致。"""

    event_id: str
    grant_id: str
    kind: str
    actor: str
    reason: str
    platform_id: str
    subject: str
    device: str
    mode: str
    console_session_id: str | None
    from_status: str
    to_status: str
    details: Mapping[str, Any]
    created_at: float


class GrantStore(Protocol):
    """授权记录的存放面。两种实现（PG、进程内）方法名与语义逐字相同。

    事件表只追加：写入只能经 `update(..., event=...)`，与授权记录的变更同事务；
    读取经 `list_events()`；没有事件的更新与删除方法。
    """

    def create(self, record: GrantRecord) -> GrantRecord: ...

    def get(self, grant_id: str) -> GrantRecord | None: ...

    def find(
        self, *, request_id: str | None = None, console_session_id: str | None = None,
    ) -> GrantRecord | None: ...

    def list(
        self,
        *,
        statuses: Sequence[str] | None = None,
        platform_id: str | None = None,
        devices: Sequence[str] | None = None,
    ) -> list[GrantRecord]: ...

    def update(
        self, grant_id: str, *, event: GrantEventSpec | None = None, **changes: Any,
    ) -> GrantRecord: ...

    def list_events(
        self, *, grant_id: str | None = None, device: str | None = None,
    ) -> list[GrantEvent]: ...


def new_grant_id() -> str:
    return f"grt-{uuid.uuid4().hex[:16]}"


def new_grant_event_id() -> str:
    return f"gev-{uuid.uuid4().hex[:16]}"


class InMemoryGrantStore:
    """进程内实现（兼容路径）。三条互斥在本类里显式实现，与 PG 的索引同条件。"""

    def __init__(self) -> None:
        self._guard = threading.RLock()
        self._rows: dict[str, GrantRecord] = {}
        # 只追加：本类只在 `update()` 里 append，不提供改写或删除事件的入口。
        self._events: list[GrantEvent] = []

    def create(self, record: GrantRecord) -> GrantRecord:
        with self._guard:
            self._check_conflicts(record, skip_grant_id=None)
            stored = replace(record)
            self._rows[stored.grant_id] = stored
            return replace(stored)

    def get(self, grant_id: str) -> GrantRecord | None:
        with self._guard:
            found = self._rows.get(str(grant_id or ""))
            return replace(found) if found is not None else None

    def find(
        self, *, request_id: str | None = None, console_session_id: str | None = None,
    ) -> GrantRecord | None:
        if not request_id and not console_session_id:
            return None
        with self._guard:
            rows = [
                row for row in self._rows.values()
                if (not request_id or row.request_id == request_id)
                and (not console_session_id or row.console_session_id == console_session_id)
            ]
        if not rows:
            return None
        # 与 PG 侧同一条排序：control 优先，其次按创建时间。
        rows.sort(key=lambda row: (row.mode != "control", row.created_at))
        return replace(rows[0])

    def list(
        self,
        *,
        statuses: Sequence[str] | None = None,
        platform_id: str | None = None,
        devices: Sequence[str] | None = None,
    ) -> list[GrantRecord]:
        with self._guard:
            rows = [
                replace(row) for row in self._rows.values()
                if (statuses is None or row.status in statuses)
                and (platform_id is None or row.platform_id == platform_id)
                and (devices is None or row.device in devices)
            ]
        rows.sort(key=lambda row: row.created_at)
        return rows

    def update(
        self, grant_id: str, *, event: GrantEventSpec | None = None, **changes: Any,
    ) -> GrantRecord:
        if event is not None:
            event.validate()
        with self._guard:
            current = self._rows.get(str(grant_id or ""))
            if current is None:
                raise GrantNotFound(f"access grant not found: {grant_id}")
            updated = replace(current, **changes)
            if str(changes.get("status") or "") == GRANT_CLOSED and updated.closed_at is None:
                updated.closed_at = time.time()
            self._check_conflicts(updated, skip_grant_id=updated.grant_id)
            # 与 PG 实现的同事务语义对齐：校验全部通过后，行变更与事件追加在同一把锁里
            # 一起生效；任何一步抛错，两者都不落。
            if event is not None:
                self._events.append(_event_from(current, updated, event))
            self._rows[updated.grant_id] = updated
            return replace(updated)

    def list_events(
        self, *, grant_id: str | None = None, device: str | None = None,
    ) -> list[GrantEvent]:
        with self._guard:
            return [
                row for row in self._events
                if (grant_id is None or row.grant_id == grant_id)
                and (device is None or row.device == device)
            ]

    def _check_conflicts(self, record: GrantRecord, *, skip_grant_id: str | None) -> None:
        """调用方已持锁。条件与 §十一 的两条部分唯一索引 + `request_id` 唯一一致。"""
        for other in self._rows.values():
            if other.grant_id == skip_grant_id or other.grant_id == record.grant_id:
                continue
            if other.request_id == record.request_id:
                raise GrantConflict("request", f"request_id already used: {record.request_id}")
            if (
                record.mode == "control"
                and other.mode == "control"
                and record.is_open()
                and other.is_open()
                and other.device == record.device
            ):
                # 与 `uq_active_grant_per_device` 的 WHERE 逐字一致：只有 control
                # 授权互斥，view 授权可以与 control 授权、与别的 view 授权并存。
                raise GrantConflict("device", f"device already has an open control grant: {record.device}")
            if (
                record.mode == "control"
                and other.mode == "control"
                and record.status in GRANT_USABLE_STATUSES
                and other.status in GRANT_USABLE_STATUSES
                and other.platform_id == record.platform_id
                and other.subject == record.subject
            ):
                raise GrantConflict(
                    "subject",
                    f"subject already has a control grant: {record.platform_id}/{record.subject}",
                )


class ControlPlaneGrantStore:
    """控制面实现：记录落 `access_grants` 表。

    `control_factory` 返回一个 `ControlPlane`（`configured_control_plane()` 的返回值）。
    压成工厂而不是直接持有对象，是因为控制面可用性是每次调用都要重新判定的三态：
    未配置返回 None（本类不该被选中）、可用返回对象、已配置但连不上抛异常（BL-026）。
    """

    def __init__(self, control_factory: Callable[[], Any]) -> None:
        self._control_factory = control_factory

    def _control(self) -> Any:
        control = self._control_factory()
        if control is None:
            raise RuntimeError("control plane is not configured; grant store is unavailable")
        return control

    def create(self, record: GrantRecord) -> GrantRecord:
        try:
            row = self._control().create_access_grant({
                "grant_id": record.grant_id,
                "request_id": record.request_id,
                "platform_id": record.platform_id,
                "subject": record.subject,
                "device": record.device,
                "mode": record.mode,
                "status": record.status,
                "console_session_id": record.console_session_id,
                "controller_token": record.controller_token,
                "expires_at": _to_datetime(record.expires_at),
            })
        except Exception as exc:  # noqa: BLE001 - 唯一约束冲突翻成领域错误，其余原样
            raise _translate(exc) from exc
        return _from_row(row)

    def get(self, grant_id: str) -> GrantRecord | None:
        row = self._control().get_access_grant(str(grant_id or ""))
        return _from_row(row) if row else None

    def find(
        self, *, request_id: str | None = None, console_session_id: str | None = None,
    ) -> GrantRecord | None:
        if not request_id and not console_session_id:
            return None
        row = self._control().find_access_grant(
            request_id=request_id, console_session_id=console_session_id,
        )
        return _from_row(row) if row else None

    def list(
        self,
        *,
        statuses: Sequence[str] | None = None,
        platform_id: str | None = None,
        devices: Sequence[str] | None = None,
    ) -> list[GrantRecord]:
        rows = self._control().list_access_grants(
            statuses=list(statuses) if statuses else None,
            platform_id=platform_id,
            devices=list(devices) if devices else None,
        )
        return [_from_row(row) for row in rows]

    def update(
        self, grant_id: str, *, event: GrantEventSpec | None = None, **changes: Any,
    ) -> GrantRecord:
        payload: dict[str, Any] = dict(changes)
        if "expires_at" in payload:
            payload["expires_at"] = _to_datetime(payload["expires_at"])
        event_payload: dict[str, Any] | None = None
        if event is not None:
            event.validate()
            event_payload = {
                "event_id": new_grant_event_id(),
                "kind": event.kind,
                "actor": event.actor,
                "reason": event.reason,
                "details": dict(event.details),
            }
        try:
            row = self._control().update_access_grant(
                str(grant_id or ""), payload, event=event_payload,
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc) from exc
        return _from_row(row)

    def list_events(
        self, *, grant_id: str | None = None, device: str | None = None,
    ) -> list[GrantEvent]:
        rows = self._control().list_access_grant_events(grant_id=grant_id, device=device)
        return [_event_from_row(row) for row in rows]


class LazyGrantStore:
    """按调用时的控制面配置选实现：已配置走 PG，未配置走进程内。

    为什么不在装配时选一次：`serve` 的模块导入发生在 `control-plane.env` 被读进环境
    之前也可能之后，而 `configured_control_plane()` 本身就是每次调用判定的三态。
    每次调用重新判定，`serve` 与 CLI 两条入口的行为因此一致。进程内那份是**单例**，
    否则兼容路径下每次调用都换一张空表。
    """

    def __init__(
        self,
        control_factory: Callable[[], Any] | None = None,
        *,
        fallback: GrantStore | None = None,
    ) -> None:
        self._control_factory = control_factory
        self._fallback = fallback if fallback is not None else InMemoryGrantStore()

    def _delegate(self) -> GrantStore:
        factory = self._control_factory
        if factory is None:
            # 压进函数体：`autophone.control` 硬依赖 psycopg，只在 platform 依赖组里。
            from autophone.application.control_plane import configured_control_plane

            factory = configured_control_plane
        # 已配置但连不上时 `configured_control_plane()` 抛 `ControlPlaneUnavailable`，
        # 本方法不接——按 BL-026 由调用方拒绝执行，不落回进程内表。
        if factory() is None:
            return self._fallback
        return ControlPlaneGrantStore(factory)

    @property
    def backend(self) -> str:
        """当前实际使用的存放位置，供 `/api/access/*` 的诊断字段与日志用。"""
        return "control_plane" if isinstance(self._delegate(), ControlPlaneGrantStore) else "process"

    def create(self, record: GrantRecord) -> GrantRecord:
        return self._delegate().create(record)

    def get(self, grant_id: str) -> GrantRecord | None:
        return self._delegate().get(grant_id)

    def find(
        self, *, request_id: str | None = None, console_session_id: str | None = None,
    ) -> GrantRecord | None:
        return self._delegate().find(
            request_id=request_id, console_session_id=console_session_id,
        )

    def list(
        self,
        *,
        statuses: Sequence[str] | None = None,
        platform_id: str | None = None,
        devices: Sequence[str] | None = None,
    ) -> list[GrantRecord]:
        return self._delegate().list(
            statuses=statuses, platform_id=platform_id, devices=devices,
        )

    def update(
        self, grant_id: str, *, event: GrantEventSpec | None = None, **changes: Any,
    ) -> GrantRecord:
        return self._delegate().update(grant_id, event=event, **changes)

    def list_events(
        self, *, grant_id: str | None = None, device: str | None = None,
    ) -> list[GrantEvent]:
        return self._delegate().list_events(grant_id=grant_id, device=device)


def _translate(exc: Exception) -> Exception:
    """把控制面的唯一约束冲突翻成 `GrantConflict`，其余原样返回。"""
    kind = getattr(exc, "conflict", None)
    if isinstance(kind, str) and kind:
        return GrantConflict(kind, str(exc))
    return exc


def _to_datetime(value: Any) -> Any:
    """epoch 秒 → 带时区的 datetime；已是 datetime 或 None 时原样。"""
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def _to_epoch(value: Any) -> float | None:
    """PG 回来的 ISO 串 / datetime → epoch 秒。认不出来返回 None，不猜。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        logger.warning(f"access grant: could not parse a timestamp: {value!r}")
        return None


def _from_row(row: Mapping[str, Any]) -> GrantRecord:
    return GrantRecord(
        grant_id=str(row.get("grant_id") or ""),
        request_id=str(row.get("request_id") or ""),
        platform_id=str(row.get("platform_id") or ""),
        subject=str(row.get("subject") or ""),
        device=str(row.get("device") or ""),
        mode=str(row.get("mode") or "control"),
        status=str(row.get("status") or GRANT_CREATING),
        console_session_id=(str(row["console_session_id"]) if row.get("console_session_id") else None),
        controller_token=(str(row["controller_token"]) if row.get("controller_token") else None),
        expires_at=_to_epoch(row.get("expires_at")) or 0.0,
        closed_at=_to_epoch(row.get("closed_at")),
        closed_reason=(str(row["closed_reason"]) if row.get("closed_reason") else None),
        error_reason=(str(row["error_reason"]) if row.get("error_reason") else None),
        created_at=_to_epoch(row.get("created_at")) or 0.0,
    )


def _event_from(before: GrantRecord, after: GrantRecord, spec: GrantEventSpec) -> GrantEvent:
    """进程内实现用：按变更前后的授权记录补齐一条事件。"""
    return GrantEvent(
        event_id=new_grant_event_id(),
        grant_id=after.grant_id,
        kind=spec.kind,
        actor=str(spec.actor),
        reason=str(spec.reason),
        platform_id=after.platform_id,
        subject=after.subject,
        device=after.device,
        mode=after.mode,
        console_session_id=after.console_session_id,
        from_status=before.status,
        to_status=after.status,
        details=dict(spec.details),
        created_at=time.time(),
    )


def _event_from_row(row: Mapping[str, Any]) -> GrantEvent:
    return GrantEvent(
        event_id=str(row.get("event_id") or ""),
        grant_id=str(row.get("grant_id") or ""),
        kind=str(row.get("kind") or ""),
        actor=str(row.get("actor") or ""),
        reason=str(row.get("reason") or ""),
        platform_id=str(row.get("platform_id") or ""),
        subject=str(row.get("subject") or ""),
        device=str(row.get("device") or ""),
        mode=str(row.get("mode") or ""),
        console_session_id=(str(row["console_session_id"]) if row.get("console_session_id") else None),
        from_status=str(row.get("from_status") or ""),
        to_status=str(row.get("to_status") or ""),
        details=dict(row.get("details") or {}),
        created_at=_to_epoch(row.get("created_at")) or 0.0,
    )


def open_grants_by_device(store: GrantStore, devices: Iterable[str] | None = None) -> dict[str, GrantRecord]:
    """还占着设备的授权记录，按设备键索引。

    一台设备可以同时有一条 control 授权与多条 view 授权（2026-09-09 裁定），所以
    取值有歧义时**固定取 control 那条**：调用方问的是「这台设备被谁占着」。
    """
    rows = store.list(
        statuses=GRANT_OPEN_STATUSES,
        devices=list(devices) if devices is not None else None,
    )
    by_device: dict[str, GrantRecord] = {}
    for row in rows:
        current = by_device.get(row.device)
        if current is None or (row.mode == "control" and current.mode != "control"):
            by_device[row.device] = row
    return by_device


__all__ = [
    "ControlPlaneGrantStore",
    "GRANT_ACTIVE",
    "GRANT_CLOSED",
    "GRANT_CLOSING",
    "GRANT_CREATING",
    "GRANT_EVENT_EXPIRED",
    "GRANT_EVENT_KICKED",
    "GRANT_EVENT_KINDS",
    "GRANT_EVENT_REVOKED",
    "GRANT_OPEN_STATUSES",
    "GRANT_STATUSES",
    "GRANT_USABLE_STATUSES",
    "GrantConflict",
    "GrantEvent",
    "GrantEventSpec",
    "GrantNotFound",
    "GrantRecord",
    "GrantStore",
    "InMemoryGrantStore",
    "LazyGrantStore",
    "new_grant_event_id",
    "new_grant_id",
    "open_grants_by_device",
]
