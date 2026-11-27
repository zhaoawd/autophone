"""vphone-mcp: ScreenLock-aware MCP server for programmatic iOS VM control.

Exposes read-only and mutating tools across 5 layers:
- Layer 0: Readiness & introspection (read-only)
- Layer 1: Hardware keys (mutating)
- Layer 2: Screenshots (read-only)
- Layer 3: Pre-mapped navigation & app/location controls (read-only + mutating)
- Layer 4: Raw touch & text interaction in preview coordinates (mutating)

All mutating tools execute under ``DeviceCoordinator.mutating_call`` (Control
Plane HTTP coordination and/or local ``ScreenLock`` serialization) and fail
closed when the device is busy, quarantined, or a configured Control Plane is
unreachable.
"""
from __future__ import annotations

import time
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, ToolAnnotations

from .actions import (
    APP_SWITCHER,
    BACK_BUTTON,
    CONTROL_CENTER,
    NEXT_PAGE,
    NOTIFICATION_CENTER,
    PREV_PAGE,
    SCROLL_DOWN,
    SCROLL_UP,
    SEARCH_BAR,
    app_position,
)
from .client import VPhoneClient
from .wire import PROTOCOL_UPSTREAM_V1, format_response_error

mcp = FastMCP("vphone")

_READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
)
_MUTATING_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
)

# Exhaustive tool classification verified by unit tests against registered tools.
READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "wait_ready",
    "screenshot",
    "get_capabilities",
    "list_apps",
    "get_location",
})

MUTATING_TOOLS: frozenset[str] = frozenset({
    "go_home",
    "press_power",
    "volume_up",
    "volume_down",
    "open_app",
    "tap_back",
    "open_search",
    "scroll_down",
    "scroll_up",
    "open_notification_center",
    "open_control_center",
    "open_app_switcher",
    "swipe_to_next_page",
    "swipe_to_previous_page",
    "tap",
    "swipe",
    "type_text",
    "launch_app",
    "terminate_app",
    "set_location",
    "stop_location",
})


def _client() -> VPhoneClient:
    return VPhoneClient()


def _require_ok(resp: dict[str, Any]) -> str:
    if resp.get("ok") is True:
        return "ok"
    raise RuntimeError(format_response_error(resp))


def _screenshot_content(resp: dict[str, Any]) -> list[ImageContent]:
    """Extract image from response and return as MCP ImageContent."""
    img_b64 = resp.get("image")
    if not img_b64:
        raise RuntimeError("no image in response")
    return [ImageContent(type="image", data=img_b64, mimeType="image/jpeg")]


def _action_screenshot(client: VPhoneClient, resp: dict[str, Any]) -> list[ImageContent]:
    """Verify action success and return its JPEG screenshot.

    In ``upstream_v1``, HID actions may return ``{"ok": true}`` without an inline
    ``image`` field; in that case capture a follow-up screenshot within the same
    held coordination lease.
    """
    _require_ok(resp)
    if resp.get("image"):
        return _screenshot_content(resp)
    if client.protocol == PROTOCOL_UPSTREAM_V1:
        shot = client.screenshot()
        _require_ok(shot)
        return _screenshot_content(shot)
    raise RuntimeError("no image in response")


# ---------------------------------------------------------------------------
# Layer 0: Readiness & introspection (read-only)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
def wait_ready(timeout_s: int = 30) -> list[ImageContent]:
    """Block until the VM is fully reachable (UDS + guest/vsock), then return a screenshot.

    Call this before starting an automation sequence, especially after a VM restart.
    Returns a screenshot on success, raises on timeout.

    Args:
        timeout_s: Maximum seconds to wait (default 30)
    """
    client = _client()
    resp = client.wait_ready(timeout=float(timeout_s))
    _require_ok(resp)
    return _screenshot_content(resp)


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
def get_capabilities() -> dict[str, Any]:
    """Inspect protocol capabilities and active ScreenLock / Control Plane coordination status.

    Read-only: never acquires ScreenLock or mutates device state.
    """
    client = _client()
    coordination = client.coordinator.status()
    caps = client.capabilities()
    return {
        "protocol": client.protocol,
        "socket_path": client.socket_path,
        "coordination": coordination,
        "capabilities": caps,
    }


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
def list_apps(filter: str = "user") -> dict[str, Any]:
    """List installed applications on the VM (read-only).

    Args:
        filter: App filter passed to vphoned (e.g. "user", "system", "running")
    """
    client = _client()
    resp = client.app_list(filter=filter)
    _require_ok(resp)
    return {"ok": True, "apps": resp.get("apps") or []}


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
def get_location(timeout_s: float = 10.0) -> dict[str, Any]:
    """Read the current simulated/device GPS fix from the guest (read-only).

    Args:
        timeout_s: Maximum seconds to wait for a CoreLocation reading (default 10.0)
    """
    client = _client()
    resp = client.location_current(timeout_s=timeout_s)
    _require_ok(resp)
    return resp


# ---------------------------------------------------------------------------
# Layer 1: Hardware keys (mutating)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_MUTATING_ANNOTATIONS)
def go_home() -> list[ImageContent]:
    """Press the home button to return to the home screen."""
    client = _client()
    with client.coordinator.mutating_call("go_home"):
        resp = client.key("home")
        return _action_screenshot(client, resp)


@mcp.tool(annotations=_MUTATING_ANNOTATIONS)
def press_power() -> list[ImageContent]:
    """Press the power button (lock/wake)."""
    client = _client()
    with client.coordinator.mutating_call("press_power"):
        resp = client.key("power")
        return _action_screenshot(client, resp)


@mcp.tool(annotations=_MUTATING_ANNOTATIONS)
def volume_up() -> list[ImageContent]:
    """Press volume up."""
    client = _client()
    with client.coordinator.mutating_call("volume_up"):
        resp = client.key("volup")
        return _action_screenshot(client, resp)


@mcp.tool(annotations=_MUTATING_ANNOTATIONS)
def volume_down() -> list[ImageContent]:
    """Press volume down."""
    client = _client()
    with client.coordinator.mutating_call("volume_down"):
        resp = client.key("voldown")
        return _action_screenshot(client, resp)


# ---------------------------------------------------------------------------
# Layer 2: Screenshots (read-only)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
def screenshot() -> list[ImageContent]:
    """Take a screenshot of the VM display (read-only).

    Returns the current screen as an embedded JPEG image (color by default;
    set VPHONE_MCP_SCREENSHOT_COLOR=0 to force the compact grayscale path).
    """
    resp = _client().screenshot()
    _require_ok(resp)
    return _screenshot_content(resp)


# ---------------------------------------------------------------------------
# Layer 3: Pre-mapped navigation, app lifecycle & location (mutating)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_MUTATING_ANNOTATIONS)
def open_app(name: str) -> list[ImageContent]:
    """Open an app from the home screen by name.

    Presses home first, then taps the app icon under a single ScreenLock lease.

    Supported apps: FaceTime, Calendar, Photos, Mail, Notes, Reminders,
    Clock, TV, Games, App Store, Maps, Health, Wallet, Settings,
    Phone, Safari, Messages, Music.

    Args:
        name: App name (case-insensitive)
    """
    pos = app_position(name)
    if pos is None:
        raise ValueError(
            f"Unknown app '{name}'. Supported: FaceTime, Calendar, Photos, Mail, "
            f"Notes, Reminders, Clock, TV, Games, App Store, Maps, Health, Wallet, "
            f"Settings, Phone, Safari, Messages, Music. "
            f"For other apps, use launch_app(bundle_id) or tap() on the icon coordinates."
        )
    client = _client()
    with client.coordinator.mutating_call("open_app"):
        home_resp = client.key("home")
        _require_ok(home_resp)
        time.sleep(0.3)
        resp = client.tap(pos[0], pos[1])
        return _action_screenshot(client, resp)


@mcp.tool(annotations=_MUTATING_ANNOTATIONS)
def launch_app(bundle_id: str) -> dict[str, Any]:
    """Launch an installed iOS application by bundle identifier.

    Args:
        bundle_id: Target bundle identifier (e.g. "com.apple.Preferences")
    """
    client = _client()
    with client.coordinator.mutating_call("launch_app"):
        resp = client.launch_app(bundle_id)
        _require_ok(resp)
        return resp


@mcp.tool(annotations=_MUTATING_ANNOTATIONS)
def terminate_app(bundle_id: str) -> dict[str, Any]:
    """Terminate a running iOS application by bundle identifier.

    Args:
        bundle_id: Target bundle identifier (e.g. "com.apple.Preferences")
    """
    client = _client()
    with client.coordinator.mutating_call("terminate_app"):
        resp = client.terminate_app(bundle_id)
        _require_ok(resp)
        return resp


@mcp.tool(annotations=_MUTATING_ANNOTATIONS)
def set_location(
    latitude: float,
    longitude: float,
    altitude: float = 0.0,
    horizontal_accuracy: float = 5.0,
    vertical_accuracy: float = 5.0,
    speed: float = -1.0,
    course: float = -1.0,
) -> dict[str, Any]:
    """Push a simulated GPS fix into the guest system-wide CoreLocation manager.

    Args:
        latitude: WGS-84 latitude (-90 to 90)
        longitude: WGS-84 longitude (-180 to 180)
        altitude: Altitude in meters (default 0.0)
        horizontal_accuracy: Horizontal accuracy in meters (default 5.0)
        vertical_accuracy: Vertical accuracy in meters or -1 for unknown (default 5.0)
        speed: Speed in m/s or -1 for unknown (default -1.0)
        course: Course in degrees [0, 360) or -1 for unknown (default -1.0)
    """
    client = _client()
    with client.coordinator.mutating_call("set_location"):
        resp = client.set_location(
            latitude,
            longitude,
            altitude=altitude,
            horizontal_accuracy=horizontal_accuracy,
            vertical_accuracy=vertical_accuracy,
            speed=speed,
            course=course,
        )
        _require_ok(resp)
        return resp


@mcp.tool(annotations=_MUTATING_ANNOTATIONS)
def stop_location() -> dict[str, Any]:
    """Stop simulated GPS injection and clear CoreLocation simulation."""
    client = _client()
    with client.coordinator.mutating_call("stop_location"):
        resp = client.stop_location()
        _require_ok(resp)
        return resp


@mcp.tool(annotations=_MUTATING_ANNOTATIONS)
def tap_back() -> list[ImageContent]:
    """Tap the iOS navigation back button (top-left corner)."""
    client = _client()
    with client.coordinator.mutating_call("tap_back"):
        resp = client.tap(BACK_BUTTON[0], BACK_BUTTON[1])
        return _action_screenshot(client, resp)


@mcp.tool(annotations=_MUTATING_ANNOTATIONS)
def open_search() -> list[ImageContent]:
    """Tap the Search bar on the home screen."""
    client = _client()
    with client.coordinator.mutating_call("open_search"):
        resp = client.tap(SEARCH_BAR[0], SEARCH_BAR[1])
        return _action_screenshot(client, resp)


@mcp.tool(annotations=_MUTATING_ANNOTATIONS)
def scroll_down() -> list[ImageContent]:
    """Scroll down on the current screen."""
    client = _client()
    with client.coordinator.mutating_call("scroll_down"):
        resp = client.swipe(**SCROLL_DOWN)
        return _action_screenshot(client, resp)


@mcp.tool(annotations=_MUTATING_ANNOTATIONS)
def scroll_up() -> list[ImageContent]:
    """Scroll up on the current screen."""
    client = _client()
    with client.coordinator.mutating_call("scroll_up"):
        resp = client.swipe(**SCROLL_UP)
        return _action_screenshot(client, resp)


@mcp.tool(annotations=_MUTATING_ANNOTATIONS)
def open_notification_center() -> list[ImageContent]:
    """Swipe down from the top-left to open Notification Center."""
    client = _client()
    with client.coordinator.mutating_call("open_notification_center"):
        resp = client.swipe(**NOTIFICATION_CENTER)
        return _action_screenshot(client, resp)


@mcp.tool(annotations=_MUTATING_ANNOTATIONS)
def open_control_center() -> list[ImageContent]:
    """Swipe down from the top-right to open Control Center."""
    client = _client()
    with client.coordinator.mutating_call("open_control_center"):
        resp = client.swipe(**CONTROL_CENTER)
        return _action_screenshot(client, resp)


@mcp.tool(annotations=_MUTATING_ANNOTATIONS)
def open_app_switcher() -> list[ImageContent]:
    """Slow swipe up from bottom to open the App Switcher."""
    client = _client()
    with client.coordinator.mutating_call("open_app_switcher"):
        resp = client.swipe(**APP_SWITCHER)
        return _action_screenshot(client, resp)


@mcp.tool(annotations=_MUTATING_ANNOTATIONS)
def swipe_to_next_page() -> list[ImageContent]:
    """Swipe left to go to the next home screen page."""
    client = _client()
    with client.coordinator.mutating_call("swipe_to_next_page"):
        resp = client.swipe(**NEXT_PAGE)
        return _action_screenshot(client, resp)


@mcp.tool(annotations=_MUTATING_ANNOTATIONS)
def swipe_to_previous_page() -> list[ImageContent]:
    """Swipe right to go to the previous home screen page."""
    client = _client()
    with client.coordinator.mutating_call("swipe_to_previous_page"):
        resp = client.swipe(**PREV_PAGE)
        return _action_screenshot(client, resp)


# ---------------------------------------------------------------------------
# Layer 4: Raw interaction (preview coordinates: 430×932)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_MUTATING_ANNOTATIONS)
def tap(x: int, y: int) -> list[ImageContent]:
    """Tap at specific coordinates on the screen.

    Coordinates are in preview pixels (430×932). Use screenshot() first to
    identify the target position — the screenshot matches this coordinate space.

    Args:
        x: Horizontal coordinate (0=left, 430=right)
        y: Vertical coordinate (0=top, 932=bottom)
    """
    client = _client()
    with client.coordinator.mutating_call("tap"):
        resp = client.tap(x, y)
        return _action_screenshot(client, resp)


@mcp.tool(annotations=_MUTATING_ANNOTATIONS)
def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> list[ImageContent]:
    """Swipe from one point to another.

    Coordinates are in preview pixels (430×932).

    Args:
        x1: Start X coordinate
        y1: Start Y coordinate
        x2: End X coordinate
        y2: End Y coordinate
        duration_ms: Swipe duration in milliseconds (default 300)
    """
    client = _client()
    with client.coordinator.mutating_call("swipe"):
        resp = client.swipe(x1, y1, x2, y2, ms=duration_ms)
        return _action_screenshot(client, resp)


@mcp.tool(annotations=_MUTATING_ANNOTATIONS)
def type_text(text: str) -> dict[str, Any]:
    """Write text to the guest clipboard via the UDS ``type`` command (timeout >= 5s).

    Note: In vphone-cli / upstream_v1, ``type`` writes to the guest UIPasteboard;
    tap Paste in the target input field to insert the text.

    Args:
        text: Text string to place on the guest clipboard
    """
    client = _client()
    with client.coordinator.mutating_call("type_text"):
        resp = client.type_text(text)
        _require_ok(resp)
        return {"ok": True}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
