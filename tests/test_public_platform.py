"""Platform acceptance uses synthetic devices and no App assets."""
from types import SimpleNamespace

import pytest

from autophone.application.device_session import DeviceSessionOps, DeviceSessionRefused
from autophone.device.lock import ScreenLock
from autophone.device.target import resolve_target
from autophone.location.system import UDSSystemLocationTransport


def test_library_names_select_independent_targets(tmp_path):
    env = {"AUTOPHONE_VPHONE_PROTOCOL": "upstream_v1", "VPHONE_LIBRARY_ROOT": str(tmp_path)}
    for name in ("rig-a", "rig-b"):
        target = resolve_target(environ={**env, "VM_DIR": name})
        assert (target.device, target.sock, target.protocol) == (name, tmp_path / name / "vphone.sock", "upstream_v1")
    assert resolve_target(environ=env).vm_dir == tmp_path / "vm-1"


def test_system_location_uses_selected_socket_and_lock(tmp_path, monkeypatch):
    calls = []
    def request(transport, payload):
        calls.append((transport.sock_path, payload["t"]))
        return {"ok": True}

    monkeypatch.setattr(UDSSystemLocationTransport, "request", request)
    for name in ("rig-a", "rig-b"):
        lock = ScreenLock(root=tmp_path)
        target = {"vm": name, "device": name, "socket": str(tmp_path / name / "vphone.sock")}
        ops = DeviceSessionOps(console=SimpleNamespace(lock=lock), target=lambda: target,
                               screenshot=lambda: b"", status=lambda device: {})
        with pytest.raises(DeviceSessionRefused, match="not served"):
            ops.call("wrong-device", "system_location", {"t": "location_current"})
        with lock.acquire(device=name, run_id="another-run"):
            with pytest.raises(DeviceSessionRefused, match="busy"):
                ops.call(name, "system_location", {"t": "location_current"})
        assert ops.call(name, "system_location", {"t": "location_current"})["ok"]

    assert calls == [(str(tmp_path / name / "vphone.sock"), "location_current") for name in ("rig-a", "rig-b")]


def test_dashboard_location_uses_control_plane_for_selected_rig(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from autophone.adapters.http.app import create_app

    monkeypatch.setattr("autophone.device.lock.default_screen_lock_root", lambda: tmp_path / "locks")

    app = create_app()
    deps = app.state.dependencies
    client = TestClient(app)
    sent = []

    def remote(device, op, payload):
        assert device == "rig-b"
        if op == "location_lock_status":
            return {"locked": False}
        assert op == "system_location"
        sent.append(payload)
        if payload["t"] == "location_source_set":
            return {"ok": True, "generation": "g1", "applied": {"last_delivery_sequence": 1}}
        if payload["t"] == "location_current":
            return {"ok": True, "latitude": 0.0, "longitude": 1.0, "timestamp": "2026-01-01T00:00:00Z"}
        assert payload == {"t": "location_source_stop", "generation": "g1"}
        return {"ok": True, "state": "off"}

    monkeypatch.setattr(deps, "_configured_control_or_http_error", lambda: object())
    monkeypatch.setattr(deps, "RemoteDeviceSessions", lambda url: SimpleNamespace(call=remote))
    monkeypatch.setattr(deps.local_device_sessions, "call", lambda *args: pytest.fail("unexpected local dispatch"))
    spec = {
        "session_id": "platform-fixed", "owner": "platform-test", "device": "rig-b",
        "source": {"source_id": "fixed-b", "kind": "fixed", "coordinate_system": "wgs84"},
        "targets": [{"target_id": "system:rig-b", "kind": "system_core_location", "coordinate_system": "wgs84"}],
    }
    sample = {"producer_sequence": 0, "emitted_wall_at": "2026-01-01T00:00:00Z", "emitted_monotonic_s": 0,
              "coordinate": {"latitude": 0.0, "longitude": 1.0, "coordinate_system": "wgs84"}}
    created = client.post("/api/location/sessions", json={"spec": spec, "initial_sample": sample})
    assert created.status_code == 200, created.text
    assert client.get("/api/location/devices/rig-b/current").json()["reading"]["longitude"] == 1.0
    stopped = client.request("DELETE", "/api/location/sessions/platform-fixed", json={"owner": "platform-test"})
    assert stopped.status_code == 200, stopped.text
    assert [message["t"] for message in sent] == ["location_source_set", "location_current", "location_source_stop"]

    def offline(*args):
        raise DeviceSessionRefused("rig offline", status=503, code="device_session_unavailable")
    monkeypatch.setattr(deps, "RemoteDeviceSessions", lambda url: SimpleNamespace(call=offline))
    assert client.get("/api/location/devices/rig-b/current").status_code == 503


def test_screenshot_tap_and_input_stay_on_selected_vm():
    import base64
    import json
    import tempfile
    from contextlib import ExitStack
    from io import BytesIO
    from pathlib import Path
    from socketserver import StreamRequestHandler, UnixStreamServer
    from threading import Thread

    from PIL import Image
    from autophone.application.device_console import UdsDeviceDriver
    from autophone.device.target import target_scope

    class Guest(StreamRequestHandler):
        def handle(self):
            message = json.loads(self.rfile.readline())
            kind = message["t"]
            if kind == "capabilities":
                reply = {"ok": True, "protocol": "upstream_v1",
                         "commands": dict.fromkeys(("screenshot", "type", "tap"), True)}
            elif kind == "screenshot":
                image = BytesIO()
                color = "white" if self.server.events else "black"
                Image.new("RGB", (430, 932), color).save(image, format="JPEG")
                reply = {"ok": True, "image": base64.b64encode(image.getvalue()).decode()}
            else:
                self.server.events.append(message)
                reply = {"ok": True}
            self.wfile.write(json.dumps(reply).encode() + b"\n")

    with tempfile.TemporaryDirectory(dir="/tmp") as library, ExitStack() as stack:
        targets, servers = [], []
        for name in ("rig-a", "rig-b"):
            target = resolve_target(environ={"VPHONE_LIBRARY_ROOT": library, "VM_DIR": name,
                                             "AUTOPHONE_VPHONE_PROTOCOL": "upstream_v1"})
            Path(target.vm_dir).mkdir()
            server = stack.enter_context(UnixStreamServer(str(target.sock), Guest))
            server.events = []
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            stack.callback(thread.join)
            stack.callback(server.shutdown)
            targets.append(target)
            servers.append(server)

        with target_scope(targets[1]):
            driver = UdsDeviceDriver()
            before = driver.screenshot()
            driver.tap(10, 12)
            driver.type_text("platform")
            after = driver.screenshot()
        assert before[1:] == after[1:] == (430, 932)
        assert before[0] != after[0]
        with target_scope(targets[0]):
            assert UdsDeviceDriver().screenshot() == before
        assert not servers[0].events
        assert [event["t"] for event in servers[1].events] == ["tap", "type"]


def test_platform_target_inventory_fails_closed(monkeypatch, isolated_screen_lock_root):
    from fastapi.testclient import TestClient
    from autophone.adapters.http.app import create_app
    from autophone.application.control_plane import ControlPlaneUnavailable

    app = create_app()
    deps = app.state.dependencies
    client = TestClient(app)
    monkeypatch.setattr(deps, "configured_control_plane", lambda: None)
    assert client.get("/api/platform/targets").json() == {
        "mode": "local", "targets": [{"rig_id": deps.DEFAULT_DEVICE, "status": "local"}],
    }
    rigs = [{"rig_id": "rig-a", "status": "online"}, {"rig_id": "rig-b", "status": "offline"}]
    monkeypatch.setattr(deps, "configured_control_plane", lambda: SimpleNamespace(list_rigs=lambda: rigs))
    monkeypatch.setattr(deps, "_access_connected_devices", lambda: {"rig-a"})
    assert client.get("/api/platform/targets").json() == {"mode": "control", "targets": rigs}

    def channel_unreachable():
        raise ConnectionRefusedError("test channel unavailable")

    monkeypatch.setattr(deps, "_access_connected_devices", channel_unreachable)
    assert client.get("/api/platform/targets").status_code == 503

    def disconnected():
        raise ControlPlaneUnavailable("test control plane is offline")

    monkeypatch.setattr(deps, "configured_control_plane", disconnected)
    monkeypatch.setattr(deps.local_device_sessions, "call", lambda *args: pytest.fail("local fallback"))
    for path in ("/api/platform/targets", "/devices/rig-b/status", "/devices/rig-b/screen.jpg",
                 "/api/location/devices/rig-b/current"):
        assert client.get(path).status_code == 503, path


def test_http_route_accepts_only_catalog_ids(monkeypatch, tmp_path, isolated_screen_lock_root):
    from fastapi.testclient import TestClient
    from autophone.adapters.http.app import create_app

    app = create_app()
    monkeypatch.setattr(app.state.dependencies, "_device_sessions", lambda *_: pytest.fail("unexpected dispatch"))
    spec = {
        "session_id": "route-validation", "owner": "platform-test", "device": "rig-b",
        "source": {"kind": "route", "source_id": "test", "coordinate_system": "gcj02"},
        "targets": [{"kind": "system_core_location", "target_id": "system:rig-b", "coordinate_system": "wgs84"}],
    }
    client = TestClient(app)
    for value in (str(tmp_path / "custom.json"), "custom.json", "../other"):
        spec["source"]["route_id"] = value
        response = client.post("/api/location/sessions", json={"spec": spec})
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "location_contract_invalid"
