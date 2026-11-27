from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from autophone.execution import overturns
from autophone.application import run_index
from autophone.assets.promotion import GENERIC_PROBE_SCHEMA, discrimination_gaps
from autophone.assets.promotion_identity import (
    artifact_content_digest_from_manifest,
    current_symbol_binding_sha256,
    steps_use_symbol_binding,
)
from autophone.execution.engine.cases_loader import load_cases

WINDOW_SIZE = 20
FLAKY_MIN_EXECUTIONS = 6
FLAKY_MIN_EACH = 2
FLAKY_MIN_FLIPS = 2
PASS_FAIL = {"passed", "failed"}


def _connect_existing(run_root: Path) -> sqlite3.Connection:
    return run_index.connect(run_root, init=False)


def _load_case_items(cases_path: Path | None) -> dict[str, dict[str, Any]]:
    if cases_path is None or not cases_path.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in load_cases(cases_path):
        cid = str((row or {}).get("id") or "")
        if cid:
            out[cid] = row
    return out


def _load_operations(operations_path: Path | None) -> list[dict[str, Any]]:
    if operations_path is None or not operations_path.exists():
        return []
    doc = yaml.safe_load(operations_path.read_text(encoding="utf-8")) or {}
    return [op for op in (doc.get("operations") or []) if isinstance(op, dict)]


def _load_active_api_expectation_ids(api_expectations_path: Path | None) -> set[str]:
    if api_expectations_path is None or not api_expectations_path.exists():
        return set()
    doc = yaml.safe_load(api_expectations_path.read_text(encoding="utf-8")) or {}
    return {
        str(item.get("id"))
        for item in (doc.get("expectations") or [])
        if isinstance(item, dict) and item.get("status") == "active" and item.get("id")
    }


def _latest_negctl_probes(
    negctl_root: Path | None,
    *,
    build_label: str | None = None,
    app: str | None = None,
) -> list[dict[str, Any]]:
    if negctl_root is None or not negctl_root.exists():
        return []
    build_dirs = sorted(d for d in negctl_root.iterdir() if d.is_dir())
    if build_label is not None:
        build_dirs = [d for d in build_dirs if d.name == build_label]
    for d in reversed(build_dirs):
        art = d / "artifact.json"
        if not art.exists():
            continue
        try:
            doc = json.loads(art.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        probes = doc.get("probes") or []
        if isinstance(probes, list):
            probe_build = str(doc.get("build_label") or d.name)
            # 按 App 过滤：通用 negctl probe 带 app 字段；旧版 probe 不带，照旧参与。
            return [
                {**p, "build_label": probe_build}
                for p in probes
                if isinstance(p, dict) and (app is None or p.get("app") in (None, app))
            ]
    return []


def _current_recipe_steps(recipes_dir: Path | None, case_id: str) -> list[Any]:
    """读取当前 Recipe（live 优先，否则 draft）的 steps 列表；不可读取时返回空列表。"""
    if recipes_dir is None or not case_id:
        return []
    for path in (recipes_dir / f"{case_id.lower()}.yaml",
                 recipes_dir / "drafts" / f"{case_id.lower()}.yaml"):
        if not path.is_file():
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            steps = doc.get("steps") if isinstance(doc, dict) else None
            return steps if isinstance(steps, list) else []
        except Exception:  # noqa: BLE001 - 读不到当前 Recipe 交由上层判别
            return []
    return []


def _current_steps_digest(recipes_dir: Path | None, case_id: str) -> str | None:
    """当前 Recipe（live 优先，否则 draft）的 steps 规范摘要；读不到为 None。"""
    if recipes_dir is None or not case_id:
        return None
    from autophone.execution.engine.recipe import recipe_steps_digest

    steps = _current_recipe_steps(recipes_dir, case_id)
    if not steps:
        return None
    try:
        return recipe_steps_digest(steps)
    except Exception:  # noqa: BLE001 - 读不到当前 Recipe 即无法核对身份
        return None


def _probe_matches_current_recipe(probe: dict[str, Any], recipes_dir: Path | None) -> bool:
    """通用 negctl probe 的 Recipe steps 摘要须等于当前 Recipe；旧格式 probe 不带身份，不比对。

    转正把 draft Recipe 重新序列化为 live 文件，文件摘要与 Case revision 都会变化，steps
    内容不变，所以健康统计按 steps 规范摘要核对。无法读取当前 Recipe 时不计为已证。"""
    if probe.get("schema") != GENERIC_PROBE_SCHEMA:
        return True
    expected = probe.get("recipe_steps_digest")
    return bool(expected) and _current_steps_digest(recipes_dir, str(probe.get("case_id") or "")) == expected


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _result_quality(
    run_root: Path,
    *,
    case_items: dict[str, dict[str, Any]],
    rows: list[sqlite3.Row],
    operations_path: Path | None,
    api_expectations_path: Path | None,
    negctl_root: Path | None,
    build_label: str | None,
    app: str | None = None,
    recipes_dir: Path | None = None,
    current_artifact_content_digest: str | None = None,
    current_binding_sha256: str | None = None,
    uses_symbol_binding: bool | None = None,
) -> dict[str, Any]:
    all_operations = _load_operations(operations_path)
    operations = [op for op in all_operations if op.get("status") == "active"]
    operations_by_name: dict[str, list[dict[str, Any]]] = {}
    for op in all_operations:
        name = str(op.get("name") or "").strip()
        if name:
            operations_by_name.setdefault(name, []).append(op)
    witness_eligible = [
        op for op in operations
        if op.get("witness") or op.get("witness_required") is True
    ]
    with_witness = [op for op in witness_eligible if op.get("witness")]

    active_cases = [
        case for case in case_items.values()
        if case.get("status") == "active"
    ]
    active_api_expectations = _load_active_api_expectation_ids(api_expectations_path)
    probes = _latest_negctl_probes(negctl_root, build_label=build_label, app=app)

    resolved_artifact_digest = current_artifact_content_digest
    if resolved_artifact_digest is None and app:
        state_root = (
            negctl_root.parents[1]
            if negctl_root is not None
            and negctl_root.name == "negctl"
            and negctl_root.parent.name == "runs"
            else run_root.parent
        )
        resolved_artifact_digest = artifact_content_digest_from_manifest(
            app, state_root=state_root, build_label=build_label,
        )

    app_dir = recipes_dir.parent if recipes_dir is not None else None
    symbol_binding_cache: dict[str, str | None] = {}

    def _probe_is_proven(probe: dict[str, Any]) -> bool:
        cid = str(probe.get("case_id") or "")
        if not cid:
            return False
        if uses_symbol_binding is not None:
            case_uses_symbols = bool(uses_symbol_binding)
        elif current_binding_sha256 is not None:
            case_uses_symbols = True
        else:
            case_uses_symbols = steps_use_symbol_binding(
                _current_recipe_steps(recipes_dir, cid),
                operations_by_name=operations_by_name,
            )
        if current_binding_sha256 is not None:
            case_binding: str | None = current_binding_sha256
        elif case_uses_symbols and app_dir is not None:
            cache_key = str(resolved_artifact_digest or "")
            if cache_key not in symbol_binding_cache:
                symbol_binding_cache[cache_key] = current_symbol_binding_sha256(
                    app_dir, app=app, artifact_content_digest=resolved_artifact_digest,
                )
            case_binding = symbol_binding_cache[cache_key]
        else:
            case_binding = None
        return (
            not discrimination_gaps(
                probe,
                current_build_label=build_label,
                current_artifact_content_digest=resolved_artifact_digest,
                current_binding_sha256=case_binding,
                uses_symbol_binding=case_uses_symbols,
            )
            and _probe_matches_current_recipe(probe, recipes_dir)
        )

    proven_case_ids = {
        str(probe.get("case_id"))
        for probe in probes
        if _probe_is_proven(probe)
    }
    covered_case_ids = {
        str(case.get("id"))
        for case in active_cases
        if str(case.get("id")) in active_api_expectations or str(case.get("id")) in proven_case_ids
    }

    flipped = [
        probe for probe in probes
        if probe.get("target_green") is True and probe.get("discriminator_flipped") is True
    ]

    events = overturns.load_overturns(run_root)
    row_pairs = {
        (str(row["run_id"]), str(row["case_id"]))
        for row in rows
    }
    relevant_events = [
        event for event in events
        if (str(event.get("run_id")), str(event.get("case_id"))) in row_pairs
    ]
    overturned_pairs = {
        (str(event.get("run_id")), str(event.get("case_id")))
        for event in relevant_events
    }
    passed_rows = [row for row in rows if row["status"] == "passed"]
    overturned_passed = [
        row for row in passed_rows
        if (str(row["run_id"]), str(row["case_id"])) in overturned_pairs
    ]

    return {
        "required_witness_coverage": {
            "eligible_operations": len(witness_eligible),
            "with_witness": len(with_witness),
            "rate": _rate(len(with_witness), len(witness_eligible)),
        },
        "discriminator_coverage": {
            "active_cases": len(active_cases),
            "covered_cases": len(covered_case_ids),
            "rate": _rate(len(covered_case_ids), len(active_cases)),
            "api_expectation_cases": len(active_api_expectations),
            "negctl_proven_cases": len(proven_case_ids),
        },
        "negative_control_flip_rate": {
            "probes": len(probes),
            "flipped": len(flipped),
            "rate": _rate(len(flipped), len(probes)),
        },
        "overturn_rate": {
            "passed_results": len(passed_rows),
            "overturned_passed_results": len(overturned_passed),
            "rate": _rate(len(overturned_passed), len(passed_rows)),
            "events": len(relevant_events),
        },
    }


def _version_boundary(prev: str | None, cur: str | None) -> bool:
    return bool(prev and cur and prev != cur)


def _flaky(window: list[dict[str, Any]]) -> tuple[bool, int]:
    signal = [entry for entry in reversed(window) if entry.get("status") in PASS_FAIL]
    if len(signal) < FLAKY_MIN_EXECUTIONS:
        return False, 0
    statuses = [str(entry["status"]) for entry in signal]
    if statuses.count("passed") < FLAKY_MIN_EACH or statuses.count("failed") < FLAKY_MIN_EACH:
        return False, 0
    flips = 0
    for prev, cur in zip(signal, signal[1:]):
        if prev.get("status") == cur.get("status"):
            continue
        if _version_boundary(prev.get("package_device_version"), cur.get("package_device_version")):
            continue
        flips += 1
    return flips >= FLAKY_MIN_FLIPS, flips


def _pass_rate(window: list[dict[str, Any]]) -> float | None:
    signal = [entry for entry in window if entry.get("status") in PASS_FAIL]
    if not signal:
        return None
    passed = sum(1 for entry in signal if entry.get("status") == "passed")
    return passed / len(signal)


def _cluster_id(detail_norm: str) -> str:
    digest = hashlib.sha1(detail_norm.encode("utf-8")).hexdigest()[:10]
    return f"cluster-{digest}"


def _cluster_rows(conn: sqlite3.Connection, app: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
          p.detail_norm,
          p.detail_raw,
          p.case_id,
          r.run_id,
          r.run_dir,
          r.package_device_version,
          r.ended_at,
          r.result_mtime_ns
        FROM case_planes p
        JOIN case_results c ON c.run_dir=p.run_dir AND c.case_id=p.case_id
        JOIN runs r ON r.run_dir=p.run_dir
        WHERE r.app=? AND r.type='regression' AND r.negative_control_id IS NULL
          AND c.status IN ('failed', 'error', 'blocked')
          AND COALESCE(p.detail_norm, '') != ''
        ORDER BY r.result_mtime_ns DESC
        """,
        (app,),
    ).fetchall()
    clusters: dict[str, dict[str, Any]] = {}
    for row in rows:
        detail = str(row["detail_norm"] or "")
        cid = _cluster_id(detail)
        cluster = clusters.setdefault(
            cid,
            {
                "cluster_id": cid,
                "signature": detail,
                "sample_detail": row["detail_raw"],
                "affected_cases": [],
                "runs": [],
                "first_seen": None,
                "last_seen": None,
            },
        )
        case_id = str(row["case_id"])
        if case_id not in cluster["affected_cases"]:
            cluster["affected_cases"].append(case_id)
        cluster["runs"].append(
            {
                "run_id": row["run_id"],
                "run_dir": row["run_dir"],
                "case_id": case_id,
                "ended_at": row["ended_at"],
                "package_device_version": row["package_device_version"],
            }
        )
        ended = row["ended_at"]
        if ended:
            if cluster["last_seen"] is None:
                cluster["last_seen"] = ended
            cluster["first_seen"] = ended
    return clusters


def app_health(
    run_root: Path,
    app: str,
    *,
    cases_path: Path | None = None,
    operations_path: Path | None = None,
    api_expectations_path: Path | None = None,
    negctl_root: Path | None = None,
    build_label: str | None = None,
    window_size: int = WINDOW_SIZE,
    recipes_dir: Path | None = None,
    current_artifact_content_digest: str | None = None,
    current_binding_sha256: str | None = None,
    uses_symbol_binding: bool | None = None,
) -> dict[str, Any]:
    run_index.ensure_current(run_root)
    case_items = _load_case_items(cases_path)
    with _connect_existing(run_root) as conn:
        rows = conn.execute(
            """
            SELECT
              c.case_id,
              c.status,
              c.blocker_scope,
              c.duration_ms,
              c.exec_via,
              c.revision,
              r.run_id,
              r.run_dir,
              r.package_device_version,
              r.package_active_version,
              r.ended_at,
              r.result_mtime_ns
            FROM case_results c
            JOIN runs r ON r.run_dir=c.run_dir
            WHERE r.app=? AND r.type='regression' AND r.negative_control_id IS NULL
            ORDER BY r.result_mtime_ns DESC
            """,
            (app,),
        ).fetchall()
        clusters = _cluster_rows(conn, app)

    by_case: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        cid = str(row["case_id"])
        if len(by_case.setdefault(cid, [])) >= window_size:
            continue
        by_case[cid].append(
            {
                "run_id": row["run_id"],
                "run_dir": row["run_dir"],
                "status": row["status"],
                "blocker_scope": row["blocker_scope"],
                "duration_ms": row["duration_ms"],
                "exec_via": row["exec_via"],
                "revision": row["revision"],
                "ended_at": row["ended_at"],
                "package_device_version": row["package_device_version"],
                "package_active_version": row["package_active_version"],
            }
        )

    case_cluster: dict[str, str] = {}
    for cid, cluster in clusters.items():
        for case_id in cluster["affected_cases"]:
            case_cluster.setdefault(case_id, cid)

    ids = sorted(set(case_items) | set(by_case))
    cases: list[dict[str, Any]] = []
    skipped_cases = 0
    blocked_cases = 0
    asset_blocked_cases = 0
    for cid in ids:
        current = case_items.get(cid) or {}
        window = by_case.get(cid, [])
        latest_status = window[0]["status"] if window else None
        if latest_status == "skipped":
            skipped_cases += 1
        if latest_status == "blocked":
            blocked_cases += 1
            if window[0].get("blocker_scope") == "asset":
                asset_blocked_cases += 1
        is_flaky, flips = _flaky(window)
        cases.append(
            {
                "case_id": cid,
                "item": current.get("item") or "",
                "case_status": current.get("status"),
                "window": window,
                "pass_rate": _pass_rate(window),
                "flaky": is_flaky,
                "flips": flips,
                "skipped_recent": latest_status == "skipped",
                "last_failed_run": next(
                    (
                        {
                            "run_id": entry["run_id"],
                            "run_dir": entry["run_dir"],
                            "status": entry["status"],
                            "ended_at": entry["ended_at"],
                        }
                        for entry in window
                        if entry.get("status") in {"failed", "error", "blocked"}
                    ),
                    None,
                ),
                "cluster_id": case_cluster.get(cid),
            }
        )

    return {
        "app": app,
        "cases": cases,
        "clusters": sorted(
            clusters.values(),
            key=lambda cluster: (len(cluster["affected_cases"]), len(cluster["runs"])),
            reverse=True,
        ),
        "debt": {
            "skipped_cases": skipped_cases,
            "blocked_cases": blocked_cases,
            "asset_blocked_cases": asset_blocked_cases,
        },
        "result_quality": _result_quality(
            run_root,
            case_items=case_items,
            rows=rows,
            operations_path=operations_path,
            api_expectations_path=api_expectations_path,
            negctl_root=negctl_root,
            build_label=build_label,
            app=app,
            recipes_dir=recipes_dir,
            current_artifact_content_digest=current_artifact_content_digest,
            current_binding_sha256=current_binding_sha256,
            uses_symbol_binding=uses_symbol_binding,
        ),
    }
