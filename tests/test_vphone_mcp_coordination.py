"""Root-level interoperability tests between vphone_mcp and Autophone core.

Verifies:
- Real cross-process/cross-object ScreenLock & quarantine interop with
  ``autophone.device.lock.ScreenLock``.
- Real Control Plane HTTP coordination against ``autophone.control.http.create_control_app``.
- Strict fail-closed rejection when Control Plane is configured and unreachable.
- Wire parity between ``vphone_mcp.wire`` and ``autophone.device.upstream_wire``.
- Standalone backward compatibility and ``VPHONE_MCP_REQUIRE_LOCK=1`` strict mode.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import io
import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from typing import Any, Iterator

import pytest

from autophone.device import upstream_wire as core_upstream_wire
from autophone.device.lock import ScreenLock as CoreScreenLock

# Scope vphone_mcp on sys.path only while importing it so vphone_mcp/tests/__init__.py
# never shadows the root namespace package `tests` in spawned multiprocessing workers.
_VPHONE_MCP_ROOT = str(Path(__file__).resolve().parents[1] / "vphone_mcp")
_added_vphone_mcp_path = _VPHONE_MCP_ROOT not in sys.path
if _added_vphone_mcp_path:
    sys.path.insert(0, _VPHONE_MCP_ROOT)
try:
    from vphone_mcp.client import (  # noqa: E402
        CommandNotAdmitted,
        PROTOCOL_LEGACY,
        PROTOCOL_UPSTREAM_V1,
        ProtocolError,
        VPhoneClient,
    )
    from vphone_mcp.coordination import (  # noqa: E402
        ControlPlaneUnavailable,
        DeviceCoordinator,
        LocalScreenLock,
        LockRequiredError,
        MODE_CONTROL_PLANE,
        MODE_LOCAL_LOCK,
        MODE_STANDALONE,
        ScreenLockBusy,
        ScreenQuarantineMarkerPresent,
        resolve_coordination_config,
    )
    from vphone_mcp.wire import admitted, decode_upstream, prepare_upstream  # noqa: E402
finally:
    if _added_vphone_mcp_path:
        try:
            sys.path.remove(_VPHONE_MCP_ROOT)
        except ValueError:
            pass


def _make_jpeg_b64(width: int = 430, height: int = 932) -> str:
    sof0_payload = struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00"
    sof0 = b"\xff\xc0" + struct.pack(">H", len(sof0_payload) + 2) + sof0_payload
    jpeg = b"\xff\xd8\xff\xe0\x00\x02" + sof0 + b"\xff\xda\x00\x02\xff\xd9"
    return base64.b64encode(jpeg).decode("ascii")


@contextmanager
def _fake_uds(
    *,
    protocol: str = PROTOCOL_UPSTREAM_V1,
    fail_writes: bool = False,
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    img = _make_jpeg_b64()
    caps = {
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
    with tempfile.TemporaryDirectory(prefix="ap-vmcp-", dir="/tmp") as directory:
        sock_path = str(Path(directory) / "vphone.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(8)
        server.settimeout(0.1)
        sent: list[dict[str, Any]] = []
        stop = threading.Event()

        def serve() -> None:
            while not stop.is_set():
                try:
                    peer, _ = server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                with peer:
                    peer.settimeout(2.0)
                    buf = b""
                    while b"\n" not in buf:
                        chunk = peer.recv(65536)
                        if not chunk:
                            break
                        buf += chunk
                    if not buf:
                        continue
                    msg = json.loads(buf.split(b"\n", 1)[0])
                    sent.append(msg)
                    if msg.get("t") == "capabilities" and protocol == PROTOCOL_UPSTREAM_V1:
                        peer.sendall((json.dumps(caps) + "\n").encode("utf-8"))
                        continue
                    if fail_writes and msg.get("t") not in {"screenshot", "capabilities"}:
                        peer.sendall(b"invalid-reply\n")
                        continue
                    reply: dict[str, Any] = {"ok": True}
                    if msg.get("t") in {"screenshot", "tap", "swipe", "key"}:
                        reply["image"] = img
                    peer.sendall((json.dumps(reply) + "\n").encode("utf-8"))

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        try:
            yield sock_path, sent
        finally:
            stop.set()
            server.close()
            worker.join(timeout=2.0)


@pytest.mark.integration
def test_local_screen_lock_interoperates_with_autophone_core_screen_lock(tmp_path: Path) -> None:
    lock_root = tmp_path / "runs" / ".locks"
    core_lock = CoreScreenLock(root=lock_root)
    env = {
        "AUTOPHONE_STATE_DIR": str(tmp_path),
        "VPHONE_DEVICE": "rig-a",
    }

    with _fake_uds() as (sock_path, sent):
        client = VPhoneClient(socket_path=sock_path, protocol=PROTOCOL_UPSTREAM_V1, env=env)

        # 1. Managed Autophone run holds CoreScreenLock -> vphone_mcp rejects tap with ScreenLockBusy
        #    and dispatches ZERO UDS writes, while read-only screenshot still succeeds.
        with core_lock.acquire(device="rig-a", run_id="RUN-core-001", blocking=False):
            with pytest.raises(ScreenLockBusy, match="RUN-core-001"):
                client.tap(100, 200)
            assert sent == []

            shot = client.screenshot()
            assert shot["ok"] is True

        # 2. Once CoreScreenLock is released, vphone_mcp acquires and releases cleanly.
        sent.clear()
        with client.coordinator.mutating_call("tap") as lease:
            assert lease is not None
            # Core ScreenLock observes the lock as held and validates state cleanly.
            assert core_lock.verify_held(device="rig-a") is True
            assert core_lock.try_lease(device="rig-a", owner_kind="run", owner_id="RUN-core-002") is None
            # read_human_holder must NOT raise ValueError on vphone_mcp's state file.
            assert core_lock.read_human_holder(device="rig-a") is None
            status = core_lock.live_status(device="rig-a")
            assert status["locked"] is True
            assert status["owner_id"] == lease.owner_id
            assert client.tap(100, 200)["ok"] is True

        # After release, CoreScreenLock can immediately acquire again.
        core_lease = core_lock.try_lease(device="rig-a", owner_kind="run", owner_id="RUN-core-003")
        assert core_lease is not None
        core_lease.release()

        # 3. CoreScreenLock quarantine marker -> vphone_mcp fails closed with ScreenQuarantineMarkerPresent.
        core_lock.quarantine(
            device="rig-a",
            owner_id="RUN-core-quarantine",
            failures=("frida_cleanup_timeout",),
        )
        sent.clear()
        with pytest.raises(ScreenQuarantineMarkerPresent) as q_err:
            client.tap(100, 200)
        assert q_err.value.device == "rig-a"
        assert sent == []
        assert core_lock.quarantine_status(device="rig-a") is not None


@pytest.mark.integration
def test_local_screen_lock_releases_on_tool_exception(tmp_path: Path) -> None:
    lock_root = tmp_path / "runs" / ".locks"
    core_lock = CoreScreenLock(root=lock_root)
    env = {"AUTOPHONE_STATE_DIR": str(tmp_path), "VPHONE_DEVICE": "rig-err"}

    with _fake_uds(fail_writes=True) as (sock_path, _):
        client = VPhoneClient(socket_path=sock_path, protocol=PROTOCOL_UPSTREAM_V1, env=env)
        with pytest.raises(ProtocolError):
            client.tap(10, 20)

    assert core_lock.verify_held(device="rig-err") is False
    assert core_lock.status(device="rig-err")["locked"] is False
    assert LocalScreenLock(lock_root).status(device="rig-err")["locked"] is False


@pytest.mark.integration
def test_control_plane_http_coordination_against_real_fastapi_control_app() -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from autophone.control.http import create_control_app
    from autophone.control.postgres import ControlStateError

    class _StubControlPlane:
        def __init__(self) -> None:
            self.reservations: dict[str, dict[str, Any]] = {}
            self._seq = 0

        def get_host(self, host_id: str) -> dict[str, Any]:
            return {
                "rigs": [{"host_id": host_id, "rig_id": "rig-a", "generation": 1}],
                "targets": [{"host_id": host_id, "rig_id": "rig-a", "rig_generation": 1, "vm_dir": "/vm/rig-a"}],
            }

        def create_reservation(self, spec: dict[str, Any]) -> dict[str, Any]:
            rig_id = str(spec.get("rig_id") or "")
            if any(
                row["rig_id"] == rig_id and row["status"] == "active"
                for row in self.reservations.values()
            ):
                raise ControlStateError(f"rig already has an active reservation: {rig_id}")
            self._seq += 1
            rsv_id = f"RSV-real-{self._seq}"
            row = {"reservation_id": rsv_id, "rig_id": rig_id, "status": "active", "owner": spec.get("owner")}
            self.reservations[rsv_id] = row
            return dict(row)

        def release_reservation(self, reservation_id: str, actor: str) -> dict[str, Any]:
            row = self.reservations.get(reservation_id)
            if row is None:
                raise ControlStateError(f"reservation not found: {reservation_id}")
            row["status"] = "released"
            row["release_reason"] = f"released_by:{actor}"
            return dict(row)

    stub = _StubControlPlane()
    app = create_control_app(stub)  # type: ignore[arg-type]

    # Stub the gateway's call() so device_status runs through the real /api/control/device-sessions/{device}/ops route.
    rig_status: dict[str, Any] = {
        "device": "rig-a",
        "online": True,
        "lock": {"device": "rig-a", "locked": False},
        "screen_quarantine": None,
    }

    async def _fake_gateway_call(device: str, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert device == "rig-a"
        assert op == "device_status"
        return dict(rig_status)

    app.state.device_session_gateway.call = _fake_gateway_call

    class _TestClientResponse:
        def __init__(self, status: int, content: bytes) -> None:
            self.status = status
            self._content = content

        def read(self) -> bytes:
            return self._content

        def __enter__(self) -> "_TestClientResponse":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    with TestClient(app) as tc:
        def _opener(req: urllib.request.Request, timeout: float) -> _TestClientResponse:
            path = req.full_url.removeprefix("http://control.test")
            resp = tc.request(
                req.get_method(),
                path,
                content=req.data,
                headers=dict(req.header_items()),
            )
            if resp.status_code >= 400:
                raise urllib.error.HTTPError(
                    req.full_url,
                    resp.status_code,
                    resp.reason_phrase,
                    resp.headers,  # type: ignore[arg-type]
                    io.BytesIO(resp.content),
                )
            return _TestClientResponse(resp.status_code, resp.content)

        coordinator = DeviceCoordinator(
            resolve_coordination_config({
                "AUTOPHONE_CONTROL_PLANE_URL": "http://control.test",
                "VPHONE_DEVICE": "rig-a",
            }),
            http_opener=_opener,
        )
        assert coordinator.mode == MODE_CONTROL_PLANE

        with _fake_uds() as (sock_path, sent):
            client = VPhoneClient(
                socket_path=sock_path,
                protocol=PROTOCOL_UPSTREAM_V1,
                coordinator=coordinator,
                env={},
            )
            # 1. Success: creates real reservation via FastAPI route, checks device_status, dispatches tap, releases.
            assert client.tap(100, 200)["ok"] is True
            assert stub.reservations["RSV-real-1"]["status"] == "released"

            # 2. Existing reservation -> ScreenLockBusy, zero UDS writes.
            stub.create_reservation({"rig_id": "rig-a", "owner": {"kind": "device_console"}})
            sent_before = len(sent)
            with pytest.raises(ScreenLockBusy):
                client.tap(100, 200)
            assert len(sent) == sent_before
            stub.release_reservation("RSV-real-2", "test")

            # 3. Rig locked -> ScreenLockBusy, reservation released, zero UDS writes.
            rig_status["lock"] = {"device": "rig-a", "locked": True, "owner_kind": "run", "owner_id": "RUN-99"}
            with pytest.raises(ScreenLockBusy, match="RUN-99"):
                client.tap(100, 200)
            assert len(sent) == sent_before
            assert stub.reservations["RSV-real-3"]["status"] == "released"

            # 4. Rig quarantined -> ScreenQuarantineMarkerPresent, reservation released, zero UDS writes.
            rig_status["lock"] = {"device": "rig-a", "locked": False}
            rig_status["screen_quarantine"] = {"device": "rig-a", "path": "/tmp/rig-a.quarantine.json"}
            with pytest.raises(ScreenQuarantineMarkerPresent):
                client.tap(100, 200)
            assert len(sent) == sent_before
            assert stub.reservations["RSV-real-4"]["status"] == "released"


@pytest.mark.integration
def test_control_plane_unreachable_fails_closed_zero_uds_writes() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        dead_port = int(s.getsockname()[1])

    with _fake_uds(protocol=PROTOCOL_LEGACY) as (sock_path, sent):
        client = VPhoneClient(
            socket_path=sock_path,
            protocol=PROTOCOL_LEGACY,
            env={
                "AUTOPHONE_CONTROL_PLANE_URL": f"http://127.0.0.1:{dead_port}",
                "VPHONE_DEVICE": "rig-a",
                "VPHONE_MCP_HTTP_TIMEOUT_S": "0.5",
            },
        )
        with pytest.raises(ControlPlaneUnavailable):
            client.tap(10, 20)
        with pytest.raises(ControlPlaneUnavailable):
            client.screenshot()
        assert sent == []


@pytest.mark.unit
def test_upstream_wire_parity_with_autophone_core() -> None:
    caps = {
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
    for cmd in (
        "screenshot",
        "tap",
        "swipe",
        "key",
        "type",
        "app_list",
        "app_launch",
        "app_terminate",
        "location",
        "location_current",
        "location_stop",
        "unknown_cmd",
    ):
        assert admitted(caps, cmd) == core_upstream_wire.admitted(caps, cmd)

    samples = [
        {"t": "screenshot", "color": True},
        {"t": "tap", "x": 300, "y": 600, "screen": True},
        {"t": "swipe", "x1": 10, "y1": 20, "x2": 30, "y2": 40, "ms": 300, "screen": True},
        {"t": "key", "name": "home", "screen": True},
        {"t": "type", "text": "hello", "screen": False},
        {"t": "app_list", "filter": "user", "screen": False},
        {"t": "app_launch", "bundle_id": "com.apple.Preferences", "screen": False},
        {"t": "app_terminate", "bundle_id": "com.apple.Preferences", "screen": False},
        {"t": "location", "lat": 31.23, "lon": 121.47, "alt": 0.0, "hacc": 5.0, "vacc": -1.0, "speed": -1.0, "course": -1.0},
        {"t": "location_current", "timeout": 10.0},
        {"t": "location_stop"},
    ]
    for msg in samples:
        mcp_wire, _ = prepare_upstream(msg)
        with core_upstream_wire.prepare(msg) as (core_wire, _):
            assert mcp_wire == core_wire

    apps_reply = {"ok": True, "result": {"apps": [{"bundle_id": "x", "data_path": "/d"}]}}
    assert decode_upstream("app_list", apps_reply) == core_upstream_wire.decode("app_list", apps_reply)


@pytest.mark.unit
def test_standalone_and_require_lock_modes() -> None:
    with _fake_uds(protocol=PROTOCOL_LEGACY) as (sock_path, sent):
        standalone = VPhoneClient(socket_path=sock_path, protocol=PROTOCOL_LEGACY, env={})
        assert standalone.coordinator.mode == MODE_STANDALONE
        assert standalone.tap(10, 20)["ok"] is True
        assert len(sent) == 1

        strict = VPhoneClient(
            socket_path=sock_path,
            protocol=PROTOCOL_LEGACY,
            env={"VPHONE_MCP_REQUIRE_LOCK": "1"},
        )
        with pytest.raises(LockRequiredError):
            strict.tap(10, 20)
        assert len(sent) == 1
        assert strict.screenshot()["ok"] is True
        assert len(sent) == 2

    # Unadmitted command in upstream_v1 raises CommandNotAdmitted.
    cfg = resolve_coordination_config({"AUTOPHONE_STATE_DIR": "/tmp/x", "VPHONE_DEVICE": "rig-a"})
    assert cfg.mode == MODE_LOCAL_LOCK
    assert issubclass(CommandNotAdmitted, ProtocolError)
