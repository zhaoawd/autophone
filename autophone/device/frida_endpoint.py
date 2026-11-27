"""Host-side Frida endpoint resolution shared by every attach/spawn caller.

One rig maps to one Frida tunnel (``127.0.0.1:<host port>``).  When two VMs run on
the same host each tunnel has its own port, so every call site must resolve the
endpoint from the rig-bound process environment instead of a literal.

Resolution order (first non-empty wins):

1. an explicit argument from the caller;
2. ``AUTOPHONE_FRIDA_HOST`` (canonical, written by ``DeviceTarget.process_environment``);
3. ``AP_FRIDA_HOST`` (legacy name kept for probes and shell wrappers);
4. ``AUTOPHONE_FRIDA_HOST_PORT`` joined with the loopback host — this is what
   ``scripts/start_vphone.sh`` exports to its own children;
5. the single-VM default ``127.0.0.1:27042``.

An unparseable port fails closed with ``ValueError`` rather than silently
attaching to the default tunnel of a different rig.
"""

from __future__ import annotations

import os
from typing import Mapping

DEFAULT_FRIDA_HOST = "127.0.0.1"
DEFAULT_FRIDA_PORT = 27042
DEFAULT_FRIDA_ENDPOINT = f"{DEFAULT_FRIDA_HOST}:{DEFAULT_FRIDA_PORT}"

ENV_FRIDA_HOST = "AUTOPHONE_FRIDA_HOST"
ENV_FRIDA_HOST_LEGACY = "AP_FRIDA_HOST"
ENV_FRIDA_HOST_PORT = "AUTOPHONE_FRIDA_HOST_PORT"


def resolve_frida_host(
    explicit: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return the ``host:port`` Frida endpoint for the current rig-bound process."""
    if explicit:
        return str(explicit)
    env = os.environ if environ is None else environ
    for key in (ENV_FRIDA_HOST, ENV_FRIDA_HOST_LEGACY):
        value = (env.get(key) or "").strip()
        if value:
            return value
    raw_port = (env.get(ENV_FRIDA_HOST_PORT) or "").strip()
    if raw_port:
        if not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
            raise ValueError(
                f"{ENV_FRIDA_HOST_PORT} must be a TCP port in 1..65535, got {raw_port!r}"
            )
        return f"{DEFAULT_FRIDA_HOST}:{int(raw_port)}"
    return DEFAULT_FRIDA_ENDPOINT
