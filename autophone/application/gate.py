"""回归准入编排：查询 Run 索引与 App 健康度，执行准入判定并登记结果。

包安装与通知通过现有适配函数执行；CLI 与 HTTP 调用同一准入实现。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from secrets import token_hex
from typing import Any, Callable

from autophone.packages import store as pkg
from autophone.assets.apps import load_app_profile
from autophone.execution.case_selection import canonical_selector, selector_key
from autophone.contracts import validate_or_raise, write_json
from autophone.application.health import app_health
from autophone.application import run_index
from autophone.application.run_index import db_path, ensure_current
from autophone.util import now_iso, read_json
from autophone.device.target import default_device

FAILED_STATUSES = {"failed", "error", "blocked"}
GATES_DIRNAME = "gates"
InstallFn = Callable[..., dict[str, Any]]


class GateError(ValueError):
    pass


def gates_root(run_root: Path) -> Path:
    return run_root.parent / GATES_DIRNAME


def gate_dir(run_root: Path, gate_id: str) -> Path:
    return gates_root(run_root) / gate_id


def gate_result_path(run_root: Path, gate_id: str) -> Path:
    return gate_dir(run_root, gate_id) / "gate-result.json"


def new_gate_id() -> str:
    return f"gate-{now_iso().replace(':', '').replace('-', '')[:15]}-{token_hex(3)}"


def load_gate_result(run_root: Path, gate_id: str) -> dict[str, Any]:
    doc = read_json(gate_result_path(run_root, gate_id))
    if not isinstance(doc, dict):
        raise FileNotFoundError(f"gate not found: {gate_id}")
    validate_or_raise("gate-result.v1.json", doc)
    return doc


def list_gate_results(run_root: Path, *, app: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    rows: list[tuple[float, dict[str, Any]]] = []
    root = gates_root(run_root)
    for path in root.glob("*/gate-result.json"):
        doc = read_json(path)
        if not isinstance(doc, dict):
            continue
        if app and doc.get("app") != app:
            continue
        try:
            validate_or_raise("gate-result.v1.json", doc)
            mtime = path.stat().st_mtime
        except Exception:
            continue
        rows.append((mtime, doc))
    rows.sort(key=lambda item: item[0], reverse=True)
    return [doc for _mtime, doc in rows[:limit]]


def _empty_diff() -> dict[str, list[dict[str, Any]]]:
    return {"newly_failed": [], "fixed": [], "still_failed": [], "new_case": [], "removed_case": []}


def _write_gate(run_root: Path, doc: dict[str, Any]) -> dict[str, Any]:
    now = now_iso()
    doc.setdefault("created_at", now)
    doc["updated_at"] = now
    validate_or_raise("gate-result.v1.json", doc)
    path = gate_result_path(run_root, str(doc["gate_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, doc)
    return doc


def _base_doc(gate_id: str, app: str, slot: str) -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "gate_id": gate_id,
        "app": app,
        "slot": slot,
        "status": "installing",
        "installed": {"slot": slot, "version": None, "sha256": None, "verified": None},
        "run": {"run_id": None, "run_dir": None, "status": None},
        "baseline": None,
        "diff": _empty_diff(),
        "flaky_excluded": [],
        "visual_diff": [],
        "warnings": [],
    }


BASELINE_POLICIES = (
    "auto",
    "previous_build_same_slot",
    "previous_released_slot",
    "explicit_build",
)


def _previous_package_version(app: str, slot: str) -> str | None:
    entries = pkg.list_packages(app)
    ordered = sorted(entries, key=lambda e: int(e.slot))
    for idx, entry in enumerate(ordered):
        if entry.slot == slot and idx > 0:
            return ordered[idx - 1].version
    return None


def resolve_baseline_build(
    app: str,
    current_build_id: str | None,
    *,
    policy: str = "auto",
    explicit_build_id: str | None = None,
) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
    """Pick the Build a gate compares against, under an explicit policy.

    ``auto`` resolves to the previous Build inside the same Version Slot when
    one exists (same version, new commit), otherwise the previous released
    Version Slot's Candidate.  The policy that actually decided is returned so
    the gate document records it rather than leaving it implicit.
    """
    if policy not in BASELINE_POLICIES:
        raise ValueError(f"unknown baseline policy {policy!r}")
    warnings: list[dict[str, Any]] = []
    if policy != "explicit_build" and not current_build_id:
        # No confirmed current identity: report it rather than reaching for the
        # current Candidate, which would compare a build against itself.
        warnings.append({
            "code": "baseline_current_build_unknown",
            "message": "gate has no confirmed current Build identity; baseline not resolved",
        })
        return None, policy, warnings
    registry = pkg.load_registry(app)

    if policy == "explicit_build":
        build = registry.build(str(explicit_build_id or ""))
        if build is None:
            warnings.append({
                "code": "baseline_build_not_found",
                "message": f"explicit baseline build {explicit_build_id!r} is not registered",
            })
            return None, policy, warnings
        return _build_ref_dict(registry, build), policy, warnings

    current = registry.build(str(current_build_id))
    if current is None:
        warnings.append({
            "code": "baseline_current_build_unknown",
            "message": f"current build {current_build_id!r} is not registered; baseline not resolved",
        })
        return None, policy, warnings

    if policy in ("auto", "previous_build_same_slot"):
        siblings = registry.builds_for_slot(current.slot)
        index = next(
            (i for i, b in enumerate(siblings) if b.build_id == current.build_id), None,
        )
        if index is not None and index > 0:
            return _build_ref_dict(registry, siblings[index - 1]), "previous_build_same_slot", warnings
        if policy == "previous_build_same_slot":
            warnings.append({
                "code": "baseline_no_previous_build_in_slot",
                "message": f"version slot {current.slot} has no earlier Build than {current.build_id}",
            })
            return None, policy, warnings

    earlier = [
        s for s in registry.version_slots if s.slot < current.slot and not s.archived
    ]
    if not earlier:
        warnings.append({
            "code": "baseline_no_previous_version_slot",
            "message": f"no released version slot precedes {current.slot}",
        })
        return None, "previous_released_slot", warnings
    previous_slot = max(earlier, key=lambda s: s.slot)
    build = registry.build(previous_slot.candidate_build_id or "")
    if build is None:
        warnings.append({
            "code": "baseline_no_candidate_build",
            "message": f"version slot {previous_slot.slot} has no candidate build",
        })
        return None, "previous_released_slot", warnings
    return _build_ref_dict(registry, build), "previous_released_slot", warnings


def _build_ref_dict(registry: Any, build: Any) -> dict[str, Any]:
    from autophone.packages.model import RegistryBuildRef

    return RegistryBuildRef.from_build(
        registry.app, build, registry.short_version_of(build),
        resolution_source="build_id",
    ).to_dict()


def _query_candidate_runs(
    run_root: Path,
    *,
    app: str,
    selector: dict[str, Any],
) -> list[dict[str, Any]]:
    ensure_current(run_root)
    key = selector_key(selector)
    with run_index.connect(run_root, init=False) as conn:
        rows = conn.execute(
            """
            SELECT run_dir, run_id, package_device_version, package_build_id,
                   package_short_version, ended_at, result_mtime_ns
            FROM runs
            WHERE app=? AND selector_key=? AND type='regression' AND negative_control_id IS NULL
              AND status IN ('passed','failed','error','blocked','pending')
            ORDER BY result_mtime_ns DESC
            """,
            (app, key),
        ).fetchall()
    return [dict(row) for row in rows]


def find_baseline(
    run_root: Path,
    *,
    app: str,
    selector: dict[str, Any],
    current_run_dir: Path | None = None,
    previous_device_version: str | None = None,
    current_device_version: str | None = None,
    baseline_build_id: str | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Most recent comparable run for the baseline.

    When a baseline Build is known, runs are matched on Build identity; runs
    predating build ids are reported as a structured warning rather than being
    silently accepted on version alone.
    """
    warnings: list[dict[str, Any]] = []
    current_rel = None
    if current_run_dir is not None:
        try:
            current_rel = current_run_dir.resolve().relative_to(run_root.resolve()).as_posix()
        except ValueError:
            current_rel = current_run_dir.as_posix()
    for row in _query_candidate_runs(run_root, app=app, selector=selector):
        if current_rel and row["run_dir"] == current_rel:
            continue
        if baseline_build_id:
            row_build = row.get("package_build_id")
            if row_build is None:
                warnings.append({
                    "code": "baseline_build_identity_unknown",
                    "message": (
                        f"candidate baseline {row.get('run_id')} predates build identity; "
                        "not accepted on version alone"
                    ),
                    "run_id": row.get("run_id"),
                    "run_dir": row.get("run_dir"),
                })
                continue
            if str(row_build) != str(baseline_build_id):
                continue
            return {
                "run_id": row["run_id"],
                "run_dir": row["run_dir"],
                "device_version": row.get("package_device_version"),
                "build_id": str(row_build),
            }, warnings
        version = row.get("package_device_version")
        if version is None:
            warnings.append({
                "code": "baseline_device_version_unknown",
                "message": f"candidate baseline {row.get('run_id')} has null package.device_version",
                "run_id": row.get("run_id"),
                "run_dir": row.get("run_dir"),
            })
            continue
        if previous_device_version and version != previous_device_version:
            continue
        if not previous_device_version and current_device_version and version == current_device_version:
            continue
        return {
            "run_id": row["run_id"],
            "run_dir": row["run_dir"],
            "device_version": version,
        }, warnings
    return None, warnings


def _case_map(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(case.get("id")): case for case in result.get("cases") or [] if case.get("id")}


def _detail(case: dict[str, Any] | None) -> str | None:
    if not case:
        return None
    for plane in ("ui", "function", "api", "telemetry"):
        detail = ((case.get("planes") or {}).get(plane) or {}).get("detail")
        if detail:
            return str(detail)
    return None


def diff_results(
    current: dict[str, Any],
    baseline: dict[str, Any],
    *,
    flaky_cases: set[str] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    flaky_cases = flaky_cases or set()
    diff = _empty_diff()
    flaky_excluded: list[dict[str, Any]] = []
    cur = _case_map(current)
    base = _case_map(baseline)
    for cid in sorted(set(cur) | set(base)):
        cc = cur.get(cid)
        bc = base.get(cid)
        cur_status = cc.get("status") if cc else None
        base_status = bc.get("status") if bc else None
        item = {
            "case_id": cid,
            "current_status": cur_status,
            "baseline_status": base_status,
            "current_detail": _detail(cc),
            "baseline_detail": _detail(bc),
            "flaky": cid in flaky_cases,
        }
        cur_failed = cur_status in FAILED_STATUSES
        base_failed = base_status in FAILED_STATUSES
        if cc and not bc:
            diff["new_case"].append(item)
        elif bc and not cc:
            diff["removed_case"].append(item)
        elif cur_failed and not base_failed:
            if cid in flaky_cases:
                flaky_excluded.append(item)
            else:
                diff["newly_failed"].append(item)
        elif base_failed and not cur_failed:
            diff["fixed"].append(item)
        elif cur_failed and base_failed:
            diff["still_failed"].append(item)
    return diff, flaky_excluded


def _flaky_cases(run_root: Path, app: str) -> set[str]:
    try:
        profile = load_app_profile(app)
        health = app_health(
            run_root,
            app,
            cases_path=profile.cases_path(),
            operations_path=profile.operations_path(),
            api_expectations_path=profile.api_expectations_path(),
            negctl_root=run_root.parent / "negctl",
            build_label=profile.build_label,
            recipes_dir=profile.recipes_dir(),
        )
    except Exception:
        health = app_health(run_root, app)
    return {str(case["case_id"]) for case in health.get("cases") or [] if case.get("flaky")}


def _read_result(run_root: Path, rel_run_dir: str) -> dict[str, Any]:
    path = run_root / rel_run_dir / "platform-run-result.json"
    doc = read_json(path)
    if not isinstance(doc, dict):
        raise GateError(f"baseline result missing: {path}")
    return doc


def run_gate(
    service: Any,
    app: str,
    slot: str,
    *,
    gate_id: str | None = None,
    build_id: str | None = None,
    selector: dict[str, Any] | None = None,
    verify: bool = True,
    baseline_policy: str = "auto",
    baseline_build_id: str | None = None,
    install_fn: InstallFn | None = None,
    visual_diff: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from autophone.application.control_plane import configured_control_plane
    control = configured_control_plane()
    gate_id = gate_id or new_gate_id()
    selector = canonical_selector(selector or {"suite": "smoke"})
    run_root = Path(service.run_root)
    doc = _base_doc(gate_id, app, slot)
    _write_gate(run_root, doc)
    from autophone.execution.dispatch import RunDispatcher
    from autophone.rig.source_identity import SourceProcessStale, assert_process_source_current

    # 本地兼容路径会先安装；必须在安装和构造派单计划前核对进程源码。
    try:
        assert_process_source_current()
    except SourceProcessStale as exc:
        doc["status"] = "error"
        doc["warnings"].append({"code": exc.code, "message": str(exc)})
        _write_gate(run_root, doc)
        return {"ok": False, "error": str(exc), "gate": doc}

    # 先判定控制面，再进行安装或 Run；后台 worker 不能沿兼容路径直连设备。
    spec = {
        "schema_version": "v1",
        "run_id": f"{gate_id}-smoke",
        "type": "regression",
        "app": app,
        "device": default_device(),
        "case_selector": selector,
        "origin": {"gate_id": gate_id},
        "planes": {"ui": {"mode": "required"}},
        "requested_by": "gate",
        "requested_at": now_iso(),
    }
    routed_handle = None
    if control is not None:
        try:
            ref = pkg.resolve_build_ref(app, slot=slot, build_id=build_id)
            build_ref = dict(ref.to_dict())
            routed_handle = RunDispatcher(service).submit(
                spec, pin_device=True, assignment_options={
                    "desired_state": {"app": app, "build_ref": build_ref},
                    "required_capabilities": ["app_install"],
                },
            )
            provisioning = routed_handle.result.get("assignment_provisioning") or {}
            if provisioning.get("status") != "passed" or not provisioning.get("readback"):
                doc["status"] = "error"
                doc["warnings"].append({"code": "install_error", "message": "Assignment has no verified provisioning readback"})
                _write_gate(run_root, doc)
                return {"ok": False, "error": "Assignment provisioning did not complete", "gate": doc}
            doc["installed"] = {
                "slot": build_ref["slot"], "version": build_ref.get("short_version"),
                "sha256": build_ref["artifact_sha256"], "build_id": build_ref["build_id"],
                "build_ref": build_ref, "verified": True,
            }
            doc["status"] = "running"
            _write_gate(run_root, doc)
        except Exception as exc:
            doc["status"] = "error"
            doc["warnings"].append({"code": "assignment_error", "message": f"{type(exc).__name__}: {exc}"})
            _write_gate(run_root, doc)
            return {"ok": False, "error": str(exc), "gate": doc}
    else:
        install_fn = install_fn or pkg.install_package

        try:
            installed = install_fn(app, slot, verify=verify, **({"build_selector": pkg.BuildSelector(slot=slot, build_id=build_id)} if build_id else {}))
            installed_ref = installed.get("build_ref")
            if isinstance(installed_ref, dict):
                installed_slot = str(installed_ref.get("slot") or installed.get("slot") or slot)
                installed_version = installed_ref.get("short_version")
                installed_sha256 = installed_ref.get("artifact_sha256")
                installed_build_id = installed_ref.get("build_id") or installed.get("build_id")
            else:
                # Compatibility for legacy/custom installers that predate Build Ref.
                entry = pkg.get_package(app, str(installed.get("slot") or slot))
                installed_slot = entry.slot
                installed_version = entry.version
                installed_sha256 = entry.sha256
                installed_build_id = installed.get("build_id") or getattr(entry, "build_id", None)
            doc["installed"] = {
                "slot": installed_slot,
                "version": installed_version,
                "sha256": installed_sha256,
                "verified": installed.get("verified"),
                "build_id": installed_build_id,
                "build_ref": installed_ref,
            }
            doc["status"] = "running"
            _write_gate(run_root, doc)
        except Exception as exc:
            doc["status"] = "error"
            doc["warnings"].append({"code": "install_error", "message": f"{type(exc).__name__}: {exc}"})
            _write_gate(run_root, doc)
            return {"ok": False, "error": str(exc), "gate": doc}

    try:
        handle = routed_handle or RunDispatcher(service).submit(spec, background=False)
    except Exception as exc:
        doc["status"] = "error"
        doc["run"] = {"run_id": spec["run_id"], "run_dir": None, "status": "error"}
        doc["warnings"].append({"code": "smoke_run_error", "message": f"{type(exc).__name__}: {exc}"})
        _write_gate(run_root, doc)
        return {"ok": False, "error": str(exc), "gate": doc}

    current_result = handle.result
    doc["run"] = {"run_id": handle.run_id, "run_dir": str(handle.run_dir), "status": current_result.get("status")}
    current_pkg = current_result.get("package") or {}
    current_device_version = current_pkg.get("device_version") or doc["installed"].get("version")
    previous_device_version = _previous_package_version(app, slot)
    baseline_ref, resolved_policy, policy_warnings = resolve_baseline_build(
        app, doc["installed"].get("build_id"),
        policy=baseline_policy, explicit_build_id=baseline_build_id,
    )
    doc["baseline_policy"] = {
        "requested": baseline_policy,
        "resolved": resolved_policy,
        "build_ref": baseline_ref,
    }
    doc["warnings"].extend(policy_warnings)
    baseline, warnings = find_baseline(
        run_root,
        app=app,
        selector=selector,
        current_run_dir=Path(handle.run_dir),
        previous_device_version=previous_device_version,
        current_device_version=current_device_version,
        baseline_build_id=(baseline_ref or {}).get("build_id"),
    )
    doc["warnings"].extend(warnings)
    if baseline is None:
        doc["status"] = "no_baseline"
        doc["visual_diff"] = list(visual_diff or [])
        _write_gate(run_root, doc)
        return {"ok": True, "gate": doc}

    baseline_result = _read_result(run_root, str(baseline["run_dir"]))
    flaky = _flaky_cases(run_root, app)
    diff, flaky_excluded = diff_results(current_result, baseline_result, flaky_cases=flaky)
    doc["baseline"] = baseline
    doc["diff"] = diff
    doc["flaky_excluded"] = flaky_excluded
    doc["visual_diff"] = list(visual_diff or [])
    doc["status"] = "failed" if diff["newly_failed"] else "passed"
    _write_gate(run_root, doc)
    return {"ok": True, "passed": doc["status"] != "failed", "gate": doc}
