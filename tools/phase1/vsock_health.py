"""vsock_health.py — background health probe for vphone.sock + guest vsock.

Runs as an independent process. Every `interval_s` seconds it:
  1. UDS health: sends {"t":"screenshot"} (host-only, no guest roundtrip).
  2. Vsock health: sends {"t":"app_list","filter":"running"}.
     This routes to vphoned over vsock port 1337 and exercises the full chain.

Results feed a rolling window. We map (last-10 ok count) → state:
    healthy   ≥ 8
    degraded  4..7
    down      ≤ 3

State + timings are written atomically to --state-file (default
/tmp/vphone_health.json). Readers (see vsock_health_client.py) use these
to decide whether to route a call, pick a timeout, or refuse.

Usage:
    python3 -m tools.phase1.vsock_health \
        --interval 3 --window 10 --state-file /tmp/vphone_health.json

Stop with Ctrl-C or SIGTERM. The daemon is stateless across restarts; the
rolling window resets.

Design notes:
- UDS is a unix socket; failures here mean vphone-cli host process is dead
  or its UDS listener is broken. Distinguish from vsock failures in the
  state file so callers can act differently (e.g. vsock-only command
  blocked, but UDS-only command may still work).
- Vsock probe uses `app_list` because:
    * it is a guest roundtrip (vphoned over vsock), so it exercises the full
      chain — exactly what we need to distinguish vsock from UDS failures
    * it is read-only: unlike `type` (UIPasteboard setString:) it does NOT
      clobber the iOS clipboard. The old `type` heartbeat raced any in-flight
      paste-based flow (e.g. the login operation), leaking the heartbeat string
      "vphone-health-heartbeat" into login fields.
    * key would change volume / press home visibly; open_app is not a UDS cmd.
- We deliberately do NOT reuse vphone_uds.py's `request` directly because
  the daemon must survive ALL exception types (ConnectionRefused, timeout,
  malformed JSON, short read, ...) and keep probing. We wrap it in
  catch-everything.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Add repo root to sys.path so this can run via `python -m tools.phase1.vsock_health`
# AND be importable without path surgery.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from autophone.device.uds import DEFAULT_SOCK, request  # noqa: E402


@dataclass
class ProbeResult:
    ok: bool
    rt_ms: float
    error: str | None = None


@dataclass
class Snapshot:
    ts: float
    state: str                      # "healthy" | "degraded" | "down" | "unknown"
    uds_state: str                  # "healthy" | "degraded" | "down"
    vsock_state: str                # "healthy" | "degraded" | "down"
    uds_window: list[bool] = field(default_factory=list)
    vsock_window: list[bool] = field(default_factory=list)
    uds_rt_p50_ms: float | None = None
    vsock_rt_p50_ms: float | None = None
    uds_rt_max_ms: float | None = None
    vsock_rt_max_ms: float | None = None
    last_error: str | None = None
    last_probe_ts: float | None = None
    probe_count: int = 0


def probe_uds(sock_path: str, timeout_s: float) -> ProbeResult:
    try:
        t0 = time.perf_counter()
        _, s = request({"t": "screenshot"}, sock_path=sock_path, timeout=timeout_s,
                       parse_image=False)
        return ProbeResult(ok=bool(s.ok), rt_ms=s.rt_ms)
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as e:
        return ProbeResult(ok=False, rt_ms=round((time.perf_counter() - t0) * 1000, 2)
                           if 't0' in locals() else 0.0, error=f"{type(e).__name__}: {e}")
    except Exception as e:   # last-resort: keep daemon alive
        return ProbeResult(ok=False, rt_ms=0.0, error=f"{type(e).__name__}: {e}")


def probe_vsock(sock_path: str, timeout_s: float) -> ProbeResult:
    # `app_list` routes through vphoned over vsock (guest roundtrip), so it
    # exercises the same chain `type` did — but read-only: it does NOT clobber
    # the iOS clipboard. (`type` is UIPasteboard setString:, and a 3s probe
    # would race any in-flight paste-based flow like the login operation.)
    try:
        t0 = time.perf_counter()
        resp, s = request(
            {"t": "app_list", "filter": "running"},
            sock_path=sock_path, timeout=timeout_s,
        )
        # `ok: false` with "guest not connected" means UDS is alive but
        # vsock/vphoned is down — distinct from UDS failure. A healthy
        # roundtrip returns the `apps` list; treat its absence as a failure.
        if not s.ok or "apps" not in resp:
            return ProbeResult(ok=False, rt_ms=s.rt_ms,
                               error=resp.get("error") or "ok=false")
        return ProbeResult(ok=True, rt_ms=s.rt_ms)
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as e:
        return ProbeResult(ok=False, rt_ms=round((time.perf_counter() - t0) * 1000, 2)
                           if 't0' in locals() else 0.0, error=f"{type(e).__name__}: {e}")
    except Exception as e:
        return ProbeResult(ok=False, rt_ms=0.0, error=f"{type(e).__name__}: {e}")


def classify(window: deque[bool], capacity: int) -> str:
    """Map rolling window to state. Conservative during warmup."""
    if len(window) < 3:
        return "unknown"
    ok = sum(window)
    if len(window) < capacity:
        # During warmup, require stricter evidence of health
        return "healthy" if ok == len(window) else ("down" if ok == 0 else "degraded")
    if ok >= max(1, int(capacity * 0.8)):
        return "healthy"
    if ok <= max(0, int(capacity * 0.3)):
        return "down"
    return "degraded"


def combine(uds: str, vsock: str) -> str:
    # Overall state is the WORSE of the two; unknown means we just started.
    order = ["healthy", "degraded", "down", "unknown"]
    # unknown sorts last; if either is down, overall is down; etc.
    if "down" in (uds, vsock):
        return "down"
    if "degraded" in (uds, vsock):
        return "degraded"
    if "unknown" in (uds, vsock):
        return "unknown"
    return "healthy"


def p50(values: list[float]) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[len(v) // 2], 2)


def fire_on_down(cmd: str, log) -> None:
    """在 overall→down 边沿持久化观测后 detached 调用采集钩子。

    必须**绝不**拖累或拖垮探测循环：整段 catch-all，子进程 detached（start_new_session
    + DEVNULL），不 wait。观测存入持久化目录，钩子不直接恢复 VM。
    """
    try:
        environment = dict(os.environ)
        if Path(cmd).resolve() == Path(__file__).resolve().parents[2] / 'scripts/on_vphone_down.sh':
            from autophone.recovery_observation import persist_edge
            # AUTOPHONE_STAGE_LOG is scoped to the startup invocation that
            # launched this daemon.  Down-edge and recovery events belong to
            # the Rig's managed recovery stream instead.
            environment.pop('AUTOPHONE_STAGE_LOG', None)
            environment['AUTOPHONE_DOWN_OBSERVATION'] = str(persist_edge(environment))
        subprocess.Popen(
            [cmd],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=environment,
        )
        log.write(f"{time.strftime('%H:%M:%S')} DOWN edge → fired hook: {cmd}\n")
    except Exception as e:  # noqa: BLE001 - 钩子失败绝不能中断守护
        log.write(f"{time.strftime('%H:%M:%S')} DOWN edge → hook FAILED: "
                  f"{type(e).__name__}: {e}\n")
    log.flush()


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".health_", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.rename(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sock", default=DEFAULT_SOCK,
                    help="Path to vphone.sock")
    ap.add_argument("--interval", type=float, default=3.0,
                    help="Seconds between probe cycles (default 3.0)")
    ap.add_argument("--window", type=int, default=10,
                    help="Rolling window size (default 10)")
    ap.add_argument("--uds-timeout", type=float, default=2.0,
                    help="UDS screenshot probe timeout")
    ap.add_argument("--vsock-timeout", type=float, default=5.0,
                    help="Vsock type probe timeout (must cover type p99 ~1.3s)")
    ap.add_argument("--state-file", default="/tmp/vphone_health.json")
    ap.add_argument("--log-file", default="/tmp/vphone_health.log")
    ap.add_argument("--on-down", default=None,
                    help="崩溃边沿(overall→down)时 detached 调用的钩子命令，"
                         "如 scripts/on_vphone_down.sh。只在翻进 down 时触发一次，"
                         "回到 healthy 后重新武装。不设则不触发。")
    ap.add_argument("--once", action="store_true",
                    help="Run one cycle and exit (for smoke tests)")
    args = ap.parse_args()

    state_path = Path(args.state_file)
    log_path = Path(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    uds_window: deque[bool] = deque(maxlen=args.window)
    vsock_window: deque[bool] = deque(maxlen=args.window)
    uds_rts: deque[float] = deque(maxlen=args.window)
    vsock_rts: deque[float] = deque(maxlen=args.window)

    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    probe_count = 0
    # 崩溃边沿去抖：只在「非 down → down」翻转触发一次钩子，回到 healthy 才重新武装。
    # 避免掉线期间每个 cycle 都拉起采集/告警。unknown（刚启动/热身）不算已武装。
    down_armed = True
    recovery_retry = None
    recovery_retry_at = 0.0
    log = log_path.open("a")
    log.write(f"\n---- vsock_health started pid={os.getpid()} "
              f"sock={args.sock} interval={args.interval}s "
              f"window={args.window} ----\n")
    log.flush()

    while running:
        if (not args.once and args.on_down and
                Path(args.on_down).resolve() == Path(__file__).resolve().parents[2] / 'scripts/on_vphone_down.sh' and
                time.monotonic() >= recovery_retry_at and
                (recovery_retry is None or recovery_retry.poll() is not None)):
            python = Path(__file__).resolve().parents[2] / '.venv/bin/python'
            try:
                recovery_retry = subprocess.Popen(
                    [str(python) if python.is_file() else sys.executable,
                     '-m', 'autophone.recovery_observation', '--flush'],
                    cwd=Path(__file__).resolve().parents[2],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                log.write(f'Recovery outbox retry failed: {type(exc).__name__}\n')
            recovery_retry_at = time.monotonic() + 30
        t_cycle_start = time.perf_counter()
        uds = probe_uds(args.sock, args.uds_timeout)
        vsock = probe_vsock(args.sock, args.vsock_timeout)

        uds_window.append(uds.ok)
        vsock_window.append(vsock.ok)
        if uds.ok:
            uds_rts.append(uds.rt_ms)
        if vsock.ok:
            vsock_rts.append(vsock.rt_ms)

        uds_state = classify(uds_window, args.window)
        vsock_state = classify(vsock_window, args.window)
        overall = combine(uds_state, vsock_state)

        # Keep only the most recent error for quick reads
        last_err = vsock.error or uds.error

        snap = Snapshot(
            ts=time.time(),
            state=overall,
            uds_state=uds_state,
            vsock_state=vsock_state,
            uds_window=list(uds_window),
            vsock_window=list(vsock_window),
            uds_rt_p50_ms=p50(list(uds_rts)),
            vsock_rt_p50_ms=p50(list(vsock_rts)),
            uds_rt_max_ms=round(max(uds_rts), 2) if uds_rts else None,
            vsock_rt_max_ms=round(max(vsock_rts), 2) if vsock_rts else None,
            last_error=last_err,
            last_probe_ts=time.time(),
            probe_count=probe_count + 1,
        )
        probe_count += 1

        atomic_write_json(state_path, asdict(snap))
        log.write(
            f"{time.strftime('%H:%M:%S')} "
            f"overall={overall:8s} uds={uds_state:8s}({uds.ok}) "
            f"vsock={vsock_state:8s}({vsock.ok}) "
            f"uds_rt={uds.rt_ms}ms vsock_rt={vsock.rt_ms}ms "
            f"err={last_err or '-'}\n"
        )
        log.flush()

        # 崩溃边沿：翻进 down 且已武装 → 触发钩子一次并解除武装；回到 healthy 才重新武装。
        if args.on_down:
            if overall == "down" and down_armed:
                fire_on_down(args.on_down, log)
                down_armed = False
            elif overall == "healthy":
                down_armed = True

        if args.once:
            break

        # Sleep the remainder of the interval, interruptible
        elapsed = time.perf_counter() - t_cycle_start
        sleep_left = args.interval - elapsed
        while running and sleep_left > 0:
            chunk = min(0.25, sleep_left)
            time.sleep(chunk)
            sleep_left -= chunk

    log.write(f"---- stopped pid={os.getpid()} after {probe_count} cycles ----\n")
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
