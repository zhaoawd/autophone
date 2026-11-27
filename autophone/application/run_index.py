"""Run 历史事实的 SQLite 查询投影与索引维护。

Run 结果文件是输入真源；本模块提供查询与索引重建，不执行 Run，也不决定转正策略。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autophone.execution.case_selection import selector_key
from autophone.util import read_json

INDEX_NAME = "index.sqlite"
_DETAIL_COORD = re.compile(r"\b\d+(?:\.\d+)?,\s*\d+(?:\.\d+)?\b")
_DETAIL_ATTEMPT = re.compile(r"\s*\(attempt\s+\d+/\d+\)", re.I)
_DETAIL_TS = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b")
_DETAIL_NUMBER = re.compile(r"\b\d{6,}\b")


@dataclass(frozen=True)
class RunQuery:
    """A filesystem-independent query over settled run facts.

    ``candidate_limit`` intentionally applies before app/type filters.  A few
    legacy callers define "latest" as "inspect the newest N runs globally";
    keeping that detail here prevents each caller from reimplementing it.
    ``limit`` applies after filters.
    """

    app: str | None = None
    types: frozenset[str] | None = None
    candidate_limit: int | None = None
    limit: int | None = None
    newest_first: bool = True


@dataclass(frozen=True)
class RunFact:
    run_dir: Path
    result: dict[str, Any]
    mtime_ns: int


@dataclass(frozen=True)
class PromotionSubject:
    """Identity used to read promotion evidence without owning gate policy."""

    kind: str
    asset_id: str | None = None
    app: str | None = None
    revision: int | None = None


def db_path(run_root: Path) -> Path:
    return run_root / INDEX_NAME


def normalize_detail(detail: Any) -> str:
    text = str(detail or "")
    text = _DETAIL_ATTEMPT.sub("", text)
    text = _DETAIL_TS.sub("<ts>", text)
    text = _DETAIL_COORD.sub("<xy>", text)
    text = _DETAIL_NUMBER.sub("<num>", text)
    return re.sub(r"\s+", " ", text).strip()


# 两台 VM 各自的 web/CLI 进程共用同一个 runs/platform/index.sqlite（路径按 run_root 而非
# rig 划分）。写入按 run_dir 主键 upsert，记录之间不会串；争用只表现为锁等待。默认
# rollback journal 下写者会阻塞读者，`rebuild` 一次长事务超过 sqlite3 默认 5s 就让另一
# 个进程报 database is locked。WAL 让读写并行，busy_timeout 把等待上限抬到一次 rebuild
# 的量级。journal_mode 切换在只读/网络文件系统上可能不生效，所以不把返回值当断言。
BUSY_TIMEOUT_S = 30.0


def connect(run_root: Path, *, init: bool = True) -> sqlite3.Connection:
    """Open the shared run index with the concurrency pragmas every process needs."""
    run_root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(run_root), timeout=BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_S * 1000)}")
    if init:
        _init(conn)
    return conn


def _connect(run_root: Path) -> sqlite3.Connection:
    return connect(run_root)


def _init(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs(
          run_dir TEXT PRIMARY KEY,
          run_id TEXT,
          day TEXT,
          app TEXT,
          device TEXT,
          type TEXT,
          status TEXT,
          package_active_slot TEXT,
          package_active_version TEXT,
          package_active_sha256 TEXT,
          package_device_version TEXT,
          package_build_id TEXT,
          package_short_version TEXT,
          package_build_version TEXT,
          package_artifact_sha256 TEXT,
          product_source_commit TEXT,
          product_source_tree_sha256 TEXT,
          build_identity_state TEXT,
          subject_build_key TEXT,
          identity_state TEXT,
          identity_source TEXT,
          profile_build_label TEXT,
          schedule_id TEXT,
          gate_id TEXT,
          selector_json TEXT,
          selector_key TEXT,
          queued_at TEXT,
          ended_at TEXT,
          result_mtime_ns INTEGER,
          result_size INTEGER,
          negative_control_id TEXT
        );

        CREATE TABLE IF NOT EXISTS run_closures(
          run_dir TEXT PRIMARY KEY,
          projection_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS run_recoveries(
          run_id TEXT NOT NULL,
          recovery_id TEXT NOT NULL,
          version INTEGER NOT NULL,
          projection_json TEXT NOT NULL,
          PRIMARY KEY(run_id, recovery_id)
        );

        CREATE TABLE IF NOT EXISTS case_results(
          run_dir TEXT,
          case_id TEXT,
          status TEXT,
          blocker_scope TEXT,
          blocker_code TEXT,
          duration_ms REAL,
          exec_via TEXT,
          revision INTEGER,
          PRIMARY KEY(run_dir, case_id)
        );
        CREATE TABLE IF NOT EXISTS case_planes(
          run_dir TEXT,
          case_id TEXT,
          plane TEXT,
          verdict TEXT,
          detail_norm TEXT,
          detail_raw TEXT,
          PRIMARY KEY(run_dir, case_id, plane)
        );
        CREATE TABLE IF NOT EXISTS metadata(
          key TEXT PRIMARY KEY,
          value TEXT
        );
        CREATE TABLE IF NOT EXISTS knowledge_observations(
          run_dir TEXT,
          observation_id TEXT,
          type TEXT,
          case_id TEXT,
          operation TEXT,
          action_id TEXT,
          subject_epoch_ref TEXT,
          subject_build_key TEXT,
          occurred_at TEXT,
          producer_kind TEXT,
          correlation_state TEXT,
          payload_json TEXT,
          PRIMARY KEY(run_dir, observation_id)
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_endpoint
          ON knowledge_observations(type, operation, case_id, subject_build_key);
        """
    )
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(case_results)")}
    for name in ("blocker_scope", "blocker_code"):
        if name not in existing:
            _add_column(conn, "case_results", name)
    # Build identity columns are derived from the run results, so an existing
    # index gains them empty and is refilled by a rebuild — never backfilled
    # from the current manifest, which no longer describes that run.
    run_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(runs)")}
    for name in (
        "package_build_id", "package_short_version", "package_build_version",
        "package_artifact_sha256", "package_active_sha256",
        "product_source_commit", "product_source_tree_sha256",
        "build_identity_state", "subject_build_key",
        "identity_state", "identity_source",
    ):
        if name not in run_columns:
            _add_column(conn, "runs", name)
    # 区分力反向 Run 的声明 id（run-spec.negative_control.control_id）。只补列、不回填：
    # 补列前写入的行来自不支持反向 Run 的代码，NULL 即正确值；也不改 result_mtime_ns——
    # 结果文件已清理的历史行不会再被 ensure_current 重写，改动时间戳会永久打乱排序与
    # 「最新 Run」判定。
    if "negative_control_id" not in run_columns:
        _add_column(conn, "runs", "negative_control_id")
    # runs 上的索引一律在补列之后建：老索引库缺列时，DDL 脚本里的 CREATE INDEX
    # 会先于 ALTER TABLE 抛 "no such column"，整个 _init 打不开库。
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_runs_run_id ON runs(run_id);
        CREATE INDEX IF NOT EXISTS idx_runs_app_type ON runs(app, type);
        CREATE INDEX IF NOT EXISTS idx_runs_selector ON runs(app, selector_key, package_device_version);
        CREATE INDEX IF NOT EXISTS idx_runs_build ON runs(app, package_build_id);
        CREATE INDEX IF NOT EXISTS idx_runs_same_build_pass
          ON runs(app, selector_key, subject_build_key, status, result_mtime_ns);
        """
    )


def _add_column(conn: sqlite3.Connection, table: str, name: str) -> None:
    """补一列 TEXT；另一进程在本进程读 table_info 之后已补上时视为完成。

    多个进程（看板、CLI、Rig Agent）可能同时首次打开同一个老索引，都判定缺列并各自
    ALTER TABLE，后到者得到 ``duplicate column name``。"""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} TEXT")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def _rel_run_dir(run_dir: Path, run_root: Path) -> str:
    try:
        return run_dir.resolve().relative_to(run_root.resolve()).as_posix()
    except ValueError:
        return run_dir.as_posix()


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO metadata(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def ingest_recoveries(run_id: str, recoveries: list[dict[str, Any]], *, run_root: Path) -> None:
    """Cache PostgreSQL projections without allowing older versions to replace newer ones."""
    with _connect(run_root) as conn:
        for row in recoveries:
            conn.execute('INSERT INTO run_recoveries(run_id,recovery_id,version,projection_json) VALUES(?,?,?,?) '
                         'ON CONFLICT(run_id,recovery_id) DO UPDATE SET version=excluded.version, '
                         'projection_json=excluded.projection_json WHERE excluded.version > run_recoveries.version',
                         (run_id, row['recovery_id'], row['version'], json.dumps(row, sort_keys=True)))


def ingest_closure(run_dir: Path, *, run_root: Path) -> None:
    from autophone.execution.closure_journal import ClosureJournal
    projection = ClosureJournal(run_dir).projection()
    with _connect(run_root) as conn:
        rel = _rel_run_dir(run_dir, run_root)
        if projection is None:
            conn.execute('DELETE FROM run_closures WHERE run_dir=?', (rel,))
        else:
            conn.execute('INSERT INTO run_closures(run_dir, projection_json) VALUES(?, ?) '
                         'ON CONFLICT(run_dir) DO UPDATE SET projection_json=excluded.projection_json',
                         (rel, json.dumps(projection, sort_keys=True)))


def negative_control_id(spec: Any) -> str | None:
    """区分力反向 Run 的声明 id；普通 Run 为 None。

    反向 Run 执行扰动后的 Recipe，它的失败是负对照的预期结果：不进入用例健康统计、
    包闸基线、Draft Trial 证据与回归裁决。判据只看 run-spec 的 ``negative_control``。
    """
    request = spec.get("negative_control") if isinstance(spec, dict) else None
    if request is None:
        return None
    return str((request or {}).get("control_id") or "") or "<unknown>"


def is_negative_control_run(run_dir: Path) -> bool:
    return negative_control_id(read_json(run_dir / "run-spec.json") or {}) is not None


def ingest_run(run_dir: Path, *, run_root: Path) -> None:
    ingest_closure(run_dir, run_root=run_root)
    result_path = run_dir / "platform-run-result.json"
    spec_path = run_dir / "run-spec.json"
    stat = result_path.stat()
    result = read_json(result_path) or {}
    spec = read_json(spec_path) or {}
    rel = _rel_run_dir(run_dir, run_root)
    origin = spec.get("origin") or {}
    selector = spec.get("case_selector") or {}
    package = result.get("package") or {}
    build_ref = package.get("registry_build_ref") or {}
    source_revision = build_ref.get("source_revision") or {}
    try:
        identity = read_json(run_dir / "build-identity.json") or {}
    except (OSError, ValueError, TypeError):
        # Build Identity 损坏时仅降级为 unknown，不阻断 Run 索引收口。
        identity = {}
    timestamps = result.get("timestamps") or {}
    with _connect(run_root) as conn:
        try:
            conn.execute(
                """
                INSERT INTO runs(
                  run_dir, run_id, day, app, device, type, status,
                  package_active_slot, package_active_version, package_active_sha256,
                  package_device_version,
                  package_build_id, package_short_version, package_build_version,
                  package_artifact_sha256, product_source_commit,
                  product_source_tree_sha256, build_identity_state,
                  subject_build_key, identity_state, identity_source,
                  profile_build_label, schedule_id, gate_id, selector_json, selector_key,
                  queued_at, ended_at, result_mtime_ns, result_size, negative_control_id
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_dir) DO UPDATE SET
                  run_id=excluded.run_id,
                  day=excluded.day,
                  app=excluded.app,
                  device=excluded.device,
                  type=excluded.type,
                  status=excluded.status,
                  package_active_slot=excluded.package_active_slot,
                  package_active_version=excluded.package_active_version,
                  package_active_sha256=excluded.package_active_sha256,
                  package_device_version=excluded.package_device_version,
                  package_build_id=excluded.package_build_id,
                  package_short_version=excluded.package_short_version,
                  package_build_version=excluded.package_build_version,
                  package_artifact_sha256=excluded.package_artifact_sha256,
                  product_source_commit=excluded.product_source_commit,
                  product_source_tree_sha256=excluded.product_source_tree_sha256,
                  build_identity_state=excluded.build_identity_state,
                  subject_build_key=excluded.subject_build_key,
                  identity_state=excluded.identity_state,
                  identity_source=excluded.identity_source,
                  profile_build_label=excluded.profile_build_label,
                  schedule_id=excluded.schedule_id,
                  gate_id=excluded.gate_id,
                  selector_json=excluded.selector_json,
                  selector_key=excluded.selector_key,
                  queued_at=excluded.queued_at,
                  ended_at=excluded.ended_at,
                  result_mtime_ns=excluded.result_mtime_ns,
                  result_size=excluded.result_size,
                  negative_control_id=excluded.negative_control_id
                """,
                (
                    rel,
                    result.get("run_id") or spec.get("run_id") or run_dir.name,
                    run_dir.parent.name,
                    result.get("app") or spec.get("app"),
                    result.get("device") or spec.get("device"),
                    result.get("type") or spec.get("type"),
                    result.get("status"),
                    package.get("default_slot", package.get("active_slot")),
                    package.get("default_version", package.get("active_version")),
                    package.get("default_sha256", package.get("active_sha256")),
                    package.get("device_version"),
                    build_ref.get("build_id"),
                    build_ref.get("short_version"),
                    build_ref.get("build_version"),
                    build_ref.get("artifact_sha256"),
                    source_revision.get("commit_sha"),
                    source_revision.get("tree_sha256"),
                    package.get("build_identity_match"),
                    identity.get("subject_build_key"),
                    identity.get("identity_state"),
                    identity.get("identity_source"),
                    package.get("profile_build_label"),
                    origin.get("schedule_id"),
                    origin.get("gate_id"),
                    json.dumps(selector, ensure_ascii=False, sort_keys=False),
                    selector_key(selector),
                    timestamps.get("queued_at"),
                    timestamps.get("collect_ended_at"),
                    stat.st_mtime_ns,
                    stat.st_size,
                    negative_control_id(spec),
                ),
            )
            conn.execute("DELETE FROM case_results WHERE run_dir=?", (rel,))
            conn.execute("DELETE FROM case_planes WHERE run_dir=?", (rel,))
            conn.execute("DELETE FROM knowledge_observations WHERE run_dir=?", (rel,))
            for case in result.get("cases") or []:
                cid = str(case.get("id") or "")
                if not cid:
                    continue
                conn.execute(
                    """
                    INSERT INTO case_results(
                      run_dir, case_id, status, blocker_scope, blocker_code, duration_ms, exec_via, revision
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rel,
                        cid,
                        case.get("status"),
                        (case.get("blocker") or {}).get("scope"),
                        (case.get("blocker") or {}).get("code"),
                        case.get("duration_ms"),
                        case.get("exec_via"),
                        case.get("revision"),
                    ),
                )
                for plane, plane_result in (case.get("planes") or {}).items():
                    detail = (plane_result or {}).get("detail")
                    conn.execute(
                        """
                        INSERT INTO case_planes(run_dir, case_id, plane, verdict, detail_norm, detail_raw)
                        VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (
                            rel,
                            cid,
                            str(plane),
                            (plane_result or {}).get("verdict"),
                            normalize_detail(detail),
                            detail,
                        ),
                    )
            observation_doc = read_json(run_dir / "observations.json") or {}
            for observation in observation_doc.get("observations") or []:
                scope = observation.get("build_scope") or {}
                producer = observation.get("producer") or {}
                conn.execute(
                    """
                    INSERT INTO knowledge_observations(
                      run_dir, observation_id, type, case_id, operation, action_id,
                      subject_epoch_ref, subject_build_key, occurred_at, producer_kind,
                      correlation_state, payload_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rel,
                        observation.get("id"),
                        observation.get("type"),
                        observation.get("case_id"),
                        observation.get("operation"),
                        observation.get("action_id"),
                        observation.get("subject_epoch_ref"),
                        scope.get("subject_build_key"),
                        observation.get("occurred_at"),
                        producer.get("kind"),
                        observation.get("correlation_state"),
                        json.dumps(observation.get("payload") or {}, ensure_ascii=False),
                    ),
                )
            _set_meta(conn, "last_ingest_error", "")
        except Exception as exc:
            _set_meta(conn, "last_ingest_error", f"{type(exc).__name__}: {exc}")
            raise


def _stale_result_paths(conn: sqlite3.Connection, run_root: Path) -> list[Path]:
    stale: list[Path] = []
    for path in run_root.glob("*/*/platform-run-result.json"):
        try:
            stat = path.stat()
        except OSError:
            continue
        rel = _rel_run_dir(path.parent, run_root)
        row = conn.execute(
            "SELECT result_mtime_ns, result_size FROM runs WHERE run_dir=?",
            (rel,),
        ).fetchone()
        if row is None or int(row["result_mtime_ns"] or 0) != stat.st_mtime_ns or int(row["result_size"] or 0) != stat.st_size:
            stale.append(path)
    return stale


def ensure_current(run_root: Path) -> None:
    with _connect(run_root) as conn:
        stale = _stale_result_paths(conn, run_root)
    for path in stale:
        ingest_run(path.parent, run_root=run_root)


def rebuild(run_root: Path, *, force: bool = False) -> dict[str, Any]:
    """Rebuild the index from run results, never dropping it before knowing why.

    重建以前是「先 unlink 再扫源」：一条源都扫不到时旧索引已经没了，而它记录的
    历史 run 目录可能早被清掉、无从再生。现在先扫源，扫不到就拒绝（``force``
    显式放行），并在重建期间把旧库留成备份，中途出错原样放回。
    """
    path = db_path(run_root)
    result_paths = sorted(run_root.glob("*/*/platform-run-result.json"))
    if not result_paths and not force and path.exists():
        existing = 0
        try:
            with _connect(run_root) as conn:
                existing = int(conn.execute("SELECT count(*) FROM runs").fetchone()[0])
        except sqlite3.Error:
            existing = 0
        if existing:
            return {
                "ok": False,
                "refused": "no_run_results_found",
                "detail": (
                    f"{run_root} has no */*/platform-run-result.json but the index holds "
                    f"{existing} rows; rebuilding would drop them irrecoverably"
                ),
                "db_path": str(path),
                "indexed_runs": existing,
            }
    backup = path.with_suffix(path.suffix + ".rebuild-bak")
    backup.unlink(missing_ok=True)
    if path.exists():
        path.replace(backup)
    count = 0
    try:
        for result_path in result_paths:
            ingest_run(result_path.parent, run_root=run_root)
            count += 1
        for journal_path in sorted(run_root.rglob('closure-intent.json')):
            ingest_closure(journal_path.parent, run_root=run_root)
    except BaseException:
        path.unlink(missing_ok=True)
        if backup.exists():
            backup.replace(path)
        raise
    backup.unlink(missing_ok=True)
    return {"ok": True, "db_path": str(path), "indexed_runs": count}


def status(run_root: Path) -> dict[str, Any]:
    path = db_path(run_root)
    with _connect(run_root) as conn:
        indexed = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        stale = len(_stale_result_paths(conn, run_root))
        row = conn.execute("SELECT value FROM metadata WHERE key='last_ingest_error'").fetchone()
    return {
        "ok": True,
        "db_path": str(path),
        "indexed_runs": int(indexed),
        "stale_runs": int(stale),
        "last_ingest_error": row["value"] if row else "",
    }


# status='failed' 是「需人工关注」聚合：产品失败、基础设施阻断及未完成 required 平面都可见。
_FAILED_STATUSES = ("failed", "error", "blocked", "partial", "pending")


def case_stat(row: sqlite3.Row) -> dict[str, int] | None:
    """把 history 查询里 LEFT JOIN 出的逐用例聚合列打包成 stat；无任何用例行 → None。"""
    total = row["stat_total"]
    if total is None:
        return None
    return {
        "total": int(total),
        "passed": int(row["stat_passed"] or 0),
        "failed": int(row["stat_failed"] or 0),
        "error": int(row["stat_error"] or 0),
        "blocked": int(row["stat_blocked"] or 0),
        "asset_blocked": int(row["stat_asset_blocked"] or 0),
    }


def history(
    run_root: Path,
    *,
    limit: int = 20,
    app: str | None = None,
    status: str | None = None,
    type: str | None = None,
    before_mtime: int | None = None,
) -> list[dict[str, Any]]:
    ensure_current(run_root)
    where: list[str] = []
    params: list[Any] = []
    if app:
        where.append("r.app = ?")
        params.append(app)
    if type:
        where.append("r.type = ?")
        params.append(type)
    if status == "failed":
        where.append(f"r.status IN ({','.join('?' * len(_FAILED_STATUSES))})")
        params.extend(_FAILED_STATUSES)
    elif status:
        where.append("r.status = ?")
        params.append(status)
    if before_mtime is not None:
        where.append("r.result_mtime_ns < ?")
        params.append(int(before_mtime))
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(limit)
    with _connect(run_root) as conn:
        rows = conn.execute(
            f"""
            SELECT
              r.*,
              cs.total   AS stat_total,
              cs.passed  AS stat_passed,
              cs.failed  AS stat_failed,
              cs.error   AS stat_error,
              cs.blocked AS stat_blocked,
              cs.asset_blocked AS stat_asset_blocked
            FROM runs r
            LEFT JOIN (
              SELECT
                run_dir,
                COUNT(*)                                   AS total,
                SUM(status='passed')                       AS passed,
                SUM(status='failed')                       AS failed,
                SUM(status='error')                        AS error,
                SUM(status='blocked')                      AS blocked,
                SUM(status='blocked' AND blocker_scope='asset') AS asset_blocked
              FROM case_results
              GROUP BY run_dir
            ) cs ON cs.run_dir = r.run_dir
            {where_sql}
            ORDER BY r.result_mtime_ns DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        run_dir = run_root / str(row["run_dir"])
        mtime_ns = int(row["result_mtime_ns"] or 0)
        items.append({
            "run_id": row["run_id"] or run_dir.name,
            "status": row["status"],
            "app": row["app"],
            "device": row["device"],
            "type": row["type"],
            "day": row["day"],
            "appv": row["package_device_version"] or row["package_active_version"],
            "subject_build_key": row["subject_build_key"],
            "identity_state": row["identity_state"] or "unknown",
            "identity_source": row["identity_source"] or "unknown",
            "queued_at": row["queued_at"],
            "ended_at": row["ended_at"],
            "stat": case_stat(row),
            "run_dir": str(run_dir),
            "report_html": str(run_dir / ("operation-report.html" if row["type"] == "operation" else "verification-report.html" if row["type"] == "verification" else "report.html")),
            "mtime": float(mtime_ns / 1_000_000_000),
            "mtime_ns": mtime_ns,
        })
    return items


def find_same_build_pass(
    run_root: Path,
    target_run_dir: Path,
    *,
    require_sha_identity: bool = False,
) -> dict[str, Any]:
    """Resolve query #6's nearest passing baseline or a structured no-answer."""
    ensure_current(run_root)
    rel = _rel_run_dir(target_run_dir, run_root)
    with _connect(run_root) as conn:
        target = conn.execute("SELECT * FROM runs WHERE run_dir=?", (rel,)).fetchone()
        if target is None:
            return {"status": "target_run_not_indexed", "target_run_dir": rel}
        key = target["subject_build_key"]
        if not key or target["identity_state"] not in {"confirmed", "version_only"}:
            return {
                "status": "build_identity_unconfirmed",
                "target_run_id": target["run_id"],
                "identity_state": target["identity_state"] or "unknown",
            }
        if require_sha_identity:
            return {
                "status": "sha_identity_unverified",
                "target_run_id": target["run_id"],
                "subject_build_key": key,
            }
        candidate = conn.execute(
            """
            SELECT * FROM runs
            WHERE app=? AND selector_key=? AND subject_build_key=?
              AND status='passed' AND run_dir<>? AND result_mtime_ns<=?
            ORDER BY result_mtime_ns DESC
            LIMIT 1
            """,
            (
                target["app"],
                target["selector_key"],
                key,
                rel,
                int(target["result_mtime_ns"] or 0),
            ),
        ).fetchone()
    if candidate is None:
        return {
            "status": "no_same_build_pass",
            "target_run_id": target["run_id"],
            "subject_build_key": key,
        }
    return {
        "status": "found",
        "target_run_id": target["run_id"],
        "baseline_run_id": candidate["run_id"],
        "baseline_run_dir": str(run_root / str(candidate["run_dir"])),
        "subject_build_key": key,
        "selector_key": target["selector_key"],
    }


def knowledge_observations(
    run_root: Path,
    *,
    app: str | None = None,
    type: str | None = None,
    operation: str | None = None,
    case_id: str | None = None,
) -> list[dict[str, Any]]:
    ensure_current(run_root)
    where: list[str] = []
    params: list[Any] = []
    if app:
        where.append("r.app=?")
        params.append(app)
    if type:
        where.append("o.type=?")
        params.append(type)
    if operation:
        where.append("o.operation=?")
        params.append(operation)
    if case_id:
        where.append("o.case_id=?")
        params.append(case_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with _connect(run_root) as conn:
        rows = conn.execute(
            f"""
            SELECT o.*, r.run_id, r.app
            FROM knowledge_observations o
            JOIN runs r ON r.run_dir=o.run_dir
            {clause}
            ORDER BY r.result_mtime_ns DESC, o.occurred_at DESC
            """,
            params,
        ).fetchall()
    return [
        {
            "id": row["observation_id"],
            "run_id": row["run_id"],
            "app": row["app"],
            "type": row["type"],
            "case_id": row["case_id"],
            "operation": row["operation"],
            "action_id": row["action_id"],
            "subject_epoch_ref": row["subject_epoch_ref"],
            "subject_build_key": row["subject_build_key"],
            "occurred_at": row["occurred_at"],
            "producer_kind": row["producer_kind"],
            "correlation_state": row["correlation_state"],
            "payload": json.loads(row["payload_json"] or "{}"),
            "run_dir": str(run_root / str(row["run_dir"])),
        }
        for row in rows
    ]


def run_build_identity(result: dict[str, Any]) -> dict[str, Any]:
    """Build identity frozen in one Run Result, as recorded; absent fields stay None.

    ``build_id`` is the installed Build Record frozen at submission
    (``package.registry_build_ref``); ``build_label`` is the top-level label
    (BL-069) or, for older results, ``package.profile_build_label`` (same
    source).  ``build_identity_match`` is the device-side comparison of that
    Build.  Policy (which Build counts as current) stays with the caller.
    """
    package = result.get("package") if isinstance(result.get("package"), dict) else {}
    ref = package.get("registry_build_ref") if isinstance(package.get("registry_build_ref"), dict) else {}
    return {
        "build_id": ref.get("build_id") or None,
        "build_label": result.get("build_label") or package.get("profile_build_label") or None,
        "build_identity_match": package.get("build_identity_match") or None,
    }


class RunCatalog:
    """Read-only seam over settled run artifacts.

    Run JSON remains the source of truth.  SQLite is only a rebuildable locator,
    and every public read falls back to the same files if the index is unusable.
    Callers therefore ask for run facts instead of knowing the day/run directory
    layout, index schema, or sidecar filenames.
    """

    def __init__(self, run_root: Path) -> None:
        self.run_root = run_root
        self._mapping_cache: dict[str, tuple[int, int, dict[str, Any]]] = {}
        self._mapping_cache_lock = threading.Lock()

    def runs(self, query: RunQuery | None = None) -> list[RunFact]:
        query = query or RunQuery()
        if query.limit is not None and int(query.limit) <= 0:
            return []
        try:
            ensure_current(self.run_root)
            sql = "SELECT run_dir, result_mtime_ns FROM runs ORDER BY result_mtime_ns"
            sql += " DESC" if query.newest_first else " ASC"
            params: list[Any] = []
            if query.candidate_limit is not None:
                sql += " LIMIT ?"
                params.append(max(0, int(query.candidate_limit)))
            with _connect(self.run_root) as conn:
                locations = [
                    (self.run_root / str(row["run_dir"]), int(row["result_mtime_ns"] or 0))
                    for row in conn.execute(sql, params).fetchall()
                ]
        except (OSError, sqlite3.Error, json.JSONDecodeError, TypeError, AttributeError):
            locations = self._file_locations(newest_first=query.newest_first)
            if query.candidate_limit is not None:
                locations = locations[:max(0, int(query.candidate_limit))]

        facts: list[RunFact] = []
        for run_dir, indexed_mtime_ns in locations:
            result_path = run_dir / "platform-run-result.json"
            result = self._read_mapping(result_path)
            if not isinstance(result, dict):
                continue
            if query.app is not None and result.get("app") != query.app:
                continue
            # Results written before run types were explicit are regression runs.
            run_type = str(result.get("type") or "regression")
            if query.types is not None and run_type not in query.types:
                continue
            try:
                mtime_ns = result_path.stat().st_mtime_ns
            except OSError:
                mtime_ns = indexed_mtime_ns
            facts.append(RunFact(run_dir=run_dir, result=result, mtime_ns=mtime_ns))
            if query.limit is not None and len(facts) >= max(0, int(query.limit)):
                break
        return facts

    def case_verdicts(self, app: str, *, limit: int = 50) -> dict[str, dict[str, Any]]:
        """Return the latest regression verdict for every case in the candidate window."""
        verdicts: dict[str, dict[str, Any]] = {}
        for fact in self.runs(RunQuery(
            app=app,
            types=frozenset({"regression"}),
            candidate_limit=limit,
        )):
            # 区分力反向 Run 的失败是负对照的预期结果，不是该用例的回归裁决。
            if self._is_negative_control_run(fact.run_dir):
                continue
            ended_at = (fact.result.get("timestamps") or {}).get("collect_ended_at")
            build = run_build_identity(fact.result)
            for case in fact.result.get("cases") or []:
                cid = str(case.get("id") or "")
                if not cid or cid in verdicts:
                    continue
                verdicts[cid] = {
                    "status": case.get("status"),
                    "run_id": fact.result.get("run_id") or fact.run_dir.name,
                    "ended_at": ended_at,
                    "item": case.get("item"),
                    **build,
                }
        return verdicts

    def promotion_runs(self, subject: PromotionSubject) -> list[dict[str, Any]]:
        """Read normalized run evidence; promotion decisions stay in ``promotion``."""
        if subject.kind == "verification":
            runs = self._verification_runs(subject)
        elif subject.kind == "operation":
            runs = self._operation_runs(subject)
        elif subject.kind == "case":
            runs = self._case_runs(subject)
        else:
            raise ValueError("promotion subject kind must be verification, operation, or case")
        runs.sort(key=lambda item: str(item.get("ended_at") or item.get("run_id") or ""))
        return runs

    def _verification_runs(self, subject: PromotionSubject) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        for fact in self.runs(RunQuery(types=frozenset({"verification"}))):
            sidecar_path = fact.run_dir / "verification-result.json"
            sidecar = self._read_mapping(sidecar_path) or {}
            item = {
                "run_id": fact.result.get("run_id") or fact.run_dir.name,
                "draft_id": sidecar.get("draft_id"),
                "status": fact.result.get("status"),
                "review_status": sidecar.get("review_status"),
                "ended_at": (fact.result.get("timestamps") or {}).get("collect_ended_at"),
                "run_dir": str(fact.run_dir),
                "verification_result": str(sidecar_path) if sidecar_path.exists() else None,
            }
            if subject.asset_id is None or item["draft_id"] == subject.asset_id:
                runs.append(item)
        return runs

    def _operation_runs(self, subject: PromotionSubject) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        indexed_dirs: set[Path] = set()
        for fact in self.runs(RunQuery(types=frozenset({"operation"}))):
            sidecar_path = fact.run_dir / "operation-result.json"
            sidecar = self._read_mapping(sidecar_path) or {}
            if not sidecar:
                continue
            indexed_dirs.add(fact.run_dir)
            item = {
                "run_id": fact.result.get("run_id") or fact.run_dir.name,
                "app": sidecar.get("app") or fact.result.get("app"),
                "draft_id": sidecar.get("draft_id"),
                "revision": (sidecar.get("operation") or {}).get("revision"),
                "status": sidecar.get("status") or fact.result.get("status"),
                "review_status": sidecar.get("review_status"),
                "ended_at": (fact.result.get("timestamps") or {}).get("collect_ended_at"),
                "run_dir": str(fact.run_dir),
                "operation_result": str(sidecar_path) if sidecar_path.exists() else None,
                "replay_speed": float(sidecar.get("replay_speed") or 1.0),
            }
            if subject.asset_id is not None and item["draft_id"] != subject.asset_id:
                continue
            if subject.app is not None and item["app"] != subject.app:
                continue
            if subject.revision is not None and item["revision"] != subject.revision:
                continue
            runs.append(item)

        # 页面影响采集和历史离线导入可能只保留 operation-result.json。该文件是
        # Operation 回放身份与状态的权威 sidecar；缺少外层 platform-run-result.json
        # 时仍保留转正证据，但使用稳定的 run_id/ended_at 排序，避免人审 mtime 改写顺序。
        for sidecar_path in self.run_root.glob("*/*/operation-result.json"):
            run_dir = sidecar_path.parent
            if run_dir in indexed_dirs:
                continue
            sidecar = self._read_mapping(sidecar_path) or {}
            if not sidecar:
                continue
            result_path = run_dir / "platform-run-result.json"
            platform_result = self._read_mapping(result_path) or {}
            if platform_result and platform_result.get("type") != "operation":
                continue
            ended_at = (platform_result.get("timestamps") or {}).get("collect_ended_at")
            ended_at = ended_at or sidecar.get("ended_at") or sidecar.get("run_id") or run_dir.name
            item = {
                "run_id": sidecar.get("run_id") or platform_result.get("run_id") or run_dir.name,
                "app": sidecar.get("app") or platform_result.get("app"),
                "draft_id": sidecar.get("draft_id"),
                "revision": (sidecar.get("operation") or {}).get("revision"),
                "status": sidecar.get("status") or platform_result.get("status"),
                "review_status": sidecar.get("review_status"),
                "ended_at": ended_at,
                "run_dir": str(run_dir),
                "operation_result": str(sidecar_path),
                "replay_speed": float(sidecar.get("replay_speed") or 1.0),
            }
            if subject.asset_id is not None and item["draft_id"] != subject.asset_id:
                continue
            if subject.app is not None and item["app"] != subject.app:
                continue
            if subject.revision is not None and item["revision"] != subject.revision:
                continue
            runs.append(item)
        return runs

    def case_recipe_runs(self, *, app: str, case_id: str) -> list[dict[str, Any]]:
        """Regression Case results from Runs that named the Case explicitly.

        A live Recipe is never selected by a Draft Trial selector, so its
        executed-Recipe identity (BL-186) is only recorded by non-Trial runs.
        Only Runs whose ``run-spec.json`` ``case_selector.ids`` lists the Case
        ID count (``promotion_identity.selector_names_case``); selection by
        tag, module, suite, flow or status alone does not.  Items have the
        same shape as case promotion runs and are sorted oldest first;
        identity judgement stays in ``promotion_identity``.
        """
        runs = self._case_runs(
            PromotionSubject(kind="case", asset_id=case_id, app=app),
            trial_only=False,
            named_only=True,
        )
        runs.sort(key=lambda item: str(item.get("ended_at") or item.get("run_id") or ""))
        return runs

    def _case_runs(
        self,
        subject: PromotionSubject,
        *,
        trial_only: bool = True,
        named_only: bool = False,
    ) -> list[dict[str, Any]]:
        from autophone.assets.promotion_identity import (
            selector_is_draft_trial,
            selector_names_case,
        )

        runs: list[dict[str, Any]] = []
        for fact in self.runs(RunQuery(types=frozenset({"regression"}))):
            spec = self._read_mapping(fact.run_dir / "run-spec.json") or {}
            if subject.app is not None and str(spec.get("app") or "") != subject.app:
                continue
            # 区分力反向 Run 执行的是扰动后的 Recipe，不是 Trial 或回归证据。
            if negative_control_id(spec) is not None:
                continue
            selector = spec.get("case_selector") or {}
            trial = selector_is_draft_trial(selector)
            if trial_only and not trial:
                continue
            reviews = self._read_mapping(fact.run_dir / "case-draft-review.json") or {}
            for case in fact.result.get("cases") or []:
                cid = str(case.get("id") or "")
                if not cid or (subject.asset_id is not None and cid != subject.asset_id):
                    continue
                if named_only and not selector_names_case(selector, cid):
                    continue
                review = reviews.get(cid) if isinstance(reviews, dict) else None
                review = review if isinstance(review, dict) else {}
                identity = case.get("draft_recipe_identity")
                identity = dict(identity) if isinstance(identity, dict) else None
                review_identity = review.get("draft_recipe_identity")
                review_matches_identity = (
                    identity is not None
                    and isinstance(review_identity, dict)
                    and review_identity == identity
                )
                runs.append({
                    "run_id": fact.result.get("run_id") or fact.run_dir.name,
                    "draft_id": cid,
                    "case_id": cid,
                    "item": case.get("item") or "",
                    "status": case.get("status"),
                    "revision": case.get("revision"),
                    "exec_via": case.get("exec_via"),
                    "draft_recipe_identity": identity,
                    "recipe_identity": (
                        dict(case["recipe_identity"])
                        if isinstance(case.get("recipe_identity"), dict)
                        else None
                    ),
                    "trial": trial,
                    "review_status": (
                        review.get("review_status") if review_matches_identity else None
                    ),
                    "reviewed_by": (
                        review.get("reviewed_by") if review_matches_identity else None
                    ),
                    "ended_at": (fact.result.get("timestamps") or {}).get("collect_ended_at"),
                    "run_dir": str(fact.run_dir),
                })
        return runs

    def _is_negative_control_run(self, run_dir: Path) -> bool:
        return negative_control_id(self._read_mapping(run_dir / "run-spec.json") or {}) is not None

    def _file_locations(self, *, newest_first: bool) -> list[tuple[Path, int]]:
        locations: list[tuple[Path, int]] = []
        for result_path in self.run_root.glob("*/*/platform-run-result.json"):
            try:
                locations.append((result_path.parent, result_path.stat().st_mtime_ns))
            except OSError:
                continue
        locations.sort(key=lambda item: item[1], reverse=newest_first)
        return locations

    def _read_mapping(self, path: Path) -> dict[str, Any] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        key = str(path)
        signature = (stat.st_mtime_ns, stat.st_size)
        with self._mapping_cache_lock:
            cached = self._mapping_cache.get(key)
        if cached is not None and cached[:2] == signature:
            return cached[2]
        try:
            value = read_json(path)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        with self._mapping_cache_lock:
            self._mapping_cache[key] = (*signature, value)
        return value
