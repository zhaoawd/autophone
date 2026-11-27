"""UDS wire protocol framing and upstream_v1 compatibility for vphone_mcp.

Mirrors ``autophone/device/uds.py`` and ``autophone/device/upstream_wire.py``
with stdlib-only dependencies so ``vphone_mcp`` remains standalone while
staying wire-compatible with both ``legacy`` and ``upstream_v1`` vphone-cli
endpoints.
"""
from __future__ import annotations

import base64
import json
import math
import os
import socket
import struct
import sys
import time
from typing import Any, Callable, Mapping

PROTOCOL_LEGACY = "legacy"
PROTOCOL_UPSTREAM_V1 = "upstream_v1"
SUPPORTED_PROTOCOLS = frozenset({PROTOCOL_LEGACY, PROTOCOL_UPSTREAM_V1})

EXPECTED_PREVIEW = (430, 932)
NATIVE_SCALE = 3
FRAME_LIMIT = 1 << 20  # 1 MiB strict upstream_v1 frame cap

# Closed hardware key set matching guest VPhoneHostControl HID whitelist.
HARDWARE_KEYS: tuple[str, ...] = ("home", "power", "volup", "voldown")

READ_ONLY_COMMANDS = frozenset({
    "screenshot",
    "app_list",
    "app_foreground",
    "ping",
    "file_get",
    "capabilities",
    "location_current",
    "location_source_status",
})

RPC_METHODS: dict[str, str] = {
    "app_list": "apps.list",
    "app_foreground": "apps.foreground",
    "app_launch": "apps.launch",
    "app_terminate": "apps.terminate",
    "open_url": "apps.open_url",
    "shell": "shell.exec",
    "ipa_install": "apps.install",
    "location": "location.set",
    "location_current": "location.current",
    "location_stop": "location.clear",
}

DIRECT_COMMANDS = frozenset({"ping", "capabilities", "tap", "swipe", "key", "type"})
LOCATION_COMMANDS = frozenset({"location", "location_current", "location_stop"})


class ProtocolError(RuntimeError):
    """UDS framing, transport, or upstream_v1 contract error."""


class CommandNotAdmitted(ProtocolError):
    """The selected upstream_v1 endpoint did not advertise this command."""


class GeometryDrift(RuntimeError):
    """Screenshot dimensions or format violated expected panel geometry."""


class NotReady(RuntimeError):
    """VM UDS endpoint or guest connection is not ready within timeout."""


def resolve_protocol(
    explicit: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Resolve active wire protocol ('legacy' or 'upstream_v1')."""
    source = os.environ if env is None else env
    if explicit is not None:
        mode = str(explicit).strip()
    else:
        mode = (
            str(source.get("VPHONE_MCP_PROTOCOL") or "").strip()
            or str(source.get("AUTOPHONE_VPHONE_PROTOCOL") or "").strip()
            or PROTOCOL_LEGACY
        )
    if mode not in SUPPORTED_PROTOCOLS:
        raise ValueError(f"unsupported vphone protocol: {mode!r}")
    return mode


def parse_jpeg_dims(buf: bytes) -> tuple[int, int] | None:
    """Return (width, height) from the first JPEG SOF marker; None if not JPEG."""
    if len(buf) < 4 or buf[0:2] != b"\xff\xd8":
        return None
    i = 2
    n = len(buf)
    sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while i < n - 1:
        if buf[i] != 0xFF:
            return None
        marker = buf[i + 1]
        i += 2
        if marker in (0xD8, 0xD9):
            continue
        if marker == 0xDA:
            return None
        if i + 2 > n:
            return None
        seg_len = struct.unpack(">H", buf[i:i + 2])[0]
        if marker in sof:
            if i + 7 > n:
                return None
            h = struct.unpack(">H", buf[i + 3:i + 5])[0]
            w = struct.unpack(">H", buf[i + 5:i + 7])[0]
            return (w, h)
        i += seg_len
    return None


def detect_image_fmt(buf: bytes) -> str:
    if len(buf) >= 3 and buf[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if len(buf) >= 4 and buf[:4] == b"\x89PNG":
        return "png"
    return f"unknown:{buf[:4].hex()}"


def _screen_descriptor(capabilities: Mapping[str, Any], reply: Mapping[str, Any]) -> tuple[int, int, int] | None:
    result = reply.get("result")
    if isinstance(result, Mapping) and "screen" in result:
        screen = result["screen"]
    elif "screen" in capabilities:
        screen = capabilities["screen"]
    else:
        return None
    if not isinstance(screen, Mapping):
        raise GeometryDrift("invalid screenshot screen descriptor")
    width = screen.get("width")
    height = screen.get("height")
    scale = screen.get("scale")
    if (
        not all(type(n) is int and n > 0 for n in (width, height, scale))
        or scale > min(width, height)
    ):
        raise GeometryDrift("invalid screenshot screen dimensions or scale")
    return int(width), int(height), int(scale)


def expected_screenshot_preview(
    capabilities: Mapping[str, Any],
    reply: Mapping[str, Any],
) -> tuple[int, int]:
    """Compute expected preview (width, height) from upstream_v1 screen descriptors."""
    descriptor = _screen_descriptor(capabilities, reply)
    if descriptor is None:
        return EXPECTED_PREVIEW
    width, height, scale = descriptor
    return width // scale, height // scale


def extract_screen_scale(
    capabilities: Mapping[str, Any],
    reply: Mapping[str, Any] | None = None,
) -> int:
    """Return panel scale factor from screen descriptor, defaulting to NATIVE_SCALE."""
    descriptor = _screen_descriptor(capabilities, reply or {})
    if descriptor is None:
        return NATIVE_SCALE
    return descriptor[2]


def validate_upstream_screenshot(
    capabilities: Mapping[str, Any],
    reply: Mapping[str, Any],
) -> tuple[int, int]:
    """Validate upstream_v1 screenshot format (JPEG) and expected panel geometry."""
    expected = expected_screenshot_preview(capabilities, reply)
    img_b64 = reply.get("image")
    if not isinstance(img_b64, str) or not img_b64:
        raise GeometryDrift(
            f"upstream screenshot geometry/format refused: missing image, expected {expected}"
        )
    try:
        img = base64.b64decode(img_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise GeometryDrift(f"upstream screenshot base64 invalid: {exc}") from exc
    fmt = detect_image_fmt(img)
    dims = parse_jpeg_dims(img) if fmt == "jpeg" else None
    if fmt != "jpeg" or dims != expected:
        raise GeometryDrift(
            f"upstream screenshot geometry/format refused: {fmt} {dims}, expected {expected}"
        )
    return dims


def admitted(capabilities: Mapping[str, Any], kind: str) -> bool:
    """Check whether an upstream_v1 endpoint advertises support for ``kind``."""
    commands = capabilities.get("commands")
    if (
        capabilities.get("ok") is not True
        or capabilities.get("protocol") != PROTOCOL_UPSTREAM_V1
        or not isinstance(commands, Mapping)
    ):
        return False
    if kind in LOCATION_COMMANDS:
        guest = capabilities.get("guest_capabilities")
        return (
            capabilities.get("guest_connected") is True
            and commands.get("rpc") is True
            and commands.get(kind, True) is True
            and isinstance(guest, list)
            and "location" in guest
        )
    if kind == "ipa_install":
        guest = capabilities.get("guest_capabilities")
        return (
            commands.get("ipa_install", True) is True
            and commands.get("rpc") is True
            and capabilities.get("guest_connected") is True
            and isinstance(guest, list)
            and "ipa_install" in guest
        )
    return commands.get(kind) is True


def _location_number(value: Any, field: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"location {field} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"location {field} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"location {field} must be a finite number")
    return result


def _location_params(kind: str, values: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        {"lat", "lon", "alt", "hacc", "vacc", "speed", "course"}
        if kind == "location"
        else {"timeout"}
        if kind == "location_current"
        else set()
    )
    if set(values) - allowed:
        raise ValueError("unsupported location parameter")
    if kind == "location_stop":
        return {}
    if kind == "location_current":
        timeout = _location_number(values.get("timeout", 10), "timeout")
        if not 0 < timeout <= 300:
            raise ValueError("location timeout must be more than 0 and at most 300 seconds")
        return {"timeout": timeout}
    latitude = _location_number(values.get("lat"), "latitude")
    longitude = _location_number(values.get("lon"), "longitude")
    altitude = _location_number(values.get("alt", 0), "altitude")
    hacc = _location_number(values.get("hacc", 5), "horizontal accuracy")
    vacc = _location_number(values.get("vacc", 5), "vertical accuracy")
    if not (
        -90 <= latitude <= 90
        and -180 <= longitude <= 180
        and abs(altitude) <= 100000
        and hacc >= 0
        and (vacc >= 0 or vacc == -1)
    ):
        raise ValueError("location coordinate or accuracy is outside the native range")
    params: dict[str, Any] = {
        "latitude": latitude,
        "longitude": longitude,
        "altitude": altitude,
        "horizontal_accuracy": hacc,
        "vertical_accuracy": vacc,
    }
    for field in ("speed", "course"):
        if field in values:
            number = _location_number(values[field], field)
            if number == -1:
                continue
            if number < 0 or (field == "course" and number >= 360):
                raise ValueError(f"location {field} is outside the native range")
            params[field] = number
    return params


def _validate_location_reading(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("location readback must be an object")
    latitude = _location_number(value.get("latitude"), "readback latitude")
    longitude = _location_number(value.get("longitude"), "readback longitude")
    age = _location_number(value.get("age_seconds"), "readback age")
    if (
        not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
        or age < 0
        or type(value.get("fresh")) is not bool
        or type(value.get("simulated")) is not bool
        or not isinstance(value.get("timestamp"), str)
        or not value["timestamp"]
    ):
        raise ValueError("location readback identity or freshness fields are invalid")


def prepare_upstream(
    message: Mapping[str, Any],
) -> tuple[dict[str, Any], Callable[[dict[str, Any]], dict[str, Any]]]:
    """Translate one logical command into an upstream_v1 wire message and decoder."""
    kind = message.get("t")
    if not isinstance(kind, str) or not kind:
        raise ValueError("missing logical command type 't'")
    if kind in RPC_METHODS:
        params = {k: v for k, v in message.items() if k not in {"t", "screen", "delay"}}
        if kind in LOCATION_COMMANDS:
            params = _location_params(kind, params)
        wire = {
            "t": "rpc",
            "method": RPC_METHODS[kind],
            "params": params,
            "screen": message.get("screen", False),
        }
        expected_loc = params if kind == "location" else None
        return wire, lambda resp: decode_upstream(kind, resp, expected_location=expected_loc)
    if kind == "screenshot":
        wire = {**message, "save": bool(message.get("path"))}
        return wire, lambda resp: resp
    if kind in DIRECT_COMMANDS:
        return dict(message), lambda resp: resp
    raise ValueError(f"unsupported upstream logical command: {kind}")


def decode_upstream(
    kind: str,
    response: dict[str, Any],
    *,
    expected_location: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Decode an upstream_v1 RPC response back into the logical response shape."""
    if response.get("ok") is not True:
        return response
    result = response.get("result")
    if not isinstance(result, dict):
        raise ValueError(f"{kind}: missing upstream result object")
    output = {**result, **{k: v for k, v in response.items() if k != "result"}}
    if kind == "location":
        location = result.get("location")
        _validate_location_reading(location)
        if (
            result.get("simulating") is not True
            or location["fresh"] is not True
            or location["simulated"] is not True
        ):
            raise ValueError("location set did not confirm a fresh simulated fix")
        if expected_location is not None and any(
            abs(location[field] - expected_location[field]) >= 1e-7
            for field in ("latitude", "longitude")
        ):
            raise ValueError("location readback differs from requested coordinates")
    elif kind == "location_current":
        _validate_location_reading(result)
    elif kind == "location_stop":
        if result.get("simulating") is not False:
            raise ValueError("location clear did not confirm simulation stopped")
        if "location" in result:
            _validate_location_reading(result["location"])
            if result["location"]["fresh"] and result["location"]["simulated"]:
                raise ValueError("location clear still reports a fresh simulated fix")
    elif kind == "app_list":
        apps = result.get("apps")
        if not isinstance(apps, list) or not all(isinstance(a, dict) for a in apps):
            raise ValueError("apps.list returned invalid apps")
        output["apps"] = [
            {**a, **({"data_container": a["data_path"]} if "data_path" in a else {})}
            for a in apps
        ]
    elif kind == "app_launch":
        if (
            type(result.get("pid")) is not int
            or result["pid"] <= 0
            or result.get("frontmost_verified") is not True
        ):
            output.update(
                ok=False,
                error="application launch/foreground was not verified",
                operation_may_continue=True,
            )
    elif kind == "shell":
        required = {
            "stdout": str,
            "stderr": str,
            "exit_code": int,
            "timed_out": bool,
            "stdout_truncated": bool,
            "stderr_truncated": bool,
        }
        if any(type(result.get(key)) is not expected_type for key, expected_type in required.items()):
            raise ValueError("shell.exec returned an invalid result")
        output["code"] = result["exit_code"]
        output["truncated"] = result["stdout_truncated"] or result["stderr_truncated"]
    return output


def check_peer_uid(channel: socket.socket) -> None:
    """Verify UDS peer credentials match the current effective UID (upstream_v1)."""
    if sys.platform == "darwin":
        version, peer_uid = struct.unpack_from("II", channel.getsockopt(0, 1, 80))
        if version != 0 or peer_uid != os.geteuid():
            raise ProtocolError("UDS peer UID mismatch")
    elif sys.platform.startswith("linux"):
        _pid, peer_uid, _gid = struct.unpack(
            "3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        if peer_uid != os.geteuid():
            raise ProtocolError("UDS peer UID mismatch")
    else:
        raise ProtocolError("unsupported UDS peer credential platform")


def exchange_uds(
    message: Mapping[str, Any],
    sock_path: str,
    timeout: float,
    protocol: str,
    *,
    logical_command: str | None = None,
) -> dict[str, Any]:
    """Perform one newline-delimited JSON exchange over AF_UNIX."""
    if timeout <= 0:
        raise ValueError("positive UDS timeout required")
    limit = FRAME_LIMIT if protocol == PROTOCOL_UPSTREAM_V1 else None
    payload = (json.dumps(dict(message), ensure_ascii=False) + "\n").encode("utf-8")
    if limit is not None and len(payload) > limit:
        raise ProtocolError("request exceeds protocol frame limit")

    deadline = time.monotonic() + timeout
    channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    chunks: list[bytes] = []
    size = 0
    dispatched = False
    try:
        channel.settimeout(timeout)
        try:
            channel.connect(sock_path)
        except FileNotFoundError as exc:
            raise ProtocolError(f"socket not found: {sock_path}") from exc
        except ConnectionRefusedError as exc:
            raise ProtocolError("connection refused — is vphone-cli running?") from exc

        if protocol == PROTOCOL_UPSTREAM_V1:
            check_peer_uid(channel)

        dispatched = True
        channel.sendall(payload)
        if protocol == PROTOCOL_LEGACY:
            try:
                channel.shutdown(socket.SHUT_WR)
            except OSError as exc:
                if exc.errno not in (57, 32):
                    raise

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("UDS response deadline exceeded")
            channel.settimeout(remaining)
            chunk = channel.recv(65536)
            if not chunk:
                break
            size += len(chunk)
            if limit is not None and size > limit:
                raise ProtocolError("response exceeds protocol frame limit")
            chunks.append(chunk)

        data = b"".join(chunks)
        line, separator, rest = data.partition(b"\n")
        if protocol == PROTOCOL_UPSTREAM_V1 and (not separator or rest.strip()):
            raise ProtocolError("incomplete or multiple UDS responses")
        try:
            obj = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise ProtocolError(f"bad response: {exc}; head={data[:120]!r}") from exc
        if not isinstance(obj, dict) or (
            protocol == PROTOCOL_UPSTREAM_V1 and type(obj.get("ok")) is not bool
        ):
            raise ProtocolError("response must be an object with boolean ok")
        return obj
    except (OSError, ProtocolError) as exc:
        if (
            dispatched
            and logical_command is not None
            and logical_command not in READ_ONLY_COMMANDS
        ):
            exc.operation_may_continue = True  # type: ignore[attr-defined]
            exc.command = logical_command  # type: ignore[attr-defined]
        raise
    finally:
        channel.close()


def format_response_error(resp: Mapping[str, Any]) -> str:
    """Format a protocol-level ``ok=false`` response preserving structured error codes."""
    code = str(resp.get("code") or "").strip()
    guest_error = resp.get("guest_error")
    if not code and isinstance(guest_error, Mapping):
        if isinstance(guest_error.get("error"), Mapping):
            code = str(guest_error["error"].get("code") or "").strip()
        else:
            code = str(guest_error.get("code") or "").strip()
    message = str(resp.get("error") or resp.get("detail") or "unknown error")
    if code and code not in message:
        return f"[{code}] {message}"
    return message


__all__ = [
    "CommandNotAdmitted",
    "DIRECT_COMMANDS",
    "EXPECTED_PREVIEW",
    "FRAME_LIMIT",
    "GeometryDrift",
    "HARDWARE_KEYS",
    "LOCATION_COMMANDS",
    "NATIVE_SCALE",
    "NotReady",
    "PROTOCOL_LEGACY",
    "PROTOCOL_UPSTREAM_V1",
    "ProtocolError",
    "READ_ONLY_COMMANDS",
    "RPC_METHODS",
    "SUPPORTED_PROTOCOLS",
    "admitted",
    "check_peer_uid",
    "decode_upstream",
    "detect_image_fmt",
    "exchange_uds",
    "expected_screenshot_preview",
    "extract_screen_scale",
    "format_response_error",
    "parse_jpeg_dims",
    "prepare_upstream",
    "resolve_protocol",
    "validate_upstream_screenshot",
]
