"""Host Coordination contract; PostgreSQL transactions remain shared with scheduling.

The ControlPlane implementation satisfies this protocol during migration. Host
consumers depend on this surface, not Assignment routing or Store transactions.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


@runtime_checkable
class HostCoordination(Protocol):
    def get_host(self, host_id: str) -> dict[str, Any]: ...

    def record_host_anomaly(self, record: Mapping[str, Any]) -> dict[str, Any]: ...

    def record_host_load_samples(
        self, host_id: str, samples: Sequence[Mapping[str, Any]]
    ) -> int: ...

    def host_active_work(
        self, host_id: str, *, recent_seconds: int = 0
    ) -> list[dict[str, Any]]: ...

    def list_host_anomalies(
        self, host_id: str, *, kind: str | None = None, since: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def get_host_anomaly(self, anomaly_id: str) -> dict[str, Any]: ...

    def add_host_anomaly_review(
        self, anomaly_id: str, *, actor: str, note: str, labels: Sequence[str] = (),
    ) -> dict[str, Any]: ...

    def reviewed_host_anomaly_ids(self, host_id: str) -> set[str]: ...

    def host_anomaly_report(
        self, host_id: str, *, since: str | None = None, include_test: bool = False,
    ) -> dict[str, Any]: ...

    def set_host_allocatable(
        self, host_id: str, *, memory_bytes: int, actor: str, reason: str,
    ) -> dict[str, Any]: ...

    def set_host_admission_mode(
        self, host_id: str, mode: str, *, actor: str, reason: str,
    ) -> dict[str, Any]: ...

    def host_breaker_state(self, host_id: str) -> dict[str, Any]: ...

    def reset_host_breaker(self, host_id: str, *, actor: str, reason: str) -> dict[str, Any]: ...

    def report_rig_resources(
        self, host_id: str, requests: Mapping[str, Mapping[str, Any]]
    ) -> None: ...

    def host_admission_shadow_report(
        self, host_id: str, *, since: str | None = None
    ) -> dict[str, Any]: ...

    def change_host_capacity(
        self,
        host_id: str,
        capacity: int,
        *,
        actor: str,
        reason: str,
        evidence: Mapping[str, Any] | None = None,
        approval: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def begin_capacity_trial(
        self,
        host_id: str,
        *,
        actor: str,
        reason: str,
        preflight_evidence: Mapping[str, Any],
        ttl_seconds: int = 300,
    ) -> dict[str, Any]: ...

    def finish_capacity_trial(
        self, trial_id: str, *, actor: str, result: Mapping[str, Any]
    ) -> dict[str, Any]: ...

    def begin_host_maintenance(
        self, host_id: str, *, intent: str, owner: str, ttl_seconds: int = 300
    ) -> dict[str, Any]: ...

    def finish_host_maintenance(
        self, maintenance_id: str, *, owner: str, result: Mapping[str, Any]
    ) -> dict[str, Any]: ...

    def quarantine_rig(
        self,
        rig_id: str,
        *,
        generation: int,
        actor: str,
        reason: str,
        evidence_refs: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]: ...
