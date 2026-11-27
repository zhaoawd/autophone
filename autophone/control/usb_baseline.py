"""Pure semantic comparison for same-Host dual-Rig USB evidence."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping


BASELINE_SCHEMA = "single-rig-usb-baseline.v1"
DUAL_OBSERVATION_SCHEMA = "same-host-dual-rig-usb-observation.v1"

# vm_fatal 事件判据的版本（谓词文本见 autophone/vm_fatal.py）。
# 1 = 2026-10-10 前的口径：Virtualization 进程任意 Error 行或含 "Fatal error" 的行。
# 2 = 只计 Fault 行、含 "Fatal error" 的行与 IOUSBHostCIExceptionType* 签名行。
# 文档不带 `vm_fatal_predicate_version` 时按 1 处理（字段引入前产生的证据）。
# 新增字段属加法变更，schema_version 不递增；可比性由本字段单独判定。
LEGACY_VM_FATAL_PREDICATE_VERSION = 1
VM_FATAL_PREDICATE_VERSION = 2
SUPPORTED_VM_FATAL_PREDICATE_VERSIONS = (
    LEGACY_VM_FATAL_PREDICATE_VERSION,
    VM_FATAL_PREDICATE_VERSION,
)


class BaselineEvidenceError(ValueError):
    pass


def vm_fatal_predicate_version(document: Mapping[str, Any], field: str) -> int:
    """Return the document's vm_fatal predicate version; absent means legacy (1)."""

    if "vm_fatal_predicate_version" not in document:
        return LEGACY_VM_FATAL_PREDICATE_VERSION
    value = document["vm_fatal_predicate_version"]
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value not in SUPPORTED_VM_FATAL_PREDICATE_VERSIONS
    ):
        raise BaselineEvidenceError(
            f"{field}.vm_fatal_predicate_version must be one of "
            f"{list(SUPPORTED_VM_FATAL_PREDICATE_VERSIONS)}"
        )
    return value


def vm_fatal_predicate_comparability(
    baseline: Mapping[str, Any],
    observation_version: int,
) -> dict[str, Any]:
    """Decide whether a baseline can be compared with an observation's vm_fatal count.

    - 两侧版本相同：直接比较。
    - 基线为 legacy（v1 或缺字段）、观测为 v2：v1 计数可能含 v2 不计的 Error 行，
      v1 基线的 host_crash 速率是 v2 等价速率的上界。速率为 0 时两者相等，可以比较，
      结果记 `baseline_predicate=legacy`；速率大于 0 时 v2 等价速率未知，拿偏高的
      基线比较会放宽判定，拒绝比较。
    - 基线为 v2、观测为 v1：观测计数可能含 v2 不计的行，拒绝比较。
    """

    baseline_version = vm_fatal_predicate_version(baseline, "baseline")
    if observation_version not in SUPPORTED_VM_FATAL_PREDICATE_VERSIONS:
        raise BaselineEvidenceError(
            "observation.vm_fatal_predicate_version must be one of "
            f"{list(SUPPORTED_VM_FATAL_PREDICATE_VERSIONS)}"
        )
    if baseline_version > observation_version:
        raise BaselineEvidenceError(
            f"baseline uses vm_fatal predicate v{baseline_version} but the observation "
            f"uses v{observation_version}; the observation is not comparable"
        )
    if baseline_version < observation_version:
        rates = baseline.get("rates_per_vm_hour")
        rate = rates.get("host_crash") if isinstance(rates, Mapping) else None
        if (
            isinstance(rate, bool)
            or not isinstance(rate, (int, float))
            or rate != 0
        ):
            raise BaselineEvidenceError(
                "baseline uses the legacy vm_fatal predicate and its host_crash rate is "
                "not zero; it cannot be compared with a "
                f"v{observation_version} observation"
            )
    return {
        "baseline_version": baseline_version,
        "baseline_version_recorded": "vm_fatal_predicate_version" in baseline,
        "observation_version": observation_version,
        "baseline_predicate": (
            "legacy"
            if baseline_version == LEGACY_VM_FATAL_PREDICATE_VERSION
            else "current"
        ),
    }


def _parse_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BaselineEvidenceError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BaselineEvidenceError(f"{field} must include a timezone")
    return parsed


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise BaselineEvidenceError(f"{field} must be a non-negative integer")
    try:
        converted = int(value)
    except (TypeError, ValueError) as exc:
        raise BaselineEvidenceError(f"{field} must be a non-negative integer") from exc
    if converted < 0 or converted != value:
        raise BaselineEvidenceError(f"{field} must be a non-negative integer")
    return converted


def _non_negative_float(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise BaselineEvidenceError(f"{field} must be a non-negative number")
    try:
        converted = float(value)
    except (TypeError, ValueError) as exc:
        raise BaselineEvidenceError(f"{field} must be a non-negative number") from exc
    if converted < 0:
        raise BaselineEvidenceError(f"{field} must be a non-negative number")
    return converted


def compare_dual_rig_observation(
    baseline: Mapping[str, Any],
    observation: Mapping[str, Any],
    *,
    declared_duration_seconds: float,
    acceptance_criteria: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare one dual-Rig window with a single-Rig M0 baseline.

    The tolerance is absolute rate increase per VM-hour. This remains defined
    when the M0 rate is zero, unlike a ratio, and the caller must freeze every
    tolerance before the live window starts.
    """

    if baseline.get("schema_version") != BASELINE_SCHEMA:
        raise BaselineEvidenceError("unsupported single-Rig baseline schema")
    if observation.get("schema_version") != DUAL_OBSERVATION_SCHEMA:
        raise BaselineEvidenceError("unsupported dual-Rig observation schema")
    predicate_record = vm_fatal_predicate_comparability(
        baseline,
        vm_fatal_predicate_version(observation, "observation"),
    )

    declared = _non_negative_float(declared_duration_seconds, "declared_duration_seconds")
    if declared <= 0:
        raise BaselineEvidenceError("declared_duration_seconds must be positive")
    window = dict(observation.get("window") or {})
    started = _parse_time(str(window.get("started_at") or ""), "window.started_at")
    ended = _parse_time(str(window.get("ended_at") or ""), "window.ended_at")
    actual_duration = (ended - started).total_seconds()
    recorded_duration = _non_negative_float(
        window.get("duration_seconds"), "window.duration_seconds"
    )
    if abs(recorded_duration - actual_duration) > 0.001:
        raise BaselineEvidenceError(
            "dual-Rig window.duration_seconds does not match started_at and ended_at"
        )
    if actual_duration + 0.001 < declared:
        raise BaselineEvidenceError(
            "dual-Rig observation duration is shorter than the declared duration"
        )

    baseline_fingerprint = dict(baseline.get("environment_fingerprint") or {})
    observed_fingerprint = dict(observation.get("environment_fingerprint") or {})
    comparable_fields = ("host_id", "macos_build", "vphone_cli_commit")
    drift = {
        field: {
            "baseline": baseline_fingerprint.get(field),
            "observed": observed_fingerprint.get(field),
        }
        for field in comparable_fields
        if not str(baseline_fingerprint.get(field) or "").strip()
        or baseline_fingerprint.get(field) != observed_fingerprint.get(field)
    }
    if drift:
        raise BaselineEvidenceError(
            "dual-Rig environment is not comparable with the baseline: "
            + ", ".join(sorted(drift))
        )

    exposure = dict(observation.get("exposure") or {})
    vm_hours = _non_negative_float(exposure.get("vm_hours"), "exposure.vm_hours")
    if vm_hours <= 0:
        raise BaselineEvidenceError("dual-Rig exposure.vm_hours must be positive")
    operation_count = _non_negative_int(
        exposure.get("device_operation_count"), "exposure.device_operation_count"
    )
    minimum_operations = _non_negative_int(
        acceptance_criteria.get("min_device_operation_count"),
        "acceptance_criteria.min_device_operation_count",
    )
    per_rig = dict(exposure.get("per_rig") or {})
    if len(per_rig) < 2:
        raise BaselineEvidenceError("same-Host exposure must contain at least two Rigs")
    incomplete_modes = sorted(
        str(rig_id)
        for rig_id, value in per_rig.items()
        if not isinstance(value, Mapping)
        or _non_negative_float(value.get("active_seconds"), "active_seconds") <= 0
        or _non_negative_float(value.get("idle_seconds"), "idle_seconds") <= 0
        or _non_negative_int(value.get("guest_probe_count"), "guest_probe_count") <= 0
    )

    baseline_rates = dict(baseline.get("rates_per_vm_hour") or {})
    baseline_host_rate = _non_negative_float(
        baseline_rates.get("host_crash"), "baseline.rates_per_vm_hour.host_crash"
    )
    baseline_usb_rates = dict(baseline_rates.get("usb_exceptions") or {})
    outcomes = dict(observation.get("outcomes") or {})
    host_crashes = _non_negative_int(
        outcomes.get("host_crash_count"), "outcomes.host_crash_count"
    )
    usb_counts = {
        str(kind): _non_negative_int(count, f"outcomes.usb_exception_counts.{kind}")
        for kind, count in dict(outcomes.get("usb_exception_counts") or {}).items()
    }
    observed_host_rate = host_crashes / vm_hours
    observed_usb_rates = {kind: count / vm_hours for kind, count in usb_counts.items()}
    new_types = sorted(set(usb_counts) - set(baseline_usb_rates))

    host_tolerance = _non_negative_float(
        acceptance_criteria.get("max_host_crash_rate_increase_per_vm_hour"),
        "acceptance_criteria.max_host_crash_rate_increase_per_vm_hour",
    )
    raw_usb_tolerances = acceptance_criteria.get(
        "max_usb_exception_rate_increase_per_vm_hour"
    )
    if not isinstance(raw_usb_tolerances, Mapping):
        raise BaselineEvidenceError(
            "acceptance_criteria.max_usb_exception_rate_increase_per_vm_hour "
            "must be an object"
        )
    usb_tolerances = {
        str(kind): _non_negative_float(
            value,
            "acceptance_criteria.max_usb_exception_rate_increase_per_vm_hour."
            + str(kind),
        )
        for kind, value in raw_usb_tolerances.items()
    }
    missing_tolerances = sorted(set(baseline_usb_rates) - set(usb_tolerances))
    if missing_tolerances:
        raise BaselineEvidenceError(
            "USB acceptance tolerance is missing baseline types: "
            + ", ".join(missing_tolerances)
        )

    failures: list[str] = []
    if operation_count < minimum_operations:
        failures.append("device_operation_count_below_minimum")
    if incomplete_modes:
        failures.append("active_idle_exposure_incomplete")
    if new_types:
        failures.append("new_usb_exception_types")
    if observed_host_rate > baseline_host_rate + host_tolerance:
        failures.append("host_crash_rate_exceeded")
    exceeded_usb_types = sorted(
        kind
        for kind, baseline_rate in baseline_usb_rates.items()
        if observed_usb_rates.get(kind, 0.0)
        > _non_negative_float(baseline_rate, f"baseline USB rate {kind}")
        + usb_tolerances[kind]
    )
    if exceeded_usb_types:
        failures.append("usb_exception_rate_exceeded")

    return {
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "declared_duration_seconds": declared,
        "observed_duration_seconds": actual_duration,
        "environment_comparison": {
            field: observed_fingerprint.get(field) for field in comparable_fields
        },
        "exposure": dict(exposure),
        "baseline_rates_per_vm_hour": dict(baseline_rates),
        "observed_rates_per_vm_hour": {
            "host_crash": observed_host_rate,
            "usb_exceptions": dict(sorted(observed_usb_rates.items())),
        },
        "acceptance_criteria": dict(acceptance_criteria),
        "new_usb_exception_types": new_types,
        "exceeded_usb_exception_types": exceeded_usb_types,
        "incomplete_active_idle_rigs": incomplete_modes,
        "vm_fatal_predicate": predicate_record,
    }
