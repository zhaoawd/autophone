"""可运行率基线的 cohort 冻结与只读出数（BL-083，基础设施评审 §7.1 波次 A）。

本模块只读：不执行 Run，不写 Control Plane，也不写 Run 索引。

- cohort 冻结：按冻结说明（App、选择器、排除项、目标 Rig、轮次、run_id 模板）从 App 资产解析
  Case 列表，记录 cases/recipes/profile 文件的 sha256 与计划 run_id，输出
  `runnability-cohort/v1` 文件。
- 出数：按 cohort 的计划 run_id 集合读取结果。PostgreSQL 模式读 Control Plane 的
  `runs ⋈ assignments`（只读事务，不执行 schema 初始化）；兼容模式读 `index.sqlite` 的
  `runs ⋈ case_results`（只读连接），assignment_id 与 host_id 从 Run 目录的
  `platform-run-result.json` 补齐。两种来源共用同一分类与汇总代码。

口径（2026-10-11 编排会话采用，用户未另行说明）：
- eligible：冻结 cohort 中被选择器选中的 Case，按计划 Run 逐一计数（Case × 轮次）；
- started：结果为 passed / failed / error；passed 另计；
- blocked：结果为 blocked（含 scope=asset）；
- skipped、pending、missing（计划 Run 不存在、无结果或结果中无该 Case）与 other 另列。
  missing 行带 `absence`：`run_not_found`、`no_result`、`group_aborted`（Run 结果的 `groups[]`
  中该 Case 所在执行组为 aborted，组内 Case 不写入 `cases[]`）或 `not_in_result`。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from autophone.contracts import validate_or_raise

COHORT_SCHEMA_VERSION = "runnability-cohort/v1"
REPORT_SCHEMA_VERSION = "runnability-report/v1"
COHORT_SCHEMA_FILE = "runnability-cohort.v1.json"
REPORT_SCHEMA_FILE = "runnability-report.v1.json"

SOURCE_POSTGRES = "pg"
SOURCE_SQLITE = "sqlite"

STARTED_STATUSES = frozenset({"passed", "failed", "error"})
CATEGORIES = ("started", "blocked", "skipped", "pending", "missing", "other")
COUNT_KEYS = (
    "eligible", "started", "passed", "failed", "error",
    "blocked", "skipped", "pending", "missing", "other",
)
RUN_RESULT_FILE = "platform-run-result.json"
ABSENCE_VALUES = ("run_not_found", "no_result", "group_aborted", "not_in_result")


class RunnabilityError(ValueError):
    """cohort 或出数输入不成立；调用方据此拒绝，不输出部分结果。"""


def classify_case_status(status: str | None) -> str:
    """把单个 Case 结果归入一个出数类别。

    `None` 表示计划的 Case 没有结果（Run 不存在、无结果或结果里没有该 Case）。
    """
    if status is None:
        return "missing"
    if status in STARTED_STATUSES:
        return "started"
    if status in ("blocked", "skipped", "pending"):
        return status
    return "other"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── cohort 冻结 ───────────────────────────────────────────────────────────────


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def freeze_cohort(
    spec: Mapping[str, Any],
    *,
    asset_root: Path | None = None,
    frozen_at: str | None = None,
) -> dict[str, Any]:
    """按冻结说明解析 Case 列表并记录资产哈希与计划 run_id。

    冻结说明字段：`cohort_id`、`groups[]`（`app`、`selector`、可选 `exclude_ids`、
    `target_rig`、`rounds`、`run_id_template`、可选 `golden_path`）、可选 `notes`。
    `run_id_template` 可用 `{rig}`、`{app}`、`{round}` 三个占位符。
    """
    from autophone.assets.apps import ROOT, load_app_profile
    from autophone.execution.case_selection import canonical_selector, select_cases
    from autophone.execution.engine.cases_loader import load_cases

    root = asset_root or ROOT
    cohort_id = str(spec.get("cohort_id") or "").strip()
    if not cohort_id:
        raise RunnabilityError("cohort_id is required")
    raw_groups = spec.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise RunnabilityError("groups must be a non-empty list")
    groups: list[dict[str, Any]] = []
    seen_run_ids: set[str] = set()
    for index, raw in enumerate(raw_groups):
        if not isinstance(raw, Mapping):
            raise RunnabilityError(f"groups[{index}] must be an object")
        app = str(raw.get("app") or "").strip()
        rig = str(raw.get("target_rig") or "").strip()
        template = str(raw.get("run_id_template") or "").strip()
        rounds = raw.get("rounds")
        if not app or not rig or not template:
            raise RunnabilityError(f"groups[{index}] needs app, target_rig and run_id_template")
        if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 1:
            raise RunnabilityError(f"groups[{index}].rounds must be a positive integer")
        if "{round}" not in template:
            raise RunnabilityError(f"groups[{index}].run_id_template must contain {{round}}")
        profile = load_app_profile(app, root=root)
        cases_path = profile.cases_path()
        selector = canonical_selector(raw.get("selector") or {})
        exclude = sorted({str(i) for i in raw.get("exclude_ids") or []})
        selected = [c for c in select_cases(load_cases(cases_path), selector)
                    if str(c.get("id") or "") not in exclude]
        case_ids = [str(c["id"]) for c in selected]
        if not case_ids:
            raise RunnabilityError(f"groups[{index}] selects no Case")
        digests: dict[str, str] = {
            _relative(profile.profile_dir / "profile.yaml", root):
                sha256_file(profile.profile_dir / "profile.yaml"),
            _relative(cases_path, root): sha256_file(cases_path),
        }
        recipes: dict[str, str | None] = {}
        for case_id in case_ids:
            recipe = profile.recipes_dir() / f"{case_id.lower()}.yaml"
            recipes[case_id] = _relative(recipe, root) if recipe.exists() else None
            if recipe.exists():
                digests[_relative(recipe, root)] = sha256_file(recipe)
        golden_path = str(raw.get("golden_path") or "").strip() or None
        if golden_path:
            golden_file = profile.profile_dir / "golden_paths.yaml"
            if not golden_file.exists():
                raise RunnabilityError(f"groups[{index}]: golden_paths.yaml not found for {app}")
            digests[_relative(golden_file, root)] = sha256_file(golden_file)
        runs = []
        for round_no in range(1, rounds + 1):
            run_id = template.format(rig=rig, app=app, round=round_no)
            if run_id in seen_run_ids:
                raise RunnabilityError(f"duplicate planned run_id: {run_id}")
            seen_run_ids.add(run_id)
            runs.append({"round": round_no, "run_id": run_id})
        groups.append({
            "app": app,
            "target_rig": rig,
            "rounds": rounds,
            "selector": selector,
            "exclude_ids": exclude,
            "golden_path": golden_path,
            "case_ids": case_ids,
            "submit_selector": {"ids": case_ids},
            "recipes": recipes,
            "asset_sha256": dict(sorted(digests.items())),
            "runs": runs,
        })
    cohort = {
        "schema_version": COHORT_SCHEMA_VERSION,
        "cohort_id": cohort_id,
        "frozen_at": frozen_at or _now(),
        "notes": str(spec.get("notes") or ""),
        "groups": groups,
    }
    validate_or_raise(COHORT_SCHEMA_FILE, cohort)
    return cohort


def load_cohort(path: Path) -> tuple[dict[str, Any], str]:
    """读取并校验 cohort 文件，返回内容与文件 sha256。"""
    try:
        cohort = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunnabilityError(f"cannot read cohort {path}: {exc}") from exc
    if not isinstance(cohort, dict):
        raise RunnabilityError("cohort must be a JSON object")
    validate_or_raise(COHORT_SCHEMA_FILE, cohort)
    run_ids = [run["run_id"] for group in cohort["groups"] for run in group["runs"]]
    if len(run_ids) != len(set(run_ids)):
        raise RunnabilityError("cohort lists a planned run_id more than once")
    return cohort, sha256_file(path)


def planned_run_ids(cohort: Mapping[str, Any]) -> list[str]:
    return [run["run_id"] for group in cohort["groups"] for run in group["runs"]]


# ── 结果读取 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    status: str | None
    blocker_scope: str | None = None
    blocker_code: str | None = None


@dataclass(frozen=True)
class RunRecord:
    """一个计划 Run 在某个来源中的事实；两种来源都归一到本结构。"""

    run_id: str
    assignment_id: str | None
    host_id: str | None
    rig_id: str | None
    run_status: str | None
    cases: tuple[CaseResult, ...] = field(default_factory=tuple)
    has_result: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)
    # 结果 `groups[]` 中 status=aborted 的执行组：case_id → 组 label。
    aborted_groups: Mapping[str, str] = field(default_factory=dict)


def _cases_from_result(result: Mapping[str, Any] | None) -> tuple[CaseResult, ...]:
    out = []
    for case in (result or {}).get("cases") or []:
        if not isinstance(case, Mapping):
            continue
        case_id = str(case.get("id") or "")
        if not case_id:
            continue
        blocker = case.get("blocker") if isinstance(case.get("blocker"), Mapping) else {}
        out.append(CaseResult(
            case_id=case_id,
            status=(str(case["status"]) if case.get("status") is not None else None),
            blocker_scope=blocker.get("scope"),
            blocker_code=blocker.get("code"),
        ))
    return tuple(out)


def _aborted_groups(result: Mapping[str, Any] | None) -> dict[str, str]:
    """Run 结果里 status=aborted 的执行组成员；这些 Case 不出现在 `cases[]` 中。"""
    out: dict[str, str] = {}
    for group in (result or {}).get("groups") or []:
        if not isinstance(group, Mapping) or group.get("status") != "aborted":
            continue
        label = str(group.get("label") or "")
        for case_id in group.get("ids") or []:
            out[str(case_id)] = label
    return out


def fetch_postgres_runs(dsn: str, schema: str, run_ids: Sequence[str]) -> list[RunRecord]:
    """在只读事务里读取计划 Run 的 `runs ⋈ assignments` 行。

    不调用 `PostgresControlStore.initialize()`：初始化会执行 DDL，与只读出数相冲突。
    """
    from autophone.control._psycopg import require_psycopg_components

    psycopg, sql, dict_row = require_psycopg_components()

    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        connection.read_only = True
        with connection.transaction():
            connection.execute(
                sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema))
            )
            rows = connection.execute(
                """
                SELECT r.run_id, r.assignment_id, r.status AS run_status, r.result,
                       a.status AS assignment_status, a.bound_host_id, a.bound_rig_id,
                       a.target_rig_id
                FROM runs r JOIN assignments a ON a.assignment_id = r.assignment_id
                WHERE r.run_id = ANY(%s)
                ORDER BY r.run_id
                """,
                (list(run_ids),),
            ).fetchall()
    records = []
    for row in rows:
        result = row["result"] if isinstance(row["result"], Mapping) else None
        notes = []
        if row["bound_rig_id"] is None:
            notes.append("assignment_not_bound")
        records.append(RunRecord(
            run_id=row["run_id"],
            assignment_id=row["assignment_id"],
            host_id=row["bound_host_id"],
            rig_id=row["bound_rig_id"] or row["target_rig_id"],
            run_status=row["run_status"],
            cases=_cases_from_result(result),
            has_result=result is not None,
            notes=tuple(notes),
            aborted_groups=_aborted_groups(result),
        ))
    return records


def _sqlite_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise RunnabilityError(f"run index not found: {path}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30.0)
    connection.row_factory = sqlite3.Row
    return connection


def fetch_sqlite_runs(run_root: Path, run_ids: Sequence[str]) -> list[RunRecord]:
    """从 `index.sqlite` 读取计划 Run 的 `runs ⋈ case_results`（只读连接）。

    `index.sqlite` 的 `runs` 表没有 assignment_id 与 host_id，这两项读 Run 目录里
    `platform-run-result.json` 的 `world_identity`。索引行比结果文件旧时记 `index_row_stale`。
    """
    from autophone.application.run_index import db_path

    if not run_ids:
        return []
    placeholders = ",".join("?" for _ in run_ids)
    connection = _sqlite_readonly(db_path(run_root))
    try:
        run_rows = connection.execute(
            f"SELECT run_dir, run_id, device, status, result_mtime_ns FROM runs "
            f"WHERE run_id IN ({placeholders}) ORDER BY run_id, run_dir",
            tuple(run_ids),
        ).fetchall()
        case_rows = connection.execute(
            f"SELECT c.run_dir, c.case_id, c.status, c.blocker_scope, c.blocker_code "
            f"FROM case_results c JOIN runs r ON r.run_dir = c.run_dir "
            f"WHERE r.run_id IN ({placeholders}) ORDER BY c.run_dir, c.case_id",
            tuple(run_ids),
        ).fetchall()
    finally:
        connection.close()
    cases_by_dir: dict[str, list[CaseResult]] = {}
    for row in case_rows:
        cases_by_dir.setdefault(row["run_dir"], []).append(CaseResult(
            case_id=row["case_id"], status=row["status"],
            blocker_scope=row["blocker_scope"], blocker_code=row["blocker_code"],
        ))
    dirs_by_run: dict[str, list[sqlite3.Row]] = {}
    for row in run_rows:
        dirs_by_run.setdefault(row["run_id"], []).append(row)
    records = []
    for run_id, rows in sorted(dirs_by_run.items()):
        notes: list[str] = []
        if len(rows) > 1:
            notes.append("duplicate_run_dirs:" + ",".join(r["run_dir"] for r in rows))
        row = max(rows, key=lambda r: r["result_mtime_ns"] or 0)
        result_path = run_root / row["run_dir"] / RUN_RESULT_FILE
        world: Mapping[str, Any] = {}
        result: Mapping[str, Any] = {}
        try:
            stat = result_path.stat()
            if row["result_mtime_ns"] is not None and stat.st_mtime_ns != row["result_mtime_ns"]:
                notes.append("index_row_stale")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            world = result.get("world_identity") or {}
        except (OSError, ValueError):
            notes.append("result_file_unreadable")
        binding = world.get("assignment_binding") or {}
        vm_ref = world.get("vm_instance_ref") or {}
        records.append(RunRecord(
            run_id=run_id,
            assignment_id=binding.get("assignment_id"),
            host_id=vm_ref.get("host_id"),
            rig_id=binding.get("rig_id") or vm_ref.get("rig_id") or row["device"],
            run_status=row["status"],
            cases=tuple(cases_by_dir.get(row["run_dir"], [])),
            has_result=True,
            notes=tuple(notes),
            aborted_groups=_aborted_groups(result),
        ))
    return records


# ── 汇总 ─────────────────────────────────────────────────────────────────────


def _empty_counts() -> dict[str, int]:
    return {key: 0 for key in COUNT_KEYS}


def _add(counts: dict[str, int], row: Mapping[str, Any]) -> None:
    counts["eligible"] += 1
    counts[row["category"]] += 1
    if row["status"] in ("passed", "failed", "error"):
        counts[row["status"]] += 1


def _rates(counts: Mapping[str, int]) -> dict[str, float | None]:
    def ratio(num: int, den: int) -> float | None:
        return round(num / den, 4) if den else None

    return {
        "started_of_eligible": ratio(counts["started"], counts["eligible"]),
        "passed_of_started": ratio(counts["passed"], counts["started"]),
        "passed_of_eligible": ratio(counts["passed"], counts["eligible"]),
        "blocked_of_eligible": ratio(counts["blocked"], counts["eligible"]),
    }


def _absence(record: RunRecord | None, case: CaseResult | None, case_id: str) -> str | None:
    if case is not None:
        return None
    if record is None:
        return "run_not_found"
    if not record.has_result:
        return "no_result"
    if case_id in record.aborted_groups:
        return "group_aborted"
    return "not_in_result"


def build_report(
    cohort: Mapping[str, Any],
    cohort_sha256: str,
    records: Iterable[RunRecord],
    *,
    source: str,
    source_detail: Mapping[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """把计划 Run 与读取到的事实对齐为逐 Case 行和四口径汇总。"""
    by_run = {record.run_id: record for record in records}
    rows: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    unexpected: list[dict[str, Any]] = []
    for group in cohort["groups"]:
        app = group["app"]
        cohort_cases = list(group["case_ids"])
        for planned in group["runs"]:
            record = by_run.get(planned["run_id"])
            results = {case.case_id: case for case in (record.cases if record else ())}
            runs.append({
                "app": app,
                "round": planned["round"],
                "run_id": planned["run_id"],
                "planned_rig": group["target_rig"],
                "found": record is not None,
                "has_result": bool(record and record.has_result),
                "assignment_id": record.assignment_id if record else None,
                "host_id": record.host_id if record else None,
                "rig_id": record.rig_id if record else None,
                "run_status": record.run_status if record else None,
                "notes": list(record.notes) if record else [],
            })
            for case_id in cohort_cases:
                case = results.get(case_id)
                status = case.status if case else None
                rows.append({
                    "app": app,
                    "round": planned["round"],
                    "case_id": case_id,
                    "host_id": record.host_id if record else None,
                    "rig_id": record.rig_id if record else None,
                    "planned_rig": group["target_rig"],
                    "assignment_id": record.assignment_id if record else None,
                    "run_id": planned["run_id"],
                    "run_status": record.run_status if record else None,
                    "status": status,
                    "category": classify_case_status(status),
                    "blocker_scope": case.blocker_scope if case else None,
                    "blocker_code": case.blocker_code if case else None,
                    "absence": _absence(record, case, case_id),
                    "aborted_group": (
                        record.aborted_groups.get(case_id) if record and case is None else None
                    ),
                })
            for case_id in sorted(set(results) - set(cohort_cases)):
                unexpected.append({
                    "run_id": planned["run_id"], "case_id": case_id,
                    "status": results[case_id].status,
                })
    summary = _empty_counts()
    by_machine: dict[tuple[Any, Any], dict[str, int]] = {}
    by_app: dict[str, dict[str, int]] = {}
    by_round: dict[tuple[str, int], dict[str, int]] = {}
    by_case: dict[tuple[str, str], dict[str, int]] = {}
    blockers: dict[tuple[Any, ...], dict[str, Any]] = {}
    missing: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        _add(summary, row)
        _add(by_machine.setdefault((row["host_id"], row["rig_id"]), _empty_counts()), row)
        _add(by_app.setdefault(row["app"], _empty_counts()), row)
        _add(by_round.setdefault((row["app"], row["round"]), _empty_counts()), row)
        _add(by_case.setdefault((row["app"], row["case_id"]), _empty_counts()), row)
        if row["blocker_code"] or row["category"] == "blocked":
            key = (row["category"], row["blocker_scope"], row["blocker_code"],
                   row["host_id"], row["rig_id"])
            entry = blockers.setdefault(key, {
                "category": row["category"],
                "blocker_scope": row["blocker_scope"],
                "blocker_code": row["blocker_code"],
                "host_id": row["host_id"],
                "rig_id": row["rig_id"],
                "count": 0,
                "case_ids": [],
                "run_ids": [],
                "assignment_ids": [],
            })
            entry["count"] += 1
            for name, value in (("case_ids", row["case_id"]), ("run_ids", row["run_id"]),
                                ("assignment_ids", row["assignment_id"])):
                if value is not None and value not in entry[name]:
                    entry[name].append(value)
        if row["category"] == "missing":
            key = (row["absence"], row["aborted_group"], row["run_status"],
                   row["host_id"], row["rig_id"])
            entry = missing.setdefault(key, {
                "absence": row["absence"],
                "aborted_group": row["aborted_group"],
                "run_status": row["run_status"],
                "host_id": row["host_id"],
                "rig_id": row["rig_id"],
                "count": 0,
                "case_ids": [],
                "run_ids": [],
                "assignment_ids": [],
            })
            entry["count"] += 1
            for name, value in (("case_ids", row["case_id"]), ("run_ids", row["run_id"]),
                                ("assignment_ids", row["assignment_id"])):
                if value is not None and value not in entry[name]:
                    entry[name].append(value)

    def keyed(items: Mapping[Any, dict[str, int]], names: Sequence[str]) -> list[dict[str, Any]]:
        out = []
        for key, counts in sorted(items.items(), key=lambda kv: tuple(str(k) for k in (
                kv[0] if isinstance(kv[0], tuple) else (kv[0],)))):
            values = key if isinstance(key, tuple) else (key,)
            out.append({**dict(zip(names, values)), "counts": counts, "rates": _rates(counts)})
        return out

    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": generated_at or _now(),
        "source": source,
        "source_detail": dict(source_detail or {}),
        "cohort": {
            "cohort_id": cohort["cohort_id"],
            "sha256": cohort_sha256,
            "frozen_at": cohort["frozen_at"],
            "planned_runs": len(runs),
        },
        "definitions": {
            "eligible": "cohort Case selected by the frozen selector, counted once per planned Run",
            "started": "Case result passed, failed or error",
            "passed": "Case result passed",
            "blocked": "Case result blocked, any blocker scope including asset",
            "separate": "skipped, pending, missing (no Run, no result or Case absent) and other",
            "absence": "why a missing row has no Case result: run_not_found, no_result, "
                       "group_aborted (its execution group in the Run result is aborted) or not_in_result",
        },
        "summary": {"counts": summary, "rates": _rates(summary)},
        "by_machine": keyed(by_machine, ("host_id", "rig_id")),
        "by_app": keyed(by_app, ("app",)),
        "by_round": keyed(by_round, ("app", "round")),
        "by_case": keyed(by_case, ("app", "case_id")),
        "blocker_codes": sorted(
            blockers.values(),
            key=lambda e: (-e["count"], str(e["blocker_code"]), str(e["rig_id"])),
        ),
        "missing_reasons": sorted(
            missing.values(),
            key=lambda e: (-e["count"], str(e["absence"]), str(e["rig_id"])),
        ),
        "runs": runs,
        "rows": rows,
        "unexpected_cases": unexpected,
    }
    validate_or_raise(REPORT_SCHEMA_FILE, report)
    return report


def runnability_report(
    cohort_path: Path,
    *,
    source: str,
    dsn: str | None = None,
    schema: str = "autophone",
    run_root: Path | None = None,
) -> dict[str, Any]:
    """读取 cohort 并从指定来源出数。"""
    cohort, digest = load_cohort(cohort_path)
    run_ids = planned_run_ids(cohort)
    if source == SOURCE_POSTGRES:
        if not dsn:
            raise RunnabilityError("PostgreSQL source requires AUTOPHONE_DATABASE_URL")
        records = fetch_postgres_runs(dsn, schema, run_ids)
        detail = {"schema": schema, "transaction": "read_only"}
    elif source == SOURCE_SQLITE:
        if run_root is None:
            raise RunnabilityError("SQLite source requires a run root")
        records = fetch_sqlite_runs(run_root, run_ids)
        detail = {"run_root": str(run_root), "connection": "mode=ro"}
    else:
        raise RunnabilityError(f"unknown source: {source}")
    return build_report(cohort, digest, records, source=source, source_detail=detail)
