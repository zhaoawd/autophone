#!/usr/bin/env python3
"""Fail-closed guest outbound-TCP health check for the VZNAT attachment.

The guest has no network client binaries at all (no curl/nc/wget, and — on
vm — no ssh either), so the probe uses bash's ``/dev/tcp`` redirection:
opening ``/dev/tcp/<host>/<port>`` performs a real TCP handshake and nothing
else.  ``timeout(1)`` bounds it, so a blackholed route surfaces as rc=124
instead of hanging until the guest shell deadline.

Three outcomes, deliberately kept apart (an earlier ssh-based probe collapsed
them and reported "network down" whenever the probe binary was missing):

- ``healthy``            — handshake completed.
- ``unhealthy``          — handshake refused / timed out / DNS failed.
- ``probe_unavailable``  — the probe itself could not run (bash or timeout
  missing, guest shell unreachable).  This is *not* evidence about NAT.

Exit status mirrors that: 0 / 1 / 2.

Name resolution inside the guest is not ready the moment the VM boots: after an
automatic restart the probe observed ``nodename nor servname provided`` 44 s in
and ``healthy`` from the same probe later, so a single unhealthy sample right
after boot is not sufficient evidence that NAT is down.  ``probe_nat_with_retry``
therefore re-probes ``dns_failed`` / ``no_route`` results within a bounded
budget.  ``refused`` and ``timed_out`` are deliberately *not* retried: a refusal
is an answer from a reachable peer, and rc=124 is the blackholed-InternetSharing
signature this gate exists to catch — retrying either would only delay the
failure by the whole budget.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import time
from datetime import datetime, timezone
from typing import Any, Callable

from autophone.device.shell import ShellError, ShellResult, ShellTimeout, run_shell
from autophone.logging import get_logger, level_from_env, setup_logging

logger = get_logger(__name__)

DEFAULT_HOST = "www.baidu.com"
DEFAULT_PORT = 443

# Bounded re-probe for boot-transient name resolution (BL-152).
DEFAULT_RETRY_INTERVAL_S = 5.0
DEFAULT_RETRY_BUDGET_S = 60.0

STATUS_HEALTHY = "healthy"
STATUS_UNHEALTHY = "unhealthy"
STATUS_PROBE_UNAVAILABLE = "probe_unavailable"

EXIT_HEALTHY = 0
EXIT_UNHEALTHY = 1
EXIT_PROBE_UNAVAILABLE = 2

# Markers the guest script prints; parsed instead of scraping bash prose.
MARKER_TOOLING_MISSING = "NAT_PROBE_TOOLING_MISSING"
MARKER_RC = "NAT_PROBE_RC"

TIMEOUT_RC = 124  # GNU timeout(1) on expiry

# Hostnames only (the probe is a health check, not a general dialer).
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_EVIDENCE_MARKERS = (
    MARKER_TOOLING_MISSING,
    MARKER_RC,
    "Connection refused",
    "Operation timed out",
    "No route to host",
    "Network is unreachable",
    "nodename nor servname",
    "Name or service not known",
    "Invalid argument",
)


def build_probe_command(host: str, port: int, *, connect_timeout_s: int) -> str:
    """Guest-side sh script: locate tooling, then one bounded TCP handshake.

    ``host``/``port`` are passed as positional arguments so they are never
    interpolated into the bash snippet itself.
    """
    return "\n".join((
        'probe_bash=$(command -v bash 2>/dev/null || true)',
        'probe_timeout=$(command -v timeout 2>/dev/null || true)',
        'if [ -z "$probe_bash" ] || [ -z "$probe_timeout" ]; then',
        f'    echo "{MARKER_TOOLING_MISSING} '
        'bash=${probe_bash:--} timeout=${probe_timeout:--}"',
        '    exit 0',
        'fi',
        '"$probe_timeout" {timeout_s} "$probe_bash" -c '
        "'exec 3<>/dev/tcp/$1/$2' nat_probe {host} {port} 2>&1".format(
            timeout_s=int(connect_timeout_s),
            host=shlex.quote(host),
            port=int(port),
        ),
        f'echo "{MARKER_RC} $?"',
    ))


def _evidence(output: str) -> list[str]:
    return [
        line.strip()
        for line in output.splitlines()
        if any(marker in line for marker in _EVIDENCE_MARKERS)
    ][-12:]


def _parse_rc(output: str) -> int | None:
    for line in reversed(output.splitlines()):
        if line.startswith(MARKER_RC):
            parts = line.split()
            if len(parts) >= 2 and parts[1].lstrip("-").isdigit():
                return int(parts[1])
    return None


def classify_probe(result: ShellResult, *, host: str, port: int) -> dict[str, Any]:
    output = "\n".join((result.stdout, result.stderr))
    rc = _parse_rc(output)
    tooling_missing = MARKER_TOOLING_MISSING in output

    if tooling_missing:
        status, reason = STATUS_PROBE_UNAVAILABLE, "guest lacks bash/timeout"
    elif rc is None:
        status, reason = STATUS_PROBE_UNAVAILABLE, "probe produced no result marker"
    elif rc == 0:
        status, reason = STATUS_HEALTHY, "tcp handshake completed"
    elif rc == TIMEOUT_RC:
        status, reason = STATUS_UNHEALTHY, "tcp connect timed out"
    else:
        status, reason = STATUS_UNHEALTHY, "tcp connect failed"

    timed_out = rc == TIMEOUT_RC or result.timed_out
    return {
        "status": status,
        "connected": status == STATUS_HEALTHY,
        "host": host,
        "port": port,
        "reason": reason,
        "probe_rc": rc,
        "guest_exit_code": result.code,
        "timed_out": timed_out,
        "operation_timed_out": timed_out,
        "refused": "Connection refused" in output,
        "dns_failed": any(
            marker in output
            for marker in ("nodename nor servname", "Name or service not known")
        ),
        "no_route": any(
            marker in output
            for marker in ("No route to host", "Network is unreachable")
        ),
        "evidence": _evidence(output),
    }


def _unavailable(host: str, port: int, reason: str) -> dict[str, Any]:
    return {
        "status": STATUS_PROBE_UNAVAILABLE,
        "connected": False,
        "host": host,
        "port": port,
        "reason": reason,
        "probe_rc": None,
        "guest_exit_code": None,
        "timed_out": False,
        "operation_timed_out": False,
        "refused": False,
        "dns_failed": False,
        "no_route": False,
        "evidence": [],
    }


def probe_nat(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    connect_timeout_s: int = 5,
    runner: Callable[..., ShellResult] = run_shell,
) -> dict[str, Any]:
    if not _HOST_RE.match(host):
        raise ValueError(f"invalid probe host: {host!r}")
    command = build_probe_command(host, port, connect_timeout_s=connect_timeout_s)
    try:
        result = runner(
            command,
            timeout_ms=max(10_000, (int(connect_timeout_s) + 5) * 1000),
        )
    except ShellTimeout:
        # The guest killed the probe before timeout(1) could report: no
        # handshake evidence either way, so this says nothing about NAT.
        return _unavailable(host, port, "guest shell killed the probe")
    except (ShellError, OSError) as exc:
        return _unavailable(host, port, f"guest shell unusable: {exc}")
    return classify_probe(result, host=host, port=port)


def is_retryable(result: dict[str, Any]) -> bool:
    """Only name-resolution / routing failures are treated as possibly transient.

    ``probe_unavailable`` says nothing about NAT and its remedy is guest tooling,
    so it is never retried; ``refused`` and ``timed_out`` are real answers from
    the network path and are reported on the first sample.
    """
    if result["status"] != STATUS_UNHEALTHY:
        return False
    return bool(result["dns_failed"] or result["no_route"])


def probe_nat_with_retry(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    connect_timeout_s: int = 5,
    retry_interval_s: float = DEFAULT_RETRY_INTERVAL_S,
    retry_budget_s: float = DEFAULT_RETRY_BUDGET_S,
    runner: Callable[..., ShellResult] = run_shell,
    sleeper: Callable[[float], Any] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Probe until healthy, until a non-retryable verdict, or until the budget ends.

    The returned dict is the *last* probe result plus ``attempts`` (one entry per
    sample) and ``retry_budget_s``; every pre-existing field keeps its meaning,
    so ``exit_code_for`` is unchanged.
    """
    started = clock()
    attempts: list[dict[str, Any]] = []
    while True:
        result = probe_nat(
            host,
            port,
            connect_timeout_s=connect_timeout_s,
            runner=runner,
        )
        elapsed = clock() - started
        attempts.append({
            "index": len(attempts) + 1,
            "status": result["status"],
            "reason": result["reason"],
            "elapsed_s": round(elapsed, 3),
            "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        })
        if not is_retryable(result):
            break
        remaining = retry_budget_s - elapsed
        if retry_interval_s <= 0 or remaining <= 0:
            break
        wait_s = min(retry_interval_s, remaining)
        logger.info(
            "guest NAT 探针第 %d 次未通过（%s，dns_failed=%s no_route=%s），"
            "%.1fs 后重试（已用 %.1fs / 预算 %.1fs）",
            len(attempts),
            result["reason"],
            result["dns_failed"],
            result["no_route"],
            wait_s,
            elapsed,
            retry_budget_s,
        )
        sleeper(wait_s)

    final = dict(result)
    final["attempts"] = attempts
    final["retry_budget_s"] = retry_budget_s
    return final


def exit_code_for(result: dict[str, Any]) -> int:
    if result["status"] == STATUS_HEALTHY:
        return EXIT_HEALTHY
    if result["status"] == STATUS_PROBE_UNAVAILABLE:
        return EXIT_PROBE_UNAVAILABLE
    return EXIT_UNHEALTHY


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--connect-timeout-s", type=int, default=5)
    parser.add_argument(
        "--retry-interval",
        type=float,
        default=DEFAULT_RETRY_INTERVAL_S,
        metavar="SECONDS",
        help="间隔多久重试一次 dns_failed / no_route 结果；0 表示不重试",
    )
    parser.add_argument(
        "--retry-budget",
        type=float,
        default=DEFAULT_RETRY_BUDGET_S,
        metavar="SECONDS",
        help="重试总时长上限，从第一次探测开始计；0 表示不重试",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be in 1..65535")
    if args.connect_timeout_s <= 0:
        parser.error("--connect-timeout-s must be > 0")
    if args.retry_interval < 0:
        parser.error("--retry-interval must be >= 0")
    if args.retry_budget < 0:
        parser.error("--retry-budget must be >= 0")
    if not _HOST_RE.match(args.host):
        parser.error("--host must be a bare hostname or IP")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # stdout 只留 JSON 结果；重试记录经 autophone.logging 写 stderr。
    setup_logging(level_from_env())
    result = probe_nat_with_retry(
        args.host,
        args.port,
        connect_timeout_s=args.connect_timeout_s,
        retry_interval_s=args.retry_interval,
        retry_budget_s=args.retry_budget,
    )
    print(json.dumps(result, ensure_ascii=False))
    return exit_code_for(result)


if __name__ == "__main__":
    raise SystemExit(main())
