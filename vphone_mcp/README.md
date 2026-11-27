# vphone_mcp

`vphone_mcp` is a standalone Model Context Protocol (MCP) server exposing programmatic iOS VM screen, touch, keyboard, app lifecycle, location, and readiness tools over `vphone-cli` Unix domain sockets (`VPHONE_SOCK`), with `ScreenLock`-aware Control Plane coordination and `upstream_v1` wire compatibility.

## Coordination Modes & Fail-Closed Semantics

`vphone_mcp` separates **read-only** tools from **mutating** tools and enforces per-Rig serialization matching Autophone's `ScreenLock` (`autophone/device/lock.py`) and Control Plane HTTP APIs (`autophone/control/`, `autophone/adapters/http/`).

Modes are auto-detected from environment variables in priority order:

1. **Managed Device Agent Proxy (`managed_proxy`)** — `AUTOPHONE_MANAGED_DEVICE_AGENT=1`
   - Used when Autophone's `StageGuard` + `AgentDeviceProxy` spawns `vphone_mcp` on a supervised private socket while the parent process already holds `ScreenLock`.
   - Passes requests directly to the parent-owned proxy socket without self-contending on `ScreenLock`.

2. **Control Plane HTTP Coordination (`control_plane`)** — configured when any of `AUTOPHONE_CONTROL_PLANE_URL`, `AUTOPHONE_CONTROL_HTTP`, `AUTOPHONE_API_URL`, `AUTOPHONE_CONTROL_WS`, or `AUTOPHONE_DATABASE_URL` is set, together with a device/Rig ID (`VPHONE_DEVICE`, `AUTOPHONE_DEVICE`, `AUTOPHONE_RIG_ID`, or `AP_DEVICE`).
   - Calls only real Autophone HTTP endpoints:
     - `GET /health` (identifies `autophone-control-plane` vs `autophone-run`, fails closed immediately if unreachable or unhealthy — **zero** UDS requests dispatched).
     - On `autophone-control-plane` (or `autophone-run` in `control` mode via `GET /api/platform/targets`): acquires a bounded-TTL Rig reservation via `POST /api/control/reservations` (`expires_at` = now + `VPHONE_MCP_LOCK_TTL_S`), checks live Rig lock and quarantine status via `POST /api/control/device-sessions/{device}/ops` (`op="device_status"`) or `GET /devices/{device}/status`, and always releases the reservation in `finally` via `POST /api/control/reservations/{reservation_id}/release`.
     - On `autophone-run` in `local` mode: checks live lock and quarantine state via `GET /devices/{device}/status` and layers local `ScreenLock` when `AUTOPHONE_STATE_DIR` or `VPHONE_MCP_LOCK_DIR` is also configured.
   - **Strict fail-closed**: if Control Plane is configured and unreachable, misconfigured (missing device ID), busy (`ScreenLockBusy`), or quarantined (`ScreenQuarantineMarkerPresent`), `vphone_mcp` refuses the call immediately and **never** falls back to direct UDS writes.

3. **Local `ScreenLock` File Coordination (`local_lock`)** — configured when a lock directory (`VPHONE_MCP_LOCK_DIR`, `AUTOPHONE_LOCK_DIR`, or `AUTOPHONE_STATE_DIR` -> `<state>/runs/.locks`) AND a device/Rig ID (`VPHONE_DEVICE`, `AUTOPHONE_DEVICE`, `AUTOPHONE_RIG_ID`, or `AP_DEVICE`) are set.
   - Uses the exact same file layout and `fcntl.flock(LOCK_EX | LOCK_NB)` semantics as `autophone.device.lock.ScreenLock` (`<device>.lock`, `<device>.json`, `<device>.quarantine.json`).
   - Acquires a non-blocking exclusive lease around every mutating MCP tool call (including composite tools like `open_app` under a single lease) with bounded TTL (`VPHONE_MCP_LOCK_TTL_S`, default `30s`) and explicit owner ID `vphone-mcp:<pid>:<tool>-<hex>`.
   - Fails closed with `ScreenLockBusy` if a managed run or console session holds the lock, or `ScreenQuarantineMarkerPresent` if `<device>.quarantine.json` exists (checked while holding `flock`).
   - Reliably restores `locked: false` and releases `flock` in `finally`, even if a tool raises an exception.

4. **Standalone Direct `VPHONE_SOCK` Mode (`standalone`)** — backward-compatible default when neither Control Plane nor local `ScreenLock` is configured.
   - Logs a one-time warning on the first mutating call.
   - Set `VPHONE_MCP_REQUIRE_LOCK=1` to enable strict guard mode, refusing any uncoordinated mutating call with `LockRequiredError` while still allowing read-only inspection (`screenshot`, `get_capabilities`, `list_apps`, `get_location`, `wait_ready`).

## Environment Variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `VPHONE_SOCK` | Path to target `vphone.sock` UDS socket | Auto-discovered (or `<library>/<vm>/vphone.sock` for `upstream_v1`) |
| `AUTOPHONE_VPHONE_PROTOCOL` / `VPHONE_MCP_PROTOCOL` | Wire protocol: `legacy` or `upstream_v1` | `legacy` |
| `AUTOPHONE_CONTROL_PLANE_URL` / `AUTOPHONE_CONTROL_HTTP` / `AUTOPHONE_API_URL` | Control Plane or `autophone serve` HTTP base URL | Unset |
| `AUTOPHONE_CONTROL_WS` | Agent WebSocket URL (derives Control Plane HTTP base URL) | Unset |
| `AUTOPHONE_DATABASE_URL` | Signals Control Plane is configured on host (fails closed via Control Plane HTTP) | Unset |
| `AUTOPHONE_CONTROL_PLANE_AUTH` / `AUTOPHONE_API_AUTH` | Optional HTTP Basic Auth (`user:pass`) | Unset |
| `AUTOPHONE_STATE_DIR` | Autophone state dir (uses `<dir>/runs/.locks` for local `ScreenLock`) | Unset |
| `VPHONE_MCP_LOCK_DIR` / `AUTOPHONE_LOCK_DIR` | Explicit local `ScreenLock` directory override | Unset |
| `VPHONE_DEVICE` / `AUTOPHONE_DEVICE` / `AUTOPHONE_RIG_ID` / `AP_DEVICE` | Logical device / Rig key for lock and Control Plane routing | Unset |
| `VPHONE_MCP_REQUIRE_LOCK` | Set `1` to fail closed on mutating calls in standalone mode | `0` |
| `VPHONE_MCP_LOCK_TTL_S` | Bounded per-call lease/reservation TTL in seconds (`1`–`300`) | `30` |
| `VPHONE_MCP_HTTP_TIMEOUT_S` | Control Plane HTTP timeout in seconds (`0.2`–`60`) | `5` |
| `VPHONE_MCP_SCREENSHOT_COLOR` | `1` for sRGB color JPEG, `0` for grayscale | `1` |
| `VPHONE_LIBRARY_ROOT` / `VM_DIR` | Upstream VM library root and VM directory name for socket discovery | `~/.vphone/machines` / `vm-1` |

## MCP Tools

- **Read-only (`readOnlyHint=true`, never acquire `ScreenLock`)**:
  - `wait_ready`, `screenshot`, `get_capabilities`, `list_apps`, `get_location`
- **Mutating (`readOnlyHint=false`, always serialized by active coordinator)**:
  - Hardware keys: `go_home`, `press_power`, `volume_up`, `volume_down`
  - Navigation & gestures: `open_app`, `tap_back`, `open_search`, `scroll_down`, `scroll_up`, `open_notification_center`, `open_control_center`, `open_app_switcher`, `swipe_to_next_page`, `swipe_to_previous_page`
  - Raw touch & clipboard: `tap`, `swipe`, `type_text` (writes to guest clipboard with `timeout >= 3s`)
  - App lifecycle & location: `launch_app`, `terminate_app`, `set_location`, `stop_location`

## `upstream_v1` Protocol Compatibility

When `AUTOPHONE_VPHONE_PROTOCOL=upstream_v1` (or `VPHONE_MCP_PROTOCOL=upstream_v1`) is set, `vphone_mcp` enforces the exact `upstream_v1` UDS contract used by Autophone (`autophone/device/uds.py` and `autophone/device/upstream_wire.py`):

- Single newline-delimited JSON framing without `SHUT_WR` half-close, 1 MiB request/response frame caps, peer UID verification (`LOCAL_PEERCRED` / `SO_PEERCRED`), and strict boolean `ok` validation.
- Preflight `{"t": "capabilities", "screen": false}` admission check before every command (raising `CommandNotAdmitted` if unadmitted).
- `screenshot` passes `"save": false` to suppress implicit host-side saves and validates JPEG format against advertised `screen` descriptors (including non-430×932 / folding panels).
- HID actions (`tap`, `swipe`, `key`) automatically capture a follow-up `screenshot()` within the same held lock lease if an `upstream_v1` server omits inline `"image"`.
- App and location commands translate cleanly to `rpc` methods (`apps.list`, `apps.launch`, `apps.terminate`, `location.set`, `location.current`, `location.clear`) with strict parameter/readback verification and structured error code preservation.
- Failed mutating writes are marked `operation_may_continue=True` and never replayed.

> **Verification Status**: Protocol framing, capabilities admission, RPC translation, dynamic geometry checks, local `ScreenLock` / quarantine interop, and Control Plane HTTP coordination are covered by automated unit and root interoperability tests **and verified live (10/10 checks) against a running `upstream_v1` `vphone-cli` VM (iOS `27.0.1 / 24A446`)**: `wait_ready`, `get_capabilities`, JPEG `screenshot` geometry validation, `list_apps`, `set_location`/`get_location`/`stop_location` round-trip, `local_lock` lifecycle/contention/quarantine enforcement, Control Plane unreachable fail-closed rejection (0 UDS calls), and live Control Plane reservation acquire/release round-trip.

## Usage

### Local `ScreenLock` Coordination (Single-VM)

```bash
uv --directory vphone_mcp sync --locked
VPHONE_SOCK="$HOME/.vphone/machines/rig-a/vphone.sock" \
AUTOPHONE_VPHONE_PROTOCOL=upstream_v1 \
AUTOPHONE_STATE_DIR="/path/to/autophone-state" \
VPHONE_DEVICE="rig-a" \
VPHONE_MCP_REQUIRE_LOCK=1 \
  uv --directory vphone_mcp run vphone-mcp
```

### Control Plane HTTP Coordination (Multi-VM)

```bash
VPHONE_SOCK="$HOME/.vphone/machines/rig-a/vphone.sock" \
AUTOPHONE_VPHONE_PROTOCOL=upstream_v1 \
AUTOPHONE_CONTROL_PLANE_URL="http://127.0.0.1:8100" \
AUTOPHONE_STATE_DIR="/path/to/autophone-state" \
VPHONE_DEVICE="rig-a" \
  uv --directory vphone_mcp run vphone-mcp
```

### Running Tests

```bash
# Standalone vphone_mcp unit tests (including FastMCP tool registration checks)
uv --directory vphone_mcp run python -m unittest discover -s tests -v
# Or with pytest:
uv --directory vphone_mcp run --with pytest pytest -v

# Root Autophone interoperability tests (real ScreenLock + Control Plane FastAPI app + upstream_wire parity)
uv run --group contract --group platform pytest tests/test_vphone_mcp_coordination.py -v
```

## License

Licensed under the [Apache License, Version 2.0](LICENSE) (see [NOTICE](NOTICE) for upstream [`vphone-cli`](https://github.com/Lakr233/vphone-cli) attribution).
