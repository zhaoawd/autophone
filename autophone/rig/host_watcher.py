"""One watcher per Host: unified log anomalies and load samples.

同机每个 Rig Agent 都启动本线程，靠文件锁选出一个工作；持锁的 Agent 退出后由另一个
接手。它只记录，不参与派单和恢复，任何失败都不能影响 Agent。
设计见 docs/platform-host-resource-admission.md §3。
"""
from __future__ import annotations

import fcntl
import json
import os
import queue
import re
import subprocess
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from autophone import host_anomaly
from autophone.host_anomaly import iso, utc_now
from autophone.logging import get_logger
from autophone.recovery_observation import atomic_json
from autophone.usb_diagnosis import diagnose_usb_incident
from autophone.vm_fatal import (
    FATAL_TEXT,
    USB_EXCEPTION_SIGNATURE,
    VM_PROCESS,
    VM_FATAL_PREDICATE,
    count_vm_fatal_rows,
)

log = get_logger(__name__)

LOG_BINARY = "/usr/bin/log"
USBMUX_PREDICATE = 'process == "usbmuxd" AND eventMessage CONTAINS "USBMuxConnectByPort"'
# USB 异常由 kernel 记录，不只来自 Virtualization 进程。这里只放宽 watcher 的过滤，不动
# VM_FATAL_PREDICATE：基线和长窗口的计数要用同一个判据才能比。
USB_EXCEPTION_PREDICATE = f'eventMessage CONTAINS "{USB_EXCEPTION_SIGNATURE}"'
LOG_PREDICATE = f"({VM_FATAL_PREDICATE}) OR ({USB_EXCEPTION_PREDICATE}) OR ({USBMUX_PREDICATE})"

SAMPLE_INTERVAL_S = 60
GAP_THRESHOLD_S = 120
MAX_REPLAY_S = 24 * 3600
ELECTION_RETRY_S = 30
FINGERPRINT_REFRESH_S = 600
PRUNE_INTERVAL_S = 3600
COLLECT_DELAY_S = 10
# 同类事件的合并窗口。62078 探测可能每约 15 秒一次，单独算会刷屏。
DEBOUNCE_S = {"lockdownd_connect": 300}
DEFAULT_DEBOUNCE_S = 60
# kern.memorystatus_vm_pressure_level：1 normal、2 warn、4 critical。2026-11-06 本机（48 GiB、2 台 VM）
# 1421 个样本中 70% 处于 warn，空闲内存 39–50%，从未到 critical；按 warn 记录会把常态当异常。
# 与资源准入（control/resource_admission.py）一致，只有 critical 才记 host_pressure。
PRESSURE_LEVEL_RECORD = 4
PRESSURE_SWAP_GROWTH_BYTES = 256 * 1024**2
PRESSURE_CLEAR_SAMPLES = 5

_ROW_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+(?P<type>[A-Za-z]+)\s+"
    r"(?P<process>[^\s\[]+)\[(?P<pid>\d+)"
)
_CONTROLLER_RE = re.compile(r"Virtual[A-Za-z.]*@([0-9a-fA-F]+)")


def classify_log_line(line: str) -> dict[str, Any] | None:
    """Classify one compact unified-log row; ``None`` when it is not an anomaly."""
    diagnosis = diagnose_usb_incident(vm_fatal_text=line, usbmux_text=line)
    row = _ROW_RE.match(line)
    common: dict[str, Any] = {"line": line.rstrip()[:2000]}
    if row is not None:
        common.update(process=row.group("process"), pid=int(row.group("pid")))
    if diagnosis.lockdownd_connect_count:
        return {"kind": "lockdownd_connect", **common}
    if diagnosis.usbmux_connects:
        return None
    is_fatal = FATAL_TEXT in line or (row is not None and row.group("type") == "F")
    if diagnosis.exceptions:
        exception_type = diagnosis.exceptions[0].kind
        kind = "vm_fatal" if is_fatal else "usb_exception"
        controller = _CONTROLLER_RE.search(line)
        if controller is not None:
            # kernel 行没有 VM 的 pid；每台 VM 有独立主控，留下地址供事后对应。
            common["usb_controller"] = controller.group(1)
        if kind == "vm_fatal":
            common["line"] = line.rstrip("\r\n")
        return {"kind": kind, "exception_type": exception_type, **common}
    if count_vm_fatal_rows(line):
        return {**common, "kind": "vm_fatal", "line": line.rstrip("\r\n")}
    return None


def line_time(line: str) -> datetime | None:
    row = _ROW_RE.match(line)
    if row is None:
        return None
    # compact 样式是本地时间，不带时区。
    return datetime.strptime(row.group("ts")[:26], "%Y-%m-%d %H:%M:%S.%f").astimezone()


class HostAnomalyWatcher:
    def __init__(
        self,
        *,
        host_id: str,
        own_target: Mapping[str, Any],
        environment: Mapping[str, str],
        root: Path | None = None,
        control_factory: Callable[[], Any] | None = None,
        collect: Callable[..., Any] | None = None,
        run: host_anomaly.Runner = host_anomaly.run_text,
        collect_delay_s: float = COLLECT_DELAY_S,
    ) -> None:
        self.host_id = host_id
        self.own_target = dict(own_target)
        self.environment = dict(environment)
        self.root = root or host_anomaly.anomaly_root(environment)
        self.control_factory = control_factory or _default_control
        self.collect = collect or host_anomaly.collect_bundle
        self.run = run
        self.collect_delay_s = collect_delay_s
        self._open: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._recent_lines: deque[int] = deque(maxlen=4096)
        self._bundles: queue.Queue[Path] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[str] | None = None
        self._in_pressure = False
        self._pressure_clear = 0
        self._last_swap: int | None = None
        self._last_sample: datetime | None = None
        self._last_prune = 0.0
        self._fingerprint = ""
        self._fingerprint_at = 0.0
        self._fingerprint_rigs: frozenset[Any] = frozenset()

    # MARK: - Paths and state

    @property
    def _host_dir(self) -> Path:
        return host_anomaly.host_root(self.root, self.host_id)

    def _state_path(self) -> Path:
        return self._host_dir / "watcher-state.json"

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self._state_path().read_text())
        except (OSError, ValueError):
            return {}

    def _save_state(self, **changes: Any) -> None:
        atomic_json(self._state_path(), {**self._load_state(), **changes})

    def _control(self) -> Any:
        try:
            return self.control_factory()
        except Exception as exc:  # noqa: BLE001 - recording continues without the Control Plane
            log.debug("control plane unavailable: %s", exc)
            return None

    def _targets(self, control: Any) -> list[dict[str, Any]]:
        if control is not None:
            try:
                targets = control.get_host(self.host_id).get("targets") or []
                if targets:
                    return [dict(target) for target in targets]
            except Exception as exc:  # noqa: BLE001
                log.debug("host targets unavailable: %s", exc)
        return [self.own_target]

    # MARK: - Recording

    def _write(
        self,
        kind: str,
        details: Mapping[str, Any],
        *,
        detected_at: datetime | None = None,
    ) -> Path:
        control = self._control()
        targets = self._targets(control)
        if kind == "vm_fatal":
            binding = self._fatal_binding(details, targets, detected_at)
            details = {**details, "pid_binding": binding}
            rigs = binding["rigs"]
        else:
            rigs = self._attribute(details.get("pid"), targets)
        path = host_anomaly.write_record(
            self.root,
            kind=kind,
            host_id=self.host_id,
            rigs=rigs,
            details=details,
            targets=targets,
            control=control,
            detected_at=detected_at,
            run=self.run,
        )
        record = json.loads(path.read_text())
        if record["bundle"]["status"] == "pending":
            self._bundles.put(path)
        return path

    def _attribute(self, pid: Any, targets: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        if pid is None:
            return []
        for vm in host_anomaly.running_vms(targets, run=self.run):
            if pid in vm["helper_pids"] and vm["rig_id"]:
                generation = next(
                    (t.get("rig_generation") for t in targets if t.get("rig_id") == vm["rig_id"]), None,
                )
                return [{"rig_id": vm["rig_id"], "generation": generation}]
        return []

    def _fatal_binding(
        self, details: Mapping[str, Any], targets: Sequence[Mapping[str, Any]], detected_at: datetime | None,
    ) -> dict[str, Any]:
        pid = details.get("pid")
        binding: dict[str, Any] = {"pid": pid, "observed_at": iso(utc_now()), "rigs": [], "status": "unresolved"}
        if details.get("replayed") or details.get("process") != VM_PROCESS or not pid or not detected_at:
            return binding
        owners = [vm for vm in host_anomaly.running_vms(targets, run=self.run)
                  if pid in vm["helper_pids"]]
        binding["disk_owners"] = owners
        if len(owners) != 1:
            return binding
        vm_dir = owners[0]["vm_dir"]
        matches = [t for t in targets if t.get("vm_dir") == vm_dir]
        if len(matches) != 1 or not matches[0].get("rig_id") or not matches[0].get("rig_generation"):
            return binding
        # A reused PID can hold the same disk. Verify the live process predates the row.
        identity = self.run(["/bin/ps", "-ww", "-p", str(pid), "-o", "lstart=,comm="]).strip()
        fields = identity.split(None, 5)
        if len(fields) != 6 or Path(fields[5]).name != VM_PROCESS:
            return binding
        try:
            started = datetime.strptime(" ".join(fields[:5]), "%a %b %d %H:%M:%S %Y").astimezone()
        except ValueError:
            return binding
        binding["process_started_at"] = iso(started)
        age = (utc_now() - detected_at).total_seconds()
        if 0 <= age <= SAMPLE_INTERVAL_S and started + timedelta(seconds=1) <= detected_at:
            binding.update(status="live_disk_owner", rigs=[{
                "rig_id": matches[0]["rig_id"], "generation": matches[0]["rig_generation"],
            }])
        return binding

    def _usb_type_is_new(self, exception_type: str) -> bool:
        seen = set(self._load_state().get("seen_usb_exception_types") or [])
        if exception_type in seen:
            return False
        self._save_state(seen_usb_exception_types=sorted(seen | {exception_type}))
        return True

    def process_line(self, line: str, *, replayed: bool = False) -> Path | None:
        """Record a log row, grouping repetitions within the debounce window."""
        fingerprint = hash(line.rstrip("\r\n"))
        if fingerprint in self._recent_lines:
            return None
        classified = classify_log_line(line)
        if classified is None:
            return None
        self._recent_lines.append(fingerprint)
        at = line_time(line) or utc_now()
        kind = classified["kind"]
        key = (kind, classified.get("exception_type"), classified.get("pid"), classified.get("usb_controller"))
        entry = self._open.get(key)
        if entry is not None and entry["first_at"] <= at <= entry["until"]:
            if kind == "vm_fatal":
                record = json.loads(entry["path"].read_text())
                details = record["details"]
                lines = [*details.get("raw_lines", [details["line"]]), classified["line"]]
                host_anomaly.update_record(entry["path"], details={
                    **details, "raw_lines": lines, "occurrences": len(lines), "last_at": iso(at),
                })
            else:
                entry["count"] += 1
                entry["last_at"] = at
            return None
        self._close_entry(key)
        details = {key_: value for key_, value in classified.items() if key_ != "kind"}
        details["occurrences"] = 1
        details["replayed"] = replayed
        if kind in {"usb_exception", "vm_fatal"} and classified.get("exception_type"):
            details["new_exception_type"] = self._usb_type_is_new(classified["exception_type"])
        path = self._write(kind, details, detected_at=at)
        self._open[key] = {
            "path": path, "count": 1, "first_at": at, "last_at": at,
            "until": at + timedelta(seconds=DEBOUNCE_S.get(kind, DEFAULT_DEBOUNCE_S)),
        }
        return path

    def _close_entry(self, key: tuple[Any, ...]) -> None:
        entry = self._open.pop(key, None)
        if entry is not None and entry["count"] > 1:
            host_anomaly.bump_occurrences(entry["path"], entry["count"], last_at=entry["last_at"])

    def _close_expired(self, now: datetime) -> None:
        for key, entry in list(self._open.items()):
            if now > entry["until"]:
                self._close_entry(key)

    # MARK: - Periodic work

    def tick(self, now: datetime | None = None) -> None:
        current = now or utc_now()
        self._close_expired(current)
        control = self._control()
        targets = self._targets(control)
        resources = host_anomaly.capture_resources(targets, run=self.run)
        self._sample(current, control, targets, resources)
        self._check_pressure(current, resources)
        self._save_state(last_seen=iso(current))
        if control is not None:
            self._report_requests(control, targets)
            self._deliver(control)
            self._prune(control)

    def _fingerprint_key(
        self, targets: Sequence[Mapping[str, Any]], running: Sequence[Mapping[str, Any]],
    ) -> str:
        running_rigs = frozenset(vm.get("rig_id") for vm in running)
        if (
            not self._fingerprint
            or running_rigs != self._fingerprint_rigs
            or time.monotonic() - self._fingerprint_at > FINGERPRINT_REFRESH_S
        ):
            environment = host_anomaly.capture_environment(targets, run=self.run)
            self._fingerprint = host_anomaly.fingerprint_key(environment, targets, running)
            self._fingerprint_rigs = running_rigs
            self._fingerprint_at = time.monotonic()
        return self._fingerprint

    def _sample(
        self,
        now: datetime,
        control: Any,
        targets: Sequence[Mapping[str, Any]],
        resources: Mapping[str, Any],
    ) -> None:
        interval = SAMPLE_INTERVAL_S
        if self._last_sample is not None:
            elapsed = (now - self._last_sample).total_seconds()
            # 超过缺口阈值的部分没有观测，不算暴露量；缺口另有记录。
            if 0 < elapsed <= GAP_THRESHOLD_S:
                interval = round(elapsed)
        self._last_sample = now
        active = None
        if control is not None:
            try:
                active = int(control.get_host(self.host_id).get("active_rigs") or 0)
            except Exception as exc:  # noqa: BLE001
                log.debug("active rigs unavailable: %s", exc)
        directory_counts = Counter(t.get("vm_dir") for t in targets)
        generations = {t["vm_dir"]: t["rig_generation"] for t in targets
                       if t.get("vm_dir") and t.get("rig_generation") and directory_counts[t["vm_dir"]] == 1}
        running = []
        for vm in resources["running_vms"]:
            row = {key: vm.get(key) for key in ("rig_id", "vm_dir", "memory_bytes")}
            if vm.get("helper_pids"):
                row.update(helper_pids=vm["helper_pids"], generation=generations.get(vm["vm_dir"]))
            running.append(row)
        host_anomaly.append_sample(self.root, self.host_id, {
            "sampled_at": iso(now),
            "interval_seconds": interval,
            "running_vm_count": resources["running_vm_count"],
            "running_vms": running,
            "active_rigs": active,
            "memory_free_percent": resources["memory_free_percent"],
            "pressure_level": resources["pressure_level"],
            "swap_used_bytes": resources["swap_used_bytes"],
            "load_1m": resources["load_1m"],
            "fingerprint": self._fingerprint_key(targets, resources["running_vms"]),
        })

    def _check_pressure(self, now: datetime, resources: Mapping[str, Any]) -> None:
        level = resources["pressure_level"]
        swap = resources["swap_used_bytes"]
        growth = swap - self._last_swap if swap is not None and self._last_swap is not None else 0
        if swap is not None:
            self._last_swap = swap
        pressured = (level is not None and level >= PRESSURE_LEVEL_RECORD) or growth >= PRESSURE_SWAP_GROWTH_BYTES
        if pressured:
            self._pressure_clear = 0
            if not self._in_pressure:
                self._in_pressure = True
                self._write("host_pressure", {"pressure_level": level, "swap_growth_bytes": growth}, detected_at=now)
            return
        if self._in_pressure:
            self._pressure_clear += 1
            if self._pressure_clear >= PRESSURE_CLEAR_SAMPLES:
                self._in_pressure = False

    def _report_requests(self, control: Any, targets: Sequence[Mapping[str, Any]]) -> None:
        try:
            control.report_rig_resources(self.host_id, host_anomaly.rig_resource_requests(targets))
        except Exception as exc:  # noqa: BLE001 - retried on the next tick
            log.warning("rig resource requests not reported: %s: %s", type(exc).__name__, exc)

    def _deliver(self, control: Any) -> None:
        try:
            host_anomaly.deliver_pending(self.root, self.host_id, control)
            host_anomaly.deliver_samples(self.root, self.host_id, control)
        except Exception as exc:  # noqa: BLE001 - retried on the next tick
            log.warning("anomaly delivery deferred: %s: %s", type(exc).__name__, exc)

    def _prune(self, control: Any) -> None:
        if time.monotonic() - self._last_prune < PRUNE_INTERVAL_S:
            return
        self._last_prune = time.monotonic()
        try:
            host_anomaly.prune_bundles(
                self.root, self.host_id, reviewed=control.reviewed_host_anomaly_ids(self.host_id),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("anomaly retention skipped: %s: %s", type(exc).__name__, exc)

    # MARK: - Gaps

    def replay_gap(self, now: datetime | None = None) -> None:
        """Read back what the stream missed. A gap is recorded, never read as "no anomaly"."""
        current = now or utc_now()
        last_seen = self._load_state().get("last_seen")
        if not last_seen:
            return
        start = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
        gap = (current - start).total_seconds()
        if gap <= 0:
            return
        truncated = gap > MAX_REPLAY_S
        replay_from = current - timedelta(seconds=MAX_REPLAY_S) if truncated else start
        output, status = self._log_show(replay_from, current)
        for line in output.splitlines():
            self.process_line(line, replayed=True)
        if gap > GAP_THRESHOLD_S:
            self._write("observation_gap", {
                "from": iso(start), "to": iso(current), "seconds": round(gap),
                "replay": status, "replay_truncated": truncated,
            }, detected_at=current)

    def _log_show(self, start: datetime, end: datetime) -> tuple[str, str]:
        args = [
            LOG_BINARY, "show", "--style", "compact", "--info",
            "--start", start.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            "--end", end.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            "--predicate", LOG_PREDICATE,
        ]
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=300, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return "", f"failed: {type(exc).__name__}"
        return result.stdout, "complete" if result.returncode == 0 else f"failed: exit {result.returncode}"

    # MARK: - Threads

    def start(self) -> None:
        self._thread = threading.Thread(target=self._main, name="host-anomaly-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _main(self) -> None:
        self._host_dir.mkdir(parents=True, exist_ok=True)
        with (self._host_dir / "watcher.lock").open("a") as lock:
            while not self._stop.is_set():
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    self._stop.wait(ELECTION_RETRY_S)
                    continue
                log.info("host anomaly watcher leading: host=%s root=%s", self.host_id, self.root)
                try:
                    self._lead()
                except Exception:  # noqa: BLE001 - never take the Agent down with us
                    log.exception("host anomaly watcher failed; retrying")
                    self._stop.wait(ELECTION_RETRY_S)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def _lead(self) -> None:
        threading.Thread(target=self._bundle_worker, name="host-anomaly-bundles", daemon=True).start()
        for path in host_anomaly.resume_pending_bundles(self.root, self.host_id):
            self._bundles.put(path)
        lines: queue.Queue[str | None] = queue.Queue()
        self._spawn_stream(lines)
        self.replay_gap()
        next_tick = 0.0
        while not self._stop.is_set():
            if self._process is None or self._process.poll() is not None:
                log.warning("log stream ended; reconnecting")
                self._stop.wait(5)
                self._spawn_stream(lines)
                self.replay_gap()
            if time.monotonic() >= next_tick:
                next_tick = time.monotonic() + SAMPLE_INTERVAL_S
                self.tick()
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                continue
            if line is not None:
                self.process_line(line)

    def _spawn_stream(self, lines: "queue.Queue[str | None]") -> None:
        self._process = subprocess.Popen(
            [LOG_BINARY, "stream", "--style", "compact", "--level", "info", "--predicate", LOG_PREDICATE],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        threading.Thread(
            target=_pump, args=(self._process.stdout, lines), name="host-anomaly-stream", daemon=True,
        ).start()

    def _bundle_worker(self) -> None:
        while not self._stop.is_set():
            try:
                path = self._bundles.get(timeout=1)
            except queue.Empty:
                continue
            self._stop.wait(self.collect_delay_s)
            try:
                self.collect(path, environment=self.environment)
            except Exception:  # noqa: BLE001
                log.exception("anomaly bundle collection failed: %s", path)


def _pump(stream: Iterable[str] | None, lines: "queue.Queue[str | None]") -> None:
    if stream is None:
        return
    for line in stream:
        lines.put(line)


def _default_control() -> Any:
    from autophone.application.control_plane import require_control_plane

    return require_control_plane()


def start_host_watcher(target: Any) -> HostAnomalyWatcher | None:
    """Start the watcher for an Agent. A failure here must not stop the Agent."""
    try:
        environment = {**os.environ, **target.process_environment()}
        watcher = HostAnomalyWatcher(
            host_id=target.host_id,
            own_target={
                "rig_id": target.rig_id,
                "rig_generation": target.rig_generation,
                "vm_dir": str(target.vm_dir),
                "cli_dir": str(target.cli_dir),
            },
            environment=environment,
        )
        watcher.start()
        return watcher
    except Exception:  # noqa: BLE001
        log.exception("host anomaly watcher did not start")
        return None
