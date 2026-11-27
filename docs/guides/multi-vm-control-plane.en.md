# Multi-VM Control Plane & Isolated Deployment Guide

[简体中文](multi-vm-control-plane.md) | **English**

When running multiple `vphone` virtual machines on the same macOS host and requiring per-Rig dispatch isolation, coordinated `ScreenLock` management, or fault isolation, enable the PostgreSQL-backed **Control Plane**.

## 1. Generate a `DeviceTarget` for Each VM

Use `autophone rig init` to generate an independent 16-field `DeviceTarget` JSON for each VM (automatically reading `udid-prediction.txt` and assigning non-overlapping host SSH/Frida ports to prevent multi-VM port collisions):

```bash
# First VM (defaults to SSH 2222 / Frida 27042)
uv run --group platform autophone rig init rig-a \
  --bundle /absolute/path/to/VPhone.bundle

# Second VM (automatically avoids existing target.json ports, e.g. SSH 2223 / Frida 27043)
uv run --group platform autophone rig init rig-b \
  --bundle /absolute/path/to/VPhone.bundle
```

Generated configs live at `~/.vphone/machines/<rig-id>/autophone/target.json`. Each Rig has its own isolated `vm_dir`, `vphone.sock`, `runtime_dir`, `log_dir`, `temp_dir`, `package_cache`, and host port assignments.

## 2. Launch the Multi-VM Control Plane Stack

### Option A: Existing PostgreSQL Instance (Recommended for Persistent Deployments)

```bash
export AUTOPHONE_DATABASE_URL="postgresql://user:pass@127.0.0.1:5432/autophone_control"
uv run --group platform autophone rig up \
  --target "$HOME/.vphone/machines/rig-a/autophone/target.json" \
  --target "$HOME/.vphone/machines/rig-b/autophone/target.json"
```

### Option B: Ephemeral Local PostgreSQL (Quick Multi-VM Evaluation)

If PostgreSQL CLI tools (`pg_config`, `initdb`, `pg_ctl`) are installed locally, pass `--ephemeral-db` to spin up a temporary Unix-socket PostgreSQL instance that cleans itself up on exit:

```bash
uv run --group platform autophone rig up --ephemeral-db \
  --target "$HOME/.vphone/machines/rig-a/autophone/target.json" \
  --target "$HOME/.vphone/machines/rig-b/autophone/target.json"
```

### Optional Flags

- `--control-port` (default `8100`) and `--dashboard-port` (default `8000`): override listener ports.
- `--allow-multi-vm`: explicitly allow booting when another VM is already running on the host.
- `--springboard-region-probe <path>`: stage a read-only SpringBoard region probe over UDS on each upstream Rig (automatically cleaned up on exit) for third-party IPA pre-install checks; build it with `scripts/build_guest_helpers.sh`.
- `--decline-usb-trust` (and `--usb-trust-wait`, default `25` seconds): once the VM and managed console are ready, check once per guest `kern.bootsessionuuid` boot cycle for the iOS USB pairing prompt and tap decline only after safely matching `"Trust This Computer?"` / `"Don't Trust"` (supported in both local mode and Control Plane mode; can also be enabled per Rig by placing an empty marker file at `<runtime_dir>/decline-usb-trust`).

## 3. Isolation & Fail-Closed Guarantees

- **Per-Target Isolation**: Selecting a target Rig on the dashboard explicitly scopes screenshots, remote console sessions, system location, IPA installs, and case runs to that `rig_id`. If one Rig Agent exits, all other online Rigs continue serving traffic unaffected.
- **No Silent Fallback**: Once `AUTOPHONE_DATABASE_URL` is configured, if the Control Plane or PostgreSQL is unreachable, both CLI and HTTP API fail closed (CLI exit code `2`, HTTP `503`) and **never silently bypass the Control Plane to access sockets directly**.
- **Network Security**: Unauthenticated access is permitted only when bound to loopback `127.0.0.1`. Before exposing on a LAN, configure `AUTOPHONE_AUTH="user:password"` and terminate TLS at a reverse proxy.

## 4. Manual Multi-Process Deployment & Lifecycle Management

To keep existing VMs running or supervise each process independently, use the low-level entry points directly:

```bash
# Inspect, start, or stop an individual Target
uv run python -m autophone.rig.lifecycle status --target /path/to/target.json
uv run python -m autophone.rig.lifecycle start  --target /path/to/target.json --allow-multi-vm
uv run python -m autophone.rig.lifecycle stop   --target /path/to/target.json

# Run Control Plane, Rig Agent, and Web Dashboard separately
autophone-control
autophone-rig-agent --target /path/to/rig-a.json --capability vm_uds --capability app_install
autophone serve --host 127.0.0.1 --port 8000
```

See also: [Device Console & Embedded Access](device-console.en.md), [System Location & Route Replay](location.en.md), and [Core Distribution & Standalone Install](../public-core.md).
