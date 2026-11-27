"""Host-side storage + pure helpers for record/replay **batch** runs.

A batch is one user action ("回放这几条录制"): a worker submits each recording's
draft replay as an ordinary ``type:operation`` sub-run, sequentially, waiting for
each to *settle* before the next.  See ``docs/record-replay-batch.md``.

This module owns **storage** (``<batches_root>/<batch_id>/batch.json``, root
injected by :class:`RunService`) and the **pure** derivations (phase / verdict /
aggregate / 失联派生 / id 铸造 / 视图组装).  The worker loop, settle polling and
heartbeat live in ``service.py`` — this module never touches the lock, ``_active``
or sub-run results directly; the view builder takes callbacks for those so it
stays unit-testable without a live VM.
"""
from __future__ import annotations

import json
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from autophone.util import now_iso as _now

SCHEMA_VERSION = "v1"

# phase 是唯一生命周期枚举（docs §后端 1 的 P1/P2 单一契约）。
PHASES = ("queued", "running", "done", "stopped")
# 子 run 落盘后的合法终态；其余（含 terminal-pending）一律折成 error，不当通过。
_TERMINAL_RUN_STATUS = {"passed", "failed", "error"}

# worker 在每条 settled 后、以及等待轮询的每个 tick 都刷 heartbeat（健康 worker 心跳恒新鲜）。
# stale 阈值要 > 轮询间隔留足余量，避免把慢一拍的健康 worker 误判失联。
DEFAULT_STALE_SECONDS = 30.0

_UNSET = object()


# ---------------------------------------------------------------------------
# id 铸造
# ---------------------------------------------------------------------------
def mint_batch_id(app: str) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{app}-batch-{secrets.token_hex(2)}"


def mint_sub_run_id(app: str, idx: int) -> str:
    """子 run_id 服务端铸造：``{ts}-{app}-b{idx:02d}-{rand}``（docs §后端 1）。"""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{app}-b{idx:02d}-{secrets.token_hex(2)}"


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
def _safe_batch_id(batch_id: Any) -> str:
    bid = str(batch_id or "").strip()
    if not bid or "/" in bid or "\\" in bid or "\x00" in bid or bid.startswith("."):
        raise ValueError(f"invalid batch_id: {batch_id!r}")
    return bid


def batch_dir(batches_root: Path, batch_id: str) -> Path:
    return batches_root / _safe_batch_id(batch_id)


def batch_path(batches_root: Path, batch_id: str) -> Path:
    return batch_dir(batches_root, batch_id) / "batch.json"


def write_batch(batches_root: Path, batch: dict[str, Any]) -> Path:
    path = batch_path(batches_root, str(batch["batch_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def read_batch(batches_root: Path, batch_id: str) -> dict[str, Any] | None:
    try:
        path = batch_path(batches_root, batch_id)
    except ValueError:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_batch_ids(batches_root: Path) -> list[str]:
    if not batches_root.exists():
        return []
    return sorted(p.parent.name for p in batches_root.glob("*/batch.json"))


# ---------------------------------------------------------------------------
# 纯派生：聚合 / verdict / 失联 / 视图
# ---------------------------------------------------------------------------
def aggregate(items: list[dict[str, Any]]) -> dict[str, int]:
    """聚合计数。pending = queued + running（运行中/待跑都算未结）。"""
    counts = {"total": len(items), "passed": 0, "failed": 0, "error": 0, "pending": 0, "blocked": 0}
    for it in items:
        st = str(it.get("status") or "queued")
        if st in counts and st != "total":
            counts[st] += 1
        elif st in ("queued", "running"):
            counts["pending"] += 1
        else:  # 未知状态保守计 pending（不静默丢）
            counts["pending"] += 1
    return counts


def verdict(phase: str, items: list[dict[str, Any]]) -> str | None:
    """批次裁决（完整优先级，docs §后端 1 P2）。phase 与 verdict 正交。

    - ``phase∈{queued,running}`` → ``None``（运行中，不预估）
    - ``phase=stopped`` → ``error``（中断不当通过）
    - 终态：任一 error/blocked ⇒ error；否则任一 failed ⇒ failed；否则 passed
    """
    if phase in ("queued", "running"):
        return None
    if phase == "stopped":
        return "error"
    statuses = {str(it.get("status") or "") for it in items}
    if "error" in statuses or "blocked" in statuses:
        return "error"
    if "failed" in statuses:
        return "failed"
    return "passed"


def is_heartbeat_stale(batch: dict[str, Any], *, now_ts: float, stale_seconds: float) -> bool:
    try:
        hb = float(batch.get("heartbeat_ts") or 0.0)
    except (TypeError, ValueError):
        hb = 0.0
    return (now_ts - hb) > stale_seconds


def display_phase(
    batch: dict[str, Any],
    *,
    is_active: Callable[[Any], bool],
    now_ts: float,
    stale_seconds: float = DEFAULT_STALE_SECONDS,
) -> str:
    """读时派生展示态：worker 失联（心跳陈旧 + current 不在飞）→ ``stopped``。

    **不回写持久 phase**——下次有心跳能自愈（docs §后端 1 失联收口，不靠 PID）。
    """
    phase = str(batch.get("phase") or "queued")
    if phase in ("queued", "running"):
        current = batch.get("current_run_id")
        if is_heartbeat_stale(batch, now_ts=now_ts, stale_seconds=stale_seconds) and not is_active(current):
            return "stopped"
    return phase


def build_view(
    batch: dict[str, Any],
    *,
    result_status_for: Callable[[str], str | None],
    is_active: Callable[[Any], bool],
    now_ts: float | None = None,
    stale_seconds: float = DEFAULT_STALE_SECONDS,
) -> dict[str, Any]:
    """组装对外视图：实时回填 item 状态 + 失联派生 phase + 聚合 + verdict。

    ``result_status_for(run_id)`` 读子 run ``platform-run-result.json`` 的折算终态
    （passed/failed/error，含 terminal-pending→error），无结果文件 → ``None``（保留存储态）。
    只有「已提交」（有 run_id）的 item 才回填；``blocked``/``queued`` 不动。
    """
    now_ts = time.time() if now_ts is None else now_ts
    items: list[dict[str, Any]] = []
    for raw in batch.get("items") or []:
        item = dict(raw)
        rid = item.get("run_id")
        if rid:
            st = result_status_for(str(rid))
            if st is not None:
                item["status"] = st
        items.append(item)
    phase = display_phase(batch, is_active=is_active, now_ts=now_ts, stale_seconds=stale_seconds)
    counts = aggregate(items)
    return {
        "schema_version": batch.get("schema_version") or SCHEMA_VERSION,
        "batch_id": batch.get("batch_id"),
        "app": batch.get("app"),
        "device": batch.get("device"),
        "created_at": batch.get("created_at"),
        "replay_speed": batch.get("replay_speed", 1.0),
        "phase": phase,
        "stored_phase": batch.get("phase"),
        "stop_reason": batch.get("stop_reason"),
        "current_run_id": batch.get("current_run_id"),
        "heartbeat_at": batch.get("heartbeat_at"),
        "items": items,
        "verdict": verdict(phase, items),
        **counts,
    }


def new_batch(
    *,
    batch_id: str,
    app: str,
    device: str,
    replay_speed: float,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """新建一条 queued 批次记录（phase=queued，heartbeat=now）。"""
    now = _now()
    return {
        "schema_version": SCHEMA_VERSION,
        "batch_id": batch_id,
        "app": app,
        "device": device,
        "created_at": now,
        "replay_speed": replay_speed,
        "phase": "queued",
        "stop_reason": None,
        "current_run_id": None,
        "heartbeat_at": now,
        "heartbeat_ts": time.time(),
        "items": items,
    }


def map_run_status(status: Any) -> str:
    """子 run 平台 status → item 终态。passed/failed/error 照收，其余（pending）→ error。"""
    st = str(status or "")
    return st if st in _TERMINAL_RUN_STATUS else "error"
