"""Intent and projection contracts; persistence and scheduling steps stay private."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable


@runtime_checkable
class ControlPlaneInterface(Protocol):
    def get_run(self, run_id: str) -> dict[str, Any]: ...
    def observe_vphone_down(self, **observation: Any) -> dict[str, Any]: ...
    def get_recovery(self, recovery_id: str) -> dict[str, Any]: ...
    def get_run_recoveries(self, run_id: str) -> list[dict[str, Any]]: ...
    def list_rig_recoveries(self, rig_id: str, *, active_only: bool = False) -> list[dict[str, Any]]: ...
    def rig_activity(self, rig_id: str) -> dict[str, list[str]]: ...
    def resume_recovery(self, recovery_id: str, *, retry: bool = False) -> dict[str, Any]: ...
    def confirm_recovery_isolation(self, recovery_id: str, *, actor: str, reason: str) -> dict[str, Any]: ...
    def submit_assignment(
        self, plan: Mapping[str, Any], idempotency_key: str
    ) -> dict[str, Any]: ...
    def cancel_assignment(
        self, assignment_id: str, actor: str, reason: str
    ) -> dict[str, Any]: ...
    def get_assignment(self, assignment_id: str) -> dict[str, Any]: ...
    def retry_assignment(self, assignment_id: str) -> dict[str, Any]: ...
    def rerun_assignment(self, assignment_id: str, *, actor: str, reason: str) -> dict[str, Any]: ...
    def reconcile_stale_run_rows(
        self, *, apply: bool = False, actor: str | None = None
    ) -> dict[str, Any]: ...
    def create_reservation(self, spec: Mapping[str, Any]) -> dict[str, Any]: ...
    def release_reservation(
        self, reservation_id: str, actor: str
    ) -> dict[str, Any]: ...
    def list_reservations(
        self, *, statuses: Any | None = None
    ) -> list[dict[str, Any]]: ...
    def list_rigs(
        self, *, include_deregistered: bool = False
    ) -> list[dict[str, Any]]: ...
    def deregister_rig(
        self, rig_id: str, generation: int, *, actor: str, reason: str
    ) -> dict[str, Any]: ...
    def recover_rig(
        self, rig_id: str, generation: int, actor: str, probe_result: Mapping[str, Any]
    ) -> dict[str, Any]: ...
    def read_observed_state(
        self, host_id: str, rig_id: str, generation: int
    ) -> dict[str, Any]: ...
    def create_access_grant(self, spec: Mapping[str, Any]) -> dict[str, Any]: ...
    def get_access_grant(self, grant_id: str) -> dict[str, Any] | None: ...
    def find_access_grant(
        self, *, request_id: str | None = None, console_session_id: str | None = None
    ) -> dict[str, Any] | None: ...
    def list_access_grants(
        self,
        *,
        statuses: Any | None = None,
        platform_id: str | None = None,
        devices: Any | None = None,
    ) -> list[dict[str, Any]]: ...
    def update_access_grant(
        self,
        grant_id: str,
        changes: Mapping[str, Any],
        *,
        event: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...
    def list_access_grant_events(
        self, *, grant_id: str | None = None, device: str | None = None
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class AgentControl(Protocol):
    def register_agent(
        self,
        *,
        host_id: str,
        rig_id: str,
        generation: int,
        capability_support: list[str],
        target: Mapping[str, Any] | None = None,
        source_identity: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...
    def begin_agent_connection(
        self, host_id: str, rig_id: str, generation: int, connection_id: str
    ) -> None: ...
    def refresh_agent_connection(self, rig_id: str, connection_id: str) -> bool: ...
    def end_agent_connection(self, rig_id: str, connection_id: str) -> None: ...
    def sync_rig(self, host_id: str, rig_id: str) -> list[dict[str, Any]]: ...
    def heartbeat(
        self, assignment_id: str, attempt_no: int, fencing: int
    ) -> dict[str, Any]: ...
    def record_stage_result(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        stage: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]: ...
    def complete_assignment(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        result: Mapping[str, Any],
    ) -> dict[str, Any]: ...
    def decline_assignment(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        *,
        reason: str,
        detail: str = "",
    ) -> dict[str, Any]: ...
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
    ) -> dict[str, Any]: ...
    def validate_device_write(
        self,
        assignment_id: str,
        attempt_no: int,
        fencing: int,
        *,
        host_id: str,
        rig_id: str,
        generation: int,
    ) -> dict[str, Any]: ...
    def publish_assignment_observed_state(
        self, assignment_id: str, attempt_no: int, fencing: int, *,
        host_id: str, rig_id: str, generation: int,
        observed_state: Mapping[str, Any], capability_state: Mapping[str, Any],
    ) -> dict[str, Any]: ...
    def update_observed_state(
        self,
        rig_id: str,
        generation: int,
        observed_state: Mapping[str, Any],
        capability_state: Mapping[str, Any],
    ) -> dict[str, Any]: ...
    def read_observed_state(
        self, host_id: str, rig_id: str, generation: int
    ) -> dict[str, Any]: ...
    def report_screen_quarantine(
        self, host_id: str, rig_id: str, generation: int, state: Mapping[str, Any]
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SchedulingResult:
    expired: tuple[str, ...]
    assignment: dict[str, Any] | None


@runtime_checkable
class SchedulingControl(Protocol):
    def reconcile_scheduling(
        self, *, connected_rigs: set[tuple[str, str]]
    ) -> SchedulingResult: ...
