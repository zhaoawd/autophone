"""PostgreSQL Adapter for Control State and deterministic scheduling."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Mapping, Sequence

from ._psycopg import (
    jsonb as Jsonb,
    psycopg_errors_by_name,
    require_psycopg,
    require_psycopg_components,
)

if TYPE_CHECKING:  # pragma: no cover
    import psycopg
    from psycopg import sql
    from psycopg.errors import UniqueViolation

from . import host_breaker
from .host_breaker import concurrency_limit
from .evidence import (
    capacity_targets_registered,
    evaluate_capacity_admission_review,
    evaluate_same_host_p0,
    evaluate_same_host_p0_capacity_preflight,
)

from autophone.logging import get_logger

log = get_logger(__name__)

_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_TERMINAL_ASSIGNMENTS = frozenset({"passed", "failed", "error", "blocked", "cancelled", "indeterminate"})
_TERMINAL_ATTEMPTS = frozenset({"passed", "failed", "error", "blocked", "cancelled", "indeterminate"})

#: Agent 在执行器之前拒单可用的原因。只列出 Agent 在任何设备操作之前判定的原因：
#: `rig_agent_stale`（本进程源码过期，BL-214）、`device_mismatch`（Run Spec 点名的设备不是
#: 本 rig，BL-217）、`vm_process_dead`（VM 进程已不在，执行前直接拒单）。
_DECLINE_REASONS = frozenset({"rig_agent_stale", "device_mismatch", "vm_process_dead"})

#: Agent 报告 Attempt 中断可用的原因（BL-240）：新进程重连时本地执行日志只有该 Attempt 的
#: ``assignment_started``，没有完成或停止记录，即前一个 Agent 进程在执行中退出。
_INTERRUPT_REASONS = frozenset({"agent_restarted_after_start"})

#: Lease 释放原因中表示「Attempt 被中断、没有 Assignment Result」的取值（BL-076、BL-240）。
INTERRUPTED_RELEASE_REASONS = frozenset({"lease_expired", "agent_interrupted"})

#: quiesce 未确认的 Assignment Result 原因（BL-220 剩余项），与 `autophone.rig.agent` 同值。
_QUIESCE_UNVERIFIED = "device_quiesce_unverified"


_RERUNNABLE_STATUSES = frozenset({"error", "failed", "indeterminate", "cancelled", "blocked"})
# Run 类型到 run_id 前缀。回归可能登录、下单、改业务状态，要按类型核对幂等性后才能加入。
_RERUNNABLE_RUN_TYPES = {"visual_calibration": "CAL-"}


_RERUN_SETTLE_LIMIT = timedelta(minutes=10)


def _rerun_decision(row: Mapping[str, Any], decision: str) -> dict[str, Any]:
    return {"assignment_id": row["assignment_id"], "decision": decision}


class ControlStateError(RuntimeError):
    pass


def _access_grant_conflict(exc: UniqueViolation) -> "ControlStateError":
    """把 `access_grants` 的唯一约束冲突翻成带 `conflict` 标记的领域错误。

    三个约束各对应一条设计契约（§8.2 创建幂等、§十一 设备互斥、§7.4 一人一台），
    调用方按 `conflict` 选 HTTP 错误码；约束名认不出来时记 `unknown`，不猜。
    """
    name = str(getattr(getattr(exc, "diag", None), "constraint_name", "") or "")
    kinds = {
        "access_grants_request_id_key": "request",
        "uq_active_grant_per_device": "device",
        "uq_control_grant_per_subject": "subject",
    }
    error = ControlStateError(f"access grant conflict on {name or 'unknown constraint'}")
    error.conflict = kinds.get(name, "unknown")  # type: ignore[attr-defined]
    return error


class StaleFencingError(ControlStateError):
    pass


def screen_quarantine_release_command(state: Mapping[str, Any] | None, rig_id: str) -> str:
    """解除命令（设备名取上报标记的设备键，缺省用 rig_id）；执行前仍须先跑不带确认的预检。"""
    markers = (state or {}).get("markers") or []
    device = str((markers[0] or {}).get("device") or rig_id) if markers else rig_id
    return (
        f"autophone device screenlock release-quarantine --device {device} "
        '--actor <who> --reason "<text>" --confirm-quiesced'
    )


class ScreenQuarantinedRig(ControlStateError):
    """目标 Rig 上报了 ScreenLock 隔离标记，拒收固定到它的 Assignment（BL-224）。"""

    code = "screen_quarantined"

    def __init__(self, rig_id: str, state: Mapping[str, Any] | None) -> None:
        self.rig_id = rig_id
        self.state = dict(state or {})
        self.release_command = screen_quarantine_release_command(self.state, rig_id)
        super().__init__(
            f"screen_quarantined: Rig {rig_id!r} reported a ScreenLock quarantine marker; "
            f"no Assignment was created. Release it on the Rig host with `{self.release_command}` "
            "(run it once without --confirm-quiesced as a precheck)"
        )

    def to_detail(self) -> dict[str, Any]:
        return {
            "code": self.code, "message": str(self), "rig_id": self.rig_id,
            "screen_quarantine": self.state, "release_command": self.release_command,
        }


def _screen_quarantined(rig: Mapping[str, Any]) -> bool:
    """rigs.screen_quarantine 为 NULL 表示该 Agent 未上报（旧版本），按未知处理，不拒绝。"""
    state = rig.get("screen_quarantine")
    return isinstance(state, Mapping) and state.get("quarantined") is True


class PostgresControlStore:
    """Hide Control State SQL and scheduling transactions behind one Interface."""

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "autophone",
        evidence_root: Path | str | None = None,
    ) -> None:
        if not _SCHEMA_RE.fullmatch(schema):
            raise ValueError(f"invalid PostgreSQL schema: {schema!r}")
        require_psycopg()
        self.dsn = dsn
        self.schema = schema
        configured_evidence_root = (
            evidence_root
            if evidence_root is not None
            else os.environ.get("AUTOPHONE_EVIDENCE_ROOT")
        )
        self.evidence_root = (
            Path(configured_evidence_root).expanduser().resolve()
            if configured_evidence_root
            else None
        )

    def initialize(self) -> None:
        """Apply schema.sql, unless this exact text is already applied.

        每个 CLI 进程都会走到这里，而整份 DDL 在一个事务里持锁到结束。schema 没变时也去
        执行，就会先拿 rigs 再等 assignments 的排他锁，和先读 assignments 再读 rigs 的
        Agent 查询死锁（2026-11-04 实测，Agent 因此退出、Rig 被隔离）。
        """
        psycopg, sql, _ = require_psycopg_components()
        ddl = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")
        digest = hashlib.sha256(ddl.encode()).hexdigest()
        state = sql.Identifier(self.schema, "schema_ddl_state")
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            if self._applied_ddl_digest(connection, state) == digest:
                return
            # 同时启动的多个进程只让一个执行 DDL，其余等它做完再复查。
            connection.execute(
                "SELECT pg_advisory_lock(hashtext(%s))", (f"autophone-ddl:{self.schema}",),
            )
            try:
                if self._applied_ddl_digest(connection, state) == digest:
                    return
                connection.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema))
                )
                connection.execute(
                    sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema))
                )
                connection.execute(ddl)
                connection.execute(
                    sql.SQL(
                        "CREATE TABLE IF NOT EXISTS {} (id BOOLEAN PRIMARY KEY DEFAULT true, "
                        "digest TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                    ).format(state)
                )
                connection.execute(
                    sql.SQL(
                        "INSERT INTO {} (id, digest) VALUES (true, %s) ON CONFLICT (id) "
                        "DO UPDATE SET digest = excluded.digest, applied_at = now()"
                    ).format(state),
                    (digest,),
                )
            finally:
                connection.execute(
                    "SELECT pg_advisory_unlock(hashtext(%s))", (f"autophone-ddl:{self.schema}",),
                )

    @staticmethod
    def _applied_ddl_digest(connection: psycopg.Connection, state: sql.Composable) -> str | None:
        _, sql, _ = require_psycopg_components()
        try:
            row = connection.execute(sql.SQL("SELECT digest FROM {}").format(state)).fetchone()
        except psycopg_errors_by_name("UndefinedTable", "InvalidSchemaName"):
            return None
        return str(row[0]) if row else None

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection]:
        psycopg, sql, dict_row = require_psycopg_components()
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            connection.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema))
            )
            yield connection

    def register_host(
        self,
        host_id: str,
        *,
        status: str = "online",
        max_active_rigs: int = 1,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """
                INSERT INTO hosts(host_id, status, max_active_rigs)
                VALUES (%s, %s, %s)
                ON CONFLICT (host_id) DO UPDATE SET
                    status = excluded.status,
                    max_active_rigs = excluded.max_active_rigs,
                    last_seen = now()
                RETURNING *
                """,
                (host_id, status, max_active_rigs),
            ).fetchone()
        return dict(row)

    def ensure_host(self, host_id: str, *, status: str = "online") -> dict[str, Any]:
        """Register an Agent heartbeat without changing an explicit capacity decision."""

        with self._connection() as connection:
            row = connection.execute(
                """
                INSERT INTO hosts(host_id, status, max_active_rigs)
                VALUES (%s, %s, 1)
                ON CONFLICT (host_id) DO UPDATE SET
                    status = excluded.status,
                    last_seen = now()
                RETURNING *
                """,
                (host_id, status),
            ).fetchone()
        return dict(row)

    def register_rig(
        self,
        rig_id: str,
        *,
        host_id: str,
        generation: int,
        status: str = "online",
        capability_support: Sequence[str] = (),
        source_identity: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT host_id, generation, deregistration FROM rigs WHERE rig_id = %s FOR UPDATE",
                (rig_id,),
            ).fetchone()
            if existing is not None and existing["deregistration"] is not None:
                raise ControlStateError(f"rig_deregistered: {rig_id}; use a new Rig identity")
            if existing is not None and generation < existing["generation"]:
                raise ControlStateError(
                    f"rig generation cannot decrease: {generation} < {existing['generation']}"
                )
            if (
                existing is not None
                and existing["host_id"] != host_id
                and generation == existing["generation"]
            ):
                raise ControlStateError(
                    "Rig host cannot change without increasing rig_generation: "
                    f"{rig_id} {existing['host_id']} -> {host_id}"
                )
            identity_changes = (
                existing is None
                or existing["host_id"] != host_id
                or generation > existing["generation"]
            )
            if identity_changes:
                affected_host_ids = {host_id}
                if existing is not None:
                    affected_host_ids.add(str(existing["host_id"]))
                for affected_host_id in sorted(affected_host_ids):
                    capacity_row = connection.execute(
                        """
                        SELECT max_active_rigs, admission_mode, environment_observation FROM hosts
                        WHERE host_id = %s FOR UPDATE
                        """,
                        (affected_host_id,),
                    ).fetchone()
                    if (
                        capacity_row is not None
                        and int(capacity_row["max_active_rigs"]) > 1
                        and not self._environment_policy(capacity_row)
                    ):
                        raise ControlStateError(
                            "Host capacity must be lowered to 1 before changing "
                            f"a qualified Rig identity: {affected_host_id}"
                        )
                if existing is not None and connection.execute(
                    """SELECT 1 FROM rig_reservations s JOIN host_maintenance m
                       ON m.maintenance_id = s.owner->>'maintenance_id'
                       WHERE s.rig_id = %s AND s.status = 'active' AND m.status = 'active'
                         AND m.expires_at > now() AND m.intent IN ('environment_smoke','environment_canary')""",
                    (rig_id,),
                ).fetchone():
                    raise ControlStateError("Rig identity is reserved for environment qualification")
            if existing is not None and generation > existing["generation"]:
                self._invalidate_generation(connection, rig_id, reason="rig_generation_changed")
            requested_status = status
            if existing is not None and generation > existing["generation"]:
                requested_status = "quarantine"
            elif existing is not None:
                current_status = connection.execute(
                    "SELECT status FROM rigs WHERE rig_id = %s",
                    (rig_id,),
                ).fetchone()["status"]
                if current_status == "quarantine":
                    requested_status = "quarantine"
            row = connection.execute(
                """
                INSERT INTO rigs(
                    rig_id, host_id, generation, status, capability_support, source_identity
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (rig_id) DO UPDATE SET
                    observed_state = CASE WHEN rigs.generation = excluded.generation
                        THEN rigs.observed_state ELSE '{}'::jsonb END,
                    capability_state = CASE WHEN rigs.generation = excluded.generation
                        THEN rigs.capability_state ELSE '{}'::jsonb END,
                    agent_connection_id = CASE WHEN rigs.generation = excluded.generation
                        THEN rigs.agent_connection_id ELSE NULL END,
                    agent_connected_until = CASE WHEN rigs.generation = excluded.generation
                        THEN rigs.agent_connected_until ELSE NULL END,
                    host_id = excluded.host_id,
                    generation = excluded.generation,
                    status = excluded.status,
                    capability_support = excluded.capability_support,
                    -- BL-151：源码身份属于**这次握手的那个 agent 进程**，所以每次注册都按
                    -- 本次上报覆盖，不保留上一个进程的值。老 agent 不报时写回 NULL，
                    -- 派单准入据此按兼容放行并告警，而不是拿旧进程的身份当作现状。
                    source_identity = excluded.source_identity,
                    last_seen = now()
                RETURNING *
                """,
                (
                    rig_id,
                    host_id,
                    generation,
                    requested_status,
                    Jsonb(sorted(set(capability_support))),
                    Jsonb(dict(source_identity)) if source_identity else None,
                ),
            ).fetchone()
        return dict(row)

    def register_rig_target(
        self,
        rig_id: str,
        *,
        host_id: str,
        generation: int,
        target: Mapping[str, Any],
    ) -> dict[str, Any]:
        fields = (
            "cli_dir", "vm_dir", "socket", "expected_usb_udid",
            "ssh_host", "ssh_port", "frida_host", "frida_port",
            "frida_guest_port", "log_dir", "temp_dir", "package_cache",
        )
        missing = [field for field in fields if target.get(field) in (None, "")]
        if missing:
            raise ControlStateError(f"Rig target fields are required: {', '.join(missing)}")
        try:
            with self._connection() as connection:
                rig = connection.execute(
                    "SELECT host_id, generation FROM rigs WHERE rig_id = %s AND deregistration IS NULL FOR UPDATE",
                    (rig_id,),
                ).fetchone()
                if rig is None:
                    raise ControlStateError(f"rig not found: {rig_id}")
                if rig["host_id"] != host_id or int(rig["generation"]) != generation:
                    raise StaleFencingError(
                        f"Rig target identity is stale: {host_id}/{rig_id}/{generation}"
                    )
                existing_target = connection.execute(
                    "SELECT * FROM rig_targets WHERE rig_id = %s FOR UPDATE",
                    (rig_id,),
                ).fetchone()
                if (
                    existing_target is not None
                    and int(existing_target["rig_generation"]) == generation
                ):
                    requested = {
                        "host_id": host_id,
                        "cli_dir": str(target["cli_dir"]),
                        "vm_dir": str(target["vm_dir"]),
                        "socket_path": str(target["socket"]),
                        "expected_usb_udid": str(target["expected_usb_udid"]),
                        "ssh_host": str(target["ssh_host"]),
                        "ssh_port": int(target["ssh_port"]),
                        "frida_host": str(target["frida_host"]),
                        "frida_port": int(target["frida_port"]),
                        "frida_guest_port": int(target["frida_guest_port"]),
                        "log_dir": str(target["log_dir"]),
                        "temp_dir": str(target["temp_dir"]),
                        "package_cache": str(target["package_cache"]),
                    }
                    changed = sorted(
                        field for field, value in requested.items()
                        if existing_target[field] != value
                    )
                    if changed:
                        raise ControlStateError(
                            "Rig target cannot change without increasing "
                            f"rig_generation: {', '.join(changed)}"
                        )
                row = connection.execute(
                    """
                    INSERT INTO rig_targets(
                        rig_id, host_id, rig_generation, cli_dir, vm_dir, socket_path,
                        expected_usb_udid, ssh_host, ssh_port, frida_host, frida_port,
                        frida_guest_port, log_dir, temp_dir, package_cache
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (rig_id) DO UPDATE SET
                        host_id = excluded.host_id,
                        rig_generation = excluded.rig_generation,
                        cli_dir = excluded.cli_dir,
                        vm_dir = excluded.vm_dir,
                        socket_path = excluded.socket_path,
                        expected_usb_udid = excluded.expected_usb_udid,
                        ssh_host = excluded.ssh_host,
                        ssh_port = excluded.ssh_port,
                        frida_host = excluded.frida_host,
                        frida_port = excluded.frida_port,
                        frida_guest_port = excluded.frida_guest_port,
                        log_dir = excluded.log_dir,
                        temp_dir = excluded.temp_dir,
                        package_cache = excluded.package_cache,
                        updated_at = now()
                    RETURNING *
                    """,
                    (
                        rig_id, host_id, generation,
                        str(target["cli_dir"]), str(target["vm_dir"]), str(target["socket"]),
                        str(target["expected_usb_udid"]),
                        str(target["ssh_host"]), int(target["ssh_port"]),
                        str(target["frida_host"]), int(target["frida_port"]),
                        int(target["frida_guest_port"]),
                        str(target["log_dir"]), str(target["temp_dir"]),
                        str(target["package_cache"]),
                    ),
                ).fetchone()
        except psycopg_errors_by_name("UniqueViolation") as exc:
            raise ControlStateError(
                f"Rig target resource conflicts on host {host_id}: {rig_id}"
            ) from exc
        return self._jsonable(dict(row))

    def resolve_device_rig(self, device: str) -> str:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT r.rig_id FROM rigs r LEFT JOIN rig_targets t ON t.rig_id = r.rig_id "
                "WHERE r.deregistration IS NULL "
                "AND (r.rig_id = %s OR regexp_replace(t.vm_dir, '^.*/', '') = %s)",
                (device, device),
            ).fetchall()
        if len(rows) != 1:
            raise ControlStateError(f"device_rig_unresolved: {device!r} matched {len(rows)} Rigs")
        return str(rows[0]["rig_id"])

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = %s", (run_id,)).fetchone()
        if row is None:
            raise ControlStateError(f"Run not found: {run_id}")
        return self._jsonable(dict(row))

    def submit_assignment(
        self,
        plan: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise ControlStateError("idempotency_key is required")
        if not bool(plan.get("requires_rig", True)):
            raise ControlStateError("Host-only work must not create a Rig Assignment")
        from autophone.reset_contract import validate_reset_spec
        try:
            validate_reset_spec(plan.get('reset_policy'))
        except ValueError as exc:
            raise ControlStateError(f'invalid reset policy: {exc}') from exc
        priority = str(plan.get("priority") or "normal")
        if priority not in {"high", "normal"}:
            raise ControlStateError(f"unsupported priority: {priority}")
        assignment_id = str(plan.get("assignment_id") or f"A-{uuid.uuid4().hex}")
        run_spec = deepcopy(plan.get("run_spec"))
        run_id = str((run_spec or {}).get("run_id") or "")
        with self._connection() as connection:
            if plan.get("target_rig_id"):
                target = connection.execute(
                    "SELECT deregistration, screen_quarantine FROM rigs WHERE rig_id = %s FOR UPDATE",
                    (str(plan["target_rig_id"]),),
                ).fetchone()
                if target is not None and target["deregistration"] is not None:
                    raise ControlStateError(f"rig_deregistered: {plan['target_rig_id']}")
                existing = connection.execute(
                    "SELECT 1 FROM assignments WHERE idempotency_key = %s", (idempotency_key,),
                ).fetchone()
                # 隔离中的 Rig 取不到 ScreenLock，派过去只会以未执行结束（BL-224）。同一幂等键
                # 已提交过的不拒绝，照常返回原 Assignment。Recovery 不经 ScreenLock，不受限。
                if (
                    existing is None and target is not None
                    and str(plan.get("kind") or "run") != "recovery"
                    and _screen_quarantined(target)
                ):
                    raise ScreenQuarantinedRig(
                        str(plan["target_rig_id"]), self._jsonable(target["screen_quarantine"]),
                    )
            row = connection.execute(
                """
                INSERT INTO assignments(
                    assignment_id, idempotency_key, kind, priority, requires_rig,
                    target_rig_id, reservation_id, required_capabilities,
                    desired_state, reset_policy, run_spec, status, queue_deadline
                ) VALUES (
                    %s, %s, %s, %s, true, %s, %s, %s, %s, %s, %s, 'queued', %s
                )
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING *
                """,
                (
                    assignment_id,
                    idempotency_key,
                    str(plan.get("kind") or "run"),
                    priority,
                    plan.get("target_rig_id"),
                    plan.get("reservation_id"),
                    Jsonb(sorted(set(plan.get("required_capabilities") or []))),
                    Jsonb(dict(plan.get("desired_state") or {})),
                    Jsonb(dict(plan.get("reset_policy") or {})),
                    Jsonb(run_spec) if isinstance(run_spec, Mapping) else None,
                    plan.get("queue_deadline"),
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM assignments WHERE idempotency_key = %s",
                    (idempotency_key,),
                ).fetchone()
            elif run_id:
                connection.execute(
                    """
                    INSERT INTO runs(run_id, assignment_id, run_spec, status)
                    VALUES (%s, %s, %s, 'queued')
                    """,
                    (run_id, row["assignment_id"], Jsonb(run_spec)),
                )
        return self.get_assignment(str(row["assignment_id"]))

    def dispatch(
        self,
        *,
        lease_seconds: int = 60,
        connected_rigs: set[tuple[str, str]] | None = None,
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            # Serialize the small M2 scheduler transaction.  This keeps host
            # capacity evaluation deterministic while workers remain
            # concurrent during execution.
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"autophone-dispatch:{self.schema}",),
            )
            self._expire_reservations(connection)
            assignments = connection.execute(
                """
                SELECT * FROM assignments
                WHERE status = 'queued'
                ORDER BY CASE priority WHEN 'high' THEN 0 ELSE 1 END, created_at, assignment_id
                FOR UPDATE SKIP LOCKED
                """
            ).fetchall()
            if not assignments:
                return None
            assignment = None
            selected = None
            first_waiting_id: str | None = None
            for candidate_assignment in assignments:
                if candidate_assignment["queue_deadline"] is not None:
                    expired = connection.execute(
                        "SELECT %s::timestamptz <= now() AS expired",
                        (candidate_assignment["queue_deadline"],),
                    ).fetchone()["expired"]
                    if expired:
                        connection.execute(
                            """
                            UPDATE assignments
                            SET status = 'cancelled', wait_reason = 'queue_deadline_exceeded', updated_at = now()
                            WHERE assignment_id = %s
                            """,
                            (candidate_assignment["assignment_id"],),
                        )
                        continue

                candidates = connection.execute(
                    """
                    SELECT r.*, h.status AS host_status, h.max_active_rigs, h.admission_mode
                    FROM rigs r
                    JOIN hosts h ON h.host_id = r.host_id
                    WHERE (%s::text IS NULL OR r.rig_id = %s::text)
                    ORDER BY r.rig_id
                    FOR UPDATE OF r, h SKIP LOCKED
                    """,
                    (
                        candidate_assignment["target_rig_id"],
                        candidate_assignment["target_rig_id"],
                    ),
                ).fetchall()
                wait_reasons = []
                required = set(candidate_assignment["required_capabilities"] or [])
                for rig in candidates:
                    reason = self._rig_wait_reason(
                        connection,
                        candidate_assignment,
                        rig,
                        required,
                        connected_rigs,
                    )
                    if reason is None:
                        selected = rig
                        assignment = candidate_assignment
                        break
                    wait_reasons.append(reason)
                if selected is not None:
                    break
                reason = self._choose_wait_reason(
                    candidate_assignment,
                    candidates,
                    wait_reasons,
                )
                connection.execute(
                    "UPDATE assignments SET wait_reason = %s, updated_at = now() WHERE assignment_id = %s",
                    (reason, candidate_assignment["assignment_id"]),
                )
                if first_waiting_id is None:
                    first_waiting_id = str(candidate_assignment["assignment_id"])
            if selected is None:
                return (
                    self._get_assignment(connection, first_waiting_id)
                    if first_waiting_id is not None
                    else None
                )
            assert assignment is not None

            fencing = int(selected["fencing_counter"]) + 1
            attempt_no = int(
                connection.execute(
                    """
                    SELECT COALESCE(max(attempt_no), 0) + 1 AS value
                    FROM assignment_attempts WHERE assignment_id = %s
                    """,
                    (assignment["assignment_id"],),
                ).fetchone()["value"]
            )
            from .reset import claim as claim_fixture
            claim_fixture(connection, assignment, attempt_no)
            connection.execute(
                "UPDATE rigs SET fencing_counter = %s WHERE rig_id = %s",
                (fencing, selected["rig_id"]),
            )
            connection.execute(
                """
                INSERT INTO assignment_attempts(
                    assignment_id, attempt_no, host_id, rig_id, rig_generation, fencing, status
                ) VALUES (%s, %s, %s, %s, %s, %s, 'assigned')
                """,
                (
                    assignment["assignment_id"],
                    attempt_no,
                    selected["host_id"],
                    selected["rig_id"],
                    selected["generation"],
                    fencing,
                ),
            )
            connection.execute(
                """
                INSERT INTO rig_leases(
                    lease_id, assignment_id, attempt_no, rig_id, rig_generation,
                    fencing, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))
                """,
                (
                    f"L-{uuid.uuid4().hex}",
                    assignment["assignment_id"],
                    attempt_no,
                    selected["rig_id"],
                    selected["generation"],
                    fencing,
                    lease_seconds,
                ),
            )
            connection.execute(
                """
                UPDATE assignments SET
                    status = 'assigned', wait_reason = NULL,
                    bound_host_id = %s, bound_rig_id = %s, bound_generation = %s,
                    current_attempt_no = %s, current_fencing = %s, updated_at = now()
                WHERE assignment_id = %s
                """,
                (
                    selected["host_id"],
                    selected["rig_id"],
                    selected["generation"],
                    attempt_no,
                    fencing,
                    assignment["assignment_id"],
                ),
            )
            if assignment["run_spec"] is not None:
                connection.execute(
                    "UPDATE runs SET status = 'assigned', updated_at = now() WHERE assignment_id = %s",
                    (assignment["assignment_id"],),
                )
            if assignment['kind'] == 'recovery':
                from .recovery import bind_recovery_attempt
                bind_recovery_attempt(connection, assignment['assignment_id'], attempt_no, fencing)
            return self._get_assignment(connection, assignment["assignment_id"])

    def get_assignment(self, assignment_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            return self._get_assignment(connection, assignment_id)

    def get_run_recoveries(self, run_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT i.* FROM recovery_incidents i "
                "LEFT JOIN runs r ON r.assignment_id = i.old_assignment_id "
                "LEFT JOIN assignments a ON a.assignment_id = i.recovery_assignment_id "
                "WHERE r.run_id = %s OR a.desired_state #>> '{interrupted_attempt,run_id}' = %s "
                "ORDER BY i.created_at", (run_id, run_id),
            ).fetchall()
        return self._jsonable([dict(row) for row in rows])

    def list_rig_recoveries(self, rig_id: str, *, active_only: bool = False) -> list[dict[str, Any]]:
        """Read one Rig's recovery incidents, oldest first; active = not recovered or isolation-confirmed."""
        query = "SELECT * FROM recovery_incidents WHERE rig_id = %s"
        if active_only:
            query += " AND state NOT IN ('recovered','isolation_confirmed')"
        with self._connection() as connection:
            rows = connection.execute(query + " ORDER BY created_at, recovery_id", (rig_id,)).fetchall()
        return self._jsonable([dict(row) for row in rows])

    def rig_activity(self, rig_id: str) -> dict[str, list[str]]:
        """Read-only: non-terminal Assignments, unreleased Leases and open recovery incidents of one Rig.

        ScreenLock 隔离标记的解除前置（BL-223）用它确认该 Rig 上没有仍可能持有设备的工作。
        """
        with self._connection() as connection:
            assignments = connection.execute(
                "SELECT assignment_id FROM assignments "
                "WHERE (target_rig_id = %s OR bound_rig_id = %s) AND NOT (status = ANY(%s)) "
                "ORDER BY created_at, assignment_id",
                (rig_id, rig_id, list(_TERMINAL_ASSIGNMENTS)),
            ).fetchall()
            leases = connection.execute(
                "SELECT lease_id FROM rig_leases WHERE rig_id = %s AND released_at IS NULL "
                "ORDER BY created_at, lease_id", (rig_id,),
            ).fetchall()
            recoveries = connection.execute(
                "SELECT recovery_id FROM recovery_incidents WHERE rig_id = %s "
                "AND state NOT IN ('recovered','isolation_confirmed') ORDER BY created_at, recovery_id",
                (rig_id,),
            ).fetchall()
        return {
            "active_assignments": [str(row["assignment_id"]) for row in assignments],
            "unreleased_leases": [str(row["lease_id"]) for row in leases],
            "active_recoveries": [str(row["recovery_id"]) for row in recoveries],
        }

    def _get_assignment(self, connection: psycopg.Connection, assignment_id: str) -> dict[str, Any]:
        assignment = connection.execute(
            "SELECT * FROM assignments WHERE assignment_id = %s",
            (assignment_id,),
        ).fetchone()
        if assignment is None:
            raise ControlStateError(f"assignment not found: {assignment_id}")
        attempt = None
        lease = None
        if assignment["current_attempt_no"] is not None:
            attempt = connection.execute(
                """
                SELECT * FROM assignment_attempts
                WHERE assignment_id = %s AND attempt_no = %s
                """,
                (assignment_id, assignment["current_attempt_no"]),
            ).fetchone()
            lease = connection.execute(
                """
                SELECT * FROM rig_leases
                WHERE assignment_id = %s AND attempt_no = %s
                ORDER BY created_at DESC LIMIT 1
                """,
                (assignment_id, assignment["current_attempt_no"]),
            ).fetchone()
        recoveries = connection.execute(
            # BL-173 schema audit: both ``UUID`` occurrences in schema.sql define
            # the same logical column, recovery_incidents.edge_id (CREATE TABLE
            # plus its idempotent ALTER migration). Assignments, attempts, and
            # leases contain no UUID-typed columns. edge_id enters an Assignment
            # projection only through this nested ``recoveries`` query, and the
            # outer _jsonable call below converts the complete aggregate.
            'SELECT * FROM recovery_incidents WHERE old_assignment_id = %s OR recovery_assignment_id = %s ORDER BY created_at',
            (assignment_id, assignment_id),
        ).fetchall()
        return self._jsonable({
            **dict(assignment),
            'recoveries': [dict(row) for row in recoveries],
            "attempt": dict(attempt) if attempt is not None else None,
            "lease": dict(lease) if lease is not None else None,
        })

    def cancel_assignment(self, assignment_id: str, actor: str, reason: str) -> dict[str, Any]:
        if not actor or not reason:
            raise ControlStateError("cancel actor and reason are required")
        with self._connection() as connection:
            assignment = connection.execute(
                "SELECT * FROM assignments WHERE assignment_id = %s FOR UPDATE",
                (assignment_id,),
            ).fetchone()
            if assignment is None:
                raise ControlStateError(f"assignment not found: {assignment_id}")
            if assignment["status"] in _TERMINAL_ASSIGNMENTS:
                return self._get_assignment(connection, assignment_id)
            request = {"actor": actor, "reason": reason, "requested_at": datetime.now().astimezone().isoformat()}
            status = "cancelled" if assignment["status"] == "queued" else "cancel_requested"
            connection.execute(
                """
                UPDATE assignments SET status = %s, cancel_request = %s, updated_at = now()
                WHERE assignment_id = %s
                """,
                (status, Jsonb(request), assignment_id),
            )
            if status == "cancelled" and assignment["run_spec"] is not None:
                connection.execute(
                    """
                    UPDATE runs SET status = 'cancelled', result = NULL, closure = NULL, updated_at = now()
                    WHERE assignment_id = %s
                    """,
                    (assignment_id,),
                )
            return self._get_assignment(connection, assignment_id)

    def heartbeat(
        self,
        assignment_id: str,
        *,
        attempt_no: int,
        fencing: int,
        lease_seconds: int = 60,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            lease = self._current_lease(connection, assignment_id, attempt_no, fencing, lock=True)
            connection.execute(
                """
                UPDATE rig_leases
                SET expires_at = CASE WHEN EXISTS (
                    SELECT 1 FROM recovery_incidents i WHERE i.old_assignment_id = rig_leases.assignment_id
                    AND i.old_attempt_no = rig_leases.attempt_no
                    AND i.state NOT IN ('recovered','isolation_confirmed')
                ) THEN expires_at ELSE now() + make_interval(secs => %s) END
                WHERE lease_id = %s
                """,
                (lease_seconds, lease["lease_id"]),
            )
            connection.execute(
                """
                UPDATE assignment_attempts SET last_heartbeat = now(), status = 'running'
                WHERE assignment_id = %s AND attempt_no = %s
                """,
                (assignment_id, attempt_no),
            )
            connection.execute(
                """
                UPDATE assignments SET
                    status = CASE
                        WHEN status = 'cancel_requested' THEN status
                        ELSE 'running'
                    END,
                    updated_at = now()
                WHERE assignment_id = %s
                """,
                (assignment_id,),
            )
            return self._get_assignment(connection, assignment_id)

    def record_stage_result(
        self,
        assignment_id: str,
        *,
        attempt_no: int,
        fencing: int,
        stage: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._connection() as connection:
            lease = self._current_lease(connection, assignment_id, attempt_no, fencing, lock=True)
            attempt = connection.execute(
                """
                SELECT stage_results FROM assignment_attempts
                WHERE assignment_id = %s AND attempt_no = %s FOR UPDATE
                """,
                (assignment_id, attempt_no),
            ).fetchone()
            stages = dict(attempt["stage_results"] or {})
            existing = stages.get(stage)
            value = deepcopy(dict(result))
            if existing is not None and existing != value:
                raise ControlStateError(f"stage result conflict: {stage}")
            stages[stage] = value
            connection.execute(
                """
                UPDATE assignment_attempts SET stage_results = %s
                WHERE assignment_id = %s AND attempt_no = %s
                """,
                (Jsonb(stages), assignment_id, attempt_no),
            )
            if stage == 'quiesce' and value.get('status') == 'failed' and value.get('device_access_released') is False:
                # Persist isolation with the fenced failure receipt. Do not
                # complete the Attempt or release its Lease without confirmed I/O stop.
                connection.execute(
                    "UPDATE rigs SET status = 'quarantine' WHERE rig_id = %s",
                    (lease['rig_id'],),
                )
            return self._get_assignment(connection, assignment_id)

    def complete_assignment(
        self,
        assignment_id: str,
        *,
        attempt_no: int,
        fencing: int,
        result: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        if result.get('schema_version') is not None:
            from autophone.contracts import validate_or_raise
            validate_or_raise('assignment-result.v1.json', dict(result))
            if (result['assignment_id'], result['attempt_no'], result['fencing']) != (assignment_id, attempt_no, fencing):
                raise ControlStateError('Assignment Result identity does not match completion authority')
            product = result.get('run_result')
            if isinstance(product, Mapping) and product.get('status') != result['product_verdict']:
                raise ControlStateError('Assignment Result product verdict mismatch')
            closure_binding = (result.get('closure') or {}).get('assignment')
            if closure_binding is not None and (
                closure_binding.get('assignment_id'), closure_binding.get('attempt_no'), closure_binding.get('fencing')
            ) != (assignment_id, attempt_no, fencing):
                raise ControlStateError('Closure projection identity mismatch')
        status = str(result.get("status") or "error")
        if status not in _TERMINAL_ATTEMPTS:
            raise ControlStateError(f"invalid assignment terminal status: {status}")
        cleanup = result.get("cleanup_receipt")
        if not isinstance(cleanup, Mapping) or cleanup.get("artifacts_sealed") is not True:
            raise ControlStateError(
                "Assignment completion requires a sealed Artifact cleanup receipt"
            )
        if not isinstance(cleanup.get("device_access_released"), bool):
            raise ControlStateError(
                "Assignment completion requires released device access"
            )
        rig_disposition = str(result.get("rig_disposition") or "")
        if rig_disposition not in {"reusable", "needs_inspection", "quarantine"}:
            raise ControlStateError(
                "Assignment completion requires a valid Rig disposition"
            )
        required_artifact_fields = {"logical_name", "uri", "sha256", "size"}
        expected_artifact_owner = f"{assignment_id}:{attempt_no}"
        for artifact in artifacts:
            missing = sorted(
                field for field in required_artifact_fields
                if artifact.get(field) in (None, "")
            )
            owner_invalid = (
                artifact.get("owner_kind") != "assignment_attempt"
                or artifact.get("owner_id") != expected_artifact_owner
            )
            if (
                missing
                or owner_invalid
                or artifact.get("seal_status", "sealed") != "sealed"
            ):
                raise ControlStateError(
                    "Assignment Artifact reference is not sealed: "
                    + ", ".join(
                        missing
                        or (["owner"] if owner_invalid else ["seal_status"])
                    )
                )
        with self._connection() as connection:
            lease = self._current_lease(connection, assignment_id, attempt_no, fencing, lock=True)
            if self._quiesce_failure(connection, assignment_id, attempt_no) is not None:
                # BL-220 剩余项：Attempt 记录过 quiesce 失败（rig 已在同一事务中进入 quarantine）。
                # 只接受如实声明「释放未确认、rig 隔离」的结构化错误回执；把它当可复用或声称已
                # 释放的回执一律拒收。
                self._assert_quiesce_unverified_completion(result)
            elif cleanup.get("device_access_released") is not True:
                raise ControlStateError(
                    "Assignment completion requires released device access"
                )
            from .reset import complete as complete_reset
            complete_reset(connection, assignment_id, attempt_no, fencing, lease, result)
            if result.get('schema_version') and isinstance(result.get('run_result'), Mapping):
                run = connection.execute('SELECT run_id FROM runs WHERE assignment_id = %s', (assignment_id,)).fetchone()
                registered = connection.execute("SELECT run_spec->>'run_id' AS run_id FROM assignments WHERE assignment_id=%s",
                                                (assignment_id,)).fetchone()
                if registered['run_id'] is not None and (run is None or result['run_result']['run_id'] != run['run_id']):
                    raise ControlStateError('Run Result identity does not match Assignment')
            from .recovery import complete_recovery
            complete_recovery(connection, self, assignment_id, attempt_no, fencing, lease, result)
            connection.execute(
                """
                UPDATE assignment_attempts SET status = %s, result = %s, ended_at = now()
                WHERE assignment_id = %s AND attempt_no = %s
                """,
                (status, Jsonb(dict(result)), assignment_id, attempt_no),
            )
            connection.execute(
                """
                UPDATE assignments SET status = %s, result = %s, updated_at = now()
                WHERE assignment_id = %s
                """,
                (status, Jsonb(dict(result)), assignment_id),
            )
            connection.execute(
                """
                UPDATE rig_leases SET released_at = now(), release_reason = 'assignment_completed'
                WHERE lease_id = %s
                """,
                (lease["lease_id"],),
            )
            if rig_disposition in {"needs_inspection", "quarantine"}:
                connection.execute(
                    "UPDATE rigs SET status = 'quarantine' WHERE rig_id = %s",
                    (lease["rig_id"],),
                )
                attempt_identity = connection.execute(
                    """
                    SELECT host_id FROM assignment_attempts
                    WHERE assignment_id = %s AND attempt_no = %s
                    """,
                    (assignment_id, attempt_no),
                ).fetchone()
                self._insert_host_event(
                    connection,
                    host_id=str(attempt_identity["host_id"]),
                    rig_id=str(lease["rig_id"]),
                    kind="assignment_rig_disposition",
                    actor="rig-agent",
                    details={
                        "assignment_id": assignment_id,
                        "attempt_no": attempt_no,
                        "disposition": rig_disposition,
                    },
                )
            run_result = result.get("run_result")
            if isinstance(run_result, Mapping):
                connection.execute(
                    """
                    UPDATE runs SET status = %s, result = %s, closure = %s, updated_at = now()
                    WHERE assignment_id = %s
                    """,
                    (str(run_result.get('status') or status), Jsonb(dict(run_result)),
                     Jsonb(result.get('closure')), assignment_id),
                )
            else:
                # Run 未执行（接单即结束、Preflight 阻断、取锁被拒）没有 Run Result：runs 行取
                # Assignment 终态，不停在 assigned（BL-224）。result 保持 NULL，不伪造 Run Result。
                connection.execute(
                    """
                    UPDATE runs SET status = %s, updated_at = now()
                    WHERE assignment_id = %s AND status IN ('queued', 'assigned')
                    """,
                    (status, assignment_id),
                )
            for artifact in artifacts:
                connection.execute(
                    """
                    INSERT INTO artifact_refs(
                        artifact_id, owner_kind, owner_id, logical_name, uri,
                        sha256, size, governance, seal_status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (owner_kind, owner_id, logical_name) DO NOTHING
                    """,
                    (
                        str(artifact.get("artifact_id") or f"AR-{uuid.uuid4().hex}"),
                        str(artifact.get("owner_kind") or "assignment"),
                        str(artifact.get("owner_id") or assignment_id),
                        str(artifact["logical_name"]),
                        str(artifact["uri"]),
                        str(artifact["sha256"]),
                        int(artifact["size"]),
                        str(artifact.get("governance") or "local_only"),
                        str(artifact.get("seal_status") or "sealed"),
                    ),
                )
            return self._get_assignment(connection, assignment_id)

    def rerun_assignment(self, assignment_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        """Submit a new Assignment with a fresh run_id for one that ended without passing.

        ``retry_assignment`` 复用 run_id 和 run 目录，校准第二次尝试会报 FileExistsError，
        修它就得覆盖第一次的证据。重跑另开一个 Assignment，并用 ``rerun_of`` 记录来源。
        只开放确认可以重复执行的 Run 类型。
        """
        if not actor or not reason:
            raise ControlStateError("Assignment rerun actor and reason are required")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM assignments WHERE assignment_id = %s", (assignment_id,),
            ).fetchone()
        if row is None:
            raise ControlStateError(f"assignment not found: {assignment_id}")
        if row["rerun_of"] is not None:
            raise ControlStateError(f"assignment is already a rerun: {assignment_id}")
        if row["status"] not in _RERUNNABLE_STATUSES:
            raise ControlStateError(f"assignment is not rerunnable: {row['status']}")
        run_spec = row["run_spec"] or {}
        prefix = _RERUNNABLE_RUN_TYPES.get(run_spec.get("type"))
        if prefix is None:
            raise ControlStateError(f"run type is not rerunnable: {run_spec.get('type')}")
        new_run_id = f"{prefix}{uuid.uuid4().hex}"
        # run_id 也出现在 run_dir 里，整段替换才不会写回第一次的目录。
        new_spec = json.loads(json.dumps(run_spec).replace(str(run_spec["run_id"]), new_run_id))
        rerun = self.submit_assignment({
            "kind": row["kind"],
            "priority": row["priority"],
            "requires_rig": True,
            "target_rig_id": row["target_rig_id"],
            "required_capabilities": row["required_capabilities"],
            "desired_state": row["desired_state"],
            "reset_policy": row["reset_policy"],
            "run_spec": new_spec,
        }, f"rerun:{assignment_id}")
        with self._connection() as connection:
            connection.execute(
                "UPDATE assignments SET rerun_of = %s WHERE assignment_id = %s",
                (assignment_id, rerun["assignment_id"]),
            )
            if row["bound_host_id"]:
                self._insert_host_event(
                    connection, host_id=row["bound_host_id"], rig_id=row["bound_rig_id"],
                    kind="assignment_rerun", actor=actor,
                    details={"assignment_id": assignment_id, "rerun_id": rerun["assignment_id"],
                             "reason": reason},
                )
            return self._get_assignment(connection, rerun["assignment_id"])

    def retry_assignment(self, assignment_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            assignment = connection.execute(
                "SELECT * FROM assignments WHERE assignment_id = %s FOR UPDATE",
                (assignment_id,),
            ).fetchone()
            if assignment is None:
                raise ControlStateError(f"assignment not found: {assignment_id}")
            if assignment["status"] not in _TERMINAL_ASSIGNMENTS:
                raise ControlStateError(f"assignment is not retryable: {assignment['status']}")
            connection.execute(
                """
                UPDATE assignments SET
                    status = 'queued', wait_reason = NULL, result = NULL,
                    cancel_request = NULL,
                    bound_host_id = NULL, bound_rig_id = NULL, bound_generation = NULL,
                    current_attempt_no = NULL, current_fencing = NULL, updated_at = now()
                WHERE assignment_id = %s
                """,
                (assignment_id,),
            )
            if assignment["run_spec"] is not None:
                connection.execute(
                    """
                    UPDATE runs SET status = 'queued', result = NULL, closure = NULL, updated_at = now()
                    WHERE assignment_id = %s
                    """,
                    (assignment_id,),
                )
            return self._get_assignment(connection, assignment_id)

    def decline_assignment(
        self,
        assignment_id: str,
        *,
        attempt_no: int,
        fencing: int,
        reason: str,
        detail: str = "",
    ) -> dict[str, Any]:
        """Agent 在执行器之前拒单：Attempt 与 Assignment 收为 `blocked`，释放租约（BL-214）。

        只接受未开始的 Attempt：没有心跳（Agent 在调用执行器之前先续租一次）且没有阶段结果，
        据此认定本 Attempt 没有访问设备，rig 状态不变。终态与 BL-224 的未执行收尾一致
        （`blocked`、`product_verdict=not_run`），重跑仍由用户显式发起（新 Attempt、新 Fencing）。
        """
        if reason not in _DECLINE_REASONS:
            raise ControlStateError(f"assignment decline reason is not accepted: {reason}")
        with self._connection() as connection:
            lease = self._current_lease(connection, assignment_id, attempt_no, fencing, lock=True)
            attempt = connection.execute(
                """
                SELECT host_id, stage_results, last_heartbeat FROM assignment_attempts
                WHERE assignment_id = %s AND attempt_no = %s FOR UPDATE
                """,
                (assignment_id, attempt_no),
            ).fetchone()
            if attempt["last_heartbeat"] is not None or attempt["stage_results"]:
                raise ControlStateError(
                    f"assignment decline refused: attempt already started: {assignment_id}/{attempt_no}"
                )
            assignment = connection.execute(
                "SELECT kind, run_spec FROM assignments WHERE assignment_id = %s FOR UPDATE",
                (assignment_id,),
            ).fetchone()
            if assignment["kind"] == "recovery":
                # 恢复 Assignment 的终态推进 recovery incident，拒单不处理，仍按租约过期收口。
                raise ControlStateError("assignment decline refused: recovery Assignment")
            result = {
                "status": "blocked",
                "reason": reason,
                "detail": detail or reason,
                "product_verdict": "not_run",
                "run_result": None,
                "declined_by": "rig-agent",
                "device_access": "not_started",
                "assignment_id": assignment_id,
                "attempt_no": attempt_no,
                "fencing": fencing,
            }
            connection.execute(
                """
                UPDATE assignment_attempts SET status = 'blocked', result = %s, ended_at = now()
                WHERE assignment_id = %s AND attempt_no = %s
                """,
                (Jsonb(result), assignment_id, attempt_no),
            )
            connection.execute(
                """
                UPDATE assignments SET status = 'blocked', result = %s, updated_at = now()
                WHERE assignment_id = %s
                """,
                (Jsonb(result), assignment_id),
            )
            connection.execute(
                """
                UPDATE rig_leases SET released_at = now(), release_reason = 'agent_declined'
                WHERE lease_id = %s
                """,
                (lease["lease_id"],),
            )
            # 派单时占用的 Fixture 未被动作过：归还占用，恢复派单前的 restored。
            connection.execute(
                """
                UPDATE reset_fixtures SET owner_assignment_id = NULL, owner_attempt_no = NULL,
                    disposition = 'restored', updated_at = now()
                WHERE owner_assignment_id = %s AND owner_attempt_no = %s AND disposition = 'unknown'
                """,
                (assignment_id, attempt_no),
            )
            if assignment["run_spec"] is not None:
                connection.execute(
                    """
                    UPDATE runs SET status = 'blocked', updated_at = now()
                    WHERE assignment_id = %s AND status IN ('queued', 'assigned')
                    """,
                    (assignment_id,),
                )
            self._insert_host_event(
                connection,
                host_id=str(attempt["host_id"]),
                rig_id=str(lease["rig_id"]),
                kind="assignment_declined",
                actor="rig-agent",
                details={"assignment_id": assignment_id, "attempt_no": attempt_no, "reason": reason},
            )
            return self._get_assignment(connection, assignment_id)

    def reconcile_stale_run_rows(self, *, apply: bool = False, actor: str | None = None) -> dict[str, Any]:
        """列出（`apply=True` 时收敛）Assignment 已终态而 `runs` 行仍为 queued/assigned 的存量行。

        修复前的租约过期路径不更新 `runs` 行（BL-214）。收敛只把 `status` 改为 Assignment 终态，
        `result` 保持原值（NULL），不伪造 Run Result。默认只读；写入须给出 actor，并写 host 事件。
        """
        if apply and not str(actor or "").strip():
            raise ControlStateError("reconcile --apply requires a non-empty actor")
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT r.run_id, r.assignment_id, r.status AS run_status, r.updated_at,
                       a.status AS assignment_status, a.bound_host_id, a.bound_rig_id
                FROM runs r JOIN assignments a ON a.assignment_id = r.assignment_id
                WHERE r.status IN ('queued', 'assigned')
                  AND a.status IN ({", ".join(["%s"] * len(_TERMINAL_ASSIGNMENTS))})
                ORDER BY r.updated_at, r.run_id
                {"FOR UPDATE OF r" if apply else ""}
                """,
                tuple(sorted(_TERMINAL_ASSIGNMENTS)),
            ).fetchall()
            listed = [{
                "run_id": row["run_id"],
                "assignment_id": row["assignment_id"],
                "run_status": row["run_status"],
                "assignment_status": row["assignment_status"],
                "target_status": row["assignment_status"],
                "updated_at": row["updated_at"],
            } for row in rows]
            if apply:
                for row in rows:
                    connection.execute(
                        """
                        UPDATE runs SET status = %s, updated_at = now()
                        WHERE run_id = %s AND status IN ('queued', 'assigned')
                        """,
                        (row["assignment_status"], row["run_id"]),
                    )
                    if row["bound_host_id"]:
                        self._insert_host_event(
                            connection,
                            host_id=str(row["bound_host_id"]),
                            rig_id=row["bound_rig_id"],
                            kind="run_row_reconciled",
                            actor=str(actor).strip(),
                            details={
                                "run_id": row["run_id"],
                                "assignment_id": row["assignment_id"],
                                "from": row["run_status"],
                                "to": row["assignment_status"],
                            },
                        )
        return self._jsonable({"applied": bool(apply), "actor": actor if apply else None, "rows": listed})

    def reap_expired_leases(self) -> list[str]:
        affected = []
        with self._connection() as connection:
            leases = connection.execute(
                """
                SELECT * FROM rig_leases
                WHERE released_at IS NULL AND expires_at <= now()
                FOR UPDATE SKIP LOCKED
                """
            ).fetchall()
            for lease in leases:
                affected.append(str(lease["assignment_id"]))
                self._interrupt_lease(connection, lease, release_reason="lease_expired")
        return affected

    def report_interrupted_attempt(
        self,
        assignment_id: str,
        *,
        attempt_no: int,
        fencing: int,
        host_id: str,
        rig_id: str,
        generation: int,
        reason: str,
        detail: str = "",
    ) -> dict[str, Any]:
        """Agent 报告本地只有「开始」记录的 Attempt 已中断：不等 Lease 过期即按回收处置（BL-240）。

        新的 Agent 进程重连时，本地执行日志里只有 ``assignment_started`` 而没有完成或停止记录的
        Attempt 不再重放，改由本方法处置：释放 Lease（``release_reason='agent_interrupted'``），
        Attempt、Assignment 与 ``runs`` 行收为 ``indeterminate``，登记 ``interrupted_attempt_closures``，
        Rig 进入 ``quarantine``。与 ``reap_expired_leases`` 同一处置，只是不等租约到期。
        Lease 释放后，该 Fencing 的设备写入、心跳与完成都由 ``_current_lease`` 拒绝。

        幂等：Lease 已释放（被回收、已报告或已完成）时不改任何行，返回当前 Assignment 记录。
        Lease 按身份加行锁，与 ``reap_expired_leases`` 的 ``SKIP LOCKED`` 串行。
        """
        if reason not in _INTERRUPT_REASONS:
            raise ControlStateError(f"interrupted attempt reason is not accepted: {reason}")
        with self._connection() as connection:
            lease = connection.execute(
                """
                SELECT * FROM rig_leases
                WHERE assignment_id = %s AND attempt_no = %s AND fencing = %s
                FOR UPDATE
                """,
                (assignment_id, attempt_no, fencing),
            ).fetchone()
            if lease is None:
                raise StaleFencingError(
                    f"stale lease or fencing: {assignment_id}/{attempt_no}/{fencing}"
                )
            attempt = connection.execute(
                "SELECT host_id FROM assignment_attempts WHERE assignment_id = %s AND attempt_no = %s",
                (assignment_id, attempt_no),
            ).fetchone()
            if (
                attempt is None
                or str(attempt["host_id"]) != host_id
                or str(lease["rig_id"]) != rig_id
                or int(lease["rig_generation"]) != generation
            ):
                raise StaleFencingError(
                    f"interrupted attempt identity mismatch: {assignment_id}/{attempt_no}/{fencing}"
                )
            if lease["released_at"] is not None:
                return self._get_assignment(connection, assignment_id)
            self._interrupt_lease(connection, lease, release_reason="agent_interrupted")
            self._insert_host_event(
                connection,
                host_id=host_id,
                rig_id=rig_id,
                kind="attempt_interrupted_reported",
                actor="rig-agent",
                details={
                    "assignment_id": assignment_id,
                    "attempt_no": attempt_no,
                    "fencing": fencing,
                    "generation": generation,
                    "reason": reason,
                    "detail": detail or reason,
                },
            )
            return self._get_assignment(connection, assignment_id)

    def _interrupt_lease(
        self, connection: psycopg.Connection, lease: Mapping[str, Any], *, release_reason: str,
    ) -> None:
        """释放一个未释放的 Lease 并把它的 Attempt 收为 ``indeterminate``；调用方持有 Lease 行锁。

        Lease 过期回收与 Agent 报告中断（BL-240）共用：终态与结果不改写，Rig 进入隔离，
        被中断的非恢复 Attempt 登记资源闭包（BL-076 G1/G2）。
        """
        from .interrupted_closure import fail_expired_closure, register_interrupted
        connection.execute(
            """
            UPDATE rig_leases SET released_at = now(), release_reason = %s
            WHERE lease_id = %s
            """,
            (release_reason, lease["lease_id"]),
        )
        interrupted = connection.execute(
            """
            UPDATE assignment_attempts
            SET status = 'indeterminate', ended_at = now()
            WHERE assignment_id = %s AND attempt_no = %s AND status NOT IN
                ('passed','failed','error','blocked','cancelled','indeterminate')
            RETURNING assignment_id
            """,
            (lease["assignment_id"], lease["attempt_no"]),
        ).fetchone()
        kind = connection.execute(
            "SELECT kind FROM assignments WHERE assignment_id = %s", (lease["assignment_id"],),
        ).fetchone()
        if kind is not None and kind["kind"] == "recovery":
            # 闭包恢复 Assignment 自身失联：它负责的被中断 Attempt 回到可重新请求的 failed（BL-076）。
            fail_expired_closure(
                connection, lease["assignment_id"], lease["attempt_no"],
                "closure_lease_expired" if release_reason == "lease_expired" else "closure_agent_interrupted",
            )
        elif interrupted is not None:
            # 被中断 Attempt 的资源闭包登记（BL-076 G1/G2）；终态与结果不改写。
            register_interrupted(connection, lease["assignment_id"], lease["attempt_no"])
        expired = connection.execute(
            """
            UPDATE assignments SET status = 'indeterminate', updated_at = now()
            WHERE assignment_id = %s AND current_attempt_no = %s
            RETURNING assignment_id
            """,
            (lease["assignment_id"], lease["attempt_no"]),
        ).fetchone()
        if expired is not None:
            # runs 行随 Assignment 收为 indeterminate，不停在 assigned（BL-214）；
            # result 保持 NULL：没有可信的 Run Result。
            connection.execute(
                """
                UPDATE runs SET status = 'indeterminate', updated_at = now()
                WHERE assignment_id = %s AND status IN ('queued', 'assigned')
                """,
                (lease["assignment_id"],),
            )
        connection.execute(
            "UPDATE rigs SET status = 'quarantine' WHERE rig_id = %s",
            (lease["rig_id"],),
        )
        interrupted_recovery = connection.execute(
            "UPDATE recovery_incidents "
            "SET state = 'quarantined', "
            "    diagnostics = COALESCE(diagnostics, '{}'::jsonb) || '{\"guest_unresponsive\": true}'::jsonb, "
            "    version = version + 1, updated_at = now() "
            "WHERE recovery_assignment_id = %s AND recovery_attempt_no = %s "
            "  AND state IN ('recovering', 'waiting_lease') "
            "RETURNING rig_id",
            (lease["assignment_id"], lease["attempt_no"]),
        ).fetchone()
        if interrupted_recovery is not None:
            rig_row = connection.execute(
                "SELECT host_id FROM rigs WHERE rig_id = %s",
                (interrupted_recovery["rig_id"],),
            ).fetchone()
            if rig_row is not None:
                self._evaluate_breaker(connection, str(rig_row["host_id"]))

    def advance_recoveries(self):
        from .recovery import RecoveryCoordinator
        coordinator = RecoveryCoordinator(self)
        for recovery_id in coordinator.pending():
            try:
                coordinator.resume(recovery_id)
            except (ControlStateError, StaleFencingError):
                continue

    def sync_rig(self, host_id: str, rig_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT assignment_id FROM assignments
                WHERE bound_host_id = %s AND bound_rig_id = %s
                  AND status IN ('assigned', 'running', 'cancel_requested')
                ORDER BY created_at
                """,
                (host_id, rig_id),
            ).fetchall()
            return [self._get_assignment(connection, row["assignment_id"]) for row in rows]

    def validate_device_write(
        self,
        assignment_id: str,
        *,
        attempt_no: int,
        fencing: int,
        host_id: str,
        rig_id: str,
        generation: int,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            lease = self._current_lease(
                connection,
                assignment_id,
                attempt_no,
                fencing,
                lock=False,
            )
            self._assert_no_quiesce_failure(connection, assignment_id, attempt_no)
            assignment = connection.execute(
                "SELECT * FROM assignments WHERE assignment_id = %s",
                (assignment_id,),
            ).fetchone()
            valid = bool(
                assignment is not None
                and assignment["status"] in {"assigned", "running", "cancel_requested"}
                and assignment["bound_host_id"] == host_id
                and assignment["bound_rig_id"] == rig_id
                and int(assignment["bound_generation"] or 0) == generation
                and lease["rig_id"] == rig_id
                and int(lease["rig_generation"]) == generation
            )
            if not valid:
                raise StaleFencingError(
                    f"device target or lease is stale: {assignment_id}/{attempt_no}/{fencing}"
                )
            return {
                "valid": True,
                "assignment_id": assignment_id,
                "attempt_no": attempt_no,
                "fencing": fencing,
                "host_id": host_id,
                "rig_id": rig_id,
                "generation": generation,
            }

    def create_reservation(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        reservation_id = str(spec.get("reservation_id") or f"RSV-{uuid.uuid4().hex}")
        rig_id = str(spec.get("rig_id") or "")
        if not rig_id:
            raise ControlStateError("reservation rig_id is required")
        with self._connection() as connection:
            self._expire_reservations(connection)
            rig = connection.execute(
                "SELECT rig_id FROM rigs WHERE rig_id = %s AND deregistration IS NULL FOR UPDATE",
                (rig_id,),
            ).fetchone()
            if rig is None:
                raise ControlStateError(f"rig not found: {rig_id}")
            active = connection.execute(
                """
                SELECT reservation_id FROM rig_reservations
                WHERE rig_id = %s AND status = 'active'
                """,
                (rig_id,),
            ).fetchone()
            if active is not None:
                raise ControlStateError(f"rig already has an active reservation: {rig_id}")
            row = connection.execute(
                """
                INSERT INTO rig_reservations(
                    reservation_id, rig_id, owner, status, expires_at
                ) VALUES (%s, %s, %s, 'active', %s)
                RETURNING *
                """,
                (
                    reservation_id,
                    rig_id,
                    Jsonb(dict(spec.get("owner") or {})),
                    spec.get("expires_at"),
                ),
            ).fetchone()
        return self._jsonable(dict(row))

    def release_reservation(self, reservation_id: str, actor: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """
                UPDATE rig_reservations
                SET status = 'released', released_at = now(), release_reason = %s
                WHERE reservation_id = %s AND status = 'active'
                RETURNING *
                """,
                (f"released_by:{actor}", reservation_id),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM rig_reservations WHERE reservation_id = %s",
                    (reservation_id,),
                ).fetchone()
            if row is None:
                raise ControlStateError(f"reservation not found: {reservation_id}")
        return self._jsonable(dict(row))

    def list_reservations(self, *, statuses: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """列出预留，供 `rig_reservations` 与 console 会话的对账用（§十三）。"""
        where = " WHERE status = ANY(%s)" if statuses else ""
        params = (list(statuses),) if statuses else ()
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM rig_reservations{where} ORDER BY created_at ASC", params,
            ).fetchall()
        return [self._jsonable(dict(row)) for row in rows]

    def report_screen_quarantine(
        self,
        host_id: str,
        rig_id: str,
        *,
        generation: int,
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        """记录 Agent 上报的 ScreenLock 隔离标记状态（BL-224）。

        标记在 Agent 所在宿主的锁根里；控制面只保存最近一次上报，提交与派单据此早退。
        身份按已握手的 host、rig、generation 核对；状态变化时写一条 host 事件。
        """
        value = dict(state)
        if type(value.get("quarantined")) is not bool:
            raise ControlStateError("screen quarantine report requires boolean quarantined")
        if not isinstance(value.get("markers"), list):
            raise ControlStateError("screen quarantine report requires a markers list")
        with self._connection() as connection:
            previous = connection.execute(
                "SELECT screen_quarantine FROM rigs WHERE rig_id = %s AND host_id = %s AND generation = %s "
                "FOR UPDATE",
                (rig_id, host_id, generation),
            ).fetchone()
            if previous is None:
                raise StaleFencingError(f"stale rig generation: {rig_id}/{generation}")
            row = connection.execute(
                """
                UPDATE rigs SET screen_quarantine = %s::jsonb || jsonb_build_object('reported_at', now())
                WHERE rig_id = %s
                RETURNING screen_quarantine
                """,
                (Jsonb(value), rig_id),
            ).fetchone()
            before = previous["screen_quarantine"]
            if not isinstance(before, Mapping) or before.get("quarantined") is not value["quarantined"]:
                self._insert_host_event(
                    connection, host_id=host_id, rig_id=rig_id,
                    kind="screen_quarantine_reported", actor="rig-agent",
                    details={"quarantined": value["quarantined"], "markers": value["markers"]},
                )
        return self._jsonable(dict(row["screen_quarantine"]))

    def list_rigs(self, *, include_deregistered: bool = False) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT r.*, h.status AS host_status, h.max_active_rigs,
                    r.status AS registered_status,
                    COALESCE(r.agent_connected_until > now(), false) AS agent_connected,
                    CASE WHEN r.status = 'online'
                        AND NOT COALESCE(r.agent_connected_until > now(), false)
                        THEN 'offline' ELSE r.status END AS status
                FROM rigs r JOIN hosts h ON h.host_id = r.host_id
                WHERE (%s OR r.deregistration IS NULL)
                ORDER BY r.rig_id
                """,
                (include_deregistered,),
            ).fetchall()
        return [self._jsonable({k: v for k, v in row.items() if k != "agent_connection_id"})
                for row in rows]

    def begin_agent_connection(
        self, host_id: str, rig_id: str, generation: int, connection_id: str,
    ) -> None:
        with self._connection() as connection:
            row = connection.execute(
                """
                UPDATE rigs SET agent_connection_id = %s,
                    agent_connected_until = now() + interval '60 seconds'
                WHERE rig_id = %s AND host_id = %s AND generation = %s
                    AND deregistration IS NULL RETURNING rig_id
                """, (connection_id, rig_id, host_id, generation),
            ).fetchone()
            if row is None:
                raise ControlStateError(f"rig_connection_identity_rejected: {rig_id}")

    def refresh_agent_connection(self, rig_id: str, connection_id: str) -> bool:
        with self._connection() as connection:
            return connection.execute(
                """
                UPDATE rigs SET agent_connected_until = now() + interval '60 seconds'
                WHERE rig_id = %s AND agent_connection_id = %s
                    AND deregistration IS NULL RETURNING rig_id
                """, (rig_id, connection_id),
            ).fetchone() is not None

    def end_agent_connection(self, rig_id: str, connection_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE rigs SET agent_connection_id = NULL, agent_connected_until = NULL
                WHERE rig_id = %s AND agent_connection_id = %s
                """, (rig_id, connection_id),
            )

    def deregister_rig(
        self, rig_id: str, generation: int, *, actor: str, reason: str,
    ) -> dict[str, Any]:
        """Retire an idle, disconnected identity without deleting its audit references."""
        if not actor.strip() or not reason.strip():
            raise ControlStateError("rig_deregister_actor_and_reason_required")
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT *, COALESCE(agent_connected_until > now(), false) AS connected
                FROM rigs WHERE rig_id = %s FOR UPDATE
                """, (rig_id,),
            ).fetchone()
            if row is None:
                raise ControlStateError(f"rig_not_found: {rig_id}")
            if row["generation"] != generation:
                raise StaleFencingError(f"stale rig generation: {rig_id}/{generation}")
            if row["deregistration"] is not None:
                return self._jsonable(row["deregistration"])
            if row["connected"]:
                raise ControlStateError(f"rig_agent_connected: {rig_id}")
            host = connection.execute(
                "SELECT max_active_rigs, admission_mode, environment_observation FROM hosts WHERE host_id = %s FOR UPDATE",
                (row["host_id"],),
            ).fetchone()
            if host["max_active_rigs"] > 1 and not self._environment_policy(host):
                raise ControlStateError("lower_host_capacity_before_rig_deregister")
            occupied = connection.execute(
                """
                SELECT 1 FROM rig_leases WHERE rig_id = %s AND released_at IS NULL
                UNION ALL SELECT 1 FROM rig_reservations WHERE rig_id = %s AND status = 'active'
                UNION ALL SELECT 1 FROM assignments
                    WHERE (target_rig_id = %s OR bound_rig_id = %s)
                    AND NOT (status = ANY(%s))
                UNION ALL SELECT 1 FROM access_grants
                    WHERE status <> 'closed' AND device IN (
                        SELECT rig_id FROM rigs WHERE rig_id = %s
                        UNION SELECT regexp_replace(vm_dir, '^.*/', '')
                            FROM rig_targets WHERE rig_id = %s)
                LIMIT 1
                """, (rig_id, rig_id, rig_id, rig_id, list(_TERMINAL_ASSIGNMENTS),
                       rig_id, rig_id),
            ).fetchone()
            if occupied is not None:
                raise ControlStateError(f"rig_in_use: {rig_id}")
            receipt = connection.execute(
                """
                UPDATE rigs SET status = 'offline',
                    agent_connection_id = NULL, agent_connected_until = NULL,
                    deregistration = jsonb_build_object(
                        'rig_id', rig_id, 'rig_generation', generation,
                        'actor', %s::text, 'reason', %s::text, 'deregistered_at', now(),
                        'target', (SELECT to_jsonb(t) FROM rig_targets t WHERE t.rig_id = rigs.rig_id))
                WHERE rig_id = %s RETURNING deregistration
                """, (actor, reason, rig_id),
            ).fetchone()["deregistration"]
            connection.execute("DELETE FROM rig_targets WHERE rig_id = %s", (rig_id,))
        return self._jsonable(receipt)

    def publish_assignment_observed_state(
        self, assignment_id: str, *, attempt_no: int, fencing: int,
        host_id: str, rig_id: str, generation: int,
        observed_state: Mapping[str, Any], capability_state: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate the live Lease and publish under the same transaction locks."""
        with self._connection() as connection:
            lease = self._current_lease(connection, assignment_id, attempt_no, fencing, lock=True)
            self._assert_no_quiesce_failure(connection, assignment_id, attempt_no)
            assignment = connection.execute(
                "SELECT * FROM assignments WHERE assignment_id = %s", (assignment_id,),
            ).fetchone()
            if (assignment is None
                    or assignment["status"] not in {"assigned", "running"}
                    or assignment["bound_host_id"] != host_id
                    or assignment["bound_rig_id"] != rig_id
                    or assignment["bound_generation"] != generation
                    or lease["rig_id"] != rig_id or lease["rig_generation"] != generation):
                raise StaleFencingError("stale installation observation authority")
            row = connection.execute(
                """UPDATE rigs SET observed_state = %s, capability_state = %s, last_seen = now()
                   WHERE rig_id = %s AND host_id = %s AND generation = %s
                     AND EXISTS (SELECT 1 FROM rig_leases
                         WHERE assignment_id = %s AND attempt_no = %s AND fencing = %s
                           AND released_at IS NULL AND expires_at > clock_timestamp())
                   RETURNING *""",
                (Jsonb(dict(observed_state)), Jsonb(dict(capability_state)), rig_id, host_id, generation,
                 assignment_id, attempt_no, fencing),
            ).fetchone()
            if row is None:
                raise StaleFencingError("stale installation observation Rig")
        return self._jsonable(dict(row))

    def update_observed_state(
        self,
        rig_id: str,
        *,
        generation: int,
        observed_state: Mapping[str, Any],
        capability_state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """
                UPDATE rigs SET observed_state = %s, capability_state = %s, last_seen = now()
                WHERE rig_id = %s AND generation = %s
                RETURNING *
                """,
                (
                    Jsonb(dict(observed_state)),
                    Jsonb(dict(capability_state or {})),
                    rig_id,
                    generation,
                ),
            ).fetchone()
            if row is None:
                raise StaleFencingError(f"stale rig generation: {rig_id}/{generation}")
        return self._jsonable(dict(row))

    def record_rig_recovery(
        self,
        rig_id: str,
        *,
        generation: int,
        actor: str,
        probe_result: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not actor:
            raise ControlStateError("recovery actor is required")
        recovered = bool(probe_result.get("passed"))
        decision = "recovered" if recovered else "remain_quarantined"
        receipt_id = f"RR-{uuid.uuid4().hex}"
        with self._connection() as connection:
            rig = connection.execute(
                "SELECT * FROM rigs WHERE rig_id = %s FOR UPDATE",
                (rig_id,),
            ).fetchone()
            if rig is None:
                raise ControlStateError(f"rig not found: {rig_id}")
            if int(rig["generation"]) != generation:
                raise StaleFencingError(f"stale rig generation: {rig_id}/{generation}")
            if rig["status"] != "quarantine":
                raise ControlStateError(f"rig is not quarantined: {rig_id}")
            incident = connection.execute("SELECT 1 FROM recovery_incidents WHERE rig_id = %s AND state NOT IN ('recovered','isolation_confirmed')",
                                          (rig_id,)).fetchone()
            if incident is not None:
                raise ControlStateError('active down-edge recovery requires a Recovery Assignment')
            # BL-076 G2：关联该 Rig 当前 Generation 下被中断（Lease 过期）且未闭包的 Attempt。
            from .interrupted_closure import open_closures, request_closure, summaries
            closures = open_closures(connection, rig_id, generation)
            in_progress = [row for row in closures if row["state"] in {"waiting_lease", "closing"}]
            if in_progress:
                raise ControlStateError(
                    "interrupted attempt closure in progress: "
                    f"{in_progress[0]['closure_assignment_id']}"
                )
            if closures and recovered:
                # Probe Result 只证明 guest 就绪，不证明被中断 Attempt 的资源已闭包：先由新 Fencing
                # 下的闭包恢复 Assignment 处置，Rig 在其完成前保持 quarantine。
                interrupted_closure = request_closure(connection, self, rig, actor, closures)
                recovered = False
                decision = "remain_quarantined"
            elif closures:
                interrupted_closure = {"status": "not_requested", "reason": "probe_not_passed",
                                       "attempts": summaries(closures)}
            else:
                interrupted_closure = {"status": "not_required", "attempts": []}
            receipt = connection.execute(
                """
                INSERT INTO rig_recovery_receipts(
                    receipt_id, rig_id, rig_generation, actor, probe_result, decision, interrupted_closure
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    receipt_id,
                    rig_id,
                    generation,
                    actor,
                    Jsonb(dict(probe_result)),
                    decision,
                    Jsonb(interrupted_closure),
                ),
            ).fetchone()
            if recovered:
                connection.execute(
                    "UPDATE rigs SET status = 'online', last_seen = now() WHERE rig_id = %s",
                    (rig_id,),
                )
        return self._jsonable(dict(receipt))

    def list_rig_recovery_receipts(self, rig_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM rig_recovery_receipts
                WHERE rig_id = %s ORDER BY created_at, receipt_id
                """,
                (rig_id,),
            ).fetchall()
        return [self._jsonable(dict(row)) for row in rows]

    # MARK: - Host anomalies

    def record_host_anomaly(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Idempotent on ``anomaly_id``: a later write carries the finished bundle state."""
        with self._connection() as connection:
            row = connection.execute(
                """
                INSERT INTO host_anomalies(
                    anomaly_id, host_id, kind, correlation_id, detected_at, fingerprint, record
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (anomaly_id) DO UPDATE SET record = excluded.record
                RETURNING anomaly_id, host_id, kind, correlation_id, detected_at, fingerprint
                """,
                (
                    record["anomaly_id"],
                    record["host_id"],
                    record["kind"],
                    record["correlation_id"],
                    record["detected_at"],
                    record["fingerprint"],
                    Jsonb(dict(record)),
                ),
            ).fetchone()
            # 不等下一次派单：熔断记录要和事故时间对得上。
            if record["kind"] in {"rig_down", "vm_fatal"}:
                self._evaluate_breaker(connection, str(record["host_id"]))
        return self._jsonable(dict(row))

    def record_host_load_samples(self, host_id: str, samples: Sequence[Mapping[str, Any]]) -> int:
        columns = (
            "sampled_at", "interval_seconds", "running_vm_count", "active_rigs",
            "memory_free_percent", "pressure_level", "swap_used_bytes", "load_1m", "fingerprint",
        )
        rows = [
            (host_id, *(sample.get(column) for column in columns),
             Jsonb(sample["running_vms"]) if sample.get("running_vms") is not None else None)
            for sample in samples
        ]
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO host_load_samples(
                    host_id, sampled_at, interval_seconds, running_vm_count, active_rigs,
                    memory_free_percent, pressure_level, swap_used_bytes, load_1m, fingerprint,
                    running_vms
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (host_id, sampled_at) DO NOTHING
                """,
                rows,
            )
        return len(samples)

    def host_active_work(self, host_id: str, *, recent_seconds: int = 0) -> list[dict[str, Any]]:
        """Work holding a Lease, plus work that released one in the last ``recent_seconds``.

        VM 被杀后任务约 1 秒就报错结束，健康检查要约 20 秒才发现掉线。只看未释放的 Lease，
        记录就关联不到被打断的任务。
        """
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT l.rig_id, l.assignment_id, l.attempt_no, l.created_at AS lease_started_at,
                       l.released_at, l.release_reason,
                       a.kind, attempt.status AS attempt_status, attempt.stage_results
                FROM rig_leases l
                JOIN rigs r ON r.rig_id = l.rig_id
                JOIN assignments a ON a.assignment_id = l.assignment_id
                JOIN assignment_attempts attempt
                  ON attempt.assignment_id = l.assignment_id AND attempt.attempt_no = l.attempt_no
                WHERE r.host_id = %s AND (
                    l.released_at IS NULL
                    OR l.released_at > now() - make_interval(secs => %s)
                )
                ORDER BY l.rig_id, l.created_at
                """,
                (host_id, recent_seconds),
            ).fetchall()
        work = []
        for row in rows:
            item = dict(row)
            item["stages"] = sorted(item.pop("stage_results") or {})
            work.append(self._jsonable(item))
        return work

    def list_host_anomalies(
        self,
        host_id: str,
        *,
        kind: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT anomaly_id, host_id, kind, correlation_id, detected_at, fingerprint,
                       record->'rigs' AS rigs,
                       record->'details' AS details,
                       record->'bundle'->>'status' AS bundle_status,
                       (record->'resources'->>'running_vm_count')::int AS running_vm_count,
                       (SELECT count(*) FROM host_anomaly_reviews review
                        WHERE review.anomaly_id = host_anomalies.anomaly_id) AS review_count
                FROM host_anomalies
                WHERE host_id = %s
                  AND (%s::text IS NULL OR kind = %s)
                  AND (%s::timestamptz IS NULL OR detected_at >= %s)
                ORDER BY detected_at DESC, anomaly_id DESC
                LIMIT %s
                """,
                (host_id, kind, kind, since, since, limit),
            ).fetchall()
        return [self._jsonable(dict(row)) for row in rows]

    def _fatal_evidence(self, connection, record):
        """Show PID observations and nearby down edges, without assigning a cause."""
        host, at = record["host_id"], record["detected_at"]
        details = record.get("details") or {}
        pid = details.get("pid")
        samples = []
        if type(pid) is int and pid > 0:
            samples = connection.execute(
                """
                SELECT s.sampled_at, vm FROM host_load_samples s
                CROSS JOIN LATERAL jsonb_array_elements(s.running_vms) vm
                WHERE host_id = %s AND sampled_at BETWEEN %s::timestamptz - interval '120 seconds'
                    AND %s::timestamptz AND vm->'helper_pids' @> %s::jsonb
                ORDER BY sampled_at DESC
                """, (host, at, at, Jsonb([pid])),
            ).fetchall()
        # Historical rows may have only the original watcher attribution.
        rigs = record.get("rigs") or []
        binding = details.get("pid_binding") or {}
        basis = "live_disk_owner" if binding.get("status") == "live_disk_owner" else "recorded_rig"
        if not rigs:
            rigs = [row["vm"] for row in samples]
            basis = "prior_pid_sample" if rigs else "unresolved"
        identities = {(r["rig_id"], r["generation"]) for r in rigs
                      if r.get("rig_id") and type(r.get("generation")) is int}
        if not identities:
            basis = "unresolved"
        elif basis == "prior_pid_sample" and len(identities) > 1:
            basis = "ambiguous_prior_pid_samples"
        incidents = []
        for rig, generation in sorted(identities):
            incidents.extend(connection.execute(
                """
                SELECT r.recovery_id, r.rig_id, r.rig_generation, r.edge_id, r.actor,
                       r.state, r.created_at, r.updated_at, r.old_assignment_id, r.old_attempt_no,
                       interrupted.result->>'rig_disposition' AS interrupted_rig_disposition,
                       r.diagnostics, r.isolation_receipt, r.recovery_assignment_id, t.attempt_no, t.fencing,
                       t.status AS attempt_status, t.result->'recovery_receipt' AS recovery_receipt,
                       t.result->'artifact_refs' AS artifact_refs, e.event_id AS isolation_event_id,
                       e.details AS isolation_event, d.anomaly_id AS down_anomaly_id, d.detected_at AS down_at
                FROM recovery_incidents r
                JOIN host_coordination_events e ON e.host_id = %s AND e.rig_id = r.rig_id
                    AND e.kind = 'recovery_observed' AND e.details->>'recovery_id' = r.recovery_id
                LEFT JOIN host_anomalies d ON d.host_id = e.host_id AND d.kind = 'rig_down'
                    AND d.record->'details'->>'edge_id' = r.edge_id::text
                    AND d.record->'rigs' @> jsonb_build_array(jsonb_build_object(
                        'rig_id', r.rig_id, 'generation', r.rig_generation))
                LEFT JOIN assignment_attempts interrupted ON interrupted.assignment_id = r.old_assignment_id
                    AND interrupted.attempt_no = r.old_attempt_no
                LEFT JOIN assignment_attempts t ON t.assignment_id = r.recovery_assignment_id
                    AND t.rig_id = r.rig_id AND t.rig_generation = r.rig_generation
                WHERE r.rig_id = %s AND r.rig_generation = %s
                    AND COALESCE(d.detected_at, r.created_at) BETWEEN %s::timestamptz - interval '120 seconds'
                                         AND %s::timestamptz + interval '120 seconds'
                ORDER BY r.created_at, t.attempt_no
                """, (host, rig, generation, at, at),
            ).fetchall())
        return {"window_seconds": 120, "identity_basis": basis, "prior_pid_samples": samples, "nearby_recoveries": incidents}

    def get_host_anomaly(self, anomaly_id: str) -> dict[str, Any]:
        """The record, its reviews, and what later happened to it.

        结果由现有表推导，不另存一份：down 边沿对应 recovery_incidents，当时在跑的
        任务看它现在的 Attempt 和 Assignment 状态。
        """
        with self._connection() as connection:
            row = connection.execute(
                "SELECT record FROM host_anomalies WHERE anomaly_id = %s", (anomaly_id,),
            ).fetchone()
            if row is None:
                raise ControlStateError(f"Host anomaly not found: {anomaly_id}")
            record = row["record"]
            reviews = connection.execute(
                """
                SELECT review_id, actor, labels, note, created_at FROM host_anomaly_reviews
                WHERE anomaly_id = %s ORDER BY created_at, review_id
                """,
                (anomaly_id,),
            ).fetchall()
            recovery = None
            edge_id = (record.get("details") or {}).get("edge_id")
            if edge_id:
                recovery = connection.execute(
                    """
                    SELECT recovery_id, state, old_assignment_id, old_attempt_no,
                           recovery_assignment_id, created_at, updated_at
                    FROM recovery_incidents WHERE edge_id = %s
                    """,
                    (edge_id,),
                ).fetchone()
            fatal = self._fatal_evidence(connection, record) if record.get("kind") == "vm_fatal" else None
            outcomes = []
            for item in (record.get("concurrency") or {}).get("work") or []:
                outcome = connection.execute(
                    """
                    SELECT a.assignment_id, a.status AS assignment_status,
                           attempt.status AS attempt_status,
                           (SELECT max(latest.attempt_no) FROM assignment_attempts latest
                            WHERE latest.assignment_id = a.assignment_id) AS latest_attempt_no
                    FROM assignments a
                    JOIN assignment_attempts attempt
                      ON attempt.assignment_id = a.assignment_id AND attempt.attempt_no = %s
                    WHERE a.assignment_id = %s
                    """,
                    (item["attempt_no"], item["assignment_id"]),
                ).fetchone()
                if outcome is not None:
                    outcomes.append(dict(outcome))
        return self._jsonable({
            "record": record,
            "reviews": [dict(review) for review in reviews],
            "outcome": {"recovery": dict(recovery) if recovery else None, "work": outcomes,
                        **({"vm_fatal_evidence": fatal} if fatal is not None else {})},
        })

    def add_host_anomaly_review(
        self, anomaly_id: str, *, actor: str, note: str, labels: Sequence[str] = (),
    ) -> dict[str, Any]:
        if not actor or not note:
            raise ControlStateError("Host anomaly review actor and note are required")
        with self._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM host_anomalies WHERE anomaly_id = %s", (anomaly_id,),
            ).fetchone() is None:
                raise ControlStateError(f"Host anomaly not found: {anomaly_id}")
            row = connection.execute(
                """
                INSERT INTO host_anomaly_reviews(review_id, anomaly_id, actor, labels, note)
                VALUES (%s, %s, %s, %s, %s) RETURNING *
                """,
                (f"HAR-{uuid.uuid4().hex}", anomaly_id, actor, Jsonb(list(labels)), note),
            ).fetchone()
        return self._jsonable(dict(row))

    def reviewed_host_anomaly_ids(self, host_id: str) -> set[str]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT review.anomaly_id FROM host_anomaly_reviews review
                JOIN host_anomalies anomaly ON anomaly.anomaly_id = review.anomaly_id
                WHERE anomaly.host_id = %s
                """,
                (host_id,),
            ).fetchall()
        return {str(row["anomaly_id"]) for row in rows}

    def host_anomaly_report(
        self, host_id: str, *, since: str | None = None, include_test: bool = False,
    ) -> dict[str, Any]:
        """Rates per VM-hour by concurrency, kind and environment.

        同一 correlation 算一次事故，事件数单独列出。没有样本的并发数发生率为 null，
        不写成 0：没有观测不等于没有异常。复盘标签为 ``test`` 或以 ``-test`` 结尾的记录
        默认不计，数量单独列出；测试期间的样本仍算在暴露量里。
        """
        with self._connection() as connection:
            exposure_rows = connection.execute(
                """
                SELECT running_vm_count, fingerprint,
                       sum(running_vm_count * interval_seconds) / 3600.0 AS vm_hours,
                       sum(interval_seconds) AS observed_seconds
                FROM host_load_samples
                WHERE host_id = %s AND (%s::timestamptz IS NULL OR sampled_at >= %s)
                GROUP BY running_vm_count, fingerprint
                """,
                (host_id, since, since),
            ).fetchall()
            anomaly_rows = connection.execute(
                """
                SELECT kind, fingerprint, correlation_id, record,
                       EXISTS (
                           SELECT 1 FROM host_anomaly_reviews review,
                                jsonb_array_elements_text(review.labels) AS label
                           WHERE review.anomaly_id = host_anomalies.anomaly_id
                             AND (label = 'test' OR label LIKE '%%-test')
                       ) AS is_test
                FROM host_anomalies
                WHERE host_id = %s AND (%s::timestamptz IS NULL OR detected_at >= %s)
                """,
                (host_id, since, since),
            ).fetchall()
        excluded = sum(1 for row in anomaly_rows if row["is_test"]) if not include_test else 0
        if not include_test:
            anomaly_rows = [row for row in anomaly_rows if not row["is_test"]]
        exposure = {
            (int(row["running_vm_count"]), row["fingerprint"]): float(row["vm_hours"] or 0)
            for row in exposure_rows
        }
        groups: dict[tuple[Any, ...], dict[str, Any]] = {}
        by_stage: dict[str, int] = {}
        gap_seconds = 0
        for row in anomaly_rows:
            record = row["record"]
            details = record.get("details") or {}
            if row["kind"] == "observation_gap":
                gap_seconds += int(details.get("seconds") or 0)
                continue
            count = (record.get("resources") or {}).get("running_vm_count")
            key = (row["kind"], details.get("exception_type"), count, row["fingerprint"])
            group = groups.setdefault(key, {"events": 0, "correlations": set()})
            group["events"] += int(details.get("occurrences") or 1)
            group["correlations"].add(row["correlation_id"])
            for item in (record.get("concurrency") or {}).get("work") or []:
                stages = item.get("stages") or ["(none)"]
                label = f"{item.get('kind')}/{stages[-1]}"
                by_stage[label] = by_stage.get(label, 0) + 1
        rates = []
        for (kind, exception_type, count, fingerprint), group in sorted(
            groups.items(), key=lambda item: tuple(str(part) for part in item[0]),
        ):
            vm_hours = exposure.get((count, fingerprint)) if count is not None else None
            incidents = len(group["correlations"])
            rates.append({
                "kind": kind,
                "exception_type": exception_type,
                "running_vm_count": count,
                "fingerprint": fingerprint,
                "incidents": incidents,
                "events": group["events"],
                "vm_hours": vm_hours,
                "incidents_per_vm_hour": incidents / vm_hours if vm_hours else None,
            })
        return self._jsonable({
            "host_id": host_id,
            "since": since,
            "exposure": [
                {
                    "running_vm_count": int(row["running_vm_count"]),
                    "fingerprint": row["fingerprint"],
                    "vm_hours": float(row["vm_hours"] or 0),
                    "observed_seconds": int(row["observed_seconds"]),
                }
                for row in sorted(
                    exposure_rows, key=lambda r: (r["running_vm_count"], r["fingerprint"]),
                )
            ],
            "rates": rates,
            "by_task_stage": dict(sorted(by_stage.items())),
            "unobserved_gap_seconds": gap_seconds,
            "excluded_test_records": excluded,
        })

    # MARK: - Resource admission (shadow)

    def set_host_allocatable(
        self, host_id: str, *, memory_bytes: int, actor: str, reason: str,
    ) -> dict[str, Any]:
        if not actor or not reason:
            raise ControlStateError("Host allocatable actor and reason are required")
        if memory_bytes <= 0:
            raise ControlStateError("Host allocatable memory must be positive")
        with self._connection() as connection:
            host = connection.execute(
                "SELECT allocatable FROM hosts WHERE host_id = %s FOR UPDATE", (host_id,),
            ).fetchone()
            if host is None:
                raise ControlStateError(f"Host not found: {host_id}")
            allocatable = {"memory_bytes": memory_bytes}
            connection.execute(
                "UPDATE hosts SET allocatable = %s WHERE host_id = %s", (Jsonb(allocatable), host_id),
            )
            event = self._insert_host_event(
                connection, host_id=host_id, rig_id=None, kind="allocatable_changed", actor=actor,
                details={"previous": host["allocatable"], "current": allocatable, "reason": reason},
            )
        return self._jsonable({"allocatable": allocatable, "event": dict(event)})

    def report_rig_resources(self, host_id: str, requests: Mapping[str, Mapping[str, Any]]) -> None:
        with self._connection() as connection:
            for rig_id, request in requests.items():
                connection.execute(
                    """
                    UPDATE rigs SET resource_requests = %s
                    WHERE rig_id = %s AND host_id = %s
                      AND resource_requests IS DISTINCT FROM %s
                    """,
                    (Jsonb(dict(request)), rig_id, host_id, Jsonb(dict(request))),
                )

    def _record_admission_shadow(
        self,
        connection: psycopg.Connection,
        assignment: Mapping[str, Any],
        rig: Mapping[str, Any],
        capacity_reason: str | None,
        active_on_host: int,
    ) -> str | None:
        """Work out and record the resource decision; return it.

        容量模式下返回值没人用，这里失败不能影响派单。资源模式下失败就是不知道 Host 的状态，
        返回 ``resource_unavailable`` 拒绝派单。savepoint 让失败不会中止派单事务。
        """
        from .resource_admission import decide

        try:
            with connection.transaction():
                host_id = str(rig["host_id"])
                host = connection.execute(
                    """SELECT allocatable, environment_observation, qualified_environment,
                           environment_observed_at, now() AS now FROM hosts WHERE host_id = %s""", (host_id,),
                ).fetchone()
                sample = connection.execute(
                    """
                    SELECT sampled_at, pressure_level, running_vms FROM host_load_samples
                    WHERE host_id = %s ORDER BY sampled_at DESC LIMIT 1
                    """,
                    (host_id,),
                ).fetchone()
                leased = connection.execute(
                    """
                    SELECT occupied.rig_id, occupied.resource_requests
                    FROM rig_leases l JOIN rigs occupied ON occupied.rig_id = l.rig_id
                    WHERE l.released_at IS NULL AND occupied.host_id = %s
                    """,
                    (host_id,),
                ).fetchall()
                breaker_state = self._evaluate_breaker(connection, host_id)
                limit = concurrency_limit(breaker_state)

                inputs: dict[str, Any] = {"breaker": breaker_state}
                if leased and host["environment_observation"] is not None:
                    exclusive = not self._environment_allows(connection, rig, host)
                else:
                    exclusive = bool(leased) and self._host_requires_exclusive(connection, rig)
                if exclusive:
                    resource_reason = "host_exclusive"
                elif limit is not None and active_on_host >= limit:
                    resource_reason = "host_breaker"
                else:
                    resource_reason, decided = decide(
                        rig_id=str(rig["rig_id"]),
                        request=rig.get("resource_requests"),
                        allocatable=host["allocatable"],
                        sample=sample,
                        leased={row["rig_id"]: row["resource_requests"] for row in leased},
                        now=host["now"],
                    )
                    inputs.update(decided)
                connection.execute(
                    """
                    INSERT INTO host_admission_shadow(
                        bucket, host_id, assignment_id, rig_id,
                        capacity_decision, resource_decision, inputs
                    ) VALUES (date_trunc('minute', now()), %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (bucket, assignment_id, rig_id, capacity_decision, resource_decision)
                    DO UPDATE SET evaluations = host_admission_shadow.evaluations + 1
                    """,
                    (
                        host_id, assignment["assignment_id"], rig["rig_id"],
                        capacity_reason or "admit", resource_reason or "admit", Jsonb(inputs),
                    ),
                )
                return resource_reason
        except Exception as exc:  # noqa: BLE001 - the savepoint keeps the dispatch transaction usable
            log.warning("resource decision failed: %s: %s", type(exc).__name__, exc)
            return "resource_unavailable"

    # MARK: - Breaker and admission mode

    def _evaluate_breaker(
        self, connection: psycopg.Connection, host_id: str, *, force_open: str | None = None,
    ) -> str:
        """Advance the breaker if due and return its state. A change leaves a Host event."""
        # 第一条记录常常就是第一次事故触发的，since 用 epoch 才不会把这次事故排除在外。
        row = connection.execute(
            "SELECT state, since, now() AS now FROM host_breaker WHERE host_id = %s FOR UPDATE",
            (host_id,),
        ).fetchone()
        if row is None:
            row = connection.execute(
                """
                INSERT INTO host_breaker(host_id, state, since, reason)
                VALUES (%s, 'closed', 'epoch', 'initial')
                ON CONFLICT (host_id) DO UPDATE SET host_id = excluded.host_id
                RETURNING state, since, now() AS now
                """,
                (host_id,),
            ).fetchone()
        # 同一 correlation 是一次事故；标为测试的记录不算。VM 进程还活着的 rig_down 只有
        # 在 guest 确实无响应（显式 guest_unresponsive 或恢复 Assignment 未就绪/失败）时
        # 才算真实事故；瞬时负载导致的假掉线（恢复顺利 guest_ready）仍不触发熔断。
        crashes = connection.execute(
            """
            SELECT min(detected_at) AS at FROM host_anomalies a
            WHERE host_id = %s AND kind IN ('rig_down', 'vm_fatal') AND detected_at >= %s
              AND (
                  a.record->'details'->>'vm_alive' IS DISTINCT FROM 'true'
                  OR COALESCE(a.record->'details'->>'guest_unresponsive', '') = 'true'
                  OR EXISTS (
                      SELECT 1 FROM recovery_incidents ri
                      LEFT JOIN assignments ra ON ra.assignment_id = ri.recovery_assignment_id
                      WHERE ri.edge_id IS NOT NULL
                        AND ri.edge_id::text = a.record->'details'->>'edge_id'
                        AND (
                            COALESCE(ri.diagnostics->>'guest_unresponsive', '') = 'true'
                            OR ra.status IN ('failed', 'error', 'indeterminate')
                        )
                  )
              )
              AND NOT EXISTS (
                  SELECT 1 FROM host_anomaly_reviews review,
                       jsonb_array_elements_text(review.labels) AS label
                  WHERE review.anomaly_id = a.anomaly_id AND (label = 'test' OR label LIKE '%%-test')
              )
            GROUP BY correlation_id
            """,
            (host_id, row["since"]),
        ).fetchall()
        opens = connection.execute(
            """
            SELECT count(*) AS value FROM host_coordination_events
            WHERE host_id = %s AND kind = 'breaker_transition' AND details->>'to' IN ('open', 'locked')
              AND created_at > now() - make_interval(secs => %s)
            """,
            (host_id, host_breaker.LOCK_WINDOW.total_seconds()),
        ).fetchone()["value"]
        change = host_breaker.next_state(
            row["state"], row["since"],
            crashes=[item["at"] for item in crashes],
            opens_in_lock_window=int(opens),
            now=row["now"],
            force_open=force_open,
        )
        if change is None:
            return str(row["state"])
        return self._set_breaker(connection, host_id, row["state"], *change, actor="breaker")

    def _set_breaker(
        self, connection: psycopg.Connection, host_id: str, previous: str, state: str, reason: str,
        *, actor: str,
    ) -> str:
        connection.execute(
            "UPDATE host_breaker SET state = %s, since = now(), reason = %s WHERE host_id = %s",
            (state, reason, host_id),
        )
        self._insert_host_event(
            connection, host_id=host_id, rig_id=None, kind="breaker_transition", actor=actor,
            details={"from": previous, "to": state, "reason": reason},
        )
        log.warning("host breaker: host=%s %s -> %s (%s)", host_id, previous, state, reason)
        return state

    def host_breaker_state(self, host_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            state = self._evaluate_breaker(connection, host_id)
            row = connection.execute(
                "SELECT state, since, reason FROM host_breaker WHERE host_id = %s", (host_id,),
            ).fetchone()
        return self._jsonable({**dict(row), "state": state})

    def reset_host_breaker(self, host_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        """The only way out of ``locked``."""
        if not actor or not reason:
            raise ControlStateError("Host breaker reset actor and reason are required")
        with self._connection() as connection:
            previous = self._evaluate_breaker(connection, host_id)
            self._set_breaker(connection, host_id, previous, "closed", reason, actor=actor)
        return self.host_breaker_state(host_id)

    def set_host_admission_mode(
        self, host_id: str, mode: str, *, actor: str, reason: str,
    ) -> dict[str, Any]:
        if not actor or not reason:
            raise ControlStateError("Host admission mode actor and reason are required")
        if mode not in {"capacity", "resource"}:
            raise ControlStateError(f"unknown admission mode: {mode}")
        with self._connection() as connection:
            host = connection.execute(
                "SELECT admission_mode, allocatable FROM hosts WHERE host_id = %s FOR UPDATE", (host_id,),
            ).fetchone()
            if host is None:
                raise ControlStateError(f"Host not found: {host_id}")
            if mode == "resource" and not host["allocatable"]:
                raise ControlStateError("resource admission requires Host allocatable memory")
            connection.execute("UPDATE hosts SET admission_mode = %s WHERE host_id = %s", (mode, host_id))
            event = self._insert_host_event(
                connection, host_id=host_id, rig_id=None, kind="admission_mode_changed", actor=actor,
                details={"previous": host["admission_mode"], "current": mode, "reason": reason},
            )
        return self._jsonable({"admission_mode": mode, "event": dict(event)})

    # MARK: - Rerun after a crash

    def rerun_interrupted_work(self) -> list[dict[str, Any]]:
        """Decide once for each rig_down whether the work it interrupted runs again.

        只有 resource 模式的 Host 才自动重跑。当时刚结束的任务不一定是被打断的（步骤 B 第 2 次
        kill），所以要看到 Rig 被隔离或结果不确定，才算被打断。重跑后再次被打断时触发熔断。
        """
        with self._connection() as connection:
            anomalies = connection.execute(
                """
                SELECT a.anomaly_id, a.host_id, a.detected_at, a.record, now() AS now
                FROM host_anomalies a JOIN hosts h ON h.host_id = a.host_id
                LEFT JOIN host_anomaly_reruns done ON done.anomaly_id = a.anomaly_id
                WHERE a.kind = 'rig_down' AND done.anomaly_id IS NULL
                  AND h.admission_mode = 'resource'
                  AND a.detected_at > now() - interval '1 hour'
                ORDER BY a.detected_at
                """
            ).fetchall()
        settled = []
        for anomaly in anomalies:
            decisions = self._rerun_decisions(anomaly)
            if decisions is None:
                continue
            with self._connection() as connection:
                inserted = connection.execute(
                    """
                    INSERT INTO host_anomaly_reruns(anomaly_id, decisions) VALUES (%s, %s)
                    ON CONFLICT (anomaly_id) DO NOTHING RETURNING anomaly_id
                    """,
                    (anomaly["anomaly_id"], Jsonb(decisions)),
                ).fetchone()
                if inserted is not None and any(d["decision"] == "interrupted_again" for d in decisions):
                    self._evaluate_breaker(
                        connection, anomaly["host_id"], force_open="rerun interrupted again",
                    )
            settled.append({"anomaly_id": anomaly["anomaly_id"], "decisions": decisions})
        return settled

    def _rerun_decisions(self, anomaly: Mapping[str, Any]) -> list[dict[str, Any]] | None:
        """``None`` means an Attempt has not settled yet, so look again on a later pass."""
        record = anomaly["record"]
        rigs = {str(rig.get("rig_id")) for rig in record.get("rigs") or []}
        decisions = []
        for item in (record.get("concurrency") or {}).get("work") or []:
            if str(item.get("rig_id")) not in rigs or item.get("kind") != "run":
                continue
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT a.assignment_id, a.status, a.rerun_of, a.run_spec,
                           attempt.status AS attempt_status, attempt.ended_at, attempt.result
                    FROM assignments a JOIN assignment_attempts attempt USING (assignment_id)
                    WHERE a.assignment_id = %s AND attempt.attempt_no = %s
                    """,
                    (item["assignment_id"], item["attempt_no"]),
                ).fetchone()
            if row is None:
                continue
            if row["status"] not in _TERMINAL_ASSIGNMENTS:
                if anomaly["now"] - anomaly["detected_at"] < _RERUN_SETTLE_LIMIT:
                    return None
                decisions.append(_rerun_decision(row, "not_settled"))
                continue
            disposition = (row["result"] or {}).get("rig_disposition")
            interrupted = row["attempt_status"] == "indeterminate" or disposition == "quarantine"
            if not interrupted:
                decisions.append(_rerun_decision(row, "not_interrupted"))
            elif row["rerun_of"] is not None:
                decisions.append(_rerun_decision(row, "interrupted_again"))
            elif (row["run_spec"] or {}).get("type") not in _RERUNNABLE_RUN_TYPES:
                decisions.append(_rerun_decision(row, "type_not_rerunnable"))
            else:
                rerun = self.rerun_assignment(
                    row["assignment_id"], actor="auto-rerun", reason=f"rig_down {anomaly['anomaly_id']}",
                )
                decisions.append({**_rerun_decision(row, "rerun"), "rerun_id": rerun["assignment_id"]})
        return decisions

    def host_admission_shadow_report(self, host_id: str, *, since: str | None = None) -> dict[str, Any]:
        """Old and new decisions side by side, disagreements first with an example of each."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT capacity_decision, resource_decision, sum(evaluations) AS evaluations,
                       count(DISTINCT assignment_id) AS assignments,
                       min(bucket) AS first_at, max(bucket) AS last_at,
                       (array_agg(inputs ORDER BY bucket DESC))[1] AS latest_inputs
                FROM host_admission_shadow
                WHERE host_id = %s AND (%s::timestamptz IS NULL OR bucket >= %s)
                GROUP BY capacity_decision, resource_decision
                """,
                (host_id, since, since),
            ).fetchall()
        groups = [
            {**dict(row), "evaluations": int(row["evaluations"]),
             "agrees": row["capacity_decision"] == row["resource_decision"]
             or ("admit" not in (row["capacity_decision"], row["resource_decision"]))}
            for row in rows
        ]
        groups.sort(key=lambda row: (row["agrees"], -row["evaluations"]))
        return self._jsonable({
            "host_id": host_id,
            "since": since,
            "evaluations": sum(row["evaluations"] for row in groups),
            "disagreements": sum(row["evaluations"] for row in groups if not row["agrees"]),
            "groups": groups,
        })

    def host_snapshot(self, host_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            self._expire_host_maintenance(connection)
            self._expire_capacity_trials(connection)
            host = connection.execute(
                "SELECT * FROM hosts WHERE host_id = %s",
                (host_id,),
            ).fetchone()
            if host is None:
                raise ControlStateError(f"host not found: {host_id}")
            rigs = connection.execute(
                "SELECT * FROM rigs WHERE host_id = %s AND deregistration IS NULL ORDER BY rig_id",
                (host_id,),
            ).fetchall()
            targets = connection.execute(
                "SELECT * FROM rig_targets WHERE host_id = %s ORDER BY rig_id",
                (host_id,),
            ).fetchall()
            maintenance = connection.execute(
                """
                SELECT * FROM host_maintenance
                WHERE host_id = %s AND status = 'active'
                ORDER BY started_at DESC LIMIT 1
                """,
                (host_id,),
            ).fetchone()
            active_rigs = int(connection.execute(
                """
                SELECT count(DISTINCT l.rig_id) AS value
                FROM rig_leases l JOIN rigs r ON r.rig_id = l.rig_id
                WHERE r.host_id = %s AND l.released_at IS NULL
                """,
                (host_id,),
            ).fetchone()["value"])
            capacity_trial = connection.execute(
                """
                SELECT * FROM host_capacity_trials
                WHERE host_id = %s AND status = 'active' AND expires_at > now()
                ORDER BY started_at DESC LIMIT 1
                """,
                (host_id,),
            ).fetchone()
            breaker = connection.execute(
                "SELECT state, since, reason FROM host_breaker WHERE host_id = %s", (host_id,),
            ).fetchone()
            events = connection.execute(
                """
                SELECT * FROM host_coordination_events
                WHERE host_id = %s ORDER BY created_at DESC, event_id DESC LIMIT 50
                """,
                (host_id,),
            ).fetchall()
        return self._jsonable({
            "host": dict(host),
            "rigs": [{k: v for k, v in row.items() if k != "agent_connection_id"} for row in rigs],
            "targets": [dict(row) for row in targets],
            "active_rigs": active_rigs,
            "effective_capacity": (
                int(capacity_trial["trial_capacity"])
                if capacity_trial is not None
                else int(host["max_active_rigs"])
            ),
            "capacity_trial": (
                dict(capacity_trial) if capacity_trial is not None else None
            ),
            "breaker": dict(breaker) if breaker is not None else None,
            "maintenance": dict(maintenance) if maintenance is not None else None,
            "events": [dict(row) for row in events],
        })

    def begin_capacity_trial(
        self,
        host_id: str,
        *,
        actor: str,
        reason: str,
        preflight_evidence: Mapping[str, Any],
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        """Temporarily admit every evidence Rig without changing configured Capacity."""

        if not actor or not reason:
            raise ControlStateError("Capacity trial actor and reason are required")
        if not 1 <= ttl_seconds <= 1800:
            raise ControlStateError("Capacity trial TTL must be between 1 and 1800 seconds")
        proof = evaluate_same_host_p0_capacity_preflight(
            preflight_evidence,
            evidence_root=self.evidence_root,
        )
        if proof.get("status") != "passed":
            raise ControlStateError(
                "Capacity trial requires passed pre-capacity live P0 evidence"
            )
        with self._connection() as connection:
            self._expire_capacity_trials(connection)
            host = connection.execute(
                "SELECT * FROM hosts WHERE host_id = %s FOR UPDATE",
                (host_id,),
            ).fetchone()
            if host is None:
                raise ControlStateError(f"host not found: {host_id}")
            if int(host["max_active_rigs"]) != 1:
                raise ControlStateError("Capacity trial requires configured Capacity 1")
            maintenance = connection.execute(
                """
                SELECT maintenance_id FROM host_maintenance
                WHERE host_id = %s AND status = 'active' AND expires_at > now()
                """,
                (host_id,),
            ).fetchone()
            if maintenance is not None:
                raise ControlStateError("Capacity trial cannot start during Host maintenance")
            active = int(connection.execute(
                """
                SELECT count(*) AS value
                FROM rig_leases l JOIN rigs r ON r.rig_id = l.rig_id
                WHERE r.host_id = %s AND l.released_at IS NULL
                """,
                (host_id,),
            ).fetchone()["value"])
            if active:
                raise ControlStateError("Capacity trial requires zero active Rig leases")
            registered_targets = connection.execute(
                """
                SELECT host_id, rig_id, rig_generation, expected_usb_udid
                FROM rig_targets WHERE host_id = %s
                """,
                (host_id,),
            ).fetchall()
            if not capacity_targets_registered(
                preflight_evidence.get("targets"),
                registered_targets,
                host_id=host_id,
            ):
                raise ControlStateError(
                    "Capacity trial targets are not registered with these identities"
                )
            trial_id = f"HCT-{uuid.uuid4().hex}"
            trial = connection.execute(
                """
                INSERT INTO host_capacity_trials(
                    trial_id, host_id, previous_capacity, trial_capacity, status,
                    actor, reason, preflight_evidence, expires_at
                ) VALUES (%s, %s, 1, %s, 'active', %s, %s, %s,
                          now() + make_interval(secs => %s))
                RETURNING *
                """,
                (trial_id, host_id, len(proof["targets"]), actor, reason, Jsonb(proof), ttl_seconds),
            ).fetchone()
            event = self._insert_host_event(
                connection,
                host_id=host_id,
                kind="capacity_trial_started",
                actor=actor,
                details={
                    "trial_id": trial_id,
                    "previous": 1,
                    "trial": 2,
                    "configured_capacity_unchanged": True,
                    "evidence_ref": proof.get("evidence_ref"),
                },
            )
        return self._jsonable({"trial": dict(trial), "event": dict(event)})

    def finish_capacity_trial(
        self,
        trial_id: str,
        *,
        actor: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not actor:
            raise ControlStateError("Capacity trial actor is required")
        with self._connection() as connection:
            self._expire_capacity_trials(connection)
            trial = connection.execute(
                "SELECT * FROM host_capacity_trials WHERE trial_id = %s FOR UPDATE",
                (trial_id,),
            ).fetchone()
            if trial is None:
                raise ControlStateError(f"Capacity trial not found: {trial_id}")
            if trial["status"] != "active":
                raise ControlStateError(f"Capacity trial is not active: {trial_id}")
            status = "rolled_back" if result.get("passed") is True else "failed"
            updated = connection.execute(
                """
                UPDATE host_capacity_trials
                SET status = %s, result = %s, ended_at = now()
                WHERE trial_id = %s RETURNING *
                """,
                (status, Jsonb(dict(result)), trial_id),
            ).fetchone()
            event = self._insert_host_event(
                connection,
                host_id=str(trial["host_id"]),
                kind="capacity_trial_rolled_back",
                actor=actor,
                details={
                    "trial_id": trial_id,
                    "previous": int(trial["previous_capacity"]),
                    "trial": int(trial["trial_capacity"]),
                    "current": int(trial["previous_capacity"]),
                    "result": dict(result),
                },
            )
        return self._jsonable({"trial": dict(updated), "event": dict(event)})

    def set_host_capacity(
        self,
        host_id: str,
        capacity: int,
        *,
        actor: str,
        reason: str,
        evidence: Mapping[str, Any] | None = None,
        approval: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if capacity < 0:
            raise ControlStateError("Host capacity must be non-negative")
        if not actor or not reason:
            raise ControlStateError("Host capacity actor and reason are required")
        with self._connection() as connection:
            self._expire_capacity_trials(connection)
            host = connection.execute(
                "SELECT * FROM hosts WHERE host_id = %s FOR UPDATE",
                (host_id,),
            ).fetchone()
            if host is None:
                raise ControlStateError(f"host not found: {host_id}")
            active_trial = connection.execute(
                """
                SELECT trial_id FROM host_capacity_trials
                WHERE host_id = %s AND status = 'active' AND expires_at > now()
                """,
                (host_id,),
            ).fetchone()
            if active_trial is not None:
                raise ControlStateError(
                    "Host capacity cannot change during an active Capacity trial"
                )
            previous = int(host["max_active_rigs"])
            supplied_proof = dict(evidence or {})
            proof = supplied_proof
            admission_review = dict(approval or {})
            if capacity > previous and capacity > 1:
                proof = evaluate_same_host_p0(
                    supplied_proof,
                    verify_evidence=True,
                    evidence_root=self.evidence_root,
                )
                if proof.get("status") != "passed":
                    raise ControlStateError(
                        "Increasing Host capacity requires passed "
                        "live_same_host_multi_rig_p0 evidence"
                    )
                admission_review = evaluate_capacity_admission_review(proof, approval)
                if admission_review.get("status") != "approved":
                    raise ControlStateError(
                        "Increasing Host capacity requires an approved "
                        "qualification review"
                    )
                registered_targets = connection.execute(
                    """
                    SELECT host_id, rig_id, rig_generation, expected_usb_udid
                    FROM rig_targets WHERE host_id = %s
                    """,
                    (host_id,),
                ).fetchall()
                if not capacity_targets_registered(
                    proof.get("targets"),
                    registered_targets,
                    host_id=host_id,
                ):
                    raise ControlStateError(
                        "Capacity evidence targets are not registered with these identities"
                    )
                if capacity > len(proof["targets"]):
                    raise ControlStateError(
                        f"Capacity {capacity} exceeds the {len(proof['targets'])} Rigs "
                        "its evidence held at once"
                    )
            # Evidence names which Rigs may run together; without it nothing is qualified.
            qualified_rigs = host["qualified_rigs"]
            if capacity > previous and capacity > 1:
                qualified_rigs = [
                    {"rig_id": row["rig_id"], "rig_generation": row["rig_generation"]}
                    for row in proof["targets"]
                ]
            elif capacity <= 1:
                qualified_rigs = None
            updated = connection.execute(
                """
                UPDATE hosts SET max_active_rigs = %s, qualified_rigs = %s, last_seen = now()
                WHERE host_id = %s RETURNING *
                """,
                (capacity, Jsonb(qualified_rigs) if qualified_rigs is not None else None, host_id),
            ).fetchone()
            event = self._insert_host_event(
                connection,
                host_id=host_id,
                kind="capacity_changed",
                actor=actor,
                details={
                    "previous": previous,
                    "current": capacity,
                    "reason": reason,
                    "evidence": proof,
                    "admission_review": admission_review,
                },
            )
        return self._jsonable({"host": dict(updated), "event": dict(event)})

    def acquire_host_maintenance(
        self,
        host_id: str,
        *,
        intent: str,
        owner: str,
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        if not intent or not owner:
            raise ControlStateError("Host maintenance intent and owner are required")
        if ttl_seconds <= 0:
            raise ControlStateError("Host maintenance ttl_seconds must be positive")
        maintenance_id = f"HM-{uuid.uuid4().hex}"
        try:
            with self._connection() as connection:
                host = connection.execute(
                    "SELECT host_id FROM hosts WHERE host_id = %s FOR UPDATE",
                    (host_id,),
                ).fetchone()
                if host is None:
                    raise ControlStateError(f"host not found: {host_id}")
                self._expire_host_maintenance(connection)
                self._expire_capacity_trials(connection)
                capacity_trial = connection.execute(
                    """
                    SELECT trial_id FROM host_capacity_trials
                    WHERE host_id = %s AND status = 'active' AND expires_at > now()
                    """,
                    (host_id,),
                ).fetchone()
                if capacity_trial is not None:
                    raise ControlStateError(
                        f"Host maintenance cannot start during a Capacity trial: {host_id}"
                    )
                active = int(connection.execute(
                    """
                    SELECT count(*) AS value
                    FROM rig_leases l JOIN rigs r ON r.rig_id = l.rig_id
                    WHERE r.host_id = %s AND l.released_at IS NULL
                    """,
                    (host_id,),
                ).fetchone()["value"])
                if active:
                    raise ControlStateError(
                        f"Host maintenance requires zero active Rig leases: {host_id}"
                    )
                row = connection.execute(
                    """
                    INSERT INTO host_maintenance(
                        maintenance_id, host_id, intent, owner, status, expires_at
                    ) VALUES (%s, %s, %s, %s, 'active',
                        now() + make_interval(secs => %s))
                    RETURNING *
                    """,
                    (maintenance_id, host_id, intent, owner, ttl_seconds),
                ).fetchone()
                self._insert_host_event(
                    connection,
                    host_id=host_id,
                    kind="maintenance_acquired",
                    actor=owner,
                    details={"maintenance_id": maintenance_id, "intent": intent},
                )
        except psycopg_errors_by_name("UniqueViolation") as exc:
            raise ControlStateError(f"Host maintenance is already active: {host_id}") from exc
        return self._jsonable(dict(row))

    def complete_host_maintenance(
        self,
        maintenance_id: str,
        *,
        owner: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        status = "passed" if bool(result.get("passed")) else "failed"
        with self._connection() as connection:
            row = connection.execute(
                """
                UPDATE host_maintenance
                SET status = %s, result = %s, ended_at = now()
                WHERE maintenance_id = %s AND owner = %s AND status = 'active'
                RETURNING *
                """,
                (status, Jsonb(dict(result)), maintenance_id, owner),
            ).fetchone()
            if row is None:
                raise ControlStateError(
                    f"active Host maintenance not found for owner: {maintenance_id}"
                )
            self._insert_host_event(
                connection,
                host_id=str(row["host_id"]),
                kind="maintenance_completed",
                actor=owner,
                details={
                    "maintenance_id": maintenance_id,
                    "status": status,
                    "result": dict(result),
                },
            )
        return self._jsonable(dict(row))

    def quarantine_rig(
        self,
        rig_id: str,
        *,
        generation: int,
        actor: str,
        reason: str,
        evidence_refs: Sequence[str] = (),
    ) -> dict[str, Any]:
        if not actor or not reason:
            raise ControlStateError("Rig quarantine actor and reason are required")
        with self._connection() as connection:
            rig = connection.execute(
                """
                UPDATE rigs SET status = 'quarantine', last_seen = now()
                WHERE rig_id = %s AND generation = %s RETURNING *
                """,
                (rig_id, generation),
            ).fetchone()
            if rig is None:
                raise StaleFencingError(f"stale rig generation: {rig_id}/{generation}")
            event = self._insert_host_event(
                connection,
                host_id=str(rig["host_id"]),
                rig_id=rig_id,
                kind="rig_quarantined",
                actor=actor,
                details={"reason": reason, "evidence_refs": list(evidence_refs)},
            )
        return self._jsonable({"rig": dict(rig), "event": dict(event)})

    def list_artifacts(self, *, owner_id: str | None = None) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if owner_id is None:
                rows = connection.execute(
                    "SELECT * FROM artifact_refs ORDER BY created_at, artifact_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM artifact_refs WHERE owner_id = %s
                    ORDER BY created_at, artifact_id
                    """,
                    (owner_id,),
                ).fetchall()
        return [self._jsonable(dict(row)) for row in rows]

    def count_assignments(self) -> int:
        with self._connection() as connection:
            return int(connection.execute("SELECT count(*) AS value FROM assignments").fetchone()["value"])

    def register_fixture_readback(self, *, target, expected, readback) -> dict[str, Any]:
        """Register a new Fixture from an operator-supplied authority observation.

        Existing or occupied resources cannot be overwritten through registration.
        Reset completion is the only path that returns a claimed resource to reuse.
        """
        from autophone.reset_contract import checksum, verify_readback
        verification = verify_readback(target, expected, readback)
        if not target.get('resource_id') or verification['status'] != 'passed':
            raise ControlStateError('Fixture registration requires independent authority readback')
        with self._connection() as connection:
            row = connection.execute('''INSERT INTO reset_fixtures
                (resource_key, target, expected, disposition, verification)
                VALUES (%s, %s, %s, 'restored', %s) ON CONFLICT DO NOTHING RETURNING *''',
                (checksum(target), Jsonb(target), Jsonb(expected), Jsonb(verification))).fetchone()
            if row is None:
                raise ControlStateError('Fixture already registered; use fenced reset completion')
            return self._jsonable(dict(row))

    # ── B 端接入授权记录（BL-104 / device-console-embed §十一） ────────────────
    #
    # 三条互斥全部由数据库表达，应用层不再自己加锁：
    # `request_id` 的唯一约束是创建幂等（§8.2），`uq_active_grant_per_device` 是设备
    # 互斥的第二道，`uq_control_grant_per_subject` 是一人一台（§7.4）。撞上哪一条由
    # `ControlStateError.conflict` 区分，调用方据此选错误码，不必解析 SQL 错误文本。

    def create_access_grant(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        """写入一条授权记录。默认状态 `creating`（§8.2：先写占用意图再开会话）。"""
        grant_id = str(spec.get("grant_id") or f"grt-{uuid.uuid4().hex[:16]}")
        request_id = str(spec.get("request_id") or "").strip()
        if not request_id:
            raise ControlStateError("access grant request_id is required")
        with self._connection() as connection:
            # Serialize known Rig grant creation with retirement. Unknown devices
            # remain supported by the local compatibility registry.
            rigs = connection.execute(
                """
                SELECT r.deregistration FROM rigs r
                LEFT JOIN rig_targets t ON t.rig_id = r.rig_id
                WHERE r.rig_id = %s OR regexp_replace(t.vm_dir, '^.*/', '') = %s
                ORDER BY r.rig_id FOR UPDATE OF r
                """, (str(spec.get("device") or ""), str(spec.get("device") or "")),
            ).fetchall()
            if any(rig["deregistration"] is not None for rig in rigs):
                raise ControlStateError(f"rig_deregistered: {spec.get('device')}")
            try:
                row = connection.execute(
                    """
                    INSERT INTO access_grants(
                        grant_id, request_id, platform_id, subject, device, mode, status,
                        console_session_id, controller_token, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        grant_id,
                        request_id,
                        str(spec.get("platform_id") or ""),
                        str(spec.get("subject") or ""),
                        str(spec.get("device") or ""),
                        str(spec.get("mode") or "control"),
                        str(spec.get("status") or "creating"),
                        spec.get("console_session_id"),
                        spec.get("controller_token"),
                        spec.get("expires_at"),
                    ),
                ).fetchone()
            except psycopg_errors_by_name("UniqueViolation") as exc:
                raise _access_grant_conflict(exc) from exc
        return self._jsonable(dict(row))

    def get_access_grant(self, grant_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM access_grants WHERE grant_id = %s", (grant_id,),
            ).fetchone()
        return self._jsonable(dict(row)) if row is not None else None

    def find_access_grant(
        self, *, request_id: str | None = None, console_session_id: str | None = None,
    ) -> dict[str, Any] | None:
        """按创建请求标识或 console 会话标识找一条授权记录。

        `request_id` 唯一，至多一条。`console_session_id` 可能被多条 view 授权共用
        （多人观看同一条操作会话），此时取 `created_at` 最早那条 control 授权优先，
        好让「按会话撤销」落在真正持锁的那条上。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if request_id:
            clauses.append("request_id = %s")
            params.append(request_id)
        if console_session_id:
            clauses.append("console_session_id = %s")
            params.append(console_session_id)
        if not clauses:
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM access_grants WHERE "
                + " AND ".join(clauses)
                + " ORDER BY (mode = 'control') DESC, created_at ASC LIMIT 1",
                tuple(params),
            ).fetchone()
        return self._jsonable(dict(row)) if row is not None else None

    def list_access_grants(
        self,
        *,
        statuses: Sequence[str] | None = None,
        platform_id: str | None = None,
        devices: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            clauses.append("status = ANY(%s)")
            params.append(list(statuses))
        if platform_id:
            clauses.append("platform_id = %s")
            params.append(platform_id)
        if devices:
            clauses.append("device = ANY(%s)")
            params.append(list(devices))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM access_grants{where} ORDER BY created_at ASC", tuple(params),
            ).fetchall()
        return [self._jsonable(dict(row)) for row in rows]

    def update_access_grant(
        self,
        grant_id: str,
        changes: Mapping[str, Any],
        *,
        event: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """按字段名改一条授权记录；`status` 转终态时自动补 `closed_at`。

        带 `event` 时在**同一事务**里向只追加的 `access_grant_events` 写一行
        （BL-116）：先 `FOR UPDATE` 取变更前状态，再 UPDATE，再 INSERT。任何一步失败
        （唯一约束、CHECK、触发器），连接上下文回滚，授权记录与事件都不落。
        事件的授权快照与前后状态取自本事务内的行，调用方只给 kind / actor / reason /
        details 与 event_id。
        """
        allowed = (
            "status", "device", "console_session_id", "controller_token",
            "expires_at", "closed_reason", "error_reason",
        )
        sets: list[str] = []
        params: list[Any] = []
        for key in allowed:
            if key in changes:
                sets.append(f"{key} = %s")
                params.append(changes[key])
        if str(changes.get("status") or "") == "closed" and "closed_at" not in changes:
            sets.append("closed_at = now()")
        if not sets and event is None:
            existing = self.get_access_grant(grant_id)
            if existing is None:
                raise ControlStateError(f"access grant not found: {grant_id}")
            return existing
        with self._connection() as connection:
            before = connection.execute(
                "SELECT status FROM access_grants WHERE grant_id = %s FOR UPDATE",
                (grant_id,),
            ).fetchone()
            if before is None:
                raise ControlStateError(f"access grant not found: {grant_id}")
            if sets:
                try:
                    row = connection.execute(
                        f"UPDATE access_grants SET {', '.join(sets)} "
                        "WHERE grant_id = %s RETURNING *",
                        (*params, grant_id),
                    ).fetchone()
                except psycopg_errors_by_name("UniqueViolation") as exc:
                    raise _access_grant_conflict(exc) from exc
            else:
                row = connection.execute(
                    "SELECT * FROM access_grants WHERE grant_id = %s", (grant_id,),
                ).fetchone()
            if event is not None:
                connection.execute(
                    """
                    INSERT INTO access_grant_events(
                        event_id, grant_id, kind, actor, reason, platform_id, subject,
                        device, mode, console_session_id, from_status, to_status, details
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(event.get("event_id") or f"gev-{uuid.uuid4().hex[:16]}"),
                        row["grant_id"],
                        str(event.get("kind") or ""),
                        str(event.get("actor") or ""),
                        str(event.get("reason") or ""),
                        row["platform_id"],
                        row["subject"],
                        row["device"],
                        row["mode"],
                        row["console_session_id"],
                        before["status"],
                        row["status"],
                        Jsonb(dict(event.get("details") or {})),
                    ),
                )
        return self._jsonable(dict(row))

    def list_access_grant_events(
        self, *, grant_id: str | None = None, device: str | None = None,
    ) -> list[dict[str, Any]]:
        """只读：按授权记录或设备列出事件，按发生时间升序。本表没有更新与删除入口。"""
        clauses: list[str] = []
        params: list[Any] = []
        if grant_id:
            clauses.append("grant_id = %s")
            params.append(grant_id)
        if device:
            clauses.append("device = %s")
            params.append(device)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM access_grant_events{where} ORDER BY created_at ASC, event_id ASC",
                tuple(params),
            ).fetchall()
        return [self._jsonable(dict(row)) for row in rows]

    def _forget_applied_schema_for_test(self) -> None:
        """Make this schema look like one built before ``schema_ddl_state`` existed."""
        psycopg, sql, _ = require_psycopg_components()
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP TABLE IF EXISTS {}").format(
                    sql.Identifier(self.schema, "schema_ddl_state")
                )
            )

    def _drop_schema_for_test(self) -> None:
        psycopg, sql, _ = require_psycopg_components()
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(self.schema))
            )

    def _rig_wait_reason(
        self,
        connection: psycopg.Connection,
        assignment: Mapping[str, Any],
        rig: Mapping[str, Any],
        required: set[str],
        connected_rigs: set[tuple[str, str]] | None,
    ) -> str | None:
        from .reset import wait_reason as fixture_wait_reason
        fixture_reason = fixture_wait_reason(connection, assignment)
        if fixture_reason is not None:
            return fixture_reason
        # Quarantine 先于连通性：它要一份 Recovery Receipt 才能解除，Agent 重连并
        # 不改变它。反过来判会让一台被隔离且 Agent 恰好没连的 Rig 只报 agent_disconnected，
        # 排障者据此去重启 Agent，重启多少次都不会派单。
        if rig.get("deregistration") is not None:
            return "rig_deregistered"
        from .recovery import authorized_recovery
        recovery = authorized_recovery(connection, assignment, rig)
        if assignment['kind'] == 'recovery' and not recovery:
            return 'recovery_not_authorized'
        if rig["status"] == "quarantine" and not recovery:
            return "rig_quarantine"
        if assignment["kind"] != "recovery" and _screen_quarantined(rig):
            return "screen_quarantined"
        if (
            connected_rigs is not None
            and (str(rig["host_id"]), str(rig["rig_id"])) not in connected_rigs
        ):
            return "agent_disconnected"
        if (rig["status"] != "online" and not recovery) or rig["host_status"] != "online":
            return "target_offline" if assignment["target_rig_id"] else "no_online_rig"
        self._expire_host_maintenance(connection)
        maintenance = connection.execute(
            """
            SELECT 1 FROM host_maintenance
            WHERE host_id = %s AND status = 'active'
            """,
            (rig["host_id"],),
        ).fetchone()
        if maintenance is not None:
            return "host_maintenance"
        if not required.issubset(set(rig["capability_support"] or [])):
            return "capability_support_insufficient"
        active_reservation = connection.execute(
            """
            SELECT reservation_id FROM rig_reservations
            WHERE rig_id = %s AND status = 'active'
            """,
            (rig["rig_id"],),
        ).fetchone()
        expected_reservation = assignment["reservation_id"]
        if active_reservation is not None and active_reservation["reservation_id"] != expected_reservation:
            return "reservation_mismatch"
        if expected_reservation and (
            active_reservation is None or active_reservation["reservation_id"] != expected_reservation
        ):
            return "reservation_mismatch"
        active_on_host = int(connection.execute(
            """
            SELECT count(*) AS value
            FROM rig_leases l JOIN rigs occupied ON occupied.rig_id = l.rig_id
            WHERE l.released_at IS NULL AND occupied.host_id = %s
            """,
            (rig["host_id"],),
        ).fetchone()["value"])
        effective_capacity = self._effective_host_capacity(
            connection,
            str(rig["host_id"]),
            int(rig["max_active_rigs"]),
        )
        capacity_reason = None
        if active_on_host >= effective_capacity:
            capacity_reason = "host_capacity"
        elif active_on_host and self._host_requires_exclusive(connection, rig):
            capacity_reason = "host_exclusive"
        active_lease = connection.execute(
            "SELECT 1 FROM rig_leases WHERE rig_id = %s AND released_at IS NULL",
            (rig["rig_id"],),
        ).fetchone()
        # 这台 Rig 自己在忙时两种逻辑都不会派单，没有可比的内容。
        resource_reason = None
        if active_lease is None:
            resource_reason = self._record_admission_shadow(
                connection, assignment, rig, capacity_reason, active_on_host,
            )
        if rig.get("admission_mode") == "resource":
            return "rig_busy" if active_lease is not None else resource_reason
        if capacity_reason is not None:
            return capacity_reason
        if active_lease is not None:
            return "rig_busy"
        return None

    @staticmethod
    def _choose_wait_reason(
        assignment: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
        reasons: Sequence[str],
    ) -> str:
        if assignment["target_rig_id"] and not candidates:
            return "target_rig_not_registered"
        # rig_quarantine 排在 agent_disconnected 之前：多个候选 Rig 时聚合成一条原因，
        # 报「解除后才可能派单」的持久阻塞，比报另一台 Rig 的瞬态失联更可行动。
        # 未修前，2026-08-20 演练留下的两条 status=online 但无 Agent 的 Rig 行，会让
        # 本机被隔离的 Rig 的真实原因永远被 agent_disconnected 压住。
        precedence = (
            "fixture_identity_missing", "fixture_unregistered", "fixture_busy",
            "fixture_quarantine", "fixture_readback_invalid", "fixture_baseline_mismatch",
            "recovery_not_authorized",
            "rig_quarantine",
            "screen_quarantined",
            "agent_disconnected",
            "target_offline",
            "host_maintenance",
            "reservation_mismatch",
            "capability_support_insufficient",
            "host_capacity",
            "host_breaker",
            "host_memory",
            "host_pressure",
            "host_pressure_unknown",
            "resource_not_configured",
            "resource_unavailable",
            "host_exclusive",
            "rig_busy",
            "no_online_rig",
        )
        return next((reason for reason in precedence if reason in reasons), "no_eligible_rig")

    def _quiesce_failure(self, connection, assignment_id, attempt_no) -> Mapping[str, Any] | None:
        attempt = connection.execute(
            "SELECT stage_results FROM assignment_attempts WHERE assignment_id = %s AND attempt_no = %s",
            (assignment_id, attempt_no),
        ).fetchone()
        quiesce = (attempt['stage_results'] or {}).get('quiesce') or {}
        if quiesce.get('status') == 'failed' and quiesce.get('device_access_released') is False:
            return quiesce
        return None

    def _assert_no_quiesce_failure(self, connection, assignment_id, attempt_no):
        if self._quiesce_failure(connection, assignment_id, attempt_no) is not None:
            raise StaleFencingError('device_quiesce_unverified: recovery Assignment required')

    @staticmethod
    def _assert_quiesce_unverified_completion(result: Mapping[str, Any]) -> None:
        """quiesce 失败后的完成回执契约（BL-220 剩余项）。

        完成只记录终态、释放 Lease：Lease 释放后该 Attempt 的写入校验仍被拒（无当前 Lease），
        rig 的 quarantine 已由 quiesce 阶段结果写入，派单不再选择它，ScreenLock 标记仍拒绝新
        lease。与租约过期相比，rig、Lease 与标记的状态相同，差别只是 Attempt 有明确终态与回执。
        """
        cleanup = result.get("cleanup_receipt") or {}
        problems = []
        if result.get("schema_version") != "assignment-result/v1":
            problems.append("schema_version=assignment-result/v1")
        if result.get("status") != "error":
            problems.append("status=error")
        if result.get("reason") != _QUIESCE_UNVERIFIED:
            problems.append(f"reason={_QUIESCE_UNVERIFIED}")
        if result.get("rig_disposition") != "quarantine":
            problems.append("rig_disposition=quarantine")
        if cleanup.get("device_access_released") is not False:
            problems.append("cleanup_receipt.device_access_released=false")
        if problems:
            # 与写入校验同一异常类（此前对该 Attempt 的任何完成都抛它）。
            raise StaleFencingError(
                f"{_QUIESCE_UNVERIFIED}: completion after a failed quiesce requires "
                + ", ".join(problems)
            )

    def _current_lease(
        self,
        connection: psycopg.Connection,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        *,
        lock: bool,
    ) -> Mapping[str, Any]:
        suffix = " FOR UPDATE" if lock else ""
        lease = connection.execute(
            """
            SELECT l.* FROM rig_leases l
            JOIN assignments a ON a.assignment_id = l.assignment_id
            JOIN rigs r ON r.rig_id = l.rig_id
            WHERE l.assignment_id = %s AND l.attempt_no = %s AND l.fencing = %s
              AND l.released_at IS NULL AND l.expires_at > now()
              AND a.current_attempt_no = l.attempt_no AND a.current_fencing = l.fencing
              AND r.generation = l.rig_generation
            """ + suffix,
            (assignment_id, attempt_no, fencing),
        ).fetchone()
        if lease is None:
            raise StaleFencingError(
                f"stale lease or fencing: {assignment_id}/{attempt_no}/{fencing}"
            )
        return lease

    def _expire_reservations(self, connection: psycopg.Connection) -> None:
        connection.execute(
            """
            UPDATE rig_reservations
            SET status = 'released', released_at = now(), release_reason = 'expired'
            WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at <= now()
            """
        )

    def _expire_host_maintenance(self, connection: psycopg.Connection) -> None:
        connection.execute(
            """
            UPDATE host_maintenance SET status = 'expired', ended_at = now()
            WHERE status = 'active' AND expires_at <= now()
            """
        )

    def _expire_capacity_trials(self, connection: psycopg.Connection) -> None:
        expired = connection.execute(
            """
            UPDATE host_capacity_trials SET status = 'expired', ended_at = now(),
                result = jsonb_build_object('passed', false, 'reason', 'ttl_expired')
            WHERE status = 'active' AND expires_at <= now()
            RETURNING trial_id, host_id, previous_capacity, trial_capacity
            """
        ).fetchall()
        for trial in expired:
            self._insert_host_event(
                connection,
                host_id=str(trial["host_id"]),
                kind="capacity_trial_expired",
                actor="control-plane",
                details={
                    "trial_id": str(trial["trial_id"]),
                    "previous": int(trial["previous_capacity"]),
                    "trial": int(trial["trial_capacity"]),
                    "current": int(trial["previous_capacity"]),
                },
            )

    @staticmethod
    def _environment_policy(host):
        return host["admission_mode"] == "resource" and host["environment_observation"] is not None

    @staticmethod
    def _environment_allows(connection, rig, host):
        qualified = host["qualified_environment"]
        if not qualified or host["now"] - host["environment_observed_at"] > timedelta(seconds=180):
            return False
        rows = connection.execute(
            """
            SELECT t.cli_dir FROM rigs r LEFT JOIN rig_targets t
              ON t.rig_id = r.rig_id AND t.host_id = r.host_id AND t.rig_generation = r.generation
            WHERE r.host_id = %s AND (r.rig_id = %s OR EXISTS (
                SELECT 1 FROM rig_leases l WHERE l.rig_id = r.rig_id AND l.released_at IS NULL))
            """, (rig["host_id"], rig["rig_id"]),
        ).fetchall()
        directories = host["environment_observation"]["directories"]
        return all(directories.get(row["cli_dir"]) == qualified for row in rows)

    def environment_canary_due(self, host_id, interval_seconds):
        with self._connection() as connection:
            return connection.execute(
                """SELECT NOT EXISTS (SELECT 1 FROM host_maintenance WHERE host_id = %s
                       AND intent = 'environment_canary'
                       AND started_at > now() - make_interval(secs => %s)) AS due""",
                (host_id, interval_seconds),
            ).fetchone()["due"]

    def observe_host_environment(self, host_id, observation, *, actor, reason):
        if not actor.strip() or not reason.strip():
            raise ControlStateError("environment actor and reason required")
        with self._connection() as connection:
            host = connection.execute(
                "SELECT * FROM hosts WHERE host_id = %s FOR UPDATE", (host_id,),
            ).fetchone()
            if not host or host["admission_mode"] != "resource":
                raise ControlStateError("environment qualification requires resource mode")
            previous = host["environment_observation"]
            changed = previous is None or previous["fingerprint"] != observation["fingerprint"]
            connection.execute(
                """UPDATE hosts SET environment_observation = %s, environment_observed_at = now(),
                       qualified_environment = CASE WHEN %s THEN NULL ELSE qualified_environment END
                   WHERE host_id = %s""", (Jsonb(observation), changed, host_id),
            )
            if changed:
                self._insert_host_event(connection, host_id=host_id, kind="environment_changed",
                                        actor=actor, details={"observation": observation, "reason": reason})
            return None if changed else host["qualified_environment"]

    def qualify_host_environment(self, host_id, maintenance_id, *, owner):
        """Bind recomputed smoke evidence to the current environment and the reserved pair."""
        if self.evidence_root is None:
            raise ControlStateError("environment smoke requires a trusted evidence root")
        output = self.evidence_root / maintenance_id
        report_path = output / "same-host-p0-report.json"
        if output.resolve().parent != self.evidence_root or report_path.resolve().parent != output:
            raise ControlStateError("invalid environment evidence directory")
        encoded = report_path.read_bytes()
        report = json.loads(encoded)
        setup = report.get("experiment_setup") or {}
        identity = setup.get("environment_fingerprint") or {}
        fingerprint = f"{identity.get('macos_build')}/{identity.get('vphone_cli_commit')}"
        if report.get("evidence_ref") != report_path.as_uri() or any(
            check.get("evidence_refs") != [(output / f"{name}.json").as_uri()]
            for name, check in report.get("checks", {}).items()
        ):
            raise ControlStateError("environment smoke evidence belongs to another maintenance window")
        proof = evaluate_same_host_p0(report, verify_evidence=True, verify_environment=True,
                                      evidence_root=self.evidence_root)
        targets = report.get("targets") or []
        if (proof["status"] != "passed" or setup.get("duration_seconds") != 120
                or setup.get("admission_profile", "smoke") != "smoke" or len(targets) != 2
                or identity.get("host_id") != host_id):
            raise ControlStateError("environment smoke evidence rejected")
        with self._connection() as connection:
            host = connection.execute(
                "SELECT * FROM hosts WHERE host_id = %s FOR UPDATE", (host_id,),
            ).fetchone()
            maintenance = connection.execute(
                """SELECT 1 FROM host_maintenance WHERE host_id = %s AND maintenance_id = %s
                   AND owner = %s AND intent = 'environment_smoke' AND status = 'active'
                   AND expires_at > now() FOR UPDATE""", (host_id, maintenance_id, owner),
            ).fetchone()
            reserved = connection.execute(
                """SELECT t.* FROM rig_targets t JOIN rigs r ON r.rig_id = t.rig_id
                      AND r.generation = t.rig_generation AND r.host_id = t.host_id
                   JOIN rig_reservations s ON s.rig_id = r.rig_id WHERE t.host_id = %s
                      AND r.deregistration IS NULL AND s.status = 'active' AND s.expires_at > now()
                      AND s.owner->>'maintenance_id' = %s""", (host_id, maintenance_id),
            ).fetchall()
            observation = host["environment_observation"] or {}
            if (not maintenance or not self._environment_policy(host)
                    or observation.get("fingerprint") != fingerprint
                    or not capacity_targets_registered(targets, reserved, host_id=host_id)
                    or not all(observation["directories"].get(t["cli_dir"]) == fingerprint for t in reserved)):
                raise ControlStateError("environment smoke identity or reservation changed")
            connection.execute("UPDATE hosts SET qualified_environment = %s WHERE host_id = %s",
                               (fingerprint, host_id))
            self._insert_host_event(connection, host_id=host_id, kind="environment_qualified", actor=owner,
                                    details={"fingerprint": fingerprint, "maintenance_id": maintenance_id,
                                             "evidence_ref": proof["evidence_ref"],
                                             "sha256": hashlib.sha256(encoded).hexdigest()})

    def _host_requires_exclusive(self, connection: psycopg.Connection, rig: Mapping[str, Any]) -> bool:
        """Only Rigs the evidence named may share a Host, and only with each other.

        A Host with no recorded set has none to enforce: Capacity 1 serializes anyway, and a
        higher value set before this column existed keeps its behavior.
        """
        host_id = str(rig["host_id"])
        trial = connection.execute(
            """
            SELECT preflight_evidence FROM host_capacity_trials
            WHERE host_id = %s AND status = 'active' AND expires_at > now()
            ORDER BY started_at DESC LIMIT 1
            """,
            (host_id,),
        ).fetchone()
        if trial is not None:
            rows = trial["preflight_evidence"].get("targets") or []
        else:
            rows = connection.execute(
                "SELECT qualified_rigs FROM hosts WHERE host_id = %s", (host_id,),
            ).fetchone()["qualified_rigs"]
        if rows is None:
            return False
        qualified = {(str(row["rig_id"]), int(row["rig_generation"])) for row in rows}
        occupied = connection.execute(
            """
            SELECT occupied.rig_id, occupied.generation
            FROM rig_leases l JOIN rigs occupied ON occupied.rig_id = l.rig_id
            WHERE l.released_at IS NULL AND occupied.host_id = %s
            """,
            (host_id,),
        ).fetchall()
        return (str(rig["rig_id"]), int(rig["generation"])) not in qualified or any(
            (str(row["rig_id"]), int(row["generation"])) not in qualified for row in occupied
        )

    def _effective_host_capacity(
        self,
        connection: psycopg.Connection,
        host_id: str,
        configured_capacity: int,
    ) -> int:
        self._expire_capacity_trials(connection)
        trial = connection.execute(
            """
            SELECT trial_capacity FROM host_capacity_trials
            WHERE host_id = %s AND status = 'active' AND expires_at > now()
            ORDER BY started_at DESC LIMIT 1
            """,
            (host_id,),
        ).fetchone()
        return int(trial["trial_capacity"]) if trial is not None else configured_capacity

    @staticmethod
    def _insert_host_event(
        connection: psycopg.Connection,
        *,
        host_id: str,
        kind: str,
        actor: str,
        details: Mapping[str, Any],
        rig_id: str | None = None,
    ) -> Mapping[str, Any]:
        return connection.execute(
            """
            INSERT INTO host_coordination_events(
                event_id, host_id, rig_id, kind, actor, details
            ) VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                f"HCE-{uuid.uuid4().hex}",
                host_id,
                rig_id,
                kind,
                actor,
                Jsonb(dict(details)),
            ),
        ).fetchone()

    def _invalidate_generation(
        self,
        connection: psycopg.Connection,
        rig_id: str,
        *,
        reason: str,
    ) -> None:
        leases = connection.execute(
            "SELECT * FROM rig_leases WHERE rig_id = %s AND released_at IS NULL FOR UPDATE",
            (rig_id,),
        ).fetchall()
        for lease in leases:
            connection.execute(
                "UPDATE rig_leases SET released_at = now(), release_reason = %s WHERE lease_id = %s",
                (reason, lease["lease_id"]),
            )
            connection.execute(
                """
                UPDATE assignment_attempts SET status = 'indeterminate', ended_at = now()
                WHERE assignment_id = %s AND attempt_no = %s
                """,
                (lease["assignment_id"], lease["attempt_no"]),
            )
            connection.execute(
                "UPDATE assignments SET status = 'indeterminate', updated_at = now() WHERE assignment_id = %s",
                (lease["assignment_id"],),
            )

    @classmethod
    def _jsonable(cls, value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, Mapping):
            return {key: cls._jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._jsonable(item) for item in value]
        return value
