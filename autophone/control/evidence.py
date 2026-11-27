"""Validation of the live same-Host multi-Rig P0 evidence bundle."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

from .usb_baseline import (
    LEGACY_VM_FATAL_PREDICATE_VERSION,
    compare_dual_rig_observation,
)


REQUIRED_P0_CHECKS = (
    "concurrent_vm_start",
    "endpoint_and_file_isolation",
    "screen_action_isolation",
    "single_rig_fault_isolation",
    "crash_usb_baseline_comparison",
    "storage_isolation",
    "capacity_rollback",
)
CAPACITY_ADMISSION_PROFILE = "capacity_qualification"
CAPACITY_ADMISSION_REVIEW_SCHEMA = "same-host-capacity-admission-review.v1"
CAPACITY_QUALIFICATION_MIN_DURATION_SECONDS = 3600.0
CAPACITY_QUALIFICATION_MIN_BASELINE_VM_HOURS = 2.0
CAPACITY_ADMISSION_ATTESTATIONS = (
    "sustained_single_rig_baselines",
    "dual_rig_long_window",
    "health_down_edges",
    "virtualization_fatal",
    "usb_exception_types",
    "lockdownd_62078_callers",
)
PRE_CAPACITY_P0_CHECKS = tuple(
    name for name in REQUIRED_P0_CHECKS if name != "capacity_rollback"
)
CAPACITY_ROLLBACK_EXPECTED_RECEIPT = {
    "previous_capacity": 1,
    "trial_capacity": 2,
    "rollback_capacity": 1,
    "configured_capacity_unchanged": True,
    "simultaneous_assignment_count": 2,
    "existing_assignments_preserved": True,
    "new_assignment_wait_reason": "host_capacity",
    "post_rollback_assignment_resumed": True,
    "final_active_rigs": 0,
    "final_effective_capacity": 1,
}
CAPACITY_ROLLBACK_REQUIRED_EVENTS = frozenset(
    {"capacity_trial_started", "capacity_trial_rolled_back"}
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _non_empty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _evidence_refs_valid(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and bool(value)
        and all(_non_empty_text(ref) for ref in value)
    )


def _sha256_valid(value: Any) -> bool:
    digest = str(value or "").lower()
    return len(digest) == 64 and all(
        character in "0123456789abcdef" for character in digest
    )


def capacity_targets_registered(
    reported_targets: Any,
    registered_targets: Any,
    *,
    host_id: str,
) -> bool:
    """Evidence names at least two Rigs, each registered with the same identity.

    A Rig the evidence does not name may stay registered: the scheduler keeps it exclusive.
    """

    def identities(value: Any) -> set[tuple[str, int, str]] | None:
        if not isinstance(value, (list, tuple)):
            return None
        result: set[tuple[str, int, str]] = set()
        for row in value:
            if not isinstance(row, Mapping) or row.get("host_id") != host_id:
                return None
            generation = row.get("rig_generation")
            if (
                isinstance(generation, bool)
                or not isinstance(generation, int)
                or generation <= 0
            ):
                return None
            rig_id = str(row.get("rig_id") or "")
            udid = str(row.get("expected_usb_udid") or "")
            if not rig_id or not udid:
                return None
            result.add((rig_id, generation, udid))
        return result if len(result) == len(value) else None

    reported = identities(reported_targets)
    registered = identities(registered_targets)
    return (
        reported is not None
        and registered is not None
        and len(reported) >= 2
        and reported <= registered
    )


def capacity_rollback_receipt_mismatches(
    receipt: Mapping[str, Any],
    *,
    rig_count: int = 2,
) -> dict[str, dict[str, Any]]:
    """Return every failed semantic requirement for a Capacity drill receipt.

    The drill must have held a lease on every Rig the evidence covers at once.
    """

    expected_receipt = {
        **CAPACITY_ROLLBACK_EXPECTED_RECEIPT,
        "trial_capacity": rig_count,
        "simultaneous_assignment_count": rig_count,
    }
    mismatches = {
        key: {"expected": expected, "actual": receipt.get(key)}
        for key, expected in expected_receipt.items()
        if receipt.get(key) != expected
    }
    event_kinds = {str(item) for item in receipt.get("events") or []}
    missing_events = sorted(CAPACITY_ROLLBACK_REQUIRED_EVENTS - event_kinds)
    if missing_events:
        mismatches["events"] = {
            "expected": sorted(CAPACITY_ROLLBACK_REQUIRED_EVENTS),
            "actual": sorted(event_kinds),
        }
    preflight_ref = receipt.get("preflight_evidence_ref")
    if not _non_empty_text(preflight_ref):
        mismatches["preflight_evidence_ref"] = {
            "expected": "non-empty evidence reference",
            "actual": preflight_ref,
        }
    preflight_sha = str(receipt.get("preflight_evidence_sha256") or "").lower()
    if len(preflight_sha) != 64 or any(
        character not in "0123456789abcdef" for character in preflight_sha
    ):
        mismatches["preflight_evidence_sha256"] = {
            "expected": "64 lowercase hexadecimal characters",
            "actual": receipt.get("preflight_evidence_sha256"),
        }
    return mismatches


def _read_evidence_bytes(
    ref: str,
    blobs: Any,
    evidence_root: Path | None,
) -> tuple[bytes | None, str | None]:
    """Return the bytes a check is graded on, preferring what a human can re-read.

    An embedded blob alone would let the report grade itself: the reviewer opens
    the file tree while the gate hashes the payload carried inside the report.
    So whenever a trusted local root is declared, the file on disk is the graded
    copy and an embedded blob must match it byte for byte. Blob-only evidence is
    accepted only for references this host cannot read back (a bundle shipped
    from another host, or a non-file reference).
    """

    blob: bytes | None = None
    if isinstance(blobs, Mapping) and _non_empty_text(blobs.get(ref)):
        try:
            blob = base64.b64decode(str(blobs[ref]), validate=True)
        except (ValueError, binascii.Error) as exc:
            return None, f"embedded evidence is not valid base64: {ref}: {exc}"
    parsed = urlparse(str(ref))
    if parsed.scheme != "file" or not parsed.path:
        if blob is None:
            return None, f"unsupported evidence reference: {ref}"
        return blob, None
    if evidence_root is None:
        if blob is None:
            return None, f"trusted evidence_root is required for local evidence: {ref}"
        return blob, None
    try:
        root = evidence_root.expanduser().resolve(strict=True)
        path = Path(unquote(parsed.path)).expanduser().resolve(strict=True)
    except OSError as exc:
        return None, f"cannot resolve evidence {ref}: {exc}"
    if not path.is_relative_to(root):
        return None, f"evidence is outside the trusted root: {ref}"
    try:
        stored = path.read_bytes()
    except OSError as exc:
        return None, f"cannot read evidence {ref}: {exc}"
    if blob is not None and blob != stored:
        return None, f"embedded evidence does not match the file on disk: {ref}"
    return stored, None


def _verify_check_artifacts(
    name: str,
    check: Mapping[str, Any],
    *,
    expected_host_id: str,
    expected_rig_ids: set[str],
    expected_udids: set[str],
    experiment_setup: Mapping[str, Any],
    evidence_root: Path | None,
    verify_environment: bool,
) -> str | None:
    refs = list(check.get("evidence_refs") or [])
    blobs = check.get("evidence_blobs")
    hashes = check.get("evidence_sha256")
    if hashes is None and len(refs) == 1 and _non_empty_text(check.get("sha256")):
        hashes = {refs[0]: check["sha256"]}
    if not isinstance(hashes, Mapping):
        return "evidence SHA-256 mapping is required"
    for ref in refs:
        encoded, failure = _read_evidence_bytes(ref, blobs, evidence_root)
        if failure is not None or encoded is None:
            return failure or f"evidence is unreadable: {ref}"
        expected_sha = str(hashes.get(ref) or "").lower()
        actual_sha = hashlib.sha256(encoded).hexdigest()
        if expected_sha != actual_sha:
            return f"evidence SHA-256 mismatch: {ref}"
        try:
            artifact = json.loads(encoded)
        except json.JSONDecodeError as exc:
            return f"evidence is not valid JSON: {ref}: {exc}"
        if not isinstance(artifact, Mapping):
            return f"evidence must be an object: {ref}"
        if artifact.get("check") != name or artifact.get("status") != "passed":
            return f"evidence check identity/status mismatch: {ref}"
        if str(artifact.get("host_id") or "") != expected_host_id:
            return f"evidence host_id mismatch: {ref}"
        if set(str(item) for item in artifact.get("rig_ids") or []) != expected_rig_ids:
            return f"evidence rig_ids mismatch: {ref}"
        if set(str(item) for item in artifact.get("expected_usb_udids") or []) != expected_udids:
            return f"evidence expected_usb_udids mismatch: {ref}"
        if not isinstance(artifact.get("observations"), Mapping) or not artifact["observations"]:
            return f"evidence observations are empty: {ref}"
        try:
            started = datetime.fromisoformat(str(artifact["started_at"]).replace("Z", "+00:00"))
            ended = datetime.fromisoformat(str(artifact["ended_at"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            return f"evidence timestamps are invalid: {ref}"
        if started.tzinfo is None or ended.tzinfo is None or ended < started:
            return f"evidence timestamp order/timezone is invalid: {ref}"
        observations = dict(artifact["observations"])
        if name == "crash_usb_baseline_comparison":
            qualification = (
                experiment_setup.get("admission_profile")
                == CAPACITY_ADMISSION_PROFILE
            )
            declared_baselines = (
                _mapping(experiment_setup.get("single_rig_baselines"))
                if qualification
                else {
                    "single": {
                        "ref": experiment_setup.get("single_rig_baseline_ref"),
                        "sha256": experiment_setup.get(
                            "single_rig_baseline_sha256"
                        ),
                    }
                }
            )
            observed_baselines = (
                _mapping(observations.get("baselines"))
                if qualification
                else {
                    "single": {
                        "ref": observations.get("baseline_ref"),
                        "sha256": observations.get("baseline_sha256"),
                    }
                }
            )
            observed_comparisons = (
                _mapping(observations.get("comparisons"))
                if qualification
                else {"single": observations.get("comparison")}
            )
            expected_baseline_ids = expected_rig_ids if qualification else {"single"}
            if (
                set(declared_baselines) != expected_baseline_ids
                or set(observed_baselines) != expected_baseline_ids
                or set(observed_comparisons) != expected_baseline_ids
            ):
                return f"USB baseline target identities mismatch: {ref}"
            for rig_id in sorted(expected_baseline_ids):
                declared = _mapping(declared_baselines.get(rig_id))
                observed = _mapping(observed_baselines.get(rig_id))
                baseline_ref = str(observed.get("ref") or "")
                baseline_sha = str(observed.get("sha256") or "").lower()
                if (
                    baseline_ref != str(declared.get("ref") or "")
                    or baseline_sha != str(declared.get("sha256") or "").lower()
                ):
                    return f"USB baseline identity mismatch for {rig_id}: {ref}"
                baseline_bytes, baseline_failure = _read_evidence_bytes(
                    baseline_ref,
                    {},
                    evidence_root,
                )
                if baseline_failure or baseline_bytes is None:
                    return baseline_failure or f"USB baseline is unreadable: {baseline_ref}"
                if hashlib.sha256(baseline_bytes).hexdigest() != baseline_sha:
                    return f"USB baseline SHA-256 mismatch: {baseline_ref}"
                try:
                    baseline = json.loads(baseline_bytes)
                    if not isinstance(baseline, Mapping):
                        return f"USB baseline must be an object: {baseline_ref}"
                    if qualification:
                        baseline_exposure = _mapping(baseline.get("exposure"))
                        observation_refs = baseline.get("observation_refs")
                        if (
                            int(baseline.get("window_count") or 0) < 2
                            or float(baseline_exposure.get("vm_hours") or 0)
                            < CAPACITY_QUALIFICATION_MIN_BASELINE_VM_HOURS
                            or not isinstance(observation_refs, (list, tuple))
                            or not _evidence_refs_valid(observation_refs)
                            or len(set(observation_refs)) != len(observation_refs)
                            or len(observation_refs)
                            != int(baseline.get("window_count") or 0)
                        ):
                            return f"qualification baseline is incomplete for {rig_id}: {ref}"
                    observed_identity = _mapping(_mapping(observations.get("observation")).get("environment_fingerprint"))
                    declared_identity = _mapping(experiment_setup.get("environment_fingerprint"))
                    if verify_environment and any(declared_identity.get(k) != observed_identity.get(k) for k in (
                        "host_id", "macos_build", "vphone_cli_commit"
                    )):
                        return f"USB observation environment identity mismatch: {ref}"
                    recomputed = compare_dual_rig_observation(
                        baseline,
                        _mapping(observations.get("observation")),
                        declared_duration_seconds=float(
                            experiment_setup.get("duration_seconds") or 0
                        ),
                        acceptance_criteria=_mapping(
                            experiment_setup.get("usb_acceptance_criteria")
                        ),
                    )
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    return f"USB baseline comparison is invalid: {ref}: {exc}"
                if recomputed.get("status") != "passed" or recomputed.get("failures"):
                    return f"USB baseline comparison did not pass for {rig_id}: {ref}"
                stored_comparison = _mapping(observed_comparisons.get(rig_id))
                if (
                    "vm_fatal_predicate" not in stored_comparison
                    and _mapping(recomputed.get("vm_fatal_predicate")).get(
                        "observation_version"
                    )
                    == LEGACY_VM_FATAL_PREDICATE_VERSION
                ):
                    # 判据版本字段引入前写出的回执（观测为 v1）没有该键；去掉复算结果
                    # 里的该键再比，历史证据仍可复核。v2 观测的回执必须带该键。
                    recomputed = {
                        key: value
                        for key, value in recomputed.items()
                        if key != "vm_fatal_predicate"
                    }
                if stored_comparison != recomputed:
                    return f"USB baseline comparison receipt mismatch for {rig_id}: {ref}"
        elif name == "capacity_rollback":
            receipt = _mapping(observations.get("receipt"))
            if capacity_rollback_receipt_mismatches(receipt, rig_count=len(expected_rig_ids)):
                return f"Capacity rollback receipt is incomplete: {ref}"
            preflight_ref = str(receipt["preflight_evidence_ref"])
            preflight_bytes, preflight_failure = _read_evidence_bytes(
                preflight_ref,
                {},
                evidence_root,
            )
            if preflight_failure or preflight_bytes is None:
                return preflight_failure or f"Capacity preflight is unreadable: {preflight_ref}"
            if hashlib.sha256(preflight_bytes).hexdigest() != str(
                receipt["preflight_evidence_sha256"]
            ).lower():
                return f"Capacity preflight SHA-256 mismatch: {preflight_ref}"
    return None


def evaluate_same_host_p0(
    report: Mapping[str, Any],
    *,
    verify_evidence: bool = False,
    evidence_root: Path | str | None = None,
    verify_environment: bool = False,
    _required_checks: tuple[str, ...] = REQUIRED_P0_CHECKS,
) -> dict[str, Any]:
    """Normalize evidence and pass only a complete seven-check live report."""

    setup = _mapping(report.get("experiment_setup"))
    checks = _mapping(report.get("checks"))
    missing_setup = []
    try:
        duration_valid = (
            not isinstance(setup.get("duration_seconds"), bool)
            and float(setup.get("duration_seconds") or 0) > 0
        )
    except (TypeError, ValueError):
        duration_valid = False
    if not duration_valid:
        missing_setup.append("duration_seconds")
    concurrent_load = setup.get("concurrent_load")
    if not (
        _non_empty_text(concurrent_load)
        or (isinstance(concurrent_load, Mapping) and bool(concurrent_load))
    ):
        missing_setup.append("concurrent_load")
    if not _non_empty_text(setup.get("sampling_method")):
        missing_setup.append("sampling_method")
    if not isinstance(setup.get("environment_fingerprint"), Mapping) or not setup.get(
        "environment_fingerprint"
    ):
        missing_setup.append("environment_fingerprint")
    if setup.get("admission_profile", "smoke") not in {
        "smoke",
        CAPACITY_ADMISSION_PROFILE,
    }:
        missing_setup.append("admission_profile")
    if setup.get("admission_profile") == CAPACITY_ADMISSION_PROFILE:
        if (
            not duration_valid
            or float(setup.get("duration_seconds"))
            < CAPACITY_QUALIFICATION_MIN_DURATION_SECONDS
        ):
            missing_setup.append("capacity_qualification_duration_seconds")
        baselines = _mapping(setup.get("single_rig_baselines"))
        baseline_refs = [
            str(_mapping(value).get("ref") or "")
            for value in baselines.values()
        ]
        baseline_digests = [
            str(_mapping(value).get("sha256") or "").lower()
            for value in baselines.values()
        ]
        if (
            len(baselines) < 2
            or len(baselines) != len(report.get("targets") or ())
            or any(
                not _non_empty_text(_mapping(value).get("ref"))
                or not _sha256_valid(_mapping(value).get("sha256"))
                for value in baselines.values()
            )
            or len(set(baseline_refs)) != len(baselines)
            or len(set(baseline_digests)) != len(baselines)
        ):
            missing_setup.append("single_rig_baselines")
    else:
        if not _non_empty_text(setup.get("single_rig_baseline_ref")):
            missing_setup.append("single_rig_baseline_ref")
        if not _sha256_valid(setup.get("single_rig_baseline_sha256")):
            missing_setup.append("single_rig_baseline_sha256")
    criteria = setup.get("usb_acceptance_criteria")
    if not isinstance(criteria, Mapping) or not criteria:
        missing_setup.append("usb_acceptance_criteria")
    failed_checks = [
        name
        for name in _required_checks
        if not isinstance(checks.get(name), Mapping)
        or checks[name].get("status") != "passed"
        or not _evidence_refs_valid(checks[name].get("evidence_refs"))
    ]
    evidence_failures: dict[str, str] = {}
    if verify_evidence:
        trusted_root = Path(evidence_root) if evidence_root is not None else None
        targets = report.get("targets")
        target_rows = list(targets) if isinstance(targets, (list, tuple)) else []
        expected_host_id = str(
            (_mapping(setup.get("environment_fingerprint"))).get("host_id") or ""
        )
        expected_rig_ids = {
            str(row.get("rig_id") or "")
            for row in target_rows
            if isinstance(row, Mapping)
        }
        expected_generations = {
            (
                str(row.get("rig_id") or ""),
                row.get("rig_generation"),
            )
            for row in target_rows
            if isinstance(row, Mapping)
        }
        expected_udids = {
            str(row.get("expected_usb_udid") or "")
            for row in target_rows
            if isinstance(row, Mapping)
        }
        if (
            len(target_rows) < 2
            or len(expected_rig_ids) != len(target_rows)
            or "" in expected_rig_ids
            or len(expected_generations) != len(target_rows)
            or any(
                isinstance(generation, bool)
                or not isinstance(generation, int)
                or generation <= 0
                for _rig_id, generation in expected_generations
            )
            or len(expected_udids) != len(target_rows)
            or "" in expected_udids
            or not expected_host_id
            or any(str(row.get("host_id") or "") != expected_host_id for row in target_rows)
        ):
            missing_setup.append("distinct_verified_targets")
        else:
            for name in _required_checks:
                check = checks.get(name)
                if not isinstance(check, Mapping) or name in failed_checks:
                    continue
                failure = _verify_check_artifacts(
                    name,
                    check,
                    expected_host_id=expected_host_id,
                    expected_rig_ids=expected_rig_ids,
                    expected_udids=expected_udids,
                    experiment_setup=setup,
                    evidence_root=trusted_root,
                    verify_environment=verify_environment,
                )
                if failure:
                    evidence_failures[name] = failure
                    failed_checks.append(name)
    evidence_ref = (
        str(report.get("evidence_ref")).strip()
        if _non_empty_text(report.get("evidence_ref"))
        else ""
    )
    passed = not missing_setup and not failed_checks and bool(evidence_ref)
    return {
        "schema_version": "same-host-p0.v1",
        "status": "passed" if passed else "failed",
        "scope": "live_same_host_multi_rig_p0",
        "evidence_ref": evidence_ref,
        "required_checks_passed": passed,
        "missing_setup": sorted(set(missing_setup)),
        "failed_checks": failed_checks,
        "evidence_failures": evidence_failures,
        "experiment_setup": deepcopy(setup),
        "targets": deepcopy(report.get("targets")),
        "checks": deepcopy(checks),
    }


def evaluate_same_host_p0_capacity_preflight(
    report: Mapping[str, Any],
    *,
    evidence_root: Path | str | None = None,
) -> dict[str, Any]:
    """Grade every live check that must precede the temporary Capacity trial."""

    return evaluate_same_host_p0(
        report,
        verify_evidence=True,
        evidence_root=evidence_root,
        _required_checks=PRE_CAPACITY_P0_CHECKS,
    )


def same_host_p0_proof_sha256(proof: Mapping[str, Any]) -> str:
    """Return the stable digest used to bind an operator review to one proof."""

    encoded = json.dumps(
        dict(proof),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evaluate_capacity_admission_review(
    proof: Mapping[str, Any],
    review: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Fail closed unless an operator review is bound to qualification evidence."""

    supplied = _mapping(review)
    failures: dict[str, str] = {}
    setup = _mapping(proof.get("experiment_setup"))
    if proof.get("status") != "passed" or proof.get("required_checks_passed") is not True:
        failures["evidence_status"] = "same-Host P0 evidence is not passed"
    if setup.get("admission_profile") != CAPACITY_ADMISSION_PROFILE:
        failures["admission_profile"] = (
            f"experiment_setup.admission_profile must be {CAPACITY_ADMISSION_PROFILE!r}"
        )
    if supplied.get("schema_version") != CAPACITY_ADMISSION_REVIEW_SCHEMA:
        failures["schema_version"] = (
            f"review schema must be {CAPACITY_ADMISSION_REVIEW_SCHEMA!r}"
        )
    if supplied.get("status") != "approved":
        failures["status"] = "review status must be 'approved'"
    if not _non_empty_text(supplied.get("reviewer")):
        failures["reviewer"] = "reviewer is required"
    if not _non_empty_text(supplied.get("note")):
        failures["note"] = "review note is required"
    reviewed_at = str(supplied.get("reviewed_at") or "")
    try:
        parsed_reviewed_at = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError:
        parsed_reviewed_at = None
    if parsed_reviewed_at is None or parsed_reviewed_at.tzinfo is None:
        failures["reviewed_at"] = "reviewed_at must be an ISO-8601 timestamp with timezone"
    evidence_ref = str(proof.get("evidence_ref") or "")
    if supplied.get("evidence_ref") != evidence_ref:
        failures["evidence_ref"] = "review evidence_ref does not match the graded proof"
    expected_sha256 = same_host_p0_proof_sha256(proof)
    if supplied.get("evidence_sha256") != expected_sha256:
        failures["evidence_sha256"] = "review digest does not match the graded proof"
    attestations = _mapping(supplied.get("attestations"))
    missing_attestations = [
        name
        for name in CAPACITY_ADMISSION_ATTESTATIONS
        if attestations.get(name) != "approved"
    ]
    if missing_attestations:
        failures["attestations"] = (
            "missing approved attestations: " + ", ".join(missing_attestations)
        )
    return {
        "schema_version": CAPACITY_ADMISSION_REVIEW_SCHEMA,
        "status": "approved" if not failures else "rejected",
        "failures": failures,
        "reviewer": supplied.get("reviewer"),
        "reviewed_at": supplied.get("reviewed_at"),
        "note": supplied.get("note"),
        "evidence_ref": evidence_ref,
        "evidence_sha256": expected_sha256,
        "attestations": {
            name: attestations.get(name)
            for name in CAPACITY_ADMISSION_ATTESTATIONS
        },
    }


def build_capacity_admission_review(
    report: Mapping[str, Any],
    *,
    reviewer: str,
    reviewed_at: str,
    note: str,
    evidence_root: Path | str | None = None,
) -> dict[str, Any]:
    """Build a review Artifact after a person accepts every admission item."""

    if evidence_root is None and report.get("schema_version") != "same-host-p0.v1":
        raise ValueError("an evaluated same-host-p0.v1 proof or evidence_root is required")
    proof = evaluate_same_host_p0(
        report,
        verify_evidence=evidence_root is not None,
        evidence_root=evidence_root,
    )
    candidate = {
        "schema_version": CAPACITY_ADMISSION_REVIEW_SCHEMA,
        "status": "approved",
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
        "note": note,
        "evidence_ref": proof.get("evidence_ref"),
        "evidence_sha256": same_host_p0_proof_sha256(proof),
        "attestations": {
            name: "approved"
            for name in CAPACITY_ADMISSION_ATTESTATIONS
        },
    }
    evaluated = evaluate_capacity_admission_review(proof, candidate)
    if evaluated["status"] != "approved":
        raise ValueError(
            "capacity admission review rejected: "
            + "; ".join(evaluated["failures"].values())
        )
    return candidate
