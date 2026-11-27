"""test_logsetup.py — 日志基础设施：级别、关联标签、并发 run.log 隔离。

并发隔离是 review P1 的回归护栏：两个 run 并发时各自 run.log 不能串台，且一个
run 退出不能把仍在跑的 run 的 DEBUG 还原掉。
"""
from __future__ import annotations

import logging
import json
import multiprocessing
import threading
import time

import pytest

from autophone import logging as logsetup
from autophone.logging import (
    background_log_queue,
    bind_run,
    configure_worker_logging,
    run_logfile,
    setup_logging,
)


def _spawn_log_worker(queue, run_dir: str) -> None:
    """Module-level target so macOS multiprocessing spawn can import it."""
    configure_worker_logging(queue)
    with bind_run("run-spawn", run_dir=run_dir, stage="execute"):
        logging.getLogger("autophone.spawn-test").info("worker relay marker")


@pytest.fixture(autouse=True)
def _fresh_logging():
    """每个用例前重置全局 logging，避免 setup_logging 的幂等门跨用例粘连。"""
    logsetup._configured = False
    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    for h in list(root.handlers):
        root.removeHandler(h)
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved:
        root.addHandler(h)
    root.setLevel(saved_level)
    logsetup._configured = False


def test_own_loggers_emit_debug_after_setup():
    setup_logging(logging.INFO)
    # 控制台级别是 INFO，但我们自己的 logger 常驻 DEBUG（供 run.log 拿全量）。
    assert logging.getLogger("autophone").level == logging.DEBUG
    assert logging.getLogger("tools").level == logging.DEBUG


@pytest.mark.parametrize("phone,masked", [
    (13812345678, "138****5678"),
    (13812345678.0, "138****5678.0"),
])
def test_structured_log_redacts_numeric_phone_without_corrupting_json(phone, masked):
    source = json.dumps({"phone": phone, "by_phone": {"13912345678": "failed"}, "code": 401, "retry": False})
    output = logsetup.redact_text(source)

    assert "13812345678" not in output
    assert "13912345678" not in output
    assert json.loads(output) == {
        "phone": masked, "by_phone": {"139****5678": "failed"}, "code": 401, "retry": False,
    }
    assert logsetup.redact_text(output) == output


def test_run_logfile_captures_full_debug(tmp_path):
    setup_logging(logging.INFO)  # 控制台 INFO
    log = logging.getLogger("autophone.test")
    path = tmp_path / "run.log"
    with bind_run("R-solo"), run_logfile(path):
        log.debug("debug-line")
        log.info("info-line")
    text = path.read_text(encoding="utf-8")
    # 控制台只 INFO，但文件拿到 DEBUG 全量，且带 run 标签。
    assert "debug-line" in text
    assert "info-line" in text
    assert "run=R-solo" in text


def test_run_router_writes_redacted_human_and_structured_journals(tmp_path, monkeypatch):
    """Normal Web/CLI setup owns both global and per-run durable sinks."""
    monkeypatch.setenv("AUTOPHONE_LOG_ROOT", str(tmp_path / "platform"))
    setup_logging(logging.INFO)
    run_dir = tmp_path / "run"
    log = logging.getLogger("autophone.journal")

    with bind_run("run-journal", run_dir=run_dir, stage="execute"):
        log.info("login mobile=13900002222 password=hunter2")

    human = (run_dir / "run.log").read_text(encoding="utf-8")
    event = json.loads((run_dir / "events.jsonl").read_text(encoding="utf-8").strip())
    global_events = list((tmp_path / "platform").glob("service-*.jsonl"))
    assert "139****2222" in human
    assert "hunter2" not in human
    assert event["run_id"] == "run-journal"
    assert event["stage"] == "execute"
    assert global_events


def test_spawned_worker_is_relayed_to_parent_run_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPHONE_LOG_ROOT", str(tmp_path / "platform"))
    setup_logging(logging.INFO)
    run_dir = tmp_path / "worker-run"
    queue = background_log_queue()
    assert queue is not None
    process = multiprocessing.get_context("spawn").Process(
        target=_spawn_log_worker,
        args=(queue, str(run_dir)),
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 0
    deadline = time.time() + 3
    while time.time() < deadline and not (run_dir / "events.jsonl").exists():
        time.sleep(0.02)
    event = json.loads((run_dir / "events.jsonl").read_text(encoding="utf-8").strip())
    assert event["run_id"] == "run-spawn"
    assert event["message"] == "worker relay marker"


def test_concurrent_runs_do_not_cross_contaminate(tmp_path):
    """两个 run 并发，各自 run.log 只含自己的记录（review P1）。"""
    setup_logging(logging.INFO)
    log = logging.getLogger("autophone.concurrent")

    files = {"a": tmp_path / "a.log", "b": tmp_path / "b.log"}
    # 三道栅栏强制交错：两边都进了 run_logfile 后再各打一条，确保对方的 handler
    # 此刻就挂在全局 root 上——没有 run_id 过滤就会串台。
    both_attached = threading.Barrier(2)
    both_logged = threading.Barrier(2)

    def worker(tag: str) -> None:
        with bind_run(f"run-{tag}"), run_logfile(files[tag]):
            both_attached.wait(timeout=5)
            for i in range(5):
                log.debug("%s-debug-%d", tag, i)
            both_logged.wait(timeout=5)

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    a_text = files["a"].read_text(encoding="utf-8")
    b_text = files["b"].read_text(encoding="utf-8")
    # a.log 只含 run-a，绝不含 run-b 的记录；反之亦然。
    assert "run=run-a" in a_text and "a-debug-0" in a_text
    assert "run=run-b" not in a_text and "b-debug-0" not in a_text
    assert "run=run-b" in b_text and "b-debug-0" in b_text
    assert "run=run-a" not in b_text and "a-debug-0" not in b_text


def test_exiting_one_run_keeps_other_debug(tmp_path):
    """一个 run 退出（摘 handler）不能让仍在跑的 run 丢 DEBUG。

    旧实现里 run_logfile 退出会把全局 root level 还原回 INFO，导致并发的另一个
    run 之后的 DEBUG 不再落盘。现在改为我们的 logger 常驻 DEBUG，不再有这问题。"""
    setup_logging(logging.INFO)
    log = logging.getLogger("autophone.survivor")
    short = tmp_path / "short.log"
    long = tmp_path / "long.log"

    with bind_run("run-long"), run_logfile(long):
        log.debug("long-before")
        # 嵌套一个短命 run 并退出（模拟另一个 run 先结束）。
        with bind_run("run-short"), run_logfile(short):
            log.debug("short-line")
        # short 退出后，long 仍应拿到 DEBUG。
        log.debug("long-after")

    long_text = long.read_text(encoding="utf-8")
    assert "long-before" in long_text
    assert "long-after" in long_text  # 关键：退出短 run 没把 DEBUG 还原掉


def test_access_log_retains_path_and_status_but_omits_query():
    from autophone.logging import AccessLogPathFilter

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", "/embed/device-console?device=vm&launch=test-fixture", "1.1", 200),
        None,
    )
    assert AccessLogPathFilter().filter(record)
    assert record.getMessage() == '127.0.0.1:1234 - "GET /embed/device-console HTTP/1.1" 200'
