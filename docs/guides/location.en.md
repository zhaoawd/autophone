# System Location & Route Replay Guide

[简体中文](location.md) | **English**

> For full domain architecture and state-machine details, see the [Location Domain Specification](../domains/location.md) and [ADR-0005](../adr/0005-unify-location-sessions-keep-target-runtimes-separate.md).

Autophone supports per-Rig isolated system CoreLocation fixed-point simulation and built-in route replay. Before using location controls, enable **Settings → Privacy & Security → Location Services** inside the target iOS guest.

- Fixed-point and route controls require the guest to advertise the `location_owned` capability.
- Reading back the current system location requires the guest to advertise the `location` capability.
- An active location session can only be paused, resumed, or stopped by the same `owner` that created it, preventing concurrent callers from overwriting each other.

## CLI Usage (Recommended)

If the dashboard is protected with `AUTOPHONE_AUTH`, export `AUTOPHONE_DASHBOARD_AUTH="user:password"` in your CLI environment:

```bash
# Read current system location on a specific Rig
autophone location current --rig rig-a

# Start a fixed-point system location session (WGS-84)
autophone location set --rig rig-a --owner operator --session-id demo-fixed \
  --latitude 37.332 --longitude -122.031

# Stop the fixed-point session
autophone location stop --rig rig-a --owner operator --session-id demo-fixed

# Replay a built-in route (advances each waypoint after host delivery acknowledgement)
autophone location route --rig rig-a --owner operator --session-id demo-route \
  example-demo-route-1p8km-driving
```

## HTTP API Examples

The following commands drive route replay, system readback, and session teardown through the dashboard HTTP API (replace `rig-a` with your registered Rig ID):

```bash
base=http://127.0.0.1:8000

# Inspect built-in routes, active sessions, and current guest readback
curl --fail "$base/api/location/routes"
curl --fail "$base/api/location/sessions"
curl --fail "$base/api/location/devices/rig-a/current"

# Start a built-in route replay session
curl --fail -X POST "$base/api/location/sessions" \
  -H 'Content-Type: application/json' \
  -d '{
    "spec": {
      "session_id": "route-demo",
      "owner": "platform-demo",
      "device": "rig-a",
      "source": {
        "source_id": "demo-route",
        "kind": "route",
        "coordinate_system": "gcj02",
        "route_id": "example-demo-route-1p8km-driving",
        "time_scale": 1
      },
      "targets": [
        {
          "target_id": "system:rig-a",
          "kind": "system_core_location",
          "coordinate_system": "wgs84"
        }
      ]
    }
  }'

# Read back guest system location and stop the session
curl --fail "$base/api/location/devices/rig-a/current"
curl --fail -X DELETE "$base/api/location/sessions/route-demo" \
  -H 'Content-Type: application/json' \
  -d '{"owner": "platform-demo"}'
```

## Observation Boundaries

The coordinates, `fresh`, and `simulated` flags returned by system readback come directly from the guest OS location service. Desired session coordinates, host delivery acknowledgements, and system readbacks must be verified separately; proving that a specific App actually consumed the simulated location requires independent observation of that App.
