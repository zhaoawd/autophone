"""Control Plane Interface and two-category work routing."""
from __future__ import annotations

import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence, Protocol

from .interface import ControlPlaneInterface, SchedulingResult
from .postgres import PostgresControlStore, StaleFencingError

from autophone.logging import get_logger

logger = get_logger(__name__)


class AssignmentExecutor(Protocol):
    def execute(self, assignment: Mapping[str, Any]) -> Mapping[str, Any]: ...


class HostWorkExecutor(Protocol):
    def execute(self, plan: Mapping[str, Any]) -> Mapping[str, Any]: ...


class ControlPlane:
    """Present Assignment operations without exposing SQL or scheduler steps."""

    def __init__(self, store: PostgresControlStore) -> None:
        self._store = store

    @property
    def evidence_root(self) -> Path | None:
        """store 评估本地 `file://` 证据时使用的可信根目录；`None` 表示未配置。"""
        return self._store.evidence_root

    def resolve_device_rig(self, device: str) -> str:
        """保留固定设备入口的选择，拒绝未知或歧义的设备别名。"""
        return self._store.resolve_device_rig(device)

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._store.get_run(run_id)

    def submit_assignment(
        self,
        plan: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        from autophone.contracts import ContractError
        from autophone.rig.plugins import validate_run_submission
        from .postgres import ControlStateError

        if isinstance(plan.get("run_spec"), Mapping):
            try:
                validate_run_submission(plan["run_spec"])
            except ContractError as exc:
                raise ControlStateError(str(exc)) from exc
        return self._store.submit_assignment(plan, idempotency_key)

    def observe_vphone_down(self, **observation):
        from .recovery import RecoveryCoordinator
        return RecoveryCoordinator(self._store).observe(**observation)

    def get_recovery(self, recovery_id):
        from .recovery import RecoveryCoordinator
        return RecoveryCoordinator(self._store).get(recovery_id)

    def get_run_recoveries(self, run_id):
        return self._store.get_run_recoveries(run_id)

    def list_rig_recoveries(self, rig_id, *, active_only=False):
        return self._store.list_rig_recoveries(rig_id, active_only=active_only)

    def rig_activity(self, rig_id):
        return self._store.rig_activity(rig_id)

    def resume_recovery(self, recovery_id, *, retry=False):
        from .recovery import RecoveryCoordinator
        return RecoveryCoordinator(self._store).resume(recovery_id, retry=retry)

    def confirm_recovery_isolation(self, recovery_id, *, actor, reason):
        from .recovery import RecoveryCoordinator
        return RecoveryCoordinator(self._store).confirm_isolation(recovery_id, actor=actor, reason=reason)

    def cancel_assignment(
        self,
        assignment_id: str,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        return self._store.cancel_assignment(assignment_id, actor, reason)

    def get_assignment(self, assignment_id: str) -> dict[str, Any]:
        return self._store.get_assignment(assignment_id)

    def create_reservation(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        return self._store.create_reservation(spec)

    def release_reservation(self, reservation_id: str, actor: str) -> dict[str, Any]:
        return self._store.release_reservation(reservation_id, actor)

    def list_rigs(self, *, include_deregistered: bool = False) -> list[dict[str, Any]]:
        if include_deregistered:
            return self._store.list_rigs(include_deregistered=True)
        return self._store.list_rigs()

    def begin_agent_connection(
        self, host_id: str, rig_id: str, generation: int, connection_id: str,
    ) -> None:
        self._store.begin_agent_connection(host_id, rig_id, generation, connection_id)

    def refresh_agent_connection(self, rig_id: str, connection_id: str) -> bool:
        return self._store.refresh_agent_connection(rig_id, connection_id)

    def end_agent_connection(self, rig_id: str, connection_id: str) -> None:
        self._store.end_agent_connection(rig_id, connection_id)

    def deregister_rig(
        self, rig_id: str, generation: int, *, actor: str, reason: str,
    ) -> dict[str, Any]:
        return self._store.deregister_rig(rig_id, generation, actor=actor, reason=reason)

    # ── B 端接入授权记录（BL-104）：直通存储，本层不加语义 ────────────────────
    def create_access_grant(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        return self._store.create_access_grant(spec)

    def get_access_grant(self, grant_id: str) -> dict[str, Any] | None:
        return self._store.get_access_grant(grant_id)

    def find_access_grant(
        self, *, request_id: str | None = None, console_session_id: str | None = None,
    ) -> dict[str, Any] | None:
        return self._store.find_access_grant(
            request_id=request_id, console_session_id=console_session_id,
        )

    def list_access_grants(
        self,
        *,
        statuses: Any | None = None,
        platform_id: str | None = None,
        devices: Any | None = None,
    ) -> list[dict[str, Any]]:
        return self._store.list_access_grants(
            statuses=statuses, platform_id=platform_id, devices=devices,
        )

    def update_access_grant(
        self,
        grant_id: str,
        changes: Mapping[str, Any],
        *,
        event: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._store.update_access_grant(grant_id, changes, event=event)

    def list_access_grant_events(
        self, *, grant_id: str | None = None, device: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._store.list_access_grant_events(grant_id=grant_id, device=device)

    def list_reservations(self, *, statuses: Any | None = None) -> list[dict[str, Any]]:
        return self._store.list_reservations(statuses=statuses)

    def read_observed_state(self, host_id: str, rig_id: str, generation: int) -> dict[str, Any]:
        """读取绑定身份的权威观测；身份失效时拒绝，不返回空观测。"""
        row = next((rig for rig in self._store.list_rigs() if rig.get("rig_id") == rig_id), None)
        if row is None or row.get("host_id") != host_id or row.get("generation") != generation:
            raise StaleFencingError(f"stale Rig observed-state identity: {host_id}/{rig_id}/{generation}")
        return deepcopy(dict(row.get("observed_state") or {}))

    def get_host(self, host_id: str) -> dict[str, Any]:
        return self._store.host_snapshot(host_id)

    def record_host_anomaly(self, record: Mapping[str, Any]) -> dict[str, Any]:
        return self._store.record_host_anomaly(record)

    def record_host_load_samples(self, host_id: str, samples: Sequence[Mapping[str, Any]]) -> int:
        return self._store.record_host_load_samples(host_id, samples)

    def host_active_work(self, host_id: str, *, recent_seconds: int = 0) -> list[dict[str, Any]]:
        return self._store.host_active_work(host_id, recent_seconds=recent_seconds)

    def list_host_anomalies(
        self, host_id: str, *, kind: str | None = None, since: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._store.list_host_anomalies(host_id, kind=kind, since=since, limit=limit)

    def get_host_anomaly(self, anomaly_id: str) -> dict[str, Any]:
        return self._store.get_host_anomaly(anomaly_id)

    def add_host_anomaly_review(
        self, anomaly_id: str, *, actor: str, note: str, labels: Sequence[str] = (),
    ) -> dict[str, Any]:
        return self._store.add_host_anomaly_review(anomaly_id, actor=actor, note=note, labels=labels)

    def reviewed_host_anomaly_ids(self, host_id: str) -> set[str]:
        return self._store.reviewed_host_anomaly_ids(host_id)

    def host_anomaly_report(
        self, host_id: str, *, since: str | None = None, include_test: bool = False,
    ) -> dict[str, Any]:
        return self._store.host_anomaly_report(host_id, since=since, include_test=include_test)

    def set_host_allocatable(
        self, host_id: str, *, memory_bytes: int, actor: str, reason: str,
    ) -> dict[str, Any]:
        return self._store.set_host_allocatable(
            host_id, memory_bytes=memory_bytes, actor=actor, reason=reason,
        )

    def set_host_admission_mode(self, host_id: str, mode: str, *, actor: str, reason: str) -> dict[str, Any]:
        return self._store.set_host_admission_mode(host_id, mode, actor=actor, reason=reason)

    def host_breaker_state(self, host_id: str) -> dict[str, Any]:
        return self._store.host_breaker_state(host_id)

    def reset_host_breaker(self, host_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        return self._store.reset_host_breaker(host_id, actor=actor, reason=reason)

    def report_rig_resources(self, host_id: str, requests: Mapping[str, Mapping[str, Any]]) -> None:
        self._store.report_rig_resources(host_id, requests)

    def host_admission_shadow_report(self, host_id: str, *, since: str | None = None) -> dict[str, Any]:
        return self._store.host_admission_shadow_report(host_id, since=since)

    def change_host_capacity(
        self,
        host_id: str,
        capacity: int,
        *,
        actor: str,
        reason: str,
        evidence: Mapping[str, Any] | None = None,
        approval: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._store.set_host_capacity(
            host_id,
            capacity,
            actor=actor,
            reason=reason,
            evidence=evidence,
            approval=approval,
        )

    def begin_capacity_trial(
        self,
        host_id: str,
        *,
        actor: str,
        reason: str,
        preflight_evidence: Mapping[str, Any],
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        return self._store.begin_capacity_trial(
            host_id,
            actor=actor,
            reason=reason,
            preflight_evidence=preflight_evidence,
            ttl_seconds=ttl_seconds,
        )

    def finish_capacity_trial(
        self,
        trial_id: str,
        *,
        actor: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._store.finish_capacity_trial(
            trial_id,
            actor=actor,
            result=result,
        )

    def begin_host_maintenance(
        self,
        host_id: str,
        *,
        intent: str,
        owner: str,
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        return self._store.acquire_host_maintenance(
            host_id,
            intent=intent,
            owner=owner,
            ttl_seconds=ttl_seconds,
        )

    def finish_host_maintenance(
        self,
        maintenance_id: str,
        *,
        owner: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._store.complete_host_maintenance(
            maintenance_id,
            owner=owner,
            result=result,
        )

    def reconcile_scheduling(
        self, *, connected_rigs: set[tuple[str, str]],
    ) -> SchedulingResult:
        """Reconcile expired ownership before selecting the next connected Rig.

        Each Store operation retains its own transaction. If dispatch fails after
        expiry reconciliation, the next call safely repeats reconciliation.
        """
        expired = self._store.reap_expired_leases()
        try:
            self._store.rerun_interrupted_work()
        except Exception as exc:  # noqa: BLE001 - an unfinished rerun decision is retried next pass
            logger.warning("rerun decision deferred: %s: %s", type(exc).__name__, exc)
        assignment = self.dispatch(connected_rigs=set(connected_rigs))
        return SchedulingResult(tuple(expired), assignment)

    def dispatch(
        self,
        *,
        connected_rigs: set[tuple[str, str]] | None = None,
    ) -> dict[str, Any] | None:
        advance = getattr(self._store, 'advance_recoveries', None)
        if advance is not None:
            advance()
        return self._store.dispatch(connected_rigs=connected_rigs)

    def reap_expired_leases(self) -> list[str]:
        return self._store.reap_expired_leases()

    def retry_assignment(self, assignment_id: str) -> dict[str, Any]:
        return self._store.retry_assignment(assignment_id)

    def rerun_assignment(self, assignment_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        return self._store.rerun_assignment(assignment_id, actor=actor, reason=reason)

    def quarantine_rig(
        self,
        rig_id: str,
        *,
        generation: int,
        actor: str,
        reason: str,
        evidence_refs: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]:
        return self._store.quarantine_rig(
            rig_id,
            generation=generation,
            actor=actor,
            reason=reason,
            evidence_refs=evidence_refs,
        )

    def register_agent(
        self,
        *,
        host_id: str,
        rig_id: str,
        generation: int,
        capability_support: list[str],
        target: Mapping[str, Any] | None = None,
        source_identity: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self._store.ensure_host(host_id)
        self._store.register_rig(
            rig_id,
            host_id=host_id,
            generation=generation,
            capability_support=capability_support,
            source_identity=source_identity,
        )
        if target is not None:
            self._store.register_rig_target(
                rig_id,
                host_id=host_id,
                generation=generation,
                target=target,
            )
        return self._store.sync_rig(host_id, rig_id)

    def sync_rig(self, host_id: str, rig_id: str) -> list[dict[str, Any]]:
        return self._store.sync_rig(host_id, rig_id)

    def heartbeat(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
    ) -> dict[str, Any]:
        return self._store.heartbeat(
            assignment_id,
            attempt_no=attempt_no,
            fencing=fencing,
        )

    def record_stage_result(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        stage: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._store.record_stage_result(
            assignment_id,
            attempt_no=attempt_no,
            fencing=fencing,
            stage=stage,
            result=result,
        )

    def complete_assignment(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        value = dict(result)
        artifacts = list(value.get("artifact_refs", []) or [])
        if not value.get('schema_version'):
            value.pop('artifact_refs', None)
        return self._store.complete_assignment(
            assignment_id,
            attempt_no=attempt_no,
            fencing=fencing,
            result=value,
            artifacts=artifacts,
        )

    def decline_assignment(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        *,
        reason: str,
        detail: str = "",
    ) -> dict[str, Any]:
        return self._store.decline_assignment(
            assignment_id, attempt_no=attempt_no, fencing=fencing, reason=reason, detail=detail,
        )

    def report_interrupted_attempt(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        *,
        host_id: str,
        rig_id: str,
        generation: int,
        reason: str,
        detail: str = "",
    ) -> dict[str, Any]:
        return self._store.report_interrupted_attempt(
            assignment_id, attempt_no=attempt_no, fencing=fencing,
            host_id=host_id, rig_id=rig_id, generation=generation, reason=reason, detail=detail,
        )

    def reconcile_stale_run_rows(self, *, apply: bool = False, actor: str | None = None) -> dict[str, Any]:
        return self._store.reconcile_stale_run_rows(apply=apply, actor=actor)

    def validate_device_write(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        *,
        host_id: str,
        rig_id: str,
        generation: int,
    ) -> dict[str, Any]:
        return self._store.validate_device_write(
            assignment_id,
            attempt_no=attempt_no,
            fencing=fencing,
            host_id=host_id,
            rig_id=rig_id,
            generation=generation,
        )

    def publish_assignment_observed_state(
        self, assignment_id: str, attempt_no: int, fencing: int, *,
        host_id: str, rig_id: str, generation: int,
        observed_state: Mapping[str, Any], capability_state: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._store.publish_assignment_observed_state(
            assignment_id, attempt_no=attempt_no, fencing=fencing,
            host_id=host_id, rig_id=rig_id, generation=generation,
            observed_state=observed_state, capability_state=capability_state,
        )

    def update_observed_state(
        self,
        rig_id: str,
        generation: int,
        observed_state: Mapping[str, Any],
        capability_state: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._store.update_observed_state(
            rig_id,
            generation=generation,
            observed_state=observed_state,
            capability_state=capability_state,
        )

    def report_screen_quarantine(
        self,
        host_id: str,
        rig_id: str,
        generation: int,
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._store.report_screen_quarantine(
            host_id, rig_id, generation=generation, state=state,
        )

    def recover_rig(
        self,
        rig_id: str,
        generation: int,
        actor: str,
        probe_result: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._store.record_rig_recovery(
            rig_id,
            generation=generation,
            actor=actor,
            probe_result=probe_result,
        )

    def execute_assigned(
        self,
        assignment_id: str,
        executor: AssignmentExecutor,
    ) -> dict[str, Any]:
        assignment = self.get_assignment(assignment_id)
        attempt = assignment.get("attempt") or {}
        result = dict(executor.execute(assignment))
        return self._store.complete_assignment(
            assignment_id,
            attempt_no=int(attempt["attempt_no"]),
            fencing=int(attempt["fencing"]),
            result=result,
            artifacts=list(result.get("artifact_refs", []) or []),
        )


class WorkRouter:
    """Route Host-only work without creating a Rig Assignment."""

    def __init__(self, control_plane: ControlPlaneInterface, host_executor: HostWorkExecutor) -> None:
        self.control_plane = control_plane
        self.host_executor = host_executor
        self._host_results: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def submit(self, plan: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        if bool(plan.get("requires_rig", True)):
            return self.control_plane.submit_assignment(plan, idempotency_key)
        with self._lock:
            previous = self._host_results.get(idempotency_key)
            if previous is not None:
                return deepcopy(previous)
            result = {
                "work_kind": "host_only",
                "idempotency_key": idempotency_key,
                "result": dict(self.host_executor.execute(plan)),
            }
            self._host_results[idempotency_key] = result
            return deepcopy(result)
