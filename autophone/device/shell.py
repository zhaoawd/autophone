"""vphone_shell — fail-closed adapter for the UDS {"t":"shell"} command.

Wraps the guest shell capability landed in vphone-cli ffc8f55 (+1a94fdf
/var/jb/bin/sh fallback). Encodes the contract measured in the 2026-06-12
smoke run (tools/phase1/shell_smoke.py) so callers don't re-learn it:

- UDS response keys are ``stdout``/``stderr`` (host renames the guest's
  out/err), plus ``code``, ``timed_out``, ``truncated``.
- Guest runs as root; sh is dash via /var/jb/bin/sh; root PATH lacks
  /var/jb — ``jb_path=True`` (default) prepends the export.
- Timeout: guest clamps timeout_ms to [1, 120000]; on expiry it SIGKILLs
  the process group → code=137, timed_out=true. We raise ShellTimeout.
- Output: 1 MiB cap per stream, then frame re-fit halving; truncated=true.
  Fail-closed: raise ShellTruncated unless allow_truncated=True.
- Background footgun: ``cmd &`` keeps the pipe write end open and the
  request drains until timeout. Use bg() to wrap with the required
  ``>/dev/null 2>&1 </dev/null``.
- The host UDS server is serial (one acceptQueue + semaphore.wait): a long
  shell blocks every other vphone.sock command (screenshot included) until
  it returns. Keep timeouts tight; push long jobs through bg() + polling.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from autophone.device.uds import request

JB_PATH_EXPORT = "export PATH=/var/jb/usr/bin:/var/jb/bin:$PATH"
GUEST_MAX_TIMEOUT_MS = 120_000  # guest-side clamp (VP_SHELL_MAX_TIMEOUT_MS)
SOCK_TIMEOUT_MARGIN_S = 20.0    # headroom over the guest deadline


class ShellError(RuntimeError):
    """Transport- or protocol-level failure (ok=false, missing fields...)."""


class ShellTimeout(ShellError):
    """Guest killed the command tree at the deadline (code=137)."""


class ShellTruncated(ShellError):
    """Output hit the 1 MiB/stream cap or frame re-fit; result is partial."""


class ShellNonZero(ShellError):
    """check=True and the command exited nonzero."""

    def __init__(self, result: "ShellResult") -> None:
        super().__init__(
            f"exit code {result.code}: {result.stderr.strip()[:200]!r}")
        self.result = result


@dataclass
class ShellResult:
    stdout: str
    stderr: str
    code: int
    timed_out: bool
    truncated: bool


def resolve_sock() -> str:
    """Resolve the selected VM socket (legacy vm by default)."""
    from autophone.device.target import resolve_target

    return str(resolve_target().sock)


def bg(cmd: str) -> str:
    """Wrap a command so it survives the request without wedging the pipe."""
    return f"({cmd}) >/dev/null 2>&1 </dev/null &"


def run_shell(
    cmd: str,
    *,
    cwd: str | None = None,
    timeout_ms: int = 30_000,
    jb_path: bool = True,
    check: bool = False,
    allow_truncated: bool = False,
    sock_path: str | None = None,
    protocol: str | None = None,
) -> ShellResult:
    """Run `cmd` as root in the guest via /var/jb/bin/sh -c.

    Raises ShellTimeout / ShellTruncated / ShellNonZero (with check=True)
    instead of returning partial or ambiguous results.
    """
    if timeout_ms > GUEST_MAX_TIMEOUT_MS:
        # Guest would silently clamp; surface the real ceiling instead.
        raise ValueError(
            f"timeout_ms {timeout_ms} exceeds guest max {GUEST_MAX_TIMEOUT_MS}")

    if jb_path:
        cmd = f"{JB_PATH_EXPORT}; {cmd}"

    msg: dict = {"t": "shell", "cmd": cmd, "screen": False,
                 "timeout_ms": timeout_ms}
    if cwd:
        msg["cwd"] = cwd

    resp, _ = request(
        msg,
        sock_path=sock_path or resolve_sock(),
        timeout=timeout_ms / 1000 + SOCK_TIMEOUT_MARGIN_S,
        parse_image=False,
        **({"protocol": protocol} if protocol is not None else {}),
    )
    if not resp.get("ok"):
        raise ShellError(f"shell request failed: {resp.get('error')!r}")

    result = ShellResult(
        stdout=resp.get("stdout", ""),
        stderr=resp.get("stderr", ""),
        code=int(resp.get("code", -1)),
        timed_out=bool(resp.get("timed_out")),
        truncated=bool(resp.get("truncated")),
    )
    if result.timed_out:
        raise ShellTimeout(
            f"guest killed command after {timeout_ms}ms: {cmd[:120]!r}")
    if result.truncated and not allow_truncated:
        raise ShellTruncated(
            f"output truncated (1 MiB/stream cap): {cmd[:120]!r}")
    if check and result.code != 0:
        raise ShellNonZero(result)
    return result


def run_shell_bg(cmd: str, **kwargs) -> ShellResult:
    """Fire-and-forget: launch `cmd` in the background and return at once."""
    kwargs.setdefault("timeout_ms", 10_000)
    return run_shell(bg(cmd), **kwargs)


if __name__ == "__main__":
    import sys
    r = run_shell(" ".join(sys.argv[1:]) or "uname -a; id")
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    sys.exit(r.code)
