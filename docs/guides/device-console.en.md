# Device Console & Embedded Access

[简体中文](device-console.md) | **English**

> Scope: Autophone Public Core (both single-VM local mode and multi-VM Control Plane mode).

After selecting a target Rig on the dashboard home page (`http://127.0.0.1:8000`), you can fetch live screenshots, open the interactive remote Device Console, control simulated system location, or expand **Regression, IPA & Recording** to execute case runs and inspect HTML evidence reports.

## ScreenLock & Coordinate Rules

- Remote Device Console sessions and web recording sessions acquire the target Rig's `ScreenLock`, preventing automated runs and manual interaction from contending over touch input.
- Under the `upstream_v1` protocol, **Send Text** writes to the target guest clipboard (`UIPasteboard`); paste into the focused input field on the device to enter text.
- Touch coordinates are submitted against the dimensions of the most recently fetched frame. Reuse `request_id` when creating a session and `event_id` when sending input events to guarantee idempotent retries.

## Zero-VM Interactive Demo Page

Without booting a VM, start the local dashboard and open the built-in interactive demo directly in your browser:

```text
http://127.0.0.1:8000/embed/device-console?demo=1
```

## HTTP API Summary

Endpoints are protected by dashboard authentication (HTTP Basic Auth whenever `AUTOPHONE_AUTH` is configured). See the [HTTP API Reference](../reference/http-api.md) for complete request and response schemas.

| Method & Path | Purpose |
| --- | --- |
| `GET /api/platform/targets` | List configured devices, deployment mode (`local`/`control`), and online status |
| `GET /devices/{device}/status` | Read target device health and active session state |
| `GET /devices/{device}/screen.jpg` | Fetch the current screenshot for the target device |
| `POST /api/devices/{device}/console` | Start a remote console session (returns `session_id`, `access_token`, `access_header`) |
| `GET /api/devices/{device}/console/{session_id}/frame` | Fetch a JPEG frame and its `X-Frame-Seq` header |
| `POST /api/devices/{device}/console/{session_id}/event` | Dispatch tap, swipe, hardware key, or clipboard events |
| `POST /api/devices/{device}/console/{session_id}/heartbeat` | Renew an active console session lease |
| `POST /api/devices/{device}/console/{session_id}/stop` | Release `ScreenLock` and terminate the session |

## Cross-Site Embedding

Issue a short-lived, single-use launch token for a specific device via `POST /api/access/launch-tokens`. When the embedded page `/embed/device-console` consumes the `launch` query parameter from the URL, it immediately strips that parameter from the browser address bar and exchanges it for a session credential via `POST /api/access/embed-sessions`.

When deploying behind a reverse proxy, enable TLS, configure `AUTOPHONE_AUTH`, and configure proxy access logs to omit query strings. See also [Core Distribution & Standalone Install](../public-core.md) and the [Interactive Architecture Diagram](../architecture.html).
