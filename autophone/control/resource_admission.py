"""Resource-based admission decision. Pure: no database, no clock, no side effects.

步骤 C 只用它做影子判定，见 docs/platform-host-resource-admission.md §4。
"""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import Any

# kern.memorystatus_vm_pressure_level：1 normal、2 warn、4 critical。2026-11-04 本机 2 台
# 空闲 VM 就会到 warn，所以初始只把 critical 算作拒绝，再按影子数据调整。
PRESSURE_REJECT_LEVEL = 4
SAMPLE_MAX_AGE_S = 180

NOT_CONFIGURED = "resource_not_configured"
PRESSURE_UNKNOWN = "host_pressure_unknown"
PRESSURE = "host_pressure"
MEMORY = "host_memory"


def decide(
    *,
    rig_id: str,
    request: Mapping[str, Any] | None,
    allocatable: Mapping[str, Any] | None,
    sample: Mapping[str, Any] | None,
    leased: Mapping[str, Mapping[str, Any] | None],
    now: datetime,
) -> tuple[str | None, dict[str, Any]]:
    """Return ``(reason, inputs)``; ``reason`` is ``None`` when the Rig would be admitted."""
    allocatable_bytes = (allocatable or {}).get("memory_bytes")
    request_bytes = (request or {}).get("memory_bytes")
    inputs: dict[str, Any] = {"allocatable_bytes": allocatable_bytes, "request_bytes": request_bytes}
    if allocatable_bytes is None or request_bytes is None:
        return NOT_CONFIGURED, inputs
    # 没有新鲜样本就不知道 Host 的状态，不能当作空闲。
    if sample is None:
        return PRESSURE_UNKNOWN, {**inputs, "sample": "missing"}
    age = (now - sample["sampled_at"]).total_seconds()
    inputs.update(sample_age_s=round(age), pressure_level=sample.get("pressure_level"))
    if age > SAMPLE_MAX_AGE_S or sample.get("running_vms") is None or sample.get("pressure_level") is None:
        return PRESSURE_UNKNOWN, inputs
    if sample["pressure_level"] >= PRESSURE_REJECT_LEVEL:
        return PRESSURE, inputs
    committed, running = committed_memory(sample["running_vms"], leased)
    added = 0 if rig_id in running else int(request_bytes)
    inputs.update(committed_bytes=committed, added_bytes=added)
    # VM 已在跑时派任务不增加内存占用。即使总量已超标，拒绝它也释放不了内存；
    # 这种压力由上面的 pressure 判定处理。
    if added and committed + added > int(allocatable_bytes):
        return MEMORY, inputs
    return None, inputs


def committed_memory(
    running_vms: Sequence[Mapping[str, Any]],
    leased: Mapping[str, Mapping[str, Any] | None],
) -> tuple[int, Collection[str]]:
    """Memory held by running VMs, including unregistered ones, plus leased Rigs not yet up."""
    running = {str(vm["rig_id"]) for vm in running_vms if vm.get("rig_id")}
    total = sum(int(vm.get("memory_bytes") or 0) for vm in running_vms)
    for rig_id, request in leased.items():
        if rig_id not in running:
            total += int((request or {}).get("memory_bytes") or 0)
    return total, running
