"""UDS client for vphone.sock with ScreenLock/Control Plane coordination and upstream_v1 support.

The MCP layer works in preview coordinates (default 430×932); this client
handles scaling to native coordinates before sending to the UDS socket and
enforces Control Plane / ScreenLock coordination on mutating commands.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import time
from typing import Any, Mapping

from .coordination import DeviceCoordinator
from .wire import (
    CommandNotAdmitted,
    EXPECTED_PREVIEW,
    FRAME_LIMIT,
    GeometryDrift,
    HARDWARE_KEYS,
    NATIVE_SCALE,
    NotReady,
    PROTOCOL_LEGACY,
    PROTOCOL_UPSTREAM_V1,
    ProtocolError,
    READ_ONLY_COMMANDS,
    admitted,
    exchange_uds,
    extract_screen_scale,
    parse_jpeg_dims as _parse_jpeg_dims,
    prepare_upstream,
    resolve_protocol,
    validate_upstream_screenshot,
)

DEFAULT_SOCK = str(
    Path.home() / "Documents" / "github" / "vphone-cli" / "vm" / "vphone.sock"
)
HEALTH_PATH = "/tmp/vphone_health.json"
STALE_AFTER_S = 10.0


def _env_color_default(env: Mapping[str, str] | None = None) -> bool:
    """Screenshot color preference, switchable via VPHONE_MCP_SCREENSHOT_COLOR.

    Default = color (sRGB). Set the env to 0/false/no/off to force the
    compact grayscale path (smaller/faster).
    """
    source = os.environ if env is None else env
    v = str(source.get("VPHONE_MCP_SCREENSHOT_COLOR", "1")).strip().lower()
    return v not in ("0", "false", "no", "off", "")


def _resolve_health_path(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    protocol: str = PROTOCOL_LEGACY,
) -> Path | None:
    """Same order as autophone.device.vsock_health_client.resolve_state_file.

    Returns ``None`` when multi-VM mode is on without a rig-bound file or when
    running ``upstream_v1`` without an explicit health file (upstream_v1 does not
    use the legacy global ``/tmp/vphone_health.json`` daemon).
    """
    if path:
        return Path(path)
    source = os.environ if env is None else env
    configured = str(source.get("VPHONE_HEALTH_STATE_FILE") or "").strip()
    if configured:
        return Path(configured)
    runtime_dir = str(source.get("AUTOPHONE_RIG_RUNTIME_DIR") or "").strip()
    if runtime_dir:
        return Path(runtime_dir) / "health.json"
    if protocol == PROTOCOL_UPSTREAM_V1:
        return None
    if str(source.get("AUTOPHONE_EXPERIMENTAL_MULTI_VM") or "").strip() == "1":
        return None
    return Path(HEALTH_PATH)


def _read_health(path: str | Path | None = None) -> tuple[str, bool]:
    """Read health state from daemon JSON. Returns (state, vsock_usable)."""
    resolved = _resolve_health_path(path)
    if resolved is None:
        return ("missing", False)
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return ("missing", False)
    age = time.time() - data.get("last_probe_ts", 0)
    if age > STALE_AFTER_S:
        return ("stale", False)
    state = data.get("state", "unknown")
    vsock_state = data.get("vsock_state", "unknown")
    routable = state not in ("down", "stale", "missing")
    vsock_ok = vsock_state not in ("down",) and routable
    return (state, vsock_ok)


class VPhoneClient:
    """Preview-coordinate UDS client with geometry guard, upstream_v1 wire support,
    and ScreenLock / Control Plane coordination.

    All public tap/swipe methods accept preview coordinates (430×932 by default)
    and scale to native coordinates before sending to the socket.
    """

    _locked_dims: tuple[int, int] | None = None
    _last_health_routable: bool = True

    def __init__(
        self,
        socket_path: str | None = None,
        color: bool | None = None,
        health_path: str | Path | None = None,
        *,
        protocol: str | None = None,
        coordinator: DeviceCoordinator | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._env = dict(env) if env is not None else None
        source = os.environ if self._env is None else self._env
        self.protocol = resolve_protocol(protocol, env=source)
        self.socket_path = (
            socket_path
            or str(source.get("VPHONE_SOCK") or "").strip()
            or self._discover(env=source, protocol=self.protocol)
        )
        self.health_path = _resolve_health_path(
            health_path,
            env=source,
            protocol=self.protocol,
        )
        self.color = _env_color_default(source) if color is None else color
        self.coordinator = (
            coordinator if coordinator is not None else DeviceCoordinator(env=source)
        )
        self._scale = NATIVE_SCALE

    @staticmethod
    def _discover(
        *,
        env: Mapping[str, str] | None = None,
        protocol: str = PROTOCOL_LEGACY,
    ) -> str:
        source = os.environ if env is None else env
        library_root_raw = str(source.get("VPHONE_LIBRARY_ROOT") or "").strip()
        vm_dir_raw = str(source.get("VM_DIR") or "").strip()
        if library_root_raw or protocol == PROTOCOL_UPSTREAM_V1:
            library_root = (
                Path(library_root_raw).expanduser()
                if library_root_raw
                else Path.home() / ".vphone" / "machines"
            )
            if vm_dir_raw:
                vm_dir = Path(vm_dir_raw).expanduser()
                if not vm_dir.is_absolute():
                    vm_dir = library_root / vm_dir
            else:
                vm_dir = library_root / "vm-1"
            return str(vm_dir / "vphone.sock")

        candidates = [
            Path.home() / "github" / "vphone-cli" / "vm" / "vphone.sock",
            Path.home() / "Documents" / "github" / "vphone-cli" / "vm" / "vphone.sock",
            Path.cwd() / "vm" / "vphone.sock",
            Path.home() / ".vphone" / "machines" / "vm-1" / "vphone.sock",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return DEFAULT_SOCK

    @classmethod
    def reset_geometry(cls) -> None:
        cls._locked_dims = None

    def _ensure_ready(self, timeout: float = 15.0) -> None:
        """Block until socket exists and health daemon (if active) reports routable state."""
        deadline = time.time() + timeout

        while time.time() < deadline:
            if not Path(self.socket_path).exists():
                time.sleep(min(0.2, max(0.01, deadline - time.time())))
                continue

            state, _vsock_ok = _read_health(self.health_path)

            if state in ("missing", "stale"):
                was_routable = VPhoneClient._last_health_routable
                VPhoneClient._last_health_routable = False
                if not was_routable:
                    VPhoneClient.reset_geometry()
                return

            if state == "down":
                time.sleep(min(0.5, max(0.01, deadline - time.time())))
                continue

            was_routable = VPhoneClient._last_health_routable
            VPhoneClient._last_health_routable = True
            if not was_routable:
                VPhoneClient.reset_geometry()
            return

        raise NotReady(
            f"VM not ready after {timeout}s — socket: {self.socket_path}, "
            f"health: {_read_health(self.health_path)[0]}"
        )

    def wait_ready(self, timeout: float = 30.0) -> dict[str, Any]:
        """Wait until UDS (and guest capabilities in upstream_v1) are ready, then screenshot."""
        self.coordinator.check_read_access("wait_ready")
        deadline = time.monotonic() + float(timeout)
        self._ensure_ready(timeout=float(timeout))
        if self.protocol == PROTOCOL_UPSTREAM_V1:
            last_error = "guest not connected"
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NotReady(
                        f"upstream_v1 VM not ready after {timeout}s — socket: {self.socket_path} ({last_error})"
                    )
                try:
                    caps = exchange_uds(
                        {"t": "capabilities", "screen": False},
                        self.socket_path,
                        min(remaining, 5.0),
                        PROTOCOL_UPSTREAM_V1,
                        logical_command="capabilities",
                    )
                except (OSError, ProtocolError) as exc:
                    last_error = str(exc)
                else:
                    if (
                        caps.get("ok") is True
                        and caps.get("protocol") == PROTOCOL_UPSTREAM_V1
                        and caps.get("guest_connected") is True
                    ):
                        break
                    last_error = (
                        f"ok={caps.get('ok')!r} protocol={caps.get('protocol')!r} "
                        f"guest_connected={caps.get('guest_connected')!r}"
                    )
                sleep_s = min(0.25, max(0.01, deadline - time.monotonic()))
                if deadline - time.monotonic() <= 0:
                    raise NotReady(
                        f"upstream_v1 VM not ready after {timeout}s — socket: {self.socket_path} ({last_error})"
                    )
                time.sleep(sleep_s)
        return self.screenshot()

    def _send(self, msg: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
        kind = str(msg.get("t") or "")
        is_read_only = kind in READ_ONLY_COMMANDS
        if is_read_only:
            self.coordinator.check_read_access(kind or "read")
            return self._send_unlocked(msg, timeout=timeout)
        with self.coordinator.mutating_call(kind or "write"):
            effective_timeout = self.coordinator.remaining_timeout_s(timeout)
            return self._send_unlocked(msg, timeout=effective_timeout)

    def _send_unlocked(self, msg: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
        kind = str(msg.get("t") or "")
        if self.protocol == PROTOCOL_UPSTREAM_V1:
            return self._send_upstream_v1(msg, timeout=timeout)

        # Legacy framing path
        wire_msg = dict(msg)
        if (
            self.color
            and "color" not in wire_msg
            and wire_msg.get("screen", True) is not False
            and kind != "type"
        ):
            wire_msg["color"] = True

        self._ensure_ready(timeout=timeout)
        obj = exchange_uds(
            wire_msg,
            self.socket_path,
            timeout,
            PROTOCOL_LEGACY,
            logical_command=kind or None,
        )
        self._geometry_check(obj)
        return obj

    def _send_upstream_v1(self, msg: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        kind = str(msg.get("t") or "")
        logical = dict(msg)
        if (
            kind == "screenshot"
            and self.color
            and "color" not in logical
            and logical.get("screen", True) is not False
        ):
            logical["color"] = True

        # Validate logical command and frame size before touching the socket.
        try:
            wire_msg, decoder = prepare_upstream(logical)
        except ValueError as exc:
            raise ProtocolError(str(exc)) from exc
        if len((json.dumps(wire_msg, ensure_ascii=False) + "\n").encode("utf-8")) > FRAME_LIMIT:
            raise ProtocolError("request exceeds protocol frame limit")

        self._ensure_ready(timeout=timeout)
        budget_start = time.monotonic()
        capabilities: dict[str, Any] = {}
        if kind not in {"ping", "capabilities"}:
            capabilities = exchange_uds(
                {"t": "capabilities", "screen": False},
                self.socket_path,
                timeout,
                PROTOCOL_UPSTREAM_V1,
                logical_command="capabilities",
            )
            if not admitted(capabilities, kind):
                raise CommandNotAdmitted(f"upstream command is not admitted: {kind}")

        remaining = timeout - (time.monotonic() - budget_start)
        if remaining <= 0:
            raise TimeoutError("UDS response deadline exceeded")

        dispatched = False
        try:
            raw_obj = exchange_uds(
                wire_msg,
                self.socket_path,
                remaining,
                PROTOCOL_UPSTREAM_V1,
                logical_command=kind or None,
            )
            dispatched = True
            obj = decoder(raw_obj)
        except ValueError as exc:
            error = ProtocolError(str(exc))
            if dispatched and kind not in READ_ONLY_COMMANDS:
                error.operation_may_continue = True  # type: ignore[attr-defined]
                error.command = kind  # type: ignore[attr-defined]
            raise error from exc

        if kind == "screenshot" and obj.get("ok") is True:
            validate_upstream_screenshot(capabilities, obj)
            self._scale = extract_screen_scale(capabilities, obj)
        elif kind == "capabilities" and obj.get("ok") is True and "screen" in obj:
            try:
                self._scale = extract_screen_scale(obj)
            except GeometryDrift:
                pass
        elif obj.get("ok") is True and obj.get("image"):
            validate_upstream_screenshot(capabilities, obj)
        return obj

    def _geometry_check(self, resp: dict[str, Any]) -> None:
        img_b64 = resp.get("image")
        if not img_b64:
            return
        img = base64.b64decode(img_b64)
        dims = _parse_jpeg_dims(img)
        if dims is None:
            return
        expected_native = (
            EXPECTED_PREVIEW[0] * NATIVE_SCALE,
            EXPECTED_PREVIEW[1] * NATIVE_SCALE,
        )
        if dims != EXPECTED_PREVIEW and dims != expected_native:
            raise GeometryDrift(
                f"screenshot dims {dims} not in {(EXPECTED_PREVIEW, expected_native)}"
            )
        preview_dims = (
            (dims[0] // NATIVE_SCALE, dims[1] // NATIVE_SCALE)
            if dims == expected_native
            else dims
        )
        if VPhoneClient._locked_dims is None:
            VPhoneClient._locked_dims = preview_dims
        elif preview_dims != VPhoneClient._locked_dims:
            raise GeometryDrift(
                f"geometry drifted: locked={VPhoneClient._locked_dims} now={preview_dims}"
            )

    # ------------------------------------------------------------------
    # Read-only device methods
    # ------------------------------------------------------------------

    def capabilities(self, timeout: float = 5.0) -> dict[str, Any]:
        response = self._send({"t": "capabilities", "screen": False}, timeout=timeout)
        result = dict(response)
        if response.get("ok") is True:
            result["available"] = True
            result.pop("reason", None)
            commands = response.get("commands")
            if isinstance(commands, Mapping):
                result["available_commands"] = sorted(
                    str(name) for name, enabled in commands.items() if enabled is True
                )
            elif isinstance(commands, list):
                result["available_commands"] = sorted({str(name) for name in commands})
            return result
        error = str(response.get("error") or "")
        normalized = error.casefold()
        unsupported = "capabilities" in normalized and any(
            marker in normalized
            for marker in ("unknown command", "unknown type", "unsupported command")
        )
        result["available"] = False
        result["reason"] = "unsupported" if unsupported else "unavailable"
        return result

    def screenshot(self) -> dict[str, Any]:
        return self._send({"t": "screenshot"})

    def app_list(
        self,
        *,
        filter: str | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        msg: dict[str, Any] = {"t": "app_list", "screen": False}
        if filter:
            msg["filter"] = str(filter)
        return self._send(msg, timeout=timeout)

    def location_current(self, timeout_s: float = 10.0) -> dict[str, Any]:
        return self._send(
            {"t": "location_current", "timeout": timeout_s, "screen": False},
            timeout=max(15.0, float(timeout_s) + 5.0),
        )

    # ------------------------------------------------------------------
    # Mutating device methods
    # ------------------------------------------------------------------

    def tap(self, x: float, y: float, screen: bool = True) -> dict[str, Any]:
        nx = int(x * self._scale)
        ny = int(y * self._scale)
        return self._send({"t": "tap", "x": nx, "y": ny, "screen": screen})

    def swipe(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        ms: int = 300,
        screen: bool = True,
    ) -> dict[str, Any]:
        return self._send({
            "t": "swipe",
            "x1": int(x1 * self._scale),
            "y1": int(y1 * self._scale),
            "x2": int(x2 * self._scale),
            "y2": int(y2 * self._scale),
            "ms": ms,
            "screen": screen,
        })

    def key(self, name: str, screen: bool = True) -> dict[str, Any]:
        normalized = str(name or "").strip()
        if normalized not in HARDWARE_KEYS:
            raise ValueError(
                f"unknown hardware key {name!r}; expected one of {', '.join(HARDWARE_KEYS)}"
            )
        return self._send({"t": "key", "name": normalized, "screen": screen})

    def type_text(self, text: str, timeout: float = 5.0) -> dict[str, Any]:
        # Guest clipboard writes have a long tail; keep timeout >= 3s per AGENTS.md.
        return self._send(
            {"t": "type", "text": text, "screen": False},
            timeout=max(float(timeout), 3.0),
        )

    def launch_app(self, bundle_id: str, timeout: float = 15.0) -> dict[str, Any]:
        bundle = str(bundle_id or "").strip()
        if not bundle:
            raise ValueError("bundle_id must be non-empty")
        return self._send(
            {"t": "app_launch", "bundle_id": bundle, "screen": False},
            timeout=timeout,
        )

    def terminate_app(self, bundle_id: str, timeout: float = 15.0) -> dict[str, Any]:
        bundle = str(bundle_id or "").strip()
        if not bundle:
            raise ValueError("bundle_id must be non-empty")
        return self._send(
            {"t": "app_terminate", "bundle_id": bundle, "screen": False},
            timeout=timeout,
        )

    def set_location(
        self,
        latitude: float,
        longitude: float,
        *,
        altitude: float = 0.0,
        horizontal_accuracy: float = 5.0,
        vertical_accuracy: float = 5.0,
        speed: float = -1.0,
        course: float = -1.0,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        return self._send(
            {
                "t": "location",
                "lat": latitude,
                "lon": longitude,
                "alt": altitude,
                "hacc": horizontal_accuracy,
                "vacc": vertical_accuracy,
                "speed": speed,
                "course": course,
                "screen": False,
            },
            timeout=timeout,
        )

    def stop_location(self, timeout: float = 15.0) -> dict[str, Any]:
        return self._send({"t": "location_stop", "screen": False}, timeout=timeout)


__all__ = [
    "CommandNotAdmitted",
    "DEFAULT_SOCK",
    "EXPECTED_PREVIEW",
    "GeometryDrift",
    "HARDWARE_KEYS",
    "HEALTH_PATH",
    "NATIVE_SCALE",
    "NotReady",
    "PROTOCOL_LEGACY",
    "PROTOCOL_UPSTREAM_V1",
    "ProtocolError",
    "STALE_AFTER_S",
    "VPhoneClient",
]
