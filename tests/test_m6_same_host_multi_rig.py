from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest

from autophone.control import ControlPlane, ControlStateError, PostgresControlStore
from autophone.control.host_coordination import HostCoordination
from autophone.control.evidence import (
    CAPACITY_ADMISSION_PROFILE,
    REQUIRED_P0_CHECKS,
    build_capacity_admission_review,
    capacity_targets_registered,
    evaluate_capacity_admission_review,
    evaluate_same_host_p0,
)
from autophone.control.usb_baseline import compare_dual_rig_observation
from autophone.device import VPhoneDeviceSession
from autophone.rig.agent import RigAgent
from autophone.rig.target import DeviceTarget
from autophone.rig.transport import InProcessAgentTransport


@pytest.fixture
def store(tmp_path: Path):
    dsn = os.environ.get("AUTOPHONE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AUTOPHONE_TEST_POSTGRES_DSN is not configured")
    instance = PostgresControlStore(
        dsn,
        schema=f"test_{uuid.uuid4().hex[:16]}",
        evidence_root=tmp_path,
    )
    instance.initialize()
    try:
        yield instance
    finally:
        instance._drop_schema_for_test()


def _target(tmp_path: Path, rig_id: str, offset: int) -> DeviceTarget:
    root = (tmp_path / rig_id).resolve()
    return DeviceTarget(
        host_id="host-a",
        rig_id=rig_id,
        rig_generation=1,
        cli_dir=(root / "cli"),
        vm_dir=(root / "vm"),
        socket=(root / "vm" / "vphone.sock"),
        expected_usb_udid=f"00000000-000000000000000{offset}",
        ssh_host="127.0.0.1",
        ssh_port=2200 + offset,
        frida_host="127.0.0.1",
        frida_port=27040 + offset,
        frida_guest_port=27042,
        log_dir=(root / "logs"),
        temp_dir=(root / "tmp"),
        package_cache=(root / "packages"),
    )


def _plan(name: str, rig_id: str) -> dict:
    return {
        "assignment_id": name,
        "kind": "run",
        "requires_rig": True,
        "priority": "normal",
        "target_rig_id": rig_id,
        "required_capabilities": ["vm_uds", "frida"],
        "desired_state": {},
        "run_spec": {
            "run_id": f"run-{name}",
            "type": "operation",
            "app": "demo",
        },
    }


def _passed_p0_evidence(
    output_dir: Path | None = None,
    *,
    targets: list[dict] | None = None,
    admission_profile: str | None = None,
    observation_predicate_version: int | None = None,
    strip_predicate_record: bool = False,
    duration: int = 3600,
) -> dict:
    if targets is None:
        targets = [
            {
                "host_id": "host-a",
                "rig_id": "rig-a",
                "rig_generation": 1,
                "expected_usb_udid": "udid-a",
            },
            {
                "host_id": "host-a",
                "rig_id": "rig-b",
                "rig_generation": 1,
                "expected_usb_udid": "udid-b",
            },
        ]
    expected_usb_udids = sorted(str(target["expected_usb_udid"]) for target in targets)
    criteria = {
        "min_device_operation_count": 1,
        "max_host_crash_rate_increase_per_vm_hour": 0,
        "max_usb_exception_rate_increase_per_vm_hour": {},
    }
    baseline = {
        "schema_version": "single-rig-usb-baseline.v1",
        "environment_fingerprint": {
            "host_id": "host-a",
            "macos_build": "test",
            "vphone_cli_commit": "abc",
        },
        "window_count": 2,
        "exposure": {"vm_hours": 2.0},
        "observation_refs": [
            "artifact://m0/window-1.json",
            "artifact://m0/window-2.json",
        ],
        "rates_per_vm_hour": {"host_crash": 0.0, "usb_exceptions": {}},
    }
    baseline_encoded = (json.dumps(baseline, sort_keys=True) + "\n").encode()
    baseline_ref = "artifact://m0/single-rig-baseline.json"
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        baseline_path = output_dir / "single-rig-baseline.json"
        baseline_path.write_bytes(baseline_encoded)
        baseline_ref = baseline_path.as_uri()
    qualification_baselines: dict[str, dict[str, str]] = {}
    qualification_baseline_values: dict[str, dict] = {}
    if admission_profile == CAPACITY_ADMISSION_PROFILE:
        for index, target in enumerate(targets, start=1):
            rig_id = str(target["rig_id"])
            value = json.loads(json.dumps(baseline))
            value["environment_fingerprint"].update({
                "vm_image_ref": f"{rig_id}@m0",
                "vm_config_sha256": str(index) * 64,
            })
            encoded = (json.dumps(value, sort_keys=True) + "\n").encode()
            ref = f"artifact://m0/{rig_id}-single-rig-baseline.json"
            if output_dir is not None:
                path = output_dir / f"{rig_id}-single-rig-baseline.json"
                path.write_bytes(encoded)
                ref = path.as_uri()
            qualification_baselines[rig_id] = {
                "ref": ref,
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
            qualification_baseline_values[rig_id] = value
    observation = {
        "schema_version": "same-host-dual-rig-usb-observation.v1",
        "environment_fingerprint": {
            "host_id": "host-a", "macos_build": "test", "vphone_cli_commit": "abc",
        },
        "window": {
            "started_at": "2026-08-19T00:00:00+00:00",
            "ended_at": "2026-08-19T01:00:00+00:00",
            "duration_seconds": 3600,
        },
        "exposure": {
            "vm_hours": len(targets),
            "device_operation_count": len(targets),
            "per_rig": {
                str(target["rig_id"]): {"active_seconds": 1800, "idle_seconds": 1800, "guest_probe_count": 1}
                for target in targets
            },
        },
        "outcomes": {"host_crash_count": 0, "usb_exception_counts": {}},
    }
    if observation_predicate_version is not None:
        observation["vm_fatal_predicate_version"] = observation_predicate_version

    def stored(value: dict) -> dict:
        # strip_predicate_record 模拟判据版本字段引入前写出的比较回执。
        if strip_predicate_record:
            value = {key: item for key, item in value.items() if key != "vm_fatal_predicate"}
        return value

    comparison = stored(compare_dual_rig_observation(
        baseline,
        observation,
        declared_duration_seconds=duration,
        acceptance_criteria=criteria,
    ))
    checks = {}
    preflight_ref = "artifact://p0/preflight.json"
    preflight_sha256 = "0" * 64
    if output_dir is not None:
        preflight_path = output_dir / "capacity-preflight.json"
        preflight_encoded = b'{"status":"passed"}\n'
        preflight_path.write_bytes(preflight_encoded)
        preflight_ref = preflight_path.as_uri()
        preflight_sha256 = hashlib.sha256(preflight_encoded).hexdigest()
    for name in REQUIRED_P0_CHECKS:
        if output_dir is None:
            checks[name] = {
                "status": "passed",
                "evidence_refs": [f"artifact://p0/{name}.json"],
            }
            continue
        path = output_dir / f"{name}.json"
        observations = {"test": "verified"}
        if name == "crash_usb_baseline_comparison":
            if admission_profile == CAPACITY_ADMISSION_PROFILE:
                observations = {
                    "baselines": qualification_baselines,
                    "observation": observation,
                    "comparisons": {
                        rig_id: stored(compare_dual_rig_observation(
                            value,
                            observation,
                            declared_duration_seconds=duration,
                            acceptance_criteria=criteria,
                        ))
                        for rig_id, value in qualification_baseline_values.items()
                    },
                }
            else:
                observations = {
                    "baseline_ref": baseline_ref,
                    "baseline_sha256": hashlib.sha256(baseline_encoded).hexdigest(),
                    "observation": observation,
                    "comparison": comparison,
                }
        elif name == "capacity_rollback":
            observations = {"receipt": {
                "previous_capacity": 1,
                "trial_capacity": len(targets),
                "rollback_capacity": 1,
                "configured_capacity_unchanged": True,
                "simultaneous_assignment_count": len(targets),
                "existing_assignments_preserved": True,
                "new_assignment_wait_reason": "host_capacity",
                "post_rollback_assignment_resumed": True,
                "final_active_rigs": 0,
                "final_effective_capacity": 1,
                "preflight_evidence_ref": preflight_ref,
                "preflight_evidence_sha256": preflight_sha256,
                "events": ["capacity_trial_started", "capacity_trial_rolled_back"],
            }}
        encoded = (json.dumps({
            "schema_version": "same-host-p0-check.v1",
            "check": name,
            "status": "passed",
            "started_at": "2026-08-19T00:00:00+00:00",
            "ended_at": "2026-08-19T01:00:00+00:00",
            "host_id": "host-a",
            "rig_ids": [str(target["rig_id"]) for target in targets],
            "expected_usb_udids": expected_usb_udids,
            "observations": observations,
        }, sort_keys=True) + "\n").encode()
        path.write_bytes(encoded)
        checks[name] = {
            "status": "passed",
            "evidence_refs": [path.as_uri()],
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "evidence_blobs": {
                path.as_uri(): base64.b64encode(encoded).decode("ascii"),
            },
        }
    experiment_setup = {
        "duration_seconds": duration,
        "concurrent_load": "2 rigs, 1 screenshot and 1 tap per 5 seconds",
        "sampling_method": "unified log plus per-Rig Agent journal",
        "environment_fingerprint": {
            "host_id": "host-a", "macos_build": "test", "vphone_cli_commit": "abc",
        },
        "single_rig_baseline_ref": baseline_ref,
        "single_rig_baseline_sha256": hashlib.sha256(baseline_encoded).hexdigest(),
        "usb_acceptance_criteria": criteria,
    }
    if admission_profile is not None:
        experiment_setup["admission_profile"] = admission_profile
    if admission_profile == CAPACITY_ADMISSION_PROFILE:
        experiment_setup["single_rig_baselines"] = qualification_baselines
    report = {
        "evidence_ref": (
            (output_dir / "live-report.json").as_uri()
            if output_dir is not None
            else "artifact://p0/live-report.json"
        ),
        "experiment_setup": experiment_setup,
        "targets": targets,
        "checks": checks,
    }
    return evaluate_same_host_p0(
        report,
        verify_evidence=output_dir is not None,
        evidence_root=output_dir,
    )


def _approved_capacity_review(proof: dict) -> dict:
    return build_capacity_admission_review(
        proof,
        reviewer="operator-a",
        reviewed_at="2026-08-20T02:00:00+08:00",
        note="reviewed sustained baselines, long window, and host diagnostics",
    )


def test_p0_evidence_requires_fixed_setup_and_all_live_checks() -> None:
    incomplete = evaluate_same_host_p0({
        "evidence_ref": "artifact://p0/incomplete.json",
        "experiment_setup": {"duration_seconds": 300},
        "checks": {},
    })

    assert incomplete["status"] == "failed"
    assert "single_rig_baseline_ref" in incomplete["missing_setup"]
    assert set(incomplete["failed_checks"]) == set(REQUIRED_P0_CHECKS)
    assert _passed_p0_evidence()["required_checks_passed"] is True


def test_p0_evidence_rejects_malformed_setup_and_string_evidence_refs() -> None:
    report = _passed_p0_evidence()
    report["experiment_setup"]["environment_fingerprint"] = "host-a"
    report["experiment_setup"]["admission_profile"] = "unknown"
    report["checks"]["concurrent_vm_start"]["evidence_refs"] = "artifact://fake"

    evaluated = evaluate_same_host_p0(report)

    assert evaluated["status"] == "failed"
    assert "environment_fingerprint" in evaluated["missing_setup"]
    assert "admission_profile" in evaluated["missing_setup"]
    assert "concurrent_vm_start" in evaluated["failed_checks"]


@pytest.mark.parametrize("profile", [None, CAPACITY_ADMISSION_PROFILE])
def test_pre_versioning_usb_comparison_receipts_remain_verifiable(
    tmp_path: Path, profile: str | None,
) -> None:
    # 2026-10-10 前的报告：观测无 vm_fatal_predicate_version，比较回执无 vm_fatal_predicate。
    legacy = _passed_p0_evidence(
        tmp_path / "legacy", admission_profile=profile, strip_predicate_record=True,
    )
    assert legacy["evidence_failures"] == {}
    assert legacy["status"] == "passed"

    current = _passed_p0_evidence(
        tmp_path / "current", admission_profile=profile, observation_predicate_version=2,
    )
    assert current["evidence_failures"] == {}
    assert current["status"] == "passed"


def test_v2_usb_comparison_receipt_must_carry_the_predicate_record(tmp_path: Path) -> None:
    evaluated = _passed_p0_evidence(
        tmp_path,
        admission_profile=CAPACITY_ADMISSION_PROFILE,
        observation_predicate_version=2,
        strip_predicate_record=True,
    )

    assert evaluated["status"] == "failed"
    assert "receipt mismatch" in evaluated["evidence_failures"][
        "crash_usb_baseline_comparison"
    ]


def test_capacity_admission_review_is_bound_to_qualification_evidence() -> None:
    smoke = _passed_p0_evidence()
    with pytest.raises(ValueError, match="admission_profile"):
        _approved_capacity_review(smoke)

    proof = _passed_p0_evidence(admission_profile=CAPACITY_ADMISSION_PROFILE)
    review = _approved_capacity_review(proof)
    assert evaluate_capacity_admission_review(proof, review)["status"] == "approved"

    mutated = json.loads(json.dumps(proof))
    mutated["experiment_setup"]["duration_seconds"] += 1
    rejected = evaluate_capacity_admission_review(mutated, review)
    assert rejected["status"] == "rejected"
    assert "evidence_sha256" in rejected["failures"]


def test_capacity_qualification_rejects_short_dual_rig_window() -> None:
    proof = _passed_p0_evidence(admission_profile=CAPACITY_ADMISSION_PROFILE)
    proof["experiment_setup"]["duration_seconds"] = 3599

    evaluated = evaluate_same_host_p0(proof)

    assert evaluated["status"] == "failed"
    assert (
        "capacity_qualification_duration_seconds"
        in evaluated["missing_setup"]
    )


def test_capacity_targets_must_be_registered_with_the_same_generation() -> None:
    reported = _passed_p0_evidence()["targets"]
    registered = json.loads(json.dumps(reported))
    assert capacity_targets_registered(reported, registered, host_id="host-a")

    registered[0]["rig_generation"] += 1
    assert not capacity_targets_registered(reported, registered, host_id="host-a")
    registered = json.loads(json.dumps(reported))
    registered.append({"host_id": "host-a", "rig_id": "rig-c", "rig_generation": 1, "expected_usb_udid": "udid-c"})
    assert capacity_targets_registered(reported, registered, host_id="host-a")


def test_qualification_requires_two_re_readable_target_baselines(
    tmp_path: Path,
) -> None:
    output = tmp_path / "p0"
    proof = _passed_p0_evidence(
        output,
        admission_profile=CAPACITY_ADMISSION_PROFILE,
    )
    assert proof["status"] == "passed"

    secondary = Path(
        proof["experiment_setup"]["single_rig_baselines"]["rig-b"]["ref"].removeprefix(
            "file://"
        )
    )
    secondary.unlink()
    evaluated = evaluate_same_host_p0(
        proof,
        verify_evidence=True,
        evidence_root=output,
    )

    assert evaluated["status"] == "failed"
    assert "cannot resolve evidence" in evaluated["evidence_failures"][
        "crash_usb_baseline_comparison"
    ]


def test_qualification_rejects_duplicate_baseline_content(tmp_path: Path) -> None:
    output = tmp_path / "p0"
    proof = _passed_p0_evidence(
        output,
        admission_profile=CAPACITY_ADMISSION_PROFILE,
    )
    proof["experiment_setup"]["single_rig_baselines"]["rig-b"].update(
        proof["experiment_setup"]["single_rig_baselines"]["rig-a"]
    )

    evaluated = evaluate_same_host_p0(proof)

    assert evaluated["status"] == "failed"
    assert "single_rig_baselines" in evaluated["missing_setup"]


def test_capacity_grade_evidence_reads_and_hashes_each_check_artifact() -> None:
    unverified = _passed_p0_evidence()

    evaluated = evaluate_same_host_p0(unverified, verify_evidence=True)

    assert evaluated["status"] == "failed"
    assert set(evaluated["evidence_failures"]) == set(REQUIRED_P0_CHECKS)


def test_capacity_grade_local_evidence_cannot_escape_trusted_root(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    outside = tmp_path / "outside"
    trusted.mkdir()
    report = _passed_p0_evidence(outside)
    for check in report["checks"].values():
        check.pop("evidence_blobs", None)

    evaluated = evaluate_same_host_p0(
        report,
        verify_evidence=True,
        evidence_root=trusted,
    )

    assert evaluated["status"] == "failed"
    assert set(evaluated["evidence_failures"]) == set(REQUIRED_P0_CHECKS)
    assert all(
        "outside the trusted root" in detail
        for detail in evaluated["evidence_failures"].values()
    )


def test_embedded_evidence_cannot_stand_in_for_the_files_a_reviewer_reads(
    tmp_path: Path,
) -> None:
    output = tmp_path / "p0"
    report = _passed_p0_evidence(output)
    assert report["status"] == "passed"
    artifact = output / "storage_isolation.json"

    artifact.write_bytes(b'{"tampered": true}\n')
    tampered = evaluate_same_host_p0(report, verify_evidence=True, evidence_root=output)

    assert tampered["status"] == "failed"
    assert (
        "does not match the file on disk"
        in tampered["evidence_failures"]["storage_isolation"]
    )

    artifact.unlink()
    deleted = evaluate_same_host_p0(report, verify_evidence=True, evidence_root=output)

    assert deleted["status"] == "failed"
    assert "cannot resolve evidence" in deleted["evidence_failures"]["storage_isolation"]


def test_remote_bundle_cannot_pass_without_a_re_readable_baseline(tmp_path: Path) -> None:
    report = _passed_p0_evidence(tmp_path / "p0")

    evaluated = evaluate_same_host_p0(report, verify_evidence=True, evidence_root=None)

    assert evaluated["status"] == "failed"
    assert "crash_usb_baseline_comparison" in evaluated["evidence_failures"]
    assert "baseline" in evaluated["evidence_failures"][
        "crash_usb_baseline_comparison"
    ].lower()


def _rewrite_check_artifact(report: dict, name: str, mutate) -> None:
    check = report["checks"][name]
    path = Path(check["evidence_refs"][0].removeprefix("file://"))
    artifact = json.loads(path.read_text(encoding="utf-8"))
    mutate(artifact)
    encoded = (json.dumps(artifact, sort_keys=True) + "\n").encode()
    path.write_bytes(encoded)
    check["sha256"] = hashlib.sha256(encoded).hexdigest()
    check["evidence_blobs"] = {
        path.as_uri(): base64.b64encode(encoded).decode("ascii")
    }


def test_qualification_rejects_insufficient_baseline_exposure(tmp_path: Path) -> None:
    output = tmp_path / "p0"
    proof = _passed_p0_evidence(
        output,
        admission_profile=CAPACITY_ADMISSION_PROFILE,
    )
    identity = proof["experiment_setup"]["single_rig_baselines"]["rig-a"]
    baseline_path = Path(identity["ref"].removeprefix("file://"))
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline["exposure"]["vm_hours"] = 1.99
    encoded = (json.dumps(baseline, sort_keys=True) + "\n").encode()
    baseline_path.write_bytes(encoded)
    identity["sha256"] = hashlib.sha256(encoded).hexdigest()
    _rewrite_check_artifact(
        proof,
        "crash_usb_baseline_comparison",
        lambda artifact: artifact["observations"]["baselines"]["rig-a"].update(
            {"sha256": identity["sha256"]}
        ),
    )

    evaluated = evaluate_same_host_p0(
        proof,
        verify_evidence=True,
        evidence_root=output,
    )

    assert evaluated["status"] == "failed"
    assert "qualification baseline is incomplete" in evaluated[
        "evidence_failures"
    ]["crash_usb_baseline_comparison"]


def test_semantic_gate_rejects_forged_capacity_receipt_even_with_matching_hash(
    tmp_path: Path,
) -> None:
    output = tmp_path / "p0"
    report = _passed_p0_evidence(output)
    _rewrite_check_artifact(
        report,
        "capacity_rollback",
        lambda artifact: artifact["observations"]["receipt"].update({
            "new_assignment_wait_reason": "rig_busy",
        }),
    )

    evaluated = evaluate_same_host_p0(report, verify_evidence=True, evidence_root=output)

    assert evaluated["status"] == "failed"
    assert "receipt is incomplete" in evaluated["evidence_failures"]["capacity_rollback"]


def test_semantic_gate_rejects_mutated_capacity_preflight(tmp_path: Path) -> None:
    output = tmp_path / "p0"
    report = _passed_p0_evidence(output)
    receipt = report["checks"]["capacity_rollback"]
    artifact_path = Path(receipt["evidence_refs"][0].removeprefix("file://"))
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    preflight_path = Path(
        artifact["observations"]["receipt"]["preflight_evidence_ref"].removeprefix(
            "file://"
        )
    )
    preflight_path.write_text('{"tampered":true}\n', encoding="utf-8")

    evaluated = evaluate_same_host_p0(report, verify_evidence=True, evidence_root=output)

    assert evaluated["status"] == "failed"
    assert "preflight SHA-256 mismatch" in evaluated["evidence_failures"][
        "capacity_rollback"
    ]


def test_semantic_gate_recomputes_usb_comparison_instead_of_trusting_receipt(
    tmp_path: Path,
) -> None:
    output = tmp_path / "p0"
    report = _passed_p0_evidence(output)
    _rewrite_check_artifact(
        report,
        "crash_usb_baseline_comparison",
        lambda artifact: artifact["observations"]["observation"]["outcomes"].update({
            "usb_exception_counts": {"Terminated": 1},
        }),
    )

    evaluated = evaluate_same_host_p0(report, verify_evidence=True, evidence_root=output)

    assert evaluated["status"] == "failed"
    assert "did not pass" in evaluated["evidence_failures"][
        "crash_usb_baseline_comparison"
    ]


def test_target_resources_are_unique_within_one_host(store, tmp_path: Path) -> None:
    store.register_host("host-a", max_active_rigs=1)
    control = ControlPlane(store)
    hosts: HostCoordination = control
    first = _target(tmp_path, "rig-a", 1)
    registration = {
        "host_id": first.host_id,
        "rig_id": first.rig_id,
        "generation": first.rig_generation,
        "capability_support": ["vm_uds"],
        "target": first.public_dict(),
    }
    control.register_agent(**registration)
    control.register_agent(**registration)
    changed_same_generation = DeviceTarget.from_mapping({
        **first.public_dict(),
        "ssh_port": first.ssh_port + 10,
    })
    with pytest.raises(ControlStateError, match="cannot change without increasing"):
        control.register_agent(
            host_id=changed_same_generation.host_id,
            rig_id=changed_same_generation.rig_id,
            generation=changed_same_generation.rig_generation,
            capability_support=["vm_uds"],
            target=changed_same_generation.public_dict(),
        )
    conflicting = DeviceTarget.from_mapping(
        {
            **_target(tmp_path, "rig-b", 2).public_dict(),
            "vm_dir": str(first.vm_dir),
        }
    )

    with pytest.raises(ControlStateError, match="resource conflicts"):
        control.register_agent(
            host_id=conflicting.host_id,
            rig_id=conflicting.rig_id,
            generation=conflicting.rig_generation,
            capability_support=["vm_uds"],
            target=conflicting.public_dict(),
        )
    snapshot = hosts.get_host("host-a")
    assert [item["rig_id"] for item in snapshot["targets"]] == ["rig-a"]


def test_capacity_change_is_evidence_gated_and_agent_reconnect_preserves_it(
    store,
    tmp_path: Path,
) -> None:
    store.register_host("host-a", max_active_rigs=1)
    control = ControlPlane(store)
    hosts: HostCoordination = control
    registered_targets = [
        _target(tmp_path, rig_id, index)
        for index, rig_id in enumerate(("rig-a", "rig-b"), start=1)
    ]
    for target in registered_targets:
        control.register_agent(
            host_id=target.host_id,
            rig_id=target.rig_id,
            generation=target.rig_generation,
            capability_support=["vm_uds", "frida"],
            target=target.public_dict(),
        )
    with pytest.raises(ControlStateError, match="requires passed"):
        hosts.change_host_capacity(
            "host-a", 2, actor="operator", reason="enable same-Host concurrency"
        )
    with pytest.raises(ControlStateError, match="requires passed"):
        hosts.change_host_capacity(
            "host-a",
            2,
            actor="operator",
            reason="forged summary",
            evidence={
                "status": "passed",
                "scope": "live_same_host_multi_rig_p0",
                "required_checks_passed": True,
                "evidence_ref": "artifact://p0/forged.json",
            },
        )

    smoke = _passed_p0_evidence(tmp_path / "smoke")
    with pytest.raises(ControlStateError, match="qualification review"):
        hosts.change_host_capacity(
            "host-a",
            2,
            actor="operator",
            reason="short smoke must not enable configured concurrency",
            evidence=smoke,
            approval={},
        )

    proof = _passed_p0_evidence(
        tmp_path / "p0",
        targets=[target.public_dict() for target in registered_targets],
        admission_profile=CAPACITY_ADMISSION_PROFILE,
    )
    stale = json.loads(json.dumps(proof))
    stale["targets"][0]["rig_generation"] += 1
    with pytest.raises(ControlStateError, match="not registered"):
        hosts.change_host_capacity(
            "host-a",
            2,
            actor="operator",
            reason="stale Rig generation",
            evidence=stale,
            approval=_approved_capacity_review(stale),
        )

    approval = _approved_capacity_review(proof)
    changed = hosts.change_host_capacity(
        "host-a",
        2,
        actor="operator",
        reason="P0 evidence approved",
        evidence=proof,
        approval=approval,
    )
    assert changed["host"]["max_active_rigs"] == 2
    assert changed["event"]["details"]["admission_review"]["status"] == "approved"

    target = registered_targets[0]
    next_generation = DeviceTarget.from_mapping({
        **target.public_dict(),
        "rig_generation": target.rig_generation + 1,
    })
    with pytest.raises(ControlStateError, match="lowered to 1"):
        control.register_agent(
            host_id=next_generation.host_id,
            rig_id=next_generation.rig_id,
            generation=next_generation.rig_generation,
            capability_support=["vm_uds", "frida"],
            target=next_generation.public_dict(),
        )

    control.register_agent(
        host_id=target.host_id,
        rig_id=target.rig_id,
        generation=target.rig_generation,
        capability_support=["vm_uds"],
        target=target.public_dict(),
    )
    assert hosts.get_host("host-a")["host"]["max_active_rigs"] == 2

    rolled_back = hosts.change_host_capacity(
        "host-a", 1, actor="operator", reason="rollback after anomaly"
    )
    assert rolled_back["host"]["max_active_rigs"] == 1
    events = hosts.get_host("host-a")["events"]
    assert [event["details"]["current"] for event in events[:2]] == [1, 2]


def test_cli_capacity_change_grades_local_evidence_with_explicit_evidence_root(
    store,
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """`control host capacity` must hand `--evidence-root` to the store it builds.

    The CLI store otherwise only reads `AUTOPHONE_EVIDENCE_ROOT`; without it the
    file:// qualification baselines cannot be re-read and Capacity stays at 1
    (same wiring gap as the BL-083 2026-10-11 capacity drill).
    """
    from autophone import cli
    from autophone.application.control_plane import reset_control_plane_cache

    store.register_host("host-a", max_active_rigs=1)
    control = ControlPlane(store)
    registered_targets = [
        _target(tmp_path, rig_id, index)
        for index, rig_id in enumerate(("rig-a", "rig-b"), start=1)
    ]
    for target in registered_targets:
        control.register_agent(
            host_id=target.host_id,
            rig_id=target.rig_id,
            generation=target.rig_generation,
            capability_support=["vm_uds", "frida"],
            target=target.public_dict(),
        )
    evidence_root = tmp_path / "run"
    proof = _passed_p0_evidence(
        evidence_root / "m6",
        targets=[target.public_dict() for target in registered_targets],
        admission_profile=CAPACITY_ADMISSION_PROFILE,
    )
    assert proof["status"] == "passed"
    proof_path = tmp_path / "proof.json"
    proof_path.write_text(json.dumps(proof), encoding="utf-8")
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(json.dumps(_approved_capacity_review(proof)), encoding="utf-8")
    monkeypatch.setenv("AUTOPHONE_DATABASE_URL", store.dsn)
    monkeypatch.setenv("AUTOPHONE_DATABASE_SCHEMA", store.schema)
    monkeypatch.delenv("AUTOPHONE_EVIDENCE_ROOT", raising=False)
    reset_control_plane_cache()
    argv = [
        "control", "host", "capacity", "host-a", "2",
        "--actor", "operator", "--reason", "P0 evidence approved",
        "--evidence", str(proof_path), "--approval", str(approval_path),
    ]
    try:
        without_root = cli.build_parser().parse_args(argv)
        with pytest.raises(ControlStateError, match="requires passed"):
            without_root.func(without_root)
        assert store.host_snapshot("host-a")["host"]["max_active_rigs"] == 1

        with_root = cli.build_parser().parse_args(
            [*argv, "--evidence-root", str(evidence_root)]
        )
        assert with_root.func(with_root) == 0
    finally:
        reset_control_plane_cache()

    printed = json.loads(capsys.readouterr().out)
    assert printed["host"]["max_active_rigs"] == 2
    assert store.host_snapshot("host-a")["host"]["max_active_rigs"] == 2


def test_capacity_above_one_always_requires_passed_evidence(
    store,
) -> None:
    store.register_host("host-a", max_active_rigs=1)
    control = ControlPlane(store)
    hosts: HostCoordination = control

    with pytest.raises(ControlStateError, match="requires passed"):
        hosts.change_host_capacity(
            "host-a",
            3,
            actor="operator",
            reason="no evidence",
        )


def test_existing_capacity_above_two_can_be_reduced_without_evidence(store) -> None:
    store.register_host("host-a", max_active_rigs=4)
    control = ControlPlane(store)
    hosts: HostCoordination = control

    changed = hosts.change_host_capacity(
        "host-a",
        3,
        actor="operator",
        reason="reduce unsupported legacy capacity",
    )

    assert changed["host"]["max_active_rigs"] == 3


def test_capacity_trial_is_temporary_and_rolls_scheduler_back_to_one(
    store,
    tmp_path: Path,
) -> None:
    store.register_host("host-a", max_active_rigs=1)
    control = ControlPlane(store)
    hosts: HostCoordination = control
    targets = []
    for index, rig_id in enumerate(("rig-a", "rig-b"), start=1):
        target = _target(tmp_path, rig_id, index)
        targets.append({
            "host_id": target.host_id,
            "rig_id": target.rig_id,
            "rig_generation": target.rig_generation,
            "expected_usb_udid": target.expected_usb_udid,
        })
        control.register_agent(
            host_id=target.host_id,
            rig_id=target.rig_id,
            generation=target.rig_generation,
            capability_support=["vm_uds", "frida"],
            target=target.public_dict(),
        )

    started = hosts.begin_capacity_trial(
        "host-a",
        actor="p0",
        reason="capacity drill",
        preflight_evidence=_passed_p0_evidence(tmp_path / "p0", targets=targets),
        ttl_seconds=60,
    )
    assert hosts.get_host("host-a")["effective_capacity"] == 2
    assert hosts.get_host("host-a")["host"]["max_active_rigs"] == 1

    store.submit_assignment(_plan("trial-a", "rig-a"), "trial-a")
    store.submit_assignment(_plan("trial-b", "rig-b"), "trial-b")
    assert store.dispatch()["status"] == "assigned"
    assert store.dispatch()["status"] == "assigned"

    hosts.finish_capacity_trial(
        started["trial"]["trial_id"],
        actor="p0",
        result={"passed": True},
    )
    snapshot = hosts.get_host("host-a")
    assert snapshot["effective_capacity"] == 1
    assert snapshot["active_rigs"] == 2
    store.submit_assignment(_plan("after", "rig-b"), "after")
    waiting = store.dispatch()
    assert waiting["assignment_id"] == "after"
    assert waiting["wait_reason"] == "host_capacity"
    assert [event["kind"] for event in snapshot["events"][:2]] == [
        "capacity_trial_rolled_back",
        "capacity_trial_started",
    ]


def test_maintenance_is_exclusive_and_blocks_scheduler(store, tmp_path: Path) -> None:
    store.register_host("host-a", max_active_rigs=1)
    control = ControlPlane(store)
    hosts: HostCoordination = control
    target = _target(tmp_path, "rig-a", 1)
    control.register_agent(
        host_id=target.host_id,
        rig_id=target.rig_id,
        generation=target.rig_generation,
        capability_support=["vm_uds", "frida"],
        target=target.public_dict(),
    )
    receipt = hosts.begin_host_maintenance(
        "host-a", intent="restart_amfidont", owner="operator-a"
    )
    assert hosts.get_host("host-a")["maintenance"]["intent"] == "restart_amfidont"
    with pytest.raises(ControlStateError, match="already active"):
        hosts.begin_host_maintenance(
            "host-a", intent="usb_recovery", owner="operator-b"
        )

    store.submit_assignment(_plan("one", "rig-a"), "one")
    queued = store.dispatch()
    assert queued["status"] == "queued"
    assert queued["wait_reason"] == "host_maintenance"

    hosts.finish_host_maintenance(
        receipt["maintenance_id"], owner="operator-a", result={"passed": True}
    )
    assigned = store.dispatch()
    assert assigned["status"] == "assigned"
    with pytest.raises(ControlStateError, match="zero active"):
        hosts.begin_host_maintenance(
            "host-a", intent="usb_recovery", owner="operator-b"
        )


def test_two_rig_execution_fault_and_artifact_isolation(store, tmp_path: Path, monkeypatch) -> None:
    # Capacity is raised only *after* both Rig identities exist: while a Host
    # sits above Capacity 1 the Control Plane refuses to add, re-generation or
    # migrate a Rig, so registering at 2 up front is no longer a legal setup.
    # A trial is how a Host reaches effective Capacity 2 without qualification
    # evidence, same as test_capacity_trial_is_temporary_and_rolls_scheduler_back_to_one.
    store.register_host("host-a", max_active_rigs=1)
    control = ControlPlane(store)
    hosts: HostCoordination = control
    targets = {
        "rig-a": _target(tmp_path, "rig-a", 1),
        "rig-b": _target(tmp_path, "rig-b", 2),
    }
    request_log: dict[str, list[dict]] = {rig_id: [] for rig_id in targets}
    frida_log = []

    def fake_frida(*, host, app_name, timeout_s):
        frida_log.append((host, app_name, timeout_s))
        return {"available": True, "host": host}

    monkeypatch.setattr("autophone.device.frida.probe_contract", fake_frida)

    class Executor:
        def __init__(self, status: str):
            self.status = status

        def execute(self, assignment, context):
            rig_id = context.target.rig_id

            def request(message, **kwargs):
                request_log[rig_id].append({"message": dict(message), **kwargs})
                if message["t"] == "screenshot":
                    return {"ok": True, "image": base64.b64encode(rig_id.encode()).decode()}, None
                if message["t"] == "file_get":
                    Path(message["save"]).write_bytes(f"download:{rig_id}".encode())
                return {"ok": True}, None

            device = VPhoneDeviceSession(
                context.target,
                context.write_fence,
                request_fn=request,
                shell_fn=lambda *a, **k: None,
            )
            device.tap(10, 20)
            assert device.screenshot() == rig_id.encode()
            upload = context.target.temp_dir / "upload.txt"
            upload.write_text(f"upload:{rig_id}", encoding="utf-8")
            device.put_file(upload, f"/tmp/{rig_id}.txt")
            downloaded = device.get_file(
                f"/tmp/{rig_id}.json",
                context.target.temp_dir / "download.json",
            )
            assert downloaded.read_text(encoding="utf-8") == f"download:{rig_id}"
            assert device.frida_probe("Demo")["available"] is True

            artifact = context.target.log_dir / "result.json"
            artifact.write_text(json.dumps({"rig_id": rig_id}), encoding="utf-8")
            content = artifact.read_bytes()
            return {
                "status": self.status,
                "cleanup_receipt": {
                    "artifacts_sealed": True,
                    "device_access_released": True,
                },
                "rig_disposition": (
                    "needs_inspection" if self.status == "error" else "reusable"
                ),
                "artifact_refs": [{
                    "artifact_id": f"artifact-{rig_id}",
                    "owner_kind": "assignment_attempt",
                    "owner_id": (
                        f"{assignment['assignment_id']}:"
                        f"{assignment['attempt']['attempt_no']}"
                    ),
                    "logical_name": "result.json",
                    "uri": str(artifact),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                    "governance": "local_only",
                    "seal_status": "sealed",
                }],
            }

    agents = {}
    for rig_id, status in (("rig-a", "error"), ("rig-b", "passed")):
        transport = InProcessAgentTransport(control)
        agent = RigAgent(
            targets[rig_id],
            transport,
            Executor(status),
            capability_support=["vm_uds", "frida"],
        )
        assert agent.synchronize() == []
        agents[rig_id] = agent

    hosts.begin_capacity_trial(
        "host-a",
        actor="p0",
        reason="two-rig execution isolation",
        preflight_evidence=_passed_p0_evidence(
            tmp_path / "p0",
            targets=[
                {
                    "host_id": target.host_id,
                    "rig_id": target.rig_id,
                    "rig_generation": target.rig_generation,
                    "expected_usb_udid": target.expected_usb_udid,
                }
                for target in targets.values()
            ],
        ),
        ttl_seconds=60,
    )
    assert hosts.get_host("host-a")["effective_capacity"] == 2

    store.submit_assignment(_plan("one", "rig-a"), "one")
    store.submit_assignment(_plan("two", "rig-b"), "two")
    first = store.dispatch()
    second = store.dispatch()
    assert {first["bound_rig_id"], second["bound_rig_id"]} == {"rig-a", "rig-b"}
    assert hosts.get_host("host-a")["active_rigs"] == 2

    by_rig = {first["bound_rig_id"]: first, second["bound_rig_id"]: second}
    agents["rig-a"].execute_assignment(by_rig["rig-a"])
    agents["rig-b"].execute_assignment(by_rig["rig-b"])

    assert store.get_assignment("one")["status"] == "error"
    assert store.get_assignment("two")["status"] == "passed"
    assert hosts.get_host("host-a")["active_rigs"] == 0
    for rig_id, target in targets.items():
        assert {entry["sock_path"] for entry in request_log[rig_id]} == {str(target.socket)}
        assert (target.log_dir / "result.json").exists()
        owner = "one:1" if rig_id == "rig-a" else "two:1"
        artifacts = store.list_artifacts(owner_id=owner)
        assert [item["artifact_id"] for item in artifacts] == [f"artifact-{rig_id}"]
    assert {item[0] for item in frida_log} == {
        targets["rig-a"].frida_endpoint,
        targets["rig-b"].frida_endpoint,
    }

    quarantined = hosts.quarantine_rig(
        targets["rig-a"].rig_id,
        generation=targets["rig-a"].rig_generation,
        actor="p0-harness",
        reason="injected executor fault",
        evidence_refs=["artifact-rig-a"],
    )
    assert quarantined["rig"]["status"] == "quarantine"
    # In-process transports exercise disposition isolation without live WebSockets.
    states = {item["rig_id"]: item["registered_status"] for item in store.list_rigs()}
    assert states == {"rig-a": "quarantine", "rig-b": "online"}
    assert all(not item["agent_connected"] for item in store.list_rigs())


def _identity(rig: str, udid: str, generation: int = 1) -> dict:
    return {"host_id": "host-a", "rig_id": rig, "rig_generation": generation, "expected_usb_udid": udid}


def test_capacity_targets_cover_any_rig_count() -> None:
    three = [_identity("a", "1"), _identity("b", "2"), _identity("c", "3")]
    assert capacity_targets_registered(three, list(reversed(three)), host_id="host-a")
    assert capacity_targets_registered(three[:2], three, host_id="host-a")
    assert not capacity_targets_registered(three, three[:2], host_id="host-a")
    assert not capacity_targets_registered(three[:1], three, host_id="host-a")
    assert not capacity_targets_registered(three + [_identity("a", "1")], three, host_id="host-a")


def test_rollback_receipt_must_prove_every_rig_held_a_lease() -> None:
    from autophone.control.evidence import (
        CAPACITY_ROLLBACK_EXPECTED_RECEIPT,
        capacity_rollback_receipt_mismatches,
    )

    receipt = {
        **CAPACITY_ROLLBACK_EXPECTED_RECEIPT,
        "trial_capacity": 3,
        "simultaneous_assignment_count": 3,
        "events": ["capacity_trial_started", "capacity_trial_rolled_back"],
        "preflight_evidence_ref": "file:///p",
    }
    assert "simultaneous_assignment_count" not in capacity_rollback_receipt_mismatches(receipt, rig_count=3)
    assert "trial_capacity" in capacity_rollback_receipt_mismatches(receipt, rig_count=4)
    short = {**receipt, "simultaneous_assignment_count": 2}
    assert "simultaneous_assignment_count" in capacity_rollback_receipt_mismatches(short, rig_count=3)


def _register_rigs(control, tmp_path: Path, names: tuple[str, ...]) -> list[dict]:
    targets = []
    for index, rig_id in enumerate(names, start=1):
        target = _target(tmp_path, rig_id, index)
        targets.append(target.public_dict())
        control.register_agent(
            host_id=target.host_id, rig_id=target.rig_id, generation=target.rig_generation,
            capability_support=["vm_uds", "frida"], target=target.public_dict(),
        )
    return targets


def test_three_rig_evidence_raises_capacity_to_three_and_no_further(store, tmp_path: Path) -> None:
    store.register_host("host-a", max_active_rigs=1)
    control = ControlPlane(store)
    hosts: HostCoordination = control
    targets = _register_rigs(control, tmp_path, ("rig-a", "rig-b", "rig-c"))
    proof = _passed_p0_evidence(tmp_path / "p0", targets=targets, admission_profile=CAPACITY_ADMISSION_PROFILE)
    assert proof["status"] == "passed", proof
    review = _approved_capacity_review(proof)
    with pytest.raises(ControlStateError, match="exceeds the 3 Rigs"):
        hosts.change_host_capacity("host-a", 4, actor="operator", reason="beyond evidence", evidence=proof, approval=review)
    changed = hosts.change_host_capacity("host-a", 3, actor="operator", reason="qualified", evidence=proof, approval=review)
    assert changed["host"]["max_active_rigs"] == 3


def test_two_rig_evidence_cannot_raise_capacity_to_three(store, tmp_path: Path) -> None:
    store.register_host("host-a", max_active_rigs=1)
    control = ControlPlane(store)
    hosts: HostCoordination = control
    targets = _register_rigs(control, tmp_path, ("rig-a", "rig-b"))
    proof = _passed_p0_evidence(tmp_path / "p0", targets=targets, admission_profile=CAPACITY_ADMISSION_PROFILE)
    with pytest.raises(ControlStateError, match="exceeds the 2 Rigs"):
        hosts.change_host_capacity("host-a", 3, actor="operator", reason="beyond evidence",
                                   evidence=proof, approval=_approved_capacity_review(proof))
    assert hosts.get_host("host-a")["host"]["max_active_rigs"] == 1


def _qualify(control, hosts, tmp_path: Path, targets: list[dict], capacity: int) -> None:
    proof = _passed_p0_evidence(tmp_path / "p0", targets=targets, admission_profile=CAPACITY_ADMISSION_PROFILE)
    hosts.change_host_capacity("host-a", capacity, actor="operator", reason="qualified",
                               evidence=proof, approval=_approved_capacity_review(proof))


def test_evidence_targets_must_all_be_registered(store, tmp_path: Path) -> None:
    store.register_host("host-a", max_active_rigs=1)
    control = ControlPlane(store)
    hosts: HostCoordination = control
    targets = _register_rigs(control, tmp_path, ("rig-a", "rig-b"))
    stranger = {**targets[1], "rig_id": "rig-x", "expected_usb_udid": "udid-x"}
    proof = _passed_p0_evidence(tmp_path / "p0", targets=[targets[0], stranger], admission_profile=CAPACITY_ADMISSION_PROFILE)
    with pytest.raises(ControlStateError, match="not registered"):
        hosts.change_host_capacity("host-a", 2, actor="operator", reason="stranger",
                                   evidence=proof, approval=_approved_capacity_review(proof))


def test_qualified_rigs_run_together_while_others_stay_exclusive(store, tmp_path: Path) -> None:
    store.register_host("host-a", max_active_rigs=1)
    control = ControlPlane(store)
    hosts: HostCoordination = control
    targets = _register_rigs(control, tmp_path, ("rig-a", "rig-b", "legacy"))
    _qualify(control, hosts, tmp_path, targets[:2], 2)
    assert {row["rig_id"] for row in hosts.get_host("host-a")["host"]["qualified_rigs"]} == {"rig-a", "rig-b"}

    store.submit_assignment(_plan("a", "rig-a"), "a")
    assert store.dispatch()["status"] == "assigned"
    store.submit_assignment(_plan("legacy-1", "legacy"), "legacy-1")
    waiting = store.dispatch()
    assert waiting["assignment_id"] == "legacy-1" and waiting["wait_reason"] == "host_exclusive"
    store.submit_assignment(_plan("b", "rig-b"), "b")
    assert store.dispatch()["assignment_id"] == "b"
    assert hosts.get_host("host-a")["active_rigs"] == 2


def test_an_unqualified_rig_holds_the_host_alone(store, tmp_path: Path) -> None:
    store.register_host("host-a", max_active_rigs=1)
    control = ControlPlane(store)
    hosts: HostCoordination = control
    targets = _register_rigs(control, tmp_path, ("rig-a", "rig-b", "legacy"))
    _qualify(control, hosts, tmp_path, targets[:2], 2)

    store.submit_assignment(_plan("legacy-1", "legacy"), "legacy-1")
    assert store.dispatch()["status"] == "assigned"
    store.submit_assignment(_plan("a", "rig-a"), "a")
    waiting = store.dispatch()
    assert waiting["assignment_id"] == "a" and waiting["wait_reason"] == "host_exclusive"
    assert hosts.get_host("host-a")["active_rigs"] == 1


def test_lowering_capacity_clears_the_qualified_set(store, tmp_path: Path) -> None:
    store.register_host("host-a", max_active_rigs=1)
    control = ControlPlane(store)
    hosts: HostCoordination = control
    targets = _register_rigs(control, tmp_path, ("rig-a", "rig-b"))
    _qualify(control, hosts, tmp_path, targets, 2)
    changed = hosts.change_host_capacity("host-a", 1, actor="operator", reason="identity change")
    assert changed["host"]["qualified_rigs"] is None


def test_trial_admits_evidence_rigs_but_not_others(store, tmp_path: Path) -> None:
    store.register_host("host-a", max_active_rigs=1)
    control = ControlPlane(store)
    hosts: HostCoordination = control
    targets = _register_rigs(control, tmp_path, ("rig-a", "rig-b", "legacy"))
    hosts.begin_capacity_trial(
        "host-a", actor="p0", reason="capacity drill",
        preflight_evidence=_passed_p0_evidence(tmp_path / "p0", targets=targets[:2]), ttl_seconds=60,
    )
    store.submit_assignment(_plan("a", "rig-a"), "a")
    assert store.dispatch()["status"] == "assigned"
    store.submit_assignment(_plan("legacy-1", "legacy"), "legacy-1")
    assert store.dispatch()["wait_reason"] == "host_exclusive"


def test_a_deregistered_rig_no_longer_blocks_qualification(store, tmp_path: Path) -> None:
    store.register_host("host-a", max_active_rigs=1)
    control = ControlPlane(store)
    hosts: HostCoordination = control
    targets = _register_rigs(control, tmp_path, ("rig-a", "rig-b", "retired"))
    store.deregister_rig("retired", generation=1, actor="operator", reason="retired")
    proof = _passed_p0_evidence(tmp_path / "p0", targets=targets[:2], admission_profile=CAPACITY_ADMISSION_PROFILE)
    changed = hosts.change_host_capacity("host-a", 2, actor="operator", reason="qualified",
                                         evidence=proof, approval=_approved_capacity_review(proof))
    assert changed["host"]["max_active_rigs"] == 2


def test_trial_admits_every_evidence_rig_at_once(store, tmp_path: Path) -> None:
    store.register_host("host-a", max_active_rigs=1)
    control = ControlPlane(store)
    hosts: HostCoordination = control
    targets = _register_rigs(control, tmp_path, ("rig-a", "rig-b", "rig-c"))
    started = hosts.begin_capacity_trial(
        "host-a", actor="p0", reason="capacity drill",
        preflight_evidence=_passed_p0_evidence(tmp_path / "p0", targets=targets), ttl_seconds=60,
    )
    assert hosts.get_host("host-a")["effective_capacity"] == 3
    for name in ("rig-a", "rig-b", "rig-c"):
        store.submit_assignment(_plan(f"trial-{name}", name), f"trial-{name}")
        assert store.dispatch()["status"] == "assigned"
    assert hosts.get_host("host-a")["active_rigs"] == 3
    hosts.finish_capacity_trial(started["trial"]["trial_id"], actor="p0", result={"passed": True})
    assert hosts.get_host("host-a")["effective_capacity"] == 1


def test_environment_worker_reserves_verifies_and_admits_by_environment(store, tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from tools.probes.host import environment_qualification as worker

    control = ControlPlane(store)
    pair = [DeviceTarget.from_mapping(row) for row in _register_rigs(control, tmp_path, ("rig-a", "rig-b"))]
    store.set_host_allocatable("host-a", memory_bytes=100, actor="ops", reason="test")
    store.set_host_admission_mode("host-a", "resource", actor="ops", reason="test")
    with store._connection() as db:
        db.execute("UPDATE hosts SET max_active_rigs=2, qualified_rigs='[]'::jsonb")
    commit = "abc"

    def environment(rows, *, run):
        return {"macos_build": "test", "vphone_cli": [
            {"dir": row["cli_dir"], "commit": commit, "dirty": False} for row in rows
        ]}

    monkeypatch.setattr(worker, "capture_environment", environment)
    args = SimpleNamespace(setup=tmp_path / "setup.json", actor="test", reason="regression")
    args.setup.write_text(json.dumps({"duration_seconds": 120, "environment_fingerprint": {
        "host_id": "host-a", "macos_build": "test", "vphone_cli_commit": "abc",
    }}))

    def sample():
        store.record_host_load_samples("host-a", [{
            "sampled_at": datetime.now(timezone.utc), "interval_seconds": 60,
            "running_vm_count": 0, "pressure_level": 1, "fingerprint": "telemetry", "running_vms": [],
        }])

    sample()
    store.report_rig_resources("host-a", {target.rig_id: {"memory_bytes": 1} for target in pair})
    for target in pair:
        store.submit_assignment(_plan(target.rig_id, target.rig_id), target.rig_id)

    def smoke(_store, _targets, setup, output, timeout):
        assert store.dispatch()["wait_reason"] == "host_maintenance"
        assert len(store.list_reservations(statuses=["active"])) == 2
        with pytest.raises(ControlStateError, match="reserved"):
            store.register_rig("rig-a", host_id="host-a", generation=2)
        report = _passed_p0_evidence(output, targets=[t.public_dict() for t in pair], duration=120)
        path = output / "same-host-p0-report.json"
        report["evidence_ref"] = path.as_uri()
        path.write_text(json.dumps(report))
        return report

    def rejected(*inputs):
        report = smoke(*inputs)
        report["checks"]["storage_isolation"]["sha256"] = "0" * 64
        (inputs[3] / "same-host-p0-report.json").write_text(json.dumps(report))
        return report

    with pytest.raises(ControlStateError, match="evidence rejected"):
        worker.tick(store, pair, args, probe=rejected)
    assert store.host_snapshot("host-a")["host"]["qualified_environment"] is None
    assert not store.list_reservations(statuses=["active"])
    assert worker.tick(store, pair, args, probe=smoke) == "environment_smoke"
    assert worker.tick(store, pair, args, probe=pytest.fail) is None
    args.canary_setup = tmp_path / "canary.json"
    long_setup = json.loads(args.setup.read_text())
    long_setup.update(duration_seconds=3600, admission_profile=CAPACITY_ADMISSION_PROFILE)
    args.canary_setup.write_text(json.dumps(long_setup))

    def canary(_store, _targets, setup, output, timeout):
        return _passed_p0_evidence(output, targets=[t.public_dict() for t in pair],
                                   admission_profile=CAPACITY_ADMISSION_PROFILE)

    assert store.environment_canary_due("host-a", 3600)
    assert worker.tick(store, pair, args, canary_due=True, probe=canary) == "environment_canary"
    assert not store.environment_canary_due("host-a", 3600)
    third = _target(tmp_path, "rig-x", 3)
    control.register_agent(host_id="host-a", rig_id="rig-x", generation=1, capability_support=["vm_uds", "frida"],
                           target=third.public_dict())
    store.register_rig("rig-x", host_id="host-a", generation=2)
    store.deregister_rig("rig-x", 2, actor="test", reason="retire")
    new = _target(tmp_path, "rig-c", 4)
    control.register_agent(host_id="host-a", rig_id="rig-c", generation=1, capability_support=["vm_uds", "frida"],
                           target=new.public_dict())
    store.report_rig_resources("host-a", {"rig-c": {"memory_bytes": 1}})
    store.submit_assignment(_plan("third", "rig-c"), "third")
    assert worker.tick(store, pair, args, probe=pytest.fail) is None
    first = store.dispatch()
    assert first["status"] == "assigned"
    with store._connection() as db:
        db.execute("UPDATE hosts SET environment_observed_at=now()-interval '181 seconds'")
    assert store.dispatch()["wait_reason"] == "host_exclusive"
    commit = "def"
    with pytest.raises(ControlStateError, match="setup does not match"):
        worker.tick(store, pair, args, probe=pytest.fail)
    assert store.dispatch()["wait_reason"] == "host_exclusive"
    commit = "abc"
    assert worker.observe(store, pair, args.actor, args.reason)[1] is None
    store.complete_assignment(first["assignment_id"], attempt_no=first["attempt"]["attempt_no"], fencing=first["attempt"]["fencing"],
                              result={"status": "passed", "rig_disposition": "reusable",
                                      "cleanup_receipt": {"artifacts_sealed": True, "device_access_released": True}})
    assert worker.tick(store, pair, args, probe=smoke) == "environment_smoke"
    assert store.dispatch()["status"] == "assigned"
    assert store.dispatch()["assignment_id"] == "third"
    store.set_host_admission_mode("host-a", "capacity", actor="test", reason="rollback")
    with pytest.raises(ControlStateError, match="lowered to 1"):
        store.register_rig("rig-d", host_id="host-a", generation=1)


@pytest.mark.parametrize("failure", [None, "probe", "restore", "wrong_schema", "changed_setup"])
def test_environment_probe_isolates_capacity_drill_and_restores_target(store, tmp_path, monkeypatch, failure):
    import subprocess

    import psycopg
    from tools.probes.host import environment_qualification as worker

    pair = [_target(tmp_path, "rig-a", 1), _target(tmp_path, "rig-b", 2)]
    store.register_host("host-a")
    for target in pair:
        store.register_rig(target.rig_id, host_id="host-a", generation=1)
    output = tmp_path / "probe"
    output.mkdir()
    schema = None
    restored = False

    from tools.probes.host.live_p0 import LiveP0Setup

    setup = {"duration_seconds": 120, "concurrent_load": {}, "sampling_method": "test",
             "environment_fingerprint": {}}

    def operator(directory, timeout):
        nonlocal schema
        request = json.loads((directory / "manual-request.json").read_text())
        schema = request["schema"]
        assert schema != store.schema
        isolated = PostgresControlStore(store.dsn, schema=schema)
        assert isolated.host_snapshot("host-a")["host"]["max_active_rigs"] == 1
        assert {r["rig_id"] for r in isolated.list_rigs()} == {t.rig_id for t in pair}
        assert set(request["required_options"]) == {
            "--include-fault-isolation", "--include-usb-baseline", "--include-capacity-rollback"}
        (output / "concurrent_vm_start.json").write_text("{}")
        (output / "single_rig_fault_isolation.json").write_text("{}")
        (output / "capacity_rollback.json").write_text('{"status":"failed"}')
        if failure == "probe":
            raise ControlStateError("manual report did not arrive")
        if failure == "wrong_schema":
            (output / "capacity_rollback.json").write_text(
                '{"status":"passed","observations":{"receipt":{"trial_id":"foreign"}}}')
        if failure == "changed_setup":
            return {"experiment_setup": {}, "targets": [t.public_dict() for t in pair]}
        return {"experiment_setup": LiveP0Setup(**setup).to_dict(), "targets": [t.public_dict() for t in pair]}

    def command(argv, *, env, timeout, check):
        nonlocal restored
        assert len(argv) == 1 and argv[0].endswith("start_vphone.sh")
        assert env["SKIP_CONTROL_PLANE"] == "1"
        assert env["VM_DIR"] == str(pair[1].vm_dir)
        restored = True
        if failure == "restore":
            raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(worker, "wait_for_manual_report", operator)
    monkeypatch.setattr(worker.subprocess, "run", command)
    if failure:
        with pytest.raises((ControlStateError, subprocess.SubprocessError)):
            worker.collect_manual_report(store, pair, setup, output, 100)
    else:
        assert worker.collect_manual_report(store, pair, setup, output, 100)["experiment_setup"]["duration_seconds"] == 120
    assert restored == (failure != "probe")
    assert schema
    with psycopg.connect(store.dsn) as connection:
        assert connection.execute("SELECT to_regnamespace(%s)", (schema,)).fetchone() == (None,)
    statuses = {r["rig_id"]: r["status"] for r in store.host_snapshot("host-a")["rigs"]}
    assert statuses == {"rig-a": "quarantine" if failure == "probe" else "online",
                        "rig-b": "quarantine" if failure in ("restore", "probe") else "online"}

def test_environment_poll_only_observes(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from tools.probes.host import environment_qualification as worker

    observed = Mock()
    monkeypatch.setattr(worker, "load_pair", lambda host, args: ["a", "b"])
    monkeypatch.setattr(worker, "observe", observed)
    monkeypatch.setattr(worker, "tick", lambda *a, **kw: pytest.fail("poll started a manual window"))
    def interrupt(_seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(worker.time, "sleep", interrupt)
    store = Mock()
    with pytest.raises(KeyboardInterrupt):
        worker.poll(store, "host", SimpleNamespace(actor="op", reason="test"), Mock())
    observed.assert_called_once_with(store, ["a", "b"], "op", "test")


def test_manual_window_waits_for_complete_json(tmp_path, monkeypatch):
    from tools.probes.host import environment_qualification as worker

    path = tmp_path / "same-host-p0-report.json"
    path.write_text("{")
    monkeypatch.setattr(worker.time, "sleep", lambda _: path.write_text('{"checks":{}}'))
    assert worker.wait_for_manual_report(tmp_path, 5) == {"checks": {}}
    with pytest.raises(ControlStateError, match="not received"):
        worker.wait_for_manual_report(tmp_path, 0)
