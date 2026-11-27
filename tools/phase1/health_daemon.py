"""Own the single ``vsock_health`` process for one vphone socket.

``start_vphone.sh`` can be entered by an automatic down hook and by a human at
nearly the same time.  A process scan followed by a later ``Popen`` is not an
atomic ownership claim, so this module keeps the claim in a PID file protected
by a separate advisory lock.  It also heals installations made before this
manager existed by retaining one exact-socket daemon and stopping duplicates.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path


DEFAULT_PID_FILE = "/tmp/vphone_health.pid"
DEFAULT_LOG_FILE = "/tmp/vphone_health.log"


class HealthDaemonError(RuntimeError):
    """The caller cannot establish exactly one usable health daemon."""


@dataclass(frozen=True)
class HealthDaemonResult:
    status: str  # started | existing
    pid: int
    duplicates_stopped: tuple[int, ...] = ()


def _canonical_sock(sock: str) -> str:
    """Return one stable identity for equivalent spellings of a socket path."""
    return os.path.realpath(os.path.abspath(os.path.expanduser(sock)))


def _command_for_pid(pid: int) -> str:
    out = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    # A child owned by a different parent can stay listed as a zombie until its
    # parent reaps it. It owns no listener and must not block a replacement.
    out = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="],
        capture_output=True,
        text=True,
    )
    return out.returncode == 0 and "Z" not in out.stdout.strip()


def _flag_value(command: str, flag: str) -> str | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    for index, value in enumerate(parts[:-1]):
        if value == flag:
            return parts[index + 1]
    return None


def _is_matching_daemon(pid: int, sock: str) -> bool:
    if not _process_alive(pid):
        return False
    command = _command_for_pid(pid)
    daemon_sock = _flag_value(command, "--sock")
    return (
        "tools.phase1.vsock_health" in command
        and daemon_sock is not None
        and _canonical_sock(daemon_sock) == _canonical_sock(sock)
    )


def matching_health_pids(sock: str) -> list[int]:
    """Return live health daemons whose parsed ``--sock`` is exactly *sock*."""
    sock = _canonical_sock(sock)
    out = subprocess.run(
        ["pgrep", "-f", "tools.phase1.vsock_health"],
        capture_output=True,
        text=True,
    )
    pids: list[int] = []
    for raw in out.stdout.splitlines():
        try:
            pid = int(raw)
        except ValueError:
            continue
        if _is_matching_daemon(pid, sock):
            pids.append(pid)
    return sorted(set(pids))


def _read_record(pid_file: Path, sock: str) -> int | None:
    try:
        record = json.loads(pid_file.read_text(encoding="utf-8"))
        pid = int(record.get("pid"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if _canonical_sock(str(record.get("sock", ""))) != _canonical_sock(sock):
        return None
    if not _process_alive(pid):
        return None

    command = _command_for_pid(pid)
    if not command:
        # The manager can run under a restricted parent where process-table
        # inspection is denied. The lock-protected PID file is still our own
        # ownership record, so trusting its live PID is safer than spawning a
        # second writer for the same state file and recovery hook.
        return pid
    daemon_sock = _flag_value(command, "--sock")
    return pid if (
        "tools.phase1.vsock_health" in command
        and daemon_sock is not None
        and _canonical_sock(daemon_sock) == _canonical_sock(sock)
    ) else None


def _write_record(pid_file: Path, *, pid: int, sock: str, state_file: Path) -> None:
    sock = _canonical_sock(sock)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{pid_file.name}.", dir=pid_file.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(
                {
                    "pid": pid,
                    "sock": sock,
                    "state_file": str(state_file),
                    "recorded_at": time.time(),
                },
                handle,
            )
        os.replace(tmp_name, pid_file)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


@contextlib.contextmanager
def _manager_lock(pid_file: Path):
    """Serialize scan → reap → spawn without locking the replaceable PID file."""
    lock_file = pid_file.with_name(f"{pid_file.name}.lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _terminate(pid: int, *, timeout_s: float = 3.0) -> None:
    if not _process_alive(pid):
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return
        time.sleep(0.05)
    raise HealthDaemonError(f"health daemon pid={pid} did not exit")


def _reap_duplicates(sock: str, pid_file: Path) -> tuple[int | None, tuple[int, ...]]:
    """Keep the recorded owner when valid, otherwise one deterministic PID."""
    sock = _canonical_sock(sock)
    matching = matching_health_pids(sock)
    recorded = _read_record(pid_file, sock)
    if recorded is not None and recorded not in matching:
        matching.append(recorded)
    if not matching:
        return None, ()

    keeper = recorded if recorded in matching else min(matching)
    stopped: list[int] = []
    for pid in matching:
        if pid == keeper:
            continue
        _terminate(pid)
        stopped.append(pid)
    return keeper, tuple(sorted(stopped))


def ensure_health_daemon(
    *,
    sock: str,
    state_file: Path,
    on_down: str,
    pid_file: Path,
    log_file: Path = Path(DEFAULT_LOG_FILE),
    interval: float = 3.0,
    detach: bool = True,
) -> HealthDaemonResult:
    """Return only after one exact-socket daemon owns the requested identity."""
    sock = _canonical_sock(sock)
    with _manager_lock(pid_file):
        keeper, stopped = _reap_duplicates(sock, pid_file)
        if keeper is not None:
            _write_record(pid_file, pid=keeper, sock=sock, state_file=state_file)
            return HealthDaemonResult("existing", keeper, stopped)

        state_file.unlink(missing_ok=True)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("ab", buffering=0) as log:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "tools.phase1.vsock_health",
                    "--sock",
                    sock,
                    "--interval",
                    str(interval),
                    "--state-file",
                    str(state_file),
                    "--log-file",
                    str(log_file),
                    "--on-down",
                    on_down,
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=detach,
            )
        if not _process_alive(proc.pid):
            raise HealthDaemonError("health daemon exited immediately after start")
        _write_record(pid_file, pid=proc.pid, sock=sock, state_file=state_file)
        return HealthDaemonResult("started", proc.pid, stopped)


def stop_health_daemons(*, sock: str, pid_file: Path) -> tuple[int, ...]:
    """Synchronously stop exact-socket daemons before replacing a dead VM."""
    sock = _canonical_sock(sock)
    with _manager_lock(pid_file):
        pids = matching_health_pids(sock)
        recorded = _read_record(pid_file, sock)
        if recorded is not None and recorded not in pids:
            pids.append(recorded)
        pids = sorted(set(pids))
        for pid in pids:
            _terminate(pid)
        with contextlib.suppress(FileNotFoundError):
            pid_file.unlink()
        return tuple(pids)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("ensure", "stop"))
    parser.add_argument("--sock", required=True)
    parser.add_argument("--state-file", default="/tmp/vphone_health.json")
    parser.add_argument("--pid-file", default=DEFAULT_PID_FILE)
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE)
    parser.add_argument("--on-down", default="scripts/on_vphone_down.sh")
    parser.add_argument("--interval", type=float, default=3.0)
    args = parser.parse_args()
    pid_file = Path(args.pid_file)

    try:
        if args.action == "stop":
            stopped = stop_health_daemons(sock=args.sock, pid_file=pid_file)
            print(json.dumps({"status": "stopped", "pids": stopped}))
        else:
            result = ensure_health_daemon(
                sock=args.sock,
                state_file=Path(args.state_file),
                on_down=args.on_down,
                pid_file=pid_file,
                log_file=Path(args.log_file),
                interval=args.interval,
            )
            print(json.dumps(asdict(result)))
    except HealthDaemonError as exc:
        print(f"health daemon manager: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
