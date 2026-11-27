"""vsock_health_client.py — consume the state file produced by vsock_health daemon.

Upper layers (MCP server, automation loops) call these to:
  - decide whether to route a command at all
  - pick a reasonable per-call timeout based on observed p50/max
  - react to staleness (daemon crashed, state file not updating)

Keep this module dependency-free (stdlib only) — importers may not have
access to the phase0/phase1 tree.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_STATE_FILE = Path("/tmp/vphone_health.json")

# If the state file's last_probe_ts is older than this, we treat it as stale
# (daemon probably died). Must be strictly larger than the daemon's interval.
STALE_AFTER_S = 10.0


@dataclass(frozen=True)
class HealthState:
    state: str                        # healthy|degraded|down|unknown|stale|missing
    uds_state: str
    vsock_state: str
    age_s: float                      # seconds since last probe
    uds_rt_p50_ms: float | None
    vsock_rt_p50_ms: float | None
    uds_rt_max_ms: float | None
    vsock_rt_max_ms: float | None
    last_error: str | None
    probe_count: int

    @property
    def is_healthy(self) -> bool:
        return self.state == "healthy"

    @property
    def is_routable(self) -> bool:
        """Any state except 'down', 'stale', or 'missing' is routable."""
        return self.state in ("healthy", "degraded", "unknown")

    @property
    def vsock_usable(self) -> bool:
        return self.vsock_state in ("healthy", "degraded", "unknown") and \
               self.state not in ("stale", "missing")


ENV_STATE_FILE = "VPHONE_HEALTH_STATE_FILE"
ENV_RIG_RUNTIME_DIR = "AUTOPHONE_RIG_RUNTIME_DIR"
ENV_MULTI_VM = "AUTOPHONE_EXPERIMENTAL_MULTI_VM"
RUNTIME_STATE_NAME = "health.json"


class HealthStateFileUnbound(LookupError):
    """Multi-VM mode without a rig-bound health file: the legacy global link is
    not updated in that mode (scripts/start_vphone.sh), so reading it would report
    another rig or a stale snapshot instead of this process's own device."""


def resolve_state_file(path: Path | str | None = None) -> Path:
    """Resolve one process's Rig health file.

    Order: explicit ``path`` > ``VPHONE_HEALTH_STATE_FILE`` >
    ``$AUTOPHONE_RIG_RUNTIME_DIR/health.json`` (the daemon's own default under
    ``scripts/start_vphone.sh``) > legacy ``/tmp/vphone_health.json``.  The legacy
    link is refused with :class:`HealthStateFileUnbound` when
    ``AUTOPHONE_EXPERIMENTAL_MULTI_VM=1`` is set, because that mode never updates it.
    """
    if path is not None:
        return Path(path)
    configured = os.environ.get(ENV_STATE_FILE, "").strip()
    if configured:
        return Path(configured)
    runtime_dir = os.environ.get(ENV_RIG_RUNTIME_DIR, "").strip()
    if runtime_dir:
        return Path(runtime_dir) / RUNTIME_STATE_NAME
    if os.environ.get(ENV_MULTI_VM, "").strip() == "1":
        raise HealthStateFileUnbound(
            f"{ENV_MULTI_VM}=1 but neither {ENV_STATE_FILE} nor {ENV_RIG_RUNTIME_DIR} "
            f"is set; refusing to read the shared {DEFAULT_STATE_FILE}"
        )
    return DEFAULT_STATE_FILE


def peek_state(path: Path | str | None = None) -> HealthState:
    """Read the current health snapshot. Never raises; returns a 'missing'
    or 'stale' state if the file is absent or not being updated.
    """
    try:
        p = resolve_state_file(path)
    except HealthStateFileUnbound as e:
        return HealthState(
            state="missing", uds_state="missing", vsock_state="missing",
            age_s=float("inf"), uds_rt_p50_ms=None, vsock_rt_p50_ms=None,
            uds_rt_max_ms=None, vsock_rt_max_ms=None,
            last_error=str(e), probe_count=0,
        )
    if not p.exists():
        return HealthState(
            state="missing", uds_state="missing", vsock_state="missing",
            age_s=float("inf"), uds_rt_p50_ms=None, vsock_rt_p50_ms=None,
            uds_rt_max_ms=None, vsock_rt_max_ms=None,
            last_error="state file missing", probe_count=0,
        )
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return HealthState(
            state="missing", uds_state="missing", vsock_state="missing",
            age_s=float("inf"), uds_rt_p50_ms=None, vsock_rt_p50_ms=None,
            uds_rt_max_ms=None, vsock_rt_max_ms=None,
            last_error=f"state file unreadable: {e}", probe_count=0,
        )
    last_probe = data.get("last_probe_ts") or data.get("ts") or 0
    age_s = time.time() - last_probe
    state = data.get("state", "unknown")
    if age_s > STALE_AFTER_S:
        state = "stale"
    return HealthState(
        state=state,
        uds_state=data.get("uds_state", "unknown"),
        vsock_state=data.get("vsock_state", "unknown"),
        age_s=round(age_s, 2),
        uds_rt_p50_ms=data.get("uds_rt_p50_ms"),
        vsock_rt_p50_ms=data.get("vsock_rt_p50_ms"),
        uds_rt_max_ms=data.get("uds_rt_max_ms"),
        vsock_rt_max_ms=data.get("vsock_rt_max_ms"),
        last_error=data.get("last_error"),
        probe_count=data.get("probe_count", 0),
    )


def suggest_timeout_ms(
    cmd: str,
    health: HealthState | None = None,
    *,
    floor_ms: int = 500,
) -> int:
    """Return a recommended per-call timeout for the given command type.

    Rules of thumb from Phase 1 A bench:
      - screenshot       — single-digit ms network, ~30ms capture on nominal,
                           safe default 1500ms.
      - tap/swipe/key    — injection is sub-ms; screen:true adds ~550ms
                           settle+capture. Default 2000ms covers p99 comfortably.
      - type             — clipboard set has a long tail (p99 1.3s, max 2.6s
                           in bench). Use 5000ms minimum.

    When health data is available, bias toward the observed max + slack.
    """
    base = {
        "screenshot": 1500,
        "tap": 2000,
        "swipe": 2500,    # gestures of 300-400ms + settle
        "key": 2000,
        "type": 5000,
    }.get(cmd, 3000)

    if not health or health.state in ("missing", "stale", "unknown"):
        return max(floor_ms, base)

    # If observed max is high, pad 2x and compare to base.
    if cmd == "type" and health.vsock_rt_max_ms:
        return max(base, int(health.vsock_rt_max_ms * 2))
    if cmd == "screenshot" and health.uds_rt_max_ms:
        return max(base, int(health.uds_rt_max_ms * 3))
    return base


def is_vsock_usable(path: Path | str | None = None) -> bool:
    """Quick yes/no for: can I safely send a command that reaches the guest?"""
    return peek_state(path).vsock_usable


# Allow `python -m autophone.device.vsock_health_client` as a one-shot CLI reader.
if __name__ == "__main__":
    import sys
    hs = peek_state(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps({
        "state": hs.state, "uds": hs.uds_state, "vsock": hs.vsock_state,
        "age_s": hs.age_s,
        "uds_rt_p50_ms": hs.uds_rt_p50_ms, "uds_rt_max_ms": hs.uds_rt_max_ms,
        "vsock_rt_p50_ms": hs.vsock_rt_p50_ms, "vsock_rt_max_ms": hs.vsock_rt_max_ms,
        "is_routable": hs.is_routable, "vsock_usable": hs.vsock_usable,
        "suggest_timeout_ms": {
            "screenshot": suggest_timeout_ms("screenshot", hs),
            "tap": suggest_timeout_ms("tap", hs),
            "type": suggest_timeout_ms("type", hs),
        },
        "last_error": hs.last_error, "probe_count": hs.probe_count,
    }, indent=2))
