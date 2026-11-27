"""Unit tests for vphone_mcp ScreenLock, Control Plane, and standalone coordination."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from typing import Any, Iterator

from vphone_mcp.client import PROTOCOL_LEGACY, PROTOCOL_UPSTREAM_V1, ProtocolError, VPhoneClient
from vphone_mcp.coordination import (
    ControlPlaneUnavailable,
    CoordinationConfigError,
    LocalScreenLock,
    LockRequiredError,
    MODE_CONTROL_PLANE,
    MODE_LOCAL_LOCK,
    MODE_MANAGED_PROXY,
    MODE_STANDALONE,
    ScreenLockBusy,
    ScreenQuarantineMarkerPresent,
    resolve_coordination_config,
)
from tests.test_upstream_v1 import default_upstream_caps, make_jpeg_b64, uds_endpoint


def _unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _MockControlServer:
    """In-process HTTP mock supporting both autophone-control-plane and autophone-run."""

    def __init__(self, *, service: str = "autophone-control-plane", platform_mode: str = "control") -> None:
        self.service = service
        self.platform_mode = platform_mode
        self.health_ok = True
        self.health_status = 200
        self.device_status_code = 200
        self.device_status_body: dict[str, Any] = {
            "device": "rig-a",
            "online": True,
            "lock": {"device": "rig-a", "locked": False},
            "screen_quarantine": None,
        }
        self.active_reservation: dict[str, Any] | None = None
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []
        self.released: list[tuple[str, str]] = []
        self._counter = 0

    def create_handler(self) -> type[BaseHTTPRequestHandler]:
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _send_json(self, code: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _read_body(self) -> dict[str, Any] | None:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return None
                return json.loads(self.rfile.read(length).decode("utf-8"))

            def do_GET(self) -> None:  # noqa: N802
                state.requests.append(("GET", self.path, None))
                if self.path == "/health":
                    self._send_json(
                        state.health_status,
                        {"ok": state.health_ok, "service": state.service},
                    )
                    return
                if self.path == "/api/platform/targets":
                    self._send_json(200, {"mode": state.platform_mode, "targets": []})
                    return
                if self.path.startswith("/devices/") and self.path.endswith("/status"):
                    self._send_json(state.device_status_code, state.device_status_body)
                    return
                self._send_json(404, {"detail": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                body = self._read_body()
                state.requests.append(("POST", self.path, body))
                if self.path == "/api/control/reservations":
                    if state.active_reservation is not None:
                        self._send_json(
                            422,
                            {"detail": f"rig already has an active reservation: {(body or {}).get('rig_id')}"},
                        )
                        return
                    state._counter += 1
                    rsv_id = f"RSV-test-{state._counter}"
                    state.active_reservation = {"reservation_id": rsv_id, **(body or {})}
                    self._send_json(200, {"reservation_id": rsv_id, "status": "active"})
                    return
                if self.path.startswith("/api/control/reservations/") and self.path.endswith("/release"):
                    rsv_id = self.path.split("/")[4]
                    actor = str((body or {}).get("actor") or "")
                    state.released.append((rsv_id, actor))
                    if state.active_reservation and state.active_reservation["reservation_id"] == rsv_id:
                        state.active_reservation = None
                    self._send_json(200, {"reservation_id": rsv_id, "status": "released"})
                    return
                if self.path.startswith("/api/control/device-sessions/") and self.path.endswith("/ops"):
                    if state.device_status_code != 200:
                        self._send_json(
                            state.device_status_code,
                            {"detail": {"code": "device_session_unavailable", "message": "no rig agent"}},
                        )
                        return
                    self._send_json(200, {"ok": True, "result": state.device_status_body})
                    return
                self._send_json(404, {"detail": "not found"})

        return Handler


@contextmanager
def run_mock_control(
    *,
    service: str = "autophone-control-plane",
    platform_mode: str = "control",
) -> Iterator[tuple[str, _MockControlServer]]:
    mock = _MockControlServer(service=service, platform_mode=platform_mode)
    server = HTTPServer(("127.0.0.1", 0), mock.create_handler())
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", mock
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


class TestCoordinationModes(unittest.TestCase):
    def test_mode_auto_detection_and_validation(self) -> None:
        self.assertEqual(resolve_coordination_config({}).mode, MODE_STANDALONE)
        self.assertEqual(
            resolve_coordination_config({
                "AUTOPHONE_MANAGED_DEVICE_AGENT": "1",
                "AUTOPHONE_CONTROL_PLANE_URL": "http://127.0.0.1:9999",
            }).mode,
            MODE_MANAGED_PROXY,
        )
        cfg_local = resolve_coordination_config({
            "AUTOPHONE_STATE_DIR": "/tmp/ap-state",
            "VPHONE_DEVICE": "rig-a",
        })
        self.assertEqual(cfg_local.mode, MODE_LOCAL_LOCK)
        self.assertEqual(cfg_local.device, "rig-a")
        self.assertEqual(cfg_local.lock_dir, Path("/tmp/ap-state").resolve() / "runs" / ".locks")

        cfg_cp = resolve_coordination_config({
            "AUTOPHONE_CONTROL_PLANE_URL": "http://127.0.0.1:8100/",
            "AUTOPHONE_DEVICE": "rig-b",
        })
        self.assertEqual(cfg_cp.mode, MODE_CONTROL_PLANE)
        self.assertEqual(cfg_cp.control_plane_url, "http://127.0.0.1:8100")

        cfg_ws = resolve_coordination_config({
            "AUTOPHONE_CONTROL_WS": "ws://127.0.0.1:8100/api/control/agent/ws",
            "AP_DEVICE": "rig-c",
        })
        self.assertEqual(cfg_ws.mode, MODE_CONTROL_PLANE)
        self.assertEqual(cfg_ws.control_plane_url, "http://127.0.0.1:8100")

        cfg_dsn = resolve_coordination_config({
            "AUTOPHONE_DATABASE_URL": "postgresql://localhost/autophone",
            "VPHONE_DEVICE": "rig-d",
        })
        self.assertEqual(cfg_dsn.mode, MODE_CONTROL_PLANE)
        self.assertEqual(cfg_dsn.control_plane_source, "AUTOPHONE_DATABASE_URL")

        # Control plane without device fails closed.
        with self.assertRaises(CoordinationConfigError):
            resolve_coordination_config({"AUTOPHONE_CONTROL_PLANE_URL": "http://127.0.0.1:8100"})

        # Explicit lock dir without device fails closed.
        with self.assertRaises(CoordinationConfigError):
            resolve_coordination_config({"VPHONE_MCP_LOCK_DIR": "/tmp/locks"})

        # Path traversal device rejected.
        with self.assertRaises(CoordinationConfigError):
            resolve_coordination_config({
                "AUTOPHONE_STATE_DIR": "/tmp/ap-state",
                "VPHONE_DEVICE": "../escape",
            })

    def test_local_screen_lock_acquire_release_contention_quarantine_and_cleanup(self) -> None:
        img = make_jpeg_b64(430, 932)
        with tempfile.TemporaryDirectory(prefix="vmcp-lock-") as state_dir:
            lock_dir = Path(state_dir) / "runs" / ".locks"
            env = {
                "AUTOPHONE_STATE_DIR": state_dir,
                "VPHONE_DEVICE": "rig-a",
                "VPHONE_MCP_LOCK_TTL_S": "15",
            }
            observed_state_during_call: dict[str, Any] = {}

            def handler(msg: dict[str, Any]) -> list[bytes]:
                if msg["t"] == "tap":
                    observed_state_during_call.update(
                        json.loads((lock_dir / "rig-a.json").read_text(encoding="utf-8"))
                    )
                    return [(json.dumps({"ok": True, "image": img}) + "\n").encode("utf-8")]
                return [(json.dumps({"ok": True, "image": img}) + "\n").encode("utf-8")]

            with uds_endpoint(handler, capabilities=default_upstream_caps(), max_requests=20) as (
                sock_path,
                sent,
                _,
            ):
                client = VPhoneClient(
                    socket_path=sock_path,
                    protocol=PROTOCOL_UPSTREAM_V1,
                    env=env,
                )
                # 1. Acquire during tap, verify state file written, verify released after.
                resp = client.tap(100, 200)
                self.assertTrue(resp["ok"])
                self.assertTrue(observed_state_during_call["locked"])
                self.assertEqual(observed_state_during_call["owner_kind"], "run")
                self.assertTrue(
                    str(observed_state_during_call["owner_id"]).startswith(f"vphone-mcp:{os.getpid()}:")
                )
                self.assertIn("expires_at", observed_state_during_call)

                after = json.loads((lock_dir / "rig-a.json").read_text(encoding="utf-8"))
                self.assertFalse(after["locked"])
                self.assertIsNone(after["owner_id"])

                # 2. Contention rejection while a managed run holds flock (zero UDS writes!).
                sent_before = len(sent)
                lock_file = lock_dir / "rig-a.lock"
                with lock_file.open("a+") as run_fh:
                    fcntl.flock(run_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    (lock_dir / "rig-a.json").write_text(
                        json.dumps({
                            "device": "rig-a",
                            "locked": True,
                            "run_id": "RUN-managed-1",
                            "owner_kind": "run",
                            "owner_id": "RUN-managed-1",
                            "pid": os.getpid(),
                        }),
                        encoding="utf-8",
                    )
                    try:
                        with self.assertRaisesRegex(ScreenLockBusy, "RUN-managed-1"):
                            client.tap(50, 50)
                        self.assertEqual(len(sent), sent_before)

                        # Read-only screenshot MUST NOT contend with held ScreenLock.
                        shot = client.screenshot()
                        self.assertTrue(shot["ok"])
                    finally:
                        fcntl.flock(run_fh.fileno(), fcntl.LOCK_UN)

                # 3. Quarantine marker fail-closed rejection (zero UDS writes!).
                sent_before = len(sent)
                q_path = lock_dir / "rig-a.quarantine.json"
                q_path.write_text(
                    json.dumps({
                        "device": "rig-a",
                        "owner_id": "RUN-bad",
                        "at": "2026-11-27T00:00:00Z",
                        "reason": "device_quiesce_unverified",
                        "failures": ["frida detach timeout"],
                        "pid": 999999,
                    }),
                    encoding="utf-8",
                )
                with self.assertRaises(ScreenQuarantineMarkerPresent) as qctx:
                    client.tap(50, 50)
                self.assertEqual(qctx.exception.device, "rig-a")
                self.assertEqual(qctx.exception.path, q_path.resolve())
                self.assertEqual(len(sent), sent_before)
                # Quarantine marker MUST remain untouched.
                self.assertTrue(q_path.exists())
                q_path.unlink()

            # 4. Lock cleanup on UDS error / tool exception.
            with uds_endpoint(
                [b"corrupt-json\n"],
                capabilities=default_upstream_caps(),
            ) as (bad_sock, _, _):
                bad_client = VPhoneClient(
                    socket_path=bad_sock,
                    protocol=PROTOCOL_UPSTREAM_V1,
                    env=env,
                )
                with self.assertRaises(ProtocolError):
                    bad_client.tap(10, 10)

            after_exc = json.loads((lock_dir / "rig-a.json").read_text(encoding="utf-8"))
            self.assertFalse(after_exc["locked"])
            # Confirm flock was genuinely released by acquiring it cleanly.
            lease = LocalScreenLock(lock_dir).acquire_lease(device="rig-a", owner_id="verify-free")
            lease.release()

    def test_control_plane_unreachable_fails_closed_with_zero_uds_writes(self) -> None:
        dead_port = _unused_tcp_port()
        img = make_jpeg_b64(430, 932)
        with uds_endpoint([(json.dumps({"ok": True, "image": img}) + "\n").encode()]) as (
            sock_path,
            sent,
            _,
        ):
            client = VPhoneClient(
                socket_path=sock_path,
                protocol=PROTOCOL_LEGACY,
                env={
                    "AUTOPHONE_CONTROL_PLANE_URL": f"http://127.0.0.1:{dead_port}",
                    "VPHONE_DEVICE": "rig-a",
                    "VPHONE_MCP_HTTP_TIMEOUT_S": "0.5",
                },
            )
            with self.assertRaises(ControlPlaneUnavailable):
                client.tap(10, 20)
            with self.assertRaises(ControlPlaneUnavailable):
                client.screenshot()
            self.assertEqual(sent, [])

    def test_control_plane_http_reservation_status_and_release_lifecycle(self) -> None:
        img = make_jpeg_b64(430, 932)
        with run_mock_control(service="autophone-control-plane") as (cp_url, mock):
            with uds_endpoint(
                [(json.dumps({"ok": True, "image": img}) + "\n").encode("utf-8")],
                capabilities=default_upstream_caps(),
                max_requests=20,
            ) as (sock_path, sent, _):
                env = {
                    "AUTOPHONE_CONTROL_PLANE_URL": cp_url,
                    "VPHONE_DEVICE": "rig-a",
                    "VPHONE_MCP_LOCK_TTL_S": "20",
                }
                client = VPhoneClient(
                    socket_path=sock_path,
                    protocol=PROTOCOL_UPSTREAM_V1,
                    env=env,
                )

                # 1. Normal mutating call: health -> reserve -> device_status -> UDS -> release.
                resp = client.tap(100, 200)
                self.assertTrue(resp["ok"])
                self.assertIsNone(mock.active_reservation)
                self.assertEqual(len(mock.released), 1)
                self.assertEqual(
                    [(m, p) for m, p, _ in mock.requests],
                    [
                        ("GET", "/health"),
                        ("POST", "/api/control/reservations"),
                        ("POST", "/api/control/device-sessions/rig-a/ops"),
                        ("POST", "/api/control/reservations/RSV-test-1/release"),
                    ],
                )

                # 2. Active reservation conflict -> ScreenLockBusy, zero UDS writes.
                sent_before = len(sent)
                mock.active_reservation = {"reservation_id": "RSV-other"}
                with self.assertRaises(ScreenLockBusy):
                    client.tap(100, 200)
                self.assertEqual(len(sent), sent_before)
                # Read-only screenshot still succeeds (no reservation acquired).
                shot = client.screenshot()
                self.assertTrue(shot["ok"])
                mock.active_reservation = None

                # 3. Live Rig locked -> ScreenLockBusy + releases our reservation!
                sent_before = len(sent)
                mock.device_status_body = {
                    "device": "rig-a",
                    "online": True,
                    "lock": {"device": "rig-a", "locked": True, "owner_kind": "run", "owner_id": "RUN-live"},
                    "screen_quarantine": None,
                }
                with self.assertRaisesRegex(ScreenLockBusy, "RUN-live"):
                    client.tap(100, 200)
                self.assertEqual(len(sent), sent_before)
                self.assertIsNone(mock.active_reservation)

                # 4. Live Rig quarantined -> ScreenQuarantineMarkerPresent + releases reservation!
                mock.device_status_body = {
                    "device": "rig-a",
                    "online": True,
                    "lock": {"device": "rig-a", "locked": False},
                    "screen_quarantine": {"device": "rig-a", "path": "/tmp/rig-a.quarantine.json"},
                }
                with self.assertRaises(ScreenQuarantineMarkerPresent):
                    client.tap(100, 200)
                self.assertEqual(len(sent), sent_before)
                self.assertIsNone(mock.active_reservation)

                # 5. Disconnected Rig (503) -> ControlPlaneUnavailable + releases reservation!
                mock.device_status_code = 503
                with self.assertRaises(ControlPlaneUnavailable):
                    client.tap(100, 200)
                self.assertEqual(len(sent), sent_before)
                self.assertIsNone(mock.active_reservation)

    def test_autophone_run_api_url_control_and_local_modes(self) -> None:
        img = make_jpeg_b64(430, 932)
        with run_mock_control(service="autophone-run", platform_mode="local") as (api_url, mock):
            with uds_endpoint(
                [(json.dumps({"ok": True, "image": img}) + "\n").encode("utf-8")],
                max_requests=10,
            ) as (sock_path, sent, _):
                # Local mode without local lock dir + REQUIRE_LOCK=1 fails closed.
                strict_client = VPhoneClient(
                    socket_path=sock_path,
                    protocol=PROTOCOL_LEGACY,
                    env={
                        "AUTOPHONE_API_URL": api_url,
                        "VPHONE_DEVICE": "rig-a",
                        "VPHONE_MCP_REQUIRE_LOCK": "1",
                    },
                )
                with self.assertRaises(LockRequiredError):
                    strict_client.tap(10, 20)
                self.assertEqual(sent, [])

                # With local lock dir layered, acquires local ScreenLock + checks /devices/{device}/status.
                with tempfile.TemporaryDirectory(prefix="vmcp-api-local-") as state_dir:
                    client = VPhoneClient(
                        socket_path=sock_path,
                        protocol=PROTOCOL_LEGACY,
                        env={
                            "AUTOPHONE_API_URL": api_url,
                            "AUTOPHONE_STATE_DIR": state_dir,
                            "VPHONE_DEVICE": "rig-a",
                            "VPHONE_MCP_REQUIRE_LOCK": "1",
                        },
                    )
                    self.assertTrue(client.tap(10, 20)["ok"])
                    self.assertEqual(len(sent), 1)
                    self.assertIn(("GET", "/devices/rig-a/status", None), mock.requests)

    def test_standalone_backward_compatibility_and_require_lock_guard(self) -> None:
        img = make_jpeg_b64(430, 932)
        with uds_endpoint(
            [(json.dumps({"ok": True, "image": img}) + "\n").encode("utf-8")],
            max_requests=10,
        ) as (sock_path, sent, _):
            # Default standalone allows direct VPHONE_SOCK backward-compatibly.
            standalone = VPhoneClient(socket_path=sock_path, protocol=PROTOCOL_LEGACY, env={})
            self.assertEqual(standalone.coordinator.mode, MODE_STANDALONE)
            self.assertTrue(standalone.tap(10, 20)["ok"])
            self.assertEqual(len(sent), 1)

            # Strict mode refuses mutating writes (zero UDS writes), allows read-only screenshot.
            strict = VPhoneClient(
                socket_path=sock_path,
                protocol=PROTOCOL_LEGACY,
                env={"VPHONE_MCP_REQUIRE_LOCK": "1"},
            )
            with self.assertRaises(LockRequiredError):
                strict.tap(10, 20)
            self.assertEqual(len(sent), 1)

            self.assertTrue(strict.screenshot()["ok"])
            self.assertEqual(len(sent), 2)

            # Managed proxy mode passes through without self-contending.
            managed = VPhoneClient(
                socket_path=sock_path,
                protocol=PROTOCOL_LEGACY,
                env={
                    "AUTOPHONE_MANAGED_DEVICE_AGENT": "1",
                    "VPHONE_MCP_REQUIRE_LOCK": "1",
                },
            )
            self.assertEqual(managed.coordinator.mode, MODE_MANAGED_PROXY)
            self.assertTrue(managed.tap(10, 20)["ok"])
            self.assertEqual(len(sent), 3)


if __name__ == "__main__":
    unittest.main()
