"""vphone_uds.py — minimal client for vphone.sock with timing + geometry guard.

Designed as the shared probe/MCP foundation. Single short-lived UDS connection
per request, line-delimited JSON, half-close the write side to signal EOR.

The host's response shape is {"ok": bool, "image"?: <base64>, "error"?: str}.
The image, when present, is JPEG. We parse the JPEG SOF0/SOF2 marker locally to
recover (width, height) without pulling in Pillow, which gives us a fail-closed
geometry check for free: if dims ever drift from the session's first frame, we
reject the response instead of mis-mapping coordinates.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Resolve the selected VM's UDS socket. The default is vm-1/; selecting
# VM_DIR=rig-b moves the whole process to the sibling rig-b image.
def _resolve_default_sock():
    from autophone.device.target import resolve_target

    return str(resolve_target().sock)

READ_ONLY_COMMANDS = frozenset({
    "screenshot", "app_list", "app_foreground", "ping", "file_get", "capabilities",
    "location_current", "location_source_status",
})

DEFAULT_SOCK = _resolve_default_sock()


def _screenshot_color_default() -> bool:
    """Screenshot color preference, switchable via VPHONE_SCREENSHOT_COLOR.

    Default = color (sRGB). 录制/回放/识别都从彩色截图受益，灰阶只在显式要紧凑
    小图时才有意义。设 env 为 0/false/no/off 切回灰阶。与 vphone_mcp 的
    VPHONE_MCP_SCREENSHOT_COLOR 同语义。
    """
    v = os.environ.get("VPHONE_SCREENSHOT_COLOR", "1").strip().lower()
    return v not in ("0", "false", "no", "off", "")

# Legacy portrait preview when the host supplies no screen descriptor.
# Coordinate conversion for existing input callers retains its original scale.
EXPECTED_PREVIEW = (430, 932)
NATIVE_SCALE = 3

# 硬件键的封闭集，镜像 guest 侧 VPhoneHostControl 的 `key` 白名单（HID usage
# 表只认这四个）。名字写错在 guest 侧只会回一句 unknown key，所以每个入口都拿
# 它做事前校验，别让「按了但没反应」看起来像设备没响应。
HARDWARE_KEYS: tuple[str, ...] = ("home", "power", "volup", "voldown")


class ProtocolError(RuntimeError):
    pass


class CommandNotAdmitted(ProtocolError):
    """The selected upstream endpoint did not advertise this command."""


class GeometryDrift(RuntimeError):
    pass


def _expected_screenshot_preview(capabilities: dict, reply: dict) -> tuple[int, int]:
    # The per-capture panel supersedes the configured framebuffer on folding guests.
    result = reply.get("result")
    if isinstance(result, dict) and "screen" in result:
        screen = result["screen"]
    elif "screen" in capabilities:
        screen = capabilities["screen"]
    else:
        return EXPECTED_PREVIEW
    if not isinstance(screen, dict):
        raise GeometryDrift("invalid screenshot screen descriptor")
    width, height, scale = (screen.get(key) for key in ("width", "height", "scale"))
    if not all(type(n) is int and n > 0 for n in (width, height, scale)) or scale > min(width, height):
        raise GeometryDrift("invalid screenshot screen dimensions or scale")
    return width // scale, height // scale


@dataclass
class Sample:
    """One round-trip trace record."""
    cmd: str
    ok: bool
    rt_ms: float
    t_write_ms: float
    t_wait_first_byte_ms: float
    t_read_total_ms: float
    bytes_recv: int
    image_bytes: int = 0
    image_w: int | None = None
    image_h: int | None = None
    image_fmt: str | None = None
    error: str | None = None
    request: dict[str, Any] = field(default_factory=dict)
    response_keys: list[str] = field(default_factory=list)


def _parse_jpeg_dims(buf: bytes) -> tuple[int, int] | None:
    """Return (width, height) from the first SOF marker; None if not JPEG."""
    if len(buf) < 4 or buf[0:2] != b"\xff\xd8":
        return None
    i = 2
    n = len(buf)
    # SOF0..SOF15, excluding DHT/DAC/JPG (FFC4, FFCC, FFC8)
    SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while i < n - 1:
        if buf[i] != 0xFF:
            return None
        marker = buf[i + 1]
        i += 2
        if marker == 0xD8 or marker == 0xD9:
            continue
        if marker == 0xDA:  # SOS — past headers
            return None
        if i + 2 > n:
            return None
        seg_len = struct.unpack(">H", buf[i:i + 2])[0]
        if marker in SOF:
            # payload: precision(1) height(2) width(2)
            if i + 7 > n:
                return None
            h = struct.unpack(">H", buf[i + 3:i + 5])[0]
            w = struct.unpack(">H", buf[i + 5:i + 7])[0]
            return (w, h)
        i += seg_len
    return None


def _detect_image_fmt(buf: bytes) -> str:
    if len(buf) >= 3 and buf[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if len(buf) >= 4 and buf[:4] == b"\x89PNG":
        return "png"
    return f"unknown:{buf[:4].hex()}"


def request(
    msg: dict[str, Any],
    *,
    sock_path: str = DEFAULT_SOCK,
    timeout: float = 15.0,
    parse_image: bool = True,
    protocol: str | None = None,
) -> tuple[dict[str, Any], Sample]:
    from autophone.device.stage_guard import StageGuard, uds_operation

    with StageGuard.operation_if_bound(uds_operation(str(msg.get("t") or "")), socket_path=sock_path):
        result = _request(msg, sock_path=sock_path, timeout=timeout, parse_image=parse_image,
                          **({"protocol": protocol} if protocol is not None else {}))
        response = result[0] if isinstance(result, tuple) and result else None
        if StageGuard.binding() is not None and isinstance(response, dict) and response.get("image"):
            # BL-227：执行侧（已绑定 Stage）取到的画面记为「最近帧」，预览通道在 Run 期间
            # 返回它而不访问设备。只保存已有 base64 字符串的引用，不解码、不落盘。
            from autophone.device.executor_frames import record_response

            record_response(str(msg.get("t") or ""), response, sock_path=sock_path)
        return result


def _request(
    msg: dict[str, Any],
    *,
    sock_path: str = DEFAULT_SOCK,
    timeout: float = 15.0,
    parse_image: bool = True,
    protocol: str | None = None,
) -> tuple[dict[str, Any], Sample]:
    """Send one request. Returns (parsed_response, timing_sample).

    Timings:
      t_write_ms         - time from connect to sendall+shutdown return
      t_wait_first_byte  - time from shutdown(W) to first recv() returning data
      t_read_total_ms    - time from first byte to EOF
      rt_ms              - total wall time from connect() to close()
    """
    if msg.get("t") not in READ_ONLY_COMMANDS:
        from autophone.device.target import check_device_write

        check_device_write(str(msg.get("t") or "unknown"))
    # 截图默认彩色（除非调用方已显式给 color，或 screen:false 不出图）。
    if (
        msg.get("t") == "screenshot"
        and "color" not in msg
        and msg.get("screen", True) is not False
        and _screenshot_color_default()
    ):
        msg = {**msg, "color": True}

    from autophone.device.target import resolve_protocol
    from autophone.device.upstream_wire import admitted, prepare
    mode = resolve_protocol(sock_path, protocol)
    if mode == "upstream_v1":
        dispatched = False
        try:
            with prepare(msg) as (wire, decode):
                if len((json.dumps(wire, ensure_ascii=False) + "\n").encode()) > 1 << 20:
                    raise ProtocolError("request exceeds protocol frame limit")
                budget_start = time.monotonic()
                request_start = time.perf_counter()
                if msg.get("t") not in {"ping", "capabilities"}:
                    capabilities, _, _ = _exchange({"t": "capabilities", "screen": False}, sock_path, timeout, mode)
                    if not admitted(capabilities, msg.get("t")):
                        raise CommandNotAdmitted(f"upstream command is not admitted: {msg.get('t')}")
                remaining = timeout - (time.monotonic() - budget_start)
                obj, times, buf = _exchange(wire, sock_path, remaining, mode, logical_command=msg.get("t"))
                dispatched = True
                times = (request_start, *times[1:])
                obj = decode(obj)
        except ValueError as exc:
            error = ProtocolError(str(exc))
            if dispatched and msg.get("t") not in READ_ONLY_COMMANDS:
                error.operation_may_continue = True
                error.command = msg.get("t")
            raise error from exc
    else:
        obj, times, buf = _exchange(msg, sock_path, timeout, mode)
    t_connect, t_shutdown, first_chunk_at, t_eof = times

    image_b64 = obj.get("image") if parse_image or (mode == "upstream_v1" and msg.get("t") == "screenshot") else None
    image_bytes = 0
    image_fmt: str | None = None
    image_wh: tuple[int, int] | None = None
    if image_b64:
        img = base64.b64decode(image_b64)
        image_bytes = len(img)
        image_fmt = _detect_image_fmt(img)
        image_wh = _parse_jpeg_dims(img) if image_fmt == "jpeg" else None
    if mode == "upstream_v1" and msg.get("t") == "screenshot" and obj.get("ok") is True:
        expected = _expected_screenshot_preview(capabilities, obj)
        if image_fmt != "jpeg" or image_wh != expected:
            raise GeometryDrift(f"upstream screenshot geometry/format refused: {image_fmt} {image_wh}, expected {expected}")

    rt_ms = (t_eof - t_connect) * 1000
    sample = Sample(
        cmd=msg.get("t", "?"),
        ok=bool(obj.get("ok")),
        rt_ms=round(rt_ms, 2),
        t_write_ms=round((t_shutdown - t_connect) * 1000, 2),
        t_wait_first_byte_ms=round(((first_chunk_at or t_eof) - t_shutdown) * 1000, 2),
        t_read_total_ms=round((t_eof - (first_chunk_at or t_eof)) * 1000, 2),
        bytes_recv=len(buf),
        image_bytes=image_bytes,
        image_w=image_wh[0] if image_wh else None,
        image_h=image_wh[1] if image_wh else None,
        image_fmt=image_fmt,
        error=obj.get("error"),
        request=dict(msg),
        response_keys=sorted(obj.keys()),
    )
    return obj, sample



def _check_peer_uid(channel):
    import sys
    if sys.platform == "darwin":
        version, peer_uid = struct.unpack_from("II", channel.getsockopt(0, 1, 80))
        if version != 0 or peer_uid != os.geteuid():
            raise ProtocolError("UDS peer UID mismatch")
    elif sys.platform.startswith("linux"):
        _pid, peer_uid, _gid = struct.unpack("3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if peer_uid != os.geteuid():
            raise ProtocolError("UDS peer UID mismatch")
    else:
        raise ProtocolError("unsupported UDS peer credential platform")

def _exchange(message, sock_path, timeout, mode, *, logical_command=None):
    if timeout <= 0:
        raise ValueError("positive UDS timeout required")
    limit = (1 << 20) if mode == "upstream_v1" else None
    payload = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
    if limit is not None and len(payload) > limit:
        raise ProtocolError("request exceeds protocol frame limit")
    t_connect = time.perf_counter()
    deadline = time.monotonic() + timeout
    channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    chunks = []
    size = 0
    first = None
    dispatched = False
    try:
        channel.settimeout(timeout)
        channel.connect(sock_path)
        if mode == "upstream_v1":
            _check_peer_uid(channel)
        dispatched = True
        channel.sendall(payload)
        if mode == "legacy":
            try:
                channel.shutdown(socket.SHUT_WR)
            except OSError as exc:
                if exc.errno not in (57, 32):
                    raise
        t_shutdown = time.perf_counter()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("UDS response deadline exceeded")
            channel.settimeout(remaining)
            chunk = channel.recv(65536)
            if not chunk:
                break
            if first is None:
                first = time.perf_counter()
            size += len(chunk)
            if limit is not None and size > limit:
                raise ProtocolError("response exceeds protocol frame limit")
            chunks.append(chunk)
        t_eof = time.perf_counter()
        data = b"".join(chunks)
        line, separator, rest = data.partition(b"\n")
        if mode == "upstream_v1" and (not separator or rest.strip()):
            raise ProtocolError("incomplete or multiple UDS responses")
        try:
            obj = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise ProtocolError(f"bad response: {exc}") from exc
        if not isinstance(obj, dict) or (mode == "upstream_v1" and type(obj.get("ok")) is not bool):
            raise ProtocolError("response must be an object with boolean ok")
        return obj, (t_connect, t_shutdown, first, t_eof), data
    except (OSError, ProtocolError) as exc:
        if dispatched and logical_command is not None and logical_command not in READ_ONLY_COMMANDS:
            exc.operation_may_continue = True
            exc.command = logical_command
        raise
    finally:
        channel.close()



class GeometryGuard:
    """Locks onto the first observed screenshot geometry; rejects drift.

    Use as a long-lived object across a probe session. The first image it sees
    sets (w, h); every subsequent image must match or it raises GeometryDrift.
    This is the fail-closed behavior architecture-v1.md §七 2-A asks for.
    """

    def __init__(self) -> None:
        self.locked: tuple[int, int] | None = None
        self.fmt: str | None = None

    def check(self, sample: Sample) -> None:
        if sample.image_w is None:
            return
        cur = (sample.image_w, sample.image_h)
        if self.locked is None:
            self.locked = cur
            self.fmt = sample.image_fmt
            return
        if cur != self.locked:
            raise GeometryDrift(
                f"image geometry drifted: locked={self.locked} fmt={self.fmt} "
                f"now={cur} fmt={sample.image_fmt}"
            )


def preview_to_native(xp: int, yp: int) -> tuple[int, int]:
    return (xp * NATIVE_SCALE, yp * NATIVE_SCALE)


def save_image(sample_response: dict[str, Any], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    img = sample_response.get("image")
    if not img:
        raise ValueError("response has no image")
    p.write_bytes(base64.b64decode(img))
    return p


def command_inventory(value: Any) -> list[str] | None:
    """Normalize a capability ``commands`` field into available command names.

    vphone-cli's ``capabilities`` response reports ``commands`` as a mapping of
    command name to availability (``{"shell": true, "file_get": false, ...}``);
    only an explicit JSON ``true`` counts as available. A plain sequence of names
    is accepted for older fixtures. Any other shape returns ``None`` so callers
    can report the inventory as unavailable instead of guessing.
    """
    if isinstance(value, Mapping):
        return sorted(str(name) for name, enabled in value.items() if enabled is True)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return sorted({str(name) for name in value})
    return None


def get_capabilities(
    *,
    sock_path: str = DEFAULT_SOCK,
    timeout: float = 5.0,
    request_fn: Callable[..., tuple[dict[str, Any], Sample]] | None = None,
) -> dict[str, Any]:
    """Read the UDS capability advertisement without probing other commands.

    Older vphone-cli builds do not implement ``capabilities``. Their explicit
    unknown-command response is returned as ``available=false`` so callers can
    distinguish protocol age from an offline socket. Connection and transport
    failures are intentionally not caught; the compatibility layer records
    those as VM availability failures.

    No fallback command is sent. Some UDS commands mutate device state, so an
    installation preflight must not infer capabilities by trying them.

    A successful response keeps every upstream field and adds
    ``available_commands``: the sorted names whose ``commands`` entry is
    ``true`` (see :func:`command_inventory`). It is omitted when ``commands``
    is missing or malformed.
    """
    send = request if request_fn is None else request_fn
    response, _sample = send(
        {"t": "capabilities", "screen": False},
        sock_path=sock_path,
        timeout=timeout,
        parse_image=False,
    )
    result = dict(response)
    if response.get("ok") is True:
        result["available"] = True
        result.pop("reason", None)
        result.pop("available_commands", None)
        inventory = command_inventory(response.get("commands"))
        if inventory is not None:
            result["available_commands"] = inventory
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


def ipa_install(
    guest_ipa_path: str,
    *,
    registration: str = "User",
    cert_path: str | None = None,
    sock_path: str = DEFAULT_SOCK,
    timeout: float = 300.0,
) -> tuple[dict[str, Any], Sample]:
    """Install an IPA using vphoned's built-in installer.

    Args:
        guest_ipa_path: Path to IPA file on guest filesystem
        registration: "User" or "System" (default: "User")
        cert_path: Optional path to .p12 certificate on guest
        sock_path: UDS socket path
        timeout: Request timeout in seconds (default: 300s for large IPAs)

    Returns:
        (response_dict, sample) tuple. response_dict["ok"] is True on success.

    Raises:
        ProtocolError: If response is malformed or connection fails
    """
    msg: dict[str, Any] = {
        "t": "ipa_install",
        "path": guest_ipa_path,
        "registration": registration,
    }
    if cert_path:
        msg["cert_path"] = cert_path

    return request(msg, sock_path=sock_path, timeout=timeout, parse_image=False)


def set_location(
    latitude: float,
    longitude: float,
    *,
    altitude: float = 0.0,
    horizontal_accuracy: float = 5.0,
    vertical_accuracy: float = 5.0,
    speed: float = 0.0,
    course: float = -1.0,
    sock_path: str = DEFAULT_SOCK,
    timeout: float = 15.0,
) -> tuple[dict[str, Any], Sample]:
    """Push a simulated GPS fix into the guest system-wide CLSimulationManager.

    This drives the same native location channel as the vphone-cli GUI menu,
    now exposed on the automation UDS surface. Fail-closed: the host returns
    ok=false when the guest is disconnected or does not advertise the
    "location" capability (old base image without vphoned_location).

    NOTE (layer caveat): this feeds CoreLocation system-wide. Apps that read
    location through a third-party location SDK may not consume it; whether a
    given SDK observes the system fix must be verified on the live device.

    Returns (response_dict, sample); response_dict["ok"] is True on success.
    """
    msg: dict[str, Any] = {
        "t": "location",
        "lat": float(latitude),
        "lon": float(longitude),
        "alt": float(altitude),
        "hacc": float(horizontal_accuracy),
        "vacc": float(vertical_accuracy),
        "speed": float(speed),
        "course": float(course),
    }
    return request(msg, sock_path=sock_path, timeout=timeout, parse_image=False)


def stop_location(
    *,
    sock_path: str = DEFAULT_SOCK,
    timeout: float = 15.0,
) -> tuple[dict[str, Any], Sample]:
    """Stop simulated GPS injection (clears CLSimulationManager). Fail-closed."""
    return request(
        {"t": "location_stop"}, sock_path=sock_path, timeout=timeout, parse_image=False
    )
