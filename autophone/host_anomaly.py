"""Durable host anomaly records: written locally first, delivered to PostgreSQL after.

设计见 docs/platform-host-resource-admission.md §3。记录先落盘再上报，所以 Control
Plane 不可用时不丢；所有采集都是尽力而为，取不到的字段写空值，不让记录本身失败。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from autophone.logging import get_logger
from autophone.paths import code_path, resolve_state_root
from autophone.recovery_observation import atomic_json

log = get_logger(__name__)

SCHEMA_VERSION = "host-anomaly.v1"
BUNDLE_KINDS = frozenset({"rig_down", "vm_fatal", "usb_exception", "host_pressure"})
CORRELATION_WINDOW_S = 120
BUNDLE_MAX_AGE_DAYS = 90
BUNDLE_MAX_TOTAL_BYTES = 20 * 1024**3
COLLECT_MINUTES = 15
COLLECT_TIMEOUT_S = 180
HOOK_BUNDLE_GRACE_S = 600
RECENT_WORK_S = 120

Runner = Callable[[Sequence[str]], str]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def run_text(args: Sequence[str]) -> str:
    try:
        return subprocess.run(
            list(args), capture_output=True, text=True, timeout=10, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def anomaly_root(environment: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    explicit = env.get("AUTOPHONE_HOST_ANOMALY_DIR")
    if explicit:
        return Path(explicit).resolve()
    return resolve_state_root(env) / "runs" / "host-anomalies"


def host_root(root: Path, host_id: str) -> Path:
    return root / host_id


# MARK: - Snapshots


def parse_swap_used_bytes(text: str) -> int | None:
    match = re.search(r"used\s*=\s*([\d.]+)([KMGT])", text)
    if match is None:
        return None
    scale = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[match.group(2)]
    return int(float(match.group(1)) * scale)


def parse_load_1m(text: str) -> float | None:
    match = re.search(r"\{\s*([\d.]+)", text)
    return float(match.group(1)) if match else None


def parse_memory_free_percent(text: str) -> int | None:
    match = re.search(r"free percentage:\s*(\d+)%", text)
    return int(match.group(1)) if match else None


def _as_int(text: str) -> int | None:
    try:
        return int(text.strip())
    except ValueError:
        return None


def running_vms(
    targets: Sequence[Mapping[str, Any]], *, run: Runner = run_text,
) -> list[dict[str, Any]]:
    """List running VMs, one row per VM.

    每台 VM 有两个 Virtualization XPC 进程，所以不能数进程。upstream VM 看 vphone-vm
    的 ``--config``；已登记的 Rig（含 legacy）再看持有 Disk.img 的进程。
    """
    by_dir: dict[str, dict[str, Any]] = {}
    for line in run(["/bin/ps", "-axo", "pid=,command="]).splitlines():
        match = re.search(r"^\s*(\d+)\s.*/vphone-vm\s+--config\s+(.+)/config\.plist\s*$", line)
        if match:
            by_dir[match.group(2)] = {"vm_dir": match.group(2), "rig_id": None, "helper_pids": []}
    for target in targets:
        vm_dir = str(target.get("vm_dir") or "")
        if not vm_dir:
            continue
        pids = [
            int(pid) for pid in run(["/usr/sbin/lsof", "-t", f"{vm_dir}/Disk.img"]).split()
            if pid.isdigit()
        ]
        if pids or vm_dir in by_dir:
            by_dir.setdefault(vm_dir, {"vm_dir": vm_dir, "rig_id": None, "helper_pids": []})
            by_dir[vm_dir].update(rig_id=target.get("rig_id"), helper_pids=pids)
    for row in by_dir.values():
        row["memory_bytes"] = _configured_memory(row["vm_dir"])
    return sorted(by_dir.values(), key=lambda row: row["vm_dir"])


def _configured_value(vm_dir: str, key: str) -> int | None:
    try:
        with (Path(vm_dir) / "config.plist").open("rb") as stream:
            value = plistlib.load(stream).get(key)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return int(value) if isinstance(value, int) else None


def _configured_memory(vm_dir: str) -> int | None:
    return _configured_value(vm_dir, "memorySize")


def rig_resource_requests(targets: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """What each Rig's VM asks for, read from its own config.plist."""
    requests = {}
    for target in targets:
        rig_id, vm_dir = target.get("rig_id"), target.get("vm_dir")
        if not rig_id or not vm_dir:
            continue
        memory = _configured_value(str(vm_dir), "memorySize")
        if memory is not None:
            requests[str(rig_id)] = {
                "memory_bytes": memory, "cpu_count": _configured_value(str(vm_dir), "cpuCount"),
            }
    return requests


def capture_resources(
    targets: Sequence[Mapping[str, Any]], *, run: Runner = run_text,
) -> dict[str, Any]:
    vms = running_vms(targets, run=run)
    return {
        "memory_free_percent": parse_memory_free_percent(
            run(["/usr/bin/memory_pressure", "-Q"]),
        ),
        "pressure_level": _as_int(run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"])),
        "swap_used_bytes": parse_swap_used_bytes(run(["/usr/sbin/sysctl", "-n", "vm.swapusage"])),
        "load_1m": parse_load_1m(run(["/usr/sbin/sysctl", "-n", "vm.loadavg"])),
        "running_vm_count": len(vms),
        "running_vms": vms,
    }


def _git_identity(directory: Path, *, run: Runner) -> dict[str, Any]:
    commit = run(["/usr/bin/git", "-C", str(directory), "rev-parse", "HEAD"]).strip()
    status = run(["/usr/bin/git", "-C", str(directory), "status", "--porcelain"])
    return {"dir": str(directory), "commit": commit or None, "dirty": bool(status.strip()) if commit else None}


def _config_sha256(vm_dir: str) -> str | None:
    try:
        return hashlib.sha256((Path(vm_dir) / "config.plist").read_bytes()).hexdigest()
    except OSError:
        return None


def capture_environment(
    targets: Sequence[Mapping[str, Any]], *, run: Runner = run_text,
) -> dict[str, Any]:
    cli_dirs = sorted({str(target["cli_dir"]) for target in targets if target.get("cli_dir")})
    return {
        "macos_build": run(["/usr/bin/sw_vers", "-buildVersion"]).strip() or None,
        "autophone": _git_identity(code_path(), run=run),
        "vphone_cli": [_git_identity(Path(directory), run=run) for directory in cli_dirs],
        "vm_config_sha256": {
            str(target["rig_id"]): _config_sha256(str(target["vm_dir"]))
            for target in targets if target.get("rig_id") and target.get("vm_dir")
        },
    }


def fingerprint_key(
    environment: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]] = (),
    running: Sequence[Mapping[str, Any]] = (),
) -> str:
    """The grouping key for rates: macOS build plus the vphone-cli commits of running VMs.

    只算在跑的 VM：已登记但没启动的 legacy Rig 不影响 Host 上实际跑的代码。记录和样本用
    同一个规则，发生率的分子分母才对得上。没有在跑的已登记 VM 时退回全部已登记的。
    """
    running_rigs = {vm.get("rig_id") for vm in running if vm.get("rig_id")}
    running_dirs = {str(t.get("cli_dir")) for t in targets if t.get("rig_id") in running_rigs}
    rows = environment.get("vphone_cli") or []
    selected = [row for row in rows if row.get("dir") in running_dirs] or rows
    commits = sorted({str(row.get("commit"))[:12] for row in selected if row.get("commit")})
    return f"{environment.get('macos_build') or 'unknown'}/{'+'.join(commits) or 'unknown'}"


def vm_process_alive(target: Mapping[str, Any], *, run: Runner = run_text) -> bool | None:
    """Whether a process still holds this VM's disk; ``None`` when the VM is not identified.

    每台 VM 持有 Disk.img 的 Virtualization 进程崩溃后就不在了。负载造成的探测超时不会让它
    消失（2026-11-04 13:09 实测）。
    """
    vm_dir = target.get("vm_dir")
    if not vm_dir:
        return None
    return any(pid.isdigit() for pid in run(["/usr/sbin/lsof", "-t", f"{vm_dir}/Disk.img"]).split())


def capture_concurrency(control: Any, host_id: str) -> dict[str, Any]:
    if control is None:
        return {"status": "unavailable", "error": "no control plane"}
    try:
        work = control.host_active_work(host_id, recent_seconds=RECENT_WORK_S)
    except Exception as exc:  # noqa: BLE001 - a record must survive a Control Plane outage
        return {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
    active = {row["rig_id"] for row in work if not row.get("released_at")}
    return {"status": "captured", "active_rigs": len(active), "work": work}


# MARK: - Records


def correlate(root: Path, host_id: str, now: datetime, *, window_s: int = CORRELATION_WINDOW_S) -> tuple[str, bool]:
    """Return ``(correlation_id, is_new)``.

    unified log 是 Host 级的：短时间内的多条异常共用一个诊断包，否则同一次崩溃会
    并发采集多份内容相同的日志。
    """
    directory = host_root(root, host_id)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".correlation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / "correlation.json"
        try:
            state = json.loads(path.read_text())
            last = datetime.fromisoformat(state["last_at"].replace("Z", "+00:00"))
            correlation_id = str(state["correlation_id"])
        except (OSError, ValueError, KeyError):
            last, correlation_id = None, ""
        is_new = last is None or (now - last).total_seconds() > window_s
        if is_new:
            correlation_id = f"HAC-{uuid.uuid4().hex}"
        atomic_json(path, {"correlation_id": correlation_id, "last_at": iso(now)})
    return correlation_id, is_new


def write_record(
    root: Path,
    *,
    kind: str,
    host_id: str,
    rigs: Sequence[Mapping[str, Any]],
    details: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
    control: Any = None,
    detected_at: datetime | None = None,
    run: Runner = run_text,
) -> Path:
    """Persist one anomaly. Returns the path of its ``record.json``."""
    detected = detected_at or utc_now()
    anomaly_id = f"HA-{uuid.uuid4().hex}"
    correlation_id, is_new = correlate(root, host_id, detected)
    directory = host_root(root, host_id) / f"{detected:%Y-%m-%d}" / anomaly_id
    wants_bundle = kind in BUNDLE_KINDS
    if not wants_bundle:
        bundle: dict[str, Any] = {"status": "not_applicable"}
    elif is_new or kind == "rig_down":
        # rig_down 总是采：健康日志按 Rig 分开，共用别人的包会丢这一台的记录。
        bundle = {"status": "pending", "path": str(directory / "bundle")}
    else:
        bundle = {"status": "shared", "correlation_id": correlation_id}
    environment = capture_environment(targets, run=run)
    resources = capture_resources(targets, run=run)
    record = {
        "schema_version": SCHEMA_VERSION,
        "anomaly_id": anomaly_id,
        "kind": kind,
        "correlation_id": correlation_id,
        "detected_at": iso(detected),
        "host_id": host_id,
        "rigs": [dict(rig) for rig in rigs],
        "environment": environment,
        "fingerprint": fingerprint_key(environment, targets, resources["running_vms"]),
        "resources": resources,
        "concurrency": capture_concurrency(control, host_id),
        "details": dict(details),
        "bundle": bundle,
        "delivery": "pending",
    }
    path = directory / "record.json"
    atomic_json(path, record)
    return path


def update_record(path: Path, **changes: Any) -> dict[str, Any]:
    """Merge top-level changes and mark the record for redelivery."""
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = json.loads(path.read_text())
        record.update(changes, delivery="pending")
        atomic_json(path, record)
    return record


def bump_occurrences(path: Path, count: int, *, last_at: datetime) -> None:
    record = json.loads(path.read_text())
    details = {**record.get("details", {}), "occurrences": count, "last_at": iso(last_at)}
    update_record(path, details=details)


def _bundle_digest(bundle_dir: Path) -> str:
    lines = []
    for item in sorted(bundle_dir.rglob("*")):
        if item.is_file() and item.name != "SHA256SUMS":
            digest = hashlib.sha256(item.read_bytes()).hexdigest()
            lines.append(f"{digest}  {item.relative_to(bundle_dir)}\n")
    manifest = "".join(lines)
    (bundle_dir / "SHA256SUMS").write_text(manifest, encoding="utf-8")
    return hashlib.sha256(manifest.encode()).hexdigest()


def collect_bundle(
    path: Path,
    *,
    environment: Mapping[str, str],
    collector: Path | None = None,
    minutes: int = COLLECT_MINUTES,
    timeout_s: int = COLLECT_TIMEOUT_S,
) -> dict[str, Any]:
    """Run the crash collector into this record's bundle directory.

    失败不抛出：熔断和恢复都不能依赖诊断包，结果只写进记录的 bundle 状态。
    """
    bundle_dir = path.parent / "bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    script = collector or Path(
        environment.get("VPHONE_DOWN_COLLECT_SCRIPT") or code_path("scripts", "collect_vphone_crash.sh")
    )
    child_env = {
        **environment,
        "VPHONE_CRASH_OUTPUT_ROOT": str(bundle_dir),
        "PYTHON_BIN": environment.get("PYTHON_BIN") or sys.executable,
    }
    child_env.pop("VPHONE_CRASH_LOG_START", None)
    child_env.pop("VPHONE_CRASH_LOG_END", None)
    status = "complete"
    error = ""
    try:
        with (path.parent / "collector.log").open("wb") as output:
            result = subprocess.run(
                [str(script), str(minutes)], env=child_env, stdout=output,
                stderr=subprocess.STDOUT, timeout=timeout_s, check=False,
            )
        if result.returncode != 0:
            status, error = "failed", f"collector exit {result.returncode}"
    except subprocess.TimeoutExpired:
        status, error = "truncated", f"collector exceeded {timeout_s}s"
    except OSError as exc:
        status, error = "failed", f"{type(exc).__name__}: {exc}"
    return finish_bundle(path, status=status, error=error)


def finish_bundle(path: Path, *, status: str, error: str = "") -> dict[str, Any]:
    """Seal the bundle directory and write its state into the record."""
    bundle_dir = path.parent / "bundle"
    if status == "complete" and not any(bundle_dir.rglob("SUMMARY.txt")):
        status, error = "failed", "collector left no SUMMARY.txt"
    bundle: dict[str, Any] = {"status": status, "path": str(bundle_dir)}
    if error:
        bundle["error"] = error
    if bundle_dir.is_dir():
        try:
            bundle["sha256"] = _bundle_digest(bundle_dir)
        except OSError as exc:
            bundle.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    update_record(path, bundle=bundle)
    return bundle


def resume_pending_bundles(root: Path, host_id: str, *, now: datetime | None = None) -> list[Path]:
    """Sort out bundles an interrupted process left in ``pending``; return those to collect.

    队列只在内存里，Agent 重启后不处理就会永远停在 pending。rig_down 的包由掉线钩子
    自己采，所以只在明显超时后才判定为中断，否则可能和还在跑的钩子抢同一个目录。
    """
    current = now or utc_now()
    resume = []
    for path in sorted(host_root(root, host_id).glob("*/HA-*/record.json")):
        try:
            record = json.loads(path.read_text())
            detected = datetime.fromisoformat(record["detected_at"].replace("Z", "+00:00"))
        except (OSError, ValueError, KeyError):
            continue
        if record.get("bundle", {}).get("status") != "pending":
            continue
        if record["kind"] != "rig_down":
            resume.append(path)
        elif (current - detected).total_seconds() > HOOK_BUNDLE_GRACE_S:
            finish_bundle(path, status="failed", error="collector interrupted")
    return resume


def pending_records(root: Path, host_id: str) -> list[Path]:
    paths = []
    for path in sorted(host_root(root, host_id).glob("*/HA-*/record.json")):
        try:
            if json.loads(path.read_text()).get("delivery") != "delivered":
                paths.append(path)
        except (OSError, ValueError):
            log.warning("unreadable anomaly record: %s", path)
    return paths


def deliver_pending(root: Path, host_id: str, control: Any) -> int:
    """Deliver every pending record; the rest stay on disk for the next pass."""
    delivered = 0
    for path in pending_records(root, host_id):
        record = json.loads(path.read_text())
        control.record_host_anomaly({k: v for k, v in record.items() if k != "delivery"})
        with path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = json.loads(path.read_text())
            # 投递期间记录被改过（比如诊断包刚写完）就留给下一轮，不能把新内容标成已投递。
            if current == record:
                atomic_json(path, {**record, "delivery": "delivered"})
                delivered += 1
    return delivered


# MARK: - Load samples


def append_sample(root: Path, host_id: str, sample: Mapping[str, Any]) -> None:
    path = host_root(root, host_id) / "samples-pending.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(sample, sort_keys=True) + "\n")


def deliver_samples(root: Path, host_id: str, control: Any) -> int:
    path = host_root(root, host_id) / "samples-pending.jsonl"
    if not path.is_file():
        return 0
    with path.open("r+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        rows = []
        for line in stream:
            try:
                rows.append(json.loads(line))
            except ValueError:
                log.warning("dropping unreadable load sample in %s", path)
        if rows:
            control.record_host_load_samples(host_id, rows)
        # 写库幂等，崩在这里只会重投，不会丢。
        stream.seek(0)
        stream.truncate()
    return len(rows)


# MARK: - Retention


def prune_bundles(
    root: Path,
    host_id: str,
    *,
    reviewed: set[str],
    now: datetime | None = None,
    max_age_days: int = BUNDLE_MAX_AGE_DAYS,
    max_total_bytes: int = BUNDLE_MAX_TOTAL_BYTES,
) -> dict[str, list[str]]:
    """Delete old bundles that already have a review; never an unreviewed one.

    只删 bundle 目录，record.json 保留。
    """
    current = now or utc_now()
    rows = []
    for path in sorted(host_root(root, host_id).glob("*/HA-*/record.json")):
        bundle_dir = path.parent / "bundle"
        if not bundle_dir.is_dir():
            continue
        try:
            record = json.loads(path.read_text())
            detected = datetime.fromisoformat(record["detected_at"].replace("Z", "+00:00"))
        except (OSError, ValueError, KeyError):
            continue
        size = sum(item.stat().st_size for item in bundle_dir.rglob("*") if item.is_file())
        rows.append({"path": path, "dir": bundle_dir, "id": record["anomaly_id"], "at": detected, "size": size})
    rows.sort(key=lambda row: row["at"])
    total = sum(row["size"] for row in rows)
    cutoff = current - timedelta(days=max_age_days)
    pruned: list[str] = []
    unreviewed: list[str] = []
    for row in rows:
        expired = row["at"] < cutoff
        if not expired and total <= max_total_bytes:
            continue
        if row["id"] not in reviewed:
            unreviewed.append(row["id"])
            continue
        shutil.rmtree(row["dir"])
        total -= row["size"]
        pruned.append(row["id"])
        record = json.loads(row["path"].read_text())
        update_record(row["path"], bundle={**record["bundle"], "status": "pruned", "pruned_at": iso(current)})
    if unreviewed:
        log.warning("anomaly bundles over retention wait for a review: %s", ", ".join(unreviewed))
    return {"pruned": pruned, "unreviewed": unreviewed}
