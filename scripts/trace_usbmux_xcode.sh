#!/bin/bash
# trace_usbmux_xcode.sh — capture short-lived Xcode execs that talk to usbmuxd.
#
# `usbmuxd` records a client as `Xcode[pid]`, but the process can exit before a
# crash collector's `ps` snapshot.  Apple ships execsnoop, a DTrace wrapper that
# records exec-success synchronously with timestamp, PID, PPID and psargs. Some
# SIP configurations deny that probe, so `auto` falls back to a 0.2s host-process
# sampler that records PID, PPID, argv and the parent chain as JSONL. The latter
# may miss a process that exits between samples; the log states which backend ran.
#
# Usage:
#   VPHONE_USB_EXEC_TRACE=1 scripts/start_vphone.sh  # fail-open auto-start
#   scripts/trace_usbmux_xcode.sh start|status|stop
#
# Production DTrace start uses `sudo -n`; authenticate once beforehand if
# necessary. For a controlled test, VPHONE_USB_EXEC_USE_SUDO=0 runs the configured
# snooper directly. Polling never needs sudo.

set -euo pipefail

MODE="${1:-status}"
TRACE_LOG="${VPHONE_USB_EXEC_TRACE_LOG:-/tmp/vphone_usbmux_xcode_exec.log}"
PID_FILE="${VPHONE_USB_EXEC_TRACE_PID_FILE:-/tmp/vphone_usbmux_xcode_exec.pid}"
SNOOP_BIN="${VPHONE_USB_EXEC_SNOOP_BIN:-/usr/bin/execsnoop}"
TARGET_COMMAND="${VPHONE_USB_EXEC_TRACE_COMMAND:-Xcode}"
USE_SUDO="${VPHONE_USB_EXEC_USE_SUDO:-1}"
START_GRACE="${VPHONE_USB_EXEC_START_GRACE:-1}"
TRACE_BACKEND="${VPHONE_USB_EXEC_TRACE_BACKEND:-auto}"
POLL_INTERVAL="${VPHONE_USB_EXEC_POLL_INTERVAL:-0.2}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POLLER="${VPHONE_USB_EXEC_POLLER:-$SCRIPT_DIR/../tools/phase1/usbmux_xcode_trace.py}"

usage() {
    cat <<'EOF'
Usage: trace_usbmux_xcode.sh start|status|stop

Environment:
  VPHONE_USB_EXEC_TRACE_LOG       Trace output (default /tmp/vphone_usbmux_xcode_exec.log)
  VPHONE_USB_EXEC_TRACE_PID_FILE  Monitor PID (default /tmp/vphone_usbmux_xcode_exec.pid)
  VPHONE_USB_EXEC_TRACE_COMMAND   Target executable basename (default Xcode)
  VPHONE_USB_EXEC_TRACE_BACKEND   auto (default), dtrace, or poll
  VPHONE_USB_EXEC_SNOOP_BIN       execsnoop executable (default /usr/bin/execsnoop)
  VPHONE_USB_EXEC_USE_SUDO         1 uses sudo -n (default); 0 runs directly
  VPHONE_USB_EXEC_POLL_INTERVAL   Sampling fallback interval in seconds (default 0.2)
EOF
}

validate_config() {
    case "$TARGET_COMMAND" in
        ''|*[!A-Za-z0-9_.-]*)
            echo "invalid VPHONE_USB_EXEC_TRACE_COMMAND: $TARGET_COMMAND" >&2
            return 1
            ;;
    esac
    case "$USE_SUDO" in
        0|1) ;;
        *)
            echo "VPHONE_USB_EXEC_USE_SUDO must be 0 or 1, got: $USE_SUDO" >&2
            return 1
            ;;
    esac
    case "$START_GRACE" in
        ''|*[!0-9.]*|.*.*)
            echo "VPHONE_USB_EXEC_START_GRACE must be a positive number, got: $START_GRACE" >&2
            return 1
            ;;
    esac
    case "$TRACE_BACKEND" in
        auto|dtrace|poll) ;;
        *)
            echo "VPHONE_USB_EXEC_TRACE_BACKEND must be auto, dtrace, or poll, got: $TRACE_BACKEND" >&2
            return 1
            ;;
    esac
    case "$POLL_INTERVAL" in
        ''|*[!0-9.]*|.*.*)
            echo "VPHONE_USB_EXEC_POLL_INTERVAL must be a positive number, got: $POLL_INTERVAL" >&2
            return 1
            ;;
    esac
}

trace_pid() {
    [ -f "$PID_FILE" ] || return 1
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    case "$pid" in
        ''|*[!0-9]*) return 1 ;;
    esac
    printf '%s\n' "$pid"
}

is_running() {
    local pid
    pid="$(trace_pid)" || return 1
    process_is_live "$pid"
}

process_is_live() {
    local pid="$1" state
    kill -0 "$pid" 2>/dev/null || return 1
    state="$(ps -p "$pid" -o state= 2>/dev/null | tr -d '[:space:]')"
    case "$state" in
        Z*) return 1 ;;
    esac
    return 0
}

clear_stale_pid() {
    if [ -f "$PID_FILE" ] && ! is_running; then
        rm -f "$PID_FILE"
    fi
}

launch_backend() {
    local backend="$1"
    "$PYTHON_BIN" - "$backend" "$USE_SUDO" "$SNOOP_BIN" "$TARGET_COMMAND" "$TRACE_LOG" \
        "$START_GRACE" "$POLLER" "$POLL_INTERVAL" <<'PY'
import subprocess
import sys
import time

backend, use_sudo, snoop, target, trace_log, grace, poller, poll_interval = sys.argv[1:]
if backend == "dtrace":
    command = [snoop, "-e", "-v", "-c", target]
    if use_sudo == "1":
        command = ["sudo", "-n", *command]
elif backend == "poll":
    command = [sys.executable, poller, "poll", "--target", target, "--interval", poll_interval]
else:
    raise SystemExit(f"unsupported trace backend: {backend}")

with open(trace_log, "ab", buffering=0) as log:
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    # macOS can defer reaping a just-execed shell wrapper for several hundred
    # milliseconds.  A sub-second configured grace therefore cannot prove that
    # a DTrace/sudo child is viable; always observe for at least 1.5s.
    time.sleep(max(float(grace), 1.5))
    rc = proc.poll()

if rc is not None:
    print(f"{backend} trace command exited during startup (rc={rc})", file=sys.stderr)
    raise SystemExit(1)
print(proc.pid)
PY
}

start_trace() {
    validate_config
    if is_running; then
        echo "usbmux Xcode exec trace already running (pid=$(trace_pid), log=$TRACE_LOG)"
        return 0
    fi
    clear_stale_pid
    mkdir -p "$(dirname "$TRACE_LOG")" "$(dirname "$PID_FILE")"
    printf '\n# trace start %s target=%s requested_backend=%s use_sudo=%s\n' \
        "$(date '+%Y-%m-%d %H:%M:%S %z')" "$TARGET_COMMAND" "$TRACE_BACKEND" "$USE_SUDO" >> "$TRACE_LOG"

    local pid backend started=0
    backend="$TRACE_BACKEND"
    case "$TRACE_BACKEND" in
        dtrace)
            [ -x "$SNOOP_BIN" ] || {
                echo "execsnoop unavailable or not executable: $SNOOP_BIN" >&2
                return 1
            }
            ;;
        poll)
            [ -f "$POLLER" ] || {
                echo "polling helper unavailable: $POLLER" >&2
                return 1
            }
            ;;
        auto)
            if [ -x "$SNOOP_BIN" ] && pid="$(launch_backend dtrace)"; then
                backend="dtrace"
                started=1
            else
                printf '# dtrace unavailable; falling back to ps sampler\n' >> "$TRACE_LOG"
                backend="poll"
            fi
            ;;
    esac

    if [ "$backend" = "poll" ]; then
        [ -f "$POLLER" ] || {
            echo "polling helper unavailable: $POLLER" >&2
            return 1
        }
        if pid="$(launch_backend poll)"; then
            :
        else
            echo "usbmux Xcode poll trace failed to start; see $TRACE_LOG" >&2
            tail -n 10 "$TRACE_LOG" >&2 || true
            return 1
        fi
    elif [ "$backend" = "dtrace" ] && [ "$started" -eq 0 ]; then
        if pid="$(launch_backend dtrace)"; then
            :
        else
            echo "usbmux Xcode DTrace failed to start; see $TRACE_LOG" >&2
            tail -n 10 "$TRACE_LOG" >&2 || true
            return 1
        fi
    fi

    printf '%s\n' "$pid" > "$PID_FILE"
    echo "usbmux Xcode trace started (backend=$backend, pid=$pid, log=$TRACE_LOG)"
}

status_trace() {
    if is_running; then
        echo "usbmux Xcode exec trace running (pid=$(trace_pid), log=$TRACE_LOG)"
        return 0
    fi
    clear_stale_pid
    echo "usbmux Xcode exec trace not running (log=$TRACE_LOG)"
    return 1
}

stop_trace() {
    if ! is_running; then
        clear_stale_pid
        echo "usbmux Xcode exec trace already stopped"
        return 0
    fi
    local pid
    pid="$(trace_pid)"
    kill "$pid" 2>/dev/null || true
    local _
    for _ in $(seq 1 20); do
        if ! process_is_live "$pid"; then
            rm -f "$PID_FILE"
            echo "usbmux Xcode exec trace stopped (pid=$pid)"
            return 0
        fi
        sleep 0.1
    done
    echo "failed to stop usbmux Xcode exec trace pid=$pid" >&2
    return 1
}

case "$MODE" in
    start) start_trace ;;
    status) status_trace ;;
    stop) stop_trace ;;
    -h|--help|help) usage ;;
    *)
        usage >&2
        exit 2
        ;;
esac
