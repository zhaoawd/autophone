"""Run 级中止信号（文件标志，仿 manual.py 范式）。

`submit()` 同步执行链没有可抢占的线程句柄：排队态卡在 `flock`、执行态深在 phase2
里。所以中止走**协作式文件信号**——`request_run_abort` 落一个标志，执行链在自己的
检查点（executor 入口 launch 前、phase2 逐用例之间）读它，见标志即收口为
`RunResult(aborted=True)`（→ 平台 status=blocked）。和 manual 的 abort.json 同款，
只是作用域是整条 run 而非单个人工断点。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autophone.util import now_iso as _now, read_json as _read_json, write_json_atomic as _write_json


class RunNotAbortable(RuntimeError):
    """请求中止一条已不在执行的 run（已完成 / 从未排队）→ 无可中止对象。"""


def abort_request_path(run_dir: Path) -> Path:
    return run_dir / "abort-request.json"


def request_run_abort(run_dir: Path, *, actor: str = "web", reason: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"actor": actor, "reason": reason, "requested_at": _now()}
    _write_json(abort_request_path(run_dir), payload)
    return payload


def read_run_abort(run_dir: Path) -> dict[str, Any] | None:
    return _read_json(abort_request_path(run_dir))


def run_abort_reason(run_dir: Path) -> str | None:
    """协作检查点用：已请求中止则返回人读原因串，否则 None。"""
    abort = read_run_abort(run_dir)
    if abort is None:
        return None
    actor = abort.get("actor") or "unknown"
    reason = abort.get("reason") or ""
    return f"aborted by {actor}" + (f": {reason}" if reason else "")
