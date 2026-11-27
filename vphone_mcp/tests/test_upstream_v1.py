"""Unit tests for vphone_mcp upstream_v1 framing, capabilities, and command dispatch."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from typing import Any, Callable, Iterator

from vphone_mcp.client import (
    CommandNotAdmitted,
    GeometryDrift,
    NotReady,
    PROTOCOL_LEGACY,
    PROTOCOL_UPSTREAM_V1,
    ProtocolError,
    VPhoneClient,
)
from vphone_mcp.coordination import CoordinationConfig, DeviceCoordinator, MODE_STANDALONE
from vphone_mcp.wire import (
    FRAME_LIMIT,
    admitted,
    decode_upstream,
    prepare_upstream,
    resolve_protocol,
)


def make_jpeg(width: int, height: int) -> bytes:
    """Build a minimal valid JPEG header with SOF0 dimensions (stdlib-only)."""
    sof0_payload = struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00"
    sof0 = b"\xff\xc0" + struct.pack(">H", len(sof0_payload) + 2) + sof0_payload
    return b"\xff\xd8\xff\xe0\x00\x02" + sof0 + b"\xff\xda\x00\x02\xff\xd9"


def make_jpeg_b64(width: int, height: int) -> str:
    return base64.b64encode(make_jpeg(width, height)).decode("ascii")


def default_upstream_caps(**overrides: Any) -> dict[str, Any]:
    caps: dict[str, Any] = {
        "ok": True,
        "protocol": "upstream_v1",
        "guest_connected": True,
        "commands": {
            "screenshot": True,
            "tap": True,
            "swipe": True,
            "key": True,
            "type": True,
            "app_list": True,
            "app_launch": True,
            "app_terminate": True,
            "rpc": True,
        },
        "guest_capabilities": ["location"],
    }
    caps.update(overrides)
    return caps


@contextmanager
def uds_endpoint(
    handler: Callable[[dict[str, Any]], list[bytes]] | list[bytes],
    *,
    capabilities: dict[str, Any] | Callable[[], dict[str, Any]] | None = None,
    delay: float = 0.0,
    max_requests: int = 10,
    eof_probe_timeout: float = 0.0,
) -> Iterator[tuple[str, list[dict[str, Any]], list[bool]]]:
    """Local AF_UNIX test server tracking sent JSON messages and peer half-closes."""
    with tempfile.TemporaryDirectory(prefix="vmcp-uds-", dir="/tmp") as directory:
        sock_path = str(Path(directory) / "vphone.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(8)
        server.settimeout(0.2)
        sent: list[dict[str, Any]] = []
        eof_before_reply: list[bool] = []
        stop = threading.Event()

        def serve() -> None:
            handled = 0
            while not stop.is_set() and handled < max_requests:
                try:
                    peer, _ = server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                with peer:
                    peer.settimeout(2.0)
                    data = b""
                    while b"\n" not in data:
                        block = peer.recv(65536)
                        if not block:
                            break
                        data += block
                    if not data:
                        continue
                    message = json.loads(data.split(b"\n", 1)[0])
                    sent.append(message)
                    handled += 1

                    # Probe recv to observe whether client called SHUT_WR before reply.
                    saw_eof = False
                    try:
                        if eof_probe_timeout > 0:
                            peer.settimeout(eof_probe_timeout)
                        else:
                            peer.setblocking(False)
                        extra = peer.recv(1)
                        if extra == b"":
                            saw_eof = True
                    except (BlockingIOError, TimeoutError):
                        saw_eof = False
                    finally:
                        peer.setblocking(True)
                        peer.settimeout(2.0)
                    eof_before_reply.append(saw_eof)

                    if message.get("t") == "capabilities" and capabilities is not None:
                        caps = capabilities() if callable(capabilities) else capabilities
                        peer.sendall((json.dumps(caps) + "\n").encode("utf-8"))
                        continue

                    if delay > 0:
                        time.sleep(delay)
                    chunks = handler(message) if callable(handler) else handler
                    for chunk in chunks:
                        try:
                            peer.sendall(chunk)
                        except BrokenPipeError:
                            break

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        try:
            yield sock_path, sent, eof_before_reply
        finally:
            stop.set()
            server.close()
            worker.join(timeout=2.0)


def _standalone_client(sock_path: str, *, protocol: str, color: bool = True) -> VPhoneClient:
    VPhoneClient.reset_geometry()
    coordinator = DeviceCoordinator(CoordinationConfig(mode=MODE_STANDALONE))
    return VPhoneClient(
        socket_path=sock_path,
        color=color,
        protocol=protocol,
        coordinator=coordinator,
        env={},
    )


class TestUpstreamV1Protocol(unittest.TestCase):
    def test_protocol_resolution_and_invalid_rejected(self) -> None:
        self.assertEqual(resolve_protocol(env={}), PROTOCOL_LEGACY)
        self.assertEqual(
            resolve_protocol(env={"AUTOPHONE_VPHONE_PROTOCOL": "upstream_v1"}),
            PROTOCOL_UPSTREAM_V1,
        )
        self.assertEqual(
            resolve_protocol(
                env={
                    "AUTOPHONE_VPHONE_PROTOCOL": "legacy",
                    "VPHONE_MCP_PROTOCOL": "upstream_v1",
                }
            ),
            PROTOCOL_UPSTREAM_V1,
        )
        with self.assertRaises(ValueError):
            resolve_protocol("invalid_proto", env={})

    def test_upstream_v1_no_shut_wr_while_legacy_uses_shut_wr(self) -> None:
        img = make_jpeg_b64(430, 932)
        reply = [(json.dumps({"ok": True, "image": img}) + "\n").encode("utf-8")]

        with uds_endpoint(
            reply,
            capabilities=default_upstream_caps(),
            eof_probe_timeout=0.05,
        ) as (path, sent, eofs):
            client = _standalone_client(path, protocol=PROTOCOL_UPSTREAM_V1)
            resp = client.screenshot()
            self.assertTrue(resp["ok"])
            self.assertEqual([m["t"] for m in sent], ["capabilities", "screenshot"])
            self.assertEqual(sent[1], {"t": "screenshot", "color": True, "save": False})
            self.assertEqual(eofs, [False, False])

        with uds_endpoint(reply, eof_probe_timeout=0.5) as (path, sent, eofs):
            client = _standalone_client(path, protocol=PROTOCOL_LEGACY)
            resp = client.screenshot()
            self.assertTrue(resp["ok"])
            self.assertEqual([m["t"] for m in sent], ["screenshot"])
            self.assertEqual(eofs, [True])

    def test_upstream_v1_frame_limits_and_malformed_replies(self) -> None:
        # Request frame limit checked before connect (missing socket).
        client = _standalone_client("/tmp/does-not-exist-vmcp.sock", protocol=PROTOCOL_UPSTREAM_V1)
        with self.assertRaisesRegex(ProtocolError, "request exceeds"):
            client.type_text("x" * FRAME_LIMIT)

        # Unknown logical command rejected before connect.
        with self.assertRaises(ValueError):
            prepare_upstream({"t": "nonexistent_cmd"})

        # Response > 1 MiB rejected.
        oversized = [b'{"ok":true,"padding":"' + b"x" * FRAME_LIMIT + b'"}\n']
        with uds_endpoint(oversized) as (path, _, _):
            c = _standalone_client(path, protocol=PROTOCOL_UPSTREAM_V1)
            with self.assertRaisesRegex(ProtocolError, "response exceeds"):
                c.capabilities()

        # Malformed or non-boolean ok replies rejected.
        for bad_body in (b'{"ok":true}', b'{"ok":true}\n{}\n', b"[]\n", b'{"ok":1}\n'):
            with uds_endpoint([bad_body]) as (path, _, _):
                c = _standalone_client(path, protocol=PROTOCOL_UPSTREAM_V1)
                with self.assertRaises(ProtocolError):
                    c.capabilities()

    def test_upstream_v1_write_failure_marks_operation_may_continue_without_replay(self) -> None:
        with uds_endpoint(
            [b'{"ok":true}\n'],
            capabilities=default_upstream_caps(),
            delay=0.15,
        ) as (path, sent, _):
            client = _standalone_client(path, protocol=PROTOCOL_UPSTREAM_V1)
            with self.assertRaises(TimeoutError) as ctx:
                client._send({"t": "tap", "x": 10, "y": 20}, timeout=0.05)
            self.assertTrue(getattr(ctx.exception, "operation_may_continue", False))
            self.assertEqual(getattr(ctx.exception, "command", None), "tap")
            self.assertEqual([m["t"] for m in sent], ["capabilities", "tap"])

        # Read-only failure does NOT set operation_may_continue.
        with uds_endpoint(
            [b"invalid\n"],
            capabilities=default_upstream_caps(),
        ) as (path, sent, _):
            client = _standalone_client(path, protocol=PROTOCOL_UPSTREAM_V1)
            with self.assertRaises(ProtocolError) as ctx:
                client.screenshot()
            self.assertFalse(getattr(ctx.exception, "operation_may_continue", False))
            self.assertEqual([m["t"] for m in sent], ["capabilities", "screenshot"])

    def test_upstream_v1_capabilities_admission_gating(self) -> None:
        denied_caps = default_upstream_caps(commands={"screenshot": True, "tap": False})
        with uds_endpoint([b'{"ok":true}\n'], capabilities=denied_caps) as (path, sent, _):
            client = _standalone_client(path, protocol=PROTOCOL_UPSTREAM_V1)
            with self.assertRaises(CommandNotAdmitted):
                client.tap(10, 20)
            self.assertEqual([m["t"] for m in sent], ["capabilities"])

        # Location requires guest_connected + rpc + guest_capabilities["location"].
        self.assertTrue(admitted(default_upstream_caps(), "location"))
        self.assertFalse(admitted(default_upstream_caps(guest_connected=False), "location"))
        self.assertFalse(admitted(default_upstream_caps(guest_capabilities=[]), "location"))

    def test_upstream_v1_capabilities_normalization(self) -> None:
        caps_reply = [
            (json.dumps(default_upstream_caps()) + "\n").encode("utf-8")
        ]
        with uds_endpoint(caps_reply) as (path, _, _):
            client = _standalone_client(path, protocol=PROTOCOL_UPSTREAM_V1)
            result = client.capabilities()
            self.assertTrue(result["available"])
            self.assertIn("screenshot", result["available_commands"])
            self.assertIn("tap", result["available_commands"])

        legacy_unknown = [b'{"ok":false,"error":"unknown command: capabilities"}\n']
        with uds_endpoint(legacy_unknown) as (path, _, _):
            client = _standalone_client(path, protocol=PROTOCOL_LEGACY)
            result = client.capabilities()
            self.assertFalse(result["available"])
            self.assertEqual(result["reason"], "unsupported")

    def test_upstream_v1_dynamic_panel_geometry_and_scaling(self) -> None:
        fold_caps = default_upstream_caps(screen={"width": 1398, "height": 2034, "scale": 3})
        img_fold = make_jpeg_b64(466, 678)
        img_unfolded = make_jpeg_b64(669, 951)

        def handler(msg: dict[str, Any]) -> list[bytes]:
            if msg["t"] == "screenshot":
                return [
                    (
                        json.dumps({
                            "ok": True,
                            "image": img_unfolded,
                            "result": {"screen": {"width": 2007, "height": 2853, "scale": 3}},
                        })
                        + "\n"
                    ).encode("utf-8")
                ]
            return [b'{"ok":true}\n']

        with uds_endpoint(handler, capabilities=fold_caps) as (path, sent, _):
            client = _standalone_client(path, protocol=PROTOCOL_UPSTREAM_V1)
            resp = client.screenshot()
            self.assertTrue(resp["ok"])

        # Mismatched dimensions fail closed with GeometryDrift.
        bad_shot = [(json.dumps({"ok": True, "image": img_fold}) + "\n").encode("utf-8")]
        with uds_endpoint(bad_shot, capabilities=default_upstream_caps()) as (path, _, _):
            client = _standalone_client(path, protocol=PROTOCOL_UPSTREAM_V1)
            with self.assertRaises(GeometryDrift):
                client.screenshot()

        # Non-JPEG image rejected with GeometryDrift.
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
        png_shot = [
            (json.dumps({"ok": True, "image": base64.b64encode(png_bytes).decode()}) + "\n").encode()
        ]
        with uds_endpoint(png_shot, capabilities=default_upstream_caps()) as (path, _, _):
            client = _standalone_client(path, protocol=PROTOCOL_UPSTREAM_V1)
            with self.assertRaises(GeometryDrift):
                client.screenshot()

    def test_upstream_v1_hid_app_and_location_commands(self) -> None:
        reading = {
            "latitude": 31.23,
            "longitude": 121.47,
            "fresh": True,
            "simulated": True,
            "timestamp": "2026-11-27T00:00:00.000Z",
            "age_seconds": 0.05,
        }

        def handler(msg: dict[str, Any]) -> list[bytes]:
            if msg["t"] == "rpc":
                method = msg["method"]
                if method == "apps.list":
                    return [
                        (
                            json.dumps({
                                "ok": True,
                                "result": {
                                    "apps": [
                                        {
                                            "bundle_id": "com.example.app",
                                            "name": "示例应用",
                                            "data_path": "/var/mobile/Containers/Data/1",
                                        }
                                    ]
                                },
                            }, ensure_ascii=False)
                            + "\n"
                        ).encode("utf-8")
                    ]
                if method == "apps.launch":
                    return [
                        (
                            json.dumps({
                                "ok": True,
                                "result": {"pid": 4242, "frontmost_verified": True},
                            })
                            + "\n"
                        ).encode("utf-8")
                    ]
                if method == "apps.terminate":
                    return [b'{"ok":true,"result":{"terminated":true}}\n']
                if method == "location.set":
                    return [
                        (
                            json.dumps({
                                "ok": True,
                                "result": {"simulating": True, "location": reading},
                            })
                            + "\n"
                        ).encode("utf-8")
                    ]
                if method == "location.current":
                    return [(json.dumps({"ok": True, "result": reading}) + "\n").encode("utf-8")]
                if method == "location.clear":
                    return [b'{"ok":true,"result":{"simulating":false}}\n']
            return [b'{"ok":true}\n']

        with uds_endpoint(handler, capabilities=default_upstream_caps(), max_requests=30) as (
            path,
            sent,
            _,
        ):
            client = _standalone_client(path, protocol=PROTOCOL_UPSTREAM_V1)

            # HID commands: no stray color field injected in upstream_v1.
            self.assertTrue(client.tap(100, 200)["ok"])
            self.assertEqual(sent[-1], {"t": "tap", "x": 300, "y": 600, "screen": True})

            self.assertTrue(client.swipe(10, 20, 30, 40, ms=250)["ok"])
            self.assertEqual(
                sent[-1],
                {"t": "swipe", "x1": 30, "y1": 60, "x2": 90, "y2": 120, "ms": 250, "screen": True},
            )

            self.assertTrue(client.key("home")["ok"])
            self.assertEqual(sent[-1], {"t": "key", "name": "home", "screen": True})

            self.assertTrue(client.type_text("hello")["ok"])
            self.assertEqual(sent[-1], {"t": "type", "text": "hello", "screen": False})

            # App RPCs
            apps_resp = client.app_list(filter="user")
            self.assertTrue(apps_resp["ok"])
            self.assertEqual(apps_resp["apps"][0]["data_container"], "/var/mobile/Containers/Data/1")
            self.assertEqual(sent[-1]["method"], "apps.list")
            self.assertEqual(sent[-1]["params"], {"filter": "user"})

            launch_resp = client.launch_app("com.example.app")
            self.assertTrue(launch_resp["ok"])
            self.assertEqual(launch_resp["pid"], 4242)
            self.assertEqual(sent[-1]["method"], "apps.launch")

            term_resp = client.terminate_app("com.example.app")
            self.assertTrue(term_resp["ok"])
            self.assertEqual(sent[-1]["method"], "apps.terminate")

            # Location RPCs
            loc_resp = client.set_location(31.23, 121.47, speed=-1.0, course=-1.0)
            self.assertTrue(loc_resp["ok"])
            self.assertEqual(sent[-1]["method"], "location.set")
            self.assertNotIn("speed", sent[-1]["params"])
            self.assertNotIn("course", sent[-1]["params"])

            cur_resp = client.location_current(timeout_s=5.0)
            self.assertTrue(cur_resp["ok"])
            self.assertAlmostEqual(cur_resp["latitude"], 31.23)

            stop_resp = client.stop_location()
            self.assertTrue(stop_resp["ok"])
            self.assertFalse(stop_resp["simulating"])

        # Invalid hardware key rejected before touching socket.
        client = _standalone_client("/tmp/missing-vmcp.sock", protocol=PROTOCOL_UPSTREAM_V1)
        with self.assertRaises(ValueError):
            client.key("invalid_button")

        # Invalid location coordinates rejected before touching socket.
        with self.assertRaisesRegex(ProtocolError, "location"):
            client.set_location(95.0, 0.0)

        # Unverified app_launch decoded as ok=False with operation_may_continue.
        unverified = decode_upstream(
            "app_launch", {"ok": True, "result": {"pid": 123, "frontmost_verified": False}}
        )
        self.assertFalse(unverified["ok"])
        self.assertTrue(unverified["operation_may_continue"])

    def test_upstream_v1_wait_ready_polls_guest_connected(self) -> None:
        calls = {"n": 0}
        img = make_jpeg_b64(430, 932)

        def caps_provider() -> dict[str, Any]:
            calls["n"] += 1
            return default_upstream_caps(guest_connected=calls["n"] >= 2)

        with uds_endpoint(
            [(json.dumps({"ok": True, "image": img}) + "\n").encode("utf-8")],
            capabilities=caps_provider,
            max_requests=10,
        ) as (path, sent, _):
            client = _standalone_client(path, protocol=PROTOCOL_UPSTREAM_V1)
            resp = client.wait_ready(timeout=2.0)
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["image"], img)
            self.assertGreaterEqual(calls["n"], 2)

        # Timeout when guest stays disconnected.
        with uds_endpoint(
            [],
            capabilities=default_upstream_caps(guest_connected=False),
            max_requests=10,
        ) as (path, _, _):
            client = _standalone_client(path, protocol=PROTOCOL_UPSTREAM_V1)
            with self.assertRaises(NotReady):
                client.wait_ready(timeout=0.3)


if __name__ == "__main__":
    unittest.main()
